#!/usr/bin/env python
# coding=utf-8
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
from bz2 import BZ2File
from getopt import getopt
import os
import sys
import cPickle


def qos():

    logdir = os.path.expanduser("~/buildbot/master/PerfBladeSim")
    logfiles = [path.replace("simlog", "stdio") for path in os.listdir(logdir) if "-simlog" in path]
    data = {}
    for logfile in logfiles:
        run = int(logfile.split("-")[0])
        logfile = os.path.join(logdir, logfile)

        # Get svn revision
        buildfile = os.path.join(logdir, "{run}".format(run=run))
        try:
            with open(buildfile) as f:
                builddata = f.read()
            builddata = cPickle.loads(builddata)
            svnvers = int(builddata.properties["got_revision"])
        except Exception:
            svnvers = "?"

        # Get Qos value
        if not os.path.exists(logfile):
            continue
        if logfile.endswith(".bz2"):
            opener = BZ2File
        else:
            opener = open
        try:
            with opener(logfile) as f:
                lines = f.readlines()
        except IOError:
            print(logfile)
            raise
        for line in lines:
            if "Qos :" in line:
                try:
                    data[run] = (svnvers, float(line.split()[-1]),)
                except ValueError:
                    pass
                break

    for key in sorted(data.keys()):
        print("{}\t{}\t{}".format(key, data[key][0], data[key][1]))


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: buildbot_analyze [options]
Options:
    -h          Print this help and exit
    --qos       Look at PerfBlade Qos values

Arguments: None

Description:
    This utility will analyze the output of BuildBot runs to extract
    historical data for all the runs.
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == "__main__":

    do_qos = False
    try:
        options, args = getopt(sys.argv[1:], "h", ["qos", ])

        for option, value in options:
            if option == "-h":
                usage()
            elif option == "--qos":
                do_qos = True
            else:
                usage("Unrecognized option: %s" % (option,))

        if len(args) != 0:
            usage("Must have no arguments")

        if do_qos:
            qos()

    except Exception as e:
        raise
        sys.exit(str(e))
