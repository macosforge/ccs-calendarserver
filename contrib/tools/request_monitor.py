#!/usr/bin/env python
##
# Copyright (c) 2009-2017 Apple Inc. All rights reserved.
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

from dateutil.parser import parse as dateparse
from subprocess import Popen, PIPE, STDOUT
import datetime
import getopt
import os
import sys
import time
import traceback
import collections
from . import tables
from cStringIO import StringIO

# Detect which OS this is being run on
child = Popen(
    args=[
        "uname",
    ],
    stdout=PIPE, stderr=STDOUT,
)
output, _ignore_error = child.communicate()
output = output.strip()
if output == "Darwin":
    OS = "OS X"
elif output == "Linux":
    OS = "Linux"
else:
    print("Unknown OS: %s" % (output,))
    sys.exit(1)

# Some system commands we need to detect
if OS == "OS X":
    NETSTAT = "/usr/sbin/netstat"
    enableListenQueue = os.path.exists(NETSTAT)
elif OS == "Linux":
    enableListenQueue = False

if OS == "OS X":
    IOSTAT = "/usr/sbin/iostat"
    enableCpuIdle = os.path.exists(IOSTAT)
elif OS == "Linux":
    IOSTAT = "/usr/bin/iostat"
    enableCpuIdle = os.path.exists(IOSTAT)

if OS == "OS X":
    VMSTAT = "/usr/bin/vm_stat"
    enableFreeMem = os.path.exists(VMSTAT)
elif OS == "Linux":
    VMSTAT = "/usr/bin/vmstat"
    enableFreeMem = os.path.exists(VMSTAT)

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)


filenames = ["/var/log/caldavd/access.log", ]
debug = False


def listenq():
    child = Popen(
        args=[
            NETSTAT, "-L", "-anp", "tcp",
        ],
        stdout=PIPE, stderr=STDOUT,
    )
    output, _ignore_error = child.communicate()
    ssl = nonssl = 0
    for line in output.split("\n"):
        if line.find("8443") != -1:
            ssl = int(line.split("/")[0])
        elif line.find("8008") != -1:
            nonssl = int(line.split("/")[0])
    return "%s+%s" % (ssl, nonssl), ssl, nonssl

_listenQueueHistory = []


def listenQueueHistory():
    global _listenQueueHistory
    latest, ssl, nonssl = listenq()
    _listenQueueHistory.insert(0, latest)
    del _listenQueueHistory[12:]
    return _listenQueueHistory, ssl, nonssl


_idleHistory = []


def idleHistory():
    global _idleHistory
    latest = cpuidle()
    _idleHistory.insert(0, latest)
    del _idleHistory[12:]
    return _idleHistory


def tail(filenames, n):
    results = collections.defaultdict(list)
    for filename in filenames:
        child = Popen(
            args=[
                "/usr/bin/tail", "-%d" % (n,), filename,
            ],
            stdout=PIPE, stderr=STDOUT,
        )
        output, _ignore_error = child.communicate()
        results[filename].extend(output.splitlines())
    return results


def range(filenames, start, end):
    results = collections.defaultdict(list)
    for filename in filenames:
        with open(filename) as f:
            for count, line in enumerate(f):
                if count >= start:
                    results[filename].append(line)
                if count > end:
                    break
    return results


def cpuPerDaemon():
    a = {}
    child = Popen(
        args=[
            "ps", "auxw",
        ],
        stdout=PIPE, stderr=STDOUT,
    )
    output, _ignore_ = child.communicate()
    for l in output.split("\n"):
        if "ProcessType=" in l:
            f = l.split()
            for l in f:
                if l.startswith("LogID="):
                    logID = int(l[6:])
                    break
            else:
                logID = None
            if logID is not None:
                a[logID] = f[2]
    return ", ".join([v for _ignore_k, v in sorted(a.items(), key=lambda i:i[0])])


def cpuidle():
    if OS == "OS X":
        child = Popen(
            args=[
                IOSTAT, "-c", "2", "-n", "0",
            ],
            stdout=PIPE, stderr=STDOUT,
        )
        output, _ignore_ = child.communicate()
        return output.splitlines()[-1].split()[2]
    elif OS == "Linux":
        child = Popen(
            args=[
                IOSTAT, "-c", "1", "2"
            ],
            stdout=PIPE, stderr=STDOUT,
        )
        output, _ignore_ = child.communicate()
        return output.splitlines()[-2].split()[5]


def freemem():
    try:
        if OS == "OS X":
            child = Popen(
                args=[
                    VMSTAT,
                ],
                stdout=PIPE, stderr=STDOUT,
            )
            output, _ignore_ = child.communicate()
            lines = output.split("\n")

            line = lines[0]
            pageSize = int(line[line.find("page size of") + 12:].split()[0])
            line = lines[1]
            freeSize = int(line[line.find("Pages free:") + 11:].split()[0][:-1])
            freed = freeSize * pageSize
            return "%d bytes (%.1f GB)" % (freed, freed / (1024.0 * 1024 * 1024),)
        elif OS == "Linux":
            child = Popen(
                args=[
                    VMSTAT, "-s", "-S", "K"
                ],
                stdout=PIPE, stderr=STDOUT,
            )
            output, _ignore_ = child.communicate()
            lines = output.splitlines()

            line = lines[4]
            freed = int(line.split()[0]) * 1024
            return "%d bytes (%.1f GB)" % (freed, freed / (1024.0 * 1024 * 1024),)
    except Exception as e:
        if debug:
            print("freemem failure", e)
            print(traceback.print_exc())
        return "error"


def parseLine(line):

    startPos = line.find("- ")
    endPos = line.find(" [")
    userId = line[startPos + 2:endPos]

    startPos = endPos + 2
    endPos = line.find(']', startPos)
    logTime = line[startPos:endPos]

    startPos = endPos + 3
    endPos = line.find(' ', startPos)
    if line[startPos] == '?':
        method = "???"
        uri = ""
        startPos += 5
    else:
        method = line[startPos:endPos]

        startPos = endPos + 1
        endPos = line.find(" HTTP/", startPos)
        uri = line[startPos:endPos]
        startPos = endPos + 11

    status = int(line[startPos:startPos + 3])

    startPos += 4
    endPos = line.find(' ', startPos)
    bytes = int(line[startPos:endPos])

    startPos = endPos + 2
    endPos = line.find('"', startPos)
    referer = line[startPos:endPos]

    startPos = endPos + 3
    endPos = line.find('"', startPos)
    client = line[startPos:endPos]

    startPos = endPos + 2
    if line[startPos] == '[':
        extended = {}

        startPos += 1
        endPos = line.find(' ', startPos)
        extended["t"] = float(line[startPos:endPos])

        startPos = endPos + 6
        endPos = line.find(' ', startPos)
        extended["i"] = int(line[startPos:endPos])

        startPos = endPos + 1
        endPos = line.find(' ', startPos)
        extended["or"] = int(line[startPos:endPos])
    else:

        items = line[startPos:].split()
        extended = dict([item.split('=') for item in items if item.find("=") != -1])

    return userId, logTime, method, uri, status, bytes, referer, client, extended


def safePercent(value, total):

    return value * 100.0 / total if total else 0.0


def usage():
    print("request_monitor [OPTIONS] [FILENAME]")
    print("")
    print("FILENAME   optional path of access log to monitor [/var/log/caldavd/access.log]")
    print("")
    print("OPTIONS")
    print("-h         print help and exit")
    print("--debug    print tracebacks and error details")
    print("--lines N  specifies how many lines to tail from access.log (default: 10000)")
    print("--range M:N  specifies a range of lines to analyze from access.log (default: all)")
    print("--procs N  specifies how many python processes are expected in the log file (default: 80)")
    print("--top N    how many long requests to print (default: 10)")
    print("--users N  how many top users to print (default: 5)")
    print("")
    print("Version: 5")

numLines = 10000
numProcs = 80
numTop = 10
numUsers = 5
lineRange = None
options, args = getopt.getopt(sys.argv[1:], "h", ["debug", "lines=", "range=", "procs=", "top=", "users="])
for option, value in options:
    if option == "-h":
        usage()
        sys.exit(0)
    elif option == "--debug":
        debug = True
    elif option == "--lines":
        numLines = int(value)
    elif option == "--range":
        lineRange = (int(value.split(":")[0]), int(value.split(":")[1]))
    elif option == "--procs":
        numProcs = int(value)
    elif option == "--top":
        numTop = int(value)
    elif option == "--users":
        numUsers = int(value)

if len(args):
    filenames = sorted([os.path.expanduser(arg) for arg in args])
    prefix = len(os.path.commonprefix(filenames))

for filename in filenames:
    if not os.path.isfile(filename):
        print("Path %s does not exist" % (filename,))
        print("")
        usage()
        sys.exit(1)

for filename in filenames:
    if not os.access(filename, os.R_OK):
        print("Path %s does not exist" % (filename,))
        print("")
        usage()
        sys.exit(1)

if debug:
    print("Starting: access log files: %s" % (", ".join(filenames),))
    print("")

while True:

    currentSec = None
    currentCount = 0
    times = []
    perfile_times = collections.defaultdict(list)
    ids = {}
    rawCounts = {}
    timesSpent = {}
    numRequests = collections.defaultdict(int)
    numServerToServer = collections.defaultdict(int)
    numProxied = collections.defaultdict(int)
    slotCount = collections.defaultdict(int)
    totalRespTime = collections.defaultdict(float)
    totalRespWithoutWRTime = collections.defaultdict(float)
    maxRespTime = collections.defaultdict(float)
    under10ms = [0, 0]
    over10ms = [0, 0]
    over100ms = [0, 0]
    over1s = [0, 0]
    over10s = [0, 0]
    over30s = [0, 0]
    over60s = [0, 0]
    totalOver1s = [0, 0]
    totalOver10s = [0, 0]
    requests = []
    users = {}
    startTime = None
    endTime = None
    errorCount = 0
    parseErrors = 0

    try:
        lines = tail(filenames, numLines) if lineRange is None else range(filenames, *lineRange)
        for filename in lines.keys():
            for line in lines[filename]:
                if not line or line.startswith("Log"):
                    continue

                numRequests[filename] += 1

                try:
                    userId, logTime, method, uri, status, bytes, _ignore_referer, client, extended = parseLine(line)
                except Exception as e:
                    parseErrors += 1

                    if debug:
                        print("Access log line parse failure", e)
                        print(traceback.print_exc())
                        print("---")
                        print(line)
                        print("---")

                    continue

                logTime = dateparse(logTime, fuzzy=True)
                times.append(logTime)
                perfile_times[filename].append(logTime)

                if status >= 500:
                    errorCount += 1

                if uri == "/ischedule":
                    numServerToServer[filename] += 1
                elif uri.startswith("/calendars"):
                    numProxied[filename] += 1

                outstanding = int(extended['or'])
                logId = int(extended['i'] if extended['i'] else 0)
                raw = rawCounts.get(logId, 0) + 1
                rawCounts[logId] = raw
                prevMax = ids.get(logId, 0)
                if outstanding > prevMax:
                    ids[logId] = outstanding
                slotCount[filename] += outstanding

                respTime = float(extended['t'])
                wrTime = float(extended.get('t-resp-wr', 0.0))
                timeSpent = timesSpent.get(logId, 0.0) + respTime
                timesSpent[logId] = timeSpent
                totalRespTime[filename] += respTime
                totalRespWithoutWRTime[filename] += respTime - wrTime
                if respTime > maxRespTime[filename]:
                    maxRespTime[filename] = respTime

                for index, testTime in enumerate((respTime, respTime - wrTime,)):
                    if testTime >= 60000.0:
                        over60s[index] += 1
                    elif testTime >= 30000.0:
                        over30s[index] += 1
                    elif testTime >= 10000.0:
                        over10s[index] += 1
                    elif testTime >= 1000.0:
                        over1s[index] += 1
                    elif testTime >= 100.0:
                        over100ms[index] += 1
                    elif testTime >= 10.0:
                        over10ms[index] += 1
                    else:
                        under10ms[index] += 1
                    if testTime >= 1000.0:
                        totalOver1s[index] += 1
                    if testTime >= 10000.0:
                        totalOver10s[index] += 1

                ext = []
                for key, value in extended.iteritems():
                    if key not in ('i', 't'):
                        if key == "cl":
                            value = float(value) / 1024
                            value = "%.1fKB" % (value,)
                            key = "req"
                        ext.append("%s:%s" % (key, value))
                ext = ", ".join(ext)

                try:
                    client = client.split(";")[2]
                    client = client.strip()
                except:
                    pass

                if userId != "-":
                    userStat = users.get(userId, {'count': 0, 'clients': {}})
                    userStat['count'] += 1
                    clientCount = userStat['clients'].get(client, 0)
                    userStat['clients'][client] = clientCount + 1
                    users[userId] = userStat

                reqStartTime = logTime - datetime.timedelta(milliseconds=respTime)
                requests.append((respTime, userId, method, bytes / 1024.0, ext, client, logId, logTime, reqStartTime))

        times.sort()
        if len(times) == 0:
            print("No data to analyze")
            time.sleep(10)
            continue

        totalRequests = sum(numRequests.values())

        print("- " * 40)
        print(datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S"),)
        if enableListenQueue:
            q, lqssl, lqnon = listenQueueHistory()
            print("Listenq (ssl+non):", q[0], " (Recent", ", ".join(q[1:]), "Oldest)")
        if enableCpuIdle:
            q = idleHistory()
            print("CPU idle %:", q[0], " (Recent", ", ".join(q[1:]), "Oldest)")
        if enableFreeMem:
            print("Memory free:", freemem())
        print("CPU Per Daemon:", cpuPerDaemon())
        print("")

        table = tables.Table()
        table.addHeader(
            ("Instance", "Requests", "Av. Requests", "Av. Response", "Av. Response", "Max. Response", "Slot", "Start", "End", "File Name"),
        )
        table.addHeader(
            ("", "", "per second", "(ms)", "no write(ms)", "(ms)", "Average", "Time", "Time", ""),
        )
        table.setDefaultColumnFormats(
            (
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.CENTER_JUSTIFY),
                tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.2f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            )
        )

        totalAverage = 0.0
        totalResponseTime = 0.0
        totalResponseWithoutWRTime = 0.0
        maxResponseTime = 0.0
        totalSlots = 0
        minStartTime = None
        maxEndTime = None
        for ctr, filename in enumerate(sorted(perfile_times.keys())):

            times = sorted(perfile_times[filename])
            startTime = times[0]
            minStartTime = startTime if minStartTime is None else min(minStartTime, startTime)

            endTime = times[-1]
            maxEndTime = endTime if maxEndTime is None else min(maxEndTime, endTime)

            deltaTime = endTime - startTime

            avgRequests = float(len(times)) / deltaTime.seconds
            totalAverage += avgRequests

            avgResponse = totalRespTime[filename] / len(times)
            totalResponseTime += totalRespTime[filename]

            avgResponseWithWR = totalRespWithoutWRTime[filename] / len(times)
            totalResponseWithoutWRTime += totalRespWithoutWRTime[filename]

            maxResponseTime = max(maxResponseTime, maxRespTime[filename])

            totalSlots += slotCount[filename]

            table.addRow((
                "#%s" % (ctr + 1,),
                len(times),
                avgRequests,
                avgResponse,
                avgResponseWithWR,
                maxRespTime[filename],
                float(slotCount[filename]) / len(times),
                startTime,
                endTime,
                filename[prefix:],
            ))

        if len(perfile_times) > 1:
            table.addFooter((
                "Total:",
                totalRequests,
                totalAverage,
                totalResponseTime / totalRequests,
                totalResponseWithoutWRTime / totalRequests,
                maxResponseTime,
                float(totalSlots) / totalRequests,
                minStartTime,
                maxEndTime,
                "",
            ))

        os = StringIO()
        table.printTable(os=os)
        print(os.getvalue())

        if enableListenQueue:
            lqlatency = (lqssl / avgRequests, lqnon / avgRequests,) if avgRequests else (0.0, 0.0,)
            print(" listenq latency (ssl+non): %.1f s %.1f s" % (
                lqlatency[0],
                lqlatency[1],
            ))

        table = tables.Table()
        table.addHeader(
            ("", "<10ms", "10ms<->100ms", "100ms<->1s", "1s<->10s", "10s<->30s", "30s<->60s", ">60s", "Over 1s", "Over 10s"),
        )
        table.setDefaultColumnFormats(
            (
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.CENTER_JUSTIFY),
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
        for i in xrange(2):
            table.addRow((
                "Overall Response" if i == 0 else "Response without Write",
                (under10ms[i], safePercent(under10ms[i], totalRequests)),
                (over10ms[i], safePercent(over10ms[i], totalRequests)),
                (over100ms[i], safePercent(over100ms[i], totalRequests)),
                (over1s[i], safePercent(over1s[i], totalRequests)),
                (over10s[i], safePercent(over10s[i], totalRequests)),
                (over30s[i], safePercent(over30s[i], totalRequests)),
                (over60s[i], safePercent(over60s[i], totalRequests)),
                safePercent(totalOver1s[i], totalRequests),
                safePercent(totalOver10s[i], totalRequests),
            ))
        os = StringIO()
        table.printTable(os=os)
        print(os.getvalue())
        print()
        if errorCount:
            print("Number of 500 errors: %d" % (errorCount,))
        if parseErrors:
            print("Number of access log parsing errors: %d" % (parseErrors,))
        if errorCount or parseErrors:
            print("")

        print("Proc:   Peak outstanding:        Seconds of processing (number of requests):")
        for l in xrange((numProcs - 1) / 10 + 1):
            base = l * 10
            print("%2d-%2d: " % (base, base + 9),)

            for i in xrange(base, base + 10):
                try:
                    r = ids[i]
                    s = "%1d" % (r,)
                except KeyError:
                    s = "."
                print(s, end="")

            print("    ", end="")

            for i in xrange(base, base + 10):
                try:
                    r = timesSpent[i] / 1000
                    c = rawCounts[i]
                    s = "%4.0f(%4d)" % (r, c)
                except KeyError:
                    s = "         ."
                print(s, end="")

            print("")

        print("")
        print("Top %d longest (in most recent %d requests):" % (numTop, sum(numRequests.values()),))
        requests.sort()
        requests.reverse()
        for i in xrange(numTop):
            try:
                respTime, userId, method, kb, ext, client, logId, logTime, reqStartTime = requests[i]
                """
                overlapCount = 0
                for request in requests:
                    _respTime, _userId, _method, _kb, _ext, _client, _logId, _logTime, _reqStartTime = request
                    if _logId == logId and _logTime > reqStartTime and _reqStartTime < logTime:
                        overlapCount += 1

                print("%7.1fms  %-12s %s res:%.1fKB, %s [%s] #%d +%d %s->%s" % (respTime, userId, method, kb, ext, client, logId, overlapCount, reqStartTime.strftime("%H:%M:%S"), logTime.strftime("%H:%M:%S"),))
                """
                print("%7.1fms  %-12s %s res:%.1fKB, %s [%s] #%d %s->%s" % (respTime, userId, method, kb, ext, client, logId, reqStartTime.strftime("%H:%M:%S"), logTime.strftime("%H:%M:%S"),))
            except:
                pass

        print("")
        print("Top %d busiest users (in most recent %d requests):" % (numUsers, totalRequests,))
        userlist = []
        for user, userStat in users.iteritems():
            userlist.append((userStat['count'], user, userStat))
        userlist.sort()
        userlist.reverse()
        for i in xrange(numUsers):
            try:
                count, user, userStat = userlist[i]
                print("%3d  %-12s " % (count, user), end="")
                clientStat = userStat['clients']
                clients = clientStat.keys()
                if len(clients) == 1:
                    print("[%s]" % (clients[0],))
                else:
                    clientList = []
                    for client in clients:
                        clientList.append("%s: %d" % (client, clientStat[client]))
                    print("[%s]" % ", ".join(clientList))
            except:
                pass

        print()

        # lineRange => do loop only once
        if lineRange is not None:
            break

    except Exception as e:
        print("Script failure", e)
        if debug:
            print(traceback.print_exc())

    time.sleep(10)
