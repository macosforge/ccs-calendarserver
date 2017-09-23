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
import sys
import os
from gzip import GzipFile
import datetime

outputFile = None
fileCount = 0
lastWeek = None


def split(fpath, outputDir):

    global outputFile, fileCount, lastWeek

    print("Splitting data for %s" % (fpath,))
    f = GzipFile(fpath) if fpath.endswith(".gz") else open(fpath)
    for line in f:
        if line.startswith("2010/0"):
            date = line[:10]
            date = date.replace("/", "")
            hours = line[11:13]

            dt = datetime.date(int(date[0:4]), int(date[4:6]), int(date[6:8]))

            currentWeek = dt.isocalendar()[1]
            if dt.weekday() == 0 and hours <= "06":
                currentWeek -= 1
            if lastWeek != currentWeek:
                if outputFile:
                    outputFile.close()
                outputFile = open(os.path.join(outputDir, "request.log.%s" % (date,)), "w")
                fileCount += 1
                lastWeek = currentWeek
                print("Changed to week of %s" % (date,))

            output = ["-----\n"]
            output.append(line)
            try:
                output.append(next(f))
                line = next(f)
                if line.startswith("Memory"):
                    line = next(f)
                output.append(line)
                output.append(next(f))
            except StopIteration:
                break
            outputFile.write("".join(output))
    f.close()


def argPath(path):
    fpath = os.path.expanduser(path)
    if not fpath.startswith("/"):
        fpath = os.path.join(pwd, fpath)
    return fpath


def expandDate(date):
    return "%s/%s/%s" % (date[0:4], date[4:6], date[6:8],)


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: monitoranalysis [options] FILE+
Options:
    -h          Print this help and exit
    -d          Directory to store split files in

Arguments:
    FILE      File names for the requests.log to analyze. A date
              range can be specified by append a comma, then a
              dash seperated pair of YYYYMMDD dates, e.g.:
              ~/request.log,20100614-20100619. Multiple
              ranges can be specified for multiple plots.

Description:
This utility will analyze the output of the request monitor tool and
generate some pretty plots of data.
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == "__main__":

    outputDir = None

    options, args = getopt.getopt(sys.argv[1:], "hd:", [])

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-d":
            outputDir = argPath(value)
        else:
            usage("Unrecognized option: %s" % (option,))

    if not outputDir or not os.path.isdir(outputDir):
        usage("Must specify a valid output directory.")

    # Process arguments
    if len(args) == 0:
        usage("Must have arguments")

    pwd = os.getcwd()

    for arg in args:
        split(argPath(arg), outputDir)

    print("Created %d files" % (fileCount,))
