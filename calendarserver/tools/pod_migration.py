#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_calverify -*-
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

"""
This tool manages an overall pod migration. Migration is done in a series of steps,
with the system admin triggering each step individually by running this tool.
"""

import os
import sys

from twisted.internet.defer import inlineCallbacks
from twisted.python.failure import Failure
from twisted.python.text import wordWrap
from twisted.python.usage import Options, UsageError

from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from twistedcaldav.timezones import TimezoneCache

from txdav.common.datastore.podding.migration.home_sync import CrossPodHomeSync

from twext.python.log import Logger
from twext.who.idirectory import RecordType

from calendarserver.tools.cmdline import utilityMain, WorkerService


log = Logger()

VERSION = "1"


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        PodMigrationOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = ''.join(
    wordWrap(
        """
        Usage: calendarserver_pod_migration [options] [input specifiers]
        """,
        int(os.environ.get('COLUMNS', '80'))
    )
)
description += "\nVersion: %s" % (VERSION,)


class ConfigError(Exception):
    pass


class PodMigrationOptions(Options):
    """
    Command-line options for 'calendarserver_pod_migration'
    """

    synopsis = description

    optFlags = [
        ['verbose', 'v', "Verbose logging."],
        ['debug', 'D', "Debug logging."],
        ['step1', '1', "Run step 1 of the migration (initial sync)"],
        ['step2', '2', "Run step 2 of the migration (incremental sync)"],
        ['step3', '3', "Run step 3 of the migration (prepare for final sync)"],
        ['step4', '4', "Run step 4 of the migration (final incremental sync)"],
        ['step5', '5', "Run step 5 of the migration (final reconcile sync)"],
        ['step6', '6', "Run step 6 of the migration (enable new home)"],
        ['step7', '7', "Run step 7 of the migration (remove old home)"],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
        ['uid', 'u', "", "Directory record uid of user to migrate [REQUIRED]"],
    ]

    longdesc = "Only one step option is allowed."

    def __init__(self):
        super(PodMigrationOptions, self).__init__()
        self.outputName = '-'

    def opt_output(self, filename):
        """
        Specify output file path (default: '-', meaning stdout).
        """
        self.outputName = filename

    opt_o = opt_output

    def openOutput(self):
        """
        Open the appropriate output file based on the '--output' option.
        """
        if self.outputName == '-':
            return sys.stdout
        else:
            return open(self.outputName, 'wb')

    def postOptions(self):
        runstep = None
        for step in range(7):
            if self["step{}".format(step + 1)]:
                if runstep is None:
                    runstep = step
                    self["runstep"] = step + 1
                else:
                    raise UsageError("Only one step option allowed")
        else:
            if runstep is None:
                raise UsageError("One step option must be present")
        if not self["uid"]:
            raise UsageError("A uid is required")


class PodMigrationService(WorkerService, object):
    """
    Service which runs, does its stuff, then stops the reactor.
    """

    def __init__(self, store, options, output, reactor, config):
        super(PodMigrationService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config
        TimezoneCache.create()

    @inlineCallbacks
    def doWork(self):
        """
        Do the work, stopping the reactor when done.
        """
        self.output.write("\n---- Pod Migration version: %s ----\n" % (VERSION,))

        # Map short name to uid
        record = yield self.store.directoryService().recordWithUID(self.options["uid"])
        if record is None:
            record = yield self.store.directoryService().recordWithShortName(RecordType.user, self.options["uid"])
            if record is not None:
                self.options["uid"] = record.uid

        try:
            yield getattr(self, "step{}".format(self.options["runstep"]))()
            self.output.close()
        except ConfigError:
            pass
        except:
            self.output.write(str(Failure()))
            log.failure("doWork()")

    @inlineCallbacks
    def step1(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 1\n")
        yield syncer.sync()

    @inlineCallbacks
    def step2(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 2\n")
        yield syncer.sync()

    @inlineCallbacks
    def step3(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 3\n")
        yield syncer.disableRemoteHome()

    @inlineCallbacks
    def step4(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            final=True,
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 4\n")
        yield syncer.sync()

    @inlineCallbacks
    def step5(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            final=True,
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 5\n")
        yield syncer.finalSync()

    @inlineCallbacks
    def step6(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 6\n")
        yield syncer.enableLocalHome()

    @inlineCallbacks
    def step7(self):
        syncer = CrossPodHomeSync(
            self.store,
            self.options["uid"],
            final=True,
            uselog=self.output if self.options["verbose"] else None
        )
        syncer.accounting("Pod Migration Step 7\n")
        yield syncer.removeRemoteHome()


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    """
    Do the export.
    """
    if reactor is None:
        from twisted.internet import reactor
    options = PodMigrationOptions()
    try:
        options.parseOptions(argv[1:])
    except UsageError as e:
        stderr.write("Invalid options specified\n")
        options.opt_help()

    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)

    def makeService(store):
        from twistedcaldav.config import config
        config.TransactionTimeoutSeconds = 0
        return PodMigrationService(store, options, output, reactor, config)

    utilityMain(options['config'], makeService, reactor, verbose=options["debug"])

if __name__ == '__main__':
    main()
