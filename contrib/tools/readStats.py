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
from __future__ import absolute_import

from StringIO import StringIO
import collections
import datetime
import getopt
import json
import socket
import sys
from . import tables
import time
import errno

"""
This tool reads data from the server's statistics socket and prints a summary.
"""


def safeDivision(value, total, factor=1):
    return value * factor / total if total else 0


SummaryValues = collections.namedtuple("SummaryValues", ("requests", "response", "slots", "cpu", "errors"))


def readSock(sockname, useTCP):
    try:
        s = socket.socket(socket.AF_INET if useTCP else socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sockname)
        s.sendall(json.dumps(["stats"]) + "\r\n")
        s.setblocking(0)
        data = ""
        while not data.endswith("\n"):
            try:
                d = s.recv(1024)
            except socket.error as se:
                if se.args[0] != errno.EWOULDBLOCK:
                    raise
                continue
            if d:
                data += d
            else:
                break
        s.close()
        data = json.loads(data)["stats"]
    except socket.error as e:
        data = {"Failed": "Unable to read statistics from server: %s %s" % (sockname, e)}
    data["server"] = sockname
    return data


def printStats(stats, multimode, showMethods, topUsers, showAgents, dumpFile):
    if len(stats) == 1:
        if "Failed" in stats[0]:
            printFailedStats(stats[0]["Failed"])
        else:
            try:
                summary = printStat(stats[0], multimode[0], showMethods, topUsers, showAgents)
            except KeyError as e:
                printFailedStats("Unable to find key '%s' in statistics from server socket" % (e,))
                sys.exit(1)

    else:
        summary = printMultipleStats(stats, multimode, showMethods, topUsers, showAgents)

    return summary


def printStat(stats, index, showMethods, topUsers, showAgents):

    print("- " * 40)
    print("Server: %s" % (stats["server"],))
    print(datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
    print("Service Uptime: %s" % (datetime.timedelta(seconds=(int(time.time() - stats["system"]["start time"]))),))
    if stats["system"]["cpu count"] > 0:
        print("Current CPU: %.1f%% (%d CPUs)" % (
            stats["system"]["cpu use"],
            stats["system"]["cpu count"],
        ))
        print("Current Memory Used: %d bytes (%.1f GB) (%.1f%% of total)" % (
            stats["system"]["memory used"],
            stats["system"]["memory used"] / (1024.0 * 1024 * 1024),
            stats["system"]["memory percent"],
        ))
    else:
        print("Current CPU: Unavailable")
        print("Current Memory Used: Unavailable")
    print()
    summary = printRequestSummary(stats)
    printHistogramSummary(stats[index], index)
    if showMethods:
        printMethodCounts(stats[index])
    if topUsers:
        printUserCounts(stats[index], topUsers)
    if showAgents:
        printAgentCounts(stats[index])

    return summary


def printMultipleStats(stats, multimode, showMethods, topUsers, showAgents):

    labels = serverLabels(stats)

    print("- " * 40)
    print(datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"))

    times = []
    for stat in stats:
        try:
            t = str(datetime.timedelta(seconds=int(time.time() - stat["system"]["start time"])))
        except KeyError:
            t = "-"
        times.append(t)

    cpus = []
    memories = []
    for stat in stats:
        if stat["system"]["cpu count"] > 0:
            cpus.append(stat["system"]["cpu use"])
            memories.append(stat["system"]["memory percent"])
        else:
            cpus.append(-1)
            memories.append(-1)

    summary = printMultiRequestSummary(stats, cpus, memories, times, labels, multimode)
    printMultiHistogramSummary(stats, multimode[0])
    if showMethods:
        printMultiMethodCounts(stats, multimode[0])
    if topUsers:
        printMultiUserCounts(stats, multimode[0], topUsers)
    if showAgents:
        printMultiAgentCounts(stats, multimode[0])

    return summary


def serverLabels(stats):
    servers = [stat["server"] for stat in stats]
    if isinstance(servers[0], tuple):
        hosts = set([item[0] for item in servers])
        ports = set([item[1] for item in servers])
        if len(ports) == 1:
            servers = [item[0] for item in servers]
        elif len(hosts) == 1:
            servers = [":%d" % item[1] for item in servers]
        elif len(hosts) == len(servers):
            servers = [item[0] for item in servers]
        else:
            servers = ["%s:%s" % item for item in servers]

    servers = [item.split(".") for item in servers]
    while True:
        if all([item[-1] == servers[0][-1] for item in servers]):
            servers = [item[:-1] for item in servers]
        else:
            break
    return [".".join(item) for item in servers]


def printFailedStats(message):

    print("- " * 40)
    print(datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
    print(message)
    print("")


def printRequestSummary(stats):
    table = tables.Table()
    table.addHeader(
        ("Period", "Requests", "Av. Requests", "Av. Response", "Av. Response", "Max. Response", "Slot", "CPU", "500's"),
    )
    table.addHeader(
        ("", "", "per second", "(ms)", "no write(ms)", "(ms)", "Average", "Average", ""),
    )
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.2f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )

    for key, seconds in (("current", 60,), ("1m", 60,), ("5m", 5 * 60,), ("1h", 60 * 60,),):

        stat = stats[key]
        table.addRow((
            key,
            stat["requests"],
            safeDivision(float(stat["requests"]), seconds),
            safeDivision(stat["t"], stat["requests"]),
            safeDivision(stat["t"] - stat["t-resp-wr"], stat["requests"]),
            stat["T-MAX"],
            safeDivision(float(stat["slots"]), stat["requests"]),
            safeDivision(stat["cpu"], stat["requests"]),
            stat["500"],
        ))

    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())

    stat = stats["1m"]
    return SummaryValues(
        requests=safeDivision(float(stat["requests"]), seconds),
        response=safeDivision(stat["t"], stat["requests"]),
        slots=safeDivision(float(stat["slots"]), stat["requests"]),
        cpu=safeDivision(stat["cpu"], stat["requests"]),
        errors=stat["500"],
    )


def printMultiRequestSummary(stats, cpus, memories, times, labels, index):

    key, seconds = index

    table = tables.Table()
    table.addHeader(
        ("Server", "Requests", "Av. Requests", "Av. Response", "Av. Response", "Max. Response", "Slot", "CPU", "CPU", "Memory", "500's", "Uptime",),
    )
    table.addHeader(
        (key, "", "per second", "(ms)", "no write(ms)", "(ms)", "Average", "Average", "Current", "Current", "", "",),
    )
    max_column = 5
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.2f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )

    totals = ["Overall:", 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, "", ]
    for ctr, stat in enumerate(stats):

        stat = stat[key]

        col = []
        col.append(labels[ctr])
        col.append(stat["requests"])
        col.append(safeDivision(float(stat["requests"]), seconds))
        col.append(safeDivision(stat["t"], stat["requests"]))
        col.append(safeDivision(stat["t"] - stat["t-resp-wr"], stat["requests"]))
        col.append(stat["T-MAX"])
        col.append(safeDivision(float(stat["slots"]), stat["requests"]))
        col.append(safeDivision(stat["cpu"], stat["requests"]))
        col.append(cpus[ctr])
        col.append(memories[ctr])
        col.append(stat["500"])
        col.append(times[ctr])
        table.addRow(col)
        for item in xrange(1, len(col) - 1):
            if item == max_column:
                totals[item] = max(totals[item], col[item])
            else:
                totals[item] += col[item]

    for item in (3, 4, 6, 7, 8, 9):
        totals[item] /= len(stats)

    table.addFooter(totals)

    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())

    return SummaryValues(
        requests=totals[2],
        response=totals[3],
        slots=totals[6],
        cpu=totals[7],
        errors=totals[10],
    )


def printHistogramSummary(stat, index):

    print("%s average response histogram" % (index,))
    table = tables.Table()
    table.addHeader(
        ("", "<10ms", "10ms<->100ms", "100ms<->1s", "1s<->10s", "10s<->30s", "30s<->60s", ">60s", "Over 1s", "Over 10s"),
    )
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d (%.1f%%)", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )
    for i in ("T", "T-RESP-WR",):
        table.addRow((
            "Overall Response" if i == "T" else "Response Write",
            (stat[i]["<10ms"], safeDivision(stat[i]["<10ms"], stat["requests"], 100.0)),
            (stat[i]["10ms<->100ms"], safeDivision(stat[i]["10ms<->100ms"], stat["requests"], 100.0)),
            (stat[i]["100ms<->1s"], safeDivision(stat[i]["100ms<->1s"], stat["requests"], 100.0)),
            (stat[i]["1s<->10s"], safeDivision(stat[i]["1s<->10s"], stat["requests"], 100.0)),
            (stat[i]["10s<->30s"], safeDivision(stat[i]["10s<->30s"], stat["requests"], 100.0)),
            (stat[i]["30s<->60s"], safeDivision(stat[i]["30s<->60s"], stat["requests"], 100.0)),
            (stat[i][">60s"], safeDivision(stat[i][">60s"], stat["requests"], 100.0)),
            safeDivision(stat[i]["Over 1s"], stat["requests"], 100.0),
            safeDivision(stat[i]["Over 10s"], stat["requests"], 100.0),
        ))
    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())


def printMultiHistogramSummary(stats, index):

    # Totals first
    keys = ("<10ms", "10ms<->100ms", "100ms<->1s", "1s<->10s", "10s<->30s", "30s<->60s", ">60s", "Over 1s", "Over 10s",)
    totals = {
        "T": dict([(k, 0) for k in keys]),
        "T-RESP-WR": dict([(k, 0) for k in keys]),
        "requests": 0,
    }

    for stat in stats:
        for i in ("T", "T-RESP-WR",):
            totals["requests"] += stat[index]["requests"]
            for k in keys:
                totals[i][k] += stat[index][i][k]

    printHistogramSummary(totals, index)


def printMethodCounts(stat):

    print("Method Counts")
    table = tables.Table()
    table.addHeader(
        ("Method", "Count", "%", "Av. Response", "%", "Total Resp. %"),
    )
    table.addHeader(
        ("", "", "", "(ms)", "", ""),
    )
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )

    response_average = {}
    for method in stat["method"].keys():
        response_average[method] = stat["method-t"][method] / stat["method"][method]

    total_count = sum(stat["method"].values())
    total_avresponse = sum(response_average.values())
    total_response = sum(stat["method-t"].values())

    for method in sorted(stat["method"].keys()):
        table.addRow((
            method,
            stat["method"][method],
            safeDivision(stat["method"][method], total_count, 100.0),
            response_average[method],
            safeDivision(response_average[method], total_avresponse, 100.0),
            safeDivision(stat["method-t"][method], total_response, 100.0),
        ))
    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())


def printMultiMethodCounts(stats, index):

    methods = collections.defaultdict(int)
    method_times = collections.defaultdict(float)
    for stat in stats:
        for method in stat[index]["method"]:
            methods[method] += stat[index]["method"][method]
        if "method-t" in stat[index]:
            for method_time in stat[index]["method-t"]:
                method_times[method_time] += stat[index]["method-t"][method_time]

    printMethodCounts({"method": methods, "method-t": method_times})


def printUserCounts(stat, topUsers):

    print("User Counts")
    table = tables.Table()
    table.addHeader(
        ("User", "Total", "Percentage"),
    )
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )

    total = sum(stat["uid"].values())
    for uid, value in sorted(stat["uid"].items(), key=lambda x: x[1], reverse=True)[:topUsers]:
        table.addRow((
            uid,
            value,
            safeDivision(value, total, 100.0),
        ))
    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())


def printMultiUserCounts(stats, index, topUsers):

    uids = collections.defaultdict(int)
    for stat in stats:
        for uid in stat[index]["uid"]:
            uids[uid] += stat[index]["uid"][uid]

    printUserCounts({"uid": uids}, topUsers)


def printAgentCounts(stat):

    print("User-Agent Counts")
    table = tables.Table()
    table.addHeader(
        ("User-Agent", "Total", "Percentage"),
    )
    table.setDefaultColumnFormats(
        (
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        )
    )

    total = sum(stat["user-agent"].values())
    for ua in sorted(stat["user-agent"].keys()):
        table.addRow((
            ua,
            stat["user-agent"][ua],
            safeDivision(stat["user-agent"][ua], total, 100.0),
        ))
    os = StringIO()
    table.printTable(os=os)
    print(os.getvalue())


def printMultiAgentCounts(stats, index):

    uas = collections.defaultdict(int)
    for stat in stats:
        for ua in stat[index]["user-agent"]:
            uas[ua] += stat[index]["user-agent"][ua]

    printAgentCounts({"user-agent": uas})


def alert(summary, alerts):

    alert = []
    for attr, description in (
        ("requests", "Request count",),
        ("response", "Response time",),
        ("slots", "Slot count",),
        ("cpu", "CPU time",),
        ("errors", "Error count",),
    ):
        threshold = getattr(alerts, attr)
        value = getattr(summary, attr)
        if threshold is not None and value > threshold:
            if isinstance(value, float):
                value = "{:.1f}".format(value)
            alert.append("{} threshold exceeded: {}".format(description, value))

    if alert:
        for _ignore in range(10):
            print('\a', end='')
            sys.stdout.flush()
            time.sleep(0.15)
        print("\n".join(alert))


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: readStats [options]
Options:
    -h            Print this help and exit
    -s            Name of local socket to read from
    -t            Delay in seconds between each sample [10 seconds]
    --tcp host:port Use TCP connection with host:port
    --0           Display multiserver current average
    --1           Display multiserver 1 minute average
    --5           Display multiserver 5 minute average (the default)
    --60          Display multiserver 1 hour average
    --methods     Include details about HTTP method usage
    --users N     Include details about top N users
    --agents      Include details about HTTP User-Agent usage
    --dump FILE   File to dump JSON data to

    --alert-requests N     Generate an alert if the overall request
                            count exceeds a threshold [INT]
    --alert-response N     Generate an alert if the overall average
                            response time exceeds a threshold [INT]
    --alert-slots N        Generate an alert if the overall slot
                            count exceeds a threshold [FLOAT]
    --alert-cpu N          Generate an alert if the overall average
                            cpu percent use exceeds a threshold [INT]
    --alert-errors N       Generate an alert if the overall error
                            count exceeds a threshold [INT]

Description:
    This utility will print a summary of statistics read from a
    server continuously with the specified delay.

""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)


if __name__ == '__main__':

    delay = 10
    servers = ("data/Logs/state/caldavd-stats.sock",)
    useTCP = False
    showMethods = False
    topUsers = 0
    showAgents = False
    dumpFile = None
    alerts = SummaryValues(
        requests=None,
        response=None,
        slots=None,
        cpu=None,
        errors=None,
    )

    multimodes = (("current", 60,), ("1m", 60,), ("5m", 5 * 60,), ("1h", 60 * 60,),)
    multimode = multimodes[2]

    options, args = getopt.getopt(sys.argv[1:], "hs:t:", [
        "tcp=", "0", "1", "5", "60", "methods", "users=", "agents", "dump=",
        "alert-requests=", "alert-response=", "alert-slots=", "alert-cpu=", "alert-errors=",
    ])

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-s":
            servers = value.split(",")
        elif option == "-t":
            delay = int(value)
        elif option == "--tcp":
            servers = [(host, int(port),) for host, port in [server.split(":") for server in value.split(",")]]
            useTCP = True
        elif option == "--0":
            multimode = multimodes[0]
        elif option == "--1":
            multimode = multimodes[1]
        elif option == "--5":
            multimode = multimodes[2]
        elif option == "--60":
            multimode = multimodes[3]
        elif option == "--methods":
            showMethods = True
        elif option == "--users":
            topUsers = int(value)
        elif option == "--agents":
            showAgents = True
        elif option == "--dump":
            dumpFile = value
        elif option == "--alert-requests":
            alerts = alerts._replace(requests=int(value))
        elif option == "--alert-response":
            alerts = alerts._replace(response=int(value))
        elif option == "--alert-slots":
            alerts = alerts._replace(slots=float(value))
        elif option == "--alert-cpu":
            alerts = alerts._replace(cpu=int(value))
        elif option == "--alert-errors":
            alerts = alerts._replace(errors=int(value))

    while True:
        now = time.time()
        stats = [readSock(server, useTCP) for server in servers]
        summary = printStats(stats, multimode, showMethods, topUsers, showAgents, dumpFile)
        if dumpFile is not None:
            stats = dict([("{}:{}".format(*servers[ctr]), stat) for ctr, stat in enumerate(stats)])
            stats["timestamp"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with open(dumpFile, "a") as f:
                f.write(json.dumps(stats))
                f.write("\n")
        alert(summary, alerts)
        time.sleep(delay - (time.time() - now))
