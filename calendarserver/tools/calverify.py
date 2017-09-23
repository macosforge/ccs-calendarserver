#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_calverify -*-
##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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
This tool scans the calendar store to analyze organizer/attendee event
states to verify that the organizer's view of attendee state matches up
with the attendees' views. It can optionally apply a fix to bring the two
views back into line.

In theory the implicit scheduling model should eliminate the possibility
of mismatches, however, because we store separate resources for organizer
and attendee events, there is a possibility of mismatch. This is greatly
lessened via the new transaction model of database changes, but it is
possible there are edge cases or actual implicit processing errors we have
missed. This tool will allow us to track mismatches to help determine these
errors and get them fixed.

Even in the long term if we move to a "single instance" store where the
organizer event resource is the only one we store (with attendee views
derived from that), in a situation where we have server-to-server scheduling
it is possible for mismatches to creep in. In that case having a way to analyze
multiple DBs for inconsistency would be good too.

"""

from __future__ import print_function

from uuid import uuid4
import collections
import itertools
import sys
import time
import traceback

from calendarserver.tools import tables
from calendarserver.tools.cmdline import utilityMain, WorkerService
from pycalendar.datetime import DateTime
from pycalendar.exceptions import ErrorBase
from pycalendar.icalendar import definitions
from pycalendar.icalendar.calendar import Calendar
from pycalendar.period import Period
from pycalendar.timezone import Timezone
from twext.enterprise.dal.syntax import Select, Parameter, Count, Update
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import usage
from twisted.python.usage import Options
from twistedcaldav.datafilters.peruserdata import PerUserDataFilter
from twistedcaldav.dateops import pyCalendarToSQLTimestamp
from twistedcaldav.ical import Component, InvalidICalendarDataError, Property, PERUSER_COMPONENT
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from twistedcaldav.timezones import TimezoneCache
from txdav.caldav.datastore.scheduling.icalsplitter import iCalSplitter
from txdav.caldav.datastore.scheduling.implicit import ImplicitScheduler
from txdav.caldav.datastore.scheduling.itip import iTipGenerator
from txdav.caldav.datastore.sql import CalendarStoreFeatures
from txdav.caldav.datastore.util import normalizationLookup
from txdav.caldav.icalendarstore import ComponentUpdateState
from txdav.common.datastore.sql_tables import schema, _BIND_MODE_OWN
from txdav.common.icommondatastore import InternalDataStoreError
from txdav.who.idirectory import RecordType as CalRecordType, AutoScheduleMode


log = Logger()


# Monkey patch
def new_validRecurrenceIDs(self, doFix=True):

    fixed = []
    unfixed = []

    # Detect invalid occurrences and fix by adding RDATEs for them
    master = self.masterComponent()
    if master is not None:
        # Get the set of all recurrence IDs
        all_rids = set(self.getComponentInstances())
        if None in all_rids:
            all_rids.remove(None)

        # If the master has no recurrence properties treat any other components as invalid
        if master.isRecurring():

            # Remove all EXDATEs with a matching RECURRENCE-ID. Do this before we start
            # processing of valid instances just in case the matching R-ID is also not valid and
            # thus will need RDATE added.
            exdates = {}
            for property in list(master.properties("EXDATE")):
                for exdate in property.value():
                    exdates[exdate.getValue()] = property
            for rid in all_rids:
                if rid in exdates:
                    if doFix:
                        property = exdates[rid]
                        for value in property.value():
                            if value.getValue() == rid:
                                property.value().remove(value)
                                break
                        master.removeProperty(property)
                        if len(property.value()) > 0:
                            master.addProperty(property)
                        del exdates[rid]
                        fixed.append("Removed EXDATE for valid override: %s" % (rid,))
                    else:
                        unfixed.append("EXDATE for valid override: %s" % (rid,))

            # Get the set of all valid recurrence IDs
            valid_rids = self.validInstances(all_rids, ignoreInvalidInstances=True)

            # Get the set of all RDATEs and add those to the valid set
            rdates = []
            for property in master.properties("RDATE"):
                rdates.extend([_rdate.getValue() for _rdate in property.value()])
            valid_rids.update(set(rdates))

            # Remove EXDATEs predating master
            dtstart = master.propertyValue("DTSTART")
            if dtstart is not None:
                for property in list(master.properties("EXDATE")):
                    newValues = []
                    changed = False
                    for exdate in property.value():
                        exdateValue = exdate.getValue()
                        if exdateValue < dtstart:
                            if doFix:
                                fixed.append("Removed earlier EXDATE: %s" % (exdateValue,))
                            else:
                                unfixed.append("EXDATE earlier than master: %s" % (exdateValue,))
                            changed = True
                        else:
                            newValues.append(exdateValue)

                    if changed and doFix:
                        # Remove the property...
                        master.removeProperty(property)
                        if newValues:
                            # ...and add it back only if it still has values
                            property.setValue(newValues)
                            master.addProperty(property)

        else:
            valid_rids = set()

        # Determine the invalid recurrence IDs by set subtraction
        invalid_rids = all_rids - valid_rids

        # Add RDATEs for the invalid ones, or remove any EXDATE.
        for invalid_rid in invalid_rids:
            brokenComponent = self.overriddenComponent(invalid_rid)
            brokenRID = brokenComponent.propertyValue("RECURRENCE-ID")
            if doFix:
                master.addProperty(Property("RDATE", [brokenRID, ]))
                fixed.append(
                    "Added RDATE for invalid occurrence: %s" %
                    (brokenRID,))
            else:
                unfixed.append("Invalid occurrence: %s" % (brokenRID,))

    return fixed, unfixed


def new_hasDuplicateAlarms(self, doFix=False):
    """
    test and optionally remove alarms that have the same ACTION and TRIGGER values in the same component.
    """
    changed = False
    if self.name() in ("VCALENDAR", PERUSER_COMPONENT,):
        for component in self.subcomponents():
            if component.name() in ("VTIMEZONE",):
                continue
            changed = component.hasDuplicateAlarms(doFix) or changed
    else:
        action_trigger = set()
        for component in tuple(self.subcomponents()):
            if component.name() == "VALARM":
                item = (component.propertyValue("ACTION"), component.propertyValue("TRIGGER"),)
                if item in action_trigger:
                    if doFix:
                        self.removeComponent(component)
                    changed = True
                else:
                    action_trigger.add(item)
    return changed

Component.validRecurrenceIDs = new_validRecurrenceIDs
if not hasattr(Component, "maxAlarmCounts"):
    Component.hasDuplicateAlarms = new_hasDuplicateAlarms

VERSION = "13"


def printusage(e=None):
    if e:
        print(e)
        print("")
    try:
        CalVerifyOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = """
Usage: calendarserver_verify_data [options]
Version: %s

This tool scans the calendar store to look for and correct any
problems.

OPTIONS:

Modes of operation:

-h                  : print help and exit.
--ical              : verify iCalendar data.
--mismatch          : verify scheduling state.
--missing           : display orphaned calendar homes - can be used.
                      with either --ical or --mismatch.
--double            : detect double-bookings.
--dark-purge        : purge room/resource events with invalid organizer
--split             : split recurring event
--upgrade           : upgrade iCalendar data.

--nuke PATH|UID|RID : remove specific calendar resources - can
                      only be used by itself. PATH is the full
                      /calendars/__uids__/XXX/YYY/ZZZ.ics object
                      resource path. UID is the iCalendar UID,
                      prefixed with "uid:", of the resources to
                      remove. RID is the SQL DB resource-id.

Options for all modes:

--fix      : changes are only made when this is present.
--config   : caldavd.plist file for the server.
-v         : verbose logging

Options for --ical / --upgrade:

--uuid     : only scan specified calendar homes. Can be a partial GUID
             to scan all GUIDs with that as a prefix.
--uid      : scan only calendar data with the specific iCalendar UID.
--calendar : When scanning calendar homes, only scan calendars with the
             specified calendar resource name.
--path     : Scan the calendar home or calendar identified by the
             specified URI

Options for --mismatch:

--uid      : look for mismatches with the specified iCalendar UID only.
--details  : log extended details on each mismatch.
--tzid     : timezone to adjust details to.

Options for --double:

--uuid     : only scan specified calendar homes. Can be a partial GUID
             to scan all GUIDs with that as a prefix or "*" for all GUIDS
             (that are marked as resources or locations in the directory).
--tzid     : timezone to adjust details to.
--summary  : report only which GUIDs have double-bookings - no details.
--days     : number of days ahead to scan [DEFAULT: 365]

Options for --double:
If none of (--no-organizer, --invalid-organizer, --disabled-organizer) is present, it
will default to (--invalid-organizer, --disabled-organizer).

Options for --dark-purge:

--uuid     : only scan specified calendar homes. Can be a partial GUID
             to scan all GUIDs with that as a prefix or "*" for all GUIDS
             (that are marked as resources or locations in the directory).
--summary  : report only which GUIDs have double-bookings - no details.
--no-organizer       : only detect events without an organizer
--invalid-organizer  : only detect events with an organizer not in the directory
--disabled-organizer : only detect events with an organizer disabled for calendaring

Options for --missing-location:

--uuid     : only scan specified calendar homes. Can be a partial GUID
             to scan all GUIDs with that as a prefix or "*" for all GUIDS
             (that are marked as locations in the directory).
--summary  : report only which GUIDs have bad events - no details.

Options for --split:

--path     : URI path to resource to split.
--rid      : UTC date-time where split occurs (YYYYMMDDTHHMMSSZ).
--summary  : only print a list of recurrences in the resource - no splitting.

CHANGES
v8: Detects ORGANIZER or ATTENDEE properties with mailto: calendar user
    addresses for users that have valid directory records. Fix is to
    replace the value with a urn:x-uid: form.

v9: Detects double-bookings.

v10: Purges data for invalid users.

v11: Allows manual splitting of recurring events.

v12: Fix double-booking false positives caused by timezones-by-reference.

v13: Add new options for --nuke and --ical. Add fix for invalid GEO.

""" % (VERSION,)


def safePercent(x, y, multiplier=100.0):
    return ((multiplier * x) / y) if y else 0


class CalVerifyOptions(Options):
    """
    Command-line options for 'calendarserver_verify_data'
    """

    synopsis = description

    optFlags = [
        ['ical', 'i', "Calendar data check."],
        ['debug', 'D', "Debug logging."],
        ['mismatch', 's', "Detect organizer/attendee mismatches."],
        ['missing', 'm', "Show 'orphaned' homes."],
        ['double', 'd', "Detect double-bookings."],
        ['dark-purge', 'p', "Purge room/resource events with invalid organizer."],
        ['missing-location', 'g', "Room events with missing location."],
        ['split', 'l', "Split an event."],
        ['upgrade', 'u', "Upgrade calendar data."],

        ['fix', 'x', "Fix problems."],
        ['verbose', 'v', "Verbose logging."],
        ['details', 'V', "Detailed logging."],
        ['summary', 'S', "Summary of double-bookings/split."],
        ['tzid', 't', "Timezone to adjust displayed times to."],

        ['no-organizer', '', "Detect dark events without an organizer"],
        ['invalid-organizer', '', "Detect dark events with an organizer not in the directory"],
        ['disabled-organizer', '', "Detect dark events with a disabled organizer"],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
        ['uuid', 'u', "", "Only check this user."],
        ['uid', 'U', "", "Only this event UID."],
        ['calendar', '', "", "Only this calendar resource name."],
        ['nuke', 'e', "", "Remove event given its path."],
        ['days', 'T', "365", "Number of days for scanning events into the future."],
        ['path', '', "", "Split event given its path."],
        ['rid', '', "", "Split date-time."],
    ]

    def __init__(self):
        super(CalVerifyOptions, self).__init__()
        self.outputName = '-'

    def getUsage(self, width=None):
        return ""

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


class CalVerifyService(WorkerService, object):
    """
    Base class for common service behaviors.
    """

    def __init__(self, store, options, output, reactor, config):
        super(CalVerifyService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config
        self._directory = store.directoryService()
        self._principalCollection = self.rootResource().getChild("principals")

        self.cuaCache = {}

        self.results = {}
        self.summary = []
        self.total = 0
        self.totalErrors = None
        self.totalExceptions = None

        TimezoneCache.create()

    def title(self):
        return ""

    @inlineCallbacks
    def doWork(self):
        """
        Do the operation stopping the reactor when done.
        """
        self.output.write("\n---- CalVerify %s version: %s ----\n" % (self.title(), VERSION,))

        try:
            yield self.doAction()
        except Exception as e:
            self.output.write("CalVerify Failure: %s\n" % (str(e),))
            log.failure("CalVerify Failure")
        except:
            self.output.write("CalVerify Failure: unknown exception\n")
            log.failure("CalVerify Failure")
        finally:
            self.output.close()

    def directoryService(self):
        """
        Return the directory service
        """
        return self._directory

    @inlineCallbacks
    def getAllHomeUIDs(self):
        ch = schema.CALENDAR_HOME
        rows = (yield Select(
            [ch.OWNER_UID, ],
            From=ch,
        ).on(self.txn))
        returnValue(tuple([uid[0] for uid in rows]))

    @inlineCallbacks
    def getMatchingHomeUIDs(self, uuid):
        ch = schema.CALENDAR_HOME
        kwds = {"uuid": uuid}
        rows = (yield Select(
            [ch.OWNER_UID, ],
            From=ch,
            Where=(ch.OWNER_UID.StartsWith(Parameter("uuid"))),
        ).on(self.txn, **kwds))
        returnValue(tuple([uid[0] for uid in rows]))

    @inlineCallbacks
    def countHomeContents(self, uid):
        ch = schema.CALENDAR_HOME
        cb = schema.CALENDAR_BIND
        co = schema.CALENDAR_OBJECT
        kwds = {"UID": uid}
        rows = (yield Select(
            [Count(co.RESOURCE_ID), ],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID)),
            Where=(ch.OWNER_UID == Parameter("UID"))
        ).on(self.txn, **kwds))
        returnValue(int(rows[0][0]) if rows else 0)

    @inlineCallbacks
    def getAllResourceInfo(self, inbox=False):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME

        if inbox:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN)
        else:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME != "inbox")

        kwds = {}
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=cojoin),
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoWithUUID(self, uuid, inbox=False, calendar=None):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME

        if calendar:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME == calendar)
        elif inbox:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN)
        else:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME != "inbox")

        kwds = {"uuid": uuid}
        if len(uuid) != 36:
            where = (ch.OWNER_UID.StartsWith(Parameter("uuid")))
        else:
            where = (ch.OWNER_UID == Parameter("uuid"))
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=cojoin),
            Where=where,
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoTimeRange(self, start):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME
        tr = schema.TIME_RANGE
        kwds = {
            "Start": pyCalendarToSQLTimestamp(start),
            "Max": pyCalendarToSQLTimestamp(DateTime(1900, 1, 1, 0, 0, 0))
        }
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN).And(
                    cb.CALENDAR_RESOURCE_NAME != "inbox").And(
                    co.ORGANIZER != "")).join(
                tr, type="left", on=(co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID)),
            Where=(tr.START_DATE >= Parameter("Start")).Or(co.RECURRANCE_MAX <= Parameter("Start")),
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoWithUID(self, uid, inbox=False):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME

        if inbox:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN)
        else:
            cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME != "inbox")

        kwds = {
            "UID": uid,
        }
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=cojoin),
            Where=(co.ICALENDAR_UID == Parameter("UID")),
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoTimeRangeWithUUID(self, start, uuid):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME
        tr = schema.TIME_RANGE
        kwds = {
            "Start": pyCalendarToSQLTimestamp(start),
            "Max": pyCalendarToSQLTimestamp(DateTime(1900, 1, 1, 0, 0, 0)),
            "UUID": uuid,
        }
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN).And(
                    cb.CALENDAR_RESOURCE_NAME != "inbox")).join(
                tr, type="left", on=(co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID)),
            Where=(ch.OWNER_UID == Parameter("UUID")).And((tr.START_DATE >= Parameter("Start")).Or(co.RECURRANCE_MAX <= Parameter("Start"))),
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoTimeRangeWithUUIDForAllUID(self, start, uuid):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME
        tr = schema.TIME_RANGE

        cojoin = (cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
            cb.BIND_MODE == _BIND_MODE_OWN).And(
            cb.CALENDAR_RESOURCE_NAME != "inbox")

        kwds = {
            "Start": pyCalendarToSQLTimestamp(start),
            "Max": pyCalendarToSQLTimestamp(DateTime(1900, 1, 1, 0, 0, 0)),
            "UUID": uuid,
        }
        rows = (yield Select(
            [ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=cojoin),
            Where=(co.ICALENDAR_UID.In(Select(
                [co.ICALENDAR_UID],
                From=ch.join(
                    cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                    co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                        cb.BIND_MODE == _BIND_MODE_OWN).And(
                        cb.CALENDAR_RESOURCE_NAME != "inbox").And(
                        co.ORGANIZER != "")).join(
                    tr, type="left", on=(co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID)),
                Where=(ch.OWNER_UID == Parameter("UUID")).And((tr.START_DATE >= Parameter("Start")).Or(co.RECURRANCE_MAX <= Parameter("Start"))),
                GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
            ))),
            GroupBy=(ch.OWNER_UID, co.RESOURCE_ID, co.ICALENDAR_UID, cb.CALENDAR_RESOURCE_NAME, co.MD5, co.ORGANIZER, co.CREATED, co.MODIFIED,),
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def getAllResourceInfoForResourceID(self, resid):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME
        kwds = {"resid": resid}
        rows = (yield Select(
            [ch.RESOURCE_ID, cb.CALENDAR_RESOURCE_ID, ],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN)),
            Where=(co.RESOURCE_ID == Parameter("resid")),
        ).on(self.txn, **kwds))
        returnValue(rows[0])

    @inlineCallbacks
    def getResourceID(self, home, calendar, resource):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME

        kwds = {
            "home": home,
            "calendar": calendar,
            "resource": resource,
        }
        rows = (yield Select(
            [co.RESOURCE_ID],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID)),
            Where=(ch.OWNER_UID == Parameter("home")).And(
                cb.CALENDAR_RESOURCE_NAME == Parameter("calendar")).And(
                co.RESOURCE_NAME == Parameter("resource")
            ),
        ).on(self.txn, **kwds))
        returnValue(rows[0][0] if rows else None)

    @inlineCallbacks
    def getCalendar(self, resid, doFix=False):
        self.parseError = None
        self.parseException = None
        co = schema.CALENDAR_OBJECT
        kwds = {"ResourceID": resid}
        rows = (yield Select(
            [co.ICALENDAR_TEXT],
            From=co,
            Where=(
                co.RESOURCE_ID == Parameter("ResourceID")
            ),
        ).on(self.txn, **kwds))
        try:
            caldata = Calendar.parseText(rows[0][0]) if rows else None
        except ErrorBase as e:
            self.parseError = "Failed to parse"
            self.parseException = str(e)
            returnValue(None)

        returnValue(caldata)

    @inlineCallbacks
    def getCalendarForOwnerByUID(self, owner, uid):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME

        kwds = {"OWNER": owner, "UID": uid}
        rows = (yield Select(
            [co.ICALENDAR_TEXT, co.RESOURCE_ID, co.CREATED, co.MODIFIED, ],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN).And(
                    cb.CALENDAR_RESOURCE_NAME != "inbox")),
            Where=(ch.OWNER_UID == Parameter("OWNER")).And(co.ICALENDAR_UID == Parameter("UID")),
        ).on(self.txn, **kwds))

        try:
            caldata = Calendar.parseText(rows[0][0]) if rows else None
        except ErrorBase:
            returnValue((None, None, None, None,))

        returnValue((caldata, rows[0][1], rows[0][2], rows[0][3],) if rows else (None, None, None, None,))

    @inlineCallbacks
    def removeEvent(self, resid):
        """
        Remove the calendar resource specified by resid - this is a force remove - no implicit
        scheduling is required so we use store apis directly.
        """

        try:
            homeID, calendarID = yield self.getAllResourceInfoForResourceID(resid)
            home = yield self.txn.calendarHomeWithResourceID(homeID)
            calendar = yield home.childWithID(calendarID)
            calendarObj = yield calendar.objectResourceWithID(resid)
            objname = calendarObj.name()
            yield calendarObj.purge(implicitly=False)
            yield self.txn.commit()
            self.txn = self.store.newTransaction()

            self.results.setdefault("Fix remove", set()).add((home.name(), calendar.name(), objname,))

            returnValue(True)
        except Exception as e:
            print("Failed to remove resource whilst fixing: %d\n%s" % (resid, e,))
            returnValue(False)

    def logResult(self, key, value, total=None):
        self.output.write("%s: %s\n" % (key, value,))
        self.results[key] = value
        self.addToSummary(key, value, total)

    def addToSummary(self, title, count, total=None):
        if total is not None:
            percent = safePercent(count, total),
        else:
            percent = ""
        self.summary.append((title, count, percent))

    def addSummaryBreak(self):
        self.summary.append(None)

    def printSummary(self):
        # Print summary of results
        table = tables.Table()
        table.addHeader(("Item", "Count", "%"))
        table.setDefaultColumnFormats(
            (
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
                tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
                tables.Table.ColumnFormat("%.1f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            )
        )
        for item in self.summary:
            table.addRow(item)

        if self.totalErrors is not None:
            table.addRow(None)
            table.addRow(("Total Errors", self.totalErrors, safePercent(self.totalErrors, self.total),))

        self.output.write("\n")
        self.output.write("Overall Summary:\n")
        table.printTable(os=self.output)


class NukeService(CalVerifyService):
    """
    Service which removes specific events.
    """

    def title(self):
        return "Nuke Service"

    @inlineCallbacks
    def doAction(self):
        """
        Remove a resource using either its path or resource id. When doing this do not
        read the iCalendar data which may be corrupt.
        """

        self.output.write("\n---- Removing calendar resource ----\n")
        self.txn = self.store.newTransaction()

        nuke = self.options["nuke"]
        if nuke.startswith("/calendars/__uids__/"):
            pathbits = nuke.split("/")
            if len(pathbits) != 6:
                printusage("Not a valid calendar object resource path: %s" % (nuke,))
            homeName = pathbits[3]
            calendarName = pathbits[4]
            resourceName = pathbits[5]

            rid = yield self.getResourceID(homeName, calendarName, resourceName)
            if rid is None:
                yield self.txn.commit()
                self.txn = None
                self.output.write("\n")
                self.output.write("Path does not exist. Nothing nuked.\n")
                returnValue(None)
            rids = [(int(rid), homeName, calendarName,)]
        elif nuke.startswith("uid:"):
            uid = nuke[4:]
            rows = yield self.getAllResourceInfoWithUID(uid, inbox=True)
            rids = []
            for owner, resid, _ignore_uid, calname, _ignore_md5, _ignore_organizer, _ignore_created, _ignore_modified in rows:
                rids.append((resid, owner, calname,))
        else:
            try:
                rids = [(int(nuke), "-", "-",)]
            except ValueError:
                printusage("nuke argument must be a calendar object path or an SQL resource-id")

        self.output.write("\nNumber of resources: %d\n\n" % (len(rids),))
        for rid, owner, calname in rids:
            if self.options["fix"]:
                result = yield self.removeEvent(rid)
                if result:
                    self.output.write("Removed resource: %s. Owner: %s. Calendar: %s\n" % (rid, owner, calname))
            else:
                self.output.write("Resource: %s. Owner: %s. Calendar: %s\n" % (rid, owner, calname))
        yield self.txn.commit()
        self.txn = None


class OrphansService(CalVerifyService):
    """
    Service which detects orphaned calendar homes.
    """

    def title(self):
        return "Orphans Service"

    @inlineCallbacks
    def doAction(self):
        """
        Report on home collections for which there are no directory records, or record is for user on
        a different pod, or a user not enabled for calendaring.
        """
        self.output.write("\n---- Finding calendar homes with missing or disabled directory records ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()
        uids = yield self.getAllHomeUIDs()
        if self.options["verbose"]:
            self.output.write("getAllHomeUIDs time: %.1fs\n" % (time.time() - t,))
        missing = []
        wrong_server = []
        disabled = []
        uids_len = len(uids)
        uids_div = 1 if uids_len < 100 else uids_len / 100
        self.addToSummary("Total Homes", uids_len)

        for ctr, uid in enumerate(uids):
            if self.options["verbose"] and divmod(ctr, uids_div)[1] == 0:
                self.output.write(("\r%d of %d (%d%%)" % (
                    ctr + 1,
                    uids_len,
                    ((ctr + 1) * 100 / uids_len),
                )).ljust(80))
                self.output.flush()

            record = yield self.directoryService().recordWithUID(uid)
            if record is None:
                contents = yield self.countHomeContents(uid)
                missing.append((uid, contents,))
            elif not record.thisServer():
                contents = yield self.countHomeContents(uid)
                wrong_server.append((uid, contents,))
            elif not record.hasCalendars:
                contents = yield self.countHomeContents(uid)
                disabled.append((uid, contents,))

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(ctr, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"]:
            self.output.write("\r".ljust(80) + "\n")

        # Print table of results
        table = tables.Table()
        table.addHeader(("Owner UID", "Calendar Objects"))
        for uid, count in sorted(missing, key=lambda x: x[0]):
            table.addRow((
                uid,
                count,
            ))

        self.output.write("\n")
        self.logResult("Homes without a matching directory record", len(missing), uids_len)
        table.printTable(os=self.output)

        # Print table of results
        table = tables.Table()
        table.addHeader(("Owner UID", "Calendar Objects"))
        for uid, count in sorted(wrong_server, key=lambda x: x[0]):
            record = yield self.directoryService().recordWithUID(uid)
            table.addRow((
                "%s/%s (%s)" % (record.recordType if record else "-", record.shortNames[0] if record else "-", uid,),
                count,
            ))

        self.output.write("\n")
        self.logResult("Homes not hosted on this server", len(wrong_server), uids_len)
        table.printTable(os=self.output)

        # Print table of results
        table = tables.Table()
        table.addHeader(("Owner UID", "Calendar Objects"))
        for uid, count in sorted(disabled, key=lambda x: x[0]):
            record = yield self.directoryService().recordWithUID(uid)
            table.addRow((
                "%s/%s (%s)" % (record.recordType if record else "-", record.shortNames[0] if record else "-", uid,),
                count,
            ))

        self.output.write("\n")
        self.logResult("Homes without an enabled directory record", len(disabled), uids_len)
        table.printTable(os=self.output)

        self.printSummary()


class BadDataService(CalVerifyService):
    """
    Service which scans for bad calendar data.
    """

    def title(self):
        return "Bad Data Service"

    @inlineCallbacks
    def doAction(self):

        self.output.write("\n---- Scanning calendar data ----\n")

        self.now = DateTime.getNowUTC()
        self.start = DateTime.getToday()
        self.start.setDateOnly(False)
        self.end = self.start.duplicate()
        self.end.offsetYear(1)
        self.fix = self.options["fix"]

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")

        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()
        descriptor = None
        if self.options["uuid"]:
            rows = yield self.getAllResourceInfoWithUUID(self.options["uuid"], inbox=True, calendar=self.options["calendar"])
            descriptor = "getAllResourceInfoWithUUID"
        elif self.options["uid"]:
            rows = yield self.getAllResourceInfoWithUID(self.options["uid"], inbox=True)
            descriptor = "getAllResourceInfoWithUID"
        elif self.options["path"]:
            segments = self.options["path"].strip("/").split("/")
            if len(segments) not in (4, 3) or segments[0] != "calendars" or segments[1] != "__uids__":
                self.output.write("Invalid path for bad data scan\n")
                returnValue(None)
            else:
                uuid = segments[2]
                calendar = segments[3] if len(segments) == 4 else None
            rows = yield self.getAllResourceInfoWithUUID(uuid, inbox=True, calendar=calendar)
            descriptor = "getAllResourceInfoWithUUID"
        else:
            rows = yield self.getAllResourceInfo(inbox=True)
            descriptor = "getAllResourceInfo"

        yield self.txn.commit()
        self.txn = None

        if self.options["verbose"]:
            self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))

        self.total = len(rows)
        self.logResult("Number of events to process", self.total)
        self.addSummaryBreak()

        yield self.calendarDataCheck(rows)

        self.printSummary()

    @inlineCallbacks
    def calendarDataCheck(self, rows):
        """
        Check each calendar resource for valid iCalendar data.
        """

        self.output.write("\n---- Verifying each calendar object resource ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()

        results_bad = []
        count = 0
        total = len(rows)
        badlen = 0
        rjust = 10
        for owner, resid, uid, calname, _ignore_md5, _ignore_organizer, _ignore_created, _ignore_modified in rows:
            try:
                result, message = yield self.validCalendarData(resid, calname == "inbox")
            except Exception as e:
                result = False
                message = "Exception for validCalendarData"
                if self.options["verbose"]:
                    print(e)
            if not result:
                results_bad.append((owner, uid, resid, message))
                badlen += 1
            count += 1
            if self.options["verbose"]:
                if count == 1:
                    self.output.write("Bad".rjust(rjust) + "Current".rjust(rjust) + "Total".rjust(rjust) + "Complete".rjust(rjust) + "\n")
                    last_output = time.time()
                if divmod(count, 100)[1] == 0 or time.time() - last_output > 1:
                    self.output.write((
                        "\r" +
                        ("%s" % badlen).rjust(rjust) +
                        ("%s" % count).rjust(rjust) +
                        ("%s" % total).rjust(rjust) +
                        ("%d%%" % safePercent(count, total)).rjust(rjust) +
                        " " + calname + " " + str(resid)
                    ).ljust(80))
                    self.output.flush()
                    last_output = time.time()

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(count, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"]:
            self.output.write((
                "\r" +
                ("%s" % badlen).rjust(rjust) +
                ("%s" % count).rjust(rjust) +
                ("%s" % total).rjust(rjust) +
                ("%d%%" % safePercent(count, total)).rjust(rjust)
            ).ljust(80) + "\n")

        # Print table of results
        table = tables.Table()
        table.addHeader(("Owner", "Event UID", "RID", "Problem",))
        for item in sorted(results_bad, key=lambda x: (x[0], x[1])):
            owner, uid, resid, message = item
            owner_record = yield self.directoryService().recordWithUID(owner)
            table.addRow((
                "%s/%s (%s)" % (owner_record.recordType if owner_record else "-", owner_record.shortNames[0] if owner_record else "-", owner,),
                uid,
                resid,
                message,
            ))

        self.output.write("\n")
        self.logResult("Bad iCalendar data", len(results_bad), total)
        self.results["Bad iCalendar data"] = results_bad
        table.printTable(os=self.output)

        if self.options["verbose"]:
            diff_time = time.time() - t
            self.output.write("Time: %.2f s  Average: %.1f ms/resource\n" % (
                diff_time,
                safePercent(diff_time, total, 1000.0),
            ))

    errorPrefix = "Calendar data had unfixable problems:\n  "

    @inlineCallbacks
    def validCalendarData(self, resid, isinbox):
        """
        Check the calendar resource for valid iCalendar data.
        """

        result = True
        message = ""

        caldata = yield self.getCalendar(resid, self.fix)

        # Look for possible GEO fix
        if caldata is None and self.parseError and "GEO value incorrect" in self.parseException:
            if self.fix:
                caldata = yield self.fixInvalidGEOProperty(resid)
                if caldata is not None:
                    result = False
                    message = "Fixed: Removed bad GEO"
            else:
                self.parseError += ": GEO"

        if caldata is None:
            if self.parseError:
                returnValue((False, self.parseError))
            else:
                returnValue((True, "Nothing to scan"))

        component = Component(None, pycalendar=caldata)
        if getattr(self.config, "MaxInstancesForRRULE", 0):
            component.truncateRecurrence(self.config.MaxInstancesForRRULE)
        try:
            if self.options["ical"]:
                component.validCalendarData(doFix=False, validateRecurrences=True)
                component.validCalendarForCalDAV(methodAllowed=isinbox)
                component.validOrganizerForScheduling(doFix=False)
                if component.hasDuplicateAlarms(doFix=False):
                    raise InvalidICalendarDataError("Duplicate VALARMS")
            yield self.noPrincipalPathCUAddresses(component, doFix=False)
            if self.options["ical"]:
                self.attendeesWithoutOrganizer(component, doFix=False)

        except ValueError as e:
            result = False
            message = str(e)
            if message.startswith(self.errorPrefix):
                message = message[len(self.errorPrefix):]
            lines = message.splitlines()
            message = lines[0] + (" ++" if len(lines) > 1 else "")
            if self.fix:
                fixresult, fixmessage = yield self.fixCalendarData(resid, isinbox)
                if fixresult:
                    message = "Fixed: " + message
                else:
                    message = fixmessage + message

        returnValue((result, message,))

    @inlineCallbacks
    def noPrincipalPathCUAddresses(self, component, doFix):

        @inlineCallbacks
        def recordWithCalendarUserAddress(address):
            principal = yield self._principalCollection.principalForCalendarUserAddress(address)
            returnValue(principal.record)

        @inlineCallbacks
        def lookupFunction(cuaddr, recordFunction, conf):

            # Return cached results, if any.add
            if cuaddr in self.cuaCache:
                returnValue(self.cuaCache[cuaddr])

            result = yield normalizationLookup(cuaddr, recordFunction, conf)
            _ignore_name, guid, _ignore_cutype, _ignore_cuaddrs = result
            if guid is None:
                if cuaddr.find("__uids__") != -1:
                    guid = cuaddr[cuaddr.find("__uids__/") + 9:][:36]
                    result = ("", guid, "", set(),)

            # Cache the result
            self.cuaCache[cuaddr] = result
            returnValue(result)

        for subcomponent in component.subcomponents(ignore=True):
            organizer = subcomponent.getProperty("ORGANIZER")
            if organizer:
                cuaddr = organizer.value()

                # http(s) principals need to be converted to urn:uuid
                if cuaddr.startswith("http"):
                    if doFix:
                        yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)
                    else:
                        raise InvalidICalendarDataError("iCalendar ORGANIZER starts with 'http(s)'")
                elif cuaddr.startswith("mailto:"):
                    if (yield lookupFunction(cuaddr, recordWithCalendarUserAddress, self.config))[1] is not None:
                        if doFix:
                            yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)
                        else:
                            raise InvalidICalendarDataError("iCalendar ORGANIZER starts with 'mailto:' and record exists")
                else:
                    if ("@" in cuaddr) and (":" not in cuaddr) and ("/" not in cuaddr):
                        if doFix:
                            # Add back in mailto: then re-normalize to urn:uuid if possible
                            organizer.setValue("mailto:%s" % (cuaddr,))
                            yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)

                            # Remove any SCHEDULE-AGENT=NONE
                            if organizer.parameterValue("SCHEDULE-AGENT", "SERVER") == "NONE":
                                organizer.removeParameter("SCHEDULE-AGENT")
                        else:
                            raise InvalidICalendarDataError("iCalendar ORGANIZER missing mailto:")

            for attendee in subcomponent.properties("ATTENDEE"):
                cuaddr = attendee.value()

                # http(s) principals need to be converted to urn:uuid
                if cuaddr.startswith("http"):
                    if doFix:
                        yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)
                    else:
                        raise InvalidICalendarDataError("iCalendar ATTENDEE starts with 'http(s)'")
                elif cuaddr.startswith("mailto:"):
                    if (yield lookupFunction(cuaddr, recordWithCalendarUserAddress, self.config))[1] is not None:
                        if doFix:
                            yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)
                        else:
                            raise InvalidICalendarDataError("iCalendar ATTENDEE starts with 'mailto:' and record exists")
                else:
                    if ("@" in cuaddr) and (":" not in cuaddr) and ("/" not in cuaddr):
                        if doFix:
                            # Add back in mailto: then re-normalize to urn:uuid if possible
                            attendee.setValue("mailto:%s" % (cuaddr,))
                            yield component.normalizeCalendarUserAddresses(lookupFunction, recordWithCalendarUserAddress)
                        else:
                            raise InvalidICalendarDataError("iCalendar ATTENDEE missing mailto:")

    def attendeesWithoutOrganizer(self, component, doFix):
        """
        Look for events with ATTENDEE properties and no ORGANIZER property.
        """

        organizer = component.getOrganizer()
        attendees = component.getAttendees()
        if organizer is None and attendees:
            if doFix:
                raise ValueError("ATTENDEEs without ORGANIZER")
            else:
                raise InvalidICalendarDataError("ATTENDEEs without ORGANIZER")

    @inlineCallbacks
    def fixCalendarData(self, resid, isinbox):
        """
        Fix problems in calendar data using store APIs.
        """

        homeID, calendarID = yield self.getAllResourceInfoForResourceID(resid)
        home = yield self.txn.calendarHomeWithResourceID(homeID)
        calendar = yield home.childWithID(calendarID)
        calendarObj = yield calendar.objectResourceWithID(resid)

        try:
            component = yield calendarObj.component()
        except InternalDataStoreError:
            returnValue((False, "Failed parse: "))

        result = True
        message = ""
        try:
            if self.options["ical"]:
                component.validCalendarData(doFix=True, validateRecurrences=True)
                component.validCalendarForCalDAV(methodAllowed=isinbox)
                component.validOrganizerForScheduling(doFix=True)
                component.hasDuplicateAlarms(doFix=True)
            yield self.noPrincipalPathCUAddresses(component, doFix=True)
            if self.options["ical"]:
                self.attendeesWithoutOrganizer(component, doFix=True)
        except ValueError:
            result = False
            message = "Failed fix: "

        if result:
            # Write out fix, commit and get a new transaction
            try:
                # Use _migrating to ignore possible overridden instance errors - we are either correcting or ignoring those
                self.txn._migrating = True
                component = yield calendarObj._setComponentInternal(component, internal_state=ComponentUpdateState.RAW)
            except Exception as e:
                print(e, component)
                print(traceback.print_exc())
                result = False
                message = "Exception fix: "
            yield self.txn.commit()
            self.txn = self.store.newTransaction()

        returnValue((result, message,))

    @inlineCallbacks
    def fixInvalidGEOProperty(self, resid, doFix=False):
        self.parseError = None
        self.parseException = None
        co = schema.CALENDAR_OBJECT
        kwds = {"ResourceID": resid}
        rows = (yield Select(
            [co.ICALENDAR_TEXT],
            From=co,
            Where=(
                co.RESOURCE_ID == Parameter("ResourceID")
            ),
        ).on(self.txn, **kwds))
        caltextdata = rows[0][0] if rows else None
        if caltextdata is None:
            returnValue(None)
        caltextdata = "\n".join(filter(lambda x: not (x.startswith("GEO:") and ";" not in x), caltextdata.splitlines()))

        try:
            caldata = Calendar.parseText(caltextdata)
        except ErrorBase as e:
            self.parseError = "Failed to parse"
            self.parseException = str(e)
            returnValue(None)

        # We now know it is valid, so write it back
        yield Update(
            {co.ICALENDAR_TEXT: caltextdata},
            Where=co.RESOURCE_ID == Parameter("ResourceID")
        ).on(self.txn, **kwds)

        returnValue(caldata)


class SchedulingMismatchService(CalVerifyService):
    """
    Service which detects mismatched scheduled events.
    """

    metadata = {
        "accessMode": "PUBLIC",
        "isScheduleObject": True,
        "scheduleTag": "abc",
        "scheduleEtags": (),
        "hasPrivateComment": False,
    }

    metadata_inbox = {
        "accessMode": "PUBLIC",
        "isScheduleObject": False,
        "scheduleTag": "",
        "scheduleEtags": (),
        "hasPrivateComment": False,
    }

    def __init__(self, store, options, output, reactor, config):
        super(SchedulingMismatchService, self).__init__(store, options, output, reactor, config)

        self.validForCalendaringUUIDs = {}

        self.fixAttendeesForOrganizerMissing = 0
        self.fixAttendeesForOrganizerMismatch = 0
        self.fixOrganizersForAttendeeMissing = 0
        self.fixOrganizersForAttendeeMismatch = 0
        self.fixFailed = 0
        self.fixedAutoAccepts = []

    def title(self):
        return "Scheduling Mismatch Service"

    @inlineCallbacks
    def doAction(self):

        self.output.write("\n---- Scanning calendar data ----\n")

        self.now = DateTime.getNowUTC()
        self.start = self.options["start"] if "start" in self.options else DateTime.getToday()
        self.start.setDateOnly(False)
        self.end = self.start.duplicate()
        self.end.offsetYear(1)
        self.fix = self.options["fix"]

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")

        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()
        descriptor = None
        if self.options["uid"]:
            rows = yield self.getAllResourceInfoWithUID(self.options["uid"])
            descriptor = "getAllResourceInfoWithUID"
        elif self.options["uuid"]:
            rows = yield self.getAllResourceInfoTimeRangeWithUUIDForAllUID(self.start, self.options["uuid"])
            descriptor = "getAllResourceInfoTimeRangeWithUUIDForAllUID"
            self.options["uuid"] = None
        else:
            rows = yield self.getAllResourceInfoTimeRange(self.start)
            descriptor = "getAllResourceInfoTimeRange"

        yield self.txn.commit()
        self.txn = None

        if self.options["verbose"]:
            self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))

        self.total = len(rows)
        self.logResult("Number of events to process", self.total)

        # Split into organizer events and attendee events
        self.organized = []
        self.organized_byuid = {}
        self.attended = []
        self.attended_byuid = collections.defaultdict(list)
        self.matched_attendee_to_organizer = collections.defaultdict(set)
        skipped, inboxes = yield self.buildResourceInfo(rows)

        self.logResult("Number of organizer events to process", len(self.organized), self.total)
        self.logResult("Number of attendee events to process", len(self.attended), self.total)
        self.logResult("Number of skipped events", skipped, self.total)
        self.logResult("Number of inbox events", inboxes)
        self.addSummaryBreak()

        self.totalErrors = 0
        yield self.verifyAllAttendeesForOrganizer()
        yield self.verifyAllOrganizersForAttendee()

        # Need to add fix summary information
        if self.fix:
            self.addSummaryBreak()
            self.logResult("Fixed missing attendee events", self.fixAttendeesForOrganizerMissing)
            self.logResult("Fixed mismatched attendee events", self.fixAttendeesForOrganizerMismatch)
            self.logResult("Fixed missing organizer events", self.fixOrganizersForAttendeeMissing)
            self.logResult("Fixed mismatched organizer events", self.fixOrganizersForAttendeeMismatch)
            self.logResult("Fix failures", self.fixFailed)
            self.logResult("Fixed Auto-Accepts", len(self.fixedAutoAccepts))
            self.results["Auto-Accepts"] = self.fixedAutoAccepts

            self.printAutoAccepts()

        self.printSummary()

    @inlineCallbacks
    def buildResourceInfo(self, rows, onlyOrganizer=False, onlyAttendee=False):
        """
        For each resource, determine whether it is an organizer or attendee event, and also
        cache the attendee partstats.

        @param rows: set of DB query rows
        @type rows: C{list}
        @param onlyOrganizer: whether organizer information only is required
        @type onlyOrganizer: C{bool}
        @param onlyAttendee: whether attendee information only is required
        @type onlyAttendee: C{bool}
        """

        skipped = 0
        inboxes = 0
        for owner, resid, uid, calname, md5, organizer, created, modified in rows:

            # Skip owners not enabled for calendaring
            if not (yield self.testForCalendaringUUID(owner)):
                skipped += 1
                continue

            # Skip inboxes
            if calname == "inbox":
                inboxes += 1
                continue

            # If targeting a specific organizer, skip events belonging to others
            if self.options["uuid"]:
                if not organizer.startswith("urn:x-uid:") or self.options["uuid"] != organizer[10:]:
                    continue

            # Cache organizer/attendee states
            if organizer.startswith("urn:x-uid:") and owner == organizer[10:]:
                if not onlyAttendee:
                    self.organized.append((owner, resid, uid, md5, organizer, created, modified,))
                    self.organized_byuid[uid] = (owner, resid, uid, md5, organizer, created, modified,)
            else:
                if not onlyOrganizer:
                    self.attended.append((owner, resid, uid, md5, organizer, created, modified,))
                    self.attended_byuid[uid].append((owner, resid, uid, md5, organizer, created, modified,))

        returnValue((skipped, inboxes))

    @inlineCallbacks
    def testForCalendaringUUID(self, uuid):
        """
        Determine if the specified directory UUID is valid for calendaring. Keep a cache of
        valid and invalid so we can do this quickly.

        @param uuid: the directory UUID to test
        @type uuid: C{str}

        @return: C{True} if valid, C{False} if not
        """

        if uuid not in self.validForCalendaringUUIDs:
            record = yield self.directoryService().recordWithUID(uuid)
            self.validForCalendaringUUIDs[uuid] = record is not None and record.hasCalendars and record.thisServer()
        returnValue(self.validForCalendaringUUIDs[uuid])

    @inlineCallbacks
    def verifyAllAttendeesForOrganizer(self):
        """
        Make sure that for each organizer, each referenced attendee has a consistent view of the organizer's event.
        We will look for events that an organizer has and are missing for the attendee, and events that an organizer's
        view of attendee status does not match the attendee's view of their own status.
        """

        self.output.write("\n---- Verifying Organizer events against Attendee copies ----\n")
        self.txn = self.store.newTransaction()

        results_missing = []
        results_mismatch = []
        attendeeResIDs = {}
        organized_len = len(self.organized)
        organizer_div = 1 if organized_len < 100 else organized_len / 100

        # Test organized events
        t = time.time()
        for ctr, organizerEvent in enumerate(self.organized):
            if self.options["verbose"] and divmod(ctr, organizer_div)[1] == 0:
                self.output.write(("\r%d of %d (%d%%) Missing: %d  Mismatched: %s" % (
                    ctr + 1,
                    organized_len,
                    ((ctr + 1) * 100 / organized_len),
                    len(results_missing),
                    len(results_mismatch),
                )).ljust(80))
                self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 10 seconds
            if time.time() - t > 10:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()
                t = time.time()

            # Get the organizer's view of attendee states
            organizer, resid, uid, _ignore_md5, _ignore_organizer, org_created, org_modified = organizerEvent
            calendar = yield self.getCalendar(resid)
            if calendar is None:
                continue
            if self.options["verbose"] and self.masterComponent(calendar) is None:
                self.output.write("Missing master for organizer: %s, resid: %s, uid: %s\n" % (organizer, resid, uid,))
            organizerViewOfAttendees = self.buildAttendeeStates(calendar, self.start, self.end)
            try:
                del organizerViewOfAttendees[organizer]
            except KeyError:
                # Odd - the organizer is not an attendee - this usually does not happen
                pass
            if len(organizerViewOfAttendees) == 0:
                continue

            # Get attendee states for matching UID
            eachAttendeesOwnStatus = {}
            attendeeCreatedModified = {}
            for attendeeEvent in self.attended_byuid.get(uid, ()):
                owner, attresid, attuid, _ignore_md5, _ignore_organizer, att_created, att_modified = attendeeEvent
                attendeeCreatedModified[owner] = (att_created, att_modified,)
                calendar = yield self.getCalendar(attresid)
                if calendar is None:
                    continue
                eachAttendeesOwnStatus[owner] = self.buildAttendeeStates(calendar, self.start, self.end, attendee_only=owner)
                attendeeResIDs[(owner, attuid)] = attresid

            # Look at each attendee in the organizer's meeting
            for organizerAttendee, organizerViewOfStatus in organizerViewOfAttendees.iteritems():
                missing = False
                mismatch = False

                self.matched_attendee_to_organizer[uid].add(organizerAttendee)

                # Skip attendees not enabled for calendaring
                if not (yield self.testForCalendaringUUID(organizerAttendee)):
                    continue

                # Double check the missing attendee situation in case we missed it during the original query
                if organizerAttendee not in eachAttendeesOwnStatus:
                    # Try to reload the attendee data
                    calendar, attresid, att_created, att_modified = yield self.getCalendarForOwnerByUID(organizerAttendee, uid)
                    if calendar is not None:
                        eachAttendeesOwnStatus[organizerAttendee] = self.buildAttendeeStates(calendar, self.start, self.end, attendee_only=organizerAttendee)
                        attendeeResIDs[(organizerAttendee, uid)] = attresid
                        attendeeCreatedModified[organizerAttendee] = (att_created, att_modified,)
                        # print("Reloaded missing attendee data")

                # If an entry for the attendee exists, then check whether attendee status matches
                if organizerAttendee in eachAttendeesOwnStatus:
                    attendeeOwnStatus = eachAttendeesOwnStatus[organizerAttendee].get(organizerAttendee, set())
                    att_created, att_modified = attendeeCreatedModified[organizerAttendee]

                    if organizerViewOfStatus != attendeeOwnStatus:
                        # Check that the difference is only cancelled or declined on the organizers side
                        for _organizerInstance, partstat in organizerViewOfStatus.difference(attendeeOwnStatus):
                            if partstat not in ("DECLINED", "CANCELLED"):
                                results_mismatch.append((uid, resid, organizer, org_created, org_modified, organizerAttendee, att_created, att_modified))
                                self.results.setdefault("Mismatch Attendee", set()).add((uid, organizer, organizerAttendee,))
                                mismatch = True
                                if self.options["details"]:
                                    self.output.write("Mismatch: on Organizer's side:\n")
                                    self.output.write("          UID: %s\n" % (uid,))
                                    self.output.write("          Organizer: %s\n" % (organizer,))
                                    self.output.write("          Attendee: %s\n" % (organizerAttendee,))
                                    self.output.write("          Instance: %s\n" % (_organizerInstance,))
                                break
                        # Check that the difference is only cancelled on the attendees side
                        for _attendeeInstance, partstat in attendeeOwnStatus.difference(organizerViewOfStatus):
                            if partstat not in ("CANCELLED",):
                                if not mismatch:
                                    results_mismatch.append((uid, resid, organizer, org_created, org_modified, organizerAttendee, att_created, att_modified))
                                    self.results.setdefault("Mismatch Attendee", set()).add((uid, organizer, organizerAttendee,))
                                mismatch = True
                                if self.options["details"]:
                                    self.output.write("Mismatch: on Attendee's side:\n")
                                    self.output.write("          Organizer: %s\n" % (organizer,))
                                    self.output.write("          Attendee: %s\n" % (organizerAttendee,))
                                    self.output.write("          Instance: %s\n" % (_attendeeInstance,))
                                break

                # Check that the status for this attendee is always declined which means a missing copy of the event is OK
                else:
                    for _ignore_instance_id, partstat in organizerViewOfStatus:
                        if partstat not in ("DECLINED", "CANCELLED"):
                            results_missing.append((uid, resid, organizer, organizerAttendee, org_created, org_modified))
                            self.results.setdefault("Missing Attendee", set()).add((uid, organizer, organizerAttendee,))
                            missing = True
                            break

                # If there was a problem we can fix it
                if (missing or mismatch) and self.fix:
                    fix_result = (yield self.fixByReinvitingAttendee(resid, attendeeResIDs.get((organizerAttendee, uid)), organizerAttendee))
                    if fix_result:
                        if missing:
                            self.fixAttendeesForOrganizerMissing += 1
                        else:
                            self.fixAttendeesForOrganizerMismatch += 1
                    else:
                        self.fixFailed += 1

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"]:
            self.output.write("\r".ljust(80) + "\n")

        # Print table of results
        table = tables.Table()
        table.addHeader(("Organizer", "Attendee", "Event UID", "Organizer RID", "Created", "Modified",))
        results_missing.sort()
        for item in results_missing:
            uid, resid, organizer, attendee, created, modified = item
            organizer_record = yield self.directoryService().recordWithUID(organizer)
            attendee_record = yield self.directoryService().recordWithUID(attendee)
            table.addRow((
                "%s/%s (%s)" % (organizer_record.recordType if organizer_record else "-", organizer_record.shortNames[0] if organizer_record else "-", organizer,),
                "%s/%s (%s)" % (attendee_record.recordType if attendee_record else "-", attendee_record.shortNames[0] if attendee_record else "-", attendee,),
                uid,
                resid,
                created,
                "" if modified == created else modified,
            ))

        self.output.write("\n")
        self.logResult("Events missing from Attendee's calendars", len(results_missing), self.total)
        table.printTable(os=self.output)
        self.totalErrors += len(results_missing)

        # Print table of results
        table = tables.Table()
        table.addHeader(("Organizer", "Attendee", "Event UID", "Organizer RID", "Created", "Modified", "Attendee RID", "Created", "Modified",))
        results_mismatch.sort()
        for item in results_mismatch:
            uid, org_resid, organizer, org_created, org_modified, attendee, att_created, att_modified = item
            organizer_record = yield self.directoryService().recordWithUID(organizer)
            attendee_record = yield self.directoryService().recordWithUID(attendee)
            table.addRow((
                "%s/%s (%s)" % (organizer_record.recordType if organizer_record else "-", organizer_record.shortNames[0] if organizer_record else "-", organizer,),
                "%s/%s (%s)" % (attendee_record.recordType if attendee_record else "-", attendee_record.shortNames[0] if attendee_record else "-", attendee,),
                uid,
                org_resid,
                org_created,
                "" if org_modified == org_created else org_modified,
                attendeeResIDs[(attendee, uid)],
                att_created,
                "" if att_modified == att_created else att_modified,
            ))

        self.output.write("\n")
        self.logResult("Events mismatched between Organizer's and Attendee's calendars", len(results_mismatch), self.total)
        table.printTable(os=self.output)
        self.totalErrors += len(results_mismatch)

    @inlineCallbacks
    def verifyAllOrganizersForAttendee(self):
        """
        Make sure that for each attendee, there is a matching event for the organizer.
        """

        self.output.write("\n---- Verifying Attendee events against Organizer copies ----\n")
        self.txn = self.store.newTransaction()

        # Now try to match up each attendee event
        missing = []
        mismatched = []
        attended_len = len(self.attended)
        attended_div = 1 if attended_len < 100 else attended_len / 100

        t = time.time()
        for ctr, attendeeEvent in enumerate(tuple(self.attended)):  # self.attended might mutate during the loop

            if self.options["verbose"] and divmod(ctr, attended_div)[1] == 0:
                self.output.write(("\r%d of %d (%d%%) Missing: %d  Mismatched: %s" % (
                    ctr + 1,
                    attended_len,
                    ((ctr + 1) * 100 / attended_len),
                    len(missing),
                    len(mismatched),
                )).ljust(80))
                self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 10 seconds
            if time.time() - t > 10:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()
                t = time.time()

            attendee, resid, uid, _ignore_md5, organizer, att_created, att_modified = attendeeEvent
            calendar = yield self.getCalendar(resid)
            if calendar is None:
                continue
            eachAttendeesOwnStatus = self.buildAttendeeStates(calendar, self.start, self.end, attendee_only=attendee)
            if attendee not in eachAttendeesOwnStatus:
                continue

            # Only care about data for hosted organizers
            if not organizer.startswith("urn:x-uid:"):
                continue
            organizer = organizer[10:]

            # Skip organizers not enabled for calendaring
            if not (yield self.testForCalendaringUUID(organizer)):
                continue

            # Double check the missing attendee situation in case we missed it during the original query
            if uid not in self.organized_byuid:
                # Try to reload the organizer info data
                rows = yield self.getAllResourceInfoWithUID(uid)
                yield self.buildResourceInfo(rows, onlyOrganizer=True)

                # if uid in self.organized_byuid:
                #     print("Reloaded missing organizer data: %s" % (uid,))

            if uid not in self.organized_byuid:

                # Check whether attendee has all instances cancelled
                if self.allCancelled(eachAttendeesOwnStatus):
                    continue

                missing.append((uid, attendee, organizer, resid, att_created, att_modified,))
                self.results.setdefault("Missing Organizer", set()).add((uid, attendee, organizer,))

                # If there is a miss we fix by removing the attendee data
                if self.fix:
                    # This is where we attempt a fix
                    fix_result = (yield self.removeEvent(resid))
                    if fix_result:
                        self.fixOrganizersForAttendeeMissing += 1
                    else:
                        self.fixFailed += 1

            elif attendee not in self.matched_attendee_to_organizer[uid]:
                # Check whether attendee has all instances cancelled
                if self.allCancelled(eachAttendeesOwnStatus):
                    continue

                mismatched.append((uid, attendee, organizer, resid, att_created, att_modified,))
                self.results.setdefault("Mismatch Organizer", set()).add((uid, attendee, organizer,))

                # If there is a mismatch we fix by re-inviting the attendee
                if self.fix:
                    fix_result = (yield self.fixByReinvitingAttendee(self.organized_byuid[uid][1], resid, attendee))
                    if fix_result:
                        self.fixOrganizersForAttendeeMismatch += 1
                    else:
                        self.fixFailed += 1

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"]:
            self.output.write("\r".ljust(80) + "\n")

        # Print table of results
        table = tables.Table()
        table.addHeader(("Organizer", "Attendee", "UID", "Attendee RID", "Created", "Modified",))
        missing.sort()
        unique_set = set()
        for item in missing:
            uid, attendee, organizer, resid, created, modified = item
            unique_set.add(uid)
            if organizer:
                organizerRecord = yield self.directoryService().recordWithUID(organizer)
                organizer = "%s/%s (%s)" % (organizerRecord.recordType if organizerRecord else "-", organizerRecord.shortNames[0] if organizerRecord else "-", organizer,)
            attendeeRecord = yield self.directoryService().recordWithUID(attendee)
            table.addRow((
                organizer,
                "%s/%s (%s)" % (attendeeRecord.recordType if attendeeRecord else "-", attendeeRecord.shortNames[0] if attendeeRecord else "-", attendee,),
                uid,
                resid,
                created,
                "" if modified == created else modified,
            ))

        self.output.write("\n")
        self.output.write("Attendee events missing in Organizer's calendar (total=%d, unique=%d):\n" % (len(missing), len(unique_set),))
        table.printTable(os=self.output)
        self.addToSummary("Attendee events missing in Organizer's calendar", len(missing), self.total)
        self.totalErrors += len(missing)

        # Print table of results
        table = tables.Table()
        table.addHeader(("Organizer", "Attendee", "UID", "Organizer RID", "Created", "Modified", "Attendee RID", "Created", "Modified",))
        mismatched.sort()
        for item in mismatched:
            uid, attendee, organizer, resid, att_created, att_modified = item
            if organizer:
                organizerRecord = yield self.directoryService().recordWithUID(organizer)
                organizer = "%s/%s (%s)" % (organizerRecord.recordType if organizerRecord else "-", organizerRecord.shortNames[0] if organizerRecord else "-", organizer,)
            attendeeRecord = yield self.directoryService().recordWithUID(attendee)
            table.addRow((
                organizer,
                "%s/%s (%s)" % (attendeeRecord.recordType if attendeeRecord else "-", attendeeRecord.shortNames[0] if attendeeRecord else "-", attendee,),
                uid,
                self.organized_byuid[uid][1],
                self.organized_byuid[uid][5],
                self.organized_byuid[uid][6],
                resid,
                att_created,
                "" if att_modified == att_created else att_modified,
            ))

        self.output.write("\n")
        self.logResult("Attendee events mismatched in Organizer's calendar", len(mismatched), self.total)
        table.printTable(os=self.output)
        self.totalErrors += len(mismatched)

    @inlineCallbacks
    def fixByReinvitingAttendee(self, orgresid, attresid, attendee):
        """
        Fix a mismatch/missing error by having the organizer send a REQUEST for the entire event to the attendee
        to trigger implicit scheduling to resync the attendee event.

        We do not have implicit apis in the store, but really want to use store-only apis here to avoid having to create
        "fake" HTTP requests and manipulate HTTP resources. So what we will do is emulate implicit behavior by copying the
        organizer resource to the attendee (filtering it for the attendee's view of the event) and deposit an inbox item
        for the same event. Right now that will wipe out any per-attendee data - notably alarms.
        """

        try:
            cuaddr = "urn:x-uid:%s" % attendee

            # Get the organizer's calendar data
            calendar = (yield self.getCalendar(orgresid))
            calendar = Component(None, pycalendar=calendar)

            # Generate an iTip message for the entire event filtered for the attendee's view
            itipmsg = iTipGenerator.generateAttendeeRequest(calendar, (cuaddr,), None)

            # Handle the case where the attendee is not actually in the organizer event at all by
            # removing the attendee event instead of re-inviting
            if itipmsg.resourceUID() is None:
                yield self.removeEvent(attresid)
                returnValue(True)

            # Convert iTip message into actual calendar data - just remove METHOD
            attendee_calendar = itipmsg.duplicate()
            attendee_calendar.removeProperty(attendee_calendar.getProperty("METHOD"))

            # Adjust TRANSP to match PARTSTAT
            self.setTransparencyForAttendee(attendee_calendar, cuaddr)

            # Get attendee home store object
            home = (yield self.txn.calendarHomeWithUID(attendee))
            if home is None:
                raise ValueError("Cannot find home")
            inbox = (yield home.calendarWithName("inbox"))
            if inbox is None:
                raise ValueError("Cannot find inbox")

            details = {}
            # Replace existing resource data, or create a new one
            if attresid:
                # TODO: transfer over per-attendee data - valarms
                _ignore_homeID, calendarID = yield self.getAllResourceInfoForResourceID(attresid)
                calendar = yield home.childWithID(calendarID)
                calendarObj = yield calendar.objectResourceWithID(attresid)
                calendarObj.scheduleTag = str(uuid4())
                yield calendarObj._setComponentInternal(attendee_calendar, internal_state=ComponentUpdateState.RAW)
                self.results.setdefault("Fix change event", set()).add((home.name(), calendar.name(), attendee_calendar.resourceUID(),))

                details["path"] = "/calendars/__uids__/%s/%s/%s" % (home.name(), calendar.name(), calendarObj.name(),)
                details["rid"] = attresid
            else:
                # Find default calendar for VEVENTs
                defaultCalendar = (yield self.defaultCalendarForAttendee(home))
                if defaultCalendar is None:
                    raise ValueError("Cannot find suitable default calendar")
                new_name = str(uuid4()) + ".ics"
                calendarObj = (yield defaultCalendar._createCalendarObjectWithNameInternal(new_name, attendee_calendar, internal_state=ComponentUpdateState.RAW, options=self.metadata))
                self.results.setdefault("Fix add event", set()).add((home.name(), defaultCalendar.name(), attendee_calendar.resourceUID(),))

                details["path"] = "/calendars/__uids__/%s/%s/%s" % (home.name(), defaultCalendar.name(), new_name,)
                details["rid"] = calendarObj._resourceID

            details["uid"] = attendee_calendar.resourceUID()
            instances = attendee_calendar.expandTimeRanges(self.end)
            for key in instances:
                instance = instances[key]
                if instance.start > self.now:
                    break
            details["start"] = instance.start.adjustTimezone(self.tzid)
            details["title"] = instance.component.propertyValue("SUMMARY")

            # Write new itip message to attendee inbox
            yield inbox.createCalendarObjectWithName(str(uuid4()) + ".ics", itipmsg, options=self.metadata_inbox)
            self.results.setdefault("Fix add inbox", set()).add((home.name(), itipmsg.resourceUID(),))

            yield self.txn.commit()
            self.txn = self.store.newTransaction()

            # Need to know whether the attendee is a location or resource with auto-accept set
            record = yield self.directoryService().recordWithUID(attendee)
            autoScheduleMode = getattr(record, "autoScheduleMode", None)
            if autoScheduleMode not in (None, AutoScheduleMode.none):
                # Log details about the event so we can have a human manually process
                self.fixedAutoAccepts.append(details)

            returnValue(True)

        except Exception as e:
            print("Failed to fix resource: %d for attendee: %s\n%s" % (orgresid, attendee, e,))
            returnValue(False)

    @inlineCallbacks
    def defaultCalendarForAttendee(self, home):

        # Check for property
        calendar = (yield home.defaultCalendar("VEVENT"))
        returnValue(calendar)

    def printAutoAccepts(self):
        # Print summary of results
        table = tables.Table()
        table.addHeader(("Path", "RID", "UID", "Start Time", "Title"))
        for item in sorted(self.fixedAutoAccepts, key=lambda x: x["path"]):
            table.addRow((
                item["path"],
                item["rid"],
                item["uid"],
                item["start"],
                item["title"],
            ))

        self.output.write("\n")
        self.output.write("Auto-Accept Fixes:\n")
        table.printTable(os=self.output)

    def masterComponent(self, calendar):
        """
        Return the master iCal component in this calendar.

        @return: the L{Component} for the master component,
            or C{None} if there isn't one.
        """
        for component in calendar.getComponents(definitions.cICalComponent_VEVENT):
            if not component.hasProperty("RECURRENCE-ID"):
                return component

        return None

    def buildAttendeeStates(self, calendar, start, end, attendee_only=None):
        # Expand events into instances in the start/end range
        results = []
        calendar.getVEvents(
            Period(
                start=start,
                end=end,
            ),
            results
        )

        # Need to do iCal fake master fixup
        overrides = len(calendar.getComponents(definitions.cICalComponent_VEVENT)) > 1

        # Create map of each attendee's instances with the instance id (start time) and attendee part-stat
        attendees = {}
        for item in results:

            # Fake master fixup
            if overrides:
                if not item.getOwner().isRecurrenceInstance():
                    if item.getOwner().getRecurrenceSet() is None or not item.getOwner().getRecurrenceSet().hasRecurrence():
                        continue

            # Get Status - ignore cancelled events
            status = item.getOwner().loadValueString(definitions.cICalProperty_STATUS)
            cancelled = status == definitions.cICalProperty_STATUS_CANCELLED

            # Get instance start
            item.getInstanceStart().adjustToUTC()
            instance_id = item.getInstanceStart().getText()

            props = item.getOwner().getProperties().get(definitions.cICalProperty_ATTENDEE, [])
            for prop in props:
                caladdr = prop.getCalAddressValue().getValue()
                if caladdr.startswith("urn:x-uid:"):
                    caladdr = caladdr[10:]
                else:
                    continue
                if attendee_only is not None and attendee_only != caladdr:
                    continue
                if cancelled:
                    partstat = "CANCELLED"
                else:
                    if not prop.hasParameter(definitions.cICalParameter_PARTSTAT):
                        partstat = definitions.cICalParameter_PARTSTAT_NEEDSACTION
                    else:
                        partstat = prop.getParameterValue(definitions.cICalParameter_PARTSTAT)

                attendees.setdefault(caladdr, set()).add((instance_id, partstat))

        return attendees

    def allCancelled(self, attendeesStatus):
        # Check whether attendees have all instances cancelled
        all_cancelled = True
        for _ignore_guid, states in attendeesStatus.iteritems():
            for _ignore_instance_id, partstat in states:
                if partstat not in ("CANCELLED", "DECLINED",):
                    all_cancelled = False
                    break
            if not all_cancelled:
                break
        return all_cancelled

    def setTransparencyForAttendee(self, calendar, attendee):
        """
        Set the TRANSP property based on the PARTSTAT value on matching ATTENDEE properties
        in each component.
        """
        for component in calendar.subcomponents(ignore=True):
            prop = component.getAttendeeProperty(attendee)
            addTransp = False
            if prop:
                partstat = prop.parameterValue("PARTSTAT", "NEEDS-ACTION")
                addTransp = partstat in ("NEEDS-ACTION", "DECLINED",)
            component.replaceProperty(Property("TRANSP", "TRANSPARENT" if addTransp else "OPAQUE"))


class DoubleBookingService(CalVerifyService):
    """
    Service which detects double-booked events.
    """

    def title(self):
        return "Double Booking Service"

    @inlineCallbacks
    def doAction(self):

        if self.options["fix"]:
            self.output.write("\nFixing is not supported.\n")
            returnValue(None)

        self.output.write("\n---- Scanning calendar data ----\n")

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")
        self.now = DateTime.getNowUTC()
        self.start = DateTime.getToday()
        self.start.setDateOnly(False)
        self.start.setTimezone(self.tzid)
        self.end = self.start.duplicate()
        self.end.offsetYear(1)
        self.fix = self.options["fix"]

        if self.options["verbose"] and self.options["summary"]:
            ot = time.time()

        # Check loop over uuid
        UUIDDetails = collections.namedtuple("UUIDDetails", ("uuid", "rname", "auto", "doubled",))
        self.uuid_details = []
        if len(self.options["uuid"]) != 36:
            self.txn = self.store.newTransaction()
            if self.options["uuid"]:
                homes = yield self.getMatchingHomeUIDs(self.options["uuid"])
            else:
                homes = yield self.getAllHomeUIDs()
            yield self.txn.commit()
            self.txn = None
            uuids = []
            for uuid in sorted(homes):
                record = yield self.directoryService().recordWithUID(uuid)
                if record is not None and record.recordType in (CalRecordType.location, CalRecordType.resource):
                    uuids.append(uuid)
        else:
            uuids = [self.options["uuid"], ]

        count = 0
        for uuid in uuids:
            self.results = {}
            self.summary = []
            self.total = 0
            count += 1

            record = yield self.directoryService().recordWithUID(uuid)
            if record is None:
                continue
            if not record.thisServer() or not record.hasCalendars:
                continue

            rname = record.displayName
            autoScheduleMode = getattr(record, "autoSchedule", AutoScheduleMode.none)

            if len(uuids) > 1 and not self.options["summary"]:
                self.output.write("\n\n-----------------------------\n")

            self.txn = self.store.newTransaction()

            if self.options["verbose"]:
                t = time.time()
            rows = yield self.getTimeRangeInfoWithUUID(uuid, self.start)
            descriptor = "getTimeRangeInfoWithUUID"

            yield self.txn.commit()
            self.txn = None

            if self.options["verbose"]:
                if not self.options["summary"]:
                    self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))
                else:
                    self.output.write("%s (%d/%d)" % (uuid, count, len(uuids),))
                    self.output.flush()

            self.total = len(rows)
            if not self.options["summary"]:
                self.logResult("UUID to process", uuid)
                self.logResult("Record name", rname)
                self.logResult("Auto-schedule-mode", autoScheduleMode.description)
                self.addSummaryBreak()
                self.logResult("Number of events to process", self.total)

            if rows:
                if not self.options["summary"]:
                    self.addSummaryBreak()
                doubled = yield self.doubleBookCheck(rows, uuid, self.start)
            else:
                doubled = False

            self.uuid_details.append(UUIDDetails(uuid, rname, autoScheduleMode, doubled))

            if not self.options["summary"]:
                self.printSummary()
            else:
                self.output.write(" - %s\n" % ("Double-booked" if doubled else "OK",))
                self.output.flush()

        if self.options["summary"]:
            table = tables.Table()
            table.addHeader(("GUID", "Name", "Auto-Schedule", "Double-Booked",))
            doubled = 0
            for item in sorted(self.uuid_details):
                if not item.doubled:
                    continue
                table.addRow((
                    item.uuid,
                    item.rname,
                    item.autoScheduleMode,
                    item.doubled,
                ))
                doubled += 1
            table.addFooter(("Total", "", "", "%d of %d" % (doubled, len(self.uuid_details),),))
            self.output.write("\n")
            table.printTable(os=self.output)

            if self.options["verbose"]:
                self.output.write("%s time: %.1fs\n" % ("Summary", time.time() - ot,))

    @inlineCallbacks
    def getTimeRangeInfoWithUUID(self, uuid, start):
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        ch = schema.CALENDAR_HOME
        tr = schema.TIME_RANGE
        kwds = {
            "uuid": uuid,
            "Start": pyCalendarToSQLTimestamp(start),
        }
        rows = (yield Select(
            [co.RESOURCE_ID, ],
            From=ch.join(
                cb, type="inner", on=(ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID)).join(
                co, type="inner", on=(cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                    cb.BIND_MODE == _BIND_MODE_OWN).And(
                    cb.CALENDAR_RESOURCE_NAME != "inbox").And(
                    co.ORGANIZER != "")).join(
                tr, type="left", on=(co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID)),
            Where=(ch.OWNER_UID == Parameter("uuid")).And((tr.START_DATE >= Parameter("Start")).Or(co.RECURRANCE_MAX <= Parameter("Start"))),
            Distinct=True,
        ).on(self.txn, **kwds))
        returnValue(tuple(rows))

    @inlineCallbacks
    def doubleBookCheck(self, rows, uuid, start):
        """
        Check each calendar resource by expanding instances within the next year, and looking for
        any that overlap with status not CANCELLED and PARTSTAT ACCEPTED.
        """

        if not self.options["summary"]:
            self.output.write("\n---- Checking instances for double-booking ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()

        InstanceDetails = collections.namedtuple("InstanceDetails", ("resid", "uid", "start", "end", "organizer", "summary",))

        end = start.duplicate()
        end.offsetDay(int(self.options["days"]))
        count = 0
        total = len(rows)
        total_instances = 0
        booked_instances = 0
        details = []
        rjust = 10
        tzid = None
        hasFloating = False
        for resid in rows:
            resid = resid[0]
            caldata = yield self.getCalendar(resid, self.fix)
            if caldata is None:
                if self.parseError:
                    returnValue((False, self.parseError))
                else:
                    returnValue((True, "Nothing to scan"))

            cal = Component(None, pycalendar=caldata)
            cal = PerUserDataFilter(uuid).filter(cal)
            uid = cal.resourceUID()
            instances = cal.expandTimeRanges(end, start, ignoreInvalidInstances=False)
            count += 1

            for instance in instances.instances.values():
                total_instances += 1

                # See if it is CANCELLED or TRANSPARENT
                if instance.component.propertyValue("STATUS") == "CANCELLED":
                    continue
                if instance.component.propertyValue("TRANSP") == "TRANSPARENT":
                    continue
                dtstart = instance.component.propertyValue("DTSTART")
                if tzid is None and dtstart.getTimezoneID():
                    tzid = Timezone(tzid=dtstart.getTimezoneID())
                hasFloating |= dtstart.isDateOnly() or dtstart.floating()

                details.append(InstanceDetails(resid, uid, instance.start, instance.end, instance.component.getOrganizer(), instance.component.propertyValue("SUMMARY")))
                booked_instances += 1

            if self.options["verbose"] and not self.options["summary"]:
                if count == 1:
                    self.output.write("Instances".rjust(rjust) + "Current".rjust(rjust) + "Total".rjust(rjust) + "Complete".rjust(rjust) + "\n")
                if divmod(count, 100)[1] == 0:
                    self.output.write((
                        "\r" +
                        ("%s" % total_instances).rjust(rjust) +
                        ("%s" % count).rjust(rjust) +
                        ("%s" % total).rjust(rjust) +
                        ("%d%%" % safePercent(count, total)).rjust(rjust)
                    ).ljust(80))
                    self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(count, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"] and not self.options["summary"]:
            self.output.write((
                "\r" +
                ("%s" % total_instances).rjust(rjust) +
                ("%s" % count).rjust(rjust) +
                ("%s" % total).rjust(rjust) +
                ("%d%%" % safePercent(count, total)).rjust(rjust)
            ).ljust(80) + "\n")

        if not self.options["summary"]:
            self.logResult("Number of instances in time-range", total_instances)
            self.logResult("Number of booked instances", booked_instances)

        # Adjust floating and sort
        if hasFloating and tzid is not None:
            utc = Timezone.UTCTimezone
            for item in details:
                if item.start.floating():
                    item.start.setTimezone(tzid)
                    item.start.adjustTimezone(utc)
                if item.end.floating():
                    item.end.setTimezone(tzid)
                    item.end.adjustTimezone(utc)
        details.sort(key=lambda x: x.start)

        # Now look for double-bookings
        DoubleBookedDetails = collections.namedtuple("DoubleBookedDetails", ("resid1", "uid1", "resid2", "uid2", "start",))
        double_booked = []
        current = details[0] if details else None
        for next in details[1:]:
            if current.end > next.start and current.resid != next.resid and not (current.organizer == next.organizer and current.summary == next.summary):
                dt = next.start.duplicate()
                dt.adjustTimezone(self.tzid)
                double_booked.append(DoubleBookedDetails(current.resid, current.uid, next.resid, next.uid, dt,))
            current = next

        # Print table of results
        if double_booked and not self.options["summary"]:
            table = tables.Table()
            table.addHeader(("RID #1", "UID #1", "RID #2", "UID #2", "Start",))
            previous1 = None
            previous2 = None
            unique_events = 0
            for item in sorted(double_booked):
                if previous1 != item.resid1:
                    unique_events += 1
                resid1 = item.resid1 if previous1 != item.resid1 else "."
                uid1 = item.uid1 if previous1 != item.resid1 else "."
                resid2 = item.resid2 if previous2 != item.resid2 else "."
                uid2 = item.uid2 if previous2 != item.resid2 else "."
                table.addRow((
                    resid1,
                    uid1,
                    resid2,
                    uid2,
                    item.start,
                ))
                previous1 = item.resid1
                previous2 = item.resid2

            self.output.write("\n")
            self.logResult("Number of double-bookings", len(double_booked))
            self.logResult("Number of unique double-bookings", unique_events)
            table.printTable(os=self.output)

        self.results["Double-bookings"] = double_booked

        if self.options["verbose"] and not self.options["summary"]:
            diff_time = time.time() - t
            self.output.write("Time: %.2f s  Average: %.1f ms/resource\n" % (
                diff_time,
                safePercent(diff_time, total, 1000.0),
            ))

        returnValue(len(double_booked) != 0)


class DarkPurgeService(CalVerifyService):
    """
    Service which detects room/resource events that have an invalid organizer.
    """

    def title(self):
        return "Dark Purge Service"

    @inlineCallbacks
    def doAction(self):

        if not self.options["no-organizer"] and not self.options["invalid-organizer"] and not self.options["disabled-organizer"]:
            self.options["invalid-organizer"] = self.options["disabled-organizer"] = True

        self.output.write("\n---- Scanning calendar data ----\n")

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")
        self.now = DateTime.getNowUTC()
        self.start = self.options["start"] if "start" in self.options else DateTime.getToday()
        self.start.setDateOnly(False)
        self.start.setTimezone(self.tzid)
        self.fix = self.options["fix"]

        if self.options["verbose"] and self.options["summary"]:
            ot = time.time()

        # Check loop over uuid
        UUIDDetails = collections.namedtuple("UUIDDetails", ("uuid", "rname", "purged",))
        self.uuid_details = []
        if len(self.options["uuid"]) != 36:
            self.txn = self.store.newTransaction()
            if self.options["uuid"]:
                homes = yield self.getMatchingHomeUIDs(self.options["uuid"])
            else:
                homes = yield self.getAllHomeUIDs()
            yield self.txn.commit()
            self.txn = None
            uuids = []
            if self.options["verbose"]:
                self.output.write("%d uuids to check\n" % (len(homes,)))
            for uuid in sorted(homes):
                record = yield self.directoryService().recordWithUID(uuid)
                if record is not None and record.recordType in (CalRecordType.location, CalRecordType.resource):
                    uuids.append(uuid)
        else:
            uuids = [self.options["uuid"], ]
        if self.options["verbose"]:
            self.output.write("%d uuids to scan\n" % (len(uuids,)))

        count = 0
        for uuid in uuids:
            self.results = {}
            self.summary = []
            self.total = 0
            count += 1

            record = yield self.directoryService().recordWithUID(uuid)
            if record is None:
                continue
            if not record.thisServer() or not record.hasCalendars:
                continue

            rname = record.displayName

            if len(uuids) > 1 and not self.options["summary"]:
                self.output.write("\n\n-----------------------------\n")

            self.txn = self.store.newTransaction()

            if self.options["verbose"]:
                t = time.time()
            rows = yield self.getAllResourceInfoTimeRangeWithUUID(self.start, uuid)
            descriptor = "getAllResourceInfoTimeRangeWithUUID"

            yield self.txn.commit()
            self.txn = None

            if self.options["verbose"]:
                if not self.options["summary"]:
                    self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))
                else:
                    self.output.write("%s (%d/%d)" % (uuid, count, len(uuids),))
                    self.output.flush()

            self.total = len(rows)
            if not self.options["summary"]:
                self.logResult("UUID to process", uuid)
                self.logResult("Record name", rname)
                self.addSummaryBreak()
                self.logResult("Number of events to process", self.total)

            if rows:
                if not self.options["summary"]:
                    self.addSummaryBreak()
                purged = yield self.darkPurge(rows, uuid)
            else:
                purged = False

            self.uuid_details.append(UUIDDetails(uuid, rname, purged))

            if not self.options["summary"]:
                self.printSummary()
            else:
                self.output.write(" - %s\n" % ("Dark Events" if purged else "OK",))
                self.output.flush()

        if count == 0:
            self.output.write("Nothing to scan\n")

        if self.options["summary"]:
            table = tables.Table()
            table.addHeader(("GUID", "Name", "RID", "UID", "Organizer",))
            purged = 0
            for item in sorted(self.uuid_details):
                if not item.purged:
                    continue
                uuid = item.uuid
                rname = item.rname
                for detail in item.purged:
                    table.addRow((
                        uuid,
                        rname,
                        detail.resid,
                        detail.uid,
                        detail.organizer,
                    ))
                    uuid = ""
                    rname = ""
                    purged += 1
            table.addFooter(("Total", "%d" % (purged,), "", "", "",))
            self.output.write("\n")
            table.printTable(os=self.output)

            if self.options["verbose"]:
                self.output.write("%s time: %.1fs\n" % ("Summary", time.time() - ot,))

    @inlineCallbacks
    def darkPurge(self, rows, uuid):
        """
        Check each calendar resource by looking at any ORGANIER property value and verifying it is valid.
        """

        if not self.options["summary"]:
            self.output.write("\n---- Checking for dark events ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()

        Details = collections.namedtuple("Details", ("resid", "uid", "organizer",))

        count = 0
        total = len(rows)
        details = []
        fixed = 0
        rjust = 10
        for resid in rows:
            resid = resid[1]
            caldata = yield self.getCalendar(resid, self.fix)
            if caldata is None:
                if self.parseError:
                    returnValue((False, self.parseError))
                else:
                    returnValue((True, "Nothing to scan"))

            cal = Component(None, pycalendar=caldata)
            uid = cal.resourceUID()

            fail = False
            organizer = cal.getOrganizer()
            if organizer is None:
                if self.options["no-organizer"]:
                    fail = True
            else:
                principal = yield self.directoryService().recordWithCalendarUserAddress(organizer)
                # FIXME: Why the mix of records and principals here?
                if principal is None and organizer.startswith("urn:x-uid:"):
                    principal = yield self.directoryService().principalCollection.principalForUID(organizer[10:])
                if principal is None:
                    if self.options["invalid-organizer"]:
                        fail = True
                elif not principal.calendarsEnabled():
                    if self.options["disabled-organizer"]:
                        fail = True

            if fail:
                details.append(Details(resid, uid, organizer,))
                if self.fix:
                    yield self.removeEvent(resid)
                    fixed += 1

            if self.options["verbose"] and not self.options["summary"]:
                if count == 1:
                    self.output.write("Current".rjust(rjust) + "Total".rjust(rjust) + "Complete".rjust(rjust) + "\n")
                if divmod(count, 100)[1] == 0:
                    self.output.write((
                        "\r" +
                        ("%s" % count).rjust(rjust) +
                        ("%s" % total).rjust(rjust) +
                        ("%d%%" % safePercent(count, total)).rjust(rjust)
                    ).ljust(80))
                    self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(count, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"] and not self.options["summary"]:
            self.output.write((
                "\r" +
                ("%s" % count).rjust(rjust) +
                ("%s" % total).rjust(rjust) +
                ("%d%%" % safePercent(count, total)).rjust(rjust)
            ).ljust(80) + "\n")

        # Print table of results
        if not self.options["summary"]:
            self.logResult("Number of dark events", len(details))

        self.results["Dark Events"] = details
        if self.fix:
            self.results["Fix dark events"] = fixed

        if self.options["verbose"] and not self.options["summary"]:
            diff_time = time.time() - t
            self.output.write("Time: %.2f s  Average: %.1f ms/resource\n" % (
                diff_time,
                safePercent(diff_time, total, 1000.0),
            ))

        returnValue(details)


class MissingLocationService(CalVerifyService):
    """
    Service which detects room/resource events that have an ATTENDEE;CUTYPE=ROOM property but no Location.
    """

    def title(self):
        return "Missing Location Service"

    @inlineCallbacks
    def doAction(self):

        self.output.write("\n---- Scanning calendar data ----\n")

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")
        self.now = DateTime.getNowUTC()
        self.start = self.options["start"] if "start" in self.options else DateTime.getToday()
        self.start.setDateOnly(False)
        self.start.setTimezone(self.tzid)
        self.fix = self.options["fix"]

        if self.options["verbose"] and self.options["summary"]:
            ot = time.time()

        # Check loop over uuid
        UUIDDetails = collections.namedtuple("UUIDDetails", ("uuid", "rname", "missing",))
        self.uuid_details = []
        if len(self.options["uuid"]) != 36:
            self.txn = self.store.newTransaction()
            if self.options["uuid"]:
                homes = yield self.getMatchingHomeUIDs(self.options["uuid"])
            else:
                homes = yield self.getAllHomeUIDs()
            yield self.txn.commit()
            self.txn = None
            uuids = []
            if self.options["verbose"]:
                self.output.write("%d uuids to check\n" % (len(homes,)))
            for uuid in sorted(homes):
                record = yield self.directoryService().recordWithUID(uuid)
                if record is not None and record.recordType == CalRecordType.location:
                    uuids.append(uuid)
        else:
            uuids = [self.options["uuid"], ]
        if self.options["verbose"]:
            self.output.write("%d uuids to scan\n" % (len(uuids,)))

        count = 0
        for uuid in uuids:
            self.results = {}
            self.summary = []
            self.total = 0
            count += 1

            record = yield self.directoryService().recordWithUID(uuid)
            if record is None:
                continue
            if not record.thisServer() or not record.hasCalendars:
                continue

            rname = record.displayName

            if len(uuids) > 1 and not self.options["summary"]:
                self.output.write("\n\n-----------------------------\n")

            self.txn = self.store.newTransaction()

            if self.options["verbose"]:
                t = time.time()
            rows = yield self.getAllResourceInfoTimeRangeWithUUID(self.start, uuid)
            descriptor = "getAllResourceInfoTimeRangeWithUUID"

            yield self.txn.commit()
            self.txn = None

            if self.options["verbose"]:
                if not self.options["summary"]:
                    self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))
                else:
                    self.output.write("%s (%d/%d)" % (uuid, count, len(uuids),))
                    self.output.flush()

            self.total = len(rows)
            if not self.options["summary"]:
                self.logResult("UUID to process", uuid)
                self.logResult("Record name", rname)
                self.addSummaryBreak()
                self.logResult("Number of events to process", self.total)

            if rows:
                if not self.options["summary"]:
                    self.addSummaryBreak()
                missing = yield self.missingLocation(rows, uuid, rname)
            else:
                missing = False

            self.uuid_details.append(UUIDDetails(uuid, rname, missing))

            if not self.options["summary"]:
                self.printSummary()
            else:
                self.output.write(" - %s\n" % ("Bad Events" if missing else "OK",))
                self.output.flush()

        if count == 0:
            self.output.write("Nothing to scan\n")

        if self.options["summary"]:
            table = tables.Table()
            table.addHeader(("GUID", "Name", "RID", "UID",))
            missing = 0
            for item in sorted(self.uuid_details):
                if not item.missing:
                    continue
                uuid = item.uuid
                rname = item.rname
                for detail in item.missing:
                    table.addRow((
                        uuid,
                        rname,
                        detail.resid,
                        detail.uid,
                    ))
                    uuid = ""
                    rname = ""
                    missing += 1
            table.addFooter(("Total", "%d" % (missing,), "", "", "",))
            self.output.write("\n")
            table.printTable(os=self.output)

            if self.options["verbose"]:
                self.output.write("%s time: %.1fs\n" % ("Summary", time.time() - ot,))

    @inlineCallbacks
    def missingLocation(self, rows, uuid, rname):
        """
        Check each calendar resource by looking at any ORGANIZER property value and verifying it is valid.
        """

        if not self.options["summary"]:
            self.output.write("\n---- Checking for missing location events ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()

        Details = collections.namedtuple("Details", ("resid", "uid",))

        count = 0
        total = len(rows)
        details = []
        fixed = 0
        rjust = 10
        cuaddr = "urn:x-uid:{}".format(uuid)
        for resid in rows:
            resid = resid[1]
            caldata = yield self.getCalendar(resid, self.fix)
            if caldata is None:
                if self.parseError:
                    returnValue((False, self.parseError))
                else:
                    returnValue((True, "Nothing to scan"))

            cal = Component(None, pycalendar=caldata)
            uid = cal.resourceUID()

            fail = False
            if cal.getOrganizer() is not None:
                for comp in cal.subcomponents():
                    if comp.name() != "VEVENT":
                        continue

                    # Look for matching location property
                    location = comp.propertyValue("LOCATION")
                    if location is not None:
                        items = location.split(";")
                        has_location = rname in items
                    else:
                        has_location = False

                    # Look for matching attendee property
                    has_attendee = cuaddr in comp.getAttendees()

                    if not has_location and has_attendee:
                        fail = True
                        break

            if fail:
                details.append(Details(resid, uid,))
                if self.fix:
                    yield self.fixCalendarData(cal, rname, cuaddr, resid)
                    fixed += 1

            if self.options["verbose"] and not self.options["summary"]:
                if count == 1:
                    self.output.write("Current".rjust(rjust) + "Total".rjust(rjust) + "Complete".rjust(rjust) + "\n")
                if divmod(count, 100)[1] == 0:
                    self.output.write((
                        "\r" +
                        ("%s" % count).rjust(rjust) +
                        ("%s" % total).rjust(rjust) +
                        ("%d%%" % safePercent(count, total)).rjust(rjust)
                    ).ljust(80))
                    self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(count, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"] and not self.options["summary"]:
            self.output.write((
                "\r" +
                ("%s" % count).rjust(rjust) +
                ("%s" % total).rjust(rjust) +
                ("%d%%" % safePercent(count, total)).rjust(rjust)
            ).ljust(80) + "\n")

        # Print table of results
        if not self.options["summary"]:
            self.logResult("Number of bad events", len(details))

        self.results["Bad Events"] = details
        if self.fix:
            self.results["Fix bad events"] = fixed

        if self.options["verbose"] and not self.options["summary"]:
            diff_time = time.time() - t
            self.output.write("Time: %.2f s  Average: %.1f ms/resource\n" % (
                diff_time,
                safePercent(diff_time, total, 1000.0),
            ))

        returnValue(details)

    @inlineCallbacks
    def fixCalendarData(self, cal, rname, cuaddr, location_resid):
        """
        Fix problems in calendar data using store APIs.
        """

        result = True
        message = ""

        # Extract organizer (strip off urn:x-uid:) and UID
        organizer = cal.getOrganizer()[10:]
        uid = cal.resourceUID()
        _ignore_calendar, resid, _ignore_created, _ignore_modified = yield self.getCalendarForOwnerByUID(organizer, uid)
        if resid is None:
            # No organizer copy exists - it is safe to delete the location resource
            yield self.removeEvent(location_resid)
            returnValue((result, message,))

        # Get the organizer's calendar object and data
        homeID, calendarID = yield self.getAllResourceInfoForResourceID(resid)
        home = yield self.txn.calendarHomeWithResourceID(homeID)
        calendar = yield home.childWithID(calendarID)
        calendarObj = yield calendar.objectResourceWithID(resid)

        try:
            component = yield calendarObj.componentForUser()
        except InternalDataStoreError:
            returnValue((False, "Failed parse: "))

        # Fix each component (need to dup component when modifying)
        component = component.duplicate()
        for comp in component.subcomponents():
            if comp.name() != "VEVENT":
                continue
            location = comp.propertyValue("LOCATION")
            if location is None:
                # Just add the location name back in
                comp.addProperty(Property("LOCATION", rname))
            else:
                # Remove the matching ATTENDEE property
                attendee = comp.getAttendeeProperty((cuaddr,))
                if attendee is not None:
                    comp.removeProperty(attendee)

        # Write out fix, commit and get a new transaction
        try:
            yield calendarObj.setComponent(component)
        except Exception as e:
            print(e, component)
            print(traceback.print_exc())
            result = False
            message = "Exception fix: "
        yield self.txn.commit()
        self.txn = self.store.newTransaction()

        returnValue((result, message,))


class EventSplitService(CalVerifyService):
    """
    Service which splits a recurring event at a specific date-time value.
    """

    def title(self):
        return "Event Split Service"

    @inlineCallbacks
    def doAction(self):
        """
        Split a resource using either its path or resource id.
        """

        self.txn = self.store.newTransaction()

        path = self.options["path"]
        if path.startswith("/calendars/__uids__/"):
            try:
                pathbits = path.split("/")
            except TypeError:
                printusage("Not a valid calendar object resource path: %s" % (path,))
            if len(pathbits) != 6:
                printusage("Not a valid calendar object resource path: %s" % (path,))
            homeName = pathbits[3]
            calendarName = pathbits[4]
            resourceName = pathbits[5]

            resid = yield self.getResourceID(homeName, calendarName, resourceName)
            if resid is None:
                yield self.txn.commit()
                self.txn = None
                self.output.write("\n")
                self.output.write("Path does not exist. Nothing split.\n")
                returnValue(None)
            resid = int(resid)
        else:
            try:
                resid = int(path)
            except ValueError:
                printusage("path argument must be a calendar object path or an SQL resource-id")

        calendarObj = yield CalendarStoreFeatures(self.txn._store).calendarObjectWithID(self.txn, resid)
        ical = yield calendarObj.component()

        # Must be the ORGANIZER's copy
        organizer = ical.getOrganizer()
        if organizer is None:
            printusage("Calendar object has no ORGANIZER property - cannot split")

        # Only allow organizers to split
        scheduler = ImplicitScheduler()
        is_attendee = (yield scheduler.testAttendeeEvent(calendarObj.calendar(), calendarObj, ical,))
        if is_attendee:
            printusage("Calendar object is not owned by the ORGANIZER - cannot split")

        if self.options["summary"]:
            result = self.doSummary(ical)
        else:
            result = yield self.doSplit(resid, calendarObj, ical)

        returnValue(result)

    def doSummary(self, ical):
        """
        Print a summary of the recurrence instances of the specified event.

        @param ical: calendar to process
        @type ical: L{Component}
        """
        self.output.write("\n---- Calendar resource instances ----\n")

        # Find the instance RECURRENCE-ID where a split is going to happen
        now = DateTime.getNowUTC()
        now.offsetDay(1)
        instances = ical.cacheExpandedTimeRanges(now)
        instances = sorted(instances.instances.values(), key=lambda x: x.start)
        for instance in instances:
            self.output.write(instance.rid.getText() + (" *\n" if instance.overridden else "\n"))

    @inlineCallbacks
    def doSplit(self, resid, calendarObj, ical):
        rid = self.options["rid"]
        try:
            if rid[-1] != "Z":
                raise ValueError
            rid = DateTime.parseText(rid)
        except ValueError:
            printusage("rid must be a valid UTC date-time value: 'YYYYMMDDTHHMMSSZ'")

        self.output.write("\n---- Splitting calendar resource ----\n")

        # Find actual RECURRENCE-ID of split
        splitter = iCalSplitter(1024, 14)
        rid = splitter.whereSplit(ical, break_point=rid, allow_past_the_end=False)
        if rid is None:
            printusage("rid is not a valid recurrence instance")

        self.output.write("\n")
        self.output.write("Actual RECURRENCE-ID: %s.\n" % (rid,))

        oldObj = yield calendarObj.split(rid=rid)
        oldUID = oldObj.uid()
        oldRelated = (yield oldObj.component()).mainComponent().propertyValue("RELATED-TO")

        self.output.write("\n")
        self.output.write("Split Resource: %s at %s, old UID: %s.\n" % (resid, rid, oldUID,))

        yield self.txn.commit()
        self.txn = None

        returnValue((oldUID, oldRelated,))


class UpgradeDataService(CalVerifyService):
    """
    Service which scans calendar data.
    """

    USE_FAST_MODE = True
    OLD_CUADDR_PREFIX = "urn:uuid:"
    NEW_CUADDR_PREFIX = "urn:x-uid:"

    def title(self):
        return "Upgrade Data Service"

    @inlineCallbacks
    def doAction(self):

        self.output.write("\n---- Scanning calendar data ----\n")

        self.now = DateTime.getNowUTC()
        self.start = DateTime.getToday()
        self.start.setDateOnly(False)
        self.end = self.start.duplicate()
        self.end.offsetYear(1)
        self.fix = self.options["fix"]

        self.tzid = Timezone(tzid=self.options["tzid"] if self.options["tzid"] else "America/Los_Angeles")

        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()
        descriptor = None
        if self.options["uuid"]:
            rows = yield self.getAllResourceInfoWithUUID(self.options["uuid"], inbox=True)
            descriptor = "getAllResourceInfoWithUUID"
        elif self.options["uid"]:
            rows = yield self.getAllResourceInfoWithUID(self.options["uid"], inbox=True)
            descriptor = "getAllResourceInfoWithUID"
        else:
            rows = yield self.getAllResourceInfo(inbox=True)
            descriptor = "getAllResourceInfo"

        yield self.txn.commit()
        self.txn = None

        if self.options["verbose"]:
            self.output.write("%s time: %.1fs\n" % (descriptor, time.time() - t,))

        self.total = len(rows)
        self.logResult("Number of events to process", self.total)
        self.addSummaryBreak()

        yield self.calendarDataUpgrade(rows)

        self.printSummary()

    @inlineCallbacks
    def calendarDataUpgrade(self, rows):
        """
        Check each calendar resource for valid iCalendar data.
        """

        self.output.write("\n---- Upgrading each calendar object resource ----\n")
        self.txn = self.store.newTransaction()

        if self.options["verbose"]:
            t = time.time()

        results_bad = []
        count = 0
        total = len(rows)
        upgradelen = 0
        badlen = 0
        rjust = 10
        for owner, resid, uid, _ignore_calname, _ignore_md5, _ignore_organizer, _ignore_created, _ignore_modified in rows:
            try:
                calendarObj = yield CalendarStoreFeatures(self.txn._store).calendarObjectWithID(self.txn, resid)
                if calendarObj._dataversion < calendarObj._currentDataVersion:
                    upgradelen += 1

                    if UpgradeDataService.USE_FAST_MODE:
                        # Read data from calendar object and manually upgrade the calendar user
                        # addresses only
                        text = yield calendarObj._text()
                        component = Component.fromString(text)

                        for subcomponent in component.subcomponents(ignore=True):
                            for prop in itertools.chain(
                                subcomponent.properties("ORGANIZER"),
                                subcomponent.properties("ATTENDEE"),
                            ):
                                cuaddr = prop.value()
                                if cuaddr.startswith(UpgradeDataService.OLD_CUADDR_PREFIX):
                                    prop.setValue(cuaddr.replace(UpgradeDataService.OLD_CUADDR_PREFIX, UpgradeDataService.NEW_CUADDR_PREFIX))

                        # Do the update right now (also suppress instance indexing)
                        calendarObj._dataversion = calendarObj._currentDataVersion
                        component.noInstanceIndexing = True
                        yield calendarObj.updateDatabase(component)
                    else:
                        # Read component and force and update if needed
                        yield calendarObj.component(doUpdate=True)

                result = True
            except Exception as e:
                result = False
                message = "Exception when reading resource"
                if self.options["verbose"]:
                    print(e)
            if not result:
                results_bad.append((owner, uid, resid, message))
                badlen += 1
            count += 1
            if self.options["verbose"]:
                if count == 1:
                    self.output.write("Upgraded".rjust(rjust) + "Bad".rjust(rjust) + "Current".rjust(rjust) + "Total".rjust(rjust) + "Complete".rjust(rjust) + "\n")
                if divmod(count, 100)[1] == 0:
                    self.output.write((
                        "\r" +
                        ("%s" % upgradelen).rjust(rjust) +
                        ("%s" % badlen).rjust(rjust) +
                        ("%s" % count).rjust(rjust) +
                        ("%s" % total).rjust(rjust) +
                        ("%d%%" % safePercent(count, total)).rjust(rjust)
                    ).ljust(80))
                    self.output.flush()

            # To avoid holding locks on all the rows scanned, commit every 100 resources
            if divmod(count, 100)[1] == 0:
                yield self.txn.commit()
                self.txn = self.store.newTransaction()

        yield self.txn.commit()
        self.txn = None
        if self.options["verbose"]:
            self.output.write((
                "\r" +
                ("%s" % upgradelen).rjust(rjust) +
                ("%s" % badlen).rjust(rjust) +
                ("%s" % count).rjust(rjust) +
                ("%s" % total).rjust(rjust) +
                ("%d%%" % safePercent(count, total)).rjust(rjust)
            ).ljust(80) + "\n")

        # Print table of results
        table = tables.Table()
        table.addHeader(("Owner", "Event UID", "RID", "Problem",))
        for item in sorted(results_bad, key=lambda x: (x[0], x[1])):
            owner, uid, resid, message = item
            owner_record = yield self.directoryService().recordWithUID(owner)
            table.addRow((
                "%s/%s (%s)" % (owner_record.recordType if owner_record else "-", owner_record.shortNames[0] if owner_record else "-", owner,),
                uid,
                resid,
                message,
            ))

        self.output.write("\n")
        self.logResult("Upgraded iCalendar data", upgradelen, total)
        self.logResult("Bad iCalendar data", len(results_bad), total)
        self.results["Upgraded iCalendar data"] = upgradelen
        self.results["Bad iCalendar data"] = results_bad
        table.printTable(os=self.output)

        if self.options["verbose"]:
            diff_time = time.time() - t
            self.output.write("Time: %.2f s  Average: %.1f ms/resource\n" % (
                diff_time,
                safePercent(diff_time, total, 1000.0),
            ))


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):

    if reactor is None:
        from twisted.internet import reactor
    options = CalVerifyOptions()
    try:
        options.parseOptions(argv[1:])
    except usage.UsageError as e:
        printusage(e)

    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)

    def makeService(store):
        from twistedcaldav.config import config
        config.TransactionTimeoutSeconds = 0
        if options["nuke"]:
            return NukeService(store, options, output, reactor, config)
        elif options["missing"]:
            return OrphansService(store, options, output, reactor, config)
        elif options["ical"]:
            return BadDataService(store, options, output, reactor, config)
        elif options["mismatch"]:
            return SchedulingMismatchService(store, options, output, reactor, config)
        elif options["double"]:
            return DoubleBookingService(store, options, output, reactor, config)
        elif options["dark-purge"]:
            return DarkPurgeService(store, options, output, reactor, config)
        elif options["missing-location"]:
            return MissingLocationService(store, options, output, reactor, config)
        elif options["split"]:
            return EventSplitService(store, options, output, reactor, config)
        elif options["upgrade"]:
            return UpgradeDataService(store, options, output, reactor, config)
        else:
            printusage("Invalid operation")
            sys.exit(1)

    utilityMain(options['config'], makeService, reactor)

if __name__ == '__main__':
    main()
