#!/usr/bin/env python2.4

##
# Copyright (c) 2005-2016 Apple Inc. All rights reserved.
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

#
# Reads CSV files in this form:
# "Title","Start Date","Start Time","End Date","End Time","All day event","Categories","Priority","Body"
# And generates an iCalendar file.
# Dedicated to Amir.
#

from __future__ import print_function
import sys
import csv
import datetime

from twistedcaldav.ical import Component, Property

##
# Fancy command line handling
##

csv_filename = sys.argv[1]

##
# Do The Right Thing
##


def parse_datetime(date, time):
    if date == "":
        return None

    start_date = datetime.date(*[int(i) for i in date.split("-")])

    if time == "":
        return start_date

    hour = int(time[:2])
    meridian = time[-2:]
    if hour == 12:
        # We're assuming 12AM == 00:00 and 12PM == 12:00, which isn't
        # true, but as true as the opposite, and more widely held.
        if meridian == "AM":
            hour -= 12
        else:
            assert meridian == "PM"
    else:
        if meridian == "PM":
            hour += 12
        else:
            assert meridian == "AM"

    start_time = datetime.time(hour, int(time[3:5]))

    return datetime.datetime.combine(start_date, start_time)

reader = csv.reader(file(csv_filename, "rb"))

calendar = Component("VCALENDAR")

# Ignore first line
next(reader)

priorities = {
    "High": "1",
    "Medium": "5",
    "Low": "9",
}

for row in reader:
    event = Component("VEVENT")

    title = row[0]
    event.addProperty(Property("SUMMARY", title))

    start = parse_datetime(row[1], row[2])
    event.addProperty(Property("DTSTART", start))

    end = None
    if row[5] == "1":
        assert row[3] == row[4] == ""
        end = start + datetime.timedelta(1)
    else:
        end = parse_datetime(row[3], row[4])
    if end is not None:
        event.addProperty(Property("DTEND", end))

    categories = row[6]
    event.addProperty(Property("CATEGORIES", categories))

    priority = priorities[row[7]]
    event.addProperty(Property("PRIORITY", priority))

    description = row[8]
    event.addProperty(Property("DESCRIPTION", description))

    calendar.addComponent(event)

print(calendar)
