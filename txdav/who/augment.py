# -*- test-case-name: txdav.who.test.test_augment -*-
##
# Copyright (c) 2013-2017 Apple Inc. All rights reserved.
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

"""
Augmenting Directory Service
"""

__all__ = [
    "AugmentedDirectoryService",
]

import time

from zope.interface import implementer

from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twistedcaldav.directory.augment import AugmentRecord
from twext.python.log import Logger
from twext.who.directory import DirectoryRecord
from twext.who.directory import DirectoryService as BaseDirectoryService
from twext.who.idirectory import (
    IDirectoryService, RecordType, FieldName as BaseFieldName, NotAllowedError
)
from twext.who.util import ConstantsContainer

from txdav.common.idirectoryservice import IStoreDirectoryService
from txdav.who.directory import (
    CalendarDirectoryRecordMixin, CalendarDirectoryServiceMixin,
)
from txdav.who.idirectory import (
    AutoScheduleMode, FieldName, RecordType as CalRecordType
)

log = Logger()


def timed(f):
    """
    A decorator which keeps track of the wrapped function's call count and
    total duration
    """

    def recordTiming(result, key, startTime):
        """
        Figures out how much time to add to the total time spent within the
        method identified by key and stores that in the timings dict.

        @param result: the result of the wrapped method
        @param timings: the dictionary to store timings in
        @type timings: C{dict}
        @param key: the method name
        @type key: C{str}
        @param startTime: the start time of the call in seconds
        @type startTime: C{float}
        """
        AugmentedDirectoryService._addTiming(key, time.time() - startTime)
        return result

    def timingWrapper(self, *args, **kwds):
        """
        Records the start time of the call and the method's name
        """
        startTime = time.time()
        d = f(self, *args, **kwds)
        d.addBoth(recordTiming, f.__name__, startTime)
        return d

    return timingWrapper


@implementer(IDirectoryService, IStoreDirectoryService)
class AugmentedDirectoryService(
    BaseDirectoryService, CalendarDirectoryServiceMixin
):
    """
    Augmented directory service.

    This is a directory service that wraps an L{IDirectoryService} and augments
    directory records with additional or modified fields.
    """

    fieldName = ConstantsContainer((
        BaseFieldName,
        FieldName,
    ))

    _timings = {}

    def __init__(self, directory, store, augmentDB):
        BaseDirectoryService.__init__(self, directory.realmName)
        self._directory = directory
        self._store = store
        self._augmentDB = augmentDB

        # An LDAP DS has extra info to expose via the dashboard
        # This is assigned in buildDirectory()
        self._ldapDS = None

    @classmethod
    def _addTiming(cls, key, duration):
        if key not in cls._timings:
            cls._timings[key] = (0, 0.0)
        count, timeSpent = cls._timings[key]
        count += 1
        timeSpent += duration
        cls._timings[key] = (count, timeSpent)

    def flush(self):
        return self._directory.flush()

    def stats(self):
        results = {}
        results.update(self._timings)

        # An LDAP DS has extra info to expose via the dashboard
        if self._ldapDS is not None:
            results.update(self._ldapDS.poolStats)

        return succeed(results)

    @property
    def recordType(self):
        # Defer to the directory service we're augmenting
        return self._directory.recordType

    def recordTypes(self):
        # Defer to the directory service we're augmenting
        return self._directory.recordTypes()

    @inlineCallbacks
    def recordsFromExpression(
        self, expression, recordTypes=None,
        limitResults=None, timeoutSeconds=None
    ):
        records = yield self._directory.recordsFromExpression(
            expression, recordTypes=recordTypes,
            limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )
        augmented = []
        for record in records:
            record = yield self._augment(record)
            augmented.append(record)
        returnValue(augmented)

    @inlineCallbacks
    def recordsWithFieldValue(
        self, fieldName, value, limitResults=None, timeoutSeconds=None
    ):
        records = yield self._directory.recordsWithFieldValue(
            fieldName, value,
            limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )
        augmented = []
        for record in records:
            record = yield self._augment(record)
            augmented.append(record)
        returnValue(augmented)

    @timed
    @inlineCallbacks
    def recordWithUID(self, uid, timeoutSeconds=None):
        # MOVE2WHO, REMOVE THIS:
        if not isinstance(uid, unicode):
            # log.warn("Need to change uid to unicode")
            uid = uid.decode("utf-8")

        record = yield self._directory.recordWithUID(
            uid, timeoutSeconds=timeoutSeconds
        )
        record = yield self._augment(record)
        returnValue(record)

    @timed
    @inlineCallbacks
    def recordWithGUID(self, guid, timeoutSeconds=None):
        record = yield self._directory.recordWithGUID(
            guid, timeoutSeconds=timeoutSeconds
        )
        record = yield self._augment(record)
        returnValue(record)

    @timed
    @inlineCallbacks
    def recordsWithRecordType(
        self, recordType, limitResults=None, timeoutSeconds=None
    ):
        records = yield self._directory.recordsWithRecordType(
            recordType, limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )
        augmented = []
        for record in records:
            record = yield self._augment(record)
            augmented.append(record)
        returnValue(augmented)

    @timed
    @inlineCallbacks
    def recordWithShortName(self, recordType, shortName, timeoutSeconds=None):
        # MOVE2WHO, REMOVE THIS:
        if not isinstance(shortName, unicode):
            # log.warn("Need to change shortName to unicode")
            shortName = shortName.decode("utf-8")

        record = yield self._directory.recordWithShortName(
            recordType, shortName, timeoutSeconds=timeoutSeconds
        )
        record = yield self._augment(record)
        returnValue(record)

    @timed
    @inlineCallbacks
    def recordsWithEmailAddress(
        self, emailAddress, limitResults=None, timeoutSeconds=None
    ):
        # MOVE2WHO, REMOVE THIS:
        if not isinstance(emailAddress, unicode):
            # log.warn("Need to change emailAddress to unicode")
            emailAddress = emailAddress.decode("utf-8")

        records = yield self._directory.recordsWithEmailAddress(
            emailAddress,
            limitResults=limitResults, timeoutSeconds=timeoutSeconds
        )
        augmented = []
        for record in records:
            record = yield self._augment(record)
            augmented.append(record)
        returnValue(augmented)

    @timed
    def recordWithCalendarUserAddress(self, *args, **kwds):
        return CalendarDirectoryServiceMixin.recordWithCalendarUserAddress(
            self, *args, **kwds
        )

    @timed
    def recordsMatchingTokens(self, *args, **kwds):
        return CalendarDirectoryServiceMixin.recordsMatchingTokens(
            self, *args, **kwds
        )

    @timed
    def recordsMatchingFields(self, *args, **kwds):
        return CalendarDirectoryServiceMixin.recordsMatchingFields(
            self, *args, **kwds
        )

    @timed
    @inlineCallbacks
    def updateRecords(self, records, create=False):
        """
        Pull out the augmented fields from each record, apply those to the
        augments database, then update the base records.
        """

        baseRecords = []
        augmentRecords = []

        for record in records:

            # Split out the base fields from the augment fields
            baseFields, augmentFields = self._splitFields(record)

            # Ignore groups for now
            if augmentFields and record.recordType != RecordType.group:
                # Create an AugmentRecord
                autoScheduleMode = {
                    AutoScheduleMode.none: "none",
                    AutoScheduleMode.accept: "accept-always",
                    AutoScheduleMode.decline: "decline-always",
                    AutoScheduleMode.acceptIfFree: "accept-if-free",
                    AutoScheduleMode.declineIfBusy: "decline-if-busy",
                    AutoScheduleMode.acceptIfFreeDeclineIfBusy: "automatic",
                }.get(augmentFields.get(FieldName.autoScheduleMode, None), None)

                kwargs = {
                    "uid": record.uid,
                    "autoScheduleMode": autoScheduleMode,
                }
                if FieldName.hasCalendars in augmentFields:
                    kwargs["enabledForCalendaring"] = augmentFields[FieldName.hasCalendars]
                if FieldName.hasContacts in augmentFields:
                    kwargs["enabledForAddressBooks"] = augmentFields[FieldName.hasContacts]
                if FieldName.loginAllowed in augmentFields:
                    kwargs["enabledForLogin"] = augmentFields[FieldName.loginAllowed]
                if FieldName.autoAcceptGroup in augmentFields:
                    kwargs["autoAcceptGroup"] = augmentFields[FieldName.autoAcceptGroup]
                if FieldName.serviceNodeUID in augmentFields:
                    kwargs["serverID"] = augmentFields[FieldName.serviceNodeUID]
                augmentRecord = AugmentRecord(**kwargs)

                augmentRecords.append(augmentRecord)

            # Create new base records:
            baseRecords.append(DirectoryRecord(self._directory, record._baseRecord.fields if hasattr(record, "_baseRecord") else baseFields))

        # Apply the augment records
        if augmentRecords:
            yield self._augmentDB.addAugmentRecords(augmentRecords)

        # Apply the base records
        if baseRecords:
            try:
                yield self._directory.updateRecords(baseRecords, create=create)
            except NotAllowedError:
                pass

    def _splitFields(self, record):
        """
        Returns a tuple of two dictionaries; the first contains all the non
        augment fields, and the second contains all the augment fields.
        """
        if record is None:
            return None

        augmentFields = {}
        baseFields = record.fields.copy()
        for field in (
            FieldName.loginAllowed,
            FieldName.hasCalendars, FieldName.hasContacts,
            FieldName.autoScheduleMode, FieldName.autoAcceptGroup,
            FieldName.serviceNodeUID
        ):
            if field in baseFields:
                augmentFields[field] = baseFields[field]
                del baseFields[field]

        return (baseFields, augmentFields)

    @inlineCallbacks
    def removeRecords(self, uids):
        yield self._augmentDB.removeAugmentRecords(uids)
        yield self._directory.removeRecords(uids)

    def _assignToField(self, fields, name, value):
        """
        Assign a value to a field only if not already present in fields.
        """
        field = self.fieldName.lookupByName(name)
        if field not in fields:
            fields[field] = value

    @inlineCallbacks
    def _augment(self, record):
        if record is None:
            returnValue(None)

        augmentRecord = yield self._augmentDB.getAugmentRecord(
            record.uid,
            self.recordTypeToOldName(record.recordType)
        )
        if augmentRecord is None:
            # Augments does not know about this record type, so return
            # the original record
            returnValue(record)

        fields = record.fields.copy()

        if augmentRecord:

            if record.recordType == RecordType.group:
                self._assignToField(fields, "hasCalendars", False)
                self._assignToField(fields, "hasContacts", False)
            else:
                self._assignToField(
                    fields, "hasCalendars",
                    augmentRecord.enabledForCalendaring
                )

                self._assignToField(
                    fields, "hasContacts",
                    augmentRecord.enabledForAddressBooks
                )

            # In the case of XML augments, a missing auto-schedule-mode
            # element has the same meaning an element with a value of "default"
            # in which case augmentRecord.autoScheduleMode = "default".  On
            # the record we're augmenting, "default" mode means autoScheduleMode
            # gets set to None (distinct from AutoScheduleMode.none!),
            # which gets swapped for config.Scheduling.Options.AutoSchedule.DefaultMode
            # in checkAttendeeAutoReply().
            # ...Except for locations/resources which will default to automatic

            autoScheduleMode = {
                "none": AutoScheduleMode.none,
                "accept-always": AutoScheduleMode.accept,
                "decline-always": AutoScheduleMode.decline,
                "accept-if-free": AutoScheduleMode.acceptIfFree,
                "decline-if-busy": AutoScheduleMode.declineIfBusy,
                "automatic": AutoScheduleMode.acceptIfFreeDeclineIfBusy,
            }.get(augmentRecord.autoScheduleMode, None)

            # Resources/Locations default to automatic
            if record.recordType in (
                CalRecordType.location,
                CalRecordType.resource
            ):
                if autoScheduleMode is None:
                    autoScheduleMode = AutoScheduleMode.acceptIfFreeDeclineIfBusy

            self._assignToField(
                fields, "autoScheduleMode",
                autoScheduleMode
            )

            if augmentRecord.autoAcceptGroup is not None:
                self._assignToField(
                    fields, "autoAcceptGroup",
                    augmentRecord.autoAcceptGroup.decode("utf-8")
                )

            self._assignToField(
                fields, "loginAllowed",
                augmentRecord.enabledForLogin
            )

            self._assignToField(
                fields, "serviceNodeUID",
                augmentRecord.serverID.decode("utf-8")
            )

        else:
            self._assignToField(fields, "hasCalendars", False)
            self._assignToField(fields, "hasContacts", False)
            self._assignToField(fields, "loginAllowed", False)

        # print("Augmented fields", fields)

        # Clone to a new record with the augmented fields
        augmentedRecord = AugmentedDirectoryRecord(self, record, fields)

        returnValue(augmentedRecord)

    @inlineCallbacks
    def setAutoScheduleMode(self, record, autoScheduleMode):
        augmentRecord = yield self._augmentDB.getAugmentRecord(
            record.uid,
            self.recordTypeToOldName(record.recordType)
        )
        if augmentRecord is not None:
            autoScheduleMode = {
                AutoScheduleMode.none: "none",
                AutoScheduleMode.accept: "accept-always",
                AutoScheduleMode.decline: "decline-always",
                AutoScheduleMode.acceptIfFree: "accept-if-free",
                AutoScheduleMode.declineIfBusy: "decline-if-busy",
                AutoScheduleMode.acceptIfFreeDeclineIfBusy: "automatic",
            }.get(autoScheduleMode)

            augmentRecord.autoScheduleMode = autoScheduleMode
            yield self._augmentDB.addAugmentRecords([augmentRecord])


class AugmentedDirectoryRecord(DirectoryRecord, CalendarDirectoryRecordMixin):
    """
    Augmented directory record.
    """

    def __init__(self, service, baseRecord, augmentedFields):
        DirectoryRecord.__init__(self, service, augmentedFields)
        CalendarDirectoryRecordMixin.__init__(self)
        self._baseRecord = baseRecord

    @timed
    @inlineCallbacks
    def members(self):
        augmented = []
        records = yield self._baseRecord.members()

        for record in records:
            augmented.append((yield self.service._augment(record)))

        returnValue(augmented)

    def addMembers(self, memberRecords):
        return self._baseRecord.addMembers(memberRecords)

    def removeMembers(self, memberRecords):
        return self._baseRecord.removeMembers(memberRecords)

    def setMembers(self, memberRecords):
        return self._baseRecord.setMembers(memberRecords)

    @timed
    @inlineCallbacks
    def groups(self):
        augmented = []

        def _groupUIDsFor(txn):
            return txn.groupUIDsFor(self.uid)

        groupUIDs = yield self.service._store.inTransaction(
            "AugmentedDirectoryRecord.groups",
            _groupUIDsFor
        )

        for groupUID in groupUIDs:
            groupRecord = yield self.service.recordWithUID(
                groupUID
            )
            if groupRecord:
                augmented.append((yield self.service._augment(groupRecord)))

        returnValue(augmented)

    @timed
    def verifyPlaintextPassword(self, password):
        return self._baseRecord.verifyPlaintextPassword(password)

    @timed
    def verifyHTTPDigest(self, *args):
        return self._baseRecord.verifyHTTPDigest(*args)

    @timed
    def accessForRecord(self, record):
        return self._baseRecord.accessForRecord(record)
