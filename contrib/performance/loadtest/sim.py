# -*- test-case-name: contrib.performance.loadtest.test_sim -*-
##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
##
from __future__ import print_function

import sys

if "twisted.internet.reactor" not in sys.modules:
    try:
        from twisted.internet import kqreactor
        kqreactor.install()
    except ImportError:
        from twisted.internet import epollreactor
        epollreactor.install()

from collections import namedtuple, defaultdict
from os import environ, mkdir
from os.path import isdir
from plistlib import readPlist
from random import Random
from sys import argv, stdout
from urlparse import urlsplit
from xml.parsers.expat import ExpatError
import itertools
import json
import shutil
import socket

from twisted.python import context
from twisted.python.filepath import FilePath
from twisted.python.log import startLogging, addObserver, removeObserver, msg
from twisted.python.usage import UsageError, Options
from twisted.python.reflect import namedAny

from twisted.application.service import Service
from twisted.application.service import MultiService

from twisted.internet.defer import Deferred
from twisted.internet.defer import gatherResults
from twisted.internet.defer import inlineCallbacks
from twisted.internet.protocol import ProcessProtocol

from twisted.web.server import Site

from contrib.performance.loadtest.ical import OS_X_10_6
from contrib.performance.loadtest.profiles import Eventer, Inviter, Accepter
from contrib.performance.loadtest.population import (
    Populator, ProfileType, ClientType, PopulationParameters, SmoothRampUp,
    CalendarClientSimulator)
from contrib.performance.loadtest.webadmin import LoadSimAdminResource

from contrib.performance.loadtest.amphub import AMPHub


class _DirectoryRecord(object):

    def __init__(self, uid, password, commonName, email, guid, podID="PodA"):
        self.uid = uid
        self.password = password
        self.commonName = commonName
        self.email = email
        self.guid = guid
        self.podID = podID

    def __repr__(self):
        return "user='{}'".format(self.uid)


def safeDivision(value, total, factor=1):
    return value * factor / total if total else 0


def generateRecords(
    count, uidPattern="user%d", passwordPattern="user%d",
    namePattern="User %d", emailPattern="user%d@example.com",
    guidPattern="user%d", podID="PodA",
):
    for i in xrange(count):
        i += 1
        uid = uidPattern % (i,)
        password = passwordPattern % (i,)
        name = namePattern % (i,)
        email = emailPattern % (i,)
        guid = guidPattern % (i,)
        yield _DirectoryRecord(uid, password, name, email, guid, podID)


def recordsFromCSVFile(path, interleavePods):
    if path:
        pathObj = FilePath(path)
    else:
        pathObj = FilePath(__file__).sibling("accounts.csv")
    records = [
        _DirectoryRecord(*line.decode('utf-8').split(u','))
        for line
        in pathObj.getContent().splitlines()
    ]

    if interleavePods:
        # For Pods we re-order the record list so that we alternate between the pods
        recordsByPod = defaultdict(list)
        for record in records:
            recordsByPod[record.podID].append(record)

        records = []
        for items in itertools.izip(*[recordsByPod[k] for k in sorted(recordsByPod.keys())]):
            records.extend(items)

    return records


class LagTrackingReactor(object):
    """
    This reactor wraps another reactor and proxies all attribute
    access (including method calls).  It only changes the behavior of
    L{IReactorTime.callLater} to insert a C{"lag"} key into the
    context which delayed function calls are invoked with.  This key
    has a float value which gives the difference in time between when
    the call was original scheduled and when the call actually took
    place.
    """

    def __init__(self, reactor):
        self._reactor = reactor

    def __getattr__(self, name):
        return getattr(self._reactor, name)

    def callLater(self, delay, function, *args, **kwargs):
        expected = self._reactor.seconds() + delay

        def modifyContext():
            now = self._reactor.seconds()
            context.call({'lag': now - expected}, function, *args, **kwargs)
        return self._reactor.callLater(delay, modifyContext)


class SimOptions(Options):
    """
    Command line configuration options for the load simulator.
    """
    config = None
    _defaultConfig = FilePath(__file__).sibling("config.plist")
    _defaultClients = FilePath(__file__).sibling("clients.plist")

    optParameters = [
        ("runtime", "t", None,
         "Specify the limit (seconds) on the time to run the simulation.",
         int),
        ("config", None, _defaultConfig,
         "Configuration plist file name from which to read simulation parameters.",
         FilePath),
        ("clients", None, _defaultClients,
         "Configuration plist file name from which to read client parameters.",
         FilePath),
    ]

    def opt_logfile(self, filename):
        """
        Enable normal logging to some file.  - for stdout.
        """
        if filename == "-":
            fObj = stdout
        else:
            fObj = file(filename, "a")
        startLogging(fObj, setStdout=False)

    def opt_debug(self):
        """
        Enable Deferred and Failure debugging.
        """
        self.opt_debug_deferred()
        self.opt_debug_failure()

    def opt_debug_deferred(self):
        """
        Enable Deferred debugging.
        """
        from twisted.internet.defer import setDebugging
        setDebugging(True)

    def opt_debug_failure(self):
        """
        Enable Failure debugging.
        """
        from twisted.python.failure import startDebugMode
        startDebugMode()

    def postOptions(self):
        try:
            configFile = self['config'].open()
        except IOError as e:
            raise UsageError("--config %s: %s" % (
                self['config'].path, e.strerror))
        try:
            try:
                self.config = readPlist(configFile)
            except ExpatError as e:
                raise UsageError("--config %s: %s" % (self['config'].path, e))
        finally:
            configFile.close()

        try:
            clientFile = self['clients'].open()
        except IOError as e:
            raise UsageError("--clients %s: %s" % (
                self['clients'].path, e.strerror))
        try:
            try:
                client_config = readPlist(clientFile)
                self.config["clients"] = client_config["clients"]
                if "arrivalInterval" in client_config:
                    self.config["arrival"]["params"]["interval"] = client_config["arrivalInterval"]
            except ExpatError as e:
                raise UsageError("--clients %s: %s" % (self['clients'].path, e))
        finally:
            clientFile.close()


Arrival = namedtuple('Arrival', 'factory parameters')


class LoadSimulator(object):
    """
    A L{LoadSimulator} simulates some configuration of calendar
    clients.

    @type server: C{str}
    @type arrival: L{Arrival}
    @type parameters: L{PopulationParameters}

    @ivar records: A C{list} of L{_DirectoryRecord} instances giving
        user information about the accounts on the server being put
        under load.
    """

    def __init__(self, servers, principalPathTemplate, webadminPort, serializationPath, arrival, parameters, observers=None,
                 records=None, reactor=None, runtime=None, workers=None,
                 configTemplate=None, workerID=None, workerCount=1):
        if reactor is None:
            from twisted.internet import reactor
        self.servers = servers
        self.principalPathTemplate = principalPathTemplate
        self.webadminPort = webadminPort
        self.serializationPath = serializationPath
        self.arrival = arrival
        self.parameters = parameters
        self.observers = observers
        self.reporter = None
        self.records = records
        self.reactor = LagTrackingReactor(reactor)
        self.runtime = runtime
        self.workers = workers
        self.configTemplate = configTemplate
        self.workerID = workerID
        self.workerCount = workerCount

    @classmethod
    def fromCommandLine(cls, args=None, output=stdout):
        if args is None:
            args = argv[1:]

        options = SimOptions()
        try:
            options.parseOptions(args)
        except UsageError as e:
            raise SystemExit(str(e))

        return cls.fromConfig(options.config, options['runtime'], output)

    @classmethod
    def fromConfig(cls, config, runtime=None, output=stdout, reactor=None):
        """
        Create a L{LoadSimulator} from a parsed instance of a configuration
        property list.
        """

        servers = config.get('servers')

        workers = config.get("workers")
        if workers is None:
            # Client / place where the simulator actually runs configuration
            workerID = config.get("workerID", 0)
            workerCount = config.get("workerCount", 1)
            configTemplate = None
            principalPathTemplate = config.get('principalPathTemplate', '/principals/users/%s/')
            serializationPath = None

            if 'clientDataSerialization' in config:
                serializationPath = config['clientDataSerialization']['Path']
                if not config['clientDataSerialization']['UseOldData']:
                    if isdir(serializationPath):
                        shutil.rmtree(serializationPath)
                serializationPath = config['clientDataSerialization']['Path']
                if not isdir(serializationPath):
                    try:
                        mkdir(serializationPath)
                    except OSError:
                        print("Unable to create client data serialization directory: %s" % (serializationPath))
                        print("Please consult the clientDataSerialization stanza of contrib/performance/loadtest/config.plist")
                        raise

            if 'arrival' in config:
                arrival = Arrival(
                    namedAny(config['arrival']['factory']),
                    config['arrival']['params'])
            else:
                arrival = Arrival(
                    SmoothRampUp, dict(groups=10, groupSize=1, interval=3))

            parameters = PopulationParameters()
            if 'clients' in config:
                for clientConfig in config['clients']:
                    parameters.addClient(
                        clientConfig["weight"],
                        ClientType(
                            namedAny(clientConfig["software"]),
                            cls._convertParams(clientConfig["params"]),
                            [
                                ProfileType(
                                    namedAny(profile["class"]),
                                    cls._convertParams(profile["params"])
                                ) for profile in clientConfig["profiles"]
                            ]))
            if not parameters.clients:
                parameters.addClient(1,
                                     ClientType(OS_X_10_6, {},
                                                [Eventer, Inviter, Accepter]))
        else:
            # Manager / observer process.
            principalPathTemplate = ''
            serializationPath = None
            arrival = None
            parameters = None
            workerID = 0
            configTemplate = config
            workerCount = 1

        webadminPort = None
        if 'webadmin' in config:
            if config['webadmin']['enabled']:
                webadminPort = config['webadmin']['HTTPPort']

        observers = []
        if 'observers' in config:
            for observer in config['observers']:
                observerName = observer["type"]
                observerParams = observer["params"]
                observers.append(namedAny(observerName)(**observerParams))

        records = []
        if 'accounts' in config:
            loader = config['accounts']['loader']
            params = config['accounts']['params']
            records.extend(namedAny(loader)(**params))
            records = cls.filterRecords(records, servers)
            output.write("Loaded {0} accounts.\n".format(len(records)))

        return cls(
            servers,
            principalPathTemplate,
            webadminPort,
            serializationPath,
            arrival,
            parameters,
            observers=observers,
            records=records,
            runtime=runtime,
            reactor=reactor,
            workers=workers,
            configTemplate=configTemplate,
            workerID=workerID,
            workerCount=workerCount,
        )

    @classmethod
    def _convertParams(cls, params):
        """
        Find parameter values which should be more structured than plistlib is
        capable of constructing and replace them with the more structured form.

        Specifically, find keys that end with C{"Distribution"} and convert
        them into some kind of distribution object using the associated
        dictionary of keyword arguments.
        """
        for k, v in params.iteritems():
            if k.endswith('Distribution'):
                params[k] = cls._convertDistribution(v)
        return params

    @classmethod
    def _convertDistribution(cls, value):
        """
        Construct and return a new distribution object using the type and
        params specified by C{value}.
        """
        return namedAny(value['type'])(**value['params'])

    @classmethod
    def filterRecords(cls, records, servers):
        """
        Remove records that do not correspond to an enabled pod.

        @param records: list of records to process
        @type records: L{list}
        @param servers: dictionary of servers
        @type servers: L{dict}
        """

        # Get all enabled pod ids
        enabled = set(filter(lambda podID: servers[podID]["enabled"], servers.keys()))
        return filter(lambda record: record.podID in enabled, records)

    @classmethod
    def main(cls, args=None):
        simulator = cls.fromCommandLine(args)
        exitCode = simulator.run()
        print("Exit code: {}".format(exitCode))
        raise SystemExit(exitCode)

    def createSimulator(self):
        populator = Populator(Random())
        return CalendarClientSimulator(
            self.records,
            populator,
            Random(),
            self.parameters,
            self.reactor,
            self.servers,
            self.principalPathTemplate,
            self.serializationPath,
            self.workerID,
            self.workerCount,
        )

    def createArrivalPolicy(self):
        return self.arrival.factory(self.reactor, **self.arrival.parameters)

    def serviceClasses(self):
        """
        Return a list of L{SimService} subclasses for C{attachServices} to
        instantiate and attach to the reactor.
        """
        if self.workers is not None:
            return [
                ObserverService,
                WorkerSpawnerService,
                ReporterService,
            ]
        return [
            ObserverService,
            SimulatorService,
            ReporterService,
        ]

    def attachServices(self, output):
        self.ms = MultiService()
        for svcclass in self.serviceClasses():
            svcclass(self, output).setServiceParent(self.ms)
        attachService(self.reactor, self, self.ms)
        self.startAmpHub()

    def startAmpHub(self):
        hostsAndPorts = []
        for serverInfo in self.servers.itervalues():
            if serverInfo["enabled"]:
                for host in serverInfo.get("ampPushHosts", []):
                    hostsAndPorts.append((host, serverInfo["ampPushPort"]))
        if hostsAndPorts:
            AMPHub.start(hostsAndPorts)

    def run(self, output=stdout):
        self.attachServices(output)
        if self.runtime is not None:
            self.reactor.callLater(self.runtime, self.stopAndReport)
        if self.webadminPort:
            self.reactor.listenTCP(self.webadminPort, Site(LoadSimAdminResource(self)))
        self.reactor.run()

        # Return code to indicate pass or fail
        return 0 if all([len(obs.failures()) == 0 for obs in self.observers]) else 1

    def stop(self):
        if self.ms.running:
            self.updateStats()
            self.ms.stopService()
            self.reactor.callLater(5, self.stopAndReport)

    def shutdown(self):
        if self.ms.running:
            self.updateStats()
            return self.ms.stopService()

    def updateStats(self):
        """
        Capture server stats and stop.
        """

        for podID, server in self.servers.items():
            if server["enabled"] and server["stats"]["enabled"]:
                _ignore_scheme, hostname, _ignore_path, _ignore_query, _ignore_fragment = urlsplit(server["uri"])
                data = self.readStatsSock((hostname.split(":")[0], server["stats"]["Port"],), True)
                if "Failed" not in data:
                    data = data["stats"]["5m"] if "stats" in data else data["5 Minutes"]
                    result = (
                        safeDivision(float(data["requests"]), 5 * 60),
                        safeDivision(data["t"], data["requests"]),
                        safeDivision(float(data["slots"]), data["requests"]),
                        safeDivision(data["cpu"], data["requests"]),
                    )
                    msg(type="sim-expired", podID=podID, reason=result)

    def stopAndReport(self):
        """
        Runtime has expired - capture server stats and stop.
        """

        self.updateStats()
        self.reactor.stop()

    def readStatsSock(self, sockname, useTCP):
        try:
            s = socket.socket(socket.AF_INET if useTCP else socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sockname)
            s.sendall('["stats"]' + "\r\n")
            data = ""
            while not data.endswith("\n"):
                d = s.recv(1024)
                if d:
                    data += d
                else:
                    break
            s.close()
            data = json.loads(data)
        except socket.error:
            data = {"Failed": "Unable to read statistics from server: %s" % (sockname,)}
        except ValueError:
            data = {"Failed": "Unable to parse statistics from server: %s" % (sockname,)}
        data["Server"] = sockname
        return data


def attachService(reactor, loadsim, service):
    """
    Attach a given L{IService} provider to the given L{IReactorCore}; cause it
    to be started when the reactor starts, and stopped when the reactor stops.
    """
    reactor.callWhenRunning(service.startService)
    reactor.addSystemEventTrigger('before', 'shutdown', loadsim.shutdown)


class SimService(Service, object):
    """
    Base class for services associated with the L{LoadSimulator}.
    """

    def __init__(self, loadsim, output):
        super(SimService, self).__init__()
        self.loadsim = loadsim
        self.output = output


class ObserverService(SimService):
    """
    A service that adds and removes a L{LoadSimulator}'s set of observers at
    start and stop time.
    """

    def startService(self):
        """
        Start observing.
        """
        super(ObserverService, self).startService()
        for obs in self.loadsim.observers:
            addObserver(obs.observe)

    def stopService(self):
        super(ObserverService, self).stopService()
        for obs in self.loadsim.observers:
            removeObserver(obs.observe)


class SimulatorService(SimService):
    """
    A service that starts the L{CalendarClientSimulator} associated with the
    L{LoadSimulator} and stops it at shutdown.
    """

    def startService(self):
        super(SimulatorService, self).startService()
        self.clientsim = self.loadsim.createSimulator()
        arrivalPolicy = self.loadsim.createArrivalPolicy()
        arrivalPolicy.run(self.clientsim)

    @inlineCallbacks
    def stopService(self):
        yield super(SimulatorService, self).stopService()
        yield self.clientsim.stop()


class ReporterService(SimService):
    """
    A service which reports all the results from all the observers on a load
    simulator when it is stopped.
    """

    def startService(self):
        """
        Start observing.
        """
        super(ReporterService, self).startService()
        self.loadsim.reporter = self

    def stopService(self):
        """
        Emit the report to the specified output file.
        """
        super(ReporterService, self).stopService()
        self.generateReport(self.output)

    def generateReport(self, output):
        """
        Emit the report to the specified output file.
        """
        failures = []
        for obs in self.loadsim.observers:
            obs.report(output)
            failures.extend(obs.failures())
        if failures:
            output.write('\n*** FAIL\n')
            output.write('\n'.join(failures))
            output.write('\n')
        else:
            output.write('\n*** PASS\n')


class ProcessProtocolBridge(ProcessProtocol):

    def __init__(self, spawner, proto):
        self.spawner = spawner
        self.proto = proto
        self.deferred = Deferred()

    def connectionMade(self):
        self.transport.getPeer = self.getPeer
        self.transport.getHost = self.getHost
        self.proto.makeConnection(self.transport)

    def getPeer(self):
        return "Peer:PID:" + str(self.transport.pid)

    def getHost(self):
        return "Host:PID:" + str(self.transport.pid)

    def outReceived(self, data):
        self.proto.dataReceived(data)

    def errReceived(self, error):
        msg("stderr received from " + str(self.transport.pid))
        msg("    " + repr(error))

    def processEnded(self, reason):
        self.proto.connectionLost(reason)
        self.deferred.callback(None)
        self.spawner.bridges.remove(self)


class WorkerSpawnerService(SimService):

    def startService(self):
        from contrib.performance.loadtest.ampsim import Manager
        super(WorkerSpawnerService, self).startService()
        self.bridges = []
        for workerID, worker in enumerate(self.loadsim.workers):
            bridge = ProcessProtocolBridge(
                self, Manager(self.loadsim, workerID, len(self.loadsim.workers),
                              self.output)
            )
            self.bridges.append(bridge)
            sh = '/bin/sh'
            self.loadsim.reactor.spawnProcess(
                bridge, sh, [sh, "-c", worker], env=environ
            )

    def stopService(self):
        TERMINATE_TIMEOUT = 30.0

        def killThemAll(name):
            for bridge in self.bridges:
                bridge.transport.signalProcess(name)
        killThemAll("TERM")
        self.loadsim.reactor.callLater(TERMINATE_TIMEOUT, killThemAll, "KILL")
        return gatherResults([bridge.deferred for bridge in self.bridges])


main = LoadSimulator.main

if __name__ == '__main__':
    main()
