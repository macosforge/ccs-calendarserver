# -*- test-case-name: twistedcaldav.test.test_sharing -*-
# #
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
# #

"""
Sharing behavior
"""


__all__ = [
    "SharedResourceMixin",
    "SharedHomeMixin",
]

from twext.who.idirectory import RecordType

from twisted.internet.defer import succeed, inlineCallbacks, DeferredList, \
    returnValue

from twistedcaldav import customxml, caldavxml
from twistedcaldav.config import config
from twistedcaldav.customxml import calendarserver_namespace
from twistedcaldav.linkresource import LinkFollowerMixIn

from txdav.common.datastore.sql_tables import _ABO_KIND_GROUP, \
    _BIND_MODE_DIRECT, _BIND_MODE_INDIRECT, _BIND_MODE_OWN, _BIND_MODE_READ, \
    _BIND_MODE_WRITE, _BIND_STATUS_ACCEPTED, _BIND_STATUS_DECLINED, \
    _BIND_STATUS_DELETED, _BIND_STATUS_INVALID, _BIND_STATUS_INVITED
from txdav.common.idirectoryservice import DirectoryRecordNotFoundError
from txdav.xml import element
from txdav.who.wiki import RecordType as WikiRecordType, WikiAccessLevel

from txweb2 import responsecode
from txweb2.dav.http import ErrorResponse, MultiStatusResponse
from txweb2.dav.resource import TwistedACLInheritable
from txweb2.dav.util import allDataFromStream, joinURL
from txweb2.http import HTTPError, Response, XMLResponse


class SharedResourceMixin(object):
    """
    A mix-in for calendar/addressbook resources that implements sharing-related
    functionality.
    """

    @inlineCallbacks
    def inviteProperty(self, request):
        """
        Calculate the customxml.Invite property (for readProperty) from the
        invites database.
        """
        if config.Sharing.Enabled:

            @inlineCallbacks
            def invitePropertyElement(invitation, includeUID=True):

                userid = "urn:x-uid:" + invitation.shareeUID
                principal = yield self.principalForUID(invitation.shareeUID)
                cn = principal.displayName() if principal else invitation.shareeUID
                returnValue(customxml.InviteUser(
                    customxml.UID.fromString(invitation.uid) if includeUID else None,
                    element.HRef.fromString(userid),
                    customxml.CommonName.fromString(cn),
                    customxml.InviteAccess(invitationBindModeToXMLMap[invitation.mode]()),
                    invitationBindStatusToXMLMap[invitation.status](),
                ))

            # See if this property is on the shared calendar
            if self.isSharedByOwner():
                invitations = yield self.validateInvites(request)
                returnValue(customxml.Invite(
                    *[(yield invitePropertyElement(invitation)) for invitation in invitations]
                ))

            # See if it is on the sharee calendar
            if self.isShareeResource():
                original = yield self._newStoreObject.ownerView()
                if original is not None:
                    invitations = yield original.allInvitations()
                    invitations = yield self.validateInvites(request, invitations)

                    ownerPrincipal = yield self.principalForUID(self._newStoreObject.ownerHome().uid())
                    if ownerPrincipal is None:
                        owner = "invalid"
                        ownerCN = "Invalid"
                    else:
                        # FIXME:  use urn:x-uid in all cases
                        if self.isCalendarCollection():
                            owner = ownerPrincipal.principalURL()
                        else:
                            owner = "urn:x-uid:" + ownerPrincipal.principalUID()
                        ownerCN = ownerPrincipal.displayName()

                    returnValue(customxml.Invite(
                        customxml.Organizer(
                            element.HRef.fromString(owner),
                            customxml.CommonName.fromString(ownerCN),
                        ),
                        *[(yield invitePropertyElement(invitation, includeUID=False)) for invitation in invitations]
                    ))

        returnValue(None)

    @inlineCallbacks
    def upgradeToShare(self):
        """
        Set the resource-type property on this resource to indicate that this
        is the owner's version of a resource which has been shared.
        """
        # Change status on store object
        yield self._newStoreObject.setShared(True)

    @inlineCallbacks
    def downgradeFromShare(self, request):

        # Change status on store object
        yield self._newStoreObject.setShared(False)

        # Remove all invitees
        for invitation in (yield self._newStoreObject.allInvitations()):
            yield self._newStoreObject.uninviteUIDFromShare(invitation.shareeUID)

        returnValue(True)

    @inlineCallbacks
    def directShare(self, request):
        """
        Directly bind an accessible calendar/address book collection into the
        current principal's calendar/addressbook home.

        @param request: the request triggering this action
        @type request: L{IRequest}

        @return: the (asynchronous) HTTP result to respond to the direct-share
            request.
        @rtype: L{Deferred} firing L{txweb2.http.Response}, failing with
            L{HTTPError}
        """

        # Need to have at least DAV:read to do this
        yield self.authorize(request, (element.Read(),))

        # Find current principal
        authz_principal = self.currentPrincipal(request).children[0]
        if not isinstance(authz_principal, element.HRef):
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "valid-principal"),
                "Current user principal not a DAV:href",
            ))
        principalURL = str(authz_principal)
        if not principalURL:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "valid-principal"),
                "Current user principal not specified",
            ))
        sharee = (yield request.locateResource(principalURL))

        # Check enabled for service
        from twistedcaldav.directory.principal import DirectoryCalendarPrincipalResource
        if not isinstance(sharee, DirectoryCalendarPrincipalResource):
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-principal"),
                "Current user principal is not a calendar/addressbook enabled principal",
            ))

        # Get the home collection
        if self.isCalendarCollection():
            shareeHomeResource = yield sharee.calendarHome(request)
        elif self.isAddressBookCollection() or self.isGroup():
            shareeHomeResource = yield sharee.addressBookHome(request)
        else:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-principal"),
                "No calendar/addressbook home for principal",
            ))

        # TODO: Make sure principal is not sharing back to themselves
        hostURL = (yield self.canonicalURL(request))
        shareeHomeURL = shareeHomeResource.url()
        if hostURL.startswith(shareeHomeURL):
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-share"),
                "Can't share your own calendar or addressbook",
            ))

        # Accept it
        shareeView = yield self._newStoreObject.directShareWithUser(
            sharee.principalUID(),
            displayName=self.displayName()
        )

        # Return the URL of the shared calendar
        sharedAsURL = joinURL(shareeHomeResource.url(), shareeView.name())
        returnValue(XMLResponse(
            code=responsecode.OK,
            element=customxml.SharedAs(
                element.HRef.fromString(sharedAsURL)
            )
        ))

    def isSharedByOwner(self):
        """
        Return True if this is an owner shared calendar collection.
        """
        try:
            return self._newStoreObject.isSharedByOwner() if self._newStoreObject else False
        except AttributeError:
            return False

    def setShare(self, share_url):
        """
        Set the URL associated with this L{SharedResourceMixin}.  (This
        is only invoked on the sharee's resource, not the owner's.)
        """
        self._isShareeResource = True
        self._share_url = share_url

    def isShareeResource(self):
        """
        Return True if this is a sharee view of a shared collection.
        """
        return (
            hasattr(self, "_newStoreObject") and
            hasattr(self._newStoreObject, "owned") and
            not self._newStoreObject.owned() and
            getattr(self._newStoreObject, "_bindMode", None) is not None
        )

    def removeShareeResource(self, request):
        """
        Called when the sharee DELETEs a shared collection.
        """
        return self._newStoreObject.deleteShare()

    @inlineCallbacks
    def _checkAccessControl(self):
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
        if self._newStoreObject.direct():
            owner = yield self.principalForUID(self._newStoreObject.ownerHome().uid())
            sharee = yield self.principalForUID(self._newStoreObject.viewerHome().uid())
            if owner.record.recordType == WikiRecordType.macOSXServerWiki:
                # Access level comes from what the wiki has granted to the
                # sharee
                access = (yield owner.record.accessForRecord(sharee.record))
                if access == WikiAccessLevel.read:
                    returnValue("read-only")
                elif access == WikiAccessLevel.write:
                    returnValue("read-write")
                else:
                    returnValue(None)
            else:
                # Check proxy access
                proxy_mode = yield sharee.proxyMode(owner)
                if proxy_mode == "none":
                    returnValue("original")
                else:
                    returnValue("read-write" if proxy_mode == "write" else "read-only")
        else:
            # Invited shares use access mode from the invite
            # Get the access for self
            bindMode = yield self._newStoreObject.effectiveShareMode()
            returnValue(invitationAccessFromBindModeMap.get(bindMode))

    @inlineCallbacks
    def shareeAccessControlList(self, request, *args, **kwargs):
        """
        Return WebDAV ACLs appropriate for the current user accessing the
        shared collection.  For an "invite" share we take the privilege granted
        to the sharee in the invite and map that to WebDAV ACLs.  For a
        "direct" share, if it is a wiki collection we map the wiki privileges
        into WebDAV ACLs, otherwise we use whatever privileges exist on the
        underlying shared collection.

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

        assert self._isShareeResource, "Only call this for a sharee resource"
        assert self.isCalendarCollection() or self.isAddressBookCollection(), "Only call this for a address book or calendar resource"

        sharee = yield self.principalForUID(self._newStoreObject.viewerHome().uid())
        access = yield self._checkAccessControl()

        if access == "original" and not self._newStoreObject.ownerHome().external():
            original = (yield request.locateResource(self._share_url))
            result = (yield original.accessControlList(request, *args, **kwargs))
            returnValue(result)

        # Direct shares use underlying privileges of shared collection
        userprivs = [
        ]
        if access in ("read-only", "read-write",):
            userprivs.append(element.Privilege(element.Read()))
            userprivs.append(element.Privilege(element.ReadACL()))
            userprivs.append(element.Privilege(element.ReadCurrentUserPrivilegeSet()))
        if access in ("read-only",):
            userprivs.append(element.Privilege(element.WriteProperties()))
        if access in ("read-write",):
            userprivs.append(element.Privilege(element.Write()))
        proxyprivs = list(userprivs)
        try:
            proxyprivs.remove(element.Privilege(element.ReadACL()))
        except ValueError:
            # If wiki says no-access then ReadACL won't be in the list
            pass

        aces = (
            # Inheritable specific access for the resource's associated principal.
            element.ACE(
                element.Principal(element.HRef(sharee.principalURL())),
                element.Grant(*userprivs),
                element.Protected(),
                TwistedACLInheritable(),
            ),
        )

        if self.isCalendarCollection():
            aces += (
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

        if self.isCalendarCollection() and config.EnableProxyPrincipals:
            aces += (
                # DAV:read/DAV:read-current-user-privilege-set access for this principal's calendar-proxy-read users.
                element.ACE(
                    element.Principal(element.HRef(joinURL(sharee.principalURL(), "calendar-proxy-read/"))),
                    element.Grant(
                        element.Privilege(element.Read()),
                        element.Privilege(element.ReadCurrentUserPrivilegeSet()),
                        element.Privilege(element.WriteProperties()),
                    ),
                    element.Protected(),
                    TwistedACLInheritable(),
                ),
                # DAV:read/DAV:read-current-user-privilege-set/DAV:write access for this principal's calendar-proxy-write users.
                element.ACE(
                    element.Principal(element.HRef(joinURL(sharee.principalURL(), "calendar-proxy-write/"))),
                    element.Grant(*proxyprivs),
                    element.Protected(),
                    TwistedACLInheritable(),
                ),
            )

        returnValue(element.ACL(*aces))

    @inlineCallbacks
    def validUserIDForShare(self, userid, request=None):
        """
        Test the user id to see if it is a valid identifier for sharing and
        return a "normalized" form for our own use (e.g. convert mailto: to
        urn:uuid).

        @param userid: the userid to test
        @type userid: C{str}

        @return: C{str} of normalized userid or C{None} if
            userid is not allowed.
        """

        # First try to resolve as a calendar principal
        principal = yield self.principalForCalendarUserAddress(userid)
        if principal is None:
            principal = yield self.principalForCalendarGroupAddress(userid)
        if principal:
            if request:
                ownerPrincipal = (yield self.ownerPrincipal(request))
                if ownerPrincipal is None or ownerPrincipal.principalURL() == principal.principalURL():
                    returnValue(None)
            returnValue(principal.principalURL())

        # TODO: we do not support external users right now so this is being hard-coded
        # off in spite of the config option.
        # elif config.Sharing.AllowExternalUsers:
        #    return userid
        else:
            returnValue(None)

    @inlineCallbacks
    def principalForCalendarGroupAddress(self, groupid):
        """
        Get principal for group address if extant
        """

        if (
            config.Sharing.Enabled and
            config.Sharing.Calendars.Enabled and
            config.Sharing.Calendars.Groups.Enabled
        ):
            # see if group
            for principalCollection in self.principalCollections():
                record = yield principalCollection.directory.recordWithCalendarUserAddress(groupid)
                if record is not None and record.recordType == RecordType.group:
                    groupPrincipal = yield principalCollection.principalForRecord(record)
                    if groupPrincipal is not None:
                        returnValue(groupPrincipal)

        returnValue(None)

    @inlineCallbacks
    def validateInvites(self, request, invitations=None):
        """
        Make sure each userid in an invite is valid - if not re-write status.
        """
        # assert request
        if invitations is None:
            invitations = yield self._newStoreObject.allInvitations()
        adjusted_invitations = []
        for invitation in invitations:
            if invitation.status != _BIND_STATUS_INVALID:
                if not (yield self.validUserIDForShare("urn:x-uid:" + invitation.shareeUID, request)):
                    self.log.error("Invalid sharee detected: {uid}", uid=invitation.shareeUID)
                    invitation = invitation._replace(status=_BIND_STATUS_INVALID)

            invitation = invitation._replace(
                mode=(
                    yield self._newStoreObject._effectiveShareMode(
                        invitation.mode, invitation.shareeUID, self._newStoreObject._txn
                    )
                )
            )
            adjusted_invitations.append(invitation)

        returnValue(adjusted_invitations)

    def inviteUIDToShare(self, userid, cn, ace, summary, request):
        """ Send out in invite first, and then add this user to the share list
            @param userid:
            @param ace: Must be one of customxml.ReadWriteAccess or customxml.ReadAccess
        """

        # TODO: Check if this collection is shared, and error out if it isn't
        resultIsList = True
        if type(userid) is not list:
            userid = [userid]
            resultIsList = False
        if type(cn) is not list:
            cn = [cn]

        dl = [self.inviteSingleUserToShare(_user, _cn, ace, summary, request) for _user, _cn in zip(userid, cn)]
        return self._processShareActionList(dl, resultIsList)

    def uninviteUIDFromShare(self, userid, ace, request):
        """
        Send out in uninvite first, and then remove this user from the share list.
        """
        # Do not validate the userid - we want to allow invalid users to be removed because they
        # may have been valid when added, but no longer valid now. Clients should be able to clear out
        # anything known to be invalid.

        # TODO: Check if this collection is shared, and error out if it isn't
        resultIsList = True
        if type(userid) is not list:
            userid = [userid]
            resultIsList = False

        dl = [self.uninviteSingleUserFromShare(user, ace, request) for user in userid]
        return self._processShareActionList(dl, resultIsList)

    def inviteUserUpdateToShare(self, userid, cn, aceOLD, aceNEW, summary, request):

        resultIsList = True
        if type(userid) is not list:
            userid = [userid]
            resultIsList = False
        if type(cn) is not list:
            cn = [cn]

        dl = [self.inviteSingleUserUpdateToShare(_user, _cn, aceOLD, aceNEW, summary, request) for _user, _cn in zip(userid, cn)]
        return self._processShareActionList(dl, resultIsList)

    def _processShareActionList(self, dl, resultIsList):
        def _defer(resultset):
            results = [result if success else False for success, result in resultset]
            return results if resultIsList else results[0]
        return DeferredList(dl).addCallback(_defer)

    @inlineCallbacks
    def inviteSingleUserToShare(self, userid, cn, ace, summary, request):  # @UnusedVariable

        # We currently only handle local users
        sharee = yield self.principalForCalendarUserAddress(userid)
        if sharee is None:
            sharee = yield self.principalForCalendarGroupAddress(userid)
            if sharee is None:
                returnValue(False)

        try:
            result = (yield self._newStoreObject.inviteUIDToShare(
                sharee.principalUID(),
                invitationBindModeFromXMLMap[type(ace)],
                summary,
            ))
        except Exception as e:
            self.log.error("Could not send sharing invite '{userid}': {ex}", userid=userid, ex=e)
            result = None

        returnValue(result)

    @inlineCallbacks
    def uninviteSingleUserFromShare(self, userid, aces, request):  # @UnusedVariable

        # Cancel invites - we'll just use whatever userid we are given. However, if we
        # cannot find a matching principal, try to extract the uid from the userid
        # and use that (to allow invalid principals to be removed).
        sharee = yield self.principalForCalendarUserAddress(userid)
        if sharee is not None:
            uid = sharee.principalUID()
        elif userid.startswith("urn:x-uid:"):
            uid = userid[10:]
        else:
            returnValue(False)

        try:
            result = (yield self._newStoreObject.uninviteUIDFromShare(uid))
        except Exception as e:
            self.log.error("Could not send sharing uninvite '{userid}': {ex}", userid=userid, ex=e)
            result = None

        returnValue(result)

    @inlineCallbacks
    def uninviteFromShare(self, invitation, request):

        yield self._newStoreObject.uninviteFromShare(invitation)
        returnValue(True)

    def inviteSingleUserUpdateToShare(self, userid, commonName, acesOLD, aceNEW, summary, request):  # @UnusedVariable

        # Just update existing
        return self.inviteSingleUserToShare(userid, commonName, aceNEW, summary, request)

    @inlineCallbacks
    def _xmlHandleInvite(self, request, docroot):
        # Sharing must be enabled for this collection
        if not self.canBeShared():
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (customxml.calendarserver_namespace, "valid-request"),
                "Sharing not supported on this resource",
            ))
        yield self.authorize(request, (element.Read(), element.Write()))
        result = (yield self._handleInvite(request, docroot))
        returnValue(result)

    @inlineCallbacks
    def _handleInvite(self, request, invitedoc):
        def _handleInviteSet(inviteset):
            userid = None
            cn = None
            access = None
            summary = None
            for item in inviteset.children:
                if isinstance(item, element.HRef):
                    userid = str(item)
                    continue
                if isinstance(item, customxml.CommonName):
                    cn = str(item)
                    continue
                if isinstance(item, customxml.InviteSummary):
                    summary = str(item)
                    continue
                if isinstance(item, customxml.ReadAccess) or isinstance(item, customxml.ReadWriteAccess):
                    access = item
                    continue
            if userid and access and summary:
                return (userid, cn, access, summary)
            else:
                error_text = []
                if userid is None:
                    error_text.append("missing href")
                if access is None:
                    error_text.append("missing access")
                if summary is None:
                    error_text.append("missing summary")
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (customxml.calendarserver_namespace, "valid-request"),
                    "%s: %s" % (", ".join(error_text), inviteset,),
                ))

        def _handleInviteRemove(inviteremove):
            userid = None
            access = []
            for item in inviteremove.children:
                if isinstance(item, element.HRef):
                    userid = str(item)
                    continue
                if isinstance(item, customxml.ReadAccess) or isinstance(item, customxml.ReadWriteAccess):
                    access.append(item)
                    continue
            if userid is None:
                raise HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (customxml.calendarserver_namespace, "valid-request"),
                    "Missing href: %s" % (inviteremove,),
                ))
            if len(access) == 0:
                access = None
            else:
                access = set(access)
            return (userid, access)

        setDict, removeDict, updateinviteDict = {}, {}, {}
        okusers = set()
        badusers = set()
        for item in invitedoc.children:
            if isinstance(item, customxml.InviteSet):
                userid, cn, access, summary = _handleInviteSet(item)
                setDict[userid] = (cn, access, summary)

                # Validate each userid on add only
                uid = (yield self.validUserIDForShare(userid, request))
                if uid is None:
                    uid = yield self.principalForCalendarGroupAddress(userid)
                (badusers if uid is None else okusers).add(userid)
            elif isinstance(item, customxml.InviteRemove):
                userid, access = _handleInviteRemove(item)
                removeDict[userid] = access

                # Treat removed userids as valid as we will fail invalid ones silently
                okusers.add(userid)

        # Only make changes if all OK
        if len(badusers) == 0:
            okusers = set()
            badusers = set()
            # Special case removing and adding the same user and treat that as an add
            sameUseridInRemoveAndSet = [u for u in removeDict.keys() if u in setDict]
            for u in sameUseridInRemoveAndSet:
                removeACL = removeDict[u]
                cn, newACL, summary = setDict[u]
                updateinviteDict[u] = (cn, removeACL, newACL, summary)
                del removeDict[u]
                del setDict[u]
            for userid, access in removeDict.iteritems():
                result = (yield self.uninviteUIDFromShare(userid, access, request))
                # If result is False that means the user being removed was not
                # actually invited, but let's not return an error in this case.
                okusers.add(userid)
            for userid, (cn, access, summary) in setDict.iteritems():
                result = (yield self.inviteUIDToShare(userid, cn, access, summary, request))
                (okusers if result else badusers).add(userid)
            for userid, (cn, removeACL, newACL, summary) in updateinviteDict.iteritems():
                result = (yield self.inviteUserUpdateToShare(userid, cn, removeACL, newACL, summary, request))
                (okusers if result else badusers).add(userid)

            # In this case bad items do not prevent ok items from being processed
            ok_code = responsecode.OK
        else:
            # In this case a bad item causes all ok items not to be processed so failed dependency is returned
            ok_code = responsecode.FAILED_DEPENDENCY

        # Do a final validation of the entire set of invites
        invites = (yield self.validateInvites(request))
        numRecords = len(invites)

        # Set the sharing state on the collection
        shared = self.isSharedByOwner()
        if shared and numRecords == 0:
            yield self.downgradeFromShare(request)
        elif not shared and numRecords != 0:
            yield self.upgradeToShare()

        # Create the multistatus response - only needed if some are bad
        if badusers:
            xml_responses = []
            xml_responses.extend([
                element.StatusResponse(element.HRef(userid), element.Status.fromResponseCode(ok_code))
                for userid in sorted(okusers)
            ])
            xml_responses.extend([
                element.StatusResponse(element.HRef(userid), element.Status.fromResponseCode(responsecode.FORBIDDEN))
                for userid in sorted(badusers)
            ])

            #
            # Return response
            #
            returnValue(MultiStatusResponse(xml_responses))
        else:
            returnValue(responsecode.OK)

    @inlineCallbacks
    def _xmlHandleInviteReply(self, request, docroot):
        # Sharing must be enabled for this collection
        if not self.canShare():
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (customxml.calendarserver_namespace, "valid-request"),
                "Sharing not supported on this resource",
            ))
        yield self.authorize(request, (element.Read(), element.Write()))
        result = (yield self._handleInviteReply(request, docroot))
        returnValue(result)

    def _handleInviteReply(self, request, docroot):
        raise NotImplementedError

    @inlineCallbacks
    def xmlRequestHandler(self, request):

        # Need to read the data and get the root element first
        xmldata = (yield allDataFromStream(request.stream))
        try:
            doc = element.WebDAVDocument.fromString(xmldata)
        except ValueError as e:
            self.log.error("Error parsing doc ({ex}) Doc:\n {x}", ex=str(e), x=xmldata)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (customxml.calendarserver_namespace, "valid-request"),
                "Invalid XML",
            ))

        root = doc.root_element
        if type(root) in self.xmlDocHandlers:
            result = (yield self.xmlDocHandlers[type(root)](self, request, root))
            returnValue(result)
        else:
            self.log.error("Unsupported XML ({r})", r=root)
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (customxml.calendarserver_namespace, "valid-request"),
                "Unsupported XML",
            ))

    xmlDocHandlers = {
        customxml.InviteShare: _xmlHandleInvite,
        customxml.InviteReply: _xmlHandleInviteReply,
    }

    def isGroup(self):
        try:
            return self._newStoreObject._kind == _ABO_KIND_GROUP
        except AttributeError:
            return False

    def POST_handler_content_type(self, request, contentType):
        if self.isCollection() or self.isGroup():
            if contentType:
                if contentType in self._postHandlers:
                    return self._postHandlers[contentType](self, request)
                else:
                    self.log.info("Got a POST on collection or group with an unsupported content type: {t}", t=contentType)
            else:
                self.log.info("Got a POST on collection or group with no content type")
        return succeed(responsecode.FORBIDDEN)

    _postHandlers = {
        ("application", "xml"): xmlRequestHandler,
        ("text", "xml"): xmlRequestHandler,
    }


invitationBindStatusToXMLMap = {
    _BIND_STATUS_INVITED: customxml.InviteStatusNoResponse,
    _BIND_STATUS_ACCEPTED: customxml.InviteStatusAccepted,
    _BIND_STATUS_DECLINED: customxml.InviteStatusDeclined,
    _BIND_STATUS_INVALID: customxml.InviteStatusInvalid,
    _BIND_STATUS_DELETED: customxml.InviteStatusDeleted,
}
invitationBindStatusFromXMLMap = dict((v, k) for k, v in invitationBindStatusToXMLMap.iteritems())

invitationBindModeToXMLMap = {
    _BIND_MODE_READ: customxml.ReadAccess,
    _BIND_MODE_WRITE: customxml.ReadWriteAccess,
}
invitationBindModeFromXMLMap = dict((v, k) for k, v in invitationBindModeToXMLMap.iteritems())

invitationAccessFromBindModeMap = {
    _BIND_MODE_OWN: "own",
    _BIND_MODE_READ: "read-only",
    _BIND_MODE_WRITE: "read-write",
    _BIND_MODE_DIRECT: "read-write",
    _BIND_MODE_INDIRECT: "read-write",
}


class SharedHomeMixin(LinkFollowerMixIn):
    """
    A mix-in for calendar/addressbook homes that defines the operations for
    manipulating a sharee's set of shared calendars.
    """

    @inlineCallbacks
    def provisionShare(self, child, request=None):
        """
        Set shared state and check access control.
        """
        if child._newStoreObject is not None and not child._newStoreObject.owned():
            ownerHomeURL = (yield self._otherPrincipalHomeURL(child._newStoreObject.ownerHome().uid()))
            ownerView = yield child._newStoreObject.ownerView()
            child.setShare(joinURL(ownerHomeURL, ownerView.name()) if ownerHomeURL else None)
            access = yield child._checkAccessControl()
            if access is None:
                returnValue(None)
        returnValue(child)

    def _otherPrincipalHomeURL(self, otherUID):
        # Is this only meant to be overridden?
        pass

    @inlineCallbacks
    def acceptShare(self, request, inviteUID, summary):

        # Accept the share
        try:
            shareeView = yield self._newStoreHome.acceptShare(inviteUID, summary)
        except DirectoryRecordNotFoundError:
            # Missing sharer record => fail request
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-share"),
                "Invite UID not valid",
            ))
        if shareeView is None:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-share"),
                "Invite UID not valid",
            ))

        # Return the URL of the shared collection
        sharedAsURL = joinURL(self.url(), shareeView.shareName())
        returnValue(XMLResponse(
            code=responsecode.OK,
            element=customxml.SharedAs(
                element.HRef.fromString(sharedAsURL)
            )
        ))

    @inlineCallbacks
    def declineShare(self, request, inviteUID):

        # Remove it if it is in the DB
        try:
            result = yield self._newStoreHome.declineShare(inviteUID)
        except DirectoryRecordNotFoundError:
            # Missing sharer record => just treat decline as success
            result = True
        if not result:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (calendarserver_namespace, "invalid-share"),
                "Invite UID not valid",
            ))
        returnValue(Response(code=responsecode.NO_CONTENT))

    def _handleInviteReply(self, request, invitereplydoc):
        """
        Handle a user accepting or declining a sharing invite
        """
        hostUrl = None
        accepted = None
        summary = None
        replytoUID = None
        for item in invitereplydoc.children:
            if isinstance(item, customxml.InviteStatusAccepted):
                accepted = True
            elif isinstance(item, customxml.InviteStatusDeclined):
                accepted = False
            elif isinstance(item, customxml.InviteSummary):
                summary = str(item)
            elif isinstance(item, customxml.HostURL):
                for hosturlItem in item.children:
                    if isinstance(hosturlItem, element.HRef):
                        hostUrl = str(hosturlItem)
            elif isinstance(item, customxml.InReplyTo):
                replytoUID = str(item)

        if accepted is None or hostUrl is None or replytoUID is None:
            raise HTTPError(ErrorResponse(
                responsecode.FORBIDDEN,
                (customxml.calendarserver_namespace, "valid-request"),
                "Missing required XML elements",
            ))
        if accepted:
            return self.acceptShare(request, replytoUID, summary=summary)
        else:
            return self.declineShare(request, replytoUID)
