# -*- test-case-name: txdav.caldav.datastore.test.test_sql -*-
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


"""
SQL backend for CalDAV storage.
"""

__all__ = [
    "CalendarHome",
    "Calendar",
    "CalendarObject",
]

from twext.enterprise.dal.record import fromTable, SerializableRecord
from twext.enterprise.dal.syntax import Count, ColumnSyntax, Delete, \
    Insert, Len, Max, Parameter, Select, Update, utcNowSQL, Union
from twext.enterprise.locking import NamedLock
from twext.enterprise.jobs.jobitem import JobItem
from twext.enterprise.jobs.workitem import WorkItem, AggregatedWorkItem, \
    WORK_PRIORITY_LOW, WORK_WEIGHT_5, WORK_WEIGHT_3
from twext.enterprise.util import parseSQLTimestamp
from twext.python.clsprop import classproperty
from twext.python.log import Logger
from twext.who.idirectory import RecordType
from txweb2.http_headers import MimeType
from txweb2.stream import readStream

from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.python.failure import Failure

from twistedcaldav import customxml, ical
from twistedcaldav.stdconfig import config
from twistedcaldav.datafilters.peruserdata import PerUserDataFilter
from twistedcaldav.dateops import normalizeForIndex, \
    pyCalendarToSQLTimestamp, parseSQLDateToPyCalendar
from twistedcaldav.ical import Component, InvalidICalendarDataError, Property, ATTENDEE_COMMENT
from twistedcaldav.instance import InvalidOverriddenInstanceError
from twistedcaldav.timezones import TimezoneException, readVTZ, hasTZ

from txdav.base.propertystore.base import PropertyName
from txdav.caldav.datastore.query.builder import buildExpression
from txdav.caldav.datastore.query.filter import Filter
from txdav.caldav.datastore.query.generator import CalDAVSQLQueryGenerator
from txdav.caldav.datastore.scheduling.cuaddress import calendarUserFromCalendarUserAddress
from txdav.caldav.datastore.scheduling.icaldiff import iCalDiff
from txdav.caldav.datastore.scheduling.icalsplitter import iCalSplitter
from txdav.caldav.datastore.scheduling.imip.token import iMIPTokenRecord
from txdav.caldav.datastore.scheduling.implicit import ImplicitScheduler
from txdav.caldav.datastore.scheduling.utils import uidFromCalendarUserAddress
from txdav.caldav.datastore.scheduling.work import allScheduleWork, ScheduleWork
from txdav.caldav.datastore.sql_attachment import Attachment, DropBoxAttachment, \
    AttachmentLink, ManagedAttachment
from txdav.caldav.datastore.sql_directory import GroupAttendeeRecord, \
    GroupShareeRecord
from txdav.caldav.datastore.util import normalizationLookup
from txdav.caldav.datastore.util import CalendarObjectBase
from txdav.caldav.datastore.util import dropboxIDFromCalendarObject
from txdav.caldav.icalendarstore import ICalendarHome, ICalendar, ICalendarObject, \
    AttachmentStoreFailed, AttachmentStoreValidManagedID, \
    TooManyAttendeesError, InvalidComponentTypeError, InvalidCalendarAccessError, \
    ResourceDeletedError, \
    AttendeeAllowedError, InvalidPerUserDataMerge, ComponentUpdateState, \
    ValidOrganizerError, ShareeAllowedError, ComponentRemoveState, \
    InvalidDefaultCalendar, \
    InvalidAttachmentOperation, DuplicatePrivateCommentsError, \
    TimeRangeUpperLimit, TimeRangeLowerLimit, InvalidSplit, \
    UnknownTimezone, SetComponentOptions, TooManyAttachments
from txdav.common.datastore.sql import CommonHome, CommonHomeChild, \
    CommonObjectResource, ECALENDARTYPE
from txdav.common.datastore.sql_directory import GroupsRecord, \
    GroupMembershipRecord
from txdav.common.datastore.sql_tables import _ATTACHMENTS_MODE_NONE, \
    _ATTACHMENTS_MODE_READ, _ATTACHMENTS_MODE_WRITE, _BIND_MODE_DIRECT, \
    _BIND_MODE_GROUP, _BIND_MODE_GROUP_READ, _BIND_MODE_GROUP_WRITE, \
    _BIND_MODE_OWN, _BIND_MODE_READ, _BIND_MODE_WRITE, _BIND_STATUS_ACCEPTED, \
    _TRANSP_OPAQUE, _TRANSP_TRANSPARENT, schema, _CHILD_TYPE_TRASH, \
    _HOME_STATUS_NORMAL
from txdav.common.datastore.sql_sharing import SharingInvitation
from txdav.common.icommondatastore import IndexedSearchException, \
    InternalDataStoreError, HomeChildNameAlreadyExistsError, \
    HomeChildNameNotAllowedError, ObjectResourceTooBigError, \
    InvalidObjectResourceError, ObjectResourceNameAlreadyExistsError, \
    ObjectResourceNameNotAllowedError, TooManyObjectResourcesError, \
    InvalidUIDError, UIDExistsError, UIDExistsElsewhereError, \
    InvalidResourceMove, InvalidComponentForStoreError, \
    NoSuchObjectResourceError, ConcurrentModification
from txdav.xml import element
from txdav.xml.parser import WebDAVDocument

from txdav.idav import ChangeCategory

from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from pycalendar.timezone import Timezone
from pycalendar.value import Value

from zope.interface.declarations import implements

from urlparse import urlparse, urlunparse
import collections
import datetime
import hashlib
import itertools
import urllib
import uuid

log = Logger()


class CalendarStoreFeatures(object):
    """
    Manages store-wide operations specific to calendars.
    """

    def __init__(self, store):
        """
        @param store: the underlying store object to use.
        @type store: L{twext.common.datastore.sql.CommonDataStore}
        """
        self._store = store

    @inlineCallbacks
    def hasDropboxAttachments(self, txn):
        """
        Determine whether any dropbox attachments are present.

        @param txn: the transaction to run under
        @type txn: L{txdav.common.datastore.sql.CommonStoreTransaction}
        """

        at = Attachment._attachmentSchema
        rows = (yield Select(
            (at.DROPBOX_ID,),
            From=at,
            Where=at.DROPBOX_ID != ".",
            Limit=1,
        ).on(txn))
        returnValue(len(rows) != 0)

    @inlineCallbacks
    def upgradeToManagedAttachments(self, batchSize=10):
        """
        Upgrade the calendar server from old-style dropbox attachments to the new
        managed attachments. This is a one-way, one-time migration step. This method
        creates its own transactions as needed (possibly multiple when batching).

        Things to do:

        1. For any CALENDAR_OBJECT rows with a DROPBOX_ID not matching an existing DROPBOX_ID
        in the ATTACHMENT table, null out CALENDAR_OBJECT.DROPBOX_ID. Do not rewrite calendar
        data to remove X-APPLE-DROPBOX.

        2. For each item in the ATTACHMENT table, convert into a managed attachment and re-write
        all calendar data referring to that attachment.

        TODO: parallelize this as much as possible as it will have to touch a lot of data.
        """

        txn = self._store.newTransaction("CalendarStoreFeatures.upgradeToManagedAttachments - preliminary work")
        try:
            # Clear out unused CALENDAR_OBJECT.DROPBOX_IDs
            co = CalendarObject._objectSchema
            at = Attachment._attachmentSchema
            yield Update(
                {co.DROPBOX_ID: None},
                Where=co.RESOURCE_ID.In(Select(
                    (co.RESOURCE_ID,),
                    From=co.join(at, co.DROPBOX_ID == at.DROPBOX_ID, "left outer"),
                    Where=(co.DROPBOX_ID != None).And(at.DROPBOX_ID == None),
                )),
            ).on(txn)

            # Count number to process so we can display progress
            rows = (yield Select(
                (at.DROPBOX_ID,),
                From=at,
                Where=at.DROPBOX_ID != ".",
                Distinct=True,
            ).on(txn))
            total = len(rows)
            count = 0
            log.warn("{total} dropbox ids to migrate", total=total)
        except RuntimeError as e:
            log.error("Dropbox migration failed when cleaning out dropbox ids: {ex}", ex=e)
            yield txn.abort()
            raise
        else:
            yield txn.commit()

        # For each remaining attachment
        rows = -1
        while rows:
            txn = self._store.newTransaction("CalendarStoreFeatures.upgradeToManagedAttachments - attachment loop count: {0}".format(count,))
            try:
                dropbox_id = "Batched select"
                rows = (yield Select(
                    (at.DROPBOX_ID,),
                    From=at,
                    Where=at.DROPBOX_ID != ".",
                    Limit=batchSize,
                    Distinct=True,
                ).on(txn))
                if len(rows) > 0:
                    for dropbox_id in rows:
                        (yield self._upgradeDropbox(txn, dropbox_id))
                    count += len(rows)
                    log.warn("{count} of {total} dropbox ids migrated", count=count, total=total)
            except RuntimeError as e:
                log.error("Dropbox migration failed for '{id}': {ex}", id=dropbox_id, ex=e)
                yield txn.abort()
                raise
            else:
                yield txn.commit()

    @inlineCallbacks
    def _upgradeDropbox(self, txn, dropbox_id):
        """
        Upgrade attachments for the corresponding dropbox box to managed attachments. This is tricky
        in that we have to spot the case of a dropbox attachment being used by more than one event
        in the owner's home (e.g., the case of a recurrence split). We have to give each owned event
        its own managed attachment reference (though they point to the same actual attachment data).
        So we need to detect owned attachments and group by UID.

        @param dropbox_id: the dropbox id to upgrade
        @type dropbox_id: C{str}
        """

        log.debug("Processing dropbox id: {id}", id=dropbox_id)

        # Get all affected calendar objects
        cobjs = (yield self._loadCalendarObjectsForDropboxID(txn, dropbox_id))
        log.debug("  {len} affected calendar objects", len=len(cobjs))

        # Get names of each matching attachment
        at = Attachment._attachmentSchema
        names = (yield Select(
            (at.PATH,),
            From=at,
            Where=at.DROPBOX_ID == dropbox_id,
        ).on(txn))
        log.debug("  {len} associated attachment objects", len=len(names))

        # For each attachment, update each calendar object
        for name in names:
            name = name[0]
            log.debug("  processing attachment object: {name}", name=name)
            attachment = (yield DropBoxAttachment.load(txn, dropbox_id, name))

            # Check for orphans
            if len(cobjs) == 0:
                # Just remove the attachment
                log.warn("Orphaned dropbox id removed: {path}", path=attachment._path)
                yield attachment.remove()
                continue

            # Find owner objects and group all by UID
            owners = []
            cobj_by_UID = collections.defaultdict(list)
            for cobj in cobjs:
                if cobj._parentCollection.ownerHome()._resourceID == attachment._ownerHomeID:
                    owners.append(cobj)
                cobj_by_UID[cobj.uid()].append(cobj)
            log.debug("    {len} owner calendar objects", len=len(owners))
            log.debug("    {len} UIDs", len=len(cobj_by_UID))
            log.debug("    {len} total calendar objects", len=sum([len(items) for items in cobj_by_UID.values()]))

            if owners:
                # Create the managed attachment without references to calendar objects.
                managed = (yield attachment.convertToManaged())
                log.debug("    converted attachment: {att!r}", att=attachment)

                # Do conversion for each owner object
                for owner_obj in owners:

                    # Add a reference to the managed attachment
                    mattachment = (yield managed.newReference(owner_obj._resourceID))
                    log.debug("    added reference for: {owner!r}", owner=owner_obj)

                    # Rewrite calendar data
                    for cobj in cobj_by_UID[owner_obj.uid()]:
                        (yield cobj.convertAttachments(attachment, mattachment))
                        log.debug("    re-wrote calendar object: {owner!r}", owner=cobj)
            else:
                # TODO: look for cobjs that were not changed and remove their ATTACH properties.
                # These could happen if the owner object no longer exists.
                # For now just remove the attachment
                log.warn("Unowned dropbox id removed: {path}", path=attachment._path)
                yield attachment.remove()
                continue

        log.debug("  finished dropbox id: {id}", id=dropbox_id)

    @inlineCallbacks
    def _loadCalendarObjectsForDropboxID(self, txn, dropbox_id):
        """
        Load all calendar objects (and associated calendars and homes) that match the
        specified dropbox id.

        @param dropbox_id: the dropbox id to match.
        @type dropbox_id: C{str}
        """

        co = CalendarObject._objectSchema
        cb = Calendar._bindSchema
        rows = (yield Select(
            (cb.CALENDAR_HOME_RESOURCE_ID, co.CALENDAR_RESOURCE_ID, co.RESOURCE_ID,),
            From=co.join(cb, co.CALENDAR_RESOURCE_ID == cb.CALENDAR_RESOURCE_ID),
            Where=(co.DROPBOX_ID == dropbox_id).And(cb.BIND_MODE == _BIND_MODE_OWN)
        ).on(txn))

        results = []
        for home_rid, calendar_rid, cobj_rid in rows:
            home = (yield txn.calendarHomeWithResourceID(home_rid))
            calendar = (yield home.childWithID(calendar_rid))
            cobj = (yield calendar.objectResourceWithID(cobj_rid))
            results.append(cobj)

        returnValue(results)

    @inlineCallbacks
    def calendarObjectDetailsWithUID(self, txn, uid):
        """
        Return details about all child object resources with the specified UID.
        Only "owned" resources are returned, no shared resources.

        @param txn: transaction to use
        @type txn: L{CommonStoreTransaction}
        @param uid: UID to query
        @type uid: C{str}

        @return: list of C{list} of home id, calendar id, and calendar object id
        @rtype: C{list} of C{list}
        """

        # First find the resource-ids of the (home, parent, object) for each object matching the UID.
        obj = CalendarHome._objectSchema
        bind = CalendarHome._bindSchema
        rows = (yield Select(
            [bind.HOME_RESOURCE_ID, obj.PARENT_RESOURCE_ID, obj.RESOURCE_ID],
            From=obj.join(bind, obj.PARENT_RESOURCE_ID == bind.RESOURCE_ID),
            Where=(obj.UID == Parameter("uid")).And(bind.BIND_MODE == _BIND_MODE_OWN),
        ).on(txn, uid=uid))

        returnValue(rows)

    @inlineCallbacks
    def calendarObjectsWithUID(self, txn, uid):
        """
        Return all child object resources with the specified UID. Only "owned" resources are returned,
        no shared resources.

        WARNING: this method loads all the home, calendar, and calendar object
        resources for the specified UID. This is potentially a memory hog for
        the case of an event with a very large number of attendees. Please avoid
        using this. Instead use L{calendarObjectDetailsWithUID}, which just
        looks up the IDs, which can then be loaded one at a time when needed.
        When doing that, make sure that all references to previously processed
        objects are removed so that they don't hang around in memory and cause
        problems.

        @param txn: transaction to use
        @type txn: L{CommonStoreTransaction}
        @param uid: UID to query
        @type uid: C{str}

        @return: list of matching L{CalendarObject}s
        @rtype: C{list}
        """

        # First find the resource-ids of the (home, parent, object) for each object matching the UID.
        obj = CalendarHome._objectSchema
        bind = CalendarHome._bindSchema
        rows = (yield Select(
            [bind.HOME_RESOURCE_ID, obj.PARENT_RESOURCE_ID, obj.RESOURCE_ID],
            From=obj.join(bind, obj.PARENT_RESOURCE_ID == bind.RESOURCE_ID),
            Where=(obj.UID == Parameter("uid")).And(bind.BIND_MODE == _BIND_MODE_OWN),
        ).on(txn, uid=uid))

        results = []
        for homeID, parentID, childID in rows:
            home = (yield txn.calendarHomeWithResourceID(homeID))
            parent = (yield home.childWithID(parentID))
            child = (yield parent.objectResourceWithID(childID))
            results.append(child)

        returnValue(results)

    @inlineCallbacks
    def calendarObjectWithID(self, txn, rid):
        """
        Return all child object resources with the specified resource id. Only "owned" resources are returned,
        no shared resources.

        @param txn: transaction to use
        @type txn: L{CommonStoreTransaction}
        @param rid: resource-id to query
        @type rid: C{int}

        @return: matching calendar object, or None if not found
        @rtype: L{CalendarObject} or C{None}
        """

        # First find the resource-ids of the (home, parent, object) for each object matching the resource id.
        obj = CalendarHome._objectSchema
        bind = CalendarHome._bindSchema
        rows = (yield Select(
            [bind.HOME_RESOURCE_ID, obj.PARENT_RESOURCE_ID, obj.RESOURCE_ID],
            From=obj.join(bind, obj.PARENT_RESOURCE_ID == bind.RESOURCE_ID),
            Where=(obj.RESOURCE_ID == Parameter("rid")).And(bind.BIND_MODE == _BIND_MODE_OWN),
        ).on(txn, rid=rid))

        results = []
        for homeID, parentID, childID in rows:
            home = (yield txn.calendarHomeWithResourceID(homeID))
            parent = (yield home.childWithID(parentID))
            child = (yield parent.objectResourceWithID(childID))
            results.append(child)

        returnValue(results[0] if results else None)


class CalendarHomeRecord(SerializableRecord, fromTable(schema.CALENDAR_HOME)):
    """
    @DynamicAttrs
    L{Record} for L{schema.CALENDAR_HOME}.
    """
    pass


class CalendarMetaDataRecord(SerializableRecord, fromTable(schema.CALENDAR_METADATA)):
    """
    @DynamicAttrs
    L{Record} for L{schema.CALENDAR_METADATA}.
    """
    pass


class CalendarBindRecord(SerializableRecord, fromTable(schema.CALENDAR_BIND)):
    """
    @DynamicAttrs
    L{Record} for L{schema.CALENDAR_BIND}.
    """
    pass


class CalendarHome(CommonHome):

    implements(ICalendarHome)

    _homeType = ECALENDARTYPE

    # structured tables.  (new, preferred)
    _homeSchema = schema.CALENDAR_HOME
    _homeMetaDataSchema = schema.CALENDAR_HOME_METADATA

    _bindSchema = schema.CALENDAR_BIND
    _revisionsSchema = schema.CALENDAR_OBJECT_REVISIONS
    _objectSchema = schema.CALENDAR_OBJECT

    _notifierPrefix = "CalDAV"
    _dataVersionKey = "CALENDAR-DATAVERSION"

    _componentCalendarName = {
        "VEVENT": "calendar",
        "VTODO": "tasks",
        "VJOURNAL": "journals",
        "VAVAILABILITY": "available",
        "VPOLL": "polls",
    }

    _componentDefaultColumn = {
        "VEVENT": schema.CALENDAR_HOME_METADATA.DEFAULT_EVENTS,
        "VTODO": schema.CALENDAR_HOME_METADATA.DEFAULT_TASKS,
        "VPOLL": schema.CALENDAR_HOME_METADATA.DEFAULT_POLLS,
    }

    _componentDefaultAttribute = {
        "VEVENT": "_default_events",
        "VTODO": "_default_tasks",
        "VPOLL": "_default_polls",
    }

    @classmethod
    def metadataColumns(cls):
        """
        Return a list of column name for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        default_collections = tuple([cls._componentDefaultColumn[name] for name in sorted(cls._componentDefaultColumn.keys())])
        return default_collections + (
            cls._homeMetaDataSchema.TRASH,
            cls._homeMetaDataSchema.ALARM_VEVENT_TIMED,
            cls._homeMetaDataSchema.ALARM_VEVENT_ALLDAY,
            cls._homeMetaDataSchema.ALARM_VTODO_TIMED,
            cls._homeMetaDataSchema.ALARM_VTODO_ALLDAY,
            cls._homeMetaDataSchema.AVAILABILITY,
            cls._homeMetaDataSchema.CREATED,
            cls._homeMetaDataSchema.MODIFIED,
        )

    @classmethod
    def metadataAttributes(cls):
        """
        Return a list of attribute names for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        default_attributes = tuple([cls._componentDefaultAttribute[name] for name in sorted(cls._componentDefaultAttribute.keys())])
        return default_attributes + (
            "_trash",
            "_alarm_vevent_timed",
            "_alarm_vevent_allday",
            "_alarm_vtodo_timed",
            "_alarm_vtodo_allday",
            "_availability",
            "_created",
            "_modified",
        )

    createCalendarWithName = CommonHome.createChildWithName
    removeCalendarWithName = CommonHome.removeChildWithName
    calendarWithName = CommonHome.childWithName
    listCalendars = CommonHome.listChildren
    loadCalendars = CommonHome.loadChildren

    @inlineCallbacks
    def calendars(self, onlyInTrash=False):
        returnValue(
            [c for c in (yield self.children(onlyInTrash=onlyInTrash))]
        )

    @inlineCallbacks
    def remove(self):
        # delete attachments corresponding to this home, also removing from disk
        yield Attachment.removedHome(self._txn, self._resourceID)

        yield super(CalendarHome, self).remove()

    @inlineCallbacks
    def purgeAll(self):
        """
        Do a complete purge of all data associated with this calendar home. For now this will assume
        a "silent" non-implicit behavior. In the future we will want to build in some of the options
        the current set of "purge" CLI tools have to allow for cancels of future events etc.
        """
        # delete attachments corresponding to this home, also removing from disk
        yield Attachment.removedHome(self._txn, self._resourceID)

        yield super(CalendarHome, self).purgeAll()

    @inlineCallbacks
    def copyMetadata(self, other, calendarIDMap):
        """
        Copy metadata from one L{CalendarObjectResource} to another. This is only
        used during a migration step.
        """

        # Simple attributes that can be copied over as-is, but the calendar id's need to be mapped
        chm = self._homeMetaDataSchema
        values = {}
        for attr, col in zip(self.metadataAttributes(), self.metadataColumns()):
            value = getattr(other, attr)
            if attr in self._componentDefaultAttribute.values() + ["_trash", ]:
                value = calendarIDMap.get(value)
            setattr(self, attr, value)
            values[col] = value

        # Update the local data
        yield Update(
            values,
            Where=chm.RESOURCE_ID == self._resourceID
        ).on(self._txn)

        # Since database entry may have changed we need to invalidate any cached query
        yield self.invalidateQueryCache()

    @inlineCallbacks
    def hasCalendarResourceUIDSomewhereElse(self, uid, ok_object, mode):
        """
        Determine if this calendar home contains any calendar objects which
        would potentially conflict with the given UID for scheduling purposes.

        @param uid: The UID to search for.
        @type uid: C{str}

        @param ok_object: a calendar object with the given UID, that doesn't
            count as a potential conflict (since, for example, it is the one
            being updated).  May be C{None} if all objects potentially count.
        @type ok_object: L{CalendarObject} or C{NoneType}

        @param mode: a string, indicating the mode to check for conflicts.  If
            this is the string "schedule", then we are checking for potential
            conflicts with a new scheduled calendar object, which will conflict
            with any calendar object matching the given C{uid} in the home.
            Otherwise, (if this is the string "calendar") we are checking for
            conflicts with a new unscheduled calendar object, which will
            conflict only with other scheduled objects.
        @type mode: C{str}

        @return: a L{Deferred} which fires with C{True} if there is a conflict
            and C{False} if not.
        """
        # FIXME: this should be documented on the interface; it should also
        # refer to calendar *object* UIDs, since calendar *resources* are an
        # HTTP protocol layer thing, not a data store thing.  (See also
        # objectResourcesWithUID.)
        objectResources = (yield self.getCalendarResourcesForUID(uid))
        for objectResource in objectResources:
            # The matching calendar resource is in the trash, so delete it
            if objectResource.isInTrash():
                yield objectResource.purge(implicitly=False)
                continue
            if ok_object and objectResource._resourceID == ok_object._resourceID:
                continue
            matched_mode = ("schedule" if objectResource.isScheduleObject else "calendar")
            if mode == "schedule" or matched_mode == "schedule":
                returnValue(objectResource)

        returnValue(None)

    @inlineCallbacks
    def getCalendarResourcesForUID(self, uid):
        """
        Find all calendar object resources in the calendar home that are not in the "inbox" collection
        and not in shared collections.
        Cache the result of this query as it can happen multiple times during scheduling under slightly
        different circumstances.

        @param uid: the UID of the calendar object resources to find
        @type uid: C{str}
        """

        if not hasattr(self, "_cachedCalendarResourcesForUID"):
            self._cachedCalendarResourcesForUID = {}
        if uid not in self._cachedCalendarResourcesForUID:
            self._cachedCalendarResourcesForUID[uid] = (yield self.objectResourcesWithUID(uid, ["inbox"], allowShared=False))
        returnValue(self._cachedCalendarResourcesForUID[uid])

    def removedCalendarResource(self, uid):
        """
        Clean-up cache when resource is removed.
        """
        if hasattr(self, "_cachedCalendarResourcesForUID") and uid in self._cachedCalendarResourcesForUID:
            del self._cachedCalendarResourcesForUID[uid]

    @inlineCallbacks
    def calendarObjectWithDropboxID(self, dropboxID):
        """
        Implement lookup via queries.
        """
        co = self._objectSchema
        cb = self._bindSchema
        rows = (yield Select(
            [co.PARENT_RESOURCE_ID,
             co.RESOURCE_ID],
            From=co.join(cb, co.PARENT_RESOURCE_ID == cb.RESOURCE_ID,
                         'left outer'),
            Where=(co.DROPBOX_ID == dropboxID).And(
                cb.HOME_RESOURCE_ID == self._resourceID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME != "inbox"),
        ).on(self._txn))

        if rows:
            calendarID, objectID = rows[0]
            calendar = (yield self.childWithID(calendarID))
            if calendar:
                calendarObject = (yield calendar.objectResourceWithID(objectID))
                returnValue(calendarObject)
        returnValue(None)

    def getAllAttachments(self):
        """
        Return all the L{Attachment} objects associated with this calendar home.
        Needed during migration.
        """
        return Attachment.loadAllAttachments(self)

    def getAttachmentLinks(self):
        """
        Read the attachment<->calendar object mapping data associated with this calendar home.
        Needed during migration only.
        """
        return AttachmentLink.linksForHome(self)

    def getAttachmentByID(self, id):
        """
        Return a specific attachment associated with this calendar home.
        Needed during migration only.
        """
        return Attachment.loadAttachmentByID(self, id)

    @inlineCallbacks
    def getAllDropboxIDs(self):
        """
        Only return dropbox-IDs that actually have an attachment associated with them. To do that we will
        look at the ATTACHMENT table and get all (old) dropbox ids for the home. Then look at the combined
        ATTACHMENT and ATTACHMENT_CALENDAR_OBJECT table to get all managed attachment drop box ids. Those
        results need to be unique and sorted.
        """
        att = schema.ATTACHMENT
        attco = schema.ATTACHMENT_CALENDAR_OBJECT
        co = schema.CALENDAR_OBJECT

        # Old dropbox items (DROPBOX_ID != "." in ATTACHMENT table)
        rows = (yield Select(
            [att.DROPBOX_ID],
            From=att.join(co, att.DROPBOX_ID == co.DROPBOX_ID),
            Where=(att.CALENDAR_HOME_RESOURCE_ID == self._resourceID).And(
                att.DROPBOX_ID != ".").And(
                co.TRASHED == None),
            SetExpression=Union(
                Select(
                    [co.DROPBOX_ID],
                    From=att.join(
                        attco, att.ATTACHMENT_ID == attco.ATTACHMENT_ID).join(
                        co, attco.CALENDAR_OBJECT_RESOURCE_ID == co.RESOURCE_ID),
                    Where=(att.CALENDAR_HOME_RESOURCE_ID == self._resourceID).And(
                        att.DROPBOX_ID == ".").And(
                        co.TRASHED == None),
                ),
                optype=Union.OPTYPE_ALL,
            )
        ).on(self._txn))
        returnValue(sorted(set([row[0] for row in rows])))

    @inlineCallbacks
    def getAllAttachmentNames(self):
        att = Attachment._attachmentSchema
        rows = (yield Select(
            [att.DROPBOX_ID],
            From=att,
            Where=(att.CALENDAR_HOME_RESOURCE_ID == self._resourceID),
            OrderBy=att.DROPBOX_ID
        ).on(self._txn))
        returnValue([row[0] for row in rows])

    @inlineCallbacks
    def getAllManagedIDs(self):
        at = Attachment._attachmentSchema
        attco = Attachment._attachmentLinkSchema
        rows = (yield Select(
            [attco.MANAGED_ID, ],
            From=attco.join(at, attco.ATTACHMENT_ID == at.ATTACHMENT_ID),
            Where=at.CALENDAR_HOME_RESOURCE_ID == self._resourceID,
            OrderBy=attco.MANAGED_ID
        ).on(self._txn))
        returnValue([row[0] for row in rows])

    @inlineCallbacks
    def getAllGroupAttendees(self):
        """
        Return a list of L{GroupAttendeeRecord},L{GroupRecord} for each group attendee referenced in calendar data
        owned by this home.
        """

        results = []
        calendars = yield self.loadChildren()
        for calendar in calendars:
            if not calendar.owned():
                continue
            children = yield calendar.objectResources()
            cobjs = [child.id() for child in children]
            if cobjs:
                result = yield GroupAttendeeRecord.groupAttendeesForObjects(self._txn, cobjs)
                results.extend(result)

        returnValue(results)

    @inlineCallbacks
    def createdHome(self):

        # Check whether components type must be separate
        if config.RestrictCalendarsToOneComponentType:
            for name in ical.allowedStoreComponents:
                cal = yield self.createCalendarWithName(self._componentCalendarName[name])
                yield cal.setSupportedComponents(name)
                if name not in ("VEVENT", "VAVAILABILITY",):
                    yield cal.setUsedForFreeBusy(False)
                yield self.setDefaultCalendar(cal, name)
        else:
            cal = yield self.createCalendarWithName("calendar")
            for name in ical.allowedStoreComponents:
                yield self.setDefaultCalendar(cal, name)

        inbox = yield self.createCalendarWithName("inbox")
        yield inbox.setUsedForFreeBusy(False)

    @inlineCallbacks
    def splitCalendars(self):
        """
        Split all regular calendars by component type
        """

        # Make sure the loop does not operate on any new calendars created during the loop
        self.log.warn("Splitting calendars for user {uid}", uid=self._ownerUID)
        calendars = yield self.calendars()
        for calendar in calendars:

            # Ignore inbox - also shared calendars are not part of .calendars()
            if calendar.isInbox():
                continue
            # Ignore trash
            if calendar.isTrash():
                continue
            split_count = yield calendar.splitCollectionByComponentTypes()
            self.log.warn("  Calendar: '{name}', split into {count}", name=calendar.name(), count=split_count + 1)

        yield self.ensureDefaultCalendarsExist()

    @inlineCallbacks
    def ensureDefaultCalendarsExist(self):
        """
        Double check that we have calendars supporting at least VEVENT and VTODO,
        and create if missing.
        """

        # Double check that we have calendars supporting at least VEVENT and VTODO
        if config.RestrictCalendarsToOneComponentType:
            supported_components = set()
            names = set()
            calendars = yield self.calendars()
            for calendar in calendars:
                if calendar.isInbox():
                    continue
                if calendar.isTrash():
                    continue
                names.add(calendar.name())
                result = yield calendar.getSupportedComponents()
                supported_components.update(result.split(","))

            @inlineCallbacks
            def _requireCalendarWithType(support_component, tryname):
                if support_component not in supported_components:
                    newname = tryname
                    if newname in names:
                        newname = str(uuid.uuid4())
                    newcal = yield self.createCalendarWithName(newname)
                    yield newcal.setSupportedComponents(support_component)

            for name in ical.allowedStoreComponents:
                yield _requireCalendarWithType(name, self._componentCalendarName[name])

    @inlineCallbacks
    def setDefaultCalendar(self, calendar, componentType):
        """
        Set the default calendar for a particular type of component.

        @param calendar: the calendar being set as the default
        @type calendar: L{CalendarObject}
        @param tasks: C{True} for VTODO, C{False} for VEVENT
        @type componentType: C{bool}
        """

        # We only support VEVENT and VTOTO right now
        componentType = componentType.upper()
        if componentType not in self._componentDefaultAttribute:
            returnValue(None)

        chm = self._homeMetaDataSchema
        attribute_to_test = self._componentDefaultAttribute[componentType]
        column_to_set = self._componentDefaultColumn[componentType]

        # Check validity of the default
        if calendar.isInbox():
            raise InvalidDefaultCalendar("Cannot set inbox as a default calendar")
        elif calendar.isTrash():
            raise InvalidDefaultCalendar("Cannot set trash as a default calendar")
        elif not calendar.owned():
            raise InvalidDefaultCalendar("Cannot set shared calendar as a default calendar")
        elif not calendar.isSupportedComponent(componentType):
            raise InvalidDefaultCalendar("Cannot set default calendar to unsupported component type")
        elif calendar.ownerHome().uid() != self.uid():
            raise InvalidDefaultCalendar("Cannot set default calendar to someone else's calendar")

        setattr(self, attribute_to_test, calendar._resourceID)
        yield Update(
            {column_to_set: calendar._resourceID},
            Where=chm.RESOURCE_ID == self._resourceID,
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyChanged()

        # CalDAV stores the default calendar properties on the inbox so we also need to send a changed notification on that
        inbox = (yield self.calendarWithName("inbox"))
        if inbox is not None:
            yield inbox.notifyPropertyChanged()

    @inlineCallbacks
    def defaultCalendar(self, componentType, create=True):
        """
        Find the default calendar for the supplied iCalendar component type. If one does
        not exist, automatically provision it.

        @param componentType: the name of the iCalendar component for the default, i.e. "VEVENT" or "VTODO"
        @type componentType: C{str}
        @param create: if C{True} and no default is found, create a calendar and make it the default, if C{False}
            and there is no default, then return C{None}
        @type create: C{bool}

        @return: the default calendar or C{None} if none found and creation not requested
        @rtype: L{Calendar} or C{None}
        """

        # We only support VEVENT and VTOTO right now
        componentType = componentType.upper()
        if componentType not in self._componentDefaultAttribute:
            returnValue(None)

        # Check any default calendar property first - this will create if none exists
        attribute_to_test = self._componentDefaultAttribute[componentType]
        defaultID = getattr(self, attribute_to_test)
        if defaultID:
            default = (yield self.childWithID(defaultID))
        else:
            default = None

        # Check that default meets requirements
        if default is not None:
            if default.isInbox():
                default = None
            elif default.isTrash():
                default = None
            elif not default.owned():
                default = None
            elif not default.isSupportedComponent(componentType):
                default = None

        # Must have a default - provision one if not
        if default is None:

            # Try to find a calendar supporting the required component type. If there are multiple, pick
            # the one with the oldest created timestamp as that will likely be the initial provision.
            existing_names = (yield self.listCalendars())
            for calendarName in existing_names:
                calendar = (yield self.calendarWithName(calendarName))
                if calendar.isInbox():
                    continue
                elif calendar.isTrash():
                    continue
                elif not calendar.owned():
                    continue
                elif not calendar.isSupportedComponent(componentType):
                    continue
                if default is None or calendar.created() < default.created():
                    default = calendar

            # If none can be found, provision one
            if default is None:
                if not create:
                    returnValue(None)
                else:
                    # Try a default name mapping first, else use a UUID
                    new_name = self._componentCalendarName[componentType]
                    if new_name in existing_names:
                        new_name = str(uuid.uuid4())
                    default = yield self.createCalendarWithName(new_name)
                    yield default.setSupportedComponents(componentType)

            # Update the metadata
            yield self.setDefaultCalendar(default, componentType)

        returnValue(default)

    def isDefaultCalendar(self, calendar):
        """
        Is the supplied calendar one of the possible default calendars.
        """
        # Not allowed to delete the default calendar
        for attr in self._componentDefaultAttribute.values():
            if calendar._resourceID == getattr(self, attr):
                return True
        return False

    ALARM_DETAILS = {
        (True, True): (_homeMetaDataSchema.ALARM_VEVENT_TIMED, "_alarm_vevent_timed"),
        (True, False): (_homeMetaDataSchema.ALARM_VEVENT_ALLDAY, "_alarm_vevent_allday"),
        (False, True): (_homeMetaDataSchema.ALARM_VTODO_TIMED, "_alarm_vtodo_timed"),
        (False, False): (_homeMetaDataSchema.ALARM_VTODO_ALLDAY, "_alarm_vtodo_allday"),
    }

    def getDefaultAlarm(self, vevent, timed):
        """
        Return the default alarm (text) for the specified alarm type.

        @param vevent: used for a vevent (C{True}) or vtodo (C{False})
        @type vevent: C{bool}
        @param timed: timed ({C{True}) or all-day ({C{False})
        @type timed: C{bool}

        @return: the alarm (text)
        @rtype: C{str}
        """

        return getattr(self, self.ALARM_DETAILS[(vevent, timed)][1])

    @inlineCallbacks
    def setDefaultAlarm(self, alarm, vevent, timed):
        """
        Set default alarm of the specified type.

        @param alarm: the alarm text
        @type alarm: C{str}
        @param vevent: used for a vevent (C{True}) or vtodo (C{False})
        @type vevent: C{bool}
        @param timed: timed ({C{True}) or all-day ({C{False})
        @type timed: C{bool}
        """

        colname, attr_alarm = self.ALARM_DETAILS[(vevent, timed)]

        setattr(self, attr_alarm, alarm)

        chm = self._homeMetaDataSchema
        yield Update(
            {colname: alarm},
            Where=chm.RESOURCE_ID == self._resourceID,
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyChanged()

    def getAvailability(self):
        """
        Return the VAVAILABILITY data.

        @return: the component (text)
        @rtype: L{Component}
        """

        return Component.fromString(self._availability) if self._availability else None

    @inlineCallbacks
    def setAvailability(self, availability):
        """
        Set VAVAILABILITY data.

        @param availability: the component
        @type availability: L{Component}
        """

        self._availability = str(availability) if availability else None

        chm = self._homeMetaDataSchema
        yield Update(
            {chm.AVAILABILITY: self._availability},
            Where=chm.RESOURCE_ID == self._resourceID,
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyChanged()

        # CalDAV stores the availability properties on the inbox so we also need to send a changed notification on that
        inbox = (yield self.calendarWithName("inbox"))
        if inbox is not None:
            yield inbox.notifyPropertyChanged()

    def iMIPTokens(self):
        return iMIPTokenRecord.query(self._txn, iMIPTokenRecord.organizer == "urn:x-uid:{}".format(self.uid()))

    def pauseWork(self):
        """
        Mark all associated work items as paused. This applies to L{ScheduleOrganizerWork},
        L{ScheduleOrganizerSendWork}, L{ScheduleReplyWork}, L{ScheduleRefreshWork}, L{ScheduleAutoReplyWork}
        """
        return self._pauseWork(1)

    def unpauseWork(self):
        """
        Mark all associated work items as unpaused. This applies to L{ScheduleOrganizerWork},
        L{ScheduleOrganizerSendWork}, L{ScheduleReplyWork}, L{ScheduleRefreshWork}, L{ScheduleAutoReplyWork}
        """
        return self._pauseWork(0)

    @inlineCallbacks
    def _pauseWork(self, pause):
        """
        Mark all associated work items as either paused or unpaused. This applies to L{ScheduleOrganizerWork},
        L{ScheduleOrganizerSendWork}, L{ScheduleReplyWork}, L{ScheduleRefreshWork}, L{ScheduleAutoReplyWork}
        """

        for workType in allScheduleWork:
            yield JobItem.updatesome(
                self._txn,
                where=JobItem.jobID.In(
                    ScheduleWork.jobIDsQueryJoin(self.id(), workType)
                ),
                pause=pause,
            )

    @inlineCallbacks
    def workItems(self):
        """
        Get all the associated scheduling work items and serialize them based on their class. Note this method is directly used
        by the cross-pod conduit and expects the results to be JSON compatible.
        """

        results = collections.defaultdict(list)
        for workType in allScheduleWork:
            workItems = yield workType.query(self._txn, workType.homeResourceID == self.id())
            for item in workItems:
                serialized = yield item.serializeWithAncillaryData()
                results[workType.__name__].append(serialized)

        returnValue(results)


CalendarHome._register(ECALENDARTYPE)


class Calendar(CommonHomeChild):
    """
    SQL-based implementation of L{ICalendar}.
    """
    implements(ICalendar)

    # structured tables.  (new, preferred)
    _homeSchema = schema.CALENDAR_HOME
    _bindSchema = schema.CALENDAR_BIND
    _homeChildSchema = schema.CALENDAR
    _homeChildMetaDataSchema = schema.CALENDAR_METADATA
    _revisionsSchema = schema.CALENDAR_OBJECT_REVISIONS
    _objectSchema = schema.CALENDAR_OBJECT
    _timeRangeSchema = schema.TIME_RANGE

    _homeRecordClass = CalendarHomeRecord
    _metadataRecordClass = CalendarMetaDataRecord
    _bindRecordClass = CalendarBindRecord
    _bindHomeIDAttributeName = "calendarHomeResourceID"
    _bindResourceIDAttributeName = "calendarResourceID"

    # Mapping of iCalendar property name to DB column name
    _queryFields = {
        "UID": _objectSchema.UID,
        "TYPE": _objectSchema.ICALENDAR_TYPE,
    }

    _supportedComponents = None

    _shadowProperties = tuple([PropertyName.fromString(prop) for prop in config.Sharing.Calendars.CollectionProperties.Shadowable])
    _proxyProperties = tuple([PropertyName.fromString(prop) for prop in config.Sharing.Calendars.CollectionProperties.ProxyOverride])
    _globalProperties = tuple([PropertyName.fromString(prop) for prop in config.Sharing.Calendars.CollectionProperties.Global])

    def __init__(self, *args, **kw):
        """
        Initialize a calendar pointing at a record in a database.
        """
        super(Calendar, self).__init__(*args, **kw)
        self._transp = _TRANSP_OPAQUE

    @classmethod
    def makeClass(cls, home, bindData, additionalBindData, metadataData, propstore=None, ownerHome=None):
        """
        Examine the calendar metadata to see which flavor of Calendar collection
        to create, then call the inherited makeClass with the right class.

        @param home: the parent home object
        @type home: L{CommonHome}
        @param bindData: the standard set of bind columns
        @type bindData: C{list}
        @param additionalBindData: additional bind data specific to sub-classes
        @type additionalBindData: C{list}
        @param metadataData: metadata data
        @type metadataData: C{list}
        @param propstore: a property store to use, or C{None} to load it automatically
        @type propstore: L{PropertyStore}
        @param ownerHome: the home of the owner, or C{None} to figure it out automatically
        @type ownerHome: L{CommonHome}

        @return: the constructed child class
        @rtype: L{CommonHomeChild}
        """

        if metadataData:
            childType = metadataData[cls.metadataColumns().index(cls._homeChildMetaDataSchema.CHILD_TYPE)]
            if childType == _CHILD_TYPE_TRASH:
                actualClass = TrashCollection
            else:
                actualClass = cls

            return super(Calendar, actualClass).makeClass(
                home, bindData, additionalBindData, metadataData,
                propstore=propstore, ownerHome=ownerHome
            )

    @classmethod
    def metadataColumns(cls):
        """
        Return a list of column name for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            cls._homeChildMetaDataSchema.SUPPORTED_COMPONENTS,
            cls._homeChildMetaDataSchema.CHILD_TYPE,
            cls._homeChildMetaDataSchema.TRASHED,
            cls._homeChildMetaDataSchema.IS_IN_TRASH,
            cls._homeChildMetaDataSchema.CREATED,
            cls._homeChildMetaDataSchema.MODIFIED,
        )

    @classmethod
    def metadataAttributes(cls):
        """
        Return a list of attribute names for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            "_supportedComponents",
            "_childType",
            "_trashed",
            "_isInTrash",
            "_created",
            "_modified",
        )

    @classmethod
    def additionalBindColumns(cls):
        """
        Return a list of column names for retrieval during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return (
            cls._bindSchema.TRANSP,
            cls._bindSchema.ALARM_VEVENT_TIMED,
            cls._bindSchema.ALARM_VEVENT_ALLDAY,
            cls._bindSchema.ALARM_VTODO_TIMED,
            cls._bindSchema.ALARM_VTODO_ALLDAY,
            cls._bindSchema.TIMEZONE,
        )

    @classmethod
    def additionalBindAttributes(cls):
        """
        Return a list of attribute names for retrieval of during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return (
            "_transp",
            "_alarm_vevent_timed",
            "_alarm_vevent_allday",
            "_alarm_vtodo_timed",
            "_alarm_vtodo_allday",
            "_timezone",
        )

    @property
    def _calendarHome(self):
        return self._home

    def serialize(self):
        """
        Override to map one column specific to this class.
        """
        data = super(Calendar, self).serialize()
        if data["metadataData"]["trashed"]:
            data["metadataData"]["trashed"] = data["metadataData"]["trashed"].isoformat(" ")
        return data

    @inlineCallbacks
    def copyMetadata(self, other):
        """
        Copy metadata from one L{Calendar} to another. This is only
        used during a migration step.
        """

        # Copy over list of attributes and the name
        self._name = other._name
        for attr in itertools.chain(self.metadataAttributes(), self.additionalBindAttributes()):
            if attr in ("_created", "_modified"):
                continue
            if hasattr(other, attr):
                setattr(self, attr, getattr(other, attr))

        # Update the metadata table
        cm = self._homeChildMetaDataSchema
        values = {}
        for attr, column in itertools.izip(self.metadataAttributes(), self.metadataColumns()):
            if attr in ("_created", "_modified"):
                continue
            values[column] = getattr(self, attr)
        yield Update(
            values,
            Where=(cm.RESOURCE_ID == self._resourceID)
        ).on(self._txn)

        # Update the bind table
        cb = self._bindSchema
        values = {
            cb.RESOURCE_NAME: self._name
        }
        for attr, column in itertools.izip(self.additionalBindAttributes(), self.additionalBindColumns()):
            values[column] = getattr(self, attr)
        yield Update(
            values,
            Where=(cb.CALENDAR_HOME_RESOURCE_ID == self.viewerHome()._resourceID).And(cb.CALENDAR_RESOURCE_ID == self._resourceID)
        ).on(self._txn)

        # Since database entry may have changed we need to invalidate any cached queries
        yield self.invalidateQueryCache()

    ownerCalendarHome = CommonHomeChild.ownerHome
    viewerCalendarHome = CommonHomeChild.viewerHome
    calendarObjects = CommonHomeChild.objectResources
    listCalendarObjects = CommonHomeChild.listObjectResources
    calendarObjectWithName = CommonHomeChild.objectResourceWithName
    calendarObjectWithUID = CommonHomeChild.objectResourceWithUID
    createCalendarObjectWithName = CommonHomeChild.createObjectResourceWithName
    calendarObjectsSinceToken = CommonHomeChild.objectResourcesSinceToken

    @inlineCallbacks
    def _createCalendarObjectWithNameInternal(self, name, component, internal_state, options=None, split_details=None):

        # Create => a new resource name
        if name in self._objects and self._objects[name]:
            raise ObjectResourceNameAlreadyExistsError()

        # Apply check to the size of the collection
        if config.MaxResourcesPerCollection:
            child_count = (yield self.countObjectResources())
            if child_count >= config.MaxResourcesPerCollection:
                raise TooManyObjectResourcesError()

        objectResource = (
            yield self._objectResourceClass._createInternal(self, name, component, internal_state, options, split_details)
        )
        self._objects[objectResource.name()] = objectResource
        self._objects[objectResource.uid()] = objectResource

        # Note: create triggers a notification when the component is set, so we
        # don't need to call notify() here like we do for object removal.
        returnValue(objectResource)

    @inlineCallbacks
    def removedObjectResource(self, child):
        yield super(Calendar, self).removedObjectResource(child)
        self.viewerHome().removedCalendarResource(child.uid())

    @inlineCallbacks
    def moveObjectResourceHere(self, name, component):
        """
        Create a new child in this collection as part of a move operation. This needs to be split out because
        behavior differs for sub-classes and cross-pod operations.

        @param name: new name to use in new parent
        @type name: C{str} or C{None} for existing name
        @param component: data for new resource
        @type component: L{Component}
        """

        # Cross-pod calls come in with component as str or unicode
        if isinstance(component, str) or isinstance(component, unicode):
            try:
                component = self._objectResourceClass._componentClass.fromString(component)
            except InvalidICalendarDataError as e:
                raise InvalidComponentForStoreError(str(e))

        yield self._createCalendarObjectWithNameInternal(name, component, internal_state=ComponentUpdateState.RAW)

    @inlineCallbacks
    def moveObjectResourceAway(self, rid, child=None):
        """
        Remove the child as the result of a move operation. This needs to be split out because
        behavior differs for sub-classes and cross-pod operations.

        @param rid: the child resource-id to move
        @type rid: C{int}
        @param child: the child resource to move - might be C{None} for cross-pod
        @type child: L{CommonObjectResource}
        """

        if child is None:
            child = yield self.objectResourceWithID(rid)
        yield child._removeInternal(internal_state=ComponentRemoveState.INTERNAL, useTrash=False)

    def calendarObjectsInTimeRange(self, start, end, timeZone):
        raise NotImplementedError()

    def objectResourcesHaveProperties(self):
        """
        inbox resources need to store Originator, Recipient etc properties.
        Other calendars do not have object resources with properties.
        """
        return self.isInbox()

    def isSupportedComponent(self, componentType):
        if self._supportedComponents:
            return componentType.upper() in self._supportedComponents.split(",")
        else:
            return True

    def getSupportedComponents(self):
        return self._supportedComponents

    @inlineCallbacks
    def setSupportedComponents(self, supported_components):
        """
        Update the database column with the supported components. Technically this should only happen once
        on collection creation, but for migration we may need to change after the fact - hence a separate api.
        """

        cal = self._homeChildMetaDataSchema
        yield Update(
            {
                cal.SUPPORTED_COMPONENTS: supported_components
            },
            Where=(cal.RESOURCE_ID == self._resourceID)
        ).on(self._txn)
        self._supportedComponents = supported_components
        yield self.invalidateQueryCache()
        yield self.notifyPropertyChanged()

    def getTimezone(self):
        """
        Return the VTIMEZONE data. L{self._timezone} will either be a full iCalendar component
        (BEGIN:VCALENDAR ... END:VCALENDAR) or just the timezone id.

        @return: the component
        @rtype: L{Component}
        """

        if self._timezone:
            if self._timezone.startswith("BEGIN:VCALENDAR"):
                return Component.fromString(self._timezone)
            else:
                try:
                    return Component(None, pycalendar=readVTZ(self._timezone))
                except TimezoneException:
                    return None
        else:
            return None

    def getTimezoneID(self):
        """
        Return the VTIMEZONE TZID value. L{self._timezone} will either be a full iCalendar component
        (BEGIN:VCALENDAR ... END:VCALENDAR) or just the timezone id.

        @return: the timezone id
        @rtype: L{str}
        """

        if self._timezone:
            if self._timezone.startswith("BEGIN:VCALENDAR"):
                tz = Component.fromString(self._timezone)
                return list(tz.timezones())[0]
            else:
                return self._timezone
        else:
            return None

    def setTimezone(self, timezone):
        """
        Set VTIMEZONE data. If the TZID is in our database, then just store the TZID value, otherwise store the
        entire iCalendar object as text.

        @param timezone: the component
        @type timezone: L{Component}
        """

        if timezone is not None:
            try:
                tzid = list(timezone.timezones())[0]
                if hasTZ(tzid):
                    self._timezone = tzid
            except TimezoneException:
                self._timezone = str(timezone)
        else:
            self._timezone = None
        return self._setTimezoneValue()

    def setTimezoneID(self, tzid):
        """
        Set VTIMEZONE via a TZID value. Make sure the TZID is in our TZ database.

        @param tzid: the component
        @type tzid: L{Component}

        @raise L{TimezoneException} if tzid is not in our database
        """

        if tzid is not None:
            hasTZ(tzid)
            self._timezone = tzid
        else:
            self._timezone = None
        return self._setTimezoneValue()

    @inlineCallbacks
    def _setTimezoneValue(self):
        """
        Store the current L{self._timezone} value in the database.
        """
        cal = self._bindSchema
        yield Update(
            {
                cal.TIMEZONE: self._timezone
            },
            Where=(cal.CALENDAR_HOME_RESOURCE_ID == self.viewerHome()._resourceID).And(cal.CALENDAR_RESOURCE_ID == self._resourceID)
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyPropertyChanged()

    ALARM_DETAILS = {
        (True, True): (_bindSchema.ALARM_VEVENT_TIMED, "_alarm_vevent_timed"),
        (True, False): (_bindSchema.ALARM_VEVENT_ALLDAY, "_alarm_vevent_allday"),
        (False, True): (_bindSchema.ALARM_VTODO_TIMED, "_alarm_vtodo_timed"),
        (False, False): (_bindSchema.ALARM_VTODO_ALLDAY, "_alarm_vtodo_allday"),
    }

    def getDefaultAlarm(self, vevent, timed):
        """
        Return the default alarm (text) for the specified alarm type.

        @param vevent: used for a vevent (C{True}) or vtodo (C{False})
        @type vevent: C{bool}
        @param timed: timed ({C{True}) or all-day ({C{False})
        @type timed: C{bool}
        @return: the alarm (text)
        @rtype: C{str}
        """

        return getattr(self, self.ALARM_DETAILS[(vevent, timed)][1])

    @inlineCallbacks
    def setDefaultAlarm(self, alarm, vevent, timed):
        """
        Set default alarm of the specified type.

        @param alarm: the alarm text
        @type alarm: C{str}
        @param vevent: used for a vevent (C{True}) or vtodo (C{False})
        @type vevent: C{bool}
        @param timed: timed ({C{True}) or all-day ({C{False})
        @type timed: C{bool}
        """

        colname, attr_alarm = self.ALARM_DETAILS[(vevent, timed)]

        setattr(self, attr_alarm, alarm)

        cal = self._bindSchema
        yield Update(
            {colname: alarm},
            Where=(cal.CALENDAR_HOME_RESOURCE_ID == self.viewerHome()._resourceID).And(cal.CALENDAR_RESOURCE_ID == self._resourceID)
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyPropertyChanged()

    def isInbox(self):
        """
        Indicates whether this calendar is an "inbox".

        @return: C{True} if it is an "inbox, C{False} otherwise
        @rtype: C{bool}
        """
        return self.name() == "inbox"

    def isUsedForFreeBusy(self):
        """
        Indicates whether the contents of this calendar contributes to free busy. Always coerce
        inbox to be transparent. Also ignore VTODO only calendars.

        @return: C{True} if it does, C{False} otherwise
        @rtype: C{bool}
        """
        supported = not self._supportedComponents or self._supportedComponents.split(",") != ["VTODO", ]
        return (self._transp == _TRANSP_OPAQUE) and not self.isInbox() and not self.isTrash() and supported

    @inlineCallbacks
    def setUsedForFreeBusy(self, use_it):
        """
        Mark this calendar as being used for free busy request. Note this is a per-user setting so we are setting
        this on the bind table entry which is related to the user viewing the calendar. Always coerce inbox to be
        transparent.

        @param use_it: C{True} if used for free busy, C{False} otherwise
        @type use_it: C{bool}
        """
        self._transp = _TRANSP_OPAQUE if use_it and (not self.isInbox() and not self.isTrash()) else _TRANSP_TRANSPARENT
        cal = self._bindSchema
        yield Update(
            {cal.TRANSP: self._transp},
            Where=(cal.CALENDAR_HOME_RESOURCE_ID == self.viewerHome()._resourceID).And(cal.CALENDAR_RESOURCE_ID == self._resourceID)
        ).on(self._txn)
        yield self.invalidateQueryCache()
        yield self.notifyPropertyChanged()

    def initPropertyStore(self, props):
        # Setup peruser special properties
        props.setSpecialProperties(
            self._shadowProperties,
            self._globalProperties,
            self._proxyProperties,
        )

    def sharedResourceType(self):
        """
        The sharing resource type
        """
        return "calendar"

    @inlineCallbacks
    def newShare(self, displayname=None):
        """
        Override in derived classes to do any specific operations needed when a share
        is first accepted.
        """

        # For a direct share we will copy any displayname and calendar-color over using the owners view
        if self.direct():
            ownerView = yield self.ownerView()
            try:
                displayname = ownerView.properties()[PropertyName.fromElement(element.DisplayName)]
            except KeyError:
                # displayname is not set, so use the owner's fullname
                record = yield ownerView.ownerHome().directoryRecord()
                if record is not None:
                    displayname = element.DisplayName.fromString(record.fullNames[0].encode("utf-8"))

            if displayname is not None:
                try:
                    self.properties()[PropertyName.fromElement(element.DisplayName)] = displayname
                except KeyError:
                    pass

            try:
                color = ownerView.properties()[PropertyName.fromElement(customxml.CalendarColor)]
                self.properties()[PropertyName.fromElement(customxml.CalendarColor)] = color
            except KeyError:
                pass

        elif displayname:
            self.properties()[PropertyName.fromElement(element.DisplayName)] = element.DisplayName.fromString(displayname)

        # Calendars always start out transparent and with empty default alarms
        yield self.setUsedForFreeBusy(False)
        yield self.setDefaultAlarm("empty", True, True)
        yield self.setDefaultAlarm("empty", True, False)
        yield self.setDefaultAlarm("empty", False, True)
        yield self.setDefaultAlarm("empty", False, False)

    def getInviteCopyProperties(self):
        """
        Get a dictionary of property name/values (as strings) for properties that are shadowable and
        need to be copied to a sharee's collection when an external (cross-pod) share is created.
        Sub-classes should override to expose the properties they care about.
        """
        props = {}
        for ename in (PropertyName.fromElement(element.DisplayName),) + self._shadowProperties:
            if ename in self.properties():
                props[ename.toString()] = self.properties()[ename].toxml()
        return props

    def setInviteCopyProperties(self, props):
        """
        Copy a set of shadowable properties (as name/value strings) onto this shared resource when
        a cross-pod invite is processed. Sub-classes should override to expose the properties they
        care about.
        """
        # Initialize these for all shares
        for ename in self._shadowProperties:
            if ename not in self.properties() and ename.toString() in props:
                self.properties()[ename] = WebDAVDocument.fromString(props[ename.toString()]).root_element

        # Only initialize these for direct shares
        if self.direct():
            for ename in (PropertyName.fromElement(element.DisplayName),):
                if ename not in self.properties() and ename.toString() in props:
                    self.properties()[ename] = WebDAVDocument.fromString(props[ename.toString()]).root_element

    # FIXME: this is DAV-ish.  Data store calendar objects don't have
    # mime types.  -wsv
    def contentType(self):
        """
        The content type of Calendar objects is text/calendar.
        """
        return MimeType.fromString("text/calendar; charset=utf-8")

    @inlineCallbacks
    def search(self, filter, useruid=None, fbtype=False):
        """
        Finds resources matching the given qualifiers.
        @param filter: the L{Filter} for the calendar-query to execute.
        @return: an iterable of tuples for each resource matching the
            given C{qualifiers}. The tuples are C{(name, uid)}, where
            C{name} is the resource name, C{uid} is the resource UID.
        """

        # We might be passed an L{Filter} or a serialization of one
        if isinstance(filter, dict):
            try:
                filter = Filter.deserialize(filter)
            except Exception:
                filter = None

        # Make sure we have a proper Filter element and get the partial SQL statement to use.
        sql_stmt = self._sqlquery(filter, useruid, fbtype)

        # No result means it is too complex for us
        if sql_stmt is None:
            raise IndexedSearchException()
        sql_stmt, args, usedtimerange = sql_stmt

        # Check for time-range re-expand
        if usedtimerange is not None:

            today = DateTime.getToday()

            # Determine how far we need to extend the current expansion of
            # events. If we have an open-ended time-range we will expand
            # one year past the start. That should catch bounded
            # recurrences - unbounded will have been indexed with an
            # "infinite" value always included.
            maxDate, isStartDate = filter.getmaxtimerange()
            if maxDate:
                maxDate = maxDate.duplicate()
                maxDate.offsetDay(1)
                maxDate.setDateOnly(True)
                upperLimit = today + Duration(days=config.FreeBusyIndexExpandMaxDays)
                if maxDate > upperLimit:
                    raise TimeRangeUpperLimit(upperLimit)
                if isStartDate:
                    maxDate += Duration(days=365)

            # Determine if the start date is too early for the restricted range we
            # are applying. If it is today or later we don't need to worry about truncation
            # in the past.
            minDate, _ignore_isEndDate = filter.getmintimerange()
            if minDate >= today:
                minDate = None
            if minDate is not None and config.FreeBusyIndexLowerLimitDays:
                truncateLowerLimit = today - Duration(days=config.FreeBusyIndexLowerLimitDays)
                if minDate < truncateLowerLimit:
                    raise TimeRangeLowerLimit(truncateLowerLimit)

            if maxDate is not None or minDate is not None:
                yield self.testAndUpdateIndex(minDate, maxDate)

        rowiter = yield sql_stmt.on(self._txn, **args)

        # Check result for missing resources
        results = []
        for row in rowiter:
            if fbtype:
                row = list(row)
                row[4] = 'Y' if row[4] else 'N'
                row[7] = indexfbtype_to_icalfbtype[row[7]]
                if row[9] is not None:
                    row[8] = row[9]
                row[8] = 'T' if row[8] else 'F'
                del row[9]
            results.append(row)

        returnValue(results)

    def _sqlquery(self, filter, useruid, fbtype):
        """
        Convert the supplied addressbook-query into a partial SQL statement.

        @param filter: the L{Filter} for the addressbook-query to convert.
        @return: a C{tuple} of (C{str}, C{list}), where the C{str} is the partial SQL statement,
                and the C{list} is the list of argument substitutions to use with the SQL API execute method.
                Or return C{None} if it is not possible to create an SQL query to fully match the addressbook-query.
        """

        if not isinstance(filter, Filter):
            return None

        try:
            expression = buildExpression(filter, self._queryFields)
            sql = CalDAVSQLQueryGenerator(expression, self, self.id(), useruid, fbtype)
            return sql.generate()
        except ValueError:
            return None

    @classproperty
    def _notExpandedWithinQuery(cls):  # @NoSelf
        """
        Query to find resources that need to be re-expanded
        """
        co = cls._objectSchema
        return Select(
            [co.RESOURCE_NAME],
            From=co,
            Where=(
                (co.RECURRANCE_MIN > Parameter("minDate"))
                .Or(co.RECURRANCE_MAX < Parameter("maxDate"))
            ).And(co.CALENDAR_RESOURCE_ID == Parameter("resourceID"))
        )

    @inlineCallbacks
    def notExpandedWithin(self, minDate, maxDate):
        """
        Gives all resources which have not been expanded beyond a given date
        in the database.  (Unused; see above L{postgresqlgenerator}.
        """
        returnValue([row[0] for row in (
            yield self._notExpandedWithinQuery.on(
                self._txn,
                minDate=pyCalendarToSQLTimestamp(normalizeForIndex(minDate)) if minDate is not None else None,
                maxDate=pyCalendarToSQLTimestamp(normalizeForIndex(maxDate)),
                resourceID=self._resourceID))]
        )

    @inlineCallbacks
    def reExpandResource(self, name, expand_start, expand_end):
        """
        Given a resource name, remove it from the database and re-add it
        with a longer expansion.
        """
        obj = yield self.calendarObjectWithName(name)

        # Use a new transaction to do this update quickly without locking the row for too long. However, the original
        # transaction may have the row locked, so use wait=False and if that fails, fall back to using the original txn.

        newTxn = obj.transaction().store().newTransaction(label="Calendar.reExpandResource")
        try:
            yield obj.lock(wait=False, txn=newTxn)
        except NoSuchObjectResourceError:
            yield newTxn.commit()
            returnValue(None)
        except:
            yield newTxn.abort()
            newTxn = None

        # Now do the re-expand using the appropriate transaction
        try:
            doExpand = False
            if newTxn is None:
                doExpand = True
            else:
                # We repeat this check because the resource may have been re-expanded by someone else
                rmin, rmax = (yield obj.recurrenceMinMax(txn=newTxn))

                # If the resource is not fully expanded, see if within the required range or not.
                # Note that expand_start could be None if no lower limit is applied, but expand_end will
                # never be None
                if rmax is not None and rmax < expand_end:
                    doExpand = True
                if rmin is not None and expand_start is not None and rmin > expand_start:
                    doExpand = True

            if doExpand:
                yield obj.updateDatabase(
                    (yield obj.component()),
                    expand_until=expand_end,
                    reCreate=True,
                    txn=newTxn,
                )
        finally:
            if newTxn is not None:
                yield newTxn.commit()

    @inlineCallbacks
    def testAndUpdateIndex(self, minDate, maxDate):
        # Find out if the index is expanded far enough
        names = yield self.notExpandedWithin(minDate, maxDate)

        # Actually expand recurrence max
        for name in names:
            self.log.info("Search falls outside range of index for {name} {min} to {max}", name=name, min=minDate, max=maxDate)
            yield self.reExpandResource(name, minDate, maxDate)

    @inlineCallbacks
    def splitCollectionByComponentTypes(self):
        """
        If the calendar contains iCalendar data with different component types, then split it into separate collections
        each containing only one component type. When doing this make sure properties and sharing state are preserved
        on any new calendars created. Also restrict the new calendars to only the one appropriate component type. Return
        the number of splits done.
        """

        # First see how many different component types there are
        split_count = 0
        components = yield self._countComponentTypes()
        if len(components) <= 1:

            # Restrict calendar to single component type
            component = components[0][0] if components else "VEVENT"
            yield self.setSupportedComponents(component.upper())

            returnValue(split_count)

        # We will leave the component type with the highest count in the current calendar and create new calendars
        # for the others which will be moved over
        maxComponent = max(components, key=lambda x: x[1])[0]

        for component, _ignore_count in components:
            if component == maxComponent:
                continue
            split_count += 1
            yield self._splitComponentType(component)

        # Restrict calendar to single component type
        yield self.setSupportedComponents(maxComponent.upper())

        returnValue(split_count)

    @inlineCallbacks
    def _countComponentTypes(self):
        """
        Count each component type in this calendar.

        @return: a C{tuple} of C{tuple} containing the component type name and count.
        """

        ob = self._objectSchema
        _componentsQuery = Select(
            [ob.ICALENDAR_TYPE, Count(ob.ICALENDAR_TYPE)],
            From=ob,
            Where=ob.CALENDAR_RESOURCE_ID == Parameter('calID'),
            GroupBy=ob.ICALENDAR_TYPE
        )

        rows = yield _componentsQuery.on(self._txn, calID=self._resourceID)
        result = tuple([(componentType, componentCount) for componentType, componentCount in sorted(rows, key=lambda x:x[0])])
        returnValue(result)

    @inlineCallbacks
    def _splitComponentType(self, component):
        """
        Create a new calendar and move all components of the specified component type into the new one.
        Make sure properties and sharing state is preserved on the new calendar.

        @param component: Component type to split out
        @type component: C{str}
        """

        # Create the new calendar
        try:
            newcalendar = yield self._home.createCalendarWithName("{0}-{1}".format(self._name, component.lower(),))
        except HomeChildNameAlreadyExistsError:
            # If the name we want exists, try repeating with up to ten more
            for ctr in range(10):
                try:
                    newcalendar = yield self._home.createCalendarWithName("{0}-{1}-[2}".format(self._name, component.lower(), ctr + 1,))
                except HomeChildNameAlreadyExistsError:
                    continue
            else:
                # At this point we are stuck
                raise HomeChildNameNotAllowedError

        # Restrict calendar to single component type
        yield newcalendar.setSupportedComponents(component.upper())

        # Transfer properties over
        yield newcalendar._properties.copyAllProperties(self._properties)

        # Transfer sharing
        yield self._transferSharingDetails(newcalendar, component)

        # Now move calendar data over
        yield self._transferCalendarObjects(newcalendar, component)

    @inlineCallbacks
    def _transferSharingDetails(self, newcalendar, component):
        """
        If the current calendar is shared, make the new calendar shared in the same way, but tweak the name.
        """

        cb = self._bindSchema
        columns = [ColumnSyntax(item) for item in self._bindSchema.model.columns]
        _bindQuery = Select(
            columns,
            From=cb,
            Where=(cb.CALENDAR_RESOURCE_ID == Parameter('calID')).And(
                cb.CALENDAR_HOME_RESOURCE_ID != Parameter('homeID'))
        )

        rows = yield _bindQuery.on(
            self._txn,
            calID=self._resourceID,
            homeID=self._home._resourceID,
        )

        if len(rows) == 0:
            returnValue(None)

        for row in rows:
            columnMap = dict(zip(columns, row))
            columnMap[cb.CALENDAR_RESOURCE_ID] = newcalendar._resourceID
            columnMap[cb.CALENDAR_RESOURCE_NAME] = "{0}-{1}".format(columnMap[cb.CALENDAR_RESOURCE_NAME], component.lower(),)
            yield Insert(columnMap).on(self._txn)

    @inlineCallbacks
    def _transferCalendarObjects(self, newcalendar, component):
        """
        Move all calendar components of the specified type to the specified calendar.
        """

        # Find resource-ids for all matching components
        ob = self._objectSchema
        _componentsQuery = Select(
            [ob.RESOURCE_ID],
            From=ob,
            Where=(ob.CALENDAR_RESOURCE_ID == Parameter('calID')).And(
                ob.ICALENDAR_TYPE == Parameter('componentType'))
        )

        rows = yield _componentsQuery.on(
            self._txn,
            calID=self._resourceID,
            componentType=component,
        )

        if len(rows) == 0:
            returnValue(None)

        for row in rows:
            resourceID = row[0]
            child = yield self.objectResourceWithID(resourceID)
            yield self.moveObjectResource(child, newcalendar)

    @classproperty
    def _moveTimeRangeUpdateQuery(cls):  # @NoSelf
        """
        DAL query to update a child to be in a new parent.
        """
        tr = cls._timeRangeSchema
        return Update(
            {tr.CALENDAR_RESOURCE_ID: Parameter("newParentID")},
            Where=tr.CALENDAR_OBJECT_RESOURCE_ID == Parameter("resourceID")
        )

    @inlineCallbacks
    def _movedObjectResource(self, child, newparent):
        """
        Make sure time range entries have the new parent resource id.
        """
        yield self._moveTimeRangeUpdateQuery.on(
            self._txn,
            newParentID=newparent._resourceID,
            resourceID=child._resourceID
        )

    # ===============================================================================
    # Group sharing
    # ===============================================================================

    # We need to handle the case where a sharee is added directly or as a member of one
    # or more groups. In order to do that we distinguish those cases via the BIND_MODE:
    #
    # BIND_MODE_READ, BIND_MODE_WRITE - an individual sharee only (i.e., not a member of
    #    a group also being shared to).
    #
    # BIND_MODE_GROUP - a sharee added solely as a member of one or more groups (i.e., not
    #    an individual).
    #
    # BIND_MODE_GROUP_READ, BIND_MODE_GROUP_WRITE - a sharee added as both an individual
    #    and as a member of a group). The READ/WRITE state reflects the individual
    #    access granted).
    #
    # For the BIND_MODE_GROUP* variants the actual access level is determined by the
    #    max of all the group access levels and the individual access level. That
    #    that is determined by the L{effectiveShareMode} method.
    #
    # When adding/removing an individual or reconciling group membership changes, the
    #    actual BIND_MODE in the bind table is determined by one of the _groupModeAfter*
    #    methods.

    @inlineCallbacks
    def reconcileGroupSharee(self, groupUID):
        """
        Reconcile bind table with group members. Note that when this is called, changes have
        already been made to the group being refreshed. So members that have been removed
        won't appear as a member of the group but will still have an associated bind row.

        What we need to do is get the list of all existing group binds and diff that with the
        list of expected group binds from all groups shared to this calendar. This means we will
        end up also reconciling other shared groups that changed. There is no way to prevent that
        short of recording in the bind table, which groups caused a member to be bound.
        """
        changed = False

        # First check that the actual group membership has changed
        if (yield self.updateShareeGroupLink(groupUID)):

            # First find all the members of all the groups being shared to
            groupMembers = yield GroupMembershipRecord.query(
                self._txn,
                GroupMembershipRecord.groupID.In(
                    GroupShareeRecord.queryExpr(
                        expr=GroupShareeRecord.calendarID == self.id(),
                        attributes=[GroupShareeRecord.groupID, ]
                    )
                ),
                distinct=True,
            )
            memberUIDs = set([grp.memberUID for grp in groupMembers])

            # Find each currently bound group sharee UID along with their bind mode
            boundUIDs = set()
            home = self._homeSchema
            bind = self._bindSchema
            rows = yield Select(
                [home.OWNER_UID, bind.BIND_MODE],
                From=bind.join(home, on=bind.HOME_RESOURCE_ID == home.RESOURCE_ID),
                Where=(bind.CALENDAR_RESOURCE_ID == self._resourceID).And(
                    bind.BIND_MODE.In((_BIND_MODE_GROUP, _BIND_MODE_GROUP_READ, _BIND_MODE_GROUP_WRITE))
                )
            ).on(self._txn)
            for shareeHomeUID, shareeBindMode in rows:

                if shareeHomeUID in memberUIDs:
                    # Group sharee still referenced via a group - make a note of it
                    boundUIDs.add(shareeHomeUID)

                elif shareeBindMode == _BIND_MODE_GROUP:
                    # Group only sharee is no longer referenced by any group - uninvite them
                    yield self.uninviteUIDFromShare(shareeHomeUID)
                    changed = True

                else:
                    # Group+individual sharee is no longer referenced by a group so update the bind
                    # mode to reflect just the individual mode
                    yield super(Calendar, self).inviteUIDToShare(
                        shareeHomeUID,
                        {
                            _BIND_MODE_GROUP_READ: _BIND_MODE_READ,
                            _BIND_MODE_GROUP_WRITE: _BIND_MODE_WRITE,
                        }.get(shareeBindMode),
                    )

            for memberUID in memberUIDs - boundUIDs:
                # Never reconcile the sharer
                if memberUID != self._home.uid():
                    shareeView = yield self.shareeView(memberUID)
                    newMode = _BIND_MODE_GROUP if shareeView is None else shareeView._groupModeAfterAddingOneGroupSharee()
                    if newMode is not None:
                        yield super(Calendar, self).inviteUIDToShare(memberUID, newMode)
                        changed = True

        returnValue(changed)

    def _groupModeAfterAddingOneIndividualSharee(self, mode):
        """
        return bind mode after adding one individual sharee with mode
        """
        addToGroupModeMap = {
            _BIND_MODE_READ: _BIND_MODE_GROUP_READ,
            _BIND_MODE_WRITE: _BIND_MODE_GROUP_WRITE,
        }
        return {
            _BIND_MODE_GROUP_READ: addToGroupModeMap.get(mode),
            _BIND_MODE_GROUP_WRITE: addToGroupModeMap.get(mode),
            _BIND_MODE_GROUP: addToGroupModeMap.get(mode),
        }.get(self._bindMode)

    @inlineCallbacks
    def _groupModeAfterRemovingOneIndividualSharee(self):
        """
        return group mode after adding one group sharee or None
        """
        if self._bindMode == _BIND_MODE_DIRECT:
            gs = schema.GROUP_SHAREE
            gm = schema.GROUP_MEMBERSHIP
            rows = yield Select(
                [Count(gs.GROUP_ID)],
                From=gs,
                Where=(
                    gs.GROUP_ID.In(
                        Select(
                            [gm.GROUP_ID],
                            From=gm,
                            Where=(
                                gm.MEMBER_UID == Parameter("uid")
                            )
                        )
                    )
                )
            ).on(self._txn, uid=self.viewerHome().uid())
            groupMode = _BIND_MODE_GROUP if rows[0][0] > 0 else None
        else:
            groupMode = {
                _BIND_MODE_GROUP_READ: _BIND_MODE_GROUP,
                _BIND_MODE_GROUP_WRITE: _BIND_MODE_GROUP,
            }.get(self._bindMode)

        returnValue(groupMode)

    def _groupModeAfterAddingOneGroupSharee(self):
        """
        return group mode after adding one group sharee or None
        """
        return {
            _BIND_MODE_GROUP: _BIND_MODE_GROUP,
            _BIND_MODE_READ: _BIND_MODE_GROUP_READ,
            _BIND_MODE_GROUP_READ: _BIND_MODE_GROUP_READ,
            _BIND_MODE_WRITE: _BIND_MODE_GROUP_WRITE,
            _BIND_MODE_GROUP_WRITE: _BIND_MODE_GROUP_WRITE,
        }.get(self._bindMode)

    @inlineCallbacks
    def _groupModeAfterRemovingOneGroupSharee(self):
        """
        Return group mode after removing one group sharee or None.
        Note that the group sharee entry still exists in the GROUP_SHAREE table when
        this method is called.
        """
        # count group sharees that self is member of
        gs = schema.GROUP_SHAREE
        gm = schema.GROUP_MEMBERSHIP
        rows = yield Select(
            [Count(gs.GROUP_ID)],
            From=gs,
            Where=(gs.CALENDAR_ID == self._resourceID).And(
                gs.GROUP_ID.In(
                    Select(
                        [gm.GROUP_ID],
                        From=gm,
                        Where=(
                            gm.MEMBER_UID == Parameter("uid")
                        )
                    )
                )
            )
        ).on(self._txn, uid=self.viewerHome().uid())

        # Count greater than one means we will have at least one remaining group
        # sharee after the one being removed is gone.
        if rows[0][0] > 1:
            # no mode change for group shares
            result = {
                _BIND_MODE_GROUP: _BIND_MODE_GROUP,
                _BIND_MODE_GROUP_READ: _BIND_MODE_GROUP_READ,
                _BIND_MODE_GROUP_WRITE: _BIND_MODE_GROUP_WRITE,
            }.get(self._bindMode)

        else:
            # return mode without any groups
            result = {
                _BIND_MODE_GROUP_READ: _BIND_MODE_READ,
                _BIND_MODE_GROUP_WRITE: _BIND_MODE_WRITE,
            }.get(self._bindMode)

        returnValue(result)

    @inlineCallbacks
    def updateShareeGroupLink(self, groupUID, mode=None):
        """
        update schema.GROUP_SHAREE
        """
        changed = False
        group = yield self._txn.groupByUID(groupUID)

        gs = schema.GROUP_SHAREE
        rows = yield Select(
            [gs.MEMBERSHIP_HASH, gs.GROUP_BIND_MODE],
            From=gs,
            Where=(gs.CALENDAR_ID == self._resourceID).And(
                gs.GROUP_ID == group.groupID)
        ).on(self._txn)
        if rows:
            [[gsMembershipHash, gsMode]] = rows
            updateMap = {}
            if gsMembershipHash != group.membershipHash:
                updateMap[gs.MEMBERSHIP_HASH] = group.membershipHash
            if mode is not None and gsMode != mode:
                updateMap[gs.GROUP_BIND_MODE] = mode
            if updateMap:
                yield Update(
                    updateMap,
                    Where=(gs.CALENDAR_ID == self._resourceID).And(
                        gs.GROUP_ID == group.groupID
                    )
                ).on(self._txn)
                changed = True
        else:
            yield Insert({
                gs.MEMBERSHIP_HASH: group.membershipHash,
                gs.GROUP_BIND_MODE: mode,
                gs.CALENDAR_ID: self._resourceID,
                gs.GROUP_ID: group.groupID,
            }).on(self._txn)
            changed = True

        returnValue(changed)

    @inlineCallbacks
    def _effectiveShareMode(self, bindMode, viewerUID, txn):
        """
        Get the effective share mode without a calendar object
        """
        if bindMode == _BIND_MODE_GROUP_WRITE:
            returnValue(_BIND_MODE_WRITE)
        elif bindMode in (_BIND_MODE_GROUP, _BIND_MODE_GROUP_READ):
            gs = schema.GROUP_SHAREE
            gm = schema.GROUP_MEMBERSHIP
            rows = yield Select(
                [Max(gs.GROUP_BIND_MODE)],  # _BIND_MODE_WRITE > _BIND_MODE_READ
                From=gs,
                Where=(gs.CALENDAR_ID == self._resourceID).And(
                    gs.GROUP_ID.In(
                        Select(
                            [gm.GROUP_ID],
                            From=gm,
                            Where=(
                                gm.MEMBER_UID == Parameter("uid")
                            )
                        )
                    )
                )
            ).on(txn, uid=viewerUID)

            # The result might be empty if the sharee has been removed from the group, but reconciliation has not
            # yet occurred, so cope with that by giving them the lowest mode since we don't know what the last valid
            # mode was.
            returnValue(rows[0][0] if rows[0][0] else _BIND_MODE_READ)
        else:
            returnValue(bindMode)

    def effectiveShareMode(self):
        """
        Get the high level sharemode for calendars shared to users or groups
        """
        return self._effectiveShareMode(self._bindMode, self.viewerHome().uid(), self._txn)

    #
    # Higher level API
    #
    @inlineCallbacks
    def inviteUIDToShare(self, shareeUID, mode, summary=None, shareName=None):
        """
        Invite a user to share this collection - either create the share if it does not exist, or
        update the existing share with new values. Make sure a notification is sent as well.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        @param mode: access mode
        @type mode: C{int}
        @param summary: share message
        @type summary: C{str}
        """

        # Go direct to individual sharing API if groups are disabled
        if not (
                config.Sharing.Enabled and
                config.Sharing.Calendars.Enabled and
                config.Sharing.Calendars.Groups.Enabled
        ):
            returnValue((yield super(Calendar, self).inviteUIDToShare(shareeUID, mode, summary, shareName)))

        record = yield self._txn.directoryService().recordWithUID(shareeUID.decode("utf-8"))
        if record is None:
            returnValue((yield super(Calendar, self).inviteUIDToShare(shareeUID, mode, summary, shareName)))

        if record.recordType != RecordType.group:
            shareeView = yield self.shareeView(shareeUID)
            groupMode = shareeView._groupModeAfterAddingOneIndividualSharee(mode) if shareeView else None
            mode = mode if groupMode is None else groupMode
            returnValue((yield super(Calendar, self).inviteUIDToShare(shareeUID, mode, summary, shareName)))

        yield self.updateShareeGroupLink(shareeUID, mode=mode)

        # invite every member of group
        shareeViews = []
        group = yield self._txn.groupByUID(shareeUID)
        memberUIDs = yield self._txn.groupMemberUIDs(group.groupID)
        for memberUID in memberUIDs:
            if memberUID != self._home.uid():
                shareeView = yield self.shareeView(memberUID)
                newMode = _BIND_MODE_GROUP if shareeView is None else shareeView._groupModeAfterAddingOneGroupSharee()
                if newMode is not None:
                    # everything but direct
                    shareeView = yield super(Calendar, self).inviteUIDToShare(memberUID, newMode, summary)
                    shareeViews.append(shareeView)

        # shared even if no group members
        yield self.setShared(True)

        returnValue(tuple(shareeViews))

    @inlineCallbacks
    def directShareWithUser(self, shareeUID, shareName=None, displayName=None):
        """
        Create a direct share with the specified user. Note it is currently up to the app layer
        to enforce access control - this is not ideal as we really should have control of that in
        the store. Once we do, this api will need to verify that access is allowed for a direct share.

        NB no invitations are used with direct sharing.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        """

        shareeView = yield self.shareeView(shareeUID)
        if shareeView is not None and shareeView._bindMode == _BIND_MODE_GROUP:
            yield self.updateShare(shareeView, mode=_BIND_MODE_DIRECT, status=_BIND_STATUS_ACCEPTED)
            returnValue(shareeView)
        else:
            returnValue((yield super(Calendar, self).directShareWithUser(shareeUID, shareName, displayName)))

    @inlineCallbacks
    def uninviteUIDFromShare(self, shareeUID):
        """
        Remove a user or group from a share. Make sure a notification is sent as well.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        """

        # Go direct to individual sharing API if groups are disabled
        if not (
                config.Sharing.Enabled and
                config.Sharing.Calendars.Enabled and
                config.Sharing.Calendars.Groups.Enabled
        ):
            returnValue((yield super(Calendar, self).uninviteUIDFromShare(shareeUID)))

        # Check if the sharee is a group
        gr = schema.GROUPS
        gs = schema.GROUP_SHAREE
        rows = yield Select(
            [gs.GROUP_ID],
            From=gs.join(gr, gs.GROUP_ID == gr.GROUP_ID),
            Where=gr.GROUP_UID == shareeUID
        ).on(self._txn)

        if rows:
            groupID = rows[0][0]
            reinvites = []

            # Uninvite each member of group
            memberUIDs = yield self._txn.groupMemberUIDs(groupID)
            for memberUID in memberUIDs:
                if memberUID != self._home.uid():
                    shareeView = yield self.shareeView(memberUID)
                    if shareeView is not None:
                        newMode = yield shareeView._groupModeAfterRemovingOneGroupSharee()
                        if newMode is None:
                            if shareeView._bindMode != _BIND_MODE_DIRECT:
                                # one group or individual share: delete share
                                yield super(Calendar, self).uninviteUIDFromShare(memberUID)
                        else:
                            # multiple groups or group and individual was shared add to reinvite list to update
                            reinvites.append((memberUID, newMode))

            # Delete before super.inviteUIDTOShare() and after super.inviteUIDToShare() so notification has correct access mode
            yield Delete(
                From=gs,
                Where=(gs.CALENDAR_ID == self._resourceID).And(
                    gs.GROUP_ID == groupID
                ),
            ).on(self._txn)

            for memberUID, newMode in reinvites:
                shareeView = yield self.shareeView(memberUID)
                yield super(Calendar, self).inviteUIDToShare(memberUID, newMode)

        else:
            shareeView = yield self.shareeView(shareeUID)
            if shareeView is not None:
                newMode = yield shareeView._groupModeAfterRemovingOneIndividualSharee()
                if newMode is None:
                    # no group was shared, so delete share
                    yield super(Calendar, self).uninviteUIDFromShare(shareeUID)
                else:
                    # multiple groups or group and individual was shared, update to new mode
                    yield super(Calendar, self).inviteUIDToShare(shareeUID, newMode)

    @inlineCallbacks
    def ownerDeleteShare(self):
        """
        This share is being deleted (by the owner) - we also need to clean up the group sharees.
        """

        yield super(Calendar, self).ownerDeleteShare()

        # Delete referenced group sharees. Note that whilst the table uses an on delete cascade,
        # we do need to remove the sharees for the case where the calendar is trashed and not
        # removed. Since the cascade is not triggered in that case and we have to do it by hand.
        gs = schema.GROUP_SHAREE
        yield Delete(
            From=gs,
            Where=(gs.CALENDAR_ID == self._resourceID),
        ).on(self._txn)

    @inlineCallbacks
    def allInvitations(self):
        """
        Get list of all invitations (non-direct) to this object.
        """
        user_invitations = yield super(Calendar, self).allInvitations()

        # Add groups (before the individuals)
        gs = schema.GROUP_SHAREE
        gr = schema.GROUPS
        rows = yield Select(
            [gr.GROUP_UID, gs.GROUP_BIND_MODE],
            From=gs.join(gr, gs.GROUP_ID == gr.GROUP_ID),
            Where=(gs.CALENDAR_ID == self._resourceID),
        ).on(self._txn)

        invitations = []
        for guid, gmode in rows:
            invite = SharingInvitation(
                guid,
                self.ownerHome().name(),
                self.ownerHome().id(),
                guid,
                0,
                _HOME_STATUS_NORMAL,
                gmode,
                _BIND_STATUS_ACCEPTED,
                "",
            )
            invitations.append(invite)

        invitations.extend(user_invitations)
        returnValue(invitations)

    @inlineCallbacks
    def groupSharees(self):
        sharees = yield GroupShareeRecord.querysimple(self._txn, calendarID=self.id())
        groups = set([sharee.groupID for sharee in sharees])
        groups = (yield GroupsRecord.query(self._txn, GroupsRecord.groupID.In(groups))) if groups else []
        returnValue({"groups": groups, "sharees": sharees})


icalfbtype_to_indexfbtype = {
    "UNKNOWN": 0,
    "FREE": 1,
    "BUSY": 2,
    "BUSY-UNAVAILABLE": 3,
    "BUSY-TENTATIVE": 4,
}

indexfbtype_to_icalfbtype = {
    0: '?',
    1: 'F',
    2: 'B',
    3: 'U',
    4: 'T',
}

accessMode_to_type = {
    "": 0,
    Component.ACCESS_PUBLIC: 1,
    Component.ACCESS_PRIVATE: 2,
    Component.ACCESS_CONFIDENTIAL: 3,
    Component.ACCESS_RESTRICTED: 4,
}
accesstype_to_accessMode = dict([(v, k) for k, v in accessMode_to_type.items()])


class CalendarObject(CommonObjectResource, CalendarObjectBase):
    implements(ICalendarObject)

    _objectSchema = schema.CALENDAR_OBJECT
    _componentClass = Component

    _currentDataVersion = 1

    def __init__(self, calendar, name, uid, resourceID=None, options=None):

        super(CalendarObject, self).__init__(calendar, name, uid, resourceID)

        if options is None:
            options = {}
        self.accessMode = options.get("accessMode", "")
        self.isScheduleObject = options.get("isScheduleObject", False)
        self.scheduleTag = options.get("scheduleTag", "")
        self.scheduleEtags = options.get("scheduleEtags", "")
        self.hasPrivateComment = options.get("hasPrivateComment", False)
        self._dropboxID = options.get("dropboxID", None)

        # Component caching
        self._cachedComponent = None
        self._cachedCommponentPerUser = {}

        self._lockedUID = False

    @classmethod
    @inlineCallbacks
    def _createInternal(cls, parent, name, component, internal_state, options=None, split_details=None):

        child = (yield cls.objectWithName(parent, name))
        if child:
            raise ObjectResourceNameAlreadyExistsError(name)

        if name.startswith("."):
            raise ObjectResourceNameNotAllowedError(name)

        c = cls._externalClass if parent.externalClass() else cls
        objectResource = c(parent, name, None, None, options=options)
        yield objectResource._setComponentInternal(component, inserting=True, internal_state=internal_state, split_details=split_details)
        yield objectResource._loadPropertyStore(created=True)

        # Note: setComponent triggers a notification, so we don't need to
        # call notify( ) here like we do for object removal.

        returnValue(objectResource)

    @classmethod
    def _allColumns(cls):  # @NoSelf
        """
        Full set of columns in the object table that need to be loaded to
        initialize the object resource state.
        """
        obj = cls._objectSchema
        return [
            obj.RESOURCE_ID,
            obj.RESOURCE_NAME,
            obj.UID,
            obj.MD5,
            Len(obj.TEXT),
            obj.ATTACHMENTS_MODE,
            obj.DROPBOX_ID,
            obj.ACCESS,
            obj.SCHEDULE_OBJECT,
            obj.SCHEDULE_TAG,
            obj.SCHEDULE_ETAGS,
            obj.PRIVATE_COMMENTS,
            obj.TRASHED,
            obj.ORIGINAL_COLLECTION,
            obj.CREATED,
            obj.MODIFIED,
            obj.DATAVERSION,
        ]

    @classmethod
    def _rowAttributes(cls):  # @NoSelf
        return (
            "_resourceID",
            "_name",
            "_uid",
            "_md5",
            "_size",
            "_attachment",
            "_dropboxID",
            "_access",
            "_schedule_object",
            "_schedule_tag",
            "_schedule_etags",
            "_private_comments",
            "_trashed",
            "_original_collection",
            "_created",
            "_modified",
            "_dataversion",
        )

    @property
    def _calendar(self):
        return self._parentCollection

    def calendar(self):
        return self._calendar

    # Stuff from put_common
    @inlineCallbacks
    def fullValidation(self, component, inserting, internal_state):
        """
        Do full validation of source and destination calendar data.
        """

        # Basic validation

        # Possible timezone stripping
        if config.EnableTimezonesByReference:
            changed = component.stripStandardTimezones()
            if changed:
                self._componentChanged = True

        # Do validation on external requests
        if internal_state == ComponentUpdateState.NORMAL:

            # Valid data sizes
            if config.MaxResourceSize:
                calsize = len(str(component))
                if calsize > config.MaxResourceSize:
                    raise ObjectResourceTooBigError()

        # Do validation on external requests
        if internal_state == ComponentUpdateState.NORMAL:

            # Valid calendar data checks
            self.validCalendarDataCheck(component, inserting)

            # Valid calendar component for check
            if not self.calendar().isSupportedComponent(component.mainType()):
                raise InvalidComponentTypeError("Invalid component type {0} for calendar: {1}".format(component.mainType(), self.calendar(),))

            # Normalize the calendar user addresses once we know we have valid
            # calendar data
            yield component.normalizeCalendarUserAddresses(normalizationLookup, self.directoryService().recordWithCalendarUserAddress)

            # Valid attendee list size check
            yield self.validAttendeeListSizeCheck(component, inserting)

        # Check location/resource organizer requirement
        yield self.validLocationResourceOrganizer(component, inserting, internal_state)

        # Check access
        if config.EnablePrivateEvents:
            self.validAccess(component, inserting, internal_state)

    @inlineCallbacks
    def reconcileGroupAttendees(self, component, inserting=False):
        """
        reconcile group attendees
        """
        if not config.GroupAttendees.Enabled:
            returnValue(False)

        # Note, as per ical.py/normalizeCalendarUserAddresses we change the CUTYPE from GROUP
        # to X-SERVER-GROUP to ensure bad clients don't spontaneously remove group attendees
        # when an event changes.
        attendeeProps = component.getAllAttendeeProperties()
        groupCUAs = set([
            attendeeProp.value() for attendeeProp in attendeeProps
            if attendeeProp.parameterValue("CUTYPE") == "X-SERVER-GROUP"
        ])

        # Map each group attendee to a list of potential member properties
        groupCUAToAttendeeMemberPropMap = {}
        for groupCUA in groupCUAs:

            groupCUAToAttendeeMemberPropMap[groupCUA] = ()
            groupRecord = yield self.directoryService().recordWithCalendarUserAddress(groupCUA)
            if groupRecord:
                # get members
                group = yield self._txn.groupByUID(groupRecord.uid)
                if group is not None:
                    members = yield self._txn.groupMembers(group.groupID)
                    groupCUAToAttendeeMemberPropMap[groupRecord.canonicalCalendarUserAddress()] = tuple(
                        [member.attendeeProperty(params={"MEMBER": groupCUA}) for member in sorted(members, key=lambda x: x.uid)]
                    )

        # sync group attendee members if inserting or group changed
        changed = False
        if inserting or (yield self.updateEventGroupLink(groupCUAToAttendeeMemberPropMap)):
            changed = component.reconcileGroupAttendees(groupCUAToAttendeeMemberPropMap)

        # save for post processing when self._resourceID is non-zero
        self._groupCUAToAttendeeMemberPropMap = groupCUAToAttendeeMemberPropMap

        returnValue(changed)

    @inlineCallbacks
    def groupEventLinks(self):
        """
        Return the current group event links for this resource.

        @return: a L{dict} with group ids as the key and membership hash as the value
        @rtype: L{dict}
        """
        records = yield GroupAttendeeRecord.querysimple(self._txn, resourceID=self._resourceID)
        returnValue(dict([(record.groupID, record,) for record in records]))

    @inlineCallbacks
    def updateEventGroupLink(self, groupCUAToAttendeeMemberPropMap=None):
        """
        update group event links
        """
        if groupCUAToAttendeeMemberPropMap is None:
            if hasattr(self, "_groupCUAToAttendeeMemberPropMap"):
                groupCUAToAttendeeMemberPropMap = self._groupCUAToAttendeeMemberPropMap
            else:
                returnValue(False)

        changed = False
        groupIDToMembershipHashMap = (yield self.groupEventLinks())

        for groupCUA in groupCUAToAttendeeMemberPropMap:
            groupRecord = yield self.directoryService().recordWithCalendarUserAddress(groupCUA)
            if groupRecord is not None:
                groupUID = groupRecord.uid
            else:
                groupUID = uidFromCalendarUserAddress(groupCUA)
            group = yield self._txn.groupByUID(groupUID)
            if group is None:
                continue

            if group.groupID in groupIDToMembershipHashMap:
                if groupIDToMembershipHashMap[group.groupID].membershipHash != group.membershipHash:
                    yield groupIDToMembershipHashMap[group.groupID].update(membershipHash=group.membershipHash)
                    changed = True
                del groupIDToMembershipHashMap[group.groupID]
            else:
                yield GroupAttendeeRecord.create(
                    self._txn,
                    resourceID=self._resourceID,
                    groupID=group.groupID,
                    membershipHash=group.membershipHash,
                )
                changed = True

        if groupIDToMembershipHashMap:
            yield GroupAttendeeRecord.deletesome(
                self._txn,
                GroupAttendeeRecord.groupID.In(groupIDToMembershipHashMap.keys()),
            )
            changed = True

        returnValue(changed)

    @inlineCallbacks
    def removeOldEventGroupLink(self, component, instances, inserting, txn):
        """
        Check to see if the calendar event has any instances ongoing into the future (past
        a cut-off value in the future - default 1 hour). If there are no ongoing instances,
        then "decouple" this resource from the automatic group reconciliation process as
        past events should only show the group membership as it existed at the time.

        @param component: the iCalendar data to process
        @type component: L{Component}
        @param instances: list of instances
        @type instances: L{InstanceList} or L{None}
        @param inserting: whether or not the resource is being created
        @type inserting: L{bool}
        @param txn: transaction to use
        @type txn: L{Transaction}
        """

        isOldEventWithGroupAttendees = False

        # If this event is old, break possible tie to group update
        if hasattr(self, "_groupCUAToAttendeeMemberPropMap"):

            # If we were not provided with any instances, then expand the component to check
            if instances is None:
                # Do an expansion out to the cut-off and then check to see if there
                # are still instances beyond that
                expand = (DateTime.getNowUTC() +
                          Duration(seconds=config.GroupAttendees.AutoUpdateSecondsFromNow))

                instances = component.expandTimeRanges(
                    expand,
                    lowerLimit=None,
                    ignoreInvalidInstances=True
                )

                isOldEventWithGroupAttendees = instances.limit is None

            elif len(instances.instances):
                cutoffDate_DateTime = (
                    DateTime.getNowUTC() +
                    Duration(seconds=config.GroupAttendees.AutoUpdateSecondsFromNow)
                )
                maxInstanceKey = sorted(instance for instance in instances)[-1]
                isOldEventWithGroupAttendees = instances[maxInstanceKey].start < cutoffDate_DateTime and instances.limit is None

            else:
                isOldEventWithGroupAttendees = instances.limit is None

            if isOldEventWithGroupAttendees:
                if inserting:
                    # don't create GROUP_ATTENDEE rows in updateEventGroupLink()
                    del self._groupCUAToAttendeeMemberPropMap
                else:
                    # delete existing group rows
                    yield GroupAttendeeRecord.deletesimple(self._txn, resourceID=self._resourceID)

        returnValue(isOldEventWithGroupAttendees)

    @inlineCallbacks
    def groupAttendeeChanged(self, groupID):
        """
        One or more group attendee membership lists have changed, so sync up with those
        changes. This might involve splitting the event if there are past instances of
        a recurring event, since we don't want the attendee list in the past instances
        to change.
        """
        component = yield self.componentForUser()

        # Change a copy of the original, as we need the original cached on the resource
        # so we can do a diff to test implicit scheduling changes
        component = component.duplicate()

        # sync group attendees
        if (yield self.reconcileGroupAttendees(component)):

            # Group attendees in the event have changed

            # Do not reconcile events entirely in the past
            if (
                yield self.removeOldEventGroupLink(
                    component,
                    instances=None,
                    inserting=False,
                    txn=self._txn
                )
            ):
                returnValue(None)

            # For recurring events we split the event so past instances are not reconciled
            if component.masterComponent() is not None and component.isRecurring():
                splitter = iCalSplitter(0, 0)
                break_point = DateTime.getToday() + Duration(seconds=config.GroupAttendees.AutoUpdateSecondsFromNow)
                rid = splitter.whereSplit(component, break_point=break_point)
                if rid is not None:
                    yield self.split(onlyThis=False, rid=rid)

                    # remove group link to ensure update (update to unknown hash would work too)
                    # FIXME: its possible that more than one group id gets updated during this single work item, so we
                    # need to make sure that ALL the group_id's are removed by this query.
                    yield GroupAttendeeRecord.deletesimple(
                        self._txn,
                        resourceID=self._resourceID,
                        groupID=groupID,
                    )

                    # update group attendee in remaining component
                    component = yield self.componentForUser()
                    component = component.duplicate()
                    yield self.reconcileGroupAttendees(component)
                    yield self._setComponentInternal(component, inserting=False, internal_state=ComponentUpdateState.SPLIT_OWNER)
                    returnValue(None)

            yield self.setComponent(component)

    def validCalendarDataCheck(self, component, inserting):
        """
        Check that the calendar data is valid iCalendar.
        @return:         tuple: (True/False if the calendar data is valid,
                                 log message string).
        """

        # Valid calendar data checks
        if not isinstance(component, Component):
            raise InvalidObjectResourceError("Wrong type of object: {0}".format(type(component),))

        try:
            component.validCalendarData(validateRecurrences=self._txn._migrating)
        except InvalidICalendarDataError as e:
            raise InvalidObjectResourceError(str(e))
        try:
            component.validCalendarForCalDAV(methodAllowed=self.calendar().isInbox())
        except InvalidICalendarDataError as e:
            raise InvalidComponentForStoreError(str(e))
        except TimezoneException as e:
            # tzid not available
            raise UnknownTimezone(str(e))

        try:
            if self._txn._migrating:
                component.validOrganizerForScheduling(doFix=True)
        except InvalidICalendarDataError as e:
            raise ValidOrganizerError(str(e))

    @inlineCallbacks
    def validAttendeeListSizeCheck(self, component, inserting):
        """
        Make sure that the Attendee list length is within bounds. We don't do this check for inbox because we
        will assume that the limit has been applied on the PUT causing the iTIP message to be created.

        FIXME: The inbox check might not take into account iSchedule stuff from outside. That needs to have
        the max attendees check applied at the time of delivery.
        """

        if config.MaxAttendeesPerInstance and not self.calendar().isInbox():
            uniqueAttendees = set()
            for attendee in component.getAllAttendeeProperties():
                uniqueAttendees.add(attendee.value())
            attendeeListLength = len(uniqueAttendees)
            if attendeeListLength > config.MaxAttendeesPerInstance:

                # Check to see whether we are increasing the count on an existing resource
                if not inserting:
                    oldcalendar = (yield self.componentForUser())
                    uniqueAttendees = set()
                    for attendee in oldcalendar.getAllAttendeeProperties():
                        uniqueAttendees.add(attendee.value())
                    oldAttendeeListLength = len(uniqueAttendees)
                else:
                    oldAttendeeListLength = 0

                if attendeeListLength > oldAttendeeListLength:
                    raise TooManyAttendeesError(
                        "Attendee list size {0} is larger than allowed limit {1}".format(
                            attendeeListLength, config.MaxAttendeesPerInstance
                        )
                    )

    @inlineCallbacks
    def validLocationResourceOrganizer(self, component, inserting, internal_state):
        """
        If the calendar owner is a location or resource, check whether an ORGANIZER property is required.
        """

        if internal_state == ComponentUpdateState.NORMAL:
            originatorPrincipal = yield self.calendar().ownerHome().directoryRecord()
            cutype = originatorPrincipal.getCUType() if originatorPrincipal is not None else "INDIVIDUAL"
            organizer = component.getOrganizer()

            # Check for an allowed change
            if organizer is None and (
                cutype == "ROOM" and not config.Scheduling.Options.AllowLocationWithoutOrganizer or
                cutype == "RESOURCE" and not config.Scheduling.Options.AllowResourceWithoutOrganizer
            ):
                raise ValidOrganizerError("Organizer required in calendar data for a {0}".format(cutype.lower(),))

            # Check for tracking the modifier
            if organizer is None and (
                cutype == "ROOM" and config.Scheduling.Options.TrackUnscheduledLocationData or
                cutype == "RESOURCE" and config.Scheduling.Options.TrackUnscheduledResourceData
            ):

                # Find current principal and update modified by details
                authz = yield self.directoryService().recordWithUID(self.calendar().viewerHome().authzuid().decode("utf-8"))
                prop = Property("X-CALENDARSERVER-MODIFIED-BY", authz.canonicalCalendarUserAddress())
                prop.setParameter("CN", authz.displayName)
                for candidate in authz.calendarUserAddresses:
                    if candidate.startswith("mailto:"):
                        prop.setParameter("EMAIL", candidate[7:])
                        break
                component.replacePropertyInAllComponents(prop)
                self._componentChanged = True

    def validAccess(self, component, inserting, internal_state):
        """
        Make sure that the X-CALENDARSERVER-ACCESS property is properly dealt with.
        """

        if component.hasProperty(Component.ACCESS_PROPERTY):

            # Must be a value we know about
            access = component.accessLevel(default=None)
            if access is None:
                raise InvalidCalendarAccessError("Private event access level not allowed")

            # Only DAV:owner is able to set the property to other than PUBLIC
            if internal_state == ComponentUpdateState.NORMAL:
                if (self.calendar().viewerHome().uid() != self.calendar().viewerHome().authzuid()) and access != Component.ACCESS_PUBLIC:
                    raise InvalidCalendarAccessError("Private event access level change not allowed")

            self.accessMode = access
        else:
            # Check whether an access property was present before and write that into the calendar data
            if not inserting and self.accessMode:
                old_access = self.accessMode
                component.addProperty(Property(name=Component.ACCESS_PROPERTY, value=old_access))

    @inlineCallbacks
    def preservePrivateComments(self, component, inserting, internal_state):
        """
        Check for private comments on the old resource and the new resource and re-insert
        ones that are lost.

        NB Do this before implicit scheduling as we don't want old clients to trigger scheduling when
        the X- property is missing.

        We now only preserve the "X-CALENDARSERVER-ATTENDEE-COMMENT" property. We will now allow clients
        to delete the "X-CALENDARSERVER-PRIVATE-COMMENT" and treat that as a removal of the attendee
        comment (which will trigger scheduling with the organizer to remove the comment on the organizer's
        side).
        """
        changed = False

        if config.Scheduling.CalDAV.get("EnablePrivateComments", True):
            old_has_private_comments = not inserting and self.hasPrivateComment
            new_has_private_comments = component.hasPropertyInAnyComponent((
                ATTENDEE_COMMENT,
            ))

            if old_has_private_comments and not new_has_private_comments and internal_state == ComponentUpdateState.NORMAL:
                # Transfer old comments to new calendar
                log.debug("Organizer private comment properties were entirely removed by the client. Restoring existing properties.")
                old_calendar = (yield self.componentForUser())
                component.transferProperties(old_calendar, (
                    ATTENDEE_COMMENT,
                ))
                changed = True

            self.hasPrivateComment = new_has_private_comments

            # Some clients appear to be buggy and are duplicating the "X-CALENDARSERVER-ATTENDEE-COMMENT" comment. We want
            # to raise an error to prevent that so the client bugs can be tracked down.

            # Look for properties with duplicate "X-CALENDARSERVER-ATTENDEE-REF" values in the same component
            if component.hasDuplicatePrivateComments(doFix=config.RemoveDuplicatePrivateComments) and internal_state == ComponentUpdateState.NORMAL:
                raise DuplicatePrivateCommentsError("Duplicate {} properties present.".format(ATTENDEE_COMMENT))

        returnValue(changed)

    @inlineCallbacks
    def replaceMissingToDoProperties(self, calendar, inserting, internal_state):
        """
        Recover any lost ORGANIZER or ATTENDEE properties in non-recurring VTODOs.
        """

        if not inserting and calendar.resourceType() == "VTODO" and not calendar.isRecurring():

            old_calendar = (yield self.componentForUser())

            new_organizer = calendar.getOrganizer()
            old_organizer = old_calendar.getOrganizerProperty()
            new_attendees = calendar.getAttendees()
            old_attendees = tuple(old_calendar.getAllAttendeeProperties())

            new_completed = calendar.masterComponent().hasProperty("COMPLETED")
            old_completed = old_calendar.masterComponent().hasProperty("COMPLETED")

            if old_organizer and not new_organizer and len(old_attendees) > 0 and len(new_attendees) == 0:
                # Transfer old organizer and attendees to new calendar
                log.debug("Organizer and attendee properties were entirely removed by the client. Restoring existing properties.")

                # Get the originator who is the owner of the calendar resource being modified
                originatorPrincipal = yield self.calendar().ownerHome().directoryRecord()
                if originatorPrincipal is not None:
                    originatorAddresses = originatorPrincipal.calendarUserAddresses
                else:
                    originatorAddresses = ("urn:x-uid:{}".format(self.calendar().ownerHome().uid),)

                for component in calendar.subcomponents():
                    if component.name() != "VTODO":
                        continue

                    if not component.hasProperty("DTSTART"):
                        # Need to put DTSTART back in or we get a date mismatch failure later
                        for old_component in old_calendar.subcomponents():
                            if old_component.name() != "VTODO":
                                continue
                            if old_component.hasProperty("DTSTART"):
                                component.addProperty(old_component.getProperty("DTSTART").duplicate())
                                break

                    # Add organizer back in from previous resource
                    component.addProperty(old_organizer.duplicate())

                    # Add attendees back in from previous resource
                    for anAttendee in old_attendees:
                        anAttendee = anAttendee.duplicate()
                        if component.hasProperty("COMPLETED") and anAttendee.value() in originatorAddresses:
                            anAttendee.setParameter("PARTSTAT", "COMPLETED")
                        component.addProperty(anAttendee)

            elif new_completed ^ old_completed and internal_state == ComponentUpdateState.NORMAL:
                # COMPLETED changed - sync up attendee state
                # We need this because many VTODO clients are not aware of scheduling,
                # i.e. they do not adjust any ATTENDEE PARTSTATs. We are going to impose
                # our own requirement that PARTSTAT is set to COMPLETED when the COMPLETED
                # property is added.

                # Transfer old organizer and attendees to new calendar
                log.debug("Sync COMPLETED property change.")

                # Get the originator who is the owner of the calendar resource being modified
                originatorPrincipal = yield self.calendar().ownerHome().directoryRecord()
                if originatorPrincipal is not None:
                    originatorAddresses = originatorPrincipal.calendarUserAddresses
                else:
                    originatorAddresses = ("urn:x-uid:{}".format(self.calendar().ownerHome().uid),)

                for component in calendar.subcomponents():
                    if component.name() != "VTODO":
                        continue

                    # Change owner partstat
                    for anAttendee in component.properties("ATTENDEE"):
                        if anAttendee.value() in originatorAddresses:
                            oldpartstat = anAttendee.parameterValue("PARTSTAT", "NEEDS-ACTION")
                            newpartstat = "COMPLETED" if component.hasProperty("COMPLETED") else "IN-PROCESS"
                            if newpartstat != oldpartstat:
                                anAttendee.setParameter("PARTSTAT", newpartstat)

    @inlineCallbacks
    def dropboxPathNormalization(self, component):
        """
        Make sure sharees only use dropbox paths of the sharer.
        """

        # Only relevant if calendar is sharee collection
        if not self.calendar().owned():

            # Get all X-APPLE-DROPBOX's and ATTACH's that are http URIs
            xdropboxes = component.getAllPropertiesInAnyComponent(
                "X-APPLE-DROPBOX",
                depth=1,
            )
            attachments = component.getAllPropertiesInAnyComponent(
                "ATTACH",
                depth=1,
            )
            attachments = [
                attachment for attachment in attachments
                if attachment.parameterValue("VALUE", "URI") == "URI" and attachment.value().startswith("http")
            ]

            if len(xdropboxes) or len(attachments):

                # Determine owner GUID
                owner = (yield self.calendar().ownerHome()).uid()

                def uriNormalize(uri):
                    urichanged = False
                    scheme, netloc, path, params, query, fragment = urlparse(uri)
                    pathbits = path.split("/")
                    if len(pathbits) >= 6 and pathbits[4] == "dropbox":
                        if pathbits[1] != "calendars":
                            pathbits[1] = "calendars"
                            urichanged = True
                        if pathbits[2] != "__uids__":
                            pathbits[2] = "__uids__"
                            urichanged = True
                        if pathbits[3] != owner:
                            pathbits[3] = owner
                            urichanged = True
                        if urichanged:
                            return urlunparse((scheme, netloc, "/".join(pathbits), params, query, fragment,))
                    return None

                for xdropbox in xdropboxes:
                    uri = uriNormalize(xdropbox.value())
                    if uri:
                        xdropbox.setValue(uri)
                        self._componentChanged = True
                for attachment in attachments:
                    uri = uriNormalize(attachment.value())
                    if uri:
                        attachment.setValue(uri)
                        self._componentChanged = True

    def processAlarms(self, component, inserting):
        """
        Remove duplicate alarms. Add a default alarm if required.

        @return: indicate whether a change was made
        @rtype: C{bool}
        """

        # Remove duplicate alarms
        if config.RemoveDuplicateAlarms and component.hasDuplicateAlarms(doFix=True):
            self._componentChanged = True

        # Only if feature enabled
        if not config.EnableDefaultAlarms:
            return

        # Check that we are creating and this is not the inbox
        if not inserting or self.calendar().isInbox():
            return

        # Never add default alarms to calendar data in shared calendars
        if not self.calendar().owned():
            return

        # Add default alarm for VEVENT and VTODO only
        mtype = component.mainType().upper()
        if component.mainType().upper() not in ("VEVENT", "VTODO"):
            return
        vevent = mtype == "VEVENT"

        # Check timed or all-day
        start, _ignore_end = component.mainComponent().getEffectiveStartEnd()
        if start is None:
            # Yes VTODOs might have no DTSTART or DUE - in this case we do not add a default
            return
        timed = not start.isDateOnly()

        # See if default exists and add using appropriate logic
        alarm = self.calendar().getDefaultAlarm(vevent, timed)
        if not alarm:
            alarm = self.calendar().viewerHome().getDefaultAlarm(vevent, timed)
        if alarm and alarm != "empty" and component.addAlarms(alarm):
            self._componentChanged = True

    @inlineCallbacks
    def addStructuredLocation(self, component):
        """
        Scan the component for ROOM attendees; if any are associated with an
        address record which has street address and geo coordinates, add an
        X-APPLE-STRUCTURED-LOCATION property and update the LOCATION property
        to contain the name and street address.  X-APPLE-STRUCTURED-LOCATION
        with X-CUADDR but no corresponding ATTENDEE are removed.
        """
        dir = self.directoryService()

        changed = False
        cache = {}

        for sub in component.subcomponents():
            existingLocationProps = list(sub.properties("LOCATION"))
            if len(existingLocationProps) == 0:
                existingLocationValue = ""
            else:
                existingLocationValue = existingLocationProps[0].value()
            existingLocations = []
            for value in existingLocationValue.split(";"):
                if value:
                    existingLocations.append(value.strip())

            # index the structured locations on X-CUADDR and X-TITLE
            allStructured = {
                "cua": {},
                "title": {}
            }
            for structured in sub.properties("X-APPLE-STRUCTURED-LOCATION"):
                cuAddr = structured.parameterValue("X-CUADDR")
                if cuAddr:
                    allStructured["cua"][cuAddr] = structured
                else:
                    title = structured.parameterValue("X-TITLE")
                    if title:
                        allStructured["title"][title] = structured

            for attendee in sub.getAllAttendeeProperties():
                if attendee.parameterValue("CUTYPE") == "ROOM":
                    value = attendee.value()

                    # Cache record based data once per-attendee
                    if value not in cache:
                        cache[value] = None
                        loc = yield dir.recordWithCalendarUserAddress(value)
                        if loc is not None:
                            uid = getattr(loc, "associatedAddress", "")
                            if uid:
                                addr = yield dir.recordWithUID(uid)
                                if addr is not None:
                                    street = getattr(addr, "streetAddress", "")
                                    geo = getattr(addr, "geographicLocation", "")
                                    if street and geo:
                                        title = attendee.parameterValue("CN")
                                        cache[value] = (street, geo, title,)

                    # Use the cached data if present
                    entry = cache[value]
                    if entry is not None:

                        street, geo, title = entry
                        newLocationValue = "{0}\n{1}".format(title, street.encode("utf-8"))

                        # Is there already a structured location property for
                        # this attendee?  If so, we'll update it.
                        # Unfortunately, we can only depend on X-CUADDR
                        # going forward, but there is going to be old existing
                        # X-APPLE-STRUCTURED-LOCATIONs that haven't yet had
                        # those added.  So let's first look up by X-CUADDR and
                        # then by X-TITLE.
                        if value in allStructured["cua"]:
                            structured = allStructured["cua"][value]
                        elif title in allStructured["title"]:
                            structured = allStructured["title"][title]
                        else:
                            structured = None

                        params = {
                            "X-ADDRESS": street,
                            "X-APPLE-RADIUS": "71",
                            "X-TITLE": title,
                            "X-CUADDR": value,
                        }

                        if structured is None:
                            # Create a new one
                            prevTitle = attendee.parameterValue("CN")
                            structured = Property(
                                "X-APPLE-STRUCTURED-LOCATION",
                                geo.encode("utf-8"), params=params,
                                valuetype=Value.VALUETYPE_URI
                            )
                            changed = True
                            sub.addProperty(structured)
                        else:
                            # Update existing one
                            prevGeo = structured.value()
                            if geo != prevGeo:
                                structured.setValue(geo)
                                changed = True
                            prevTitle = structured.parameterValue("X-TITLE")
                            for paramName, paramValue in params.iteritems():
                                prevValue = structured.parameterValue(paramName)
                                if paramValue != prevValue:
                                    structured.setParameter(paramName, paramValue)
                                    changed = True

                        if changed:
                            # Replace old location values with the new ones
                            for i in xrange(len(existingLocations)):
                                existingLocation = existingLocations[i]
                                if (
                                    prevTitle is not None and
                                    (
                                        # it's either an exact match or matches
                                        # up to the newline which precedes the
                                        # street address
                                        existingLocation == prevTitle or
                                        existingLocation.startswith("{}\n".format(prevTitle))
                                    )
                                ):
                                    existingLocations[i] = newLocationValue
                                    break
                            else:
                                existingLocations.append(newLocationValue)

            # Remove any server-generated structured locations without an ATTENDEE
            for structured in sub.properties("X-APPLE-STRUCTURED-LOCATION"):
                cuAddr = structured.parameterValue("X-CUADDR")
                if cuAddr is not None:  # therefore it's one that requires an ATTENDEE...
                    attendeeProp = sub.getAttendeeProperty((cuAddr,))
                    if attendeeProp is None:  # ...remove it if no matching ATTENDEE
                        sub.removeProperty(structured)
                        changed = True

            # Update the LOCATION
            newLocationValue = "; ".join(existingLocations)
            if newLocationValue != existingLocationValue:
                newLocProperty = Property(
                    "LOCATION",
                    newLocationValue
                )
                sub.replaceProperty(newLocProperty)
                changed = True

        if changed:
            self._componentChanged = True

    @inlineCallbacks
    def decorateHostedStatus(self, component):
        """
        Scan the component for attendees; if any are hosted externally (e.g.
        they don't exist in our directory) add the attribute:
        X-APPLE-HOSTED-STATUS=EXTERNAL
        """
        dir = self.directoryService()
        for sub in component.subcomponents():
            for attendee in sub.getAllAttendeeProperties():
                value = attendee.value()

                record = yield dir.recordWithCalendarUserAddress(value)
                if record is None:
                    status = "external"
                else:
                    status = "local"

                prevValue = attendee.parameterValue(config.HostedStatus.Parameter)
                if config.HostedStatus.Values[status]:
                    attendee.setParameter(
                        config.HostedStatus.Parameter,
                        config.HostedStatus.Values[status]
                    )
                else:
                    attendee.removeParameter(config.HostedStatus.Parameter)
                if attendee.parameterValue(config.HostedStatus.Parameter) != prevValue:
                    self._componentChanged = True

    @inlineCallbacks
    def doImplicitScheduling(self, component, inserting, internal_state, options, split_details=None, updateSelf=False):

        new_component = None
        did_implicit_action = False
        is_scheduling_resource = False
        schedule_state = None

        is_internal = internal_state not in (
            ComponentUpdateState.NORMAL,
            ComponentUpdateState.ATTACHMENT_UPDATE,
            ComponentUpdateState.SPLIT_OWNER,
        )

        # Do scheduling
        if not self.calendar().isInbox():
            # For splitting we are passed a "raw" component - one with the per-user data pieces in it.
            # We need to filter that down just to the owner's view to do scheduling, but still ensure the
            # raw component is written out.
            if split_details is not None:
                user_uuid = self._parentCollection.viewerHome().uid()
                component = PerUserDataFilter(user_uuid).filter(component.duplicate())

            scheduler = ImplicitScheduler(logItems=self._txn.logItems, options=options)

            # PUT
            resourceToUpdate = None if inserting else self
            # (If we're in the trash, it's okay to update this, even if inserting=True)
            resourceToUpdate = self if updateSelf else resourceToUpdate
            do_implicit_action, is_scheduling_resource = (yield scheduler.testImplicitSchedulingPUT(
                self.calendar(),
                resourceToUpdate,
                component,
                internal_request=is_internal,
            ))

            # Structured locations for organizer non-splits only
            if scheduler.state == "organizer" and internal_state != ComponentUpdateState.SPLIT_OWNER:
                yield self.addStructuredLocation(component)

            # Group attendees - though not during a split
            if scheduler.state == "organizer" and internal_state != ComponentUpdateState.SPLIT_OWNER:
                changed = yield self.reconcileGroupAttendees(component, inserting)
                if changed:
                    yield scheduler.extractAttendees()

            # Set an attribute on this object to indicate that it is valid to check for an event split. We need to do this here so that if a timeout
            # occurs whilst doing implicit processing (most likely because the event is too big) we are able to subsequently detect that it is OK
            # to split and then try that.
            if internal_state not in (ComponentUpdateState.SPLIT_OWNER, ComponentUpdateState.SPLIT_ATTENDEE,) and scheduler.state == "organizer":
                self.okToSplit = True

            if do_implicit_action and not is_internal:

                # Cannot do implicit in sharee's shared calendar
                if not self.calendar().owned():
                    scheduler.setSchedulingNotAllowed(
                        ShareeAllowedError,
                        "Sharee's cannot schedule",
                    )

                new_calendar = (yield scheduler.doImplicitScheduling(self.schedule_tag_match, split_details))
                if new_calendar:
                    if isinstance(new_calendar, int):
                        returnValue(new_calendar)
                    else:
                        new_component = new_calendar
                did_implicit_action = True
                schedule_state = scheduler.state

        returnValue((is_scheduling_resource, new_component, did_implicit_action, schedule_state,))

    @inlineCallbacks
    def mergePerUserData(self, component, inserting):
        """
        Merge incoming calendar data with other user's per-user data in existing calendar data.

        @param component: incoming calendar data
        @type component: L{twistedcaldav.ical.Component}
        """
        accessUID = self.calendar().viewerHome().uid()
        oldCal = (yield self.component()) if not inserting else None

        # Duplicate before we do the merge because someone else may "own" the calendar object
        # and we should not change it. This is not ideal as we may duplicate it unnecessarily
        # but we currently have no api to let the caller tell us whether it cares about the
        # whether the calendar data is changed or not.
        try:
            component = PerUserDataFilter(accessUID).merge(component.duplicate(), oldCal)
        except ValueError:
            log.error("Invalid per-user data merge")
            raise InvalidPerUserDataMerge("Cannot merge per-user data")

        returnValue(component)

    def processScheduleTags(self, component, inserting, internal_state):

        # Check for scheduling object resource and write property
        if self.isScheduleObject:
            # Need to figure out when to change the schedule tag:
            #
            # 1. If this is not an internal request then the resource is being explicitly changed
            # 2. If it is an internal request for the Organizer, schedule tag never changes
            # 3. If it is an internal request for an Attendee and the message being processed came
            #    from the Organizer then the schedule tag changes.

            # Check what kind of processing is going on
            change_scheduletag = not (
                (internal_state == ComponentUpdateState.ORGANIZER_ITIP_UPDATE) or
                (internal_state == ComponentUpdateState.ATTENDEE_ITIP_UPDATE) and hasattr(self._txn, "doing_attendee_refresh")
            )

            if change_scheduletag or not self.scheduleTag:
                self.scheduleTag = str(uuid.uuid4())

            # Handle weak etag compatibility
            if config.Scheduling.CalDAV.ScheduleTagCompatibility:
                if change_scheduletag:
                    # Schedule-Tag change => weak ETag behavior must not happen
                    etags = ()
                else:
                    # Schedule-Tag did not change => add current ETag to list of those that can
                    # be used in a weak precondition test
                    etags = self.scheduleEtags
                    if etags is None:
                        etags = ()
                etags += (self._generateEtag(str(component)),)
                self.scheduleEtags = etags
            else:
                self.scheduleEtags = ()
        else:
            self.scheduleTag = ""
            self.scheduleEtags = ()

    @inlineCallbacks
    def _lockUID(self, uid, internal_state):
        """
        Create a lock on the component's UID and verify, after getting the lock, that the incoming UID
        meets the requirements of the store.
        """

        if not self._lockedUID and internal_state in (ComponentUpdateState.NORMAL, ComponentUpdateState.SPLIT_OWNER):
            yield NamedLock.acquire(self._txn, "ImplicitUIDLock:%s" % (hashlib.md5(uid).hexdigest(),))
            self._lockedUID = True

    @inlineCallbacks
    def _lockAndCheckUID(self, component, inserting, internal_state):
        """
        Create a lock on the component's UID and verify, after getting the lock, that the incoming UID
        meets the requirements of the store.
        """

        new_uid = component.resourceUID()
        yield self._lockUID(new_uid, internal_state)

        # UID conflict check - note we do this after reserving the UID to avoid a race condition where two requests
        # try to write the same calendar data to two different resource URIs.
        if not self.calendar().isInbox():
            # Cannot overwrite a resource with different UID
            if not inserting:
                if self._uid != new_uid:
                    raise InvalidUIDError("Cannot change the UID in an existing resource.")
            else:
                # New UID must be unique for the owner - no need to do this on an overwrite as we can assume
                # the store is already consistent in this regard
                elsewhere = (yield self.calendar().ownerHome().hasCalendarResourceUIDSomewhereElse(new_uid, self, "schedule"))
                if elsewhere is not None:
                    if elsewhere.calendar().id() == self.calendar().id():
                        raise UIDExistsError("UID already exists in same calendar.")
                    else:
                        raise UIDExistsElsewhereError("UID already exists in different calendar: {0}".format(elsewhere.calendar().name(),))

    @inlineCallbacks
    def setComponent(self, component, inserting=False, options=None):
        """
        Public api for storing a component. This will do full data validation checks on the specified component.
        Scheduling will be done automatically.
        """

        # Cross-pod calls come in with component as str or unicode
        if isinstance(component, str) or isinstance(component, unicode):
            try:
                component = self._componentClass.fromString(component)
            except InvalidICalendarDataError as e:
                raise InvalidComponentForStoreError(str(e))
        try:
            result = yield self._setComponentInternal(component, inserting, ComponentUpdateState.NORMAL, options)
        except Exception:
            ex = Failure()

            # If the failure is due to a txn timeout and we have the special attribute indicating it is OK to
            # attempt a split, then try splitting only when doing an update.
            if self._txn.timedout and hasattr(self, "okToSplit") and not inserting:
                yield self.timeoutSplit()

            ex.raiseException()

        returnValue(result)

    @inlineCallbacks
    def _setComponentInternal(self, component, inserting=False, internal_state=ComponentUpdateState.NORMAL, options=None, split_details=None, updateSelf=False):
        """
        Setting the component internally to the store itself. This will bypass a whole bunch of data consistency checks
        on the assumption that those have been done prior to the component data being provided, provided the flag is set.
        This should always be treated as an api private to the store.
        """

        self._componentChanged = False
        self.schedule_tag_match = (
            not self.calendar().isInbox() and
            internal_state == ComponentUpdateState.NORMAL and
            SetComponentOptions.value(options, SetComponentOptions.smartMerge)
        )
        schedule_state = None

        if internal_state in (ComponentUpdateState.SPLIT_OWNER, ComponentUpdateState.SPLIT_ATTENDEE,):
            # When splitting, some state from the previous resource needs to be properly
            # preserved in the new one when storing the component. Since we don't do the "full"
            # store here, we need to add the explicit pieces we need for state preservation.

            # Check access
            if config.EnablePrivateEvents:
                self.validAccess(component, inserting, internal_state)

            # Preserve private comments
            yield self.preservePrivateComments(component, inserting, internal_state)

            managed_copied, managed_removed = (yield self.resourceCheckAttachments(component, inserting))

            # Do scheduling only for owner split
            if internal_state == ComponentUpdateState.SPLIT_OWNER:
                # UID lock - this will remain active until the end of the current txn
                yield self._lockAndCheckUID(component, inserting, internal_state)

                # Make sure various bits of scheduling meta-data are correctly setup to ensure the
                # new resource created by a split has the proper state
                implicit_result = yield self.doImplicitScheduling(component, inserting, internal_state, options, split_details)
                if isinstance(implicit_result, int):
                    msg = "Invalid return status code from ImplicitScheduler during split: %s" % (implicit_result,)
                    log.error(msg)
                    raise InvalidObjectResourceError(msg)

                self.isScheduleObject, new_component, did_implicit_action, schedule_state = implicit_result
                if new_component is not None:
                    component = new_component
                if did_implicit_action:
                    self._componentChanged = True

            elif internal_state == ComponentUpdateState.SPLIT_ATTENDEE:
                # This must always be set since the only time we get is is during scheduling
                self.isScheduleObject = True

            self.processScheduleTags(component, inserting, internal_state)

        elif internal_state != ComponentUpdateState.RAW:
            # Handle all validation operations here.
            yield self.fullValidation(component, inserting, internal_state)

            # UID lock - this will remain active until the end of the current txn
            yield self._lockAndCheckUID(component, inserting, internal_state)

            # Preserve private comments
            changed = yield self.preservePrivateComments(component, inserting, internal_state)
            if changed:
                self._componentChanged = True

            # Fix broken VTODOs
            # Note: if this does recover lost organizer/attendee properties, the
            # implicit code below will set _componentChanged
            yield self.replaceMissingToDoProperties(component, inserting, internal_state)

            # Handle sharing dropbox normalization (and set _componentChanged)
            yield self.dropboxPathNormalization(component)

            # Pre-process managed attachments
            if internal_state == ComponentUpdateState.NORMAL:
                managed_copied, managed_removed = (yield self.resourceCheckAttachments(component, inserting))

            # Default/duplicate alarms (and set _componentChanged)
            self.processAlarms(component, inserting)

            # Process hosted status (and set _componentChanged)
            if config.HostedStatus.Enabled:
                yield self.decorateHostedStatus(component)

            # Do scheduling
            implicit_result = (yield self.doImplicitScheduling(component, inserting, internal_state, options, updateSelf=updateSelf))
            if isinstance(implicit_result, int):
                if implicit_result == ImplicitScheduler.STATUS_ORPHANED_CANCELLED_EVENT:
                    raise ResourceDeletedError("Resource created but immediately deleted by the server.")

                elif implicit_result == ImplicitScheduler.STATUS_ORPHANED_EVENT:

                    # Now forcibly delete the event
                    if not inserting:
                        yield self._removeInternal(internal_state=ComponentRemoveState.INTERNAL, useTrash=False)
                        raise ResourceDeletedError("Resource modified but immediately deleted by the server.")
                    else:
                        raise AttendeeAllowedError("Attendee cannot create event for Organizer: {0}".format(implicit_result,))

                else:
                    msg = "Invalid return status code from ImplicitScheduler: {0}".format(implicit_result,)
                    log.error(msg)
                    raise InvalidObjectResourceError(msg)
            else:
                self.isScheduleObject, new_component, did_implicit_action, schedule_state = implicit_result
                if new_component is not None:
                    component = new_component
                if did_implicit_action:
                    self._componentChanged = True

            if not hasattr(self, "tr_change"):
                if inserting or hasattr(component, "noInstanceIndexing") or not config.FreeBusyIndexSmartUpdate:
                    self.tr_change = None
                else:
                    oldcomponent = yield self.componentForUser()
                    self.tr_change = iCalDiff(oldcomponent, component, False).timeRangeDifference()

            # Always do the per-user data merge right before we store
            component = (yield self.mergePerUserData(component, inserting))

            self.processScheduleTags(component, inserting, internal_state)

        # When migrating we always do validity check to fix issues
        elif self._txn._migrating:
            self.validCalendarDataCheck(component, inserting)

        # If updateSelf is True, we want to turn inserting off within updateDatabase
        yield self.updateDatabase(component, inserting=inserting if not updateSelf else False)

        # update GROUP_ATTENDEE table rows
        if inserting:
            yield self.updateEventGroupLink()

        # Post process managed attachments
        if internal_state in (
            ComponentUpdateState.NORMAL,
            ComponentUpdateState.SPLIT_OWNER,
            ComponentUpdateState.SPLIT_ATTENDEE,
        ):
            if managed_copied:
                yield self.copyResourceAttachments(managed_copied)
            if managed_removed:
                yield self.removeResourceAttachments(managed_removed)

        if inserting:
            yield self._calendar._insertRevision(self._name)
        else:
            yield self._calendar._updateRevision(self._name)

        # Determine change category
        category = ChangeCategory.default
        if internal_state == ComponentUpdateState.INBOX:
            category = ChangeCategory.inbox
        elif internal_state == ComponentUpdateState.ORGANIZER_ITIP_UPDATE:
            category = ChangeCategory.organizerITIPUpdate
        elif (
            internal_state == ComponentUpdateState.ATTENDEE_ITIP_UPDATE and
            hasattr(self._txn, "doing_attendee_refresh")
        ):
            category = ChangeCategory.attendeeITIPUpdate

        yield self._calendar.notifyChanged(category=category)

        # Finally check if a split is needed
        if internal_state not in (ComponentUpdateState.SPLIT_OWNER, ComponentUpdateState.SPLIT_ATTENDEE,) and schedule_state == "organizer":
            yield self.checkSplit()

        returnValue(self._componentChanged)

    def _generateEtag(self, componentText):
        return hashlib.md5(componentText + (self.scheduleTag if self.scheduleTag else "")).hexdigest()

    @inlineCallbacks
    def updateDatabase(self, component, expand_until=None, reCreate=False,
                       inserting=False, txn=None):
        """
        Update the database tables for the new data being written. Occasionally we might need to do an update to
        time-range data via a separate transaction, so we allow that to be passed in. Note that in that case
        access to the parent resources will not occur in this method, so the queries on the new txn won't depend
        on any parent objects having the same txn set.

        @param component: calendar data to store
        @type component: L{Component}
        """

        # Setup appropriate txn
        txn = txn if txn is not None else self._txn

        # inbox does things slightly differently
        isInboxItem = self.calendar().isInbox()

        # In some cases there is no need to remove/rebuild the instance index because we know no time or
        # freebusy related properties have changed (e.g. an attendee reply and refresh). In those cases
        # the component will have a special attribute present to let us know to suppress the instance indexing.
        if inserting or reCreate:
            instanceIndexingRequired = True
        elif getattr(component, "noInstanceIndexing", False):
            instanceIndexingRequired = False
        else:
            instanceIndexingRequired = getattr(self, "tr_change", True) in (None, True)
        instances = None

        if instanceIndexingRequired:

            # Decide how far to expand based on the component. doInstanceIndexing will indicate whether we
            # store expanded instance data immediately, or wait until a re-expand is triggered by some later
            # operation.
            doInstanceIndexing = False
            master = component.masterComponent()
            if (master is None or not component.isRecurring()):
                # When there is no master we have a set of overridden components -
                #   index them all.
                # When there is one instance - index it.
                expand = DateTime(2100, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
                doInstanceIndexing = True
            else:

                # If migrating or re-creating or config option for delayed indexing is off, always index
                if reCreate or txn._migrating or (not config.FreeBusyIndexDelayedExpand and not isInboxItem):
                    doInstanceIndexing = True

                # Duration into the future through which recurrences are expanded in the index
                # by default.  This is a caching parameter which affects the size of the index;
                # it does not affect search results beyond this period, but it may affect
                # performance of such a search.
                expand = (DateTime.getToday() +
                          Duration(days=config.FreeBusyIndexExpandAheadDays))

                if expand_until and expand_until > expand:
                    expand = expand_until

                # Maximum duration into the future through which recurrences are expanded in the
                # index.  This is a caching parameter which affects the size of the index; it
                # does not affect search results beyond this period, but it may affect
                # performance of such a search.
                #
                # When a search is performed on a time span that goes beyond that which is
                # expanded in the index, we have to open each resource which may have data in
                # that time period.  In order to avoid doing that multiple times, we want to
                # cache those results.  However, we don't necessarily want to cache all
                # occurrences into some obscenely far-in-the-future date, so we cap the caching
                # period.  Searches beyond this period will always be relatively expensive for
                # resources with occurrences beyond this period.
                if expand > (DateTime.getToday() +
                             Duration(days=config.FreeBusyIndexExpandMaxDays)):
                    raise IndexedSearchException

            if config.FreeBusyIndexLowerLimitDays:
                truncateLowerLimit = DateTime.getToday()
                truncateLowerLimit.offsetDay(-config.FreeBusyIndexLowerLimitDays)
            else:
                truncateLowerLimit = None

            # Always do recurrence expansion even if we do not intend to index - we need this to double-check the
            # validity of the iCalendar recurrence data.
            try:
                instances = component.expandTimeRanges(expand, lowerLimit=truncateLowerLimit, ignoreInvalidInstances=reCreate)
                recurrenceLimit = instances.limit
                recurrenceLowerLimit = instances.lowerLimit
            except InvalidOverriddenInstanceError as e:
                self.log.error(
                    "Invalid instance {rid} when indexing {name} in {cal!r}",
                    rid=e.rid, name=self._name, cal=self._calendar,
                )

                if txn._migrating:
                    # TODO: fix the data here by re-writing component then re-index
                    instances = component.expandTimeRanges(expand, lowerLimit=truncateLowerLimit, ignoreInvalidInstances=True)
                    recurrenceLimit = instances.limit
                    recurrenceLowerLimit = instances.lowerLimit
                else:
                    raise

            # Now coerce indexing to off if needed
            if not doInstanceIndexing:
                # instances = None # used by removeOldEventGroupLink() call at end
                recurrenceLowerLimit = None
                recurrenceLimit = DateTime(1900, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)

        co = self._objectSchema
        tr = schema.TIME_RANGE

        # Do not update if reCreate (re-indexing - we don't want to re-write data
        # or cause modified to change)
        if not reCreate:
            componentText = str(component)
            self._objectText = componentText
            self._cachedComponent = component
            self._cachedCommponentPerUser = {}

            organizer = component.getOrganizer()
            if not organizer:
                organizer = ""

            # CALENDAR_OBJECT table update
            self._uid = component.resourceUID()
            self._md5 = self._generateEtag(componentText)
            self._size = len(componentText)

            # Special - if migrating we need to preserve the original md5
            if txn._migrating and hasattr(component, "md5"):
                self._md5 = component.md5

            # Determine attachment mode (ignore inbox's) - NB we have to do this
            # after setting up other properties as UID at least is needed
            self._attachment = _ATTACHMENTS_MODE_NONE
            if not self._dropboxID:
                if not isInboxItem:
                    if component.hasPropertyInAnyComponent("X-APPLE-DROPBOX"):
                        self._attachment = _ATTACHMENTS_MODE_WRITE
                        self._dropboxID = (yield self.dropboxID())
                    else:
                        # Only include a dropbox id if dropbox attachments exist
                        attachments = component.getAllPropertiesInAnyComponent("ATTACH")
                        has_dropbox = any([attachment.value().find("/dropbox/") != -1 for attachment in attachments])
                        if has_dropbox:
                            self._attachment = _ATTACHMENTS_MODE_READ
                            self._dropboxID = (yield self.dropboxID())

            values = {
                co.CALENDAR_RESOURCE_ID: self._calendar._resourceID,
                co.RESOURCE_NAME: self._name,
                co.ICALENDAR_TEXT: componentText,
                co.ICALENDAR_UID: self._uid,
                co.ICALENDAR_TYPE: component.resourceType(),
                co.ATTACHMENTS_MODE: self._attachment,
                co.DROPBOX_ID: self._dropboxID,
                co.ORGANIZER: organizer,
                co.ACCESS: self._access,
                co.SCHEDULE_OBJECT: self._schedule_object,
                co.SCHEDULE_TAG: self._schedule_tag,
                co.SCHEDULE_ETAGS: self._schedule_etags,
                co.PRIVATE_COMMENTS: self._private_comments,
                co.MD5: self._md5,
                co.DATAVERSION: self._currentDataVersion,
            }

            # Only needed if indexing being changed
            if instanceIndexingRequired:
                values[co.RECURRANCE_MIN] = pyCalendarToSQLTimestamp(normalizeForIndex(recurrenceLowerLimit)) if recurrenceLowerLimit else None
                values[co.RECURRANCE_MAX] = pyCalendarToSQLTimestamp(normalizeForIndex(recurrenceLimit)) if recurrenceLimit else None

            if inserting:
                self._resourceID, self._created, self._modified = (
                    yield Insert(
                        values,
                        Return=(co.RESOURCE_ID, co.CREATED, co.MODIFIED)
                    ).on(txn)
                )[0]
                self._created = parseSQLTimestamp(self._created)
                self._modified = parseSQLTimestamp(self._modified)
                self._original_collection = None
                self._trashed = None
            else:
                values[co.MODIFIED] = utcNowSQL
                self._modified = parseSQLTimestamp((
                    yield Update(
                        values,
                        Where=co.RESOURCE_ID == self._resourceID,
                        Return=co.MODIFIED,
                    ).on(txn)
                )[0][0])

                # Need to wipe the existing time-range for this and rebuild if required
                if instanceIndexingRequired:
                    yield Delete(
                        From=tr,
                        Where=tr.CALENDAR_OBJECT_RESOURCE_ID == self._resourceID
                    ).on(txn)
        else:
            # Keep MODIFIED the same when doing an index-only update
            values = {
                co.RECURRANCE_MIN: pyCalendarToSQLTimestamp(normalizeForIndex(recurrenceLowerLimit)) if recurrenceLowerLimit else None,
                co.RECURRANCE_MAX: pyCalendarToSQLTimestamp(normalizeForIndex(recurrenceLimit)) if recurrenceLimit else None,
                co.MODIFIED: self._modified,
            }

            yield Update(
                values,
                Where=co.RESOURCE_ID == self._resourceID
            ).on(txn)

            # Need to wipe the existing time-range for this and rebuild
            yield Delete(
                From=tr,
                Where=tr.CALENDAR_OBJECT_RESOURCE_ID == self._resourceID
            ).on(txn)

        if instanceIndexingRequired and doInstanceIndexing:
            yield self._addInstances(component, instances, truncateLowerLimit, isInboxItem, txn)

        yield self.removeOldEventGroupLink(component, instances, inserting, txn)

    @inlineCallbacks
    def _addInstances(self, component, instances, truncateLowerLimit, isInboxItem, txn):
        """
        Add the set of supplied instances to the store.

        @param component: the component whose instances are being added
        @type component: L{Component}
        @param instances: the set of instances to add
        @type instances: L{InstanceList}
        @param truncateLowerLimit: the lower limit for instances
        @type truncateLowerLimit: L{DateTime}
        @param isInboxItem: indicates if an inbox item
        @type isInboxItem: C{bool}
        @param txn: transaction to use
        @type txn: L{Transaction}
        """

        # TIME_RANGE table update
        lowerLimitApplied = False
        for key in instances:
            instance = instances[key]
            start = instance.start
            end = instance.end
            floating = instance.start.floating()
            transp = instance.component.adjustedTransp() == "TRANSPARENT"
            fbtype = instance.component.getFBType()
            start.setTimezoneUTC(True)
            end.setTimezoneUTC(True)

            # Ignore if below the lower limit
            if truncateLowerLimit and end < truncateLowerLimit:
                lowerLimitApplied = True
                continue

            yield self._addInstanceDetails(component, instance.rid, start, end, floating, transp, fbtype, isInboxItem, txn)

        # For truncated items we insert a tomb stone lower bound so that a time-range
        # query with just an end bound will match
        if lowerLimitApplied or instances.lowerLimit and len(instances.instances) == 0:
            start = DateTime(1901, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
            end = DateTime(1901, 1, 1, 1, 0, 0, tzid=Timezone.UTCTimezone)
            yield self._addInstanceDetails(component, None, start, end, False, True, "UNKNOWN", isInboxItem, txn)

        # Special - for unbounded recurrence we insert a value for "infinity"
        # that will allow an open-ended time-range to always match it.
        # We also need to add the "infinity" value if the event was bounded but
        # starts after the future expansion cut-off limit.
        if component.isRecurringUnbounded() or instances.limit and len(instances.instances) == 0:
            start = DateTime(2100, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
            end = DateTime(2100, 1, 1, 1, 0, 0, tzid=Timezone.UTCTimezone)
            yield self._addInstanceDetails(component, None, start, end, False, True, "UNKNOWN", isInboxItem, txn)

    @inlineCallbacks
    def _addInstanceDetails(self, component, rid, start, end, floating, transp, fbtype, isInboxItem, txn):

        tr = schema.TIME_RANGE
        tpy = schema.PERUSER

        instanceid = (yield Insert({
            tr.CALENDAR_RESOURCE_ID: self._calendar._resourceID,
            tr.CALENDAR_OBJECT_RESOURCE_ID: self._resourceID,
            tr.FLOATING: floating,
            tr.START_DATE: pyCalendarToSQLTimestamp(start),
            tr.END_DATE: pyCalendarToSQLTimestamp(end),
            tr.FBTYPE: icalfbtype_to_indexfbtype.get(fbtype, icalfbtype_to_indexfbtype["FREE"]),
            tr.TRANSPARENT: transp,
        }, Return=tr.INSTANCE_ID).on(txn))[0][0]

        # Don't do transparency for inbox items - we never do freebusy on inbox
        if not isInboxItem:
            peruserdata = component.perUserData(rid)
            for useruid, (usertransp, adjusted_start, adjusted_end) in peruserdata:

                def _adjustDateTime(dt, adjustment, add_duration):
                    if isinstance(adjustment, Duration):
                        return pyCalendarToSQLTimestamp((dt + adjustment) if add_duration else (dt - adjustment))
                    elif isinstance(adjustment, DateTime):
                        return pyCalendarToSQLTimestamp(normalizeForIndex(adjustment))
                    else:
                        return None

                if usertransp != transp or adjusted_start is not None or adjusted_end is not None:
                    (yield Insert({
                        tpy.TIME_RANGE_INSTANCE_ID: instanceid,
                        tpy.USER_ID: useruid if useruid else ".",
                        tpy.TRANSPARENT: usertransp,
                        tpy.ADJUSTED_START_DATE: _adjustDateTime(start, adjusted_start, add_duration=False),
                        tpy.ADJUSTED_END_DATE: _adjustDateTime(end, adjusted_end, add_duration=True),
                    }).on(txn))

    @inlineCallbacks
    def copyMetadata(self, other):
        """
        Copy metadata from one L{CalendarObjectResource} to another. This is only
        used during a migration step.
        """
        co = self._objectSchema
        values = {
            co.ATTACHMENTS_MODE: other._attachment,
            co.DROPBOX_ID: other._dropboxID,
            co.ACCESS: other._access,
            co.SCHEDULE_OBJECT: other._schedule_object,
            co.SCHEDULE_TAG: other._schedule_tag,
            co.SCHEDULE_ETAGS: other._schedule_etags,
            co.PRIVATE_COMMENTS: other._private_comments,
        }

        yield Update(
            values,
            Where=co.RESOURCE_ID == self._resourceID
        ).on(self._txn)

    @inlineCallbacks
    def component(self, doUpdate=False):
        """
        Read calendar data and validate/fix it. Do not raise a store error here
        if there are unfixable errors as that could prevent the overall request
        to fail. Instead we will hand bad data off to the caller - that is not
        ideal but in theory we should have checked everything on the way in and
        only allowed in good data.
        """

        if self._cachedComponent is None:

            text = yield self._text()

            try:
                component = Component.fromString(text)
            except InvalidICalendarDataError as e:
                # This is a really bad situation, so do raise
                raise InternalDataStoreError(
                    "Data corruption detected ({0}) in id: {1}".format(
                        e, self._resourceID
                    )
                )

            # Fix any bogus data we can
            fixed, unfixed = component.validCalendarData(doFix=True, doRaise=False)

            if unfixed:
                self.log.error(
                    "Calendar data id={id} had unfixable problems:\n  {problems}",
                    id=self._resourceID, problems="\n  ".join(unfixed),
                )

            if fixed:
                self.log.error(
                    "Calendar data id={id} had fixable problems:\n  {problems}",
                    id=self._resourceID, problems="\n  ".join(fixed),
                )

            # Check for on-demand data upgrade
            if self._dataversion < self._currentDataVersion:
                yield self.upgradeData(component, doUpdate)

            self._cachedComponent = component
            self._cachedCommponentPerUser = {}

        returnValue(self._cachedComponent)

    @inlineCallbacks
    def componentForUser(self, user_uuid=None):
        """
        Return the iCalendar component filtered for the specified user's per-user data.

        @param user_uuid: the user UUID to filter on
        @type user_uuid: C{str}

        @return: the filtered calendar component
        @rtype: L{twistedcaldav.ical.Component}
        """

        if user_uuid is None:
            user_uuid = self._parentCollection.viewerHome().uid()

        if user_uuid not in self._cachedCommponentPerUser:
            caldata = yield self.component()
            filtered = PerUserDataFilter(user_uuid).filter(caldata.duplicate())
            self._cachedCommponentPerUser[user_uuid] = filtered
        returnValue(self._cachedCommponentPerUser[user_uuid])

    @inlineCallbacks
    def upgradeData(self, component, doUpdate=False):
        """
        Implement in sub-classes. If the data version of this item does not match
        the current data version, call this method and implement a data upgrade,
        writing back the new data and updating the data version.
        """

        if self._dataversion < 1:
            # Normalize CUAs:
            yield component.normalizeCalendarUserAddresses(
                normalizationLookup,
                self.directoryService().recordWithCalendarUserAddress
            )

        self._dataversion = self._currentDataVersion
        if doUpdate:
            # Do the update right now
            yield self.updateDatabase(component)
        else:
            # Do the update later
            notBefore = datetime.datetime.utcnow() + datetime.timedelta(seconds=CalendarObject.CalendarObjectUpgradeWork.delay)
            yield self._txn.enqueue(CalendarObject.CalendarObjectUpgradeWork, resourceID=self._resourceID, notBefore=notBefore)

    class CalendarObjectUpgradeWork(AggregatedWorkItem, fromTable(schema.CALENDAR_OBJECT_UPGRADE_WORK)):
        """
        A L{WorkItem} that upgrades a calendar object's data if needed.
        """

        group = property(lambda self: (self.table.RESOURCE_ID == self.resourceID))
        default_priority = WORK_PRIORITY_LOW
        default_weight = WORK_WEIGHT_3
        delay = 60

        @inlineCallbacks
        def doWork(self):

            log.debug("Data upgrade calendar object with resource-id: {rid}", rid=self.resourceID)

            # Get the actual owned calendar object with this ID
            cobj = (yield CalendarStoreFeatures(self.transaction._store).calendarObjectWithID(self.transaction, self.resourceID))
            if cobj is None:
                returnValue(None)

            # Check for and do the update
            if cobj._dataversion != cobj._currentDataVersion:
                yield cobj.component(doUpdate=True)

    def moveValidation(self, destination, name):
        """
        Validate whether a move to the specified collection is allowed.

        @param destination: destination calendar collection
        @type destination: L{CalendarCollection}
        @param name: name of new resource
        @type name: C{str}
        """

        # Calendar to calendar moves are OK if the resource (viewer) owner is the same.
        # Use resourceOwnerPrincipal for this as that takes into account sharing such that the
        # returned principal relates to the URI path used to access the resource rather than the
        # underlying resource owner (sharee).
        sourceowner = self.calendar().viewerHome().uid()
        destowner = destination.viewerHome().uid()

        if not destination.isTrash() and sourceowner != destowner:
            msg = "Calendar-to-calendar moves with different homes are not supported."
            log.debug(msg)
            raise InvalidResourceMove(msg)

        # Calendar to calendar moves where Organizer is present are not OK if the owners are different.
        sourceowner = self.calendar().ownerHome().uid()
        destowner = destination.ownerHome().uid()

        if sourceowner != destowner and self._schedule_object:
            msg = "Calendar-to-calendar moves with an organizer property present and different owners are not supported."
            log.debug(msg)
            raise InvalidResourceMove(msg)

        # NB there is no need to do a UID lock and test here as we are moving an existing resource
        # with the already imposed constraint of unique UIDs.

        return succeed(None)

    @inlineCallbacks
    def toTrash(self):
        yield self._removeInternal(
            internal_state=ComponentRemoveState.NORMAL, useTrash=config.EnableTrashCollection
        )

    @inlineCallbacks
    def _reallyRemove(self):
        yield self._removeInternal(
            internal_state=ComponentRemoveState.NORMAL, useTrash=False
        )

    @inlineCallbacks
    def purge(self, implicitly=True):
        """
        Do an (optionally implicit) removal of this object resource, bypassing the trash.
        """
        yield self._removeInternal(
            internal_state=ComponentRemoveState.NORMAL if implicitly else ComponentRemoveState.NORMAL_NO_IMPLICIT, useTrash=False
        )

    @inlineCallbacks
    def _removeInternal(
        self, internal_state=ComponentRemoveState.NORMAL, useTrash=False
    ):

        isinbox = self._calendar.isInbox()

        # Pre-flight scheduling operation
        scheduler = None
        if not isinbox and internal_state == ComponentRemoveState.NORMAL:
            # Get data we need for implicit scheduling
            calendar = (yield self.componentForUser())
            scheduler = ImplicitScheduler(logItems=self._txn.logItems)
            # Cannot do implicit in sharee's shared calendar
            if not self.calendar().owned():
                scheduler.setSchedulingNotAllowed(
                    ShareeAllowedError,
                    "Sharee's cannot schedule",
                )

            do_implicit_action, _ignore = (yield scheduler.testImplicitSchedulingDELETE(
                self.calendar(),
                self,
                calendar,
                internal_request=(internal_state != ComponentUpdateState.NORMAL),
            ))
            if do_implicit_action:
                yield NamedLock.acquire(self._txn, "ImplicitUIDLock:{0}".format(hashlib.md5(calendar.resourceUID()).hexdigest(),))

        if isinbox:
            useTrash = False
        else:
            calendar = (yield self.componentForUser())
            status = calendar.mainComponent().getProperty("STATUS")
            if status is not None:
                status = status.strvalue()
                if status == "CANCELLED":
                    useTrash = False

        if useTrash:
            # Always remove the group attendee link to prevent trashed items from being reconciled when a group changes
            yield GroupAttendeeRecord.deletesimple(self._txn, resourceID=self._resourceID)

            yield super(CalendarObject, self).toTrash()

        else:
            # Need to also remove attachments
            if internal_state != ComponentRemoveState.INTERNAL:
                if self._dropboxID:
                    yield DropBoxAttachment.resourceRemoved(self._txn, self._resourceID, self._dropboxID)
                yield ManagedAttachment.resourceRemoved(self._txn, self._resourceID)

            yield super(CalendarObject, self)._reallyRemove()

        # Do scheduling
        if scheduler is not None:
            yield scheduler.doImplicitScheduling()

    def removeNotifyCategory(self):
        """
        Indicates what category to use when determining the priority of push
        notifications when this object is removed.

        @returns: The "inbox" category if this object is in the inbox, otherwise
            the "default" category
        @rtype: L{ChangeCategory}
        """
        return (ChangeCategory.inbox if self._calendar.isInbox() else
                ChangeCategory.default)

    @classproperty
    def _recurrenceMinMaxByIDQuery(cls):  # @NoSelf
        """
        DAL query to load RECURRANCE_MIN, RECURRANCE_MAX via an object's resource ID.
        """
        co = cls._objectSchema
        return Select(
            [co.RECURRANCE_MIN, co.RECURRANCE_MAX, ],
            From=co,
            Where=co.RESOURCE_ID == Parameter("resourceID"),
        )

    @inlineCallbacks
    def recurrenceMinMax(self, txn=None):
        """
        Get the RECURRANCE_MIN, RECURRANCE_MAX value from the database. Occasionally we might need to do an
        update to time-range data via a separate transaction, so we allow that to be passed in.

        @return: L{DateTime} result
        """
        # Setup appropriate txn
        txn = txn if txn is not None else self._txn

        rMin, rMax = (
            yield self._recurrenceMinMaxByIDQuery.on(txn, resourceID=self._resourceID)
        )[0]
        returnValue((
            parseSQLDateToPyCalendar(rMin) if rMin is not None else None,
            parseSQLDateToPyCalendar(rMax) if rMax is not None else None,
        ))

    @classproperty
    def _instanceQuery(cls):  # @NoSelf
        """
        DAL query to load TIME_RANGE data via an object's resource ID.
        """
        tr = schema.TIME_RANGE
        return Select(
            [
                tr.INSTANCE_ID,
                tr.START_DATE,
                tr.END_DATE,
            ],
            From=tr,
            Where=tr.CALENDAR_OBJECT_RESOURCE_ID == Parameter("resourceID"),
        )

    @inlineCallbacks
    def instances(self, txn=None):
        """
        Get the set of instances from the database.

        @return: C{list} result
        """
        # Setup appropriate txn
        txn = txn if txn is not None else self._txn

        instances = (
            yield self._instanceQuery.on(txn,
                                         resourceID=self._resourceID)
        )
        returnValue(tuple(instances))

    @inlineCallbacks
    def organizer(self):
        returnValue((yield self.component()).getOrganizer())

    def getMetadata(self):
        metadata = {}
        metadata["accessMode"] = self.accessMode
        metadata["isScheduleObject"] = self.isScheduleObject
        metadata["scheduleTag"] = self.scheduleTag
        metadata["scheduleEtags"] = self.scheduleEtags
        metadata["hasPrivateComment"] = self.hasPrivateComment
        return metadata

    def _get_accessMode(self):
        return accesstype_to_accessMode[self._access]

    def _set_accessMode(self, value):
        self._access = accessMode_to_type[value]

    accessMode = property(_get_accessMode, _set_accessMode)

    def _get_isScheduleObject(self):
        return self._schedule_object

    def _set_isScheduleObject(self, value):
        self._schedule_object = value

    isScheduleObject = property(_get_isScheduleObject, _set_isScheduleObject)

    def _get_scheduleTag(self):
        return self._schedule_tag

    def _set_scheduleTag(self, value):
        self._schedule_tag = value

    scheduleTag = property(_get_scheduleTag, _set_scheduleTag)

    def _get_scheduleEtags(self):
        return tuple(self._schedule_etags.split(",")) if self._schedule_etags else ()

    def _set_scheduleEtags(self, value):
        self._schedule_etags = ",".join(value) if value else ""

    scheduleEtags = property(_get_scheduleEtags, _set_scheduleEtags)

    def _get_hasPrivateComment(self):
        return self._private_comments

    def _set_hasPrivateComment(self, value):
        self._private_comments = value

    hasPrivateComment = property(_get_hasPrivateComment, _set_hasPrivateComment)

    @inlineCallbacks
    def resourceCheckAttachments(self, component, inserting=False):
        """
        A component is being changed or added. Check any ATTACH properties that may be present
        to verify they are owned by the organizer/owner of the resource. Strip any that are invalid.

        @param component: calendar component about to be stored
        @type component: L{Component}
        """

        # Check count limit
        newCount = component.maxAttachmentsPerInstance()
        if newCount > config.MaximumAttachmentsPerInstance:
            if inserting:
                # New resources never allowed to go over the limit
                raise TooManyAttachments
            else:
                # Existing resources cannot have their count increased if already over the limit
                oldcomponent = (yield self.component())
                oldCount = oldcomponent.maxAttachmentsPerInstance()
                if newCount > oldCount:
                    raise TooManyAttachments

        # Retrieve all ATTACH properties with a MANAGED-ID in new data
        newattached = collections.defaultdict(list)
        newattachments = component.getAllPropertiesInAnyComponent("ATTACH", depth=1,)
        for attachment in newattachments:
            managed_id = attachment.parameterValue("MANAGED-ID")
            if managed_id is not None:
                newattached[managed_id].append(attachment)

        # Retrieve all ATTACH properties with a MANAGED-ID in old data
        if not inserting:
            oldcomponent = (yield self.component())
            oldattached = collections.defaultdict(list)
            oldattachments = oldcomponent.getAllPropertiesInAnyComponent("ATTACH", depth=1,)
            for attachment in oldattachments:
                managed_id = attachment.parameterValue("MANAGED-ID")
                if managed_id is not None:
                    oldattached[managed_id].append(attachment)
        else:
            oldattached = collections.defaultdict(list)

        # Punt if no managed attachments
        if len(newattached) + len(oldattached) == 0:
            returnValue((None, None,))

        newattached_keys = set(newattached.keys())
        oldattached_keys = set(oldattached.keys())

        # Determine what was removed
        removed = set(oldattached_keys) - set(newattached_keys)

        # Make sure that clients don't accidently remove the parameters we care about
        if not inserting:
            for managed_id in list(removed):
                oldattachment = oldattached[managed_id][0]
                oldattachment_value = oldattachment.value()
                found_match = False
                for newattachment in newattachments:
                    if newattachment.value() == oldattachment_value:
                        newattachment.setParameter("MANAGED-ID", oldattachment.parameterValue("MANAGED-ID"))
                        newattachment.setParameter("FMTTYPE", oldattachment.parameterValue("FMTTYPE"))
                        newattachment.setParameter("FILENAME", oldattachment.parameterValue("FILENAME"))
                        newattachment.setParameter("SIZE", oldattachment.parameterValue("SIZE"))
                        found_match = True
                if found_match:
                    removed.remove(managed_id)

        # Determine what was added
        added = set(newattached_keys) - set(oldattached_keys)
        changed = {}
        for managed_id in added:
            changed[managed_id] = newattached[managed_id]

        if not self._dropboxID:
            self._dropboxID = str(uuid.uuid4())
        changes = yield self._addingManagedIDs(self._txn, self._parentCollection, self._dropboxID, changed, component.resourceUID())

        # Make sure existing data is not changed
        same = oldattached_keys & newattached_keys
        for managed_id in same:
            newattachment = newattached[managed_id]
            oldattachment = oldattached[managed_id][0]
            for newattachment in newattached[managed_id]:
                if newattachment != oldattachment:
                    newattachment.setParameter("FMTTYPE", oldattachment.parameterValue("FMTTYPE"))
                    newattachment.setParameter("FILENAME", oldattachment.parameterValue("FILENAME"))
                    newattachment.setParameter("SIZE", oldattachment.parameterValue("SIZE"))
                    newattachment.setValue(oldattachment.value())

        returnValue((changes, removed,))

    @inlineCallbacks
    def _addingManagedIDs(self, txn, parent, dropbox_id, attached, newuid):
        # Now check each managed-id
        changes = []
        for managed_id, attachments in attached.items():

            # Must be in the same home as this resource
            details = (yield ManagedAttachment.usedManagedID(txn, managed_id))
            if len(details) == 0:
                raise AttachmentStoreValidManagedID
            homes = set()
            uids = set()
            for home_id, _ignore_resource_id, uid in details:
                homes.add(home_id)
                uids.add(uid)
            if len(homes) != 1:
                # This is a bad store error - there should be only one home associated with a managed-id
                raise InternalDataStoreError

            # Policy:
            #
            # 1. If Managed-ID is re-used in a resource with the same UID - it is fine - just rewrite the details
            # 2. If Managed-ID is re-used in a different resource but owned by the same user - just rewrite the details
            # 3. Otherwise, strip off the managed-id property and treat as unmanaged.

            # 1. UID check
            if newuid in uids:
                yield self._syncAttachmentProperty(txn, managed_id, dropbox_id, attachments)

            # 2. Same home
            elif home_id == parent.ownerHome()._resourceID:

                # Record the change as we will need to add a new reference to this attachment
                yield self._syncAttachmentProperty(txn, managed_id, dropbox_id, attachments)
                changes.append(managed_id)

            else:
                self._stripAttachmentProperty(attachments)

        returnValue(changes)

    @inlineCallbacks
    def _syncAttachmentProperty(self, txn, managed_id, dropbox_id, attachments):
        """
        Make sure the supplied set of attach properties are all sync'd with the current value of the
        matching managed-id attachment.

        @param managed_id: Managed-Id to sync with
        @type managed_id: C{str}
        @param attachments: list of attachment properties
        @type attachments: C{list} of L{twistedcaldav.ical.Property}
        """

        # Try to load any attachment directly referenced by this object, otherwise load one referenced by some
        # any other - they are all the same.
        new_attachment = (yield ManagedAttachment.load(txn, self._resourceID, managed_id))
        if new_attachment is None:
            new_attachment = (yield ManagedAttachment.load(txn, None, managed_id))
        new_attachment._objectDropboxID = dropbox_id
        for attachment in attachments:
            yield new_attachment.updateProperty(attachment)

    def _stripAttachmentProperty(self, attachments):
        """
        Strip off managed-id related properties from an attachment.
        """
        for attachment in attachments:
            attachment.removeParameter("MANAGED-ID")
            attachment.removeParameter("MTAG")

    @inlineCallbacks
    def copyResourceAttachments(self, attached):
        """
        Copy an attachment reference for some other resource and link it to this resource.

        @param attached: managed id for the attachments to copy
        @type attached: C{str}
        """
        for managed_id in attached:
            yield ManagedAttachment.copyManagedID(self._txn, managed_id, self._resourceID)

    @inlineCallbacks
    def removeResourceAttachments(self, attached):
        """
        Remove an attachment reference for this resource.

        @param attached: managed-ids to remove
        @type attached: C{tuple}
        """
        for managed_id in attached:
            yield self.removeManagedAttachmentWithID(managed_id)

    @inlineCallbacks
    def _checkValidManagedAttachmentChange(self):
        """
        Make sure a managed attachment add, update or remover operation is valid.
        """

        # Only allow organizers to manipulate managed attachments for now
        calendar = (yield self.componentForUser())
        scheduler = ImplicitScheduler(logItems=self._txn.logItems)
        is_attendee = (yield scheduler.testAttendeeEvent(self.calendar(), self, calendar,))
        if is_attendee:
            raise InvalidAttachmentOperation("Attendees are not allowed to manipulate managed attachments")

    CONTROL_CHARACTER_FILTER = "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0A\x0B\x0C\x0D\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\x1B\x1C\x1D\x1E\x1F"

    @inlineCallbacks
    def addAttachment(self, rids, content_type, filename, stream):
        """
        Add a new managed attachment to this calendar object.

        @param rids: list of recurrence-ids for components to add to, or C{None} to add to all.
        @type rids: C{str} or C{None}
        @param content_type: the MIME media type/subtype of the attachment
        @type content_type: L{MimeType}
        @param filename: the name for the attachment
        @type filename: C{str}
        @param stream: the stream to read attachment data from
        @type stream: L{IStream}
        """

        # Check validity of request
        yield self._checkValidManagedAttachmentChange()

        # Don't exceed the attachment count limit
        calendar = (yield self.componentForUser())
        if calendar.maxAttachmentsPerInstance() >= config.MaximumAttachmentsPerInstance:
            raise TooManyAttachments

        # Protect against invalid file names
        if isinstance(filename, unicode):
            filename = filename.encode("utf-8")
        filename = filename.translate(None, CalendarObject.CONTROL_CHARACTER_FILTER)

        # First write the data stream

        # We need to know the resource_ID of the home collection of the owner
        # (not sharee) of this event
        try:
            attachment = (yield self.createManagedAttachment())
            t = attachment.store(content_type, filename)
            yield readStream(stream, t.write)
        except Exception as e:
            self.log.error("Unable to store attachment: {ex}", ex=e)
            raise AttachmentStoreFailed
        yield t.loseConnection()

        if not self._dropboxID:
            self._dropboxID = str(uuid.uuid4())
        attachment._objectDropboxID = self._dropboxID

        # Now try and adjust the actual calendar data.
        # NB We need a copy of the original calendar data as implicit scheduling will need to compare that to
        # the original in order to detect changes that would case scheduling.
        calendar = calendar.duplicate()

        attach, location = (yield attachment.attachProperty())
        if rids is None:
            calendar.addPropertyToAllComponents(attach)
        else:
            # TODO - per-recurrence attachments
            pass

        # Here is where we want to store data implicitly
        yield self._setComponentInternal(calendar, internal_state=ComponentUpdateState.ATTACHMENT_UPDATE)

        returnValue((attachment, location,))

    @inlineCallbacks
    def updateAttachment(self, managed_id, content_type, filename, stream):
        """
        Update a managed attachment in this calendar object.

        @param managed_id: the attachment's managed-id
        @type managed_id: C{str}
        @param content_type: the new MIME media type/subtype of the attachment
        @type content_type: L{MIMEType}
        @param filename: the new name for the attachment
        @type filename: C{str}
        @param stream: the stream to read new attachment data from
        @type stream: L{IStream}
        """

        # Check validity of request
        yield self._checkValidManagedAttachmentChange()

        # Protect against invalid file names
        if isinstance(filename, unicode):
            filename = filename.encode("utf-8")
        filename = filename.translate(None, CalendarObject.CONTROL_CHARACTER_FILTER)

        # First check the supplied managed-id is associated with this resource
        cobjs = (yield ManagedAttachment.referencesTo(self._txn, managed_id))
        if self._resourceID not in cobjs:
            raise AttachmentStoreValidManagedID

        # Next write the data stream to existing attachment

        # We need to know the resource_ID of the home collection of the owner
        # (not sharee) of this event
        try:
            # Check that this is a proper update
            oldattachment = (yield self.attachmentWithManagedID(managed_id))
            if oldattachment is None:
                self.log.error("Missing managed attachment even though ATTACHMENT_CALENDAR_OBJECT indicates it is present: {mid}", mid=managed_id)
                raise AttachmentStoreFailed

            # We actually create a brand new attachment object for the update, but with the same managed-id. That way, other resources
            # referencing the old attachment data will still see that.
            attachment = (yield self.updateManagedAttachment(managed_id, oldattachment))
            t = attachment.store(content_type, filename)
            yield readStream(stream, t.write)
        except Exception as e:
            self.log.error("Unable to store attachment: {ex}", ex=e)
            raise AttachmentStoreFailed
        yield t.loseConnection()

        # Now try and adjust the actual calendar data.
        # NB We need a copy of the original calendar data as implicit scheduling will need to compare that to
        # the original in order to detect changes that would case scheduling.
        calendar = (yield self.componentForUser())
        calendar = calendar.duplicate()

        attach, location = (yield attachment.attachProperty())
        calendar.replaceAllPropertiesWithParameterMatch(attach, "MANAGED-ID", managed_id)

        # Here is where we want to store data implicitly
        yield self._setComponentInternal(calendar, internal_state=ComponentUpdateState.ATTACHMENT_UPDATE)

        returnValue((attachment, location,))

    @inlineCallbacks
    def removeAttachment(self, rids, managed_id):
        """
        Remove a managed attachment from this calendar object.

        @param rids: list of recurrence-ids for components to add to, or C{None} to add to all.
        @type rids: C{str} or C{None}
        @param managed_id: the attachment's managed-id
        @type managed_id: C{str}
        """

        # Check validity of request
        yield self._checkValidManagedAttachmentChange()

        # First check the supplied managed-id is associated with this resource
        cobjs = (yield ManagedAttachment.referencesTo(self._txn, managed_id))
        if self._resourceID not in cobjs:
            raise AttachmentStoreValidManagedID

        # Now try and adjust the actual calendar data.
        # NB We need a copy of the original calendar data as implicit scheduling will need to compare that to
        # the original in order to detect changes that would case scheduling.
        all_removed = False
        calendar = (yield self.componentForUser())
        calendar = calendar.duplicate()

        if rids is None:
            calendar.removeAllPropertiesWithParameterMatch("ATTACH", "MANAGED-ID", managed_id)
            all_removed = True
        else:
            # TODO: per-recurrence removal
            pass

        # Here is where we want to store data implicitly
        yield self._setComponentInternal(calendar, internal_state=ComponentUpdateState.ATTACHMENT_UPDATE)

        # Remove it - this will take care of actually removing it from the store if there are
        # no more references to the attachment
        if all_removed:
            yield self.removeManagedAttachmentWithID(managed_id)

    @inlineCallbacks
    def convertAttachments(self, oldattachment, newattachment):
        """
        Convert ATTACH properties in the calendar data from a dropbox attachment to a managed attachment.
        This is only used when migrating from dropbox to managed attachments. The ATTACH/ATTACH_CALENDAR_OBJECT
        DB tables have already been updated to reflect the new managed attachment entry, however the CALENDAR_OBJECT.
        DROPBOX_ID column has not.

        @param oldattachment: the old dropbox attachment being converted
        @type oldattachment: L{DropBoxAttachment}
        @param newattachment: the new managed attachment
        @type newattachment: L{ManagedAttachment}
        """

        # Scan each component looking for an ATTACH matching the old dropbox, remove
        # that and add a new managed ATTACH property
        cal = (yield self.component())
        for component in cal.subcomponents():
            attachments = component.properties("ATTACH")
            removed = False
            for attachment in tuple(attachments):
                if attachment.value().endswith("/dropbox/{0}/{1}".format(
                    urllib.quote(oldattachment.dropboxID()),
                    urllib.quote(oldattachment.name()),
                )):
                    component.removeProperty(attachment)
                    removed = True
            if removed:
                attach, _ignore_location = (yield newattachment.attachProperty())
                component.addProperty(attach)
            component.removeAllPropertiesWithName("X-APPLE-DROPBOX")

        # Write the component back (and no need to re-index as we have not
        # changed any timing properties in the calendar data).
        cal.noInstanceIndexing = True
        yield self._setComponentInternal(cal, internal_state=ComponentUpdateState.RAW)

    @inlineCallbacks
    def createManagedAttachment(self):

        # We need to know the resource_ID of the home collection of the owner
        # (not sharee) of this event
        ownerHomeID = (yield self._parentCollection.ownerHome().id())
        managedID = str(uuid.uuid4())
        returnValue((
            yield ManagedAttachment.create(
                self._txn, managedID, ownerHomeID, self._resourceID,
            )
        ))

    @inlineCallbacks
    def updateManagedAttachment(self, managedID, oldattachment):

        # We need to know the resource_ID of the home collection of the owner
        # (not sharee) of this event
        ownerHomeID = (yield self._parentCollection.ownerHome().id())
        returnValue((
            yield ManagedAttachment.update(
                self._txn, managedID, ownerHomeID, self._resourceID, oldattachment._attachmentID,
            )
        ))

    def attachmentWithManagedID(self, managed_id):
        return ManagedAttachment.load(self._txn, self._resourceID, managed_id)

    @inlineCallbacks
    def removeManagedAttachmentWithID(self, managed_id):
        attachment = (yield self.attachmentWithManagedID(managed_id))
        if attachment is not None:
            yield attachment.removeFromResource(self._resourceID)

    @inlineCallbacks
    def managedAttachmentList(self):
        """
        Get a list of managed attachments where the names returned are for the last path segment
        of the attachment URI.
        """
        at = Attachment._attachmentSchema
        attco = Attachment._attachmentLinkSchema
        rows = (yield Select(
            [attco.MANAGED_ID, at.PATH, ],
            From=attco.join(at, attco.ATTACHMENT_ID == at.ATTACHMENT_ID),
            Where=attco.CALENDAR_OBJECT_RESOURCE_ID == Parameter("resourceID")
        ).on(self._txn, resourceID=self._resourceID))
        returnValue([ManagedAttachment.lastSegmentOfUriPath(row[0], row[1]) for row in rows])

    @inlineCallbacks
    def managedAttachmentRetrieval(self, name):
        """
        Return a managed attachment specified by the last path segment of the attachment URI.
        """

        # Scan all the associated attachments for the one that matches
        at = Attachment._attachmentSchema
        attco = Attachment._attachmentLinkSchema
        rows = (yield Select(
            [attco.MANAGED_ID, at.PATH, ],
            From=attco.join(at, attco.ATTACHMENT_ID == at.ATTACHMENT_ID),
            Where=attco.CALENDAR_OBJECT_RESOURCE_ID == Parameter("resourceID")
        ).on(self._txn, resourceID=self._resourceID))

        for att_managed_id, att_name in rows:
            if ManagedAttachment.lastSegmentOfUriPath(att_managed_id, att_name) == name:
                attachment = (yield self.attachmentWithManagedID(att_managed_id))
                returnValue(attachment)
        returnValue(None)

    @inlineCallbacks
    def createAttachmentWithName(self, name):

        # We need to know the resource_ID of the home collection of the owner
        # (not sharee) of this event
        ownerHomeID = (yield self._parentCollection.ownerHome().id())
        dropboxID = (yield self.dropboxID())
        returnValue((
            yield DropBoxAttachment.create(
                self._txn, dropboxID, name, ownerHomeID,
            )
        ))

    @inlineCallbacks
    def removeAttachmentWithName(self, name):
        attachment = (yield self.attachmentWithName(name))
        yield attachment.remove()

    def attachmentWithName(self, name):
        return DropBoxAttachment.load(self._txn, self._dropboxID, name)

    def attendeesCanManageAttachments(self):
        return self._attachment == _ATTACHMENTS_MODE_WRITE

    dropboxID = dropboxIDFromCalendarObject

    _attachmentsQuery = Select(
        [schema.ATTACHMENT.PATH],
        From=schema.ATTACHMENT,
        Where=schema.ATTACHMENT.DROPBOX_ID == Parameter('dropboxID')
    )

    @inlineCallbacks
    def attachments(self):
        if self._dropboxID:
            rows = yield self._attachmentsQuery.on(
                self._txn,
                dropboxID=self._dropboxID,
            )
            result = []
            for row in rows:
                result.append((yield self.attachmentWithName(row[0])))
            returnValue(result)
        else:
            returnValue(())

    def initPropertyStore(self, props):
        # Setup peruser special properties - these are hard-coded for now as clients are not expected
        # to be using properties on resources - but we do use one special "live" property which we
        # keep in the propstore.
        props.setSpecialProperties(
            (
            ),
            (
                PropertyName.fromElement(customxml.ScheduleChanges),
            ),
            (),
        )

    # IDataStoreObject
    def contentType(self):
        """
        The content type of Calendar objects is text/calendar.
        """
        return MimeType.fromString("text/calendar; charset=utf-8")

    @inlineCallbacks
    def checkSplit(self, txn=None):
        """
        Determine if the calendar data needs to be split, and enqueue a split work item if needed. Note we may
        need to do this in some other transaction.
        """

        if config.Scheduling.Options.Splitting.Enabled:
            will = (yield self.willSplit())
            if will:
                notBefore = datetime.datetime.utcnow() + datetime.timedelta(seconds=config.Scheduling.Options.Splitting.Delay)
                if txn is None:
                    txn = self._txn
                work = (yield txn.enqueue(CalendarObject.CalendarObjectSplitterWork, resourceID=self._resourceID, notBefore=notBefore))

                # _workItems is used during unit testing only, to track the created work
                if not hasattr(self, "_workItems"):
                    self._workItems = []
                self._workItems.append(work)

    @inlineCallbacks
    def willSplit(self):
        """
        Determine if the calendar data needs to be split as per L{iCalSplitter}.
        """

        splitter = iCalSplitter(config.Scheduling.Options.Splitting.Size, config.Scheduling.Options.Splitting.PastDays)
        component = (yield self.component())

        # Do some sanity checks first

        # Organizer either missing or must be valid
        organizer = component.getOrganizer()
        if organizer is not None:
            # Must be valid for scheduling
            organizerAddress = (yield calendarUserFromCalendarUserAddress(organizer, self._txn))
            if organizerAddress is None:
                returnValue(False)

            if organizerAddress.hosted():
                # Must be enabled and organizer
                if not organizerAddress.record.enabledAsOrganizer():
                    returnValue(False)

                # Cannot be the attendee
                if organizerAddress.record.uid != self.calendar().ownerHome().uid():
                    returnValue(False)

        will_split, _ignore_fullyInFuture = splitter.willSplit(component)
        returnValue(will_split)

    @inlineCallbacks
    def timeoutSplit(self):
        """
        A txn timeout occurred. Check to see if it is possible to split this event and if so schedule that to occur
        as the timeout might be the result of the resource being too large and doing a split here will allow a
        subsequent operation to succeed since the split can reduce the size.
        """

        # Can only do if cached data exists
        if self._cachedComponent:
            txn = self.transaction().store().newTransaction(label="Timeout checkSplit")
            yield self.checkSplit(txn=txn)
            yield txn.commit()

    @inlineCallbacks
    def splitAt(self, rid, pastUID=None):
        """
        User initiated split. We need to verify it is OK to do so first. We will allow any recurring item to
        be split, but will not allow attendees to split invites.

        @param rid: the date-time where the split should occur. This need not be a specific instance
            date-time - this method will choose the next instance on or after this value.
        @type rid: L{DateTime}
        @param pastuid: the UID of the new (past) resource to be created
        @type pastuid: L{str}
        """

        # Cannot create one with the same UID as this
        if pastUID and pastUID == self._uid:
            raise InvalidSplit("Cannot split an event and re-use the same UID.")

        # Must be recurring
        component = yield self.component()
        if not component.isRecurring():
            raise InvalidSplit("Cannot split a non-recurring event.")

        # Cannot be attendee
        organizer = component.getOrganizer()
        organizerAddress = (yield calendarUserFromCalendarUserAddress(organizer, self._txn)) if organizer else None
        if organizer is not None and organizerAddress.record.uid != self.calendar().ownerHome().uid():
            raise InvalidSplit("Only organizers can split events.")

        # rid value type must match
        dtstart = component.mainComponent().propertyValue("DTSTART")
        if dtstart.isDateOnly():
            if not rid.isDateOnly():
                # We ought to reject this but for now we will fix it
                rid.setDateOnly(True)
        elif dtstart.floating():
            if not rid.floating() or rid.isDateOnly():
                raise InvalidSplit("rid parameter value type must match DTSTART value type.")
        else:
            if rid.floating():
                raise InvalidSplit("rid parameter value type must match DTSTART value type.")

        # Determine valid split point
        splitter = iCalSplitter(1024, 14)
        rid = splitter.whereSplit(component, break_point=rid, allow_past_the_end=False)
        if rid is None:
            raise InvalidSplit("Cannot find a suitable recurrence-id to split at.")

        # Do split and return new resource
        try:
            olderObject = yield self.split(rid=rid, olderUID=pastUID)
        except (ObjectResourceNameAlreadyExistsError, UIDExistsError, UIDExistsElsewhereError):
            raise InvalidSplit("Chosen UID exists elsewhere.")
        except Exception as e:
            log.error("Unknown split exception: {ex}", ex=str(e))
            raise InvalidSplit("Unknown error.")
        returnValue(olderObject)

    @inlineCallbacks
    def split(self, onlyThis=False, rid=None, olderUID=None, coercePartstatsInExistingResource=False, splitter=None):
        """
        Split this and all matching UID calendar objects as per L{iCalSplitter}.

        We need to handle scheduling with non-hosted users here. Here is what we will do:

        1) Send an iTIP message for the original event (in its now future-truncated state) and
        include a special X- parameter in the iTIP message to indicate a split was done and
        what the RECURRENCE-ID was where the split was made. This will allow "smart" clients/servers
        to spot the split action and apply that locally upon receipt and processing of the iTIP
        message. That way they get to preserve the existing per-user data for the old instances. Other
        clients/servers will just apply the change via normal iTIP processing.

        2) Send an iTIP message for the new event (which will be for the old instances). "Smart"
        clients that already got and processed the message from #1 will simply apply this on top
        of their split copy - it should be identical, part from per-user data, so it will apply
        cleanly. We can include an X- headers to indicate the split R-ID so "smart" clients/servers
        can simply ignore this message.

        @param onlyThis: if L{True} then only this L{CalendarObject} will be split (the organizer copy only),
            if L{False}, then organizer and attendee copies are split (this is the normal behavior).
        @type onlyThis: L{bool}
        @param rid: the date-time where the split should occur. This needs to be a specific instance
            date-time or L{None} to have one automatically determined. To determine a valid value,
            given an arbitrary date-time, use the L{iCalSplitter.willSplit) method (which is used
            in L{CalendarObject.splitAt}).
        @type rid: L{DateTime}
        @param olderUID: sets the iCalendar UID to be used in the new resource created during the split.
            If L{None} a UUID is generated and used.
        @type olderUID: L{str}
        """

        # First job is to grab a UID lock on this entire series of events
        yield self._lockUID(self._uid, internal_state=ComponentUpdateState.SPLIT_OWNER)

        # Find all other calendar objects on this server with the same UID
        if onlyThis:
            resources = ()
        else:
            resources = (yield CalendarStoreFeatures(self._txn._store).calendarObjectDetailsWithUID(self._txn, self._uid))

        if splitter is None:
            splitter = iCalSplitter(config.Scheduling.Options.Splitting.Size, config.Scheduling.Options.Splitting.PastDays)

        # Determine the recurrence-id of the split and create a new UID for it
        calendar = (yield self.component())
        if rid is None:
            rid = splitter.whereSplit(calendar)
        newerUID = calendar.resourceUID()
        if olderUID is None:
            olderUID = str(uuid.uuid4())
            olderResourceName = "{0}.ics".format(olderUID)
        else:
            olderResourceName = "{0}.ics".format(olderUID.encode("base64")[:-1].rstrip("="))

        # Now process this resource, but do implicit scheduling for attendees not hosted on this server.
        # We need to do this before processing attendee copies.
        calendar_old, calendar_new = splitter.split(calendar, rid=rid, olderUID=olderUID)
        if calendar_new.getOrganizer() is not None:
            calendar_new.bumpiTIPInfo(oldcalendar=calendar, doSequence=True)
            calendar_old.bumpiTIPInfo(oldcalendar=None, doSequence=True)

        # If the split results in nothing either resource, then there is really nothing
        # to actually split
        if calendar_new.mainType() is None or calendar_old.mainType() is None:
            returnValue(None)

        # Store changed data
        yield self._setComponentInternal(calendar_new, internal_state=ComponentUpdateState.SPLIT_OWNER, split_details=(rid, olderUID, True, coercePartstatsInExistingResource))
        olderObject = yield self.calendar()._createCalendarObjectWithNameInternal(
            olderResourceName,
            calendar_old,
            ComponentUpdateState.SPLIT_OWNER,
            options={"dropboxID": olderUID},
            split_details=(rid, newerUID, False, False)
        )

        # Reconcile trash state
        if self.isInTrash():
            yield olderObject._updateToTrashQuery.on(
                olderObject._txn, originalCollection=self._original_collection, trashed=self._trashed, resourceID=olderObject._resourceID
            )

        # Split each one - but not this resource
        lastHome = None
        for homeID, parentID, resourceID in sorted(resources):

            # Each time the home changes, wipe it from the txn cache so that
            # we do not accumulate them in memory whilst we go through this loop
            if lastHome is not None:
                if lastHome.id() != homeID:
                    self._txn.cleanHomeCache(lastHome)

            if resourceID == self._resourceID:
                continue
            lastHome = home = (yield self._txn.calendarHomeWithResourceID(homeID))
            if home is None:
                continue
            parent = (yield home.childWithID(parentID))
            if parent is None:
                continue
            resource = (yield parent.objectResourceWithID(resourceID))
            if resource is None:
                continue
            yield resource.splitForAttendee(
                rid,
                olderUID,
                coercePartstatsInExistingResource=coercePartstatsInExistingResource
            )
            resource = None
            parent = None
            home = None

        # Clean up the last one
        if lastHome is not None:
            self._txn.cleanHomeCache(lastHome)
            lastHome = None

        returnValue(olderObject)

    @inlineCallbacks
    def splitForAttendee(self, rid=None, olderUID=None, coercePartstatsInExistingResource=False):
        """
        Split this attendee resource as per L{split}. Note this is also used on any Organizer inbox items.
        Also, for inbox items, we are not protected by the ImplicitUID lock - it is possible that the inbox
        resource gets deleted whilst we are iterating the entire set of UIDs, so we need to handle a
        L{ConcurrentModification} error here by ignoring it.
        """
        splitter = iCalSplitter(config.Scheduling.Options.Splitting.Size, config.Scheduling.Options.Splitting.PastDays)
        try:
            ical = (yield self.component())
        except ConcurrentModification:
            # Resource was deleted between the time we looked it up and now - this is OK,
            # We can simply ignore it as there is nothing left to split
            returnValue(None)

        ical_old, ical_new = splitter.split(ical, rid=rid, olderUID=olderUID)
        ical_new.bumpiTIPInfo(oldcalendar=ical, doSequence=True)
        ical_old.bumpiTIPInfo(oldcalendar=None, doSequence=True)

        # Store changed data
        if ical_new.mainType() is not None:

            if coercePartstatsInExistingResource:
                organizer = ical_new.getOrganizer()
                for attendee in ical_new.getAllAttendeeProperties():
                    if attendee.parameterValue("SCHEDULE-AGENT", "SERVER").upper() == "SERVER" and attendee.hasParameter("PARTSTAT") and attendee.value() != organizer:
                        attendee.setParameter("PARTSTAT", "NEEDS-ACTION")

            yield self._setComponentInternal(ical_new, internal_state=ComponentUpdateState.SPLIT_ATTENDEE)
        else:
            # The split removed all components from this object - remove it
            yield self._removeInternal(internal_state=ComponentRemoveState.INTERNAL, useTrash=False)

        # Create a new resource and store its data (but not if the parent is "inbox", or if it is empty)
        if not self.calendar().isInbox() and ical_old.mainType() is not None:
            olderObject = yield self.calendar()._createCalendarObjectWithNameInternal(
                "{0}.ics".format(olderUID,),
                ical_old,
                ComponentUpdateState.SPLIT_ATTENDEE,
                options={"dropboxID": olderUID},
            )

            # Reconcile trash state
            if self.isInTrash():
                yield olderObject._updateToTrashQuery.on(
                    olderObject._txn, originalCollection=self._original_collection, trashed=self._trashed, resourceID=olderObject._resourceID
                )

    class CalendarObjectSplitterWork(WorkItem, fromTable(schema.CALENDAR_OBJECT_SPLITTER_WORK)):

        group = property(lambda self: (self.table.RESOURCE_ID == self.resourceID))
        default_priority = WORK_PRIORITY_LOW
        default_weight = WORK_WEIGHT_5

        @inlineCallbacks
        def doWork(self):

            log.debug("Splitting calendar object with resource-id: {rid}", rid=self.resourceID)

            # Delete all other work items with the same resourceID
            yield Delete(
                From=self.table,
                Where=self.group
            ).on(self.transaction)

            # Get the actual owned calendar object with this ID
            cobj = (yield CalendarStoreFeatures(self.transaction._store).calendarObjectWithID(self.transaction, self.resourceID))
            if cobj is None:
                returnValue(None)

            # Check it is still split-able
            will = (yield cobj.willSplit())

            if will:
                # Now do the spitting
                yield cobj.split()

    @inlineCallbacks
    def fromTrash(self):
        name = yield super(CalendarObject, self).fromTrash()

        caldata = yield self.componentForUser()
        organizer = caldata.getOrganizer()
        if organizer:
            # This is a scheduled event

            uid = self._parentCollection.viewerHome().uid()
            organizerAddress = yield calendarUserFromCalendarUserAddress(organizer, self._txn)

            if organizerAddress.record.uid == uid:
                # If the ORGANIZER is moving the event from trash
                splitter = iCalSplitter()
                willSplit, fullyInFuture = splitter.willSplit(caldata)
                if willSplit:
                    log.debug("Splitting scheduled event being recovered by organizer from trash")
                    yield self.split(
                        coercePartstatsInExistingResource=True,
                        splitter=splitter
                    )
                    # original resource is the ongoing one,
                    # the new resource is the past one

                    caldata = yield self.componentForUser()  # again, now that we've split
                    if (yield self.reconcileGroupAttendees(caldata)):
                        # changed
                        yield self._setComponentInternal(
                            caldata, inserting=True, updateSelf=True
                        )

                    else:
                        # Update the attendee's copy of the ongoing one
                        yield ImplicitScheduler().refreshAllAttendeesExceptSome(
                            self._txn,
                            self,
                        )
                else:
                    if fullyInFuture:
                        log.debug("Scheduled event being recovered by organizer from trash, fully in the future")
                        newdata = caldata.duplicate()
                        newdata.bumpiTIPInfo(doSequence=True)
                        for attendee in newdata.getAllAttendeeProperties():
                            if attendee.parameterValue("SCHEDULE-AGENT", "SERVER").upper() == "SERVER" and attendee.hasParameter("PARTSTAT") and attendee.value() != organizer:
                                attendee.setParameter("PARTSTAT", "NEEDS-ACTION")

                        yield self._setComponentInternal(
                            newdata, inserting=True, updateSelf=True
                        )

                    else:
                        # past
                        log.debug("Scheduled event being recovered by organizer from trash, fully in the past")
                        yield ImplicitScheduler().refreshAllAttendeesExceptSome(
                            self._txn,
                            self,
                        )

            else:
                # If an ATTENDEE is moving the event from trash
                log.debug("Scheduled event being recovered by attendee from trash")
                yield ImplicitScheduler().sendAttendeeReply(
                    self._txn,
                    self
                )
        else:
            log.debug("Recovered un-scheduled event from trash")

        returnValue(name)


class UnsupportedQuery(Exception):
    """
    Indicates the particular set of query parameters passed are not supported.
    """


class TrashCollection(Calendar):

    _childType = _CHILD_TYPE_TRASH

    def isTrash(self):
        return True

    @classproperty
    def _trashForCollectionQuery(cls):
        obj = cls._objectSchema
        return Select(
            [obj.RESOURCE_ID], From=obj,
            Where=(obj.ORIGINAL_COLLECTION == Parameter("resourceID")),
            OrderBy=obj.TRASHED,
            Ascending=False
        )

    @classproperty
    def _trashForCollectionStartingQuery(cls):
        obj = cls._objectSchema
        return Select(
            [obj.RESOURCE_ID], From=obj,
            Where=(
                obj.ORIGINAL_COLLECTION == Parameter("resourceID")).And(
                obj.TRASHED >= Parameter("start")),
            OrderBy=obj.TRASHED,
            Ascending=False
        )

    @classproperty
    def _trashForCollectionEndingQuery(cls):
        obj = cls._objectSchema
        return Select(
            [obj.RESOURCE_ID], From=obj,
            Where=(
                obj.ORIGINAL_COLLECTION == Parameter("resourceID")).And(
                obj.TRASHED <= Parameter("end")),
            OrderBy=obj.TRASHED,
            Ascending=False
        )

    @inlineCallbacks
    def trashForCollection(self, resourceID, start=None, end=None):

        if start is None and end is None:
            results = yield self._trashForCollectionQuery.on(
                self._txn, resourceID=resourceID
            )
        elif start is None and end is not None:
            results = yield self._trashForCollectionEndingQuery.on(
                self._txn, resourceID=resourceID, end=end
            )
        elif start is not None and end is None:
            results = yield self._trashForCollectionStartingQuery.on(
                self._txn, resourceID=resourceID, start=start
            )
        else:
            raise UnsupportedQuery()

        resources = []
        for (objectResourceID,) in results:
            resource = yield self.objectResourceWithID(objectResourceID)
            if resource is not None:
                resources.append(resource)
        returnValue(resources)


# Hook-up class relationships at the end after they have all been defined
from txdav.caldav.datastore.sql_external import CalendarHomeExternal, CalendarExternal, CalendarObjectExternal
CalendarHome._externalClass = CalendarHomeExternal
CalendarHome._childClass = Calendar
CommonHome._trashClass = TrashCollection
Calendar._externalClass = CalendarExternal
Calendar._objectResourceClass = CalendarObject
CalendarObject._externalClass = CalendarObjectExternal
