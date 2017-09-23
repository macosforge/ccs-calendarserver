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

import twistedcaldav.test.util
from twistedcaldav.config import config
from twistedcaldav.ical import Component
from twistedcaldav.timezones import TimezoneCache, TimezoneException
from twistedcaldav.timezones import readTZ, listTZs
from pycalendar.datetime import DateTime
from pycalendar.timezone import Timezone

import os
import threading
from twisted.python.failure import Failure


class TimezoneProblemTest (twistedcaldav.test.util.TestCase):
    """
    Timezone support tests
    """

    data_dir = os.path.join(os.path.dirname(__file__), "data")

    def tearDown(self):
        TimezoneCache.clear()
        TimezoneCache.create()

    def doTest(self, filename, dtstart, dtend, testEqual=True):

        if testEqual:
            testMethod = self.assertEqual
        else:
            testMethod = self.assertNotEqual

        calendar = Component.fromStream(file(os.path.join(self.data_dir, filename)))
        if calendar.name() != "VCALENDAR":
            self.fail("Calendar is not a VCALENDAR")

        instances = calendar.expandTimeRanges(DateTime(2100, 1, 1))
        for key in instances:
            instance = instances[key]
            start = instance.start
            end = instance.end
            testMethod(start, dtstart)
            testMethod(end, dtend)
            break

    def test_truncatedApr(self):
        """
        Custom VTZ with truncated standard time - 2006. Daylight 2007 OK.
        """

        TimezoneCache.create(empty=True)
        TimezoneCache.clear()

        self.doTest(
            "TruncatedApr01.ics",
            DateTime(2007, 0o4, 0o1, 16, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 0o4, 0o1, 17, 0, 0, Timezone.UTCTimezone)
        )

    def test_truncatedDec(self):
        """
        Custom VTZ valid from 2007. Daylight 2007 OK.
        """

        TimezoneCache.create(empty=True)
        TimezoneCache.clear()

        self.doTest(
            "TruncatedDec10.ics",
            DateTime(2007, 12, 10, 17, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 12, 10, 18, 0, 0, Timezone.UTCTimezone)
        )

    def test_truncatedAprThenDecFail(self):
        """
        Custom VTZ with truncated standard time - 2006 loaded first. Daylight 2007 OK, standard 2007 wrong.
        """

        TimezoneCache.create(empty=True)
        TimezoneCache.clear()

        self.doTest(
            "TruncatedApr01.ics",
            DateTime(2007, 0o4, 0o1, 16, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 0o4, 0o1, 17, 0, 0, Timezone.UTCTimezone),
        )
        self.doTest(
            "TruncatedDec10.ics",
            DateTime(2007, 12, 10, 17, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 12, 10, 18, 0, 0, Timezone.UTCTimezone),
            testEqual=False
        )

    def test_truncatedAprThenDecOK(self):
        """
        VTZ loaded from std timezone DB. 2007 OK.
        """

        TimezoneCache.create()

        self.doTest(
            "TruncatedApr01.ics",
            DateTime(2007, 0o4, 0o1, 16, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 0o4, 0o1, 17, 0, 0, Timezone.UTCTimezone),
        )
        self.doTest(
            "TruncatedDec10.ics",
            DateTime(2007, 12, 10, 17, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 12, 10, 18, 0, 0, Timezone.UTCTimezone),
        )

    def test_truncatedDecThenApr(self):
        """
        Custom VTZ valid from 2007 loaded first. Daylight 2007 OK.
        """

        TimezoneCache.create(empty=True)
        TimezoneCache.clear()

        self.doTest(
            "TruncatedDec10.ics",
            DateTime(2007, 12, 10, 17, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 12, 10, 18, 0, 0, Timezone.UTCTimezone)
        )
        self.doTest(
            "TruncatedApr01.ics",
            DateTime(2007, 0o4, 0o1, 16, 0, 0, Timezone.UTCTimezone),
            DateTime(2007, 0o4, 0o1, 17, 0, 0, Timezone.UTCTimezone)
        )


class TimezoneCacheTest (twistedcaldav.test.util.TestCase):
    """
    Timezone support tests
    """

    data_dir = os.path.join(os.path.dirname(__file__), "data")

    def test_basic(self):

        TimezoneCache.create()
        self.assertTrue(readTZ("America/New_York"))
        self.assertTrue(readTZ("US/Eastern"))

    def test_not_in_cache(self):

        TimezoneCache.create()

        data = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:US-Eastern
LAST-MODIFIED:19870101T000000Z
BEGIN:STANDARD
DTSTART:19671029T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:Eastern Standard Time (US & Canada)
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19870405T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=4
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:Eastern Daylight Time (US & Canada)
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:12345-67890
DTSTART;TZID="US-Eastern":20071225T000000
DTEND;TZID="US-Eastern":20071225T010000
ATTENDEE:mailto:user1@example.com
ATTENDEE:mailto:user2@example.com
END:VEVENT
END:VCALENDAR
"""

        calendar = Component.fromString(data)
        if calendar.name() != "VCALENDAR":
            self.fail("Calendar is not a VCALENDAR")
        instances = calendar.expandTimeRanges(DateTime(2100, 1, 1))
        for key in instances:
            instance = instances[key]
            start = instance.start
            end = instance.end
            self.assertEqual(start, DateTime(2007, 12, 25, 0o5, 0, 0, Timezone.UTCTimezone))
            self.assertEqual(end, DateTime(2007, 12, 25, 0o6, 0, 0, Timezone.UTCTimezone))
            break

    def test_getTZExtrasPath(self):
        """
        Make sure TimezoneCache._getTZExtrasPath returns a valid file.
        """
        extras = TimezoneCache._getTZExtrasPath()
        self.assertTrue(os.path.isfile(extras))


class TimezonePackageTest (twistedcaldav.test.util.TestCase):
    """
    Timezone support tests
    """

    def setUp(self):
        super(TimezonePackageTest, self).setUp()
        TimezoneCache.clear()
        TimezoneCache.create()

    def test_ReadTZ(self):

        self.assertTrue(readTZ("America/New_York").find("TZID:America/New_York") != -1)
        self.assertRaises(TimezoneException, readTZ, "America/Pittsburgh")

    def test_ReadTZCached(self):

        self.assertTrue(readTZ("America/New_York").find("TZID:America/New_York") != -1)
        self.assertTrue(readTZ("America/New_York").find("TZID:America/New_York") != -1)
        self.assertRaises(TimezoneException, readTZ, "America/Pittsburgh")
        self.assertRaises(TimezoneException, readTZ, "America/Pittsburgh")

    def test_ListTZs(self):

        results = listTZs()
        self.assertTrue("America/New_York" in results)
        self.assertTrue("Europe/London" in results)
        self.assertTrue("GB" in results)

    def test_ListTZsCached(self):

        results = listTZs()
        self.assertTrue("America/New_York" in results)
        self.assertTrue("Europe/London" in results)
        self.assertTrue("GB" in results)

    def test_copyPackage(self):
        """
        Test that copying of the tz package works.
        """

        self.patch(config, "UsePackageTimezones", True)
        TimezoneCache.clear()
        TimezoneCache.create()

        self.assertFalse(os.path.exists(os.path.join(config.DataRoot, "zoneinfo")))
        self.assertFalse(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "America", "New_York.ics")))

        pkg_tz = readTZ("America/New_York")

        self.patch(config, "UsePackageTimezones", False)
        TimezoneCache.clear()
        TimezoneCache.create()

        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo")))
        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "America", "New_York.ics")))

        copy_tz = readTZ("America/New_York")

        self.assertEqual(str(pkg_tz), str(copy_tz))

    def test_copyPackage_missingVersion(self):
        """
        Test that tz data is updated if the version is missing.
        """

        self.patch(config, "UsePackageTimezones", False)
        TimezoneCache.clear()
        TimezoneCache.create()

        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo")))
        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "version.txt")))
        os.remove(os.path.join(config.DataRoot, "zoneinfo", "version.txt"))
        self.assertFalse(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "version.txt")))

        TimezoneCache.create()
        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "version.txt")))

    def test_copyPackage_Concurrency(self):
        """
        Test that concurrent copying of the tz package works.
        """

        self.patch(config, "UsePackageTimezones", False)
        TimezoneCache.clear()

        ex = [None, None]

        def _try(n):
            try:
                TimezoneCache.create()
            except:
                f = Failure()
                ex[n] = str(f)

        t1 = threading.Thread(target=_try, args=(0,))
        t2 = threading.Thread(target=_try, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(ex[0] is None, msg=ex[0])
        self.assertTrue(ex[1] is None, msg=ex[1])

        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo")))
        self.assertTrue(os.path.exists(os.path.join(config.DataRoot, "zoneinfo", "America", "New_York.ics")))

    test_copyPackage_Concurrency.skip = "This tests needs to use separate processes rather than threads to work properly as the FilesystemLock object is process-based "
