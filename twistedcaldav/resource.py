# -*- test-case-name: twistedcaldav.test.test_resource,twistedcaldav.test.test_wrapping -*-
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

"""
CalDAV-aware resources.
"""

__all__ = [
    "CalDAVComplianceMixIn",
    "CalDAVResource",
    "CalendarPrincipalCollectionResource",
    "CalendarPrincipalResource",
    "isCalendarCollectionResource",
    "isPseudoCalendarCollectionResource",
    "isAddressBookCollectionResource",
]

from calendarserver.push.notifier import getPubSubAPSConfiguration

from twext.enterprise.ienterprise import AlreadyFinishedError
from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.defer import succeed, maybeDeferred, fail

from twistedcaldav import caldavxml, customxml
from twistedcaldav import carddavxml
from twistedcaldav import ical
from twistedcaldav.cache import PropfindCacheMixin
from twistedcaldav.caldavxml import caldav_namespace
from twistedcaldav.carddavxml import carddav_namespace
from twistedcaldav.config import config
from twistedcaldav.customxml import calendarserver_namespace
from twistedcaldav.datafilters.hiddeninstance import HiddenInstanceFilter
from twistedcaldav.datafilters.privateevents import PrivateEventFilter
from twistedcaldav.extensions import DAVResource, DAVPrincipalResource, \
    DAVResourceWithChildrenMixin
from twistedcaldav.ical import Component
from twistedcaldav.icaldav import ICalDAVResource, ICalendarPrincipalResource
from twistedcaldav.linkresource import LinkResource
from twistedcaldav.sharing import SharedResourceMixin, SharedHomeMixin
from twistedcaldav.vcard import Component as vComponent

from txdav.caldav.datastore.util import normalizationLookup
from txdav.common.icommondatastore import InternalDataStoreError, \
    SyncTokenValidException, ExternalShareFailed
from txdav.common.datastore.podding.base import FailedCrossPodRequestError
from txdav.xml import element
from txdav.xml.element import dav_namespace

from txweb2 import responsecode, http, http_headers
from txweb2.auth.wrapper import UnauthorizedResponse
from txweb2.dav.auth import AuthenticationWrapper as SuperAuthenticationWrapper
from txweb2.dav.http import ErrorResponse
from txweb2.dav.idav import IDAVPrincipalCollectionResource
from txweb2.dav.resource import AccessDeniedError, DAVPrincipalCollectionResource, \
    davPrivilegeSet
from txweb2.dav.resource import TwistedACLInheritable
from txweb2.dav.util import joinURL, parentForURL, normalizeURL
from txweb2.http import HTTPError, RedirectResponse, StatusResponse, Response, JSONResponse
from txweb2.http_headers import MimeType, ETag
from txweb2.stream import MemoryStream

from urlparse import urlsplit
from uuid import UUID
from zope.interface import implements
import hashlib
import time
import urllib
import uuid

##
# Sharing Conts
##
SHARE_ACCEPT_STATE_NEEDS_ACTION = "0"
SHARE_ACCEPT_STATE_ACCEPTED = "1"
SHARE_ACCEPT_STATE_DECLINED = "2"
SHARE_ACCEPT_STATE_DELETED = "-1"

shareAccpetStates = {}
shareAccpetStates[SHARE_ACCEPT_STATE_NEEDS_ACTION] = "NEEDS-ACTION"
shareAccpetStates[SHARE_ACCEPT_STATE_ACCEPTED] = "ACCEPTED"
shareAccpetStates[SHARE_ACCEPT_STATE_DECLINED] = "DECLINED"
shareAccpetStates[SHARE_ACCEPT_STATE_DELETED] = "DELETED"

shareAcceptStatesByXML = {}
shareAcceptStatesByXML["NEEDS-ACTION"] = customxml.InviteStatusNoResponse()
shareAcceptStatesByXML["ACCEPTED"] = customxml.InviteStatusAccepted()
shareAcceptStatesByXML["DECLINED"] = customxml.InviteStatusDeclined()
shareAcceptStatesByXML["DELETED"] = customxml.InviteStatusDeleted()


def requiresPermissions(*permissions, **kw):
    """
    A decorator to wrap http_ methods in, to indicate that they should not be
    run until the current user principal has been authorized for the given
    permission set.
    """
    fromParent = kw.get('fromParent')
    # FIXME: direct unit tests

    def wrap(thunk):
        def authAndContinue(self, request, *args, **kwargs):
            if permissions:
                d = self.authorize(request, permissions)
            else:
                d = succeed(None)
            if fromParent:
                d.addCallback(
                    lambda whatever:
                        request.locateResource(parentForURL(request.uri))
                ).addCallback(
                    lambda parent:
                        parent.authorize(request, fromParent)
                )
            d.addCallback(lambda whatever: thunk(self, request, *args, **kwargs))
            return d
        return authAndContinue
    return wrap


class CalDAVComplianceMixIn(object):

    def davComplianceClasses(self):
        return (
            tuple(super(CalDAVComplianceMixIn, self).davComplianceClasses()) +
            config.CalDAVComplianceClasses
        )


class ReadOnlyResourceMixIn (object):
    """
    Read only resource.
    """

    def writeProperty(self, property, request):
        raise HTTPError(self.readOnlyResponse)

    def http_ACL(self, request):
        return responsecode.FORBIDDEN

    def http_DELETE(self, request):
        return responsecode.FORBIDDEN

    def http_MKCOL(self, request):
        return responsecode.FORBIDDEN

    def http_MOVE(self, request):
        return responsecode.FORBIDDEN

    def http_PROPPATCH(self, request):
        return responsecode.FORBIDDEN

    def http_PUT(self, request):
        return responsecode.FORBIDDEN

    def http_MKCALENDAR(self, request):
        return ErrorResponse(
            responsecode.FORBIDDEN,
            (caldav_namespace, "calendar-collection-location-ok"),
            "Resource is read-only",
        )


class ReadOnlyNoCopyResourceMixIn (ReadOnlyResourceMixIn):
    """
    Read only resource that disallows COPY.
    """

    def http_COPY(self, request):
        return responsecode.FORBIDDEN


def _calendarPrivilegeSet():
    edited = False

    top_supported_privileges = []

    for supported_privilege in davPrivilegeSet.childrenOfType(element.SupportedPrivilege):
        all_privilege = supported_privilege.childOfType(element.Privilege)
        if isinstance(all_privilege.children[0], element.All):
            all_description = supported_privilege.childOfType(element.Description)
            all_supported_privileges = []
            for all_supported_privilege in supported_privilege.childrenOfType(element.SupportedPrivilege):
                read_privilege = all_supported_privilege.childOfType(element.Privilege)
                if isinstance(read_privilege.children[0], element.Read):
                    read_description = all_supported_privilege.childOfType(element.Description)
                    read_supported_privileges = list(all_supported_privilege.childrenOfType(element.SupportedPrivilege))
                    read_supported_privileges.append(
                        element.SupportedPrivilege(
                            element.Privilege(caldavxml.ReadFreeBusy()),
                            element.Description("allow free busy report query", **{"xml:lang": "en"}),
                        )
                    )
                    all_supported_privileges.append(
                        element.SupportedPrivilege(read_privilege, read_description, *read_supported_privileges)
                    )
                    edited = True
                else:
                    all_supported_privileges.append(all_supported_privilege)
            top_supported_privileges.append(
                element.SupportedPrivilege(all_privilege, all_description, *all_supported_privileges)
            )
        else:
            top_supported_privileges.append(supported_privilege)

    assert edited, "Structure of davPrivilegeSet changed in a way that I don't know how to extend for calendarPrivilegeSet"

    return element.SupportedPrivilegeSet(*top_supported_privileges)

calendarPrivilegeSet = _calendarPrivilegeSet()


class CalDAVResource (
        CalDAVComplianceMixIn, SharedResourceMixin,
        DAVResourceWithChildrenMixin, DAVResource
):
    """
    CalDAV resource.

    Extends L{DAVResource} to provide CalDAV functionality.
    """
    log = Logger()

    implements(ICalDAVResource)

    uuid_namespace = UUID("DD0E1AC0-56D6-40D4-8765-2F4D8A0F28A5")

    ##
    # HTTP
    ##

    def render(self, request):

        if not self.exists():
            return responsecode.NOT_FOUND

        if config.EnableMonolithicCalendars:
            #
            # Send listing instead of iCalendar data to HTML agents
            # This is mostly useful for debugging...
            #
            # FIXME: Add a self-link to the dirlist with a query string so
            #     users can still download the actual iCalendar data?
            #
            # FIXME: Are there better ways to detect this than hacking in
            #     user agents?
            #
            # FIXME: In the meantime, make this a configurable regex list?
            #
            agent = request.headers.getHeader("user-agent")
            if agent is not None and (
                agent.startswith("Mozilla/") and agent.find("Gecko") != -1
            ):
                renderAsHTML = True
            else:
                renderAsHTML = False
        else:
            renderAsHTML = True

        if not renderAsHTML and self.isPseudoCalendarCollection():
            # Render a monolithic iCalendar file
            if request.path[-1] != "/":
                # Redirect to include trailing '/' in URI
                return RedirectResponse(request.unparseURL(path=urllib.quote(urllib.unquote(request.path), safe=':/') + '/'))

            def _defer(result):
                data, accepted_type = result
                response = Response()
                response.stream = MemoryStream(data.getText(accepted_type))
                response.headers.setHeader("content-type", MimeType.fromString("%s; charset=utf-8" % (accepted_type,)))
                return response

            d = self.iCalendarRolledup(request)
            d.addCallback(_defer)
            return d

        return super(CalDAVResource, self).render(request)

    _associatedTransaction = None
    _transactionError = False

    def associateWithTransaction(self, transaction):
        """
        Associate this resource with a L{txdav.caldav.idav.ITransaction}; when this
        resource (or any of its children) are rendered successfully, commit the
        transaction.  Otherwise, abort the transaction.

        @param transaction: the transaction to associate this resource and its
            children with.

        @type transaction: L{txdav.caldav.idav.ITransaction}
        """
        # FIXME: needs to reject association with transaction if it's already
        # got one (resources associated with a transaction are not reusable)
        self._associatedTransaction = transaction

    def propagateTransaction(self, otherResource):
        """
        Propagate the transaction associated with this resource to another
        resource (which should ostensibly be a child resource).

        @param otherResource: Another L{CalDAVResource}, usually one being
            constructed as a child of this one.

        @type otherResource: L{CalDAVResource} (or a subclass thereof)
        """
        if not self._associatedTransaction:
            raise RuntimeError("No associated transaction to propagate")
        otherResource.associateWithTransaction(self._associatedTransaction)

    def methodRaisedException(self, failure):
        """
        An C{http_METHOD} method raised an exception.  Any type of exception,
        including those that result in perfectly valid HTTP responses, should
        abort the transaction.
        """
        self._transactionError = True
        return super(CalDAVResource, self).methodRaisedException(failure)

    @inlineCallbacks
    def renderHTTP(self, request, transaction=None):
        """
        Override C{renderHTTP} to commit the transaction when the resource is
        successfully rendered.

        @param request: the request to generate a response for.
        @type request: L{txweb2.iweb.IRequest}
        @param transaction: optional transaction to use instead of associated transaction
        @type transaction: L{txdav.caldav.idav.ITransaction}
        """
        try:
            response = yield super(CalDAVResource, self).renderHTTP(request)
        except AlreadyFinishedError:
            self._transactionError = True
        except ExternalShareFailed:
            # This happens when an external share is no longer valid and has been fixed
            # by removing it from this pod. We need to treat this as a 503 "error" but let
            # the transaction commit
            self._transactionError = False
            response = StatusResponse(responsecode.SERVICE_UNAVAILABLE, "Shared collection not valid - removing.")
        except FailedCrossPodRequestError:
            # This happens when a cross-pod connection attempt fails. Treat as a 503 so the client
            # can try again once the pod is back up.
            response = StatusResponse(responsecode.SERVICE_UNAVAILABLE, "Unable to do cross-pod request.")
            response.headers.setHeader("Retry-After", time.time() + config.TransactionHTTPRetrySeconds)

        if transaction is None:
            transaction = self._associatedTransaction
        if transaction is not None:
            if self._transactionError:
                try:
                    yield transaction.abort()
                except AlreadyFinishedError:
                    if transaction.timedout:
                        response = http.StatusResponse(responsecode.SERVICE_UNAVAILABLE, responsecode.RESPONSES[responsecode.SERVICE_UNAVAILABLE])
                        response.headers.setHeader("Retry-After", time.time() + config.TransactionHTTPRetrySeconds)
                        raise HTTPError(response)
                    else:
                        raise
            else:
                yield transaction.commit()

                # Log extended item
                if transaction.logItems:
                    if not hasattr(request, "extendedLogItems"):
                        request.extendedLogItems = {}
                    request.extendedLogItems.update(transaction.logItems)

                # May need to reset the last-modified header in the response as txn.commit() can change it due to pre-commit hooks
                if response.headers.hasHeader("last-modified"):
                    response.headers.setHeader("last-modified", self.lastModified())
        returnValue(response)

    @inlineCallbacks
    def handleMissingTrailingSlash(self, request):
        try:
            _ignore_authnUser, authzUser = yield self.authenticate(request)
        except Exception:
            authzUser = None

        # Turn 301 into 401
        if authzUser is None:
            response = (yield UnauthorizedResponse.makeResponse(
                request.credentialFactories,
                request.remoteAddr
            ))
            returnValue(response)
        else:
            response = RedirectResponse(
                request.unparseURL(
                    path=urllib.quote(
                        urllib.unquote(request.path),
                        safe=':/') + '/'
                )
            )
            returnValue(response)

    # Begin transitional new-store resource interface:

    def copyDeadPropertiesTo(self, other):
        """
        Copy this resource's dead properties to another resource.  This requires
        that the new resource have a back-end store.

        @param other: a resource to copy all properties to.
        @type other: subclass of L{CalDAVResource}
        """
        self.newStoreProperties().update(other.newStoreProperties())

    def newStoreProperties(self):
        """
        Return an L{IMapping} that represents properties.  Only available on
        new-storage objects.
        """
        raise NotImplementedError("%s does not implement newStoreProperties" %
                                  (self,))

    def storeRemove(self, *a, **kw):
        """
        Remove this resource from storage.
        """
        raise NotImplementedError("%s does not implement storeRemove" %
                                  (self,))

    def storeStream(self, stream, format):
        """
        Store the content of the stream in this resource, as it would via a PUT.

        @param stream: The stream containing the data to be stored.
        @type stream: L{IStream}

        @return: a L{Deferred} which fires with an HTTP response.
        @rtype: L{Deferred}
        """
        raise NotImplementedError("%s does not implement storeStream" % (self,))

    # End transitional new-store interface

    ##
    # WebDAV
    ##

    def liveProperties(self):
        baseProperties = (
            element.Owner.qname(),  # Private Events needs this but it is also OK to return empty
        )

        if self.isPseudoCalendarCollection():
            baseProperties += (
                caldavxml.SupportedCalendarComponentSet.qname(),
                caldavxml.SupportedCalendarData.qname(),
                customxml.GETCTag.qname(),
            )
            if config.MaxResourceSize:
                baseProperties += (
                    caldavxml.MaxResourceSize.qname(),
                )
            if config.MaxAllowedInstances:
                baseProperties += (
                    caldavxml.MaxInstances.qname(),
                )
            if config.MaxAttendeesPerInstance:
                baseProperties += (
                    caldavxml.MaxAttendeesPerInstance.qname(),
                )

        if self.isCalendarCollection():
            baseProperties += (
                element.ResourceID.qname(),

                # These are "live" properties in the sense of WebDAV, however "live" for twext actually means
                # ones that are also always present, but the default alarm properties are allowed to be absent
                # and are in fact stored in the property store.
                # caldavxml.DefaultAlarmVEventDateTime.qname(),
                # caldavxml.DefaultAlarmVEventDate.qname(),
                # caldavxml.DefaultAlarmVToDoDateTime.qname(),
                # caldavxml.DefaultAlarmVToDoDate.qname(),

                customxml.PubSubXMPPPushKeyProperty.qname(),

                caldavxml.ScheduleCalendarTransp.qname(),
            )

        if self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection():
            baseProperties += (
                element.ResourceID.qname(),
                carddavxml.SupportedAddressData.qname(),
                customxml.GETCTag.qname(),
                customxml.PubSubXMPPPushKeyProperty.qname(),
            )
            if config.MaxResourceSize:
                baseProperties += (
                    carddavxml.MaxResourceSize.qname(),
                )

        if self.isDirectoryBackedAddressBookCollection():
            baseProperties += (
                element.ResourceID.qname(),
                carddavxml.SupportedAddressData.qname(),
            )

        if self.isNotificationCollection():
            baseProperties += (
                customxml.GETCTag.qname(),
            )

        if hasattr(self, "scheduleTag") and self.scheduleTag:
            baseProperties += (
                caldavxml.ScheduleTag.qname(),
            )

        if config.EnableSyncReport and (element.Report(element.SyncCollection(),) in self.supportedReports()):
            baseProperties += (element.SyncToken.qname(),)

        if config.EnableAddMember and (self.isCalendarCollection() or self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection()):
            baseProperties += (element.AddMember.qname(),)

        if config.Sharing.Enabled:
            if config.Sharing.Calendars.Enabled and self.isCalendarCollection():
                baseProperties += (
                    customxml.AllowedSharingModes.qname(),
                    customxml.SharedURL.qname(),
                )
                if self.isSharedByOwner() or self.isShareeResource():
                    baseProperties += (
                        customxml.Invite.qname(),
                    )

            elif (
                config.Sharing.AddressBooks.Enabled and self.isAddressBookCollection() or
                config.Sharing.AddressBooks.Groups.Enabled and self.isGroup()
            ) and not self.isDirectoryBackedAddressBookCollection():
                baseProperties += (
                    customxml.Invite.qname(),
                    customxml.AllowedSharingModes.qname(),
                )

        return super(CalDAVResource, self).liveProperties() + baseProperties

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        # Push notification DAV property "pushkey"
        if qname == customxml.PubSubXMPPPushKeyProperty.qname() and (
            self.isCalendarCollection() or (self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection())
        ):

            if hasattr(self, "_newStoreObject"):
                notifier = self._newStoreObject.getNotifier("push")
                if notifier is not None:
                    propVal = customxml.PubSubXMPPPushKeyProperty(notifier.nodeName())
                    returnValue(propVal)

            returnValue(customxml.PubSubXMPPPushKeyProperty())

        elif qname == element.Owner.qname():
            owner = (yield self.owner(request))
            returnValue(element.Owner(owner))

        elif qname == element.ResourceType.qname():
            returnValue(self.resourceType())

        elif qname == element.ResourceID.qname():
            returnValue(element.ResourceID(element.HRef.fromString(self.resourceID())))

        elif qname == customxml.GETCTag.qname() and (
            self.isPseudoCalendarCollection() or
            self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection() or
            self.isNotificationCollection()
        ):
            returnValue(customxml.GETCTag.fromString((yield self.getInternalSyncToken())))

        elif qname == element.SyncToken.qname() and config.EnableSyncReport and (
            element.Report(element.SyncCollection(),) in self.supportedReports()
        ):
            returnValue(element.SyncToken.fromString((yield self.getSyncToken())))

        elif qname == element.AddMember.qname() and config.EnableAddMember and (
            self.isCalendarCollection() or self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection()
        ):
            url = (yield self.canonicalURL(request))
            returnValue(element.AddMember(element.HRef.fromString(url + "/;add-member")))

        elif qname == caldavxml.SupportedCalendarComponentSet.qname() and self.isPseudoCalendarCollection():
            returnValue(self.getSupportedComponentSet())

        elif qname == caldavxml.SupportedCalendarData.qname() and self.isPseudoCalendarCollection():
            dataTypes = []
            dataTypes.append(
                caldavxml.CalendarData(**{
                    "content-type": "text/calendar",
                    "version": "2.0",
                }),
            )
            if config.EnableJSONData:
                dataTypes.append(
                    caldavxml.CalendarData(**{
                        "content-type": "application/calendar+json",
                        "version": "2.0",
                    }),
                )
            returnValue(caldavxml.SupportedCalendarData(*dataTypes))

        elif qname == caldavxml.MaxResourceSize.qname() and self.isPseudoCalendarCollection():
            if config.MaxResourceSize:
                returnValue(caldavxml.MaxResourceSize.fromString(
                    str(config.MaxResourceSize)
                ))

        elif qname == caldavxml.MaxInstances.qname() and self.isPseudoCalendarCollection():
            if config.MaxAllowedInstances:
                returnValue(caldavxml.MaxInstances.fromString(
                    str(config.MaxAllowedInstances)
                ))

        elif qname == caldavxml.MaxAttendeesPerInstance.qname() and self.isPseudoCalendarCollection():
            if config.MaxAttendeesPerInstance:
                returnValue(caldavxml.MaxAttendeesPerInstance.fromString(
                    str(config.MaxAttendeesPerInstance)
                ))

        elif qname == caldavxml.ScheduleTag.qname():
            # CalDAV-scheduling
            if hasattr(self, "scheduleTag") and self.scheduleTag:
                returnValue(caldavxml.ScheduleTag.fromString(
                    self.scheduleTag
                ))

        elif qname == caldavxml.ScheduleCalendarTransp.qname() and self.isCalendarCollection():
            returnValue(caldavxml.ScheduleCalendarTransp(caldavxml.Opaque() if self._newStoreObject.isUsedForFreeBusy() else caldavxml.Transparent()))

        elif qname == carddavxml.SupportedAddressData.qname() and self.isAddressBookCollection():
            # CardDAV, section 6.2.2
            dataTypes = []
            dataTypes.append(
                carddavxml.AddressDataType(**{
                    "content-type": "text/vcard",
                    "version": "3.0",
                }),
            )
            if config.EnableJSONData:
                dataTypes.append(
                    carddavxml.AddressDataType(**{
                        "content-type": "application/vcard+json",
                        "version": "3.0",
                    }),
                )
            returnValue(carddavxml.SupportedAddressData(*dataTypes))

        elif qname == carddavxml.MaxResourceSize.qname() and self.isAddressBookCollection() and not self.isDirectoryBackedAddressBookCollection():
            # CardDAV, section 6.2.3
            if config.MaxResourceSize:
                returnValue(carddavxml.MaxResourceSize.fromString(
                    str(config.MaxResourceSize)
                ))

        elif qname == customxml.Invite.qname():
            if config.Sharing.Enabled and (
                config.Sharing.Calendars.Enabled and self.isCalendarCollection() or (
                    config.Sharing.AddressBooks.Enabled and self.isAddressBookCollection() or
                    config.Sharing.AddressBooks.Groups.Enabled and self.isGroup()
                ) and not self.isDirectoryBackedAddressBookCollection()
            ):
                result = (yield self.inviteProperty(request))
                returnValue(result)

        elif qname == customxml.AllowedSharingModes.qname():
            if config.Sharing.Enabled:
                if config.Sharing.Calendars.Enabled and self.isCalendarCollection():
                    returnValue(customxml.AllowedSharingModes(customxml.CanBeShared()))
                elif (
                    config.Sharing.AddressBooks.Enabled and self.isAddressBookCollection() or
                    config.Sharing.AddressBooks.Groups.Enabled and self.isGroup()
                ) and not self.isDirectoryBackedAddressBookCollection():
                    returnValue(customxml.AllowedSharingModes(customxml.CanBeShared()))

        elif qname == customxml.SharedURL.qname():
            if self.isShareeResource():
                returnValue(customxml.SharedURL(element.HRef.fromString(self._share_url)))
            else:
                returnValue(None)

        result = (yield super(CalDAVResource, self).readProperty(property, request))
        returnValue(result)

    @inlineCallbacks
    def writeProperty(self, property, request):
        assert isinstance(property, element.WebDAVElement), (
            "%r is not a WebDAVElement instance" % (property,)
        )

        if property.qname() == caldavxml.SupportedCalendarComponentSet.qname():
            if not self.isPseudoCalendarCollection():
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Property %s may only be set on calendar collection." % (property,)
                ))
            for component in property.children:
                if component not in self.supportedCalendarComponentSet:
                    raise HTTPError(StatusResponse(
                        responsecode.NOT_IMPLEMENTED,
                        "Component %s is not supported by this server" % (component.toxml(),)
                    ))

        # Strictly speaking CalDAV:calendar-timezone and CalDAV:calendar-timezone-id are live properties
        # in the sense that the server enforces what can be stored, however they need not actually
        # exist so we cannot list them in liveProperties on this resource, since their presence there
        # means that hasProperty will always return True for it.
        elif property.qname() in (caldavxml.CalendarTimeZone.qname(), caldavxml.CalendarTimeZoneID.qname(),):
            if not self.isCalendarCollection():
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Property %s may only be set on calendar collection." % (property,)
                ))

        # Validate default alarm properties (do this even if the default alarm feature is off)
        elif property.qname() in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            if not self.isCalendarCollection() and not isinstance(self, CalendarHomeResource):
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Property %s may only be set on calendar or home collection." % (property,)
                ))

        elif property.qname() == caldavxml.ScheduleCalendarTransp.qname():
            if not self.isCalendarCollection():
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Property %s may only be set on calendar collection." % (property,)
                ))

        result = (yield super(CalDAVResource, self).writeProperty(property, request))
        returnValue(result)

    ##
    # ACL
    ##

    def _get_accessMode(self):
        """
        Needed as a stub because only calendar object resources use this but we need to do ACL
        determination on the generic CalDAVResource for now.
        """
        return ""

    def _set_accessMode(self, value):
        raise NotImplementedError

    accessMode = property(_get_accessMode, _set_accessMode)

    # FIXME: Perhaps this is better done in authorize() instead.
    @inlineCallbacks
    def accessControlList(self, request, *args, **kwargs):

        acls = None
        if self.isShareeResource():
            acls = (yield self.shareeAccessControlList(request, *args, **kwargs))

        if acls is None:
            acls = (yield super(CalDAVResource, self).accessControlList(request, *args, **kwargs))

        # Look for private events access classification
        if self.accessMode:
            if self.accessMode in (Component.ACCESS_PRIVATE, Component.ACCESS_CONFIDENTIAL, Component.ACCESS_RESTRICTED,):
                # Need to insert ACE to prevent non-owner principals from seeing this resource
                owner = (yield self.owner(request))
                newacls = []
                if self.accessMode == Component.ACCESS_PRIVATE:
                    newacls.extend(config.AdminACEs)
                    newacls.extend(config.ReadACEs)
                    newacls.append(element.ACE(
                        element.Invert(
                            element.Principal(owner),
                        ),
                        element.Deny(
                            element.Privilege(
                                element.Read(),
                            ),
                            element.Privilege(
                                element.Write(),
                            ),
                        ),
                        element.Protected(),
                    ))
                else:
                    newacls.extend(config.AdminACEs)
                    newacls.extend(config.ReadACEs)
                    newacls.append(element.ACE(
                        element.Invert(
                            element.Principal(owner),
                        ),
                        element.Deny(
                            element.Privilege(
                                element.Write(),
                            ),
                        ),
                        element.Protected(),
                    ))
                newacls.extend(acls.children)

                acls = element.ACL(*newacls)

        returnValue(acls)

    @inlineCallbacks
    def owner(self, request):
        """
        Return the DAV:owner property value (MUST be a DAV:href or None).
        """

        if getattr(self, "_newStoreObject", None) is not None:
            if not hasattr(self._newStoreObject, "ownerHome"):
                home = self._newStoreObject.parentCollection().ownerHome()
            else:
                home = self._newStoreObject.ownerHome()
            principal = (yield self.principalForUID(home.uid()))
            returnValue(element.HRef(principal.principalURL()) if principal else None)
        else:
            parent = (yield self.locateParent(request, request.urlForResource(self)))
        if parent and isinstance(parent, CalDAVResource):
            result = (yield parent.owner(request))
            returnValue(result)
        else:
            returnValue(None)

    @inlineCallbacks
    def ownerPrincipal(self, request):
        """
        Return the principal resource for the owner of this resource.
        """
        if hasattr(self, "_newStoreObject"):
            if not hasattr(self._newStoreObject, "ownerHome"):
                home = self._newStoreObject.parentCollection().ownerHome()
            else:
                home = self._newStoreObject.ownerHome()
            returnValue((yield self.principalForUID(home.uid())))
        else:
            parent = (yield self.locateParent(request, request.urlForResource(self)))
        if parent and isinstance(parent, CalDAVResource):
            result = (yield parent.ownerPrincipal(request))
            returnValue(result)
        else:
            returnValue(None)

    @inlineCallbacks
    def resourceOwnerPrincipal(self, request):
        """
        This is the principal resource of the owner of the resource based on the URI used to access it. For a shared
        collection it will be the sharee, otherwise it will be the regular the ownerPrincipal.
        """
        parent = (yield self.locateParent(
            request, request.urlForResource(self)
        ))
        if parent and isinstance(parent, CalDAVResource):
            result = (yield parent.resourceOwnerPrincipal(request))
            returnValue(result)
        else:
            returnValue(None)

    @inlineCallbacks
    def isOwner(self, request):
        """
        Determine whether the DAV:owner of this resource matches the currently
        authorized principal in the request, or if the user is a read-only or
        read-write administrator.
        """
        current = self.currentPrincipal(request)
        if current in config.AllAdminPrincipalObjects:
            returnValue(True)
        if element.Principal((yield self.owner(request))) == current:
            returnValue(True)
        returnValue(False)

    ##
    # DAVResource
    ##

    def displayName(self):
        if 'record' in dir(self):
            if self.record.fullNames:
                return self.record.fullNames[0]
            elif self.record.shortNames:
                return self.record.shortNames[0]

        return super(DAVResource, self).displayName()

    def name(self):
        return None

    def resourceID(self):
        return None

    ##
    # CalDAV
    ##

    def isCalendarCollection(self):
        """
        See L{ICalDAVResource.isCalendarCollection}.
        """
        return self.isSpecialCollection(caldavxml.Calendar)

    def isAddressBookCollection(self):
        """
        See L{ICalDAVResource.isAddressBookCollection}.
        """
        return self.isSpecialCollection(carddavxml.AddressBook)

    def isNotificationCollection(self):
        """
        See L{ICalDAVResource.isNotificationCollection}.
        """
        return self.isSpecialCollection(customxml.Notification)

    def isDirectoryBackedAddressBookCollection(self):       # ATM - temporary fix? (this one worked)
        return False

    def isSpecialCollection(self, collectiontype):
        """
        See L{ICalDAVResource.isSpecialCollection}.
        """
        if not self.isCollection():
            return False

        try:
            resourcetype = self.resourceType()
        except HTTPError as e:
            assert e.response.code == responsecode.NOT_FOUND, (
                "Unexpected response code: %s" % (e.response.code,)
            )
            return False
        return bool(resourcetype.childrenOfType(collectiontype))

    def isPseudoCalendarCollection(self):
        """
        See L{ICalDAVResource.isPseudoCalendarCollection}.
        """
        return self.isCalendarCollection()

    def findCalendarCollections(self, depth, request, callback, privileges=None):
        return self.findSpecialCollections(caldavxml.Calendar, depth, request, callback, privileges)

    def findAddressBookCollections(self, depth, request, callback, privileges=None):
        return self.findSpecialCollections(carddavxml.AddressBook, depth, request, callback, privileges)

    @inlineCallbacks
    def findSpecialCollectionsFaster(self, type, depth, request, callback, privileges=None):
        assert depth in ("0", "1", "infinity"), "Invalid depth: %s" % (depth,)

        if depth != "0" and self.isCollection():
            basepath = request.urlForResource(self)
            for childname in (yield self.listChildren()):
                childpath = joinURL(basepath, childname)
                child = (yield request.locateResource(childpath))
                if child:
                    if privileges:
                        try:
                            yield child.checkPrivileges(request, privileges)
                        except AccessDeniedError:
                            continue
                    if child.isSpecialCollection(type):
                        callback(child, childpath)

                    # No more regular collections. If we leave this in then dropbox is scanned at depth:infinity
                    # and that is very painful as it requires scanning all calendar resources too. Eventually we need
                    # to fix drop box and probably re-enable this for the generic case.
    #                elif child.isCollection():
    #                    if depth == "infinity":
    #                        yield child.findSpecialCollectionsFaster(type, depth, request, callback, privileges)

    findSpecialCollections = findSpecialCollectionsFaster

    def isDefaultCalendar(self, request):

        assert self.isCalendarCollection()

        return self._newStoreParentHome.isDefaultCalendar(self._newStoreObject)

    def iCalendarAddressDoNormalization(self, ical):
        """
        Normalize calendar user addresses in the supplied iCalendar object into their
        urn:uuid form where possible. Also reset CN= property and add EMAIL property.

        @param ical: calendar object to normalize.
        @type ical: L{Component}
        @return: L{Deferred}
        """
        return ical.normalizeCalendarUserAddresses(
            normalizationLookup,
            self.record.service.recordWithCalendarUserAddress
        )

    @inlineCallbacks
    def principalForCalendarUserAddress(self, address):
        for principalCollection in self.principalCollections():
            principal = (yield principalCollection.principalForCalendarUserAddress(address))
            if principal is not None:
                returnValue(principal)
        returnValue(None)

    @inlineCallbacks
    def principalForUID(self, principalUID):
        for principalCollection in self.principalCollections():
            principal = (yield principalCollection.principalForUID(principalUID))
            if principal is not None:
                returnValue(principal)
        returnValue(None)

    @inlineCallbacks
    def movedAddressBook(self, request, defaultAddressBook, destination, destination_uri):
        """
        AddressBook has been moved. Need to do some extra clean-up.
        """

        # Adjust the default addressbook setting if necessary
        if defaultAddressBook:
            principal = (yield self.resourceOwnerPrincipal(request))
            home = (yield principal.addressBookHome(request))
            (_ignore_scheme, _ignore_host, destination_path, _ignore_query, _ignore_fragment) = urlsplit(normalizeURL(destination_uri))
            yield home.writeProperty(carddavxml.DefaultAddressBookURL(element.HRef(destination_path)), request)

    @inlineCallbacks
    def isDefaultAddressBook(self, request):

        assert self.isAddressBookCollection()

        # Not allowed to delete the default address book
        principal = (yield self.resourceOwnerPrincipal(request))
        home = (yield principal.addressBookHome(request))
        default = (yield home.readProperty(carddavxml.DefaultAddressBookURL.qname(), request))
        if default and len(default.children) == 1:
            defaultURL = normalizeURL(str(default.children[0]))
            myURL = (yield self.canonicalURL(request))
            returnValue(defaultURL == myURL)

        returnValue(False)

    @inlineCallbacks
    def vCard(self):
        """
        See L{ICalDAVResource.vCard}.

        This implementation returns the an object created from the data returned
        by L{vCardText} when given the same arguments.

        Note that L{vCardText} by default calls this method, which creates
        an infinite loop.  A subclass must override one of both of these
        methods.
        """
        try:
            vcard_data = yield self.vCardText()
        except InternalDataStoreError:
            returnValue(None)

        if vcard_data is None:
            returnValue(None)

        try:
            returnValue(vComponent.fromString(vcard_data))
        except ValueError:
            returnValue(None)

    def supportedReports(self):
        result = super(CalDAVResource, self).supportedReports()
        if config.EnableCalDAV:
            result.append(element.Report(caldavxml.CalendarQuery(),))
            result.append(element.Report(caldavxml.CalendarMultiGet(),))
        if self.isCollection():
            # Only allowed on collections
            result.append(element.Report(caldavxml.FreeBusyQuery(),))
        if config.EnableCardDAV:
            result.append(element.Report(carddavxml.AddressBookQuery(),))
            result.append(element.Report(carddavxml.AddressBookMultiGet(),))
        if (
            self.isPseudoCalendarCollection() or
            self.isAddressBookCollection() or
            self.isNotificationCollection()
        ) and config.EnableSyncReport:
            # Only allowed on calendar/inbox/addressbook/notification collections
            result.append(element.Report(element.SyncCollection(),))
        return result

    def writeNewACEs(self, newaces):
        """
        Write a new ACL to the resource's property store. We override this for calendar collections
        and force all the ACEs to be inheritable so that all calendar object resources within the
        calendar collection have the same privileges unless explicitly overridden. The same applies
        to drop box collections as we want all resources (attachments) to have the same privileges as
        the drop box collection.

        @param newaces: C{list} of L{ACE} for ACL being set.
        """

        # Do this only for regular calendar collections and Inbox/Outbox
        if self.isPseudoCalendarCollection() or self.isAddressBookCollection():
            edited_aces = []
            for ace in newaces:
                if TwistedACLInheritable() not in ace.children:
                    children = list(ace.children)
                    children.append(TwistedACLInheritable())
                    edited_aces.append(element.ACE(*children))
                else:
                    edited_aces.append(ace)
        else:
            edited_aces = newaces

        # Do inherited with possibly modified set of aces
        super(CalDAVResource, self).writeNewACEs(edited_aces)

    ##
    # Utilities
    ##

    def locateParent(self, request, uri):
        """
        Locates the parent resource of the resource with the given URI.
        @param request: an L{IRequest} object for the request being processed.
        @param uri: the URI whose parent resource is desired.
        """
        return request.locateResource(parentForURL(uri))

    @inlineCallbacks
    def canonicalURL(self, request):

        if not hasattr(self, "_canonical_url"):

            myurl = request.urlForResource(self)
            _ignore_scheme, _ignore_host, path, _ignore_query, _ignore_fragment = urlsplit(normalizeURL(myurl))
            lastpath = path.split("/")[-1]

            parent = (yield request.locateResource(parentForURL(myurl)))
            if parent and isinstance(parent, CalDAVResource):
                canonical_parent = (yield parent.canonicalURL(request))
                self._canonical_url = joinURL(canonical_parent, lastpath)
            else:
                self._canonical_url = myurl

        returnValue(self._canonical_url)

    ##
    # Quota
    ##

    def hasQuotaRoot(self, request):
        """
        Quota root only ever set on calendar homes.
        """
        return False

    def quotaRoot(self, request):
        """
        Quota root only ever set on calendar homes.
        """
        return None

    @inlineCallbacks
    def quotaRootResource(self, request):
        """
        Return the quota root for this resource.

        @return: L{DAVResource} or C{None}
        """

        sharedParent = None
        if self.isShareeResource():
            # A sharee collection's quota root is the resource owner's root
            if self._share_url:
                sharedParent = (yield request.locateResource(parentForURL(self._share_url)))
        else:
            parent = (yield self.locateParent(request, request.urlForResource(self)))
            if isCalendarCollectionResource(parent) or isAddressBookCollectionResource(parent):
                if parent.isShareeResource():
                    # A sharee collection's quota root is the resource owner's root
                    sharedParent = (yield request.locateResource(parentForURL(parent._share_url)))

        if sharedParent:
            result = (yield sharedParent.quotaRootResource(request))
        else:
            result = (yield super(CalDAVResource, self).quotaRootResource(request))

        returnValue(result)

    # Collection sync stuff

    @inlineCallbacks
    def whatchanged(self, client_token, depth):

        client_data_token = None
        client_config_token = None

        if client_token:
            if "/" in client_token:
                client_data_token, client_config_token = client_token.split("/")
            else:
                client_data_token = client_token

        current_token = (yield self.getSyncToken())
        if "/" in current_token:
            current_token = current_token.split("/")[0]
        current_uuid, current_revision = current_token[6:].split("_", 1)
        current_revision = int(current_revision)

        if client_data_token:
            try:
                if not client_data_token.startswith("data:,"):
                    raise ValueError
                caluuid, revision = client_data_token[6:].split("_", 1)
                revision = int(revision)

                # Check client token validity
                if caluuid != current_uuid:
                    raise ValueError
                if revision > current_revision:
                    raise ValueError
            except ValueError:
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (dav_namespace, "valid-sync-token"),
                    "Sync token is invalid",
                ))
        else:
            revision = 0

        try:
            changed, removed, notallowed = yield self._indexWhatChanged(revision, depth)
        except SyncTokenValidException:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (dav_namespace, "valid-sync-token"),
                "Sync token not recognized",
            ))

        if config.EnableConfigSyncToken:
            # Append the app-level portion of sync token (e.g. derived from config)
            newConfigToken = config.syncToken()
            current_token = "{}/{}".format(current_token, newConfigToken)

            # If the config token changed, note that in the returned tuple
            resourceChanged = (newConfigToken != client_config_token)
        else:
            resourceChanged = False

        returnValue((changed, removed, notallowed, current_token, resourceChanged))

    def _indexWhatChanged(self, revision, depth):
        # Now handled directly by newstore
        raise NotImplementedError

    @inlineCallbacks
    def getSyncToken(self):
        """
        Return current sync-token value.
        """

        internal_token = (yield self.getInternalSyncToken())
        internal_token = "data:,%s" % (internal_token,)
        token = config.joinToken(internal_token)
        returnValue(token)

    def getInternalSyncToken(self):
        """
        Return current internal sync-token value.
        """
        raise HTTPError(StatusResponse(responsecode.NOT_FOUND, "Property not supported"))

    #
    # Stuff from CalDAVFile
    #

    @inlineCallbacks
    def checkPreconditions(self, request):
        """
        We override the base class to handle the special implicit scheduling weak ETag behavior
        for compatibility with old clients using If-Match.
        """

        if config.Scheduling.CalDAV.ScheduleTagCompatibility:

            if self.exists() and hasattr(self, "scheduleEtags"):
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

        result = (yield super(CalDAVResource, self).checkPreconditions(request))
        returnValue(result)

    @inlineCallbacks
    def createCalendar(self, request):
        """
        External API for creating a calendar.  Verify that the parent is a
        collection, exists, is I{not} a calendar collection; that this resource
        does not yet exist, then create it.

        @param request: the request used to look up parent resources to
            validate.

        @type request: L{txweb2.iweb.IRequest}

        @return: a deferred that fires when a calendar collection has been
            created in this resource.
        """
        if self.exists():
            self.log.error("Attempt to create collection where file exists: {s!r}", s=self)
            raise HTTPError(StatusResponse(responsecode.NOT_ALLOWED, "File exists"))

        # newStore guarantees that we always have a parent calendar home
        # if not self.fp.parent().isdir():
        #     log.error("Attempt to create collection with no parent: {p}", p=self.fp.path)
        #     raise HTTPError(StatusResponse(responsecode.CONFLICT, "No parent collection"))

        #
        # Verify that no parent collection is a calendar also
        #

        parent = (yield self._checkParents(request, isPseudoCalendarCollectionResource))

        if parent is not None:
            self.log.error("Cannot create a calendar collection within a calendar collection {p!r}", p=parent)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (caldavxml.caldav_namespace, "calendar-collection-location-ok"),
                "Cannot create a calendar collection inside another calendar collection",
            ))

        # Check for any quota limits
        if config.MaxCollectionsPerHome:
            parent = (yield self.locateParent(request, request.urlForResource(self)))
            if (yield parent.countOwnedChildren()) >= config.MaxCollectionsPerHome:  # NB this ignores shares
                self.log.error("Cannot create a calendar collection because there are too many already present in {p!r}", p=parent)
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    customxml.MaxCollections(),
                    "Too many calendar collections",
                ))

        returnValue((yield self.createCalendarCollection()))

    def createCalendarCollection(self):
        """
        Internal API for creating a calendar collection.

        @return: a L{Deferred} which fires when the underlying collection has
            actually been created.
        """
        return fail(NotImplementedError())

    def iCalendarRolledup(self, request):
        """
        Only implemented by calendar collections; see storebridge.
        """
        raise HTTPError(responsecode.NOT_ALLOWED)

    @inlineCallbacks
    def iCalendarFiltered(self, isowner):

        # Now "filter" the resource calendar data
        caldata = (yield self.componentForUser())
        caldata = HiddenInstanceFilter().filter(caldata)
        caldata = PrivateEventFilter(self.accessMode, isowner).filter(caldata)
        returnValue(caldata)

    def component(self):
        # storebridge handles this method
        raise NotImplementedError()

    @inlineCallbacks
    def createAddressBook(self, request):
        """
        External API for creating an addressbook.  Verify that the parent is a
        collection, exists, is I{not} an addressbook collection; that this resource
        does not yet exist, then create it.

        @param request: the request used to look up parent resources to
            validate.

        @type request: L{txweb2.iweb.IRequest}

        @return: a deferred that fires when an addressbook collection has been
            created in this resource.
        """
        #
        # request object is required because we need to validate against parent
        # resources, and we need the request in order to locate the parents.
        #

        if self.exists():
            self.log.error("Attempt to create collection where file exists: {s!r}", s=self)
            raise HTTPError(StatusResponse(responsecode.NOT_ALLOWED, "File exists"))

        # newStore guarantees that we always have a parent calendar home
        # if not os.path.isdir(os.path.dirname(self.fp.path)):
        #     log.error("Attempt to create collection with no parent: {p}", p=self.fp.path)
        #     raise HTTPError(StatusResponse(responsecode.CONFLICT, "No parent collection"))

        #
        # Verify that no parent collection is a calendar also
        #

        parent = (yield self._checkParents(request, isAddressBookCollectionResource))
        if parent is not None:
            self.log.error("Cannot create an address book collection within an address book collection {p!r}", p=parent)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (carddavxml.carddav_namespace, "addressbook-collection-location-ok"),
                "Cannot create an address book collection inside of an address book collection",
            ))

        # Check for any quota limits
        if config.MaxCollectionsPerHome:
            parent = (yield self.locateParent(request, request.urlForResource(self)))
            if (yield parent.countOwnedChildren()) >= config.MaxCollectionsPerHome:  # NB this ignores shares
                self.log.error("Cannot create a calendar collection because there are too many already present in {p!r}", p=parent)
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    customxml.MaxCollections(),
                    "Too many address book collections",
                ))

        returnValue((yield self.createAddressBookCollection()))

    def createAddressBookCollection(self):
        """
        Internal API for creating an addressbook collection.

        @return: a L{Deferred} which fires when the underlying collection has
            actually been created.
        """
        return fail(NotImplementedError())

    @inlineCallbacks
    def vCardRolledup(self, request):
        # TODO: just catenate all the vCards together
        yield fail(HTTPError((ErrorResponse(responsecode.BAD_REQUEST))))

    @inlineCallbacks
    def vCardText(self, name=None):
        if self.isAddressBookCollection():
            if name is None:
                returnValue(str((yield self.vCard())))
            vcard_resource = yield self.getChild(name)
            returnValue((yield vcard_resource.vCardText()))
        elif self.isCollection():
            returnValue(None)
        else:
            if name is not None:
                raise AssertionError("name must be None for non-collection vcard resource")
        # FIXME: StoreBridge handles this case
        raise NotImplementedError

    def supportedPrivileges(self, request):
        # read-free-busy support on calendar collection and calendar object resources
        if self.isCollection():
            return succeed(calendarPrivilegeSet)
        else:
            def gotParent(parent):
                if parent and isCalendarCollectionResource(parent):
                    return succeed(calendarPrivilegeSet)
                else:
                    return super(CalDAVResource, self).supportedPrivileges(request)

            d = self.locateParent(request, request.urlForResource(self))
            d.addCallback(gotParent)
            return d

        return super(CalDAVResource, self).supportedPrivileges(request)

    ##
    # Quota
    ##

    def quotaSize(self, request):
        """
        Get the size of this resource.
        TODO: Take into account size of dead-properties. Does stat include xattrs size?

        @return: an L{Deferred} with a C{int} result containing the size of the resource.
        """
#        if self.isCollection():
#            @inlineCallbacks
#            def walktree(top):
#                """
#                Recursively descend the directory tree rooted at top,
#                calling the callback function for each regular file
#
#                @param top: L{FilePath} for the directory to walk.
#                """
#
#                total = 0
#                for f in top.listdir():
#
#                    # Ignore the database
#                    if f.startswith("."):
#                        continue
#
#                    child = top.child(f)
#                    if child.isdir():
#                        # It's a directory, recurse into it
#                        total += yield walktree(child)
#                    elif child.isfile():
#                        # It's a file, call the callback function
#                        total += child.getsize()
#                    else:
#                        # Unknown file type, print a message
#                        pass
#
#                returnValue(total)
#
#            return walktree(self.fp)
#        else:
#            return succeed(self.fp.getsize())
        return succeed(0)

    ##
    # Utilities
    ##

    @staticmethod
    def _isChildURI(request, uri, immediateChild=True):
        """
        Verify that the supplied URI represents a resource that is a child
        of the request resource.
        @param request: the request currently in progress
        @param uri: the URI to test
        @return: True if the supplied URI is a child resource
                 False if not
        """
        if uri is None:
            return False

        #
        # Parse the URI
        #

        (scheme, host, path, query, fragment) = urlsplit(uri)  # @UnusedVariable

        # Request hostname and child uri hostname have to be the same.
        if host and host != request.headers.getHeader("host"):
            return False

        # Child URI must start with request uri text.
        parent = request.uri
        if not parent.endswith("/"):
            parent += "/"

        return path.startswith(parent) and (len(path) > len(parent)) and (not immediateChild or (path.find("/", len(parent)) == -1))

    @inlineCallbacks
    def _checkParents(self, request, test):
        """
        @param request: the request being processed.
        @param test: a callable
        @return: the closest parent for this resource using the request URI from
            the given request for which C{test(parent)} evaluates to a true
            value, or C{None} if no parent matches.
        """
        parent = self
        parent_uri = request.uri

        while True:
            parent_uri = parentForURL(parent_uri)
            if not parent_uri:
                break

            parent = yield request.locateResource(parent_uri)

            if test(parent):
                returnValue(parent)


class CalendarPrincipalCollectionResource (DAVPrincipalCollectionResource, CalDAVResource):
    """
    CalDAV principal collection.
    """
    implements(IDAVPrincipalCollectionResource)

    def isCollection(self):
        return True

    def isCalendarCollection(self):
        return False

    def isAddressBookCollection(self):
        return False

    def isDirectoryBackedAddressBookCollection(self):
        return False

    def principalForCalendarUserAddress(self, address):
        return None

    def supportedReports(self):
        """
        Principal collections are the only resources supporting the
        principal-search-property-set report.
        """
        result = super(CalendarPrincipalCollectionResource, self).supportedReports()
        result.append(element.Report(element.PrincipalSearchPropertySet(),))
        return result

    def principalSearchPropertySet(self):
        return element.PrincipalSearchPropertySet(
            element.PrincipalSearchProperty(
                element.PropertyContainer(
                    element.DisplayName()
                ),
                element.Description(
                    element.PCDATAElement("Display Name"),
                    **{"xml:lang": "en"}
                ),
            ),
            element.PrincipalSearchProperty(
                element.PropertyContainer(
                    caldavxml.CalendarUserAddressSet()
                ),
                element.Description(
                    element.PCDATAElement("Calendar User Addresses"),
                    **{"xml:lang": "en"}
                ),
            ),
        )


class CalendarPrincipalResource (CalDAVComplianceMixIn, DAVResourceWithChildrenMixin, DAVPrincipalResource):
    """
    CalDAV principal resource.

    Extends L{DAVPrincipalResource} to provide CalDAV functionality.
    """
    implements(ICalendarPrincipalResource)

    def liveProperties(self):

        baseProperties = ()

        if self.calendarsEnabled():
            baseProperties += (
                (caldav_namespace, "calendar-home-set"),
                (caldav_namespace, "calendar-user-address-set"),
                (caldav_namespace, "schedule-inbox-URL"),
                (caldav_namespace, "schedule-outbox-URL"),
                (caldav_namespace, "calendar-user-type"),
                (calendarserver_namespace, "calendar-proxy-read-for"),
                (calendarserver_namespace, "calendar-proxy-write-for"),
                (calendarserver_namespace, "auto-schedule-mode"),
            )

        if self.addressBooksEnabled():
            baseProperties += (carddavxml.AddressBookHomeSet.qname(),)
            if self.directoryAddressBookEnabled():
                baseProperties += (carddavxml.DirectoryGateway.qname(),)

        if config.EnableDropBox or config.EnableManagedAttachments:
            baseProperties += (customxml.DropBoxHomeURL.qname(),)

        if config.Sharing.Enabled:
            baseProperties += (customxml.NotificationURL.qname(),)

        return super(CalendarPrincipalResource, self).liveProperties() + baseProperties

    def isCollection(self):
        return True

    @inlineCallbacks
    def handleMissingTrailingSlash(self, request):
        try:
            _ignore_authnUser, authzUser = yield self.authenticate(request)
        except Exception:
            authzUser = None

        # Turn 301 into 401
        if authzUser is None:
            response = (yield UnauthorizedResponse.makeResponse(
                request.credentialFactories,
                request.remoteAddr
            ))
            returnValue(response)
        else:
            response = RedirectResponse(
                request.unparseURL(
                    path=urllib.quote(
                        urllib.unquote(request.path),
                        safe=':/') + '/'
                )
            )
            returnValue(response)

    def calendarsEnabled(self):
        return config.EnableCalDAV

    def addressBooksEnabled(self):
        return config.EnableCardDAV

    def directoryAddressBookEnabled(self):
        return config.DirectoryAddressBook.Enabled and config.EnableSearchAddressBook

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        namespace, name = qname

        if namespace == caldav_namespace and self.calendarsEnabled():
            if name == "calendar-home-set":
                returnValue(caldavxml.CalendarHomeSet(
                    *[element.HRef(url) for url in self.calendarHomeURLs()]
                ))

            elif name == "calendar-user-address-set":
                returnValue(caldavxml.CalendarUserAddressSet(
                    *[element.HRef(uri) for uri in sorted(self.calendarUserAddresses())]
                ))

            elif name == "schedule-inbox-URL":
                url = self.scheduleInboxURL()
                if url is None:
                    returnValue(None)
                else:
                    returnValue(caldavxml.ScheduleInboxURL(element.HRef(url)))

            elif name == "schedule-outbox-URL":
                url = self.scheduleOutboxURL()
                if url is None:
                    returnValue(None)
                else:
                    returnValue(caldavxml.ScheduleOutboxURL(element.HRef(url)))

            elif name == "calendar-user-type":
                returnValue(caldavxml.CalendarUserType(self.record.getCUType()))

        elif namespace == calendarserver_namespace:
            if name == "dropbox-home-URL" and (config.EnableDropBox or config.EnableManagedAttachments):
                url = self.dropboxURL()
                if url is None:
                    returnValue(None)
                else:
                    returnValue(customxml.DropBoxHomeURL(element.HRef(url)))

            elif name == "notification-URL" and config.Sharing.Enabled:
                url = yield self.notificationURL()
                if url is None:
                    returnValue(None)
                else:
                    returnValue(customxml.NotificationURL(element.HRef(url)))

            elif name == "calendar-proxy-read-for" and self.calendarsEnabled():
                results = (yield self.proxyFor(False))
                returnValue(customxml.CalendarProxyReadFor(
                    *[element.HRef(principal.principalURL()) for principal in results]
                ))

            elif name == "calendar-proxy-write-for" and self.calendarsEnabled():
                results = (yield self.proxyFor(True))
                returnValue(customxml.CalendarProxyWriteFor(
                    *[element.HRef(principal.principalURL()) for principal in results]
                ))

            elif name == "auto-schedule-mode" and self.calendarsEnabled():
                autoScheduleMode = yield self.getAutoScheduleMode()
                returnValue(customxml.AutoScheduleMode(autoScheduleMode.description if autoScheduleMode else "default"))

        elif namespace == carddav_namespace and self.addressBooksEnabled():
            if name == "addressbook-home-set":
                returnValue(carddavxml.AddressBookHomeSet(
                    *[element.HRef(abhome_url) for abhome_url in self.addressBookHomeURLs()]
                ))
            elif name == "directory-gateway" and self.directoryAddressBookEnabled():
                returnValue(carddavxml.DirectoryGateway(
                    element.HRef.fromString(joinURL("/", config.DirectoryAddressBook.name, "/"))
                ))

        result = (yield super(CalendarPrincipalResource, self).readProperty(property, request))
        returnValue(result)

    def calendarFreeBusyURIs(self, request):
        def gotInbox(inbox):
            if inbox is None:
                return ()

            def getFreeBusy(has):
                if not has:
                    return ()

                def parseFreeBusy(freeBusySet):
                    return tuple(str(href) for href in freeBusySet.children)

                d = inbox.readProperty((caldav_namespace, "calendar-free-busy-set"), request)
                d.addCallback(parseFreeBusy)
                return d

            d = inbox.hasProperty((caldav_namespace, "calendar-free-busy-set"), request)
            d.addCallback(getFreeBusy)
            return d

        d = self.scheduleInbox(request)
        d.addCallback(gotInbox)
        return d

    def scheduleInbox(self, request):
        """
        @return: the deferred schedule inbox for this principal.
        """
        return request.locateResource(self.scheduleInboxURL())

    ##
    # Quota
    ##

    def hasQuotaRoot(self, request):
        """
        Quota root only ever set on calendar homes.
        """
        return False

    def quotaRoot(self, request):
        """
        Quota root only ever set on calendar homes.
        """
        return None


class DefaultAlarmPropertyMixin(object):
    """
    A mixin for use with calendar home and calendars to allow direct access to
    the default alarm properties in a more useful way that using readProperty.
    In particular it will handle inheritance of the property from the home if a
    calendar does not explicitly have the property.

    Important: we need to distinguish between the property not being present, or
    present but empty, however the store by default is unable to distinguish between
    None and and empty C{str}. So what we do is use the value "empty" to represent
    a present but empty property.
    """

    ALARM_PROPERTIES = {
        caldavxml.DefaultAlarmVEventDateTime.qname(): (True, True,),
        caldavxml.DefaultAlarmVEventDate.qname(): (True, False,),
        caldavxml.DefaultAlarmVToDoDateTime.qname(): (False, True,),
        caldavxml.DefaultAlarmVToDoDate.qname(): (False, False,),
    }

    ALARM_PROPERTY_CLASSES = {
        caldavxml.DefaultAlarmVEventDateTime.qname(): caldavxml.DefaultAlarmVEventDateTime,
        caldavxml.DefaultAlarmVEventDate.qname(): caldavxml.DefaultAlarmVEventDate,
        caldavxml.DefaultAlarmVToDoDateTime.qname(): caldavxml.DefaultAlarmVToDoDateTime,
        caldavxml.DefaultAlarmVToDoDate.qname(): caldavxml.DefaultAlarmVToDoDate,
    }

    def getDefaultAlarmProperty(self, propname):

        vevent, timed = DefaultAlarmPropertyMixin.ALARM_PROPERTIES[propname]

        if self.isCalendarCollection():

            # Get from calendar or inherit from home
            alarm = self._newStoreObject.getDefaultAlarm(vevent, timed)
            if alarm is None:
                return self.parentResource().getDefaultAlarmProperty(propname)
            elif alarm == "empty":
                return DefaultAlarmPropertyMixin.ALARM_PROPERTY_CLASSES[propname]()
        else:
            # Just return whatever is on the home
            alarm = self._newStoreHome.getDefaultAlarm(vevent, timed)

        return DefaultAlarmPropertyMixin.ALARM_PROPERTY_CLASSES[propname](alarm) if alarm else None

    @inlineCallbacks
    def setDefaultAlarmProperty(self, prop):

        vevent, timed = DefaultAlarmPropertyMixin.ALARM_PROPERTIES[prop.qname()]
        alarm = str(prop)

        if self.isCalendarCollection():
            yield self._newStoreObject.setDefaultAlarm(alarm if alarm else "empty", vevent, timed)
        else:
            yield self._newStoreHome.setDefaultAlarm(alarm if alarm else "empty", vevent, timed)

    @inlineCallbacks
    def removeDefaultAlarmProperty(self, propname):

        vevent, timed = DefaultAlarmPropertyMixin.ALARM_PROPERTIES[propname]

        if self.isCalendarCollection():
            yield self._newStoreObject.setDefaultAlarm(None, vevent, timed)
        else:
            yield self._newStoreHome.setDefaultAlarm(None, vevent, timed)


class CommonHomeResource(PropfindCacheMixin, SharedHomeMixin, CalDAVResource):
    """
    Logic common to Calendar and Addressbook home resources.

    @ivar _provisionedChildren: A map of resource names to built-in children
        with protocol-level meanings, like C{"attachments"}, C{"inbox"},
        C{"outbox"}, and so on.
    @type _provisionedChildren: L{dict} mapping L{bytes} to L{Resource}

    @ivar _provisionedLinks: A map of resource names to built-in links that the
        server has inserted into this L{CommonHomeResource}.
    @type _provisionedLinks: L{dict} mapping L{bytes} to L{Resource}
    """

    def __init__(self, parent, name, transaction, home):
        self.parent = parent
        self.name = name
        self.associateWithTransaction(transaction)
        self._provisionedChildren = {}
        self._provisionedLinks = {}
        self._setupProvisions()
        self._newStoreHome = home
        CalDAVResource.__init__(self)

        from twistedcaldav.storebridge import _NewStorePropertiesWrapper
        self._dead_properties = _NewStorePropertiesWrapper(
            self._newStoreHome.properties()
        )

    @classmethod
    @inlineCallbacks
    def createHomeResource(cls, parent, name, transaction):
        home, _ignored_created = yield cls.homeFromTransaction(
            transaction, name)
        resource = cls(parent, name, transaction, home)
        returnValue(resource)

    @classmethod
    def homeFromTransaction(cls, transaction, uid):
        """
        Create or retrieve an appropriate back-end-home object from a
        transaction and a home UID.

        @return: a L{Deferred} which fires a 2-tuple of C{(created, home)}
            where C{created} is a boolean indicating whether this call created
            the home in the back-end, and C{home} is the home object itself.
        """
        raise NotImplementedError("Subclasses must implement.")

    def _setupProvisions(self):
        pass

    def liveProperties(self):

        props = super(CommonHomeResource, self).liveProperties() + (
            (customxml.calendarserver_namespace, "push-transports"),
            (customxml.calendarserver_namespace, "pushkey"),
        )

        if config.MaxCollectionsPerHome:
            props += (customxml.MaxCollections.qname(),)

        return props

    def url(self):
        return joinURL(self.parent.url(), self.name, "/")

    def canonicalURL(self, request):
        return succeed(self.url())

    def exists(self):
        # FIXME: tests
        return True

    def isCollection(self):
        return True

    def quotaSize(self, request):
        # FIXME: tests, workingness
        return succeed(0)

    def hasQuotaRoot(self, request):
        """
        Is this resource a quota root?  This returns True if the backend is
        enforcing quota.

        @return: a C{True} if this resource has quota root, C{False} otherwise.
        """
        return self._newStoreHome.quotaAllowedBytes() is not None

    def quotaRoot(self, request):
        """
        Retrieve the number of total allowed bytes from the backend.

        @return: a C{int} containing the maximum allowed bytes if this
            collection is quota-controlled, or C{None} if not quota controlled.
        """
        return self._newStoreHome.quotaAllowedBytes()

    def currentQuotaUse(self, request):
        """
        Get the quota use value
        """
        return maybeDeferred(self._newStoreHome.quotaUsedBytes)

    def supportedReports(self):
        result = super(CommonHomeResource, self).supportedReports()
        if config.EnableSyncReport and config.EnableSyncReportHome:
            # Allowed on any home
            result.append(element.Report(element.SyncCollection(),))
        return result

    def _mergeSyncTokens(self, hometoken, notificationtoken):
        """
        Merge two sync tokens, choosing the higher revision number of the two,
        but keeping the home resource-id intact.  If the config portion of
        the token is present, it is also kept intact.
        """
        if "/" in hometoken:
            hometoken, configtoken = hometoken.split("/")
        else:
            configtoken = None

        if "/" in notificationtoken:
            notificationtoken = notificationtoken.split("/")[0]

        homekey, homerev = hometoken.split("_", 1)
        notrev = notificationtoken.split("_", 1)[1]
        if int(notrev) > int(homerev):
            hometoken = "%s_%s" % (homekey, notrev,)

        if configtoken:
            hometoken = "{}/{}".format(hometoken, configtoken)

        return hometoken

    def canShare(self):
        raise NotImplementedError

    @inlineCallbacks
    def findChildrenFaster(
        self, depth, request, okcallback, badcallback, missingcallback, unavailablecallback,
        names, privileges, inherited_aces
    ):
        """
        Override to pre-load children in certain collection types for better performance.
        """

        if depth == "1":
            yield self._newStoreHome.loadChildren()

        result = (yield super(CommonHomeResource, self).findChildrenFaster(
            depth, request, okcallback, badcallback, missingcallback, unavailablecallback, names, privileges, inherited_aces
        ))

        returnValue(result)

    @inlineCallbacks
    def makeChild(self, name):
        # Try built-in children first
        if name in self._provisionedChildren:
            cls = self._provisionedChildren[name]
            from twistedcaldav.notifications import NotificationCollectionResource
            if cls is NotificationCollectionResource:
                returnValue((yield self.createNotificationsCollection()))
            child = yield self._provisionedChildren[name](self)
            self.propagateTransaction(child)
            self.putChild(name, child)
            returnValue(child)

        # Try built-in links next
        if name in self._provisionedLinks:
            child = LinkResource(self, self._provisionedLinks[name])
            self.putChild(name, child)
            returnValue(child)

        # get regular or shared child
        child = yield self.makeRegularChild(name)

        # add _share attribute if child is shared; verify that child should
        # still be accessible and convert it to None if it's not.
        child = yield self.provisionShare(child)

        returnValue(child)

    @inlineCallbacks
    def createNotificationsCollection(self):
        txn = self._associatedTransaction
        notifications = yield txn.notificationsWithUID(self._newStoreHome.uid(), create=True)

        from twistedcaldav.storebridge import StoreNotificationCollectionResource
        similar = StoreNotificationCollectionResource(
            notifications,
            self,
            self._newStoreHome,
            principalCollections=self.principalCollections(),
        )
        self.propagateTransaction(similar)
        returnValue(similar)

    def makeRegularChild(self, name):
        raise NotImplementedError

    @inlineCallbacks
    def listChildren(self):
        """
        @return: a sequence of the names of all known children of this resource.
        """
        children = set(self._provisionedChildren.keys())
        children.update(self._provisionedLinks.keys())
        children.update((yield self._newStoreHome.listChildren()))
        returnValue(children)

    @inlineCallbacks
    def countOwnedChildren(self):
        """
        @return: the number of children (not shared ones).
        """
        returnValue(len(list((yield self._newStoreHome.listChildren()))))

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        if qname == customxml.MaxCollections.qname() and config.MaxCollectionsPerHome:
            returnValue(customxml.MaxCollections.fromString(config.MaxCollectionsPerHome))

        elif qname == (customxml.calendarserver_namespace, "push-transports"):

            if config.Notifications.Services.APNS.Enabled:

                notifier = self._newStoreHome.getNotifier("push")
                nodeName = notifier.nodeName() if notifier is not None else None
                if nodeName:
                    notifierID = self._newStoreHome.notifierID()
                    if notifierID:
                        children = []

                        apsConfiguration = getPubSubAPSConfiguration(notifierID, config)
                        if apsConfiguration:
                            children.append(
                                customxml.PubSubTransportProperty(
                                    customxml.PubSubSubscriptionProperty(
                                        element.HRef(
                                            apsConfiguration["SubscriptionURL"]
                                        ),
                                    ),
                                    customxml.PubSubAPSBundleIDProperty(
                                        apsConfiguration["APSBundleID"]
                                    ),
                                    customxml.PubSubAPSEnvironmentProperty(
                                        apsConfiguration["APSEnvironment"]
                                    ),
                                    customxml.PubSubAPSRefreshIntervalProperty(
                                        str(apsConfiguration["SubscriptionRefreshIntervalSeconds"])
                                    ),
                                    type="APSD",
                                )
                            )

                        returnValue(customxml.PubSubPushTransportsProperty(*children))

            returnValue(None)

        elif qname == (customxml.calendarserver_namespace, "pushkey"):
            if (
                config.Notifications.Services.AMP.Enabled or
                config.Notifications.Services.APNS.Enabled
            ):
                notifier = self._newStoreHome.getNotifier("push")
                if notifier is not None:
                    returnValue(customxml.PubSubXMPPPushKeyProperty(notifier.nodeName()))
            returnValue(None)

        returnValue((yield super(CommonHomeResource, self).readProperty(property, request)))

    ##
    # ACL
    ##

    @inlineCallbacks
    def owner(self, request):
        principal = yield self.principalForRecord()
        returnValue(element.HRef(principal.principalURL()))

    def ownerPrincipal(self, request):
        return self.principalForRecord()

    def resourceOwnerPrincipal(self, request):
        return self.principalForRecord()

    @inlineCallbacks
    def defaultAccessControlList(self):
        myPrincipal = yield self.principalForRecord()

        # Server may be read only
        if config.EnableReadOnlyServer:
            owner_privs = (
                element.Privilege(element.Read()),
                element.Privilege(element.ReadCurrentUserPrivilegeSet()),
            )
        else:
            owner_privs = (element.Privilege(element.All()),)

        aces = (
            # Inheritable access for the resource's associated principal.
            element.ACE(
                element.Principal(element.HRef(myPrincipal.principalURL())),
                element.Grant(*owner_privs),
                element.Protected(),
                TwistedACLInheritable(),
            ),
        )

        # Give read access to config.ReadPrincipals
        aces += config.ReadACEs

        # Give all access to config.AdminPrincipals
        aces += config.AdminACEs

        returnValue(element.ACL(*aces))

    def accessControlList(self, request, inheritance=True, expanding=False, inherited_aces=None):
        # Permissions here are fixed, and are not subject to inheritance rules, etc.
        return self.defaultAccessControlList()

    def principalCollections(self):
        return self.parent.principalCollections()

    def principalForRecord(self):
        raise NotImplementedError("Subclass must implement principalForRecord()")

    @inlineCallbacks
    def etag(self):
        """
        Use the sync token as the etag
        """
        if self._newStoreHome:
            if config.EnableSyncReport and config.EnableSyncReportHome:
                token = (yield self.getInternalSyncToken())
            else:
                token = str(self._newStoreHome.modified())
            returnValue(ETag(hashlib.md5(token).hexdigest()))
        else:
            returnValue(None)

    def resourceID(self):
        return uuid.uuid5(self.uuid_namespace, str(self._newStoreHome.id())).urn

    def lastModified(self):
        return self._newStoreHome.modified() if self._newStoreHome else None

    def creationDate(self):
        return self._newStoreHome.created() if self._newStoreHome else None

    def notifierID(self):
        return "%s/%s" % self._newStoreHome.notifierID()

    def notifyChanged(self):
        return self._newStoreHome.notifyChanged()

    def http_PUT(self, request):
        """
        Cannot PUT to existing collection.
        """
        return responsecode.FORBIDDEN

    # Methods not supported
    http_ACL = None
    http_COPY = None
    http_MOVE = None


class CalendarHomeResource(DefaultAlarmPropertyMixin, CommonHomeResource):
    """
    Calendar home collection classmethod.
    """

    @classmethod
    @inlineCallbacks
    def homeFromTransaction(cls, transaction, uid):
        storeHome = yield transaction.calendarHomeWithUID(uid)
        if storeHome is not None:
            created = False
        else:
            storeHome = yield transaction.calendarHomeWithUID(uid, create=True)
            created = True
        returnValue((storeHome, created))

    def liveProperties(self):

        existing = super(CalendarHomeResource, self).liveProperties()
        existing += (
            caldavxml.SupportedCalendarComponentSets.qname(),

            # These are "live" properties in the sense of WebDAV, however "live" for twext actually means
            # ones that are also always present, but the default alarm properties are allowed to be absent
            # and are in fact stored in the property store.
            # caldavxml.DefaultAlarmVEventDateTime.qname(),
            # caldavxml.DefaultAlarmVEventDate.qname(),
            # caldavxml.DefaultAlarmVToDoDateTime.qname(),
            # caldavxml.DefaultAlarmVToDoDate.qname(),

        )

        if config.EnableManagedAttachments:
            existing += (
                caldavxml.ManagedAttachmentsServerURL.qname(),
            )

        if config.EnableTimezonesByReference:
            existing += (
                caldavxml.TimezoneServiceSet.qname(),
            )

        return existing

    def dynamicProperties(self):
        return super(CalendarHomeResource, self).dynamicProperties() + tuple(
            DefaultAlarmPropertyMixin.ALARM_PROPERTIES.keys()
        )

    def hasProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        # Force calendar collections to always appear to have the property
        if qname in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            return succeed(self.getDefaultAlarmProperty(qname) is not None)

        else:
            return super(CalendarHomeResource, self).hasProperty(property, request)

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        if qname == caldavxml.SupportedCalendarComponentSets.qname():
            if config.RestrictCalendarsToOneComponentType:
                prop = caldavxml.SupportedCalendarComponentSets(*[
                    caldavxml.SupportedCalendarComponentSet(
                        caldavxml.CalendarComponent(
                            name=name,
                        ),
                    ) for name in ical.allowedStoreComponents
                ])
            else:
                prop = caldavxml.SupportedCalendarComponentSets()
            returnValue(prop)

        elif qname == caldavxml.ManagedAttachmentsServerURL.qname():
            if config.EnableManagedAttachments:
                # The HRef is empty - this will force the client to treat all managed attachment URLs
                # as relative to this server scheme/host.
                returnValue(caldavxml.ManagedAttachmentsServerURL(element.HRef.fromString("")))
            else:
                returnValue(None)

        elif qname == caldavxml.TimezoneServiceSet.qname():
            if config.EnableTimezonesByReference and config.TimezoneService.Enabled:
                # Href points to standard timezone dist service.
                returnValue(caldavxml.TimezoneServiceSet(element.HRef.fromString(config.TimezoneService.URI)))
            else:
                returnValue(None)

        elif qname in DefaultAlarmPropertyMixin.ALARM_PROPERTIES:
            returnValue(self.getDefaultAlarmProperty(qname))

        result = (yield super(CalendarHomeResource, self).readProperty(property, request))
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

        result = (yield super(CalendarHomeResource, self).writeProperty(property, request))
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

        result = (yield super(CalendarHomeResource, self).removeProperty(property, request))
        returnValue(result)

    def _setupProvisions(self):

        # Cache children which must be of a specific type
        from twistedcaldav.storebridge import StoreScheduleInboxResource
        self._provisionedChildren["inbox"] = StoreScheduleInboxResource.maybeCreateInbox

        from twistedcaldav.scheduling_store.caldav.resource import ScheduleOutboxResource
        self._provisionedChildren["outbox"] = ScheduleOutboxResource

        if config.EnableDropBox and not config.EnableManagedAttachments:
            from twistedcaldav.storebridge import DropboxCollection
            self._provisionedChildren["dropbox"] = DropboxCollection

        if config.EnableManagedAttachments:
            from twistedcaldav.storebridge import AttachmentsCollection
            self._provisionedChildren["dropbox"] = AttachmentsCollection

        if config.FreeBusyURL.Enabled:
            from twistedcaldav.freebusyurl import FreeBusyURLResource
            self._provisionedChildren["freebusy"] = FreeBusyURLResource

        if config.Sharing.Enabled and config.Sharing.Calendars.Enabled:
            from twistedcaldav.notifications import NotificationCollectionResource
            self._provisionedChildren["notification"] = NotificationCollectionResource

    def canShare(self):
        return config.Sharing.Enabled and config.Sharing.Calendars.Enabled and self.exists()

    @inlineCallbacks
    def _otherPrincipalHomeURL(self, otherUID):
        ownerPrincipal = (yield self.principalForUID(otherUID))
        if ownerPrincipal and len(ownerPrincipal.calendarHomeURLs()):
            returnValue(ownerPrincipal.calendarHomeURLs()[0])
        else:
            returnValue(None)

    @inlineCallbacks
    def makeRegularChild(self, name):
        newCalendar = yield self._newStoreHome.calendarWithName(name)
        if newCalendar and not newCalendar.owned() and not self.canShare():
            newCalendar = None

        from twistedcaldav.storebridge import CalendarCollectionResource
        similar = CalendarCollectionResource(
            newCalendar, self, name=name,
            principalCollections=self.principalCollections()
        )
        self.propagateTransaction(similar)
        returnValue(similar)

    def hasCalendarResourceUIDSomewhereElse(self, uid, ok_object, mode):
        """
        Test if there are other child object resources with the specified UID.

        Pass through direct to store.
        """
        return self._newStoreHome.hasCalendarResourceUIDSomewhereElse(uid, ok_object._newStoreObject, mode)

    @inlineCallbacks
    def defaultAccessControlList(self):
        myPrincipal = yield self.principalForRecord()

        # Server may be read only
        if config.EnableReadOnlyServer:
            owner_privs = (
                element.Privilege(element.Read()),
                element.Privilege(element.ReadCurrentUserPrivilegeSet()),
            )
        else:
            owner_privs = (element.Privilege(element.All()),)

        aces = (
            # Inheritable access for the resource's associated principal.
            element.ACE(
                element.Principal(element.HRef(myPrincipal.principalURL())),
                element.Grant(*owner_privs),
                element.Protected(),
                TwistedACLInheritable(),
            ),
            # Inheritable CALDAV:read-free-busy access for authenticated users.
            element.ACE(
                element.Principal(element.Authenticated()),
                element.Grant(element.Privilege(caldavxml.ReadFreeBusy())),
                TwistedACLInheritable(),
            ),
        )

        # Give read access to config.ReadPrincipals
        aces += config.ReadACEs

        # Give all access to config.AdminPrincipals
        aces += config.AdminACEs

        if config.EnableProxyPrincipals:
            # Server may be read only
            if config.EnableReadOnlyServer:
                ro_proxy_privs = rw_proxy_privs = (
                    element.Privilege(element.Read()),
                    element.Privilege(element.ReadCurrentUserPrivilegeSet()),
                )
            else:
                ro_proxy_privs = (
                    element.Privilege(element.Read()),
                    element.Privilege(element.ReadCurrentUserPrivilegeSet()),
                    element.Privilege(element.WriteProperties()),
                )
                rw_proxy_privs = (
                    element.Privilege(element.Read()),
                    element.Privilege(element.ReadCurrentUserPrivilegeSet()),
                    element.Privilege(element.Write()),
                )

            aces += (
                # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-read users.
                element.ACE(
                    element.Principal(element.HRef(joinURL(myPrincipal.principalURL(), "calendar-proxy-read/"))),
                    element.Grant(*ro_proxy_privs),
                    element.Protected(),
                    TwistedACLInheritable(),
                ),
                # DAV:read/DAV:read-current-user-privilege-set/DAV:write access for this principal's calendar-proxy-write users.
                element.ACE(
                    element.Principal(element.HRef(joinURL(myPrincipal.principalURL(), "calendar-proxy-write/"))),
                    element.Grant(*rw_proxy_privs),
                    element.Protected(),
                    TwistedACLInheritable(),
                ),
            )

        returnValue(element.ACL(*aces))

    @inlineCallbacks
    def getInternalSyncToken(self):
        # The newstore implementation supports this directly
        caltoken = yield self._newStoreHome.syncToken()

        if config.Sharing.Enabled and config.Sharing.Calendars.Enabled:
            notificationtoken = yield (yield self.getChild("notification")).getInternalSyncToken()

            # Merge tokens
            caltoken = self._mergeSyncTokens(caltoken, notificationtoken)

        returnValue(caltoken)

    @inlineCallbacks
    def _indexWhatChanged(self, revision, depth):
        # The newstore implementation supports this directly
        changed, deleted, notallowed = yield self._newStoreHome.resourceNamesSinceToken(
            revision, depth
        )

        # Need to insert some addition items on first sync
        if revision == 0:
            changed.append("outbox/")

            if config.FreeBusyURL.Enabled:
                changed.append("freebusy")

            if config.Sharing.Enabled and config.Sharing.Calendars.Enabled:
                changed.append("notification/")

            # Dropbox is never synchronized
            if config.EnableDropBox or config.EnableManagedAttachments:
                notallowed.append("dropbox/")

        # Add in notification changes
        if config.Sharing.Enabled and config.Sharing.Calendars.Enabled:
            noti_changed, noti_deleted, noti_notallowed = yield (yield self.getChild("notification"))._indexWhatChanged(revision, depth)

            if noti_changed or noti_deleted:
                changed.append("notification")
            if depth == "infinity":
                changed.extend([joinURL("notification", name) for name in noti_changed])
                deleted.extend([joinURL("notification", name) for name in noti_deleted])
                notallowed.extend([joinURL("notification", name) for name in noti_notallowed])

        returnValue((changed, deleted, notallowed))

    @requiresPermissions(element.WriteContent())
    @inlineCallbacks
    def POST_handler_action(self, request, action):
        """
        Handle a POST request with an action= query parameter

        @param request: the request to process
        @type request: L{Request}
        @param action: the action to execute
        @type action: C{str}
        """
        if action == "emptytrash":
            days = int(request.args.get("days", ("0",))[0])
            yield self._newStoreHome.emptyTrash(days=days)
            returnValue(
                self._ok("ok", "Empty Trash")
            )

        elif action == "gettrashcontents":
            contents = yield self._newStoreHome.getTrashContents()
            returnValue(
                self._ok("ok", "Trash Contents", contents)
            )

        elif action == "recovertrash":
            mode = request.args.get("mode", ("event",))[0]
            recoveryID = request.args.get("id", (None,))[0]
            if recoveryID is None:
                returnValue(
                    self._ok("error", "Invalid id")
                )
            if recoveryID == "all":
                recoveryID = None
            else:
                try:
                    recoveryID = int(recoveryID)
                except ValueError:
                    returnValue(
                        self._ok("error", "Invalid id")
                    )

            yield self._newStoreHome.recoverTrash(mode, recoveryID)
            returnValue(
                self._ok("ok", "Recover Trash")
            )

        else:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (caldav_namespace, "valid-action-parameter",),
                "The action parameter in the request-URI is not valid",
            ))

    def _ok(self, status, description, result=None):
        if result is None:
            result = {}
        result["status"] = status
        result["description"] = description
        return JSONResponse(
            responsecode.OK,
            result,
        )


class AddressBookHomeResource (CommonHomeResource):
    """
    Address book home collection resource.
    """

    def __init__(self, *args, **kw):
        super(AddressBookHomeResource, self).__init__(*args, **kw)
        # get some Access header items
        self.http_MKCOL = None
        self.http_MKCALENDAR = None

    @classmethod
    @inlineCallbacks
    def homeFromTransaction(cls, transaction, uid):
        storeHome = yield transaction.addressbookHomeWithUID(uid)
        if storeHome is not None:
            created = False
        else:
            storeHome = yield transaction.addressbookHomeWithUID(uid, create=True)
            created = True
        returnValue((storeHome, created))

    def liveProperties(self):

        return super(AddressBookHomeResource, self).liveProperties() + (
            carddavxml.DefaultAddressBookURL.qname(),
        )

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        if qname == carddavxml.DefaultAddressBookURL.qname():
            # Must have a valid default
            try:
                defaultAddressBookProperty = self.readDeadProperty(property)
            except HTTPError:
                defaultAddressBookProperty = None
            if defaultAddressBookProperty and len(defaultAddressBookProperty.children) == 1:
                defaultAddressBook = str(defaultAddressBookProperty.children[0])
                adbk = (yield request.locateResource(str(defaultAddressBook)))
                if adbk is not None and isAddressBookCollectionResource(adbk) and adbk.exists() and not adbk.isShareeResource():
                    returnValue(defaultAddressBookProperty)

            # Default is not valid - we have to try to pick one
            defaultAddressBookProperty = (yield self.pickNewDefaultAddressBook(request))
            returnValue(defaultAddressBookProperty)

        result = (yield super(AddressBookHomeResource, self).readProperty(property, request))
        returnValue(result)

    @inlineCallbacks
    def writeProperty(self, property, request):
        assert isinstance(property, element.WebDAVElement)

        if property.qname() == carddavxml.DefaultAddressBookURL.qname():
            # Verify that the address book added in the PROPPATCH is valid.
            property.children = [element.HRef(normalizeURL(str(href))) for href in property.children]
            new_adbk = [str(href) for href in property.children]
            adbk = None
            if len(new_adbk) == 1:
                adbkURI = str(new_adbk[0])
                adbk = (yield request.locateResource(str(new_adbk[0])))
            if adbk is None or not adbk.exists() or not isAddressBookCollectionResource(adbk) or adbk.isShareeResource():
                # Validate that href's point to a valid addressbook.
                raise HTTPError(ErrorResponse(
                    responsecode.CONFLICT,
                    (carddav_namespace, "valid-default-addressbook-URL"),
                    "Invalid URI",
                ))
            else:
                # Canonicalize the URL to __uids__ form and always ensure a trailing /
                adbkURI = (yield adbk.canonicalURL(request))
                if not adbkURI.endswith("/"):
                    adbkURI += "/"
                property = carddavxml.DefaultAddressBookURL(element.HRef(adbkURI))

        yield super(AddressBookHomeResource, self).writeProperty(property, request)

    def _setupProvisions(self):

        # Cache children which must be of a specific type
        if config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled and not config.Sharing.Calendars.Enabled:
            from twistedcaldav.notifications import NotificationCollectionResource
            self._provisionedChildren["notification"] = NotificationCollectionResource

    def makeNewStore(self):
        return self._associatedTransaction.addressbookHomeWithUID(self.name, create=True), False     # Don't care about created

    def canShare(self):
        return config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled and self.exists()

    @inlineCallbacks
    def _otherPrincipalHomeURL(self, otherUID):
        ownerPrincipal = (yield self.principalForUID(otherUID))
        returnValue(ownerPrincipal.addressBookHomeURLs()[0] if ownerPrincipal else None)

    @inlineCallbacks
    def makeRegularChild(self, name):

        # Check for public/global path
        from twistedcaldav.storebridge import (
            AddressBookCollectionResource,
        )
        mainCls = AddressBookCollectionResource
        newAddressBook = yield self._newStoreHome.addressbookWithName(name)
        if newAddressBook and not newAddressBook.owned() and not self.canShare():
            newAddressBook = None
        similar = mainCls(
            newAddressBook, self, name,
            principalCollections=self.principalCollections()
        )
        self.propagateTransaction(similar)
        returnValue(similar)

    @inlineCallbacks
    def pickNewDefaultAddressBook(self, request):
        """
        First see if "addressbook" exists in the addressbook home and pick that. Otherwise
        pick the first one we see.
        """
        defaultAddressBookURL = joinURL(self.url(), "addressbook")
        defaultAddressBook = (yield self.makeRegularChild("addressbook"))
        if defaultAddressBook is None or not defaultAddressBook.exists():
            addressbooks = yield self._newStoreHome.addressbooks()
            ownedAddressBooks = [addressbook for addressbook in addressbooks if addressbook.owned()]
            ownedAddressBooks.sort(key=lambda ab: ab.name())

            # These are only unshared children
            # FIXME: the back-end should re-provision a default addressbook here.
            # Really, the dead property shouldn't be necessary, and this should
            # be entirely computed by a back-end method like 'defaultAddressBook()'
            try:
                anAddressBook = ownedAddressBooks[0]
            except IndexError:
                raise RuntimeError("No address books at all.")

            defaultAddressBookURL = joinURL(self.url(), anAddressBook.name())

        # Always ensure a trailing /
        if not defaultAddressBookURL.endswith("/"):
            defaultAddressBookURL += "/"

        self.writeDeadProperty(
            carddavxml.DefaultAddressBookURL(
                element.HRef(defaultAddressBookURL)
            )
        )
        returnValue(carddavxml.DefaultAddressBookURL(
            element.HRef(defaultAddressBookURL))
        )

    @inlineCallbacks
    def getInternalSyncToken(self):
        # The newstore implementation supports this directly
        adbktoken = yield self._newStoreHome.syncToken()

        if config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled and not config.Sharing.Calendars.Enabled:
            notifcationtoken = yield (yield self.getChild("notification")).getInternalSyncToken()

            # Merge tokens
            adbkkey, adbkrev = adbktoken.split("_", 1)
            notrev = notifcationtoken.split("_", 1)[1]
            if int(notrev) > int(adbkrev):
                adbktoken = "%s_%s" % (adbkkey, notrev,)

        returnValue(adbktoken)

    @inlineCallbacks
    def _indexWhatChanged(self, revision, depth):
        # The newstore implementation supports this directly
        changed, deleted, notallowed = yield self._newStoreHome.resourceNamesSinceToken(
            revision, depth
        )

        # Need to insert some addition items on first sync
        if revision == 0:
            if config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled and not config.Sharing.Calendars.Enabled:
                changed.append("notification/")

        # Add in notification changes
        if config.Sharing.Enabled and config.Sharing.AddressBooks.Enabled and not config.Sharing.Calendars.Enabled:
            noti_changed, noti_deleted, noti_notallowed = yield (yield self.getChild("notification"))._indexWhatChanged(revision, depth)

            changed.extend([joinURL("notification", name) for name in noti_changed])
            deleted.extend([joinURL("notification", name) for name in noti_deleted])
            notallowed.extend([joinURL("notification", name) for name in noti_notallowed])

        returnValue((changed, deleted, notallowed))


class AuthenticationWrapper(SuperAuthenticationWrapper):

    """ AuthenticationWrapper implementation which allows overriding
        credentialFactories on a per-resource-path basis """

    def __init__(
        self, resource, portal,
        wireEncryptedCredentialFactories, wireUnencryptedCredentialFactories,
        loginInterfaces, overrides=None
    ):

        super(AuthenticationWrapper, self).__init__(
            resource, portal,
            wireEncryptedCredentialFactories, wireUnencryptedCredentialFactories,
            loginInterfaces
        )

        self.overrides = {}
        if overrides:
            for path, factories in overrides.iteritems():
                self.overrides[path] = dict(
                    [
                        (factory.scheme, factory)
                        for factory in factories
                    ]
                )

    def hook(self, req):
        """ Uses the default credentialFactories unless the request is for
            one of the overridden paths """

        super(AuthenticationWrapper, self).hook(req)

        factories = self.overrides.get(
            req.path.rstrip("/"),
            req.credentialFactories
        )
        req.credentialFactories = factories


##
# Utilities
##

def isCalendarCollectionResource(resource):
    try:
        resource = ICalDAVResource(resource)
    except TypeError:
        return False
    else:
        return resource.isCalendarCollection()


def isPseudoCalendarCollectionResource(resource):
    try:
        resource = ICalDAVResource(resource)
    except TypeError:
        return False
    else:
        return resource.isPseudoCalendarCollection()


def isAddressBookCollectionResource(resource):
    try:
        resource = ICalDAVResource(resource)
    except TypeError:
        return False
    else:
        return resource.isAddressBookCollection()
