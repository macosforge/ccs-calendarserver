from __future__ import absolute_import
##
# Copyright (c) 2009-2017 Apple Inc. All rights reserved.
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

from twisted.python.filepath import FilePath
from twisted.trial.unittest import TestCase

from .protocolanalysis import CalendarServerLogAnalyzer


class UserInteractionTests(TestCase):
    """
    Tests for analysis of the way users interact with each other, done
    by CalendarServerLogAnalyzer.
    """

    def test_propfindOtherCalendar(self):
        """
        L{CalendarServerLogAnalyzer}'s C{otherUserCalendarRequests}
        attribute is populated with data about the frequency with
        which users access a calendar belonging to a different user.

        The C{"PROPFIND Calendar Home"} key is associated with a
        C{dict} with count bucket labels as keys and counts of how
        many users PROPFINDs on calendars belonging to a number of
        other users which falls into that bucket.
        """
        format = (
            '17.128.126.80 - %(user)s [27/Sep/2010:05:13:17 +0000] '
            '"PROPFIND /calendars/__uids__/%(other)s/ HTTP/1.1" 207 '
            '21274 "-" "DAVKit/4.0.3 (732); CalendarStore/4.0.3 (991); '
            'iCal/4.0.3 (1388); Mac OS X/10.6.4 (10F569)" i=16 t=199.1 or=2\n')
        path = FilePath(self.mktemp())
        path.setContent(
            # A user accessing his own calendar
            format % dict(user="user01", other="user01") +

            # A user accessing the calendar of one other person
            format % dict(user="user02", other="user01") +

            # A user accessing the calendar of one other person twice
            format % dict(user="user03", other="user01") +
            format % dict(user="user03", other="user01") +

            # A user accessing the calendars of two other people
            format % dict(user="user04", other="user01") +
            format % dict(user="user04", other="user02") +

            # Another user accessing the calendars of two other people
            format % dict(user="user05", other="user03") +
            format % dict(user="user05", other="user04"))

        analyzer = CalendarServerLogAnalyzer(startHour=22, endHour=24)
        analyzer.analyzeLogFile(path.path)

        self.assertEquals(
            analyzer.summarizeUserInteraction("PROPFIND Calendar Home"),
            {"(a):0": 1, "(b):1": 2, "(c):2": 2})

    test_propfindOtherCalendar.todo = (
        "needs fixing"
    )
