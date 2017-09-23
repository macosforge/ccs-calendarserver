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

from getopt import getopt, GetoptError
import errno
import json
import os
import sched
import socket
import sys
import time


def usage(e=None):
    name = os.path.basename(sys.argv[0])
    print("usage: %s [options]" % (name,))
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -s: server host (and optional port) [localhost:8100]")
    print("      or unix socket path prefixed by 'unix:'")
    print("")
    print("This tool monitors the server's job assignment rate.")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():
    try:
        (optargs, _ignore_args) = getopt(
            sys.argv[1:], "hs:", [
                "help",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    server = ("localhost", 8100)

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-s"):
            if not arg.startswith("unix:"):
                server = arg.split(":")
                if len(server) == 1:
                    server.append(8100)
                else:
                    server[1] = int(server[1])
                server = tuple(server)
            else:
                server = arg

        else:
            raise NotImplementedError(opt)

    d = Monitor(server)
    d.run()


class Monitor(object):
    """
    Main monitor controller. Use Python's L{sched} feature to schedule
    updates.
    """

    screen = None
    registered_windows = {}
    registered_order = []

    def __init__(self, server):
        self.paused = False
        self.seconds = 1.0
        self.sched = sched.scheduler(time.time, time.sleep)
        self.client = MonitorClient(server)
        self.client.addItem("test_work")
        self.last_queued = None
        self.last_completed = None
        self.last_time = None

    def run(self):
        """
        Create the initial window and run the L{scheduler}.
        """
        self.sched.enter(self.seconds, 0, self.updateResults, ())
        self.sched.run()

    def updateResults(self):
        """
        Periodic update of the current window and check for a key press.
        """

        t = time.time()
        self.client.update()
        if len(self.client.currentData) == 0:
            print("Failed to read any valid data from the server - exiting")
            sys.exit(1)

        queued = self.client.currentData["test_work"]["queued"]
        completed = self.client.currentData["test_work"]["completed"]
        assigned = self.client.currentData["test_work"]["assigned"]
        if self.last_queued is not None:
            diff_queued = (self.last_queued - queued) / (t - self.last_time)
            diff_completed = (completed - self.last_completed) / (t - self.last_time)
        else:
            diff_queued = 0
            diff_completed = 0
        self.last_queued = queued
        self.last_completed = completed
        self.last_time = t
        print("{}\t{}\t{:.1f}\t{:.1f}".format(queued, assigned, diff_queued, diff_completed,))

        self.sched.enter(max(self.seconds - (time.time() - t), 0), 0, self.updateResults, ())


class MonitorClient(object):
    """
    Client that connects to a server and fetches information.
    """

    def __init__(self, sockname):
        self.socket = None
        if isinstance(sockname, str):
            self.sockname = sockname[5:]
            self.useTCP = False
        else:
            self.sockname = sockname
            self.useTCP = True
        self.currentData = {}
        self.items = []

    def readSock(self, items):
        """
        Open a socket, send the specified request, and retrieve the response. Keep the socket open.
        """
        try:
            if self.socket is None:
                self.socket = socket.socket(socket.AF_INET if self.useTCP else socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.connect(self.sockname)
                self.socket.setblocking(0)
            self.socket.sendall(json.dumps(items) + "\r\n")
            data = ""
            t = time.time()
            while not data.endswith("\n"):
                try:
                    d = self.socket.recv(1024)
                except socket.error as se:
                    if se.args[0] != errno.EWOULDBLOCK:
                        raise
                    if time.time() - t > 5:
                        raise socket.error
                    continue
                if d:
                    data += d
                else:
                    break
            data = json.loads(data)
        except socket.error:
            data = {}
            self.socket = None
        except ValueError:
            data = {}
        return data

    def update(self):
        """
        Update the current data from the server.
        """

        # Only read each item once
        self.currentData = self.readSock(list(set(self.items)))

    def getOneItem(self, item):
        """
        Update the current data from the server.
        """
        data = self.readSock([item])
        return data[item] if data else None

    def addItem(self, item):
        """
        Add a server data item to monitor.
        """
        self.items.append(item)

    def removeItem(self, item):
        """
        No need to monitor this item.
        """
        self.items.remove(item)

if __name__ == "__main__":
    main()
