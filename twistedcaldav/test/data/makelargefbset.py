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
import xattr

if __name__ == "__main__":

    user_max = 99

    options, args = getopt.getopt(sys.argv[1:], "n:")

    for option, value in options:
        if option == "-n":
            user_max = int(value)
        else:
            print("Unrecognized option: %s" % (option,))
            raise ValueError

    for ctr in xrange(1, user_max + 1):
        path = "calendars/users/user%02d" % (ctr,)

        try:
            os.makedirs(path)
        except OSError:
            pass

        try:
            os.makedirs(os.path.join(path, "calendar"))
        except OSError:
            pass

        inboxname = os.path.join(path, "inbox")
        attrs = xattr.xattr(inboxname)
        attrs["WebDAV:{urn:ietf:params:xml:ns:caldav}calendar-free-busy-set"] = """<?xml version='1.0' encoding='UTF-8'?>
    <calendar-free-busy-set xmlns='urn:ietf:params:xml:ns:caldav'>
      <href xmlns='DAV:'>/calendars/users/user%02d/calendar/</href>
      <href xmlns='DAV:'>/calendars/users/user%02d/calendar.1000/</href>
    </calendar-free-busy-set>""" % (ctr, ctr,)
