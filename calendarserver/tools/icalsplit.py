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
from __future__ import print_function

import os
import sys
from getopt import getopt, GetoptError

from twistedcaldav.ical import Component as iComponent


def splitICalendarFile(inputFileName, outputDirectory):
    """
    Reads iCalendar data from a file and outputs a separate file for
    each iCalendar component into a directory.  This is useful for
    converting a monolithic iCalendar object into a set of objects
    that comply with CalDAV's requirements on resources.
    """
    with open(inputFileName) as inputFile:
        calendar = iComponent.fromStream(inputFile)

    assert calendar.name() == "VCALENDAR"

    topLevelProperties = tuple(calendar.properties())

    for subcomponent in calendar.subcomponents():
        subcalendar = iComponent("VCALENDAR")

        #
        # Add top-level properties from monolithic calendar to
        # top-level properties of subcalendar.
        #
        for property in topLevelProperties:
            subcalendar.addProperty(property)

        subcalendar.addComponent(subcomponent)

        uid = subcalendar.resourceUID()
        subFileName = os.path.join(outputDirectory, uid + ".ics")

        print("Writing %s" % (subFileName,))

        subcalendar_file = file(subFileName, "w")
        try:
            subcalendar_file.write(str(subcalendar))
        finally:
            subcalendar_file.close()


def usage(e=None):
    if e:
        print(e)
        print("")

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] input_file output_directory" % (name,))
    print("")
    print("  Splits up monolithic iCalendar data into separate files for each")
    print("  subcomponent so as to comply with CalDAV requirements for")
    print("  individual resources.")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():
    try:
        (optargs, args) = getopt(
            sys.argv[1:], "h", [
                "help",
            ],
        )
    except GetoptError as e:
        usage(e)

    for opt, _ignore_arg in optargs:
        if opt in ("-h", "--help"):
            usage()

    try:
        inputFileName, outputDirectory = args
    except ValueError:
        if len(args) > 2:
            many = "many"
        else:
            many = "few"
        usage("Too %s arguments" % (many,))

    splitICalendarFile(inputFileName, outputDirectory)


if __name__ == "__main__":
    main()
