##
# Copyright (c) 2007-2017 Apple Inc. All rights reserved.
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
##

import sys
import os
import stat
import grp
import random

from time import sleep
from os.path import dirname, abspath
from collections import namedtuple

from zope.interface import implements

from twisted.application import service
from twisted.python import log as logging
from twisted.python.threadable import isInIOThread
from twisted.internet.reactor import callFromThread
from twisted.python.usage import Options, UsageError
from twisted.python.procutils import which

from twisted.runner.procmon import ProcessMonitor, LoggingProtocol

from twisted.internet.interfaces import IProcessTransport, IReactorProcess
from twisted.internet.protocol import ServerFactory
from twisted.internet.defer import Deferred, inlineCallbacks, succeed, gatherResults
from twisted.internet.task import Clock
from twisted.internet import reactor
from twisted.application.service import IService, IServiceCollection, Application
from twisted.application import internet

from twext.python.log import Logger
from twext.python.filepath import CachingFilePath as FilePath
from plistlib import writePlist  # @UnresolvedImport
from txweb2.dav import auth
from txweb2.log import LogWrapperResource
from twext.internet.tcp import MaxAcceptTCPServer, MaxAcceptSSLServer

from twistedcaldav import memcacheclient
from twistedcaldav.config import config, ConfigDict, ConfigurationError
from twistedcaldav.resource import AuthenticationWrapper
from twistedcaldav.stdconfig import DEFAULT_CONFIG

from twistedcaldav.directory.calendar import DirectoryCalendarHomeProvisioningResource
from twistedcaldav.directory.principal import DirectoryPrincipalProvisioningResource

from twistedcaldav.test.util import StoreTestCase, CapturingProcessProtocol, \
    TestCase

from calendarserver.tap.caldav import (
    CalDAVOptions, CalDAVServiceMaker, CalDAVService, GroupOwnedUNIXServer,
    DelayedStartupProcessMonitor, DelayedStartupLineLogger, TwistdSlaveProcess,
    _CONTROL_SERVICE_NAME, getSystemIDs, PreProcessingService,
    DataStoreMonitor, ErrorLoggingMultiService)
from calendarserver.provision.root import RootResource
from StringIO import StringIO
import tempfile
from twisted.python.log import ILogObserver

log = Logger()


# Points to top of source tree.
sourceRoot = dirname(dirname(dirname(dirname(abspath(__file__)))))


class NotAProcessTransport(object):
    """
    Simple L{IProcessTransport} stub.
    """
    implements(IProcessTransport)

    def __init__(self, processProtocol, executable, args, env, path,
                 uid, gid, usePTY, childFDs):
        """
        Hold on to all the attributes passed to spawnProcess.
        """
        self.processProtocol = processProtocol
        self.executable = executable
        self.args = args
        self.env = env
        self.path = path
        self.uid = uid
        self.gid = gid
        self.usePTY = usePTY
        self.childFDs = childFDs


class InMemoryProcessSpawner(Clock):
    """
    Stub out L{IReactorProcess} and L{IReactorClock} so that we can examine the
    interaction of L{DelayedStartupProcessMonitor} and the reactor.
    """
    implements(IReactorProcess)

    def __init__(self):
        """
        Create some storage to hold on to all the fake processes spawned.
        """
        super(InMemoryProcessSpawner, self).__init__()
        self.processTransports = []
        self.waiting = []

    def waitForOneProcess(self, amount=10.0):
        """
        Wait for an L{IProcessTransport} to be created by advancing the clock.
        If none are created in the specified amount of time, raise an
        AssertionError.
        """
        self.advance(amount)
        if self.processTransports:
            return self.processTransports.pop(0)
        else:
            raise AssertionError(
                "There were no process transports available.  Calls: " +
                repr(self.calls)
            )

    def spawnProcess(self, processProtocol, executable, args=(), env={},
                     path=None, uid=None, gid=None, usePTY=0,
                     childFDs=None):

        transport = NotAProcessTransport(
            processProtocol, executable, args, env, path, uid, gid, usePTY,
            childFDs
        )
        transport.startedAt = self.seconds()
        self.processTransports.append(transport)
        if self.waiting:
            self.waiting.pop(0).callback(transport)
        return transport


class TestCalDAVOptions (CalDAVOptions):
    """
    A fake implementation of CalDAVOptions that provides
    empty implementations of checkDirectory and checkFile.
    """

    def checkDirectory(self, *args, **kwargs):
        pass

    def checkFile(self, *args, **kwargs):
        pass

    def checkDirectories(self, *args, **kwargs):
        pass

    def loadConfiguration(self):
        """
        Simple wrapper to avoid printing during test runs.
        """
        oldout = sys.stdout
        newout = sys.stdout = StringIO()
        try:
            return CalDAVOptions.loadConfiguration(self)
        finally:
            sys.stdout = oldout
            log.info(
                "load configuration console output: {result}", result=newout.getvalue()
            )


class CalDAVOptionsTest(StoreTestCase):
    """
    Test various parameters of our usage.Options subclass
    """
    @inlineCallbacks
    def setUp(self):
        """
        Set up our options object, giving it a parent, and forcing the
        global config to be loaded from defaults.
        """
        yield super(CalDAVOptionsTest, self).setUp()
        self.config = TestCalDAVOptions()
        self.config.parent = Options()
        self.config.parent["uid"] = 0
        self.config.parent["gid"] = 0
        self.config.parent["nodaemon"] = False

    def tearDown(self):
        config.setDefaults(DEFAULT_CONFIG)
        config.reload()

    def test_overridesConfig(self):
        """
        Test that values on the command line's -o and --option options
        overide the config file
        """
        myConfig = ConfigDict(DEFAULT_CONFIG)
        myConfigFile = self.mktemp()
        writePlist(myConfig, myConfigFile)

        argv = [
            "-f", myConfigFile,
            "-o", "EnableSACLs",
            "-o", "HTTPPort=80",
            "-o", "BindAddresses=127.0.0.1,127.0.0.2,127.0.0.3",
            "-o", "DocumentRoot=/dev/null",
            "-o", "UserName=None",
            "-o", "EnableProxyPrincipals=False",
        ]

        self.config.parseOptions(argv)

        self.assertEquals(config.EnableSACLs, True)
        self.assertEquals(config.HTTPPort, 80)
        self.assertEquals(config.BindAddresses,
                          ["127.0.0.1", "127.0.0.2", "127.0.0.3"])
        self.assertEquals(config.DocumentRoot, "/dev/null")
        self.assertEquals(config.UserName, None)
        self.assertEquals(config.EnableProxyPrincipals, False)

        argv = ["-o", "Authentication=This Doesn't Matter"]

        self.assertRaises(UsageError, self.config.parseOptions, argv)

    def test_setsParent(self):
        """
        Test that certain values are set on the parent (i.e. twistd's
        Option's object)
        """
        myConfig = ConfigDict(DEFAULT_CONFIG)
        myConfigFile = self.mktemp()
        writePlist(myConfig, myConfigFile)

        argv = [
            "-f", myConfigFile,
            "-o", "PIDFile=/dev/null",
            "-o", "umask=63",
            # integers in plists & calendarserver command line are always
            # decimal; umask is traditionally in octal.
        ]

        self.config.parseOptions(argv)

        self.assertEquals(self.config.parent["pidfile"], "/dev/null")
        self.assertEquals(self.config.parent["umask"], 0077)

    def test_specifyConfigFile(self):
        """
        Test that specifying a config file from the command line
        loads the global config with those values properly.
        """
        myConfig = ConfigDict(DEFAULT_CONFIG)

        myConfig.Authentication.Basic.Enabled = False
        myConfig.HTTPPort = 80
        myConfig.ServerHostName = "calendar.calenderserver.org"

        myConfigFile = self.mktemp()
        writePlist(myConfig, myConfigFile)

        args = ["-f", myConfigFile]

        self.config.parseOptions(args)

        self.assertEquals(config.ServerHostName, myConfig["ServerHostName"])
        self.assertEquals(config.HTTPPort, myConfig.HTTPPort)
        self.assertEquals(
            config.Authentication.Basic.Enabled,
            myConfig.Authentication.Basic.Enabled
        )

    def test_specifyDictPath(self):
        """
        Test that we can specify command line overrides to leafs using
        a "/" seperated path.  Such as "-o MultiProcess/ProcessCount=1"
        """
        myConfig = ConfigDict(DEFAULT_CONFIG)
        myConfigFile = self.mktemp()
        writePlist(myConfig, myConfigFile)

        argv = [
            "-o", "MultiProcess/ProcessCount=102",
            "-f", myConfigFile,
        ]

        self.config.parseOptions(argv)

        self.assertEquals(config.MultiProcess["ProcessCount"], 102)


def inServiceHierarchy(svc, predicate):
    """
    Find services in the service collection which satisfy the given predicate.
    """
    for subsvc in svc.services:
        if IServiceCollection.providedBy(subsvc):
            for value in inServiceHierarchy(subsvc, predicate):
                yield value
        if predicate(subsvc):
            yield subsvc


def determineAppropriateGroupID():
    """
    Determine a secondary group ID which can be used for testing, or None
    if the executing user has no additional unix group memberships.
    """
    currentGroups = os.getgroups()
    if len(currentGroups) < 2:
        return None
    else:
        return currentGroups[1]


class SocketGroupOwnership(StoreTestCase):
    """
    Tests for L{GroupOwnedUNIXServer}.
    """

    def test_groupOwnedUNIXSocket(self):
        """
        When a L{GroupOwnedUNIXServer} is started, it will change the group of
        its socket.
        """
        alternateGroup = determineAppropriateGroupID()
        if alternateGroup is None:
            self.skipTest ((
            "This test requires that the user running it is a member of at"
            " least two unix groups."
            ))
        socketName = self.mktemp()
        gous = GroupOwnedUNIXServer(alternateGroup, socketName, ServerFactory(), mode=0660)
        gous.privilegedStartService()
        self.addCleanup(gous.stopService)
        filestat = os.stat(socketName)
        self.assertTrue(stat.S_ISSOCK(filestat.st_mode))
        self.assertEquals(filestat.st_gid, alternateGroup)
        self.assertEquals(filestat.st_uid, os.getuid())


# Tests for the various makeService_ flavors:

class CalDAVServiceMakerTestBase(StoreTestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(CalDAVServiceMakerTestBase, self).setUp()
        self.options = TestCalDAVOptions()
        self.options.parent = Options()
        self.options.parent["gid"] = None
        self.options.parent["uid"] = None
        self.options.parent["nodaemon"] = None


class CalDAVServiceMakerTestSingle(CalDAVServiceMakerTestBase):

    def configure(self):
        super(CalDAVServiceMakerTestSingle, self).configure()
        config.ProcessType = "Single"

    def test_makeService(self):
        CalDAVServiceMaker().makeService(self.options)
        # No error


class CalDAVServiceMakerTestSlave(CalDAVServiceMakerTestBase):

    def configure(self):
        super(CalDAVServiceMakerTestSlave, self).configure()
        config.ProcessType = "Slave"

    def test_makeService(self):
        CalDAVServiceMaker().makeService(self.options)
        # No error


class CalDAVServiceMakerTestUnknown(CalDAVServiceMakerTestBase):

    def configure(self):
        super(CalDAVServiceMakerTestUnknown, self).configure()
        config.ProcessType = "Unknown"

    def test_makeService(self):
        self.assertRaises(UsageError, CalDAVServiceMaker().makeService, self.options)
        # error


class ModesOnUNIXSocketsTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(ModesOnUNIXSocketsTests, self).configure()
        config.ProcessType = "Combined"
        config.HTTPPort = 0
        self.alternateGroup = determineAppropriateGroupID()
        # If the current user isn't a member of >1 unix groups,
        # this test should proceed anyway, so use the primary group ID
        if self.alternateGroup is None:
            self.alternateGroup = os.getgroups()[0]
        config.GroupName = grp.getgrgid(self.alternateGroup).gr_name
        config.Stats.EnableUnixStatsSocket = True

    def test_modesOnUNIXSockets(self):
        """
        The logging and stats UNIX sockets that are bound as part of the
        'Combined' service hierarchy should have a secure mode specified: only
        the executing user should be able to open and send to them.
        """
        svc = CalDAVServiceMaker().makeService(self.options)
        for serviceName in [_CONTROL_SERVICE_NAME]:
            socketService = svc.getServiceNamed(serviceName)
            self.assertIsInstance(socketService, GroupOwnedUNIXServer)
            m = socketService.kwargs.get("mode", 0666)
            self.assertEquals(
                m, int("660", 8),
                "Wrong mode on %s: %s" % (serviceName, oct(m))
            )
            self.assertEquals(socketService.gid, self.alternateGroup)
        for serviceName in ["unix-stats"]:
            socketService = svc.getServiceNamed(serviceName)
            self.assertIsInstance(socketService, GroupOwnedUNIXServer)
            m = socketService.kwargs.get("mode", 0666)
            self.assertEquals(
                m, int("660", 8),
                "Wrong mode on %s: %s" % (serviceName, oct(m))
            )
            self.assertEquals(socketService.gid, self.alternateGroup)


class TestLoggingProtocol(LoggingProtocol):

    def processEnded(self, reason):
        LoggingProtocol.processEnded(self, reason)
        self.service.processEnded(self.name)


class TestErrorLoggingMultiService(TestCase):

    def test_nonAsciiLog(self):
        """
        Make sure that the file based error log can write non ascii data
        """

        logpath = self.mktemp()
        service = ErrorLoggingMultiService(
            True,
            logpath,
            10000,
            10,
            False,
        )
        app = Application("non-ascii")
        service.setServiceParent(app)

        observer = app.getComponent(ILogObserver, None)
        self.assertTrue(observer is not None)

        log = Logger(observer=observer)
        log.error(u"Couldn\u2019t be wrong")

        with open(logpath) as f:
            logentry = f.read()
        self.assertIn("Couldn\xe2\x80\x99t be wrong", logentry)


class TestProcessMonitor(ProcessMonitor):

    def startProcess(self, name):
        """
        @param name: The name of the process to be started
        """
        # If a protocol instance already exists, it means the process is
        # already running
        if name in self.protocols:
            return

        args, uid, gid, env = self.processes[name]

        proto = TestLoggingProtocol()
        proto.service = self
        proto.name = name
        self.protocols[name] = proto
        self.timeStarted[name] = self._reactor.seconds()
        self._reactor.spawnProcess(
            proto, args[0], args, uid=uid, gid=gid, env=env
        )

    def stopService(self):
        """
        Return a deferred that fires when all child processes have ended.
        """
        service.Service.stopService(self)

        self.stopping = True
        self.deferreds = {}
        for name in self.processes:
            self.deferreds[name] = Deferred()

        # Cancel any outstanding restarts
        for name, delayedCall in self.restart.items():
            if delayedCall.active():
                delayedCall.cancel()

        for name in self.processes:
            self.stopProcess(name)

        return gatherResults(self.deferreds.values())

    def processEnded(self, name):
        if self.stopping:
            deferred = self.deferreds.pop(name, None)
            if deferred is not None:
                deferred.callback(None)


class MemcacheSpawner(TestCase):

    def setUp(self):
        super(MemcacheSpawner, self).setUp()
        self.monitor = TestProcessMonitor()
        self.monitor.startService()
        self.socket = os.path.join(tempfile.gettempdir(), "memcache.sock")
        self.patch(config.Memcached.Pools.Default, "ServerEnabled", True)

    def test_memcacheUnix(self):
        """
        Spawn a memcached process listening on a unix socket that becomes
        connectable in no more than one second. Connect and interact.
        Verify secure file permissions on the socket file.
        """
        self.patch(config.Memcached.Pools.Default, "MemcacheSocket", self.socket)
        CalDAVServiceMaker()._spawnMemcached(monitor=self.monitor)
        sleep(1)
        mc = memcacheclient.Client(["unix:{}".format(self.socket)], debug=1)
        rando = random.random()
        mc.set("the_answer", rando)
        self.assertEquals(rando, mc.get("the_answer"))
        # The socket file should not be usable to other users
        st = os.stat(self.socket)
        self.assertTrue(str(oct(st.st_mode)).endswith("00"))
        mc.disconnect_all()

    def test_memcacheINET(self):
        """
        Spawn a memcached process listening on a network socket that becomes
        connectable in no more than one second. Interact with it.
        """
        self.patch(config.Memcached.Pools.Default, "MemcacheSocket", "")
        ba = config.Memcached.Pools.Default.BindAddress
        bp = config.Memcached.Pools.Default.Port
        CalDAVServiceMaker()._spawnMemcached(monitor=self.monitor)
        sleep(1)
        mc = memcacheclient.Client(["{}:{}".format(ba, bp)], debug=1)
        rando = random.random()
        mc.set("the_password", rando)
        self.assertEquals(rando, mc.get("the_password"))
        mc.disconnect_all()

    def tearDown(self):
        """
        Verify that our spawned memcached can be reaped.
        """
        return self.monitor.stopService()


class ProcessMonitorTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(ProcessMonitorTests, self).configure()
        config.ProcessType = "Combined"

    def test_processMonitor(self):
        """
        In the master, there should be exactly one
        L{DelayedStartupProcessMonitor} in the service hierarchy so that it
        will be started by startup.
        """
        self.assertEquals(
            1,
            len(
                list(inServiceHierarchy(
                    CalDAVServiceMaker().makeService(self.options),
                    lambda x: isinstance(x, DelayedStartupProcessMonitor)))
            )
        )


class SlaveServiceTests(CalDAVServiceMakerTestBase):
    """
    Test various configurations of the Slave service
    """

    def configure(self):
        super(SlaveServiceTests, self).configure()
        config.ProcessType = "Slave"
        config.HTTPPort = 8008
        config.SSLPort = 8443
        pemFile = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        config.SSLPrivateKey = pemFile
        config.SSLCertificate = pemFile
        config.SSLKeychainIdentity = "org.calendarserver.test"
        config.EnableSSL = True

    def test_defaultService(self):
        """
        Test the value of a Slave service in it's simplest
        configuration.
        """
        service = CalDAVServiceMaker().makeService(self.options)

        self.failUnless(
            IService(service),
            "%s does not provide IService" % (service,)
        )
        self.failUnless(
            service.services,
            "No services configured"
        )
        self.failUnless(
            isinstance(service, CalDAVService),
            "%s is not a CalDAVService" % (service,)
        )

    def test_defaultListeners(self):
        """
        Test that the Slave service has sub services with the
        default TCP and SSL configuration
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        expectedSubServices = dict((
            (MaxAcceptTCPServer, config.HTTPPort),
            (MaxAcceptSSLServer, config.SSLPort),
        ))

        configuredSubServices = [(s.__class__, getattr(s, 'args', None))
                                 for s in service.services]
        checked = 0
        for serviceClass, serviceArgs in configuredSubServices:
            if serviceClass in expectedSubServices:
                checked += 1
                self.assertEquals(
                    serviceArgs[0],
                    dict(expectedSubServices)[serviceClass]
                )
        # TCP+SSL services for each bind address
        self.assertEquals(checked, 2 * len(config.BindAddresses))

    def test_SSLKeyConfiguration(self):
        """
        Test that the configuration of the SSLServer reflect the config file's
        SSL Private Key and SSL Certificate
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        sslService = None
        for s in service.services:
            if isinstance(s, internet.SSLServer):
                sslService = s
                break

        self.failIf(sslService is None, "No SSL Service found")

        context = sslService.args[2]

        self.assertEquals(
            config.SSLPrivateKey,
            context.privateKeyFileName
        )
        self.assertEquals(
            config.SSLCertificate,
            context.certificateFileName,
        )

class SNIModeServiceTests(CalDAVServiceMakerTestBase):
    """
    Test various configurations of SNI Mode
    """

    def configure(self):
        super(SNIModeServiceTests, self).configure()
        config.ProcessType = "Slave"
        config.HTTPPort = 8008
        config.SSLPort = 8443
        config.EnableSSL = True
        config.SNIMode = True
        pemDir = os.path.join(sourceRoot, "twistedcaldav/test/data")
        pemDefault = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        config.SSLDir = pemDir
        config.DefaultSSLBundle = pemDefault

    def test_SNIConfiguration(self):
        """
        Test that the configuration of the SSLServer reflect the fact we're using SNI
        """
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        sslService = None
        for s in service.services:
            if isinstance(s, internet.SSLServer):
                sslService = s
                break

        self.failIf(sslService is None, "No SSL/TLS Service found")

        context = sslService.args[2]

        from txsni.snimap import SNIMap
        self.assertTrue(isinstance( context, SNIMap ))

class NoSSLTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(NoSSLTests, self).configure()
        config.ProcessType = "Slave"
        config.HTTPPort = 8008
        # pemFile = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        # config.SSLPrivateKey = pemFile
        # config.SSLCertificate = pemFile
        # config.EnableSSL = True

    def test_noSSL(self):
        """
        Test the single service to make sure there is no SSL Service when SSL
        is disabled
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        self.assertNotIn(
            internet.SSLServer,
            [s.__class__ for s in service.services]
        )


class NoHTTPTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(NoHTTPTests, self).configure()
        config.ProcessType = "Slave"
        config.SSLPort = 8443
        pemFile = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        config.SSLPrivateKey = pemFile
        config.SSLCertificate = pemFile
        config.SSLKeychainIdentity = "org.calendarserver.test"
        config.EnableSSL = True

    def test_noHTTP(self):
        """
        Test the single service to make sure there is no TCPServer when
        HTTPPort is not configured
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        self.assertNotIn(
            internet.TCPServer,
            [s.__class__ for s in service.services]
        )


class SingleBindAddressesTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(SingleBindAddressesTests, self).configure()
        config.ProcessType = "Slave"
        config.HTTPPort = 8008
        config.BindAddresses = ["127.0.0.1"]

    def test_singleBindAddresses(self):
        """
        Test that the TCPServer and SSLServers are bound to the proper address
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        for s in service.services:
            if isinstance(s, (internet.TCPServer, internet.SSLServer)):
                self.assertEquals(s.kwargs["interface"], "127.0.0.1")


class MultipleBindAddressesTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(MultipleBindAddressesTests, self).configure()
        config.ProcessType = "Slave"
        config.HTTPPort = 8008
        config.SSLPort = 8443
        pemFile = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        config.SSLPrivateKey = pemFile
        config.SSLCertificate = pemFile
        config.SSLKeychainIdentity = "org.calendarserver.test"
        config.EnableSSL = True
        config.BindAddresses = [
            "127.0.0.1",
            "10.0.0.2",
            "172.53.13.123",
        ]

    def test_multipleBindAddresses(self):
        """
        Test that the TCPServer and SSLServers are bound to the proper
        addresses.
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        tcpServers = []
        sslServers = []

        for s in service.services:
            if isinstance(s, internet.TCPServer):
                tcpServers.append(s)
            elif isinstance(s, internet.SSLServer):
                sslServers.append(s)

        self.assertEquals(len(tcpServers), len(config.BindAddresses))
        self.assertEquals(len(sslServers), len(config.BindAddresses))

        for addr in config.BindAddresses:
            for s in tcpServers:
                if s.kwargs["interface"] == addr:
                    tcpServers.remove(s)

            for s in sslServers:
                if s.kwargs["interface"] == addr:
                    sslServers.remove(s)

        self.assertEquals(len(tcpServers), 0)
        self.assertEquals(len(sslServers), 0)


class ListenBacklogTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(ListenBacklogTests, self).configure()
        config.ProcessType = "Slave"
        config.ListenBacklog = 1024
        config.HTTPPort = 8008
        config.SSLPort = 8443
        pemFile = os.path.join(sourceRoot, "twistedcaldav/test/data/server.pem")
        config.SSLPrivateKey = pemFile
        config.SSLCertificate = pemFile
        config.SSLKeychainIdentity = "org.calendarserver.test"
        config.EnableSSL = True
        config.BindAddresses = [
            "127.0.0.1",
            "10.0.0.2",
            "172.53.13.123",
        ]

    def test_listenBacklog(self):
        """
        Test that the backlog arguments is set in TCPServer and SSLServers
        """
        # Note: the listeners are bundled within a MultiService named "ConnectionService"
        service = CalDAVServiceMaker().makeService(self.options)
        service = service.getServiceNamed(CalDAVService.connectionServiceName)

        for s in service.services:
            if isinstance(s, (internet.TCPServer, internet.SSLServer)):
                self.assertEquals(s.kwargs["backlog"], 1024)


class AuthWrapperAllEnabledTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(AuthWrapperAllEnabledTests, self).configure()
        config.HTTPPort = 8008
        config.Authentication.Digest.Enabled = True
        config.Authentication.Kerberos.Enabled = True
        config.Authentication.Kerberos.ServicePrincipal = "http/hello@bob"
        config.Authentication.Basic.Enabled = True

    def test_AuthWrapperAllEnabled(self):
        """
        Test the configuration of the authentication wrapper
        when all schemes are enabled.
        """

        authWrapper = self.rootResource.resource
        self.failUnless(
            isinstance(
                authWrapper,
                auth.AuthenticationWrapper
            )
        )

        expectedSchemes = ["negotiate", "digest", "basic"]

        for scheme in authWrapper.credentialFactories:
            self.failUnless(scheme in expectedSchemes)

        self.assertEquals(len(expectedSchemes),
                          len(authWrapper.credentialFactories))

        ncf = authWrapper.credentialFactories["negotiate"]

        self.assertEquals(ncf.service, "http@HELLO")
        self.assertEquals(ncf.realm, "bob")


class ServicePrincipalNoneTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(ServicePrincipalNoneTests, self).configure()
        config.HTTPPort = 8008
        config.Authentication.Digest.Enabled = True
        config.Authentication.Kerberos.Enabled = True
        config.Authentication.Kerberos.ServicePrincipal = ""
        config.Authentication.Basic.Enabled = True

    def test_servicePrincipalNone(self):
        """
        Test that the Kerberos principal look is attempted if the principal is empty.
        """
        authWrapper = self.rootResource.resource
        self.assertFalse("negotiate" in authWrapper.credentialFactories)


class AuthWrapperPartialEnabledTests(CalDAVServiceMakerTestBase):

    def configure(self):
        super(AuthWrapperPartialEnabledTests, self).configure()
        config.Authentication.Digest.Enabled = True
        config.Authentication.Kerberos.Enabled = False
        config.Authentication.Basic.Enabled = False

    def test_AuthWrapperPartialEnabled(self):
        """
        Test that the expected credential factories exist when
        only a partial set of authentication schemes is
        enabled.
        """

        authWrapper = self.rootResource.resource
        expectedSchemes = ["digest"]

        for scheme in authWrapper.credentialFactories:
            self.failUnless(scheme in expectedSchemes)

        self.assertEquals(
            len(expectedSchemes),
            len(authWrapper.credentialFactories)
        )


class ResourceTests(CalDAVServiceMakerTestBase):

    def test_LogWrapper(self):
        """
        Test the configuration of the log wrapper
        """
        self.failUnless(isinstance(self.rootResource, LogWrapperResource))

    def test_AuthWrapper(self):
        """
        Test the configuration of the auth wrapper
        """
        self.failUnless(isinstance(self.rootResource.resource, AuthenticationWrapper))

    def test_rootResource(self):
        """
        Test the root resource
        """
        self.failUnless(isinstance(self.rootResource.resource.resource, RootResource))

    @inlineCallbacks
    def test_principalResource(self):
        """
        Test the principal resource
        """
        self.failUnless(isinstance(
            (yield self.actualRoot.getChild("principals")),
            DirectoryPrincipalProvisioningResource
        ))

    @inlineCallbacks
    def test_calendarResource(self):
        """
        Test the calendar resource
        """
        self.failUnless(isinstance(
            (yield self.actualRoot.getChild("calendars")),
            DirectoryCalendarHomeProvisioningResource
        ))

    @inlineCallbacks
    def test_sameDirectory(self):
        """
        Test that the principal hierarchy has a reference
        to the same DirectoryService as the calendar hierarchy
        """
        principals = yield self.actualRoot.getChild("principals")
        calendars = yield self.actualRoot.getChild("calendars")

        self.assertEquals(principals.directory, calendars.directory)


class DummyProcessObject(object):
    """
    Simple stub for Process Object API which just has an executable and some
    arguments.

    This is a stand in for L{TwistdSlaveProcess}.
    """

    def __init__(self, scriptname, *args):
        self.scriptname = scriptname
        self.args = args

    def starting(self):
        pass

    def stopped(self):
        pass

    def getCommandLine(self):
        """
        Simple command line.
        """
        return [self.scriptname] + list(self.args)

    def getFileDescriptors(self):
        """
        Return a dummy, empty mapping of file descriptors.
        """
        return {}

    def getName(self):
        """
        Get a dummy name.
        """
        return 'Dummy'


class ScriptProcessObject(DummyProcessObject):
    """
    Simple stub for the Process Object API that will run a test script.
    """

    def getCommandLine(self):
        """
        Get the command line to invoke this script.
        """
        return [
            sys.executable,
            FilePath(__file__).sibling(self.scriptname).path
        ] + list(self.args)


class DelayedStartupProcessMonitorTests(StoreTestCase):
    """
    Test cases for L{DelayedStartupProcessMonitor}.
    """

    def test_lineAfterLongLine(self):
        """
        A "long" line of output from a monitored process (longer than
        L{LineReceiver.MAX_LENGTH}) should be logged in chunks rather than all
        at once, to avoid resource exhaustion.
        """
        dspm = DelayedStartupProcessMonitor()
        dspm.addProcessObject(
            ScriptProcessObject(
                'longlines.py',
                str(DelayedStartupLineLogger.MAX_LENGTH)
            ),
            os.environ
        )
        dspm.startService()
        self.addCleanup(dspm.stopService)

        logged = []

        def tempObserver(event):
            # Probably won't be a problem, but let's not have any intermittent
            # test issues that stem from multi-threaded log messages randomly
            # going off...
            if not isInIOThread():
                callFromThread(tempObserver, event)
                return
            if event.get('isError'):
                d.errback()
            if event.get("log_system") == u'Dummy':
                logged.append(event["msg"])
                if event["msg"] == u'z':
                    d.callback("done")

        logging.addObserver(tempObserver)
        self.addCleanup(logging.removeObserver, tempObserver)
        d = Deferred()

        def assertions(result):
            self.assertEquals(["x",
                               "y",
                               "y",  # final segment
                               "z"],
                              [msg[0] for msg in logged])
            self.assertEquals([" (truncated, continued)",
                               " (truncated, continued)",
                               "y",
                               "z"],
                              [msg[-len(" (truncated, continued)"):] for msg in logged])
        d.addCallback(assertions)
        return d

    def test_breakLineIntoSegments(self):
        """
        Exercise the line-breaking logic with various key lengths
        """
        testLogger = DelayedStartupLineLogger()
        testLogger.MAX_LENGTH = 10
        for input, output in [
            ("", []),
            ("a", ["a"]),
            ("abcde", ["abcde"]),
            ("abcdefghij", ["abcdefghij"]),
            (
                "abcdefghijk",
                [
                    "abcdefghij (truncated, continued)",
                    "k"
                ]
            ),
            (
                "abcdefghijklmnopqrst",
                [
                    "abcdefghij (truncated, continued)",
                    "klmnopqrst"
                ]
            ),
            (
                "abcdefghijklmnopqrstuv",
                [
                    "abcdefghij (truncated, continued)",
                    "klmnopqrst (truncated, continued)",
                    "uv"
                ]
            ),
        ]:
            self.assertEquals(output, testLogger._breakLineIntoSegments(input))

    def test_acceptDescriptorInheritance(self):
        """
        If a L{TwistdSlaveProcess} specifies some file descriptors to be
        inherited, they should be inherited by the subprocess.
        """
        imps = InMemoryProcessSpawner()
        dspm = DelayedStartupProcessMonitor(imps)

        # Most arguments here will be ignored, so these are bogus values.
        slave = TwistdSlaveProcess(
            twistd="bleh",
            tapname="caldav",
            configFile="/does/not/exist",
            id=10,
            interfaces='127.0.0.1',
            inheritFDs=[3, 7],
            inheritSSLFDs=[19, 25],
        )

        dspm.addProcessObject(slave, {})
        dspm.startService()
        # We can easily stub out spawnProcess, because caldav calls it, but a
        # bunch of callLater calls are buried in procmon itself, so we need to
        # use the real clock.
        oneProcessTransport = imps.waitForOneProcess()
        self.assertEquals(oneProcessTransport.childFDs,
                          {0: 'w', 1: 'r', 2: 'r',
                           3: 3, 7: 7,
                           19: 19, 25: 25})

    def test_changedArgumentEachSpawn(self):
        """
        If the result of C{getCommandLine} changes on subsequent calls,
        subsequent calls should result in different arguments being passed to
        C{spawnProcess} each time.
        """
        imps = InMemoryProcessSpawner()
        dspm = DelayedStartupProcessMonitor(imps)
        slave = DummyProcessObject('scriptname', 'first')
        dspm.addProcessObject(slave, {})
        dspm.startService()
        oneProcessTransport = imps.waitForOneProcess()
        self.assertEquals(oneProcessTransport.args,
                          ['scriptname', 'first'])
        slave.args = ['second']
        oneProcessTransport.processProtocol.processEnded(None)
        twoProcessTransport = imps.waitForOneProcess()
        self.assertEquals(twoProcessTransport.args,
                          ['scriptname', 'second'])

    def test_metaDescriptorInheritance(self):
        """
        If a L{TwistdSlaveProcess} specifies a meta-file-descriptor to be
        inherited, it should be inherited by the subprocess, and a
        configuration argument should be passed that indicates to the
        subprocess.
        """
        imps = InMemoryProcessSpawner()
        dspm = DelayedStartupProcessMonitor(imps)
        # Most arguments here will be ignored, so these are bogus values.
        slave = TwistdSlaveProcess(
            twistd="bleh",
            tapname="caldav",
            configFile="/does/not/exist",
            id=10,
            interfaces='127.0.0.1',
            metaSocket=FakeDispatcher().addSocket()
        )

        dspm.addProcessObject(slave, {})
        dspm.startService()
        oneProcessTransport = imps.waitForOneProcess()
        self.assertIn("MetaFD=4", oneProcessTransport.args)
        self.assertEquals(
            oneProcessTransport.args[oneProcessTransport.args.index("MetaFD=4") - 1],
            '-o',
            "MetaFD argument was not passed as an option"
        )
        self.assertEquals(oneProcessTransport.childFDs,
                          {0: 'w', 1: 'r', 2: 'r',
                           4: 4})

    def test_startServiceDelay(self):
        """
        Starting a L{DelayedStartupProcessMonitor} should result in the process
        objects that have been added to it being started once per
        delayInterval.
        """
        imps = InMemoryProcessSpawner()
        dspm = DelayedStartupProcessMonitor(imps)
        dspm.delayInterval = 3.0
        sampleCounter = range(0, 5)
        for counter in sampleCounter:
            slave = TwistdSlaveProcess(
                twistd="bleh",
                tapname="caldav",
                configFile="/does/not/exist",
                id=counter * 10,
                interfaces='127.0.0.1',
                metaSocket=FakeDispatcher().addSocket()
            )
            dspm.addProcessObject(slave, {"SAMPLE_ENV_COUNTER": str(counter)})
        dspm.startService()

        # Advance the clock a bunch of times, allowing us to time things with a
        # comprehensible resolution.
        imps.pump([0] + [dspm.delayInterval / 2.0] * len(sampleCounter) * 3)
        expectedValues = [dspm.delayInterval * n for n in sampleCounter]
        self.assertEquals([x.startedAt for x in imps.processTransports],
                          expectedValues)


class FakeFD(object):

    def __init__(self, n):
        self.fd = n

    def fileno(self):
        return self.fd


class FakeSubsocket(object):

    def __init__(self, fakefd):
        self.fakefd = fakefd

    def childSocket(self):
        return self.fakefd

    def start(self):
        pass

    def restarted(self):
        pass

    def stop(self):
        pass


class FakeDispatcher(object):
    n = 3

    def addSocket(self):
        self.n += 1
        return FakeSubsocket(FakeFD(self.n))


class TwistdSlaveProcessTests(StoreTestCase):
    """
    Tests for L{TwistdSlaveProcess}.
    """

    def test_pidfile(self):
        """
        The result of L{TwistdSlaveProcess.getCommandLine} includes an option
        setting the name of the pidfile to something including the instance id.
        """
        slave = TwistdSlaveProcess("/path/to/twistd", "something", "config", 7, [])
        commandLine = slave.getCommandLine()

        option = 'PIDFile=something-instance-7.pid'
        self.assertIn(option, commandLine)
        self.assertEquals(commandLine[commandLine.index(option) - 1], '-o')


class ReExecServiceTests(StoreTestCase):

    @inlineCallbacks
    def test_reExecService(self):
        """
        Verify that sending a HUP to the test reexec.tac causes startService
        and stopService to be called again by counting the number of times
        START and STOP appear in the process output.
        """
        # Inherit the reactor used to run trial
        reactorArg = "--reactor=select"
        for arg in sys.argv:
            if arg.startswith("--reactor"):
                reactorArg = arg
                break

        tacFilePath = os.path.join(os.path.dirname(__file__), "reexec.tac")
        twistd = which("twistd")[0]
        deferred = Deferred()
        proc = reactor.spawnProcess(
            CapturingProcessProtocol(deferred, None),
            sys.executable,
            [sys.executable, '-W', 'ignore', twistd, reactorArg, '-n', '-y', tacFilePath],
            env=os.environ
        )
        reactor.callLater(3, proc.signalProcess, "HUP")
        reactor.callLater(6, proc.signalProcess, "TERM")
        output = yield deferred
        self.assertEquals(output.count("START"), 2)
        self.assertEquals(output.count("STOP"), 2)


class SystemIDsTests(StoreTestCase):
    """
    Verifies the behavior of calendarserver.tap.caldav.getSystemIDs
    """

    def _wrappedFunction(self):
        """
        Return a copy of the getSystemIDs function with test implementations
        of the ID lookup functions swapped into the namespace.
        """

        def _getpwnam(name):
            if name == "exists":
                Getpwnam = namedtuple("Getpwnam", ("pw_uid"))
                return Getpwnam(42)
            else:
                raise KeyError(name)

        def _getgrnam(name):
            if name == "exists":
                Getgrnam = namedtuple("Getgrnam", ("gr_gid"))
                return Getgrnam(43)
            else:
                raise KeyError(name)

        def _getuid():
            return 44

        def _getgid():
            return 45

        return type(getSystemIDs)(
            getSystemIDs.func_code,  # @UndefinedVariable
            {
                "getpwnam": _getpwnam,
                "getgrnam": _getgrnam,
                "getuid": _getuid,
                "getgid": _getgid,
                "KeyError": KeyError,
                "ConfigurationError": ConfigurationError,
            }
        )

    def test_getSystemIDs_UserNameNotFound(self):
        """
        If userName is passed in but is not found on the system, raise a
        ConfigurationError
        """
        self.assertRaises(
            ConfigurationError, self._wrappedFunction(),
            "nonexistent", "exists"
        )

    def test_getSystemIDs_GroupNameNotFound(self):
        """
        If groupName is passed in but is not found on the system, raise a
        ConfigurationError
        """
        self.assertRaises(
            ConfigurationError, self._wrappedFunction(),
            "exists", "nonexistent"
        )

    def test_getSystemIDs_NamesNotSpecified(self):
        """
        If names are not provided, use the IDs of the process
        """
        self.assertEquals(self._wrappedFunction()("", ""), (44, 45))

    def test_getSystemIDs_NamesSpecified(self):
        """
        If names are provided, use the IDs corresponding to those names
        """
        self.assertEquals(self._wrappedFunction()("exists", "exists"), (42, 43))


#
# Tests for PreProcessingService
#

class Step(object):

    def __init__(self, recordCallback, shouldFail):
        self._recordCallback = recordCallback
        self._shouldFail = shouldFail

    def stepWithResult(self, result):
        self._recordCallback(self.successValue, None)
        if self._shouldFail:
            1 / 0
        return succeed(result)

    def stepWithFailure(self, failure):
        self._recordCallback(self.errorValue, failure)
        if self._shouldFail:
            return failure


class StepOne(Step):
    successValue = "one success"
    errorValue = "one failure"


class StepTwo(Step):
    successValue = "two success"
    errorValue = "two failure"


class StepThree(Step):
    successValue = "three success"
    errorValue = "three failure"


class StepFour(Step):
    successValue = "four success"
    errorValue = "four failure"


class PreProcessingServiceTestCase(TestCase):

    def fakeServiceCreator(self, cp, store, lo, storageService):
        self.history.append(("serviceCreator", store, storageService))

    def setUp(self):
        self.history = []
        self.clock = Clock()
        self.pps = PreProcessingService(
            self.fakeServiceCreator, None, "store",
            None, "storageService", reactor=self.clock
        )

    def _record(self, value, failure):
        self.history.append(value)

    def test_allSuccess(self):
        self.pps.addStep(
            StepOne(self._record, False)
        ).addStep(
            StepTwo(self._record, False)
        ).addStep(
            StepThree(self._record, False)
        ).addStep(
            StepFour(self._record, False)
        )
        self.pps.startService()
        self.assertEquals(
            self.history,
            [
                'one success', 'two success', 'three success', 'four success',
                ('serviceCreator', 'store', 'storageService')
            ]
        )

    def test_allFailure(self):
        self.pps.addStep(
            StepOne(self._record, True)
        ).addStep(
            StepTwo(self._record, True)
        ).addStep(
            StepThree(self._record, True)
        ).addStep(
            StepFour(self._record, True)
        )
        self.pps.startService()
        self.assertEquals(
            self.history,
            [
                'one success', 'two failure', 'three failure', 'four failure',
                ('serviceCreator', None, 'storageService')
            ]
        )

    def test_partialFailure(self):
        self.pps.addStep(
            StepOne(self._record, True)
        ).addStep(
            StepTwo(self._record, False)
        ).addStep(
            StepThree(self._record, True)
        ).addStep(
            StepFour(self._record, False)
        )
        self.pps.startService()
        self.assertEquals(
            self.history,
            [
                'one success', 'two failure', 'three success', 'four failure',
                ('serviceCreator', 'store', 'storageService')
            ]
        )


class StubStorageService(object):

    def __init__(self):
        self.hardStopCalled = False

    def hardStop(self):
        self.hardStopCalled = True


class StubReactor(object):

    def __init__(self):
        self.stopCalled = False

    def stop(self):
        self.stopCalled = True


class DataStoreMonitorTestCase(TestCase):

    def test_monitor(self):
        storageService = StubStorageService()
        stubReactor = StubReactor()
        monitor = DataStoreMonitor(stubReactor, storageService)

        monitor.disconnected()
        self.assertTrue(storageService.hardStopCalled)
        self.assertTrue(stubReactor.stopCalled)

        storageService.hardStopCalled = False
        stubReactor.stopCalled = False
        monitor.deleted()
        self.assertTrue(storageService.hardStopCalled)
        self.assertTrue(stubReactor.stopCalled)

        storageService.hardStopCalled = False
        stubReactor.stopCalled = False
        monitor.renamed()
        self.assertTrue(storageService.hardStopCalled)
        self.assertTrue(stubReactor.stopCalled)
