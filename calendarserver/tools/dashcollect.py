#!/usr/bin/env python
##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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

"""
A service that logs dashboard data from multiple hosts and stores them in
log files. It can also, optionally, make the most recent data available
for retrieval via a simple TCP socket read on a specific port.

This tool uses its own config file, specified with the '-f' option. A
sample is shown below:

{
    "title": "My CalDAV service",
    "pods": {
        "podA": {
            "description": "Main pod",
            "servers": [
                "podAhost1.example.com:8100",
                "podAhost2.example.com:8100"
            ]
        },
        "podB": {
            "description": "Development pod",
            "servers": [
                "podBhost1.example.com:8100",
                "podBhost2.example.com:8100"
            ]
        }
    }
}
"""
from __future__ import print_function

from argparse import HelpFormatter, SUPPRESS, OPTIONAL, ZERO_OR_MORE, \
    ArgumentParser
from collections import OrderedDict
from datetime import datetime, date
from threading import Thread
import SocketServer
import errno
import json
import os
import sched
import socket
import sys
import time
import zlib

verbose = False


def _verbose(log):
    if verbose:
        print(log)


class MyHelpFormatter(HelpFormatter):
    """
    Help message formatter which adds default values to argument help and
    retains formatting of all help text.
    """

    def _fill_text(self, text, width, indent):
        return ''.join([indent + line for line in text.splitlines(True)])

    def _get_help_string(self, action):
        help = action.help
        if '%(default)' not in action.help:
            if action.default is not SUPPRESS:
                defaulting_nargs = [OPTIONAL, ZERO_OR_MORE]
                if action.option_strings or action.nargs in defaulting_nargs:
                    help += ' (default: %(default)s)'
        return help


def main():
    try:
        # to produce a docstring target
        thisFile = __file__
    except NameError:
        # unlikely but possible...
        thisFile = sys.argv[0]
    parser = ArgumentParser(
        formatter_class=MyHelpFormatter,
        description="Dashboard service for CalendarServer.",
        epilog="To view the docstring, run: pydoc {}".format(thisFile),
    )
    parser.add_argument("-f", default=SUPPRESS, required=True, help="Server config file (see docstring for details)")
    parser.add_argument("-l", default=SUPPRESS, required=True, help="Log file directory")
    parser.add_argument("-n", action="store_true", help="Create a new log file when starting, existing log file is deleted")
    parser.add_argument("-s", default="localhost:8200", help="Make JSON data available on the specified host:port")
    parser.add_argument("-t", action="store_true", help="Rotate log files every hour, otherwise once per day")
    parser.add_argument("-z", action="store_true", help="zlib compress json records in log files")
    parser.add_argument("-v", action="store_true", help="Verbose")
    args = parser.parse_args()
    if args.v:
        global verbose
        verbose = True

    config = Config()
    try:
        config.loadFromFile(args.f)
    except:
        parser.print_usage()
        sys.exit(1)

    # Remove any existing logfile is asked
    if args.n:
        logfile = DashboardCollector.logfile(args.l, args.t)
        if os.path.exists(logfile):
            os.remove(logfile)

    print("Running DashboardCollector...")
    dash = DashboardCollector(config, args.l, args.t, args.z)
    dash_thread = Thread(target=dash.run)
    dash_thread.start()

    if args.s:
        print("Running the CollectorService...")
        host = args.s
        if not host.startswith("unix:"):
            host = host.split(":")
            if len(host) == 1:
                host.append(8200)
            else:
                host[1] = int(host[1])
            host = tuple(host)

        try:
            server = CollectorService(host, CollectorRequestHandler)
            server.dashboard = dash
            server_thread = Thread(target=server.serve_forever)
            server_thread.daemon = True
            server_thread.start()
        except Exception as e:
            print("Terminating service due to error: {}".format(e))
            dash.stop()
            server.shutdown()
            server_thread.join()
            sys.exit(1)

    while dash_thread.isAlive():
        try:
            dash_thread.join(1000)
        except KeyboardInterrupt:
            print("Terminating service")
            dash.stop()
            server.shutdown()
            server_thread.join()


class Config(object):
    """
    Loads the config and creates a list of L{Pod}'s.

    JSON schema for server config file:

        ; root object
        OBJECT (title, pods)

        ; Title/description of this config
        MEMBER title "title" : STRING

        ; pods - set of pods to monitor
        MEMBER pods "pod" : Object (
            *pod
        )

        ; An event type
        MEMBER pod "" : Object(
            ?description, servers
        )

        ; The description of a pod
        MEMBER description "description" : STRING

        ; Servers associated with a pod
        ; Server names are either "host:port", or "unix:path"
        MEMBER servers "servers" : ARRAY STRING
    """

    def loadFromFile(self, path):
        _verbose("Loading config file {}".format(path))
        with open(path) as f:
            try:
                jsondata = json.loads(f.read(), object_pairs_hook=OrderedDict)
            except Exception:
                print("Could not read JSON data from {}".format(path))
                raise RuntimeError("Could not read JSON data from {}".format(path))

        try:
            self.title = jsondata["title"]
            _verbose("Config '{}'".format(self.title))
            self.pods = [Pod(podname, data) for podname, data in jsondata["pods"].items()]
        except Exception:
            print("No valid JSON data in {}".format(path))
            raise RuntimeError("No valid JSON data in {}".format(path))


class Pod(object):
    """
    Model object that represents an L{Pod}.
    """

    def __init__(self, title, jsondata):
        """
        Parse the pod details from the JSON data and create the list of L{Server}'s.
        """
        self.title = title
        self.description = jsondata.get("description", "")
        _verbose("  Pod '{}': {}".format(self.title, self.description))
        self.servers = [Server(data) for data in jsondata.get("servers", [])]

        # Setup each L{Server} with the set of stats items they need to read
        for ctr, server in enumerate(self.servers):
            _verbose("    Server: {}".format(server.sockname))
            server.addItem("stats_system")
            server.addItem("stats")
            server.addItem("slots")
            server.addItem("job_assignments")
            server.addItem("jobcount")
            server.addItem("directory")

            # Only read this once as otherwise too much load
            if ctr == 0:
                server.addItem("jobs")

    def sendSock(self):
        """
        Update the data for each L{Server} in this L{Pod}.
        """
        _verbose("  Pod send: {}".format(self.title))
        for server in self.servers:
            server.sendSock()

    def update(self, data):
        """
        Update the data for each L{Server} in this L{Pod}.
        """
        _verbose("  Pod read: {}".format(self.title))
        data[self.title] = OrderedDict()
        for server in self.servers:
            server.update(data[self.title])


class Server(object):
    """
    Model object that represents a server in a pod.
    """

    def __init__(self, host):
        """
        Setup the appropriate socket connection details.
        """
        self.host = host
        self.socket = None
        if host.startswith("unix:"):
            self.sockname = host[5:]
            self.useTCP = False
        else:
            host = host.split(":")
            if len(host) == 1:
                host.append(8100)
            else:
                host[1] = int(host[1])
            self.sockname = tuple(host)
            self.useTCP = True
        self.currentData = {}
        self.items = []

    def sendSock(self):
        """
        Open a socket, send the specified request, and retrieve the response. Keep the socket open.
        """
        items = list(set(self.items))
        try:
            if self.socket is None:
                if self.useTCP:
                    self.socket = socket.create_connection(self.sockname, 1.0)
                else:
                    self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.socket.connect(self.sockname)
                self.socket.setblocking(0)
            self.socket.sendall(json.dumps(items) + "\r\n")
        except socket.error as e:
            self.socket = None
            _verbose("    server failed: {} {}".format(self.host, e))
        except ValueError as e:
            _verbose("    server failed: {} {}".format(self.host, e))

    def readSock(self, items):
        """
        Open a socket, send the specified request, and retrieve the response. Keep the socket open.
        """
        if self.socket is None:
            return {}
        try:
            data = ""
            t = time.time()
            while not data.endswith("\n"):
                try:
                    d = self.socket.recv(1024)
                except socket.error as se:
                    if se.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.time() - t > 5:
                        raise socket.error
                    continue
                if d:
                    data += d
                else:
                    break
            data = json.loads(data, object_pairs_hook=OrderedDict)
        except socket.error as e:
            data = {}
            self.socket = None
            _verbose("    server failed: {} {}".format(self.host, e))
        except ValueError as e:
            data = {}
            _verbose("    server failed: {} {}".format(self.host, e))
        return data

    def update(self, data):
        """
        Update the current data from the server.
        """

        # Only read each item once
        self.currentData = self.readSock(list(set(self.items)))
        data[self.host] = self.currentData
        _verbose("    Server read: {} {}".format(self.host, len(data[self.host])))
        #_verbose("      Data: {}".format(self.currentData))

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
        try:
            self.items.remove(item)
        except ValueError:
            # Don't care if the item is not present
            pass


class DashboardCollector(object):
    """
    Main dashboard controller. Use Python's L{sched} feature to schedule
    updates.
    """

    def __init__(self, config, logdir, loghourly, compress_log):
        self.logdir = logdir
        self.loghourly = loghourly
        self.compress_log = compress_log
        self.title = config.title
        self.pods = config.pods
        self.sched = sched.scheduler(time.time, time.sleep)
        self.seconds = 1
        self.lastData = {}
        self._stop = False

    def run(self):
        """
        Start the L{scheduler}.
        """
        _verbose("Starting Dashboard")
        self.sched.enter(self.seconds, 0, self.update, ())
        self.sched.run()
        _verbose("Stopped Dashboard")

    def stop(self):
        self._stop = True

    @staticmethod
    def logfile(logdir, loghourly):
        """
        Log file name based on current date so it rotates once a day, or hourly.
        """
        today = date.today().isoformat()
        if loghourly:
            hour = datetime.now().hour
            return os.path.join(logdir, "dashboard-{today}T{hour:02d}00.log".format(today=today, hour=hour))
        else:
            return os.path.join(logdir, "dashboard-{today}.log".format(today=today))

    def update(self):
        """
        Update data from each pod.
        """

        _verbose("Update pods")
        j = OrderedDict()
        j["timestamp"] = datetime.now().replace(microsecond=0).isoformat()
        j["pods"] = OrderedDict()

        for pod in self.pods:
            pod.sendSock()

        for pod in self.pods:
            pod.update(j["pods"])

        # Filter out unwanted data
        for jpod in j["pods"].values():
            for jhost in jpod.values():
                for stattype in ("current", "1m", "5m", "1h"):
                    for statkey in ("uid", "user-agent"):
                        try:
                            del jhost["stats"][stattype][statkey]
                        except KeyError:
                            pass

        # Append to log file
        with open(self.logfile(self.logdir, self.loghourly), "a") as f:
            jstr = json.dumps(j)
            zstr = zlib.compress(jstr).encode("base64").replace("\n", "") if self.compress_log else jstr
            f.write("\x1e{}\n".format(zstr))

        self.lastData = j

        if not self._stop:
            self.sched.enter(self.seconds, 0, self.update, ())


class CollectorService(SocketServer.ThreadingTCPServer):
    """
    L{ThreadingTCPServer} that sends out the current data from
    the L{DashbordCollector}.
    """

    def data(self):
        if hasattr(self, "dashboard"):
            return json.dumps(self.dashboard.lastData)
        else:
            return "{}"


class CollectorRequestHandler(SocketServer.BaseRequestHandler):
    """
    Request handler for L{CollectorService} that just sends back the current data.
    """

    def handle(self):
        self.request.sendall(self.server.data())


if __name__ == "__main__":
    main()
