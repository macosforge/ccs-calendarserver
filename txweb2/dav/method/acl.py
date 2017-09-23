# -*- test-case-name: txweb2.dav.test.test_lock -*-
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
WebDAV ACL method
"""

__all__ = ["http_ACL"]

from twisted.internet.defer import deferredGenerator, waitForDeferred

from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.http import StatusResponse, HTTPError
from txdav.xml import element as davxml
from txweb2.dav.http import ErrorResponse
from txweb2.dav.util import davXMLFromStream

log = Logger()


def http_ACL(self, request):
    """
    Respond to a ACL request. (RFC 3744, section 8.1)
    """
    if not self.exists():
        log.error("File not found: %s" % (self,))
        yield responsecode.NOT_FOUND
        return

    #
    # Check authentication and access controls
    #
    x = waitForDeferred(self.authorize(request, (davxml.WriteACL(),)))
    yield x
    x.getResult()

    #
    # Read request body
    #
    doc = waitForDeferred(davXMLFromStream(request.stream))
    yield doc
    try:
        doc = doc.getResult()
    except ValueError as e:
        log.error("Error while handling ACL body: %s" % (e,))
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(e)))

    #
    # Set properties
    #
    if doc is None:
        error = "Request XML body is required."
        log.error("Error: {err}", err=error)
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, error))

    #
    # Parse request
    #
    acl = doc.root_element
    if not isinstance(acl, davxml.ACL):
        error = ("Request XML body must be an acl element."
                 % (davxml.PropertyUpdate.sname(),))
        log.error("Error: {err}", err=error)
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, error))

    #
    # Do ACL merger
    #
    result = waitForDeferred(self.mergeAccessControlList(acl, request))
    yield result
    result = result.getResult()

    #
    # Return response
    #
    if result is None:
        yield responsecode.OK
    else:
        yield ErrorResponse(responsecode.FORBIDDEN, result)

http_ACL = deferredGenerator(http_ACL)
