#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_upgrade -*-
##
# Copyright (c) 2006-2017 Apple Inc. All rights reserved.
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
This tool allows any necessary upgrade to complete, then exits.
"""
from __future__ import print_function

import os
import sys
import time

from txdav.common.datastore.sql import CommonDataStore

from twisted.internet.defer import succeed
from twisted.logger import LogLevel, formatEvent, formatTime
from twisted.logger import FilteringLogObserver, LogLevelFilterPredicate
from twisted.python.text import wordWrap
from twisted.python.usage import Options, UsageError

from twext.python.log import Logger

from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE

from calendarserver.tools.cmdline import utilityMain, WorkerService
from calendarserver.tap.caldav import CalDAVServiceMaker

log = Logger()


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        UpgradeOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = '\n'.join(
    wordWrap(
        """
        Usage: calendarserver_upgrade [options] [input specifiers]\n
        """ + __doc__,
        int(os.environ.get('COLUMNS', '80'))
    )
)


class UpgradeOptions(Options):
    """
    Command-line options for 'calendarserver_upgrade'

    @ivar upgraders: a list of L{DirectoryUpgradeer} objects which can identify the
        calendars to upgrade, given a directory service.  This list is built by
        parsing --record and --collection options.
    """

    synopsis = description

    optFlags = [
        ['status', 's', "Check database status and exit."],
        ['check', 'c', "Check current database schema and exit."],
        ['postprocess', 'p', "Perform post-database-import processing."],
        ['debug', 'D', "Print log messages to STDOUT."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
        ['prefix', 'x', "", "Only upgrade homes with the specified GUID prefix - partial upgrade only."],
    ]

    def __init__(self):
        super(UpgradeOptions, self).__init__()
        self.upgradeers = []
        self.outputName = '-'
        self.merge = False

    def opt_output(self, filename):
        """
        Specify output file path (default: '-', meaning stdout).
        """
        self.outputName = filename

    opt_o = opt_output

    def opt_merge(self):
        """
        Rather than skipping homes that exist on the filesystem but not in the
        database, merge their data into the existing homes.
        """
        self.merge = True

    opt_m = opt_merge

    def openOutput(self):
        """
        Open the appropriate output file based on the '--output' option.
        """
        if self.outputName == '-':
            return sys.stdout
        else:
            return open(self.outputName, 'wb')


class UpgraderService(WorkerService, object):
    """
    Service which runs, exports the appropriate records, then stops the reactor.
    """

    started = False

    def __init__(self, store, options, output, reactor, config):
        super(UpgraderService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config
        self._directory = None

    def doWork(self):
        """
        Immediately stop.  The upgrade will have been run before this.
        """
        # If we get this far the database is OK
        if self.options["status"]:
            self.output.write("Database OK.\n")
        elif self.options["check"]:
            checkResult = getattr(CommonDataStore, "checkSchemaResults", None)
            if checkResult is not None:
                if checkResult:
                    self.output.write("Database check FAILED:\n{}\n".format("\n".join(checkResult)))
                else:
                    self.output.write("Database check OK.\n")
            else:
                self.output.write("Database check FAILED.\n")
        else:
            self.output.write("Upgrade complete, shutting down.\n")
        UpgraderService.started = True
        return succeed(None)

    def doWorkWithoutStore(self):
        """
        Immediately stop.  The upgrade will have been run before this and failed.
        """
        if self.options["status"]:
            self.output.write("Upgrade needed.\n")
        elif self.options["check"]:
            checkResult = getattr(CommonDataStore, "checkSchemaResults", None)
            if checkResult is not None:
                if checkResult:
                    self.output.write("Database check FAILED:\n{}\n".format("\n".join(checkResult)))
                else:
                    self.output.write("Database check OK.\n")
            else:
                self.output.write("Database check FAILED.\n")
        else:
            self.output.write("Upgrade failed.\n")
        UpgraderService.started = True
        return succeed(None)

    def postStartService(self):
        """
        Quit right away
        """
        from twisted.internet import reactor
        from twisted.internet.error import ReactorNotRunning
        try:
            reactor.stop()
        except ReactorNotRunning:
            # I don't care.
            pass

    def stopService(self):
        """
        Stop the service.  Nothing to do; everything should be finished by this
        time.
        """


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    """
    Do the export.
    """
    from twistedcaldav.config import config
    if reactor is None:
        from twisted.internet import reactor

    options = UpgradeOptions()
    try:
        options.parseOptions(argv[1:])
    except UsageError as e:
        usage(e)

    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)

    if options.merge:
        def setMerge(data):
            data.MergeUpgrades = True
        config.addPostUpdateHooks([setMerge])

    def makeService(store):
        return UpgraderService(store, options, output, reactor, config)

    def onlyUpgradeEvents(eventDict):
        text = formatEvent(eventDict)
        output.write(formatTime(eventDict.get("log_time", time.time())) + " " + text + "\n")
        output.flush()

    if not options["status"] and not options["check"]:
        # When doing an upgrade always send L{LogLevel.warn} logging to the tool output
        log.observer.addObserver(FilteringLogObserver(
            onlyUpgradeEvents,
            [LogLevelFilterPredicate(defaultLogLevel=LogLevel.warn), ]
        ))

    def customServiceMaker():
        customService = CalDAVServiceMaker()
        customService.doPostImport = options["postprocess"]
        return customService

    def _patchConfig(config):
        config.FailIfUpgradeNeeded = options["status"] or options["check"]
        config.CheckExistingSchema = options["check"]
        if options["prefix"]:
            config.UpgradeHomePrefix = options["prefix"]
        if not options["status"] and not options["check"]:
            config.DefaultLogLevel = "debug"

    def _onShutdown():
        if not UpgraderService.started:
            print("Failed to start service.")

    utilityMain(options["config"], makeService, reactor, customServiceMaker, patchConfig=_patchConfig, onShutdown=_onShutdown, verbose=options["debug"])


if __name__ == '__main__':
    main()
