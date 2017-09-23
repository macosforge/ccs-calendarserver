#!/usr/bin/env python

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

from __future__ import print_function
import getopt
import os
import sys

if __name__ == "__main__":
    wd = os.path.dirname(__file__)
    document_root = "."
    user_max = 99
    user_one = None
    calendars = ("calendar.10", "calendar.100", "calendar.1000",)

    options, args = getopt.getopt(sys.argv[1:], "n:d:o:")

    for option, value in options:
        if option == "-n":
            user_max = int(value)
        elif option == "-o":
            user_one = int(value)
        elif option == "-d":
            document_root = os.path.abspath(value)
        else:
            print("Unrecognized option: %s" % (option,))
            raise ValueError

    for ctr in (xrange(user_one, user_one + 1) if user_one else xrange(1, user_max + 1)):
        path = os.path.join(document_root, "calendars/__uids__/us/er/user%02d" % (ctr,))

        try:
            os.makedirs(path)
        except OSError:
            pass

        try:
            os.makedirs(path)
        except OSError:
            pass

        for calendar in calendars:
            if not os.path.isdir(os.path.join(path, calendar)):
                print("Expanding %s to %s" % (calendar, path))
                cmd = "tar -C %r -zx -f %r" % (path,
                                               os.path.join(wd,
                                                            calendar + ".tgz"))
                os.system(cmd)
