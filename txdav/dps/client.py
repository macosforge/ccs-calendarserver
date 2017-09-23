from __future__ import print_function
##
# Copyright (c) 2014-2017 Apple Inc. All rights reserved.
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

import cPickle as pickle
import time
import uuid

from twext.python.log import Logger
from twext.who.directory import DirectoryRecord as BaseDirectoryRecord
from twext.who.directory import DirectoryService as BaseDirectoryService
from twext.who.expression import Operand
from twext.who.idirectory import RecordType, IDirectoryService
import twext.who.idirectory
from twext.who.util import ConstantsContainer
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.internet.error import ConnectError
from twisted.internet.protocol import ClientCreator
from twisted.protocols import amp
from twisted.python.constants import Names, NamedConstant
from txdav.caldav.icalendardirectoryservice import (
    ICalendarStoreDirectoryRecord
)
from txdav.common.idirectoryservice import IStoreDirectoryService
from txdav.dps.commands import (
    RecordWithShortNameCommand, RecordWithUIDCommand, RecordWithGUIDCommand,
    RecordsWithRecordTypeCommand, RecordsWithEmailAddressCommand,
    RecordsMatchingTokensCommand, RecordsMatchingFieldsCommand,
    MembersCommand, GroupsCommand, SetMembersCommand,
    VerifyPlaintextPasswordCommand, VerifyHTTPDigestCommand,
    WikiAccessForUIDCommand, ContinuationCommand,
    StatsCommand, ExternalDelegatesCommand, ExpandedMemberUIDsCommand,
    AddMembersCommand, RemoveMembersCommand,
    UpdateRecordsCommand, ExpandedMembersCommand, FlushCommand,
    SetAutoScheduleModeCommand, ContainsUIDsCommand
)
from txdav.who.delegates import RecordType as DelegatesRecordType
from txdav.who.directory import (
    CalendarDirectoryRecordMixin, CalendarDirectoryServiceMixin
)
import txdav.who.augment
import txdav.who.delegates
import txdav.who.idirectory
import txdav.who.wiki
from zope.interface import implementer

log = Logger()

#
# Client implementation of Directory Proxy Service
#


# MOVE2WHO TODOs:
# LDAP
# Store based directory service (records in the store, i.e.
#    locations/resources)
# Store autoAcceptGroups in the group db?

@implementer(IDirectoryService, IStoreDirectoryService)
class DirectoryService(BaseDirectoryService, CalendarDirectoryServiceMixin):
    """
    Client side of directory proxy
    """

    # FIXME: somehow these should come from the actual directory:

    recordType = ConstantsContainer(
        (twext.who.idirectory.RecordType,
         txdav.who.idirectory.RecordType,
         txdav.who.delegates.RecordType,
         txdav.who.wiki.RecordType)
    )

    fieldName = ConstantsContainer(
        (twext.who.idirectory.FieldName,
         txdav.who.idirectory.FieldName,
         txdav.who.augment.FieldName)
    )

    def _dictToRecord(self, serializedFields):
        """
        Turn a dictionary of fields sent from the server into a directory
        record
        """
        if not serializedFields:
            return None

        # print("FIELDS", serializedFields)

        fields = {}
        for fieldName, value in serializedFields.iteritems():
            try:
                field = self.fieldName.lookupByName(fieldName)
            except ValueError:
                # unknown field
                pass
            else:
                valueType = self.fieldName.valueType(field)
                if valueType in (unicode, bool):
                    fields[field] = value
                elif valueType is uuid.UUID:
                    fields[field] = uuid.UUID(value)
                elif issubclass(valueType, Names):
                    if value is not None:
                        fields[field] = field.valueType.lookupByName(value)
                    else:
                        fields[field] = None
                elif issubclass(valueType, NamedConstant):
                    if fieldName == "recordType":  # Is there a better way?
                        fields[field] = self.recordType.lookupByName(value)

        # print("AFTER:", fields)
        return DirectoryRecord(self, fields)

    def _processSingleRecord(self, result):
        """
        Takes a dictionary with a "fields" key whose value is a pickled
        dictionary of a record's fields, and returns a record.
        """
        serializedFields = pickle.loads(result['fields'])
        return self._dictToRecord(serializedFields)

    def _processMultipleRecords(self, result):
        """
        Takes a dictionary with a "items" key whose value is an iterable
        of pickled dictionaries (of records' fields), and returns a list of
        records.
        """
        serializedFieldsList = []
        for fields in result["items"]:
            fields = pickle.loads(fields)
            serializedFieldsList.append(fields)
        results = []
        for serializedFields in serializedFieldsList:
            record = self._dictToRecord(serializedFields)
            if record is not None:
                results.append(record)
        return results

    @inlineCallbacks
    def _getConnection(self):

        from twistedcaldav.config import config
        path = config.DirectoryProxy.SocketPath
        if getattr(self, "_connection", None) is None:
            log.debug("Creating connection")
            connection = (
                yield ClientCreator(reactor, amp.AMP).connectUNIX(path)
            )
            self._connection = connection
        returnValue(self._connection)

    @inlineCallbacks
    def _sendCommand(self, command, **kwds):
        """
        Execute a remote AMP command, first making the connection to the peer.
        Any kwds are passed on to the AMP command.

        @param command: the AMP command to call
        @type command: L{twisted.protocols.amp.Command}
        """
        ampProto = (yield self._getConnection())
        try:
            results = (yield ampProto.callRemote(command, **kwds))
        except Exception as e:
            log.error("Failed AMP command", error=e)
            #  FIXME: is there a way to hook into ConnectionLost?
            self._connection = None
            raise
        returnValue(results)

    def _logResultTiming(self, command, startTime, results):
        duration = time.time() - startTime
        numResults = 0
        if "fields" in results:
            numResults = 1
        if "items" in results:
            numResults = len(results["items"])
        log.debug(
            "DPS call {command} duration={duration:.2f}ms, results={numResults}",
            command=command, duration=1000.0 * duration, numResults=numResults
        )

    @inlineCallbacks
    def _call(self, command, postProcess, **kwds):
        """
        Execute a remote AMP command, first making the connection to the peer,
        then making the call, then running the results through the postProcess
        callback.  Any kwds are passed on to the AMP command.

        @param command: the AMP command to call
        @type command: L{twisted.protocols.amp.Command}

        @param postProcess: a callable which takes the AMP response dictionary
            and performs any required massaging of the results, returning a
            L{Deferred} which fires with the post-processed results
        @type postProcess: callable
        """
        startTime = time.time()
        results = yield self._sendCommand(command, **kwds)
        if results.get("continuation", None) is None:
            # We have all the results
            self._logResultTiming(command, startTime, results)
            returnValue(postProcess(results))

        # There are more results to fetch, so loop until the continuation
        # keyword we get back is None

        multi = [results]

        while results.get("continuation", None) is not None:
            results = yield self._sendCommand(
                ContinuationCommand,
                continuation=results["continuation"]
            )
            multi.append(results)

        results = {"items": []}
        for result in multi:
            results["items"].extend(result["items"])

        self._logResultTiming(command, startTime, results)
        returnValue(postProcess(results))

    def recordWithShortName(self, recordType, shortName, timeoutSeconds=None):
        # MOVE2WHO
        # temporary hack until we can fix all callers not to pass strings:
        if isinstance(recordType, (str, unicode)):
            recordType = self.recordType.lookupByName(recordType)

        # MOVE2WHO, REMOVE THIS HACK TOO:
        if not isinstance(shortName, unicode):
            # log.warn("Need to change shortName to unicode")
            shortName = shortName.decode("utf-8")

        kwds = {
            "recordType": recordType.name.encode("utf-8"),
            "shortName": shortName.encode("utf-8")
        }
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordWithShortNameCommand,
            self._processSingleRecord,
            **kwds
        )

    def recordWithUID(self, uid, timeoutSeconds=None):
        # MOVE2WHO, REMOVE THIS:
        if not isinstance(uid, unicode):
            # log.warn("Need to change uid to unicode")
            uid = uid.decode("utf-8")

        kwds = {
            "uid": uid.encode("utf-8"),
        }
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordWithUIDCommand,
            self._processSingleRecord,
            **kwds
        )

    def recordWithGUID(self, guid, timeoutSeconds=None):
        kwds = {
            "guid": str(guid),
        }
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordWithGUIDCommand,
            self._processSingleRecord,
            **kwds
        )

    def recordsWithRecordType(
        self, recordType, limitResults=None, timeoutSeconds=None
    ):
        kwds = {
            "recordType": recordType.name.encode("utf-8")
        }
        if limitResults is not None:
            kwds["limitResults"] = limitResults
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds
        return self._call(
            RecordsWithRecordTypeCommand,
            self._processMultipleRecords,
            **kwds
        )

    def recordsWithEmailAddress(
        self, emailAddress, limitResults=None, timeoutSeconds=None
    ):
        kwds = {
            "emailAddress": emailAddress.encode("utf-8")
        }
        if limitResults is not None:
            kwds["limitResults"] = limitResults
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordsWithEmailAddressCommand,
            self._processMultipleRecords,
            **kwds
        )

    def recordsMatchingTokens(
        self, tokens, context=None, limitResults=None, timeoutSeconds=None
    ):
        kwds = {
            "tokens": [t.encode("utf-8") for t in tokens],
            "context": context
        }
        if limitResults is not None:
            kwds["limitResults"] = limitResults
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordsMatchingTokensCommand,
            self._processMultipleRecords,
            **kwds
        )

    def recordsMatchingFields(
        self, fields, operand=Operand.OR, recordType=None,
        limitResults=None, timeoutSeconds=None
    ):
        newFields = []
        for fieldName, searchTerm, matchFlags, matchType in fields:

            if isinstance(searchTerm, uuid.UUID):
                searchTerm = unicode(searchTerm)

            newFields.append(
                (
                    fieldName.encode("utf-8"),
                    searchTerm.encode("utf-8"),
                    matchFlags.name.encode("utf-8"),
                    matchType.name.encode("utf-8")
                )
            )
        if recordType is not None:
            recordType = recordType.name.encode("utf-8")

        kwds = {
            "fields": newFields,
            "operand": operand.name.encode("utf-8"),
            "recordType": recordType
        }
        if limitResults is not None:
            kwds["limitResults"] = limitResults
        if timeoutSeconds is not None:
            kwds["timeoutSeconds"] = timeoutSeconds

        return self._call(
            RecordsMatchingFieldsCommand,
            self._processMultipleRecords,
            **kwds
        )

    def recordsWithDirectoryBasedDelegates(self):
        return self._call(
            ExternalDelegatesCommand,
            self._processMultipleRecords
        )

    def recordsFromExpression(self, expression, recordTypes=None):
        raise NotImplementedError(
            "This won't work until expressions are serializable to send "
            "across AMP"
        )

    def updateRecords(self, records, create=False):
        # FIXME: cannot create right now
        if create:
            raise ValueError("Cannot create records using DPS")

        recordUIDs = [r.uid.encode("utf-8") for r in records]
        return self._sendCommand(
            UpdateRecordsCommand,
            uids=recordUIDs,
            create=False,
        )

    def setAutoScheduleMode(self, record, autoScheduleMode):
        return self._sendCommand(
            SetAutoScheduleModeCommand,
            uid=record.uid.encode("utf-8"),
            autoScheduleMode=autoScheduleMode.name,
        )

    @inlineCallbacks
    def flush(self):
        try:
            yield self._sendCommand(FlushCommand)
            returnValue(None)
        except ConnectError:
            returnValue(None)

    @inlineCallbacks
    def stats(self):
        try:
            result = yield self._sendCommand(StatsCommand)
            returnValue(pickle.loads(result['stats']))
        except ConnectError:
            returnValue({})


@implementer(ICalendarStoreDirectoryRecord)
class DirectoryRecord(BaseDirectoryRecord, CalendarDirectoryRecordMixin):

    def verifyPlaintextPassword(self, password):
        if isinstance(password, unicode):
            password = password.encode("utf-8")
        return self.service._call(
            VerifyPlaintextPasswordCommand,
            lambda x: x['authenticated'],
            uid=self.uid.encode("utf-8"),
            password=password,
        )

    def verifyHTTPDigest(
        self, username, realm, uri, nonce, cnonce,
        algorithm, nc, qop, response, method,
    ):
        return self.service._call(
            VerifyHTTPDigestCommand,
            lambda x: x['authenticated'],
            uid=self.uid.encode("utf-8"),
            username=username.encode("utf-8"),
            realm=realm.encode("utf-8"),
            uri=uri.encode("utf-8"),
            nonce=nonce.encode("utf-8"),
            cnonce=cnonce.encode("utf-8"),
            algorithm=algorithm.encode("utf-8"),
            nc=nc.encode("utf-8"),
            qop=qop.encode("utf-8"),
            response=response.encode("utf-8"),
            method=method.encode("utf-8"),
        )

    def members(self):
        if self.recordType in (
            RecordType.group,
            DelegatesRecordType.readDelegateGroup,
            DelegatesRecordType.writeDelegateGroup,
            DelegatesRecordType.readDelegatorGroup,
            DelegatesRecordType.writeDelegatorGroup,
        ):
            return self.service._call(
                MembersCommand,
                self.service._processMultipleRecords,
                uid=self.uid.encode("utf-8")
            )
        else:
            return succeed([])

    def expandedMembers(self):
        """
        Only allowed for delegates - not regular groups.
        """
        if self.recordType in (
            DelegatesRecordType.readDelegateGroup,
            DelegatesRecordType.writeDelegateGroup,
            DelegatesRecordType.readDelegatorGroup,
            DelegatesRecordType.writeDelegatorGroup,
        ):
            return self.service._call(
                ExpandedMembersCommand,
                self.service._processMultipleRecords,
                uid=self.uid.encode("utf-8")
            )
        else:
            return succeed([])

    def groups(self):
        return self.service._call(
            GroupsCommand,
            self.service._processMultipleRecords,
            uid=self.uid.encode("utf-8")
        )

    def addMembers(self, members):
        log.debug("DPS Client addMembers")
        memberUIDs = [m.uid.encode("utf-8") for m in members]
        return self.service._call(
            AddMembersCommand,
            lambda x: x['success'],
            uid=self.uid.encode("utf-8"),
            memberUIDs=memberUIDs
        )

    def removeMembers(self, members):
        log.debug("DPS Client removeMembers")
        memberUIDs = [m.uid.encode("utf-8") for m in members]
        return self.service._call(
            RemoveMembersCommand,
            lambda x: x['success'],
            uid=self.uid.encode("utf-8"),
            memberUIDs=memberUIDs
        )

    def setMembers(self, members):
        log.debug("DPS Client setMembers")
        memberUIDs = [m.uid.encode("utf-8") for m in members]
        return self.service._call(
            SetMembersCommand,
            lambda x: x['success'],
            uid=self.uid.encode("utf-8"),
            memberUIDs=memberUIDs
        )

    def _convertUIDs(self, results):
        uids = []
        for uid in results["items"]:
            uids.append(uid.decode("utf-8"))
        return uids

    def expandedMemberUIDs(self):
        log.debug("DPS Client expandedMemberUIDs")
        if self.recordType is RecordType.group:
            return self.service._call(
                ExpandedMemberUIDsCommand,
                self._convertUIDs,
                uid=self.uid.encode("utf-8")
            )
        else:
            return succeed([])

    def containsUID(self, uid):
        """
        Is the supplied UID an expanded member of this proxy group.

        @param uid: UID to test
        @type uid: L{str}

        @return: result
        @rtype: L{bool}
        """
        log.debug("DPS Client containsUID")
        return self.service._call(
            ContainsUIDsCommand,
            lambda x: x['result'],
            uid=self.uid.encode("utf-8"),
            testUid=uid.encode("utf-8")
        )

    def _convertAccess(self, results):
        access = results["access"].decode("utf-8")
        return txdav.who.wiki.WikiAccessLevel.lookupByName(access)

    def accessForRecord(self, record):
        log.debug("DPS Client accessForRecord")
        return self.service._call(
            WikiAccessForUIDCommand,
            self._convertAccess,
            wikiUID=self.uid.encode("utf-8"),
            uid=record.uid.encode("utf-8") if record else ""
        )


# Test client:

@inlineCallbacks
def makeEvenBetterRequest():
    ds = DirectoryService(None)
    record = (yield ds.recordWithShortName(RecordType.user, "sagen"))
    print("short name: {r}".format(r=record))
    if record:
        authenticated = (yield record.verifyPlaintextPassword("negas"))
        print("plain auth: {a}".format(a=authenticated))

    # record = (yield ds.recordWithUID("__dre__"))
    # print("uid: {r}".format(r=record))
    # if record:
    #     authenticated = (yield record.verifyPlaintextPassword("erd"))
    #     print("plain auth: {a}".format(a=authenticated))

    # record = yield ds.recordWithGUID(
    #     "A3B1158F-0564-4F5B-81E4-A89EA5FF81B0"
    # )
    # print("guid: {r}".format(r=record))

    # records = yield ds.recordsWithRecordType(RecordType.user)
    # print("recordType: {r}".format(r=records))

    # records = yield ds.recordsWithEmailAddress(
    #     "cdaboo@bitbucket.calendarserver.org"
    # )
    # print("emailAddress: {r}".format(r=records))


def succeeded(result):
    print("yay")
    reactor.stop()


def failed(failure):
    print("boo: {f}".format(f=failure))
    reactor.stop()


if __name__ == '__main__':
    d = makeEvenBetterRequest()
    d.addCallbacks(succeeded, failed)
    reactor.run()
