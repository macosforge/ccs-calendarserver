# -*- test-case-name: txweb2.dav.test.test_mkcol -*-
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
WebDAV MKCOL method
"""

__all__ = ["http_MKCOL"]

from twisted.internet.defer import deferredGenerator, waitForDeferred

from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.http import HTTPError, StatusResponse
from txdav.xml import element as davxml
from txweb2.dav.fileop import mkcollection
from txweb2.dav.util import noDataFromStream, parentForURL

log = Logger()


def http_MKCOL(self, request):
    """
    Respond to a MKCOL request. (RFC 2518, section 8.3)
    """
    parent = waitForDeferred(request.locateResource(parentForURL(request.uri)))
    yield parent
    parent = parent.getResult()

    x = waitForDeferred(parent.authorize(request, (davxml.Bind(),)))
    yield x
    x.getResult()

    if self.exists():
        log.error("Attempt to create collection where file exists: %s"
                  % (self,))
        raise HTTPError(responsecode.NOT_ALLOWED)

    if not parent.isCollection():
        log.error("Attempt to create collection with non-collection parent: %s"
                  % (self,))
        raise HTTPError(StatusResponse(
            responsecode.CONFLICT,
            "Parent resource is not a collection."
        ))

    #
    # Read request body
    #
    x = waitForDeferred(noDataFromStream(request.stream))
    yield x
    try:
        x.getResult()
    except ValueError as e:
        log.error("Error while handling MKCOL body: %s" % (e,))
        raise HTTPError(responsecode.UNSUPPORTED_MEDIA_TYPE)

    response = waitForDeferred(mkcollection(self.fp))
    yield response
    yield response.getResult()

http_MKCOL = deferredGenerator(http_MKCOL)
