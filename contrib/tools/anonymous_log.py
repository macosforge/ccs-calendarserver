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

from gzip import GzipFile
import getopt
import os
import sys
import traceback


class CalendarServerLogAnalyzer(object):

    def __init__(self):

        self.userCtr = 1
        self.users = {}

        self.guidCtr = 1
        self.guids = {}

        self.resourceCtr = 1
        self.resources = {}

    def anonymizeLogFile(self, logFilePath):

        fpath = os.path.expanduser(logFilePath)
        if fpath.endswith(".gz"):
            f = GzipFile(fpath)
        else:
            f = open(fpath)

        try:
            for line in f:

                if not line.startswith("Log"):
                    line = self.anonymizeLine(line)
                print(line, end="")

        except Exception as e:
            print("Exception: %s for %s" % (e, line,))
            raise

        finally:
            f.close()

    def anonymizeLine(self, line):

        startPos = line.find("- ")
        endPos = line.find(" [")
        userid = line[startPos + 2:endPos]

        if userid != "-":
            if userid not in self.users:
                self.users[userid] = "user%05d" % (self.userCtr,)
                self.userCtr += 1
            line = line[:startPos + 2] + self.users[userid] + line[endPos:]
            endPos = line.find(" [")

        startPos = endPos + 1

        startPos = line.find(']', startPos + 21) + 3
        endPos = line.find(' ', startPos)
        if line[startPos] != '?':

            startPos = endPos + 1
            endPos = line.find(" HTTP/", startPos)
            uri = line[startPos:endPos]

            splits = uri.split("/")
            if len(splits) >= 4:
                if splits[1] in ("calendars", "principals"):

                    if splits[3] not in self.guids:
                        self.guids[splits[3]] = "guid%05d" % (self.guidCtr,)
                        self.guidCtr += 1
                    splits[3] = self.guids[splits[3]]

                    if len(splits) > 4:
                        if splits[4] not in ("", "calendar", "inbox", "outbox", "dropbox"):
                            if splits[4] not in self.resources:
                                self.resources[splits[4]] = "resource%d" % (self.resourceCtr,)
                                self.resourceCtr += 1
                            splits[4] = self.resources[splits[4]]

                    if len(splits) > 5:
                        for x in range(5, len(splits)):
                            if splits[x]:
                                if splits[x] not in self.resources:
                                    self.resources[splits[x]] = "resource%d%s" % (self.resourceCtr, os.path.splitext(splits[x])[1])
                                    self.resourceCtr += 1
                                splits[x] = self.resources[splits[x]]

                    line = line[:startPos] + "/".join(splits) + line[endPos:]

        return line


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: anonymous_log [options] [FILE]
Options:
    -h            Print this help and exit

Arguments:
    FILE      File names for the access logs to anonymize

Description:
    This utility will anonymize the content of an access log.

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
        if len(args) == 0:
            args = ("/var/log/caldavd/access.log",)

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

            CalendarServerLogAnalyzer().anonymizeLogFile(arg)

    except Exception as e:
        sys.exit(str(e))
        print(traceback.print_exc())
