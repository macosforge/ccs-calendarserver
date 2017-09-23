#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_export -*-
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
This tool reads calendar data from a series of inputs and generates a single
iCalendar file which can be opened in many calendar applications.

This can be used to quickly create an iCalendar file from a user's calendars.

This tool requires access to the calendar server's configuration and data
storage; it does not operate by talking to the server via the network.  It
therefore does not apply any of the access restrictions that the server would.
As such, one should be mindful that data exported via this tool may be sensitive.

Please also note that this is not an appropriate tool for backups, as there is
data associated with users and calendars beyond the iCalendar as visible to the
owner of that calendar, including DAV properties, information about sharing, and
per-user data such as alarms.
"""

from __future__ import print_function

import itertools
import os
import shutil
import sys

from calendarserver.tools.cmdline import utilityMain, WorkerService
from twext.enterprise.dal.syntax import Select
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.python.text import wordWrap
from twisted.python.usage import Options, UsageError
from twistedcaldav import customxml
from twistedcaldav.ical import Component, Property
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from txdav.base.propertystore.base import PropertyName
from txdav.caldav.datastore.scheduling.utils import normalizeCUAddr
from txdav.caldav.datastore.sql import Calendar
from txdav.common.datastore.sql_tables import schema
from txdav.xml import element as davxml


log = Logger()


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        ExportOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = '\n'.join(
    wordWrap(
        """
        Usage: calendarserver_export [options] [input specifiers]\n
        """ + __doc__,
        int(os.environ.get('COLUMNS', '80'))
    )
)


class ExportOptions(Options):
    """
    Command-line options for 'calendarserver_export'

    @ivar exporters: a list of L{DirectoryExporter} objects which can identify the
        calendars to export, given a directory service.  This list is built by
        parsing --record and --collection options.
    """

    synopsis = description

    optFlags = [
        ['debug', 'D', "Debug logging."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
    ]

    def __init__(self):
        super(ExportOptions, self).__init__()
        self.exporters = []
        self.outputName = '-'
        self.outputDirectoryName = None
        self.exportAll = False
        self.exportAllType = "VEVENT"
        self.convertToMailto = False

    def opt_uid(self, uid):
        """
        Add a calendar home directly by its UID (which is usually a directory
        service's GUID).
        """
        self.exporters.append(UIDExporter(uid))

    def opt_record(self, recordName):
        """
        Add a directory record's calendar home (format: 'recordType:shortName').
        """
        recordType, shortName = recordName.split(":", 1)
        self.exporters.append(DirectoryExporter(recordType, shortName))

    opt_r = opt_record

    def opt_collection(self, collectionName):
        """
        Add a calendar collection.  This option must be passed after --record
        (or a synonym, like --user).  for example, to export user1's calendars
        called 'meetings' and 'team', invoke 'calendarserver_export --user=user1
        --collection=meetings --collection=team'.
        """
        self.exporters[-1].collections.append(collectionName)

    opt_c = opt_collection

    def opt_directory(self, dirname):
        """
        Specify output directory path (works with calendars and contacts).
        """
        self.outputDirectoryName = dirname

    opt_d = opt_directory

    def opt_output(self, filename):
        """
        Specify output file path (default: '-', meaning stdout; only works with calendars).
        """
        self.outputName = filename

    opt_o = opt_output

    def opt_user(self, user):
        """
        Add a user's calendar home (shorthand for '-r users:shortName').
        """
        self.opt_record("users:" + user)

    opt_u = opt_user

    def opt_all(self):
        """
        Export calendars from every calendar home in the database.
        """
        self.exportAll = True

    def opt_mailto(self):
        """
        When exporting, convert calendar user addresses to mailto: form where possible.
        """
        self.convertToMailto = True

    def opt_calendars(self):
        """
        Export calendars (the default)
        """
        if len(self.exporters) > 0:
            self.exporters[-1].exportType = "VEVENT"
        else:
            self.exportAllType = "VEVENT"

    def opt_contacts(self):
        """
        Export VCARDs
        """
        if len(self.exporters) > 0:
            self.exporters[-1].exportType = "VCARD"
        else:
            self.exportAllType = "VCARD"

    def openOutput(self):
        """
        Open the appropriate output file based on the '--output' option.
        """
        if self.outputName == '-':
            return sys.stdout
        else:
            return open(self.outputName, 'wb')


class _ExporterBase(object):
    """
    Base exporter implementation that works from a home UID.

    @ivar collections: A list of the names of collections that this exporter
        should enumerate.

    @type collections: C{list} of C{str}

    """

    def __init__(self, exportType="VEVENT"):
        self.collections = []
        self.exportType = exportType

    def getHomeUID(self, exportService):
        """
        Subclasses must implement this.
        """
        raise NotImplementedError()

    @inlineCallbacks
    def listCollections(self, txn, exportService):
        """
        Enumerate all calendars based on the directory record and/or calendars
        for this calendar home.
        """
        uid = yield self.getHomeUID(exportService)
        if self.exportType == "VEVENT":
            home = yield txn.calendarHomeWithUID(uid, create=True)
        else:
            home = yield txn.addressbookHomeWithUID(uid, create=True)
        result = []
        if self.collections:
            for collection in self.collections:
                if self.exportType == "VEVENT":
                    collection = yield home.calendarWithName(collection)
                else:
                    collection = yield home.addressbookWithName(collection)
                result.append(collection)
        else:
            if self.exportType == "VEVENT":
                for collection in (yield home.calendars()):
                    if collection.name() != 'inbox':
                        result.append(collection)
            else:
                for collection in (yield home.addressbooks()):
                    result.append(collection)
        returnValue(result)


class UIDExporter(_ExporterBase):
    """
    An exporter that constructs a list of calendars based on the UID of the
    home, and an optional list of calendar names.

    @ivar uid: The UID of a calendar home to export.

    @type uid: C{str}
    """

    def __init__(self, uid, exportType="VEVENT"):
        super(UIDExporter, self).__init__(exportType=exportType)
        self.uid = uid

    def getHomeUID(self, exportService):
        return succeed(self.uid)


class DirectoryExporter(_ExporterBase):
    """
    An exporter that constructs a list of calendars based on the directory
    services record ID of the home, and an optional list of calendar names.

    @ivar recordType: The directory record type to export.  For example:
        'users'.

    @type recordType: C{str}

    @ivar shortName: The shortName of the directory record to export, according
        to C{recordType}.

    @type shortName: C{str}
    """

    def __init__(self, recordType, shortName, exportType="VEVENT"):
        super(DirectoryExporter, self).__init__(exportType=exportType)
        self.recordType = recordType
        self.shortName = shortName

    @inlineCallbacks
    def getHomeUID(self, exportService):
        """
        Retrieve the home UID.
        """
        directory = exportService.directoryService()
        record = yield directory.recordWithShortName(
            directory.oldNameToRecordType(self.recordType),
            self.shortName
        )
        returnValue(record.uid)


@inlineCallbacks
def exportToFile(calendars, fileobj, convertToMailto=False):
    """
    Export some calendars to a file as their owner would see them.

    @param calendars: an iterable of L{ICalendar} providers (or L{Deferred}s of
        same).

    @param fileobj: an object with a C{write} method that will accept some
        iCalendar data.

    @return: a L{Deferred} which fires when the export is complete.  (Note that
        the file will not be closed.)
    @rtype: L{Deferred} that fires with C{None}
    """
    comp = Component.newCalendar()
    for calendar in calendars:
        calendar = yield calendar
        for obj in (yield calendar.calendarObjects()):
            evt = yield obj.filteredComponent(
                calendar.ownerCalendarHome().uid(), True
            )
            for sub in evt.subcomponents():
                if sub.name() != 'VTIMEZONE':
                    # Omit all VTIMEZONE components here - we will include them later
                    # when we serialize the whole calendar.
                    if convertToMailto:
                        convertCUAsToMailto(sub)
                    comp.addComponent(sub)

    fileobj.write(comp.getTextWithTimezones(True))


@inlineCallbacks
def exportToDirectory(collections, dirname, convertToMailto=False):
    """
    Export some calendars to a file as their owner would see them.

    @param calendars: an iterable of L{ICalendar} providers (or L{Deferred}s of
        same).

    @param dirname: the path to a directory to store calendar files in; each
        calendar being exported will have its own .ics file

    @return: a L{Deferred} which fires when the export is complete.  (Note that
        the file will not be closed.)
    @rtype: L{Deferred} that fires with C{None}
    """

    for collection in collections:

        if isinstance(collection, Calendar):
            homeUID = collection.ownerCalendarHome().uid()

            calendarProperties = collection.properties()
            comp = Component.newCalendar()
            for element, propertyName in (
                (davxml.DisplayName, "NAME"),
                (customxml.CalendarColor, "COLOR"),
            ):

                value = calendarProperties.get(PropertyName.fromElement(element), None)
                if value:
                    comp.addProperty(Property(propertyName, str(value)))

            source = "/calendars/__uids__/{}/{}/".format(homeUID, collection.name())
            comp.addProperty(Property("SOURCE", source))

            for obj in (yield collection.calendarObjects()):
                evt = yield obj.filteredComponent(homeUID, True)
                for sub in evt.subcomponents():
                    if sub.name() != 'VTIMEZONE':
                        # Omit all VTIMEZONE components here - we will include them later
                        # when we serialize the whole calendar.
                        if convertToMailto:
                            convertCUAsToMailto(sub)
                        comp.addComponent(sub)

            filename = os.path.join(dirname, "{}_{}.ics".format(homeUID, collection.name()))
            with open(filename, 'wb') as fileobj:
                fileobj.write(comp.getTextWithTimezones(True))

        else: # addressbook

            homeUID = collection.ownerAddressBookHome().uid()
            filename = os.path.join(dirname, "{}_{}.vcf".format(homeUID, collection.name()))
            with open(filename, 'wb') as fileobj:
                for obj in (yield collection.addressbookObjects()):
                    vcard = yield obj.component()
                    fileobj.write(vcard.getText())


def convertCUAsToMailto(comp):
    """
    Replace non-mailto: CUAs with mailto: CUAs where possible (i.e. there is an
    EMAIL parameter value attached)
    """
    for attendeeProp in itertools.chain(comp.getAllAttendeeProperties(), [comp.getOrganizerProperty()]):
        cuaddr = normalizeCUAddr(attendeeProp.value())
        if not cuaddr.startswith("mailto:"):
            emailAddress = attendeeProp.parameterValue("EMAIL", None)
            if emailAddress:
                attendeeProp.setValue("mailto:%s" % (emailAddress,))
                attendeeProp.removeParameter("EMAIL")


class ExporterService(WorkerService, object):
    """
    Service which runs, exports the appropriate records, then stops the reactor.
    """

    def __init__(self, store, options, output, reactor, config):
        super(ExporterService, self).__init__(store)
        self.options = options
        self.output = output
        self.reactor = reactor
        self.config = config
        self._directory = self.store.directoryService()

    @inlineCallbacks
    def doWork(self):
        """
        Do the export, stopping the reactor when done.
        """
        txn = self.store.newTransaction()

        if self.options.exportAll:
            if self.options.exportAllType == "VEVENT":
                homeTable = schema.CALENDAR_HOME
            else:
                homeTable = schema.ADDRESSBOOK_HOME

            rows = (yield Select(
                [homeTable.OWNER_UID, ],
                From=homeTable,
            ).on(txn))
            for uid in [row[0] for row in rows]:
                self.options.exporters.append(UIDExporter(uid, exportType=self.options.exportAllType))

        try:

            allCollections = itertools.chain(
                *[(yield exporter.listCollections(txn, self)) for exporter in
                  self.options.exporters]
            )

            if self.options.outputDirectoryName:
                dirname = self.options.outputDirectoryName
                if os.path.exists(dirname):
                    shutil.rmtree(dirname)
                os.mkdir(dirname)
                yield exportToDirectory(allCollections, dirname, self.options.convertToMailto)
            else:
                yield exportToFile(allCollections, self.output, self.options.convertToMailto)
                self.output.close()

            yield txn.commit()
            # TODO: should be read-only, so commit/abort shouldn't make a
            # difference.  commit() for now, in case any transparent cache /
            # update stuff needed to happen, don't want to undo it.
        except:
            log.failure("doWork()")

    def directoryService(self):
        """
        Get an appropriate directory service.
        """
        return self._directory

    def stopService(self):
        """
        Stop the service.  Nothing to do; everything should be finished by this
        time.
        """
        # TODO: stopping this service mid-export should really stop the export
        # loop, but this is not implemented because nothing will actually do it
        # except hitting ^C (which also calls reactor.stop(), so that will exit
        # anyway).


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    """
    Do the export.
    """
    if reactor is None:
        from twisted.internet import reactor

    options = ExportOptions()
    try:
        options.parseOptions(argv[1:])
    except UsageError as e:
        usage(e)

    if options.outputDirectoryName:
        output = None
    else:
        try:
            output = options.openOutput()
        except IOError as e:
            stderr.write(
                "Unable to open output file for writing: %s\n" % (e)
            )
            sys.exit(1)

    def makeService(store):
        from twistedcaldav.config import config
        config.TransactionTimeoutSeconds = 0
        return ExporterService(store, options, output, reactor, config)

    utilityMain(options["config"], makeService, reactor, verbose=options["debug"])
