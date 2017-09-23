# -*- test-case-name: txdav.who.test.test_groups -*-
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
Group membership caching
"""

from twext.enterprise.dal.record import fromTable
from twext.enterprise.dal.syntax import Select
from twext.enterprise.jobs.workitem import AggregatedWorkItem, RegeneratingWorkItem
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue, succeed, \
    DeferredList
from twistedcaldav.config import config
from txdav.caldav.datastore.sql import CalendarStoreFeatures
from txdav.caldav.datastore.sql_directory import GroupAttendeeRecord
from txdav.common.datastore.sql_directory import GroupsRecord
from txdav.common.datastore.sql_tables import schema, _BIND_MODE_OWN
import datetime
import itertools
import time
from txdav.who.delegates import Delegates

log = Logger()


class GroupCacherPollingWork(
    RegeneratingWorkItem,
    fromTable(schema.GROUP_CACHER_POLLING_WORK)
):

    group = "group_cacher_polling"

    @classmethod
    def initialSchedule(cls, store, seconds):
        def _enqueue(txn):
            return GroupCacherPollingWork.reschedule(txn, seconds)

        if config.GroupCaching.Enabled:
            return store.inTransaction("GroupCacherPollingWork.initialSchedule", _enqueue)
        else:
            return succeed(None)

    def regenerateInterval(self):
        """
        Return the interval in seconds between regenerating instances.
        """
        groupCacher = getattr(self.transaction, "_groupCacher", None)
        return groupCacher.updateSeconds if groupCacher else 10

    @inlineCallbacks
    def doWork(self):

        groupCacher = getattr(self.transaction, "_groupCacher", None)
        if groupCacher is not None:

            startTime = time.time()
            try:
                yield groupCacher.update(self.transaction)
            except Exception as e:
                log.error(
                    "Failed to update new group membership cache ({error})",
                    error=e
                )
            endTime = time.time()
            log.debug(
                "GroupCacher polling took {duration:0.2f} seconds",
                duration=(endTime - startTime)
            )


class GroupRefreshWork(AggregatedWorkItem, fromTable(schema.GROUP_REFRESH_WORK)):

    group = property(lambda self: (self.table.GROUP_UID == self.groupUID))

    @inlineCallbacks
    def doWork(self):
        groupCacher = getattr(self.transaction, "_groupCacher", None)
        if groupCacher is not None:

            try:
                yield groupCacher.refreshGroup(
                    self.transaction, self.groupUID.decode("utf-8")
                )
            except Exception as e:
                log.error(
                    "Failed to refresh group {group} {err}",
                    group=self.groupUID, err=e
                )

        else:
            log.debug(
                "Rescheduling group refresh for {group}: {when}",
                group=self.groupUID,
                when=datetime.datetime.utcnow() + datetime.timedelta(seconds=10)
            )
            yield self.reschedule(self.transaction, 10, groupUID=self.groupUID)


class GroupDelegateChangesWork(AggregatedWorkItem, fromTable(schema.GROUP_DELEGATE_CHANGES_WORK)):

    group = property(lambda self: (self.table.DELEGATOR_UID == self.delegatorUID))

    @inlineCallbacks
    def doWork(self):
        groupCacher = getattr(self.transaction, "_groupCacher", None)
        if groupCacher is not None:

            try:
                yield groupCacher.applyExternalAssignments(
                    self.transaction,
                    self.delegatorUID.decode("utf-8"),
                    self.readDelegateUID.decode("utf-8"),
                    self.writeDelegateUID.decode("utf-8")
                )
            except Exception as e:
                log.error(
                    "Failed to apply external delegates for {uid} {err}",
                    uid=self.delegatorUID, err=e
                )


class GroupAttendeeReconciliationWork(
    AggregatedWorkItem, fromTable(schema.GROUP_ATTENDEE_RECONCILE_WORK)
):

    group = property(
        lambda self: (self.table.RESOURCE_ID == self.resourceID)
    )

    @inlineCallbacks
    def doWork(self):

        # get db object
        calendarObject = yield CalendarStoreFeatures(
            self.transaction._store
        ).calendarObjectWithID(
            self.transaction, self.resourceID
        )
        yield calendarObject.groupAttendeeChanged(self.groupID)


class GroupShareeReconciliationWork(
    AggregatedWorkItem, fromTable(schema.GROUP_SHAREE_RECONCILE_WORK)
):

    group = property(
        lambda self: (self.table.CALENDAR_ID == self.calendarID)
    )

    @inlineCallbacks
    def doWork(self):

        bind = schema.CALENDAR_BIND
        rows = yield Select(
            [bind.HOME_RESOURCE_ID],
            From=bind,
            Where=(bind.CALENDAR_RESOURCE_ID == self.calendarID).And(
                bind.BIND_MODE == _BIND_MODE_OWN
            ),
        ).on(self.transaction)
        if rows:
            homeID = rows[0][0]
            home = yield self.transaction.calendarHomeWithResourceID(homeID)
            calendar = yield home.childWithID(self.calendarID)
            # Might be None if the calendar is in the trash or was removed before the work started
            if calendar is not None:
                group = (yield self.transaction.groupByID(self.groupID))
                yield calendar.reconcileGroupSharee(group.groupUID)


def diffAssignments(old, new):
    """
    Compare two proxy assignment lists and return their differences in the form
    of two lists -- one for added/updated assignments, and one for removed
    assignments.

    @param old: dictionary of delegator: (readGroupUID, writeGroupUID)
    @type old: C{dict}

    @param new: dictionary of delegator: (readGroupUID, writeGroupUID)
    @type new: C{dict}

    @return: Tuple of two lists; the first list contains tuples of (delegator,
        (readGroupUID, writeGroupUID)), and represents all the new or updated
        assignments.  The second list contains all the delegators which used to
        have a delegate but don't anymore.
    """
    changed = []
    removed = []
    for key in old.iterkeys():
        if key not in new:
            removed.append(key)
        else:
            if old[key] != new[key]:
                changed.append((key, new[key]))
    for key in new.iterkeys():
        if key not in old:
            changed.append((key, new[key]))
    return changed, removed


class GroupCacher(object):
    log = Logger()

    def __init__(
        self, directory,
        updateSeconds=600,
        initialSchedulingDelaySeconds=10,
        batchSize=100,
        batchSchedulingIntervalSeconds=2,
        useDirectoryBasedDelegates=False,
        directoryBasedDelegatesSource=None,
        cacheNotifier=None,
    ):
        self.directory = directory
        self.useDirectoryBasedDelegates = useDirectoryBasedDelegates
        if useDirectoryBasedDelegates and directoryBasedDelegatesSource is None:
            directoryBasedDelegatesSource = self.directory.recordsWithDirectoryBasedDelegates
        self.directoryBasedDelegatesSource = directoryBasedDelegatesSource
        self.cacheNotifier = cacheNotifier
        self.updateSeconds = updateSeconds
        self.initialSchedulingDelaySeconds = initialSchedulingDelaySeconds
        self.batchSize = batchSize
        self.batchSchedulingIntervalSeconds = batchSchedulingIntervalSeconds

    @inlineCallbacks
    def update(self, txn):

        if self.useDirectoryBasedDelegates:
            # Pull in delegate assignments from the directory and stick them
            # into the delegate db
            recordsWithDirectoryBasedDelegates = yield self.directoryBasedDelegatesSource()
            externalAssignments = {}
            for record in recordsWithDirectoryBasedDelegates:
                try:
                    readWriteProxy = record.readWriteProxy
                except AttributeError:
                    readWriteProxy = None
                try:
                    readOnlyProxy = record.readOnlyProxy
                except AttributeError:
                    readOnlyProxy = None

                if readOnlyProxy or readWriteProxy:
                    externalAssignments[record.uid] = (readOnlyProxy, readWriteProxy)

            yield self.scheduleExternalAssignments(txn, externalAssignments)

        # Figure out which groups matter
        groupUIDs = yield self.groupsToRefresh(txn)
        # self.log.debug(
        #     "Groups to refresh: {g}", g=groupUIDs
        # )

        # Get the set of all known groups in the DB
        knownGroupUIDs = yield txn.allGroups()

        # We'll want to remove groups no longer in use
        groupsToRemove = knownGroupUIDs - groupUIDs

        # Also look for groups which have been marked as missing for a while
        if config.AutomaticPurging.Enabled:
            dateLimit = (
                datetime.datetime.utcnow() -
                datetime.timedelta(seconds=float(config.AutomaticPurging.GroupPurgeIntervalSeconds))
            )
            missingGroups = yield txn.groupsMissingSince(dateLimit)
            groupsToRemove |= missingGroups

        # Delete the groups in batches
        groupsToRemove = list(groupsToRemove)
        batchSize = 100
        deletedGroupUIDs = []
        while groupsToRemove:
            batch = groupsToRemove[:batchSize]
            del groupsToRemove[:batchSize]

            rows = yield GroupsRecord.deletesome(
                txn,
                GroupsRecord.groupUID.In(batch),
                returnCols=GroupsRecord.groupUID,
            )
            deletedGroupUIDs.extend([row[0] for row in rows])

        if deletedGroupUIDs:
            self.log.debug(
                "Deleted old or unused groups {d}", d=deletedGroupUIDs
            )

        # For each of those groups, create a per-group refresh work item
        futureSeconds = self.initialSchedulingDelaySeconds
        i = 0
        for groupUID in set(groupUIDs) - set(deletedGroupUIDs):
            self.log.debug(
                "Enqueuing group refresh for {u} in {sec} seconds",
                u=groupUID, sec=futureSeconds
            )
            yield GroupRefreshWork.reschedule(txn, futureSeconds, groupUID=groupUID)
            i += 1
            if i % self.batchSize == 0:
                i = 0
                futureSeconds += self.batchSchedulingIntervalSeconds

    @inlineCallbacks
    def scheduleExternalAssignments(
        self, txn, newAssignments, immediately=False
    ):

        oldAssignments = yield txn.externalDelegates()

        # external assignments is of the form:
        # { delegatorUID: (readDelegateGroupUID, writeDelegateGroupUID),
        # }

        changed, removed = diffAssignments(oldAssignments, newAssignments)
        if changed:
            for (
                delegatorUID, (readDelegateUID, writeDelegateUID)
            ) in changed:
                self.log.debug(
                    "Scheduling external delegate assignment changes for {uid}",
                    uid=delegatorUID
                )
                if not readDelegateUID:
                    readDelegateUID = ""
                if not writeDelegateUID:
                    writeDelegateUID = ""
                if immediately:
                    yield self.applyExternalAssignments(
                        txn, delegatorUID, readDelegateUID, writeDelegateUID
                    )
                else:
                    yield GroupDelegateChangesWork.reschedule(
                        txn, 0, delegatorUID=delegatorUID,
                        readDelegateUID=readDelegateUID,
                        writeDelegateUID=writeDelegateUID
                    )
        if removed:
            for delegatorUID in removed:
                self.log.debug(
                    "Scheduling external delegation assignment removal for {uid}",
                    uid=delegatorUID
                )
                if immediately:
                    yield self.applyExternalAssignments(
                        txn, delegatorUID, "", ""
                    )
                else:
                    yield GroupDelegateChangesWork.reschedule(
                        txn, 0, delegatorUID=delegatorUID,
                        readDelegateUID="", writeDelegateUID=""
                    )

    @inlineCallbacks
    def applyExternalAssignments(
        self, txn, delegatorUID, readDelegateUID, writeDelegateUID
    ):
        self.log.debug(
            "External delegate assignments changed for {uid}",
            uid=delegatorUID
        )

        # Retrieve the previous delegate assignments (if any) because we have
        # to invalidate their cached entries after calling assignExternalDelegates()
        (previousReadDelegateUID, previousWriteDelegateUID) = yield txn.externalDelegatesForDelegator(delegatorUID)

        readDelegateGroupID = writeDelegateGroupID = None

        if readDelegateUID:
            readDelegateGroup = yield txn.groupByUID(readDelegateUID)
            if readDelegateGroup is None:
                # The group record does not actually exist
                readDelegateUID = None
            else:
                readDelegateGroupID = readDelegateGroup.groupID

        if writeDelegateUID:
            writeDelegateGroup = yield txn.groupByUID(writeDelegateUID)
            if writeDelegateGroup is None:
                # The group record does not actually exist
                writeDelegateUID = None
            else:
                writeDelegateGroupID = writeDelegateGroup.groupID

        yield txn.assignExternalDelegates(
            delegatorUID, readDelegateGroupID, writeDelegateGroupID,
            readDelegateUID, writeDelegateUID
        )

        # Send cache change notifications
        if self.cacheNotifier is not None:
            self.cacheNotifier.changed(delegatorUID)
            if readDelegateUID:
                self.cacheNotifier.changed(readDelegateUID)
            if writeDelegateUID:
                self.cacheNotifier.changed(writeDelegateUID)
            if previousReadDelegateUID:
                self.cacheNotifier.changed(previousReadDelegateUID)
            if previousWriteDelegateUID:
                self.cacheNotifier.changed(previousWriteDelegateUID)

        # Invalidate the relevant memcached entries
        yield Delegates.invalidateExternalAssignment(txn, delegatorUID, readDelegateUID, writeDelegateUID, previousReadDelegateUID, previousWriteDelegateUID)

    @inlineCallbacks
    def refreshGroup(self, txn, groupUID):
        """
        Does the work of a per-group refresh work item
        Faults in the flattened membership of a group, as UIDs
        and updates the GROUP_MEMBERSHIP table
        WorkProposal is returned for tests
        """
        self.log.debug("Refreshing group: {g}", g=groupUID)

        record = (yield self.directory.recordWithUID(groupUID))
        if record is None:
            # the group has disappeared from the directory
            self.log.info("Group is missing: {g}", g=groupUID)
        else:
            self.log.debug("Got group record: {u}", u=record.uid)

        group = yield txn.groupByUID(groupUID, create=(record is not None))

        if group:
            membershipChanged, addedUIDs, removedUIDs = yield txn.refreshGroup(group, record)

            if membershipChanged:
                self.log.info(
                    "Membership changed for group {uid} {name}:\n\tadded {added}\n\tremoved {removed}",
                    uid=group.groupUID,
                    name=group.name,
                    added=",".join(addedUIDs),
                    removed=",".join(removedUIDs),
                )

                # Send cache change notifications
                if self.cacheNotifier is not None:
                    self.cacheNotifier.changed(group.groupUID)
                    for uid in itertools.chain(addedUIDs, removedUIDs):
                        self.cacheNotifier.changed(uid)

                # Notifier other store APIs of changes
                wpsAttendee = yield self.scheduleGroupAttendeeReconciliations(txn, group.groupID)
                wpsShareee = yield self.scheduleGroupShareeReconciliations(txn, group.groupID)

                returnValue(wpsAttendee + wpsShareee)
            else:
                self.log.debug(
                    "No membership change for group {uid} {name}",
                    uid=group.groupUID,
                    name=group.name
                )

        returnValue(tuple())

    def synchronizeMembers(self, txn, groupID, newMemberUIDs):
        return txn.synchronizeMembers(groupID, newMemberUIDs)

    def cachedMembers(self, txn, groupID):
        """
        The members of the given group as recorded in the db
        """
        return txn.groupMembers(groupID)

    def cachedGroupsFor(self, txn, uid):
        """
        The UIDs of the groups the uid is a member of
        """
        return txn.groupUIDsFor(uid)

    @inlineCallbacks
    def scheduleGroupAttendeeReconciliations(self, txn, groupID):
        """
        Find all events who have this groupID as an attendee and create
        work items for them.
        returns: WorkProposal
        """

        records = yield GroupAttendeeRecord.querysimple(txn, groupID=groupID)

        workItems = []
        for record in records:
            work = yield GroupAttendeeReconciliationWork.reschedule(
                txn,
                seconds=float(config.GroupAttendees.ReconciliationDelaySeconds),
                resourceID=record.resourceID,
                groupID=groupID,
            )
            workItems.append(work)
        returnValue(tuple(workItems))

    @inlineCallbacks
    def scheduleGroupShareeReconciliations(self, txn, groupID):
        """
        Find all calendars who have shared to this groupID and create
        work items for them.
        returns: WorkProposal
        """
        gs = schema.GROUP_SHAREE
        rows = yield Select(
            [gs.CALENDAR_ID, ],
            From=gs,
            Where=gs.GROUP_ID == groupID,
        ).on(txn)

        workItems = []
        for [calendarID] in rows:
            work = yield GroupShareeReconciliationWork.reschedule(
                txn,
                seconds=float(config.Sharing.Calendars.Groups.ReconciliationDelaySeconds),
                calendarID=calendarID,
                groupID=groupID,
            )
            workItems.append(work)
        returnValue(tuple(workItems))

    @inlineCallbacks
    def groupsToRefresh(self, txn):
        delegatedUIDs = set((yield txn.allGroupDelegates()))
        self.log.debug(
            "There are {count} group delegates", count=len(delegatedUIDs)
        )

        # Also get group delegates from other pods
        if (
            txn.directoryService().serversDB() is not None and
            len(txn.directoryService().serversDB().allServersExceptThis(filter_v5=True)) != 0
        ):
            results = yield DeferredList([
                txn.store().conduit.send_all_group_delegates(txn, server) for
                server in txn.directoryService().serversDB().allServersExceptThis(filter_v5=True)
            ], consumeErrors=True)
            for result in results:
                if result and result[0]:
                    delegatedUIDs.update(result[1])
            self.log.debug(
                "There are {count} group delegates on this and other pods", count=len(delegatedUIDs)
            )

        # Get groupUIDs for all group attendees
        groups = yield GroupsRecord.query(
            txn,
            GroupsRecord.groupID.In(GroupAttendeeRecord.queryExpr(
                expr=None,
                attributes=(GroupAttendeeRecord.groupID,),
                distinct=True,
            ))
        )
        attendeeGroupUIDs = frozenset([group.groupUID for group in groups])
        self.log.debug(
            "There are {count} group attendees", count=len(attendeeGroupUIDs)
        )

        # Get groupUIDs for all group shares
        gs = schema.GROUP_SHAREE
        gr = schema.GROUPS
        rows = yield Select(
            [gr.GROUP_UID],
            From=gr,
            Where=gr.GROUP_ID.In(
                Select(
                    [gs.GROUP_ID],
                    From=gs,
                    Distinct=True
                )
            )
        ).on(txn)
        shareeGroupUIDs = frozenset([row[0] for row in rows])
        self.log.debug(
            "There are {count} group sharees", count=len(shareeGroupUIDs)
        )

        returnValue(frozenset(delegatedUIDs | attendeeGroupUIDs | shareeGroupUIDs))
