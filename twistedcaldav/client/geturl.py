##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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

from twext.python.log import Logger

from twisted.internet import reactor, protocol
from twisted.internet.defer import inlineCallbacks, Deferred, returnValue
from twisted.web import http_headers
from twisted.web.client import Agent
from twisted.web.http import MOVED_PERMANENTLY, TEMPORARY_REDIRECT, FOUND

from urlparse import urlparse
from urlparse import urlunparse

__all__ = [
    "getURL",
]

log = Logger()


class AccumulatingProtocol(protocol.Protocol):
    """
    L{AccumulatingProtocol} is an L{IProtocol} implementation which collects
    the data delivered to it and can fire a Deferred when it is connected or
    disconnected.

    @ivar made: A flag indicating whether C{connectionMade} has been called.
    @ivar data: A string giving all the data passed to C{dataReceived}.
    @ivar closed: A flag indicated whether C{connectionLost} has been called.
    @ivar closedReason: The value of the I{reason} parameter passed to
        C{connectionLost}.
    @ivar closedDeferred: If set to a L{Deferred}, this will be fired when
        C{connectionLost} is called.
    """
    made = closed = 0
    closedReason = None

    closedDeferred = None

    data = ""

    factory = None

    def connectionMade(self):
        self.made = 1
        if (
            self.factory is not None and
            self.factory.protocolConnectionMade is not None
        ):
            d = self.factory.protocolConnectionMade
            self.factory.protocolConnectionMade = None
            d.callback(self)

    def dataReceived(self, data):
        self.data += data

    def connectionLost(self, reason):
        self.closed = 1
        self.closedReason = reason
        if self.closedDeferred is not None:
            d, self.closedDeferred = self.closedDeferred, None
            d.callback(None)


@inlineCallbacks
def getURL(url, method="GET", redirect=0):

    if isinstance(url, unicode):
        url = url.encode("utf-8")
    agent = Agent(reactor)
    headers = http_headers.Headers({})

    try:
        response = (yield agent.request(method, url, headers, None))
    except Exception as e:
        log.error(str(e))
        response = None
    else:
        if response.code in (MOVED_PERMANENTLY, FOUND, TEMPORARY_REDIRECT,):
            if redirect > 3:
                log.error("Too many redirects")
            else:
                location = response.headers.getRawHeaders("location")
                if location:
                    newresponse = (yield getURL(location[0], method=method, redirect=redirect + 1))
                    if response.code == MOVED_PERMANENTLY:
                        scheme, netloc, url, _ignore_params, _ignore_query, _ignore_fragment = urlparse(location[0])
                        newresponse.location = urlunparse((scheme, netloc, url, None, None, None,))
                    returnValue(newresponse)
                else:
                    log.error("Redirect without a Location header")

    if response is not None and response.code / 100 == 2:
        protocol = AccumulatingProtocol()
        response.deliverBody(protocol)
        whenFinished = protocol.closedDeferred = Deferred()
        yield whenFinished
        response.data = protocol.data
    else:
        log.error("Failed getURL: {url}", url=url)

    returnValue(response)
