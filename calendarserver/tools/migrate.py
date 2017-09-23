#!/usr/bin/env python

##
# Copyright (c) 2006-2017 Apple Inc. All rights reserved.
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

"""
This tool migrates existing calendar data from any previous calendar server
version to the current version.

This tool requires access to the calendar server's configuration and
data storage; it does not operate by talking to the server via the
network.
"""

import os
import sys
from getopt import getopt, GetoptError

from twistedcaldav.config import ConfigurationError
from twistedcaldav.upgrade import upgradeData

from calendarserver.tools.util import loadConfig


def usage(e=None):
    if e:
        print(e)
        print("")

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options]" % (name,))
    print("")
    print("Migrate calendar data to current version")
    print(__doc__)
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -f --config: Specify caldavd.plist configuration path")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():
    try:
        (optargs, args) = getopt(
            sys.argv[1:], "hf:", [
                "config=",
                "help",
            ],
        )
    except GetoptError as e:
        usage(e)

    configFileName = None

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

    if args:
        usage("Too many arguments: %s" % (" ".join(args),))

    try:
        config = loadConfig(configFileName)
    except ConfigurationError as e:
        sys.stdout.write("%s\n" % (e,))
        sys.exit(1)

    profiling = False

    if profiling:
        import cProfile
        cProfile.runctx("upgradeData(c)", globals(), {"c": config}, "/tmp/upgrade.prof")
    else:
        upgradeData(config)

if __name__ == "__main__":
    main()
