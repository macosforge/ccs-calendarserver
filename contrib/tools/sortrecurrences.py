#!/usr/bin/env python
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
from __future__ import print_function

import getopt
import os
import sys
import traceback
from pycalendar.icalendar.calendar import Calendar


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: sortrecurrences FILE
Options:
    -h            Print this help and exit

Arguments:
    FILE      File name for the calendar data to sort

Description:
    This utility will output a sorted iCalendar component.

""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == "__main__":

    try:

        options, args = getopt.getopt(sys.argv[1:], "h", [])

        for option, value in options:
            if option == "-h":
                usage()
            else:
                usage("Unrecognized option: %s" % (option,))

        # Process arguments
        if len(args) != 1:
            usage("Must have one argument")

        pwd = os.getcwd()

        analyzers = []
        for arg in args:
            arg = os.path.expanduser(arg)
            if not arg.startswith("/"):
                arg = os.path.join(pwd, arg)
            if arg.endswith("/"):
                arg = arg[:-1]
            if not os.path.exists(arg):
                print("Path does not exist: '%s'. Ignoring." % (arg,))
                continue

            cal = Calendar()
            cal.parse(open(arg))
            print(str(cal.serialize()))

    except Exception as e:
        sys.exit(str(e))
        print(traceback.print_exc())
