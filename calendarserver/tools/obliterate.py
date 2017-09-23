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
This tool scans wipes out user data without using slow store object apis
that attempt to keep the DB consistent. Instead it assumes facts about the
schema and how the various table data are related. Normally the purge principal
tool should be used to "correctly" remove user data. This is an emergency tool
needed when data has been accidently migrated into the DB but no users actually
have access to it as they are not enabled on the server.
"""

import os
import sys
import time

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.text import wordWrap
from twisted.python.usage import Options

from twext.enterprise.dal.syntax import Parameter, Delete, Select, Union, \
    CompoundComparison, ExpressionSyntax, Count
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
        ObliterateOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = ''.join(
    wordWrap(
        """
        Usage: calendarserver_obliterate [options] [input specifiers]
        """,
        int(os.environ.get('COLUMNS', '80'))
    )
)
description += "\nVersion: %s" % (VERSION,)


class ConfigError(Exception):
    pass


class ObliterateOptions(Options):
    """
    Command-line options for 'calendarserver_obliterate'
    """

    synopsis = description

    optFlags = [
        ['verbose', 'v', "Verbose logging."],
        ['debug', 'D', "Debug logging."],
        ['fix-props', 'p', "Fix orphaned resource properties only."],
        ['dry-run', 'n', "Do not make any changes."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
        ['data', 'd', "./uuids.txt", "Path where list of uuids to obliterate is."],
        ['uuid', 'u', "", "Obliterate this user's data."],
    ]

    def __init__(self):
        super(ObliterateOptions, self).__init__()
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


# Need to patch this in if not present in actual server code
def NotIn(self, subselect):
    # Can't be Select.__contains__ because __contains__ gets __nonzero__
    # called on its result by the 'in' syntax.
    return CompoundComparison(self, 'not in', subselect)

if not hasattr(ExpressionSyntax, "NotIn"):
    ExpressionSyntax.NotIn = NotIn


class ObliterateService(WorkerService, object):
    """
    Service which runs, does its stuff, then stops the reactor.
    """

    def __init__(self, store, options, output, reactor, config):
        super(ObliterateService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config

        self.results = {}
        self.summary = []
        self.totalHomes = 0
        self.totalCalendars = 0
        self.totalResources = 0
        self.attachments = set()

    @inlineCallbacks
    def doWork(self):
        """
        Do the work, stopping the reactor when done.
        """
        self.output.write("\n---- Obliterate version: %s ----\n" % (VERSION,))
        if self.options["dry-run"]:
            self.output.write("---- DRY RUN No Changes Being Made ----\n")

        try:
            if self.options["fix-props"]:
                yield self.obliterateOrphanedProperties()
            else:
                yield self.obliterateUUIDs()

            self.output.close()
        except ConfigError:
            pass
        except:
            log.failure("doWork()")

    @inlineCallbacks
    def obliterateOrphanedProperties(self):
        """
        Obliterate orphaned data in RESOURCE_PROPERTIES table.
        """

        # Get list of distinct resource_property resource_ids to delete
        self.txn = self.store.newTransaction()

        ch = schema.CALENDAR_HOME
        ca = schema.CALENDAR
        co = schema.CALENDAR_OBJECT
        ah = schema.ADDRESSBOOK_HOME
        ao = schema.ADDRESSBOOK_OBJECT
        rp = schema.RESOURCE_PROPERTY

        rows = (yield Select(
            [rp.RESOURCE_ID, ],
            Distinct=True,
            From=rp,
            Where=(rp.RESOURCE_ID.NotIn(
                Select(
                    [ch.RESOURCE_ID],
                    From=ch,
                    SetExpression=Union(
                        Select(
                            [ca.RESOURCE_ID],
                            From=ca,
                            SetExpression=Union(
                                Select(
                                    [co.RESOURCE_ID],
                                    From=co,
                                    SetExpression=Union(
                                        Select(
                                            [ah.RESOURCE_ID],
                                            From=ah,
                                            SetExpression=Union(
                                                Select(
                                                    [ao.RESOURCE_ID],
                                                    From=ao,
                                                ),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ))
        ).on(self.txn))

        if not rows:
            self.output.write("No orphaned resource properties\n")
            returnValue(None)

        resourceIDs = [row[0] for row in rows]
        resourceIDs_len = len(resourceIDs)
        t = time.time()
        for ctr, resourceID in enumerate(resourceIDs):
            self.output.write("%d of %d (%d%%): ResourceID: %s\n" % (
                ctr + 1,
                resourceIDs_len,
                ((ctr + 1) * 100 / resourceIDs_len),
                resourceID,
            ))

            yield self.removePropertiesForResourceID(resourceID)

            # Commit every 10 DELETEs
            if divmod(ctr + 1, 10)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None

        self.output.write("Obliteration time: %.1fs\n" % (time.time() - t,))

    @inlineCallbacks
    def obliterateUUIDs(self):
        """
        Obliterate specified UUIDs.
        """
        if self.options["uuid"]:
            uuids = [self.options["uuid"], ]
        elif self.options["data"]:
            if not os.path.exists(self.options["data"]):
                self.output.write("%s is not a valid file\n" % (self.options["data"],))
                raise ConfigError

            with open(self.options["data"]) as f:
                uuids = f.read().split()
        else:
            self.output.write("One of --data or --uuid must be specified\n")
            raise ConfigError

        t = time.time()
        uuids_len = len(uuids)
        for ctr, uuid in enumerate(uuids):
            self.txn = self.store.newTransaction()
            self.output.write("%d of %d (%d%%): UUID: %s - " % (
                ctr + 1,
                uuids_len,
                ((ctr + 1) * 100 / uuids_len),
                uuid,
            ))
            result = (yield self.processUUID(uuid))
            self.output.write("%s\n" % (result,))
            yield self.txn.commit()
            self.txn = None

        self.output.write("\nTotal Homes: %d\n" % (self.totalHomes,))
        self.output.write("Total Calendars: %d\n" % (self.totalCalendars,))
        self.output.write("Total Resources: %d\n" % (self.totalResources,))
        if self.attachments:
            self.output.write("Attachments removed: %s\n" % (len(self.attachments,)))
            # for attachment in self.attachments:
            #     self.output.write("    %s\n" % (attachment,))
        self.output.write("Obliteration time: %.1fs\n" % (time.time() - t,))

    @inlineCallbacks
    def processUUID(self, uuid):

        # Get the resource-id for the home
        ch = schema.CALENDAR_HOME
        kwds = {"UUID": uuid}
        rows = (yield Select(
            [ch.RESOURCE_ID, ],
            From=ch,
            Where=(
                ch.OWNER_UID == Parameter("UUID")
            ),
        ).on(self.txn, **kwds))

        if not rows:
            returnValue("No home found")
        homeID = rows[0][0]
        self.totalHomes += 1

        # Count resources
        resourceCount = (yield self.countResources(uuid))
        self.totalResources += resourceCount

        # Remove revisions - do before deleting calendars to remove
        # foreign key constraint
        yield self.removeRevisionsForHomeResourceID(homeID)

        # Look at each calendar and unbind/delete-if-owned
        count = (yield self.deleteCalendars(homeID))
        self.totalCalendars += count

        # Remove properties
        yield self.removePropertiesForResourceID(homeID)

        # Remove notifications
        yield self.removeNotificationsForUUID(uuid)

        # Remove attachments
        attachmentCount = (yield self.removeAttachments(homeID))

        # Now remove the home
        yield self.removeHomeForResourceID(homeID)

        returnValue("Home, %d calendars, %d resources%s - deleted" % (
            count,
            resourceCount,
            (", %d attachmensts" % (attachmentCount,)) if attachmentCount else "",
        ))

    @inlineCallbacks
    def countResources(self, uuid):
        ch = schema.CALENDAR_HOME
        cb = schema.CALENDAR_BIND
        co = schema.CALENDAR_OBJECT
        kwds = {"UUID": uuid}
        rows = (yield Select(
            [
                Count(co.RESOURCE_ID),
            ],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN)).join(
                co, type="left", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID)),
            Where=(
                ch.OWNER_UID == Parameter("UUID")
            ),
        ).on(self.txn, **kwds))

        returnValue(rows[0][0] if rows else 0)

    @inlineCallbacks
    def deleteCalendars(self, homeID):

        # Get list of binds and bind mode
        cb = schema.CALENDAR_BIND
        kwds = {"resourceID": homeID}
        rows = (yield Select(
            [cb.CALENDAR_RESOURCE_ID, cb.BIND_MODE, ],
            From=cb,
            Where=(
                cb.CALENDAR_HOME_RESOURCE_ID == Parameter("resourceID")
            ),
        ).on(self.txn, **kwds))
        if not rows:
            returnValue(0)

        for resourceID, mode in rows:
            if mode == _BIND_MODE_OWN:
                yield self.deleteCalendar(resourceID)
            else:
                yield self.deleteBind(homeID, resourceID)

        returnValue(len(rows))

    @inlineCallbacks
    def deleteCalendar(self, resourceID):

        # Need to delete any remaining CALENDAR_OBJECT_REVISIONS entries
        yield self.removeRevisionsForCalendarResourceID(resourceID)

        # Delete the CALENDAR entry (will cascade to CALENDAR_BIND and CALENDAR_OBJECT)
        if not self.options["dry-run"]:
            ca = schema.CALENDAR
            kwds = {
                "ResourceID": resourceID,
            }
            yield Delete(
                From=ca,
                Where=(
                    ca.RESOURCE_ID == Parameter("ResourceID")
                ),
            ).on(self.txn, **kwds)

        # Remove properties
        yield self.removePropertiesForResourceID(resourceID)

    @inlineCallbacks
    def deleteBind(self, homeID, resourceID):
        if not self.options["dry-run"]:
            cb = schema.CALENDAR_BIND
            kwds = {
                "HomeID": homeID,
                "ResourceID": resourceID,
            }
            yield Delete(
                From=cb,
                Where=(
                    (cb.CALENDAR_HOME_RESOURCE_ID == Parameter("HomeID")).And
                    (cb.CALENDAR_RESOURCE_ID == Parameter("ResourceID"))
                ),
            ).on(self.txn, **kwds)

    @inlineCallbacks
    def removeRevisionsForHomeResourceID(self, resourceID):
        if not self.options["dry-run"]:
            rev = schema.CALENDAR_OBJECT_REVISIONS
            kwds = {"ResourceID": resourceID}
            yield Delete(
                From=rev,
                Where=(
                    rev.CALENDAR_HOME_RESOURCE_ID == Parameter("ResourceID")
                ),
            ).on(self.txn, **kwds)

    @inlineCallbacks
    def removeRevisionsForCalendarResourceID(self, resourceID):
        if not self.options["dry-run"]:
            rev = schema.CALENDAR_OBJECT_REVISIONS
            kwds = {"ResourceID": resourceID}
            yield Delete(
                From=rev,
                Where=(
                    rev.CALENDAR_RESOURCE_ID == Parameter("ResourceID")
                ),
            ).on(self.txn, **kwds)

    @inlineCallbacks
    def removePropertiesForResourceID(self, resourceID):
        if not self.options["dry-run"]:
            props = schema.RESOURCE_PROPERTY
            kwds = {"ResourceID": resourceID}
            yield Delete(
                From=props,
                Where=(
                    props.RESOURCE_ID == Parameter("ResourceID")
                ),
            ).on(self.txn, **kwds)

    @inlineCallbacks
    def removeNotificationsForUUID(self, uuid):

        # Get NOTIFICATION_HOME.RESOURCE_ID
        nh = schema.NOTIFICATION_HOME
        kwds = {"UUID": uuid}
        rows = (yield Select(
            [nh.RESOURCE_ID, ],
            From=nh,
            Where=(
                nh.OWNER_UID == Parameter("UUID")
            ),
        ).on(self.txn, **kwds))

        if rows:
            resourceID = rows[0][0]

            # Delete NOTIFICATION rows
            if not self.options["dry-run"]:
                no = schema.NOTIFICATION
                kwds = {"ResourceID": resourceID}
                yield Delete(
                    From=no,
                    Where=(
                        no.NOTIFICATION_HOME_RESOURCE_ID == Parameter("ResourceID")
                    ),
                ).on(self.txn, **kwds)

            # Delete NOTIFICATION_HOME (will cascade to NOTIFICATION_OBJECT_REVISIONS)
            if not self.options["dry-run"]:
                kwds = {"UUID": uuid}
                yield Delete(
                    From=nh,
                    Where=(
                        nh.OWNER_UID == Parameter("UUID")
                    ),
                ).on(self.txn, **kwds)

    @inlineCallbacks
    def removeAttachments(self, resourceID):

        # Get ATTACHMENT paths
        at = schema.ATTACHMENT
        kwds = {"resourceID": resourceID}
        rows = (yield Select(
            [at.PATH, ],
            From=at,
            Where=(
                at.CALENDAR_HOME_RESOURCE_ID == Parameter("resourceID")
            ),
        ).on(self.txn, **kwds))

        if rows:
            self.attachments.update([row[0] for row in rows])

            # Delete ATTACHMENT rows
            if not self.options["dry-run"]:
                at = schema.ATTACHMENT
                kwds = {"resourceID": resourceID}
                yield Delete(
                    From=at,
                    Where=(
                        at.CALENDAR_HOME_RESOURCE_ID == Parameter("resourceID")
                    ),
                ).on(self.txn, **kwds)

        returnValue(len(rows) if rows else 0)

    @inlineCallbacks
    def removeHomeForResourceID(self, resourceID):
        if not self.options["dry-run"]:
            ch = schema.CALENDAR_HOME
            kwds = {"ResourceID": resourceID}
            yield Delete(
                From=ch,
                Where=(
                    ch.RESOURCE_ID == Parameter("ResourceID")
                ),
            ).on(self.txn, **kwds)

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
    options = ObliterateOptions()
    options.parseOptions(argv[1:])
    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)

    def makeService(store):
        from twistedcaldav.config import config
        config.TransactionTimeoutSeconds = 0
        return ObliterateService(store, options, output, reactor, config)

    utilityMain(options['config'], makeService, reactor, verbose=options["debug"])

if __name__ == '__main__':
    main()
