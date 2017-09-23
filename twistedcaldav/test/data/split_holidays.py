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
# Splits up the monolithic .Mac holidays calendar into a separate calendar
# for each subcomponent therein.
# These split-up calendars are useable as CalDAV resources.
#

from __future__ import print_function
import os

from twistedcaldav.ical import Component

monolithic_filename = os.path.join(os.path.dirname(__file__), "Holidays.ics")

calendar = Component.fromStream(file(monolithic_filename))

assert calendar.name() == "VCALENDAR"

for subcomponent in calendar.subcomponents():
    subcalendar = Component("VCALENDAR")

    #
    # Add top-level properties from monolithic calendar to top-level properties
    # of subcomponent calendar.
    #
    for property in calendar.properties():
        subcalendar.addProperty(property)

    subcalendar.addComponent(subcomponent)

    uid = subcalendar.resourceUID()
    subcalendar_filename = os.path.join(os.path.dirname(__file__), "Holidays", uid + ".ics")

    print("Writing %s" % (subcalendar_filename,))

    subcalendar_file = file(subcalendar_filename, "w")
    try:
        subcalendar_file.write(str(subcalendar))
    finally:
        subcalendar_file.close()
