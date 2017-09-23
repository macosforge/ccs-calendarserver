#!/usr/bin/env python
##
# Copyright (c) 2012-2017 Apple Inc. All rights reserved.
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

from calendarserver.push.amppush import subscribeToIDs
from calendarserver.tools.cmdline import utilityMain, WorkerService

from getopt import getopt, GetoptError

from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, succeed

import os
import sys

log = Logger()


def usage(e=None):

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] [pushkey ...]" % (name,))
    print("")
    print("  Monitor AMP Push Notifications")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -f --config <path>: Specify caldavd.plist configuration path")
    print("  -p --port <port>: AMP port to connect to")
    print("  -s --server <hostname>: AMP server to connect to")
    print("  --debug: verbose logging")
    print("")

    if e:
        sys.stderr.write("%s\n" % (e,))
        sys.exit(64)
    else:
        sys.exit(0)


class MonitorAMPNotifications(WorkerService):

    ids = []
    hostname = None
    port = None

    def doWork(self):
        return monitorAMPNotifications(self.hostname, self.port, self.ids)

    def postStartService(self):
        """
        Don't quit right away
        """
        pass


def main():

    try:
        (optargs, args) = getopt(
            sys.argv[1:], "f:hp:s:v", [
                "config=",
                "debug",
                "help",
                "port=",
                "server=",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    configFileName = None
    hostname = "localhost"
    port = 62311
    debug = False

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

        elif opt in ("-p", "--port"):
            port = int(arg)

        elif opt in ("-s", "--server"):
            hostname = arg

        elif opt in ("--debug"):
            debug = True

        else:
            raise NotImplementedError(opt)

    if not args:
        usage("Not enough arguments")

    MonitorAMPNotifications.ids = args
    MonitorAMPNotifications.hostname = hostname
    MonitorAMPNotifications.port = port

    utilityMain(
        configFileName,
        MonitorAMPNotifications,
        verbose=debug
    )


def notificationCallback(id, dataChangedTimestamp, priority):
    print("Received notification for:", id, "Priority", priority)
    return succeed(True)


@inlineCallbacks
def monitorAMPNotifications(hostname, port, ids):
    print("Subscribing to notifications...")
    yield subscribeToIDs(hostname, port, ids, notificationCallback)
    print("Waiting for notifications...")
