# -*- test-case-name: calendarserver.tools.shell.test.test_vfs -*-
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
Virtual file system for data store objects.
"""

__all__ = [
    "ListEntry",
    "File",
    "Folder",
    "RootFolder",
    "UIDsFolder",
    "RecordFolder",
    "UsersFolder",
    "LocationsFolder",
    "ResourcesFolder",
    "GroupsFolder",
    "PrincipalHomeFolder",
    "CalendarHomeFolder",
    "CalendarFolder",
    "CalendarObject",
    "AddressBookHomeFolder",
]


from cStringIO import StringIO
from time import strftime, localtime

from twisted.internet.defer import succeed
from twisted.internet.defer import inlineCallbacks, returnValue

from twext.python.log import Logger

from txdav.common.icommondatastore import NotFoundError

from twistedcaldav.ical import InvalidICalendarDataError

from calendarserver.tools.tables import Table
from calendarserver.tools.shell.directory import recordInfo

log = Logger()


class ListEntry(object):
    """
    Information about a C{File} as returned by C{File.list()}.
    """

    def __init__(self, parent, Class, Name, **fields):
        self.parent = parent  # The class implementing list()
        self.fileClass = Class
        self.fileName = Name
        self.fields = fields

        fields["Name"] = Name

    def __str__(self):
        return self.toString()

    def __repr__(self):
        fields = self.fields.copy()
        del fields["Name"]

        if fields:
            fields = " %s" % (fields,)
        else:
            fields = ""

        return "<%s(%s): %r%s>" % (
            self.__class__.__name__,
            self.fileClass.__name__,
            self.fileName,
            fields,
        )

    def isFolder(self):
        return issubclass(self.fileClass, Folder)

    def toString(self):
        if self.isFolder():
            return "%s/" % (self.fileName,)
        else:
            return self.fileName

    @property
    def fieldNames(self):
        if not hasattr(self, "_fieldNames"):
            if hasattr(self.parent.list, "fieldNames"):
                if "Name" in self.parent.list.fieldNames:
                    self._fieldNames = tuple(self.parent.list.fieldNames)
                else:
                    self._fieldNames = ("Name",) + tuple(self.parent.list.fieldNames)
            else:
                self._fieldNames = ["Name"] + sorted(n for n in self.fields
                                                     if n != "Name")

        return self._fieldNames

    def toFields(self):
        try:
            return tuple(self.fields[fieldName] for fieldName in self.fieldNames)
        except KeyError as e:
            raise AssertionError(
                "Field %s is not in %r, defined by %s"
                % (e, self.fields.keys(), self.parent.__name__)
            )


class File(object):
    """
    Object in virtual data hierarchy.
    """

    def __init__(self, service, path):
        assert type(path) is tuple

        self.service = service
        self.path = path

    def __str__(self):
        return "/" + "/".join(self.path)

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self)

    def __eq__(self, other):
        if isinstance(other, File):
            return self.path == other.path
        else:
            return NotImplemented

    def describe(self):
        return succeed("%s (%s)" % (self, self.__class__.__name__))

    def list(self):
        return succeed((
            ListEntry(self, self.__class__, self.path[-1]),
        ))


class Folder(File):
    """
    Location in virtual data hierarchy.
    """

    def __init__(self, service, path):
        File.__init__(self, service, path)

        self._children = {}
        self._childClasses = {}

    def __str__(self):
        if self.path:
            return "/" + "/".join(self.path) + "/"
        else:
            return "/"

    @inlineCallbacks
    def locate(self, path):
        if path and path[-1] == "":
            path.pop()

        if not path:
            returnValue(RootFolder(self.service))

        name = path[0]
        if name:
            target = (yield self.child(name))
            if len(path) > 1:
                target = (yield target.locate(path[1:]))
        else:
            target = (yield RootFolder(self.service).locate(path[1:]))

        returnValue(target)

    @inlineCallbacks
    def child(self, name):
        # FIXME: Move this logic to locate()
        # if not name:
        #     return succeed(self)
        # if name == ".":
        #     return succeed(self)
        # if name == "..":
        #     path = self.path[:-1]
        #     if not path:
        #         path = "/"
        #     return RootFolder(self.service).locate(path)

        if name in self._children:
            returnValue(self._children[name])

        if name in self._childClasses:
            child = (yield self._childClasses[name](self.service, self.path + (name,)))
            self._children[name] = child
            returnValue(child)

        raise NotFoundError("Folder %r has no child %r" % (str(self), name))

    def list(self):
        result = {}
        for name in self._children:
            result[name] = ListEntry(self, self._children[name].__class__, name)
        for name in self._childClasses:
            if name not in result:
                result[name] = ListEntry(self, self._childClasses[name], name)
        return succeed(result.itervalues())


class RootFolder(Folder):
    """
    Root of virtual data hierarchy.

    Hierarchy::

      /                    RootFolder
        uids/              UIDsFolder
          <uid>/           PrincipalHomeFolder
            calendars/     CalendarHomeFolder
              <name>/      CalendarFolder
                <uid>      CalendarObject
            addressbooks/  AddressBookHomeFolder
              <name>/      AddressBookFolder
                <uid>      AddressBookObject
        users/             UsersFolder
          <name>/          PrincipalHomeFolder
            ...
        locations/         LocationsFolder
          <name>/          PrincipalHomeFolder
            ...
        resources/         ResourcesFolder
          <name>/          PrincipalHomeFolder
            ...
    """

    def __init__(self, service):
        Folder.__init__(self, service, ())

        self._childClasses["uids"] = UIDsFolder
        self._childClasses["users"] = UsersFolder
        self._childClasses["locations"] = LocationsFolder
        self._childClasses["resources"] = ResourcesFolder
        self._childClasses["groups"] = GroupsFolder


class UIDsFolder(Folder):
    """
    Folder containing all principals by UID.
    """

    def child(self, name):
        return PrincipalHomeFolder(self.service, self.path + (name,), name)

    @inlineCallbacks
    def list(self):
        results = {}

        # FIXME: This should be the merged total of calendar homes and address book homes.
        # FIXME: Merge in directory UIDs also?
        # FIXME: Add directory info (eg. name) to list entry

        @inlineCallbacks
        def addResult(ignoredTxn, home):
            uid = home.uid()

            record = yield self.service.directory.recordWithUID(uid)
            if record:
                info = {
                    "Record Type": record.recordType,
                    "Short Name": record.shortNames[0],
                    "Full Name": record.fullName,
                }
            else:
                info = {}

            results[uid] = ListEntry(self, PrincipalHomeFolder, uid, **info)
        yield self.service.store.withEachCalendarHomeDo(addResult)
        yield self.service.store.withEachAddressbookHomeDo(addResult)
        returnValue(results.itervalues())


class RecordFolder(Folder):

    def _recordForName(self, name):
        recordTypeAttr = "recordType_" + self.recordType
        recordType = getattr(self.service.directory, recordTypeAttr)
        return self.service.directory.recordWithShortName(recordType, name)

    def child(self, name):
        record = self._recordForName(name)

        if record is None:
            return Folder.child(self, name)

        return PrincipalHomeFolder(
            self.service,
            self.path + (name,),
            record.uid,
            record=record
        )

    @inlineCallbacks
    def list(self):
        names = set()

        for record in (yield self.service.directory.recordsWithRecordType(
            self.recordType
        )):
            for shortName in record.shortNames:
                if shortName in names:
                    continue
                names.add(shortName)
                yield ListEntry(
                    self,
                    PrincipalHomeFolder,
                    shortName,
                    **{
                        "UID": record.uid,
                        "Full Name": record.fullName,
                    }
                )

    list.fieldNames = ("UID", "Full Name")


class UsersFolder(RecordFolder):
    """
    Folder containing all user principals by name.
    """
    recordType = "users"


class LocationsFolder(RecordFolder):
    """
    Folder containing all location principals by name.
    """
    recordType = "locations"


class ResourcesFolder(RecordFolder):
    """
    Folder containing all resource principals by name.
    """
    recordType = "resources"


class GroupsFolder(RecordFolder):
    """
    Folder containing all group principals by name.
    """
    recordType = "groups"


class PrincipalHomeFolder(Folder):
    """
    Folder containing everything related to a given principal.
    """

    def __init__(self, service, path, uid, record=None):
        Folder.__init__(self, service, path)

        if record is None:
            # FIXME: recordWithUID returns a Deferred but we cannot return or yield it in an __init__ method
            record = self.service.directory.recordWithUID(uid)

        if record is not None:
            assert uid == record.uid

        self.uid = uid
        self.record = record

    @inlineCallbacks
    def _initChildren(self):
        if not hasattr(self, "_didInitChildren"):
            txn = self.service.store.newTransaction()

            try:
                if (
                    self.record is not None and
                    self.service.config.EnableCalDAV and
                    self.record.hasCalendars
                ):
                    create = True
                else:
                    create = False

                # Try assuming it exists
                home = (yield txn.calendarHomeWithUID(self.uid, create=False))

                if home is None and create:
                    # Doesn't exist, so create it in a different
                    # transaction, to avoid having to commit the live
                    # transaction.
                    txnTemp = self.service.store.newTransaction()
                    try:
                        home = (yield txnTemp.calendarHomeWithUID(self.uid, create=True))
                        (yield txnTemp.commit())

                        # Fetch the home again. This time we expect it to be there.
                        home = (yield txn.calendarHomeWithUID(self.uid, create=False))
                        assert home
                    finally:
                        (yield txn.abort())

                if home:
                    self._children["calendars"] = CalendarHomeFolder(
                        self.service,
                        self.path + ("calendars",),
                        home,
                        self.record,
                    )

                if (
                    self.record is not None and
                    self.service.config.EnableCardDAV and
                    self.record.enabledForAddressBooks
                ):
                    create = True
                else:
                    create = False

                # Again, assume it exists
                home = (yield txn.addressbookHomeWithUID(self.uid))

                if not home and create:
                    # Repeat the above dance.
                    txnTemp = self.service.store.newTransaction()
                    try:
                        home = (yield txnTemp.addressbookHomeWithUID(self.uid, create=True))
                        (yield txnTemp.commit())

                        # Fetch the home again. This time we expect it to be there.
                        home = (yield txn.addressbookHomeWithUID(self.uid, create=False))
                        assert home
                    finally:
                        (yield txn.abort())

                if home:
                    self._children["addressbooks"] = AddressBookHomeFolder(
                        self.service,
                        self.path + ("addressbooks",),
                        home,
                        self.record,
                    )
            finally:
                (yield txn.abort())

        self._didInitChildren = True

    def _needsChildren(m):  # @NoSelf
        def decorate(self, *args, **kwargs):
            d = self._initChildren()
            d.addCallback(lambda _: m(self, *args, **kwargs))
            return d
        return decorate

    @_needsChildren
    def child(self, name):
        return Folder.child(self, name)

    @_needsChildren
    def list(self):
        return Folder.list(self)

    def describe(self):
        return recordInfo(self.service.directory, self.record)


class CalendarHomeFolder(Folder):
    """
    Calendar home folder.
    """

    def __init__(self, service, path, home, record):
        Folder.__init__(self, service, path)

        self.home = home
        self.record = record

    @inlineCallbacks
    def child(self, name):
        calendar = (yield self.home.calendarWithName(name))
        if calendar:
            returnValue(CalendarFolder(self.service, self.path + (name,), calendar))
        else:
            raise NotFoundError("Calendar home %r has no calendar %r" % (self, name))

    @inlineCallbacks
    def list(self):
        calendars = (yield self.home.calendars())
        result = []
        for calendar in calendars:
            displayName = calendar.displayName()
            if displayName is None:
                displayName = "(unset)"

            info = {
                "Display Name": displayName,
                "Sync Token": (yield calendar.syncToken()),
            }
            result.append(ListEntry(self, CalendarFolder, calendar.name(), **info))
        returnValue(result)

    @inlineCallbacks
    def describe(self):
        description = ["Calendar home:\n"]

        #
        # Attributes
        #
        uid = (yield self.home.uid())
        created = (yield self.home.created())
        modified = (yield self.home.modified())
        quotaUsed = (yield self.home.quotaUsedBytes())
        quotaAllowed = (yield self.home.quotaAllowedBytes())

        recordType = (yield self.record.recordType)
        recordShortName = (yield self.record.shortNames[0])

        rows = []
        rows.append(("UID", uid))
        rows.append(("Owner", "(%s)%s" % (recordType, recordShortName)))
        rows.append(("Created", timeString(created)))
        rows.append(("Last modified", timeString(modified)))
        if quotaUsed is not None:
            rows.append((
                "Quota",
                "%s of %s (%.2s%%)"
                % (quotaUsed, quotaAllowed, quotaUsed / quotaAllowed)
            ))

        description.append("Attributes:")
        description.append(tableString(rows))

        #
        # Properties
        #
        properties = (yield self.home.properties())
        if properties:
            description.append(tableStringForProperties(properties))

        returnValue("\n".join(description))


class CalendarFolder(Folder):
    """
    Calendar.
    """

    def __init__(self, service, path, calendar):
        Folder.__init__(self, service, path)

        self.calendar = calendar

    @inlineCallbacks
    def _childWithObject(self, object):
        uid = (yield object.uid())
        returnValue(CalendarObject(self.service, self.path + (uid,), object, uid))

    @inlineCallbacks
    def child(self, name):
        object = (yield self.calendar.calendarObjectWithUID(name))

        if not object:
            raise NotFoundError("Calendar %r has no object %r" % (str(self), name))

        child = (yield self._childWithObject(object))
        returnValue(child)

    @inlineCallbacks
    def list(self):
        result = []

        for object in (yield self.calendar.calendarObjects()):
            object = (yield self._childWithObject(object))
            items = (yield object.list())
            result.append(items[0])

        returnValue(result)

    @inlineCallbacks
    def describe(self):
        description = ["Calendar:\n"]

        #
        # Attributes
        #
        ownerHome = (yield self.calendar.ownerCalendarHome())  # FIXME: Translate into human
        syncToken = (yield self.calendar.syncToken())

        rows = []
        rows.append(("Owner", ownerHome))
        rows.append(("Sync Token", syncToken))

        description.append("Attributes:")
        description.append(tableString(rows))

        #
        # Properties
        #
        properties = (yield self.calendar.properties())
        if properties:
            description.append(tableStringForProperties(properties))

        returnValue("\n".join(description))

    def delete(self, implicit=True):

        if implicit:
            # We need data store-level scheduling support to implement
            # this.
            raise NotImplementedError("Delete not implemented.")
        else:
            self.calendarObject.remove()


class CalendarObject(File):
    """
    Calendar object.
    """

    def __init__(self, service, path, calendarObject, uid):
        File.__init__(self, service, path)

        self.object = calendarObject
        self.uid = uid

    @inlineCallbacks
    def lookup(self):
        if not hasattr(self, "component"):
            component = (yield self.object.component())

            try:
                mainComponent = component.mainComponent()

                assert self.uid == mainComponent.propertyValue("UID")

                self.componentType = mainComponent.name()
                self.summary = mainComponent.propertyValue("SUMMARY")
                self.mainComponent = mainComponent

            except InvalidICalendarDataError as e:
                log.error("{path}: {ex}", path=self.path, ex=e)

                self.componentType = "?"
                self.summary = "** Invalid data **"
                self.mainComponent = None

            self.component = component

    @inlineCallbacks
    def list(self):
        (yield self.lookup())
        returnValue((ListEntry(self, CalendarObject, self.uid, {
            "Component Type": self.componentType,
            "Summary": self.summary.replace("\n", " "),
        }),))

    list.fieldNames = ("Component Type", "Summary")

    @inlineCallbacks
    def text(self):
        (yield self.lookup())
        returnValue(str(self.component))

    @inlineCallbacks
    def describe(self):
        (yield self.lookup())

        description = ["Calendar object:\n"]

        #
        # Calendar object attributes
        #
        rows = []

        rows.append(("UID", self.uid))
        rows.append(("Component Type", self.componentType))
        rows.append(("Summary", self.summary))

        organizer = self.mainComponent.getProperty("ORGANIZER")
        if organizer:
            organizerName = organizer.parameterValue("CN")
            organizerEmail = organizer.parameterValue("EMAIL")

            name = " (%s)" % (organizerName,) if organizerName else ""
            email = " <%s>" % (organizerEmail,) if organizerEmail else ""

            rows.append(("Organizer", "%s%s%s" % (organizer.value(), name, email)))

        rows.append(("Created", timeString(self.object.created())))
        rows.append(("Modified", timeString(self.object.modified())))

        description.append("Attributes:")
        description.append(tableString(rows))

        #
        # Attachments
        #
        attachments = (yield self.object.attachments())
        for attachment in attachments:
            contentType = attachment.contentType()
            contentType = "%s/%s" % (contentType.mediaType, contentType.mediaSubtype)

            rows = []
            rows.append(("Name", attachment.name()))
            rows.append(("Size", "%s bytes" % (attachment.size(),)))
            rows.append(("Content Type", contentType))
            rows.append(("MD5 Sum", attachment.md5()))
            rows.append(("Created", timeString(attachment.created())))
            rows.append(("Modified", timeString(attachment.modified())))

            description.append("Attachment:")
            description.append(tableString(rows))

        #
        # Properties
        #
        properties = (yield self.object.properties())
        if properties:
            description.append(tableStringForProperties(properties))

        returnValue("\n".join(description))


class AddressBookHomeFolder(Folder):
    """
    Address book home folder.
    """

    def __init__(self, service, path, home, record):
        Folder.__init__(self, service, path)

        self.home = home
        self.record = record

    # FIXME


def tableString(rows, header=None):
    table = Table()
    if header:
        table.addHeader(header)
    for row in rows:
        table.addRow(row)

    output = StringIO()
    table.printTable(os=output)
    return output.getvalue()


def tableStringForProperties(properties):
    return "Properties:\n%s" % (tableString((
        (name.toString(), truncateAtNewline(properties[name]))
        for name in sorted(properties)
    )))


def timeString(time):
    if time is None:
        return "(unknown)"

    return strftime("%a, %d %b %Y %H:%M:%S %z(%Z)", localtime(time))


def truncateAtNewline(text):
    text = str(text)
    try:
        index = text.index("\n")
    except ValueError:
        return text

    return text[:index] + "..."
