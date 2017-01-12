##
# Copyright (c) 2005-2016 Apple Inc. All rights reserved.
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
from __future__ import print_function

import os
from plistlib import readPlistFromString
import plistlib
import xml

from twistedcaldav.stdconfig import config
from twext.python.filepath import CachingFilePath as FilePath
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, Deferred, returnValue
from twisted.trial.unittest import TestCase
from twistedcaldav import memcacher
from twistedcaldav.memcacheclient import ClientFactory
from twistedcaldav.test.util import CapturingProcessProtocol
from txdav.common.datastore.test.util import (
    theStoreBuilder, StubNotifierFactory
)
from txdav.who.idirectory import AutoScheduleMode
from txdav.who.util import directoryFromConfig


class RunCommandTestCase(TestCase):

    @inlineCallbacks
    def setUp(self):

        self.serverRoot = self.mktemp()
        os.mkdir(self.serverRoot)
        self.absoluteServerRoot = os.path.abspath(self.serverRoot)

        configRoot = os.path.join(self.absoluteServerRoot, "Config")
        if not os.path.exists(configRoot):
            os.makedirs(configRoot)

        dataRoot = os.path.join(self.absoluteServerRoot, "Data")
        if not os.path.exists(dataRoot):
            os.makedirs(dataRoot)

        documentRoot = os.path.join(self.absoluteServerRoot, "Documents")
        if not os.path.exists(documentRoot):
            os.makedirs(documentRoot)

        logRoot = os.path.join(self.absoluteServerRoot, "Logs")
        if not os.path.exists(logRoot):
            os.makedirs(logRoot)

        runRoot = os.path.join(self.absoluteServerRoot, "Run")
        if not os.path.exists(runRoot):
            os.makedirs(runRoot)

        config.reset()

        testRoot = os.path.join(os.path.dirname(__file__), "gateway")
        templateName = os.path.join(testRoot, "caldavd.plist")
        with open(templateName) as templateFile:
            template = templateFile.read()

        databaseRoot = os.path.abspath("_spawned_scripts_db" + str(os.getpid()))
        newConfig = template % {
            "ServerRoot": self.absoluteServerRoot,
            "DataRoot": dataRoot,
            "DatabaseRoot": databaseRoot,
            "DocumentRoot": documentRoot,
            "ConfigRoot": configRoot,
            "LogRoot": logRoot,
            "RunRoot": runRoot,
            "WritablePlist": os.path.join(
                os.path.abspath(configRoot), "caldavd-writable.plist"
            ),
        }
        configFilePath = FilePath(
            os.path.join(configRoot, "caldavd.plist")
        )

        configFilePath.setContent(newConfig)

        self.configFileName = configFilePath.path
        config.load(self.configFileName)

        config.Memcached.Pools.Default.ClientEnabled = False
        config.Memcached.Pools.Default.ServerEnabled = False
        ClientFactory.allowTestCache = True
        memcacher.Memcacher.allowTestCache = True
        memcacher.Memcacher.reset()
        config.DirectoryAddressBook.Enabled = False
        config.UsePackageTimezones = True

        origUsersFile = FilePath(
            os.path.join(
                os.path.dirname(__file__),
                "gateway",
                "users-groups.xml"
            )
        )
        copyUsersFile = FilePath(
            os.path.join(config.DataRoot, "accounts.xml")
        )
        origUsersFile.copyTo(copyUsersFile)

        origResourcesFile = FilePath(
            os.path.join(
                os.path.dirname(__file__),
                "gateway",
                "resources-locations.xml"
            )
        )
        copyResourcesFile = FilePath(
            os.path.join(config.DataRoot, "resources.xml")
        )
        origResourcesFile.copyTo(copyResourcesFile)

        origAugmentFile = FilePath(
            os.path.join(
                os.path.dirname(__file__),
                "gateway",
                "augments.xml"
            )
        )
        copyAugmentFile = FilePath(os.path.join(config.DataRoot, "augments.xml"))
        origAugmentFile.copyTo(copyAugmentFile)

        self.notifierFactory = StubNotifierFactory()
        self.store = yield theStoreBuilder.buildStore(self, self.notifierFactory)
        self.directory = directoryFromConfig(config, self.store)

    @inlineCallbacks
    def runCommand(
        self, command, error=False, script="calendarserver_command_gateway",
        additionalArgs=None, parseOutput=True
    ):
        """
        Run the given command by feeding it as standard input to
        calendarserver_command_gateway in a subprocess.
        """

        if isinstance(command, unicode):
            command = command.encode("utf-8")

        sourceRoot = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        cmd = script  # assumes it's on PATH

        args = [cmd, "-f", self.configFileName]
        if error:
            args.append("--error")

        if additionalArgs:
            args.extend(additionalArgs)

        cwd = sourceRoot

        deferred = Deferred()
        reactor.spawnProcess(CapturingProcessProtocol(deferred, command), cmd, args, env=os.environ, path=cwd)
        output = yield deferred
        if parseOutput:
            try:
                plist = readPlistFromString(output)
                returnValue(plist)
            except xml.parsers.expat.ExpatError, e:  # @UndefinedVariable
                print("Error (%s) parsing (%s)" % (e, output))
                raise
        else:
            returnValue(output)


class GatewayTestCase(RunCommandTestCase):

    def _flush(self):
        # Flush both XML directories
        self.directory._directory.services[1].flush()
        self.directory._directory.services[2].flush()

    @inlineCallbacks
    def test_getLocationAndResourceList(self):
        results = yield self.runCommand(command_getLocationAndResourceList)
        self.assertEquals(len(results["result"]), 20)

    @inlineCallbacks
    def test_getLocationList(self):
        results = yield self.runCommand(command_getLocationList)
        self.assertEquals(len(results["result"]), 10)

    @inlineCallbacks
    def test_getLocationAttributes(self):
        yield self.runCommand(command_createLocation)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        results = yield self.runCommand(command_getLocationAttributes)
        # self.assertEquals(results["result"]["Capacity"], "40")
        # self.assertEquals(results["result"]["Description"], "Test Description")
        self.assertEquals(results["result"]["RecordName"], ["createdlocation01"])
        self.assertEquals(
            results["result"]["RealName"],
            "Created Location 01 %s %s" % (unichr(208), u"\U0001f4a3"))
        # self.assertEquals(results["result"]["Comment"], "Test Comment")
        self.assertEquals(results["result"]["AutoScheduleMode"], u"acceptIfFree")
        self.assertEquals(results["result"]["AutoAcceptGroup"], "E5A6142C-4189-4E9E-90B0-9CD0268B314B")
        self.assertEquals(set(results["result"]["ReadProxies"]), set(['user03', 'user04']))
        self.assertEquals(set(results["result"]["WriteProxies"]), set(['user05', 'user06']))

    @inlineCallbacks
    def test_getResourceList(self):
        results = yield self.runCommand(command_getResourceList)
        self.assertEquals(len(results["result"]), 10)

    @inlineCallbacks
    def test_getResourceAttributes(self):
        yield self.runCommand(command_createResource)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        results = yield self.runCommand(command_getResourceAttributes)
        # self.assertEquals(results["result"]["Comment"], "Test Comment")
        # self.assertEquals(results["result"]["Type"], "Computer")
        self.assertEquals(set(results["result"]["ReadProxies"]), set(['user03', 'user04']))
        self.assertEquals(set(results["result"]["WriteProxies"]), set(['user05', 'user06']))

    @inlineCallbacks
    def test_createAddress(self):

        record = yield self.directory.recordWithUID("C701069D-9CA1-4925-A1A9-5CD94767B74B")
        self.assertEquals(record, None)
        yield self.runCommand(command_createAddress)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("C701069D-9CA1-4925-A1A9-5CD94767B74B")
        self.assertEquals(
            record.displayName,
            "Created Address 01 %s %s" % (unichr(208), u"\U0001f4a3")
        )

        self.assertEquals(record.abbreviatedName, "Addr1")
        self.assertEquals(record.streetAddress, "1 Infinite Loop\nCupertino, 95014\nCA")
        self.assertEquals(record.geographicLocation, "geo:37.331,-122.030")

        results = yield self.runCommand(command_getAddressList)
        self.assertEquals(len(results["result"]), 1)

        results = yield self.runCommand(command_getAddressAttributes)
        self.assertEquals(results["result"]["RealName"], u'Created Address 01 \xd0 \U0001f4a3')

        results = yield self.runCommand(command_setAddressAttributes)

        results = yield self.runCommand(command_getAddressAttributes)
        self.assertEquals(results["result"]["RealName"], u'Updated Address')
        self.assertEquals(results["result"]["StreetAddress"], u'Updated Street Address')
        self.assertEquals(results["result"]["GeographicLocation"], u'Updated Geo')

        results = yield self.runCommand(command_deleteAddress)

        results = yield self.runCommand(command_getAddressList)
        self.assertEquals(len(results["result"]), 0)

    @inlineCallbacks
    def test_createLocation(self):

        record = yield self.directory.recordWithUID("836B1B66-2E9A-4F46-8B1C-3DD6772C20B2")
        self.assertEquals(record, None)
        yield self.runCommand(command_createLocation)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("836B1B66-2E9A-4F46-8B1C-3DD6772C20B2")
        self.assertEquals(
            record.fullNames[0],
            u"Created Location 01 %s %s" % (unichr(208), u"\U0001f4a3"))

        self.assertNotEquals(record, None)
        # self.assertEquals(record.autoScheduleMode, "")

        self.assertEquals(record.floor, u"First")
        # self.assertEquals(record.extras["capacity"], "40")

        results = yield self.runCommand(command_getLocationAttributes)
        self.assertEquals(set(results["result"]["ReadProxies"]), set(['user03', 'user04']))
        self.assertEquals(set(results["result"]["WriteProxies"]), set(['user05', 'user06']))

    @inlineCallbacks
    def test_createLocationMinimal(self):
        """
        Ensure we can create a location with just the bare minimum of values,
        i.e. RealName
        """

        yield self.runCommand(command_createLocationMinimal)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        records = yield self.directory.recordsWithRecordType(self.directory.recordType.location)
        for record in records:
            if record.displayName == u"Minimal":
                break
        else:
            self.fail("We did not find the Minimal record")

    @inlineCallbacks
    def test_setLocationAttributes(self):

        yield self.runCommand(command_createLocation)
        yield self.runCommand(command_setLocationAttributes)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("836B1B66-2E9A-4F46-8B1C-3DD6772C20B2")

        # self.assertEquals(record.extras["comment"], "Updated Test Comment")
        self.assertEquals(record.floor, "Second")
        # self.assertEquals(record.extras["capacity"], "41")
        self.assertEquals(record.autoScheduleMode, AutoScheduleMode.acceptIfFree)
        self.assertEquals(record.autoAcceptGroup, "F5A6142C-4189-4E9E-90B0-9CD0268B314B")

        results = yield self.runCommand(command_getLocationAttributes)
        self.assertEquals(results["result"]["AutoScheduleMode"], "acceptIfFree")
        self.assertEquals(results["result"]["AutoAcceptGroup"], "F5A6142C-4189-4E9E-90B0-9CD0268B314B")
        self.assertEquals(set(results["result"]["ReadProxies"]), set(['user03']))
        self.assertEquals(set(results["result"]["WriteProxies"]), set(['user05', 'user06', 'user07']))

    @inlineCallbacks
    def test_setAddressOnLocation(self):
        yield self.runCommand(command_createLocation)
        yield self.runCommand(command_createAddress)
        yield self.runCommand(command_setAddressOnLocation)
        results = yield self.runCommand(command_getLocationAttributes)
        self.assertEquals(results["result"]["AssociatedAddress"], "C701069D-9CA1-4925-A1A9-5CD94767B74B")
        self._flush()
        record = yield self.directory.recordWithUID("836B1B66-2E9A-4F46-8B1C-3DD6772C20B2")
        self.assertEquals(record.associatedAddress, "C701069D-9CA1-4925-A1A9-5CD94767B74B")
        yield self.runCommand(command_removeAddressFromLocation)
        results = yield self.runCommand(command_getLocationAttributes)
        self.assertEquals(results["result"]["AssociatedAddress"], "")
        self._flush()
        record = yield self.directory.recordWithUID("836B1B66-2E9A-4F46-8B1C-3DD6772C20B2")
        self.assertEquals(record.associatedAddress, u"")

    @inlineCallbacks
    def test_destroyLocation(self):

        record = yield self.directory.recordWithUID("location01")
        self.assertNotEquals(record, None)

        yield self.runCommand(command_deleteLocation)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("location01")
        self.assertEquals(record, None)

    @inlineCallbacks
    def test_createResource(self):

        record = yield self.directory.recordWithUID("AF575A61-CFA6-49E1-A0F6-B5662C9D9801")
        self.assertEquals(record, None)

        yield self.runCommand(command_createResource)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("AF575A61-CFA6-49E1-A0F6-B5662C9D9801")
        self.assertNotEquals(record, None)

    @inlineCallbacks
    def test_setResourceAttributes(self):

        yield self.runCommand(command_createResource)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("AF575A61-CFA6-49E1-A0F6-B5662C9D9801")
        self.assertEquals(record.displayName, "Laptop 1")

        yield self.runCommand(command_setResourceAttributes)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("AF575A61-CFA6-49E1-A0F6-B5662C9D9801")
        self.assertEquals(record.displayName, "Updated Laptop 1")

    @inlineCallbacks
    def test_destroyResource(self):

        record = yield self.directory.recordWithUID("resource01")
        self.assertNotEquals(record, None)

        yield self.runCommand(command_deleteResource)

        # Tell the resources services to flush its cache and re-read XML
        self._flush()

        record = yield self.directory.recordWithUID("resource01")
        self.assertEquals(record, None)

    @inlineCallbacks
    def test_addWriteProxy(self):
        results = yield self.runCommand(command_addWriteProxy)
        self.assertEquals(len(results["result"]["Proxies"]), 1)

    @inlineCallbacks
    def test_removeWriteProxy(self):
        yield self.runCommand(command_addWriteProxy)
        results = yield self.runCommand(command_removeWriteProxy)
        self.assertEquals(len(results["result"]["Proxies"]), 0)

    @inlineCallbacks
    def test_purgeOldEvents(self):
        results = yield self.runCommand(command_purgeOldEvents)
        self.assertEquals(results["result"]["EventsRemoved"], 0)
        self.assertEquals(results["result"]["RetainDays"], 42)
        results = yield self.runCommand(command_purgeOldEventsNoDays)
        self.assertEquals(results["result"]["RetainDays"], 365)

    @inlineCallbacks
    def test_readConfig(self):
        """
        Verify readConfig returns with only the writable keys
        """
        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config"
        )
        self.assertEquals(results["result"]["RedirectHTTPToHTTPS"], False)
        self.assertEquals(results["result"]["EnableSearchAddressBook"], False)
        self.assertEquals(results["result"]["EnableCalDAV"], True)
        self.assertEquals(results["result"]["EnableCardDAV"], True)
        self.assertEquals(results["result"]["EnableSSL"], False)
        self.assertEquals(results["result"]["DefaultLogLevel"], "warn")

        self.assertEquals(results["result"]["Notifications"]["Services"]["APNS"]["Enabled"], False)

        # This is a read only key that is returned
        self.assertEquals(results["result"]["ServerRoot"], self.absoluteServerRoot)

        # Verify other non-writeable keys are not present, such as DataRoot which is not writable
        self.assertFalse("DataRoot" in results["result"])

    @inlineCallbacks
    def test_writeConfig(self):
        """
        Verify writeConfig updates the writable plist file only
        """
        results = yield self.runCommand(
            command_writeConfig,
            script="calendarserver_config"
        )

        self.assertEquals(results["result"]["EnableCalDAV"], False)
        self.assertEquals(results["result"]["EnableCardDAV"], False)
        self.assertEquals(results["result"]["EnableSSL"], True)
        self.assertEquals(results["result"]["Notifications"]["Services"]["APNS"]["Enabled"], True)
        hostName = "hostname_%s_%s" % (unichr(208), u"\U0001f4a3")
        self.assertTrue(results["result"]["ServerHostName"].endswith(hostName))

        # The static plist should still have EnableCalDAV = True
        staticPlist = plistlib.readPlist(self.configFileName)
        self.assertTrue(staticPlist["EnableCalDAV"])


command_addReadProxy = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>addReadProxy</string>
        <key>Principal</key>
        <string>location01</string>
        <key>Proxy</key>
        <string>user03</string>
</dict>
</plist>
"""

command_addWriteProxy = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>addWriteProxy</string>
        <key>Principal</key>
        <string>location01</string>
        <key>Proxy</key>
        <string>user01</string>
</dict>
</plist>
"""

command_createAddress = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>createAddress</string>
        <key>GeneratedUID</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
        <key>RealName</key>
        <string>Created Address 01 %s %s</string>
        <key>AbbreviatedName</key>
        <string>Addr1</string>
        <key>RecordName</key>
        <array>
                <string>createdaddress01</string>
        </array>
        <key>StreetAddress</key>
        <string>1 Infinite Loop\nCupertino, 95014\nCA</string>
        <key>GeographicLocation</key>
        <string>geo:37.331,-122.030</string>
</dict>
</plist>
""" % (unichr(208), u"\U0001f4a3")


command_createLocation = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>createLocation</string>
        <key>AutoScheduleMode</key>
        <string>acceptIfFree</string>
        <key>AutoAcceptGroup</key>
        <string>E5A6142C-4189-4E9E-90B0-9CD0268B314B</string>
        <key>GeneratedUID</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
        <key>RealName</key>
        <string>Created Location 01 %s %s</string>
        <key>RecordName</key>
        <array>
                <string>createdlocation01</string>
        </array>
        <key>Comment</key>
        <string>Test Comment</string>
        <key>Description</key>
        <string>Test Description</string>
        <key>Floor</key>
        <string>First</string>
        <!--
        <key>Capacity</key>
        <string>40</string>
        -->
        <key>AssociatedAddress</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
        <key>ReadProxies</key>
        <array>
            <string>user03</string>
            <string>user04</string>
        </array>
        <key>WriteProxies</key>
        <array>
            <string>user05</string>
            <string>user06</string>
        </array>
</dict>
</plist>
""" % (unichr(208), u"\U0001f4a3")


command_createLocationMinimal = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>createLocation</string>
        <key>RealName</key>
        <string>Minimal</string>
</dict>
</plist>
"""


command_createResource = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>createResource</string>
        <key>AutoScheduleMode</key>
        <string>declineIfBusy</string>
        <key>GeneratedUID</key>
        <string>AF575A61-CFA6-49E1-A0F6-B5662C9D9801</string>
        <key>RealName</key>
        <string>Laptop 1</string>
        <!--
        <key>Comment</key>
        <string>Test Comment</string>
        <key>Description</key>
        <string>Test Description</string>
        <key>Type</key>
        <string>Computer</string>
        -->
        <key>RecordName</key>
        <array>
                <string>laptop1</string>
        </array>
        <key>ReadProxies</key>
        <array>
            <string>user03</string>
            <string>user04</string>
        </array>
        <key>WriteProxies</key>
        <array>
            <string>user05</string>
            <string>user06</string>
        </array>
</dict>
</plist>
"""

command_deleteLocation = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>deleteLocation</string>
        <key>GeneratedUID</key>
        <string>location01</string>
</dict>
</plist>
"""

command_deleteResource = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>deleteResource</string>
        <key>GeneratedUID</key>
        <string>resource01</string>
</dict>
</plist>
"""


command_deleteAddress = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>deleteAddress</string>
        <key>GeneratedUID</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
</dict>
</plist>
"""

command_getLocationAndResourceList = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getLocationAndResourceList</string>
</dict>
</plist>
"""

command_getLocationList = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getLocationList</string>
</dict>
</plist>
"""

command_getResourceList = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getResourceList</string>
</dict>
</plist>
"""

command_getAddressList = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getAddressList</string>
</dict>
</plist>
"""


command_listReadProxies = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>listReadProxies</string>
        <key>Principal</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
</dict>
</plist>
"""

command_listWriteProxies = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>listWriteProxies</string>
        <key>Principal</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
</dict>
</plist>
"""

command_removeReadProxy = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>removeReadProxy</string>
        <key>Principal</key>
        <string>location01</string>
        <key>Proxy</key>
        <string>user03</string>
</dict>
</plist>
"""

command_removeWriteProxy = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>removeWriteProxy</string>
        <key>Principal</key>
        <string>location01</string>
        <key>Proxy</key>
        <string>user01</string>
</dict>
</plist>
"""

command_setLocationAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>setLocationAttributes</string>
        <key>AutoSchedule</key>
        <true/>
        <key>AutoAcceptGroup</key>
        <string>F5A6142C-4189-4E9E-90B0-9CD0268B314B</string>
        <key>GeneratedUID</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
        <key>RealName</key>
        <string>Updated Location 01</string>
        <key>RecordName</key>
        <array>
                <string>createdlocation01</string>
        </array>
        <key>Comment</key>
        <string>Updated Test Comment</string>
        <key>Description</key>
        <string>Updated Test Description</string>
        <key>Floor</key>
        <string>Second</string>
        <!--
        <key>Capacity</key>
        <string>41</string>
        -->
        <key>ReadProxies</key>
        <array>
            <string>user03</string>
        </array>
        <key>WriteProxies</key>
        <array>
            <string>user05</string>
            <string>user06</string>
            <string>user07</string>
        </array>
</dict>
</plist>
"""

command_setAddressOnLocation = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>setLocationAttributes</string>
        <key>GeneratedUID</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
        <key>AssociatedAddress</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
</dict>
</plist>
"""

command_removeAddressFromLocation = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>setLocationAttributes</string>
        <key>GeneratedUID</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
        <key>AssociatedAddress</key>
        <string></string>
</dict>
</plist>
"""


command_getLocationAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getLocationAttributes</string>
        <key>GeneratedUID</key>
        <string>836B1B66-2E9A-4F46-8B1C-3DD6772C20B2</string>
</dict>
</plist>
"""

command_getAddressAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getAddressAttributes</string>
        <key>GeneratedUID</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
</dict>
</plist>
"""

command_setAddressAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>setAddressAttributes</string>
        <key>GeneratedUID</key>
        <string>C701069D-9CA1-4925-A1A9-5CD94767B74B</string>
        <key>RealName</key>
        <string>Updated Address</string>
        <key>StreetAddress</key>
        <string>Updated Street Address</string>
        <key>GeographicLocation</key>
        <string>Updated Geo</string>

</dict>
</plist>
"""


command_setResourceAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>setResourceAttributes</string>
        <key>AutoScheduleMode</key>
        <string>acceptIfFree</string>
        <key>GeneratedUID</key>
        <string>AF575A61-CFA6-49E1-A0F6-B5662C9D9801</string>
        <key>RealName</key>
        <string>Updated Laptop 1</string>
        <key>RecordName</key>
        <array>
                <string>laptop1</string>
        </array>
</dict>
</plist>
"""

command_getResourceAttributes = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getResourceAttributes</string>
        <key>GeneratedUID</key>
        <string>AF575A61-CFA6-49E1-A0F6-B5662C9D9801</string>
</dict>
</plist>
"""

command_purgeOldEvents = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>purgeOldEvents</string>
        <key>RetainDays</key>
        <integer>42</integer>
</dict>
</plist>
"""

command_purgeOldEventsNoDays = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>purgeOldEvents</string>
</dict>
</plist>
"""

command_readConfig = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>readConfig</string>
</dict>
</plist>
"""

command_writeConfig = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>writeConfig</string>
        <key>Values</key>
        <dict>
            <key>EnableCalDAV</key>
            <false/>
            <key>EnableCardDAV</key>
            <false/>
            <key>EnableSSL</key>
            <true/>
            <key>Notifications.Services.APNS.Enabled</key>
            <true/>
            <key>ServerHostName</key>
            <string>hostname_%s_%s</string>
        </dict>
</dict>
</plist>
""" % (unichr(208), u"\U0001f4a3")
