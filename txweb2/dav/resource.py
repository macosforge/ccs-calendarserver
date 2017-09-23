# -*- test-case-name: txweb2.dav.test.test_resource -*-
##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# DRI: Wilfredo Sanchez, wsanchez@apple.com
##
from __future__ import print_function

"""
WebDAV resources.
"""

__all__ = [
    "DAVPropertyMixIn",
    "DAVResource",
    "DAVLeafResource",
    "DAVPrincipalResource",
    "DAVPrincipalCollectionResource",
    "AccessDeniedError",
    "isPrincipalResource",
    "TwistedACLInheritable",
    "TwistedGETContentMD5",
    "TwistedQuotaRootProperty",
    "allACL",
    "readonlyACL",
    "davPrivilegeSet",
    "unauthenticatedPrincipal",
]

import cPickle as pickle
import urllib

from zope.interface import implements

from twisted.cred.error import LoginFailed, UnauthorizedLogin
from twisted.python.failure import Failure
from twisted.internet.defer import (
    Deferred, maybeDeferred, succeed, inlineCallbacks, returnValue
)
from twisted.internet import reactor
from twisted.internet._sslverify import Certificate

from twext.python.log import Logger
from txdav.xml import element
from txdav.xml.base import encodeXMLName
from txdav.xml.element import WebDAVElement, WebDAVEmptyElement, WebDAVTextElement
from txdav.xml.element import dav_namespace
from txdav.xml.element import twisted_dav_namespace, twisted_private_namespace
from txdav.xml.element import registerElement, lookupElement
from txweb2 import responsecode
from txweb2.auth.tls import TLSCredentialsFactory, TLSCredentials
from txweb2.http import HTTPError, RedirectResponse, StatusResponse
from txweb2.http_headers import generateContentType
from txweb2.iweb import IResponse
from txweb2.resource import LeafResource
from txweb2.server import NoURLForResourceError
from txweb2.static import MetaDataMixin, StaticRenderMixin
from txweb2.auth.wrapper import UnauthorizedResponse
from txweb2.dav.idav import IDAVResource, IDAVPrincipalResource, IDAVPrincipalCollectionResource
from txweb2.dav.http import NeedPrivilegesResponse
from txweb2.dav.noneprops import NonePropertyStore
from txweb2.dav.util import unimplemented, parentForURL, joinURL
from txweb2.dav.auth import PrincipalCredentials
from twistedcaldav import customxml


log = Logger()


class DAVPropertyMixIn (MetaDataMixin):
    """
    Mix-in class which implements the DAV property access API in
    L{IDAVResource}.

    There are three categories of DAV properties, for the purposes of
    how this class manages them.  A X{property} is either a X{live
    property} or a X{dead property}, and live properties are split
    into two categories:

     1. Dead properties.  There are properties that the server simply
        stores as opaque data.  These are store in the X{dead property
        store}, which is provided by subclasses via the
        L{deadProperties} method.

     2. Live properties which are always computed.  These properties
        aren't stored anywhere (by this class) but instead are derived
        from the resource state or from data that is persisted
        elsewhere.  These are listed in the L{liveProperties}
        attribute and are handled explicitly by the L{readProperty}
        method.

     3. Live properties may be acted on specially and are stored in
        the X{dead property store}.  These are not listed in the
        L{liveProperties} attribute, but may be handled specially by
        the property access methods.  For example, L{writeProperty}
        might validate the data and refuse to write data it deems
        inappropriate for a given property.

    There are two sets of property access methods.  The first group
    (L{hasProperty}, etc.) provides access to all properties.  They
    automatically figure out which category a property falls into and
    act accordingly.

    The second group (L{hasDeadProperty}, etc.) accesses the dead
    property store directly and bypasses any live property logic that
    exists in the first group of methods.  These methods are used by
    the first group of methods, and there are cases where they may be
    needed by other methods.  I{Accessing dead properties directly
    should be done with caution.}  Bypassing the live property logic
    means that values may not be the correct ones for use in DAV
    requests such as PROPFIND, and may be bypassing security checks.
    In general, one should never bypass the live property logic as
    part of a client request for property data.

    Properties in the L{twisted_private_namespace} namespace are
    internal to the server and should not be exposed to clients.  They
    can only be accessed via the dead property store.
    """
    # Note: The DAV:owner and DAV:group live properties are only
    # meaningful if you are using ACL semantics (i.e. Unix-like) which
    # use them.  This (generic) class does not.

    def liveProperties(self):

        return (
            (dav_namespace, "resourcetype"),
            (dav_namespace, "getetag"),
            (dav_namespace, "getcontenttype"),
            (dav_namespace, "getcontentlength"),
            (dav_namespace, "getlastmodified"),
            (dav_namespace, "creationdate"),
            (dav_namespace, "displayname"),
            (dav_namespace, "supportedlock"),
            (dav_namespace, "supported-report-set"),  # RFC 3253, section 3.1.5
            # (dav_namespace, "owner"                     ), # RFC 3744, section 5.1
            # (dav_namespace, "group"                     ), # RFC 3744, section 5.2
            (dav_namespace, "supported-privilege-set"),  # RFC 3744, section 5.3
            (dav_namespace, "current-user-privilege-set"),  # RFC 3744, section 5.4
            (dav_namespace, "current-user-principal"),  # RFC 5397, Section 3
            (dav_namespace, "acl"),  # RFC 3744, section 5.5
            (dav_namespace, "acl-restrictions"),  # RFC 3744, section 5.6
            (dav_namespace, "inherited-acl-set"),  # RFC 3744, section 5.7
            (dav_namespace, "principal-collection-set"),  # RFC 3744, section 5.8
            (dav_namespace, "quota-available-bytes"),  # RFC 4331, section 3
            (dav_namespace, "quota-used-bytes"),  # RFC 4331, section 4

            (twisted_dav_namespace, "resource-class"),
        )

    def dynamicProperties(self):
        """
        These are not live but are generated by the server not from the dead
        property store.
        """

        return (
            (dav_namespace, "quota-available-bytes"),
            (dav_namespace, "quota-used-bytes"),
        )

    def deadProperties(self):
        """
        Provides internal access to the WebDAV dead property store.
        You probably shouldn't be calling this directly if you can use
        the property accessors in the L{IDAVResource} API instead.
        However, a subclass must override this method to provide it's
        own dead property store.

        This implementation returns an instance of
        L{NonePropertyStore}, which cannot store dead properties.
        Subclasses must override this method if they wish to store
        dead properties.

        @return: a dict-like object from which one can read and to
            which one can write dead properties.  Keys are qname
            tuples (i.e. C{(namespace, name)}) as returned by
            L{WebDAVElement.qname()} and values are
            L{WebDAVElement} instances.
        """
        if not hasattr(self, "_dead_properties"):
            self._dead_properties = NonePropertyStore(self)
        return self._dead_properties

    def hasProperty(self, property, request):
        """
        See L{IDAVResource.hasProperty}.
        """
        if type(property) is tuple:
            qname = property
        else:
            qname = (property.namespace, property.name)

        if qname[0] == twisted_private_namespace:
            return succeed(False)

        # Need to special case the dynamic live properties
        namespace, name = qname
        if namespace == dav_namespace:
            if name in ("quota-available-bytes", "quota-used-bytes"):
                d = self.hasQuota(request)
                d.addCallback(lambda result: result)
                return d

        return succeed(
            qname in self.liveProperties() or
            self.deadProperties().contains(qname)
        )

    def readProperty(self, property, request):
        """
        See L{IDAVResource.readProperty}.
        """
        @inlineCallbacks
        def defer():
            if type(property) is tuple:
                qname = property
                sname = encodeXMLName(*property)
            else:
                qname = property.qname()
                sname = property.sname()

            namespace, name = qname

            if namespace == dav_namespace:
                if name == "resourcetype":
                    # Allow live property to be overridden by dead property
                    if self.deadProperties().contains(qname):
                        returnValue(self.deadProperties().get(qname))
                    if self.isCollection():
                        returnValue(element.ResourceType.collection)  # @UndefinedVariable
                    returnValue(element.ResourceType.empty)  # @UndefinedVariable

                if name == "getetag":
                    etag = (yield self.etag())
                    if etag is None:
                        returnValue(None)
                    returnValue(element.GETETag(etag.generate()))

                if name == "getcontenttype":
                    mimeType = self.contentType()
                    if mimeType is None:
                        returnValue(None)
                    returnValue(element.GETContentType(generateContentType(mimeType)))

                if name == "getcontentlength":
                    length = self.contentLength()
                    if length is None:
                        # TODO: really we should "render" the resource and
                        # determine its size from that but for now we just
                        # return an empty element.
                        returnValue(element.GETContentLength(""))
                    else:
                        returnValue(element.GETContentLength(str(length)))

                if name == "getlastmodified":
                    lastModified = self.lastModified()
                    if lastModified is None:
                        returnValue(None)
                    returnValue(element.GETLastModified.fromDate(lastModified))

                if name == "creationdate":
                    creationDate = self.creationDate()
                    if creationDate is None:
                        returnValue(None)
                    returnValue(element.CreationDate.fromDate(creationDate))

                if name == "displayname":
                    displayName = self.displayName()
                    if displayName is None:
                        returnValue(None)
                    returnValue(element.DisplayName(displayName))

                if name == "supportedlock":
                    returnValue(element.SupportedLock(
                        element.LockEntry(
                            element.LockScope.exclusive,  # @UndefinedVariable
                            element.LockType.write  # @UndefinedVariable
                        ),
                        element.LockEntry(
                            element.LockScope.shared,  # @UndefinedVariable
                            element.LockType.write  # @UndefinedVariable
                        ),
                    ))

                if name == "supported-report-set":
                    returnValue(element.SupportedReportSet(*[
                        element.SupportedReport(report,)
                        for report in self.supportedReports()
                    ]))

                if name == "supported-privilege-set":
                    returnValue((yield self.supportedPrivileges(request)))

                if name == "acl-restrictions":
                    returnValue(element.ACLRestrictions())

                if name == "inherited-acl-set":
                    returnValue(element.InheritedACLSet(*self.inheritedACLSet()))

                if name == "principal-collection-set":
                    returnValue(element.PrincipalCollectionSet(*[
                        element.HRef(
                            principalCollection.principalCollectionURL()
                        )
                        for principalCollection in self.principalCollections()
                    ]))

                @inlineCallbacks
                def ifAllowed(privileges, callback):
                    try:
                        yield self.checkPrivileges(request, privileges)
                        result = yield callback()
                    except AccessDeniedError:
                        raise HTTPError(StatusResponse(
                            responsecode.UNAUTHORIZED,
                            "Access denied while reading property %s."
                            % (sname,)
                        ))
                    returnValue(result)

                if name == "current-user-privilege-set":
                    @inlineCallbacks
                    def callback():
                        privs = yield self.currentPrivileges(request)
                        returnValue(element.CurrentUserPrivilegeSet(*privs))
                    returnValue((yield ifAllowed(
                        (element.ReadCurrentUserPrivilegeSet(),),
                        callback
                    )))

                if name == "acl":
                    @inlineCallbacks
                    def callback():
                        acl = yield self.accessControlList(request)
                        if acl is None:
                            acl = element.ACL()
                        returnValue(acl)
                    returnValue(
                        (yield ifAllowed((element.ReadACL(),), callback))
                    )

                if name == "current-user-principal":
                    returnValue(element.CurrentUserPrincipal(
                        self.currentPrincipal(request).children[0]
                    ))

                if name == "quota-available-bytes":
                    qvalue = yield self.quota(request)
                    if qvalue is None:
                        raise HTTPError(StatusResponse(
                            responsecode.NOT_FOUND,
                            "Property %s does not exist." % (sname,)
                        ))
                    else:
                        returnValue(element.QuotaAvailableBytes(str(qvalue[0])))

                if name == "quota-used-bytes":
                    qvalue = yield self.quota(request)
                    if qvalue is None:
                        raise HTTPError(StatusResponse(
                            responsecode.NOT_FOUND,
                            "Property %s does not exist." % (sname,)
                        ))
                    else:
                        returnValue(element.QuotaUsedBytes(str(qvalue[1])))

            elif namespace == twisted_dav_namespace:
                if name == "resource-class":
                    returnValue(ResourceClass(self.__class__.__name__))

            elif namespace == twisted_private_namespace:
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Properties in the %s namespace are private to the server."
                    % (sname,)
                ))
            returnValue(self.deadProperties().get(qname))

        return defer()

    def writeProperty(self, property, request):
        """
        See L{IDAVResource.writeProperty}.
        """
        assert isinstance(property, WebDAVElement), (
            "Not a property: %r" % (property,)
        )

        def defer():
            if property.protected:
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Protected property %s may not be set."
                    % (property.sname(),)
                ))

            if property.namespace == twisted_private_namespace:
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Properties in the %s namespace are private to the server."
                    % (property.sname(),)
                ))

            return self.deadProperties().set(property)

        return maybeDeferred(defer)

    def removeProperty(self, property, request):
        """
        See L{IDAVResource.removeProperty}.
        """
        def defer():
            if type(property) is tuple:
                qname = property
                sname = encodeXMLName(*property)
            else:
                qname = property.qname()
                sname = property.sname()

            if qname in self.liveProperties():
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Live property %s cannot be deleted." % (sname,)
                ))

            if qname[0] == twisted_private_namespace:
                raise HTTPError(StatusResponse(
                    responsecode.FORBIDDEN,
                    "Properties in the %s namespace are private to the server."
                    % (qname[0],)
                ))

            return self.deadProperties().delete(qname)

        return maybeDeferred(defer)

    @inlineCallbacks
    def listProperties(self, request):
        """
        See L{IDAVResource.listProperties}.
        """
        qnames = set(self.liveProperties())

        # Add dynamic live properties that exist
        for dqname in self.dynamicProperties():
            has = (yield self.hasProperty(dqname, request))
            if not has:
                if dqname in qnames:
                    qnames.remove(dqname)
            else:
                qnames.add(dqname)

        for qname in self.deadProperties().list():
            if (
                qname not in qnames and
                qname[0] != twisted_private_namespace
            ):
                qnames.add(qname)

        returnValue(qnames)

    def listAllprop(self, request):
        """
        Some DAV properties should not be returned to a C{DAV:allprop}
        query.  RFC 3253 defines several such properties.  This method
        computes a subset of the property qnames returned by
        L{listProperties} by filtering out elements whose class have
        the C{.hidden} attribute set to C{True}.

        @return: a list of qnames of properties which are defined and
            are appropriate for use in response to a C{DAV:allprop}
            query.
        """
        def doList(qnames):
            result = []

            for qname in qnames:
                try:
                    if not lookupElement(qname).hidden:
                        result.append(qname)
                except KeyError:
                    # Unknown element
                    result.append(qname)

            return result

        d = self.listProperties(request)
        d.addCallback(doList)
        return d

    def hasDeadProperty(self, property):
        """
        Same as L{hasProperty}, but bypasses the live property store
        and checks directly from the dead property store.
        """
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        return self.deadProperties().contains(qname)

    def readDeadProperty(self, property):
        """
        Same as L{readProperty}, but bypasses the live property store
        and reads directly from the dead property store.
        """
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        return self.deadProperties().get(qname)

    def writeDeadProperty(self, property):
        """
        Same as L{writeProperty}, but bypasses the live property store
        and writes directly to the dead property store.  Note that
        this should not be used unless you know that you are writing
        to an overrideable live property, as this bypasses the logic
        which protects protected properties.  The result of writing to
        a non-overrideable live property with this method is
        undefined; the value in the dead property store may or may not
        be ignored when reading the property with L{readProperty}.
        """
        self.deadProperties().set(property)

    def removeDeadProperty(self, property):
        """
        Same as L{removeProperty}, but bypasses the live property
        store and acts directly on the dead property store.
        """
        if self.hasDeadProperty(property):
            if type(property) is tuple:
                qname = property
            else:
                qname = property.qname()

            self.deadProperties().delete(qname)

    #
    # Overrides some methods in MetaDataMixin in order to allow DAV properties
    # to override the values of some HTTP metadata.
    #
    def contentType(self):
        if self.hasDeadProperty((element.dav_namespace, "getcontenttype")):
            return self.readDeadProperty(
                (element.dav_namespace, "getcontenttype")
            ).mimeType()
        else:
            return super(DAVPropertyMixIn, self).contentType()

    def displayName(self):
        if self.hasDeadProperty((element.dav_namespace, "displayname")):
            return str(self.readDeadProperty(
                (element.dav_namespace, "displayname")
            ))
        else:
            return super(DAVPropertyMixIn, self).displayName()


class DAVResource (DAVPropertyMixIn, StaticRenderMixin):
    """
    WebDAV resource.
    """
    implements(IDAVResource)

    def __init__(self, principalCollections=None):
        """
        @param principalCollections: an iterable of
            L{IDAVPrincipalCollectionResource}s which contain
            principals to be used in ACLs for this resource.
        """
        if principalCollections is not None:
            self._principalCollections = frozenset([
                IDAVPrincipalCollectionResource(principalCollection)
                for principalCollection in principalCollections
            ])

    ##
    # DAV
    ##

    def davComplianceClasses(self):
        """
        This implementation raises L{NotImplementedError}.
        @return: a sequence of strings denoting WebDAV compliance
            classes.  For example, a DAV level 2 server might return
            ("1", "2").
        """
        unimplemented(self)

    def isCollection(self):
        """
        See L{IDAVResource.isCollection}.

        This implementation raises L{NotImplementedError}; a subclass
        must override this method.
        """
        unimplemented(self)

    def findChildren(
        self, depth, request, callback,
        privileges=None, inherited_aces=None
    ):
        """
        See L{IDAVResource.findChildren}.

        This implementation works for C{depth} values of C{"0"},
        C{"1"}, and C{"infinity"}.  As long as C{self.listChildren} is
        implemented
        """
        assert depth in ("0", "1", "infinity"), "Invalid depth: %s" % (depth,)

        if depth == "0" or not self.isCollection():
            return succeed(None)

        completionDeferred = Deferred()
        basepath = request.urlForResource(self)
        children = []

        def checkPrivilegesError(failure):
            failure.trap(AccessDeniedError)
            reactor.callLater(0, getChild)

        def checkPrivileges(child):
            if child is None or not hasattr(child, "checkPrivileges"):
                return None

            if privileges is None:
                return child

            d = child.checkPrivileges(
                request, privileges,
                inherited_aces=inherited_aces
            )
            d.addCallback(lambda _: child)
            return d

        def gotChild(child, childpath):
            if child is None:
                callback(None, childpath + "/")
            else:
                if child.isCollection():
                    callback(child, childpath + "/")
                    if depth == "infinity":
                        d = child.findChildren(
                            depth, request,
                            callback, privileges
                        )
                        d.addCallback(lambda x: reactor.callLater(0, getChild))
                        return d
                else:
                    callback(child, childpath)

            reactor.callLater(0, getChild)

        def getChild():
            try:
                childname = children.pop()
            except IndexError:
                completionDeferred.callback(None)
            else:
                childpath = joinURL(basepath, urllib.quote(childname))
                d = request.locateChildResource(self, childname)
                d.addCallback(checkPrivileges)
                d.addCallbacks(gotChild, checkPrivilegesError, (childpath,))
                d.addErrback(completionDeferred.errback)

        def gotChildren(listChildrenResult):
            children[:] = list(listChildrenResult)
            getChild()
        maybeDeferred(self.listChildren).addCallback(gotChildren).addErrback(completionDeferred.errback)

        return completionDeferred

    @inlineCallbacks
    def findChildrenFaster(
        self, depth, request, okcallback, badcallback, missingcallback, unavailablecallback,
        names, privileges, inherited_aces
    ):
        """
        See L{IDAVResource.findChildren}.

        This implementation works for C{depth} values of C{"0"},
        C{"1"}, and C{"infinity"}.  As long as C{self.listChildren} is
        implemented

        @param depth: a C{str} for the depth: "0", "1" and "infinity"
        only allowed.
        @param request: the L{Request} for the current request in
        progress
        @param okcallback: a callback function used on all resources
            that pass the privilege check, or C{None}
        @param badcallback: a callback function used on all resources
            that fail the privilege check, or C{None}
        @param missingcallback: a callback function used on all resources
            that are missing, or C{None}
        @param names: a C{list} of C{str}'s containing the names of
            the child resources to lookup. If empty or C{None} all
            children will be examined, otherwise only the ones in the
            list.
        @param privileges: a list of privileges to check.
        @param inherited_aces: the list of parent ACEs that are
            inherited by all children.
        """
        assert depth in ("0", "1", "infinity"), "Invalid depth: %s" % (depth,)

        if depth == "0" or not self.isCollection():
            returnValue(None)

        # First find all depth 1 children
        names1 = []
        namesDeep = []
        collections1 = []
        if names:
            for name in names:
                (names1 if name.rstrip("/").find("/") == -1 else namesDeep).append(name.rstrip("/"))

        # children = []
        # yield self.findChildren("1", request, lambda x, y: children.append((x, y)), privileges=None, inherited_aces=None)

        children = []
        basepath = request.urlForResource(self)
        childnames = list((yield self.listChildren()))
        for childname in childnames:
            childpath = joinURL(basepath, urllib.quote(childname))
            try:
                child = (yield request.locateChildResource(self, childname))
            except HTTPError as e:
                log.error("Resource cannot be located: %s" % (str(e),))
                if unavailablecallback:
                    unavailablecallback(childpath)
                continue
            if child is not None:
                if child.isCollection():
                    collections1.append((child, childpath + "/"))
                if names and childname not in names1:
                    continue
                if child.isCollection():
                    children.append((child, childpath + "/"))
                else:
                    children.append((child, childpath))

        if missingcallback:
            for name in set(names1) - set(childnames):
                missingcallback(joinURL(basepath, urllib.quote(name)))

        # Generate (acl,supported_privs) map
        aclmap = {}
        for resource, url in children:
            acl = (yield resource.accessControlList(
                request, inheritance=False, inherited_aces=inherited_aces
            ))
            supportedPrivs = (yield resource.supportedPrivileges(request))
            aclmap.setdefault(
                (pickle.dumps(acl), supportedPrivs),
                (acl, supportedPrivs, [])
            )[2].append((resource, url))

        # Now determine whether each ace satisfies privileges
        # print(aclmap)
        for items in aclmap.itervalues():
            checked = (yield self.checkACLPrivilege(
                request, items[0], items[1], privileges, inherited_aces
            ))
            if checked:
                for resource, url in items[2]:
                    if okcallback:
                        okcallback(resource, url)
            else:
                if badcallback:
                    for resource, url in items[2]:
                        badcallback(resource, url)

        if depth == "infinity":
            # Split names into child collection groups
            child_collections = {}
            for name in namesDeep:
                collection, name = name.split("/", 1)
                child_collections.setdefault(collection, []).append(name)

            for collection, url in collections1:
                collection_name = url.split("/")[-2]
                if collection_name in child_collections:
                    collection_inherited_aces = (
                        yield collection.inheritedACEsforChildren(request)
                    )
                    yield collection.findChildrenFaster(
                        depth, request, okcallback, badcallback, missingcallback, unavailablecallback,
                        child_collections[collection_name] if names else None, privileges,
                        inherited_aces=collection_inherited_aces
                    )

        returnValue(None)

    @inlineCallbacks
    def checkACLPrivilege(
        self, request, acl, privyset, privileges, inherited_aces
    ):

        if acl is None:
            returnValue(False)

        principal = self.currentPrincipal(request)

        # Other principal types don't make sense as actors.
        assert principal.children[0].name in ("unauthenticated", "href"), (
            "Principal is not an actor: %r" % (principal,)
        )

        acl = self.fullAccessControlList(acl, inherited_aces)

        pending = list(privileges)
        denied = []

        for ace in acl.children:
            for privilege in tuple(pending):
                if not self.matchPrivilege(
                    element.Privilege(privilege), ace.privileges, privyset
                ):
                    continue

                match = (yield self.matchPrincipal(
                    principal, ace.principal, request
                ))

                if match:
                    if ace.invert:
                        continue
                else:
                    if not ace.invert:
                        continue

                pending.remove(privilege)

                if not ace.allow:
                    denied.append(privilege)

        returnValue(len(denied) + len(pending) == 0)

    def fullAccessControlList(self, acl, inherited_aces):
        """
        See L{IDAVResource.accessControlList}.

        This implementation looks up the ACL in the private property
        C{(L{twisted_private_namespace}, "acl")}.
        If no ACL has been stored for this resource, it returns the value
        returned by C{defaultAccessControlList}.
        If access is disabled it will return C{None}.
        """
        #
        # Inheritance is problematic. Here is what we do:
        #
        # 1. A private element <Twisted:inheritable> is defined for use inside
        #    of a <DAV:ace>. This private element is removed when the ACE is
        #    exposed via WebDAV.
        #
        # 2. When checking ACLs with inheritance resolution, the server must
        #    examine all parent resources of the current one looking for any
        #    <Twisted:inheritable> elements.
        #
        # If those are defined, the relevant ace is applied to the ACL on the
        # current resource.
        #

        # Dynamically update privileges for those ace's that are inherited.
        if acl:
            aces = list(acl.children)
        else:
            aces = []

        aces.extend(inherited_aces)

        acl = element.ACL(*aces)

        return acl

    def supportedReports(self):
        """
        See L{IDAVResource.supportedReports}.

        This implementation lists the three main ACL reports and
        expand-property.
        """
        result = []
        result.append(element.Report(element.ACLPrincipalPropSet(),))
        result.append(element.Report(element.PrincipalMatch(),))
        result.append(element.Report(element.PrincipalPropertySearch(),))
        result.append(element.Report(element.ExpandProperty(),))
        result.append(element.Report(customxml.CalendarServerPrincipalSearch(),))
        return result

    ##
    # Authentication
    ##

    def authorize(self, request, privileges, recurse=False):
        """
        See L{IDAVResource.authorize}.
        """
        def whenAuthenticated(result):
            privilegeCheck = self.checkPrivileges(request, privileges, recurse)
            return privilegeCheck.addErrback(whenAccessDenied)

        def whenAccessDenied(f):
            f.trap(AccessDeniedError)

            # If we were unauthenticated to start with (no
            # Authorization header from client) then we should return
            # an unauthorized response instead to force the client to
            # login if it can.

            # We're not adding the headers here because this response
            # class is supposed to be a FORBIDDEN status code and
            # "Authorization will not help" according to RFC2616

            def translateError(response):
                return Failure(HTTPError(response))

            if request.authnUser == None:
                return UnauthorizedResponse.makeResponse(
                    request.credentialFactories,
                    request.remoteAddr).addCallback(translateError)
            else:
                return translateError(
                    NeedPrivilegesResponse(request.uri, f.value.errors))

        d = self.authenticate(request)
        d.addCallback(whenAuthenticated)
        return d

    def authenticate(self, request):
        """
        Authenticate the given request against the portal, setting
        both C{request.authzUser} (a L{DAVPrincipalResource}, the user for the
        purposes of authorization) and C{request.authnUser} (a L{DAVPrincipalResource},
        the user for the purposes of authentication) when it has
        been authenticated.

        In order to authenticate, the request must have been
        previously prepared by
        L{txweb2.dav.auth.AuthenticationWrapper.hook} to have the
        necessary authentication metadata.

        If the request was not thusly prepared, both C{authzUser} and
        C{authnUser} will be None.

        @param request: the request which may contain authentication
            information and a reference to a portal to authenticate
            against.
        @type request: L{txweb2.iweb.IRequest}.
        @return: a L{Deferred} which fires with a 2-tuple of
            C{(authnUser, authzUser)} if either the request is
            unauthenticated OR contains valid credentials to
            authenticate as a principal, or errbacks with L{HTTPError}
            if the authentication scheme is unsupported, or the
            credentials provided by the request are not valid.
        """

        # Bypass normal authentication if its already been done (by SACL check)
        if (
            getattr(request, "authnUser", None) is not None and
            getattr(request, "authzUser", None) is not None
        ):
            return succeed((request.authnUser, request.authzUser))

        if not (
            hasattr(request, "portal") and
            hasattr(request, "credentialFactories") and
            hasattr(request, "loginInterfaces")
        ):
            request.authnUser = None
            request.authzUser = None
            return succeed((request.authnUser, request.authzUser))

        # Check for native TLS client cert
        if request.clientCredentials() is not None:
            # Make this look as if it is done via the usual HTTP auth header approach
            authHeader = (TLSCredentialsFactory.scheme, request.clientCredentials())

        else:
            # Check for reverse proxy TLS client auth
            rproxy_cert = request.headers.getRawHeaders(TLSCredentials.CERTIFICATE_HEADER, ["*"])[0]
            rproxy_user = request.headers.getRawHeaders(TLSCredentials.USERNAME_HEADER, ["*"])[0]
            if rproxy_cert != "*":
                # Make this look as if it is done via the usual HTTP auth header approach
                try:
                    cert = Certificate.loadPEM(rproxy_cert.replace("\\r", "\r").replace("\\n", "\n"))
                except:
                    raise HTTPError(responsecode.BAD_REQUEST)
                authHeader = (
                    TLSCredentialsFactory.scheme,
                    TLSCredentials(cert)
                )
            elif rproxy_user != "*":
                # Make this look as if it is done via the usual HTTP auth header approach
                authHeader = (
                    TLSCredentialsFactory.scheme,
                    TLSCredentials(None, username=rproxy_user)
                )
            else:
                authHeader = request.headers.getHeader("authorization")

        if authHeader is not None:
            if authHeader[0] not in request.credentialFactories:
                log.debug(
                    "Client authentication scheme %s is not provided by server %s"
                    % (authHeader[0], request.credentialFactories.keys())
                )
                d = UnauthorizedResponse.makeResponse(
                    request.credentialFactories,
                    request.remoteAddr
                )
                return d.addCallback(lambda response: Failure(HTTPError(response)))
            else:
                factory = request.credentialFactories[authHeader[0]]

                def gotCreds(creds):
                    d = self.principalsForAuthID(request, creds.username)
                    d.addCallback(gotDetails, creds)
                    return d

                # Try to match principals in each principal collection
                # on the resource
                def gotDetails(details, creds):
                    if details == (None, None):
                        log.info(
                            "Could not find the principal resource for user id: %s"
                            % (creds.username,)
                        )
                        return Failure(HTTPError(responsecode.UNAUTHORIZED))

                    authnPrincipal = IDAVPrincipalResource(details[0])
                    authzPrincipal = IDAVPrincipalResource(details[1])
                    return PrincipalCredentials(authnPrincipal, authzPrincipal, creds)

                def login(pcreds):
                    return request.portal.login(pcreds, None, *request.loginInterfaces)

                def gotAuth(result):
                    request.authnUser = result[1]
                    request.authzUser = result[2]
                    return (request.authnUser, request.authzUser)

                def translateUnauthenticated(f):
                    f.trap(UnauthorizedLogin, LoginFailed)
                    ips = [request.remoteAddr.host, ]
                    fwdHeaders = request.headers.getRawHeaders("x-forwarded-for", "")
                    for hdr in fwdHeaders:
                        ips.append("fwd={}".format(hdr))
                    ips = ", ".join(ips)
                    log.info("Authentication failed: %s, client: %s" % (f.value, ips,))
                    d = UnauthorizedResponse.makeResponse(
                        request.credentialFactories, request.remoteAddr
                    )
                    d.addCallback(lambda response: Failure(HTTPError(response)))
                    return d

                d = factory.decode(authHeader[1], request)
                d.addCallback(gotCreds)
                d.addCallback(login)
                d.addCallbacks(gotAuth, translateUnauthenticated)
                return d
        else:
            if (
                hasattr(request, "checkedWiki") and
                hasattr(request, "authnUser") and
                hasattr(request, "authzUser")
            ):
                # This request has already been authenticated via the wiki
                return succeed((request.authnUser, request.authzUser))

            request.authnUser = None
            request.authzUser = None
            return succeed((request.authnUser, request.authzUser))

    ##
    # ACL
    ##

    def currentPrincipal(self, request):
        """
        @param request: the request being processed.
        @return: the current authorized principal, as derived from the
            given request.
        """
        if getattr(request, "authzUser", None) is not None:
            return request.authzUser.principalElement()
        else:
            return unauthenticatedPrincipal

    def principalCollections(self):
        """
        See L{IDAVResource.principalCollections}.
        """
        if hasattr(self, "_principalCollections"):
            return self._principalCollections
        else:
            return ()

    def defaultRootAccessControlList(self):
        """
        @return: the L{element.ACL} element containing the default
            access control list for this resource.
        """
        #
        # The default behaviour is to allow GET access to everything
        # and deny any type of write access (PUT, DELETE, etc.) to
        # everything.
        #
        return readonlyACL

    def defaultAccessControlList(self):
        """
        @return: the L{element.ACL} element containing the default
            access control list for this resource.
        """
        #
        # The default behaviour is no ACL; we should inherit from the parent
        # collection.
        #
        return succeed(element.ACL())

    def setAccessControlList(self, acl):
        """
        See L{IDAVResource.setAccessControlList}.

        This implementation stores the ACL in the private property
        C{(L{twisted_private_namespace}, "acl")}.
        """
        self.writeDeadProperty(acl)

    @inlineCallbacks
    def mergeAccessControlList(self, new_acl, request):
        """
        Merges the supplied access control list with the one on this
        resource.  Merging means change all the non-inherited and
        non-protected ace's in the original, and do not allow the new
        one to specify an inherited or protected access control
        entry. This is the behaviour required by the C{ACL}
        request. (RFC 3744, section 8.1).

        @param new_acl:  an L{element.ACL} element
        @param request: the request being processed.
        @return: a tuple of the C{DAV:error} precondition element if
            an error occurred, C{None} otherwise.

        This implementation stores the ACL in the private property
        """
        # C{(L{twisted_private_namespace}, "acl")}.

        # Steps for ACL evaluation:
        #  1. Check that ace's on incoming do not match a protected ace
        #  2. Check that ace's on incoming do not match an inherited ace
        #  3. Check that ace's on incoming all have deny before grant
        #  4. Check that ace's on incoming do not use abstract privilege
        #  5. Check that ace's on incoming are supported
        #     (and are not inherited themselves)
        #  6. Check that ace's on incoming have valid principals
        #  7. Copy the original
        #  8. Remove all non-inherited and non-protected - and also inherited
        #  9. Add in ace's from incoming
        # 10. Verify that new acl is not in conflict with itself
        # 11. Update acl on the resource

        # Get the current access control list, preserving any private
        # properties on the ACEs as we will need to keep those when we
        # change the ACL.

        old_acl = (yield self.accessControlList(request, expanding=True))

        # Check disabled
        if old_acl is None:
            returnValue(None)

        # Need to get list of supported privileges
        supported = []

        def addSupportedPrivilege(sp):
            """
            Add the element in any DAV:Privilege to our list
            and recurse into any DAV:SupportedPrivilege's
            """
            for item in sp.children:
                if isinstance(item, element.Privilege):
                    supported.append(item.children[0])
                elif isinstance(item, element.SupportedPrivilege):
                    addSupportedPrivilege(item)

        supportedPrivs = (yield self.supportedPrivileges(request))
        for item in supportedPrivs.children:
            assert isinstance(item, element.SupportedPrivilege), (
                "Not a SupportedPrivilege: %r" % (item,)
            )
            addSupportedPrivilege(item)

        # Steps 1 - 6
        got_deny = False
        for ace in new_acl.children:
            for old_ace in old_acl.children:
                if (ace.principal == old_ace.principal):
                    # Step 1
                    if old_ace.protected:
                        log.error("Attempt to overwrite protected ace %r "
                                  "on resource %r"
                                  % (old_ace, self))
                        returnValue((
                            element.dav_namespace,
                            "no-protected-ace-conflict"
                        ))

                    # Step 2
                    #
                    # RFC3744 says that we either enforce the
                    # inherited ace conflict or we ignore it but use
                    # access control evaluation to determine whether
                    # there is any impact. Given that we have the
                    # "inheritable" behavior it does not make sense to
                    # disallow overrides of inherited ACEs since
                    # "inheritable" cannot itself be controlled via
                    # protocol.
                    #
                    # Otherwise, we'd use this logic:
                    #
                    # elif old_ace.inherited:
                    #    log.error("Attempt to overwrite inherited ace %r "
                    #              "on resource %r" % (old_ace, self))
                    #    returnValue((
                    #        element.dav_namespace,
                    #        "no-inherited-ace-conflict"
                    #    ))

            # Step 3
            if ace.allow and got_deny:
                log.error("Attempt to set grant ace %r after deny ace "
                          "on resource %r"
                          % (ace, self))
                returnValue((element.dav_namespace, "deny-before-grant"))
            got_deny = not ace.allow

            # Step 4: ignore as this server has no abstract privileges
            # (FIXME: none yet?)

            # Step 5
            for privilege in ace.privileges:
                if privilege.children[0] not in supported:
                    log.error("Attempt to use unsupported privilege %r "
                              "in ace %r on resource %r"
                              % (privilege.children[0], ace, self))
                    returnValue((
                        element.dav_namespace,
                        "not-supported-privilege"
                    ))

            if ace.protected:
                log.error("Attempt to create protected ace %r on resource %r"
                          % (ace, self))
                returnValue((element.dav_namespace, "no-ace-conflict"))

            if ace.inherited:
                log.error("Attempt to create inherited ace %r on resource %r" % (ace, self))
                returnValue((element.dav_namespace, "no-ace-conflict"))

            # Step 6
            valid = (yield self.validPrincipal(ace.principal, request))

            if not valid:
                log.error("Attempt to use unrecognized principal %r "
                          "in ace %r on resource %r"
                          % (ace.principal, ace, self))
                returnValue((element.dav_namespace, "recognized-principal"))

        # Step 8 & 9
        #
        # Iterate through the old ones and replace any that are in the
        # new set, or remove the non-inherited/non-protected not in
        # the new set
        #
        new_aces = [ace for ace in new_acl.children]
        new_set = []
        for old_ace in old_acl.children:
            for i, new_ace in enumerate(new_aces):
                if self.samePrincipal(new_ace.principal, old_ace.principal):
                    new_set.append(new_ace)
                    del new_aces[i]
                    break
            else:
                if old_ace.protected and not old_ace.inherited:
                    new_set.append(old_ace)
        new_set.extend(new_aces)

        # Step 10
        # FIXME: verify acl is self-consistent

        # Step 11
        yield self.writeNewACEs(new_set)
        returnValue(None)

    def writeNewACEs(self, new_aces):
        """
        Write a new ACL to the resource's property store.  This is a
        separate method so that it can be overridden by resources that
        need to do extra processing of ACLs being set via the ACL
        command.
        @param new_aces: C{list} of L{ACE} for ACL being set.
        """
        return self.setAccessControlList(element.ACL(*new_aces))

    def matchPrivilege(self, privilege, ace_privileges, supportedPrivileges):
        for ace_privilege in ace_privileges:
            if (
                privilege == ace_privilege or
                ace_privilege.isAggregateOf(privilege, supportedPrivileges)
            ):
                return True

        return False

    @inlineCallbacks
    def checkPrivileges(
        self, request, privileges, recurse=False,
        principal=None, inherited_aces=None
    ):
        """
        Check whether the given principal has the given privileges.
        (RFC 3744, section 5.5)

        @param request: the request being processed.
        @param privileges: an iterable of L{WebDAVElement}
            elements denoting access control privileges.
        @param recurse: C{True} if a recursive check on all child
            resources of this resource should be performed as well,
            C{False} otherwise.
        @param principal: the L{element.Principal} to check privileges
            for.  If C{None}, it is deduced from C{request} by calling
            L{currentPrincipal}.
        @param inherited_aces: a list of L{element.ACE}s corresponding
            to the pre-computed inheritable aces from the parent
            resource hierarchy.
        @return: a L{Deferred} that callbacks with C{None} or errbacks
            with an L{AccessDeniedError}
        """

        if principal is None:
            principal = self.currentPrincipal(request)

        supportedPrivs = (yield self.supportedPrivileges(request))

        # Other principals types don't make sense as actors.
        assert principal.children[0].name in ("unauthenticated", "href"), (
            "Principal is not an actor: %r" % (principal,)
        )

        errors = []

        resources = [(self, None)]

        if recurse:
            yield self.findChildren(
                "infinity", request,
                lambda x, y: resources.append((x, y))
            )

        for resource, uri in resources:
            acl = (
                yield resource.accessControlList(
                    request,
                    inherited_aces=inherited_aces
                )
            )

            # Check for disabled
            if acl is None:
                errors.append((uri, list(privileges)))
                continue

            pending = list(privileges)
            denied = []

            for ace in acl.children:
                for privilege in tuple(pending):
                    if not self.matchPrivilege(
                        element.Privilege(privilege),
                        ace.privileges, supportedPrivs
                    ):
                        continue

                    match = (
                        yield self.matchPrincipal(principal, ace.principal, request)
                    )

                    if match:
                        if ace.invert:
                            continue
                    else:
                        if not ace.invert:
                            continue

                    pending.remove(privilege)

                    if not ace.allow:
                        denied.append(privilege)

            denied += pending  # If no matching ACE, then denied

            if denied:
                errors.append((uri, denied))

        if errors:
            raise AccessDeniedError(errors,)

        returnValue(None)

    def supportedPrivileges(self, request):
        """
        See L{IDAVResource.supportedPrivileges}.

        This implementation returns a supported privilege set
        containing only the DAV:all privilege.
        """
        return succeed(davPrivilegeSet)

    def currentPrivileges(self, request):
        """
        See L{IDAVResource.currentPrivileges}.

        This implementation returns a current privilege set containing
        only the DAV:all privilege.
        """
        current = self.currentPrincipal(request)
        return self.privilegesForPrincipal(current, request)

    @inlineCallbacks
    def accessControlList(
        self, request, inheritance=True,
        expanding=False, inherited_aces=None
    ):
        """
        See L{IDAVResource.accessControlList}.

        This implementation looks up the ACL in the private property
        C{(L{twisted_private_namespace}, "acl")}.  If no ACL has been
        stored for this resource, it returns the value returned by
        C{defaultAccessControlList}.  If access is disabled it will
        return C{None}.
        """
        #
        # Inheritance is problematic. Here is what we do:
        #
        # 1. A private element <Twisted:inheritable> is defined for
        #    use inside of a <DAV:ace>. This private element is
        #    removed when the ACE is exposed via WebDAV.
        #
        # 2. When checking ACLs with inheritance resolution, the
        #    server must examine all parent resources of the current
        #    one looking for any <Twisted:inheritable> elements.
        #
        # If those are defined, the relevant ace is applied to the ACL on the
        # current resource.
        #

        myURL = None

        def getMyURL():
            url = request.urlForResource(self)

            assert url is not None, (
                "urlForResource(self) returned None for resource %s" % (self,)
            )

            return url

        try:
            acl = self.readDeadProperty(element.ACL)
        except HTTPError as e:
            assert e.response.code == responsecode.NOT_FOUND, (
                "Expected %s response from readDeadProperty() exception, "
                "not %s"
                % (responsecode.NOT_FOUND, e.response.code)
            )

            # Produce a sensible default for an empty ACL.
            if myURL is None:
                myURL = getMyURL()

            if myURL == "/":
                # If we get to the root without any ACLs, then use the default.
                acl = self.defaultRootAccessControlList()
            else:
                acl = yield self.defaultAccessControlList()

        # Dynamically update privileges for those ace's that are inherited.
        if inheritance:
            aces = list(acl.children)

            if myURL is None:
                myURL = getMyURL()

            if inherited_aces is None:
                if myURL != "/":
                    parentURL = parentForURL(myURL)

                    parent = (yield request.locateResource(parentURL))

                    if parent:
                        parent_acl = (
                            yield parent.accessControlList(
                                request, inheritance=True, expanding=True
                            )
                        )

                        # Check disabled
                        if parent_acl is None:
                            returnValue(None)

                        for ace in parent_acl.children:
                            if ace.inherited:
                                aces.append(ace)
                            elif TwistedACLInheritable() in ace.children:
                                # Adjust ACE for inherit on this resource
                                children = list(ace.children)
                                children.remove(TwistedACLInheritable())
                                children.append(
                                    element.Inherited(element.HRef(parentURL))
                                )
                                aces.append(element.ACE(*children))
            else:
                aces.extend(inherited_aces)

            # Always filter out any remaining private properties when we are
            # returning the ACL for the final resource after doing parent
            # inheritance.
            if not expanding:
                aces = [
                    element.ACE(*[
                        c for c in ace.children
                        if c != TwistedACLInheritable()
                    ])
                    for ace in aces
                ]

            acl = element.ACL(*aces)

        returnValue(acl)

    def inheritedACEsforChildren(self, request):
        """
        Do some optimisation of access control calculation by
        determining any inherited ACLs outside of the child resource
        loop and supply those to the checkPrivileges on each child.

        @param request: the L{IRequest} for the request in progress.
        @return: a C{list} of L{Ace}s that child resources of this one
            will inherit.
        """

        # Get the parent ACLs with inheritance and preserve the
        # <inheritable> element.

        def gotACL(parent_acl):
            # Check disabled
            if parent_acl is None:
                return None

            # Filter out those that are not inheritable (and remove
            # the inheritable element from those that are)
            aces = []
            for ace in parent_acl.children:
                if ace.inherited:
                    aces.append(ace)
                elif TwistedACLInheritable() in ace.children:
                    # Adjust ACE for inherit on this resource
                    children = list(ace.children)
                    children.remove(TwistedACLInheritable())
                    children.append(
                        element.Inherited(
                            element.HRef(request.urlForResource(self))
                        )
                    )
                    aces.append(element.ACE(*children))
            return aces

        d = self.accessControlList(request, inheritance=True, expanding=True)
        d.addCallback(gotACL)
        return d

    def inheritedACLSet(self):
        """
        @return: a sequence of L{element.HRef}s from which ACLs are
        inherited.

        This implementation returns an empty set.
        """
        return []

    @inlineCallbacks
    def principalsForAuthID(self, request, authid):
        """
        Return authentication and authorization principal identifiers
        for the authentication identifier passed in. In this
        implementation authn and authz principals are the same.

        @param request: the L{IRequest} for the request in progress.
        @param authid: a string containing the
            authentication/authorization identifier for the principal
            to lookup.
        @return: a deferred tuple of two tuples. Each tuple is
            C{(principal, principalURI)} where: C{principal} is the
            L{Principal} that is found; {principalURI} is the C{str}
            URI of the principal.  The first tuple corresponds to
            authentication identifiers, the second to authorization
            identifiers.  It will errback with an
            HTTPError(responsecode.FORBIDDEN) if the principal isn't
            found.
        """
        authnPrincipal = yield self.findPrincipalForAuthID(authid)

        if authnPrincipal is None:
            returnValue((None, None))

        authzPrincipal = yield self.authorizationPrincipal(request, authid, authnPrincipal)
        returnValue((authnPrincipal, authzPrincipal))

    @inlineCallbacks
    def findPrincipalForAuthID(self, authid):
        """
        Return authentication and authorization principal identifiers
        for the authentication identifier passed in. In this
        implementation authn and authz principals are the same.

        @param authid: a string containing the
            authentication/authorization identifier for the principal
            to lookup.
        @return: a tuple of C{(principal, principalURI)} where:
            C{principal} is the L{Principal} that is found;
            {principalURI} is the C{str} URI of the principal.  If not
            found return None.
        """
        for collection in self.principalCollections():
            principal = yield collection.principalForUser(authid)
            if principal is not None:
                returnValue(principal)
        returnValue(None)

    def authorizationPrincipal(self, request, authid, authnPrincipal):
        """
        Determine the authorization principal for the given request
        and authentication principal.  This implementation simply uses
        that authentication principal as the authorization principal.

        @param request: the L{IRequest} for the request in progress.
        @param authid: a string containing the
            authentication/authorization identifier for the principal
            to lookup.
        @param authnPrincipal: the L{IDAVPrincipal} for the
            authenticated principal
        @return: a deferred result C{tuple} of (L{IDAVPrincipal},
            C{str}) containing the authorization principal resource
            and URI respectively.
        """
        return succeed(authnPrincipal)

    def samePrincipal(self, principal1, principal2):
        """
        Check whether the two principals are exactly the same in terms of
        elements and data.

        @param principal1: a L{Principal} to test.
        @param principal2: a L{Principal} to test.
        @return: C{True} if they are the same, C{False} otherwise.
        """

        # The interesting part of a principal is it's one child
        principal1 = principal1.children[0]
        principal2 = principal2.children[0]

        if type(principal1) == type(principal2):
            if isinstance(principal1, element.Property):
                return (
                    type(principal1.children[0]) ==
                    type(principal2.children[0])
                )
            elif isinstance(principal1, element.HRef):
                return (
                    str(principal1.children[0]) ==
                    str(principal2.children[0])
                )
            else:
                return True
        else:
            return False

    def matchPrincipal(self, principal1, principal2, request):
        """
        Check whether the principal1 is a principal in the set defined
        by principal2.

        @param principal1: a L{Principal} to test. C{principal1} must
            contain a L{element.HRef} or L{element.Unauthenticated}
            element.
        @param principal2: a L{Principal} to test.
        @param request: the request being processed.
        @return: C{True} if they match, C{False} otherwise.
        """
        # See RFC 3744, section 5.5.1

        # The interesting part of a principal is it's one child
        principal1 = principal1.children[0]
        principal2 = principal2.children[0]

        if not hasattr(request, "matchPrincipals"):
            request.matchPrincipals = {}

        cache_key = (str(principal1), str(principal2))

        match = request.matchPrincipals.get(cache_key, None)
        if match is not None:
            return succeed(match)

        def doMatch():
            if isinstance(principal2, element.All):
                return succeed(True)

            elif isinstance(principal2, element.Authenticated):
                if isinstance(principal1, element.Unauthenticated):
                    return succeed(False)
                elif isinstance(principal1, element.All):
                    return succeed(False)
                else:
                    return succeed(True)

            elif isinstance(principal2, element.Unauthenticated):
                if isinstance(principal1, element.Unauthenticated):
                    return succeed(True)
                else:
                    return succeed(False)

            elif isinstance(principal1, element.Unauthenticated):
                return succeed(False)

            assert isinstance(principal1, element.HRef), (
                "Not an HRef: %r" % (principal1,)
            )

            def resolved(principal2):
                assert principal2 is not None, "principal2 is None"

                # Compare two HRefs and do group membership test as well
                if principal1 == principal2:
                    return True

                return self.principalIsGroupMember(
                    str(principal1), str(principal2), request
                )

            d = self.resolvePrincipal(principal2, request)
            d.addCallback(resolved)
            return d

        def cache(match):
            request.matchPrincipals[cache_key] = match
            return match

        d = doMatch()
        d.addCallback(cache)
        return d

    @inlineCallbacks
    def principalIsGroupMember(self, principal1, principal2, request):
        """
        Check whether one principal is a group member of another.

        @param principal1: C{str} principalURL for principal to test.
        @param principal2: C{str} principalURL for possible group
            principal to test against.
        @param request: the request being processed.
        @return: L{Deferred} with result C{True} if principal1 is a
            member of principal2, C{False} otherwise
        """
        resource1 = yield request.locateResource(principal1)
        resource2 = yield request.locateResource(principal2)

        if resource2 and isinstance(resource2, DAVPrincipalResource):
            isContained = yield resource2.containsPrincipal(resource1)
            returnValue(isContained)
        returnValue(False)

    def validPrincipal(self, ace_principal, request):
        """
        Check whether the supplied principal is valid for this resource.
        @param ace_principal: the L{Principal} element to test
        @param request: the request being processed.
        @return C{True} if C{ace_principal} is valid, C{False} otherwise.

        This implementation tests for a valid element type and checks
        for an href principal that exists inside of a principal
        collection.
        """
        def defer():
            #
            # We know that the element contains a valid element type, so all
            # we need to do is check for a valid property and a valid href.
            #
            real_principal = ace_principal.children[0]

            if isinstance(real_principal, element.Property):
                # See comments in matchPrincipal().  We probably need
                # some common code.
                log.error("Encountered a property principal (%s), "
                          "but handling is not implemented."
                          % (real_principal,))
                return False

            if isinstance(real_principal, element.HRef):
                return self.validHrefPrincipal(real_principal, request)

            return True

        return maybeDeferred(defer)

    def validHrefPrincipal(self, href_principal, request):
        """
        Check whether the supplied principal (in the form of an Href)
        is valid for this resource.

        @param href_principal: the L{Href} element to test
        @param request: the request being processed.
        @return C{True} if C{href_principal} is valid, C{False}
            otherwise.

        This implementation tests for a href element that corresponds
        to a principal resource and matches the principal-URL.
        """

        # Must have the principal resource type and must match the
        # principal-URL

        def _matchPrincipalURL(resource):
            return (
                isPrincipalResource(resource) and
                resource.principalURL() == str(href_principal)
            )

        d = request.locateResource(str(href_principal))
        d.addCallback(_matchPrincipalURL)
        return d

    def resolvePrincipal(self, principal, request):
        """
        Resolves a L{element.Principal} element into a L{element.HRef}
        element if possible.  Specifically, the given C{principal}'s
        contained element is resolved.

        L{element.Property} is resolved to the URI in the contained
        property.

        L{element.Self} is resolved to the URI of this resource.

        L{element.HRef} elements are returned as-is.

        All other principals, including meta-principals
        (eg. L{element.All}), resolve to C{None}.

        @param principal: the L{element.Principal} child element to
        resolve.
        @param request: the request being processed.
        @return: a deferred L{element.HRef} element or C{None}.
        """

        if isinstance(principal, element.Property):
            # NotImplementedError("Property principals are not implemented.")
            #
            # We can't raise here without potentially crippling the
            # server in a way that can't be fixed over the wire, so
            # let's refuse the match and log an error instead.
            #
            # Note: When fixing this, also fix validPrincipal()
            #
            log.error("Encountered a property principal (%s), "
                      "but handling is not implemented; invalid for ACL use."
                      % (principal,))
            return succeed(None)

            #
            # FIXME: I think this is wrong - we need to get the
            # namespace and name from the first child of DAV:property
            #
            namespace = principal.attributes.get(["namespace"], dav_namespace)
            name = principal.attributes["name"]

            def gotPrincipal(principal):
                try:
                    principal = principal.getResult()
                except HTTPError as e:
                    assert e.response.code == responsecode.NOT_FOUND, (
                        "%s (!= %s) status from readProperty() exception"
                        % (e.response.code, responsecode.NOT_FOUND)
                    )
                    return None

                if not isinstance(principal, element.Principal):
                    log.error("Non-principal value in property %s "
                              "referenced by property principal."
                              % (encodeXMLName(namespace, name),))
                    return None

                if len(principal.children) != 1:
                    return None

                # The interesting part of a principal is it's one child
                principal = principal.children[0]

                # XXXXXX FIXME XXXXXX

            d = self.readProperty((namespace, name), request)
            d.addCallback(gotPrincipal)
            return d

        elif isinstance(principal, element.Self):
            try:
                self = IDAVPrincipalResource(self)
            except TypeError:
                log.error("DAV:self ACE is set on non-principal resource %r"
                          % (self,))
                return succeed(None)
            principal = element.HRef(self.principalURL())

        if isinstance(principal, element.HRef):
            return succeed(principal)

        assert isinstance(principal, (
            element.All,
            element.Authenticated,
            element.Unauthenticated
        )), "Not a meta-principal: %r" % (principal,)

        return succeed(None)

    @inlineCallbacks
    def privilegesForPrincipal(self, principal, request):
        """
        See L{IDAVResource.privilegesForPrincipal}.
        """
        # NB Return aggregate privileges expanded.

        acl = (yield self.accessControlList(request))

        # Check disabled
        if acl is None:
            returnValue(())

        granted = []
        denied = []
        for ace in acl.children:
            # First see if the ace's principal affects the principal
            # being tested.  FIXME: support the DAV:invert operation

            match = (
                yield self.matchPrincipal(principal, ace.principal, request)
            )

            if match:
                # Expand aggregate privileges
                ps = []
                supportedPrivs = (
                    yield self.supportedPrivileges(request)
                )
                for p in ace.privileges:
                    ps.extend(p.expandAggregate(supportedPrivs))

                # Merge grant/deny privileges
                if ace.allow:
                    granted.extend([p for p in ps if p not in granted])
                else:
                    denied.extend([p for p in ps if p not in denied])

        # Subtract denied from granted
        allowed = tuple(p for p in granted if p not in denied)

        returnValue(allowed)

    def matchACEinACL(self, acl, ace):
        """
        Find an ACE in the ACL that matches the supplied ACE's principal.
        @param acl: the L{ACL} to look at.
        @param ace: the L{ACE} to try and match
        @return:    the L{ACE} in acl that matches, None otherwise.
        """
        for a in acl.children:
            if self.samePrincipal(a.principal, ace.principal):
                return a

        return None

    def principalSearchPropertySet(self):
        """
        @return: a L{element.PrincipalSearchPropertySet} element describing the
        principal properties that can be searched on this principal collection,
        or C{None} if this is not a principal collection.

        This implementation returns None. Principal collection resources must
        override and return their own suitable response.
        """
        return None

    ##
    # Quota
    ##

    """
    The basic policy here is to define a private 'quota-root' property
    on a collection.  That property will contain the maximum allowed
    bytes for the collections and all its contents.

    In order to determine the quota property values on a resource, the
    server must look for the private property on that resource and any
    of its parents. If found on a parent, then that parent should be
    queried for quota information. If not found, no quota exists for
    the resource.

    To determine that actual quota in use we will cache the used byte
    count on the quota-root collection in another private property. It
    is the servers responsibility to keep that property up to date by
    adjusting it after every PUT, DELETE, COPY, MOVE, MKCOL,
    PROPPATCH, ACL, POST or any other method that may affect the size
    of stored data. If the private property is not present, the server
    will fall back to getting the size by iterating over all resources
    (this is done in static.py).
    """

    def quota(self, request):
        """
        Get current available & used quota values for this resource's
        quota root collection.

        @return: an L{Deferred} with result C{tuple} containing two
            C{int}'s the first is quota-available-bytes, the second is
            quota-used-bytes, or C{None} if quota is not defined on
            the resource.
        """

        # See if already cached
        if hasattr(request, "quota"):
            if self in request.quota:
                return succeed(request.quota[self])
        else:
            request.quota = {}

        # Find the quota root for this resource and return its data
        def gotQuotaRootResource(qroot_resource):
            if qroot_resource:
                qroot = qroot_resource.quotaRoot(request)
                if qroot is not None:
                    def gotUsage(used):
                        available = qroot - used
                        if available < 0:
                            available = 0
                        request.quota[self] = (available, used)
                        return (available, used)

                    d = qroot_resource.currentQuotaUse(request)
                    d.addCallback(gotUsage)
                    return d

            request.quota[self] = None
            return None

        d = self.quotaRootResource(request)
        d.addCallback(gotQuotaRootResource)
        return d

    def hasQuota(self, request):
        """
        Check whether this resource is under quota control by checking
        each parent to see if it has a quota root.

        @return: C{True} if under quota control, C{False} if not.
        """

        def gotQuotaRootResource(qroot_resource):

            return qroot_resource is not None

        d = self.quotaRootResource(request)
        d.addCallback(gotQuotaRootResource)
        return d

    def hasQuotaRoot(self, request):
        """
        @return: a C{True} if this resource has quota root, C{False} otherwise.
        """
        return self.hasDeadProperty(TwistedQuotaRootProperty)

    def quotaRoot(self, request):
        """
        @return: a C{int} containing the maximum allowed bytes if this
            collection is quota-controlled, or C{None} if not quota
            controlled.
        """
        if self.hasDeadProperty(TwistedQuotaRootProperty):
            return int(str(self.readDeadProperty(TwistedQuotaRootProperty)))
        else:
            return None

    @inlineCallbacks
    def quotaRootResource(self, request):
        """
        Return the quota root for this resource.

        @return: L{DAVResource} or C{None}
        """

        if self.hasQuotaRoot(request):
            returnValue(self)

        # Check the next parent
        try:
            url = request.urlForResource(self)
        except NoURLForResourceError:
            returnValue(None)
        while (url != "/"):
            url = parentForURL(url)
            if url is None:
                break
            parent = (yield request.locateResource(url))
            if parent is None:
                break
            if parent.hasQuotaRoot(request):
                returnValue(parent)

        returnValue(None)

    def setQuotaRoot(self, request, maxsize):
        """
        @param maxsize: a C{int} containing the maximum allowed bytes
            for the contents of this collection, or C{None} to remove
            quota restriction.
        """
        assert self.isCollection(), "Only collections can have a quota root"
        assert maxsize is None or isinstance(maxsize, int), (
            "maxsize must be an int or None"
        )

        if maxsize is not None:
            self.writeDeadProperty(TwistedQuotaRootProperty(str(maxsize)))
        else:
            # Remove both the root and the cached used value
            self.removeDeadProperty(TwistedQuotaRootProperty)
            self.removeDeadProperty(TwistedQuotaUsedProperty)

    def quotaSize(self, request):
        """
        Get the size of this resource (if its a collection get total
        for all children as well).  TODO: Take into account size of
        dead-properties.

        @return: a C{int} containing the size of the resource.
        """
        unimplemented(self)

    def checkQuota(self, request, available):
        """
        Check to see whether all quota roots have sufficient available
        bytes.  We currently do not use hierarchical quota checks -
        i.e. only the most immediate quota root parent is checked for
        quota.

        @param available: a C{int} containing the additional quota
            required.
        @return: C{True} if there is sufficient quota remaining on all
            quota roots, C{False} otherwise.
        """

        def _defer(quotaroot):
            if quotaroot:
                # Check quota on this root (if it has one)
                quota = quotaroot.quotaRoot(request)
                if quota is not None:
                    if available > quota[0]:
                        return False

            return True

        d = self.quotaRootResource(request)
        d.addCallback(_defer)
        return d

    def quotaSizeAdjust(self, request, adjust):
        """
        Update the quota used value on all quota root parents of this
        resource.

        @param adjust: a C{int} containing the number of bytes added
            (positive) or removed (negative) that should be used to
            adjust the cached total.
        """

        def _defer(quotaroot):
            if quotaroot:
                # Check quota on this root (if it has one)
                return quotaroot.updateQuotaUse(request, adjust)

        d = self.quotaRootResource(request)
        d.addCallback(_defer)
        return d

    def currentQuotaUse(self, request):
        """
        Get the cached quota use value, or if not present (or invalid)
        determine quota use by brute force.

        @return: an L{Deferred} with a C{int} result containing the
            current used byte if this collection is quota-controlled,
            or C{None} if not quota controlled.
        """
        assert self.isCollection(), "Only collections can have a quota root"
        assert self.hasQuotaRoot(request), (
            "Quota use only on quota root collection"
        )

        # Try to get the cached value property
        if self.hasDeadProperty(TwistedQuotaUsedProperty):
            return succeed(
                int(str(self.readDeadProperty(TwistedQuotaUsedProperty)))
            )
        else:
            # Do brute force size determination and cache the result
            # in the private property
            def _defer(result):
                self.writeDeadProperty(TwistedQuotaUsedProperty(str(result)))
                return result
            d = self.quotaSize(request)
            d.addCallback(_defer)
            return d

    def updateQuotaUse(self, request, adjust):
        """
        Update the quota used value on this resource.

        @param adjust: a C{int} containing the number of bytes added
            (positive) or removed (negative) that should be used to
            adjust the cached total.
        @return: an L{Deferred} with a C{int} result containing the
            current used byte if this collection is quota-controlled,
            or C{None} if not quota controlled.
        """
        assert self.isCollection(), "Only collections can have a quota root"

        # Get current value
        def _defer(size):
            size += adjust

            # Sanity check the resulting size
            if size >= 0:
                self.writeDeadProperty(TwistedQuotaUsedProperty(str(size)))
            else:
                # Remove the dead property and re-read to do brute
                # force quota calc
                log.info("Attempt to set quota used to a negative value: %s "
                         "(adjustment: %s)"
                         % (size, adjust,))
                self.removeDeadProperty(TwistedQuotaUsedProperty)
                return self.currentQuotaUse(request)

        d = self.currentQuotaUse(request)
        d.addCallback(_defer)
        return d

    ##
    # HTTP
    ##

    def renderHTTP(self, request):
        # FIXME: This is for testing with litmus; comment out when not in use
        # litmus = request.headers.getRawHeaders("x-litmus")
        # if litmus: log.info("*** Litmus test: %s ***" % (litmus,))

        #
        # If this is a collection and the URI doesn't end in "/", redirect.
        #
        if self.isCollection() and request.path[-1:] != "/":
            return self.handleMissingTrailingSlash(request)

        def setHeaders(response):
            response = IResponse(response)

            # Only set the DAV header on an OPTIONS request
            if request.method == "OPTIONS":
                response.headers.setHeader("dav", self.davComplianceClasses())

            #
            # If this is a collection and the URI doesn't end in "/",
            # add a Content-Location header.  This is needed even if
            # we redirect such requests (as above) in the event that
            # this resource was created or modified by the request.
            #
            if self.isCollection() and request.path[-1:] != "/" and not response.headers.hasHeader("content-location"):
                response.headers.setHeader(
                    "content-location", request.path + "/"
                )

            return response

        def onError(f):
            # If we get an HTTPError, run its response through
            # setHeaders() as well.
            f.trap(HTTPError)
            return setHeaders(f.value.response)

        d = maybeDeferred(super(DAVResource, self).renderHTTP, request)
        return d.addCallbacks(setHeaders, onError)

    def handleMissingTrailingSlash(self, request):
        return RedirectResponse(
            request.unparseURL(
                path=urllib.quote(
                    urllib.unquote(request.path),
                    safe=':/') + '/'
            )
        )


class DAVLeafResource (DAVResource, LeafResource):
    """
    DAV resource with no children.
    """

    def findChildren(
        self, depth, request, callback,
        privileges=None, inherited_aces=None
    ):
        return succeed(None)


class DAVPrincipalResource (DAVResource):
    """
    Resource representing a WebDAV principal.  (RFC 3744, section 2)
    """
    implements(IDAVPrincipalResource)

    ##
    # WebDAV
    ##

    def liveProperties(self):

        return super(DAVPrincipalResource, self).liveProperties() + (
            (dav_namespace, "alternate-URI-set"),
            (dav_namespace, "principal-URL"),
            (dav_namespace, "group-member-set"),
            (dav_namespace, "group-membership"),
        )

    def davComplianceClasses(self):
        return ("1", "access-control",)

    def isCollection(self):
        return False

    def readProperty(self, property, request):
        def defer():
            if type(property) is tuple:
                qname = property
            else:
                qname = property.qname()

            namespace, name = qname

            if namespace == dav_namespace:
                if name == "alternate-URI-set":
                    return element.AlternateURISet(*[
                        element.HRef(u) for u in self.alternateURIs()
                    ])

                if name == "principal-URL":
                    return element.PrincipalURL(
                        element.HRef(self.principalURL())
                    )

                if name == "group-member-set":
                    def callback(members):
                        return element.GroupMemberSet(*[
                            element.HRef(p.principalURL())
                            for p in members
                        ])

                    d = self.groupMembers()
                    d.addCallback(callback)
                    return d

                if name == "group-membership":
                    def callback(memberships):
                        return element.GroupMembership(*[
                            element.HRef(g.principalURL())
                            for g in memberships
                        ])

                    d = self.groupMemberships()
                    d.addCallback(callback)
                    return d

                if name == "resourcetype":
                    if self.isCollection():
                        return element.ResourceType(
                            element.Collection(),
                            element.Principal()
                        )
                    else:
                        return element.ResourceType(element.Principal())

            return super(DAVPrincipalResource, self).readProperty(
                qname, request
            )

        return maybeDeferred(defer)

    ##
    # ACL
    ##

    def alternateURIs(self):
        """
        See L{IDAVPrincipalResource.alternateURIs}.

        This implementation returns C{()}.  Subclasses should override
        this method to provide alternate URIs for this resource if
        appropriate.
        """
        return ()

    def principalURL(self):
        """
        See L{IDAVPrincipalResource.principalURL}.

        This implementation raises L{NotImplementedError}.  Subclasses
        must override this method to provide the principal URL for
        this resource.
        """
        unimplemented(self)

    def principalElement(self):
        """
        See L{IDAVPrincipalResource.principalURL}.

        This implementation raises L{NotImplementedError}.  Subclasses
        must override this method to provide the principal URL for
        this resource.
        """
        return element.Principal(element.HRef.fromString(self.principalURL()))

    def groupMembers(self):
        """
        This implementation returns a Deferred which fires with C{()},
        which is appropriate for non-group principals.  Subclasses
        should override this method to provide member URLs for this
        resource if appropriate.

        @see: L{IDAVPrincipalResource.groupMembers}.
        """
        return succeed(())

    def expandedGroupMembers(self):
        """
        This implementation returns a Deferred which fires with C{()},
        which is appropriate for non-group principals.  Subclasses
        should override this method to provide expanded member URLs
        for this resource if appropriate.

        @see: L{IDAVPrincipalResource.expandedGroupMembers}
        """
        return succeed(())

    def groupMemberships(self):
        """
        See L{IDAVPrincipalResource.groupMemberships}.

        This implementation raises L{NotImplementedError}.  Subclasses
        must override this method to provide the group URLs for this
        resource.
        """
        unimplemented(self)

    def principalMatch(self, href):
        """
        Check whether the supplied principal matches this principal or
        is a member of this principal resource.
        @param href: the L{HRef} to test.
        @return: True if there is a match, False otherwise.
        """
        uri = str(href)
        if self.principalURL() == uri:
            return succeed(True)
        else:
            d = self.expandedGroupMembers()
            d.addCallback(
                lambda members:
                    uri in [member.principalURL() for member in members]
            )
            return d

    @inlineCallbacks
    def containsPrincipal(self, principal):
        """
        Is the given principal contained within our expanded group membership?

        @param principal: The principal to check
        @type principal: L{DirectoryCalendarPrincipalResource}
        @return: True if principal is a member, False otherwise
        @rtype: C{boolean}
        """
        members = yield self.expandedGroupMembers()
        returnValue(principal in members)


class DAVPrincipalCollectionResource (DAVResource):
    """
    WebDAV principal collection resource.  (RFC 3744, section 5.8)

    This is an abstract class; subclasses must implement
    C{principalForUser} in order to properly implement it.
    """

    implements(IDAVPrincipalCollectionResource)

    def __init__(self, url, principalCollections=()):
        """
        @param url: This resource's URL.
        """
        DAVResource.__init__(self, principalCollections=principalCollections)

        assert url.endswith("/"), "Collection URL must end in '/'"
        self._url = url

    def principalCollectionURL(self):
        """
        Return the URL for this principal collection.
        """
        return self._url

    def principalForUser(self, user):
        """
        Subclasses must implement this method.

        @see: L{IDAVPrincipalCollectionResource.principalForUser}

        @raise: L{NotImplementedError}
        """
        raise NotImplementedError(
            "%s did not implement principalForUser" % (self.__class__)
        )


class AccessDeniedError(Exception):

    def __init__(self, errors):
        """
        An error to be raised when some request fails to meet
        sufficient access privileges for a resource.

        @param errors: sequence of tuples, one for each resource for
            which one or more of the given privileges are not granted,
            in the form C{(uri, privileges)}, where uri is a URL path
            relative to resource or C{None} if the error was in this
            resource, privileges is a sequence of the privileges which
            are not granted a subset thereof.
        """
        Exception.__init__(self, "Access denied for some resources: %r"
                           % (errors,))
        self.errors = errors


##
# Utilities
##

def isPrincipalResource(resource):
    try:
        resource = IDAVPrincipalResource(resource)
    except TypeError:
        return False
    else:
        return True


class TwistedACLInheritable (WebDAVEmptyElement):
    """
    When set on an ACE, this indicates that the ACE privileges should
    be inherited by all child resources within the resource with this
    ACE.
    """
    namespace = twisted_dav_namespace
    name = "inheritable"

registerElement(TwistedACLInheritable)
element.ACE.allowed_children[(twisted_dav_namespace, "inheritable")] = (0, 1)


class TwistedGETContentMD5 (WebDAVTextElement):
    """
    MD5 hash of the resource content.
    """
    namespace = twisted_dav_namespace
    name = "getcontentmd5"

registerElement(TwistedGETContentMD5)


class TwistedQuotaRootProperty (WebDAVTextElement):
    """
    When set on a collection, this property indicates that the
    collection has a quota limit for the size of all resources stored
    in the collection (and any associate meta-data such as
    properties).  The value is a number - the maximum size in bytes
    allowed.
    """
    namespace = twisted_private_namespace
    name = "quota-root"

registerElement(TwistedQuotaRootProperty)


class TwistedQuotaUsedProperty (WebDAVTextElement):
    """
    When set on a collection, this property contains the cached
    running total of the size of all resources stored in the
    collection (and any associate meta-data such as properties).  The
    value is a number - the size in bytes used.
    """
    namespace = twisted_private_namespace
    name = "quota-used"

registerElement(TwistedQuotaUsedProperty)

allACL = element.ACL(
    element.ACE(
        element.Principal(element.All()),
        element.Grant(element.Privilege(element.All())),
        element.Protected(),
        TwistedACLInheritable()
    )
)

readonlyACL = element.ACL(
    element.ACE(
        element.Principal(element.All()),
        element.Grant(element.Privilege(element.Read())),
        element.Protected(),
        TwistedACLInheritable()
    )
)

allPrivilegeSet = element.SupportedPrivilegeSet(
    element.SupportedPrivilege(
        element.Privilege(element.All()),
        element.Description("all privileges", **{"xml:lang": "en"})
    )
)

#
# This is one possible graph of the "standard" privileges documented
# in 3744, section 3.
#
davPrivilegeSet = element.SupportedPrivilegeSet(
    element.SupportedPrivilege(
        element.Privilege(element.All()),
        element.Description(
            "all privileges",
            **{"xml:lang": "en"}
        ),
        element.SupportedPrivilege(
            element.Privilege(element.Read()),
            element.Description(
                "read resource",
                **{"xml:lang": "en"}
            ),
        ),
        element.SupportedPrivilege(
            element.Privilege(element.Write()),
            element.Description(
                "write resource",
                **{"xml:lang": "en"}
            ),
            element.SupportedPrivilege(
                element.Privilege(element.WriteProperties()),
                element.Description(
                    "write resource properties",
                    **{"xml:lang": "en"}
                ),
            ),
            element.SupportedPrivilege(
                element.Privilege(element.WriteContent()),
                element.Description(
                    "write resource content",
                    **{"xml:lang": "en"}
                ),
            ),
            element.SupportedPrivilege(
                element.Privilege(element.Bind()),
                element.Description(
                    "add child resource",
                    **{"xml:lang": "en"}
                ),
            ),
            element.SupportedPrivilege(
                element.Privilege(element.Unbind()),
                element.Description(
                    "remove child resource",
                    **{"xml:lang": "en"}
                ),
            ),
        ),
        element.SupportedPrivilege(
            element.Privilege(element.Unlock()),
            element.Description(
                "unlock resource without ownership of lock",
                **{"xml:lang": "en"}
            ),
        ),
        element.SupportedPrivilege(
            element.Privilege(element.ReadACL()),
            element.Description(
                "read resource access control list",
                **{"xml:lang": "en"}
            ),
        ),
        element.SupportedPrivilege(
            element.Privilege(element.WriteACL()),
            element.Description(
                "write resource access control list",
                **{"xml:lang": "en"}
            ),
        ),
        element.SupportedPrivilege(
            element.Privilege(element.ReadCurrentUserPrivilegeSet()),
            element.Description(
                "read privileges for current principal",
                **{"xml:lang": "en"}
            ),
        ),
    ),
)

unauthenticatedPrincipal = element.Principal(element.Unauthenticated())


class ResourceClass (WebDAVTextElement):
    namespace = twisted_dav_namespace
    name = "resource-class"
    hidden = False
