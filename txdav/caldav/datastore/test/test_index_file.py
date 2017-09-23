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

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.internet.task import deferLater

from txdav.caldav.datastore.index_file import Index, MemcachedUIDReserver
from txdav.caldav.datastore.query.filter import Filter
from txdav.common.icommondatastore import ReservationError, \
    InternalDataStoreError

from twistedcaldav import caldavxml
from twistedcaldav.caldavxml import TimeRange
from twistedcaldav.ical import Component, InvalidICalendarDataError
from twistedcaldav.instance import InvalidOverriddenInstanceError
from twistedcaldav.test.util import InMemoryMemcacheProtocol
import twistedcaldav.test.util

from pycalendar.datetime import DateTime

import os


class MinimalCalendarObjectReplacement(object):
    """
    Provide the minimal set of attributes and methods from CalDAVFile required
    by L{Index}.
    """

    def __init__(self, filePath):
        self.fp = filePath

    def iCalendar(self):
        with self.fp.open() as f:
            text = f.read()
        try:
            component = Component.fromString(text)
            # Fix any bogus data we can
            component.validCalendarData()
            component.validCalendarForCalDAV(methodAllowed=False)
        except InvalidICalendarDataError as e:
            raise InternalDataStoreError(
                "File corruption detected (%s) in file: %s"
                % (e, self._path.path)
            )
        return component


class MinimalResourceReplacement(object):
    """
    Provide the minimal set of attributes and methods from CalDAVFile required
    by L{Index}.
    """

    class MinimalTxn(object):

        def postCommit(self, _ignore):
            pass

        def postAbort(self, _ignore):
            pass

    def __init__(self, filePath):
        self.fp = filePath
        self._txn = MinimalResourceReplacement.MinimalTxn()

    def isCalendarCollection(self):
        return True

    def getChild(self, name):
        # FIXME: this should really return something with a child method
        return MinimalCalendarObjectReplacement(self.fp.child(name))

    def initSyncToken(self):
        pass


class SQLIndexTests (twistedcaldav.test.util.TestCase):
    """
    Test abstract SQL DB class
    """

    def setUp(self):
        super(SQLIndexTests, self).setUp()
        self.site.resource.isCalendarCollection = lambda: True
        self.indexDirPath = self.site.resource.fp
        # FIXME: since this resource lies about isCalendarCollection, it doesn't
        # have all the associated backend machinery to actually get children.
        self.db = Index(MinimalResourceReplacement(self.indexDirPath))

    def tearDown(self):
        self.db._db_close()

    def test_reserve_uid_ok(self):
        uid = "test-test-test"
        d = self.db.isReservedUID(uid)
        d.addCallback(self.assertFalse)
        d.addCallback(lambda _: self.db.reserveUID(uid))
        d.addCallback(lambda _: self.db.isReservedUID(uid))
        d.addCallback(self.assertTrue)
        d.addCallback(lambda _: self.db.unreserveUID(uid))
        d.addCallback(lambda _: self.db.isReservedUID(uid))
        d.addCallback(self.assertFalse)

        return d

    def test_reserve_uid_twice(self):
        uid = "test-test-test"
        d = self.db.reserveUID(uid)
        d.addCallback(lambda _: self.db.isReservedUID(uid))
        d.addCallback(self.assertTrue)
        d.addCallback(lambda _:
                      self.assertFailure(self.db.reserveUID(uid),
                                         ReservationError))
        return d

    def test_unreserve_unreserved(self):
        uid = "test-test-test"
        return self.assertFailure(self.db.unreserveUID(uid),
                                  ReservationError)

    def test_reserve_uid_timeout(self):
        # WARNING: This test is fundamentally flawed and will fail
        # intermittently because it uses the real clock.
        uid = "test-test-test"
        from twistedcaldav.config import config
        old_timeout = config.UIDReservationTimeOut
        config.UIDReservationTimeOut = 1

        def _finally():
            config.UIDReservationTimeOut = old_timeout

        d = self.db.isReservedUID(uid)
        d.addCallback(self.assertFalse)
        d.addCallback(lambda _: self.db.reserveUID(uid))
        d.addCallback(lambda _: self.db.isReservedUID(uid))
        d.addCallback(self.assertTrue)
        d.addCallback(lambda _: deferLater(reactor, 2, lambda: None))
        d.addCallback(lambda _: self.db.isReservedUID(uid))
        d.addCallback(self.assertFalse)
        self.addCleanup(_finally)

        return d

    def test_index(self):
        data = (
            (
                "#1.1 Simple component",
                "1.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                False,
                True,
            ),
            (
                "#2.1 Recurring component",
                "2.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
""",
                False,
                True,
            ),
            (
                "#2.2 Recurring component with override",
                "2.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.2
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-2.2
RECURRENCE-ID:20080608T120000Z
DTSTART:20080608T120000Z
DTEND:20080608T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                False,
                True,
            ),
            (
                "#2.3 Recurring component with broken override - new",
                "2.3",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.3
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-2.3
RECURRENCE-ID:20080609T120000Z
DTSTART:20080608T120000Z
DTEND:20080608T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                False,
                False,
            ),
            (
                "#2.4 Recurring component with broken override - existing",
                "2.4",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.4
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-2.4
RECURRENCE-ID:20080609T120000Z
DTSTART:20080608T120000Z
DTEND:20080608T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                True,
                True,
            ),
        )

        for description, name, calendar_txt, reCreate, ok in data:
            calendar = Component.fromString(calendar_txt)
            if ok:
                with open(os.path.join(self.indexDirPath.path, name), "w") as f:
                    f.write(calendar_txt)

                self.db.addResource(name, calendar, reCreate=reCreate)
                self.assertTrue(self.db.resourceExists(name), msg=description)
            else:
                self.assertRaises(InvalidOverriddenInstanceError, self.db.addResource, name, calendar)
                self.assertFalse(self.db.resourceExists(name), msg=description)

        self.db._db_recreate()
        for description, name, calendar_txt, reCreate, ok in data:
            if ok:
                self.assertTrue(self.db.resourceExists(name), msg=description)
            else:
                self.assertFalse(self.db.resourceExists(name), msg=description)

        self.db.testAndUpdateIndex(DateTime(2020, 1, 1))
        for description, name, calendar_txt, reCreate, ok in data:
            if ok:
                self.assertTrue(self.db.resourceExists(name), msg=description)
            else:
                self.assertFalse(self.db.resourceExists(name), msg=description)

    @inlineCallbacks
    def test_index_timerange(self):
        """
        A plain (not freebusy) time range test.
        """
        data = (
            (
                "#1.1 Simple component - busy",
                "1.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                "20080601T000000Z", "20080602T000000Z",
            ),
            (
                "#1.2 Simple component - transparent",
                "1.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.2
DTSTART:20080602T120000Z
DTEND:20080602T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
""",
                "20080602T000000Z", "20080603T000000Z",
            ),
            (
                "#1.3 Simple component - canceled",
                "1.3",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.3
DTSTART:20080603T120000Z
DTEND:20080603T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
""",
                "20080603T000000Z", "20080604T000000Z",
            ),
            (
                "#1.4 Simple component - tentative",
                "1.4",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.4
DTSTART:20080604T120000Z
DTEND:20080604T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
STATUS:TENTATIVE
END:VEVENT
END:VCALENDAR
""",
                "20080604T000000Z", "20080605T000000Z",
            ),
            (
                "#2.1 Recurring component - busy",
                "2.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.1
DTSTART:20080605T120000Z
DTEND:20080605T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
END:VCALENDAR
""",
                "20080605T000000Z", "20080607T000000Z",
            ),
            (
                "#2.2 Recurring component - busy",
                "2.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.2
DTSTART:20080607T120000Z
DTEND:20080607T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-2.2
RECURRENCE-ID:20080608T120000Z
DTSTART:20080608T140000Z
DTEND:20080608T150000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
""",
                "20080607T000000Z", "20080609T000000Z",
            ),
        )

        for description, name, calendar_txt, trstart, trend in data:
            calendar = Component.fromString(calendar_txt)

            with open(os.path.join(self.indexDirPath.path, name), "w") as f:
                f.write(calendar_txt)

            self.db.addResource(name, calendar)
            self.assertTrue(self.db.resourceExists(name), msg=description)

            # Create fake filter element to match time-range
            filter = caldavxml.Filter(
                caldavxml.ComponentFilter(
                    caldavxml.ComponentFilter(
                        TimeRange(
                            start=trstart,
                            end=trend,
                        ),
                        name=("VEVENT", "VFREEBUSY", "VAVAILABILITY"),
                    ),
                    name="VCALENDAR",
                )
            )
            filter = Filter(filter)

            resources = yield self.db.indexedSearch(filter)
            index_results = set()
            for found_name, _ignore_uid, _ignore_type in resources:
                index_results.add(found_name)

            self.assertEqual(set((name,)), index_results, msg=description)

    @inlineCallbacks
    def test_index_timespan(self):
        data = (
            (
                "#1.1 Simple component - busy",
                "1.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
""",
                "20080601T000000Z", "20080602T000000Z",
                "mailto:user1@example.com",
                (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),),
            ),
            (
                "#1.2 Simple component - transparent",
                "1.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.2
DTSTART:20080602T120000Z
DTEND:20080602T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
""",
                "20080602T000000Z", "20080603T000000Z",
                "mailto:user1@example.com",
                (('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'T'),),
            ),
            (
                "#1.3 Simple component - canceled",
                "1.3",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.3
DTSTART:20080603T120000Z
DTEND:20080603T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
""",
                "20080603T000000Z", "20080604T000000Z",
                "mailto:user1@example.com",
                (('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'F', 'F'),),
            ),
            (
                "#1.4 Simple component - tentative",
                "1.4",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.4
DTSTART:20080604T120000Z
DTEND:20080604T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
STATUS:TENTATIVE
END:VEVENT
END:VCALENDAR
""",
                "20080604T000000Z", "20080605T000000Z",
                "mailto:user1@example.com",
                (('N', "2008-06-04 12:00:00", "2008-06-04 13:00:00", 'T', 'F'),),
            ),
            (
                "#2.1 Recurring component - busy",
                "2.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.1
DTSTART:20080605T120000Z
DTEND:20080605T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
END:VCALENDAR
""",
                "20080605T000000Z", "20080607T000000Z",
                "mailto:user1@example.com",
                (
                    ('N', "2008-06-05 12:00:00", "2008-06-05 13:00:00", 'B', 'F'),
                    ('N', "2008-06-06 12:00:00", "2008-06-06 13:00:00", 'B', 'F'),
                ),
            ),
            (
                "#2.2 Recurring component - busy",
                "2.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.2
DTSTART:20080607T120000Z
DTEND:20080607T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-2.2
RECURRENCE-ID:20080608T120000Z
DTSTART:20080608T140000Z
DTEND:20080608T150000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
""",
                "20080607T000000Z", "20080609T000000Z",
                "mailto:user1@example.com",
                (
                    ('N', "2008-06-07 12:00:00", "2008-06-07 13:00:00", 'B', 'F'),
                    ('N', "2008-06-08 14:00:00", "2008-06-08 15:00:00", 'B', 'T'),
                ),
            ),
        )

        for description, name, calendar_txt, trstart, trend, organizer, instances in data:
            calendar = Component.fromString(calendar_txt)

            with open(os.path.join(self.indexDirPath.path, name), "w") as f:
                f.write(calendar_txt)

            self.db.addResource(name, calendar)
            self.assertTrue(self.db.resourceExists(name), msg=description)

            # Create fake filter element to match time-range
            filter = caldavxml.Filter(
                caldavxml.ComponentFilter(
                    caldavxml.ComponentFilter(
                        TimeRange(
                            start=trstart,
                            end=trend,
                        ),
                        name=("VEVENT", "VFREEBUSY", "VAVAILABILITY"),
                    ),
                    name="VCALENDAR",
                )
            )
            filter = Filter(filter)

            resources = yield self.db.indexedSearch(filter, fbtype=True)
            index_results = set()
            for _ignore_name, _ignore_uid, type, test_organizer, float, start, end, fbtype, transp in resources:
                self.assertEqual(test_organizer, organizer, msg=description)
                index_results.add((float, start, end, fbtype, transp,))

            self.assertEqual(set(instances), index_results, msg=description)

    @inlineCallbacks
    def test_index_timespan_per_user(self):
        data = (
            (
                "#1.1 Single per-user non-recurring component",
                "1.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.1
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080602T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),),
                    ),
                    (
                        "user02",
                        (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),),
                    ),
                ),
            ),
            (
                "#1.2 Two per-user non-recurring component",
                "1.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.2
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080602T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),),
                    ),
                    (
                        "user02",
                        (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),),
                    ),
                    (
                        "user03",
                        (('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),),
                    ),
                ),
            ),
            (
                "#2.1 Single per-user simple recurring component",
                "2.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.1
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080603T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),
                            ('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'T'),
                        ),
                    ),
                    (
                        "user02",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'F'),
                        ),
                    ),
                ),
            ),
            (
                "#2.2 Two per-user simple recurring component",
                "2.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.2
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080603T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),
                            ('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'T'),
                        ),
                    ),
                    (
                        "user02",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'F'),
                        ),
                    ),
                    (
                        "user03",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 12:00:00", "2008-06-02 13:00:00", 'B', 'F'),
                        ),
                    ),
                ),
            ),
            (
                "#3.1 Single per-user complex recurring component",
                "3.1",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-1.1
RECURRENCE-ID:20080602T120000Z
DTSTART:20080602T130000Z
DTEND:20080602T140000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.1
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:20080602T120000Z
TRANSP:OPAQUE
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080604T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),
                            ('N', "2008-06-02 13:00:00", "2008-06-02 14:00:00", 'B', 'F'),
                            ('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'B', 'T'),
                        ),
                    ),
                    (
                        "user02",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 13:00:00", "2008-06-02 14:00:00", 'B', 'F'),
                            ('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'B', 'F'),
                        ),
                    ),
                ),
            ),
            (
                "#3.2 Two per-user complex recurring component",
                "3.2",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.2
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:VEVENT
UID:12345-67890-1.2
RECURRENCE-ID:20080602T120000Z
DTSTART:20080602T130000Z
DTEND:20080602T140000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:20080602T120000Z
TRANSP:OPAQUE
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890-1.2
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Test
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:20080603T120000Z
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
""",
                "20080601T000000Z", "20080604T000000Z",
                "mailto:user1@example.com",
                (
                    (
                        "user01",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'T'),
                            ('N', "2008-06-02 13:00:00", "2008-06-02 14:00:00", 'B', 'F'),
                            ('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'B', 'T'),
                        ),
                    ),
                    (
                        "user02",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 13:00:00", "2008-06-02 14:00:00", 'B', 'F'),
                            ('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'B', 'T'),
                        ),
                    ),
                    (
                        "user03",
                        (
                            ('N', "2008-06-01 12:00:00", "2008-06-01 13:00:00", 'B', 'F'),
                            ('N', "2008-06-02 13:00:00", "2008-06-02 14:00:00", 'B', 'F'),
                            ('N', "2008-06-03 12:00:00", "2008-06-03 13:00:00", 'B', 'F'),
                        ),
                    ),
                ),
            ),
        )

        for description, name, calendar_txt, trstart, trend, organizer, peruserinstances in data:
            calendar = Component.fromString(calendar_txt)

            with open(os.path.join(self.indexDirPath.path, name), "w") as f:
                f.write(calendar_txt)

            self.db.addResource(name, calendar)
            self.assertTrue(self.db.resourceExists(name), msg=description)

            # Create fake filter element to match time-range
            filter = caldavxml.Filter(
                caldavxml.ComponentFilter(
                    caldavxml.ComponentFilter(
                        TimeRange(
                            start=trstart,
                            end=trend,
                        ),
                        name=("VEVENT", "VFREEBUSY", "VAVAILABILITY"),
                    ),
                    name="VCALENDAR",
                )
            )
            filter = Filter(filter)

            for useruid, instances in peruserinstances:
                resources = yield self.db.indexedSearch(filter, useruid=useruid, fbtype=True)
                index_results = set()
                for _ignore_name, _ignore_uid, type, test_organizer, float, start, end, fbtype, transp in resources:
                    self.assertEqual(test_organizer, organizer, msg=description)
                    index_results.add((str(float), str(start), str(end), str(fbtype), str(transp),))

                self.assertEqual(set(instances), index_results, msg="%s, user:%s" % (description, useruid,))

            self.db.deleteResource(name)

    def test_index_revisions(self):
        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-1.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.1
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""
        data3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-2.3
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
DTSTAMP:20080601T120000Z
ORGANIZER;CN="User 01":mailto:user1@example.com
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""

        calendar = Component.fromString(data1)
        self.db.addResource("data1.ics", calendar)
        calendar = Component.fromString(data2)
        self.db.addResource("data2.ics", calendar)
        calendar = Component.fromString(data3)
        self.db.addResource("data3.ics", calendar)
        self.db.deleteResource("data3.ics")

        tests = (
            (0, (["data1.ics", "data2.ics", ], [], [],)),
            (1, (["data2.ics", ], ["data3.ics", ], [],)),
            (2, ([], ["data3.ics", ], [],)),
            (3, ([], ["data3.ics", ], [],)),
            (4, ([], [], [],)),
            (5, ([], [], [],)),
        )

        for revision, results in tests:
            self.assertEquals(self.db.whatchanged(revision), results, "Mismatched results for whatchanged with revision %d" % (revision,))


class MemcacheTests(SQLIndexTests):

    def setUp(self):
        super(MemcacheTests, self).setUp()
        self.memcache = InMemoryMemcacheProtocol()
        self.db.reserver = MemcachedUIDReserver(self.db, self.memcache)

    def tearDown(self):
        super(MemcacheTests, self).tearDown()
        for _ignore_k, v in self.memcache._timeouts.iteritems():
            if v.active():
                v.cancel()
