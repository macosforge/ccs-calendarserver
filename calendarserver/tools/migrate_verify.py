#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_calverify -*-
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

"""
This tool takes a list of files paths from a file store being migrated
and compares that to the results of a migration to an SQL store. Items
not migrated are logged.
"""

import os
import sys

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.text import wordWrap
from twisted.python.usage import Options

from twext.enterprise.dal.syntax import Select, Parameter
from twext.python.log import Logger

from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE

from txdav.common.datastore.sql_tables import schema, _BIND_MODE_OWN

from calendarserver.tools.cmdline import utilityMain, WorkerService

log = Logger()

VERSION = "1"


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        MigrateVerifyOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = ''.join(
    wordWrap(
        """
        Usage: calendarserver_migrate_verify [options] [input specifiers]
        """,
        int(os.environ.get('COLUMNS', '80'))
    )
)
description += "\nVersion: %s" % (VERSION,)


class ConfigError(Exception):
    pass


class MigrateVerifyOptions(Options):
    """
    Command-line options for 'calendarserver_migrate_verify'
    """

    synopsis = description

    optFlags = [
        ['debug', 'D', "Debug logging."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
        ['data', 'd', "./paths.txt", "List of file paths for migrated data."],
    ]

    def __init__(self):
        super(MigrateVerifyOptions, self).__init__()
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


class MigrateVerifyService(WorkerService, object):
    """
    Service which runs, does its stuff, then stops the reactor.
    """

    def __init__(self, store, options, output, reactor, config):
        super(MigrateVerifyService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config

        self.pathsByGUID = {}
        self.badPaths = []
        self.validPaths = 0
        self.ignoreInbox = 0
        self.ignoreDropbox = 0
        self.missingGUIDs = []
        self.missingCalendars = []
        self.missingResources = []

    @inlineCallbacks
    def doWork(self):
        """
        Do the work, stopping the reactor when done.
        """
        self.output.write("\n---- Migrate Verify version: %s ----\n" % (VERSION,))

        try:
            self.readPaths()
            yield self.doCheck()
            self.output.close()
        except ConfigError:
            pass
        except:
            log.failure("doWork()")

    def readPaths(self):

        self.output.write("-- Reading data file: %s\n" % (self.options["data"]))

        with open(os.path.expanduser(self.options["data"])) as datafile:
            total = 0
            invalidGUIDs = set()
            for line in datafile:
                line = line.strip()
                total += 1
                segments = line.split("/")
                while segments and segments[0] != "__uids__":
                    segments.pop(0)
                if segments and len(segments) >= 6:
                    guid = segments[3]
                    calendar = segments[4]
                    resource = segments[5]

                    if calendar == "inbox":
                        self.ignoreInbox += 1
                        invalidGUIDs.add(guid)
                    elif calendar == "dropbox":
                        self.ignoreDropbox += 1
                        invalidGUIDs.add(guid)
                    elif len(segments) > 6:
                        self.badPaths.append(line)
                        invalidGUIDs.add(guid)
                    else:
                        self.pathsByGUID.setdefault(guid, {}).setdefault(calendar, set()).add(resource)
                        self.validPaths += 1
                else:
                    if segments and len(segments) >= 4:
                        invalidGUIDs.add(segments[3])
                    self.badPaths.append(line)

        # Remove any invalid GUIDs that actuall were valid
        invalidGUIDs = [pguid for pguid in invalidGUIDs if pguid not in self.pathsByGUID]

        self.output.write("\nTotal lines read: %d\n" % (total,))
        self.output.write("Total guids: valid: %d  invalid: %d  overall: %d\n" % (
            len(self.pathsByGUID),
            len(invalidGUIDs),
            len(self.pathsByGUID) + len(invalidGUIDs),
        ))
        self.output.write("Total valid calendars: %d\n" % (sum([len(v) for v in self.pathsByGUID.values()]),))
        self.output.write("Total valid resources: %d\n" % (self.validPaths,))
        self.output.write("Total inbox resources: %d\n" % (self.ignoreInbox,))
        self.output.write("Total dropbox resources: %d\n" % (self.ignoreDropbox,))
        self.output.write("Total bad paths: %d\n" % (len(self.badPaths),))

        self.output.write("\n-- Invalid GUIDs\n")
        for invalidGUID in sorted(invalidGUIDs):
            self.output.write("Invalid GUID: %s\n" % (invalidGUID,))

        self.output.write("\n-- Bad paths\n")
        for badPath in sorted(self.badPaths):
            self.output.write("Bad path: %s\n" % (badPath,))

    @inlineCallbacks
    def doCheck(self):
        """
        Check path data against the SQL store.
        """

        self.output.write("\n-- Scanning database for missed migrations\n")

        # Get list of distinct resource_property resource_ids to delete
        self.txn = self.store.newTransaction()

        total = len(self.pathsByGUID)
        totalMissingCalendarResources = 0
        count = 0
        for guid in self.pathsByGUID:

            if divmod(count, 10)[1] == 0:
                self.output.write(("\r%d of %d (%d%%)" % (
                    count,
                    total,
                    (count * 100 / total),
                )).ljust(80))
                self.output.flush()

            # First check the presence of each guid and the calendar count
            homeID = (yield self.guid2ResourceID(guid))
            if homeID is None:
                self.missingGUIDs.append(guid)
                continue

            # Now get the list of calendar names and calendar resource IDs
            results = (yield self.calendarsForUser(homeID))
            if results is None:
                results = []
            calendars = dict(results)
            for calendar in self.pathsByGUID[guid].keys():
                if calendar not in calendars:
                    self.missingCalendars.append("%s/%s (resources: %d)" % (guid, calendar, len(self.pathsByGUID[guid][calendar])))
                    totalMissingCalendarResources += len(self.pathsByGUID[guid][calendar])
                else:
                    # Now get list of all calendar resources
                    results = (yield self.resourcesForCalendar(calendars[calendar]))
                    if results is None:
                        results = []
                    results = [result[0] for result in results]
                    db_resources = set(results)

                    # Also check for split calendar
                    if "%s-vtodo" % (calendar,) in calendars:
                        results = (yield self.resourcesForCalendar(calendars["%s-vtodo" % (calendar,)]))
                        if results is None:
                            results = []
                        results = [result[0] for result in results]
                        db_resources.update(results)

                    # Also check for split calendar
                    if "%s-vevent" % (calendar,) in calendars:
                        results = (yield self.resourcesForCalendar(calendars["%s-vevent" % (calendar,)]))
                        if results is None:
                            results = []
                        results = [result[0] for result in results]
                        db_resources.update(results)

                    old_resources = set(self.pathsByGUID[guid][calendar])
                    self.missingResources.extend(["%s/%s/%s" % (guid, calendar, resource,) for resource in old_resources.difference(db_resources)])

            # Commit every 10 time through
            if divmod(count + 1, 10)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

            count += 1

        yield self.txn.commit()
        self.txn = None

        self.output.write("\n\nTotal missing GUIDs: %d\n" % (len(self.missingGUIDs),))
        for guid in sorted(self.missingGUIDs):
            self.output.write("%s\n" % (guid,))

        self.output.write("\nTotal missing Calendars: %d (resources: %d)\n" % (len(self.missingCalendars), totalMissingCalendarResources,))
        for calendar in sorted(self.missingCalendars):
            self.output.write("%s\n" % (calendar,))

        self.output.write("\nTotal missing Resources: %d\n" % (len(self.missingResources),))
        for resource in sorted(self.missingResources):
            self.output.write("%s\n" % (resource,))

    @inlineCallbacks
    def guid2ResourceID(self, guid):
        ch = schema.CALENDAR_HOME
        kwds = {"GUID": guid}
        rows = (yield Select(
            [
                ch.RESOURCE_ID,
            ],
            From=ch,
            Where=(
                ch.OWNER_UID == Parameter("GUID")
            ),
        ).on(self.txn, **kwds))

        returnValue(rows[0][0] if rows else None)

    @inlineCallbacks
    def calendarsForUser(self, rid):
        cb = schema.CALENDAR_BIND
        kwds = {"RID": rid}
        rows = (yield Select(
            [
                cb.CALENDAR_RESOURCE_NAME,
                cb.CALENDAR_RESOURCE_ID,
            ],
            From=cb,
            Where=(
                cb.CALENDAR_HOME_RESOURCE_ID == Parameter("RID")
            ).And(cb.BIND_MODE == _BIND_MODE_OWN),
        ).on(self.txn, **kwds))

        returnValue(rows)

    @inlineCallbacks
    def resourcesForCalendar(self, rid):
        co = schema.CALENDAR_OBJECT
        kwds = {"RID": rid}
        rows = (yield Select(
            [
                co.RESOURCE_NAME,
            ],
            From=co,
            Where=(
                co.CALENDAR_RESOURCE_ID == Parameter("RID")
            ),
        ).on(self.txn, **kwds))

        returnValue(rows)

    def stopService(self):
        """
        Stop the service.  Nothing to do; everything should be finished by this
        time.
        """


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    """
    Do the export.
    """
    if reactor is None:
        from twisted.internet import reactor
    options = MigrateVerifyOptions()
    options.parseOptions(argv[1:])
    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)

    def makeService(store):
        from twistedcaldav.config import config
        config.TransactionTimeoutSeconds = 0
        return MigrateVerifyService(store, options, output, reactor, config)

    utilityMain(options['config'], makeService, reactor, verbose=options["debug"])

if __name__ == '__main__':
    main()
