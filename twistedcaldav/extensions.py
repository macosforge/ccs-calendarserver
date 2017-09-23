# -*- test-case-name: twistedcaldav.test.test_extensions -*-
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

from __future__ import print_function

"""
Extensions to web2.dav
"""

__all__ = [
    "DAVResource",
    "DAVResourceWithChildrenMixin",
    "DAVPrincipalResource",
    "DAVFile",
    "ReadOnlyWritePropertiesResourceMixIn",
    "ReadOnlyResourceMixIn",
    "CachingPropertyStore",
]

import urllib
import time
from itertools import cycle

from twisted.internet.defer import succeed, maybeDeferred
from twisted.internet.defer import inlineCallbacks, returnValue

from twisted.web.template import Element, XMLFile, renderer, tags, flattenString
from twisted.python.modules import getModule

from txweb2 import responsecode, server
from txweb2.http import HTTPError, Response, RedirectResponse
from txweb2.http import StatusResponse
from txweb2.http_headers import MimeType
from txweb2.stream import FileStream
from txweb2.static import MetaDataMixin, StaticRenderMixin
from txdav.xml import element
from txdav.xml.base import encodeXMLName
from txdav.xml.element import dav_namespace
from txweb2.dav.http import MultiStatusResponse
from txweb2.dav.static import DAVFile as SuperDAVFile
from txweb2.dav.resource import DAVResource as SuperDAVResource
from txweb2.dav.resource import (
    DAVPrincipalResource as SuperDAVPrincipalResource
)
from twisted.internet.defer import gatherResults
from txweb2.dav.method import prop_common

from twext.python.log import Logger

from twistedcaldav import customxml
from twistedcaldav.customxml import calendarserver_namespace

from twistedcaldav.method.report import http_REPORT

from twistedcaldav.config import config
from txdav.who.directory import CalendarDirectoryRecordMixin
from twext.who.expression import Operand, MatchType, MatchFlags


thisModule = getModule(__name__)

log = Logger()


class WebDAVServerInfoMixIn(object):

    def renderHTTP(self, request):
        def _addServerInfoURL(request, response):
            # server-info-token processing
            if config.EnableServerInfo and (
                request.headers.hasHeader("server-info-token") and request.headers.getRawHeaders("server-info-token")[0] != config.ServerInfoToken or
                not request.headers.hasHeader("server-info-token") and request.method == "OPTIONS"
            ):
                uri = request.unparseURL(path="/server-info")
                response.headers.setRawHeaders("server-info-url", (uri,))
            return response
        request.addResponseFilter(_addServerInfoURL, onlyOnce=True)
        return super(WebDAVServerInfoMixIn, self).renderHTTP(request)


class DirectoryPrincipalPropertySearchMixIn(object):

    @inlineCallbacks
    def report_DAV__principal_property_search(
        self, request,
        principal_property_search
    ):
        """
        Generate a principal-property-search REPORT. (RFC 3744, section 9.4)
        Overrides twisted implementation, targeting only directory-enabled
        searching.
        """
        # Verify root element
        if not isinstance(principal_property_search, element.PrincipalPropertySearch):
            msg = "%s expected as root element, not %s." % (element.PrincipalPropertySearch.sname(), principal_property_search.sname())
            log.warn(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Should we AND (the default) or OR (if test="anyof")?
        testMode = principal_property_search.attributes.get("test", "allof")
        if testMode not in ("allof", "anyof"):
            msg = "Bad XML: unknown value for test attribute: %s" % (testMode,)
            log.warn(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))
        operand = Operand.AND if testMode == "allof" else Operand.OR

        # Are we narrowing results down to a single CUTYPE?
        cuType = principal_property_search.attributes.get("type", None)
        if cuType not in ("INDIVIDUAL", "GROUP", "RESOURCE", "ROOM", None):
            msg = "Bad XML: unknown value for type attribute: %s" % (cuType,)
            log.warn(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Only handle Depth: 0
        depth = request.headers.getHeader("depth", "0")
        if depth != "0":
            log.error("Error in principal-property-search REPORT, Depth set to {depth}", depth=depth)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, "Depth %s not allowed" % (depth,)))

        # Get any limit value from xml
        clientLimit = None

        # Get a single DAV:prop element from the REPORT request body
        propertiesForResource = None
        propElement = None
        propertySearches = []
        applyTo = False
        for child in principal_property_search.children:
            if child.qname() == (dav_namespace, "prop"):
                propertiesForResource = prop_common.propertyListForResource
                propElement = child

            elif child.qname() == (
                dav_namespace,
                "apply-to-principal-collection-set"
            ):
                applyTo = True

            elif child.qname() == (dav_namespace, "property-search"):
                props = child.childOfType(element.PropertyContainer)
                props.removeWhitespaceNodes()

                match = child.childOfType(element.Match)
                caseless = match.attributes.get("caseless", "yes")
                if caseless not in ("yes", "no"):
                    msg = "Bad XML: unknown value for caseless attribute: %s" % (caseless,)
                    log.warn(msg)
                    raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))
                caseless = (caseless == "yes")
                matchType = match.attributes.get("match-type", u"contains").encode("utf-8")
                if matchType not in ("starts-with", "contains", "equals"):
                    msg = "Bad XML: unknown value for match-type attribute: %s" % (matchType,)
                    log.warn(msg)
                    raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

                # Convert to twext.who.expression form
                matchType = {
                    "starts-with": MatchType.startsWith,
                    "contains": MatchType.contains,
                    "equals": MatchType.equals
                }.get(matchType)
                matchFlags = MatchFlags.caseInsensitive if caseless else MatchFlags.none

                # Ignore any query strings under three letters
                matchText = match.toString()  # gives us unicode
                if len(matchText) >= 3:
                    propertySearches.append((props.children, matchText, matchFlags, matchType))

            elif child.qname() == (calendarserver_namespace, "limit"):
                try:
                    nresults = child.childOfType(customxml.NResults)
                    clientLimit = int(str(nresults))
                except (TypeError, ValueError,):
                    msg = "Bad XML: unknown value for <limit> element"
                    log.warn(msg)
                    raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Run report
        resultsWereLimited = None
        resources = []
        if applyTo or not hasattr(self, "directory"):
            for principalCollection in self.principalCollections():
                uri = principalCollection.principalCollectionURL()
                resource = (yield request.locateResource(uri))
                if resource:
                    resources.append((resource, uri))
        else:
            resources.append((self, request.uri))

        # We need to access a directory service
        principalCollection = resources[0][0]
        if not hasattr(principalCollection, "directory"):
            # Use Twisted's implementation instead in this case
            result = (yield super(DirectoryPrincipalPropertySearchMixIn, self).report_DAV__principal_property_search(request, principal_property_search))
            returnValue(result)

        dir = principalCollection.directory

        # See if we can take advantage of the directory
        fields = []
        nonDirectorySearches = []
        for props, match, matchFlags, matchType in propertySearches:
            nonDirectoryProps = []
            for prop in props:
                try:
                    fieldName, match = principalCollection.propertyToField(
                        prop, match)
                except ValueError as e:
                    raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(e)))
                if fieldName:
                    fields.append((fieldName, match, matchFlags, matchType))
                else:
                    nonDirectoryProps.append(prop)
            if nonDirectoryProps:
                nonDirectorySearches.append((
                    nonDirectoryProps, match,
                    matchFlags, matchType
                ))

        matchingResources = []
        matchcount = 0

        # nonDirectorySearches are ignored
        if fields:

            if cuType:
                recordType = CalendarDirectoryRecordMixin.fromCUType(cuType)
            else:
                recordType = None

            records = (
                yield dir.recordsMatchingFields(
                    fields,
                    operand=operand,
                    recordType=recordType,
                    limitResults=clientLimit,
                    timeoutSeconds=config.PrincipalSearchReportTimeout
                )
            )

            for record in records:
                resource = yield principalCollection.principalForRecord(record)
                if resource:
                    matchingResources.append(resource)

                    # We've determined this is a matching resource
                    matchcount += 1
                    if clientLimit is not None and matchcount >= clientLimit:
                        resultsWereLimited = ("client", matchcount)
                        break
                    if matchcount >= config.MaxPrincipalSearchReportResults:
                        resultsWereLimited = ("server", matchcount)
                        break

        # Generate the response
        responses = []
        for resource in matchingResources:
            url = resource.url()
            yield prop_common.responseForHref(
                request,
                responses,
                element.HRef.fromString(url),
                resource,
                propertiesForResource,
                propElement
            )

        if resultsWereLimited is not None:
            if resultsWereLimited[0] == "server":
                log.debug("Too many matching resources in principal-property-search report")
            responses.append(element.StatusResponse(
                element.HRef.fromString(request.uri),
                element.Status.fromResponseCode(
                    responsecode.INSUFFICIENT_STORAGE_SPACE
                ),
                element.Error(element.NumberOfMatchesWithinLimits()),
                element.ResponseDescription(
                    "Results limited by %s at %d"
                    % resultsWereLimited
                ),
            ))
        returnValue(MultiStatusResponse(responses))

    @inlineCallbacks
    def report_http___calendarserver_org_ns__calendarserver_principal_search(
        self, request,
        calendarserver_principal_search
    ):
        """
        Generate a calendarserver-principal-search REPORT.

        @param request: Request object
        @param calendarserver_principal_search: CalendarServerPrincipalSearch object
        """

        # Verify root element
        if not isinstance(calendarserver_principal_search, customxml.CalendarServerPrincipalSearch):
            msg = "%s expected as root element, not %s." % (customxml.CalendarServerPrincipalSearch.sname(), calendarserver_principal_search.sname())
            log.warn(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Only handle Depth: 0
        depth = request.headers.getHeader("depth", "0")
        if depth != "0":
            log.error("Error in calendarserver-principal-search REPORT, Depth set to {depth}", depth=depth)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, "Depth %s not allowed" % (depth,)))

        tokens, context, applyTo, clientLimit, propElement = extractCalendarServerPrincipalSearchData(calendarserver_principal_search)

        if not validateTokens(tokens):
            raise HTTPError(StatusResponse(
                responsecode.FORBIDDEN,
                "Insufficient search token length"
            ))

        # Run report
        resultsWereLimited = None
        resources = []
        if applyTo or not hasattr(self, "directory"):
            for principalCollection in self.principalCollections():
                uri = principalCollection.principalCollectionURL()
                resource = (yield request.locateResource(uri))
                if resource:
                    resources.append((resource, uri))
        else:
            resources.append((self, request.uri))

        # We need to access a directory service
        principalCollection = resources[0][0]
        dir = principalCollection.directory

        matchingResources = []
        matchcount = 0

        limitResults = config.MaxPrincipalSearchReportResults
        if clientLimit is not None:
            limitResults = min(clientLimit, limitResults)

        records = (yield dir.recordsMatchingTokens(
            tokens,
            context=context,
            limitResults=limitResults,
            timeoutSeconds=config.PrincipalSearchReportTimeout
        ))

        for record in records:
            resource = yield principalCollection.principalForRecord(record)
            if resource:
                matchingResources.append(resource)

                # We've determined this is a matching resource
                matchcount += 1
                if clientLimit is not None and matchcount >= clientLimit:
                    resultsWereLimited = ("client", matchcount)
                    break
                if matchcount >= config.MaxPrincipalSearchReportResults:
                    resultsWereLimited = ("server", matchcount)
                    break

        # Generate the response
        responses = []
        for resource in matchingResources:
            url = resource.url()
            yield prop_common.responseForHref(
                request,
                responses,
                element.HRef.fromString(url),
                resource,
                prop_common.propertyListForResource,
                propElement
            )

        if resultsWereLimited is not None:
            if resultsWereLimited[0] == "server":
                log.debug("Too many matching resources in calendarserver-principal-search report")
            responses.append(element.StatusResponse(
                element.HRef.fromString(request.uri),
                element.Status.fromResponseCode(
                    responsecode.INSUFFICIENT_STORAGE_SPACE
                ),
                element.Error(element.NumberOfMatchesWithinLimits()),
                element.ResponseDescription(
                    "Results limited by %s at %d"
                    % resultsWereLimited
                ),
            ))
        returnValue(MultiStatusResponse(responses))


class DirectoryElement(Element):
    """
    A L{DirectoryElement} is an L{Element} for rendering the contents of a
    L{DirectoryRenderingMixIn} resource as HTML.
    """

    loader = XMLFile(
        thisModule.filePath.sibling("directory-listing.html")
    )

    def __init__(self, resource):
        """
        @param resource: the L{DirectoryRenderingMixIn} resource being
            listed.
        """
        super(DirectoryElement, self).__init__()
        self.resource = resource

    @renderer
    def resourceDetail(self, request, tag):
        """
        Renderer which returns a distinct element for this resource's data.
        Subclasses should override.
        """
        return ''

    @renderer
    def children(self, request, tag):
        """
        Renderer which yields all child object tags as table rows.
        """
        whenChildren = (
            maybeDeferred(self.resource.listChildren)
            .addCallback(sorted)
            .addCallback(
                lambda names: gatherResults(
                    [maybeDeferred(self.resource.getChild, x) for x in names]
                )
                .addCallback(lambda children: zip(children, names))
            )
        )

        @whenChildren.addCallback
        def gotChildren(children):
            for even, [child, name] in zip(cycle(["odd", "even"]), children):
                [url, name, size, lastModified, contentType] = map(
                    str, self.resource.getChildDirectoryEntry(
                        child, name, request)
                )
                yield tag.clone().fillSlots(
                    url=url, name=name, size=str(size),
                    lastModified=lastModified, even=even, type=contentType,
                )
        return whenChildren

    @renderer
    def main(self, request, tag):
        """
        Main renderer; fills slots for title, etc.
        """
        return tag.fillSlots(name=request.path)

    @renderer
    def properties(self, request, tag):
        """
        Renderer which yields all properties as table row tags.
        """
        whenPropertiesListed = self.resource.listProperties(request)

        @whenPropertiesListed.addCallback
        def gotProperties(qnames):
            accessDeniedValue = object()

            def gotError(f, name):
                f.trap(HTTPError)
                code = f.value.response.code
                if code == responsecode.NOT_FOUND:
                    log.error(
                        "Property {name} was returned by listProperties() "
                        "but does not exist for resource {rsrc!r}.",
                        name=name, rsrc=self.resource
                    )
                    return (name, None)
                if code == responsecode.UNAUTHORIZED:
                    return (name, accessDeniedValue)
                return f

            whenAllProperties = gatherResults([
                maybeDeferred(self.resource.readProperty, qn, request)
                .addCallback(lambda p, iqn=qn: (p.sname(), p.toxml())
                             if p is not None else (encodeXMLName(*iqn), None))
                .addErrback(gotError, encodeXMLName(*qn))
                for qn in sorted(qnames)
            ])

            @whenAllProperties.addCallback
            def gotValues(items):
                for even, [name, value] in zip(cycle(["odd", "even"]), items):
                    if value is None:
                        value = tags.i("(no value)")
                    elif value is accessDeniedValue:
                        value = tags.i("(access forbidden)")
                    yield tag.clone().fillSlots(
                        even=even, name=name, value=value,
                    )
            return whenAllProperties
        return whenPropertiesListed


class DirectoryRenderingMixIn(object):

    def renderDirectory(self, request):
        """
        Render a directory listing.
        """
        def gotBody(output):
            mime_params = {"charset": "utf-8"}
            response = Response(200, {}, output)
            response.headers.setHeader(
                "content-type",
                MimeType("text", "html", mime_params)
            )
            return response
        return flattenString(request, self.htmlElement()).addCallback(gotBody)

    def htmlElement(self):
        """
        Create a L{DirectoryElement} or appropriate subclass for rendering this
        resource.
        """
        return DirectoryElement(self)

    def getChildDirectoryEntry(self, child, name, request):
        def orNone(value, default="?", f=None):
            if value is None:
                return default
            elif f is not None:
                return f(value)
            else:
                return value

        url = urllib.quote(name, '/')
        if isinstance(child, DAVResource) and child.isCollection():
            url += "/"
            name += "/"

        if isinstance(child, MetaDataMixin):
            size = child.contentLength()
            lastModified = child.lastModified()
            rtypes = []
            fullrtype = child.resourceType() if hasattr(child, "resourceType") else None
            if fullrtype is not None:
                for rtype in fullrtype.children:
                    rtypes.append(rtype.name)
            if rtypes:
                rtypes = "(%s)" % (", ".join(rtypes),)
            if child.isCollection() if hasattr(child, "isCollection") else False:
                contentType = rtypes
            else:
                mimeType = child.contentType()
                if mimeType is None:
                    print('BAD contentType() IMPLEMENTATION', child)
                    contentType = 'application/octet-stream'
                else:
                    contentType = "%s/%s" % (mimeType.mediaType, mimeType.mediaSubtype)
                if rtypes:
                    contentType += " %s" % (rtypes,)
        else:
            size = None
            lastModified = None
            contentType = None
            if hasattr(child, "resourceType"):
                rtypes = []
                fullrtype = child.resourceType()
                for rtype in fullrtype.children:
                    rtypes.append(rtype.name)
                if rtypes:
                    contentType = "(%s)" % (", ".join(rtypes),)

        return (
            url,
            name,
            orNone(size),
            orNone(
                lastModified,
                default="",
                f=lambda t: time.strftime("%Y-%b-%d %H:%M", time.localtime(t))
            ),
            contentType,
        )


class DAVResource (
    WebDAVServerInfoMixIn,
    DirectoryPrincipalPropertySearchMixIn,
    SuperDAVResource,
    DirectoryRenderingMixIn,
    StaticRenderMixin
):
    """
    Extended L{txweb2.dav.resource.DAVResource} implementation.

    Note we add StaticRenderMixin as a base class because we need all the etag etc behavior
    that is currently in static.py but is actually applicable to any type of resource.
    """
    log = Logger()

    http_REPORT = http_REPORT

    def davComplianceClasses(self):
        return ("1", "access-control")  # Add "2" when we have locking

    def render(self, request):
        if not self.exists():
            return responsecode.NOT_FOUND

        if self.isCollection():
            return self.renderDirectory(request)
        return super(DAVResource, self).render(request)

    def resourceType(self):
        # Allow live property to be overridden by dead property
        if self.deadProperties().contains((dav_namespace, "resourcetype")):
            return self.deadProperties().get((dav_namespace, "resourcetype"))
        return element.ResourceType(element.Collection()) if self.isCollection() else element.ResourceType()

    def contentType(self):
        return MimeType("httpd", "unix-directory") if self.isCollection() else None


class DAVResourceWithChildrenMixin (object):
    """
    Bits needed from txweb2.static
    """

    def __init__(self, principalCollections=None):
        self.putChildren = {}
        super(DAVResourceWithChildrenMixin, self).__init__(principalCollections=principalCollections)

    def putChild(self, name, child):
        """
        Register a child with the given name with this resource.
        @param name: the name of the child (a URI path segment)
        @param child: the child to register
        """
        self.putChildren[name] = child

    def getChild(self, name):
        """
        Look up a child resource.  First check C{self.putChildren}, then call
        C{self.makeChild} if no pre-existing children were found.

        @return: the child of this resource with the given name.
        """
        if name == "":
            return self

        result = self.putChildren.get(name, None)
        if not result:
            result = self.makeChild(name)
        return result

    def makeChild(self, name):
        """
        Called by L{DAVResourceWithChildrenMixin.getChild} to dynamically
        create children that have not been pre-created with C{putChild}.
        """
        return None

    def listChildren(self):
        """
        @return: a sequence of the names of all known children of this resource.
        """
        return self.putChildren.keys()

    def countChildren(self):
        """
        @return: the number of all known children of this resource.
        """
        return len(self.putChildren.keys())

    def locateChild(self, req, segments):
        """
        See L{IResource.locateChild}.
        """
        thisSegment = segments[0]
        moreSegments = segments[1:]
        return maybeDeferred(self.getChild, thisSegment).addCallback(
            lambda it: (it, moreSegments)
        )


class DAVResourceWithoutChildrenMixin (object):
    """
    Bits needed from txweb2.static
    """

    def __init__(self, principalCollections=None):
        self.putChildren = {}
        super(DAVResourceWithChildrenMixin, self).__init__(principalCollections=principalCollections)

    def findChildren(
        self, depth, request, callback,
        privileges=None, inherited_aces=None
    ):
        return succeed(None)

    def locateChild(self, request, segments):
        return self, server.StopTraversal


class DAVPrincipalResource (
    WebDAVServerInfoMixIn,
    DirectoryPrincipalPropertySearchMixIn,
    SuperDAVPrincipalResource,
    DirectoryRenderingMixIn
):
    """
    Extended L{txweb2.dav.static.DAVFile} implementation.
    """
    log = Logger()

    def liveProperties(self):
        return super(DAVPrincipalResource, self).liveProperties() + (
            (calendarserver_namespace, "expanded-group-member-set"),
            (calendarserver_namespace, "expanded-group-membership"),
            (calendarserver_namespace, "record-type"),
        )

    http_REPORT = http_REPORT

    def render(self, request):
        if not self.exists():
            return responsecode.NOT_FOUND

        if self.isCollection():
            return self.renderDirectory(request)
        return super(DAVResource, self).render(request)

    @inlineCallbacks
    def readProperty(self, property, request):
        if type(property) is tuple:
            qname = property
        else:
            qname = property.qname()

        namespace, name = qname

        if namespace == dav_namespace:
            if name == "resourcetype":
                rtype = self.resourceType()
                returnValue(rtype)

        elif namespace == calendarserver_namespace:
            if name == "expanded-group-member-set":
                principals = (yield self.expandedGroupMembers())
                returnValue(customxml.ExpandedGroupMemberSet(
                    *[element.HRef(p.principalURL()) for p in principals]
                ))

            elif name == "expanded-group-membership":
                principals = (yield self.expandedGroupMemberships())
                returnValue(customxml.ExpandedGroupMembership(
                    *[element.HRef(p.principalURL()) for p in principals]
                ))

            elif name == "record-type":
                if hasattr(self, "record"):
                    returnValue(
                        customxml.RecordType(
                            self.record.service.recordTypeToOldName(
                                self.record.recordType
                            )
                        )
                    )
                else:
                    raise HTTPError(StatusResponse(
                        responsecode.NOT_FOUND,
                        "Property %s does not exist." % (qname,)
                    ))

        result = (yield super(DAVPrincipalResource, self).readProperty(property, request))
        returnValue(result)

    def groupMembers(self):
        return succeed(())

    def expandedGroupMembers(self):
        return succeed(())

    def groupMemberships(self):
        return succeed(())

    def expandedGroupMemberships(self):
        return succeed(())

    def resourceType(self):
        # Allow live property to be overridden by dead property
        if self.deadProperties().contains((dav_namespace, "resourcetype")):
            return self.deadProperties().get((dav_namespace, "resourcetype"))
        if self.isCollection():
            return element.ResourceType(element.Principal(), element.Collection())
        else:
            return element.ResourceType(element.Principal())


class DAVFile (WebDAVServerInfoMixIn, SuperDAVFile, DirectoryRenderingMixIn):
    """
    Extended L{txweb2.dav.static.DAVFile} implementation.
    """
    log = Logger()

    def resourceType(self):
        # Allow live property to be overridden by dead property
        if self.deadProperties().contains((dav_namespace, "resourcetype")):
            return self.deadProperties().get((dav_namespace, "resourcetype"))
        if self.isCollection():
            return element.ResourceType.collection  # @UndefinedVariable
        return element.ResourceType.empty  # @UndefinedVariable

    def render(self, request):
        if not self.fp.exists():
            return responsecode.NOT_FOUND

        if self.fp.isdir():
            if request.path[-1] != "/":
                # Redirect to include trailing '/' in URI
                return RedirectResponse(request.unparseURL(path=urllib.quote(urllib.unquote(request.path), safe=':/') + '/'))
            else:
                ifp = self.fp.childSearchPreauth(*self.indexNames)
                if ifp:
                    # Render from the index file
                    return self.createSimilarFile(ifp.path).render(request)

                return self.renderDirectory(request)

        try:
            f = self.fp.open()
        except IOError as e:
            import errno
            if e[0] == errno.EACCES:
                return responsecode.FORBIDDEN
            elif e[0] == errno.ENOENT:
                return responsecode.NOT_FOUND
            else:
                raise

        response = Response()
        response.stream = FileStream(f, 0, self.fp.getsize())

        for (header, value) in (
            ("content-type", self.contentType()),
            ("content-encoding", self.contentEncoding()),
        ):
            if value is not None:
                response.headers.setHeader(header, value)

        return response


class ReadOnlyWritePropertiesResourceMixIn (object):
    """
    Read only that will allow writing of properties resource.
    """
    readOnlyResponse = StatusResponse(
        responsecode.FORBIDDEN,
        "Resource is read only."
    )

    def _forbidden(self, request):
        return self.readOnlyResponse

    http_DELETE = _forbidden
    http_MOVE = _forbidden
    http_PUT = _forbidden


class ReadOnlyResourceMixIn (ReadOnlyWritePropertiesResourceMixIn):
    """
    Read only resource.
    """
    http_PROPPATCH = ReadOnlyWritePropertiesResourceMixIn._forbidden

    def writeProperty(self, property, request):
        raise HTTPError(self.readOnlyResponse)

    def accessControlList(
        self, request, inheritance=True, expanding=False, inherited_aces=None
    ):
        # Permissions here are fixed, and are not subject to
        # inheritance rules, etc.
        return self.defaultAccessControlList()


class PropertyNotFoundError (HTTPError):

    def __init__(self, qname):
        HTTPError.__init__(
            self,
            StatusResponse(
                responsecode.NOT_FOUND,
                "No such property: %s" % encodeXMLName(*qname)
            )
        )


class CachingPropertyStore (object):
    """
    DAV property store using a dict in memory on top of another
    property store implementation.
    """
    log = Logger()

    def __init__(self, propertyStore):
        self.propertyStore = propertyStore
        self.resource = propertyStore.resource

    def get(self, qname, uid=None):
        # self.log.debug("Get: {p}, {n}", p=self.resource.fp.path, n=qname)

        cache = self._cache()

        cachedQname = qname + (uid,)

        if cachedQname in cache:
            property = cache.get(cachedQname, None)
            if property is None:
                self.log.debug("Cache miss: {s!r}, {p}, {n}", s=self, p=self.resource.fp.path, n=qname)
                try:
                    property = self.propertyStore.get(qname, uid)
                except HTTPError:
                    del cache[cachedQname]
                    raise PropertyNotFoundError(qname)
                cache[cachedQname] = property

            return property
        else:
            raise PropertyNotFoundError(qname)

    def set(self, property, uid=None):
        # self.log.debug("Set: {p}, {prop!r}", p=self.resource.fp.path, prop=property)

        cache = self._cache()

        cachedQname = property.qname() + (uid,)

        cache[cachedQname] = None
        self.propertyStore.set(property, uid)
        cache[cachedQname] = property

    def contains(self, qname, uid=None):
        # self.log.debug("Contains: {p}, {n}", p=self.resource.fp.path, n=qname)

        cachedQname = qname + (uid,)

        try:
            cache = self._cache()
        except HTTPError as e:
            if e.response.code == responsecode.NOT_FOUND:
                return False
            else:
                raise

        if cachedQname in cache:
            # self.log.debug("Contains cache hit: {s!r}, {p}, {n}", s=self, p=self.resource.fp.path, n=qname)
            return True
        else:
            return False

    def delete(self, qname, uid=None):
        # self.log.debug("Delete: {p}, {n}", p=self.resource.fp.path, n=qname)

        cachedQname = qname + (uid,)

        if self._data is not None and cachedQname in self._data:
            del self._data[cachedQname]

        self.propertyStore.delete(qname, uid)

    def list(self, uid=None, filterByUID=True):
        # self.log.debug("List: {p}", p=self.resource.fp.path)
        keys = self._cache().iterkeys()
        if filterByUID:
            return [
                (namespace, name)
                for namespace, name, propuid in keys
                if propuid == uid
            ]
        else:
            return keys

    def _cache(self):
        if not hasattr(self, "_data"):
            # self.log.debug("Cache init: {p}", p=self.resource.fp.path)
            self._data = dict(
                (name, None)
                for name in self.propertyStore.list(filterByUID=False)
            )
        return self._data


def extractCalendarServerPrincipalSearchData(doc):
    """
    Extract relevant info from a CalendarServerPrincipalSearch document

    @param doc: CalendarServerPrincipalSearch object to extract info from
    @return: A tuple containing:
        the list of tokens
        the context string
        the applyTo boolean
        the clientLimit integer
        the propElement containing the properties to return
    """
    context = doc.attributes.get("context", None)
    applyTo = False
    tokens = []
    clientLimit = None
    for child in doc.children:
        if child.qname() == (dav_namespace, "prop"):
            propElement = child

        elif child.qname() == (
            dav_namespace,
            "apply-to-principal-collection-set"
        ):
            applyTo = True

        elif child.qname() == (calendarserver_namespace, "search-token"):
            tokenValue = child.toString().strip()
            if tokenValue:
                tokens.append(tokenValue)

        elif child.qname() == (calendarserver_namespace, "limit"):
            try:
                nresults = child.childOfType(customxml.NResults)
                clientLimit = int(str(nresults))
            except (TypeError, ValueError,):
                msg = "Bad XML: unknown value for <limit> element"
                log.warn(msg)
                raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

    return tokens, context, applyTo, clientLimit, propElement


def validateTokens(tokens):
    """
    Make sure there is at least one token longer than one character

    @param tokens: the tokens to inspect
    @type tokens: iterable of utf-8 encoded strings

    @return: True if tokens are valid, False otherwise
    @rtype: boolean
    """
    for token in tokens:
        if len(token) > 1:
            return True
    return False
