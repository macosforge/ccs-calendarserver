#!/usr/bin/env python

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
##
from __future__ import print_function

# Suppress warning that occurs on Linux
import sys
if sys.platform.startswith("linux"):
    from Crypto.pct_warnings import PowmInsecureWarning
    import warnings
    warnings.simplefilter("ignore", PowmInsecureWarning)

"""
This tool gets and sets Calendar Server configuration keys
"""

from getopt import getopt, GetoptError
from itertools import chain
import os
import plistlib
import signal
import subprocess
import xml

from plistlib import readPlistFromString, writePlistToString
from twistedcaldav.config import config, ConfigDict, ConfigurationError, mergeData
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from twisted.python.filepath import FilePath


WRITABLE_CONFIG_KEYS = [
    "AccountingCategories",
    "Authentication.Basic.AllowedOverWireUnencrypted",
    "Authentication.Basic.Enabled",
    "Authentication.Digest.AllowedOverWireUnencrypted",
    "Authentication.Digest.Enabled",
    "Authentication.Kerberos.AllowedOverWireUnencrypted",
    "Authentication.Kerberos.Enabled",
    "Authentication.Wiki.Enabled",
    "BehindTLSProxy",
    "DefaultLogLevel",
    "DirectoryAddressBook.params.queryPeopleRecords",
    "DirectoryAddressBook.params.queryUserRecords",
    "EnableCalDAV",
    "EnableCardDAV",
    "EnableSACLs",
    "EnableSearchAddressBook",
    "EnableSSL",
    "HTTPPort",
    "LogLevels",
    "Notifications.Services.APNS.Enabled",
    "RedirectHTTPToHTTPS",
    "Scheduling.iMIP.Enabled",
    "Scheduling.iMIP.Receiving.Port",
    "Scheduling.iMIP.Receiving.Server",
    "Scheduling.iMIP.Receiving.Type",
    "Scheduling.iMIP.Receiving.Username",
    "Scheduling.iMIP.Receiving.UseSSL",
    "Scheduling.iMIP.Sending.Address",
    "Scheduling.iMIP.Sending.Port",
    "Scheduling.iMIP.Sending.Server",
    "Scheduling.iMIP.Sending.Username",
    "Scheduling.iMIP.Sending.UseSSL",
    "ServerHostName",
    "SSLAuthorityChain",
    "SSLCertificate",
    "SSLPort",
    "SSLPrivateKey",
    "SSLKeychainIdentity",
]

READONLY_CONFIG_KEYS = [
    "ServerRoot",
]

ACCOUNTING_CATEGORIES = {
    "http": ("HTTP",),
    "itip": ("iTIP", "iTIP-VFREEBUSY",),
    "implicit": ("Implicit Errors",),
    "autoscheduling": ("AutoScheduling",),
    "ischedule": ("iSchedule",),
    "migration": ("migration",),
}

LOGGING_CATEGORIES = {
    "directory": ("twext^who", "txdav^who",),
    "imip": ("txdav^caldav^datastore^scheduling^imip",),
}


def usage(e=None):
    if e:
        print(e)
        print("")

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] config_key" % (name,))
    print("")
    print("Print the value of the given config key.")
    print("options:")
    print("  -a --accounting: Specify accounting categories (combinations of http, itip, implicit, autoscheduling, ischedule, migration, or off)")
    print("  -h --help: print this help and exit")
    print("  -f --config: Specify caldavd.plist configuration path")
    print("  -l --logging: Specify logging categories (combinations of directory, imip, or default)")
    print("  -r --restart: Restart the calendar service")
    print("  --start: Start the calendar service and agent")
    print("  --stop: Stop the calendar service and agent")
    print("  -w --writeconfig: Specify caldavd.plist configuration path for writing")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def runAsRootCheck():
    """
    If we're running in Server.app context and are not running as root, exit.
    """

    if os.path.abspath(__file__).startswith("/Applications/Server.app/"):
        if os.getuid() != 0:
            print("Must be run as root")
            sys.exit(1)


def main():

    runAsRootCheck()

    try:
        (optargs, args) = getopt(
            sys.argv[1:], "hf:rw:a:l:", [
                "help",
                "config=",
                "writeconfig=",
                "restart",
                "start",
                "stop",
                "accounting=",
                "logging=",
            ],
        )
    except GetoptError as e:
        usage(e)

    configFileName = DEFAULT_CONFIG_FILE
    writeConfigFileName = ""
    accountingCategories = None
    loggingCategories = None
    doStop = False
    doStart = False
    doRestart = False

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

        elif opt in ("-w", "--writeconfig"):
            writeConfigFileName = arg

        elif opt in ("-a", "--accounting"):
            accountingCategories = arg.split(",")

        elif opt in ("-l", "--logging"):
            loggingCategories = arg.split(",")

        if opt == "--stop":
            doStop = True

        if opt == "--start":
            doStart = True

        if opt in ("-r", "--restart"):
            doRestart = True

    try:
        config.load(configFileName)
    except ConfigurationError as e:
        sys.stdout.write("%s\n" % (e,))
        sys.exit(1)

    if not writeConfigFileName:
        # If --writeconfig was not passed, use WritableConfigFile from
        # main plist.  If that's an empty string, writes will happen to
        # the main file.
        writeConfigFileName = config.WritableConfigFile

    if not writeConfigFileName:
        writeConfigFileName = configFileName

    if doStop:
        setServiceState("org.calendarserver.agent", "disable")
        setServiceState("org.calendarserver.calendarserver", "disable")
        setEnabled(False)

    if doStart:
        setServiceState("org.calendarserver.agent", "enable")
        setServiceState("org.calendarserver.calendarserver", "enable")
        setEnabled(True)

    if doStart or doStop:
        setReverseProxies()
        sys.exit(0)

    if doRestart:
        restartService(config.PIDFile)
        sys.exit(0)

    writable = WritableConfig(config, writeConfigFileName)
    writable.read()

    # Convert logging categories to actual config key changes
    if loggingCategories is not None:
        args = ["LogLevels="]
        if loggingCategories != ["default"]:
            for cat in loggingCategories:
                if cat in LOGGING_CATEGORIES:
                    for moduleName in LOGGING_CATEGORIES[cat]:
                        args.append("LogLevels.{}=debug".format(moduleName))

    # Convert accounting categories to actual config key changes
    if accountingCategories is not None:
        args = ["AccountingCategories="]
        if accountingCategories != ["off"]:
            for cat in accountingCategories:
                if cat in ACCOUNTING_CATEGORIES:
                    for key in ACCOUNTING_CATEGORIES[cat]:
                        args.append("AccountingCategories.{}=True".format(key))

    processArgs(writable, args)


def setServiceState(service, state):
    """
    Invoke serverctl to enable/disable a service
    """
    SERVERCTL = "/Applications/Server.app/Contents/ServerRoot/usr/sbin/serverctl"
    child = subprocess.Popen(
        args=[SERVERCTL, state, "service={}".format(service)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _ignore_output, error = child.communicate()
    if child.returncode:
        sys.stdout.write(
            "Error from serverctl: %d, %s" % (child.returncode, error)
        )


def setEnabled(enabled):
    command = {
        "command": "writeConfig",
        "Values": {
            "EnableCalDAV": enabled,
            "EnableCardDAV": enabled,
        },
    }

    runner = Runner([command], quiet=True)
    runner.run()


def setReverseProxies():
    """
    Invoke calendarserver_reverse_proxies
    """
    SERVERCTL = "/Applications/Server.app/Contents/ServerRoot/usr/libexec/calendarserver_reverse_proxies"
    child = subprocess.Popen(
        args=[SERVERCTL, ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child.communicate()


def processArgs(writable, args, restart=True):
    """
    Perform the read/write operations requested in the command line args.
    If there are no args, stdin is read, and plist-formatted commands are
    processed from there.
    @param writable: the WritableConfig
    @param args: a list of utf-8 encoded strings
    @param restart: whether to restart the calendar server after making a
        config change.
    """
    if args:
        for configKey in args:
            # args come in as utf-8 encoded strings
            configKey = configKey.decode("utf-8")

            if "=" in configKey:
                # This is an assignment
                configKey, stringValue = configKey.split("=")
                if stringValue:
                    value = writable.convertToValue(stringValue)
                    valueDict = setKeyPath({}, configKey, value)
                    writable.set(valueDict)
                else:
                    writable.delete(configKey)
            else:
                # This is a read
                c = config
                for subKey in configKey.split("."):
                    c = c.get(subKey, None)
                    if c is None:
                        sys.stderr.write("No such config key: %s\n" % configKey)
                        break
                sys.stdout.write("%s=%s\n" % (configKey.encode("utf-8"), c))

        writable.save(restart=restart)

    else:
        # Read plist commands from stdin
        rawInput = sys.stdin.read()
        try:
            plist = readPlistFromString(rawInput)
            # Note: values in plist will already be unicode
        except xml.parsers.expat.ExpatError as e:  # @UndefinedVariable
            respondWithError(str(e))
            return

        # If the plist is an array, each element of the array is a separate
        # command dictionary.
        if isinstance(plist, list):
            commands = plist
        else:
            commands = [plist]

        runner = Runner(commands)
        runner.run()


class Runner(object):

    """
    A class which carries out commands, which are plist strings containing
    dictionaries with a "command" key, plus command-specific data.
    """

    def __init__(self, commands, quiet=False):
        """
        @param commands: the commands to run
        @type commands: list of plist strings
        """
        self.commands = commands
        self.quiet = quiet

    def validate(self):
        """
        Validate all the commands by making sure this class implements
        all the command keys.
        @return: True if all commands are valid, False otherwise
        """
        # Make sure commands are valid
        for command in self.commands:
            if 'command' not in command:
                respondWithError("'command' missing from plist")
                return False
            commandName = command['command']
            methodName = "command_%s" % (commandName,)
            if not hasattr(self, methodName):
                respondWithError("Unknown command '%s'" % (commandName,))
                return False
        return True

    def run(self):
        """
        Find the appropriate method for each command and call them.
        """
        try:
            for command in self.commands:
                commandName = command['command']
                methodName = "command_%s" % (commandName,)
                if hasattr(self, methodName):
                    getattr(self, methodName)(command)
                else:
                    respondWithError("Unknown command '%s'" % (commandName,))

        except Exception as e:
            respondWithError("Command failed: '%s'" % (str(e),))
            raise

    def command_readConfig(self, command):
        """
        Return current configuration

        @param command: the dictionary parsed from the plist read from stdin
        @type command: C{dict}
        """
        result = {}
        for keyPath in chain(WRITABLE_CONFIG_KEYS, READONLY_CONFIG_KEYS):
            value = getKeyPath(config, keyPath)
            if value is not None:
                # Note: config contains utf-8 encoded strings, but plistlib
                # wants unicode, so decode here:
                if isinstance(value, str):
                    value = value.decode("utf-8")
                setKeyPath(result, keyPath, value)
        respond(command, result)

    def command_writeConfig(self, command):
        """
        Write config to secondary, writable plist

        @param command: the dictionary parsed from the plist read from stdin
        @type command: C{dict}
        """
        writable = WritableConfig(config, config.WritableConfigFile)
        writable.read()
        valuesToWrite = command.get("Values", {})
        # Note: values are unicode if they contain non-ascii
        for keyPath, value in flattenDictionary(valuesToWrite):
            if keyPath in WRITABLE_CONFIG_KEYS:
                writable.set(setKeyPath(ConfigDict(), keyPath, value))
        try:
            writable.save(restart=False)
        except Exception as e:
            respond(command, {"error": str(e)})
        else:
            config.reload()
            if not self.quiet:
                self.command_readConfig(command)


def setKeyPath(parent, keyPath, value):
    """
    Allows the setting of arbitrary nested dictionary keys via a single
    dot-separated string.  For example, setKeyPath(parent, "foo.bar.baz",
    "xyzzy") would create any intermediate missing directories (or whatever
    class parent is, such as ConfigDict) so that the following structure
    results:  parent = { "foo" : { "bar" : { "baz" : "xyzzy } } }

    @param parent: the object to modify
    @type parent: any dict-like object
    @param keyPath: a dot-delimited string specifying the path of keys to
        traverse
    @type keyPath: C{str}
    @param value: the value to set
    @type value: c{object}
    @return: parent
    """
    original = parent

    # Embedded ^ should be replaced by .
    # That way, we can still use . as key path separator even though sometimes
    # a key part wants to have a . in it (such as a LogLevels module).
    # For example:  LogLevels.twext^who, will get converted to a "LogLevels"
    # dict containing a the key "twext.who"
    parts = [p.replace("^", ".") for p in keyPath.split(".")]

    for part in parts[:-1]:
        child = parent.get(part, None)
        if child is None:
            parent[part] = child = parent.__class__()
        parent = child
    parent[parts[-1]] = value
    return original


def getKeyPath(parent, keyPath):
    """
    Allows the getting of arbitrary nested dictionary keys via a single
    dot-separated string.  For example, getKeyPath(parent, "foo.bar.baz")
    would fetch parent["foo"]["bar"]["baz"].  If any of the keys don't
    exist, None is returned instead.

    @param parent: the object to traverse
    @type parent: any dict-like object
    @param keyPath: a dot-delimited string specifying the path of keys to
        traverse
    @type keyPath: C{str}
    @return: the value at keyPath
    """
    parts = keyPath.split(".")
    for part in parts[:-1]:
        child = parent.get(part, None)
        if child is None:
            return None
        parent = child
    return parent.get(parts[-1], None)


def flattenDictionary(dictionary, current=""):
    """
    Returns a generator of (keyPath, value) tuples for the given dictionary,
    where each keyPath is a dot-separated string representing the complete
    path to a nested key.

    @param dictionary: the dict object to traverse
    @type dictionary: C{dict}
    @param current: do not use; used internally for recursion
    @type current: C{str}
    @return: generator of (keyPath, value) tuples
    """
    for key, value in dictionary.iteritems():
        if isinstance(value, dict):
            for result in flattenDictionary(value, current + key + "."):
                yield result
        else:
            yield (current + key, value)


def restartService(pidFilename):
    """
    Given the path to a PID file, sends a HUP signal to the contained pid
    in order to cause calendar server to restart.

    @param pidFilename: an absolute path to a PID file
    @type pidFilename: C{str}
    """
    if os.path.exists(pidFilename):
        with open(pidFilename, "r") as pidFile:
            pid = pidFile.read().strip()
        try:
            pid = int(pid)
        except ValueError:
            return
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass


class WritableConfig(object):
    """
    A wrapper around a Config object which allows writing of values.  The idea
    is a deployment could have a master plist which doesn't change, and have
    it include a plist file which does.  This class facilitates writing to that
    included plist.
    """

    def __init__(self, wrappedConfig, fileName):
        """
        @param wrappedConfig: the Config object to read from
        @type wrappedConfig: C{Config}
        @param fileName: the full path to the modifiable plist
        @type fileName: C{str}
        """
        self.config = wrappedConfig
        self.fileName = fileName
        self.changes = None
        self.currentConfigSubset = ConfigDict()
        self.dirty = False

    def set(self, data):
        """
        Merges data into a ConfigDict of changes intended to be saved to disk
        when save( ) is called.

        @param data: a dict containing new values
        @type data: C{dict}
        """
        if not isinstance(data, ConfigDict):
            data = ConfigDict(mapping=data)
        mergeData(self.currentConfigSubset, data)
        self.dirty = True

    def delete(self, key):
        """
        Deletes the specified key

        @param key: the key to delete
        @type key: C{str}
        """
        try:
            del self.currentConfigSubset[key]
            self.dirty = True
        except:
            pass

    def read(self):
        """
        Reads in the data contained in the writable plist file.

        @return: C{ConfigDict}
        """
        if os.path.exists(self.fileName):
            self.currentConfigSubset = ConfigDict(mapping=plistlib.readPlist(self.fileName))
        else:
            self.currentConfigSubset = ConfigDict()

    def toString(self):
        return plistlib.writePlistToString(self.currentConfigSubset)

    def save(self, restart=False):
        """
        Writes any outstanding changes to the writable plist file.  Optionally
        restart calendar server.

        @param restart: whether to restart the calendar server.
        @type restart: C{bool}
        """
        if self.dirty:
            content = writePlistToString(self.currentConfigSubset)
            fp = FilePath(self.fileName)
            fp.setContent(content)
            self.dirty = False
            if restart:
                restartService(self.config.PIDFile)

    @classmethod
    def convertToValue(cls, string):
        """
        Inspect string and convert the value into an appropriate Python data type
        TODO: change this to look at actual types defined within stdconfig
        """
        if "." in string:
            try:
                value = float(string)
            except ValueError:
                value = string
        else:
            try:
                value = int(string)
            except ValueError:
                if string == "True":
                    value = True
                elif string == "False":
                    value = False
                else:
                    value = string
        return value


def respond(command, result):
    sys.stdout.write(writePlistToString({'command': command['command'], 'result': result}))


def respondWithError(msg, status=1):
    sys.stdout.write(writePlistToString({'error': msg, }))
