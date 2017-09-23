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
from caldavclientlibrary.client.httpshandler import SmartHTTPConnection
from multiprocessing import Process, Value
import getopt
import itertools
import json
import random
import sys
import time
from urlparse import urlparse
import collections

PRIORITY = {
    "low": 0,
    "medium": 1,
    "high": 2,
}


def httploop(ctr, config, complete):

    # Random time delay
    time.sleep(random.randint(0, config["interval"]) / 1000.0)

    ServerDetail = collections.namedtuple("ServerDetails", ("host", "port", "use_ssl", "headers",))
    server_details = []
    for server in config["server"].split(","):
        url = urlparse(server)
        use_ssl = url[0] == "https"
        if "@" in url[1]:
            auth, net_loc = url[1].split("@")
        else:
            auth = "admin:admin"
            net_loc = url[1]
        host, port = net_loc.split(":")
        port = int(port)
        user, pswd = auth.split(":")

        headers = {}
        headers["User-Agent"] = "httploop/1"
        headers["Depth"] = "1"
        headers["Authorization"] = "Basic " + "{}:{}".format(user, pswd).encode("base64")[:-1]
        headers["Content-Type"] = "application/json"

        server_details.append(ServerDetail(host, port, use_ssl, headers))

    random.shuffle(server_details)

    interval = config["interval"] / 1000.0
    total = config["limit"] / config["numProcesses"]

    count = 0

    jstr = json.dumps({
        "action": "testwork",
        "when": config["when"],
        "delay": config["delay"],
        "jobs": config["jobs"],
        "priority": PRIORITY[config["priority"]],
        "weight": config["weight"],
    })

    base_time = time.time()
    iter = itertools.cycle(server_details)
    while not complete.value:

        server = next(iter)
        count += 1

        http = SmartHTTPConnection(server.host, server.port, server.use_ssl, False)

        try:
            server.headers["User-Agent"] = "httploop-{}/{}".format(ctr, count,)
            http.request("POST", "/control", jstr, server.headers)

            response = http.getresponse()
            response.read()

        except Exception as e:
            print("Count: {}".format(count,))
            print(repr(e))
            raise

        finally:
            http.close()

        if total != 0 and count >= total:
            break

        base_time += interval
        sleep_time = base_time - time.time()
        if sleep_time < 0:
            print("Interval less than zero: process #{}".format(ctr))
            base_time = time.time()
        else:
            time.sleep(sleep_time)


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: loadtest [options]
Options:
    -h             Print this help and exit
    -n NUM         Number of child processes [10]
    -i MSEC        Millisecond delay between each request [1000]
    -r RATE        Requests/second rate [10]
    -j JOBS        Number of jobs per HTTP request [1]
    -s URL         URL to connect to [https://localhost:8443],
                    can also be a comma separated list of URLs
                    to cycle through
    -b SEC         Number of seconds for notBefore [0]
    -d MSEC        Number of milliseconds for the work [10]
    -l NUM         Total number of requests from all processes
                    or zero for unlimited [0]
    -t SEC         Number of seconds to run [unlimited]
    -p low|medium|high  Work priority level [high]
    -w NUM         Work weight level 1..10 [1]

Arguments: None

Description:
This utility will use the control API to generate test work items at
the specified rate using multiple processes to generate each HTTP
request. Each child process will execute HTTP requests at a specific
interval (with a random start offset for each process). The total
number of processes can also be set to give an effective rate.
Alternatively, the rate can be set directly and the tool will pick
a suitable number of processes and interval to use.

The following combinations of -n, -i and -r are allowed:

    -n, -i
    -n, -r
    -r
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == '__main__':

    done = Value("i", 0)
    config = {
        "numProcesses": 10,
        "interval": 1000,
        "jobs": 1,
        "server": "https://localhost:8443",
        "when": 0,
        "delay": 10,
        "priority": "high",
        "weight": 1,
        "limit": 0,
    }
    numProcesses = None
    interval = None
    rate = None
    runtime = None

    options, args = getopt.getopt(sys.argv[1:], "b:d:hi:j:l:n:p:r:s:t:w:", [])

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-n":
            numProcesses = int(value)
        elif option == "-i":
            interval = int(value)
        elif option == "-j":
            config["jobs"] = int(value)
        elif option == "-r":
            rate = int(value)
        elif option == "-s":
            config["server"] = value
        elif option == "-b":
            config["when"] = int(value)
        elif option == "-d":
            config["delay"] = int(value)
        elif option == "-l":
            config["limit"] = int(value)
        elif option == "-t":
            runtime = int(value)
        elif option == "-p":
            if value not in PRIORITY.keys():
                usage("Unrecognized priority: {}".format(value,))
            config["priority"] = value
        elif option == "-w":
            config["weight"] = int(value)
        else:
            usage("Unrecognized option: {}".format(option,))

    # Determine the actual number of processes and interval
    if numProcesses is not None and interval is not None and rate is None:
        config["numProcesses"] = numProcesses
        config["interval"] = interval
    elif numProcesses is not None and interval is None and rate is not None:
        config["numProcesses"] = numProcesses
        config["interval"] = (config["numProcesses"] * 1000) / rate
    elif numProcesses is None and interval is None and rate is not None:
        if rate <= 100:
            config["numProcesses"] = rate
            config["interval"] = 1000
        else:
            config["numProcesses"] = 100
            config["interval"] = (config["numProcesses"] * 1000) / rate
    elif numProcesses is None and interval is None and rate is None:
        pass
    else:
        usage("Wrong combination of -n, -i and -r")
    effective_rate = (config["numProcesses"] * 1000) / config["interval"]

    if runtime:
        config["limit"] = effective_rate * runtime

    print("Run details:")
    print("  Number of servers: {}".format(len(config["server"].split(","))))
    print("  Number of processes: {}".format(config["numProcesses"]))
    print("  Interval between requests: {} ms".format(config["interval"]))
    print("  Effective request rate: {} req/sec".format(effective_rate))
    print("  Jobs per request: {}".format(config["jobs"]))
    print("  Effective job rate: {} jobs/sec".format(effective_rate * config["jobs"]))
    print(("  Total number of requests: {}").format(config["limit"] if config["limit"] != 0 else "unlimited"))
    print("")
    print("Work details:")
    print(("  Priority: {}").format(config["priority"]))
    print(("  Weight: {}").format(config["weight"]))
    print(("  Start delay: {} s").format(config["when"]))
    print(("  Execution time: {} ms").format(config["delay"]))
    print(("  Average queue depth: {}").format((effective_rate * config["delay"]) / 1000))
    print("")
    print("Starting up...")

    # Create a set of processes, then start them all
    procs = [Process(target=httploop, args=(ctr + 1, config, done,)) for ctr in range(config["numProcesses"])]
    map(lambda p: p.start(), procs)

    # Wait for user to cancel, then signal end and wait for processes to finish
    time.sleep(1)
    raw_input("\n\nEnd run (hit return)")
    done.value = 1
    map(lambda p: p.join(), procs)

    print("\nRun complete")
