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

from __future__ import print_function
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time


class SimRegress(object):
    """
    Class that manages running the sim against a range of SVN revisions.
    """

    TMP_DIR = "/tmp/CalendarServer-SimRegress"

    def __init__(self, startRev, stopRev, stepRev):
        self.startRev = startRev
        self.currentRev = startRev
        self.stopRev = stopRev
        self.stepRev = stepRev
        self.cwd = os.getcwd()
        self.results = []

    def run(self):

        # Get the actual SVN revisions we want to use
        svn_revs = self.getRevisions()
        print("SVN Revisions to analyze: {}".format(svn_revs))

        # Create the tmp dir and do initial checkout
        for revision in svn_revs:
            self.currentRev = revision
            logfile = os.path.join(self.cwd, "Log-rev-{}.txt".format(self.currentRev))
            with open(logfile, "w") as f:
                self.log = f
                self.checkRevision()
                self.buildServer()
                self.runServer()
                qos = self.runSim()
                self.stopServer()
            with open(logfile) as f:
                qos = filter(lambda line: line.strip().startswith("Qos : "), f.read().splitlines())
                try:
                    qos = float(qos[0].strip()[len("Qos : "):])
                except (IndexError, ValueError):
                    qos = None
                print("Revision: {}  Qos: {}".format(self.currentRev, qos))
                self.results.append((self.currentRev, qos))

        print("All revisions complete")

        print("\n*** Results:")
        print("Rev\tQos")
        for result in self.results:
            rev, qos = result
            qos = "{:.4f}".format(qos) if qos is not None else "-"
            print("{}\t{}".format(rev, qos))

    def getRevisions(self):

        if self.stopRev is None:
            print("Determining HEAD revision")
            out = subprocess.check_output("svn info -r HEAD".split())
            rev = filter(lambda line: line.startswith("Revision: "), out.splitlines())
            if rev:
                self.stopRev = int(rev[0][len("Revision: "):])
                print("Using HEAD revision: {}".format(self.stopRev))

        return range(self.startRev, self.stopRev, self.stepRev) + [self.stopRev]

    def checkRevision(self):
        # Create the tmp dir and do initial checkout
        if not os.path.exists(SimRegress.TMP_DIR):
            os.makedirs(SimRegress.TMP_DIR)
            os.chdir(SimRegress.TMP_DIR)
            self.checkoutInitialRevision()
        else:
            os.chdir(SimRegress.TMP_DIR)
            actualRevision = self.currentRevision()
            if actualRevision > self.currentRev:
                # If actualRevision code is newer than what we want, always wipe it
                # and start from scratch
                shutil.rmtree(SimRegress.TMP_DIR)
                self.checkRevision()
            elif actualRevision < self.currentRev:
                # Upgrade from older revision to what we want
                self.updateRevision()

    def checkoutInitialRevision(self):
        print("Checking out revision: {}".format(self.startRev))
        subprocess.call(
            "svn checkout http://svn.calendarserver.org/repository/calendarserver/CalendarServer/trunk -r {start} .".format(start=self.startRev).split(),
            stdout=self.log, stderr=self.log,
        )

    def currentRevision(self):
        print("Checking current revision")
        out = subprocess.check_output("svn info".split())
        rev = filter(lambda line: line.startswith("Revision: "), out.splitlines())
        return int(rev[0][len("Revision: "):]) if rev else None

    def updateRevision(self):
        print("Updating to revision: {}".format(self.currentRev))
        subprocess.call(
            "svn up -r {rev} .".format(rev=self.currentRev).split(),
            stdout=self.log, stderr=self.log,
        )

    def patchConfig(self, configPath):
        """
        Patch the plist config file to use settings that make sense for the sim.

        @param configPath: path to plist file to patch
        @type configPath: L{str}
        """

        f = plistlib.readPlist(configPath)
        f['Authentication']['Kerberos']['Enabled'] = False
        plistlib.writePlist(f, configPath)

    def buildServer(self):
        print("Building revision: {}".format(self.currentRev))
        subprocess.call("./bin/develop".split(), stdout=self.log, stderr=self.log)

    def runServer(self):
        print("Running revision: {}".format(self.currentRev))
        shutil.copyfile("conf/caldavd-test.plist", "conf/caldavd-dev.plist")
        self.patchConfig("conf/caldavd-dev.plist")
        if os.path.exists("data"):
            shutil.rmtree("data")
        subprocess.call("./bin/run -nd".split(), stdout=self.log, stderr=self.log)

        # Wait for first child pid to appear then wait another 10 seconds
        t = time.time()
        while time.time() - t < 60:
            if os.path.exists("data/Logs/state/caldav-instance-0.pid"):
                time.sleep(10)
                break

    def stopServer(self):
        print("Stopping revision: {}".format(self.currentRev))
        subprocess.call("./bin/run -k".split(), stdout=self.log, stderr=self.log)

    def runSim(self):
        print("Running sim")
        if os.path.exists("/tmp/sim"):
            shutil.rmtree("/tmp/sim")
        subprocess.call("{exe} {sim} --config {config} --clients {clients} --runtime 300".format(
            exe=sys.executable,
            sim=os.path.join(self.cwd, "contrib/performance/loadtest/sim.py"),
            config=os.path.join(self.cwd, "contrib/performance/loadtest/config-old.plist"),
            clients=os.path.join(self.cwd, "contrib/performance/loadtest/clients-old.plist"),
        ).split(), stdout=self.log, stderr=self.log)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Run the sim tool against a specific range of server revisions.")
    parser.add_argument("--start", type=int, required=True, help="Revision number to start at")
    parser.add_argument("--stop", type=int, help="Revision number to stop at")
    parser.add_argument("--step", default=100, type=int, help="Revision number steps")

    args = parser.parse_args()

    SimRegress(args.start, args.stop, args.step).run()
