# -*- test-case-name: txweb2.dav.test.test_report -*-
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

"""
WebDAV REPORT method
"""

__all__ = [
    "max_number_of_matches",
    "NumberOfMatchesWithinLimits",
    "http_REPORT",
]

import string

from twisted.internet.defer import deferredGenerator, waitForDeferred

from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.http import HTTPError, StatusResponse
from txweb2.dav.http import ErrorResponse
from txweb2.dav.util import davXMLFromStream
from txdav.xml import element as davxml
from txdav.xml.element import lookupElement
from txdav.xml.base import encodeXMLName

log = Logger()


max_number_of_matches = 500


class NumberOfMatchesWithinLimits(Exception):

    def __init__(self, limit):

        super(NumberOfMatchesWithinLimits, self).__init__()
        self.limit = limit

    def maxLimit(self):
        return self.limit


def http_REPORT(self, request):
    """
    Respond to a REPORT request. (RFC 3253, section 3.6)
    """
    if not self.exists():
        log.error("File not found: %s" % (self,))
        raise HTTPError(responsecode.NOT_FOUND)

    #
    # Check authentication and access controls
    #
    x = waitForDeferred(self.authorize(request, (davxml.Read(),)))
    yield x
    x.getResult()

    #
    # Read request body
    #
    try:
        doc = waitForDeferred(davXMLFromStream(request.stream))
        yield doc
        doc = doc.getResult()
    except ValueError as e:
        log.error("Error while handling REPORT body: %s" % (e,))
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

    ok = string.ascii_letters + string.digits + "_"

    def to_method(s):
        out = []
        for c in s:
            if c in ok:
                out.append(c)
            else:
                out.append("_")
        return "report_" + "".join(out)

    if namespace:
        method_name = to_method("_".join((namespace, name)))

        if namespace == davxml.dav_namespace:
            request.submethod = "DAV:" + name
        else:
            request.submethod = encodeXMLName(namespace, name)
    else:
        method_name = to_method(name)

        request.submethod = name

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
        log.error("Unsupported REPORT %s for resource %s (no method %s)"
                  % (encodeXMLName(namespace, name), self, method_name))

        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            davxml.SupportedReport()
        ))

    d = waitForDeferred(method(request, doc.root_element))
    yield d
    yield d.getResult()

http_REPORT = deferredGenerator(http_REPORT)
