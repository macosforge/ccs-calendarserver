##
# Copyright (c) 2013-2016 Apple Inc. All rights reserved.
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

from twistedcaldav.test.util import TestCase
from twistedcaldav.config import ConfigDict
from calendarserver.tools.config import (
    WritableConfig, setKeyPath, getKeyPath,
    flattenDictionary, processArgs)
from calendarserver.tools.test.test_gateway import RunCommandTestCase
from twisted.internet.defer import inlineCallbacks
from twisted.python.filepath import FilePath
from xml.parsers.expat import ExpatError
import plistlib

PREAMBLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
"""


class WritableConfigTestCase(TestCase):

    def setUp(self):
        self.configFile = self.mktemp()
        self.fp = FilePath(self.configFile)

    def test_readSuccessful(self):
        content = """<plist version="1.0">
    <dict>
        <key>string</key>
        <string>foo</string>
    </dict>
</plist>"""
        self.fp.setContent(PREAMBLE + content)

        config = ConfigDict()
        writable = WritableConfig(config, self.configFile)
        writable.read()
        self.assertEquals(writable.currentConfigSubset, {"string": "foo"})

    def test_readInvalidXML(self):
        self.fp.setContent("invalid")
        config = ConfigDict()
        writable = WritableConfig(config, self.configFile)
        self.assertRaises(ExpatError, writable.read)

    def test_updates(self):
        content = """<plist version="1.0">
    <dict>
        <key>key1</key>
        <string>before</string>
        <key>key2</key>
        <integer>10</integer>
    </dict>
</plist>"""
        self.fp.setContent(PREAMBLE + content)
        config = ConfigDict()
        writable = WritableConfig(config, self.configFile)
        writable.read()
        writable.set({"key1": "after"})
        writable.set({"key2": 15})
        writable.set({"key2": 20})  # override previous set
        writable.set({"key3": ["a", "b", "c"]})
        self.assertEquals(writable.currentConfigSubset, {"key1": "after", "key2": 20, "key3": ["a", "b", "c"]})
        writable.save()

        writable2 = WritableConfig(config, self.configFile)
        writable2.read()
        self.assertEquals(writable2.currentConfigSubset, {"key1": "after", "key2": 20, "key3": ["a", "b", "c"]})

    def test_convertToValue(self):
        self.assertEquals(True, WritableConfig.convertToValue("True"))
        self.assertEquals(False, WritableConfig.convertToValue("False"))
        self.assertEquals(1, WritableConfig.convertToValue("1"))
        self.assertEquals(1.2, WritableConfig.convertToValue("1.2"))
        self.assertEquals("xyzzy", WritableConfig.convertToValue("xyzzy"))
        self.assertEquals("xy.zzy", WritableConfig.convertToValue("xy.zzy"))

    def test_processArgs(self):
        """
        Ensure utf-8 encoded command line args are handled properly
        """
        content = """<plist version="1.0">
    <dict>
        <key>key1</key>
        <string>before</string>
    </dict>
</plist>"""
        self.fp.setContent(PREAMBLE + content)
        config = ConfigDict()
        writable = WritableConfig(config, self.configFile)
        writable.read()
        processArgs(writable, ["key1=\xf0\x9f\x92\xa3"], restart=False)
        writable2 = WritableConfig(config, self.configFile)
        writable2.read()
        self.assertEquals(writable2.currentConfigSubset, {'key1': u'\U0001f4a3'})


class ConfigTestCase(RunCommandTestCase):

    @inlineCallbacks
    def test_readConfig(self):
        """
        Verify readConfig returns with only the writable keys
        """
        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(results["result"]["RedirectHTTPToHTTPS"], False)
        self.assertEquals(results["result"]["EnableSearchAddressBook"], False)
        self.assertEquals(results["result"]["EnableCalDAV"], True)
        self.assertEquals(results["result"]["EnableCardDAV"], True)
        self.assertEquals(results["result"]["EnableSSL"], False)
        self.assertEquals(results["result"]["DefaultLogLevel"], "warn")

        self.assertEquals(results["result"]["Notifications"]["Services"]["APNS"]["Enabled"], False)

        # Verify not all keys are present, such as umask which is not accessible
        self.assertFalse("umask" in results["result"])

    @inlineCallbacks
    def test_writeConfig(self):
        """
        Verify writeConfig updates the writable plist file only
        """
        results = yield self.runCommand(
            command_writeConfig,
            script="calendarserver_config")

        self.assertEquals(results["result"]["EnableCalDAV"], False)
        self.assertEquals(results["result"]["EnableCardDAV"], False)
        self.assertEquals(results["result"]["EnableSSL"], True)
        self.assertEquals(results["result"]["Notifications"]["Services"]["APNS"]["Enabled"], True)
        hostName = "hostname_%s_%s" % (unichr(208), u"\U0001f4a3")
        self.assertTrue(results["result"]["ServerHostName"].endswith(hostName))

        # We tried to modify ServerRoot, but make sure our change did not take
        self.assertNotEquals(results["result"]["ServerRoot"], "/You/Shall/Not/Pass")

        # The static plist should still have EnableCalDAV = True
        staticPlist = plistlib.readPlist(self.configFileName)
        self.assertTrue(staticPlist["EnableCalDAV"])

    @inlineCallbacks
    def test_error(self):
        """
        Verify sending a bogus command returns an error
        """
        results = yield self.runCommand(
            command_bogusCommand,
            script="calendarserver_config")
        self.assertEquals(results["error"], "Unknown command 'bogus'")

    @inlineCallbacks
    def test_commandLineArgs(self):
        """
        Verify commandline assignments works
        """

        # Check current values

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(results["result"]["DefaultLogLevel"], "warn")
        self.assertEquals(results["result"]["Scheduling"]["iMIP"]["Enabled"], False)

        # Set a single-level key and a multi-level key

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["DefaultLogLevel=debug", "Scheduling.iMIP.Enabled=True"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(results["result"]["DefaultLogLevel"], "debug")
        self.assertEquals(results["result"]["Scheduling"]["iMIP"]["Enabled"], True)
        self.assertEquals(results["result"]["LogLevels"], {})

        # Directly set some LogLevels sub-keys

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["LogLevels.testing=debug", "LogLevels.testing2=warn"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"],
            {"testing": "debug", "testing2": "warn"}
        )

        # Test that setting additional sub-keys retains previous sub-keys

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["LogLevels.testing3=info", "LogLevels.testing4=warn"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"],
            {"testing": "debug", "testing2": "warn", "testing3": "info", "testing4": "warn"}
        )

        # Test that an empty value deletes the key

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["LogLevels="],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"],  # now an empty dict
            {}
        )

    @inlineCallbacks
    def test_loggingCategories(self):
        """
        Verify logging categories get mapped to the correct python module names
        """

        # Check existing values

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(results["result"]["LogLevels"], {})

        # Set to directory and imip

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["--logging=directory,imip"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"],
            {
                "twext.who": "debug",
                "txdav.who": "debug",
                "txdav.caldav.datastore.scheduling.imip": "debug",
            }
        )

        # Set up to imip, and the directory modules should go away

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["--logging=imip"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"],
            {
                "txdav.caldav.datastore.scheduling.imip": "debug",
            }
        )

        # Set to default, and all modules should go away

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["--logging=default"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["LogLevels"], {}
        )

    @inlineCallbacks
    def test_accountingCategories(self):
        """
        Verify accounting categories get mapped to the correct keys
        """
        # Check existing values

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["AccountingCategories"],
            {
                'AutoScheduling': False,
                'HTTP': False,
                'Implicit Errors': False,
                'iSchedule': False,
                'xPod': False,
                'iTIP': False,
                'iTIP-VFREEBUSY': False,
                'Invalid Instance': False,
                'migration': False,
            }
        )

        # Set to http and itip

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["--accounting=http,itip"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["AccountingCategories"],
            {
                'AutoScheduling': False,
                'HTTP': True,
                'Implicit Errors': False,
                'iSchedule': False,
                'xPod': False,
                'iTIP': True,
                'iTIP-VFREEBUSY': True,
                'Invalid Instance': False,
                'migration': False,
            }
        )

        # Set to off, so all are off

        results = yield self.runCommand(
            None,
            script="calendarserver_config",
            additionalArgs=["--accounting=off"],
            parseOutput=False
        )

        results = yield self.runCommand(
            command_readConfig,
            script="calendarserver_config")

        self.assertEquals(
            results["result"]["AccountingCategories"],
            {
                'AutoScheduling': False,
                'HTTP': False,
                'Implicit Errors': False,
                'iSchedule': False,
                'xPod': False,
                'iTIP': False,
                'iTIP-VFREEBUSY': False,
                'Invalid Instance': False,
                'migration': False,
            }
        )

    def test_keyPath(self):
        d = ConfigDict()
        setKeyPath(d, "one", "A")
        setKeyPath(d, "one", "B")
        setKeyPath(d, "two.one", "C")
        setKeyPath(d, "two.one", "D")
        setKeyPath(d, "two.two", "E")
        setKeyPath(d, "three.one.one", "F")
        setKeyPath(d, "three.one.two", "G")

        self.assertEquals(d.one, "B")
        self.assertEquals(d.two.one, "D")
        self.assertEquals(d.two.two, "E")
        self.assertEquals(d.three.one.one, "F")
        self.assertEquals(d.three.one.two, "G")

        self.assertEquals(getKeyPath(d, "one"), "B")
        self.assertEquals(getKeyPath(d, "two.one"), "D")
        self.assertEquals(getKeyPath(d, "two.two"), "E")
        self.assertEquals(getKeyPath(d, "three.one.one"), "F")
        self.assertEquals(getKeyPath(d, "three.one.two"), "G")

    def test_flattenDictionary(self):
        dictionary = {
            "one": "A",
            "two": {
                "one": "D",
                "two": "E",
            },
            "three": {
                "one": {
                    "one": "F",
                    "two": "G",
                },
            },
        }
        self.assertEquals(
            set(list(flattenDictionary(dictionary))),
            set([("one", "A"), ("three.one.one", "F"), ("three.one.two", "G"), ("two.one", "D"), ("two.two", "E")])
        )


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
            <key>ServerRoot</key>
            <string>/You/Shall/Not/Pass</string>
            <key>ServerHostName</key>
            <string>hostname_%s_%s</string>
        </dict>
</dict>
</plist>
""" % (unichr(208), u"\U0001f4a3")

command_bogusCommand = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>bogus</string>
</dict>
</plist>
"""
