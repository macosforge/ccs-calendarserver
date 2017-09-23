from __future__ import absolute_import
##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

from twisted.trial.unittest import TestCase

from .benchmarks.event_change_date import replaceTimestamp

calendarHead = """\
BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//Apple Inc.//iCal 4.0.3//EN
BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:STANDARD
DTSTART:20071104T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
TZNAME:PST
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:20070311T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
TZNAME:PDT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:f0b54c7b-c8ae-4dca-807f-5909da771964
"""

calendarDates = """\
DTSTART;TZID=America/Los_Angeles:20100730T111505
DTEND;TZID=America/Los_Angeles:20100730T111508
"""

calendarTail = """\
ATTENDEE;CN=User 02;CUTYPE=INDIVIDUAL;EMAIL=user02@example.com;PARTSTAT=NE
 EDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:use
 r02
CREATED:20100729T193912Z
DTSTAMP:20100729T195557Z
ORGANIZER;CN=User 03;EMAIL=user03@example.com:urn:x-uid:user03
SEQUENCE:1
SUMMARY:STUFF IS THINGS
TRANSP:OPAQUE
END:VEVENT
END:VCALENDAR
"""


class TimestampReplaceTests(TestCase):

    def test_replaceTimestamp(self):
        """
        replaceTimestamp adjusts the DTSTART and DTEND timestamp
        values by one hour times the counter parameter.
        """
        oldCalendar = calendarHead + calendarDates + calendarTail
        newCalendar = replaceTimestamp(oldCalendar, 3)
        self.assertEquals(
            calendarHead +
            "DTSTART;TZID=America/Los_Angeles:20100730T141505\n"
            "DTEND;TZID=America/Los_Angeles:20100730T141508\n" +
            calendarTail,
            newCalendar)
