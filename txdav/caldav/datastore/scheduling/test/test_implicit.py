from __future__ import print_function
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
from pycalendar.timezone import Timezone

from txweb2 import responsecode
from txweb2.http import HTTPError

from twisted.internet import reactor
from twisted.internet.defer import succeed, inlineCallbacks, returnValue
from twisted.internet.task import deferLater
from twisted.trial.unittest import TestCase

from twistedcaldav.config import config
from twistedcaldav.ical import Component
from twistedcaldav.timezones import TimezoneCache

from txdav.caldav.datastore.scheduling.cuaddress import LocalCalendarUser
from txdav.caldav.datastore.scheduling.implicit import ImplicitScheduler
from txdav.caldav.datastore.scheduling.scheduler import ScheduleResponseQueue
from txdav.caldav.icalendarstore import AttendeeAllowedError, \
    ComponentUpdateState
from txdav.caldav.datastore.sql import CalendarObject
from txdav.common.datastore.test.util import CommonCommonTests, populateCalendarsFrom

from twext.enterprise.jobs.jobitem import JobItem
from twext.python.clsprop import classproperty

import hashlib
import sys


class FakeScheduler(object):
    """
    A fake CalDAVScheduler that does nothing except track who messages were sent to.
    """

    def __init__(self, recipients):
        self.recipients = recipients

    def doSchedulingViaPUT(self, originator, recipients, calendar, internal_request=False, suppress_refresh=False):
        self.recipients.extend(recipients)
        return succeed(ScheduleResponseQueue("FAKE", responsecode.OK))


class Implicit(CommonCommonTests, TestCase):
    """
    iCalendar support tests
    """

    @inlineCallbacks
    def setUp(self):
        yield super(Implicit, self).setUp()
        yield self.buildStoreAndDirectory()

    @inlineCallbacks
    def test_removed_attendees(self):

        data = (
            (
                "#1.1 Simple component, no change",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                (),
            ),
            (
                "#1.2 Simple component, one removal",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
""",
                (("mailto:user02@example.com", None),),
            ),
            (
                "#1.3 Simple component, two removals",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user02@example.com", None),
                    ("mailto:user03@example.com", None),
                ),
            ),
            (
                "#2.1 Simple recurring component, two removals",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user02@example.com", None),
                    ("mailto:user03@example.com", None),
                ),
            ),
            (
                "#2.2 Simple recurring component, add exdate",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
EXDATE:20080801T120000Z
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user01@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#2.3 Simple recurring component, add multiple comma exdates",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
EXDATE:20080801T120000Z,20080901T120000Z
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user01@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user01@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#2.3 Simple recurring component, add multiple comma/property exdates",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
EXDATE:20080801T120000Z,20080901T120000Z
EXDATE:20081201T120000Z
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user01@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user01@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 9, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user01@example.com", DateTime(2008, 12, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 12, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 12, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#3.1 Complex recurring component with same attendees, no change",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                (),
            ),
            (
                "#3.2 Complex recurring component with same attendees, change master/override",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user03@example.com", None),
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#3.3 Complex recurring component with same attendees, change override",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#3.4 Complex recurring component with same attendees, change master",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user03@example.com", None),
                ),
            ),
            (
                "#3.5 Complex recurring component with same attendees, remove override - no exdate",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                (),
            ),
            (
                "#3.6 Complex recurring component with same attendees, remove override - exdate",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
EXDATE:20080801T120000Z
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user01@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user03@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#4.1 Complex recurring component with different attendees, change master/override",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user04@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user03@example.com", None),
                    ("mailto:user04@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#4.2 Complex recurring component with different attendees, remove override - no exdate",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user04@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user04@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
            (
                "#4.3 Complex recurring component with different attendees, remove override - exdate",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20080801T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user04@example.com
END:VEVENT
END:VCALENDAR
""",
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=MONTHLY
EXDATE:20080801T120000Z
END:VEVENT
END:VCALENDAR
""",
                (
                    ("mailto:user01@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user02@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                    ("mailto:user04@example.com", DateTime(2008, 8, 1, 12, 0, 0, tzid=Timezone.UTCTimezone)),
                ),
            ),
        )

        for description, calendar1, calendar2, result in data:
            scheduler = ImplicitScheduler()
            scheduler.resource = None
            scheduler.oldcalendar = Component.fromString(calendar1)
            scheduler.oldAttendeesByInstance = scheduler.oldcalendar.getAttendeesByInstance(True, onlyScheduleAgentServer=True)
            scheduler.oldInstances = set(scheduler.oldcalendar.getComponentInstances())
            scheduler.calendar = Component.fromString(calendar2)

            txn = self.transactionUnderTest()
            scheduler.txn = txn
            scheduler.calendar_home = yield self.homeUnderTest(txn=txn, name=u"user01", create=True)

            yield scheduler.extractCalendarData()
            scheduler.findRemovedAttendees()
            self.assertEqual(scheduler.cancelledAttendees, set(result), msg=description)
            yield self.commit()

    @inlineCallbacks
    def test_process_request_excludes_includes(self):
        """
        Test that processRequests correctly excludes or includes the specified attendees.
        """

        data = (
            ((), None, 3, ("mailto:user02@example.com", "mailto:user03@example.com", "mailto:user04@example.com",),),
            (("mailto:user02@example.com",), None, 2, ("mailto:user03@example.com", "mailto:user04@example.com",),),
            ((), ("mailto:user02@example.com", "mailto:user04@example.com",), 2, ("mailto:user02@example.com", "mailto:user04@example.com",),),
        )

        calendar = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
ATTENDEE:mailto:user04@example.com
END:VEVENT
END:VCALENDAR
"""

        for excludes, includes, result_count, result_set in data:
            scheduler = ImplicitScheduler()
            scheduler.resource = None
            scheduler.calendar = Component.fromString(calendar)
            scheduler.state = "organizer"
            scheduler.action = "modify"
            scheduler.internal_request = True
            scheduler.except_attendees = excludes
            scheduler.only_refresh_attendees = includes
            scheduler.changed_rids = None
            scheduler.reinvites = None

            txn = self.transactionUnderTest()
            scheduler.txn = txn
            scheduler.calendar_home = yield self.homeUnderTest(txn=txn, name=u"user01", create=True)

            # Get some useful information from the calendar
            yield scheduler.extractCalendarData()
            record = yield self.directory.recordWithUID(scheduler.calendar_home.uid())
            scheduler.organizerAddress = LocalCalendarUser(
                "mailto:user01@example.com",
                record,
            )

            recipients = []

            def makeFakeScheduler():
                return FakeScheduler(recipients)
            scheduler.makeScheduler = makeFakeScheduler

            count = (yield scheduler.processRequests())
            self.assertEqual(count, result_count)
            self.assertEqual(len(recipients), result_count)
            self.assertEqual(set(recipients), set(result_set))
            yield self.commit()


class ImplicitRequests(CommonCommonTests, TestCase):
    """
    Test txdav.caldav.datastore.scheduling.implicit.
    """

    @inlineCallbacks
    def setUp(self):
        yield super(ImplicitRequests, self).setUp()
        yield self.buildStoreAndDirectory()
        yield self.populate()

    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()

    @classproperty(cache=False)
    def requirements(cls):  # @NoSelf
        return {
            "user01": {
                "calendar_1": {
                },
                "inbox": {
                },
            },
            "user02": {
                "calendar_1": {
                },
                "inbox": {
                },
            },
            "user03": {
                "calendar_1": {
                },
                "inbox": {
                },
            },
        }

    @inlineCallbacks
    def _createCalendarObject(self, data, user, name):
        calendar_collection = (yield self.calendarUnderTest(home=user))
        yield calendar_collection.createCalendarObjectWithName("test.ics", Component.fromString(data))
        yield self.commit()

    @inlineCallbacks
    def _listCalendarObjects(self, user, collection_name="calendar_1"):
        collection = (yield self.calendarUnderTest(name=collection_name, home=user))
        items = (yield collection.listCalendarObjects())
        yield self.commit()
        returnValue(items)

    @inlineCallbacks
    def _getCalendarData(self, user, name=None):
        if name is None:
            items = (yield self._listCalendarObjects(user))
            name = items[0]

        calendar_resource = (yield self.calendarObjectUnderTest(name=name, home=user))
        calendar = (yield calendar_resource.component())
        yield self.commit()
        returnValue(str(calendar).replace("\r\n ", ""))

    @inlineCallbacks
    def _setCalendarData(self, data, user, name=None):
        if name is None:
            items = (yield self._listCalendarObjects(user))
            name = items[0]

        calendar_resource = (yield self.calendarObjectUnderTest(name=name, home=user))
        yield calendar_resource.setComponent(Component.fromString(data))
        yield self.commit()

    @inlineCallbacks
    def test_testImplicitSchedulingPUT_ScheduleState(self):
        """
        Test that checkImplicitState() always returns True for any organizer, valid or not.
        """

        data = (
            (
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
END:VEVENT
END:VCALENDAR
""",
                False,
            ),
            (
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""",
                True,
            ),
            (
                """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:bogus@bogus.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:bogus@bogus.com
END:VEVENT
END:VCALENDAR
""",
                True,
            ),
        )

        calendar_collection = (yield self.calendarUnderTest(home="user01"))
        for calendar, result in data:
            calendar = Component.fromString(calendar)

            scheduler = ImplicitScheduler()
            doAction, isScheduleObject = (yield scheduler.testImplicitSchedulingPUT(calendar_collection, None, calendar, False))
            self.assertEqual(doAction, result)
            self.assertEqual(isScheduleObject, result)

    @inlineCallbacks
    def test_testImplicitSchedulingPUT_FixScheduleState(self):
        """
        Test that testImplicitSchedulingPUT will fix an old cached schedule object state by
        re-evaluating the calendar data.
        """

        calendarOld = Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""")

        calendarNew = Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""")

        calendar_collection = (yield self.calendarUnderTest(home="user01"))
        calresource = (yield calendar_collection.createCalendarObjectWithName(
            "1.ics", calendarOld
        ))
        calresource.isScheduleObject = False

        scheduler = ImplicitScheduler()
        try:
            doAction, isScheduleObject = (yield scheduler.testImplicitSchedulingPUT(calendar_collection, calresource, calendarNew, False))
        except Exception as e:
            print(e)
            self.fail("Exception must not be raised")
        self.assertTrue(doAction)
        self.assertTrue(isScheduleObject)

    @inlineCallbacks
    def test_testImplicitSchedulingPUT_NoChangeScheduleState(self):
        """
        Test that testImplicitSchedulingPUT will prevent attendees from changing the
        schedule object state.
        """

        calendarOld = Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
END:VEVENT
END:VCALENDAR
""")

        calendarNew = Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 02":mailto:user02@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
""")

        calendar_collection = (yield self.calendarUnderTest(home="user01"))
        calresource = (yield calendar_collection.createCalendarObjectWithName(
            "1.ics", calendarOld
        ))
        calresource.isScheduleObject = False

        scheduler = ImplicitScheduler()
        try:
            yield scheduler.testImplicitSchedulingPUT(calendar_collection, calresource, calendarNew, False)
        except HTTPError:
            pass
        except:
            self.fail("HTTPError exception must be raised")
        else:
            self.fail("Exception must be raised")

    @inlineCallbacks
    def test_doImplicitScheduling_NewOrganizerEvent(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data, "user01", "test.ics")

        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))

    @inlineCallbacks
    def test_doImplicitScheduling_UpdateOrganizerEvent(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T130000Z
DTEND:20080601T140000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user01", "test.ics")

        yield self._setCalendarData(data2, "user01", "test.ics")

        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 2)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))
        self.assertTrue(list2[1].startswith(hashlib.md5("12345-67890").hexdigest()))

    @inlineCallbacks
    def test_doImplicitScheduling_DeleteOrganizerEvent(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user01", "test.ics")

        calendar_resource = (yield self.calendarObjectUnderTest(name="test.ics", home="user01"))
        yield calendar_resource.remove()
        yield self.commit()

        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 2)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))
        self.assertTrue(list2[1].startswith(hashlib.md5("12345-67890").hexdigest()))

    @inlineCallbacks
    def test_doImplicitScheduling_UpdateMailtoOrganizerEvent(self):
        """
        Test that doImplicitScheduling works when the existing calendar data contains a non-normalized
        organizer calendar user address.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01";SCHEDULE-AGENT=NONE:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20080601T130000Z
DTEND:20080601T140000Z
ORGANIZER;CN="User 01";SCHEDULE-AGENT=NONE:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""

        self.patch(CalendarObject.CalendarObjectUpgradeWork, "delay", 1)

        yield self._createCalendarObject(data1, "user01", "test.ics")

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        actualVersion = CalendarObject._currentDataVersion
        self.patch(CalendarObject, "_currentDataVersion", 0)
        yield cobj._setComponentInternal(Component.fromString(data1), internal_state=ComponentUpdateState.RAW)
        CalendarObject._currentDataVersion = actualVersion
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        comp = yield cobj.component()
        # Because CUA normalization happens in component() now too...
        self.assertTrue(comp.getOrganizer().startswith("urn:x-uid:"))
        self.assertFalse(comp.getOrganizerScheduleAgent())
        yield self.commit()

        yield JobItem.waitEmpty(self.storeUnderTest().newTransaction, reactor, 60)

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        comp = yield cobj.component()
        # Because CUA normalization happens in component() now too...
        self.assertTrue(comp.getOrganizer().startswith("urn:x-uid:"))
        self.assertFalse(comp.getOrganizerScheduleAgent())
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        actualVersion = CalendarObject._currentDataVersion
        self.patch(CalendarObject, "_currentDataVersion", 0)
        yield cobj.setComponent(Component.fromString(data2))
        CalendarObject._currentDataVersion = actualVersion
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        comp = yield cobj.component()
        self.assertTrue(comp.getOrganizer().startswith("urn:x-uid:"))
        self.assertTrue(comp.getOrganizerScheduleAgent())
        yield self.commit()

        yield JobItem.waitEmpty(self.storeUnderTest().newTransaction, reactor, 60)

        cobj = yield self.calendarObjectUnderTest(home="user01", name="test.ics")
        comp = yield cobj.component()
        self.assertTrue(comp.getOrganizer().startswith("urn:x-uid:"))
        self.assertTrue(comp.getOrganizerScheduleAgent())
        yield self.commit()

    @inlineCallbacks
    def test_doImplicitScheduling_AttendeeEventNoOrganizerEvent(self):
        """
        Test that doImplicitScheduling handles an attendee reply with no organizer event.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-no-organizer
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        try:
            yield self._createCalendarObject(data, "user02", "test.ics")
        except AttendeeAllowedError:
            pass
        except:
            self.fail("Wrong exception raised: %s" % (sys.exc_info()[0].__name__,))
        else:
            self.fail("Exception not raised")

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 0)

    @inlineCallbacks
    def test_doImplicitScheduling_AttendeeReply(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees who can then reply.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user01", "test.ics")

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=1.2" in calendar1)

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)

        yield self._setCalendarData(data2, "user02")

        yield JobItem.waitEmpty(self.storeUnderTest().newTransaction, reactor, 60)

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 1)

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=2.0" in calendar1)
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar1)

    @inlineCallbacks
    def test_doImplicitScheduling_refreshAllAttendeesExceptSome(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees who can then reply.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
"""

        # Need refreshes to occur immediately, not via reactor.callLater
        self.patch(config.Scheduling.Options, "AttendeeRefreshBatch", False)

        yield self._createCalendarObject(data1, "user01", "test.ics")

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 0)

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=1.2" in calendar1)

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)

        calendar2 = (yield self._getCalendarData("user02"))
        self.assertTrue("PARTSTAT=ACCEPTED" not in calendar2)

        list3 = (yield self._listCalendarObjects("user03", "inbox"))
        self.assertEqual(len(list3), 1)

        calendar3 = (yield self._getCalendarData("user03"))
        self.assertTrue("PARTSTAT=ACCEPTED" not in calendar3)

        yield self._setCalendarData(data2, "user02")

        yield JobItem.waitEmpty(self.storeUnderTest().newTransaction, reactor, 60)

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 1)

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=2.0" in calendar1)
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar1)

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)

        calendar2 = (yield self._getCalendarData("user02"))
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar2)

        list3 = (yield self._listCalendarObjects("user03", "inbox"))
        self.assertEqual(len(list3), 1)

        calendar3 = (yield self._getCalendarData("user03"))
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar3)

    @inlineCallbacks
    def test_doImplicitScheduling_refreshAllAttendeesExceptSome_Batched(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees who can then reply.
        Verify that batched refreshing is working.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-attendee-reply
DTSTAMP:20080601T120000Z
DTSTART:20080601T120000Z
DTEND:20080601T130000Z
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
END:VEVENT
END:VCALENDAR
"""

        # Need refreshes to occur immediately, not via reactor.callLater
        self.patch(config.Scheduling.Options, "AttendeeRefreshBatch", 5)
        self.patch(config.Scheduling.Options.WorkQueues, "AttendeeRefreshBatchDelaySeconds", 1)

        yield self._createCalendarObject(data1, "user01", "test.ics")

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 0)

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=1.2" in calendar1)

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)

        calendar2 = (yield self._getCalendarData("user02"))
        self.assertTrue("PARTSTAT=ACCEPTED" not in calendar2)

        list3 = (yield self._listCalendarObjects("user03", "inbox"))
        self.assertEqual(len(list3), 1)

        calendar3 = (yield self._getCalendarData("user03"))
        self.assertTrue("PARTSTAT=ACCEPTED" not in calendar3)

        yield self._setCalendarData(data2, "user02")

        yield JobItem.waitEmpty(self.storeUnderTest().newTransaction, reactor, 60)

        list1 = (yield self._listCalendarObjects("user01", "inbox"))
        self.assertEqual(len(list1), 1)

        calendar1 = (yield self._getCalendarData("user01", "test.ics"))
        self.assertTrue("SCHEDULE-STATUS=2.0" in calendar1)
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar1)

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 1)

        calendar2 = (yield self._getCalendarData("user02"))
        self.assertTrue("PARTSTAT=ACCEPTED" in calendar2)

        @inlineCallbacks
        def _test_user03_refresh():
            list3 = (yield self._listCalendarObjects("user03", "inbox"))
            self.assertEqual(len(list3), 1)

            calendar3 = (yield self._getCalendarData("user03"))
            self.assertTrue("PARTSTAT=ACCEPTED" in calendar3)

        yield deferLater(reactor, 2.0, _test_user03_refresh)

    @inlineCallbacks
    def test_doImplicitScheduling_OrganizerEventTimezoneDST(self):
        """
        Test that doImplicitScheduling delivers scheduling messages to attendees. This test
        creates an exception close to a DST transition to make sure timezone DST handling
        is correct.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART;TZID=America/Los_Angeles:20140302T190000
DTEND;TZID=America/Los_Angeles:20140302T193000
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        data2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART;TZID=America/Los_Angeles:20140302T190000
DTEND;TZID=America/Los_Angeles:20140302T193000
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
RECURRENCE-ID;TZID=America/Los_Angeles:20140308T190000
DTSTART;TZID=America/Los_Angeles:20140308T190000
DTEND;TZID=America/Los_Angeles:20140308T193000
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""
        TimezoneCache.create()

        yield self._createCalendarObject(data1, "user01", "test.ics")

        yield self._setCalendarData(data2, "user01", "test.ics")

        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))

        list2 = (yield self._listCalendarObjects("user02", "inbox"))
        self.assertEqual(len(list2), 2)
        self.assertTrue(list2[0].startswith(hashlib.md5("12345-67890").hexdigest()))
        self.assertTrue(list2[1].startswith(hashlib.md5("12345-67890").hexdigest()))

    @inlineCallbacks
    def test_doImplicitScheduling_MissingAttendeeWithInvalidUser(self):
        """
        Test that doImplicitMissingAttendee works when the event contains an
        invalid attendee.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20140302T190000Z
DURATION:PT1H
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:foo@bar.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user02", "test.ics")
        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)

        yield self._setCalendarData(data1, "user02", "test.ics")
        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)

    @inlineCallbacks
    def test_doImplicitScheduling_MissingAttendeeWithiMIP(self):
        """
        Test that doImplicitMissingAttendee works when iMIP is enabled and the event
        contains an iMIP attendee.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20140302T190000Z
DURATION:PT1H
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:foo@bar.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        self.patch(config.Scheduling.iMIP, "Enabled", True)
        self.patch(config.Scheduling.iMIP, "AddressPatterns", ["mailto:.*"])

        yield self._createCalendarObject(data1, "user02", "test.ics")
        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)

        yield self._setCalendarData(data1, "user02", "test.ics")
        list2 = (yield self._listCalendarObjects("user02"))
        self.assertEqual(len(list2), 1)

    @inlineCallbacks
    def test_sendAttendeeReply_ScheduleAgentNone(self):
        """
        Test that sendAttendeeReply does nothing when the Organizer has
        SCHEDULE-AGENT=NONE.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20140302T190000Z
DURATION:PT1H
ORGANIZER;SCHEDULE-AGENT=NONE;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user02", "test.ics")

        cobj = yield self.calendarObjectUnderTest(home="user02", name="test.ics",)
        result = yield ImplicitScheduler().sendAttendeeReply(cobj._txn, cobj)
        self.assertFalse(result)

    @inlineCallbacks
    def test_sendAttendeeReply_ScheduleAgentClient(self):
        """
        Test that sendAttendeeReply does nothing when the Organizer has
        SCHEDULE-AGENT=CLIENT.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20140302T190000Z
DURATION:PT1H
ORGANIZER;SCHEDULE-AGENT=CLIENT;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user02", "test.ics")

        cobj = yield self.calendarObjectUnderTest(home="user02", name="test.ics",)
        result = yield ImplicitScheduler().sendAttendeeReply(cobj._txn, cobj)
        self.assertFalse(result)

    @inlineCallbacks
    def test_sendAttendeeReply_NoAttendee(self):
        """
        Test that sendAttendeeReply does nothing when the Attencdee is not
        listed in the event. This will not normally ever be possible, but a case
        like this was seen due to a processing error elsewehere.
        """

        data1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTAMP:20080601T120000Z
DTSTART:20140302T190000Z
DURATION:PT1H
ORGANIZER;CN="User 01":mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
ATTENDEE:mailto:user03@example.com
RRULE:FREQ=DAILY;UNTIL=20140309T075959Z
END:VEVENT
END:VCALENDAR
"""
        yield self._createCalendarObject(data1, "user02", "test.ics")

        cobj = yield self.calendarObjectUnderTest(home="user02", name="test.ics",)

        # Need to remove SCHEDULE-AGENT=NONE on ORGANIZER as that will have been added during the store operation
        cal = yield cobj.componentForUser()
        cal.removePropertyParameters("ORGANIZER", ("SCHEDULE-AGENT", "SCHEDULE-STATUS",))
        result = yield ImplicitScheduler().sendAttendeeReply(cobj._txn, cobj)
        self.assertFalse(result)


class ScheduleAgentFixBase(CommonCommonTests, TestCase):
    """
    Test txdav.caldav.datastore.scheduling.implicit.
    """

    @inlineCallbacks
    def setUp(self):
        yield super(ScheduleAgentFixBase, self).setUp()
        yield self.buildStoreAndDirectory()
        yield self.populate()
        self.patch(config.Scheduling.Options, "AttendeeRefreshBatch", 0)

    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()

    metadata = {
        "accessMode": "PUBLIC",
        "isScheduleObject": True,
        "scheduleTag": "abc",
        "scheduleEtags": (),
        "hasPrivateComment": False,
    }

    @classproperty(cache=False)
    def requirements(cls):  # @NoSelf
        return {
            "user01": {
                "calendar_1": {
                    "organizer.ics": (cls.organizer_data, cls.metadata),
                },
                "inbox": {
                },
            },
            "user02": {
                "calendar_1": {
                    "attendee2.ics": (cls.attendee2_data, cls.metadata),
                },
                "inbox": {
                },
            },
            "user03": {
                "calendar_1": {
                    "attendee3.ics": (cls.attendee3_data, cls.metadata),
                },
                "inbox": {
                },
            },
        }


class ScheduleAgentFix(ScheduleAgentFixBase):
    """
    Test that implicit scheduling where an attendee has S-A=CLIENT and S-A=SERVER is
    corrected when the attendee updates.
    """

    organizer_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
RECURRENCE-ID:20140102T100000Z
DTSTART:20140102T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user02
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    attendee2_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER;SCHEDULE-AGENT=CLIENT:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
RECURRENCE-ID:20140102T100000Z
DTSTART:20140102T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER;SCHEDULE-AGENT=SERVER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user02
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    attendee2_update_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER;SCHEDULE-AGENT=CLIENT:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
RECURRENCE-ID:20140102T100000Z
DTSTART:20140102T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER;SCHEDULE-AGENT=SERVER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE;PARTSTAT=ACCEPTED:urn:x-uid:user02
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    attendee3_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
RECURRENCE-ID:20140102T100000Z
DTSTART:20140102T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user02
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    @inlineCallbacks
    def test_doImplicitScheduling(self):
        """
        Test that doImplicitScheduling fixes an inconsistent schedule-agent state when an
        attendee stores their data.
        """

        cobj = yield self.calendarObjectUnderTest(home="user02", name="attendee2.ics")
        yield cobj.setComponent(Component.fromString(self.attendee2_update_data))
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(home="user02", name="attendee2.ics")
        comp = yield cobj.component()
        self.assertTrue(comp.masterComponent() is None)
        self.assertTrue(comp.getOrganizerScheduleAgent())

        inbox = yield self.calendarUnderTest(home="user01", name="inbox")
        cobjs = yield inbox.calendarObjects()
        self.assertTrue(len(cobjs) == 1)


class MissingOrganizerFix(ScheduleAgentFixBase):
    """
    Test that an attendee with a copy of an event without any organizer or attendee
    properties is corrected when the organizer updates.
    """

    organizer_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    organizer_update_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user02
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    attendee2_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
END:VEVENT
END:VCALENDAR
"""

    attendee3_data = """BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20051222T205953Z
CREATED:20060101T150000Z
DTSTART:20140101T100000Z
DURATION:PT1H
SUMMARY:event 1
UID:event1@ninevah.local
ORGANIZER:urn:x-uid:user01
ATTENDEE:urn:x-uid:user01
ATTENDEE:urn:x-uid:user03
END:VEVENT
END:VCALENDAR
"""

    @inlineCallbacks
    def test_doImplicitScheduling(self):
        """
        Test that doImplicitScheduling fixes an inconsistent schedule-agent state when an
        attendee stores their data.
        """

        cobj = yield self.calendarObjectUnderTest(home="user02", name="attendee2.ics")
        comp = yield cobj.component()
        self.assertTrue(comp.getOrganizer() is None)
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(home="user01", name="organizer.ics")
        yield cobj.setComponent(Component.fromString(self.organizer_update_data))
        yield self.commit()

        cal = yield self.calendarUnderTest(home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertTrue(len(cobjs) == 2)
        for cobj in cobjs:
            comp = yield cobj.component()
            if comp.resourceUID() == "event1@ninevah.local":
                self.assertTrue(comp.getOrganizer() is not None)
            else:
                self.assertTrue(comp.getOrganizer() is None)

        inbox = yield self.calendarUnderTest(home="user02", name="inbox")
        cobjs = yield inbox.calendarObjects()
        self.assertTrue(len(cobjs) == 1)
