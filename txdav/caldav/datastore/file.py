# -*- test-case-name: txdav.caldav.datastore.test.test_file -*-
##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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
from twistedcaldav.datafilters.peruserdata import PerUserDataFilter

"""
File calendar store.
"""

__all__ = [
    "CalendarStore",
    "CalendarStoreTransaction",
    "CalendarHome",
    "Calendar",
    "CalendarObject",
]

import hashlib
import uuid

from errno import ENOENT

from twisted.internet.defer import inlineCallbacks, returnValue, succeed, fail

from twistedcaldav.ical import Component as VComponent
from txdav.xml import element as davxml
from txdav.xml.rfc2518 import GETContentType
from txweb2.dav.resource import TwistedGETContentMD5
from txweb2.http_headers import generateContentType, MimeType

from twistedcaldav import caldavxml, customxml, ical
from twistedcaldav.caldavxml import ScheduleCalendarTransp, Opaque, Transparent
from twistedcaldav.config import config
from twistedcaldav.ical import InvalidICalendarDataError

from txdav.caldav.icalendarstore import IAttachment, AttachmentSizeTooLarge
from txdav.caldav.icalendarstore import ICalendar, ICalendarObject
from txdav.caldav.icalendarstore import ICalendarHome

from txdav.caldav.datastore.index_file import Index as OldIndex, \
    IndexSchedule as OldInboxIndex
from txdav.caldav.datastore.util import (
    validateCalendarComponent, dropboxIDFromCalendarObject, CalendarObjectBase,
    StorageTransportBase, AttachmentRetrievalTransport
)

from txdav.common.datastore.file import (
    CommonDataStore, CommonStoreTransaction, CommonHome, CommonHomeChild,
    CommonObjectResource, CommonStubResource)
from txdav.caldav.icalendarstore import QuotaExceeded

from txdav.common.icommondatastore import ConcurrentModification
from txdav.common.icommondatastore import InternalDataStoreError
from txdav.base.datastore.file import writeOperation, hidden, FileMetaDataMixin
from txdav.base.propertystore.base import PropertyName

from zope.interface import implements

contentTypeKey = PropertyName.fromElement(GETContentType)
md5key = PropertyName.fromElement(TwistedGETContentMD5)

CalendarStore = CommonDataStore

CalendarStoreTransaction = CommonStoreTransaction

IGNORE_NAMES = ('dropbox', 'notification', 'freebusy')


class CalendarHome(CommonHome):
    implements(ICalendarHome)

    _topPath = "calendars"
    _notifierPrefix = "CalDAV"

    _componentCalendarName = {
        "VEVENT": "calendar",
        "VTODO": "tasks",
        "VJOURNAL": "journals",
        "VAVAILABILITY": "available",
        "VPOLL": "polls",
    }

    def __init__(self, uid, path, calendarStore, transaction):
        super(CalendarHome, self).__init__(uid, path, calendarStore, transaction)

        self._childClass = Calendar

    createCalendarWithName = CommonHome.createChildWithName
    removeCalendarWithName = CommonHome.removeChildWithName

    def childWithName(self, name):
        if name in IGNORE_NAMES:
            # "dropbox" is a file storage area, not a calendar.
            return None
        else:
            return super(CalendarHome, self).childWithName(name)

    calendarWithName = childWithName

    def children(self):
        """
        Return a generator of the child resource objects.
        """
        for child in self.listCalendars():
            yield self.calendarWithName(child)

    calendars = children

    def listChildren(self):
        """
        Return a generator of the child resource names.
        """
        for name in super(CalendarHome, self).listChildren():
            if name in IGNORE_NAMES:
                continue
            yield name

    listCalendars = listChildren
    loadCalendars = CommonHome.loadChildren

    @inlineCallbacks
    def hasCalendarResourceUIDSomewhereElse(self, uid, ok_object, type):

        objectResources = (yield self.getCalendarResourcesForUID(uid))
        for objectResource in objectResources:
            if ok_object and objectResource._path == ok_object._path:
                continue
            matched_type = "schedule" if objectResource.isScheduleObject else "calendar"
            if type == "schedule" or matched_type == "schedule":
                returnValue(objectResource)

        returnValue(None)

    @inlineCallbacks
    def getCalendarResourcesForUID(self, uid):

        results = (yield self.objectResourcesWithUID(uid, ("inbox",)))
        returnValue(results)

    @inlineCallbacks
    def calendarObjectWithDropboxID(self, dropboxID):
        """
        Implement lookup with brute-force scanning.
        """
        for calendar in self.calendars():
            for calendarObject in calendar.calendarObjects():
                if dropboxID == (yield calendarObject.dropboxID()):
                    returnValue(calendarObject)

    @inlineCallbacks
    def getAllDropboxIDs(self):
        dropboxIDs = []
        for calendar in self.calendars():
            for calendarObject in calendar.calendarObjects():
                component = calendarObject.component()
                if (
                    component.hasPropertyInAnyComponent("X-APPLE-DROPBOX") or
                    component.hasPropertyInAnyComponent("ATTACH")
                ):
                    dropboxID = (yield calendarObject.dropboxID())
                    dropboxIDs.append(dropboxID)
        returnValue(dropboxIDs)

    @property
    def _calendarStore(self):
        return self._dataStore

    def createdHome(self):

        # Check whether components type must be separate
        if config.RestrictCalendarsToOneComponentType:
            for name in ical.allowedStoreComponents:
                cal = self.createCalendarWithName(self._componentCalendarName[name])
                cal.setSupportedComponents(name)
                props = cal.properties()
                if name not in ("VEVENT", "VAVAILABILITY",):
                    props[PropertyName(*ScheduleCalendarTransp.qname())] = ScheduleCalendarTransp(Transparent())
                else:
                    props[PropertyName(*ScheduleCalendarTransp.qname())] = ScheduleCalendarTransp(Opaque())
        else:
            cal = self.createCalendarWithName("calendar")

        self.createCalendarWithName("inbox")

    def ensureDefaultCalendarsExist(self):
        """
        Double check that we have calendars supporting at least VEVENT and VTODO,
        and create if missing.
        """

        # Double check that we have calendars supporting at least VEVENT and VTODO
        if config.RestrictCalendarsToOneComponentType:
            supported_components = set()
            names = set()
            for calendar in self.calendars():
                if calendar.name() == "inbox":
                    continue
                names.add(calendar.name())
                result = calendar.getSupportedComponents()
                supported_components.update(result.split(","))

            def _requireCalendarWithType(support_component, tryname):
                if support_component not in supported_components:
                    newname = tryname
                    if newname in names:
                        newname = str(uuid.uuid4())
                    newcal = self.createCalendarWithName(newname)
                    newcal.setSupportedComponents(support_component)

            _requireCalendarWithType("VEVENT", "calendar")
            _requireCalendarWithType("VTODO", "tasks")


class Calendar(CommonHomeChild):
    """
    File-based implementation of L{ICalendar}.
    """
    implements(ICalendar)

    def __init__(self, name, calendarHome, owned, realName=None):
        """
        Initialize a calendar pointing at a path on disk.

        @param name: the subdirectory of calendarHome where this calendar
            resides.
        @type name: C{str}

        @param calendarHome: the home containing this calendar.
        @type calendarHome: L{CalendarHome}

        @param realName: If this calendar was just created, the name which it
        will eventually have on disk.
        @type realName: C{str}
        """
        super(Calendar, self).__init__(name, calendarHome, owned, realName=realName)

        self._index = Index(self)
        self._objectResourceClass = CalendarObject

    @property
    def _calendarHome(self):
        return self._home

    ownerCalendarHome = CommonHomeChild.ownerHome
    viewerCalendarHome = CommonHomeChild.viewerHome
    calendarObjects = CommonHomeChild.objectResources
    listCalendarObjects = CommonHomeChild.listObjectResources
    calendarObjectWithName = CommonHomeChild.objectResourceWithName
    calendarObjectWithUID = CommonHomeChild.objectResourceWithUID
    createCalendarObjectWithName = CommonHomeChild.createObjectResourceWithName
    calendarObjectsSinceToken = CommonHomeChild.objectResourcesSinceToken

    def _createCalendarObjectWithNameInternal(self, name, component, internal_state, options=None):
        return self.createCalendarObjectWithName(name, component, options)

    def calendarObjectsInTimeRange(self, start, end, timeZone):
        raise NotImplementedError()

    def setSupportedComponents(self, supported_components):
        """
        Update the private property with the supported components. Technically this should only happen once
        on collection creation, but for migration we may need to change after the fact - hence a separate api.
        """

        pname = PropertyName.fromElement(customxml.TwistedCalendarSupportedComponents)
        if supported_components:
            self.properties()[pname] = customxml.TwistedCalendarSupportedComponents.fromString(supported_components)
        elif pname in self.properties():
            del self.properties()[pname]

    def getSupportedComponents(self):
        result = str(self.properties().get(PropertyName.fromElement(customxml.TwistedCalendarSupportedComponents), ""))
        return result if result else None

    def isSupportedComponent(self, componentType):
        supported = self.getSupportedComponents()
        if supported:
            return componentType.upper() in supported.split(",")
        else:
            return True

    def initPropertyStore(self, props):
        # Setup peruser special properties
        props.setSpecialProperties(
            (
                PropertyName.fromElement(caldavxml.CalendarDescription),
                PropertyName.fromElement(caldavxml.CalendarTimeZone),
            ),
            (
                PropertyName.fromElement(customxml.GETCTag),
                PropertyName.fromElement(caldavxml.SupportedCalendarComponentSet),
            ),
            (),
        )

    def contentType(self):
        """
        The content type of Calendar objects is text/calendar.
        """
        return MimeType.fromString("text/calendar; charset=utf-8")

    def splitCollectionByComponentTypes(self):
        """
        If the calendar contains iCalendar data with different component types, then split it into separate collections
        each containing only one component type. When doing this make sure properties and sharing state are preserved
        on any new calendars created.
        """

        # TODO: implement this for filestore
        pass

    def _countComponentTypes(self):
        """
        Count each component type in this calendar.

        @return: a C{tuple} of C{tuple} containing the component type name and count.
        """

        rows = self._index._oldIndex.componentTypeCounts()
        result = tuple([(componentType, componentCount) for componentType, componentCount in sorted(rows, key=lambda x:x[0])])
        return result

    def _splitComponentType(self, component):
        """
        Create a new calendar and move all components of the specified component type into the new one.
        Make sure properties and sharing state is preserved on the new calendar.

        @param component: Component type to split out
        @type component: C{str}
        """

        # TODO: implement this for filestore
        pass

    def _transferSharingDetails(self, newcalendar, component):
        """
        If the current calendar is shared, make the new calendar shared in the same way, but tweak the name.
        """

        # TODO: implement this for filestore
        pass

    def _transferCalendarObjects(self, newcalendar, component):
        """
        Move all calendar components of the specified type to the specified calendar.
        """

        # TODO: implement this for filestore
        pass


class CalendarObject(CommonObjectResource, CalendarObjectBase):
    """
    @ivar _path: The path of the .ics file on disk

    @type _path: L{FilePath}
    """
    implements(ICalendarObject)

    def __init__(self, name, calendar, metadata=None):
        super(CalendarObject, self).__init__(name, calendar)
        self._attachments = {}

        if metadata is not None:
            self.accessMode = metadata.get("accessMode", "")
            self.isScheduleObject = metadata.get("isScheduleObject", False)
            self.scheduleTag = metadata.get("scheduleTag", "")
            self.scheduleEtags = metadata.get("scheduleEtags", "")
            self.hasPrivateComment = metadata.get("hasPrivateComment", False)

    @property
    def _calendar(self):
        return self._parentCollection

    def calendar(self):
        return self._calendar

    @writeOperation
    def setComponent(self, component, inserting=False):

        validateCalendarComponent(self, self._calendar, component, inserting, self._transaction._migrating)

        self._calendar.retrieveOldIndex().addResource(
            self.name(), component
        )

        componentText = str(component)
        self._objectText = componentText

        def do():
            # Mark all properties as dirty, so they can be added back
            # to the newly updated file.
            self.properties().update(self.properties())

            backup = None
            if self._path.exists():
                backup = hidden(self._path.temporarySibling())
                self._path.moveTo(backup)

            fh = self._path.open("w")
            try:
                # FIXME: concurrency problem; if this write is interrupted
                # halfway through, the underlying file will be corrupt.
                fh.write(componentText)
            finally:
                fh.close()

            md5 = hashlib.md5(componentText).hexdigest()
            self.properties()[md5key] = TwistedGETContentMD5.fromString(md5)

            # Now re-write the original properties on the updated file
            self.properties().flush()

            def undo():
                if backup:
                    backup.moveTo(self._path)
                else:
                    self._path.remove()
            return undo
        self._transaction.addOperation(do, "set calendar component %r" % (self.name(),))

        self._calendar.notifyChanged()

    def component(self):
        """
        Read calendar data and validate/fix it. Do not raise a store error here if there are unfixable
        errors as that could prevent the overall request to fail. Instead we will hand bad data off to
        the caller - that is not ideal but in theory we should have checked everything on the way in and
        only allowed in good data.
        """
        text = self._text()
        try:
            component = VComponent.fromString(text)
        except InvalidICalendarDataError as e:
            # This is a really bad situation, so do raise
            raise InternalDataStoreError(
                "File corruption detected (%s) in file: %s"
                % (e, self._path.path)
            )

        # Fix any bogus data we can
        fixed, unfixed = component.validCalendarData(doFix=True, doRaise=False)

        if unfixed:
            self.log.error("Calendar data at {path} had unfixable problems:\n  {problems}", path=self._path.path, problems="\n  ".join(unfixed))

        if fixed:
            self.log.error("Calendar data at {path} had fixable problems:\n  {problems}", path=self._path.path, problems="\n  ".join(fixed))

        return component

    def componentForUser(self, user_uuid=None):
        """
        Return the iCalendar component filtered for the specified user's per-user data.

        @param user_uuid: the user UUID to filter on
        @type user_uuid: C{str}

        @return: the filtered calendar component
        @rtype: L{twistedcaldav.ical.Component}
        """

        if user_uuid is None:
            user_uuid = self._parentCollection.viewerHome().uid()
        return PerUserDataFilter(user_uuid).filter(self.component().duplicate())

    def _text(self):
        if self._objectText is not None:
            return self._objectText

        try:
            fh = self._path.open()
        except IOError as e:
            if e[0] == ENOENT:
                raise ConcurrentModification()
            else:
                raise

        try:
            text = fh.read()
        finally:
            fh.close()

        if not (
            text.startswith("BEGIN:VCALENDAR\r\n") or
            text.endswith("\r\nEND:VCALENDAR\r\n")
        ):
            # Handle case of old wiki data written using \n instead of \r\n
            if (
                text.startswith("BEGIN:VCALENDAR\n") and
                text.endswith("\nEND:VCALENDAR\n")
            ):
                text = text.replace("\n", "\r\n")
            else:
                # Cannot deal with this data
                raise InternalDataStoreError(
                    "File corruption detected (improper start) in file: %s"
                    % (self._path.path,)
                )

        self._objectText = text
        return text

    def uid(self):
        if not hasattr(self, "_uid"):
            self._uid = self.component().resourceUID()
        return self._uid

    def componentType(self):
        if not hasattr(self, "_componentType"):
            self._componentType = self.component().mainType()
        return self._componentType

    def organizer(self):
        return self.component().getOrganizer()

    def getMetadata(self):
        metadata = {}
        metadata["accessMode"] = self.accessMode
        metadata["isScheduleObject"] = self.isScheduleObject
        metadata["scheduleTag"] = self.scheduleTag
        metadata["scheduleEtags"] = self.scheduleEtags
        metadata["hasPrivateComment"] = self.hasPrivateComment
        return metadata

    def _get_accessMode(self):
        return str(self.properties().get(PropertyName.fromElement(customxml.TwistedCalendarAccessProperty), ""))

    def _set_accessMode(self, value):
        pname = PropertyName.fromElement(customxml.TwistedCalendarAccessProperty)
        if value:
            self.properties()[pname] = customxml.TwistedCalendarAccessProperty(value)
        elif pname in self.properties():
            del self.properties()[pname]

    accessMode = property(_get_accessMode, _set_accessMode)

    def _get_isScheduleObject(self):
        """
        If the property is not present, then return None, else return a bool based on the
        str "true" or "false" value.
        """
        prop = self.properties().get(PropertyName.fromElement(customxml.TwistedSchedulingObjectResource))
        if prop is not None:
            prop = str(prop) == "true"
        return prop

    def _set_isScheduleObject(self, value):
        pname = PropertyName.fromElement(customxml.TwistedSchedulingObjectResource)
        if value is not None:
            self.properties()[pname] = customxml.TwistedSchedulingObjectResource.fromString("true" if value else "false")
        elif pname in self.properties():
            del self.properties()[pname]

    isScheduleObject = property(_get_isScheduleObject, _set_isScheduleObject)

    def _get_scheduleTag(self):
        return str(self.properties().get(PropertyName.fromElement(caldavxml.ScheduleTag), ""))

    def _set_scheduleTag(self, value):
        pname = PropertyName.fromElement(caldavxml.ScheduleTag)
        if value:
            self.properties()[pname] = caldavxml.ScheduleTag.fromString(value)
        elif pname in self.properties():
            del self.properties()[pname]

    scheduleTag = property(_get_scheduleTag, _set_scheduleTag)

    def _get_scheduleEtags(self):
        return tuple([str(etag) for etag in self.properties().get(PropertyName.fromElement(customxml.TwistedScheduleMatchETags), customxml.TwistedScheduleMatchETags()).children])

    def _set_scheduleEtags(self, value):
        if value:
            etags = [davxml.GETETag.fromString(etag) for etag in value]
            self.properties()[PropertyName.fromElement(customxml.TwistedScheduleMatchETags)] = customxml.TwistedScheduleMatchETags(*etags)
        else:
            try:
                del self.properties()[PropertyName.fromElement(customxml.TwistedScheduleMatchETags)]
            except KeyError:
                pass

    scheduleEtags = property(_get_scheduleEtags, _set_scheduleEtags)

    def _get_hasPrivateComment(self):
        return PropertyName.fromElement(customxml.TwistedCalendarHasPrivateCommentsProperty) in self.properties()

    def _set_hasPrivateComment(self, value):
        pname = PropertyName.fromElement(customxml.TwistedCalendarHasPrivateCommentsProperty)
        if value:
            self.properties()[pname] = customxml.TwistedCalendarHasPrivateCommentsProperty()
        elif pname in self.properties():
            del self.properties()[pname]

    hasPrivateComment = property(_get_hasPrivateComment, _set_hasPrivateComment)

    def addAttachment(self, pathpattern, rids, content_type, filename, stream):
        raise NotImplementedError

    def updateAttachment(self, pathpattern, managed_id, content_type, filename, stream):
        raise NotImplementedError

    def removeAttachment(self, rids, managed_id):
        raise NotImplementedError

    @inlineCallbacks
    def createAttachmentWithName(self, name):
        """
        Implement L{ICalendarObject.removeAttachmentWithName}.
        """
        # Make a (FIXME: temp, remember rollbacks) file in dropbox-land
        dropboxPath = yield self._dropboxPath()
        attachment = Attachment(self, name, dropboxPath)
        self._attachments[name] = attachment
        returnValue(attachment)

    @inlineCallbacks
    def removeAttachmentWithName(self, name):
        """
        Implement L{ICalendarObject.removeAttachmentWithName}.
        """
        # FIXME: rollback, tests for rollback

        attachment = (yield self.attachmentWithName(name))
        oldSize = attachment.size()

        (yield self._dropboxPath()).child(name).remove()
        if name in self._attachments:
            del self._attachments[name]

        # Adjust quota
        self._calendar._home.adjustQuotaUsedBytes(-oldSize)

    @inlineCallbacks
    def attachmentWithName(self, name):
        # Attachments can be local or remote, but right now we only care about
        # local.  So we're going to base this on the listing of files in the
        # dropbox and not on the calendar data.  However, we COULD examine the
        # 'attach' properties.

        if name in self._attachments:
            returnValue(self._attachments[name])
        # FIXME: cache consistently (put it in self._attachments)
        dbp = yield self._dropboxPath()
        if dbp.child(name).exists():
            returnValue(Attachment(self, name, dbp))
        else:
            # FIXME: test for non-existent attachment.
            returnValue(None)

    def attendeesCanManageAttachments(self):
        return self.component().hasPropertyInAnyComponent("X-APPLE-DROPBOX")

    def dropboxID(self):
        # NB: Deferred
        return dropboxIDFromCalendarObject(self)

    @inlineCallbacks
    def _dropboxPath(self):
        dropboxPath = self._parentCollection._home._path.child(
            "dropbox"
        ).child((yield self.dropboxID()))
        if not dropboxPath.isdir():
            dropboxPath.makedirs()
        returnValue(dropboxPath)

    @inlineCallbacks
    def attachments(self):
        # See comment on attachmentWithName.
        dropboxPath = (yield self._dropboxPath())
        returnValue(
            [Attachment(self, name, dropboxPath)
             for name in dropboxPath.listdir()
             if not name.startswith(".")]
        )

    def initPropertyStore(self, props):
        # Setup peruser special properties
        props.setSpecialProperties(
            (
            ),
            (
                PropertyName.fromElement(customxml.TwistedCalendarAccessProperty),
                PropertyName.fromElement(customxml.TwistedSchedulingObjectResource),
                PropertyName.fromElement(caldavxml.ScheduleTag),
                PropertyName.fromElement(customxml.TwistedScheduleMatchETags),
                PropertyName.fromElement(customxml.TwistedCalendarHasPrivateCommentsProperty),
                PropertyName.fromElement(customxml.ScheduleChanges),
            ),
            (),
        )

    # IDataStoreObject
    def contentType(self):
        """
        The content type of Calendar objects is text/calendar.
        """
        return MimeType.fromString("text/calendar; charset=utf-8")


class AttachmentStorageTransport(StorageTransportBase):

    def __init__(self, attachment, contentType, dispositionName):
        """
        Initialize this L{AttachmentStorageTransport} and open its file for
        writing.

        @param attachment: The attachment whose data is being filled out.
        @type attachment: L{Attachment}
        """
        super(AttachmentStorageTransport, self).__init__(
            attachment, contentType, dispositionName)
        self._path = self._attachment._path.temporarySibling()
        self._file = self._path.open("w")

        self._txn.postAbort(self.aborted)

    @property
    def _txn(self):
        return self._attachment._txn

    def aborted(self):
        """
        Transaction aborted - clean up temp files.
        """
        if self._path.exists():
            self._path.remove()

    def write(self, data):
        # FIXME: multiple chunks
        self._file.write(data)
        return super(AttachmentStorageTransport, self).write(data)

    def loseConnection(self):
        home = self._attachment._calendarObject._calendar._home
        oldSize = self._attachment.size()
        newSize = self._file.tell()
        # FIXME: do anything
        self._file.close()

        # Check max size for attachment
        if newSize > config.MaximumAttachmentSize:
            self._path.remove()
            return fail(AttachmentSizeTooLarge())

        # Check overall user quota
        allowed = home.quotaAllowedBytes()
        if allowed is not None and allowed < (home.quotaUsedBytes() + (newSize - oldSize)):
            self._path.remove()
            return fail(QuotaExceeded())

        self._path.moveTo(self._attachment._path)

        md5 = hashlib.md5(self._attachment._path.getContent()).hexdigest()
        props = self._attachment.properties()
        props[contentTypeKey] = GETContentType(
            generateContentType(self._contentType)
        )
        props[md5key] = TwistedGETContentMD5.fromString(md5)

        # Adjust quota
        home.adjustQuotaUsedBytes(newSize - oldSize)
        props.flush()
        return succeed(None)


class Attachment(FileMetaDataMixin):
    """
    An L{Attachment} is a container for the data associated with a I{locally-
    stored} calendar attachment.  That is to say, there will only be
    L{Attachment} objects present on the I{organizer's} copy of and event, and
    only for C{ATTACH} properties where this server is storing the resource.
    (For example, the organizer may specify an C{ATTACH} property that
    references an URI on a remote server.)
    """

    implements(IAttachment)

    def __init__(self, calendarObject, name, dropboxPath):
        self._calendarObject = calendarObject
        self._name = name
        self._dropboxPath = dropboxPath

    @property
    def _txn(self):
        return self._calendarObject._txn

    def name(self):
        return self._name

    def properties(self):
        home = self._calendarObject._parentCollection._home
        uid = home.uid()
        propStoreClass = home._dataStore._propertyStoreClass
        return propStoreClass(uid, lambda: self._path)

    def store(self, contentType, dispositionName=None):
        return AttachmentStorageTransport(self, contentType, dispositionName)

    def retrieve(self, protocol):
        return AttachmentRetrievalTransport(self._path).start(protocol)

    @property
    def _path(self):
        return self._dropboxPath.child(self.name())


class CalendarStubResource(CommonStubResource):
    """
    Just enough resource to keep the calendar's sql DB classes going.
    """

    def isCalendarCollection(self):
        return True

    def getChild(self, name):
        calendarObject = self.resource.calendarObjectWithName(name)
        if calendarObject:
            class ChildResource(object):

                def __init__(self, calendarObject):
                    self.calendarObject = calendarObject

                def iCalendar(self):
                    return self.calendarObject.component()

            return ChildResource(calendarObject)
        else:
            return None


class Index(object):
    #
    # OK, here's where we get ugly.
    # The index code needs to be rewritten also, but in the meantime...
    #

    def __init__(self, calendar):
        self.calendar = calendar
        stubResource = CalendarStubResource(calendar)
        if self.calendar.name() == 'inbox':
            indexClass = OldInboxIndex
        else:
            indexClass = OldIndex
        self._oldIndex = indexClass(stubResource)

    def calendarObjects(self):
        calendar = self.calendar
        for name, uid, componentType in self._oldIndex.bruteForceSearch():
            calendarObject = calendar.calendarObjectWithName(name)

            # Precache what we found in the index
            calendarObject._uid = uid
            calendarObject._componentType = componentType

            yield calendarObject
