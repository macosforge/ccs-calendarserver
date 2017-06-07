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

from pycalendar.datetime import DateTime
from twext.enterprise.dal.syntax import Select
from twext.enterprise.jobs.jobitem import JobItem
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.filepath import FilePath
from twistedcaldav.config import config
from twistedcaldav.ical import Component, normalize_iCalStr
from txdav.caldav.datastore.scheduling.imip.token import iMIPTokenRecord
from txdav.caldav.datastore.sql import ManagedAttachment
from txdav.caldav.datastore.sql_directory import GroupShareeRecord
from txdav.common.datastore.podding.migration.home_sync import CrossPodHomeSync
from txdav.common.datastore.podding.migration.sync_metadata import CalendarMigrationRecord, \
    AttachmentMigrationRecord
from txdav.common.datastore.podding.test.util import MultiStoreConduitTest
from txdav.common.datastore.sql_directory import DelegateRecord, \
    ExternalDelegateGroupsRecord, DelegateGroupsRecord, GroupsRecord
from txdav.common.datastore.sql_notification import NotificationCollection
from txdav.common.datastore.sql_tables import schema, _HOME_STATUS_EXTERNAL, \
    _BIND_MODE_READ, _HOME_STATUS_MIGRATING, _HOME_STATUS_NORMAL, \
    _HOME_STATUS_DISABLED
from txdav.common.datastore.test.util import populateCalendarsFrom
from txdav.who.delegates import Delegates
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream
from uuid import uuid4
import json
from txdav.caldav.datastore.scheduling.work import ScheduleOrganizerWork, \
    ScheduleReplyWork, ScheduleRefreshWork, ScheduleAutoReplyWork


class TestCrossPodHomeSync(MultiStoreConduitTest):
    """
    Test that L{CrossPodHomeSync} works.
    """

    nowYear = {"now": DateTime.getToday().getYear()}

    caldata1 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTART:{now:04d}0102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    caldata1_changed = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTART:{now:04d}0102T150000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance changed
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    caldata2 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid2
DTSTART:{now:04d}0102T160000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    caldata3 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid3
DTSTART:{now:04d}0102T160000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    caldata4 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid4
DTSTART:{now:04d}0102T180000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=DAILY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    inboxdata1 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:uid1-inbox
DTSTART:{now:04d}0102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(**nowYear)

    @inlineCallbacks
    def test_remote_home(self):
        """
        Test that a remote home can be accessed.
        """

        home01 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        self.assertTrue(home01 is not None)
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        home = yield syncer._remoteHome(self.theTransactionUnderTest(1))
        self.assertTrue(home is not None)
        self.assertEqual(home.id(), home01.id())
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_prepare_home(self):
        """
        Test that L{prepareCalendarHome} creates a home.
        """

        # No home present
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        home = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home is None)
        yield self.commitTransaction(1)

        yield syncer.prepareCalendarHome()

        # Home is present
        home = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home is not None)
        children = yield home.listChildren()
        self.assertEqual(len(children), 0)
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_prepare_home_external_txn(self):
        """
        Test that L{prepareCalendarHome} creates a home.
        """

        # No home present
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        home = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home is None)
        yield self.commitTransaction(1)

        yield syncer.prepareCalendarHome(txn=self.theTransactionUnderTest(1))
        yield self.commitTransaction(1)

        # Home is present
        home = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home is not None)
        children = yield home.listChildren()
        self.assertEqual(len(children), 0)
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_home_metadata(self):
        """
        Test that L{syncCalendarHomeMetaData} sync home metadata correctly.
        """

        alarm_event_timed = """BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:alarm_event_timed
TRIGGER:-PT10M
END:VALARM
"""
        alarm_event_allday = """BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:alarm_event_allday
TRIGGER:-PT10M
END:VALARM
"""
        alarm_todo_timed = """BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:alarm_todo_timed
TRIGGER:-PT10M
END:VALARM
"""
        alarm_todo_allday = """BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:alarm_todo_allday
TRIGGER:-PT10M
END:VALARM
"""
        availability = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Inc.//Example Calendar//EN
BEGIN:VAVAILABILITY
UID:20061005T133225Z-00001-availability@example.com
DTSTART:20060101T000000Z
DTEND:20060108T000000Z
DTSTAMP:20061005T133225Z
ORGANIZER:mailto:bernard@example.com
BEGIN:AVAILABLE
UID:20061005T133225Z-00001-A-availability@example.com
DTSTART:20060102T090000Z
DTEND:20060102T120000Z
DTSTAMP:20061005T133225Z
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Weekdays from 9:00 to 12:00
END:AVAILABLE
END:VAVAILABILITY
END:VCALENDAR
"""

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        events0 = yield home0.createChildWithName("events")
        yield home0.setDefaultCalendar(events0, "VEVENT")
        yield home0.setDefaultAlarm(alarm_event_timed, True, True)
        yield home0.setDefaultAlarm(alarm_event_allday, True, False)
        yield home0.setDefaultAlarm(alarm_todo_timed, False, True)
        yield home0.setDefaultAlarm(alarm_todo_allday, False, False)
        yield home0.setAvailability(Component.fromString(availability))
        yield self.commitTransaction(0)

        # Trigger sync
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.sync()

        # Home is present with correct metadata
        home1 = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home1 is not None)
        calendar1 = yield home1.childWithName("calendar")
        events1 = yield home1.childWithName("events")
        tasks1 = yield home1.childWithName("tasks")
        self.assertFalse(home1.isDefaultCalendar(calendar1))
        self.assertTrue(home1.isDefaultCalendar(events1))
        self.assertTrue(home1.isDefaultCalendar(tasks1))
        self.assertEqual(home1.getDefaultAlarm(True, True), alarm_event_timed)
        self.assertEqual(home1.getDefaultAlarm(True, False), alarm_event_allday)
        self.assertEqual(home1.getDefaultAlarm(False, True), alarm_todo_timed)
        self.assertEqual(home1.getDefaultAlarm(False, False), alarm_todo_allday)
        self.assertEqual(normalize_iCalStr(home1.getAvailability()), normalize_iCalStr(availability))
        yield self.commitTransaction(1)

        # Make some changes
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        yield home0.setDefaultCalendar(calendar0, "VEVENT")
        yield home0.setDefaultAlarm(None, True, True)
        yield home0.setDefaultAlarm(None, False, True)
        yield self.commitTransaction(0)

        # Trigger sync again
        yield syncer.sync()

        # Home is present with correct metadata
        home1 = yield self.homeUnderTest(self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        self.assertTrue(home1 is not None)
        calendar1 = yield home1.childWithName("calendar")
        events1 = yield home1.childWithName("events")
        tasks1 = yield home1.childWithName("tasks")
        self.assertTrue(home1.isDefaultCalendar(calendar1))
        self.assertFalse(home1.isDefaultCalendar(events1))
        self.assertTrue(home1.isDefaultCalendar(tasks1))
        self.assertEqual(home1.getDefaultAlarm(True, True), None)
        self.assertEqual(home1.getDefaultAlarm(True, False), alarm_event_allday)
        self.assertEqual(home1.getDefaultAlarm(False, True), None)
        self.assertEqual(home1.getDefaultAlarm(False, False), alarm_todo_allday)
        self.assertEqual(normalize_iCalStr(home1.getAvailability()), normalize_iCalStr(availability))
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_get_calendar_sync_list(self):
        """
        Test that L{getCalendarSyncList} returns the correct results.
        """

        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.commitTransaction(0)
        home01 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01")
        self.assertTrue(home01 is not None)
        calendars01 = yield home01.loadChildren()
        results01 = {}
        for calendar in calendars01:
            if calendar.owned():
                sync_token = yield calendar.syncToken()
                results01[calendar.id()] = CalendarMigrationRecord.make(
                    calendarHomeResourceID=home01.id(),
                    remoteResourceID=calendar.id(),
                    localResourceID=0,
                    lastSyncToken=sync_token,
                )

        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        results = yield syncer.getCalendarSyncList()
        self.assertEqual(results, results01)

    @inlineCallbacks
    def test_sync_calendar_initial_empty(self):
        """
        Test that L{syncCalendar} syncs an initially non-existent local calendar with
        an empty remote calendar.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        remote_id = calendar0.id()
        remote_sync_token = yield calendar0.syncToken()
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()

        # No local calendar exists yet
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        children = yield home1.listChildren()
        self.assertEqual(len(children), 0)
        yield self.commitTransaction(1)

        # Trigger sync of the one calendar
        local_sync_state = {}
        remote_sync_state = {remote_id: CalendarMigrationRecord.make(
            calendarHomeResourceID=home0.id(),
            remoteResourceID=remote_id,
            localResourceID=0,
            lastSyncToken=remote_sync_token,
        )}
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )
        self.assertEqual(len(local_sync_state), 1)
        self.assertEqual(local_sync_state[remote_id].lastSyncToken, remote_sync_state[remote_id].lastSyncToken)

        # Local calendar exists
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is not None)
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_sync_calendar_initial_with_data(self):
        """
        Test that L{syncCalendar} syncs an initially non-existent local calendar with
        a remote calendar containing data. Also check a change to one event is then
        sync'd the second time.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        o1 = yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.caldata1))
        o2 = yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.caldata2))
        o3 = yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.caldata3))
        inbox0 = yield home0.childWithName("inbox")
        inbox_o1 = yield inbox0.createCalendarObjectWithName("inbox1.ics", Component.fromString(self.inboxdata1))
        remote_id = calendar0.id()
        inbox_remote_id = inbox0.id()
        mapping0 = dict([(o.name(), o.id()) for o in (o1, o2, o3, inbox_o1)])
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()

        # No local calendar exists yet
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is None)
        yield self.commitTransaction(1)

        # Trigger sync of the one calendar
        local_sync_state = {}
        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )
        self.assertEqual(len(local_sync_state), 1)
        self.assertEqual(local_sync_state[remote_id].lastSyncToken, remote_sync_state[remote_id].lastSyncToken)

        # Trigger sync of the one inbox
        inbox_local_sync_state = {}
        inbox_remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            inbox_remote_id,
            inbox_local_sync_state,
            inbox_remote_sync_state,
        )
        self.assertEqual(len(inbox_local_sync_state), 1)
        self.assertEqual(inbox_local_sync_state[inbox_remote_id].lastSyncToken, inbox_remote_sync_state[inbox_remote_id].lastSyncToken)

        @inlineCallbacks
        def _checkCalendarObjectMigrationState(home, mapping1):
            com = schema.CALENDAR_OBJECT_MIGRATION
            mappings = yield Select(
                columns=[com.REMOTE_RESOURCE_ID, com.LOCAL_RESOURCE_ID],
                From=com,
                Where=(com.CALENDAR_HOME_RESOURCE_ID == home.id())
            ).on(self.theTransactionUnderTest(1))
            expected_mappings = dict([(mapping0[name], mapping1[name]) for name in mapping0.keys()])
            self.assertEqual(dict(mappings), expected_mappings)

        # Local calendar/inbox exists
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is not None)
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "2.ics", "3.ics",)))
        mapping1 = dict([(o.name(), o.id()) for o in children])
        inbox1 = yield home1.childWithName("inbox")
        self.assertTrue(inbox1 is not None)
        children = yield inbox1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("inbox1.ics",)))
        mapping1.update(dict([(o.name(), o.id()) for o in children]))
        yield _checkCalendarObjectMigrationState(home1, mapping1)
        yield self.commitTransaction(1)

        # Change one resource
        object0 = yield self.calendarObjectUnderTest(
            txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics"
        )
        yield object0.setComponent(Component.fromString(self.caldata1_changed))
        yield self.commitTransaction(0)

        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )

        object1 = yield self.calendarObjectUnderTest(
            txn=self.theTransactionUnderTest(1), home="user01", status=_HOME_STATUS_MIGRATING, calendar_name="calendar", name="1.ics"
        )
        caldata = yield object1.component()
        self.assertEqual(normalize_iCalStr(caldata), normalize_iCalStr(self.caldata1_changed))
        yield self.commitTransaction(1)

        # Remove one resource
        object0 = yield self.calendarObjectUnderTest(
            txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="2.ics"
        )
        yield object0.remove()
        del mapping0["2.ics"]
        yield self.commitTransaction(0)

        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )

        calendar1 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(1), home="user01", status=_HOME_STATUS_MIGRATING, name="calendar")
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "3.ics",)))
        mapping1 = dict([(o.name(), o.id()) for o in children])
        inbox1 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(1), home="user01", status=_HOME_STATUS_MIGRATING, name="inbox")
        children = yield inbox1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("inbox1.ics",)))
        mapping1.update(dict([(o.name(), o.id()) for o in children]))
        yield _checkCalendarObjectMigrationState(home1, mapping1)
        yield self.commitTransaction(1)

        # Add one resource
        calendar0 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(0), home="user01", name="calendar")
        o4 = yield calendar0.createCalendarObjectWithName("4.ics", Component.fromString(self.caldata4))
        mapping0[o4.name()] = o4.id()
        yield self.commitTransaction(0)

        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )

        calendar1 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(1), home="user01", status=_HOME_STATUS_MIGRATING, name="calendar")
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "3.ics", "4.ics")))
        mapping1 = dict([(o.name(), o.id()) for o in children])
        inbox1 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(1), home="user01", status=_HOME_STATUS_MIGRATING, name="inbox")
        children = yield inbox1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("inbox1.ics",)))
        mapping1.update(dict([(o.name(), o.id()) for o in children]))
        yield _checkCalendarObjectMigrationState(home1, mapping1)
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_sync_calendars_add_remove(self):
        """
        Test that L{syncCalendar} syncs an initially non-existent local calendar with
        a remote calendar containing data. Also check a change to one event is then
        sync'd the second time.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        children0 = yield home0.loadChildren()
        details0 = dict([(child.id(), child.name()) for child in children0])
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()

        # No local calendar exists yet
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        children1 = yield home1.loadChildren()
        self.assertEqual(len(children1), 0)
        yield self.commitTransaction(1)

        # Trigger sync
        yield syncer.syncCalendarList()
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        children1 = yield home1.loadChildren()
        details1 = dict([(child.id(), child.name()) for child in children1])
        self.assertEqual(set(details1.values()), set(details0.values()))
        yield self.commitTransaction(1)

        # Add a calendar
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        newcalendar0 = yield home0.createCalendarWithName("new-calendar")
        details0[newcalendar0.id()] = newcalendar0.name()
        yield self.commitTransaction(0)

        # Trigger sync
        yield syncer.syncCalendarList()
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        children1 = yield home1.loadChildren()
        details1 = dict([(child.id(), child.name()) for child in children1])
        self.assertTrue("new-calendar" in details1.values())
        self.assertEqual(set(details1.values()), set(details0.values()))
        yield self.commitTransaction(1)

        # Remove a calendar
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("new-calendar")
        del details0[calendar0.id()]
        yield calendar0.purge()
        yield self.commitTransaction(0)

        # Trigger sync
        yield syncer.syncCalendarList()
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        children1 = yield home1.loadChildren()
        details1 = dict([(child.id(), child.name()) for child in children1])
        self.assertTrue("new-calendar" not in details1.values())
        self.assertEqual(set(details1.values()), set(details0.values()))
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_sync_attachments_add_remove(self):
        """
        Test that L{syncAttachments} syncs attachment data, then an update to the data,
        and finally a removal of the data.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.caldata1))
        yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.caldata2))
        yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.caldata3))
        remote_id = calendar0.id()
        mapping0 = dict()
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()

        # Trigger sync of the one calendar
        local_sync_state = {}
        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )
        self.assertEqual(len(local_sync_state), 1)
        self.assertEqual(local_sync_state[remote_id].lastSyncToken, remote_sync_state[remote_id].lastSyncToken)

        @inlineCallbacks
        def _mapLocalIDToRemote(remote_id):
            records = yield AttachmentMigrationRecord.all(self.theTransactionUnderTest(1))
            yield self.commitTransaction(1)
            for record in records:
                if record.remoteResourceID == remote_id:
                    returnValue(record.localResourceID)
            else:
                returnValue(None)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set())
        self.assertEqual(removed, set())

        @inlineCallbacks
        def _checkAttachmentObjectMigrationState(home, mapping1):
            am = schema.ATTACHMENT_MIGRATION
            mappings = yield Select(
                columns=[am.REMOTE_RESOURCE_ID, am.LOCAL_RESOURCE_ID],
                From=am,
                Where=(am.CALENDAR_HOME_RESOURCE_ID == home.id())
            ).on(self.theTransactionUnderTest(1))
            expected_mappings = dict([(mapping0[name], mapping1[name]) for name in mapping0.keys()])
            self.assertEqual(dict(mappings), expected_mappings)

        # Local calendar exists
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is not None)
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "2.ics", "3.ics",)))

        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)
        yield self.commitTransaction(1)

        # Add one attachment
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics")
        attachment, _ignore_location = yield object1.addAttachment(None, MimeType.fromString("text/plain"), "test.txt", MemoryStream("Here is some text #1."))
        id0_1 = attachment.id()
        md50_1 = attachment.md5()
        managedid0_1 = attachment.managedID()
        mapping0[md50_1] = id0_1
        yield self.commitTransaction(0)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set(((yield _mapLocalIDToRemote(id0_1)),)))
        self.assertEqual(removed, set())

        # Validate changes
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)

        # Add another attachment
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="2.ics")
        attachment, _ignore_location = yield object1.addAttachment(None, MimeType.fromString("text/plain"), "test2.txt", MemoryStream("Here is some text #2."))
        id0_2 = attachment.id()
        md50_2 = attachment.md5()
        mapping0[md50_2] = id0_2
        yield self.commitTransaction(0)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set(((yield _mapLocalIDToRemote(id0_2)),)))
        self.assertEqual(removed, set())

        # Validate changes
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)

        # Change original attachment (this is actually a remove and a create all in one)
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics")
        attachment, _ignore_location = yield object1.updateAttachment(managedid0_1, MimeType.fromString("text/plain"), "test.txt", MemoryStream("Here is some text #1 - changed."))
        del mapping0[md50_1]
        id0_1_changed = attachment.id()
        md50_1_changed = attachment.md5()
        managedid0_1_changed = attachment.managedID()
        mapping0[md50_1_changed] = id0_1_changed
        yield self.commitTransaction(0)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set(((yield _mapLocalIDToRemote(id0_1_changed)),)))
        self.assertEqual(removed, set((id0_1,)))

        # Validate changes
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)

        # Add original to a different resource
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics")
        component = yield object1.componentForUser()
        attach = component.mainComponent().getProperty("ATTACH")

        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="3.ics")
        component = yield object1.componentForUser()
        attach = component.mainComponent().addProperty(attach)
        yield object1.setComponent(component)
        yield self.commitTransaction(0)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set())
        self.assertEqual(removed, set())

        # Validate changes
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)

        # Change original attachment in original resource (this creates a new one and does not remove the old)
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics")
        attachment, _ignore_location = yield object1.updateAttachment(managedid0_1_changed, MimeType.fromString("text/plain"), "test.txt", MemoryStream("Here is some text #1 - changed again."))
        id0_1_changed_again = attachment.id()
        md50_1_changed_again = attachment.md5()
        mapping0[md50_1_changed_again] = id0_1_changed_again
        yield self.commitTransaction(0)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()
        self.assertEqual(changed, set(((yield _mapLocalIDToRemote(id0_1_changed_again)),)))
        self.assertEqual(removed, set())

        # Validate changes
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        attachments = yield home1.getAllAttachments()
        mapping1 = dict([(o.md5(), o.id()) for o in attachments])
        yield _checkAttachmentObjectMigrationState(home1, mapping1)

    @inlineCallbacks
    def test_link_attachments(self):
        """
        Test that L{linkAttachments} links attachment data to the associated calendar object.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.notificationCollectionUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        object0_1 = yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.caldata1))
        object0_2 = yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.caldata2))
        yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.caldata3))
        remote_id = calendar0.id()

        attachment, _ignore_location = yield object0_1.addAttachment(None, MimeType.fromString("text/plain"), "test.txt", MemoryStream("Here is some text #1."))
        id0_1 = attachment.id()
        md50_1 = attachment.md5()
        managedid0_1 = attachment.managedID()
        pathID0_1 = ManagedAttachment.lastSegmentOfUriPath(managedid0_1, attachment.name())

        attachment, _ignore_location = yield object0_2.addAttachment(None, MimeType.fromString("text/plain"), "test2.txt", MemoryStream("Here is some text #2."))
        id0_2 = attachment.id()
        md50_2 = attachment.md5()
        managedid0_2 = attachment.managedID()
        pathID0_2 = ManagedAttachment.lastSegmentOfUriPath(managedid0_2, attachment.name())

        yield self.commitTransaction(0)

        # Add original to a different resource
        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="1.ics")
        component = yield object1.componentForUser()
        attach = component.mainComponent().getProperty("ATTACH")

        object1 = yield self.calendarObjectUnderTest(txn=self.theTransactionUnderTest(0), home="user01", calendar_name="calendar", name="3.ics")
        component = yield object1.componentForUser()
        attach = component.mainComponent().addProperty(attach)
        yield object1.setComponent(component)
        yield self.commitTransaction(0)

        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()

        # Trigger sync of the one calendar
        local_sync_state = {}
        remote_sync_state = yield syncer.getCalendarSyncList()
        yield syncer.syncCalendar(
            remote_id,
            local_sync_state,
            remote_sync_state,
        )
        self.assertEqual(len(local_sync_state), 1)
        self.assertEqual(local_sync_state[remote_id].lastSyncToken, remote_sync_state[remote_id].lastSyncToken)

        # Sync attachments
        changed, removed = yield syncer.syncAttachments()

        @inlineCallbacks
        def _mapLocalIDToRemote(remote_id):
            records = yield AttachmentMigrationRecord.all(self.theTransactionUnderTest(1))
            yield self.commitTransaction(1)
            for record in records:
                if record.remoteResourceID == remote_id:
                    returnValue(record.localResourceID)
            else:
                returnValue(None)

        self.assertEqual(changed, set(((yield _mapLocalIDToRemote(id0_1)), (yield _mapLocalIDToRemote(id0_2)),)))
        self.assertEqual(removed, set())

        # Link attachments (after home is disabled)
        yield syncer.disableRemoteHome()
        len_links = yield syncer.linkAttachments()
        self.assertEqual(len_links, 3)

        # Local calendar exists
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is not None)
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "2.ics", "3.ics",)))

        # Make sure calendar object is associated with attachment
        object1 = yield calendar1.objectResourceWithName("1.ics")
        attachments = yield object1.managedAttachmentList()
        self.assertEqual(attachments, [pathID0_1, ])

        attachment = yield object1.attachmentWithManagedID(managedid0_1)
        self.assertTrue(attachment is not None)
        self.assertEqual(attachment.md5(), md50_1)

        # Make sure calendar object is associated with attachment
        object1 = yield calendar1.objectResourceWithName("2.ics")
        attachments = yield object1.managedAttachmentList()
        self.assertEqual(attachments, [pathID0_2, ])

        attachment = yield object1.attachmentWithManagedID(managedid0_2)
        self.assertTrue(attachment is not None)
        self.assertEqual(attachment.md5(), md50_2)

        # Make sure calendar object is associated with attachment
        object1 = yield calendar1.objectResourceWithName("3.ics")
        attachments = yield object1.managedAttachmentList()
        self.assertEqual(attachments, [pathID0_1, ])

        attachment = yield object1.attachmentWithManagedID(managedid0_1)
        self.assertTrue(attachment is not None)
        self.assertEqual(attachment.md5(), md50_1)

    @inlineCallbacks
    def test_delegate_reconcile(self):
        """
        Test that L{delegateReconcile} copies over the full set of delegates and caches associated groups..
        """

        # Create remote home
        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.notificationCollectionUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.commitTransaction(0)

        # Add some delegates
        txn = self.theTransactionUnderTest(0)
        record01 = yield txn.directoryService().recordWithUID(u"user01")
        record02 = yield txn.directoryService().recordWithUID(u"user02")
        record03 = yield txn.directoryService().recordWithUID(u"user03")

        group01 = yield txn.directoryService().recordWithUID(u"__top_group_1__")
        group02 = yield txn.directoryService().recordWithUID(u"right_coast")

        # Add user02 and user03 as individual delegates
        yield Delegates.addDelegate(txn, record01, record02, True)
        yield Delegates.addDelegate(txn, record01, record03, False)

        # Add group delegates
        yield Delegates.addDelegate(txn, record01, group01, True)
        yield Delegates.addDelegate(txn, record01, group02, False)

        yield self.commitTransaction(0)

        # Initially no local delegates
        txn = self.theTransactionUnderTest(1)
        delegates = yield txn.dumpIndividualDelegatesLocal(u"user01")
        self.assertEqual(len(delegates), 0)
        delegates = yield txn.dumpGroupDelegatesLocal(u"user04")
        self.assertEqual(len(delegates), 0)
        externals = yield txn.dumpExternalDelegatesLocal(u"user01")
        self.assertEqual(len(externals), 0)
        yield self.commitTransaction(1)

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.disableRemoteHome()
        yield syncer.delegateReconcile()

        # Now have local delegates
        txn = self.theTransactionUnderTest(1)

        delegates = yield txn.dumpIndividualDelegatesLocal(u"user01")
        self.assertEqual(
            set(delegates),
            set((
                DelegateRecord.make(delegator="user01", delegate="user02", readWrite=1),
                DelegateRecord.make(delegator="user01", delegate="user03", readWrite=0),
            )),
        )

        delegateGroups = yield txn.dumpGroupDelegatesLocal(u"user01")
        group_top = yield txn.groupByUID(u"__top_group_1__")
        group_right = yield txn.groupByUID(u"right_coast")
        self.assertEqual(
            set([item[0] for item in delegateGroups]),
            set((
                DelegateGroupsRecord.make(delegator="user01", groupID=group_top.groupID, readWrite=1, isExternal=False),
                DelegateGroupsRecord.make(delegator="user01", groupID=group_right.groupID, readWrite=0, isExternal=False),
            )),
        )

        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_delegate_reconcile_directory_based(self):
        """
        Test that L{delegateReconcile} copies over the full set of delegates and caches associated groups..
        """

        # Create remote home
        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.notificationCollectionUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.commitTransaction(0)

        # Add external delegates
        txn = self.theTransactionUnderTest(0)
        yield txn.assignExternalDelegates(u"user01", None, None, u"external1", u"external2")

        yield self.commitTransaction(0)

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.disableRemoteHome()
        yield syncer.delegateReconcile()

        # Now have directory based delegates
        txn = self.theTransactionUnderTest(1)

        externals = yield txn.dumpExternalDelegatesLocal(u"user01")
        self.assertEqual(
            set(externals),
            set((
                ExternalDelegateGroupsRecord.make(
                    delegator="user01",
                    groupUIDRead="external1",
                    groupUIDWrite="external2",
                ),
            )),
        )

        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_notifications_reconcile(self):
        """
        Test that L{delegateReconcile} copies over the full set of delegates and caches associated groups..
        """

        # Create remote home - and add some fake notifications
        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        notifications = yield self.theTransactionUnderTest(0).notificationsWithUID("user01", create=True)
        uid1 = str(uuid4())
        obj1 = yield notifications.writeNotificationObject(uid1, "type1", "data1")
        id1 = obj1.id()
        uid2 = str(uuid4())
        obj2 = yield notifications.writeNotificationObject(uid2, "type2", "data2")
        id2 = obj2.id()
        yield self.commitTransaction(0)

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()
        yield syncer.disableRemoteHome()
        changes = yield syncer.notificationsReconcile()
        self.assertEqual(changes, 2)

        # Now have local notifications
        notifications = yield NotificationCollection.notificationsWithUID(
            self.theTransactionUnderTest(1),
            "user01",
            status=_HOME_STATUS_MIGRATING,
        )
        results = yield notifications.notificationObjects()
        self.assertEqual(len(results), 2)
        for result in results:
            for test_uid, test_id, test_type, test_data in ((uid1, id1, "type1", "data1",), (uid2, id2, "type2", "data2",),):
                if result.uid() == test_uid:
                    self.assertNotEqual(result.id(), test_id)
                    self.assertEqual(json.loads(result.notificationType()), test_type)
                    data = yield result.notificationData()
                    self.assertEqual(json.loads(data), test_data)
                    break
            else:
                self.fail("Notification uid {} not found".format(result.uid()))
        yield self.commitTransaction(1)

    def test_disable_remote_home(self):
        """
        Test that L{disableRemoteHome} changes the remote status and prevents a normal state
        home from being created. Assume remote notification collection exists.
        """
        return self._do_disable_remote_home(notification_exists=True)

    def test_disable_remote_home_no_notifications(self):
        """
        Test that L{disableRemoteHome} changes the remote status and prevents a normal state
        home from being created. Assume remote notification collection does not exist.
        """
        return self._do_disable_remote_home(notification_exists=False)

    @inlineCallbacks
    def _do_disable_remote_home(self, notification_exists=True):
        """
        Test that L{disableRemoteHome} changes the remote status and prevents a normal state
        home from being created.
        """

        self.patch(config.Scheduling.Options.WorkQueues, "RequestDelaySeconds", 1000)

        # Create remote home
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)

        # Create some fake events
        calendar0 = yield home0.childWithName("calendar")
        o1 = yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.caldata1))
        o2 = yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.caldata2))
        o3 = yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.caldata3))

        # Add some fake scheduling work
        yield ScheduleOrganizerWork.schedule(
            self.theTransactionUnderTest(0),
            "uid1",
            "create",
            home0,
            None,
            None,
            Component.fromString(self.caldata1),
            "urn:x-uid:user01",
            2,
            False,
        )
        yield ScheduleReplyWork.reply(
            self.theTransactionUnderTest(0),
            home0,
            o2,
            Component.fromString(self.caldata1),
            "urn:x-uid:user01",
        )
        yield ScheduleRefreshWork.refreshAttendees(
            self.theTransactionUnderTest(0),
            o1,
            Component.fromString(self.caldata1),
            ("urn:x-uid:user02",),
        )
        yield ScheduleAutoReplyWork.autoReply(
            self.theTransactionUnderTest(0),
            o3,
            "ACCEPTED"
        )

        # Notifications
        if notification_exists:
            yield self.theTransactionUnderTest(0).notificationsWithUID("user01", create=True)

        yield self.commitTransaction(0)

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()
        yield syncer.disableRemoteHome()

        # It is disabled
        home = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01")
        self.assertTrue(home is None)
        home = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", status=_HOME_STATUS_NORMAL)
        self.assertTrue(home is None)
        home = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", status=_HOME_STATUS_DISABLED)
        self.assertTrue(home is not None)

        # Work is paused
        jobs = yield JobItem.all(self.theTransactionUnderTest(0))
        count = 0
        for job in jobs:
            if job.workType in (
                ScheduleOrganizerWork.table.model.name,
                ScheduleReplyWork.table.model.name,
                ScheduleRefreshWork.table.model.name,
                ScheduleAutoReplyWork.table.model.name,
            ):
                self.assertTrue(job.pause)
                work = yield job.workItem()
                yield work.delete()
                yield job.delete()
                count += 1
        self.assertEqual(count, 4)

        yield self.commitTransaction(0)

    @inlineCallbacks
    def test_work_items_sync(self):
        """
        Test that L{workItemsReconcile} copies over work items from the remote pod.
        """

        self.patch(config.Scheduling.Options.WorkQueues, "RequestDelaySeconds", 1000)

        # Create remote home
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)

        # Create some fake events
        calendar0 = yield home0.childWithName("calendar")
        o1 = yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.caldata1))
        o2 = yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.caldata2))
        o3 = yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.caldata3))

        # Add some fake scheduling work
        yield ScheduleOrganizerWork.schedule(
            self.theTransactionUnderTest(0),
            "uid1",
            "create",
            home0,
            None,
            None,
            Component.fromString(self.caldata1),
            "urn:x-uid:user01",
            2,
            False,
        )
        yield ScheduleReplyWork.reply(
            self.theTransactionUnderTest(0),
            home0,
            o2,
            Component.fromString(self.caldata1),
            "urn:x-uid:user01",
        )
        yield ScheduleRefreshWork.refreshAttendees(
            self.theTransactionUnderTest(0),
            o1,
            Component.fromString(self.caldata1),
            ("urn:x-uid:user02",),
        )
        yield ScheduleAutoReplyWork.autoReply(
            self.theTransactionUnderTest(0),
            o3,
            "ACCEPTED"
        )

        # Notifications
        yield self.theTransactionUnderTest(0).notificationsWithUID("user01", create=True)

        yield self.commitTransaction(0)

        # Do a full data sync since we need references to the calendar objects
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.sync()

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.prepareCalendarHome()
        count = yield syncer.workItemsReconcile()
        self.assertEqual(count, 4)

        # Work is paused
        pod1Data = {}
        jobs = yield JobItem.all(self.theTransactionUnderTest(1))
        count = 0
        for job in jobs:
            if job.workType in (
                ScheduleOrganizerWork.table.model.name,
                ScheduleReplyWork.table.model.name,
                ScheduleRefreshWork.table.model.name,
                ScheduleAutoReplyWork.table.model.name,
            ):
                self.assertTrue(job.pause)
                work = yield job.workItem()
                pod1Data[job.workType] = work.serialize()
                for attrname in ("homeResourceID", "resourceID", "workID",):
                    del pod1Data[job.workType][attrname]
                yield work.delete()
                yield job.delete()
                count += 1
        self.assertEqual(count, 4)
        yield self.commitTransaction(1)

        # Old work
        pod0Data = {}
        jobs = yield JobItem.all(self.theTransactionUnderTest(0))
        count = 0
        for job in jobs:
            if job.workType in (
                ScheduleOrganizerWork.table.model.name,
                ScheduleReplyWork.table.model.name,
                ScheduleRefreshWork.table.model.name,
                ScheduleAutoReplyWork.table.model.name,
            ):
                work = yield job.workItem()
                pod0Data[job.workType] = work.serialize()
                for attrname in ("homeResourceID", "resourceID", "workID",):
                    del pod0Data[job.workType][attrname]
                yield work.delete()
                yield job.delete()
                count += 1
        self.assertEqual(count, 4)
        yield self.commitTransaction(0)

        self.assertEqual(pod0Data, pod1Data)


class TestSharingSync(MultiStoreConduitTest):
    """
    Test that L{CrossPodHomeSync} sharing sync works.
    """

    @inlineCallbacks
    def setUp(self):
        self.accounts = FilePath(__file__).sibling("accounts").child("groupAccounts.xml")
        self.augments = FilePath(__file__).sibling("accounts").child("augments.xml")
        yield super(TestSharingSync, self).setUp()
        yield self.populate()

    def configure(self):
        super(TestSharingSync, self).configure()
        config.Sharing.Enabled = True
        config.Sharing.Calendars.Enabled = True
        config.Sharing.Calendars.Groups.Enabled = True
        config.Sharing.Calendars.Groups.ReconciliationDelaySeconds = 0

    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.theStoreUnderTest(0))

    requirements = {
        "user01": None,
        "user02": None,
        "user06": None,
        "user07": None,
        "user08": None,
        "user09": None,
        "user10": None,
    }

    @inlineCallbacks
    def _createShare(self, shareFrom, shareTo, accept=True, name="calendar"):
        # Invite
        txnindex = 1 if shareFrom[0] == "p" else 0
        home = yield self.homeUnderTest(txn=self.theTransactionUnderTest(txnindex), name=shareFrom, create=True)
        if name != "calendar":
            yield home.createChildWithName(name)
        calendar = yield home.childWithName(name)
        shareeView = yield calendar.inviteUIDToShare(shareTo, _BIND_MODE_READ, "summary")
        yield self.commitTransaction(txnindex)

        # Accept
        if accept:
            inviteUID = shareeView.shareUID()
            txnindex = 1 if shareTo[0] == "p" else 0
            shareeHome = yield self.homeUnderTest(txn=self.theTransactionUnderTest(txnindex), name=shareTo)
            shareeView = yield shareeHome.acceptShare(inviteUID)
            sharedName = shareeView.name()
            yield self.commitTransaction(txnindex)
        else:
            sharedName = None

        returnValue(sharedName)

    @inlineCallbacks
    def test_shared_collections_reconcile(self):
        """
        Test that L{sharedCollectionsReconcile} copies over the full set of delegates and caches associated groups.
        """

        # Create home
        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.notificationCollectionUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.commitTransaction(0)

        # Shared by migrating user
        shared_name_02 = yield self._createShare("user01", "user02")
        shared_name_03 = yield self._createShare("user01", "puser03")

        # Shared to migrating user
        shared_name_04 = yield self._createShare("user04", "user01")
        shared_name_04a = yield self._createShare("user04", "user01", True, "shared")
        shared_name_05 = yield self._createShare("puser05", "user01")

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.sync()
        yield syncer.disableRemoteHome()
        changes = yield syncer.sharedByCollectionsReconcile()
        self.assertEqual(changes, 2)
        changes = yield syncer.sharedToCollectionsReconcile()
        self.assertEqual(changes, 3)

        # Local calendar exists with shares
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        invites1 = yield calendar1.sharingInvites()
        self.assertEqual(len(invites1), 2)
        self.assertEqual(set([invite.uid for invite in invites1]), set((shared_name_02, shared_name_03,)))
        yield self.commitTransaction(1)

        # Remote sharee can access it
        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user02")
        calendar0 = yield home0.childWithName(shared_name_02)
        self.assertTrue(calendar0 is not None)

        # Local sharee can access it
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="puser03")
        calendar1 = yield home1.childWithName(shared_name_03)
        self.assertTrue(calendar1 is not None)

        # Local shared calendars exist
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName(shared_name_04)
        self.assertTrue(calendar1 is not None)
        calendar1 = yield home1.childWithName(shared_name_05)
        self.assertTrue(calendar1 is not None)
        yield self.commitTransaction(1)

        # Sharers see migrated user as sharee
        externalHome0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", status=_HOME_STATUS_EXTERNAL)
        calendar0 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(0), home="user04", name="calendar")
        invites = yield calendar0.allInvitations()
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0].shareeUID, "user01")
        self.assertEqual(invites[0].shareeHomeID, externalHome0.id())
        yield self.commitTransaction(0)

        shareeHome1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield self.calendarUnderTest(txn=self.theTransactionUnderTest(1), home="puser05", name="calendar")
        invites = yield calendar1.allInvitations()
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0].shareeUID, "user01")
        self.assertEqual(invites[0].shareeHomeID, shareeHome1.id())
        yield self.commitTransaction(1)

    @inlineCallbacks
    def test_group_shared_collections_reconcile(self):
        """
        Test that L{sharedCollectionsReconcile} copies over the full set of delegates and caches associated groups.
        """

        # Create home
        yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.notificationCollectionUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        yield self.commitTransaction(0)

        # Shared by migrating user
        yield self._createShare("user01", "group02", accept=False)

        # Sync from remote side
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        yield syncer.sync()
        yield syncer.disableRemoteHome()
        changes = yield syncer.sharedByCollectionsReconcile()
        self.assertEqual(changes, 3)
        changes = yield syncer.sharedToCollectionsReconcile()
        self.assertEqual(changes, 0)

        # Local calendar exists with shares
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        invites1 = yield calendar1.sharingInvites()
        self.assertEqual(len(invites1), 3)
        sharee = yield GroupShareeRecord.querysimple(self.theTransactionUnderTest(1), calendarID=calendar1.id())
        self.assertEqual(len(sharee), 1)
        group = yield GroupsRecord.querysimple(self.theTransactionUnderTest(1), groupID=sharee[0].groupID)
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0].groupUID, "group02")
        yield self.commitTransaction(1)


class TestGroupAttendeeSync(MultiStoreConduitTest):
    """
    GroupAttendeeReconciliation tests
    """

    now = {"now1": DateTime.getToday().getYear() + 1}

    groupdata1 = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:{now1:04d}0101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
END:VEVENT
END:VCALENDAR""".format(**now)

    groupdata2 = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:{now1:04d}0101T100000Z
DURATION:PT1H
SUMMARY:event 2
UID:event2@ninevah.local
ORGANIZER:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:group02@example.com
END:VEVENT
END:VCALENDAR""".format(**now)

    groupdata3 = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:{now1:04d}0101T100000Z
DURATION:PT1H
SUMMARY:event 3
UID:event3@ninevah.local
ORGANIZER:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:group04@example.com
END:VEVENT
END:VCALENDAR""".format(**now)

    @inlineCallbacks
    def setUp(self):
        self.accounts = FilePath(__file__).sibling("accounts").child("groupAccounts.xml")
        yield super(TestGroupAttendeeSync, self).setUp()
        yield self.populate()

    def configure(self):
        super(TestGroupAttendeeSync, self).configure()
        config.GroupAttendees.Enabled = True
        config.GroupAttendees.ReconciliationDelaySeconds = 0
        config.GroupAttendees.AutoUpdateSecondsFromNow = 0

    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.theStoreUnderTest(0))

    requirements = {
        "user01": None,
        "user02": None,
        "user06": None,
        "user07": None,
        "user08": None,
        "user09": None,
        "user10": None,
    }

    @inlineCallbacks
    def test_group_attendees(self):
        """
        Test that L{groupAttendeeReconcile} links groups to the associated calendar object.
        """

        home0 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(0), name="user01", create=True)
        calendar0 = yield home0.childWithName("calendar")
        yield calendar0.createCalendarObjectWithName("1.ics", Component.fromString(self.groupdata1))
        yield calendar0.createCalendarObjectWithName("2.ics", Component.fromString(self.groupdata2))
        yield calendar0.createCalendarObjectWithName("3.ics", Component.fromString(self.groupdata3))
        yield self.commitTransaction(0)

        yield JobItem.waitEmpty(self.theStoreUnderTest(0).newTransaction, reactor, 60.0)

        # Trigger sync
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.sync()

        # Link groups
        len_links = yield syncer.groupAttendeeReconcile()
        self.assertEqual(len_links, 2)

        # Local calendar exists
        home1 = yield self.homeUnderTest(txn=self.theTransactionUnderTest(1), name="user01", status=_HOME_STATUS_MIGRATING)
        calendar1 = yield home1.childWithName("calendar")
        self.assertTrue(calendar1 is not None)
        children = yield calendar1.objectResources()
        self.assertEqual(set([child.name() for child in children]), set(("1.ics", "2.ics", "3.ics",)))

        object2 = yield calendar1.objectResourceWithName("2.ics")
        record = (yield object2.groupEventLinks()).values()[0]
        group02 = yield self.theTransactionUnderTest(1).groupByUID(u"group02")
        self.assertEqual(record.groupID, group02.groupID)
        self.assertEqual(record.membershipHash, group02.membershipHash)

        object3 = yield calendar1.objectResourceWithName("3.ics")
        record = (yield object3.groupEventLinks()).values()[0]
        group04 = yield self.theTransactionUnderTest(1).groupByUID(u"group04")
        self.assertEqual(record.groupID, group04.groupID)
        self.assertEqual(record.membershipHash, group04.membershipHash)


class TestiMIPTokensSync(MultiStoreConduitTest):
    """
    Test that L{CrossPodHomeSync} iMIP token sync works.
    """

    @inlineCallbacks
    def setUp(self):
        self.accounts = FilePath(__file__).sibling("accounts").child("groupAccounts.xml")
        self.augments = FilePath(__file__).sibling("accounts").child("augments.xml")
        yield super(TestiMIPTokensSync, self).setUp()
        yield self.populate()

    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.theStoreUnderTest(0))

    requirements = {
        "user01": None,
        "user02": None,
        "user06": None,
        "user07": None,
        "user08": None,
        "user09": None,
        "user10": None,
    }

    @inlineCallbacks
    def _createTokens(self, txn, organizer, attendee_prefix, number):

        for n in range(number):
            yield iMIPTokenRecord.create(
                txn,
                token=str(uuid4()),
                organizer=organizer,
                attendee="mailto:{}{}@example.com".format(attendee_prefix, n + 1),
                icaluid=str(uuid4()),
            )

    @inlineCallbacks
    def test_token_sync(self):
        """
        Test that L{iMIPTokensReconcile} copies over the full set of iMIP tokens.
        """

        # Start with tokens on each pod for different sets of users
        txn = self.theTransactionUnderTest(0)
        yield self._createTokens(txn, "urn:x-uid:user01", "xyz_user01", 10)
        yield self._createTokens(txn, "urn:x-uid:user02", "xyz_user02", 10)
        yield self.commitTransaction(0)

        txn = self.theTransactionUnderTest(1)
        yield self._createTokens(txn, "urn:x-uid:puser01", "xyz_puser01", 10)
        yield self._createTokens(txn, "urn:x-uid:puser02", "xyz_puser02", 10)
        yield self.commitTransaction(1)

        # Double-check tokens are there
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:user01")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:user02")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:puser01")
        self.assertEqual(len(records), 0)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:puser02")
        self.assertEqual(len(records), 0)
        yield self.commitTransaction(0)

        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:user01")
        self.assertEqual(len(records), 0)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:user02")
        self.assertEqual(len(records), 0)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:puser01")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:puser02")
        self.assertEqual(len(records), 10)
        yield self.commitTransaction(1)

        # Do the sync
        syncer = CrossPodHomeSync(self.theStoreUnderTest(1), "user01")
        yield syncer.loadRecord()
        count = yield syncer.iMIPTokensReconcile()
        self.assertEqual(count, 10)

        # Tokens have been copied - original still in place
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:user01")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:user02")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:puser01")
        self.assertEqual(len(records), 0)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(0), iMIPTokenRecord.organizer == "urn:x-uid:puser02")
        self.assertEqual(len(records), 0)
        yield self.commitTransaction(0)

        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:user01")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:user02")
        self.assertEqual(len(records), 0)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:puser01")
        self.assertEqual(len(records), 10)
        records = yield iMIPTokenRecord.query(self.theTransactionUnderTest(1), iMIPTokenRecord.organizer == "urn:x-uid:puser02")
        self.assertEqual(len(records), 10)
        yield self.commitTransaction(1)
