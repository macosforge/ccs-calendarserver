# -*- test-case-name: twistedcaldav.test.test_wrapping -*-
##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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

import hashlib
import time
from urlparse import urlsplit, urljoin
import uuid

from pycalendar.datetime import DateTime
from twext.enterprise.locking import LockTimeout
from twext.python.log import Logger
from twisted.internet.defer import succeed, inlineCallbacks, returnValue, maybeDeferred
from twisted.internet.protocol import Protocol
from twisted.python.util import FancyEqMixin
from twistedcaldav import customxml, carddavxml, caldavxml, ical
from twistedcaldav.caldavxml import (
    caldav_namespace, MaxAttendeesPerInstance, MaxInstances, NoUIDConflict
)
from twistedcaldav.carddavxml import carddav_namespace, NoUIDConflict as NovCardUIDConflict
from twistedcaldav.config import config
from twistedcaldav.customxml import calendarserver_namespace
from twistedcaldav.ical import (
    Component as VCalendar, Property as VProperty,
    iCalendarProductID, Component
)
from twistedcaldav.instance import (
    InvalidOverriddenInstanceError, TooManyInstancesError
)
from twistedcaldav.memcachelock import MemcacheLockTimeoutError
from twistedcaldav.notifications import NotificationCollectionResource, NotificationResource
from twistedcaldav.resource import CalDAVResource, DefaultAlarmPropertyMixin, \
    requiresPermissions
from twistedcaldav.scheduling_store.caldav.resource import ScheduleInboxResource
from twistedcaldav.sharing import (
    invitationBindStatusToXMLMap, invitationBindModeToXMLMap
)
from twistedcaldav.util import bestAcceptType, matchClientFixes
from twistedcaldav.vcard import Component as VCard, InvalidVCardDataError
from txdav.base.propertystore.base import PropertyName
from txdav.caldav.icalendarstore import (
    QuotaExceeded, AttachmentStoreFailed,
    AttachmentStoreValidManagedID, AttachmentRemoveFailed,
    AttachmentDropboxNotAllowed, InvalidComponentTypeError,
    TooManyAttendeesError, InvalidCalendarAccessError, ValidOrganizerError,
    InvalidPerUserDataMerge,
    AttendeeAllowedError, ResourceDeletedError, InvalidAttachmentOperation,
    ShareeAllowedError, DuplicatePrivateCommentsError, InvalidSplit,
    AttachmentSizeTooLarge, UnknownTimezone, SetComponentOptions, TooManyAttachments)
from txdav.carddav.iaddressbookstore import (
    KindChangeNotAllowedError, GroupWithUnsharedAddressNotAllowedError
)
from txdav.common.datastore.podding.base import FailedCrossPodRequestError
from txdav.common.datastore.sql_tables import (
    _BIND_MODE_READ, _BIND_MODE_WRITE,
    _BIND_MODE_DIRECT, _BIND_STATUS_ACCEPTED
)
from txdav.common.icommondatastore import (
    NoSuchObjectResourceError,
    TooManyObjectResourcesError, ObjectResourceTooBigError,
    InvalidObjectResourceError, ObjectResourceNameNotAllowedError,
    ObjectResourceNameAlreadyExistsError, UIDExistsError,
    UIDExistsElsewhereError, InvalidUIDError, InvalidResourceMove,
    InvalidComponentForStoreError, AlreadyInTrashError,
    HomeChildNameAlreadyExistsError, ConcurrentModification
)
from txdav.idav import PropertyChangeNotAllowedError
from txdav.who.wiki import RecordType as WikiRecordType
from txdav.xml import element as davxml, element
from txdav.xml.base import dav_namespace, WebDAVUnknownElement, encodeXMLName
from txweb2 import responsecode, http_headers, http
from txweb2.dav.http import ErrorResponse, ResponseQueue, MultiStatusResponse
from txweb2.dav.noneprops import NonePropertyStore
from txweb2.dav.resource import (
    TwistedACLInheritable, AccessDeniedError, davPrivilegeSet
)
from txweb2.dav.util import parentForURL, allDataFromStream, joinURL, davXMLFromStream
from txweb2.filter.location import addLocation
from txweb2.http import HTTPError, StatusResponse, Response
from txweb2.http_headers import ETag, MimeType, MimeDisposition
from txweb2.iweb import IResponse
from txweb2.responsecode import (
    FORBIDDEN, NO_CONTENT, NOT_FOUND, CREATED, CONFLICT, PRECONDITION_FAILED,
    BAD_REQUEST, OK, INSUFFICIENT_STORAGE_SPACE, SERVICE_UNAVAILABLE
)
from txweb2.stream import ProducerStream, readStream, MemoryStream
from twistedcaldav.timezones import TimezoneException


"""
Wrappers to translate between the APIs in L{txdav.caldav.icalendarstore} and
L{txdav.carddav.iaddressbookstore} and those in L{twistedcaldav}.
"""

log = Logger()


class _NewStorePropertiesWrapper(object):
    """
    Wrap a new-style property store (a L{txdav.idav.IPropertyStore}) in the old-
    style interface for compatibility with existing code.
    """

    # FIXME: UID arguments on everything need to be tested against something.
    def __init__(self, newPropertyStore):
        """
        Initialize an old-style property store from a new one.

        @param newPropertyStore: the new-style property store.
        @type newPropertyStore: L{txdav.idav.IPropertyStore}
        """
        self._newPropertyStore = newPropertyStore

    @classmethod
    def _convertKey(cls, qname):
        namespace, name = qname
        return PropertyName(namespace, name)

    def get(self, qname):
        try:
            return self._newPropertyStore[self._convertKey(qname)]
        except KeyError:
            raise HTTPError(StatusResponse(
                NOT_FOUND,
                "No such property: %s" % (encodeXMLName(*qname),)
            ))

    def set(self, prop):
        try:
            self._newPropertyStore[self._convertKey(prop.qname())] = prop
        except PropertyChangeNotAllowedError:
            raise HTTPError(StatusResponse(
                FORBIDDEN,
                "Property cannot be changed: %s" % (prop.sname(),)
            ))

    def delete(self, qname):
        try:
            del self._newPropertyStore[self._convertKey(qname)]
        except KeyError:
            # RFC 2518 Section 12.13.1 says that removal of
            # non-existing property is not an error.
            pass

    def contains(self, qname):
        return (self._convertKey(qname) in self._newPropertyStore)

    def list(self):
        return [(pname.namespace, pname.name) for pname in
                self._newPropertyStore.keys()]


class _NewStoreFileMetaDataHelper(object):

    def exists(self):
        return self._newStoreObject is not None

    def name(self):
        return self._newStoreObject.name() if self._newStoreObject is not None else self._name

    def etag(self):
        return succeed(ETag(self._newStoreObject.md5()) if self._newStoreObject is not None else None)

    def contentType(self):
        return self._newStoreObject.contentType() if self._newStoreObject is not None else None

    def contentLength(self):
        return self._newStoreObject.size() if self._newStoreObject is not None else None

    def lastModified(self):
        return self._newStoreObject.modified() if self._newStoreObject is not None else None

    def creationDate(self):
        return self._newStoreObject.created() if self._newStoreObject is not None else None

    def newStoreProperties(self):
        return self._newStoreObject.properties() if self._newStoreObject is not None else None


class _CommonStoreExceptionHandler(object):
    """
    A mix-in class that is used to help trap store exceptions and turn them into
    appropriate HTTP errors.

    The class properties define mappings from a store exception type to a L{tuple} whose
    first item is one of the class methods defined in this mix-in, and whose second argument
    is the L{arg} passed to the class method. In some cases the second L{tuple} item will not
    be present, and instead the argument will be directly provided to the class method.
    """

    # The following are used to map store exceptions into HTTP error responses
    StoreExceptionsErrors = {}
    StoreMoveExceptionsErrors = {}

    @classmethod
    def _storeExceptionStatus(cls, err, arg):
        """
        Raise a status error.

        @param err: the actual exception that caused the error
        @type err: L{Exception}
        @param arg: description of error or C{None}
        @type arg: C{str} or C{None}
        """
        raise HTTPError(StatusResponse(responsecode.FORBIDDEN, arg if arg is not None else str(err)))

    @classmethod
    def _storeExceptionError(cls, err, arg):
        """
        Raise a DAV:error error with the supplied error element.

        @param err: the actual exception that caused the error
        @type err: L{Exception}
        @param arg: the error element
        @type arg: C{tuple}
        """
        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            arg,
            str(err),
        ))

    @classmethod
    def _storeExceptionUnavailable(cls, err, arg):
        """
        Raise a service unavailable error.

        @param err: the actual exception that caused the error
        @type err: L{Exception}
        @param arg: description of error or C{None}
        @type arg: C{str} or C{None}
        """
        response = StatusResponse(responsecode.SERVICE_UNAVAILABLE, arg if arg is not None else str(err))
        response.headers.setHeader("Retry-After", time.time() + config.TransactionHTTPRetrySeconds)
        raise HTTPError(response)

    @classmethod
    def _handleStoreException(cls, ex, exceptionMap):
        """
        Process a store exception and see if it is in the supplied mapping. If so, execute the
        method in the mapping (which will raise an HTTPError).

        @param ex: the store exception that was raised
        @type ex: L{Exception}
        @param exceptionMap: the store exception mapping to use
        @type exceptionMap: L{dict}
        """
        if type(ex) in exceptionMap:
            error, arg = exceptionMap[type(ex)]
            error(ex, arg)

    @classmethod
    def _handleStoreExceptionArg(cls, ex, exceptionMap, arg):
        """
        Process a store exception and see if it is in the supplied mapping. If so, execute the
        method in the mapping (which will raise an HTTPError). This method is used when the argument
        to the class method needs to be provided at runtime, rather than statically.

        @param ex: the store exception that was raised
        @type ex: L{Exception}
        @param exceptionSet: the store exception set to use
        @type exceptionSet: L{set}
        @param arg: the argument to use
        @type arg: L{object}
        """
        if type(ex) in exceptionMap:
            error = exceptionMap[type(ex)]
            error(ex, arg)


class _CommonHomeChildCollectionMixin(_CommonStoreExceptionHandler):
    """
    Methods for things which are like calendars.
    """

    _childClass = None

    def _initializeWithHomeChild(self, child, home):
        """
        Initialize with a home child object.

        @param child: the new store home child object.
        @type calendar: L{txdav.common._.CommonHomeChild}

        @param home: the home through which the given home child was accessed.
        @type home: L{txdav.common._.CommonHome}
        """
        self._newStoreObject = child
        self._newStoreParentHome = home._newStoreHome
        self._parentResource = home
        self._dead_properties = _NewStorePropertiesWrapper(
            self._newStoreObject.properties()
        ) if self._newStoreObject else NonePropertyStore(self)

    def liveProperties(self):

        props = super(_CommonHomeChildCollectionMixin, self).liveProperties()

        if config.MaxResourcesPerCollection:
            props += (customxml.MaxResources.qname(),)

        if config.EnableBatchUpload:
            props += (customxml.BulkRequests.qname(),)

        return props

    @inlineCallbacks
    def readProperty(self, prop, request):
        if type(prop) is tuple:
            qname = prop
        else:
            qname = prop.qname()

        if qname == customxml.MaxResources.qname() and config.MaxResourcesPerCollection:
            returnValue(customxml.MaxResources.fromString(config.MaxResourcesPerCollection))

        elif qname == customxml.BulkRequests.qname() and config.EnableBatchUpload:
            returnValue(customxml.BulkRequests(
                customxml.Simple(
                    customxml.MaxBulkResources.fromString(str(config.MaxResourcesBatchUpload)),
                    customxml.MaxBulkBytes.fromString(str(config.MaxBytesBatchUpload)),
                ),
                customxml.CRUD(
                    customxml.MaxBulkResources.fromString(str(config.MaxResourcesBatchUpload)),
                    customxml.MaxBulkBytes.fromString(str(config.MaxBytesBatchUpload)),
                ),
            ))

        result = (yield super(_CommonHomeChildCollectionMixin, self).readProperty(prop, request))
        returnValue(result)

    def url(self):
        return joinURL(self._parentResource.url(), self._name, "/")

    def owner_url(self):
        if self.isShareeResource():
            return joinURL(self._share_url, "/") if self._share_url else ""
        else:
            return self.url()

    def parentResource(self):
        return self._parentResource

    def exists(self):
        # FIXME: tests
        return self._newStoreObject is not None

    @inlineCallbacks
    def _indexWhatChanged(self, revision, depth):
        # The newstore implementation supports this directly
        returnValue(
            (yield self._newStoreObject.resourceNamesSinceToken(revision))
        )

    @inlineCallbacks
    def makeChild(self, name):
        """
        Create a L{CalendarObjectResource} based on a calendar object name.
        """

        if self._newStoreObject:
            try:
                newStoreObject = yield self._newStoreObject.objectResourceWithName(name)
            except Exception as err:
                self._handleStoreException(err, self.StoreExceptionsErrors)
                raise

            similar = self._childClass(
                newStoreObject,
                self._newStoreObject,
                self,
                name,
                principalCollections=self._principalCollections
            )

            self.propagateTransaction(similar)
            returnValue(similar)
        else:
            returnValue(NoParent())

    @inlineCallbacks
    def listChildren(self):
        """
        @return: a sequence of the names of all known children of this resource.
        """
        children = set(self.putChildren.keys())
        children.update((yield self._newStoreObject.listObjectResources()))
        returnValue(sorted(children))

    def countChildren(self):
        """
        @return: L{Deferred} with the count of all known children of this resource.
        """
        return self._newStoreObject.countObjectResources()

    @inlineCallbacks
    def resourceExists(self, name):
        """
        Indicate whether a resource with the specified name exists.

        @return: C{True} if it exists
        @rtype: C{bool}
        """
        allNames = yield self._newStoreObject.listObjectResources()
        returnValue(name in allNames)

    def name(self):
        return self._name

    @inlineCallbacks
    def etag(self):
        """
        Use the sync token as the etag
        """
        if self._newStoreObject:
            token = (yield self.getInternalSyncToken())
            returnValue(ETag(hashlib.md5(token).hexdigest()))
        else:
            returnValue(None)

    def lastModified(self):
        return self._newStoreObject.modified() if self._newStoreObject else None

    def creationDate(self):
        return self._newStoreObject.created() if self._newStoreObject else None

    def getInternalSyncToken(self):
        return self._newStoreObject.syncToken() if self._newStoreObject else None

    def resourceID(self):
        rid = "%s/%s" % (self._newStoreParentHome.id(), self._newStoreObject.id(),)
        return uuid.uuid5(self.uuid_namespace, rid).urn

    @inlineCallbacks
    def findChildrenFaster(
        self, depth, request, okcallback, badcallback, missingcallback, unavailablecallback,
        names, privileges, inherited_aces
    ):
        """
        Override to pre-load children in certain collection types for better performance.
        """

        if depth == "1":
            if names:
                yield self._newStoreObject.objectResourcesWithNames(names)
            else:
                yield self._newStoreObject.objectResources()

        result = (yield super(_CommonHomeChildCollectionMixin, self).findChildrenFaster(
            depth, request, okcallback, badcallback, missingcallback, unavailablecallback, names, privileges, inherited_aces
        ))

        returnValue(result)

    @inlineCallbacks
    def createCollection(self):
        """
        Override C{createCollection} to actually do the work.
        """
        try:
            self._newStoreObject = (yield self._newStoreParentHome.createChildWithName(self._name))
        except HomeChildNameAlreadyExistsError:
            # We already check for an existing child prior to this call so the only time this fails is if
            # there is an unaccepted share with the same name
            raise HTTPError(StatusResponse(responsecode.FORBIDDEN, "Unaccepted share exists"))

        # Re-initialize to get stuff setup again now we have a "real" object
        self._initializeWithHomeChild(self._newStoreObject, self._parentResource)

        returnValue(CREATED)

    def http_PUT(self, request):
        """
        Cannot PUT to existing collection. Use POST instead.
        """
        return FORBIDDEN

    @requiresPermissions(fromParent=[davxml.Unbind()])
    @inlineCallbacks
    def http_DELETE(self, request):
        """
        Override http_DELETE to validate 'depth' header.
        """

        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        depth = request.headers.getHeader("depth", "infinity")
        if depth != "infinity":
            msg = "illegal depth header for DELETE on collection: %s" % (
                depth,
            )
            log.error(msg)
            raise HTTPError(StatusResponse(BAD_REQUEST, msg))
        try:
            response = (yield self.storeRemove(request))
        except Exception as err:
            self._handleStoreException(err, self.StoreExceptionsErrors)
            raise
        returnValue(response)

    @inlineCallbacks
    def storeRemove(self, request):
        """
        Delete this collection resource, first deleting each contained
        object resource.

        This has to emulate the behavior in fileop.delete in that any errors
        need to be reported back in a multistatus response.

        @param request: The request used to locate child resources.  Note that
            this is the request which I{triggered} the C{DELETE}, but which may
            not actually be a C{DELETE} request itself.

        @type request: L{txweb2.iweb.IRequest}

        @return: an HTTP response suitable for sending to a client (or
            including in a multi-status).

        @rtype: something adaptable to L{txweb2.iweb.IResponse}
        """

        # Check sharee collection first
        if self.isShareeResource():
            log.debug("Removing shared collection {s!r}", s=self)
            yield self.removeShareeResource(request)
            # Re-initialize to get stuff setup again now we have no object
            self._initializeWithHomeChild(None, self._parentResource)
            returnValue(NO_CONTENT)

        log.debug("Deleting collection {s!r}", s=self)

        # 'deluri' is this resource's URI; I should be able to synthesize it
        # from 'self'.

        errors = ResponseQueue(request.uri, "DELETE", NO_CONTENT)

        for childname in (yield self.listChildren()):

            childurl = joinURL(request.uri, childname)

            # FIXME: use a more specific API; we should know what this child
            # resource is, and not have to look it up.  (Sharing information
            # needs to move into the back-end first, though.)
            child = (yield request.locateChildResource(self, childname))

            try:
                yield child.storeRemove(request)
            except:
                log.failure("storeRemove({request})", request=request)
                errors.add(childurl, BAD_REQUEST)

        # Now do normal delete

        # Actually delete it.
        yield self._newStoreObject.remove()

        # Re-initialize to get stuff setup again now we have no object
        self._initializeWithHomeChild(None, self._parentResource)

        # FIXME: handle exceptions, possibly like this:

        #        if isinstance(more_responses, MultiStatusResponse):
        #            # Merge errors
        #            errors.responses.update(more_responses.children)

        response = errors.response()

        returnValue(response)

    def http_COPY(self, request):
        """
        Copying of calendar collections isn't allowed.
        """
        # FIXME: no direct tests
        return FORBIDDEN

    # FIXME: access control
    @inlineCallbacks
    def http_MOVE(self, request):
        """
        Moving a collection is allowed for the purposes of changing
        that collections's name.
        """
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        # Can not move outside of home or to existing collection
        sourceURI = request.uri
        destinationURI = urlsplit(request.headers.getHeader("destination"))[2]
        if parentForURL(sourceURI) != parentForURL(destinationURI):
            returnValue(FORBIDDEN)

        destination = yield request.locateResource(destinationURI)
        if destination.exists():
            returnValue(FORBIDDEN)

        # Forget the destination now as after the move we will need to re-init it with its
        # new store object
        request._forgetResource(destination, destinationURI)

        # Move is valid so do it
        basename = destinationURI.rstrip("/").split("/")[-1]
        yield self._newStoreObject.rename(basename)
        returnValue(NO_CONTENT)

    @inlineCallbacks
    def POST_handler_add_member(self, request):
        """
        Handle a POST ;add-member request on this collection

        @param request: the request object
        @type request: L{Request}
        """

        # Create a name for the new child
        name = str(uuid.uuid4()) + self.resourceSuffix()

        # Get a resource for the new child
        parentURL = request.path
        newchildURL = joinURL(parentURL, name)
        newchild = (yield request.locateResource(newchildURL))

        # Treat as if it were a regular PUT to a new resource
        response = (yield newchild.http_PUT(request))

        # May need to add a location header
        addLocation(request, request.unparseURL(path=newchildURL, params=""))

        returnValue(response)

    @inlineCallbacks
    def checkCTagPrecondition(self, request):
        if request.headers.hasHeader("If"):
            iffy = request.headers.getRawHeaders("If")[0]
            prefix = "<%sctag/" % (customxml.mm_namespace,)
            if prefix in iffy:
                testctag = iffy[iffy.find(prefix):]
                testctag = testctag[len(prefix):]
                testctag = testctag.split(">", 1)[0]
                ctag = (yield self.getInternalSyncToken())
                if testctag != ctag:
                    raise HTTPError(StatusResponse(PRECONDITION_FAILED, "CTag pre-condition failure"))

    def checkReturnChanged(self, request):
        if request.headers.hasHeader("X-MobileMe-DAV-Options"):
            return_changed = request.headers.getRawHeaders("X-MobileMe-DAV-Options")[0]
            return ("return-changed-data" in return_changed)
        else:
            return False

    @requiresPermissions(davxml.Bind())
    @inlineCallbacks
    def simpleBatchPOST(self, request):

        # If CTag precondition
        yield self.checkCTagPrecondition(request)

        # Look for return changed data option
        return_changed = self.checkReturnChanged(request)

        # Read in all data
        data = (yield allDataFromStream(request.stream))

        format = request.headers.getHeader("content-type")
        if format:
            format = "%s/%s" % (format.mediaType, format.mediaSubtype,)
        components = self.componentsFromData(data, format)
        if components is None:
            raise HTTPError(StatusResponse(BAD_REQUEST, "Could not parse valid data from request body"))

        # Build response
        xmlresponses = [None] * len(components)
        indexedComponents = [idxComponent for idxComponent in enumerate(components)]
        yield self.bulkCreate(indexedComponents, request, return_changed, xmlresponses, format)

        result = MultiStatusResponse(xmlresponses)

        newctag = (yield self.getInternalSyncToken())
        result.headers.setRawHeaders("CTag", (newctag,))

        # Setup some useful logging
        request.submethod = "Simple batch"
        if not hasattr(request, "extendedLogItems"):
            request.extendedLogItems = {}
        request.extendedLogItems["rcount"] = len(xmlresponses)

        returnValue(result)

    @inlineCallbacks
    def bulkCreate(self, indexedComponents, request, return_changed, xmlresponses, format):
        """
        Do create from simpleBatchPOST or crudCreate()
        Subclasses may override
        """
        for index, component in indexedComponents:

            try:
                if component is None:
                    newchildURL = ""
                    newchild = None
                    changedComponent = None
                    raise ValueError("Invalid component")

                # Create a new name if one was not provided
                name = hashlib.md5(str(index) + component.resourceUID() + str(time.time()) + request.path).hexdigest() + self.resourceSuffix()

                # Get a resource for the new item
                newchildURL = joinURL(request.path, name)
                newchild = (yield request.locateResource(newchildURL))
                changedComponent = (yield self.storeResourceData(newchild, component, returnChangedData=return_changed))

            except HTTPError as e:
                # Extract the pre-condition
                code = e.response.code
                if isinstance(e.response, ErrorResponse):
                    error = e.response.error
                    error = (error.namespace, error.name,)

                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, None, code, error, format)
                )

            except Exception:
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, None, BAD_REQUEST, None, format)
                )

            else:
                if not return_changed:
                    changedComponent = None
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, changedComponent, None, None, format)
                )

    @inlineCallbacks
    def bulkCreateResponse(self, component, newchildURL, newchild, changedComponent, code, error, format):
        """
        generate one xmlresponse for bulk create
        """
        if code is None:
            etag = (yield newchild.etag())
            if changedComponent is None:
                returnValue(
                    davxml.PropertyStatusResponse(
                        davxml.HRef.fromString(newchildURL),
                        davxml.PropertyStatus(
                            davxml.PropertyContainer(
                                davxml.GETETag.fromString(etag.generate()),
                                customxml.UID.fromString(component.resourceUID() if component else ""),
                            ),
                            davxml.Status.fromResponseCode(OK),
                        )
                    )
                )
            else:
                returnValue(
                    davxml.PropertyStatusResponse(
                        davxml.HRef.fromString(newchildURL),
                        davxml.PropertyStatus(
                            davxml.PropertyContainer(
                                davxml.GETETag.fromString(etag.generate()),
                                self.xmlDataElementType().fromComponent(changedComponent, format),
                            ),
                            davxml.Status.fromResponseCode(OK),
                        )
                    )
                )
        else:
            returnValue(
                davxml.StatusResponse(
                    davxml.HRef.fromString(""),
                    davxml.Status.fromResponseCode(code),
                    davxml.Error(
                        WebDAVUnknownElement.withName(*error),
                        customxml.UID.fromString(component.resourceUID() if component else ""),
                    ) if error else None,
                )
            )

    @inlineCallbacks
    def crudBatchPOST(self, request, xmlroot):

        # Need to force some kind of overall authentication on the request
        yield self.authorize(request, (davxml.Read(), davxml.Write(),))

        # If CTag precondition
        yield self.checkCTagPrecondition(request)

        # Look for return changed data option
        return_changed = self.checkReturnChanged(request)

        # setup for create, update, and delete
        crudDeleteInfo = []
        crudUpdateInfo = []
        crudCreateInfo = []
        for index, xmlchild in enumerate(xmlroot.children):

            # Determine the multiput operation: create, update, delete
            href = xmlchild.childOfType(davxml.HRef.qname())
            set_items = xmlchild.childOfType(davxml.Set.qname())
            prop = set_items.childOfType(davxml.PropertyContainer.qname()) if set_items is not None else None
            xmldata_root = prop if prop else set_items
            xmldata = xmldata_root.childOfType(self.xmlDataElementType().qname()) if xmldata_root is not None else None
            if href is None:

                if xmldata is None:
                    raise HTTPError(StatusResponse(BAD_REQUEST, "Could not parse valid data from request body without a DAV:Href present"))

                crudCreateInfo.append((index, xmldata))
            else:
                delete = xmlchild.childOfType(customxml.Delete.qname())
                ifmatch = xmlchild.childOfType(customxml.IfMatch.qname())
                if ifmatch:
                    ifmatch = str(ifmatch.children[0]) if len(ifmatch.children) == 1 else None
                if delete is None:
                    if set_items is None:
                        raise HTTPError(StatusResponse(BAD_REQUEST, "Could not parse valid data from request body - no set_items of delete operation"))
                    if xmldata is None:
                        raise HTTPError(StatusResponse(BAD_REQUEST, "Could not parse valid data from request body for set_items operation"))
                    crudUpdateInfo.append((index, str(href), xmldata, ifmatch))
                else:
                    crudDeleteInfo.append((index, str(href), ifmatch))

        # now do the work
        xmlresponses = [None] * len(xmlroot.children)
        yield self.crudDelete(crudDeleteInfo, request, xmlresponses)
        yield self.crudCreate(crudCreateInfo, request, xmlresponses, return_changed)
        yield self.crudUpdate(crudUpdateInfo, request, xmlresponses, return_changed)

        result = MultiStatusResponse(xmlresponses)  # @UndefinedVariable

        newctag = (yield self.getInternalSyncToken())
        result.headers.setRawHeaders("CTag", (newctag,))

        # Setup some useful logging
        request.submethod = "CRUD batch"
        if not hasattr(request, "extendedLogItems"):
            request.extendedLogItems = {}
        request.extendedLogItems["rcount"] = len(xmlresponses)
        if crudCreateInfo:
            request.extendedLogItems["create"] = len(crudCreateInfo)
        if crudUpdateInfo:
            request.extendedLogItems["update"] = len(crudUpdateInfo)
        if crudDeleteInfo:
            request.extendedLogItems["delete"] = len(crudDeleteInfo)

        returnValue(result)

    @inlineCallbacks
    def crudCreate(self, crudCreateInfo, request, xmlresponses, return_changed):

        if crudCreateInfo:
            # Do privilege check on collection once
            try:
                yield self.authorize(request, (davxml.Bind(),))
                hasPrivilege = True
            except HTTPError as e:
                hasPrivilege = e

            # get components
            indexedComponents = []
            for index, xmldata in crudCreateInfo:

                try:
                    component = xmldata.generateComponent()
                except:
                    component = None
                format = xmldata.content_type

                if hasPrivilege is not True:
                    e = hasPrivilege  # use same code pattern as exception
                    code = e.response.code
                    if isinstance(e.response, ErrorResponse):
                        error = e.response.error
                        error = (error.namespace, error.name,)

                    xmlresponse = yield self.bulkCreateResponse(component, None, None, None, code, error, format)
                    xmlresponses[index] = xmlresponse

                else:
                    indexedComponents.append((index, component,))

            yield self.bulkCreate(indexedComponents, request, return_changed, xmlresponses, format)

    @inlineCallbacks
    def crudUpdate(self, crudUpdateInfo, request, xmlresponses, return_changed):

        for index, href, xmldata, ifmatch in crudUpdateInfo:

            code = None
            error = None
            try:
                component = xmldata.generateComponent()
                format = xmldata.content_type

                updateResource = (yield request.locateResource(href))
                if not updateResource.exists():
                    raise HTTPError(NOT_FOUND)

                # Check privilege
                yield updateResource.authorize(request, (davxml.Write(),))

                # Check if match
                etag = (yield updateResource.etag())
                if ifmatch and ifmatch != etag.generate():
                    raise HTTPError(PRECONDITION_FAILED)

                changedComponent = yield self.storeResourceData(updateResource, component, returnChangedData=return_changed)
                etag = (yield updateResource.etag())

            except HTTPError as e:
                # Extract the pre-condition
                code = e.response.code
                if isinstance(e.response, ErrorResponse):
                    error = e.response.error
                    error = (error.namespace, error.name,)

            except Exception:
                code = BAD_REQUEST

            if code is None:
                if changedComponent is None:
                    xmlresponses[index] = davxml.PropertyStatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.PropertyStatus(
                            davxml.PropertyContainer(
                                davxml.GETETag.fromString(etag.generate()),
                            ),
                            davxml.Status.fromResponseCode(OK),
                        )
                    )
                else:
                    xmlresponses[index] = davxml.PropertyStatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.PropertyStatus(
                            davxml.PropertyContainer(
                                davxml.GETETag.fromString(etag.generate()),
                                self.xmlDataElementType().fromComponent(changedComponent, format),
                            ),
                            davxml.Status.fromResponseCode(OK),
                        )
                    )
            else:
                xmlresponses[index] = davxml.StatusResponse(
                    davxml.HRef.fromString(href),
                    davxml.Status.fromResponseCode(code),
                    davxml.Error(
                        WebDAVUnknownElement.withName(*error),
                    ) if error else None,
                )

    @inlineCallbacks
    def crudDelete(self, crudDeleteInfo, request, xmlresponses):

        if crudDeleteInfo:

            # Do privilege check on collection once
            try:
                yield self.authorize(request, (davxml.Unbind(),))
                hasPrivilege = True
            except HTTPError as e:
                hasPrivilege = e

            for index, href, ifmatch in crudDeleteInfo:
                code = None
                error = None
                try:
                    if hasPrivilege is not True:
                        raise hasPrivilege

                    deleteResource = (yield request.locateResource(href))
                    if not deleteResource.exists():
                        raise HTTPError(NOT_FOUND)

                    # Check if match
                    etag = (yield deleteResource.etag())
                    if ifmatch and ifmatch != etag.generate():
                        raise HTTPError(PRECONDITION_FAILED)

                    yield deleteResource.storeRemove(request)

                except HTTPError as e:
                    # Extract the pre-condition
                    code = e.response.code
                    if isinstance(e.response, ErrorResponse):
                        error = e.response.error
                        error = (error.namespace, error.name,)

                except Exception:
                    code = BAD_REQUEST

                if code is None:
                    xmlresponses[index] = davxml.StatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.Status.fromResponseCode(OK),
                    )
                else:
                    xmlresponses[index] = davxml.StatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.Status.fromResponseCode(code),
                        davxml.Error(
                            WebDAVUnknownElement.withName(*error),
                        ) if error else None,
                    )

    def search(self, filter, **kwargs):
        return self._newStoreObject.search(filter, **kwargs)

    def notifierID(self):
        return "%s/%s" % self._newStoreObject.notifierID()

    def notifyChanged(self):
        return self._newStoreObject.notifyChanged()


class _CalendarCollectionBehaviorMixin():
    """
    Functions common to calendar and inbox collections
    """

    # Support component set behaviors
    def setSupportedComponentSet(self, support_components_property):
        """
        Parse out XML property into list of components and give to store.
        """
        support_components = tuple([comp.attributes["name"].upper() for comp in support_components_property.children])
        return self.setSupportedComponents(support_components)

    def getSupportedComponentSet(self):
        comps = self._newStoreObject.getSupportedComponents()
        if comps:
            comps = comps.split(",")
        else:
            comps = ical.allowedStoreComponents
        return caldavxml.SupportedCalendarComponentSet(
            *[caldavxml.CalendarComponent(name=item) for item in comps]
        )

    def setSupportedComponents(self, components):
        """
        Set the allowed component set for this calendar.

        @param components: list of names of components to support
        @type components: C{list}
        """

        # Validate them first - raise on failure
        if not self.validSupportedComponents(components):
            raise HTTPError(StatusResponse(FORBIDDEN, "Invalid CALDAV:supported-calendar-component-set"))

        support_components = ",".join(sorted([comp.upper() for comp in components]))
        return maybeDeferred(self._newStoreObject.setSupportedComponents, support_components)

    def getSupportedComponents(self):
        comps = self._newStoreObject.getSupportedComponents()
        if comps:
            comps = comps.split(",")
        else:
            comps = ical.allowedStoreComponents
        return comps

    def isSupportedComponent(self, componentType):
        return self._newStoreObject.isSupportedComponent(componentType)

    def validSupportedComponents(self, components):
        """
        Test whether the supplied set of components is valid for the current server's component set
        restrictions.
        """
        if config.RestrictCalendarsToOneComponentType:
            return components in (("VEVENT",), ("VTODO",),)
        return True


class CalendarCollectionResource(DefaultAlarmPropertyMixin, _CalendarCollectionBehaviorMixin, _CommonHomeChildCollectionMixin, CalDAVResource):
    """
    Wrapper around a L{txdav.caldav.icalendar.ICalendar}.
    """

    StoreExceptionsErrors = {
        LockTimeout: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Lock timed out.",),
        AlreadyInTrashError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "not-in-trash",),),
        FailedCrossPodRequestError: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Cross-pod request failed.",),
    }

    def __init__(self, calendar, home, name=None, *args, **kw):
        """
        Create a CalendarCollectionResource from a L{txdav.caldav.icalendar.ICalendar}
        and the arguments required for L{CalDAVResource}.
        """

        self._childClass = CalendarObjectResource
        super(CalendarCollectionResource, self).__init__(*args, **kw)
        self._initializeWithHomeChild(calendar, home)
        self._name = calendar.name() if calendar else name

        if config.EnableBatchUpload:
            self._postHandlers[("text", "calendar")] = _CommonHomeChildCollectionMixin.simpleBatchPOST
            if config.EnableJSONData:
                self._postHandlers[("application", "calendar+json")] = _CommonHomeChildCollectionMixin.simpleBatchPOST
            self.xmlDocHandlers[customxml.Multiput] = _CommonHomeChildCollectionMixin.crudBatchPOST

    def __repr__(self):
        return "<Calendar Collection Resource %r:%r %s>" % (
            self._newStoreParentHome.uid(),
            self._name,
            "" if self._newStoreObject else "Non-existent"
        )

    def isCollection(self):
        return True

    def isCalendarCollection(self):
        """
        Yes, it is a calendar collection.
        """
        return True

    def resourceType(self):
        if self.isSharedByOwner():
            return customxml.ResourceType.sharedownercalendar
        elif self.isShareeResource():
            return customxml.ResourceType.sharedcalendar
        elif self._newStoreObject.isTrash():
            return customxml.ResourceType.trash
        else:
            return caldavxml.ResourceType.calendar

    @inlineCallbacks
    def iCalendarRolledup(self, request):
        # FIXME: uncached: implement cache in the storage layer

        # Accept header handling
        accepted_type = bestAcceptType(request.headers.getHeader("accept"), Component.allowedTypes())
        if accepted_type is None:
            raise HTTPError(StatusResponse(responsecode.NOT_ACCEPTABLE, "Cannot generate requested data type"))

        # Generate a monolithic calendar
        calendar = VCalendar("VCALENDAR")
        calendar.addProperty(VProperty("VERSION", "2.0"))
        calendar.addProperty(VProperty("PRODID", iCalendarProductID))

        # Add a display name if available
        displayName = self.displayName()
        if displayName is not None:
            calendar.addProperty(VProperty("X-WR-CALNAME", displayName))

        # Do some optimisation of access control calculation by determining any
        # inherited ACLs outside of the child resource loop and supply those to
        # the checkPrivileges on each child.
        filteredaces = (yield self.inheritedACEsforChildren(request))

        tzids = set()
        isowner = (yield self.isOwner(request))

        for name in (yield self._newStoreObject.listObjectResources()):
            try:
                child = yield request.locateChildResource(self, name)
            except TypeError:
                child = None

            if child is not None:
                # Check privileges of child - skip if access denied
                try:
                    yield child.checkPrivileges(request, (davxml.Read(),), inherited_aces=filteredaces)
                except AccessDeniedError:
                    continue

                # Get the access filtered view of the data
                try:
                    subcalendar = yield child.iCalendarFiltered(isowner)
                except ValueError:
                    continue
                assert subcalendar.name() == "VCALENDAR"

                for component in subcalendar.subcomponents():

                    # Only insert VTIMEZONEs once
                    if component.name() == "VTIMEZONE":
                        tzid = component.propertyValue("TZID")
                        if tzid in tzids:
                            continue
                        tzids.add(tzid)

                    calendar.addComponent(component)

        returnValue((calendar, accepted_type,))

    createCalendarCollection = _CommonHomeChildCollectionMixin.createCollection

    @classmethod
    def componentsFromData(cls, data, format):
        """
        Need to split a single VCALENDAR into separate ones based on UID with the
        appropriate VTIEMZONES included.
        """
        return Component.componentsFromData(data, format)

    @classmethod
    def resourceSuffix(cls):
        return ".ics"

    @classmethod
    def xmlDataElementType(cls):
        return caldavxml.CalendarData

    def dynamicProperties(self):
        return super(CalendarCollectionResource, self).dynamicProperties() + tuple(
            DefaultAlarmPropertyMixin.ALARM_PROPERTIES.keys()
        ) + (
            caldavxml.CalendarTimeZone.qname(),
            caldavxml.CalendarTimeZoneID.qname(),
        )

    def hasProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        # Handle certain built-in values
        if qname in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            return succeed(self.getDefaultAlarmProperty(qname) is not None)

        elif qname in (caldavxml.CalendarTimeZone.qname(), caldavxml.CalendarTimeZoneID.qname(),):
            return succeed(self._newStoreObject.getTimezone() is not None)

        else:
            return super(CalendarCollectionResource, self).hasProperty(property, request)

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        if qname in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            returnValue(self.getDefaultAlarmProperty(qname))

        elif qname == caldavxml.CalendarTimeZone.qname():
            timezone = self._newStoreObject.getTimezone()
            format = property.content_type if isinstance(property, caldavxml.CalendarTimeZone) else None
            returnValue(caldavxml.CalendarTimeZone.fromCalendar(timezone, format=format) if timezone else None)

        elif qname == caldavxml.CalendarTimeZoneID.qname():
            tzid = self._newStoreObject.getTimezoneID()
            returnValue(caldavxml.CalendarTimeZoneID.fromString(tzid) if tzid else None)

        result = (yield super(CalendarCollectionResource, self).readProperty(property, request))
        returnValue(result)

    @inlineCallbacks
    def writeProperty(self, property, request):

        if property.qname() in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            if not property.valid():
                raise HTTPError(ErrorResponse(
                    responsecode.CONFLICT,
                    (caldav_namespace, "valid-calendar-data"),
                    description="Invalid property"
                ))
            yield self.setDefaultAlarmProperty(property)
            returnValue(None)

        elif property.qname() == caldavxml.CalendarTimeZone.qname():
            if not property.valid():
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (caldav_namespace, "valid-calendar-data"),
                    description="Invalid property"
                ))
            yield self._newStoreObject.setTimezone(property.calendar())
            returnValue(None)

        elif property.qname() == caldavxml.CalendarTimeZoneID.qname():
            tzid = property.toString()
            try:
                yield self._newStoreObject.setTimezoneID(tzid)
            except TimezoneException:
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (caldav_namespace, "valid-timezone"),
                    description="Invalid property"
                ))
            returnValue(None)

        elif property.qname() == caldavxml.ScheduleCalendarTransp.qname():
            yield self._newStoreObject.setUsedForFreeBusy(property == caldavxml.ScheduleCalendarTransp(caldavxml.Opaque()))
            returnValue(None)

        result = (yield super(CalendarCollectionResource, self).writeProperty(property, request))
        returnValue(result)

    @inlineCallbacks
    def removeProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        if qname in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            result = (yield self.removeDefaultAlarmProperty(qname))
            returnValue(result)

        elif qname in (caldavxml.CalendarTimeZone.qname(), caldavxml.CalendarTimeZoneID.qname(),):
            yield self._newStoreObject.setTimezone(None)
            returnValue(None)

        result = (yield super(CalendarCollectionResource, self).removeProperty(property, request))
        returnValue(result)

    def canBeShared(self):
        return config.Sharing.Enabled and config.Sharing.Calendars.Enabled

    @inlineCallbacks
    def storeResourceData(self, newchild, component, returnChangedData=False):

        yield newchild.storeComponent(component)
        if returnChangedData and newchild._newStoreObject._componentChanged:
            result = (yield newchild.componentForUser())
            returnValue(result)
        else:
            returnValue(None)

    # FIXME: access control
    @inlineCallbacks
    def http_MOVE(self, request):
        """
        Moving a calendar collection is allowed for the purposes of changing
        that calendar's name.
        """
        result = (yield super(CalendarCollectionResource, self).http_MOVE(request))
        returnValue(result)


class StoreScheduleInboxResource(_CalendarCollectionBehaviorMixin, _CommonHomeChildCollectionMixin, ScheduleInboxResource):

    def __init__(self, *a, **kw):

        self._childClass = CalendarObjectResource
        super(StoreScheduleInboxResource, self).__init__(*a, **kw)
        self.parent.propagateTransaction(self)

    @classmethod
    @inlineCallbacks
    def maybeCreateInbox(cls, *a, **kw):
        self = cls(*a, **kw)
        home = self.parent._newStoreHome
        storage = yield home.calendarWithName("inbox")
        if storage is None:
            # raise RuntimeError("backend should be handling this for us")
            # FIXME: spurious error, sanity check, should not be needed;
            # unfortunately, user09's calendar home does not have an inbox, so
            # this is a temporary workaround.
            yield home.createCalendarWithName("inbox")
            storage = yield home.calendarWithName("inbox")
        self._initializeWithHomeChild(
            storage,
            self.parent
        )
        self._name = storage.name()
        returnValue(self)

    def provisionFile(self):
        pass

    def provision(self):
        pass

    def http_DELETE(self, request):
        return FORBIDDEN

    def http_COPY(self, request):
        return FORBIDDEN

    def http_MOVE(self, request):
        return FORBIDDEN


class _GetChildHelper(CalDAVResource):

    def locateChild(self, request, segments):
        if segments[0] == '':
            return self, segments[1:]
        return self.getChild(segments[0]), segments[1:]

    def getChild(self, name):
        return None

    def readProperty(self, prop, request):
        if type(prop) is tuple:
            qname = prop
        else:
            qname = prop.qname()

        if qname == (dav_namespace, "resourcetype"):
            return succeed(self.resourceType())
        return super(_GetChildHelper, self).readProperty(prop, request)

    def davComplianceClasses(self):
        return ("1", "access-control")

    @requiresPermissions(davxml.Read())
    def http_GET(self, request):
        return super(_GetChildHelper, self).http_GET(request)


class DropboxCollection(_GetChildHelper):
    """
    A collection of all dropboxes (containers for attachments), presented as a
    resource under the user's calendar home, where a dropbox is a
    L{CalendarObjectDropbox}.
    """
    # FIXME: no direct tests for this class at all.

    def __init__(self, parent, *a, **kw):
        kw.update(principalCollections=parent.principalCollections())
        super(DropboxCollection, self).__init__(*a, **kw)
        self._newStoreHome = parent._newStoreHome
        parent.propagateTransaction(self)

    def isCollection(self):
        """
        It is a collection.
        """
        return True

    @inlineCallbacks
    def getChild(self, name):
        calendarObject = yield self._newStoreHome.calendarObjectWithDropboxID(name)
        if calendarObject is None:
            returnValue(NoDropboxHere())
        objectDropbox = CalendarObjectDropbox(
            calendarObject, principalCollections=self.principalCollections()
        )
        self.propagateTransaction(objectDropbox)
        returnValue(objectDropbox)

    def resourceType(self,):
        return davxml.ResourceType.dropboxhome  # @UndefinedVariable

    def listChildren(self):
        return self._newStoreHome.getAllDropboxIDs()


class NoDropboxHere(_GetChildHelper):

    def getChild(self, name):
        raise HTTPError(FORBIDDEN)

    def isCollection(self):
        return False

    def exists(self):
        return False

    def http_GET(self, request):
        return FORBIDDEN

    def http_MKCALENDAR(self, request):
        return FORBIDDEN

    @requiresPermissions(fromParent=[davxml.Bind()])
    def http_MKCOL(self, request):
        return CREATED


class CalendarObjectDropbox(_GetChildHelper):
    """
    A wrapper around a calendar object which serves that calendar object's
    attachments as a DAV collection.
    """

    def __init__(self, calendarObject, *a, **kw):
        super(CalendarObjectDropbox, self).__init__(*a, **kw)
        self._newStoreCalendarObject = calendarObject

    def isCollection(self):
        return True

    def resourceType(self):
        return davxml.ResourceType.dropbox  # @UndefinedVariable

    @inlineCallbacks
    def getChild(self, name):
        attachment = yield self._newStoreCalendarObject.attachmentWithName(name)
        result = CalendarAttachment(
            self._newStoreCalendarObject,
            attachment,
            name,
            False,
            principalCollections=self.principalCollections()
        )
        self.propagateTransaction(result)
        returnValue(result)

    @requiresPermissions(davxml.WriteACL())
    @inlineCallbacks
    def http_ACL(self, request):
        """
        Don't ever actually make changes, but attempt to deny any ACL requests
        that refer to permissions not referenced by attendees in the iCalendar
        data.
        """

        attendees = (yield self._newStoreCalendarObject.component()).getAttendees()
        attendees = [attendee.split("urn:x-uid:")[-1] for attendee in attendees]
        document = yield davXMLFromStream(request.stream)
        for ace in document.root_element.children:
            for child in ace.children:
                if isinstance(child, davxml.Principal):
                    for href in child.children:
                        principalURI = href.children[0].data
                        uidsPrefix = '/principals/__uids__/'
                        if not principalURI.startswith(uidsPrefix):
                            # Unknown principal.
                            returnValue(FORBIDDEN)
                        principalElements = principalURI[
                            len(uidsPrefix):].split("/")
                        if principalElements[-1] == '':
                            principalElements.pop()
                        if principalElements[-1] in ('calendar-proxy-read',
                                                     'calendar-proxy-write'):
                            principalElements.pop()
                        if len(principalElements) != 1:
                            returnValue(FORBIDDEN)
                        principalUID = principalElements[0]
                        if principalUID not in attendees:
                            returnValue(FORBIDDEN)
        returnValue(OK)

    @requiresPermissions(fromParent=[davxml.Bind()])
    def http_MKCOL(self, request):
        return CREATED

    @requiresPermissions(fromParent=[davxml.Unbind()])
    def http_DELETE(self, request):
        return NO_CONTENT

    @inlineCallbacks
    def listChildren(self):
        l = []
        for attachment in (yield self._newStoreCalendarObject.attachments()):
            l.append(attachment.name())
        returnValue(l)

    @inlineCallbacks
    def accessControlList(self, request, *a, **kw):
        """
        All principals identified as ATTENDEEs on the event for this dropbox
        may read all its children. Also include proxies of ATTENDEEs. Ignore
        unknown attendees.
        """
        originalACL = yield super(
            CalendarObjectDropbox, self).accessControlList(request, *a, **kw)
        originalACEs = list(originalACL.children)

        if config.EnableProxyPrincipals:
            owner = (yield self.ownerPrincipal(request))

            originalACEs += (
                # DAV:write-acl access for this principal's calendar-proxy-write users.
                davxml.ACE(
                    davxml.Principal(davxml.HRef(joinURL(owner.principalURL(), "calendar-proxy-write/"))),
                    davxml.Grant(
                        davxml.Privilege(davxml.WriteACL()),
                    ),
                    davxml.Protected(),
                    TwistedACLInheritable(),
                ),
            )

        othersCanWrite = self._newStoreCalendarObject.attendeesCanManageAttachments()
        cuas = (yield self._newStoreCalendarObject.component()).getAttendees()
        newACEs = []
        for calendarUserAddress in cuas:
            principal = yield self.principalForCalendarUserAddress(
                calendarUserAddress
            )
            if principal is None:
                continue

            principalURL = principal.principalURL()
            writePrivileges = [
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
                davxml.Privilege(davxml.Write()),
            ]
            readPrivileges = [
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
            ]
            if othersCanWrite:
                privileges = writePrivileges
            else:
                privileges = readPrivileges
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(principalURL)),
                davxml.Grant(*privileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(joinURL(principalURL, "calendar-proxy-write/"))),
                davxml.Grant(*privileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(joinURL(principalURL, "calendar-proxy-read/"))),
                davxml.Grant(*readPrivileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))

        # Now also need invitees
        newACEs.extend((yield self.sharedDropboxACEs()))

        returnValue(davxml.ACL(*tuple(originalACEs + newACEs)))

    @inlineCallbacks
    def sharedDropboxACEs(self):

        aces = ()

        invites = yield self._newStoreCalendarObject._parentCollection.sharingInvites()
        for invite in invites:

            # Only want accepted invites
            if invite.status != _BIND_STATUS_ACCEPTED:
                continue

            userprivs = [
            ]
            if invite.mode in (_BIND_MODE_READ, _BIND_MODE_WRITE,):
                userprivs.append(davxml.Privilege(davxml.Read()))
                userprivs.append(davxml.Privilege(davxml.ReadACL()))
                userprivs.append(davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()))
            if invite.mode in (_BIND_MODE_READ,):
                userprivs.append(davxml.Privilege(davxml.WriteProperties()))
            if invite.mode in (_BIND_MODE_WRITE,):
                userprivs.append(davxml.Privilege(davxml.Write()))
            proxyprivs = list(userprivs)
            proxyprivs.remove(davxml.Privilege(davxml.ReadACL()))

            principal = yield self.principalForUID(invite.shareeUID)
            if principal is not None:
                aces += (
                    # Inheritable specific access for the resource's associated principal.
                    davxml.ACE(
                        davxml.Principal(davxml.HRef(principal.principalURL())),
                        davxml.Grant(*userprivs),
                        davxml.Protected(),
                        TwistedACLInheritable(),
                    ),
                )

                if config.EnableProxyPrincipals:
                    aces += (
                        # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-read users.
                        davxml.ACE(
                            davxml.Principal(davxml.HRef(joinURL(principal.principalURL(), "calendar-proxy-read/"))),
                            davxml.Grant(
                                davxml.Privilege(davxml.Read()),
                                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
                            ),
                            davxml.Protected(),
                            TwistedACLInheritable(),
                        ),
                        # DAV:read/DAV:read-current-user-privilege-set/DAV:write access for this principal's calendar-proxy-write users.
                        davxml.ACE(
                            davxml.Principal(davxml.HRef(joinURL(principal.principalURL(), "calendar-proxy-write/"))),
                            davxml.Grant(*proxyprivs),
                            davxml.Protected(),
                            TwistedACLInheritable(),
                        ),
                    )

        returnValue(aces)


class AttachmentsCollection(_GetChildHelper):
    """
    A collection of all managed attachments, presented as a
    resource under the user's calendar home. Attachments are stored
    in L{AttachmentsChildCollection} child collections of this one.
    """
    # FIXME: no direct tests for this class at all.

    def __init__(self, parent, *a, **kw):
        kw.update(principalCollections=parent.principalCollections())
        super(AttachmentsCollection, self).__init__(*a, **kw)
        self.parent = parent
        self._newStoreHome = self.parent._newStoreHome
        self.parent.propagateTransaction(self)

    def isCollection(self):
        """
        It is a collection.
        """
        return True

    @inlineCallbacks
    def getChild(self, name):
        calendarObject = yield self._newStoreHome.calendarObjectWithDropboxID(name)

        # Hide the dropbox if it has no children
        if calendarObject:
            if calendarObject.isInTrash():
                # Don't allow access to attachments for items in the trash
                calendarObject = None
            else:
                l = (yield calendarObject.managedAttachmentList())
                if len(l) == 0:
                    l = (yield calendarObject.attachments())
                    if len(l) == 0:
                        calendarObject = None

        if calendarObject is None:
            returnValue(NoDropboxHere())
        objectDropbox = AttachmentsChildCollection(
            calendarObject, self, principalCollections=self.principalCollections()
        )
        self.propagateTransaction(objectDropbox)
        returnValue(objectDropbox)

    def resourceType(self,):
        return davxml.ResourceType.dropboxhome  # @UndefinedVariable

    def listChildren(self):
        return self._newStoreHome.getAllDropboxIDs()

    def supportedPrivileges(self, request):
        # Just DAV standard privileges - no CalDAV ones
        return succeed(davPrivilegeSet)

    @inlineCallbacks
    def defaultAccessControlList(self):
        """
        Only read privileges allowed for managed attachments.
        """
        myPrincipal = yield self.parent.principalForRecord()

        read_privs = (
            davxml.Privilege(davxml.Read()),
            davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
        )

        aces = (
            # Inheritable access for the resource's associated principal.
            davxml.ACE(
                davxml.Principal(davxml.HRef(myPrincipal.principalURL())),
                davxml.Grant(*read_privs),
                davxml.Protected(),
                TwistedACLInheritable(),
            ),
        )

        # Give read access to config.ReadPrincipals
        aces += config.ReadACEs

        # Give all access to config.AdminPrincipals
        aces += config.AdminACEs

        if config.EnableProxyPrincipals:
            aces += (
                # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-read users.
                davxml.ACE(
                    davxml.Principal(davxml.HRef(joinURL(myPrincipal.principalURL(), "calendar-proxy-read/"))),
                    davxml.Grant(*read_privs),
                    davxml.Protected(),
                    TwistedACLInheritable(),
                ),
                # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-write users.
                davxml.ACE(
                    davxml.Principal(davxml.HRef(joinURL(myPrincipal.principalURL(), "calendar-proxy-write/"))),
                    davxml.Grant(*read_privs),
                    davxml.Protected(),
                    TwistedACLInheritable(),
                ),
            )

        returnValue(davxml.ACL(*aces))

    def accessControlList(self, request, inheritance=True, expanding=False, inherited_aces=None):
        # Permissions here are fixed, and are not subject to inheritance rules, etc.
        return self.defaultAccessControlList()


class AttachmentsChildCollection(_GetChildHelper):
    """
    A collection of all containers for attachments, presented as a
    resource under the user's calendar home, where a dropbox is a
    L{CalendarObjectDropbox}.
    """
    # FIXME: no direct tests for this class at all.

    def __init__(self, calendarObject, parent, *a, **kw):
        kw.update(principalCollections=parent.principalCollections())
        super(AttachmentsChildCollection, self).__init__(*a, **kw)
        self._newStoreCalendarObject = calendarObject
        parent.propagateTransaction(self)

    def isCollection(self):
        """
        It is a collection.
        """
        return True

    @inlineCallbacks
    def getChild(self, name):
        attachmentObject = yield self._newStoreCalendarObject.managedAttachmentRetrieval(name)
        if attachmentObject is not None:
            result = CalendarAttachment(
                None,
                attachmentObject,
                name,
                True,
                principalCollections=self.principalCollections()
            )
        else:
            attachment = yield self._newStoreCalendarObject.attachmentWithName(name)
            result = CalendarAttachment(
                self._newStoreCalendarObject,
                attachment,
                name,
                False,
                principalCollections=self.principalCollections()
            )

        self.propagateTransaction(result)
        returnValue(result)

    def resourceType(self,):
        return davxml.ResourceType.dropbox  # @UndefinedVariable

    @inlineCallbacks
    def listChildren(self):
        l = (yield self._newStoreCalendarObject.managedAttachmentList())
        for attachment in (yield self._newStoreCalendarObject.attachments()):
            l.append(attachment.name())
        returnValue(l)

    @inlineCallbacks
    def http_ACL(self, request):
        # For managed attachment compatibility this is always forbidden as dropbox clients must never be
        # allowed to store attachments or make any changes.
        return FORBIDDEN

    def http_MKCOL(self, request):
        # For managed attachment compatibility this is always forbidden as dropbox clients must never be
        # allowed to store attachments or make any changes.
        return FORBIDDEN

    @requiresPermissions(fromParent=[davxml.Unbind()])
    def http_DELETE(self, request):
        # For managed attachment compatibility this always succeeds as dropbox clients will do
        # this but we don't want them to see an error. Managed attachments will always be cleaned
        # up on removal of the actual calendar object resource.
        return NO_CONTENT

    @inlineCallbacks
    def accessControlList(self, request, *a, **kw):
        """
        All principals identified as ATTENDEEs on the event for this dropbox
        may read all its children. Also include proxies of ATTENDEEs. Ignore
        unknown attendees. Do not allow attendees to write as we don't support
        that with managed attachments. Also include sharees of the event.
        """
        originalACL = yield super(
            AttachmentsChildCollection, self).accessControlList(request, *a, **kw)
        originalACEs = list(originalACL.children)

        if config.EnableProxyPrincipals:
            owner = (yield self.ownerPrincipal(request))

            originalACEs += (
                # DAV:write-acl access for this principal's calendar-proxy-write users.
                davxml.ACE(
                    davxml.Principal(davxml.HRef(joinURL(owner.principalURL(), "calendar-proxy-write/"))),
                    davxml.Grant(
                        davxml.Privilege(davxml.WriteACL()),
                    ),
                    davxml.Protected(),
                    TwistedACLInheritable(),
                ),
            )

        cuas = (yield self._newStoreCalendarObject.component()).getAttendees()
        newACEs = []
        for calendarUserAddress in cuas:
            principal = yield self.principalForCalendarUserAddress(
                calendarUserAddress
            )
            if principal is None:
                continue

            principalURL = principal.principalURL()
            privileges = [
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
            ]
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(principalURL)),
                davxml.Grant(*privileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(joinURL(principalURL, "calendar-proxy-write/"))),
                davxml.Grant(*privileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))
            newACEs.append(davxml.ACE(
                davxml.Principal(davxml.HRef(joinURL(principalURL, "calendar-proxy-read/"))),
                davxml.Grant(*privileges),
                davxml.Protected(),
                TwistedACLInheritable(),
            ))

        # Now also need invitees
        newACEs.extend((yield self.sharedDropboxACEs()))

        returnValue(davxml.ACL(*tuple(originalACEs + newACEs)))

    @inlineCallbacks
    def _sharedAccessControl(self, invite):
        """
        Check the shared access mode of this resource, potentially consulting
        an external access method if necessary.

        @return: a L{Deferred} firing a L{bytes} or L{None}, with one of the
            potential values: C{"own"}, which means that the home is the owner
            of the collection and it is not shared; C{"read-only"}, meaning
            that the home that this collection is bound into has only read
            access to this collection; C{"read-write"}, which means that the
            home has both read and write access; C{"original"}, which means
            that it should inherit the ACLs of the owner's collection, whatever
            those happen to be, or C{None}, which means that the external
            access control mechanism has dictate the home should no longer have
            any access at all.
        """
        if invite.mode in (_BIND_MODE_DIRECT,):
            ownerUID = invite.ownerUID
            owner = yield self.principalForUID(ownerUID)
            shareeUID = invite.shareeUID
            if owner.record.recordType == WikiRecordType.macOSXServerWiki:
                # Access level comes from what the wiki has granted to the
                # sharee
                sharee = yield self.principalForUID(shareeUID)
                access = (yield owner.record.accessForRecord(sharee.record))
                if access == "read":
                    returnValue("read-only")
                elif access in ("write", "admin"):
                    returnValue("read-write")
                else:
                    returnValue(None)
            else:
                returnValue("original")
        elif invite.mode in (_BIND_MODE_READ,):
            returnValue("read-only")
        elif invite.mode in (_BIND_MODE_WRITE,):
            returnValue("read-write")
        returnValue("original")

    @inlineCallbacks
    def sharedDropboxACEs(self):

        aces = ()
        invites = yield self._newStoreCalendarObject._parentCollection.sharingInvites()
        for invite in invites:

            # Only want accepted invites
            if invite.status != _BIND_STATUS_ACCEPTED:
                continue

            privileges = [
                davxml.Privilege(davxml.Read()),
                davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()),
            ]
            userprivs = []
            access = (yield self._sharedAccessControl(invite))
            if access in ("read-only", "read-write",):
                userprivs.extend(privileges)

            principal = yield self.principalForUID(invite.shareeUID)
            if principal is not None:
                aces += (
                    # Inheritable specific access for the resource's associated principal.
                    davxml.ACE(
                        davxml.Principal(davxml.HRef(principal.principalURL())),
                        davxml.Grant(*userprivs),
                        davxml.Protected(),
                        TwistedACLInheritable(),
                    ),
                )

                if config.EnableProxyPrincipals:
                    aces += (
                        # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-read users.
                        davxml.ACE(
                            davxml.Principal(davxml.HRef(joinURL(principal.principalURL(), "calendar-proxy-read/"))),
                            davxml.Grant(*userprivs),
                            davxml.Protected(),
                            TwistedACLInheritable(),
                        ),
                        # DAV:read/DAV:read-current-user-privilege-set/DAV:write access for this principal's calendar-proxy-write users.
                        davxml.ACE(
                            davxml.Principal(davxml.HRef(joinURL(principal.principalURL(), "calendar-proxy-write/"))),
                            davxml.Grant(*userprivs),
                            davxml.Protected(),
                            TwistedACLInheritable(),
                        ),
                    )

        returnValue(aces)


class CalendarAttachment(_NewStoreFileMetaDataHelper, _GetChildHelper):

    def __init__(self, calendarObject, attachment, attachmentName, managed, **kw):
        super(CalendarAttachment, self).__init__(**kw)
        self._newStoreCalendarObject = calendarObject  # This can be None for a managed attachment
        self._newStoreAttachment = self._newStoreObject = attachment
        self._managed = managed
        self._dead_properties = NonePropertyStore(self)
        self.attachmentName = attachmentName

    def getChild(self, name):
        return None

    def displayName(self):
        return self.name()

    @requiresPermissions(davxml.WriteContent())
    @inlineCallbacks
    def http_PUT(self, request):
        # FIXME: direct test
        # FIXME: CDT test to make sure that permissions are enforced.

        # Cannot PUT to a managed attachment
        if self._managed:
            raise HTTPError(FORBIDDEN)

        content_type = request.headers.getHeader("content-type")
        if content_type is None:
            content_type = MimeType("application", "octet-stream")

        try:
            creating = (self._newStoreAttachment is None)
            if creating:
                self._newStoreAttachment = self._newStoreObject = (
                    yield self._newStoreCalendarObject.createAttachmentWithName(
                        self.attachmentName))
            t = self._newStoreAttachment.store(content_type)
            yield readStream(request.stream, t.write)

        except AttachmentDropboxNotAllowed:
            log.error("Dropbox cannot be used after migration to managed attachments")
            raise HTTPError(FORBIDDEN)

        except Exception as e:
            log.error("Unable to store attachment: {ex}", ex=e)
            raise HTTPError(SERVICE_UNAVAILABLE)

        try:
            yield t.loseConnection()
        except AttachmentSizeTooLarge:
            raise HTTPError(
                ErrorResponse(FORBIDDEN,
                              (caldav_namespace, "max-attachment-size"))
            )
        except QuotaExceeded:
            raise HTTPError(
                ErrorResponse(INSUFFICIENT_STORAGE_SPACE,
                              (dav_namespace, "quota-not-exceeded"))
            )
        returnValue(CREATED if creating else NO_CONTENT)

    @requiresPermissions(davxml.Read())
    def http_GET(self, request):

        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        stream = ProducerStream()

        class StreamProtocol(Protocol):

            def connectionMade(self):
                stream.registerProducer(self.transport, False)

            def dataReceived(self, data):
                stream.write(data)

            def connectionLost(self, reason):
                stream.finish()
        try:
            self._newStoreAttachment.retrieve(StreamProtocol())
        except IOError as e:
            log.error("Unable to read attachment: {s!r}, due to: {ex}", s=self, ex=e)
            raise HTTPError(NOT_FOUND)

        headers = {"content-type": self.contentType()}
        headers["content-disposition"] = MimeDisposition("attachment", params={"filename": self.displayName()})
        return Response(OK, headers, stream)

    @requiresPermissions(fromParent=[davxml.Unbind()])
    @inlineCallbacks
    def http_DELETE(self, request):
        # Cannot DELETE a managed attachment
        if self._managed:
            raise HTTPError(FORBIDDEN)

        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        yield self._newStoreCalendarObject.removeAttachmentWithName(
            self._newStoreAttachment.name()
        )
        self._newStoreAttachment = self._newStoreCalendarObject = None
        returnValue(NO_CONTENT)

    http_MKCOL = None
    http_MKCALENDAR = None

    def http_PROPPATCH(self, request):
        """
        No dead properties allowed on attachments.
        """
        return FORBIDDEN

    def isCollection(self):
        return False

    def supportedPrivileges(self, request):
        # Just DAV standard privileges - no CalDAV ones
        return succeed(davPrivilegeSet)


class NoParent(CalDAVResource):

    def http_MKCALENDAR(self, request):
        return CONFLICT

    def http_PUT(self, request):
        return CONFLICT

    def isCollection(self):
        return False

    def exists(self):
        return False


class _CommonObjectResource(_NewStoreFileMetaDataHelper, _CommonStoreExceptionHandler, CalDAVResource, FancyEqMixin):

    _componentFromStream = None

    def __init__(self, storeObject, parentObject, parentResource, name, *args, **kw):
        """
        Construct a L{_CommonObjectResource} from an L{CommonObjectResource}.

        @param storeObject: The storage for the object.
        @type storeObject: L{txdav.common.CommonObjectResource}
        """
        super(_CommonObjectResource, self).__init__(*args, **kw)
        self._initializeWithObject(storeObject, parentObject)
        self._parentResource = parentResource
        self._name = name
        self._metadata = {}

    def _initializeWithObject(self, storeObject, parentObject):
        self._newStoreParent = parentObject
        self._newStoreObject = storeObject
        self._dead_properties = _NewStorePropertiesWrapper(
            self._newStoreObject.properties()
        ) if self._newStoreObject and self._newStoreParent.objectResourcesHaveProperties() else NonePropertyStore(self)

    def url(self):
        return joinURL(self._parentResource.url(), self.name())

    def isCollection(self):
        return False

    def quotaSize(self, request):
        return succeed(self._newStoreObject.size())

    def uid(self):
        return self._newStoreObject.uid()

    def component(self):
        return self._newStoreObject.component()

    def componentForUser(self):
        return self._newStoreObject.component()

    def allowedTypes(self):
        """
        Return a dict of allowed MIME types for storing, mapped to equivalent PyCalendar types.
        """
        raise NotImplementedError

    def determineType(self, content_type):
        """
        Determine if the supplied content-type is valid for storing and return the matching PyCalendar type.
        """
        format = None
        if content_type is not None:
            format = "%s/%s" % (content_type.mediaType, content_type.mediaSubtype,)
        return format if format in self.allowedTypes() else None

    @inlineCallbacks
    def render(self, request):
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        # Accept header handling
        accepted_type = bestAcceptType(request.headers.getHeader("accept"), self.allowedTypes())
        if accepted_type is None:
            raise HTTPError(StatusResponse(responsecode.NOT_ACCEPTABLE, "Cannot generate requested data type"))

        output = yield self.componentForUser()

        response = Response(OK, {}, output.getText(accepted_type))
        response.headers.setHeader("content-type", MimeType.fromString("%s; charset=utf-8" % (accepted_type,)))
        returnValue(response)

    @inlineCallbacks
    def checkPreconditions(self, request):
        """
        We override the base class to trap the failure case and process any Prefer header.
        """

        try:
            response = yield super(_CommonObjectResource, self).checkPreconditions(request)
        except HTTPError as e:
            if e.response.code == responsecode.PRECONDITION_FAILED:
                response = yield self._processPrefer(request, e.response)
                raise HTTPError(response)
            else:
                raise

        returnValue(response)

    @inlineCallbacks
    def _processPrefer(self, request, response):
        # Look for Prefer header
        prefer = request.headers.getHeader("prefer", {})
        returnRepresentation = any([key == "return" and value == "representation" for key, value, _ignore_args in prefer])

        if returnRepresentation and (response.code / 100 == 2 or response.code == responsecode.PRECONDITION_FAILED):
            oldcode = response.code
            response = (yield self.http_GET(request))
            if oldcode in (responsecode.CREATED, responsecode.PRECONDITION_FAILED):
                response.code = oldcode
            response.headers.removeHeader("content-location")
            response.headers.setHeader("content-location", self.url())

        returnValue(response)

    @requiresPermissions(fromParent=[davxml.Unbind()])
    def http_DELETE(self, request):
        """
        Override http_DELETE to validate 'depth' header.
        """
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        return self.storeRemove(request)

    def http_COPY(self, request):
        """
        Copying of calendar data isn't allowed.
        """
        # FIXME: no direct tests
        return FORBIDDEN

    @inlineCallbacks
    def http_MOVE(self, request):
        """
        MOVE for object resources.
        """

        # Do some pre-flight checks - must exist, must be move to another
        # CommonHomeChild in the same Home, destination resource must not exist
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        parent = (yield request.locateResource(parentForURL(request.uri)))

        #
        # Find the destination resource
        #
        destination_uri = request.headers.getHeader("destination")
        overwrite = request.headers.getHeader("overwrite", True)

        if not destination_uri:
            msg = "No destination header in MOVE request."
            log.error(msg)
            raise HTTPError(StatusResponse(BAD_REQUEST, msg))

        destination = (yield request.locateResource(destination_uri))
        if destination is None:
            msg = "Destination of MOVE does not exist: %s" % (destination_uri,)
            log.debug(msg)
            raise HTTPError(StatusResponse(BAD_REQUEST, msg))
        if destination.exists():
            if overwrite:
                msg = "Cannot overwrite existing resource with a MOVE"
                log.debug(msg)
                raise HTTPError(StatusResponse(FORBIDDEN, msg))
            else:
                msg = "Cannot MOVE to existing resource without overwrite flag enabled"
                log.debug(msg)
                raise HTTPError(StatusResponse(PRECONDITION_FAILED, msg))

        # Check for parent calendar collection
        destination_uri = urlsplit(destination_uri)[2]
        destinationparent = (yield request.locateResource(parentForURL(destination_uri)))
        if not isinstance(destinationparent, _CommonHomeChildCollectionMixin):
            msg = "Destination of MOVE is not valid: %s" % (destination_uri,)
            log.debug(msg)
            raise HTTPError(StatusResponse(FORBIDDEN, msg))
        if parentForURL(parentForURL(destination_uri)) != parentForURL(parentForURL(request.uri)):
            msg = "Can only MOVE within the same home collection: %s" % (destination_uri,)
            log.debug(msg)
            raise HTTPError(StatusResponse(FORBIDDEN, msg))

        #
        # Check authentication and access controls
        #
        yield parent.authorize(request, (davxml.Unbind(),))
        yield destinationparent.authorize(request, (davxml.Bind(),))

        # May need to add a location header
        addLocation(request, destination_uri)

        try:
            response = (yield self.storeMove(request, destinationparent, destination.name()))
            self._newStoreObject = None
            returnValue(response)

        # Handle the various store errors
        except Exception as err:
            self._handleStoreException(err, self.StoreMoveExceptionsErrors)
            raise

    def http_PROPPATCH(self, request):
        """
        No dead properties allowed on object resources.
        """
        if self._newStoreParent.objectResourcesHaveProperties():
            return super(_CommonObjectResource, self).http_PROPPATCH(request)
        else:
            return FORBIDDEN

    @inlineCallbacks
    def storeStream(self, stream, format):

        # FIXME: direct tests
        component = self._componentFromStream((yield allDataFromStream(stream)), format)
        result = (yield self.storeComponent(component))
        returnValue(result)

    @inlineCallbacks
    def storeComponent(self, component, **kwargs):

        try:
            if self._newStoreObject:
                yield self._newStoreObject.setComponent(component, **kwargs)
                returnValue(NO_CONTENT)
            else:
                self._newStoreObject = (yield self._newStoreParent.createObjectResourceWithName(
                    self.name(), component, self._metadata
                ))

                # Re-initialize to get stuff setup again now we have no object
                self._initializeWithObject(self._newStoreObject, self._newStoreParent)
                returnValue(CREATED)

        # Map store exception to HTTP errors
        except Exception as err:
            self._handleStoreException(err, self.StoreExceptionsErrors)
            raise

    @inlineCallbacks
    def storeMove(self, request, destinationparent, destination_name):
        """
        Move this object to a different parent.

        @param request:
        @type request: L{txweb2.iweb.IRequest}
        @param destinationparent: Parent to move to
        @type destinationparent: L{CommonHomeChild}
        @param destination_name: name of new resource
        @type destination_name: C{str}
        """

        yield self._newStoreObject.moveTo(destinationparent._newStoreObject, destination_name)
        returnValue(CREATED)

    @inlineCallbacks
    def storeRemove(self, request):
        """
        Delete this object.

        @param request: Unused by this implementation; present for signature
            compatibility with L{CalendarCollectionResource.storeRemove}.

        @type request: L{txweb2.iweb.IRequest}

        @return: an HTTP response suitable for sending to a client (or
            including in a multi-status).

         @rtype: something adaptable to L{txweb2.iweb.IResponse}
        """

        # Do delete

        try:
            yield self._newStoreObject.remove()
        except NoSuchObjectResourceError:
            raise HTTPError(NOT_FOUND)

        # Map store exception to HTTP errors
        except Exception as err:
            self._handleStoreException(err, self.StoreExceptionsErrors)
            raise

        # Re-initialize to get stuff setup again now we have no object
        self._initializeWithObject(None, self._newStoreParent)

        returnValue(NO_CONTENT)


class _MetadataProperty(object):
    """
    A python property which can be set either on a _newStoreObject or on some
    metadata if no new store object exists yet.
    """

    def __init__(self, name):
        self.name = name

    def __get__(self, oself, ptype=None):
        if oself._newStoreObject:
            return getattr(oself._newStoreObject, self.name)
        else:
            return oself._metadata.get(self.name, None)

    def __set__(self, oself, value):
        if oself._newStoreObject:
            setattr(oself._newStoreObject, self.name, value)
        else:
            oself._metadata[self.name] = value


class _CalendarObjectMetaDataMixin(object):
    """
    Dynamically create the required meta-data for an object resource
    """

    accessMode = _MetadataProperty("accessMode")
    isScheduleObject = _MetadataProperty("isScheduleObject")
    scheduleTag = _MetadataProperty("scheduleTag")
    scheduleEtags = _MetadataProperty("scheduleEtags")
    hasPrivateComment = _MetadataProperty("hasPrivateComment")


class CalendarObjectResource(_CalendarObjectMetaDataMixin, _CommonObjectResource):
    """
    A resource wrapping a calendar object.
    """

    compareAttributes = (
        "_newStoreObject",
    )

    _componentFromStream = VCalendar.fromString

    def allowedTypes(self):
        """
        Return a tuple of allowed MIME types for storing.
        """
        return Component.allowedTypes()

    @inlineCallbacks
    def inNewTransaction(self, request, label=""):
        """
        Implicit auto-replies need to span multiple transactions.  Clean out
        the given request's resource-lookup mapping, transaction, and re-look-
        up this L{CalendarObjectResource}'s calendar object in a new
        transaction.

        @return: a Deferred which fires with the new transaction, so it can be
            committed.
        """
        objectName = self._newStoreObject.name()
        calendar = self._newStoreObject.calendar()
        calendarName = calendar.name()
        ownerHome = calendar.ownerCalendarHome()
        homeUID = ownerHome.uid()
        txn = ownerHome.transaction().store().newTransaction(
            "new transaction for %s, doing: %s" % (self._newStoreObject.name(), label,))
        newParent = (
            yield (yield txn.calendarHomeWithUID(homeUID))
            .calendarWithName(calendarName)
        )
        newObject = (yield newParent.calendarObjectWithName(objectName))
        request._newStoreTransaction = txn
        request._resourcesByURL.clear()
        request._urlsByResource.clear()
        self._initializeWithObject(newObject, newParent)
        returnValue(txn)

    def componentForUser(self):
        return self._newStoreObject.componentForUser()

    def validIfScheduleMatch(self, request):
        """
        Check to see if the given request's C{If-Schedule-Tag-Match} header
        matches this resource's schedule tag.

        @raise HTTPError: if the tag does not match.

        @return: None
        """
        # Note, internal requests shouldn't issue this.
        header = request.headers.getHeader("If-Schedule-Tag-Match")
        if header:
            # Do "precondition" test
            if (self.scheduleTag != header):
                log.debug(
                    "If-Schedule-Tag-Match: header value '{h}' does not match resource value '{r}'",
                    h=header, r=self.scheduleTag
                )
                raise HTTPError(PRECONDITION_FAILED)
            return True

        elif config.Scheduling.CalDAV.ScheduleTagCompatibility:
            # Compatibility with old clients. Policy:
            #
            # 1. If If-Match header is not present, never do smart merge.
            # 2. If If-Match is present and the specified ETag is
            #    considered a "weak" match to the current Schedule-Tag,
            #    then do smart merge, else reject with a 412.
            #
            # Actually by the time we get here the precondition will
            # already have been tested and found to be OK, so we can just
            # always do smart merge now if If-Match is present.
            return request.headers.getHeader("If-Match") is not None

        else:
            return False

    StoreExceptionsErrors = {
        ObjectResourceNameNotAllowedError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        ObjectResourceNameAlreadyExistsError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        TooManyObjectResourcesError: (_CommonStoreExceptionHandler._storeExceptionError, customxml.MaxResources(),),
        ObjectResourceTooBigError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "max-resource-size"),),
        InvalidObjectResourceError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-calendar-data"),),
        InvalidComponentForStoreError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-calendar-object-resource"),),
        InvalidComponentTypeError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "supported-calendar-component"),),
        TooManyAttendeesError: (_CommonStoreExceptionHandler._storeExceptionError, MaxAttendeesPerInstance.fromString(str(config.MaxAttendeesPerInstance)),),
        InvalidCalendarAccessError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "valid-access-restriction"),),
        ValidOrganizerError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "valid-organizer"),),
        UIDExistsError: (_CommonStoreExceptionHandler._storeExceptionError, NoUIDConflict(),),
        UIDExistsElsewhereError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "unique-scheduling-object-resource"),),
        InvalidUIDError: (_CommonStoreExceptionHandler._storeExceptionError, NoUIDConflict(),),
        InvalidPerUserDataMerge: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-calendar-data"),),
        AttendeeAllowedError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "attendee-allowed"),),
        InvalidOverriddenInstanceError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-calendar-data"),),
        TooManyInstancesError: (_CommonStoreExceptionHandler._storeExceptionError, MaxInstances.fromString(str(config.MaxAllowedInstances)),),
        AttachmentStoreValidManagedID: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-managed-id"),),
        TooManyAttachments: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "max-attachments"),),
        ShareeAllowedError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "sharee-privilege-needed",),),
        DuplicatePrivateCommentsError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "no-duplicate-private-comments",),),
        LockTimeout: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Lock timed out.",),
        UnknownTimezone: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-timezone"),),
        AlreadyInTrashError: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "not-in-trash",),),
        FailedCrossPodRequestError: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Cross-pod request failed.",),
    }

    StoreMoveExceptionsErrors = {
        ObjectResourceNameNotAllowedError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        ObjectResourceNameAlreadyExistsError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        TooManyObjectResourcesError: (_CommonStoreExceptionHandler._storeExceptionError, customxml.MaxResources(),),
        InvalidResourceMove: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "valid-move"),),
        InvalidComponentTypeError: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "supported-calendar-component"),),
        LockTimeout: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Lock timed out.",),
    }

    StoreAttachmentValidErrors = {
        AttachmentStoreFailed: _CommonStoreExceptionHandler._storeExceptionError,
        InvalidAttachmentOperation: _CommonStoreExceptionHandler._storeExceptionError,
    }

    StoreAttachmentExceptionsErrors = {
        AttachmentStoreValidManagedID: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-managed-id-parameter",),),
        AttachmentRemoveFailed: (_CommonStoreExceptionHandler._storeExceptionError, (caldav_namespace, "valid-attachment-remove",),),
    }

    @inlineCallbacks
    def _checkPreconditions(self, request):
        """
        We override the base class to handle the special implicit scheduling weak ETag behavior
        for compatibility with old clients using If-Match.
        """

        if config.Scheduling.CalDAV.ScheduleTagCompatibility:

            if self.exists():
                etags = self.scheduleEtags
                if len(etags) > 1:
                    # This is almost verbatim from txweb2.static.checkPreconditions
                    if request.method not in ("GET", "HEAD"):

                        # Always test against the current etag first just in case schedule-etags is out of sync
                        etag = (yield self.etag())
                        etags = (etag,) + tuple([http_headers.ETag(schedule_etag) for schedule_etag in etags])

                        # Loop over each tag and succeed if any one matches, else re-raise last exception
                        exists = self.exists()
                        last_modified = self.lastModified()
                        last_exception = None
                        for etag in etags:
                            try:
                                http.checkPreconditions(
                                    request,
                                    entityExists=exists,
                                    etag=etag,
                                    lastModified=last_modified,
                                )
                            except HTTPError as e:
                                last_exception = e
                            else:
                                break
                        else:
                            if last_exception:
                                raise last_exception

                    # Check per-method preconditions
                    method = getattr(self, "preconditions_" + request.method, None)
                    if method:
                        returnValue((yield method(request)))
                    else:
                        returnValue(None)

        result = (yield super(CalendarObjectResource, self).checkPreconditions(request))
        returnValue(result)

    @inlineCallbacks
    def checkPreconditions(self, request):
        """
        We override the base class to do special schedule tag processing.
        """

        try:
            response = yield self._checkPreconditions(request)
        except HTTPError as e:
            if e.response.code == responsecode.PRECONDITION_FAILED:
                response = yield self._processPrefer(request, e.response)
                raise HTTPError(response)
            else:
                raise

        returnValue(response)

    def canBeShared(self):
        return False

    @inlineCallbacks
    def http_PUT(self, request):

        # Content-type check
        content_type = request.headers.getHeader("content-type")
        format = self.determineType(content_type)
        if format is None:
            log.error("MIME type {content_type} not allowed in calendar collection", content_type=content_type)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (caldav_namespace, "supported-calendar-data"),
                "Invalid MIME type for calendar collection",
            ))

        # Do schedule tag check
        try:
            schedule_tag_match = self.validIfScheduleMatch(request)
        except HTTPError as e:
            if e.response.code == responsecode.PRECONDITION_FAILED:
                response = yield self._processPrefer(request, e.response)
                raise HTTPError(response)
            else:
                raise

        # Read the calendar component from the stream
        try:
            calendardata = (yield allDataFromStream(request.stream))
            if not hasattr(request, "extendedLogItems"):
                request.extendedLogItems = {}
            request.extendedLogItems["cl"] = str(len(calendardata)) if calendardata else "0"

            # We must have some data at this point
            if calendardata is None:
                # Use correct DAV:error response
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (caldav_namespace, "valid-calendar-data"),
                    description="No calendar data"
                ))

            try:
                component = Component.fromString(calendardata, format)
            except ValueError as e:
                log.error(str(e))
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (caldav_namespace, "valid-calendar-data"),
                    "Can't parse calendar data: %s" % (str(e),)
                ))

            # Look for client fixes
            ua = request.headers.getHeader("User-Agent")
            client_fix_transp = "ForceAttendeeTRANSP" in matchClientFixes(config, ua)

            # Setup options
            options = {
                SetComponentOptions.smartMerge: schedule_tag_match,
                SetComponentOptions.clientFixTRANSP: client_fix_transp,
            }

            try:
                response = (yield self.storeComponent(component, options=options))
            except ResourceDeletedError:
                # This is OK - it just means the server deleted the resource during the PUT. We make it look
                # like the PUT succeeded.
                response = responsecode.NO_CONTENT if self.exists() else responsecode.CREATED

                # Re-initialize to get stuff setup again now we have no object
                self._initializeWithObject(None, self._newStoreParent)

                returnValue(response)

            response = IResponse(response)

            if self._newStoreObject.isScheduleObject:
                # Add a response header
                response.headers.setHeader("Schedule-Tag", self._newStoreObject.scheduleTag)

            # Must not set ETag in response if data changed
            if self._newStoreObject._componentChanged:
                def _removeEtag(request, response):
                    response.headers.removeHeader('etag')
                    return response
                _removeEtag.handleErrors = True

                request.addResponseFilter(_removeEtag, atEnd=True)

            # Handle Prefer header
            if request.headers.getHeader("accept") is None:
                request.headers.setHeader("accept", dict(((MimeType.fromString(format), 1.0,),)))
            response = yield self._processPrefer(request, response)

            returnValue(response)

        # Handle the various store errors
        except Exception as err:

            if isinstance(err, ValueError):
                log.error("Error while handling (calendar) PUT: {ex}", ex=err)
                raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(err)))
            else:
                raise

    @requiresPermissions(fromParent=[davxml.Unbind()])
    def http_DELETE(self, request):
        """
        Override http_DELETE to do schedule tag behavior.
        """
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        # Do schedule tag check
        self.validIfScheduleMatch(request)

        return self.storeRemove(request)

    @inlineCallbacks
    def http_MOVE(self, request):
        """
        Need If-Schedule-Tag-Match behavior
        """

        # Do some pre-flight checks - must exist, must be move to another
        # CommonHomeChild in the same Home, destination resource must not exist
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        # Do schedule tag check
        self.validIfScheduleMatch(request)

        result = (yield super(CalendarObjectResource, self).http_MOVE(request))
        returnValue(result)

    @inlineCallbacks
    def POST_handler_action(self, request, action):
        """
        Handle a POST request with an action= query parameter

        @param request: the request to process
        @type request: L{Request}
        @param action: the action to execute
        @type action: C{str}
        """
        if action.startswith("attachment-"):
            result = (yield self.POST_handler_attachment(request, action))
            returnValue(result)
        else:
            actioner = {
                "split": self.POST_handler_split,
            }
            if action in actioner:
                result = (yield actioner[action](request, action))
                returnValue(result)
            else:
                raise HTTPError(ErrorResponse(
                    FORBIDDEN,
                    (caldav_namespace, "valid-action-parameter",),
                    "The action parameter in the request-URI is not valid",
                ))

    @requiresPermissions(davxml.WriteContent())
    @inlineCallbacks
    def POST_handler_split(self, request, action):
        """
        Handle a split of a calendar object resource.

        @param request: HTTP request object
        @type request: L{Request}
        @param action: The request-URI 'action' argument
        @type action: C{str}

        @return: an HTTP response
        """

        # Resource must exist
        if not self.exists():
            raise HTTPError(NOT_FOUND)

        # Do schedule tag check
        try:
            self.validIfScheduleMatch(request)
        except HTTPError as e:
            if e.response.code == responsecode.PRECONDITION_FAILED:
                response = yield self._processPrefer(request, e.response)
                raise HTTPError(response)
            else:
                raise

        # Split point is in the rid query parameter
        rid = request.args.get("rid")
        if rid is None:
            raise HTTPError(ErrorResponse(
                FORBIDDEN,
                (caldav_namespace, "valid-rid-parameter",),
                "The rid parameter in the request-URI contains an invalid value",
            ))

        try:
            rid = DateTime.parseText(rid[0])
        except ValueError:
            raise HTTPError(ErrorResponse(
                FORBIDDEN,
                (caldav_namespace, "valid-rid-parameter",),
                "The rid parameter in the request-URI contains an invalid value",
            ))

        # Client may provide optional UID for the split-off past component
        pastUID = request.args.get("uid")
        if pastUID is not None:
            pastUID = pastUID[0]

        try:
            otherStoreObject = yield self._newStoreObject.splitAt(rid, pastUID)
        except InvalidSplit as e:
            raise HTTPError(ErrorResponse(
                FORBIDDEN,
                (calendarserver_namespace, "valid-split",),
                str(e),
            ))

        other = yield request.locateChildResource(self._parentResource, otherStoreObject.name())
        if other is None:
            raise responsecode.INTERNAL_SERVER_ERROR

        # Look for Prefer header
        prefer = request.headers.getHeader("prefer", {})
        returnRepresentation = any([key == "return" and value == "representation" for key, value, _ignore_args in prefer])

        if returnRepresentation:
            # Accept header handling
            accepted_type = bestAcceptType(request.headers.getHeader("accept"), Component.allowedTypes())
            if accepted_type is None:
                raise HTTPError(StatusResponse(responsecode.NOT_ACCEPTABLE, "Cannot generate requested data type"))
            etag1 = yield self.etag()
            etag2 = yield other.etag()
            scheduletag1 = self.scheduleTag
            scheduletag2 = otherStoreObject.scheduleTag
            cal1 = yield self.componentForUser()
            cal2 = yield other.componentForUser()

            xml_responses = [
                davxml.PropertyStatusResponse(
                    davxml.HRef.fromString(self.url()),
                    davxml.PropertyStatus(
                        davxml.PropertyContainer(
                            davxml.GETETag.fromString(etag1.generate()),
                            caldavxml.ScheduleTag.fromString(scheduletag1),
                            caldavxml.CalendarData.fromComponent(cal1, accepted_type),
                        ),
                        davxml.Status.fromResponseCode(OK),
                    )
                ),
                davxml.PropertyStatusResponse(
                    davxml.HRef.fromString(other.url()),
                    davxml.PropertyStatus(
                        davxml.PropertyContainer(
                            davxml.GETETag.fromString(etag2.generate()),
                            caldavxml.ScheduleTag.fromString(scheduletag2),
                            caldavxml.CalendarData.fromComponent(cal2, accepted_type),
                        ),
                        davxml.Status.fromResponseCode(OK),
                    )
                ),
            ]

            # Return multistatus with calendar data for this resource and the new one
            result = MultiStatusResponse(xml_responses)
        else:
            result = Response(responsecode.NO_CONTENT)
            result.headers.addRawHeader("Split-Component-URL", other.url())

        returnValue(result)

    @requiresPermissions(davxml.WriteContent())
    @inlineCallbacks
    def POST_handler_attachment(self, request, action):
        """
        Handle a managed attachments request on the calendar object resource.

        @param request: HTTP request object
        @type request: L{Request}
        @param action: The request-URI 'action' argument
        @type action: C{str}

        @return: an HTTP response
        """

        if not config.EnableManagedAttachments:
            returnValue(StatusResponse(responsecode.FORBIDDEN, "Managed Attachments not supported."))

        # Resource must exist to allow attachment operations
        if not self.exists():
            raise HTTPError(NOT_FOUND)

        def _getRIDs():
            rids = request.args.get("rid")
            if rids is not None:
                rids = rids[0].split(",")
                try:
                    rids = [DateTime.parseText(rid) if rid != "M" else None for rid in rids]
                except ValueError:
                    raise HTTPError(ErrorResponse(
                        FORBIDDEN,
                        (caldav_namespace, "valid-rid-parameter",),
                        "The rid parameter in the request-URI contains an invalid value",
                    ))

                if rids:
                    raise HTTPError(ErrorResponse(
                        FORBIDDEN,
                        (caldav_namespace, "valid-rid-parameter",),
                        "Server does not support per-instance attachments",
                    ))

            return rids

        def _getMID():
            mid = request.args.get("managed-id")
            if mid is None:
                raise HTTPError(ErrorResponse(
                    FORBIDDEN,
                    (caldav_namespace, "valid-managed-id-parameter",),
                    "The managed-id parameter is missing from the request-URI",
                ))
            return mid[0]

        def _getContentInfo():
            content_type = request.headers.getHeader("content-type")
            if content_type is None:
                content_type = MimeType("application", "octet-stream")
            content_disposition = request.headers.getHeader("content-disposition")
            if content_disposition is None or "filename" not in content_disposition.params:
                filename = str(uuid.uuid4())
            else:
                filename = content_disposition.params["filename"]
            return content_type, filename

        valid_preconditions = {
            "attachment-add": "valid-attachment-add",
            "attachment-update": "valid-attachment-update",
            "attachment-remove": "valid-attachment-remove",
        }

        # Dispatch to store object
        try:
            if action == "attachment-add":
                rids = _getRIDs()
                content_type, filename = _getContentInfo()
                attachment, location = (yield self._newStoreObject.addAttachment(rids, content_type, filename, request.stream))
                post_result = Response(CREATED)
                if not hasattr(request, "extendedLogItems"):
                    request.extendedLogItems = {}
                request.extendedLogItems["cl"] = str(attachment.size())

            elif action == "attachment-update":
                mid = _getMID()
                content_type, filename = _getContentInfo()
                attachment, location = (yield self._newStoreObject.updateAttachment(mid, content_type, filename, request.stream))
                post_result = Response(NO_CONTENT)
                if not hasattr(request, "extendedLogItems"):
                    request.extendedLogItems = {}
                request.extendedLogItems["cl"] = str(attachment.size())

            elif action == "attachment-remove":
                rids = _getRIDs()
                mid = _getMID()
                yield self._newStoreObject.removeAttachment(rids, mid)
                post_result = Response(NO_CONTENT)

            else:
                raise HTTPError(ErrorResponse(
                    FORBIDDEN,
                    (caldav_namespace, "valid-action-parameter",),
                    "The action parameter in the request-URI is not valid",
                ))

        except AttachmentSizeTooLarge:
            raise HTTPError(
                ErrorResponse(FORBIDDEN,
                              (caldav_namespace, "max-attachment-size"))
            )

        except TooManyAttachments:
            raise HTTPError(
                ErrorResponse(FORBIDDEN,
                              (caldav_namespace, "max-attachments"))
            )

        except QuotaExceeded:
            raise HTTPError(ErrorResponse(
                INSUFFICIENT_STORAGE_SPACE,
                (dav_namespace, "quota-not-exceeded"),
                "Could not store the supplied attachment because user quota would be exceeded",
            ))

        # Map store exception to HTTP errors
        except Exception as err:

            self._handleStoreExceptionArg(err, self.StoreAttachmentValidErrors, (caldav_namespace, valid_preconditions[action],))
            self._handleStoreException(err, self.StoreAttachmentExceptionsErrors)
            self._handleStoreException(err, self.StoreExceptionsErrors)
            raise

        # Look for Prefer header
        result = yield self._processPrefer(request, post_result)

        if action in ("attachment-add", "attachment-update",):
            result.headers.setHeader("location", location)
            result.headers.addRawHeader("Cal-Managed-ID", attachment.managedID())

        returnValue(result)


class AddressBookCollectionResource(_CommonHomeChildCollectionMixin, CalDAVResource):
    """
    Wrapper around a L{txdav.carddav.iaddressbook.IAddressBook}.
    """

    def __init__(self, addressbook, home, name=None, *args, **kw):
        """
        Create a AddressBookCollectionResource from a L{txdav.carddav.iaddressbook.IAddressBook}
        and the arguments required for L{CalDAVResource}.
        """

        self._childClass = AddressBookObjectResource
        super(AddressBookCollectionResource, self).__init__(*args, **kw)
        self._initializeWithHomeChild(addressbook, home)
        self._name = addressbook.name() if addressbook else name

        if config.EnableBatchUpload:
            self._postHandlers[("text", "vcard")] = AddressBookCollectionResource.simpleBatchPOST
            if config.EnableJSONData:
                self._postHandlers[("application", "vcard+json")] = _CommonHomeChildCollectionMixin.simpleBatchPOST
            self.xmlDocHandlers[customxml.Multiput] = AddressBookCollectionResource.crudBatchPOST

    def __repr__(self):
        return "<AddressBook Collection Resource %r:%r %s>" % (
            self._newStoreParentHome.uid(),
            self._name,
            "" if self._newStoreObject else "Non-existent"
        )

    def isCollection(self):
        return True

    def isAddressBookCollection(self):
        return True

    def resourceType(self):
        if self.isSharedByOwner():
            return customxml.ResourceType.sharedowneraddressbook
        elif self.isShareeResource():
            return customxml.ResourceType.sharedaddressbook
        else:
            return carddavxml.ResourceType.addressbook

    createAddressBookCollection = _CommonHomeChildCollectionMixin.createCollection

    @classmethod
    def componentsFromData(cls, data, format):
        try:
            return VCard.allFromString(data, format)
        except InvalidVCardDataError:
            return None

    @classmethod
    def resourceSuffix(cls):
        return ".vcf"

    @classmethod
    def xmlDataElementType(cls):
        return carddavxml.AddressData

    def canBeShared(self):
        return config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled

    @inlineCallbacks
    def storeResourceData(self, newchild, component, returnChangedData=False):

        yield newchild.storeComponent(component)
        if returnChangedData and newchild._newStoreObject._componentChanged:
            result = (yield newchild.componentForUser())
            returnValue(result)
        else:
            returnValue(None)

    def http_MOVE(self, request):
        """
        Addressbooks may not be renamed.
        """
        return FORBIDDEN

    @inlineCallbacks
    def makeChild(self, name):
        """
        call super and provision group share
        """
        abObjectResource = yield super(AddressBookCollectionResource, self).makeChild(name)
        # if abObjectResource.exists() and abObjectResource._newStoreObject.shareUID() is not None:
        #     abObjectResource = yield self.parentResource().provisionShare(abObjectResource)
        returnValue(abObjectResource)

    @inlineCallbacks
    def bulkCreate(self, indexedComponents, request, return_changed, xmlresponses, format):
        """
        bulk create allowing groups to contain member UIDs added during the same bulk create
        """
        groupRetries = []
        coaddedUIDs = set()
        for index, component in indexedComponents:

            try:
                if component is None:
                    newchildURL = ""
                    newchild = None
                    changedComponent = None
                    raise ValueError("Invalid component")

                # Create a new name if one was not provided
                name = hashlib.md5(str(index) + component.resourceUID() + str(time.time()) + request.path).hexdigest() + self.resourceSuffix()

                # Get a resource for the new item
                newchildURL = joinURL(request.path, name)
                newchild = (yield request.locateResource(newchildURL))
                changedComponent = (yield self.storeResourceData(newchild, component, returnChangedData=return_changed))

            except GroupWithUnsharedAddressNotAllowedError as e:
                # save off info and try again below
                missingUIDs = set(e.message)
                groupRetries.append((index, component, newchildURL, newchild, missingUIDs,))

            except HTTPError as e:
                # Extract the pre-condition
                code = e.response.code
                if isinstance(e.response, ErrorResponse):
                    error = e.response.error
                    error = (error.namespace, error.name,)

                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, None, code, error, format)
                )

            except Exception:
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, None, BAD_REQUEST, None, format)
                )

            else:
                if not return_changed:
                    changedComponent = None
                coaddedUIDs |= set([component.resourceUID()])
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, changedComponent, None, None, format)
                )

        if groupRetries:
            # get set of UIDs added
            coaddedUIDs |= set([groupRetry[1].resourceUID() for groupRetry in groupRetries])

            # check each group add to see if it will succeed if coaddedUIDs are allowed
            while(True):
                for groupRetry in groupRetries:
                    if bool(groupRetry[4] - coaddedUIDs):
                        break
                else:
                    break

                # give FORBIDDEN response
                index, component, newchildURL, newchild, missingUIDs = groupRetry
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, None, FORBIDDEN, None, format)
                )
                coaddedUIDs -= set([component.resourceUID()])  # group uid not added
                groupRetries.remove(groupRetry)  # remove this retry

            for index, component, newchildURL, newchild, missingUIDs in groupRetries:
                # newchild._metadata -> newchild._options during store
                newchild._metadata["coaddedUIDs"] = coaddedUIDs

                # don't catch errors, abort the whole transaction
                changedComponent = yield self.storeResourceData(newchild, component, returnChangedData=return_changed)
                if not return_changed:
                    changedComponent = None
                xmlresponses[index] = (
                    yield self.bulkCreateResponse(component, newchildURL, newchild, changedComponent, None, None, format)
                )

    @inlineCallbacks
    def crudDelete(self, crudDeleteInfo, request, xmlresponses):
        """
        Change handling of privileges
        """
        if crudDeleteInfo:
            # Do privilege check on collection once
            try:
                yield self.authorize(request, (davxml.Unbind(),))
                hasPrivilege = True
            except HTTPError as e:
                hasPrivilege = e

            for index, href, ifmatch in crudDeleteInfo:
                code = None
                error = None
                try:
                    deleteResource = (yield request.locateResource(href))
                    if not deleteResource.exists():
                        raise HTTPError(NOT_FOUND)

                    # Check if match
                    etag = (yield deleteResource.etag())
                    if ifmatch and ifmatch != etag.generate():
                        raise HTTPError(PRECONDITION_FAILED)

                    # ===========================================================
                    # # If unshared is allowed deletes fails but crud adds works work!
                    # if (hasPrivilege is not True and not (
                    #             deleteResource.isShareeResource() or
                    #             deleteResource._newStoreObject.isGroupForSharedAddressBook()
                    #         )
                    #     ):
                    #     raise hasPrivilege
                    # ===========================================================

                    # don't allow shared group deletion -> unshare
                    if (
                        deleteResource.isShareeResource() or
                        deleteResource._newStoreObject.isGroupForSharedAddressBook()
                    ):
                        raise HTTPError(FORBIDDEN)

                    if hasPrivilege is not True:
                        raise hasPrivilege

                    yield deleteResource.storeRemove(request)

                except HTTPError as e:
                    # Extract the pre-condition
                    code = e.response.code
                    if isinstance(e.response, ErrorResponse):
                        error = e.response.error
                        error = (error.namespace, error.name,)

                except Exception:
                    code = BAD_REQUEST

                if code is None:
                    xmlresponses[index] = davxml.StatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.Status.fromResponseCode(OK),
                    )
                else:
                    xmlresponses[index] = davxml.StatusResponse(
                        davxml.HRef.fromString(href),
                        davxml.Status.fromResponseCode(code),
                        davxml.Error(
                            WebDAVUnknownElement.withName(*error),
                        ) if error else None,
                    )


class AddressBookObjectResource(_CommonObjectResource):
    """
    A resource wrapping a addressbook object.
    """

    compareAttributes = (
        "_newStoreObject",
    )

    _componentFromStream = VCard.fromString

    def allowedTypes(self):
        """
        Return a tuple of allowed MIME types for storing.
        """
        return VCard.allowedTypes()

    @inlineCallbacks
    def vCardText(self):
        data = yield self.vCard()
        returnValue(str(data))

    vCard = _CommonObjectResource.component

    StoreExceptionsErrors = {
        ObjectResourceNameNotAllowedError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        ObjectResourceNameAlreadyExistsError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        TooManyObjectResourcesError: (_CommonStoreExceptionHandler._storeExceptionError, customxml.MaxResources(),),
        ObjectResourceTooBigError: (_CommonStoreExceptionHandler._storeExceptionError, (carddav_namespace, "max-resource-size"),),
        InvalidObjectResourceError: (_CommonStoreExceptionHandler._storeExceptionError, (carddav_namespace, "valid-address-data"),),
        InvalidComponentForStoreError: (_CommonStoreExceptionHandler._storeExceptionError, (carddav_namespace, "valid-addressbook-object-resource"),),
        UIDExistsError: (_CommonStoreExceptionHandler._storeExceptionError, NovCardUIDConflict(),),
        InvalidUIDError: (_CommonStoreExceptionHandler._storeExceptionError, NovCardUIDConflict(),),
        InvalidPerUserDataMerge: (_CommonStoreExceptionHandler._storeExceptionError, (carddav_namespace, "valid-address-data"),),
        LockTimeout: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Lock timed out.",),
        FailedCrossPodRequestError: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Cross-pod request failed.",),
    }

    StoreMoveExceptionsErrors = {
        ObjectResourceNameNotAllowedError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        ObjectResourceNameAlreadyExistsError: (_CommonStoreExceptionHandler._storeExceptionStatus, None,),
        TooManyObjectResourcesError: (_CommonStoreExceptionHandler._storeExceptionError, customxml.MaxResources(),),
        InvalidResourceMove: (_CommonStoreExceptionHandler._storeExceptionError, (calendarserver_namespace, "valid-move"),),
        LockTimeout: (_CommonStoreExceptionHandler._storeExceptionUnavailable, "Lock timed out.",),
    }

    def resourceType(self):
        if self.isSharedByOwner():
            return customxml.ResourceType.sharedownergroup
        elif self.isShareeResource():
            return customxml.ResourceType.sharedgroup
        else:
            return super(AddressBookObjectResource, self).resourceType()

    @inlineCallbacks
    def storeRemove(self, request):
        """
        Remove this address book object
        """

        # Handle sharing
        if self.isShareeResource():
            log.debug("Removing shared resource {s!r}", s=self)
            yield self.removeShareeResource(request)
            # Re-initialize to get stuff setup again now we have no object
            self._initializeWithObject(None, self._newStoreParent)
            returnValue(NO_CONTENT)
        elif self._newStoreObject.isGroupForSharedAddressBook():
            abCollectionResource = (yield request.locateResource(parentForURL(request.uri)))
            returnValue((yield abCollectionResource.storeRemove(request)))

        elif self.isSharedByOwner():
            yield self.downgradeFromShare(request)

        response = (
            yield super(AddressBookObjectResource, self).storeRemove(
                request
            )
        )

        returnValue(response)

    def canBeShared(self):
        return (
            config.Sharing.Enabled and
            config.Sharing.AddressBooks.Enabled and
            config.Sharing.AddressBooks.Groups.Enabled
        )

    @inlineCallbacks
    def http_PUT(self, request):

        # Content-type check
        content_type = request.headers.getHeader("content-type")
        format = self.determineType(content_type)
        if format is None:
            log.error("MIME type {content_type} not allowed in vcard collection", content_type=content_type)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (carddav_namespace, "supported-address-data"),
                "Invalid MIME type for vcard collection",
            ))

        # Read the vcard from the stream
        try:
            vcarddata = (yield allDataFromStream(request.stream))
            if not hasattr(request, "extendedLogItems"):
                request.extendedLogItems = {}
            request.extendedLogItems["cl"] = str(len(vcarddata)) if vcarddata else "0"

            # We must have some data at this point
            if vcarddata is None:
                # Use correct DAV:error response
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (carddav_namespace, "valid-address-data"),
                    description="No vcard data"
                ))

            try:
                component = VCard.fromString(vcarddata, format)
            except ValueError as e:
                log.error(str(e))
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (carddav_namespace, "valid-address-data"),
                    "Could not parse vCard",
                ))

            try:
                response = (yield self.storeComponent(component))
            except ResourceDeletedError:
                # This is OK - it just means the server deleted the resource during the PUT. We make it look
                # like the PUT succeeded.
                response = responsecode.NO_CONTENT if self.exists() else responsecode.CREATED

                # Re-initialize to get stuff setup again now we have no object
                self._initializeWithObject(None, self._newStoreParent)

                returnValue(response)

            response = IResponse(response)

            # Must not set ETag in response if data changed
            if self._newStoreObject._componentChanged:
                def _removeEtag(request, response):
                    response.headers.removeHeader('etag')
                    return response
                _removeEtag.handleErrors = True

                request.addResponseFilter(_removeEtag, atEnd=True)

            # Look for Prefer header
            if request.headers.getHeader("accept") is None:
                request.headers.setHeader("accept", dict(((MimeType.fromString(format), 1.0,),)))
            response = yield self._processPrefer(request, response)

            returnValue(response)

        # Handle the various store errors
        except KindChangeNotAllowedError:
            raise HTTPError(StatusResponse(
                FORBIDDEN,
                "vCard kind may not be changed",)
            )

        # Handle the various store errors
        except GroupWithUnsharedAddressNotAllowedError:
            raise HTTPError(StatusResponse(
                FORBIDDEN,
                "Sharee cannot add unshared group members",)
            )

        except Exception as err:

            if isinstance(err, ValueError):
                log.error("Error while handling (vCard) PUT: {ex}", ex=err)
                raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(err)))
            else:
                raise

    @inlineCallbacks
    def http_DELETE(self, request):
        """
        Override http_DELETE handle shared group deletion without fromParent=[davxml.Unbind()]
        """
        if (
            self.isShareeResource() or
            self.exists() and self._newStoreObject.isGroupForSharedAddressBook()
        ):
            returnValue((yield self.storeRemove(request)))

        returnValue((yield super(AddressBookObjectResource, self).http_DELETE(request)))

    @inlineCallbacks
    def accessControlList(self, request, *a, **kw):
        """
        Return WebDAV ACLs appropriate for the current user accessing the
        a vcard in a shared addressbook or shared group.

        Items in an "invite" share get read-only privileges.
        (It's not clear if that case ever occurs)

        "direct" shares are not supported.

        @param request: the request used to locate the owner resource.
        @type request: L{txweb2.iweb.IRequest}

        @param args: The arguments for
            L{txweb2.dav.idav.IDAVResource.accessControlList}

        @param kwargs: The keyword arguments for
            L{txweb2.dav.idav.IDAVResource.accessControlList}, plus
            keyword-only arguments.

        @return: the appropriate WebDAV ACL for the sharee
        @rtype: L{davxml.ACL}
        """
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        if not self._parentResource.isShareeResource():
            returnValue((yield super(AddressBookObjectResource, self).accessControlList(request, *a, **kw)))

        # Direct shares use underlying privileges of shared collection
        userprivs = []
        userprivs.append(davxml.Privilege(davxml.Read()))
        userprivs.append(davxml.Privilege(davxml.ReadACL()))
        userprivs.append(davxml.Privilege(davxml.ReadCurrentUserPrivilegeSet()))

        if (yield self._newStoreObject.readWriteAccess()):
            userprivs.append(davxml.Privilege(davxml.Write()))
        else:
            userprivs.append(davxml.Privilege(davxml.WriteProperties()))

        sharee = yield self.principalForUID(self._newStoreObject.viewerHome().uid())
        aces = (
            # Inheritable specific access for the resource's associated principal.
            davxml.ACE(
                davxml.Principal(davxml.HRef(sharee.principalURL())),
                davxml.Grant(*userprivs),
                davxml.Protected(),
                TwistedACLInheritable(),
            ),
        )

        # Give read access to config.ReadPrincipals
        aces += config.ReadACEs

        # Give all access to config.AdminPrincipals
        aces += config.AdminACEs

        returnValue(davxml.ACL(*aces))


class _NotificationChildHelper(object):
    """
    Methods for things which are like notification objects.
    """

    def _initializeWithNotifications(self, notifications, home):
        """
        Initialize with a notification collection.

        @param notifications: the wrapped notification collection backend
            object.
        @type notifications: L{txdav.common.inotification.INotificationCollection}

        @param home: the home through which the given notification collection
            was accessed.
        @type home: L{txdav.icommonstore.ICommonHome}
        """
        self._newStoreNotifications = notifications
        self._newStoreParentHome = home
        self._dead_properties = _NewStorePropertiesWrapper(
            self._newStoreNotifications.properties()
        )

    def locateChild(self, request, segments):
        if segments[0] == '':
            return self, segments[1:]
        return self.getChild(segments[0]), segments[1:]

    def exists(self):
        # FIXME: tests
        return True

    @inlineCallbacks
    def makeChild(self, name):
        """
        Create a L{NotificationObjectFile} or L{ProtoNotificationObjectFile}
        based on the name of a notification.
        """
        newStoreObject = (
            yield self._newStoreNotifications.notificationObjectWithName(name)
        )

        similar = StoreNotificationObjectFile(newStoreObject, self)

        # FIXME: tests should be failing without this line.
        # Specifically, http_PUT won't be committing its transaction properly.
        self.propagateTransaction(similar)
        returnValue(similar)

    @inlineCallbacks
    def listChildren(self):
        """
        @return: a sequence of the names of all known children of this resource.
        """
        children = set(self.putChildren.keys())
        children.update((yield self._newStoreNotifications.listNotificationObjects()))
        returnValue(children)


class StoreNotificationCollectionResource(_NotificationChildHelper, NotificationCollectionResource):
    """
    Wrapper around a L{txdav.caldav.icalendar.ICalendar}.
    """

    def __init__(self, notifications, homeResource, home, *args, **kw):
        """
        Create a CalendarCollectionResource from a L{txdav.caldav.icalendar.ICalendar}
        and the arguments required for L{CalDAVResource}.
        """
        super(StoreNotificationCollectionResource, self).__init__(*args, **kw)
        self._initializeWithNotifications(notifications, home)
        self._parentResource = homeResource

    def name(self):
        return "notification"

    def url(self):
        return joinURL(self._parentResource.url(), self.name(), "/")

    @inlineCallbacks
    def listChildren(self):
        l = []
        for notification in (yield self._newStoreNotifications.notificationObjects()):
            l.append(notification.name())
        returnValue(l)

    def isCollection(self):
        return True

    def getInternalSyncToken(self):
        return self._newStoreNotifications.syncToken()

    @inlineCallbacks
    def _indexWhatChanged(self, revision, depth):
        # The newstore implementation supports this directly
        returnValue(
            (yield self._newStoreNotifications.resourceNamesSinceToken(revision))
        )

    def deleteNotification(self, request, record):
        return maybeDeferred(
            self._newStoreNotifications.removeNotificationObjectWithName,
            record.name
        )


class StoreNotificationObjectFile(_NewStoreFileMetaDataHelper, NotificationResource):
    """
    A resource wrapping a calendar object.
    """

    def __init__(self, notificationObject, *args, **kw):
        """
        Construct a L{CalendarObjectResource} from an L{ICalendarObject}.

        @param calendarObject: The storage for the calendar object.
        @type calendarObject: L{txdav.caldav.icalendarstore.ICalendarObject}
        """
        super(StoreNotificationObjectFile, self).__init__(*args, **kw)
        self._initializeWithObject(notificationObject)

    def _initializeWithObject(self, notificationObject):
        self._newStoreObject = notificationObject
        self._dead_properties = NonePropertyStore(self)

    def liveProperties(self):

        props = super(StoreNotificationObjectFile, self).liveProperties()
        props += (customxml.NotificationType.qname(),)
        return props

    @inlineCallbacks
    def readProperty(self, prop, request):
        if type(prop) is tuple:
            qname = prop
        else:
            qname = prop.qname()

        if qname == customxml.NotificationType.qname():
            jsontype = self._newStoreObject.notificationType()

            # FIXME: notificationType( ) does not always return json; it can
            # currently return a utf-8 encoded str of XML
            if isinstance(jsontype, str):
                returnValue(
                    davxml.WebDAVDocument.fromString(jsontype).root_element
                )

            if jsontype["notification-type"] == "invite-notification":
                typeAttr = {"shared-type": jsontype["shared-type"]}
                xmltype = customxml.InviteNotification(**typeAttr)
            elif jsontype["notification-type"] == "invite-reply":
                xmltype = customxml.InviteReply()
            else:
                raise HTTPError(responsecode.INTERNAL_SERVER_ERROR)
            returnValue(customxml.NotificationType(xmltype))

        returnValue((yield super(StoreNotificationObjectFile, self).readProperty(prop, request)))

    def isCollection(self):
        return False

    def quotaSize(self, request):
        return succeed(self._newStoreObject.size())

    @inlineCallbacks
    def text(self, ignored=None):
        assert ignored is None, "This is a notification object, not a notification"
        jsondata = (yield self._newStoreObject.notificationData())

        # FIXME: notificationData( ) does not always return json; it can
        # currently return a utf-8 encoded str of XML
        if isinstance(jsondata, str):
            returnValue(jsondata)

        if jsondata["notification-type"] == "invite-notification":
            ownerPrincipal = yield self.principalForUID(jsondata["owner"])
            if ownerPrincipal is None:
                ownerCN = ""
                ownerCollectionURL = ""
                owner = "urn:x-uid:" + jsondata["owner"]
            else:
                ownerCN = ownerPrincipal.displayName()
                ownerHomeURL = ownerPrincipal.calendarHomeURLs()[0] if jsondata["shared-type"] == "calendar" else ownerPrincipal.addressBookHomeURLs()[0]
                ownerCollectionURL = urljoin(ownerHomeURL, jsondata["ownerName"])

                # FIXME:  use urn:uuid always?
                if jsondata["shared-type"] == "calendar":
                    owner = ownerPrincipal.principalURL()
                else:
                    owner = "urn:x-uid:" + ownerPrincipal.principalUID()

            if "supported-components" in jsondata:
                comps = jsondata["supported-components"]
                if comps:
                    comps = comps.split(",")
                else:
                    comps = ical.allowedStoreComponents
                supported = caldavxml.SupportedCalendarComponentSet(
                    *[caldavxml.CalendarComponent(name=item) for item in comps]
                )
            else:
                supported = None

            typeAttr = {"shared-type": jsondata["shared-type"]}
            xmldata = customxml.Notification(
                customxml.DTStamp.fromString(jsondata["dtstamp"]),
                customxml.InviteNotification(
                    customxml.UID.fromString(jsondata["uid"]),
                    element.HRef.fromString("urn:x-uid:" + jsondata["sharee"]),
                    invitationBindStatusToXMLMap[jsondata["status"]](),
                    customxml.InviteAccess(invitationBindModeToXMLMap[jsondata["access"]]()),
                    customxml.HostURL(
                        element.HRef.fromString(ownerCollectionURL),
                    ),
                    customxml.Organizer(
                        element.HRef.fromString(owner),
                        customxml.CommonName.fromString(ownerCN),
                    ),
                    customxml.InviteSummary.fromString(jsondata["summary"]),
                    supported,
                    **typeAttr
                ),
            )
        elif jsondata["notification-type"] == "invite-reply":
            ownerPrincipal = yield self.principalForUID(jsondata["owner"])
            ownerHomeURL = ownerPrincipal.calendarHomeURLs()[0] if jsondata["shared-type"] == "calendar" else ownerPrincipal.addressBookHomeURLs()[0]

            shareePrincipal = yield self.principalForUID(jsondata["sharee"])

            # FIXME:  use urn:x-uid always?
            if shareePrincipal is not None:
                if jsondata["shared-type"] == "calendar":
                    # Prefer mailto:, otherwise use principal URL
                    for cua in shareePrincipal.calendarUserAddresses():
                        if cua.startswith("mailto:"):
                            break
                    else:
                        cua = shareePrincipal.principalURL()
                else:
                    cua = "urn:x-uid:" + shareePrincipal.principalUID()
                commonName = shareePrincipal.displayName()
            else:
                cua = "urn:x-uid:" + jsondata["sharee"]
                commonName = ""

            typeAttr = {"shared-type": jsondata["shared-type"]}
            xmldata = customxml.Notification(
                customxml.DTStamp.fromString(jsondata["dtstamp"]),
                customxml.InviteReply(
                    element.HRef.fromString(cua),
                    invitationBindStatusToXMLMap[jsondata["status"]](),
                    customxml.HostURL(
                        element.HRef.fromString(urljoin(ownerHomeURL, jsondata["ownerName"])),
                    ),
                    customxml.InReplyTo.fromString(jsondata["in-reply-to"]),
                    customxml.InviteSummary.fromString(jsondata["summary"]) if jsondata["summary"] else None,
                    customxml.CommonName.fromString(commonName) if commonName else None,
                    **typeAttr
                ),
            )
        else:
            raise HTTPError(responsecode.INTERNAL_SERVER_ERROR)
        returnValue(xmldata.toxml())

    @requiresPermissions(davxml.Read())
    @inlineCallbacks
    def http_GET(self, request):
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        try:
            returnValue(
                Response(OK, {"content-type": self.contentType()},
                         MemoryStream((yield self.text())))
            )
        except ConcurrentModification:
            raise HTTPError(NOT_FOUND)

    @requiresPermissions(fromParent=[davxml.Unbind()])
    def http_DELETE(self, request):
        """
        Override http_DELETE to validate 'depth' header.
        """
        if not self.exists():
            log.debug("Resource not found: {s!r}", s=self)
            raise HTTPError(NOT_FOUND)

        return self.storeRemove(request)

    def http_PROPPATCH(self, request):
        """
        No dead properties allowed on notification objects.
        """
        return FORBIDDEN

    @inlineCallbacks
    def storeRemove(self, request):
        """
        Remove this notification object.
        """
        try:

            storeNotifications = self._newStoreObject.notificationCollection()

            # Do delete

            # FIXME: public attribute please
            yield storeNotifications.removeNotificationObjectWithName(
                self._newStoreObject.name()
            )

            self._initializeWithObject(None)

        except MemcacheLockTimeoutError:
            raise HTTPError(StatusResponse(CONFLICT, "Resource: %s currently in use on the server." % (request.uri,)))
        except NoSuchObjectResourceError:
            raise HTTPError(NOT_FOUND)

        returnValue(NO_CONTENT)
