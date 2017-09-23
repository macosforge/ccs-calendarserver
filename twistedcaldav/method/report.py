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
WebDAV REPORT method
"""

__all__ = ["http_REPORT"]

import string

from twisted.internet.defer import inlineCallbacks, returnValue
from txweb2 import responsecode
from txweb2.http import HTTPError, StatusResponse
from txweb2.dav.util import davXMLFromStream
from txdav.xml import element as davxml
from txdav.xml.base import encodeXMLName
from txdav.xml.element import lookupElement

from twext.python.log import Logger
from txweb2.dav.http import ErrorResponse

from twistedcaldav import caldavxml

log = Logger()


@inlineCallbacks
def http_REPORT(self, request):
    """
    Respond to a REPORT request. (RFC 3253, section 3.6)
    """
    if not self.exists():
        log.error("Resource not found: {s!r}", s=self)
        raise HTTPError(responsecode.NOT_FOUND)

    #
    # Read request body
    #
    try:
        doc = (yield davXMLFromStream(request.stream))
    except ValueError as e:
        log.error("Error while handling REPORT body: {err}", err=str(e))
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(e)))

    if doc is None:
        raise HTTPError(StatusResponse(
            responsecode.BAD_REQUEST,
            "REPORT request body may not be empty"
        ))

    #
    # Parse request
    #
    namespace = doc.root_element.namespace
    name = doc.root_element.name

    if namespace:
        if namespace == davxml.dav_namespace:
            request.submethod = "DAV:" + name
        elif namespace == caldavxml.caldav_namespace:
            request.submethod = "CalDAV:" + name
        else:
            request.submethod = encodeXMLName(namespace, name)
    else:
        request.submethod = name

    def to_method(namespace, name):
        if namespace:
            s = "_".join((namespace, name))
        else:
            s = name

        ok = string.ascii_letters + string.digits + "_"
        out = []
        for c in s:
            if c in ok:
                out.append(c)
            else:
                out.append("_")
        return "report_" + "".join(out)

    method_name = to_method(namespace, name)

    try:
        method = getattr(self, method_name)

        # Also double-check via supported-reports property
        reports = self.supportedReports()
        test = lookupElement((namespace, name))
        if not test:
            raise AttributeError()
        test = davxml.Report(test())
        if test not in reports:
            raise AttributeError()
    except AttributeError:
        #
        # Requested report is not supported.
        #
        log.error(
            "Unsupported REPORT {name} for resource {resource} (no method {method})",
            name=encodeXMLName(namespace, name), resource=self, method=method_name,
        )

        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            davxml.SupportedReport(),
            "Report not supported on this resource",
        ))

    #
    # Check authentication and access controls
    #
    privileges = (davxml.Read(),)
    if method_name == "report_urn_ietf_params_xml_ns_caldav_free_busy_query":
        privileges = (caldavxml.ReadFreeBusy(),)
    yield self.authorize(request, privileges)

    result = (yield method(request, doc.root_element))
    returnValue(result)
