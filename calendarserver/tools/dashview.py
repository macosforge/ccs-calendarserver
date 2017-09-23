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

"""
A curses (or plain text) based dashboard for viewing various aspects of the
server as exposed by the L{DashboardProtocol} stats socket.
"""
from __future__ import print_function

from argparse import HelpFormatter, SUPPRESS, OPTIONAL, ZERO_OR_MORE, \
    ArgumentParser
from collections import OrderedDict, defaultdict
from operator import itemgetter
from zlib import decompress
import curses.panel
import errno
import fcntl
import json
import logging
import os
import sched
import socket
import struct
import sys
import termios
import time

LOG_FILENAME = "db.log"
#logging.basicConfig(filename=LOG_FILENAME, level=logging.DEBUG)


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
    parser = ArgumentParser(
        formatter_class=MyHelpFormatter,
        description="Dashboard collector viewer service for CalendarServer.",
        epilog="One of -s or -l should be specified",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-s", default="localhost:8200", help="Dashboard collector service host:port")
    group.add_argument("-l", default=SUPPRESS, help="Dashboard collector log file to replay data from")
    args = parser.parse_args()

    #
    # Get configuration
    #
    if hasattr(args, "l"):
        server = None
        logfile = open(os.path.expanduser(args.l))
    else:
        server = args.s
        if not server.startswith("unix:"):
            server = server.split(":")
            if len(server) == 1:
                server.append(8100)
            else:
                server[1] = int(server[1])
            server = tuple(server)
        logfile = None

    def _wrapped(stdscrn):
        try:
            curses.curs_set(0)  # make the cursor invisible
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, curses.COLOR_WHITE)
        except:
            pass
        d = Dashboard(server, logfile, stdscrn)
        d.run()
    curses.wrapper(_wrapped)

#    client = DashboardClient(self, server)
#    client.getOneItem("podA", "localhost:8100", "jobcount")
#    print(json.dumps(client.currentData["pods"]["podA"]["aggregate"], indent=1))

#    dashboard = Dashboard(server, logfile, None)
#    client = DashboardLogfile(dashboard, logfile)
#    client.getOneItem("podA", "localhost:8100", "jobcount")
#    print(json.dumps(client.currentData["pods"]["podA"]["cal-prod-01.pixar.com:8100"], indent=1))


def safeDivision(value, total, factor=1):
    return value * factor / total if total else 0


def defaultIfNone(x, default):
    return x if x is not None else default


def terminal_size():
    h, w, _ignore_hp, _ignore_wp = struct.unpack(
        'HHHH',
        fcntl.ioctl(
            0, termios.TIOCGWINSZ,
            struct.pack('HHHH', 0, 0, 0, 0)
        )
    )
    return w, h


class Dashboard(object):
    """
    Main dashboard controller. Use Python's L{sched} feature to schedule
    updates.
    """

    MODE_SERVER = 1
    MODE_LOGREPLAY = 2

    screen = None
    registered_windows = OrderedDict()
    registered_window_sets = {
        "D": ("Directory Panels", [],),
        "H": ("HTTP Panels", [],),
        "J": ("Jobs Panels", [],),
    }

    def __init__(self, server, logfile, screen):
        self.screen = screen
        self.paused = False
        self.seconds = 1.0
        self.sched = sched.scheduler(time.time, time.sleep)

        self.aggregate = False
        self.selected_server = Point()
        self.server_window = None

        if server:
            self.client = DashboardClient(self, server)
            self.mode = self.MODE_SERVER
        else:
            self.client = DashboardLogfile(self, logfile)
            self.mode = self.MODE_LOGREPLAY
        self.client_error = False

    @classmethod
    def registerWindow(cls, wtype, keypress):
        """
        Register a window type along with a key press action. This allows the
        controller to select the appropriate window when its key is pressed,
        and also provides help information to the L{HelpWindow} for each
        available window type.
        """
        cls.registered_windows[keypress] = wtype

    @classmethod
    def registerWindowSet(cls, wtype, keypress):
        """
        Register a set of window types along with a key press action. This allows the
        controller to select the appropriate set of windows when its key is pressed,
        and also provides help information to the L{HelpWindow} for each
        available window set type.
        """
        cls.registered_window_sets[keypress][1].append(wtype)

    def run(self):
        """
        Create the initial window and run the L{scheduler}.
        """
        self.windows = []
        self.client.update()
        self.displayWindow(None)
        self.sched.enter(self.seconds, 0, self.updateDisplay, ())
        self.sched.run()

    def displayWindow(self, wtype):
        """
        Toggle the specified window, or reset to launch state if None.
        """

        # Toggle a specific window on or off
        if isinstance(wtype, type):
            if wtype not in [type(w) for w in self.windows]:
                self.windows.append(wtype(self).makeWindow())
                self.windows[-1].activate()
            else:
                for window in self.windows:
                    if type(window) == wtype:
                        window.deactivate()
                        self.windows.remove(window)
                    if len(self.windows) == 0:
                        self.displayWindow(self.registered_windows["h"])

            self.resetWindows()

        # Reset the screen to the default config
        else:
            if self.windows:
                for window in self.windows:
                    window.deactivate()
                self.windows = []
            top = 0

            self.server_window = ServersMenu(self).makeWindow()
            self.windows.append(self.server_window)
            self.windows[-1].activate()
            top += self.windows[-1].nlines + 1
            help_top = top

            if wtype is None:
                # All windows in registered order
                ordered_windows = self.registered_windows.values()
            else:
                ordered_windows = list(wtype)
            for wtype in filter(lambda x: x.all, ordered_windows):
                new_win = wtype(self).makeWindow(top=top)
                logging.debug('created %r at panel level %r' % (new_win, new_win.z_order))
                self.windows.append(wtype(self).makeWindow(top=top))
                self.windows[-1].activate()
                top += self.windows[-1].nlines + 1

            # Don't display help panel if the window is too narrow
            term_w, term_h = terminal_size()
            logging.debug("HelpWindow: rows: %s  cols: %s" % (term_h, term_w))
            if int(term_w) > 100:
                logging.debug("HelpWindow: term_w > 100, making window with top at %d" % (top))
                self.windows.append(HelpWindow(self).makeWindow(top=help_top))
                self.windows[-1].activate()

        curses.panel.update_panels()
        self.updateDisplay(True)

    def resetWindows(self):
        """
        Reset the current set of windows.
        """
        if self.windows:
            logging.debug("resetting windows: %r" % (self.windows))
            for window in self.windows:
                window.deactivate()
            old_windows = self.windows
            self.windows = []
            top = 0
            for old in old_windows:
                logging.debug("processing window of type %r" % (type(old)))
                self.windows.append(old.__class__(self).makeWindow(top=top))
                self.windows[-1].activate()
                # Allow the help window to float on the right edge
                if old.__class__.__name__ != "HelpWindow":
                    top += self.windows[-1].nlines + 1

    def updateDisplay(self, initialUpdate=False):
        """
        Periodic update of the current window and check for a key press.
        """
        if not self.paused:
            self.client.update()
        client_error = len(self.client.currentData) == 0
        if client_error ^ self.client_error:
            self.client_error = client_error
            self.resetWindows()
        elif filter(lambda x: x.requiresReset(), self.windows):
            self.resetWindows()

        try:
            if not self.paused or initialUpdate:
                for window in filter(
                    lambda x: x.requiresUpdate() or initialUpdate,
                    self.windows
                ):
                    logging.debug("updating: {}".format(window))
                    window.update()
        except Exception as e:
            logging.debug("updateDisplay failed: {}".format(e))

        # Check keystrokes
        self.processKeys()

        if not initialUpdate:
            self.sched.enter(self.seconds, 0, self.updateDisplay, ())

    def processKeys(self):
        """
        Check for a key press.
        """
        try:
            self.windows[-1].window.keypad(1)
            c = self.windows[-1].window.getkey()
        except:
            c = -1
        if c == "q":
            sys.exit(0)
        elif c == " ":
            self.paused = not self.paused
        elif c == "t":
            self.seconds = 1.0 if self.seconds == 0.1 else 0.1
        elif c == "a":
            self.displayWindow(None)
        elif c == "n":
            if self.windows:
                for window in self.windows:
                    window.deactivate()
                self.windows = []
                self.displayWindow(self.registered_windows["h"])

        elif c == "x":
            self.aggregate = not self.aggregate
            self.client.update()
            self.displayWindow(None)

        elif c in self.registered_windows:
            self.displayWindow(self.registered_windows[c])

        elif c in self.registered_window_sets:
            self.displayWindow(self.registered_window_sets[c][1])

        elif c in (curses.keyname(curses.KEY_LEFT), curses.keyname(curses.KEY_RIGHT)) and self.server_window:
            self.selected_server.xplus(-1 if c == curses.keyname(curses.KEY_LEFT) else 1)
            if self.selected_server.x < 0:
                self.selected_server.x = 0
            elif self.selected_server.x >= len(self.serversForPod(self.pods()[self.selected_server.y])):
                self.selected_server.x = len(self.serversForPod(self.pods()[self.selected_server.y])) - 1
            self.resetWindows()

        elif c in (curses.keyname(curses.KEY_UP), curses.keyname(curses.KEY_DOWN)) and self.server_window:
            self.selected_server.yplus(-1 if c == curses.keyname(curses.KEY_UP) else 1)
            if self.selected_server.y < 0:
                self.selected_server.y = 0
            elif self.selected_server.y >= len(self.pods()):
                self.selected_server.y = len(self.pods()) - 1
            if self.selected_server.x >= len(self.serversForPod(self.pods()[self.selected_server.y])):
                self.selected_server.x = len(self.serversForPod(self.pods()[self.selected_server.y])) - 1
            self.resetWindows()

        elif c == "1" and self.mode == self.MODE_LOGREPLAY:
            self.client.skipLines(60)

        elif c == "5" and self.mode == self.MODE_LOGREPLAY:
            self.client.skipLines(300)

    def dataForItem(self, item):
        return self.client.getOneItem(
            self.selectedPod(),
            self.selectedServer(),
            item,
        )

    def timestamp(self):
        return self.client.currentData["timestamp"]

    def pods(self):
        return sorted(self.client.currentData.get("pods", {}).keys())

    def selectedPod(self):
        return self.pods()[self.selected_server.y]

    def serversForPod(self, pod):
        return sorted(self.client.currentData.get("pods", {pod: {}})[pod].keys())

    def selectedServer(self):
        return self.serversForPod(self.selectedPod())[self.selected_server.x]

    def selectedAggregateCount(self):
        return self.client.currentServerCount.get(self.selectedPod(), 1) if self.aggregate else 1


class BaseDashboardClient(object):
    """
    Base class for clients that get dashboard data.
    """

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.currentData = {}
        self.currentServerCount = {}

    def readData(self):
        """
        Get the data
        """
        raise NotImplementedError

    def update(self):
        """
        Update the current data from the server.
        """

        # Only read each item once
        self.currentData = self.readData()
        for pod, servers in self.currentData["pods"].items():
            self.currentServerCount[pod] = len(servers)
        if self.dashboard.aggregate:
            self.aggregateData()

    def getOneItem(self, pod, server, item):
        """
        Update the current data from the server.
        """
        if len(self.currentData) == 0:
            self.update()

        # jobs are only requested from the first server in a pod because otherwise
        # it would be too expensive to run the DB query for all servers. So when we
        # need the jobs data, always substitute the first server's data
        if item in ("jobs", "jobcount"):
            server = sorted(self.currentData["pods"][pod].keys())[0]

        return self.currentData["pods"][pod][server].get(item)

    def aggregateData(self):
        """
        Aggregate the data from all hosts into one for each pod.
        """
        results = OrderedDict()
        results["timestamp"] = self.currentData["timestamp"]
        results["pods"] = OrderedDict()
        for pod in self.currentData["pods"].keys():
            results["pods"][pod] = OrderedDict()
            results["pods"][pod]["aggregate"] = self.aggregatePodData(self.currentData["pods"][pod])
        self.currentData = results

    def aggregatePodData(self, data):
        """
        Aggregate the data from a pod from all hosts into one.

        @param data: host data to aggregate
        @type data: L{dict}
        """

        results = OrderedDict()
        results["aggregateCount"] = len(data)

        # Get all items available in all servers first
        items = defaultdict(list)
        for server in data.keys():
            for item in data[server].keys():
                items[item].append(data[server][item])

        # Iterate each item to get the data sets and aggregate
        for item, allHostData in items.items():
            method_name = "aggregator_{}".format(item)
            if not hasattr(Aggregator, method_name):
                print("Missing aggregator for {}".format(item))
                logging.error("Missing aggregator for {}".format(item))
                results[item] = allHostData[0]
            else:
                results[item] = getattr(Aggregator, method_name)(allHostData)

        return results


class DashboardClient(BaseDashboardClient):
    """
    Client that connects to a server and fetches information.
    """

    def __init__(self, dashboard, sockname):
        super(DashboardClient, self).__init__(dashboard)
        self.socket = None
        if isinstance(sockname, str):
            self.sockname = sockname[5:]
            self.useTCP = False
        else:
            self.sockname = sockname
            self.useTCP = True

    def readData(self):
        """
        Open a socket, send the specified request, and retrieve the response. The socket closes.
        """
        try:
            self.socket = socket.socket(socket.AF_INET if self.useTCP else socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.connect(self.sockname)
            self.socket.setblocking(0)
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
            data = json.loads(data, object_pairs_hook=OrderedDict)
            logging.debug("data: {}".format(len(data)))
            self.socket.close()
            self.socket = None
        except socket.error as e:
            data = {}
            self.socket = None
            logging.debug("readData: failed: {}".format(e))
        except ValueError as e:
            data = {}
            logging.debug("readData: failed: {}".format(e))
        return data


class DashboardLogfile(BaseDashboardClient):
    """
    Client that gets data from a log file.
    """

    def __init__(self, dashboard, logfile):
        super(DashboardLogfile, self).__init__(dashboard)
        self.logfile = logfile

    def readData(self):
        """
        Read one line of data from the logile
        """
        try:
            line = self.logfile.readline()
            if line[0] == "\x1e":
                line = line[1:]
            if line[0] != "{":
                line = decompress(line.decode("base64"))
            data = json.loads(line)
        except ValueError as e:
            data = {}
            logging.debug("readData: failed: {}".format(e))
        return data

    def skipLines(self, count):
        try:
            for _ignore in range(count):
                self.logfile.readline()
        except ValueError as e:
            logging.debug("skipLines: failed: {}".format(e))


class Aggregator(object):

    @staticmethod
    def aggregator_directory(serversdata):
        results = OrderedDict()
        for server_data in serversdata:
            for operation_name, operation_details in server_data.items():
                if operation_name not in results:
                    results[operation_name] = operation_details
                elif isinstance(results[operation_name], list):
                    results[operation_name][0] += operation_details[0]
                    results[operation_name][1] += operation_details[1]
                else:
                    results[operation_name] += operation_details

        return results

    @staticmethod
    def aggregator_job_assignments(serversdata):

        results = OrderedDict()
        results["workers"] = [[0, 0, 0]] * max(map(len, map(itemgetter("workers"), serversdata)))
        results["level"] = sum(map(itemgetter("level"), serversdata))

        for server_data in serversdata:
            for ctr, item in enumerate(server_data["workers"]):
                for i in range(3):
                    results["workers"][ctr][i] += item[i]
        return results

    @staticmethod
    def aggregator_jobcount(serversdata):
        return serversdata[0]

    @staticmethod
    def aggregator_jobs(serversdata):
        return serversdata[0]

    @staticmethod
    def aggregator_stats(serversdata):
        results = OrderedDict()
        for stat in ("current", "1m", "5m", "1h"):
            if stat in serversdata[0]:
                results[stat] = Aggregator.serverStat(map(itemgetter(stat), serversdata))

        # NB ignore the "system" key as it is not used

        return results

    @staticmethod
    def serverStat(serversdata):
        results = OrderedDict()

        # Values that are summed
        for key in ("requests", "t", "t-resp-wr", "401", "500", "cpu", "slots", "max-slots",):
            if key in serversdata[0]:
                results[key] = sum(map(itemgetter(key), serversdata))

        # Averaged
        for key in ("cpu", "max-slots",):
            if key in results:
                results[key] = safeDivision(results[key], len(serversdata))

        # Values that are maxed
        for key in ("T-MAX",):
            if key in serversdata[0]:
                results[key] = max(map(itemgetter(key), serversdata))

        # Values that are summed dict values
        for key in ("method", "method-t", "uid", "user-agent", "T", "T-RESP-WR",):
            if key in serversdata[0]:
                results[key] = Aggregator.dictValueSums(map(itemgetter(key), serversdata))

        return results

    @staticmethod
    def aggregator_slots(serversdata):
        results = OrderedDict()

        # Sum all items in each dict
        slot_count = len(serversdata[0]["slots"])
        results["slots"] = []
        for i in range(slot_count):
            results["slots"].append(Aggregator.dictValueSums(map(
                itemgetter(i),
                map(itemgetter("slots"), serversdata)
            )))
            results["slots"][i]["slot"] = i

        # Check for any one being overloaded
        results["overloaded"] = any(map(itemgetter("overloaded"), serversdata))

        return results

    @staticmethod
    def aggregator_stats_system(serversdata):
        results = OrderedDict()
        for server_data in serversdata:
            for stat_name, stat_value in server_data.items():
                if stat_name not in results:
                    results[stat_name] = stat_value
                else:
                    results[stat_name] += stat_value

        # Some values should be averaged
        for stat_name in ("memory used", "cpu use", "memory percent"):
            results[stat_name] = safeDivision(results[stat_name], len(serversdata))

        # Want the earliest time
        results["start time"] = min(map(itemgetter("start time"), serversdata))

        return results

    @staticmethod
    def dictValueSums(listOfDicts):
        """
        Sum the values of a list of dicts.

        @param listOfDicts: list of dicts to sum
        @type listOfDicts: L{list} of L{dict}
        """
        results = OrderedDict()
        for result in listOfDicts:
            for key, value in result.items():
                if key not in results:
                    results[key] = value
                else:
                    results[key] += value
        return results


class Point(object):

    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y

    def __eq__(self, other):
        return self.x == other.x and self.y == other.y

    def xplus(self, xdiff=1):
        self.x += xdiff

    def yplus(self, ydiff=1):
        self.y += ydiff


class BaseWindow(object):
    """
    Common behavior for window types.
    """

    help = "Not Implemented"
    all = True
    clientItem = None

    windowTitle = ""
    formatWidth = 0
    additionalRows = 0

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.rowCount = 0
        self.needsReset = False
        self.z_order = "bottom"

    def makeWindow(self, top=0, left=0):
        self.updateRowCount()
        self._createWindow(
            self.windowTitle,
            self.rowCount + self.additionalRows,
            self.formatWidth,
            begin_y=top, begin_x=left
        )
        return self

    def updateRowCount(self):
        """
        Update L{self.rowCount} based on the current data
        """
        raise NotImplementedError()

    def _createWindow(
        self, title, nlines, ncols, begin_y=0, begin_x=0
    ):
        """
        Initialize a curses window based on the sizes required.
        """
        self.window = curses.newwin(nlines, ncols, begin_y, begin_x)
        self.window.nodelay(1)
        self.panel = curses.panel.new_panel(self.window)
        eval("self.panel.%s()" % (self.z_order,))
        self.title = title
        self.nlines = nlines
        self.ncols = ncols
        self.iter = 0
        self.lastResult = {}

    def requiresUpdate(self):
        """
        Indicates whether a window type has dynamic data that should be
        refreshed on each update, or whether it is static data (e.g.,
        L{HelpWindow}) that only needs to be drawn once.
        """
        return True

    def requiresReset(self):
        """
        Indicates that the window needs a full reset, because e.g., the
        number of items it displays has changed.
        """
        return self.needsReset

    def activate(self):
        """
        About to start displaying.
        """
        # Update once when activated
        if not self.requiresUpdate():
            self.update()

    def deactivate(self):
        """
        Clear any drawing done by the current window type.
        """
        self.window.erase()
        self.window.refresh()

    def update(self):
        """
        Periodic window update - redraw the window.
        """
        raise NotImplementedError()

    def tableHeader(self, hdrs, count):
        """
        Generate the header rows.
        """
        self.window.erase()
        self.window.border()
        self.window.addstr(
            0, 2,
            self.title + " {} ({})".format(count, self.iter)
        )

        pt = Point(1, 1)

        for hdr in hdrs:
            self.window.addstr(pt.y, pt.x, hdr, curses.A_REVERSE)
            pt.yplus()

        return pt

    def tableFooter(self, feet, pt):
        """
        Generate the footer rows.
        """
        self.window.hline(pt.y, pt.x, "-", self.formatWidth - 2)
        pt.yplus()
        for footer in feet:
            self.window.addstr(pt.y, pt.x, footer)
            pt.yplus()

    def tableRow(self, text, pt, style=curses.A_NORMAL):
        """
        Generate a single row.
        """
        try:
            self.window.addstr(
                pt.y, pt.x, text,
                style
            )
        except curses.error as e:
            logging.debug("tableRow: failed: {}".format(e))
        pt.yplus()

    def clientData(self, item=None):
        return self.dashboard.dataForItem(item if item else self.clientItem)


class ServersMenu(BaseWindow):
    """
    Top menu if multiple servers are present.
    """

    help = "servers help"
    all = False

    windowTitle = "Servers"
    formatWidth = 0
    additionalRows = 0

    def makeWindow(self, top=0, left=0):
        term_w, _ignore_term_h = terminal_size()
        self.formatWidth = term_w - 50
        return super(ServersMenu, self).makeWindow(0, 0)

    def updateRowCount(self):
        self.rowCount = len(self.dashboard.pods())

    def requiresUpdate(self):
        return False

    def update(self):

        self.window.erase()

        pods = self.dashboard.pods()
        width = max(map(len, pods)) if pods else 0

        pt = Point()
        for row, pod in enumerate(pods):
            pt.x = 0

            s = ("Pod: {:>" + str(width) + "} | Servers: |").format(pod)
            self.window.addstr(pt.y, pt.x, s)
            pt.xplus(len(s))

            selected_server = None
            for column, server in enumerate(self.dashboard.serversForPod(pod)):
                cell = Point(column, row)
                selected = cell == self.dashboard.selected_server
                s = " {:02d} ".format(column + 1)
                self.window.addstr(
                    pt.y, pt.x, s,
                    curses.A_REVERSE if selected else curses.A_NORMAL
                )
                pt.xplus(len(s))
                self.window.addstr(pt.y, pt.x, "|")
                pt.xplus()
                if selected:
                    selected_server = server

            self.window.addstr(pt.y, pt.x, " {}".format(selected_server if selected_server else ""))
            pt.yplus()

        self.window.refresh()


class HelpWindow(BaseWindow):
    """
    Display help for the dashboard.
    """

    help = "Help"
    all = False
    helpItems = (
        " a - All Panels",
        " n - No Panels",
        "",
        "   - (space) Pause",
        " t - Toggle Update Speed",
        " x - Toggle Aggregate Mode",
        "",
        " q - Quit",
    )
    helpItemsReplay = (
        " a - All Panels",
        " n - No Panels",
        "",
        "   - (space) Pause",
        " 1 - ahead one minute",
        " 5 - ahead five minutes",
        " t - Toggle Update Speed",
        " x - Toggle Aggregate Mode",
        "",
        " q - Quit",
    )

    windowTitle = "Help"
    formatWidth = 28
    additionalRows = 3

    def __init__(self, dashboard):
        super(HelpWindow, self).__init__(dashboard)
        if self.dashboard.mode == self.dashboard.MODE_LOGREPLAY:
            self.helpItems = self.helpItemsReplay

    def makeWindow(self, top=0, left=0):
        term_w, _ignore_term_h = terminal_size()
        help_x_offset = term_w - self.formatWidth
        return super(HelpWindow, self).makeWindow(0, help_x_offset)

    def updateRowCount(self):
        self.rowCount = len(self.helpItems) + len(filter(lambda x: len(x[1]) != 0, Dashboard.registered_window_sets.values())) + len(Dashboard.registered_windows)

    def requiresUpdate(self):
        return False

    def update(self):

        self.window.erase()
        self.window.border()
        self.window.addstr(0, 2, "Hotkeys")

        pt = Point(1, 1)

        items = [" {} - {}".format(keypress, wtype.help) for keypress, wtype in Dashboard.registered_windows.items()]
        items.append("")
        items.extend([" {} - {}".format(key, value[0]) for key, value in Dashboard.registered_window_sets.items() if value[1]])
        items.extend(self.helpItems)

        for item in items:
            self.tableRow(item, pt)

        self.window.refresh()


class SystemWindow(BaseWindow):
    """
    Displays the system information provided by the server.
    """

    help = "System Status"
    clientItem = "stats_system"

    windowTitle = "System"
    formatWidth = 54
    additionalRows = 3

    def updateRowCount(self):
        self.rowCount = len(defaultIfNone(self.clientData(), (1, 2, 3, 4,))) + 1

    def update(self):
        records = defaultIfNone(self.clientData(), {
            "cpu use": 0.0,
            "memory percent": 0.0,
            "memory used": 0,
            "start time": time.time(),
        })
        records["timestamp"] = self.dashboard.timestamp()

        if len(records) != self.rowCount:
            self.needsReset = True
            return
        self.iter += 1

        s = " {:<30}{:>20} ".format("Item", "Value")
        pt = self.tableHeader((s,), len(records))

        records["cpu use"] = "{:.2f}".format(records["cpu use"])
        records["memory percent"] = "{:.1f}".format(records["memory percent"])
        records["memory used"] = "{:.2f} GB".format(
            records["memory used"] / (1000.0 * 1000.0 * 1000.0)
        )
        records["uptime"] = int(time.time() - records["start time"])
        hours, mins = divmod(records["uptime"] / 60, 60)
        records["uptime"] = "{}:{:02d} hh:mm".format(hours, mins)
        del records["start time"]
        records["timestamp"] = records["timestamp"].replace("T", " ")

        for item, value in sorted(records.items(), key=lambda x: x[0]):
            changed = (
                item in self.lastResult and self.lastResult[item] != value
            )
            s = " {:<30}{:>20} ".format(item, value)
            self.tableRow(
                s, pt,
                curses.A_REVERSE if changed else curses.A_NORMAL,
            )

        self.window.refresh()

        self.lastResult = records


class RequestStatsWindow(BaseWindow):
    """
    Displays the status of the server's master process worker slave slots.
    """

    help = "HTTP Requests"
    clientItem = "stats"

    windowTitle = "Request Statistics"
    formatWidth = 92
    additionalRows = 4

    def updateRowCount(self):
        self.rowCount = 4

    def update(self):
        records = defaultIfNone(self.clientData(), {})
        self.iter += 1

        s1 = " {:<8}{:>8}{:>10}{:>10}{:>10}{:>10}{:>8}{:>8}{:>8}{:>8} ".format(
            "Period", "Reqs", "Av-Reqs", "Av-Resp", "Av-NoWr", "Max-Resp", "Slot", "Slot", "CPU ", "500's"
        )
        s2 = " {:<8}{:>8}{:>10}{:>10}{:>10}{:>10}{:>8}{:>8}{:>8}{:>8} ".format(
            "", "", "per sec", "(ms)", "(ms)", "(ms)", "Avg.", "Max", "Avg.", ""
        )
        pt = self.tableHeader((s1, s2,), len(records))

        for key, seconds in (("current", 60,), ("1m", 60,), ("5m", 5 * 60,), ("1h", 60 * 60,),):
            stat = records.get(key, {
                "requests": 0,
                "t": 0.0,
                "t-resp-wr": 0.0,
                "T-MAX": 0.0,
                "slots": 0,
                "cpu": 0.0,
                "500": 0,
            })
            s = " {:<8}{:>8}{:>10.1f}{:>10.1f}{:>10.1f}{:>10.1f}{:>8.2f}{:>8}{:>7.1f}%{:>8} ".format(
                key,
                stat["requests"],
                safeDivision(float(stat["requests"]), seconds),
                safeDivision(stat["t"], stat["requests"]),
                safeDivision(stat["t"] - stat["t-resp-wr"], stat["requests"]),
                stat["T-MAX"],
                safeDivision(float(stat["slots"]), stat["requests"]),
                stat.get("max-slots", 0),
                safeDivision(stat["cpu"], stat["requests"]),
                stat["500"],
            )
            self.tableRow(s, pt)

        self.window.refresh()

        self.lastResult = records


class HTTPSlotsWindow(BaseWindow):
    """
    Displays the status of the server's master process worker slave slots.
    """

    help = "HTTP Slots"
    clientItem = "slots"

    windowTitle = "HTTP Slots"
    formatWidth = 72
    additionalRows = 5

    def updateRowCount(self):
        self.rowCount = len(defaultIfNone(self.clientData(), {"slots": ()})["slots"])

    def update(self):
        data = defaultIfNone(self.clientData(), {"slots": {}, "overloaded": False})
        records = data["slots"]
        if len(records) != self.rowCount:
            self.needsReset = True
            return
        self.iter += 1

        s = " {:>4}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8} ".format(
            "Slot", "unack", "ack", "uncls", "total",
            "start", "strting", "stopped", "abd"
        )
        pt = self.tableHeader((s,), len(records))

        for record in sorted(records, key=lambda x: x["slot"]):
            changed = (
                record["slot"] in self.lastResult and
                self.lastResult[record["slot"]] != record
            )
            s = " {:>4}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8} ".format(
                record["slot"],
                record["unacknowledged"],
                record["acknowledged"],
                record["unclosed"],
                record["total"],
                record["started"],
                record["starting"],
                record["stopped"],
                record["abandoned"],
            )
            count = record["unacknowledged"] + record["acknowledged"]
            self.tableRow(
                s, pt,
                curses.A_REVERSE if changed else (
                    curses.A_BOLD if count else curses.A_NORMAL
                ),
            )

        active_count = sum(
            [
                record["unacknowledged"] + record["acknowledged"]
                for record in records
            ]
        )
        active_slots = len(records) * self.dashboard.selectedAggregateCount()

        s = " {:<12}{:>8}{:>16}".format(
            "Total:", active_count,
            sum([record["total"] for record in records]),
        )
        s += "    {:>3d}%".format(safeDivision(active_count, active_slots, 100))
        if data["overloaded"]:
            s += " OVERLOADED"
        self.tableFooter((s,), pt)

        self.window.refresh()

        self.lastResult = records


class MethodsWindow(BaseWindow):
    """
    Display the status of the server's request methods.
    """

    help = "HTTP Methods"
    clientItem = "stats"
    stats_keys = ("current", "1m", "5m", "1h",)

    windowTitle = "Methods"
    formatWidth = 116
    additionalRows = 8

    def updateRowCount(self):
        stats = defaultIfNone(self.clientData(), {})
        methods = set()
        for key in self.stats_keys:
            methods.update(stats.get(key, {}).get("method", {}).keys())
        nlines = len(methods)
        self.rowCount = nlines

    def update(self):
        stats = defaultIfNone(self.clientData(), {})
        methods = set()
        for key in self.stats_keys:
            methods.update(stats.get(key, {}).get("method", {}).keys())
        if len(methods) != self.rowCount:
            self.needsReset = True
            return

        records = {}
        records_t = {}
        for key in self.stats_keys:
            records[key] = defaultIfNone(self.clientData(), {}).get(key, {}).get("method", {})
            records_t[key] = defaultIfNone(self.clientData(), {}).get(key, {}).get("method-t", {})
        self.iter += 1

        s1 = " {:<40}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10} ".format(
            "", "------", "current---", "------", "1m--------", "------", "5m--------", "------", "1h--------",
        )
        s2 = " {:<40}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10} ".format(
            "Method", "Number", "Av-Time", "Number", "Av-Time", "Number", "Av-Time", "Number", "Av-Time",
        )
        s3 = " {:<40}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10} ".format(
            "", "", "(ms)", "", "(ms)", "", "(ms)", "", "(ms)",
        )
        pt = self.tableHeader((s1, s2, s3,), len(records))

        total_methods = dict([(key, 0) for key in self.stats_keys])
        total_time = dict([(key, 0.0) for key in self.stats_keys])
        for method_type in sorted(methods):
            for key in self.stats_keys:
                total_methods[key] += records[key].get(method_type, 0)
                total_time[key] += records_t[key].get(method_type, 0.0)
            changed = self.lastResult.get(method_type, 0) != records["current"].get(method_type, 0)
            items = [method_type]
            for key in self.stats_keys:
                items.append(records[key].get(method_type, 0))
                items.append(safeDivision(records_t[key].get(method_type, 0), records[key].get(method_type, 0)))
            s = " {:<40}{:>8}{:>10.1f}{:>8}{:>10.1f}{:>8}{:>10.1f}{:>8}{:>10.1f} ".format(
                *items
            )
            self.tableRow(
                s, pt,
                curses.A_REVERSE if changed else curses.A_NORMAL,
            )

        items = ["Total:"]
        for key in self.stats_keys:
            items.append(total_methods[key])
            items.append(safeDivision(total_time[key], total_methods[key]))
        s1 = " {:<40}{:>8}{:>10.1f}{:>8}{:>10.1f}{:>8}{:>10.1f}{:>8}{:>10.1f} ".format(
            *items
        )
        items = ["401s:"]
        for key in self.stats_keys:
            items.append(defaultIfNone(self.clientData(), {}).get(key, {}).get("401", 0))
            items.append("")
        s2 = " {:<40}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10}{:>8}{:>10} ".format(
            *items
        )
        self.tableFooter((s1, s2,), pt)

        self.window.refresh()

        self.lastResult = defaultIfNone(self.clientData(), {}).get("current", {}).get("method", {})


class AssignmentsWindow(BaseWindow):
    """
    Displays the status of the server's master process worker slave slots.
    """

    help = "Job Assignments"
    clientItem = "job_assignments"

    windowTitle = "Job Assignments"
    formatWidth = 40
    additionalRows = 5

    def updateRowCount(self):
        self.rowCount = len(defaultIfNone(self.clientData(), {"workers": ()})["workers"])

    def update(self):
        data = defaultIfNone(self.clientData(), {"workers": {}, "level": 0})
        records = data["workers"]
        if len(records) != self.rowCount:
            self.needsReset = True
            return
        self.iter += 1

        s = " {:>4}{:>12}{:>8}{:>12} ".format(
            "Slot", "assigned", "load", "completed"
        )
        pt = self.tableHeader((s,), len(records))

        total_assigned = 0
        total_completed = 0
        for ctr, details in enumerate(records):
            assigned, load, completed = details
            total_assigned += assigned
            total_completed += completed
            changed = (
                ctr in self.lastResult and
                self.lastResult[ctr] != assigned
            )
            s = " {:>4}{:>12}{:>8}{:>12} ".format(
                ctr,
                assigned,
                load,
                completed,
            )
            self.tableRow(
                s, pt,
                curses.A_REVERSE if changed else curses.A_NORMAL,
            )

        s = " {:<6}{:>10}{:>8}{:>12}".format(
            "Total:",
            total_assigned,
            "{}%".format(data["level"]),
            total_completed,
        )
        self.tableFooter((s,), pt)

        self.window.refresh()

        self.lastResult = records


class JobsWindow(BaseWindow):
    """
    Display the status of the server's job queue.
    """

    help = "Job Activity"
    clientItem = "jobs"

    windowTitle = "Jobs"
    formatWidth = 98
    additionalRows = 6

    def updateRowCount(self):
        self.rowCount = defaultIfNone(self.clientData("jobcount"), 0)

    def update(self):
        records = defaultIfNone(self.clientData(), {})
        if len(records) != self.rowCount:
            self.needsReset = True
            return
        self.iter += 1

        s1 = " {:<40}{:>8}{:>10}{:>8}{:>8}{:>10}{:>10} ".format(
            "Work Type", "Queued", "Assigned", "Late", "Failed", "Completed", "Av-Time",
        )
        s2 = " {:<40}{:>8}{:>10}{:>8}{:>8}{:>10}{:>10} ".format(
            "", "", "", "", "", "", "(ms)",
        )
        pt = self.tableHeader((s1, s2,), len(records))

        total_queued = 0
        total_assigned = 0
        total_late = 0
        total_failed = 0
        total_completed = 0
        total_time = 0.0
        for work_type, details in sorted(records.items(), key=lambda x: x[0]):
            total_queued += details["queued"]
            total_assigned += details["assigned"]
            total_late += details["late"]
            total_failed += details["failed"]
            total_completed += details["completed"]
            total_time += details["time"]
            changed = (
                work_type in self.lastResult and
                self.lastResult[work_type]["queued"] != details["queued"]
            )
            s = "{}{:<40}{:>8}{:>10}{:>8}{:>8}{:>10}{:>10.1f} ".format(
                ">" if details["queued"] else " ",
                work_type,
                details["queued"],
                details["assigned"],
                details["late"],
                details["failed"],
                details["completed"],
                safeDivision(details["time"], details["completed"], 1000.0)
            )
            self.tableRow(
                s, pt,
                curses.A_REVERSE if changed else (
                    curses.A_BOLD if details["queued"] else curses.A_NORMAL
                ),
            )

        s = " {:<40}{:>8}{:>10}{:>8}{:>8}{:>10}{:>10.1f} ".format(
            "Total:",
            total_queued,
            total_assigned,
            total_late,
            total_failed,
            total_completed,
            safeDivision(total_time, total_completed, 1000.0)
        )
        self.tableFooter((s,), pt)

        self.window.refresh()

        self.lastResult = records


class DirectoryStatsWindow(BaseWindow):
    """
    Displays the status of the server's directory service calls
    """

    help = "Directory Service"
    clientItem = "directory"

    windowTitle = "Directory Service"
    formatWidth = 89
    additionalRows = 8

    def updateRowCount(self):
        self.rowCount = len(defaultIfNone(self.clientData(), {}))

    def update(self):
        records = defaultIfNone(self.clientData(), {})
        if len(records) != self.rowCount:
            self.needsReset = True
            return

        self.iter += 1

        s1 = " {:<40}{:>15}{:>15}{:>15} ".format(
            "Method", "Calls", "Total", "Average"
        )
        s2 = " {:<40}{:>15}{:>15}{:>15} ".format(
            "", "", "(sec)", "(ms)"
        )
        pt = self.tableHeader((s1, s2,), len(records))

        overallCount = 0
        overallCountRatio = 0
        overallCountCached = 0
        overallCountUncached = 0
        overallTimeSpent = 0.0

        for methodName, result in sorted(records.items(), key=lambda x: x[0]):
            if isinstance(result, int):
                count, timeSpent = result, 0.0
            else:
                count, timeSpent = result
            overallCount += count
            if methodName.endswith("-hit"):
                overallCountRatio += count
                overallCountCached += count
            if methodName.endswith("-miss") or methodName.endswith("-expired"):
                overallCountRatio += count
                overallCountUncached += count
            overallTimeSpent += timeSpent

            s = " {:<40}{:>15d}{:>15.1f}{:>15.3f} ".format(
                methodName,
                count,
                timeSpent,
                safeDivision(timeSpent, count, 1000.0),
            )
            self.tableRow(s, pt)

        s = " {:<40}{:>15d}{:>15.1f}{:>15.3f} ".format(
            "Total:",
            overallCount,
            overallTimeSpent,
            safeDivision(overallTimeSpent, overallCount, 1000.0)
        )
        s_cached = " {:<40}{:>15d}{:>14.1f}%{:>15s} ".format(
            "Total Cached:",
            overallCountCached,
            safeDivision(overallCountCached, overallCountRatio, 100.0),
            "",
        )
        s_uncached = " {:<40}{:>15d}{:>14.1f}%{:>15s} ".format(
            "Total Uncached:",
            overallCountUncached,
            safeDivision(overallCountUncached, overallCountRatio, 100.0),
            "",
        )
        self.tableFooter((s, s_cached, s_uncached), pt)

        self.window.refresh()


Dashboard.registerWindow(HelpWindow, "h")
Dashboard.registerWindow(SystemWindow, "s")
Dashboard.registerWindow(RequestStatsWindow, "r")
Dashboard.registerWindow(HTTPSlotsWindow, "c")
Dashboard.registerWindow(MethodsWindow, "m")
Dashboard.registerWindow(AssignmentsWindow, "w")
Dashboard.registerWindow(JobsWindow, "j")
Dashboard.registerWindow(DirectoryStatsWindow, "d")

Dashboard.registerWindowSet(SystemWindow, "H")
Dashboard.registerWindowSet(RequestStatsWindow, "H")
Dashboard.registerWindowSet(HTTPSlotsWindow, "H")
Dashboard.registerWindowSet(MethodsWindow, "H")

Dashboard.registerWindowSet(SystemWindow, "J")
Dashboard.registerWindowSet(AssignmentsWindow, "J")
Dashboard.registerWindowSet(JobsWindow, "J")

Dashboard.registerWindowSet(DirectoryStatsWindow, "D")

if __name__ == "__main__":
    main()
