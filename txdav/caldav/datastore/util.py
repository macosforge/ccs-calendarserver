# -*- test-case-name: txdav.caldav.datastore.test.test_util -*-
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

"""
Utility logic common to multiple backend implementations.
"""

import os

from zope.interface.declarations import implements

from txdav.caldav.icalendarstore import IAttachmentStorageTransport, \
    ComponentUpdateState

from twisted.python.failure import Failure
from twisted.internet.defer import inlineCallbacks, Deferred, returnValue
from twisted.internet.protocol import Protocol

from twext.python.log import Logger

from txweb2 import http_headers

from twistedcaldav.ical import InvalidICalendarDataError
from twistedcaldav.ical import Component as VComponent

from twistedcaldav import ical
from twistedcaldav.datafilters.hiddeninstance import HiddenInstanceFilter
from twistedcaldav.datafilters.privateevents import PrivateEventFilter
from twistedcaldav.ical import PERUSER_UID

from txdav.common.icommondatastore import (
    InvalidObjectResourceError, NoSuchObjectResourceError,
    InternalDataStoreError, HomeChildNameAlreadyExistsError
)
from txdav.base.datastore.util import normalizeUUIDOrNot
from twisted.protocols.basic import FileSender
from twisted.internet.interfaces import ITransport
from twisted.internet.interfaces import IConsumer
from twisted.internet.error import ConnectionLost
from twisted.internet.task import LoopingCall

log = Logger()

validationBypass = False


def validateCalendarComponent(calendarObject, calendar, component, inserting, migrating):
    """
    Validate a calendar component for a particular calendar.

    @param calendarObject: The calendar object whose component will be
        replaced.
    @type calendarObject: L{ICalendarObject}

    @param calendar: The calendar which the L{ICalendarObject} is present in.
    @type calendar: L{ICalendar}

    @param component: The VComponent to be validated.
    @type component: L{VComponent}
    """

    if validationBypass:
        return

    if not isinstance(component, VComponent):
        raise TypeError(type(component))

    try:
        if not inserting and component.resourceUID() != calendarObject.uid():
            raise InvalidObjectResourceError(
                "UID may not change (%s != %s)" % (
                    component.resourceUID(), calendarObject.uid()
                )
            )
    except NoSuchObjectResourceError:
        pass

    try:
        # FIXME: This is a bad way to do this test (== 'inbox'), there should be a
        # Calendar-level API for it.
        component.validCalendarData(validateRecurrences=migrating)
        component.validCalendarForCalDAV(methodAllowed=calendar.name() == 'inbox')
        if migrating:
            component.validOrganizerForScheduling(doFix=True)
    except InvalidICalendarDataError as e:
        raise InvalidObjectResourceError(e)


@inlineCallbacks
def normalizationLookup(cuaddr, recordFunction, config):
    """
    Lookup function to be passed to ical.normalizeCalendarUserAddresses.
    Returns a tuple of (Full name C{unicode}, uid C{unicode}, cudtype C{str}, and
    calendar user address list C{unicode}) for the given cuaddr.
    recordFunction is called to retrieve the record for the cuaddr.
    """
    try:
        record = yield recordFunction(cuaddr)
    except Exception as e:
        log.debug("Lookup of {cuaddr} failed: {ex}", cuaddr=cuaddr, ex=e)
        record = None

    if record is None:
        returnValue((None, None, None, None))
    else:

        # RFC5545 syntax does not allow backslash escaping in
        # parameter values. A double-quote is thus not allowed
        # in a parameter value except as the start/end delimiters.
        # Single quotes are allowed, so we convert any double-quotes
        # to single-quotes.
        fullName = record.displayName.replace('"', "'").encode("utf-8")
        cuas = set(
            [cua.encode("utf-8") for cua in record.calendarUserAddresses]
        )
        returnValue((fullName, record.uid, record.getCUType(), cuas))


@inlineCallbacks
def dropboxIDFromCalendarObject(calendarObject):
    """
    Helper to implement L{ICalendarObject.dropboxID}.

    @param calendarObject: The calendar object to retrieve a dropbox ID for.
    @type calendarObject: L{ICalendarObject}
    """

    # Try "X-APPLE-DROPBOX" first
    dropboxProperty = (yield calendarObject.component()).getFirstPropertyInAnyComponent("X-APPLE-DROPBOX")
    if dropboxProperty is not None and dropboxProperty.value():
        componentDropboxID = dropboxProperty.value().rstrip("/").split("/")[-1]
        returnValue(componentDropboxID)

    # Now look at each ATTACH property and see if it might be a dropbox item
    # and if so extract the id from that

    attachments = (yield calendarObject.component()).getAllPropertiesInAnyComponent(
        "ATTACH",
        depth=1,
    )
    for attachment in attachments:

        # Make sure the value type is URI and http(s) and it is in a dropbox
        valueType = attachment.parameterValue("VALUE", "URI")
        if valueType == "URI" and attachment.value().startswith("http"):
            segments = attachment.value().split("/")
            try:
                if segments[-3] == "dropbox":
                    returnValue(segments[-2])
            except IndexError:
                pass

    # Return a "safe" version of the UID
    uid = calendarObject.uid()
    if uid.startswith("http://"):
        uid = uid.replace("http://", "")
    if uid.startswith("https://"):
        uid = uid.replace("https://", "")
    uid = uid.replace("/", "-")
    uid = uid.replace(":", "")
    uid = uid.replace(".", "")
    returnValue(uid + ".dropbox")


@inlineCallbacks
def _migrateCalendar(inCalendar, outCalendar, getComponent, merge=False):
    """
    Copy all calendar objects and properties in the given input calendar to the
    given output calendar.

    @param inCalendar: the L{ICalendar} to retrieve calendar objects from.

    @param outCalendar: the L{ICalendar} to store calendar objects to.

    @param getComponent: a 1-argument callable; see L{migrateHome}.

    @param merge: a boolean indicating whether we should attempt to merge the
        calendars together.

    @return: a tuple of (ok count, bad count)
    """

    ok_count = 0
    bad_count = 0
    outCalendar.properties().update(inCalendar.properties())
    outHome = outCalendar.ownerCalendarHome()
    for calendarObject in (yield inCalendar.calendarObjects()):
        try:
            ctype = yield calendarObject.componentType()
        except Exception as e:  # Don't stop for any error
            log.error(
                "  Failed to migrate calendar object: {home}/{cal}/{rsrc} (ex)",
                home=inCalendar.ownerHome().name(),
                cal=inCalendar.name(),
                rsrc=calendarObject.name(),
                ex=str(e),
            )
            bad_count += 1
            continue

        if ctype not in ("VEVENT", "VTODO"):
            log.error(
                "Migration skipping unsupported ({ctype}) calendar object {obj!r}",
                ctype=ctype, obj=calendarObject,
            )
            continue
        if merge:
            mightConflict = yield outHome.hasCalendarResourceUIDSomewhereElse(
                calendarObject.uid(), None, "schedule"
            )
            if mightConflict is not None:
                log.warn(
                    "Not migrating object {home}/{cal}/{rsrc} due to potential conflict",
                    home=outHome.uid(), cal=outCalendar.name(), rsrc=calendarObject.name(),
                )
                continue
        try:
            # Must account for metadata
            component = yield getComponent(calendarObject)
            component.md5 = calendarObject.md5()
            yield outCalendar._createCalendarObjectWithNameInternal(
                calendarObject.name(),
                component,
                internal_state=ComponentUpdateState.RAW,
                options=calendarObject.getMetadata(),
            )

            # Only the owner's properties are migrated, since previous releases of
            # calendar server didn't have per-user properties.
            outObject = yield outCalendar.calendarObjectWithName(
                calendarObject.name())
            if outCalendar.objectResourcesHaveProperties():
                outObject.properties().update(calendarObject.properties())

            if inCalendar.name() == "inbox":
                # Because of 9023803, skip attachment processing within inboxes
                continue

            # Migrate attachments.
            for attachment in (yield calendarObject.attachments()):
                name = attachment.name()
                ctype = attachment.contentType()
                exists = yield outObject.attachmentWithName(name)
                if exists is None:
                    newattachment = yield outObject.createAttachmentWithName(name)
                    transport = newattachment.store(ctype)
                    proto = _AttachmentMigrationProto(transport)
                    attachment.retrieve(proto)
                    yield proto.done

            ok_count += 1

        except InternalDataStoreError:
            log.error(
                "  InternalDataStoreError: Failed to migrate calendar object: {home}/{cal}/{rsrc}",
                home=inCalendar.ownerHome().name(),
                cal=inCalendar.name(),
                rsrc=calendarObject.name(),
            )
            bad_count += 1

        except Exception as e:
            log.error(
                "  {ex}: Failed to migrate calendar object: {home}/{cal}/{rsrc}",
                ex=str(e),
                home=inCalendar.ownerHome().name(),
                cal=inCalendar.name(),
                rsrc=calendarObject.name(),
            )
            bad_count += 1

    returnValue((ok_count, bad_count,))


# MIME helpers - mostly copied from txweb2.static

def loadMimeTypes(mimetype_locations=['/etc/mime.types']):
    """
    Multiple file locations containing mime-types can be passed as a list.
    The files will be sourced in that order, overriding mime-types from the
    files sourced beforehand, but only if a new entry explicitly overrides
    the current entry.
    """
    import mimetypes
    # Grab Python's built-in mimetypes dictionary.
    contentTypes = mimetypes.types_map  # @UndefinedVariable
    # Update Python's semi-erroneous dictionary with a few of the
    # usual suspects.
    contentTypes.update(
        {
            '.conf': 'text/plain',
            '.diff': 'text/plain',
            '.exe': 'application/x-executable',
            '.flac': 'audio/x-flac',
            '.java': 'text/plain',
            '.ogg': 'application/ogg',
            '.oz': 'text/x-oz',
            '.swf': 'application/x-shockwave-flash',
            '.tgz': 'application/x-gtar',
            '.wml': 'text/vnd.wap.wml',
            '.xul': 'application/vnd.mozilla.xul+xml',
            '.py': 'text/plain',
            '.patch': 'text/plain',
        }
    )
    # Users can override these mime-types by loading them out configuration
    # files (this defaults to ['/etc/mime.types']).
    for location in mimetype_locations:
        if os.path.exists(location):
            contentTypes.update(mimetypes.read_mime_types(location))

    return contentTypes


def getType(filename, types, defaultType="application/octet-stream"):
    _ignore_p, ext = os.path.splitext(filename)
    ext = ext.lower()
    return types.get(ext, defaultType)


class _AttachmentMigrationProto(Protocol, object):

    def __init__(self, storeTransport):
        self.storeTransport = storeTransport
        self.done = Deferred()

    def connectionMade(self):
        self.storeTransport.registerProducer(self.transport, False)

    def dataReceived(self, data):
        self.storeTransport.write(data)

    @inlineCallbacks
    def connectionLost(self, reason):
        try:
            yield self.storeTransport.loseConnection()
        except:
            self.done.errback()
        else:
            self.done.callback(None)


@inlineCallbacks
def migrateHome(inHome, outHome, getComponent=lambda x: x.component(), merge=False):
    """
    Copy all calendars and properties in the given input calendar home to the
    given output calendar home.

    @param inHome: the L{ICalendarHome} to retrieve calendars and properties
        from.

    @param outHome: the L{ICalendarHome} to store calendars and properties
        into.

    @param getComponent: a 1-argument callable that takes an L{ICalendarObject}
        (from a calendar in C{inHome}) and returns a L{VComponent} (to store in
        a calendar in outHome).

    @param merge: a boolean indicating whether to raise an exception when
        encountering a conflicting element of data (calendar or event), or to
        attempt to merge them together.

    @return: a L{Deferred} that fires with C{None} when the migration is
        complete.
    """
    from twistedcaldav.config import config
    if not merge:
        if config.RestrictCalendarsToOneComponentType:
            for name in ical.allowedStoreComponents:
                yield outHome.removeCalendarWithName(outHome._componentCalendarName[name])
        else:
            yield outHome.removeCalendarWithName("calendar")
        yield outHome.removeCalendarWithName("inbox")

    outHome.properties().update(inHome.properties())
    inCalendars = yield inHome.calendars()
    for calendar in inCalendars:
        name = calendar.name()
        if name == "outbox":
            continue
        d = outHome.createCalendarWithName(name)
        if merge:
            d.addErrback(lambda f: f.trap(HomeChildNameAlreadyExistsError))
        yield d
        outCalendar = yield outHome.calendarWithName(name)
        try:
            yield _migrateCalendar(calendar, outCalendar, getComponent, merge=merge)
        except InternalDataStoreError:
            log.error(
                "  Failed to migrate calendar: {home}/{cal}", home=inHome.name(), cal=name,
            )

    # No migration for notifications, since they weren't present in earlier
    # released versions of CalendarServer.

    # May need to create inbox if it was not present in the original file store for some reason
    inboxCalendar = yield outHome.calendarWithName("inbox")
    if inboxCalendar is None:
        yield outHome.createCalendarWithName("inbox")

    # May need to split calendars by component type
    if config.RestrictCalendarsToOneComponentType:
        yield outHome.splitCalendars()


class CalendarObjectBase(object):
    """
    Base logic shared between file- and sql-based L{ICalendarObject}
    implementations.
    """

    @inlineCallbacks
    def filteredComponent(self, accessUID, asAdmin=False):
        """
        Filter this calendar object's iCalendar component as it would be
        perceived by a particular user, accounting for per-user iCalendar data
        and private events, and return a L{Deferred} that fires with that
        object.

        Unlike the result of C{component()}, which contains storage-specific
        iCalendar properties, this is a valid iCalendar object which could be
        serialized and displayed to other iCalendar-processing software.

        @param accessUID: the UID of the principal who is accessing this
            component.
        @type accessUID: C{str} (UTF-8 encoded)

        @param asAdmin: should the given UID be treated as an administrator?  If
            this is C{True}, the resulting component will have an unobscured
            view of private events, even if the given UID is not actually the
            owner of said events.  (However, per-instance overridden values will
            still be seen as the given C{accessUID}.)

        @return: a L{Deferred} which fires with a
            L{twistedcaldav.ical.Component}.
        """
        component = yield self.componentForUser(accessUID)
        calendar = self.calendar()
        isOwner = asAdmin or (calendar.owned() and
                              calendar.ownerCalendarHome().uid() == accessUID)
        for data_filter in [
            HiddenInstanceFilter(),
            PrivateEventFilter(self.accessMode, isOwner),
        ]:
            component = data_filter.filter(component)
        returnValue(component)


class StorageTransportAddress(object):
    """
    Peer / host address for L{IAttachmentStorageTransport} implementations.

    @ivar attachment: the L{IAttachment} being stored.

    @type attachment: L{IAttachment} provider

    @ivar isHost: is this a host address or peer address?

    @type isHost: C{bool}
    """

    def __init__(self, attachment, isHost):
        """
        Initialize with the attachment being stored.
        """
        self.attachment = attachment
        self.isHost = isHost

    def __repr__(self):
        if self.isHost:
            host = " (host)"
        else:
            host = ""
        return '<Storing Attachment: %r%s>' % (self.attachment.name(), host)


class StorageTransportBase(object):
    """
    Base logic shared between file- and sql-based L{IAttachmentStorageTransport}
    implementations.
    """

    implements(IAttachmentStorageTransport)

    contentTypes = loadMimeTypes()

    def __init__(self, attachment, contentType, dispositionName):
        """
        Create a storage transport with a reference to an L{IAttachment} and a
        L{txweb2.http_headers.MimeType}.
        """
        from twisted.internet import reactor
        self._clock = reactor
        self._attachment = attachment
        self._contentType = contentType
        self._dispositionName = dispositionName
        self._producer = None

        # Make sure we have some kind of content-type
        if self._contentType is None:
            self._contentType = http_headers.MimeType.fromString(getType(self._attachment.name(), self.contentTypes))

    def resetDetails(self, contentType, dispositionName):
        self._contentType = contentType
        self._dispositionName = dispositionName

    def write(self, data):
        """
        Children must override this to actually write the data, but should
        upcall this implementation to interact properly with producers.
        """
        if self._producer and self._streamingProducer:
            # XXX this needs to be in a callLater because otherwise
            # resumeProducing will call write which will call resumeProducing
            # (etc) forever.
            self._clock.callLater(0, self._producer.resumeProducing)

    def registerProducer(self, producer, streaming):
        self._producer = producer
        self._streamingProducer = streaming

    def getPeer(self):
        return StorageTransportAddress(self._attachment, False)

    def getHost(self):
        return StorageTransportAddress(self._attachment, True)

    def writeSequence(self, seq):
        return self.write(''.join(seq))

    def stopProducing(self):
        return self.loseConnection()


class AttachmentRetrievalTransport(FileSender, object):
    """
    The transport for a protocol that does L{IAttachment.retrieve}.
    """
    implements(ITransport)

    def __init__(self, filePath):
        from twisted.internet import reactor
        self.filePath = filePath
        self.clock = reactor

    def start(self, protocol):
        this = self

        class Consumer(object):
            implements(IConsumer)

            def registerProducer(self, producer, streaming):
                protocol.makeConnection(producer)
                this._maybeLoopDelivery()

            def write(self, data):
                protocol.dataReceived(data)

            def unregisterProducer(self):
                this._done(protocol)
        self.beginFileTransfer(self.filePath.open(), Consumer())

    def _done(self, protocol):
        if self._deliveryLoop:
            self._deliveryLoop.stop()
        protocol.connectionLost(Failure(ConnectionLost()))

    def write(self, data):
        raise NotImplemented("This is a read-only transport.")

    def writeSequence(self, datas):
        self.write("".join(datas))

    def loseConnection(self):
        pass

    def getPeer(self):
        return self

    def getHost(self):
        return self

    _everResumedProducing = False

    def resumeProducing(self):
        self._everResumedProducing = True
        super(AttachmentRetrievalTransport, self).resumeProducing()

    _deliveryLoop = None

    def _maybeLoopDelivery(self):
        """
        If no consumer was registered (as inferred by the fact that
        resumeProducing() wasn't called)
        """
        if not self._everResumedProducing:
            # Not registered as a streaming producer.
            def deliverNextChunk():
                super(AttachmentRetrievalTransport, self).resumeProducing()
            self._deliveryLoop = LoopingCall(deliverNextChunk)
            self._deliveryLoop.start(0.01, True)


def fixOneCalendarObject(component):
    """
    Correct the properties which may contain a user's directory UUID within a
    single calendar component, by normalizing the directory UUID.

    @param component: any iCalendar component.
    @type component: L{twistedcaldav.ical.Component}

    @return: a 2-tuple of the number of fixes performed and the new
        L{Component}
    """
    fixes = 0
    for calprop in component.properties():
        if calprop.name() in (
            "ATTENDEE", "ORGANIZER", PERUSER_UID
        ):
            preval = calprop.value()
            postval = normalizeUUIDOrNot(preval)
            if preval != postval:
                fixes += 1
                calprop.setValue(postval)
    for subc in component.subcomponents():
        count, _ignore_fixsubc = fixOneCalendarObject(subc)
        fixes += count
    return fixes, component


@inlineCallbacks
def fixOneCalendarHome(home):
    """
    Correct the case of UIDs on one L{ICalendarHome}.

    @return: a L{Deferred} that fires with the number of fixes made when the
        fixes are complete.
    """
    fixedThisHome = 0
    for calendar in (yield home.calendars()):
        for calObj in (yield calendar.calendarObjects()):
            try:
                comp = (yield calObj.component())
                fixCount, comp = fixOneCalendarObject(comp)
                fixedThisHome += fixCount
                if fixCount:
                    yield calObj._setComponentInternal(comp, internal_state=ComponentUpdateState.RAW)
            except:
                log.failure(
                    "Error while processing calendar/object {calendarName} {calendarObject}",
                    calendarName=calendar.name(),
                    calendarObject=calObj.name(),
                )
    returnValue(fixedThisHome)
