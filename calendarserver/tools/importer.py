#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_importer -*-
##
# Copyright (c) 2014-2017 Apple Inc. All rights reserved.
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
This tool imports calendar data

This tool requires access to the calendar server's configuration and data
storage; it does not operate by talking to the server via the network.  It
therefore does not apply any of the access restrictions that the server would.
"""

from __future__ import print_function

import os
import sys
import uuid

from calendarserver.tools.cmdline import utilityMain, WorkerService
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks
from twisted.python.text import wordWrap
from twisted.python.usage import Options, UsageError
from twistedcaldav import customxml
from twistedcaldav.ical import Component
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from twistedcaldav.timezones import TimezoneCache
from txdav.base.propertystore.base import PropertyName
from txdav.caldav.datastore.scheduling.cuaddress import LocalCalendarUser
from txdav.caldav.datastore.scheduling.itip import iTipGenerator
from txdav.caldav.datastore.scheduling.processing import ImplicitProcessor
from txdav.caldav.icalendarstore import ComponentUpdateState
from txdav.common.icommondatastore import UIDExistsError
from txdav.xml import element as davxml

log = Logger()


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        ImportOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = '\n'.join(
    wordWrap(
        """
        Usage: calendarserver_import [options] [input specifiers]\n
        """ + __doc__,
        int(os.environ.get('COLUMNS', '80'))
    )
)


class ImportException(Exception):
    """
    An error occurred during import
    """


class ImportOptions(Options):
    """
    Command-line options for 'calendarserver_import'
    """

    synopsis = description

    optFlags = [
        ['debug', 'D', "Debug logging."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
    ]

    def __init__(self):
        super(ImportOptions, self).__init__()
        self.inputName = '-'
        self.inputDirectoryName = None

    def opt_directory(self, dirname):
        """
        Specify input directory path.
        """
        self.inputDirectoryName = dirname

    opt_d = opt_directory

    def opt_input(self, filename):
        """
        Specify input file path (default: '-', meaning stdin).
        """
        self.inputName = filename

    opt_i = opt_input

    def openInput(self):
        """
        Open the appropriate input file based on the '--input' option.
        """
        if self.inputName == '-':
            return sys.stdin
        else:
            return open(self.inputName, 'r')


# These could probably live on the collection class:

def setCollectionPropertyValue(collection, element, value):
    collectionProperties = collection.properties()
    collectionProperties[PropertyName.fromElement(element)] = (
        element.fromString(value)
    )


def getCollectionPropertyValue(collection, element):
    collectionProperties = collection.properties()
    name = PropertyName.fromElement(element)
    if name in collectionProperties:
        return str(collectionProperties[name])
    else:
        return None


@inlineCallbacks
def importCollectionComponent(store, component):
    """
    Import a component representing a collection (e.g. VCALENDAR) into the
    store.

    The homeUID and collection resource name the component will be imported
    into is derived from the SOURCE property on the VCALENDAR (which must
    be present).  The code assumes it will be a URI with slash-separated parts
    with the penultimate part specifying the homeUID and the last part
    specifying the calendar resource name.  The NAME property will be used
    to set the DAV:display-name, while the COLOR property will be used to set
    calendar-color.

    Subcomponents (e.g. VEVENTs) are grouped into resources by UID.  Objects
    which have a UID already in use within the home will be skipped.

    @param store: The db store to add the component to
    @type store: L{IDataStore}
    @param component: The component to store
    @type component: L{twistedcaldav.ical.Component}
    """

    sourceURI = component.propertyValue("SOURCE")
    if not sourceURI:
        raise ImportException("Calendar is missing SOURCE property")

    ownerUID, collectionResourceName = sourceURI.strip("/").split("/")[-2:]

    dir = store.directoryService()
    ownerRecord = yield dir.recordWithUID(ownerUID)
    if not ownerRecord:
        raise ImportException("{} is not in the directory".format(ownerUID))

    # Set properties on the collection
    txn = store.newTransaction()
    home = yield txn.calendarHomeWithUID(ownerUID, create=True)
    collection = yield home.childWithName(collectionResourceName)
    if not collection:
        print("Creating calendar: {}".format(collectionResourceName))
        collection = yield home.createChildWithName(collectionResourceName)
    for propertyName, element in (
        ("NAME", davxml.DisplayName),
        ("COLOR", customxml.CalendarColor),
    ):
        value = component.propertyValue(propertyName)
        if value is not None:
            setCollectionPropertyValue(collection, element, value)
            print(
                "Setting {name} to {value}".format(name=propertyName, value=value)
            )
    yield txn.commit()

    # Populate the collection; NB we use a txn for each object, and we might
    # want to batch them?
    groupedComponents = Component.componentsFromComponent(component)
    for groupedComponent in groupedComponents:

        try:
            uid = list(groupedComponent.subcomponents())[0].propertyValue("UID")
        except:
            continue

        # If event is unscheduled or the organizer matches homeUID, store the
        # component

        print("Event UID: {}".format(uid))
        storeDirectly = True
        organizer = groupedComponent.getOrganizer()
        if organizer is not None:
            organizerRecord = yield dir.recordWithCalendarUserAddress(organizer)
            if organizerRecord is None:
                # Organizer does not exist, so skip this event
                continue
            else:
                if ownerRecord.uid != organizerRecord.uid:
                    # Owner is not the organizer
                    storeDirectly = False

        if storeDirectly:
            resourceName = "{}.ics".format(str(uuid.uuid4()))
            try:
                yield storeComponentInHomeAndCalendar(
                    store, groupedComponent, ownerUID, collectionResourceName,
                    resourceName
                )
                print("Imported: {}".format(uid))
            except UIDExistsError:
                # That event is already in the home
                print("Skipping since UID already exists: {}".format(uid))

            except Exception as e:
                print(
                    "Failed to import due to: {error}\n{comp}".format(
                        error=e,
                        comp=groupedComponent
                    )
                )

        else:
            # Owner is an attendee, not the organizer
            # Apply the PARTSTATs from the import and from the possibly
            # existing event (existing event takes precedence) to the
            # organizer's copy.

            # Put the attendee copy into the right calendar now otherwise it
            # could end up on the default calendar when the change to the
            # organizer's copy causes an attendee update
            resourceName = "{}.ics".format(str(uuid.uuid4()))
            try:
                yield storeComponentInHomeAndCalendar(
                    store, groupedComponent, ownerUID, collectionResourceName,
                    resourceName, asAttendee=True
                )
                print("Imported: {}".format(uid))
            except UIDExistsError:
                # No need since the event is already in the home
                pass

            # Now use the iTip reply processing to update the organizer's copy
            # with the PARTSTATs from the component we're restoring.
            attendeeCUA = ownerRecord.canonicalCalendarUserAddress()
            organizerCUA = organizerRecord.canonicalCalendarUserAddress()
            processor = ImplicitProcessor()
            newComponent = iTipGenerator.generateAttendeeReply(groupedComponent, attendeeCUA, method="X-RESTORE")
            if newComponent is not None:
                txn = store.newTransaction()
                yield processor.doImplicitProcessing(
                    txn,
                    newComponent,
                    LocalCalendarUser(attendeeCUA, ownerRecord),
                    LocalCalendarUser(organizerCUA, organizerRecord)
                )
                yield txn.commit()


@inlineCallbacks
def storeComponentInHomeAndCalendar(
    store, component, homeUID, collectionResourceName, objectResourceName,
    asAttendee=False
):
    """
    Add a component to the store as an objectResource

    If the calendar home does not yet exist for homeUID it will be created.
    If the collection by the name collectionResourceName does not yet exist
    it will be created.

    @param store: The db store to add the component to
    @type store: L{IDataStore}
    @param component: The component to store
    @type component: L{twistedcaldav.ical.Component}
    @param homeUID: uid of the home collection
    @type collectionResourceName: C{str}
    @param collectionResourceName: name of the collection resource
    @type collectionResourceName: C{str}
    @param objectResourceName: name of the objectresource
    @type objectResourceName: C{str}
    """
    txn = store.newTransaction()
    home = yield txn.calendarHomeWithUID(homeUID, create=True)
    collection = yield home.childWithName(collectionResourceName)
    if not collection:
        collection = yield home.createChildWithName(collectionResourceName)

    yield collection._createCalendarObjectWithNameInternal(
        objectResourceName, component,
        (
            ComponentUpdateState.ATTENDEE_ITIP_UPDATE
            if asAttendee else
            ComponentUpdateState.NORMAL
        )
    )
    yield txn.commit()


class ImporterService(WorkerService, object):
    """
    Service which runs, imports the data, then stops the reactor.
    """

    def __init__(self, store, options, reactor, config):
        super(ImporterService, self).__init__(store)
        self.options = options
        self.reactor = reactor
        self.config = config
        self._directory = self.store.directoryService()

        TimezoneCache.create()

    @inlineCallbacks
    def doWork(self):
        """
        Do the export, stopping the reactor when done.
        """
        try:
            if self.options.inputDirectoryName:
                dirname = self.options.inputDirectoryName
                if not os.path.exists(dirname):
                    sys.stderr.write(
                        "Directory does not exist: {}\n".format(dirname)
                    )
                    sys.exit(1)
                for filename in os.listdir(dirname):
                    fullpath = os.path.join(dirname, filename)
                    print("Importing {}".format(fullpath))
                    with open(fullpath, 'r') as fileobj:
                        component = Component.allFromStream(fileobj)
                    yield importCollectionComponent(self.store, component)

            else:
                try:
                    input = self.options.openInput()
                except IOError as e:
                    sys.stderr.write(
                        "Unable to open input file for reading: %s\n" % (e)
                    )
                    sys.exit(1)

                component = Component.allFromStream(input)
                input.close()
                yield importCollectionComponent(self.store, component)
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
        # TODO: stopping this service mid-import should really stop the import
        # loop, but this is not implemented because nothing will actually do it
        # except hitting ^C (which also calls reactor.stop(), so that will exit
        # anyway).


def main(argv=sys.argv, reactor=None):
    """
    Do the import.
    """
    if reactor is None:
        from twisted.internet import reactor

    options = ImportOptions()
    try:
        options.parseOptions(argv[1:])
    except UsageError as e:
        usage(e)

    def makeService(store):
        from twistedcaldav.config import config
        return ImporterService(store, options, reactor, config)

    utilityMain(options["config"], makeService, reactor, verbose=options["debug"])
