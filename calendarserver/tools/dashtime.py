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
Tool that extracts time series data from a dashcollect log.
"""
from __future__ import print_function

from argparse import SUPPRESS, OPTIONAL, ZERO_OR_MORE, HelpFormatter, \
    ArgumentParser
from bz2 import BZ2File
from collections import OrderedDict, defaultdict
from zlib import decompress
import json
import matplotlib.pyplot as plt
import operator
import os
import sys


verbose = False


def _verbose(log):
    if verbose:
        print(log)


def safeDivision(value, total, factor=1):
    return value * factor / total if total else 0


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


class DataType(object):
    """
    Base class for object that can process the different types of data in a
    dashcollect log.
    """

    allTypes = OrderedDict()
    key = ""

    # This indicates whether the class of data is based on a 1 minute average -
    # which means the data represents a 60 second delay compared to the "real-
    # time" value. If it is the average then setting this flag will cause the
    # first 60 data items to be skipped.
    skip60 = False

    @staticmethod
    def getTitle(measurement):
        if "-" in measurement:
            measurement, item = measurement.split("-", 1)
        else:
            item = ""
        return DataType.allTypes[measurement].title(item)

    @staticmethod
    def getMaxY(measurement, numHosts):
        if "-" in measurement:
            measurement = measurement.split("-", 1)[0]
        return DataType.allTypes[measurement].maxY(numHosts)

    @staticmethod
    def skip(measurement):
        if "-" in measurement:
            measurement = measurement.split("-", 1)[0]
        return DataType.allTypes[measurement].skip60

    @staticmethod
    def process(measurement, stats, host):
        if "-" in measurement:
            measurement, item = measurement.split("-", 1)
        else:
            item = ""
        return DataType.allTypes[measurement].calculate(stats, item, host)

    @staticmethod
    def title(item):
        raise NotImplementedError

    @staticmethod
    def maxY(numHosts):
        raise NotImplementedError

    @staticmethod
    def calculate(stats, item, hosts):
        """
        If hosts is L{None} then data from all hosts will be aggregated.

        @param stats: per-Pod L{dict} of data from each host in the pod.
        @type stats: L{dict}
        @param item: additional L{dict} key for data of interest
        @type item: L{str}
        @param hosts: list of hosts to process
        @type hosts: L{list}
        """
        raise NotImplementedError


class CPUDataType(DataType):
    """
    CPU use.
    """

    key = "cpu"

    @staticmethod
    def title(item):
        return "CPU Use %"

    @staticmethod
    def maxY(numHosts):
        return 100 * numHosts

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats_system"]["cpu use"] if stats[onehost] else 0 for onehost in hosts])


class MemoryDataType(DataType):
    """
    CPU use.
    """

    key = "mem"

    @staticmethod
    def title(item):
        return "Memory Use %"

    @staticmethod
    def maxY(numHosts):
        return 100 * numHosts

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats_system"]["memory percent"] if stats[onehost] else 0 for onehost in hosts])


class RequestsDataType(DataType):
    """
    Number of requests.
    """

    key = "reqs"
    skip60 = True

    @staticmethod
    def title(item):
        return "Requests/sec"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats"]["1m"]["requests"] if stats[onehost] else 0 for onehost in hosts]) / 60.0


class ResponseDataType(DataType):
    """
    Average response time.
    """

    key = "respt"
    skip60 = True

    @staticmethod
    def title(item):
        return "Av. Response Time (ms)"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        tsum = sum([stats[onehost]["stats"]["1m"]["t"] if stats[onehost] else 0 for onehost in hosts])
        rsum = sum([stats[onehost]["stats"]["1m"]["requests"] if stats[onehost] else 0 for onehost in hosts])
        return safeDivision(tsum, rsum)


class Code500DataType(DataType):
    """
    Number of 500 requests.
    """

    key = "500"
    skip60 = True

    @staticmethod
    def title(item):
        return "Code 500/sec"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats"]["1m"]["500"] if stats[onehost] else 0 for onehost in hosts]) / 60.0


class Code401DataType(DataType):
    """
    Number of 401 requests.
    """

    key = "401"
    skip60 = True

    @staticmethod
    def title(item):
        return "Code 401/sec"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats"]["1m"]["401"] if stats[onehost] else 0 for onehost in hosts]) / 60.0


class MaxSlotsDataType(DataType):
    """
    Max slots.
    """

    key = "slots"
    skip60 = True

    @staticmethod
    def title(item):
        return "Max. Slots"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats"]["1m"]["max-slots"] if stats[onehost] else 0 for onehost in hosts])


class JobsCompletedDataType(DataType):
    """
    Job completion count from job assignments.
    """

    key = "jcomp"

    lastCompleted = defaultdict(int)

    @staticmethod
    def title(item):
        return "Completed"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        result = 0
        for onehost in hosts:
            completed = sum(map(operator.itemgetter(2), stats[onehost]["job_assignments"]["workers"])) if stats[onehost] else 0
            delta = completed - JobsCompletedDataType.lastCompleted[onehost] if JobsCompletedDataType.lastCompleted[onehost] else 0
            if delta >= 0:
                result += delta
            JobsCompletedDataType.lastCompleted[onehost] = completed
        return result


class MethodCountDataType(DataType):
    """
    Count of specified methods. L{item} should be set to the full name of the
    "decorated" method seen in dashview.
    """

    key = "methodc"
    skip60 = True

    @staticmethod
    def title(item):
        return item

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        return sum([stats[onehost]["stats"]["1m"]["method"].get(item, 0) if stats[onehost] else 0 for onehost in hosts])


class MethodResponseDataType(DataType):
    """
    Average response time of specified methods. L{item} should be set to the
    full name of the "decorated" method seen in dashview.
    """

    key = "methodr"
    skip60 = True

    @staticmethod
    def title(item):
        return item

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        tsum = sum([stats[onehost]["stats"]["1m"]["method-t"].get(item, 0) if stats[onehost] else 0 for onehost in hosts])
        rsum = sum([stats[onehost]["stats"]["1m"]["method"].get(item, 0) if stats[onehost] else 0 for onehost in hosts])
        return safeDivision(tsum, rsum)


class JobQueueDataType(DataType):
    """
    Count of queued job items. L{item} should be set to the full name or prefix
    of job types to process. Or if set to L{None}, all jobs are counted.
    """

    key = "jqueue"

    @staticmethod
    def title(item):
        return ("JQ " + "_".join(map(operator.itemgetter(0), item.split("_")))) if item else "Jobs Queued"

    @staticmethod
    def maxY(numHosts):
        return None

    @staticmethod
    def calculate(stats, item, hosts):
        # Job queue stat only read for first host
        onehost = sorted(stats.keys())[0]
        if len(stats[onehost]) == 0:
            return 0
        if item:
            return sum(map(operator.itemgetter("queued"), {k: v for k, v in stats[onehost]["jobs"].items() if k.startswith(item)}.values()))
        else:
            return sum(map(operator.itemgetter("queued"), stats[onehost]["jobs"].values()))


# Register the known L{DataType}s
for dtype in DataType.__subclasses__():
    DataType.allTypes[dtype.key] = dtype


class Calculator(object):

    def __init__(self, args):
        if args.v:
            global verbose
            verbose = True

        # Get the log file
        self.logname = os.path.expanduser(args.l)
        try:
            if self.logname.endswith(".bz2"):
                self.logfile = BZ2File(self.logname)
            else:
                self.logfile = open(self.logname)
        except:
            print("Failed to open logfile {}".format(args.l))
            sys.exit(1)

        self.pod = getattr(args, "p", None)
        self.single_server = getattr(args, "s", None)

        self.save = args.save
        self.savedir = getattr(args, "i", None)
        self.noshow = args.noshow

        self.mode = args.mode

        # Start/end lines in log file to process
        self.line_start = args.start
        self.line_count = args.count

        # Plot arrays that will be generated
        self.x = []
        self.y = OrderedDict()
        self.titles = {}
        self.ymaxes = {}

    def singleHost(self, valuekeys):
        """
        Generate data for a single host only.

        @param valuekeys: L{DataType} keys to process
        @type valuekeys: L{list} or L{str}
        """
        self._plotHosts(valuekeys, (self.single_server,))

    def combinedHosts(self, valuekeys):
        """
        Generate data for all hosts.

        @param valuekeys: L{DataType} keys to process
        @type valuekeys: L{list} or L{str}
        """
        self._plotHosts(valuekeys, None)

    def _plotHosts(self, valuekeys, hosts):
        """
        Generate data for a the specified list of hosts.

        @param valuekeys: L{DataType} keys to process
        @type valuekeys: L{list} or L{str}
        @param hosts: lists of hosts to process
        @type hosts: L{list} or L{str}
        """

        # For each log file line, process the data for each required measurement
        with self.logfile:
            line = self.logfile.readline()
            ctr = 0
            while line:
                if ctr < self.line_start:
                    ctr += 1
                    line = self.logfile.readline()
                    continue

                if line[0] == "\x1e":
                    line = line[1:]
                if line[0] != "{":
                    line = decompress(line.decode("base64"))
                jline = json.loads(line)

                timestamp = jline["timestamp"]
                self.x.append(int(timestamp[14:16]) * 60 + int(timestamp[17:19]))
                ctr += 1

                # Initialize the plot arrays when we know how many hosts there are
                if len(self.y) == 0:
                    if self.pod is None:
                        self.pod = sorted(jline["pods"].keys())[0]
                    if hosts is None:
                        hosts = sorted(jline["pods"][self.pod].keys())
                    for measurement in valuekeys:
                        self.y[measurement] = []
                        self.titles[measurement] = DataType.getTitle(measurement)
                        self.ymaxes[measurement] = DataType.getMaxY(measurement, len(hosts))

                for measurement in valuekeys:
                    stats = jline["pods"][self.pod]
                    try:
                        self.y[measurement].append(DataType.process(measurement, stats, hosts))
                    except KeyError:
                        self.y[measurement].append(None)

                line = self.logfile.readline()
                if self.line_count != -1 and ctr > self.line_start + self.line_count:
                    break

        # Offset data that is averaged over the previous minute
        for measurement in valuekeys:
            if DataType.skip(measurement):
                self.y[measurement] = self.y[measurement][60:]
                self.y[measurement].extend([None] * 60)

    def perHost(self, perhostkeys, combinedkeys):
        """
        Generate a set of per-host plots, together we a set of plots for all-
        host data.

        @param perhostkeys: L{DataType} keys for per-host data to process
        @type perhostkeys: L{list} or L{str}
        @param combinedkeys: L{DataType} keys for all-host data to process
        @type combinedkeys: L{list} or L{str}
        """

        # For each log file line, process the data for each required measurement
        with self.logfile:
            line = self.logfile.readline()
            ctr = 0
            while line:
                if ctr < self.line_start:
                    ctr += 1
                    line = self.logfile.readline()
                    continue

                if line[0] == "\x1e":
                    line = line[1:]
                if line[0] != "{":
                    line = decompress(line.decode("base64"))
                jline = json.loads(line)

                timestamp = jline["timestamp"]
                self.x.append(int(timestamp[14:16]) * 60 + int(timestamp[17:19]))
                ctr += 1

                # Initialize the plot arrays when we know how many hosts there are
                if len(self.y) == 0:
                    if self.pod is None:
                        self.pod = sorted(jline["pods"].keys())[0]
                    hosts = sorted(jline["pods"][self.pod].keys())

                    for host in hosts:
                        for measurement in perhostkeys:
                            ykey = "{}={}".format(measurement, host)
                            self.y[ykey] = []
                            self.titles[ykey] = DataType.getTitle(measurement)
                            self.ymaxes[ykey] = DataType.getMaxY(measurement, 1)

                    for measurement in combinedkeys:
                        self.y[measurement] = []
                        self.titles[measurement] = DataType.getTitle(measurement)
                        self.ymaxes[measurement] = DataType.getMaxY(measurement, len(hosts))

                # Get actual measurement data
                for host in hosts:
                    for measurement in perhostkeys:
                        ykey = "{}={}".format(measurement, host)
                        stats = jline["pods"][self.pod]
                        self.y[ykey].append(DataType.process(measurement, stats, (host,)))

                for measurement in combinedkeys:
                    stats = jline["pods"][self.pod]
                    self.y[measurement].append(DataType.process(measurement, stats, hosts))

                line = self.logfile.readline()
                if self.line_count != -1 and ctr > self.line_start + self.line_count:
                    break

        # Offset data that is averaged over the previous minute. Also determine
        # the highest max value of all the per-host measurements and scale each
        # per-host plot to the same range.
        overall_ymax = defaultdict(int)
        for host in hosts:
            for measurement in perhostkeys:
                ykey = "{}={}".format(measurement, host)
                overall_ymax[measurement] = max(overall_ymax[measurement], max(self.y[ykey]))
                if DataType.skip(measurement):
                    self.y[ykey] = self.y[ykey][60:]
                    self.y[ykey].extend([None] * 60)
        for host in hosts:
            for measurement in perhostkeys:
                ykey = "{}={}".format(measurement, host)
                self.ymaxes[ykey] = overall_ymax[measurement]

        for measurement in combinedkeys:
            if DataType.skip(measurement):
                self.y[measurement] = self.y[measurement][60:]
                self.y[measurement].extend([None] * 60)

    def run(self, mode, *args):
        getattr(self, mode)(*args)

    def plot(self):
        # Generate a single stacked plot of the data
        if self.mode.startswith("scatter"):
            plt.figure(figsize=(10, 10))
            keys = self.y.keys()
            x_key = CPUDataType.key
            keys.remove(x_key)
            plotmax = len(keys)
            for plotnum, y_key in enumerate(keys):
                plt.subplot(len(keys), 1, plotnum + 1)
                plotScatter(
                    self.titles[y_key],
                    self.y[x_key], self.y[y_key],
                    (0, self.ymaxes[x_key],),
                    (0, self.ymaxes[y_key]),
                    plotnum == plotmax - 1,
                )
        else:
            plt.figure(figsize=(18.5, min(5 + len(self.y.keys()), 18)))
            plotmax = len(self.y.keys())
            for plotnum, measurement in enumerate(self.y.keys()):
                plt.subplot(len(self.y), 1, plotnum + 1)
                plotSeries(self.titles[measurement], self.x, self.y[measurement], 0, self.ymaxes[measurement], plotnum == plotmax - 1)
        if self.save:
            if self.savedir:
                dirpath = self.savedir
                if not os.path.exists(dirpath):
                    os.makedirs(dirpath)
            else:
                dirpath = os.path.dirname(self.logname)
            fname = ".".join((os.path.basename(self.logname), self.mode, "png"),)
            plt.savefig(os.path.join(dirpath, fname), orientation="landscape", format="png")
        if not self.noshow:
            plt.show()


def main():

    selectMode = {
        "basic":
            # Generic aggregated data for all hosts
            (
                "combinedHosts",
                (
                    CPUDataType.key,
                    RequestsDataType.key,
                    ResponseDataType.key,
                    JobsCompletedDataType.key,
                    JobQueueDataType.key,
                )
            ),
        "basicjob":
            # Data aggregated for all hosts - job detail
            (
                "combinedHosts",
                (
                    CPUDataType.key,
                    RequestsDataType.key,
                    ResponseDataType.key,
                    JobsCompletedDataType.key,
                    JobQueueDataType.key + "-SCHEDULE",
                    JobQueueDataType.key + "-PUSH",
                    JobQueueDataType.key,
                ),
            ),
        "basicschedule":
            # Data aggregated for all hosts - job detail
            (
                "combinedHosts",
                (
                    CPUDataType.key,
                    JobsCompletedDataType.key,
                    JobQueueDataType.key + "-SCHEDULE_ORGANIZER_WORK",
                    JobQueueDataType.key + "-SCHEDULE_ORGANIZER_SEND_WORK",
                    JobQueueDataType.key + "-SCHEDULE_REPLY_WORK",
                    JobQueueDataType.key + "-SCHEDULE_AUTO_REPLY_WORK",
                    JobQueueDataType.key + "-SCHEDULE_REFRESH_WORK",
                    JobQueueDataType.key + "-PUSH",
                    JobQueueDataType.key,
                ),
            ),
        "basicmethod":
            # Data aggregated for all hosts - method detail
            (
                "combinedHosts",
                (
                    CPUDataType.key,
                    RequestsDataType.key,
                    ResponseDataType.key,
                    MethodCountDataType.key + "-PUT ics",
                    MethodCountDataType.key + "-REPORT cal-home-sync",
                    MethodCountDataType.key + "-PROPFIND Calendar Home",
                    MethodCountDataType.key + "-REPORT cal-sync",
                    MethodCountDataType.key + "-PROPFIND Calendar",
                ),
            ),

        "hostrequests":
            # Per-host requests, and total requests & CPU
            (
                "perHost",
                (RequestsDataType.key,),
                (
                    RequestsDataType.key,
                    CPUDataType.key,
                ),
            ),
        "hostcpu":
            # Per-host CPU, and total CPU
            (
                "perHost",
                (CPUDataType.key,),
                (
                    RequestsDataType.key,
                    CPUDataType.key,
                ),
            ),
        "hostcompleted":
            # Per-host job completion, and total CPU, total jobs queued
            (
                "perHost",
                (JobsCompletedDataType.key,),
                (
                    CPUDataType.key,
                    JobQueueDataType.key,
                ),
            ),
        "scatter":
            # Scatter plots of request count and response time vs CPU
            (
                "combinedHosts",
                (
                    CPUDataType.key,
                    RequestsDataType.key,
                    ResponseDataType.key,
                )
            ),
    }

    parser = ArgumentParser(
        formatter_class=MyHelpFormatter,
        description="Dashboard time series processor.",
        epilog="""Available modes:

basic - stacked plots of total CPU, total request count, total average response
    time, completed jobs, and job queue size.

basicjob - as per basic but with queued SCHEDULE_*_WORK and queued
    PUSH_NOTIFICATION_WORK plots.

basicschedule - stacked plots of total CPU, completed jobs, each queued
    SCHEDULE_*_WORK, queued, PUSH_NOTIFICATION_WORK, and overall job queue size.

basicmethod - stacked plots of total CPU, total request count, total average
    response time, PUT-ics, REPORT cal-home-sync, PROPFIND Calendar Home, REPORT
    cal-sync, and PROPFIND Calendar.

hostrequests - stacked plots of per-host request counts, total request count,
    and total CPU.

hostcpu - stacked plots of per-host CPU, total request count, and total CPU.

hostcompleted - stacked plots of per-host completed jobs, total CPU, and job
    queue size.

scatter - scatter plot of request count and response time vs CPU.
""",
    )
    parser.add_argument("-l", default=SUPPRESS, required=True, help="Log file to process")
    parser.add_argument("-i", default=SUPPRESS, help="Directory to store image (default: log file directory)")
    parser.add_argument("-p", default=SUPPRESS, help="Name of pod to analyze")
    parser.add_argument("-s", default=SUPPRESS, help="Name of server to analyze")
    parser.add_argument("--save", action="store_true", help="Save plot PNG image")
    parser.add_argument("--noshow", action="store_true", help="Don't show the plot on screen")
    parser.add_argument("--start", type=int, default=0, help="Log line to start from")
    parser.add_argument("--count", type=int, default=-1, help="Number of log lines to process from start")
    parser.add_argument("--mode", default="basic", choices=sorted(selectMode.keys()), help="Type of plot to produce")
    parser.add_argument("-v", action="store_true", help="Verbose")
    args = parser.parse_args()

    calculator = Calculator(args)
    calculator.run(*selectMode[args.mode])
    calculator.plot()


def plotSeries(title, x, y, ymin=None, ymax=None, last_subplot=True):
    """
    Plot the chosen dataset key for each scanned data file.

    @param key: data set key to use
    @type key: L{str}
    @param ymin: minimum value for y-axis or L{None} for default
    @type ymin: L{int} or L{float}
    @param ymax: maximum value for y-axis or L{None} for default
    @type ymax: L{int} or L{float}
    """

    plt.plot(x, y)

    if ymin is not None:
        plt.ylim(ymin=ymin)
    if ymax is not None:
        plt.ylim(ymax=ymax)
    plt.xlim(0, 3600)

    frame = plt.gca()
    if last_subplot:
        plt.xlabel("Time (minutes)")
    plt.ylabel(title, fontsize="small", horizontalalignment="right", rotation="horizontal")

    # Setup axes - want 0 - 60 minute scale for x-axis
    plt.tick_params(labelsize="small")
    plt.xticks(range(0, 3601, 300), range(0, 61, 5) if last_subplot else [])
    frame.set_xticks(range(0, 3601, 60), minor=True)
    plt.grid(True, "minor", "x", alpha=0.5, linewidth=0.5)


def plotScatter(title, x, y, xlim=None, ylim=None, last_subplot=True):
    """
    Plot the chosen dataset key for each scanned data file.

    @param key: data set key to use
    @type key: L{str}
    @param xlim: minimum, maximum value for x-axis or L{None} for default
    @type xlim: L{tuple} of two L{int} or L{float}
    @param ylim: minimum, maximum value for y-axis or L{None} for default
    @type ylim: L{tuple} of two L{int} or L{float}
    """

    # Remove any None values
    x, y = zip(*filter(lambda x: x[0] is not None and x[1] is not None, zip(x, y)))

    plt.scatter(x, y, marker=".")

    plt.xlabel("CPU")
    plt.ylabel(title, fontsize="small", verticalalignment="center", rotation="vertical")
    if xlim is not None:
        plt.xlim(*xlim)
    if ylim is not None:
        plt.ylim(*ylim)

if __name__ == "__main__":
    main()
