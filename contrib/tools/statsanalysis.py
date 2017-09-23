#!/usr/bin/env python
##
# Copyright (c) 2014-2017 Apple Inc. All rights reserved.
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

from json import dumps, loads
from matplotlib.ticker import AutoMinorLocator
import collections
import getopt
import matplotlib.pyplot as plt
import os
import sys

dataset = {}


def safeDivision(value, total, factor=1):
    return value * factor / total if total else 0


def analyze(fpath, title):
    """
    Analyze a readStats data file.

    @param fpath: path of file to analyze
    @type fpath: L{str}
    @param title: title to use for data set
    @type title: L{str}
    """

    print("Analyzing data for %s" % (title,))
    fpath_cached = fpath + ".cached"
    if os.path.exists(fpath_cached):
        with open(fpath_cached) as f:
            d = loads(f.read())
        dataset[title] = dict([(int(k), v) for k, v in d.items()])
    else:
        dataset[title] = {}
        json = False
        with open(fpath) as f:
            line = next(f)
            if line.startswith("- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"):
                analyzeTableFormat(f, title)
            elif line.startswith("{"):
                analyzeJSONFormat(f, line, title)
                json = True

        if not json:
            with open(fpath_cached, "w") as f:
                f.write(dumps(dataset[title]))

    print("Read %d data points\n" % (len(dataset[title]),))


def analyzeTableFormat(f, title):
    """
    Analyze a "table" format output file. First line has already been tested.

    @param f: file object to read
    @type f: L{File}
    @param title: title to use for data set
    @type title: L{str}
    """
    analyzeTableRecord(f, title)
    for line in f:
        if line.startswith("- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"):
            analyzeTableRecord(f, title)


def analyzeTableRecord(liter, title):
    """
    Analyze one entry from the readStats data file.

    @param liter: iterator over lines in the file
    @type liter: L{iter}
    @param title: title to use for data set
    @type title: L{str}
    """

    dt = next(liter)
    time = dt[11:]
    seconds = 60 * (60 * int(time[0:2]) + int(time[3:5])) + int(time[6:8])
    for line in liter:
        if line.startswith("| Overall:"):
            overall = parseOverall(line)
            dataset[title][seconds] = overall
        elif line.startswith("| Method"):
            methods = parseMethods(liter)
            dataset[title][seconds].update(methods)
            break


def parseOverall(line):
    """
    Parse the "Overall:" stats summary line.

    @param line: the line to parse
    @type line: L{str}
    """

    splits = line.split("|")
    overall = {}
    keys = (
        ("Requests", lambda x: int(x)),
        ("Av. Requests per second", lambda x: float(x)),
        ("Av. Response", lambda x: float(x)),
        ("Av. Response no write", lambda x: float(x)),
        ("Max. Response", lambda x: float(x)),
        ("Slot Average", lambda x: float(x)),
        ("CPU Average", lambda x: float(x[:-1])),
        ("CPU Current", lambda x: float(x[:-1])),
        ("Memory Current", lambda x: float(x[:-1])),
        ("500's", lambda x: int(x)),
    )
    for ctr, item in enumerate(keys):
        key, conv = item
        overall["Overall:{}".format(key)] = conv(splits[2 + ctr].strip())

    return overall


def parseMethods(liter):
    """
    Parse the "Method Count" table from a data file entry.

    @param liter: iterator over lines in the file
    @type liter: L{iter}
    """

    while liter.next()[1] != "-":
        pass

    methods = {}
    for line in liter:
        if line[0] == "+":
            break
        splits = line.split("|")
        keys = (
            ("Count", lambda x: int(x)),
            ("Count %", lambda x: float(x[:-1])),
            ("Av. Response", lambda x: float(x)),
            ("Av. Response %", lambda x: float(x[:-1])),
            ("Total Resp. %", lambda x: float(x[:-1])),
        )
        for ctr, item in enumerate(keys):
            key, conv = item
            methods["Method:{}:{}".format(splits[1].strip(), key)] = conv(splits[2 + ctr].strip())

    return methods


def analyzeJSONFormat(f, first, title):
    """
    Analyze a JSON format output file. First line has already been tested.

    @param f: file object to read
    @type f: L{File}
    @param title: title to use for data set
    @type title: L{str}
    """
    analyzeJSONRecord(first, title)
    for line in f:
        if line:
            analyzeJSONRecord(line, title)


def analyzeJSONRecord(line, title):
    """
    Analyze a JSON record.

    @param line: line of JSON data to parse
    @type line: L{str}
    @param title: title to use for data set
    @type title: L{str}
    """

    j = loads(line)

    time = j["timestamp"][11:]
    seconds = 60 * (60 * int(time[0:2]) + int(time[3:5])) + int(time[6:8])
    dataset[title][seconds] = {}

    allstats = [stat for server, stat in j.items() if server != "timestamp"]

    analyzeJSONStatsSummary(allstats, title, seconds)
    analyzeJSONStatsMethods(allstats, title, seconds)


def analyzeJSONStatsSummary(allstats, title, seconds):
    """
    Analyze all server JSON summary stats.

    @param allstats: set of JSON stats to analyze
    @type allstats: L{dict}
    @param title: title to use for data set
    @type title: L{str}
    @param seconds: timestamp for stats record
    @type seconds: L{int}
    """

    results = collections.defaultdict(list)
    for stats in allstats:
        stat = stats["1m"]

        results["Requests"].append(stat["requests"])
        results["Av. Requests per second"].append(safeDivision(float(stat["requests"]), 60.0))
        results["Av. Response"].append(safeDivision(stat["t"], stat["requests"]))
        results["Av. Response no write"].append(safeDivision(stat["t"] - stat["t-resp-wr"], stat["requests"]))
        results["Max. Response"].append(stat["T-MAX"])
        results["Slot Average"].append(safeDivision(float(stat["slots"]), stat["requests"]))
        results["CPU Average"].append(safeDivision(stat["cpu"], stat["requests"]))
        results["CPU Current"].append(stats["system"]["cpu use"])
        results["Memory Current"].append(stats["system"]["memory percent"])
        results["500's"].append(stat["500"])

    for key in ("Requests", "Av. Requests per second", "500's",):
        dataset[title][seconds]["Overall:{}".format(key)] = sum(results[key])

    for key in ("Av. Response", "Av. Response no write", "Slot Average", "CPU Average", "CPU Current", "Memory Current",):
        dataset[title][seconds]["Overall:{}".format(key)] = sum(results[key]) / len(allstats)

    dataset[title][seconds]["Overall:Max. Response"] = max(results["Max. Response"])


def analyzeJSONStatsMethods(allstats, title, seconds):
    """
    Analyze all server JSON method stats.

    @param allstats: set of JSON stats to analyze
    @type allstats: L{dict}
    @param title: title to use for data set
    @type title: L{str}
    @param seconds: timestamp for stats record
    @type seconds: L{int}
    """

    methods = collections.defaultdict(int)
    method_times = collections.defaultdict(float)
    response_average = {}
    for stat in allstats:
        for method in stat["1m"]["method"]:
            methods[method] += stat["1m"]["method"][method]
        if "method-t" in stat["1m"]:
            for method_time in stat["1m"]["method-t"]:
                method_times[method_time] += stat["1m"]["method-t"][method_time]
                response_average[method_time] = method_times[method_time] / methods[method_time]

    total_count = sum(methods.values())
    total_avresponse = sum(response_average.values())
    total_response = sum(method_times.values())

    for method, count in methods.items():
        dataset[title][seconds]["Method:{}:Count".format(method)] = count
        dataset[title][seconds]["Method:{}:Count %".format(method)] = safeDivision(methods[method], total_count, 100.0)
        dataset[title][seconds]["Method:{}:Av. Response".format(method)] = response_average[method]
        dataset[title][seconds]["Method:{}:Av. Response %".format(method)] = safeDivision(response_average[method], total_avresponse, 100.0)
        dataset[title][seconds]["Method:{}:Total Resp. %".format(method)] = safeDivision(method_times[method], total_response, 100.0)


def plotSeries(key, ymin=None, ymax=None):
    """
    Plot the chosen dataset key for each scanned data file.

    @param key: data set key to use
    @type key: L{str}
    @param ymin: minimum value for y-axis or L{None} for default
    @type ymin: L{int} or L{float}
    @param ymax: maximum value for y-axis or L{None} for default
    @type ymax: L{int} or L{float}
    """

    titles = []
    for title, data in sorted(dataset.items(), key=lambda x: x[0]):
        titles.append(title)
        x, y = zip(*[(k / 3600.0, v[key]) for k, v in sorted(data.items(), key=lambda x: x[0]) if key in v])

        plt.plot(x, y)

    plt.xlabel("Hours")
    plt.ylabel(key)
    plt.xlim(0, 24)
    if ymin is not None:
        plt.ylim(ymin=ymin)
    if ymax is not None:
        plt.ylim(ymax=ymax)
    plt.xticks(
        (1, 4, 7, 10, 13, 16, 19, 22,),
        (18, 21, 0, 3, 6, 9, 12, 15,),
    )
    plt.minorticks_on()
    plt.gca().xaxis.set_minor_locator(AutoMinorLocator(n=3))
    plt.grid(True, "major", "x", alpha=0.5, linewidth=0.5)
    plt.grid(True, "minor", "x", alpha=0.5, linewidth=0.5)
    plt.legend(titles, 'upper left', shadow=True, fancybox=True)
    plt.show()


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: statsanalysis [options]
Options:
    -h             Print this help and exit
    -s             Directory to scan for data [~/stats]

Arguments:

Description:
This utility will analyze the output of the readStats tool and
generate some pretty plots of data.
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == "__main__":

    scanDir = os.path.expanduser("~/stats")
    options, args = getopt.getopt(sys.argv[1:], "hs:", [])

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-s":
            scanDir = os.path.expanduser(value)
        else:
            usage("Unrecognized option: %s" % (option,))

    if scanDir and len(args) != 0:
        usage("No arguments allowed when scanning a directory")

    pwd = os.getcwd()

    # Scan data directory and read in data from each file found
    fnames = os.listdir(scanDir)
    count = 1
    for name in fnames:
        if name.startswith("stats_all.log") and not name.endswith(".cached"):
            print("Found file: %s" % (os.path.join(scanDir, name),))
            trailer = name[len("stats_all.log"):]
            if trailer.startswith("-"):
                trailer = trailer[1:]
            initialDate = None
            analyze(os.path.join(scanDir, name), trailer)
            count += 1

    # Build the set of data set keys that can be plotted and let the user
    # choose which one
    keys = set()
    for data in dataset.values():
        for items in data.values():
            keys.update([":".join(k.split(":")[:2]) for k in items.keys()])
    keys = sorted(list(keys))
    while True:
        print("Select a key:")
        print("\n".join(["{:2d}. {}".format(ctr + 1, key) for ctr, key in enumerate(keys)]))
        print(" Q. Quit\n")
        cmd = raw_input("Key: ")
        if cmd.lower() == 'q':
            print("\nQuitting")
            break
        try:
            position = int(cmd) - 1
        except ValueError:
            print("Invalid key. Try again.\n")
            continue

        if keys[position].startswith("Method:"):
            key = keys[position]
            methodkeys = ("Count", "Count %", "Av. Response", "Av. Response %", "Total Resp. %")
            while True:
                print("Select a sub-key:")
                print("\n".join(["{:2d}. {}".format(ctr + 1, subkey) for ctr, subkey in enumerate(methodkeys)]))
                print(" B. Back\n")
                cmd = raw_input("SubKey: ")
                if cmd.lower() == 'b':
                    key = None
                    break
                try:
                    position = int(cmd) - 1
                except ValueError:
                    print("Invalid subkey. Try again.\n")
                    continue
                break
            key = "{}:{}".format(key, methodkeys[position]) if key is not None else None
        else:
            key = keys[position]

        if key:
            plotSeries(key, ymin=0, ymax=None)
