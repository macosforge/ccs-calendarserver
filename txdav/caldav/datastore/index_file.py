# -*- test-case-name: twistedcaldav.test.test_index -*-
##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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
CalDAV Index.

This API is considered private to static.py and is therefore subject to
change.
"""

__all__ = [
    "db_basename",
    "ReservationError",
    "MemcachedUIDReserver",
    "Index",
    "IndexSchedule",
]

import datetime
import time
import hashlib

try:
    import sqlite3 as sqlite
except ImportError:
    from pysqlite2 import dbapi2 as sqlite

from twisted.internet.defer import maybeDeferred, succeed

from twext.python.log import Logger

from txdav.caldav.datastore.query.builder import buildExpression
from txdav.caldav.datastore.query.filter import Filter
from txdav.common.datastore.query.filegenerator import sqllitegenerator
from txdav.common.icommondatastore import SyncTokenValidException, \
    ReservationError, IndexedSearchException

from twistedcaldav.dateops import pyCalendarToSQLTimestamp
from twistedcaldav.ical import Component
from twistedcaldav.sql import AbstractSQLDatabase
from twistedcaldav.sql import db_prefix
from twistedcaldav.instance import InvalidOverriddenInstanceError
from twistedcaldav.config import config
from twistedcaldav.memcachepool import CachePoolUserMixIn

from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from pycalendar.timezone import Timezone

log = Logger()

db_basename = db_prefix + "sqlite"
schema_version = "10"
collection_types = {"Calendar": "Regular Calendar Collection", "iTIP": "iTIP Calendar Collection"}

icalfbtype_to_indexfbtype = {
    "FREE": 'F',
    "BUSY": 'B',
    "BUSY-UNAVAILABLE": 'U',
    "BUSY-TENTATIVE": 'T',
}
indexfbtype_to_icalfbtype = dict([(v, k) for k, v in icalfbtype_to_indexfbtype.iteritems()])


class AbstractCalendarIndex(AbstractSQLDatabase):
    """
    Calendar collection index abstract base class that defines the apis for the index.
    This will be subclassed for the two types of index behaviour we need: one for
    regular calendar collections, one for schedule calendar collections.
    """
    log = Logger()

    def __init__(self, resource):
        """
        @param resource: the L{CalDAVResource} resource to
            index. C{resource} must be a calendar collection (ie.
            C{resource.isPseudoCalendarCollection()} returns C{True}.)
        """
        self.resource = resource
        db_filename = self.resource.fp.child(db_basename).path
        super(AbstractCalendarIndex, self).__init__(db_filename, False)

        self.resource._txn.postCommit(self._db_close)
        self.resource._txn.postAbort(self._db_close)

    def create(self):
        """
        Create the index and initialize it.
        """
        self._db()

    def reserveUID(self, uid):
        """
        Reserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is already reserved
        """
        raise NotImplementedError

    def unreserveUID(self, uid):
        """
        Unreserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is not reserved
        """
        raise NotImplementedError

    def isReservedUID(self, uid):
        """
        Check to see whether a UID is reserved.
        @param uid: the UID to check
        @return: True if C{uid} is reserved, False otherwise.
        """
        raise NotImplementedError

    def isAllowedUID(self, uid, *names):
        """
        Checks to see whether to allow an operation with adds the the specified
        UID is allowed to the index.  Specifically, the operation may not
        violate the constraint that UIDs must be unique, and the UID must not
        be reserved.
        @param uid: the UID to check
        @param names: the names of resources being replaced or deleted by the
            operation; UIDs associated with these resources are not checked.
        @return: True if the UID is not in the index and is not reserved,
            False otherwise.
        """
        raise NotImplementedError

    def resourceNamesForUID(self, uid):
        """
        Looks up the names of the resources with the given UID.
        @param uid: the UID of the resources to look up.
        @return: a list of resource names
        """
        names = self._db_values_for_sql("select NAME from RESOURCE where UID = :1", uid)

        #
        # Check that each name exists as a child of self.resource.  If not, the
        # resource record is stale.
        #
        resources = []
        for name in names:
            name_utf8 = name.encode("utf-8")
            if name is not None and self.resource.getChild(name_utf8) is None:
                # Clean up
                log.error("Stale resource record found for child {name} with UID {uid} in {rsrc!r}", name=name, uid=uid, resource=self.resource)
                self._delete_from_db(name, uid, False)
                self._db_commit()
            else:
                resources.append(name_utf8)

        return resources

    def resourceNameForUID(self, uid):
        """
        Looks up the name of the resource with the given UID.
        @param uid: the UID of the resource to look up.
        @return: If the resource is found, its name; C{None} otherwise.
        """
        result = None

        for name in self.resourceNamesForUID(uid):
            assert result is None, "More than one resource with UID %s in calendar collection %r" % (uid, self)
            result = name

        return result

    def resourceUIDForName(self, name):
        """
        Looks up the UID of the resource with the given name.
        @param name: the name of the resource to look up.
        @return: If the resource is found, the UID of the resource; C{None}
            otherwise.
        """
        uid = self._db_value_for_sql("select UID from RESOURCE where NAME = :1", name)

        return uid

    def componentTypeCounts(self):
        """
        Count each type of component.
        """
        return self._db_execute("select TYPE, COUNT(TYPE) from RESOURCE group by TYPE")

    def addResource(self, name, calendar, fast=False, reCreate=False):
        """
        Adding or updating an existing resource.
        To check for an update we attempt to get an existing UID
        for the resource name. If present, then the index entries for
        that UID are removed. After that the new index entries are added.
        @param name: the name of the resource to add.
        @param calendar: a L{Calendar} object representing the resource
            contents.
        @param fast: if C{True} do not do commit, otherwise do commit.
        """
        oldUID = self.resourceUIDForName(name)
        if oldUID is not None:
            self._delete_from_db(name, oldUID, False)
        self._add_to_db(name, calendar, reCreate=reCreate)
        if not fast:
            self._db_commit()

    def deleteResource(self, name):
        """
        Remove this resource from the index.
        @param name: the name of the resource to add.
        @param uid: the UID of the calendar component in the resource.
        """
        uid = self.resourceUIDForName(name)
        if uid is not None:
            self._delete_from_db(name, uid)
            self._db_commit()

    def resourceExists(self, name):
        """
        Determines whether the specified resource name exists in the index.
        @param name: the name of the resource to test
        @return: True if the resource exists, False if not
        """
        uid = self._db_value_for_sql("select UID from RESOURCE where NAME = :1", name)
        return uid is not None

    def resourcesExist(self, names):
        """
        Determines whether the specified resource name exists in the index.
        @param names: a C{list} containing the names of the resources to test
        @return: a C{list} of all names that exist
        """
        statement = "select NAME from RESOURCE where NAME in ("
        for ctr in (item[0] for item in enumerate(names)):
            if ctr != 0:
                statement += ", "
            statement += ":%s" % (ctr,)
        statement += ")"
        results = self._db_values_for_sql(statement, *names)
        return results

    def testAndUpdateIndex(self, minDate):
        # Find out if the index is expanded far enough
        names = self.notExpandedBeyond(minDate)
        # Actually expand recurrence max
        for name in names:
            self.log.info("Search falls outside range of index for {name} {date}", name=name, date=minDate)
            self.reExpandResource(name, minDate)

    def whatchanged(self, revision):

        results = [(name.encode("utf-8"), deleted) for name, deleted in self._db_execute("select NAME, DELETED from REVISIONS where REVISION > :1", revision)]
        results.sort(key=lambda x: x[1])

        changed = []
        deleted = []
        invalid = []
        for name, wasdeleted in results:
            if name:
                if wasdeleted == 'Y':
                    if revision:
                        deleted.append(name)
                else:
                    changed.append(name)
            else:
                raise SyncTokenValidException

        return (changed, deleted, invalid)

    def lastRevision(self):
        return self._db_value_for_sql(
            "select REVISION from REVISION_SEQUENCE"
        )

    def bumpRevision(self, fast=False):
        self._db_execute(
            """
            update REVISION_SEQUENCE set REVISION = REVISION + 1
            """,
        )
        self._db_commit()
        return self._db_value_for_sql(
            """
            select REVISION from REVISION_SEQUENCE
            """,
        )

    def indexedSearch(self, filter, useruid="", fbtype=False):
        """
        Finds resources matching the given qualifiers.
        @param filter: the L{Filter} for the calendar-query to execute.
        @return: an iterable of tuples for each resource matching the
            given C{qualifiers}. The tuples are C{(name, uid, type)}, where
            C{name} is the resource name, C{uid} is the resource UID, and
            C{type} is the resource iCalendar component type.
        """

        # Make sure we have a proper Filter element and get the partial SQL
        # statement to use.
        if isinstance(filter, Filter):
            if fbtype:
                # Lookup the useruid - try the empty (default) one if needed
                dbuseruid = self._db_value_for_sql(
                    "select PERUSERID from PERUSER where USERUID == :1",
                    useruid,
                )
            else:
                dbuseruid = ""

            qualifiers = sqlcalendarquery(filter, None, dbuseruid, fbtype)
            if qualifiers is not None:
                # Determine how far we need to extend the current expansion of
                # events. If we have an open-ended time-range we will expand one
                # year past the start. That should catch bounded recurrences - unbounded
                # will have been indexed with an "infinite" value always included.
                maxDate, isStartDate = filter.getmaxtimerange()
                if maxDate:
                    maxDate = maxDate.duplicate()
                    maxDate.setDateOnly(True)
                    if isStartDate:
                        maxDate += Duration(days=365)
                    self.testAndUpdateIndex(maxDate)
            else:
                # We cannot handle this filter in an indexed search
                raise IndexedSearchException()

        else:
            qualifiers = None

        # Perform the search
        if qualifiers is None:
            rowiter = self._db_execute("select NAME, UID, TYPE from RESOURCE")
        else:
            if fbtype:
                # For a free-busy time-range query we return all instances
                rowiter = self._db_execute(
                    "select DISTINCT RESOURCE.NAME, RESOURCE.UID, RESOURCE.TYPE, RESOURCE.ORGANIZER, TIMESPAN.FLOAT, TIMESPAN.START, TIMESPAN.END, TIMESPAN.FBTYPE, TIMESPAN.TRANSPARENT, TRANSPARENCY.TRANSPARENT" +
                    qualifiers[0],
                    *qualifiers[1]
                )
            else:
                rowiter = self._db_execute("select DISTINCT RESOURCE.NAME, RESOURCE.UID, RESOURCE.TYPE" + qualifiers[0], *qualifiers[1])

        # Check result for missing resources
        results = []
        for row in rowiter:
            name = row[0]
            if self.resource.getChild(name.encode("utf-8")):
                if fbtype:
                    row = list(row)
                    if row[9]:
                        row[8] = row[9]
                    del row[9]
                results.append(row)
            else:
                log.error(
                    "Calendar resource {name} is missing from {rsrc!r}. Removing from index.",
                    name=name, rsrc=self.resource,
                )
                self.deleteResource(name)

        return results

    def bruteForceSearch(self):
        """
        List the whole index and tests for existence, updating the index
        @return: all resources in the index
        """
        # List all resources
        rowiter = self._db_execute("select NAME, UID, TYPE from RESOURCE")

        # Check result for missing resources:

        results = []
        for row in rowiter:
            name = row[0]
            if self.resource.getChild(name.encode("utf-8")):
                results.append(row)
            else:
                log.error(
                    "Calendar resource {name} is missing from {rsrc!r}. Removing from index.",
                    name=name, rsrc=self.resource,
                )

        return results

    def _db_version(self):
        """
        @return: the schema version assigned to this index.
        """
        return schema_version

    def _add_to_db(self, name, calendar, cursor=None, expand_until=None, reCreate=False):
        """
        Records the given calendar resource in the index with the given name.
        Resource names and UIDs must both be unique; only one resource name may
        be associated with any given UID and vice versa.
        NB This method does not commit the changes to the db - the caller
        MUST take care of that
        @param name: the name of the resource to add.
        @param calendar: a L{Calendar} object representing the resource
            contents.
        """
        raise NotImplementedError

    def _delete_from_db(self, name, uid, dorevision=True):
        """
        Deletes the specified entry from all dbs.
        @param name: the name of the resource to delete.
        @param uid: the uid of the resource to delete.
        """
        raise NotImplementedError


def sqlcalendarquery(filter, calendarid=None, userid=None, freebusy=False):
    """
    Convert the supplied calendar-query into a partial SQL statement.

    @param filter: the L{Filter} for the calendar-query to convert.
    @return: a C{tuple} of (C{str}, C{list}), where the C{str} is the partial SQL statement,
            and the C{list} is the list of argument substitutions to use with the SQL API execute method.
            Or return C{None} if it is not possible to create an SQL query to fully match the calendar-query.
    """
    try:
        expression = buildExpression(filter, sqllitegenerator.FIELDS)
        sql = sqllitegenerator(expression, calendarid, userid, freebusy)
        return sql.generate()
    except ValueError:
        return None


class CalendarIndex (AbstractCalendarIndex):
    """
    Calendar index - abstract class for indexer that indexes calendar objects in a collection.
    """

    def __init__(self, resource):
        """
        @param resource: the L{CalDAVResource} resource to
            index.
        """
        super(CalendarIndex, self).__init__(resource)

    def _db_init_data_tables_base(self, q, uidunique):
        """
        Initialise the underlying database tables.
        @param q:           a database cursor to use.
        """
        #
        # RESOURCE table is the primary index table
        #   NAME: Last URI component (eg. <uid>.ics, RESOURCE primary key)
        #   UID: iCalendar UID (may or may not be unique)
        #   TYPE: iCalendar component type
        #   RECURRANCE_MAX: Highest date of recurrence expansion
        #   ORGANIZER: cu-address of the Organizer of the event
        #
        q.execute(
            """
            create table RESOURCE (
                RESOURCEID     integer primary key autoincrement,
                NAME           text unique,
                UID            text%s,
                TYPE           text,
                RECURRANCE_MAX date,
                ORGANIZER      text
            )
            """ % (" unique" if uidunique else "",)
        )

        #
        # TIMESPAN table tracks (expanded) time spans for resources
        #   NAME: Related resource (RESOURCE foreign key)
        #   FLOAT: 'Y' if start/end are floating, 'N' otherwise
        #   START: Start date
        #   END: End date
        #   FBTYPE: FBTYPE value:
        #     '?' - unknown
        #     'F' - free
        #     'B' - busy
        #     'U' - busy-unavailable
        #     'T' - busy-tentative
        #   TRANSPARENT: Y if transparent, N if opaque (default non-per-user value)
        #
        q.execute(
            """
            create table TIMESPAN (
                INSTANCEID   integer primary key autoincrement,
                RESOURCEID   integer,
                FLOAT        text(1),
                START        date,
                END          date,
                FBTYPE       text(1),
                TRANSPARENT  text(1)
            )
            """
        )
        q.execute(
            """
            create index STARTENDFLOAT on TIMESPAN (START, END, FLOAT)
            """
        )

        #
        # PERUSER table tracks per-user ids
        #   PERUSERID: autoincrement primary key
        #   UID: User ID used in calendar data
        #
        q.execute(
            """
            create table PERUSER (
                PERUSERID       integer primary key autoincrement,
                USERUID         text
            )
            """
        )
        q.execute(
            """
            create index PERUSER_UID on PERUSER (USERUID)
            """
        )

        #
        # TRANSPARENCY table tracks per-user per-instance transparency
        #   PERUSERID: user id key
        #   INSTANCEID: instance id key
        #   TRANSPARENT: Y if transparent, N if opaque
        #
        q.execute(
            """
            create table TRANSPARENCY (
                PERUSERID       integer,
                INSTANCEID      integer,
                TRANSPARENT     text(1)
            )
            """
        )

        #
        # REVISIONS table tracks changes
        #   NAME: Last URI component (eg. <uid>.ics, RESOURCE primary key)
        #   REVISION: revision number
        #   WASDELETED: Y if revision deleted, N if added or changed
        #
        q.execute(
            """
            create table REVISION_SEQUENCE (
                REVISION        integer
            )
            """
        )
        q.execute(
            """
            insert into REVISION_SEQUENCE (REVISION) values (0)
            """
        )
        q.execute(
            """
            create table REVISIONS (
                NAME            text unique,
                REVISION        integer,
                DELETED         text(1)
            )
            """
        )
        q.execute(
            """
            create index REVISION on REVISIONS (REVISION)
            """
        )

        if uidunique:
            #
            # RESERVED table tracks reserved UIDs
            #   UID: The UID being reserved
            #   TIME: When the reservation was made
            #
            q.execute(
                """
                create table RESERVED (
                    UID  text unique,
                    TIME date
                )
                """
            )

        # Cascading triggers to help on delete
        q.execute(
            """
            create trigger resourceDelete after delete on RESOURCE
            for each row
            begin
                delete from TIMESPAN where TIMESPAN.RESOURCEID = OLD.RESOURCEID;
            end
            """
        )
        q.execute(
            """
            create trigger timespanDelete after delete on TIMESPAN
            for each row
            begin
                delete from TRANSPARENCY where INSTANCEID = OLD.INSTANCEID;
            end
            """
        )

    def _db_can_upgrade(self, old_version):
        """
        Can we do an in-place upgrade
        """

        # v10 is a big change - no upgrade possible
        return False

    def _db_upgrade_data_tables(self, q, old_version):
        """
        Upgrade the data from an older version of the DB.
        """

        # v10 is a big change - no upgrade possible
        pass

    def notExpandedBeyond(self, minDate):
        """
        Gives all resources which have not been expanded beyond a given date
        in the index
        """
        return self._db_values_for_sql("select NAME from RESOURCE where RECURRANCE_MAX < :1", pyCalendarToSQLTimestamp(minDate))

    def reExpandResource(self, name, expand_until):
        """
        Given a resource name, remove it from the database and re-add it
        with a longer expansion.
        """
        calendar = self.resource.getChild(name).iCalendar()
        self._add_to_db(name, calendar, expand_until=expand_until, reCreate=True)
        self._db_commit()

    def _add_to_db(self, name, calendar, cursor=None, expand_until=None, reCreate=False):
        """
        Records the given calendar resource in the index with the given name.
        Resource names and UIDs must both be unique; only one resource name may
        be associated with any given UID and vice versa.
        NB This method does not commit the changes to the db - the caller
        MUST take care of that
        @param name: the name of the resource to add.
        @param calendar: a L{Calendar} object representing the resource
            contents.
        """
        uid = calendar.resourceUID()
        organizer = calendar.getOrganizer()
        if not organizer:
            organizer = ""

        # Decide how far to expand based on the component
        doInstanceIndexing = False
        master = calendar.masterComponent()
        if master is None or not calendar.isRecurring():
            # When there is no master we have a set of overridden components - index them all.
            # When there is one instance - index it.
            expand = DateTime(2100, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
            doInstanceIndexing = True
        else:
            # If migrating or re-creating or config option for delayed indexing is off, always index
            if reCreate or not config.FreeBusyIndexDelayedExpand:
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
                raise IndexedSearchException()

        # Always do recurrence expansion even if we do not intend to index - we need this to double-check the
        # validity of the iCalendar recurrence data.
        try:
            instances = calendar.expandTimeRanges(expand, ignoreInvalidInstances=reCreate)
            recurrenceLimit = instances.limit
        except InvalidOverriddenInstanceError as e:
            log.error("Invalid instance {rid} when indexing {name} in {rsrc!r}", rid=e.rid, name=name, rsrc=self.resource)
            raise

        # Now coerce indexing to off if needed
        if not doInstanceIndexing:
            instances = None
            recurrenceLimit = DateTime(1900, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)

        self._delete_from_db(name, uid, False)

        # Add RESOURCE item
        self._db_execute(
            """
            insert into RESOURCE (NAME, UID, TYPE, RECURRANCE_MAX, ORGANIZER)
            values (:1, :2, :3, :4, :5)
            """, name, uid, calendar.resourceType(), pyCalendarToSQLTimestamp(recurrenceLimit) if recurrenceLimit else None, organizer
        )
        resourceid = self.lastrowid

        # Get a set of all referenced per-user UIDs and map those to entries already
        # in the DB and add new ones as needed
        useruids = calendar.allPerUserUIDs()
        useruids.add("")
        useruidmap = {}
        for useruid in useruids:
            peruserid = self._db_value_for_sql(
                "select PERUSERID from PERUSER where USERUID = :1",
                useruid
            )
            if peruserid is None:
                self._db_execute(
                    """
                    insert into PERUSER (USERUID)
                    values (:1)
                    """, useruid
                )
                peruserid = self.lastrowid
            useruidmap[useruid] = peruserid

        if doInstanceIndexing:
            for key in instances:
                instance = instances[key]
                start = instance.start
                end = instance.end
                float = 'Y' if instance.start.floating() else 'N'
                transp = 'T' if instance.component.propertyValue("TRANSP") == "TRANSPARENT" else 'F'
                self._db_execute(
                    """
                    insert into TIMESPAN (RESOURCEID, FLOAT, START, END, FBTYPE, TRANSPARENT)
                    values (:1, :2, :3, :4, :5, :6)
                    """,
                    resourceid,
                    float,
                    pyCalendarToSQLTimestamp(start),
                    pyCalendarToSQLTimestamp(end),
                    icalfbtype_to_indexfbtype.get(instance.component.getFBType(), 'F'),
                    transp
                )
                instanceid = self.lastrowid
                peruserdata = calendar.perUserData(instance.rid)
                for useruid, (transp, _ignore_adjusted_start, _ignore_adjusted_end) in peruserdata:
                    peruserid = useruidmap[useruid]
                    self._db_execute(
                        """
                        insert into TRANSPARENCY (PERUSERID, INSTANCEID, TRANSPARENT)
                        values (:1, :2, :3)
                        """, peruserid, instanceid, 'T' if transp else 'F'
                    )

            # Special - for unbounded recurrence we insert a value for "infinity"
            # that will allow an open-ended time-range to always match it.
            if calendar.isRecurringUnbounded():
                start = DateTime(2100, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
                end = DateTime(2100, 1, 1, 1, 0, 0, tzid=Timezone.UTCTimezone)
                float = 'N'
                self._db_execute(
                    """
                    insert into TIMESPAN (RESOURCEID, FLOAT, START, END, FBTYPE, TRANSPARENT)
                    values (:1, :2, :3, :4, :5, :6)
                    """, resourceid, float, pyCalendarToSQLTimestamp(start), pyCalendarToSQLTimestamp(end), '?', '?'
                )
                instanceid = self.lastrowid
                peruserdata = calendar.perUserData(None)
                for useruid, (transp, _ignore_adjusted_start, _ignore_adjusted_end) in peruserdata:
                    peruserid = useruidmap[useruid]
                    self._db_execute(
                        """
                        insert into TRANSPARENCY (PERUSERID, INSTANCEID, TRANSPARENT)
                        values (:1, :2, :3)
                        """, peruserid, instanceid, 'T' if transp else 'F'
                    )

        self._db_execute(
            """
            insert or replace into REVISIONS (NAME, REVISION, DELETED)
            values (:1, :2, :3)
            """, name, self.bumpRevision(fast=True), 'N',
        )

    def _delete_from_db(self, name, uid, dorevision=True):
        """
        Deletes the specified entry from all dbs.
        @param name: the name of the resource to delete.
        @param uid: the uid of the resource to delete.
        """
        self._db_execute("delete from RESOURCE where NAME = :1", name)
        if dorevision:
            self._db_execute(
                """
                update REVISIONS SET REVISION = :1, DELETED = :2
                where NAME = :3
                """, self.bumpRevision(fast=True), 'Y', name
            )


def wrapInDeferred(f):
    def _(*args, **kwargs):
        return maybeDeferred(f, *args, **kwargs)

    return _


class MemcachedUIDReserver(CachePoolUserMixIn):
    log = Logger()

    def __init__(self, index, cachePool=None):
        self.index = index
        self._cachePool = cachePool

    def _key(self, uid):
        return 'reservation:%s' % (
            hashlib.md5('%s:%s' % (uid,
                                   self.index.resource.fp.path)).hexdigest())

    def reserveUID(self, uid):
        uid = uid.encode('utf-8')
        self.log.debug(
            "Reserving UID {uid} @ {path}",
            uid=uid, path=self.index.resource.fp.path,
        )

        def _handleFalse(result):
            if result is False:
                raise ReservationError(
                    "UID %s already reserved for calendar collection %s."
                    % (uid, self.index.resource)
                )

        d = self.getCachePool().add(self._key(uid),
                                    'reserved',
                                    expireTime=config.UIDReservationTimeOut)
        d.addCallback(_handleFalse)
        return d

    def unreserveUID(self, uid):
        uid = uid.encode('utf-8')
        self.log.debug(
            "Unreserving UID {uid} @ {path}",
            uid=uid, path=self.index.resource.fp.path,
        )

        def _handleFalse(result):
            if result is False:
                raise ReservationError(
                    "UID %s is not reserved for calendar collection %s."
                    % (uid, self.index.resource)
                )

        d = self.getCachePool().delete(self._key(uid))
        d.addCallback(_handleFalse)
        return d

    def isReservedUID(self, uid):
        uid = uid.encode('utf-8')
        self.log.debug(
            "Is reserved UID {uid} @ {path}",
            uid=uid, path=self.index.resource.fp.path,
        )

        def _checkValue(xxx_todo_changeme):
            (flags, value) = xxx_todo_changeme
            if value is None:
                return False
            else:
                return True

        d = self.getCachePool().get(self._key(uid))
        d.addCallback(_checkValue)
        return d


class SQLUIDReserver(object):

    def __init__(self, index):
        self.index = index

    @wrapInDeferred
    def reserveUID(self, uid):
        """
        Reserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is already reserved
        """

        try:
            self.index._db_execute("insert into RESERVED (UID, TIME) values (:1, :2)", uid, datetime.datetime.now())
            self.index._db_commit()
        except sqlite.IntegrityError:
            self.index._db_rollback()
            raise ReservationError(
                "UID %s already reserved for calendar collection %s."
                % (uid, self.index.resource)
            )
        except sqlite.Error as e:
            log.error("Unable to reserve UID: {ex}", ex=e)
            self.index._db_rollback()
            raise

    def unreserveUID(self, uid):
        """
        Unreserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is not reserved
        """

        def _cb(result):
            if result == False:
                raise ReservationError(
                    "UID %s is not reserved for calendar collection %s."
                    % (uid, self.index.resource)
                )
            else:
                try:
                    self.index._db_execute(
                        "delete from RESERVED where UID = :1", uid)
                    self.index._db_commit()
                except sqlite.Error as e:
                    log.error("Unable to unreserve UID: {ex}", ex=e)
                    self.index._db_rollback()
                    raise

        d = self.isReservedUID(uid)
        d.addCallback(_cb)
        return d

    @wrapInDeferred
    def isReservedUID(self, uid):
        """
        Check to see whether a UID is reserved.
        @param uid: the UID to check
        @return: True if C{uid} is reserved, False otherwise.
        """

        rowiter = self.index._db_execute("select UID, TIME from RESERVED where UID = :1", uid)
        for uid, attime in rowiter:
            # Double check that the time is within a reasonable period of now
            # otherwise we probably have a stale reservation
            tm = time.strptime(attime[:19], "%Y-%m-%d %H:%M:%S")
            dt = datetime.datetime(year=tm.tm_year, month=tm.tm_mon, day=tm.tm_mday, hour=tm.tm_hour, minute=tm.tm_min, second=tm.tm_sec)
            if datetime.datetime.now() - dt > datetime.timedelta(seconds=config.UIDReservationTimeOut):
                try:
                    self.index._db_execute("delete from RESERVED where UID = :1", uid)
                    self.index._db_commit()
                except sqlite.Error as e:
                    log.error("Unable to unreserve UID: {ex}", ex=e)
                    self.index._db_rollback()
                    raise
                return False
            else:
                return True

        return False


class Index (CalendarIndex):
    """
    Calendar collection index - regular collection that enforces CalDAV UID uniqueness requirement.
    """

    def __init__(self, resource):
        """
        @param resource: the L{CalDAVResource} resource to
            index. C{resource} must be a calendar collection (i.e.
            C{resource.isPseudoCalendarCollection()} returns C{True}.)
        """
        assert resource.isCalendarCollection(), "non-calendar collection resource %s has no index." % (resource,)
        super(Index, self).__init__(resource)

        if (
            hasattr(config, "Memcached") and
            config.Memcached.Pools.Default.ClientEnabled
        ):
            self.reserver = MemcachedUIDReserver(self)
        else:
            self.reserver = SQLUIDReserver(self)

    #
    # A dict of sets. The dict keys are calendar collection paths,
    # and the sets contains reserved UIDs for each path.
    #

    def reserveUID(self, uid):
        return self.reserver.reserveUID(uid)

    def unreserveUID(self, uid):
        return self.reserver.unreserveUID(uid)

    def isReservedUID(self, uid):
        return self.reserver.isReservedUID(uid)

    def isAllowedUID(self, uid, *names):
        """
        Checks to see whether to allow an operation which would add the
        specified UID to the index.  Specifically, the operation may not
        violate the constraint that UIDs must be unique.
        @param uid: the UID to check
        @param names: the names of resources being replaced or deleted by the
            operation; UIDs associated with these resources are not checked.
        @return: True if the UID is not in the index and is not reserved,
            False otherwise.
        """
        rname = self.resourceNameForUID(uid)
        return (rname is None or rname in names)

    def _db_type(self):
        """
        @return: the collection type assigned to this index.
        """
        return collection_types["Calendar"]

    def _db_init_data_tables(self, q):
        """
        Initialise the underlying database tables.
        @param q:           a database cursor to use.
        """

        # Create database where the RESOURCE table has unique UID column.
        self._db_init_data_tables_base(q, True)

    def _db_recreate(self, do_commit=True):
        """
        Re-create the database tables from existing calendar data.
        """

        #
        # Populate the DB with data from already existing resources.
        # This allows for index recovery if the DB file gets
        # deleted.
        #
        fp = self.resource.fp
        for name in fp.listdir():
            if name.startswith("."):
                continue

            child = fp.child(name)
            if not child.isfile():
                continue
            try:
                stream = child.open()
            except (IOError, OSError) as e:
                log.error("Unable to open resource {name}: {ex}", name=name, ex=e)
                continue

            # FIXME: This is blocking I/O
            try:
                calendar = Component.fromStream(stream)
                calendar.validCalendarData()
                calendar.validCalendarForCalDAV(methodAllowed=False)
            except ValueError:
                log.error("Non-calendar resource: {name}", name=name)
            else:
                # og.info("Indexing resource: {name}", name=name)
                self.addResource(name, calendar, True, reCreate=True)
            finally:
                stream.close()

        # Do commit outside of the loop for better performance
        if do_commit:
            self._db_commit()


class IndexSchedule (CalendarIndex):
    """
    Schedule collection index - does not require UID uniqueness.
    """

    def reserveUID(self, uid):  # @UnusedVariable
        """
        Reserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is already reserved
        """

        # iTIP does not require unique UIDs
        return succeed(None)

    def unreserveUID(self, uid):  # @UnusedVariable
        """
        Unreserve a UID for this index's resource.
        @param uid: the UID to reserve
        @raise ReservationError: if C{uid} is not reserved
        """

        # iTIP does not require unique UIDs
        return succeed(None)

    def isReservedUID(self, uid):  # @UnusedVariable
        """
        Check to see whether a UID is reserved.
        @param uid: the UID to check
        @return: True if C{uid} is reserved, False otherwise.
        """

        # iTIP does not require unique UIDs
        return succeed(False)

    def isAllowedUID(self, uid, *names):  # @UnusedVariable
        """
        Checks to see whether to allow an operation with adds the the specified
        UID is allowed to the index.  Specifically, the operation may not
        violate the constraint that UIDs must be unique, and the UID must not
        be reserved.
        @param uid: the UID to check
        @param names: the names of resources being replaced or deleted by the
            operation; UIDs associated with these resources are not checked.
        @return: True if the UID is not in the index and is not reserved,
            False otherwise.
        """

        # iTIP does not require unique UIDs
        return True

    def _db_type(self):
        """
        @return: the collection type assigned to this index.
        """
        return collection_types["iTIP"]

    def _db_init_data_tables(self, q):
        """
        Initialise the underlying database tables.
        @param q:           a database cursor to use.
        """

        # Create database where the RESOURCE table has a UID column that is not unique.
        self._db_init_data_tables_base(q, False)

    def _db_recreate(self, do_commit=True):
        """
        Re-create the database tables from existing calendar data.
        """

        #
        # Populate the DB with data from already existing resources.
        # This allows for index recovery if the DB file gets
        # deleted.
        #
        fp = self.resource.fp
        for name in fp.listdir():
            if name.startswith("."):
                continue

            child = fp.child(name)
            if not child.isfile():
                continue
            try:
                stream = child.open()
            except (IOError, OSError) as e:
                log.error("Unable to open resource {name}: {ex}", name=name, ex=e)
                continue

            # FIXME: This is blocking I/O
            try:
                calendar = Component.fromStream(stream)
                calendar.validCalendarData()
                calendar.validCalendarForCalDAV(methodAllowed=True)
            except ValueError:
                log.error("Non-calendar resource: {name}", name=name)
            else:
                # log.info("Indexing resource: {name}", name=name)
                self.addResource(name, calendar, True, reCreate=True)
            finally:
                stream.close()

        # Do commit outside of the loop for better performance
        if do_commit:
            self._db_commit()
