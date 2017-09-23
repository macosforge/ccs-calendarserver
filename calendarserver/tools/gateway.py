#!/usr/bin/env python

##
# Copyright (c) 2006-2017 Apple Inc. All rights reserved.
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

from getopt import getopt, GetoptError
import os
from plistlib import readPlistFromString, writePlistToString
import uuid
import xml

from calendarserver.tools.cmdline import utilityMain
from calendarserver.tools.config import (
    WRITABLE_CONFIG_KEYS, setKeyPath, getKeyPath, flattenDictionary,
    WritableConfig
)
from calendarserver.tools.purge import (
    WorkerService, PurgeOldEventsService,
    DEFAULT_BATCH_SIZE, DEFAULT_RETAIN_DAYS,
    PrincipalPurgeWork
)
from calendarserver.tools.util import (
    recordForPrincipalID, autoDisableMemcached,
    getProxies, setProxies
)
from pycalendar.datetime import DateTime
from twext.who.directory import DirectoryRecord
from twisted.internet.defer import inlineCallbacks, succeed, returnValue
from twistedcaldav.config import config, ConfigDict

from txdav.who.idirectory import RecordType as CalRecordType
from twext.who.idirectory import FieldName
from twisted.python.constants import Names, NamedConstant
from txdav.who.delegates import Delegates, RecordType as DelegateRecordType


attrMap = {
    'GeneratedUID': {'attr': 'uid', },
    'RealName': {'attr': 'fullNames', },
    'RecordName': {'attr': 'shortNames', },
    'AutoScheduleMode': {'attr': 'autoScheduleMode', },
    'AutoAcceptGroup': {'attr': 'autoAcceptGroup', },

    # 'Comment': {'extras': True, 'attr': 'comment', },
    # 'Description': {'extras': True, 'attr': 'description', },
    # 'Type': {'extras': True, 'attr': 'type', },

    # For "Locations", i.e. scheduled spaces
    'Capacity': {'attr': 'capacity', },
    'Floor': {'attr': 'floor', },
    'AssociatedAddress': {'attr': 'associatedAddress', },

    # For "Addresses", i.e. nonscheduled areas containing Locations
    'AbbreviatedName': {'attr': 'abbreviatedName', },
    'StreetAddress': {'attr': 'streetAddress', },
    'GeographicLocation': {'attr': 'geographicLocation', },
}


def usage(e=None):

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options]" % (name,))
    print("")
    print("  TODO: describe usage")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -e --error: send stderr to stdout")
    print("  -f --config <path>: Specify caldavd.plist configuration path")
    print("")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


class RunnerService(WorkerService):
    """
    A wrapper around Runner which uses utilityMain to get the store
    """

    commands = None

    @inlineCallbacks
    def doWork(self):
        """
        Create/run a Runner to execute the commands
        """
        runner = Runner(self.store, self.commands)
        if runner.validate():
            yield runner.run()

    def doWorkWithoutStore(self):
        respondWithError("Database is not available")
        return succeed(None)


def main():

    try:
        (optargs, _ignore_args) = getopt(
            sys.argv[1:], "hef:", [
                "help",
                "error",
                "config=",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    configFileName = None
    debug = False

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        if opt in ("-e", "--error"):
            debug = True

        elif opt in ("-f", "--config"):
            configFileName = arg

        else:
            raise NotImplementedError(opt)

    #
    # Read commands from stdin
    #
    rawInput = sys.stdin.read()
    try:
        plist = readPlistFromString(rawInput)
    except xml.parsers.expat.ExpatError as e:  # @UndefinedVariable
        respondWithError(str(e))
        return

    # If the plist is an array, each element of the array is a separate
    # command dictionary.
    if isinstance(plist, list):
        commands = plist
    else:
        commands = [plist]

    RunnerService.commands = commands
    utilityMain(configFileName, RunnerService, verbose=debug)


class Runner(object):

    def __init__(self, store, commands, output=None):
        self.store = store
        self.dir = store.directoryService()
        self.commands = commands
        if output is None:
            output = sys.stdout
        self.output = output

    def validate(self):
        # Make sure commands are valid
        for command in self.commands:
            if 'command' not in command:
                self.respondWithError("'command' missing from plist")
                return False
            commandName = command['command']
            methodName = "command_%s" % (commandName,)
            if not hasattr(self, methodName):
                self.respondWithError("Unknown command '%s'" % (commandName,))
                return False
        return True

    @inlineCallbacks
    def run(self):

        # This method can be called as the result of an agent request.  We
        # check to see if memcached is there for each call because the server
        # could have stopped/started since the last time.

        for pool in config.Memcached.Pools.itervalues():
            pool.ClientEnabled = True
        autoDisableMemcached(config)

        try:
            for command in self.commands:
                commandName = command['command']
                methodName = "command_%s" % (commandName,)
                if hasattr(self, methodName):
                    (yield getattr(self, methodName)(command))
                else:
                    self.respondWithError("Unknown command '%s'" % (commandName,))

        except Exception as e:
            self.respondWithError("Command failed: '%s'" % (str(e),))
            raise

    # Locations

    # deferred
    def command_getLocationList(self, command):
        return self.respondWithRecordsOfTypes(self.dir, command, ["locations"])

    @inlineCallbacks
    def _saveRecord(self, typeName, recordType, command, oldFields=None):
        """
        Save a record using the values in the command plist, starting with
        any fields in the optional oldFields.

        @param typeName: one of "locations", "resources", "addresses"; used
            to return the appropriate list of records afterwards.
        @param recordType: the type of record to save
        @param command: the command containing values
        @type command: C{dict}
        @param oldFields: the optional fields to start with, which will be
            overridden by values from command
        @type oldFiles: C{dict}
        """

        if oldFields is None:
            fields = {
                self.dir.fieldName.recordType: recordType,
                self.dir.fieldName.hasCalendars: True,
                self.dir.fieldName.hasContacts: True,
            }
            create = True
        else:
            fields = oldFields.copy()
            create = False

        for key, info in attrMap.iteritems():
            if key in command:
                attrName = info['attr']
                field = self.dir.fieldName.lookupByName(attrName)
                valueType = self.dir.fieldName.valueType(field)
                value = command[key]

                # For backwards compatibility, convert to a list if needed
                if (
                    self.dir.fieldName.isMultiValue(field) and
                    not isinstance(value, list)
                ):
                    value = [value]

                if valueType == int:
                    value = int(value)
                elif issubclass(valueType, Names):
                    if value is not None:
                        value = valueType.lookupByName(value)
                else:
                    if isinstance(value, list):
                        newList = []
                        for item in value:
                            if isinstance(item, str):
                                newList.append(item.decode("utf-8"))
                            else:
                                newList.append(item)
                        value = newList
                    elif isinstance(value, str):
                        value = value.decode("utf-8")

                fields[field] = value

        if FieldName.uid not in fields:
            # No uid provided, so generate one
            fields[FieldName.uid] = unicode(uuid.uuid4()).upper()

        if FieldName.shortNames not in fields:
            # No short names were provided, so copy from uid
            fields[FieldName.shortNames] = [fields[FieldName.uid]]

        record = DirectoryRecord(self.dir, fields)
        yield self.dir.updateRecords([record], create=create)

        readProxies = command.get("ReadProxies", None)
        if readProxies:
            proxyRecords = []
            for proxyUID in readProxies:
                proxyRecord = yield self.dir.recordWithUID(proxyUID)
                if proxyRecord is not None:
                    proxyRecords.append(proxyRecord)
            readProxies = proxyRecords

        writeProxies = command.get("WriteProxies", None)
        if writeProxies:
            proxyRecords = []
            for proxyUID in writeProxies:
                proxyRecord = yield self.dir.recordWithUID(proxyUID)
                if proxyRecord is not None:
                    proxyRecords.append(proxyRecord)
            writeProxies = proxyRecords

        yield setProxies(record, readProxies, writeProxies)

        yield self.respondWithRecordsOfTypes(self.dir, command, [typeName])

    def command_createLocation(self, command):
        return self._saveRecord("locations", CalRecordType.location, command)

    def command_createResource(self, command):
        return self._saveRecord("resources", CalRecordType.resource, command)

    def command_createAddress(self, command):
        return self._saveRecord("addresses", CalRecordType.address, command)

    @inlineCallbacks
    def command_setLocationAttributes(self, command):
        uid = command['GeneratedUID']
        record = yield self.dir.recordWithUID(uid)
        yield self._saveRecord(
            "locations",
            CalRecordType.location,
            command,
            oldFields=record.fields
        )

    @inlineCallbacks
    def command_setResourceAttributes(self, command):
        uid = command['GeneratedUID']
        record = yield self.dir.recordWithUID(uid)
        yield self._saveRecord(
            "resources",
            CalRecordType.resource,
            command,
            oldFields=record.fields
        )

    @inlineCallbacks
    def command_setAddressAttributes(self, command):
        uid = command['GeneratedUID']
        record = yield self.dir.recordWithUID(uid)
        yield self._saveRecord(
            "addresses",
            CalRecordType.address,
            command,
            oldFields=record.fields
        )

    @inlineCallbacks
    def command_getLocationAttributes(self, command):
        uid = command['GeneratedUID']
        record = yield self.dir.recordWithUID(uid)
        if record is None:
            self.respondWithError("Principal not found: %s" % (uid,))
            return
        recordDict = recordToDict(record)
        # recordDict['AutoSchedule'] = yield principal.getAutoSchedule()
        try:
            recordDict['AutoAcceptGroup'] = record.autoAcceptGroup
        except AttributeError:
            pass

        readProxies, writeProxies = yield getProxies(record)
        recordDict['ReadProxies'] = [r.uid for r in readProxies]
        recordDict['WriteProxies'] = [r.uid for r in writeProxies]
        self.respond(command, recordDict)

    command_getResourceAttributes = command_getLocationAttributes
    command_getAddressAttributes = command_getLocationAttributes

    # Resources

    def command_getResourceList(self, command):
        return self.respondWithRecordsOfTypes(self.dir, command, ["resources"])

    # deferred
    def command_getLocationAndResourceList(self, command):
        return self.respondWithRecordsOfTypes(self.dir, command, ["locations", "resources"])

    # Addresses

    def command_getAddressList(self, command):
        return self.respondWithRecordsOfTypes(self.dir, command, ["addresses"])

    @inlineCallbacks
    def _delete(self, typeName, command):
        uid = command['GeneratedUID']
        yield self.dir.removeRecords([uid])
        yield self.respondWithRecordsOfTypes(self.dir, command, [typeName])

    @inlineCallbacks
    def command_deleteLocation(self, command):
        txn = self.store.newTransaction()
        uid = command['GeneratedUID']
        yield txn.enqueue(PrincipalPurgeWork, uid=uid)
        yield txn.commit()

        yield self._delete("locations", command)

    @inlineCallbacks
    def command_deleteResource(self, command):
        txn = self.store.newTransaction()
        uid = command['GeneratedUID']
        yield txn.enqueue(PrincipalPurgeWork, uid=uid)
        yield txn.commit()

        yield self._delete("resources", command)

    def command_deleteAddress(self, command):
        return self._delete("addresses", command)

    # Config

    def command_readConfig(self, command):
        """
        Return current configuration

        @param command: the dictionary parsed from the plist read from stdin
        @type command: C{dict}
        """
        config.reload()
        # config.Memcached.Pools.Default.ClientEnabled = False

        result = {}
        for keyPath in WRITABLE_CONFIG_KEYS:
            value = getKeyPath(config, keyPath)
            if value is not None:
                # Note: config contains utf-8 encoded strings, but plistlib
                # wants unicode, so decode here:
                if isinstance(value, str):
                    value = value.decode("utf-8")
                setKeyPath(result, keyPath, value)
        self.respond(command, result)

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
            self.respond(command, {"error": str(e)})
        else:
            self.command_readConfig(command)

    # Proxies

    def command_listWriteProxies(self, command):
        return self._listProxies(command, "write")

    def command_listReadProxies(self, command):
        return self._listProxies(command, "read")

    @inlineCallbacks
    def _listProxies(self, command, proxyType):
        record = yield recordForPrincipalID(self.dir, command['Principal'])
        if record is None:
            self.respondWithError("Principal not found: %s" % (command['Principal'],))
            returnValue(None)
        yield self.respondWithProxies(command, record, proxyType)

    def command_addReadProxy(self, command):
        return self._addProxy(command, "read")

    def command_addWriteProxy(self, command):
        return self._addProxy(command, "write")

    @inlineCallbacks
    def _addProxy(self, command, proxyType):
        record = yield recordForPrincipalID(self.dir, command['Principal'])
        if record is None:
            self.respondWithError("Principal not found: %s" % (command['Principal'],))
            returnValue(None)

        proxyRecord = yield recordForPrincipalID(self.dir, command['Proxy'])
        if proxyRecord is None:
            self.respondWithError("Proxy not found: %s" % (command['Proxy'],))
            returnValue(None)

        txn = self.store.newTransaction()
        yield Delegates.addDelegate(txn, record, proxyRecord, (proxyType == "write"))
        yield txn.commit()
        yield self.respondWithProxies(command, record, proxyType)

    def command_removeReadProxy(self, command):
        return self._removeProxy(command, "read")

    def command_removeWriteProxy(self, command):
        return self._removeProxy(command, "write")

    @inlineCallbacks
    def _removeProxy(self, command, proxyType):
        record = yield recordForPrincipalID(self.dir, command['Principal'])
        if record is None:
            self.respondWithError("Principal not found: %s" % (command['Principal'],))
            returnValue(None)

        proxyRecord = yield recordForPrincipalID(self.dir, command['Proxy'])
        if proxyRecord is None:
            self.respondWithError("Proxy not found: %s" % (command['Proxy'],))
            returnValue(None)

        txn = self.store.newTransaction()
        yield Delegates.removeDelegate(txn, record, proxyRecord, (proxyType == "write"))
        yield txn.commit()
        yield self.respondWithProxies(command, record, proxyType)

    @inlineCallbacks
    def command_purgeOldEvents(self, command):
        """
        Convert RetainDays from the command dictionary into a date, then purge
        events older than that date.

        @param command: the dictionary parsed from the plist read from stdin
        @type command: C{dict}
        """
        retainDays = command.get("RetainDays", DEFAULT_RETAIN_DAYS)
        cutoff = DateTime.getToday()
        cutoff.setDateOnly(False)
        cutoff.offsetDay(-retainDays)
        eventCount = (yield PurgeOldEventsService.purgeOldEvents(self.store, None, cutoff, DEFAULT_BATCH_SIZE))
        self.respond(command, {'EventsRemoved': eventCount, "RetainDays": retainDays})

    @inlineCallbacks
    def respondWithProxies(self, command, record, proxyType):
        proxies = []
        recordType = {
            "read": DelegateRecordType.readDelegateGroup,
            "write": DelegateRecordType.writeDelegateGroup,
        }[proxyType]
        proxyGroup = yield self.dir.recordWithShortName(recordType, record.uid)
        for member in (yield proxyGroup.members()):
            proxies.append(member.uid)

        self.respond(command, {
            'Principal': record.uid, 'Proxies': proxies
        })

    @inlineCallbacks
    def respondWithRecordsOfTypes(self, directory, command, recordTypes):
        result = []
        for recordType in recordTypes:
            recordType = directory.oldNameToRecordType(recordType)
            for record in (yield directory.recordsWithRecordType(recordType)):
                recordDict = recordToDict(record)
                result.append(recordDict)
        self.respond(command, result)

    def respond(self, command, result):
        self.output.write(writePlistToString({'command': command['command'], 'result': result}))

    def respondWithError(self, msg, status=1):
        self.output.write(writePlistToString({'error': msg, }))


def recordToDict(record):
    recordDict = {}
    for key, info in attrMap.iteritems():
        try:
            value = record.fields[record.service.fieldName.lookupByName(info['attr'])]
            if value is None:
                continue
            # For backwards compatibility, present fullName/RealName as single
            # value even though twext.who now has it as multiValue
            if key == "RealName":
                value = value[0]
            if isinstance(value, str):
                value = value.decode("utf-8")
            elif isinstance(value, NamedConstant):
                value = value.name
            recordDict[key] = value
        except KeyError:
            pass
    return recordDict


def respondWithError(msg, status=1):
    sys.stdout.write(writePlistToString({'error': msg, }))


if __name__ == "__main__":
    main()
