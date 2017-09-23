# -*- test-case-name: contrib.performance.loadtest.test_population -*-
##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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
from __future__ import division

"""
Tools for generating a population of CalendarServer users based on
certain usage parameters.
"""

from tempfile import mkdtemp
from itertools import izip
from datetime import datetime
from urllib2 import HTTPBasicAuthHandler
from urllib2 import HTTPDigestAuthHandler
from urllib2 import HTTPPasswordMgrWithDefaultRealm
import collections
import json
import os

from twisted.internet.defer import DeferredList
from twisted.python.failure import Failure
from twisted.python.filepath import FilePath
from twisted.python.util import FancyEqMixin
from twisted.python.log import msg, err

from twistedcaldav.timezones import TimezoneCache

from contrib.performance.stats import mean, median, stddev, mad
from contrib.performance.loadtest.trafficlogger import loggedReactor
from contrib.performance.loadtest.logger import SummarizingMixin
from contrib.performance.loadtest.ical import OS_X_10_6, RequestLogger
from contrib.performance.loadtest.profiles import Eventer, Inviter, Accepter


class ProfileType(object, FancyEqMixin):
    """
    @ivar profileType: A L{ProfileBase} subclass, or an L{ICalendarUserProfile}
        implementation.

    @ivar params: A C{dict} which will be passed to C{profileType} as keyword
        arguments to create a new profile instance.
    """
    compareAttributes = ("profileType", "params")

    def __init__(self, profileType, params):
        self.profileType = profileType
        self.params = params

    def __call__(self, reactor, simulator, client, number):
        return self.profileType(reactor, simulator, client, number, **self.params)


class ClientType(object, FancyEqMixin):
    """
    @ivar clientType: An L{ICalendarClient} implementation
    @ivar profileTypes: A list of L{ProfileType} instances
    """
    compareAttributes = ("clientType", "profileTypes")

    def __init__(self, clientType, clientParams, profileTypes):
        self.clientType = clientType
        self.clientParams = clientParams
        self.profileTypes = profileTypes

    def new(self, reactor, server, principalPathTemplate, serializationPath, userRecord, authInfo, instanceNumber):
        """
        Create a new instance of this client type.
        """
        return self.clientType(
            reactor, server, principalPathTemplate,
            serializationPath, userRecord, authInfo, instanceNumber,
            **self.clientParams
        )


class PopulationParameters(object, FancyEqMixin):
    """
    Descriptive statistics about a population of Calendar Server users.
    """
    compareAttributes = ("clients",)

    def __init__(self):
        self.clients = []

    def addClient(self, weight, clientType):
        """
        Add another type of client to these parameters.

        @param weight: A C{int} giving the weight of this client type.
            The higher the weight, the more frequently a client of
            this type will show up in the population described by
            these parameters.

        @param clientType: A L{ClientType} instance describing the
            type of client to add.
        """
        self.clients.append((weight, clientType))

    def clientTypes(self):
        """
        Return a list of two-tuples giving the weights and types of
        clients in the population.
        """
        return self.clients


class Populator(object):
    """
    @ivar userPattern: A C{str} giving a formatting pattern to use to
        construct usernames.  The string will be interpolated with a
        single integer, the incrementing counter of how many users
        have thus far been "used".

    @ivar passwordPattern: Similar to C{userPattern}, but for
        passwords.
    """

    def __init__(self, random):
        self._random = random

    def _cycle(self, elements):
        while True:
            for (weight, value) in elements:
                for _ignore_i in range(weight):
                    yield value

    def populate(self, parameters):
        """
        Generate individuals such as might be randomly selected from a
        population with the given parameters.

        @type parameters: L{PopulationParameters}
        @rtype: generator of L{ClientType} instances
        """
        for (clientType,) in izip(self._cycle(parameters.clientTypes())):
            yield clientType


class CalendarClientSimulator(object):

    def __init__(self, records, populator, random, parameters, reactor, servers,
                 principalPathTemplate, serializationPath, workerIndex=0, workerCount=1):
        self._records = records
        self.populator = populator
        self._random = random
        self.reactor = reactor
        self.servers = servers
        self.principalPathTemplate = principalPathTemplate
        self.serializationPath = serializationPath
        self._pop = self.populator.populate(parameters)
        self._user = 0
        self._stopped = False
        self.workerIndex = workerIndex
        self.workerCount = workerCount
        self.clients = []

        TimezoneCache.create()

    def getUserRecord(self, index):
        return self._records[index]

    def getRandomUserRecord(self, besides=None):
        count = len(self._records)

        if count == 0:
            # No records!
            return None

        if count == 1 and besides == 0:
            # There is only one item and caller doesn't want it!
            return None

        for _i in xrange(100):
            # Try to find one that is not "besides"
            n = self._random.randint(0, count - 1)
            if besides != n:
                # Got it.
                break
        else:
            # Give up
            return None
        return self._records[n]

    def _nextUserNumber(self):
        result = self._user
        self._user += 1
        return result

    def _createUser(self, number):
        record = self._records[number]
        user = record.uid
        authBasic = HTTPBasicAuthHandler(password_mgr=HTTPPasswordMgrWithDefaultRealm())
        authBasic.add_password(
            realm=None,
            uri=self.servers[record.podID]["uri"],
            user=user.encode('utf-8'),
            passwd=record.password.encode('utf-8'))
        authDigest = HTTPDigestAuthHandler(passwd=HTTPPasswordMgrWithDefaultRealm())
        authDigest.add_password(
            realm=None,
            uri=self.servers[record.podID]["uri"],
            user=user.encode('utf-8'),
            passwd=record.password.encode('utf-8'))
        return record, user, {"basic": authBasic, "digest": authDigest, }

    def stop(self):
        """
        Indicate that the simulation is over.  CalendarClientSimulator doesn't
        actively react to this, but it does cause all future failures to be
        disregarded (as some are expected, as the simulation will always stop
        while some requests are in flight).
        """

        # Give all the clients a chance to stop (including unsubscribe from push)
        deferreds = []
        for client in self.clients:
            deferreds.append(client.stop())
        self._stopped = True
        return DeferredList(deferreds)

    def add(self, numClients, clientsPerUser):
        instanceNumber = 0
        for _ignore_n in range(numClients):
            number = self._nextUserNumber()

            for _ignore_peruser in range(clientsPerUser):
                clientType = next(self._pop)
                if (number % self.workerCount) != self.workerIndex:
                    # If we're in a distributed work scenario and we are worker N,
                    # we have to skip all but every Nth request (since every node
                    # runs the same arrival policy).
                    continue

                record, _ignore_user, auth = self._createUser(number)

                reactor = loggedReactor(self.reactor)
                client = clientType.new(
                    reactor,
                    self.servers[record.podID],
                    self.principalPathTemplate,
                    self.serializationPath,
                    record,
                    auth,
                    instanceNumber
                )
                instanceNumber += 1
                self.clients.append(client)
                d = client.run()
                d.addErrback(self._clientFailure, reactor)

                for profileType in clientType.profileTypes:
                    profile = profileType(reactor, self, client, number)
                    if profile.enabled:
                        d = profile.initialize()

                        def _run(result):
                            d2 = profile.run()
                            d2.addErrback(self._profileFailure, profileType, reactor)
                            return d2
                        d.addCallback(_run)

        # XXX this status message is prone to be slightly inaccurate, but isn't
        # really used by much anyway.
        msg(type="status", clientCount=self._user - 1)

    def _dumpLogs(self, loggingReactor, reason):
        path = FilePath(mkdtemp())
        logstate = loggingReactor.getLogFiles()
        i = 0
        for i, log in enumerate(logstate.finished):
            path.child('%03d.log' % (i,)).setContent(log.getvalue())
        for i, log in enumerate(logstate.active, i):
            path.child('%03d.log' % (i,)).setContent(log.getvalue())
        path.child('reason.log').setContent(reason.getTraceback())
        return path

    def _clientFailure(self, reason, reactor):
        if not self._stopped:
            where = self._dumpLogs(reactor, reason)
            err(reason, "Client stopped with error; recent traffic in %r" % (
                where.path,))
            if not isinstance(reason, Failure):
                reason = Failure(reason)
            msg(type="client-failure", reason="%s: %s" % (reason.type, reason.value,))

    def _profileFailure(self, reason, profileType, reactor):
        if not self._stopped:
            where = self._dumpLogs(reactor, reason)
            err(reason, "Profile stopped with error; recent traffic in %r" % (
                where.path,))

    def _simFailure(self, reason, reactor):
        if not self._stopped:
            msg(type="sim-failure", reason=reason)


class SmoothRampUp(object):

    def __init__(self, reactor, groups, groupSize, interval, clientsPerUser):
        self.reactor = reactor
        self.groups = groups
        self.groupSize = groupSize
        self.interval = interval
        self.clientsPerUser = clientsPerUser

    def run(self, simulator):
        for i in range(self.groups):
            self.reactor.callLater(
                self.interval * i, simulator.add, self.groupSize, self.clientsPerUser)


class StatisticsBase(object):

    def observe(self, event):
        if event.get('type') == 'response':
            self.eventReceived(event)
        elif event.get('type') == 'client-failure':
            self.clientFailure(event)
        elif event.get('type') == 'sim-failure':
            self.simFailure(event)

    def report(self, output):
        pass

    def failures(self):
        return []


class SimpleStatistics(StatisticsBase):

    def __init__(self):
        self._times = []
        self._failures = collections.defaultdict(int)
        self._simFailures = collections.defaultdict(int)

    def eventReceived(self, event):
        self._times.append(event['duration'])
        if len(self._times) == 200:
            print('mean:', mean(self._times))
            print('median:', median(self._times))
            print('stddev:', stddev(self._times))
            print('mad:', mad(self._times))
            del self._times[:100]

    def clientFailure(self, event):
        self._failures[event] += 1

    def simFailure(self, event):
        self._simFailures[event] += 1


class ReportStatistics(StatisticsBase, SummarizingMixin):
    """

    @ivar _users: A C{set} containing all user UIDs which have been observed in
        events.  When generating the final report, the size of this set is
        reported as the number of users in the simulation.

    """

    # the response time thresholds to display together with failing % count threshold
    _thresholds_default = {
        "requests": {
            "limits": [0.1, 0.5, 1.0, 3.0, 5.0, 10.0, 30.0],
            "thresholds": {
                "default": [100.0, 100.0, 100.0, 5.0, 1.0, 0.5, 0.0],
            }
        }
    }
    _fail_cut_off = 1.0     # % of total count at which failed requests will cause a failure

    _fields_init = [
        ('request', -30, '%-30s'),
        ('count', 8, '%8s'),
        ('%', 6, '%6.1f'),
        ('failed', 8, '%8s'),
    ]

    _fields_extend = [
        ('mean', 8, '%8.4f'),
        ('median', 8, '%8.4f'),
        ('stddev', 8, '%8.4f'),
        ('QoS', 8, '%8.4f'),
        ('STATUS', 8, '%8s'),
    ]

    def __init__(self, **params):
        self._perMethodTimes = {}
        self._users = set()
        self._clients = set()
        self._failed_clients = []
        self._failed_sim = collections.defaultdict(int)
        self._startTime = datetime.now()
        self._expired_data = {}

        # Load parameters from config
        if "thresholdsPath" in params:
            with open(params["thresholdsPath"]) as f:
                jsondata = json.load(f)
        elif "thresholds" in params:
            jsondata = params["thresholds"]
        else:
            jsondata = self._thresholds_default
        self._thresholds = [[limit, {}] for limit in jsondata["requests"]["limits"]]
        for ctr, item in enumerate(self._thresholds):
            for k, v in jsondata["requests"]["thresholds"].items():
                item[1][k] = v[ctr]

        self._fields = self._fields_init[:]
        for threshold, _ignore_fail_at in self._thresholds:
            self._fields.append(('>%g sec' % (threshold,), 10, '%10s'))
        self._fields.extend(self._fields_extend)

        if "benchmarksPath" in params:
            with open(params["benchmarksPath"]) as f:
                self.benchmarks = json.load(f)
        else:
            self.benchmarks = {}

        if "failCutoff" in params:
            self._fail_cut_off = params["failCutoff"]

    def observe(self, event):
        if event.get('type') == 'sim-expired':
            self.simExpired(event)
        else:
            super(ReportStatistics, self).observe(event)

    def countUsers(self):
        return len(self._users)

    def countClients(self):
        return len(self._clients)

    def countClientFailures(self):
        return len(self._failed_clients)

    def countSimFailures(self):
        return len(self._failed_sim)

    def eventReceived(self, event):
        dataset = self._perMethodTimes.setdefault(event['method'], [])
        dataset.append((event['success'], event['duration']))
        self._users.add(event['user'])
        self._clients.add(event['client_id'])

    def clientFailure(self, event):
        self._failed_clients.append(event['reason'])

    def simFailure(self, event):
        self._failed_sim[event['reason']] += 1

    def simExpired(self, event):
        self._expired_data[event['podID']] = event['reason']

    def printMiscellaneous(self, output, items):
        maxColumnWidth = str(len(max(items.iterkeys(), key=len)))
        fmt = "%" + maxColumnWidth + "s : %-s\n"
        for k in sorted(items.iterkeys()):
            output.write(fmt % (k.title(), items[k],))

    def qos(self):
        """
        Determine a "quality of service" value that can be used for comparisons between runs. This value
        is based on the percentage deviation of means of each request from a set of "benchmarks" for each
        type of request.
        """

        # Get means for each type of method
        means = {}
        for method, results in self._perMethodTimes.items():
            try:
                means[method] = mean([duration for success, duration in results if success])
            except ZeroDivisionError:
                pass  # Ignore the case where all samples were unsuccessful?

        # Determine percentage differences with weighting
        differences = []
        for method, value in means.items():
            result = self.qos_value(method, value)
            if result is not None:
                differences.append(result)

        return ("%-8.4f" % mean(differences)) if differences else "None"

    def qos_value(self, method, value):
        benchmark = self.benchmarks.get(method)
        if benchmark is None:
            return None
        test_mean, weight = (benchmark["mean"], benchmark["weight"],)
        return ((value / test_mean) - 1.0) * weight + 1.0

    def _summarizeData(self, operation, data, total_count):
        data = SummarizingMixin._summarizeData(self, operation, data, total_count)
        value = self.qos_value(operation, data[-4])
        if value is None:
            value = 0.0
        return data[:-1] + (value,) + data[-1:]

    def report(self, output):
        output.write("\n")
        output.write("** REPORT **\n")
        output.write("\n")
        runtime = datetime.now() - self._startTime
        cpu = os.times()
        cpuUser = cpu[0] + cpu[2]
        cpuSys = cpu[1] + cpu[3]
        cpuTotal = cpuUser + cpuSys
        runHours, remainder = divmod(runtime.seconds, 3600)
        runMinutes, runSeconds = divmod(remainder, 60)
        cpuHours, remainder = divmod(cpuTotal, 3600)
        cpuMinutes, cpuSeconds = divmod(remainder, 60)
        items = {
            'Users': self.countUsers(),
            'Clients': self.countClients(),
            'Start time': self._startTime.strftime('%m/%d %H:%M:%S'),
            'Run time': "%02d:%02d:%02d" % (runHours, runMinutes, runSeconds),
            'CPU Time': "user %-5.2f sys %-5.2f total %02d:%02d:%02d" % (cpuUser, cpuSys, cpuHours, cpuMinutes, cpuSeconds,),
            'QoS': self.qos(),
        }
        if self.countClientFailures() > 0:
            items['Failed clients'] = self.countClientFailures()
            for ctr, reason in enumerate(self._failed_clients, 1):
                items['Failure #%d' % (ctr,)] = reason
        if self.countSimFailures() > 0:
            for reason, count in self._failed_sim.items():
                items['Failed operation'] = "%s : %d times" % (reason, count,)
        output.write("* Client\n")
        self.printMiscellaneous(output, items)
        output.write("\n")

        for podID, data in self._expired_data.items():
            items = {
                "Req/sec": "%.1f" % (data[0],),
                "Response": "%.1f (ms)" % (data[1],),
                "Slots": "%.2f" % (data[2],),
                "CPU": "%.1f%%" % (data[3],),
            }
            output.write("* Server {podID} (Last 5 minutes)\n".format(podID=podID))
            self.printMiscellaneous(output, items)
            output.write("\n")
        output.write("* Details\n")

        self.printHeader(output, [
            (label, width)
            for (label, width, _ignore_fmt)
            in self._fields
        ])
        self.printData(
            output,
            [fmt for (label, width, fmt) in self._fields],
            sorted(self._perMethodTimes.items())
        )

    _FAILED_REASON = "Greater than %(cutoff)g%% %(method)s failed"

    _REASON_1 = "Greater than %(cutoff)g%% %(method)s exceeded "
    _REASON_2 = "%g second response time"

    def failures(self):
        # TODO
        reasons = []

        for (method, times) in self._perMethodTimes.iteritems():
            failures = 0
            overDurations = [0] * len(self._thresholds)

            for success, duration in times:
                if not success:
                    failures += 1
                for ctr, item in enumerate(self._thresholds):
                    threshold, _ignore_fail_at = item
                    if duration > threshold:
                        overDurations[ctr] += 1

            checks = [
                (failures, self._fail_cut_off, self._FAILED_REASON),
            ]

            for ctr, item in enumerate(self._thresholds):
                threshold, fail_at = item
                fail_at = fail_at.get(method, fail_at["default"])
                checks.append(
                    (overDurations[ctr], fail_at, self._REASON_1 + self._REASON_2 % (threshold,))
                )

            for count, cutoff, reason in checks:
                if count * 100.0 / len(times) > cutoff:
                    reasons.append(reason % dict(method=method, cutoff=cutoff))

        if self.countClientFailures() != 0:
            reasons.append("Client failures: %d" % (self.countClientFailures(),))
        if self.countSimFailures() != 0:
            reasons.append("Overall failures: %d" % (self.countSimFailures(),))
        return reasons


def main():
    import random

    from twisted.internet import reactor
    from twisted.python.log import addObserver

    from twisted.python.failure import startDebugMode
    startDebugMode()

    report = ReportStatistics()
    addObserver(SimpleStatistics().observe)
    addObserver(report.observe)
    addObserver(RequestLogger().observe)

    r = random.Random()
    r.seed(100)
    populator = Populator(r)
    parameters = PopulationParameters()
    parameters.addClient(
        1, ClientType(OS_X_10_6, [Eventer, Inviter, Accepter]))
    simulator = CalendarClientSimulator(
        populator, r, parameters, reactor, '127.0.0.1', 8008)

    arrivalPolicy = SmoothRampUp(groups=10, groupSize=1, interval=3)
    arrivalPolicy.run(reactor, simulator)

    reactor.run()
    report.report()

if __name__ == '__main__':
    main()
