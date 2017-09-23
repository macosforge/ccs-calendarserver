# -*- test-case-name: txweb2.dav.test.test_prop.PROP.test_PROPFIND -*-
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
##

"""
WebDAV PROPFIND method
"""

__all__ = ["http_PROPFIND"]

from twisted.python.failure import Failure
from twisted.internet.defer import inlineCallbacks, returnValue
from txweb2.http import HTTPError
from txweb2 import responsecode
from txweb2.http import StatusResponse
from txdav.xml import element as davxml
from txweb2.dav.http import MultiStatusResponse, statusForFailure, \
    ErrorResponse
from txweb2.dav.util import normalizeURL, davXMLFromStream, parentForURL

from twext.python.log import Logger

log = Logger()

"""
This is a direct copy of the twisted implementation of PROPFIND except that it uses the
findChildrenFaster method to optimize child privilege checking.
"""


@inlineCallbacks
def http_PROPFIND(self, request):
    """
    Respond to a PROPFIND request. (RFC 2518, section 8.1)
    """
    if not self.exists():
        # Return 403 if parent does not allow Bind
        parentURL = parentForURL(request.uri)
        parent = (yield request.locateResource(parentURL))
        yield parent.authorize(request, (davxml.Bind(),))

        log.error("Resource not found: {s!r}", s=self)
        raise HTTPError(responsecode.NOT_FOUND)

    #
    # Check authentication and access controls
    #
    yield self.authorize(request, (davxml.Read(),))

    #
    # Read request body
    #
    try:
        doc = (yield davXMLFromStream(request.stream))
    except ValueError as e:
        log.error("Error while handling PROPFIND body: {ex}", ex=e)
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(e)))

    if doc is None:
        # No request body means get all properties.
        search_properties = "all"
    else:
        #
        # Parse request
        #
        find = doc.root_element
        if not isinstance(find, davxml.PropertyFind):
            error = ("Non-%s element in PROPFIND request body: %s"
                     % (davxml.PropertyFind.sname(), find))
            log.error("Error: {err}", err=error)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, error))

        container = find.children[0]

        if isinstance(container, davxml.AllProperties):
            # Get all properties
            search_properties = "all"
        elif isinstance(container, davxml.PropertyName):
            # Get names only
            search_properties = "names"
        elif isinstance(container, davxml.PropertyContainer):
            properties = container.children
            search_properties = properties
        else:
            raise AssertionError("Unexpected element type in %s: %s"
                                 % (davxml.PropertyFind.sname(), container))

    #
    # Generate XML output stream
    #
    request_uri = request.uri
    depth = request.headers.getHeader("depth", "infinity")

    # By policy we will never allow a depth:infinity propfind
    if depth == "infinity":
        raise HTTPError(ErrorResponse(responsecode.FORBIDDEN, davxml.PropfindFiniteDepth()))

    # Look for Prefer header first, then try Brief
    prefer = request.headers.getHeader("prefer", {})
    returnMinimal = any([key == "return" and value == "minimal" for key, value, _ignore_args in prefer])
    noRoot = any([key == "depth-noroot" and value is None for key, value, _ignore_args in prefer])
    if not returnMinimal:
        returnMinimal = request.headers.getHeader("brief", False)

    xml_responses = []

    # FIXME: take advantage of the new generative properties of findChildren

    my_url = normalizeURL(request_uri)
    if self.isCollection() and not my_url.endswith("/"):
        my_url += "/"

    # Do some optimization of access control calculation by determining any inherited ACLs outside of
    # the child resource loop and supply those to the checkPrivileges on each child.
    filtered_aces = (yield self.inheritedACEsforChildren(request))

    if depth in ("1", "infinity") and noRoot:
        resources = []
    else:
        resources = [(responsecode.OK, self, my_url)]

    yield self.findChildrenFaster(
        depth,
        request,
        lambda x, y: resources.append((responsecode.OK, x, y)),
        lambda x, y: resources.append((responsecode.FORBIDDEN, x, y)),
        None,
        lambda x: resources.append((responsecode.SERVICE_UNAVAILABLE, None, x)),
        None,
        (davxml.Read(),),
        inherited_aces=filtered_aces,
    )

    # This needed for propfind cache tracking of children changes
    if depth == "1":
        request.childCacheURIs = []

    for respcode, resource, uri in resources:
        if respcode == responsecode.OK:
            if search_properties == "names":
                try:
                    resource_properties = (yield resource.listProperties(request))
                except:
                    log.error("Unable to get properties for resource {r!r}", r=resource)
                    raise

                properties_by_status = {
                    responsecode.OK: [propertyName(p) for p in resource_properties]
                }
            else:
                properties_by_status = {
                    responsecode.OK: [],
                    responsecode.NOT_FOUND: [],
                }

                if search_properties == "all":
                    properties_to_enumerate = (yield resource.listAllprop(request))
                else:
                    properties_to_enumerate = search_properties

                for property in properties_to_enumerate:
                    has = (yield resource.hasProperty(property, request))
                    if has:
                        try:
                            resource_property = (yield resource.readProperty(property, request))
                        except:
                            f = Failure()
                            status = statusForFailure(f, "getting property: %s" % (property,))
                            if status not in properties_by_status:
                                properties_by_status[status] = []
                            if not returnMinimal or status != responsecode.NOT_FOUND:
                                properties_by_status[status].append(propertyName(property))
                        else:
                            if resource_property is not None:
                                properties_by_status[responsecode.OK].append(resource_property)
                            elif not returnMinimal and search_properties != "all":
                                properties_by_status[responsecode.NOT_FOUND].append(propertyName(property))
                    elif not returnMinimal and search_properties != "all":
                        properties_by_status[responsecode.NOT_FOUND].append(propertyName(property))

            propstats = []

            for status in properties_by_status:
                properties = properties_by_status[status]
                if not properties:
                    continue

                xml_status = davxml.Status.fromResponseCode(status)
                xml_container = davxml.PropertyContainer(*properties)
                xml_propstat = davxml.PropertyStatus(xml_container, xml_status)

                propstats.append(xml_propstat)

            # Always need to have at least one propstat present (required by Prefer header behavior)
            if len(propstats) == 0:
                propstats.append(davxml.PropertyStatus(
                    davxml.PropertyContainer(),
                    davxml.Status.fromResponseCode(responsecode.OK)
                ))

            xml_response = davxml.PropertyStatusResponse(davxml.HRef(uri), *propstats)

            # This needed for propfind cache tracking of children changes
            if depth == "1":
                if resource != self:
                    if hasattr(resource, "owner_url"):
                        request.childCacheURIs.append(resource.owner_url())
                    elif hasattr(resource, "url"):
                        request.childCacheURIs.append(resource.url())
        else:
            xml_response = davxml.StatusResponse(davxml.HRef(uri), davxml.Status.fromResponseCode(respcode))

        xml_responses.append(xml_response)

    if not hasattr(request, "extendedLogItems"):
        request.extendedLogItems = {}
    request.extendedLogItems["responses"] = len(xml_responses)

    #
    # Return response
    #
    returnValue(MultiStatusResponse(xml_responses))


##
# Utilities
##

def propertyName(prop):
    if type(prop) is tuple:
        name = prop
    else:
        name = prop.qname()
    property_namespace, property_name = name
    pname = davxml.WebDAVUnknownElement()
    pname.namespace = property_namespace
    pname.name = property_name
    return pname
