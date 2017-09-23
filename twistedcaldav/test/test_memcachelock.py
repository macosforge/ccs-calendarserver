# Copyright (c) 2007 Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Test the memcache client protocol.
"""

from twisted.test.proto_helpers import StringTransportWithDisconnection
from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks
from twisted.protocols.memcache import MemCacheProtocol

from twistedcaldav.memcachelock import MemcacheLock, MemcacheLockTimeoutError

from twistedcaldav.test.util import TestCase


class MemCacheTestCase(TestCase):
    """
    Test client protocol class L{MemCacheProtocol}.
    """

    class FakedMemcacheLock(MemcacheLock):

        def __init__(
            self, faked, namespace, locktoken,
            timeout=5.0, retry_interval=0.1, expire_time=0
        ):
            """
            @param namespace: a unique namespace for this lock's tokens
            @type namespace: C{str}

            @param locktoken: the name of the locktoken
            @type locktoken: C{str}

            @param timeout: the maximum time in seconds that the lock should
                block
            @type timeout: C{float}

            @param retry_interval: the interval to retry acquiring the lock
            @type retry_interval: C{float}

            @param expiryTime: the time in seconds for the lock to expire.
                Zero: no expiration.
            @type expiryTime: C{float}
            """

            super(MemCacheTestCase.FakedMemcacheLock, self).__init__(
                namespace, locktoken, timeout, retry_interval, expire_time
            )
            self.faked = faked

        def _getMemcacheProtocol(self):

            return self.faked

    def setUp(self):
        """
        Create a memcache client, connect it to a string protocol, and make it
        use a deterministic clock.
        """
        TestCase.setUp(self)
        self.proto = MemCacheProtocol()
        self.clock = Clock()
        self.proto.callLater = self.clock.callLater
        self.transport = StringTransportWithDisconnection()
        self.transport.protocol = self.proto
        self.proto.makeConnection(self.transport)

    def _test(self, d, send, recv, result):
        """
        Shortcut method for classic tests.

        @param d: the resulting deferred from the memcache command.
        @type d: C{Deferred}

        @param send: the expected data to be sent.
        @type send: C{str}

        @param recv: the data to simulate as reception.
        @type recv: C{str}

        @param result: the expected result.
        @type result: C{any}
        """
        def cb(res):
            self.assertEquals(res, result)
        self.assertEquals(self.transport.value(), send)
        self.transport.clear()
        d.addCallback(cb)
        self.proto.dataReceived(recv)
        return d

    def test_get(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking"
        )
        return self._test(
            lock.get("foo"),
            "get lock:foo-acbd18db4cc2f85cedef654fccc4a4d8\r\n",
            (
                "VALUE lock:foo-acbd18db4cc2f85cedef654fccc4a4d8 0 3\r\n"
                "bar\r\nEND\r\n"
            ),
            "bar"
        )

    def test_set(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking"
        )
        return self._test(
            lock.set("foo", "bar"),
            "set lock:foo-acbd18db4cc2f85cedef654fccc4a4d8 0 0 3\r\nbar\r\n",
            "STORED\r\n",
            True
        )

    @inlineCallbacks
    def test_acquire(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking"
        )
        yield self._test(
            lock.acquire(),
            "add lock:locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n1\r\n",
            "STORED\r\n",
            True
        )
        self.assertTrue(lock._hasLock)

    @inlineCallbacks
    def test_acquire_ok_timeout_0(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking", timeout=0
        )
        yield self._test(
            lock.acquire(),
            "add lock:locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n1\r\n",
            "STORED\r\n",
            True
        )
        self.assertTrue(lock._hasLock)

    @inlineCallbacks
    def test_acquire_fails_timeout_0(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking", timeout=0
        )
        try:
            yield self._test(
                lock.acquire(),
                (
                    "add lock:"
                    "locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n"
                    "1\r\n"
                ),
                "NOT_STORED\r\n",
                True
            )
        except MemcacheLockTimeoutError:
            pass
        except Exception as e:
            self.fail("Unknown exception thrown: %s" % (e,))
        else:
            self.fail("No timeout exception thrown")
        self.assertFalse(lock._hasLock)

    @inlineCallbacks
    def test_acquire_release(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking"
        )
        yield self._test(
            lock.acquire(),
            "add lock:locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n1\r\n",
            "STORED\r\n",
            True
        )
        self.assertTrue(lock._hasLock)
        yield self._test(
            lock.release(),
            "delete lock:locking-559159aa00cc525bfe5c4b34cf16cccb\r\n",
            "DELETED\r\n",
            True
        )
        self.assertFalse(lock._hasLock)

    @inlineCallbacks
    def test_acquire_clean(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", "locking"
        )
        yield self._test(
            lock.acquire(),
            "add lock:locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n1\r\n",
            "STORED\r\n",
            True
        )
        yield self._test(
            lock.clean(),
            "delete lock:locking-559159aa00cc525bfe5c4b34cf16cccb\r\n",
            "DELETED\r\n",
            True
        )

    @inlineCallbacks
    def test_acquire_unicode(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """
        lock = MemCacheTestCase.FakedMemcacheLock(
            self.proto, "lock", u"locking"
        )
        yield self._test(
            lock.acquire(),
            "add lock:locking-559159aa00cc525bfe5c4b34cf16cccb 0 0 1\r\n1\r\n",
            "STORED\r\n",
            True
        )
        self.assertTrue(lock._hasLock)

    @inlineCallbacks
    def test_acquire_invalid_token1(self):
        """
        L{MemCacheProtocol.get} should return a L{Deferred} which is
        called back with the value and the flag associated with the given key
        if the server returns a successful result.
        """

        try:
            lock = MemCacheTestCase.FakedMemcacheLock(self.proto, "lock", 1)
            yield lock.acquire()
            self.fail("AssertionError not raised")
        except AssertionError:
            pass
        except:
            self.fail("AssertionError not raised")

        try:
            lock = MemCacheTestCase.FakedMemcacheLock(
                self.proto, "lock", ("abc",)
            )
            yield lock.acquire()
            self.fail("AssertionError not raised")
        except AssertionError:
            pass
        except:
            self.fail("AssertionError not raised")
