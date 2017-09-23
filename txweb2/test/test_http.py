
from __future__ import nested_scopes

import os
import sys
import time

from zope.interface import implements

from twisted.trial import unittest
from txweb2 import http, http_headers, responsecode, iweb, stream
from txweb2 import channel

from twisted.internet import reactor, protocol, address, interfaces, utils
from twisted.internet import defer
from twisted.internet.defer import waitForDeferred, deferredGenerator
from twisted.protocols import loopback
from twisted.python import util, runtime
from txweb2.channel.http import SSLRedirectRequest, HTTPFactory, HTTPChannel
from twisted.internet.task import deferLater
from twext.internet.ssl import ChainingOpenSSLContextFactory


class RedirectResponseTestCase(unittest.TestCase):

    def testTemporary(self):
        """
        Verify the "temporary" parameter sets the appropriate response code
        """
        req = http.RedirectResponse("http://example.com/", temporary=False)
        self.assertEquals(req.code, responsecode.MOVED_PERMANENTLY)
        req = http.RedirectResponse("http://example.com/", temporary=True)
        self.assertEquals(req.code, responsecode.TEMPORARY_REDIRECT)


class PreconditionTestCase(unittest.TestCase):

    def checkPreconditions(self, request, response, expectedResult, expectedCode,
                           **kw):
        preconditionsPass = True

        try:
            http.checkPreconditions(request, response, **kw)
        except http.HTTPError as e:
            preconditionsPass = False
            self.assertEquals(e.response.code, expectedCode)
        self.assertEquals(preconditionsPass, expectedResult)

    def testWithoutHeaders(self):
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        out_headers = http_headers.Headers()
        response = http.Response(responsecode.OK, out_headers, None)

        self.checkPreconditions(request, response, True, responsecode.OK)

        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        self.checkPreconditions(request, response, True, responsecode.OK)

        out_headers.removeHeader("ETag")
        out_headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT
        self.checkPreconditions(request, response, True, responsecode.OK)

        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        self.checkPreconditions(request, response, True, responsecode.OK)

    def testIfMatch(self):
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        out_headers = http_headers.Headers()
        response = http.Response(responsecode.OK, out_headers, None)

        # Behavior with no ETag set, should be same as with an ETag
        request.headers.setRawHeaders("If-Match", ('*',))
        self.checkPreconditions(request, response, True, responsecode.OK)
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED, entityExists=False)

        # Ask for tag, but no etag set.
        request.headers.setRawHeaders("If-Match", ('"frob"',))
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)

        # Actually set the ETag header
        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        out_headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT

        # behavior of entityExists
        request.headers.setRawHeaders("If-Match", ('*',))
        self.checkPreconditions(request, response, True, responsecode.OK)
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED, entityExists=False)

        # tag matches
        request.headers.setRawHeaders("If-Match", ('"frob", "foo"',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        # none match
        request.headers.setRawHeaders("If-Match", ('"baz", "bob"',))
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)

        # But if we have an error code already, ignore this header
        response.code = responsecode.INTERNAL_SERVER_ERROR
        self.checkPreconditions(request, response, True, responsecode.INTERNAL_SERVER_ERROR)
        response.code = responsecode.OK

        # Must only compare strong tags
        out_headers.setHeader("ETag", http_headers.ETag('foo', weak=True))
        request.headers.setRawHeaders("If-Match", ('W/"foo"',))
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)

    def testIfUnmodifiedSince(self):
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        out_headers = http_headers.Headers()
        response = http.Response(responsecode.OK, out_headers, None)

        # No Last-Modified => always fail.
        request.headers.setRawHeaders("If-Unmodified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)

        # Set output headers
        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        out_headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT

        request.headers.setRawHeaders("If-Unmodified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        request.headers.setRawHeaders("If-Unmodified-Since", ('Sat, 01 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)

        # But if we have an error code already, ignore this header
        response.code = responsecode.INTERNAL_SERVER_ERROR
        self.checkPreconditions(request, response, True, responsecode.INTERNAL_SERVER_ERROR)
        response.code = responsecode.OK

        # invalid date => header ignored
        request.headers.setRawHeaders("If-Unmodified-Since", ('alalalalalalalalalala',))
        self.checkPreconditions(request, response, True, responsecode.OK)

    def testIfModifiedSince(self):
        if time.time() < 946771200:
            self.fail(RuntimeError("Your computer's clock is way wrong, "
                                   "this test will be invalid."))

        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        out_headers = http_headers.Headers()
        response = http.Response(responsecode.OK, out_headers, None)

        # No Last-Modified => always succeed
        request.headers.setRawHeaders("If-Modified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        # Set output headers
        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        out_headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT

        request.headers.setRawHeaders("If-Modified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)

        # With a non-GET method
        request.method = "PUT"
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)
        request.method = "GET"

        request.headers.setRawHeaders("If-Modified-Since", ('Sat, 01 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        # But if we have an error code already, ignore this header
        response.code = responsecode.INTERNAL_SERVER_ERROR
        self.checkPreconditions(request, response, True, responsecode.INTERNAL_SERVER_ERROR)
        response.code = responsecode.OK

        # invalid date => header ignored
        request.headers.setRawHeaders("If-Modified-Since", ('alalalalalalalalalala',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        # date in the future => assume modified
        request.headers.setHeader("If-Modified-Since", time.time() + 500)
        self.checkPreconditions(request, response, True, responsecode.OK)

    def testIfNoneMatch(self):
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        out_headers = http_headers.Headers()
        response = http.Response(responsecode.OK, out_headers, None)

        request.headers.setRawHeaders("If-None-Match", ('"foo"',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        out_headers.setHeader("ETag", http_headers.ETag('foo'))
        out_headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT

        # behavior of entityExists
        request.headers.setRawHeaders("If-None-Match", ('*',))
        request.method = "PUT"
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)
        request.method = "GET"
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)
        self.checkPreconditions(request, response, True, responsecode.OK, entityExists=False)

        # tag matches
        request.headers.setRawHeaders("If-None-Match", ('"frob", "foo"',))
        request.method = "PUT"
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)
        request.method = "GET"
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)

        # now with IMS, also:
        request.headers.setRawHeaders("If-Modified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        request.method = "PUT"
        self.checkPreconditions(request, response, False, responsecode.PRECONDITION_FAILED)
        request.method = "GET"
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)

        request.headers.setRawHeaders("If-Modified-Since", ('Sat, 01 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        request.headers.removeHeader("If-Modified-Since")

        # none match
        request.headers.setRawHeaders("If-None-Match", ('"baz", "bob"',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        # now with IMS, also:
        request.headers.setRawHeaders("If-Modified-Since", ('Mon, 03 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        request.headers.setRawHeaders("If-Modified-Since", ('Sat, 01 Jan 2000 00:00:00 GMT',))
        self.checkPreconditions(request, response, True, responsecode.OK)

        request.headers.removeHeader("If-Modified-Since")

        # But if we have an error code already, ignore this header
        response.code = responsecode.INTERNAL_SERVER_ERROR
        self.checkPreconditions(request, response, True, responsecode.INTERNAL_SERVER_ERROR)
        response.code = responsecode.OK

        # Weak tags okay for GET
        out_headers.setHeader("ETag", http_headers.ETag('foo', weak=True))
        request.headers.setRawHeaders("If-None-Match", ('W/"foo"',))
        self.checkPreconditions(request, response, False, responsecode.NOT_MODIFIED)

        # Weak tags not okay for other methods
        request.method = "PUT"
        out_headers.setHeader("ETag", http_headers.ETag('foo', weak=True))
        request.headers.setRawHeaders("If-None-Match", ('W/"foo"',))
        self.checkPreconditions(request, response, True, responsecode.OK)

    def testNoResponse(self):
        # Ensure that passing etag/lastModified arguments instead of response works.
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        request.method = "PUT"
        request.headers.setRawHeaders("If-None-Match", ('"foo"',))

        self.checkPreconditions(request, None, True, responsecode.OK)
        self.checkPreconditions(request, None, False, responsecode.PRECONDITION_FAILED,
                                etag=http_headers.ETag('foo'),
                                lastModified=946771200)

        # Make sure that, while you shoudn't do this, that it doesn't cause an error
        request.method = "GET"
        self.checkPreconditions(request, None, False, responsecode.NOT_MODIFIED,
                                etag=http_headers.ETag('foo'))


class IfRangeTestCase(unittest.TestCase):

    def testIfRange(self):
        request = http.Request(None, "GET", "/", "HTTP/1.1", 0, http_headers.Headers())
        response = TestResponse()

        self.assertEquals(http.checkIfRange(request, response), True)

        request.headers.setRawHeaders("If-Range", ('"foo"',))
        self.assertEquals(http.checkIfRange(request, response), False)

        response.headers.setHeader("ETag", http_headers.ETag('foo'))
        self.assertEquals(http.checkIfRange(request, response), True)

        request.headers.setRawHeaders("If-Range", ('"bar"',))
        response.headers.setHeader("ETag", http_headers.ETag('foo'))
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('W/"foo"',))
        response.headers.setHeader("ETag", http_headers.ETag('foo', weak=True))
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('"foo"',))
        response.headers.removeHeader("ETag")
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('Sun, 02 Jan 2000 00:00:00 GMT',))
        response.headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT
        self.assertEquals(http.checkIfRange(request, response), True)

        request.headers.setRawHeaders("If-Range", ('Sun, 02 Jan 2000 00:00:01 GMT',))
        response.headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('Sun, 01 Jan 2000 23:59:59 GMT',))
        response.headers.setHeader("Last-Modified", 946771200)  # Sun, 02 Jan 2000 00:00:00 GMT
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('Sun, 01 Jan 2000 23:59:59 GMT',))
        response.headers.removeHeader("Last-Modified")
        self.assertEquals(http.checkIfRange(request, response), False)

        request.headers.setRawHeaders("If-Range", ('jwerlqjL#$Y*KJAN',))
        self.assertEquals(http.checkIfRange(request, response), False)


class LoopbackRelay(loopback.LoopbackRelay):
    implements(interfaces.IProducer)

    def pauseProducing(self):
        self.paused = True

    def resumeProducing(self):
        self.paused = False

    def stopProducing(self):
        self.loseConnection()

    def loseWriteConnection(self):
        # HACK.
        self.loseConnection()

    def abortConnection(self):
        self.aborted = True

    def getHost(self):
        """
        Synthesize a slightly more realistic 'host' thing.
        """
        return address.IPv4Address('TCP', 'localhost', 4321)


class TestRequestMixin(object):

    def __init__(self, *args, **kwargs):
        super(TestRequestMixin, self).__init__(*args, **kwargs)
        self.cmds = []
        headers = list(self.headers.getAllRawHeaders())
        headers.sort()
        self.cmds.append(('init', self.method, self.uri, self.clientproto, self.stream.length, tuple(headers)))

    def process(self):
        pass

    def handleContentChunk(self, data):
        self.cmds.append(('contentChunk', data))

    def handleContentComplete(self):
        self.cmds.append(('contentComplete',))

    def connectionLost(self, reason):
        self.cmds.append(('connectionLost', reason))

    def _finished(self, x):
        self._reallyFinished(x)


class TestRequest(TestRequestMixin, http.Request):
    """
    Stub request for testing.
    """


class TestSSLRedirectRequest(TestRequestMixin, SSLRedirectRequest):
    """
    Stub request for HSTS testing.
    """


class TestResponse(object):
    implements(iweb.IResponse)

    code = responsecode.OK
    headers = None

    def __init__(self):
        self.headers = http_headers.Headers()
        self.stream = stream.ProducerStream()

    def write(self, data):
        self.stream.write(data)

    def finish(self):
        self.stream.finish()


class TestClient(protocol.Protocol):
    data = ""
    done = False

    def dataReceived(self, data):
        self.data += data

    def write(self, data):
        self.transport.write(data)

    def connectionLost(self, reason):
        self.done = True
        self.transport.loseConnection()

    def loseConnection(self):
        self.done = True
        self.transport.loseConnection()


class TestConnection:

    def __init__(self):
        self.requests = []
        self.client = None
        self.callLaters = []

    def fakeCallLater(self, secs, f):
        assert secs == 0
        self.callLaters.append(f)


class HTTPTests(unittest.TestCase):

    requestClass = TestRequest

    def setUp(self):
        super(HTTPTests, self).setUp()

        # We always need this set to True - previous tests may have changed it
        HTTPChannel.allowPersistentConnections = True

    def connect(self, logFile=None, **protocol_kwargs):
        cxn = TestConnection()

        def makeTestRequest(*args):
            cxn.requests.append(self.requestClass(*args))
            return cxn.requests[-1]

        factory = channel.HTTPFactory(requestFactory=makeTestRequest,
                                      _callLater=cxn.fakeCallLater,
                                      **protocol_kwargs)

        cxn.client = TestClient()
        cxn.server = factory.buildProtocol(address.IPv4Address('TCP', '127.0.0.1', 2345))

        cxn.serverToClient = LoopbackRelay(cxn.client, logFile)
        cxn.clientToServer = LoopbackRelay(cxn.server, logFile)
        cxn.server.makeConnection(cxn.serverToClient)
        cxn.client.makeConnection(cxn.clientToServer)

        return cxn

    def iterate(self, cxn):
        callLaters = cxn.callLaters
        cxn.callLaters = []
        for f in callLaters:
            f()
        cxn.serverToClient.clearBuffer()
        cxn.clientToServer.clearBuffer()
        if cxn.serverToClient.shouldLose:
            cxn.serverToClient.clearBuffer()
        if cxn.clientToServer.shouldLose:
            cxn.clientToServer.clearBuffer()

    def compareResult(self, cxn, cmds, data):
        self.iterate(cxn)
        for receivedRequest, expectedCommands in map(None, cxn.requests, cmds):
            sortedHeaderCommands = []
            for cmd in expectedCommands:
                if len(cmd) == 6:
                    sortedHeaders = list(cmd[5])
                    sortedHeaders.sort()
                    sortedHeaderCommands.append(cmd[:5] + (tuple(sortedHeaders),))
                else:
                    sortedHeaderCommands.append(cmd)
            self.assertEquals(receivedRequest.cmds, sortedHeaderCommands)
        self.assertEquals(set(cxn.client.data.splitlines()), set(data.splitlines()))

    def assertDone(self, cxn, done=True):
        self.iterate(cxn)
        self.assertEquals(cxn.client.done, done)


class GracefulShutdownTestCase(HTTPTests):

    def _callback(self, result):
        self.callbackFired = True

    def testAllConnectionsClosedWithoutConnectedChannels(self):
        """
        allConnectionsClosed( ) should fire right away if no connected channels
        """
        self.callbackFired = False

        factory = HTTPFactory(None)
        factory.allConnectionsClosed().addCallback(self._callback)
        self.assertTrue(self.callbackFired)  # now!

    def testallConnectionsClosedWithConnectedChannels(self):
        """
        allConnectionsClosed( ) should only fire after all connected channels
        have been removed
        """
        self.callbackFired = False

        factory = HTTPFactory(None)
        factory.addConnectedChannel("A")
        factory.addConnectedChannel("B")
        factory.addConnectedChannel("C")

        factory.allConnectionsClosed().addCallback(self._callback)

        factory.removeConnectedChannel("A")
        self.assertFalse(self.callbackFired)  # wait for it...

        factory.removeConnectedChannel("B")
        self.assertFalse(self.callbackFired)  # wait for it...

        factory.removeConnectedChannel("C")
        self.assertTrue(self.callbackFired)  # now!


class CoreHTTPTestCase(HTTPTests):
    # Note: these tests compare the client output using string
    #       matching. It is acceptable for this to change and break
    #       the test if you know what you are doing.

    def testHTTP0_9(self, nouri=False):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        if nouri:
            cxn.client.write("GET\r\n")
        else:
            cxn.client.write("GET /\r\n")
        # Second request which should not be handled
        cxn.client.write("GET /two\r\n")

        cmds[0] += [('init', 'GET', '/', (0, 9), 0, ()), ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Yo", ("One", "Two"))
        cxn.requests[0].writeResponse(response)
        response.write("")

        self.compareResult(cxn, cmds, data)

        response.write("Output")
        data += "Output"
        self.compareResult(cxn, cmds, data)

        response.finish()
        self.compareResult(cxn, cmds, data)

        self.assertDone(cxn)

    def testHTTP0_9_nouri(self):
        self.testHTTP0_9(True)

    def testHTTP1_0(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.0\r\nContent-Length: 5\r\nHost: localhost\r\n\r\nInput")
        # Second request which should not be handled
        cxn.client.write("GET /two HTTP/1.0\r\n\r\n")

        cmds[0] += [('init', 'GET', '/', (1, 0), 5,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input'),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Yo", ("One", "Two"))
        cxn.requests[0].writeResponse(response)
        response.write("")

        data += "HTTP/1.1 200 OK\r\nYo: One\r\nYo: Two\r\nConnection: close\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        response.write("Output")
        data += "Output"
        self.compareResult(cxn, cmds, data)

        response.finish()
        self.compareResult(cxn, cmds, data)

        self.assertDone(cxn)

    def testHTTP1_0_keepalive(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.0\r\nConnection: keep-alive\r\nContent-Length: 5\r\nHost: localhost\r\n\r\nInput")
        cxn.client.write("GET /two HTTP/1.0\r\n\r\n")
        # Third request shouldn't be handled
        cxn.client.write("GET /three HTTP/1.0\r\n\r\n")

        cmds[0] += [('init', 'GET', '/', (1, 0), 5,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input'),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response0 = TestResponse()
        response0.headers.setRawHeaders("Content-Length", ("6",))
        response0.headers.setRawHeaders("Yo", ("One", "Two"))
        cxn.requests[0].writeResponse(response0)
        response0.write("")

        data += "HTTP/1.1 200 OK\r\nContent-Length: 6\r\nYo: One\r\nYo: Two\r\nConnection: Keep-Alive\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        response0.write("Output")
        data += "Output"
        self.compareResult(cxn, cmds, data)

        response0.finish()

        # Now for second request:
        cmds.append([])
        cmds[1] += [('init', 'GET', '/two', (1, 0), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response1 = TestResponse()
        response1.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[1].writeResponse(response1)
        response1.write("")

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        self.compareResult(cxn, cmds, data)
        response1.finish()

        self.assertDone(cxn)

    def testHTTP1_1_pipelining(self):
        cxn = self.connect(maxPipeline=2)
        cmds = []
        data = ""

        # Both these show up immediately.
        cxn.client.write("GET / HTTP/1.1\r\nContent-Length: 5\r\nHost: localhost\r\n\r\nInput")
        cxn.client.write("GET /two HTTP/1.1\r\nHost: localhost\r\n\r\n")
        # Doesn't show up until the first is done.
        cxn.client.write("GET /three HTTP/1.1\r\nHost: localhost\r\n\r\n")
        # Doesn't show up until the second is done.
        cxn.client.write("GET /four HTTP/1.1\r\nHost: localhost\r\n\r\n")

        cmds.append([])
        cmds[0] += [('init', 'GET', '/', (1, 1), 5,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input'),
                    ('contentComplete',)]
        cmds.append([])
        cmds[1] += [('init', 'GET', '/two', (1, 1), 0,
                     (('Host', ['localhost']),)),
                    ('contentComplete',)]

        self.compareResult(cxn, cmds, data)

        response0 = TestResponse()
        response0.headers.setRawHeaders("Content-Length", ("6",))
        cxn.requests[0].writeResponse(response0)
        response0.write("")

        data += "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        response0.write("Output")
        data += "Output"
        self.compareResult(cxn, cmds, data)

        response0.finish()

        # Now the third request gets read:
        cmds.append([])
        cmds[2] += [('init', 'GET', '/three', (1, 1), 0,
                     (('Host', ['localhost']),)),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        # Let's write out the third request before the second.
        # This should not cause anything to be written to the client.
        response2 = TestResponse()
        response2.headers.setRawHeaders("Content-Length", ("5",))
        cxn.requests[2].writeResponse(response2)

        response2.write("Three")
        response2.finish()

        self.compareResult(cxn, cmds, data)

        response1 = TestResponse()
        response1.headers.setRawHeaders("Content-Length", ("3",))
        cxn.requests[1].writeResponse(response1)
        response1.write("Two")

        data += "HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nTwo"
        self.compareResult(cxn, cmds, data)

        response1.finish()

        # Fourth request shows up
        cmds.append([])
        cmds[3] += [('init', 'GET', '/four', (1, 1), 0,
                     (('Host', ['localhost']),)),
                    ('contentComplete',)]
        data += "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nThree"
        self.compareResult(cxn, cmds, data)

        response3 = TestResponse()
        response3.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[3].writeResponse(response3)
        response3.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        self.assertDone(cxn, done=False)
        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testHTTP1_1_chunking(self, extraHeaders=""):
        cxn = self.connect()
        cmds = [[]]
        data = ""
        cxn.client.write("GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nHost: localhost\r\n\r\n5\r\nInput\r\n")

        cmds[0] += [('init', 'GET', '/', (1, 1), None,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input')]

        self.compareResult(cxn, cmds, data)

        cxn.client.write("1; blahblahblah\r\na\r\n10\r\nabcdefghijklmnop\r\n")
        cmds[0] += [('contentChunk', 'a'), ('contentChunk', 'abcdefghijklmnop')]
        self.compareResult(cxn, cmds, data)

        cxn.client.write("0\r\nRandom-Ignored-Trailer: foo\r\n\r\n")
        cmds[0] += [('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        cxn.requests[0].writeResponse(response)
        response.write("Output")
        expected = ["HTTP/1.1 200 OK"]
        if extraHeaders:
            expected.append(extraHeaders)
        expected.extend([
            "Transfer-Encoding: chunked",
            "",
            "6",
            "Output",
            "",
        ])
        data += "\r\n".join(expected)
        self.compareResult(cxn, cmds, data)

        response.write("blahblahblah")
        data += "C\r\nblahblahblah\r\n"
        self.compareResult(cxn, cmds, data)

        response.finish()
        data += "0\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testHTTP1_1_expect_continue(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""
        cxn.client.write("GET / HTTP/1.1\r\nContent-Length: 5\r\nHost: localhost\r\nExpect: 100-continue\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 5,
                     (('Expect', ['100-continue']), ('Host', ['localhost'])))]
        self.compareResult(cxn, cmds, data)

        cxn.requests[0].stream.read()
        data += "HTTP/1.1 100 Continue\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        cxn.client.write("Input")
        cmds[0] += [('contentChunk', 'Input'),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("6",))
        cxn.requests[0].writeResponse(response)
        response.write("Output")
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nOutput"
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testHTTP1_1_expect_continue_early_reply(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""
        cxn.client.write("GET / HTTP/1.1\r\nContent-Length: 5\r\nHost: localhost\r\nExpect: 100-continue\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 5,
                     (('Host', ['localhost']), ('Expect', ['100-continue'])))]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("6",))
        cxn.requests[0].writeResponse(response)
        response.write("Output")
        response.finish()

        cmds[0] += [('contentComplete',)]
        data += "HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nOutput"
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testHeaderContinuation(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\nHost: localhost\r\nFoo: yada\r\n yada\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0,
                     (('Host', ['localhost']), ('Foo', ['yada yada']),)),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testTimeout_immediate(self):
        # timeout 0 => timeout on first iterate call
        cxn = self.connect(inputTimeOut=0)
        return deferLater(reactor, 0, self.assertDone, cxn)

    def testTimeout_inRequest(self):
        cxn = self.connect(inputTimeOut=0.3)
        cxn.client.write("GET / HTTP/1.1\r\n")
        return deferLater(reactor, 0.5, self.assertDone, cxn)

    def testTimeout_betweenRequests(self):
        cxn = self.connect(betweenRequestsTimeOut=0.3)
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[0].writeResponse(response)
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

        self.compareResult(cxn, cmds, data)
        return deferLater(reactor, 0.5, self.assertDone, cxn)  # Wait for timeout

    def testTimeout_idleRequest(self):
        cxn = self.connect(idleTimeOut=0.3)
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        return deferLater(reactor, 0.5, self.assertDone, cxn)  # Wait for timeout

    def testTimeout_abortRequest(self):
        cxn = self.connect(allowPersistentConnections=False, closeTimeOut=0.3)
        cxn.client.transport.loseConnection = lambda: None
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[0].writeResponse(response)
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

        self.compareResult(cxn, cmds, data)

        def _check(cxn):
            self.assertDone(cxn)
            self.assertTrue(cxn.serverToClient.aborted)
        return deferLater(reactor, 0.5, self.assertDone, cxn)  # Wait for timeout

    def testConnectionCloseRequested(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        cxn.client.write("GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
        cmds.append([])
        cmds[1] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[0].writeResponse(response)
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[1].writeResponse(response)
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

        self.compareResult(cxn, cmds, data)
        self.assertDone(cxn)

    def testConnectionKeepAliveOff(self):
        cxn = self.connect(allowPersistentConnections=False)
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[0].writeResponse(response)
        response.finish()

        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

        self.compareResult(cxn, cmds, data)
        self.assertDone(cxn)

    def testExtraCRLFs(self):
        cxn = self.connect()
        cmds = [[]]
        data = ""

        # Some broken clients (old IEs) send an extra CRLF after post
        cxn.client.write("POST / HTTP/1.1\r\nContent-Length: 5\r\nHost: localhost\r\n\r\nInput\r\n")
        cmds[0] += [('init', 'POST', '/', (1, 1), 5,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input'),
                    ('contentComplete',)]

        self.compareResult(cxn, cmds, data)

        cxn.client.write("GET /two HTTP/1.1\r\n\r\n")
        cmds.append([])
        cmds[1] += [('init', 'GET', '/two', (1, 1), 0, ()),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)

    def testDisallowPersistentConnections(self):
        cxn = self.connect(allowPersistentConnections=False)
        cmds = [[]]
        data = ""

        cxn.client.write("GET / HTTP/1.1\r\nHost: localhost\r\n\r\nGET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        cmds[0] += [('init', 'GET', '/', (1, 1), 0,
                     (('Host', ['localhost']),)),
                    ('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.finish()
        cxn.requests[0].writeResponse(response)
        data += 'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
        self.compareResult(cxn, cmds, data)
        self.assertDone(cxn)

    def testIgnoreBogusContentLength(self):
        # Ensure that content-length is ignored when transfer-encoding
        # is also specified.
        cxn = self.connect()
        cmds = [[]]
        data = ""
        cxn.client.write("GET / HTTP/1.1\r\nContent-Length: 100\r\nTransfer-Encoding: chunked\r\nHost: localhost\r\n\r\n5\r\nInput\r\n")

        cmds[0] += [('init', 'GET', '/', (1, 1), None,
                     (('Host', ['localhost']),)),
                    ('contentChunk', 'Input')]

        self.compareResult(cxn, cmds, data)

        cxn.client.write("0\r\n\r\n")
        cmds[0] += [('contentComplete',)]
        self.compareResult(cxn, cmds, data)

        response = TestResponse()
        response.finish()
        cxn.requests[0].writeResponse(response)
        data += "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        self.compareResult(cxn, cmds, data)

        cxn.client.loseConnection()
        self.assertDone(cxn)


class ErrorTestCase(HTTPTests):

    def assertStartsWith(self, first, second, msg=None):
        self.assert_(first.startswith(second), '%r.startswith(%r)' % (first, second))

    def checkError(self, cxn, code):
        self.iterate(cxn)
        self.assertStartsWith(cxn.client.data, "HTTP/1.1 %d " % code)
        self.assertIn("\r\nConnection: close\r\n", cxn.client.data)
        # Ensure error messages have a defined content-length.
        self.assertIn("\r\nContent-Length:", cxn.client.data)
        self.assertDone(cxn)

    def testChunkingError1(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nasdf\r\n")

        self.checkError(cxn, 400)

    def testChunkingError2(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nblahblah\r\n")

        self.checkError(cxn, 400)

    def testChunkingError3(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\nasdf\r\n")

        self.checkError(cxn, 400)

    def testTooManyHeaders(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\n")
        cxn.client.write("Foo: Bar\r\n" * 5000)

        self.checkError(cxn, 400)

    def testLineTooLong(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\n")
        cxn.client.write("Foo: " + ("Bar" * 10000))

        self.checkError(cxn, 400)

    def testLineTooLong2(self):
        cxn = self.connect()
        cxn.client.write("GET " + ("/Bar") * 10000 + " HTTP/1.1\r\n")

        self.checkError(cxn, 414)

    def testNoColon(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\n")
        cxn.client.write("Blahblah\r\n\r\n")

        self.checkError(cxn, 400)

    def test_nonAsciiHeader(self):
        """
        As per U{RFC 822 section 3,
        <http://www.w3.org/Protocols/rfc822/3_Lexical.html#z0>}, headers are
        ASCII only.
        """
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\nX-Extra-Header: \xff\r\n\r\n")
        self.checkError(cxn, responsecode.BAD_REQUEST)
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\nX-E\xfftra-Header: foo\r\n\r\n")
        self.checkError(cxn, responsecode.BAD_REQUEST)

    def testBadRequest(self):
        cxn = self.connect()
        cxn.client.write("GET / more HTTP/1.1\r\n")

        self.checkError(cxn, 400)

    def testWrongProtocol(self):
        cxn = self.connect()
        cxn.client.write("GET / Foobar/1.0\r\n")

        self.checkError(cxn, 400)

    def testBadProtocolVersion(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1\r\n")

        self.checkError(cxn, 400)

    def testBadProtocolVersion2(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/-1.0\r\n")

        self.checkError(cxn, 400)

    def testWrongProtocolVersion(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/2.0\r\n")

        self.checkError(cxn, 505)

    def testUnsupportedTE(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\n")
        cxn.client.write("Transfer-Encoding: blahblahblah, chunked\r\n\r\n")
        self.checkError(cxn, 501)

    def testTEWithoutChunked(self):
        cxn = self.connect()
        cxn.client.write("GET / HTTP/1.1\r\n")
        cxn.client.write("Transfer-Encoding: gzip\r\n\r\n")
        self.checkError(cxn, 400)


class PipelinedErrorTestCase(ErrorTestCase):
    # Make sure that even low level reading errors don't corrupt the data stream,
    # but always wait until their turn to respond.

    def connect(self):
        cxn = ErrorTestCase.connect(self)
        cxn.client.write("GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")

        cmds = [[('init', 'GET', '/', (1, 1), 0,
                  (('Host', ['localhost']),)),
                 ('contentComplete',)]]
        data = ""
        self.compareResult(cxn, cmds, data)
        return cxn

    def checkError(self, cxn, code):
        self.iterate(cxn)
        self.assertEquals(cxn.client.data, '')

        response = TestResponse()
        response.headers.setRawHeaders("Content-Length", ("0",))
        cxn.requests[0].writeResponse(response)
        response.write('')

        data = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        self.iterate(cxn)
        self.assertEquals(cxn.client.data, data)

        # Reset the data so the checkError's startswith test can work right.
        cxn.client.data = ""

        response.finish()
        ErrorTestCase.checkError(self, cxn, code)


class SimpleFactory(channel.HTTPFactory):

    def buildProtocol(self, addr):
        # Do a bunch of crazy crap just so that the test case can know when the
        # connection is done.
        p = channel.HTTPFactory.buildProtocol(self, addr)
        cl = p.connectionLost

        def newCl(reason):
            reactor.callLater(0, lambda: self.testcase.connlost.callback(None))
            return cl(reason)
        p.connectionLost = newCl
        self.conn = p
        return p


class SimpleRequest(http.Request):

    def process(self):
        response = TestResponse()
        if self.uri == "/error":
            response.code = 402
        elif self.uri == "/forbidden":
            response.code = 403
        else:
            response.code = 404
            response.write("URI %s unrecognized." % self.uri)
        response.finish()
        self.writeResponse(response)


class AbstractServerTestMixin:

    type = None

    def testBasicWorkingness(self):
        args = ('-u', util.sibpath(__file__, "simple_client.py"), "basic",
                str(self.port), self.type)
        d = waitForDeferred(
            utils.getProcessOutputAndValue(sys.executable, args=args,
                                           env=os.environ)
        )
        yield d
        out, err, code = d.getResult()

        self.assertEquals(code, 0, "Error output:\n%s" % (err,))
        self.assertEquals(out, "HTTP/1.1 402 Payment Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    testBasicWorkingness = deferredGenerator(testBasicWorkingness)

    def testLingeringClose(self):
        args = ('-u', util.sibpath(__file__, "simple_client.py"),
                "lingeringClose", str(self.port), self.type)
        d = waitForDeferred(
            utils.getProcessOutputAndValue(sys.executable, args=args,
                                           env=os.environ)
        )
        yield d
        out, err, code = d.getResult()
        self.assertEquals(code, 0, "Error output:\n%s" % (err,))
        self.assertEquals(out, "HTTP/1.1 402 Payment Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    testLingeringClose = deferredGenerator(testLingeringClose)


class TCPServerTest(unittest.TestCase, AbstractServerTestMixin):

    type = 'tcp'

    def setUp(self):
        factory = SimpleFactory(requestFactory=SimpleRequest)

        factory.testcase = self
        self.factory = factory
        self.connlost = defer.Deferred()

        self.socket = reactor.listenTCP(0, factory)
        self.port = self.socket.getHost().port

    def tearDown(self):
        # Make sure the listening port is closed
        d = defer.maybeDeferred(self.socket.stopListening)

        def finish(v):
            # And make sure the established connection is, too
            self.factory.conn.transport.loseConnection()
            return self.connlost
        return d.addCallback(finish)


try:
    from twisted.internet import ssl
    ssl  # pyflakes
except ImportError:
    # happens the first time the interpreter tries to import it
    ssl = None
if ssl and not ssl.supported:
    # happens second and later times
    ssl = None

certPath = util.sibpath(__file__, "server.pem")


class SSLServerTest(unittest.TestCase, AbstractServerTestMixin):
    type = 'ssl'

    def setUp(self):
        HTTPChannel.allowPersistentConnections = True
        sCTX = ChainingOpenSSLContextFactory(
            certPath, certPath,
            keychainIdentity="org.calendarserver.test",
        )
        factory = SimpleFactory(requestFactory=SimpleRequest)

        factory.testcase = self
        self.factory = factory
        self.connlost = defer.Deferred()

        self.socket = reactor.listenSSL(0, factory, sCTX)
        self.port = self.socket.getHost().port

    def tearDown(self):
        # Make sure the listening port is closed
        d = defer.maybeDeferred(self.socket.stopListening)

        def finish(v):
            # And make sure the established connection is, too
            self.factory.conn.transport.loseConnection()
            return self.connlost
        return d.addCallback(finish)

    def testLingeringClose(self):
        return super(SSLServerTest, self).testLingeringClose()

    if runtime.platform.isWindows():
        # This may not just be Windows, but all platforms with more recent
        # versions of OpenSSL.  Do some more experimentation...
        testLingeringClose.todo = "buffering kills the connection too early; test this some other way"


if interfaces.IReactorProcess(reactor, None) is None:
    TCPServerTest.skip = SSLServerTest.skip = "Required process support missing from reactor"
elif interfaces.IReactorSSL(reactor, None) is None:
    SSLServerTest.skip = "Required SSL support missing from reactor"
elif ssl is None:
    SSLServerTest.skip = "SSL not available, cannot test SSL."
