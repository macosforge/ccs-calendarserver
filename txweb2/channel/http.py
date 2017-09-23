# -*- test-case-name: txweb2.test.test_http -*-
##
# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
# Copyright (c) 2008-2017 Apple Inc. All rights reserved.
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
##

import time
import warnings
import socket
from random import randint
from cStringIO import StringIO

from zope.interface import implements

from twisted.internet import interfaces, protocol, reactor
from twisted.internet.defer import succeed, Deferred
from twisted.protocols import policies, basic

from twext.python.log import Logger
from txweb2 import responsecode
from txweb2 import http_headers
from txweb2 import http
from txweb2.auth.tls import TLSCredentials
from txweb2.http import RedirectResponse
from txweb2.server import Request

from twistedcaldav.config import config
from twistedcaldav import accounting
from twisted.internet._sslverify import Certificate
from twisted.internet.error import CertificateError

log = Logger()


class OverloadedLoggingServerProtocol (protocol.Protocol):

    def __init__(self, retryAfter, outstandingRequests):
        self.retryAfter = retryAfter
        self.outstandingRequests = outstandingRequests

    def connectionMade(self):
        log.info(overloaded=self)

        self.transport.write(
            "HTTP/1.0 503 Service Unavailable\r\n"
            "Content-Type: text/html\r\n"
        )

        if self.retryAfter:
            self.transport.write(
                "Retry-After: %s\r\n" % (self.retryAfter,)
            )

        self.transport.write(
            "Connection: close\r\n\r\n"
            "<html><head><title>Service Unavailable</title></head>"
            "<body><h1>Service Unavailable</h1>"
            "The server is currently overloaded, "
            "please try again later.</body></html>"
        )
        self.transport.loseConnection()


class SSLRedirectRequest(Request):
    """
    An L{SSLRedirectRequest} prevents processing if the request is over plain
    HTTP; instead, it redirects to HTTPS.
    """

    def process(self):
        ignored, secure = self.chanRequest.getHostInfo()
        if not secure:
            if config.SSLPort == 443:
                location = (
                    "https://%s%s"
                    % (config.ServerHostName, self.uri)
                )
            else:
                location = (
                    "https://%s:%d%s"
                    % (config.ServerHostName, config.SSLPort, self.uri)
                )
            log.debug("Redirecting unsecured request")
            return super(SSLRedirectRequest, self).writeResponse(
                RedirectResponse(location)
            )
        else:
            return super(SSLRedirectRequest, self).process()


# >%

PERSIST_NO_PIPELINE, PERSIST_PIPELINE = (1, 2)

_cachedHostNames = {}


def _cachedGetHostByAddr(hostaddr):
    hostname = _cachedHostNames.get(hostaddr)
    if hostname is None:
        try:
            hostname = socket.gethostbyaddr(hostaddr)[0]
        except (socket.herror, socket.gaierror):
            hostname = hostaddr
        _cachedHostNames[hostaddr] = hostname
    return hostname


class StringTransport(object):
    """
    I am a StringIO wrapper that conforms for the transport API. I support
    the 'writeSequence' method.
    """

    def __init__(self):
        self.s = StringIO()

    def writeSequence(self, seq):
        self.s.write(''.join(seq))

    def __getattr__(self, attr):
        return getattr(self.__dict__['s'], attr)


class AbortedException(Exception):
    pass


class HTTPParser(object):
    """This class handles the parsing side of HTTP processing. With a suitable
    subclass, it can parse either the client side or the server side of the
    connection.
    """

    # Class config:
    parseCloseAsEnd = False

    # Instance vars
    chunkedIn = False
    headerlen = 0
    length = None
    inHeaders = None
    partialHeader = ''
    connHeaders = None
    finishedReading = False

    channel = None

    # For subclassing...
    # Needs attributes:
    #  version

    # Needs functions:
    #  createRequest()
    #  processRequest()
    #  _abortWithError()
    #  handleContentChunk(data)
    #  handleContentComplete()

    # Needs functions to exist on .channel
    #  channel.maxHeaderLength
    #  channel.requestReadFinished(self)
    #  channel.setReadPersistent(self, persistent)
    # (from LineReceiver):
    #  channel.setRawMode()
    #  channel.setLineMode(extraneous)
    #  channel.pauseProducing()
    #  channel.resumeProducing()
    #  channel.stopProducing()

    def __init__(self, channel):
        self.inHeaders = http_headers.Headers()
        self.channel = channel

    def lineReceived(self, line):
        if self.chunkedIn:
            # Parsing a chunked input
            if self.chunkedIn == 1:
                # First we get a line like "chunk-size [';' chunk-extension]"
                # (where chunk extension is just random crap as far as we're concerned)
                # RFC says to ignore any extensions you don't recognize -- that's all of them.
                chunksize = line.split(';', 1)[0]
                try:
                    self.length = int(chunksize, 16)
                except:
                    self._abortWithError(responsecode.BAD_REQUEST, "Invalid chunk size, not a hex number: %s!" % chunksize)
                if self.length < 0:
                    self._abortWithError(responsecode.BAD_REQUEST, "Invalid chunk size, negative.")

                if self.length == 0:
                    # We're done, parse the trailers line
                    self.chunkedIn = 3
                else:
                    # Read self.length bytes of raw data
                    self.channel.setRawMode()
            elif self.chunkedIn == 2:
                # After we got data bytes of the appropriate length, we end up here,
                # waiting for the CRLF, then go back to get the next chunk size.
                if line != '':
                    self._abortWithError(responsecode.BAD_REQUEST, "Excess %d bytes sent in chunk transfer mode" % len(line))
                self.chunkedIn = 1
            elif self.chunkedIn == 3:
                # TODO: support Trailers (maybe! but maybe not!)

                # After getting the final "0" chunk we're here, and we *EAT MERCILESSLY*
                # any trailer headers sent, and wait for the blank line to terminate the
                # request.
                if line == '':
                    self.allContentReceived()
        # END of chunk handling
        elif line == '':
            # Empty line => End of headers
            if self.partialHeader:
                self.headerReceived(self.partialHeader)
            self.partialHeader = ''
            self.allHeadersReceived()    # can set chunkedIn
            self.createRequest()
            if self.chunkedIn:
                # stay in linemode waiting for chunk header
                pass
            elif self.length == 0:
                # no content expected
                self.allContentReceived()
            else:
                # await raw data as content
                self.channel.setRawMode()
                # Should I do self.pauseProducing() here?
            self.processRequest()
        else:
            self.headerlen += len(line)
            if self.headerlen > self.channel.maxHeaderLength:
                self._abortWithError(responsecode.BAD_REQUEST, 'Headers too long.')

            if line[0] in ' \t':
                # Append a header continuation
                self.partialHeader += line
            else:
                if self.partialHeader:
                    self.headerReceived(self.partialHeader)
                self.partialHeader = line

    def rawDataReceived(self, data):
        """Handle incoming content."""
        datalen = len(data)
        if datalen < self.length:
            self.handleContentChunk(data)
            self.length = self.length - datalen
        else:
            self.handleContentChunk(data[:self.length])
            extraneous = data[self.length:]
            channel = self.channel  # could go away from allContentReceived.
            if not self.chunkedIn:
                self.allContentReceived()
            else:
                # NOTE: in chunked mode, self.length is the size of the current chunk,
                # so we still have more to read.
                self.chunkedIn = 2  # Read next chunksize

            channel.setLineMode(extraneous)

    def headerReceived(self, line):
        """
        Store this header away. Check for too much header data (>
        channel.maxHeaderLength) and non-ASCII characters; abort the
        connection with C{BAD_REQUEST} if so.
        """
        nameval = line.split(':', 1)
        if len(nameval) != 2:
            self._abortWithError(responsecode.BAD_REQUEST, "No ':' in header.")
        name, val = nameval
        for field in name, val:
            try:
                field.decode('ascii')
            except UnicodeDecodeError:
                self._abortWithError(responsecode.BAD_REQUEST,
                                     "Headers must be ASCII")
        val = val.lstrip(' \t')
        self.inHeaders.addRawHeader(name, val)

    def allHeadersReceived(self):
        # Split off connection-related headers
        connHeaders = self.splitConnectionHeaders()

        # Set connection parameters from headers
        self.setConnectionParams(connHeaders)
        self.connHeaders = connHeaders

    def allContentReceived(self):
        self.finishedReading = True
        self.channel.requestReadFinished(self)
        self.handleContentComplete()

    def splitConnectionHeaders(self):
        """
        Split off connection control headers from normal headers.

        The normal headers are then passed on to user-level code, while the
        connection headers are stashed in .connHeaders and used for things like
        request/response framing.

        This corresponds roughly with the HTTP RFC's description of 'hop-by-hop'
        vs 'end-to-end' headers in RFC2616 S13.5.1, with the following
        exceptions:

         - proxy-authenticate and proxy-authorization are not treated as
           connection headers.

         - content-length is, as it is intimately related with low-level HTTP
           parsing, and is made available to user-level code via the stream
           length, rather than a header value. (except for HEAD responses, in
           which case it is NOT used by low-level HTTP parsing, and IS kept in
           the normal headers.
        """

        def move(name):
            h = inHeaders.getRawHeaders(name, None)
            if h is not None:
                inHeaders.removeHeader(name)
                connHeaders.setRawHeaders(name, h)

        # NOTE: According to HTTP spec, we're supposed to eat the
        # 'Proxy-Authenticate' and 'Proxy-Authorization' headers also, but that
        # doesn't sound like a good idea to me, because it makes it impossible
        # to have a non-authenticating transparent proxy in front of an
        # authenticating proxy. An authenticating proxy can eat them itself.
        #
        # 'Proxy-Connection' is an undocumented HTTP 1.0 abomination.
        connHeaderNames = ['content-length', 'connection', 'keep-alive', 'te',
                           'trailers', 'transfer-encoding', 'upgrade',
                           'proxy-connection']
        inHeaders = self.inHeaders
        connHeaders = http_headers.Headers()

        move('connection')
        if self.version < (1, 1):
            # Remove all headers mentioned in Connection, because a HTTP 1.0
            # proxy might have erroneously forwarded it from a 1.1 client.
            for name in connHeaders.getHeader('connection', ()):
                if inHeaders.hasHeader(name):
                    inHeaders.removeHeader(name)
        else:
            # Otherwise, just add the headers listed to the list of those to move
            connHeaderNames.extend(connHeaders.getHeader('connection', ()))

        # If the request was HEAD, self.length has been set to 0 by
        # HTTPClientRequest.submit; in this case, Content-Length should
        # be treated as a response header, not a connection header.

        # Note: this assumes the invariant that .length will always be None
        # coming into this function, unless this is a HEAD request.
        if self.length is not None:
            connHeaderNames.remove('content-length')

        for headername in connHeaderNames:
            move(headername)

        return connHeaders

    def setConnectionParams(self, connHeaders):
        # Figure out persistent connection stuff
        if self.version >= (1, 1):
            if 'close' in connHeaders.getHeader('connection', ()):
                readPersistent = False
            else:
                readPersistent = PERSIST_PIPELINE
        elif 'keep-alive' in connHeaders.getHeader('connection', ()):
            readPersistent = PERSIST_NO_PIPELINE
        else:
            readPersistent = False

        # Okay, now implement section 4.4 Message Length to determine
        # how to find the end of the incoming HTTP message.
        transferEncoding = connHeaders.getHeader('transfer-encoding')

        if transferEncoding:
            if transferEncoding[-1] == 'chunked':
                # Chunked
                self.chunkedIn = 1
                # Cut off the chunked encoding (cause it's special)
                transferEncoding = transferEncoding[:-1]
            elif not self.parseCloseAsEnd:
                # Would close on end of connection, except this can't happen for
                # client->server data. (Well..it could actually, since TCP has half-close
                # but the HTTP spec says it can't, so we'll pretend it's right.)
                self._abortWithError(responsecode.BAD_REQUEST, "Transfer-Encoding received without chunked in last position.")

            # TODO: support gzip/etc encodings.
            # FOR NOW: report an error if the client uses any encodings.
            # They shouldn't, because we didn't send a TE: header saying it's okay.
            if transferEncoding:
                self._abortWithError(responsecode.NOT_IMPLEMENTED, "Transfer-Encoding %s not supported." % transferEncoding)
        else:
            # No transfer-coding.
            self.chunkedIn = 0
            if self.parseCloseAsEnd:
                # If no Content-Length, then it's indeterminate length data
                # (unless the responsecode was one of the special no body ones)
                # Also note that for HEAD requests, connHeaders won't have
                # content-length even if the response did.
                if self.code in http.NO_BODY_CODES:
                    self.length = 0
                else:
                    self.length = connHeaders.getHeader('content-length', self.length)

                # If it's an indeterminate stream without transfer encoding, it must be
                # the last request.
                if self.length is None:
                    readPersistent = False
            else:
                # If no Content-Length either, assume no content.
                self.length = connHeaders.getHeader('content-length', 0)

        # Set the calculated persistence
        self.channel.setReadPersistent(readPersistent)

    def abortParse(self):
        # If we're erroring out while still reading the request
        if not self.finishedReading:
            self.finishedReading = True
            self.channel.setReadPersistent(False)
            self.channel.requestReadFinished(self)

    # producer interface
    def pauseProducing(self):
        if not self.finishedReading:
            self.channel.pauseProducing()

    def resumeProducing(self):
        if not self.finishedReading:
            self.channel.resumeProducing()

    def stopProducing(self):
        if not self.finishedReading:
            self.channel.stopProducing()


class HTTPChannelRequest(HTTPParser):
    """This class handles the state and parsing for one HTTP request.
    It is responsible for all the low-level connection oriented behavior.
    Thus, it takes care of keep-alive, de-chunking, etc., and passes
    the non-connection headers on to the user-level Request object."""

    command = path = version = None
    queued = 0
    request = None

    out_version = "HTTP/1.1"

    def __init__(self, channel, queued=0):
        HTTPParser.__init__(self, channel)
        self.queued = queued

        # Buffer writes to a string until we're first in line
        # to write a response
        if queued:
            self.transport = StringTransport()
        else:
            self.transport = self.channel.transport

        # set the version to a fallback for error generation
        self.version = (1, 0)

    def gotInitialLine(self, initialLine):
        parts = initialLine.split()

        # Parse the initial request line
        if len(parts) != 3:
            if len(parts) == 1:
                parts.append('/')
            if len(parts) == 2 and parts[1][0] == '/':
                parts.append('HTTP/0.9')
            else:
                self._abortWithError(responsecode.BAD_REQUEST, 'Bad request line: %s' % initialLine)

        self.command, self.path, strversion = parts
        try:
            protovers = http.parseVersion(strversion)
            if protovers[0] != 'http':
                raise ValueError()
        except ValueError:
            self._abortWithError(responsecode.BAD_REQUEST, "Unknown protocol: %s" % strversion)

        self.version = protovers[1:3]

        # Ensure HTTP 0 or HTTP 1.
        if self.version[0] > 1:
            self._abortWithError(responsecode.HTTP_VERSION_NOT_SUPPORTED, 'Only HTTP 0.9 and HTTP 1.x are supported.')

        if self.version[0] == 0:
            # simulate end of headers, as HTTP 0 doesn't have headers.
            self.lineReceived('')

    def lineLengthExceeded(self, line, wasFirst=False):
        code = wasFirst and responsecode.REQUEST_URI_TOO_LONG or responsecode.BAD_REQUEST
        self._abortWithError(code, 'Header line too long.')

    def createRequest(self):
        self.request = self.channel.requestFactory(self, self.command, self.path, self.version, self.length, self.inHeaders)
        del self.inHeaders

    def processRequest(self):
        self.request.process()

    def handleContentChunk(self, data):
        self.request.handleContentChunk(data)

    def handleContentComplete(self):
        self.request.handleContentComplete()

    #  HTTPChannelRequest *RESPONSE* methods #
    producer = None
    chunkedOut = False
    finished = False

    # Request Callbacks #
    def writeIntermediateResponse(self, code, headers=None):
        if self.version >= (1, 1):
            self._writeHeaders(code, headers, False)

    def writeHeaders(self, code, headers):
        self._writeHeaders(code, headers, True)

    def _writeHeaders(self, code, headers, addConnectionHeaders):
        # HTTP 0.9 doesn't have headers.
        if self.version[0] == 0:
            return

        l = []
        code_message = responsecode.RESPONSES.get(code, "Unknown Status")

        l.append('%s %s %s\r\n' % (self.out_version, code,
                                   code_message))
        if headers is not None:
            for name, valuelist in headers.getAllRawHeaders():
                for value in valuelist:
                    l.append("%s: %s\r\n" % (name, value))

        if addConnectionHeaders:
            # if we don't have a content length, we send data in
            # chunked mode, so that we can support persistent connections.
            if (
                headers.getHeader('content-length') is None and
                self.command != "HEAD" and code not in http.NO_BODY_CODES
            ):
                if self.version >= (1, 1):
                    l.append("%s: %s\r\n" % ('Transfer-Encoding', 'chunked'))
                    self.chunkedOut = True
                else:
                    # Cannot use persistent connections if we can't do chunking
                    self.channel.dropQueuedRequests()

            if self.channel.isLastRequest(self):
                l.append("%s: %s\r\n" % ('Connection', 'close'))
            elif self.version < (1, 1):
                l.append("%s: %s\r\n" % ('Connection', 'Keep-Alive'))

        l.append("\r\n")
        self.transport.writeSequence(l)

    def write(self, data):
        if not data:
            return
        elif self.chunkedOut:
            self.transport.writeSequence(("%X\r\n" % len(data), data, "\r\n"))
        else:
            self.transport.write(data)

    def finish(self):
        """We are finished writing data."""
        if self.finished:
            warnings.warn("Warning! request.finish called twice.", stacklevel=2)
            return

        if self.chunkedOut:
            # write last chunk and closing CRLF
            self.transport.write("0\r\n\r\n")

        self.finished = True
        if not self.queued:
            self._cleanup()

    def abortConnection(self, closeWrite=True):
        """Abort the HTTP connection because of some kind of unrecoverable
        error. If closeWrite=False, then only abort reading, but leave
        the writing side alone. This is mostly for internal use by
        the HTTP request parsing logic, so that it can call an error
        page generator.

        Otherwise, completely shut down the connection.
        """
        self.abortParse()
        if closeWrite:
            if self.producer:
                self.producer.stopProducing()
                self.unregisterProducer()

            self.finished = True
            if self.queued:
                self.transport.reset()
                self.transport.truncate()
            else:
                self._cleanup()

    def getHostInfo(self):
        return self.channel._host, self.channel._secure

    def getRemoteHost(self):
        return self.channel.transport.getPeer()

    # End Request Callbacks #

    def _abortWithError(self, errorcode, text=''):
        """Handle low level protocol errors."""
        headers = http_headers.Headers()
        headers.setHeader('content-length', len(text) + 1)

        self.abortConnection(closeWrite=False)
        self.writeHeaders(errorcode, headers)
        self.write(text)
        self.write("\n")
        self.finish()
        log.warn("Aborted request (%d) %s" % (errorcode, text))
        raise AbortedException

    def _cleanup(self):
        """Called when have finished responding and are no longer queued."""
        if self.producer:
            log.error(RuntimeError("Producer was not unregistered for %s" % self))
            self.unregisterProducer()
        self.channel.requestWriteFinished(self)
        del self.transport

    # methods for channel - end users should not use these

    def noLongerQueued(self):
        """Notify the object that it is no longer queued.

        We start writing whatever data we have to the transport, etc.

        This method is not intended for users.
        """
        if not self.queued:
            raise RuntimeError("noLongerQueued() got called unnecessarily.")

        self.queued = 0

        # set transport to real one and send any buffer data
        data = self.transport.getvalue()
        self.transport = self.channel.transport
        if data:
            self.transport.write(data)

        # if we have producer, register it with transport
        if (self.producer is not None) and not self.finished:
            self.transport.registerProducer(self.producer, True)

        # if we're finished, clean up
        if self.finished:
            self._cleanup()

    # consumer interface
    def registerProducer(self, producer, streaming):
        """Register a producer.
        """

        if self.producer:
            raise ValueError("registering producer %s before previous one (%s) was unregistered" % (producer, self.producer))

        self.producer = producer

        if self.queued:
            producer.pauseProducing()
        else:
            self.transport.registerProducer(producer, streaming)

    def unregisterProducer(self):
        """Unregister the producer."""
        if not self.queued:
            self.transport.unregisterProducer()
        self.producer = None

    def connectionLost(self, reason):
        """connection was lost"""
        if self.queued and self.producer:
            self.producer.stopProducing()
            self.producer = None
        if self.request:
            self.request.connectionLost(reason)


class HTTPChannel(basic.LineReceiver, policies.TimeoutMixin, object):
    """A receiver for HTTP requests. Handles splitting up the connection
    for the multiple HTTPChannelRequests that may be in progress on this
    channel.

    @ivar timeOut: number of seconds to wait before terminating an
    idle connection.

    @ivar maxPipeline: number of outstanding in-progress requests
    to allow before pausing the input.

    @ivar maxHeaderLength: number of bytes of header to accept from
    the client.

    """

    implements(interfaces.IHalfCloseableProtocol)

    # Configuration parameters. Set in instances or subclasses.

    # How many simultaneous requests to handle.
    maxPipeline = 4

    # Timeout when between two requests
    betweenRequestsTimeOut = 15
    # Timeout between lines or bytes while reading a request
    inputTimeOut = 60 * 4
    # Timeout between end of request read and end of response write
    idleTimeOut = 60 * 5
    # Timeout when closing non-persistent connection
    closeTimeOut = 20

    # maximum length of headers (10KiB)
    maxHeaderLength = 10240

    # Allow persistent connections?
    allowPersistentConnections = True

    # ChannelRequest
    chanRequestFactory = HTTPChannelRequest
    requestFactory = http.Request

    _first_line = 2
    readPersistent = PERSIST_PIPELINE

    _readLost = False
    _writeLost = False

    _abortTimer = None
    chanRequest = None

    peerCertificateCheck = False
    peerCredentials = None

    def _callLater(self, secs, fun):
        reactor.callLater(secs, fun)

    def __init__(self):
        # the request queue
        self.requests = []

    def connectionMade(self):
        self._secure = interfaces.ISSLTransport(self.transport, None) is not None
        address = self.transport.getHost()
        self._host = _cachedGetHostByAddr(address.host)
        self.setTimeout(self.inputTimeOut)
        self.factory.addConnectedChannel(self)

    def processPeerCertificate(self):
        # Look for SSL client cert
        if self._secure:
            try:
                self.peerCredentials = TLSCredentials(Certificate.peerFromTransport(self.transport))
            except CertificateError:
                pass

        self.peerCertificateCheck = True

    def lineReceived(self, line):

        if self._secure and not self.peerCertificateCheck:
            self.processPeerCertificate()

        if self._first_line:
            self.setTimeout(self.inputTimeOut)
            # if this connection is not persistent, drop any data which
            # the client (illegally) sent after the last request.
            if not self.readPersistent:
                self.dataReceived = self.lineReceived = lambda *args: None
                return

            # IE sends an extraneous empty line (\r\n) after a POST request;
            # eat up such a line, but only ONCE
            if not line and self._first_line == 1:
                self._first_line = 2
                return

            self._first_line = 0

            if not self.allowPersistentConnections:
                # Don't allow a second request
                self.readPersistent = False

            try:
                self.chanRequest = self.chanRequestFactory(self, len(self.requests))
                self.requests.append(self.chanRequest)
                self.chanRequest.gotInitialLine(line)
            except AbortedException:
                pass
        else:
            try:
                self.chanRequest.lineReceived(line)
            except AbortedException:
                pass

    def lineLengthExceeded(self, line):
        if self._first_line:
            # Fabricate a request object to respond to the line length violation.
            self.chanRequest = self.chanRequestFactory(self,
                                                       len(self.requests))
            self.requests.append(self.chanRequest)
            self.chanRequest.gotInitialLine("GET fake HTTP/1.0")
        try:
            self.chanRequest.lineLengthExceeded(line, self._first_line)
        except AbortedException:
            pass

    def rawDataReceived(self, data):
        self.setTimeout(self.inputTimeOut)
        try:
            self.chanRequest.rawDataReceived(data)
        except AbortedException:
            pass

    def requestReadFinished(self, request):
        if(self.readPersistent is PERSIST_NO_PIPELINE or
           len(self.requests) >= self.maxPipeline):
            self.pauseProducing()

        # reset state variables
        self._first_line = 1
        self.chanRequest = None
        self.setLineMode()

        # Set an idle timeout, in case this request takes a long
        # time to finish generating output.
        if len(self.requests) > 0:
            self.setTimeout(self.idleTimeOut)

    def _startNextRequest(self):
        # notify next request, if present, it can start writing
        del self.requests[0]

        if self._writeLost:
            self.transport.loseConnection()
        elif self.requests:
            self.requests[0].noLongerQueued()

            # resume reading if allowed to
            if(not self._readLost and
               self.readPersistent is not PERSIST_NO_PIPELINE and
               len(self.requests) < self.maxPipeline):
                self.resumeProducing()
        elif self._readLost:
            # No more incoming data, they already closed!
            self.transport.loseConnection()
        else:
            # no requests in queue, resume reading
            self.setTimeout(self.betweenRequestsTimeOut)
            self.resumeProducing()

    def setReadPersistent(self, persistent):
        if self.readPersistent:
            # only allow it to be set if it's not currently False
            self.readPersistent = persistent

    def dropQueuedRequests(self):
        """Called when a response is written that forces a connection close."""
        self.readPersistent = False
        # Tell all requests but first to abort.
        for request in self.requests[1:]:
            request.connectionLost(None)
        del self.requests[1:]

    def isLastRequest(self, request):
        # Is this channel handling the last possible request
        return not self.readPersistent and self.requests[-1] == request

    def requestWriteFinished(self, request):
        """Called by first request in queue when it is done."""
        if request != self.requests[0]:
            raise TypeError

        # Don't del because we haven't finished cleanup, so,
        # don't want queue len to be 0 yet.
        self.requests[0] = None

        if self.readPersistent or len(self.requests) > 1:
            # Do this in the next reactor loop so as to
            # not cause huge call stacks with fast
            # incoming requests.
            self._callLater(0, self._startNextRequest)
        else:
            # Set an abort timer in case an orderly close hangs
            self.setTimeout(None)
            self._abortTimer = reactor.callLater(self.closeTimeOut, self._abortTimeout)
            # eactor.callLater(0.1, self.transport.loseConnection)
            self.transport.loseConnection()

    def timeoutConnection(self):
        # log.info("Timing out client: %s" % str(self.transport.getPeer()))
        # Set an abort timer in case an orderly close hangs
        self._abortTimer = reactor.callLater(self.closeTimeOut, self._abortTimeout)
        policies.TimeoutMixin.timeoutConnection(self)

    def _abortTimeout(self):
        log.error("Connection aborted - took too long to close: {c}", c=str(self.transport.getPeer()))
        self._abortTimer = None
        self.transport.abortConnection()

    def readConnectionLost(self):
        """Read connection lost"""
        # If in the lingering-close state, lose the socket.
        if self._abortTimer:
            self._abortTimer.cancel()
            self._abortTimer = None
            self.transport.loseConnection()
            return

        # If between requests, drop connection
        # when all current requests have written their data.
        self._readLost = True
        if not self.requests:
            # No requests in progress, lose now.
            self.transport.loseConnection()

        # If currently in the process of reading a request, this is
        # probably a client abort, so lose the connection.
        if self.chanRequest:
            self.transport.loseConnection()

    def connectionLost(self, reason):
        self.factory.removeConnectedChannel(self)

        self._writeLost = True
        self.readConnectionLost()
        self.setTimeout(None)

        # Tell all requests to abort.
        for request in self.requests:
            if request is not None:
                request.connectionLost(reason)


class OverloadedServerProtocol(protocol.Protocol):

    def connectionMade(self):
        self.transport.write("HTTP/1.0 503 Service Unavailable\r\n"
                             "Content-Type: text/html\r\n"
                             "Connection: close\r\n\r\n"
                             "<html><head><title>503 Service Unavailable</title></head>"
                             "<body><h1>Service Unavailable</h1>"
                             "The server is currently overloaded, "
                             "please try again later.</body></html>")
        self.transport.loseConnection()


class HTTPFactory(protocol.ServerFactory):
    """
    Factory for HTTP server.

    @ivar outstandingRequests: the number of currently connected HTTP channels.

    @type outstandingRequests: C{int}

    @ivar connectedChannels: all the channels that have currently active
    connections.

    @type connectedChannels: C{set} of L{HTTPChannel}
    """

    protocol = HTTPChannel

    protocolArgs = None

    def __init__(self, requestFactory, maxRequests=600, **kwargs):
        self.maxRequests = maxRequests
        self.protocolArgs = kwargs
        self.protocolArgs['requestFactory'] = requestFactory
        self.connectedChannels = set()
        self.allConnectionsClosedDeferred = None

    def buildProtocol(self, addr):
        if self.outstandingRequests >= self.maxRequests:
            return OverloadedServerProtocol()

        p = protocol.ServerFactory.buildProtocol(self, addr)

        for arg, value in self.protocolArgs.iteritems():
            setattr(p, arg, value)
        return p

    def addConnectedChannel(self, channel):
        """
        Add a connected channel to the set of currently connected channels and
        increase the outstanding request count.
        """
        self.connectedChannels.add(channel)

    def removeConnectedChannel(self, channel):
        """
        Remove a connected channel from the set of currently connected channels
        and decrease the outstanding request count.
        If someone is waiting for all the requests to be completed,
        self.allConnectionsClosedDeferred will be non-None; fire that callback
        when the number of outstanding requests hits zero.
        """
        self.connectedChannels.remove(channel)

        if self.allConnectionsClosedDeferred is not None:
            if self.outstandingRequests == 0:
                self.allConnectionsClosedDeferred.callback(None)

    @property
    def outstandingRequests(self):
        return len(self.connectedChannels)

    def allConnectionsClosed(self):
        """
        Return a Deferred that will fire when all outstanding requests have completed.
        @return: A Deferred with a result of None
        """
        if self.outstandingRequests == 0:
            return succeed(None)
        self.allConnectionsClosedDeferred = Deferred()
        return self.allConnectionsClosedDeferred


class HTTP503LoggingFactory (HTTPFactory):
    """
    Factory for HTTP server which emits a 503 response when overloaded.
    """

    def __init__(self, requestFactory, maxRequests=600, retryAfter=0, vary=False, **kwargs):
        self.retryAfter = retryAfter
        self.vary = vary
        HTTPFactory.__init__(self, requestFactory, maxRequests, **kwargs)

    def buildProtocol(self, addr):
        if self.vary:
            retryAfter = randint(int(self.retryAfter * 1 / 2), int(self.retryAfter * 3 / 2))
        else:
            retryAfter = self.retryAfter

        if self.outstandingRequests >= self.maxRequests:
            return OverloadedLoggingServerProtocol(retryAfter, self.outstandingRequests)

        p = protocol.ServerFactory.buildProtocol(self, addr)

        for arg, value in self.protocolArgs.iteritems():
            setattr(p, arg, value)

        return p


class HTTPLoggingChannelRequest(HTTPChannelRequest):

    class TransportLoggingWrapper(object):

        def __init__(self, transport, logData):

            self.transport = transport
            self.logData = logData

        def write(self, data):
            if self.logData is not None and data:
                self.logData.append(data)
            self.transport.write(data)

        def writeSequence(self, seq):
            if self.logData is not None and seq:
                self.logData.append(''.join(seq))
            self.transport.writeSequence(seq)

        def __getattr__(self, attr):
            return getattr(self.__dict__['transport'], attr)

    class LogData(object):

        def __init__(self):
            self.request = []
            self.response = []

    def __init__(self, channel, queued=0):
        super(HTTPLoggingChannelRequest, self).__init__(channel, queued)

        if accounting.accountingEnabledForCategory("HTTP"):
            self.logData = HTTPLoggingChannelRequest.LogData()
            self.transport = HTTPLoggingChannelRequest.TransportLoggingWrapper(self.transport, self.logData.response)
        else:
            self.logData = None

    def gotInitialLine(self, initialLine):
        if self.logData is not None:
            self.startTime = time.time()
            self.logData.request.append(">>>> Request starting at: %.3f\r\n\r\n" % (self.startTime,))
            self.logData.request.append("%s\r\n" % (initialLine,))
        super(HTTPLoggingChannelRequest, self).gotInitialLine(initialLine)

    def lineReceived(self, line):

        if self.logData is not None:
            # We don't want to log basic credentials
            loggedLine = line
            if line.lower().startswith("authorization:"):
                bits = line[14:].strip().split(" ")
                if bits[0].lower() == "basic" and len(bits) == 2:
                    loggedLine = "%s %s %s" % (line[:14], bits[0], "X" * len(bits[1]))
            self.logData.request.append("%s\r\n" % (loggedLine,))
        super(HTTPLoggingChannelRequest, self).lineReceived(line)

    def handleContentChunk(self, data):

        if self.logData is not None:
            self.logData.request.append(data)
        super(HTTPLoggingChannelRequest, self).handleContentChunk(data)

    def handleContentComplete(self):

        if self.logData is not None:
            doneTime = time.time()
            self.logData.request.append("\r\n\r\n>>>> Request complete at: %.3f (elapsed: %.1f ms)" % (doneTime, 1000 * (doneTime - self.startTime),))
        super(HTTPLoggingChannelRequest, self).handleContentComplete()

    def writeHeaders(self, code, headers):
        if self.logData is not None:
            doneTime = time.time()
            self.logData.response.append("\r\n\r\n<<<< Response sending at: %.3f (elapsed: %.1f ms)\r\n\r\n" % (doneTime, 1000 * (doneTime - self.startTime),))
        super(HTTPLoggingChannelRequest, self).writeHeaders(code, headers)

    def finish(self):

        super(HTTPLoggingChannelRequest, self).finish()

        if self.logData is not None:
            doneTime = time.time()
            self.logData.response.append("\r\n\r\n<<<< Response complete at: %.3f (elapsed: %.1f ms)\r\n" % (doneTime, 1000 * (doneTime - self.startTime),))
            accounting.emitAccounting("HTTP", "", "".join(self.logData.request) + "".join(self.logData.response), self.command)

HTTPChannel.chanRequestFactory = HTTPLoggingChannelRequest


class LimitingHTTPFactory(HTTPFactory):
    """
    HTTPFactory which stores maxAccepts on behalf of the MaxAcceptPortMixin

    @ivar myServer: a reference to a L{MaxAcceptTCPServer} that this
        L{LimitingHTTPFactory} will limit.  This must be set externally.
    """

    def __init__(self, requestFactory, maxRequests=600, maxAccepts=100, **kwargs):
        HTTPFactory.__init__(self, requestFactory, maxRequests, **kwargs)
        self.maxAccepts = maxAccepts

    def buildProtocol(self, addr):
        """
        Override L{HTTPFactory.buildProtocol} in order to avoid ever returning
        an L{OverloadedServerProtocol}; this should be handled in other ways.
        """
        p = protocol.ServerFactory.buildProtocol(self, addr)
        for arg, value in self.protocolArgs.iteritems():
            setattr(p, arg, value)
        return p

    def addConnectedChannel(self, channel):
        """
        Override L{HTTPFactory.addConnectedChannel} to pause listening on the
        socket when there are too many outstanding channels.
        """
        HTTPFactory.addConnectedChannel(self, channel)
        if self.outstandingRequests >= self.maxRequests:
            self.myServer.myPort.stopReading()

    def removeConnectedChannel(self, channel):
        """
        Override L{HTTPFactory.removeConnectedChannel} to resume listening on the
        socket when there are too many outstanding channels.
        """
        HTTPFactory.removeConnectedChannel(self, channel)
        if self.outstandingRequests < self.maxRequests:
            self.myServer.myPort.startReading()


__all__ = [
    "HTTPFactory",
    "HTTP503LoggingFactory",
    "LimitingHTTPFactory",
    "SSLRedirectRequest",
]
