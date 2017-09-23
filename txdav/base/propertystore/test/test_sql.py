##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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

"""
Tests for txdav.caldav.datastore.postgres, mostly based on
L{txdav.caldav.datastore.test.common}.
"""

from twisted.internet.defer import inlineCallbacks, returnValue

from twistedcaldav.memcacher import Memcacher

from txdav.common.datastore.test.util import buildStore, StubNotifierFactory

from txdav.base.propertystore.test.base import (
    PropertyStoreTest, propertyName, propertyValue)

from twisted.internet.defer import gatherResults
from twext.enterprise.ienterprise import AlreadyFinishedError

try:
    from txdav.base.propertystore.sql import PropertyStore
except ImportError as e:
    # XXX: when could this ever fail?
    PropertyStore = None
    importErrorMessage = str(e)


class PropertyStoreTest(PropertyStoreTest):

    @inlineCallbacks
    def setUp(self):
        self.notifierFactory = StubNotifierFactory()
        self.store = yield buildStore(self, self.notifierFactory)
        self.addCleanup(self.maybeCommitLast)
        self._txn = self.store.newTransaction()
        self.propertyStore = \
            self.propertyStore1 = yield PropertyStore.load("user01", None, None, self._txn, 1)
        self.propertyStore2 = yield PropertyStore.load("user01", "user02", None, self._txn, 1)
        self.propertyStore3 = yield PropertyStore.load("user01", None, "user03", self._txn, 1)
        self.propertyStore4 = yield PropertyStore.load("user01", "user02", "user04", self._txn, 1)

    @inlineCallbacks
    def maybeCommitLast(self):
        if hasattr(self, "_txn"):
            result = yield self._txn.commit()
            delattr(self, "_txn")
        else:
            result = None
        self.propertyStore = \
            self.propertyStore1 = \
            self.propertyStore2 = \
            self.propertyStore3 = \
            self.propertyStore4 = None
        returnValue(result)

    @inlineCallbacks
    def _changed(self, store):
        if hasattr(self, "_txn"):
            yield self._txn.commit()
            delattr(self, "_txn")
        self._txn = self.store.newTransaction()

        store = self.propertyStore1
        self.propertyStore = \
            self.propertyStore1 = yield PropertyStore.load("user01", None, None, self._txn, 1)
        self.propertyStore1._shadowableKeys = store._shadowableKeys
        self.propertyStore1._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore1._globalKeys = store._globalKeys

        store = self.propertyStore2
        self.propertyStore2 = yield PropertyStore.load("user01", "user02", None, self._txn, 1)
        self.propertyStore2._shadowableKeys = store._shadowableKeys
        self.propertyStore2._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore2._globalKeys = store._globalKeys

        store = self.propertyStore3
        self.propertyStore3 = yield PropertyStore.load("user01", None, "user03", self._txn, 1)
        self.propertyStore3._shadowableKeys = store._shadowableKeys
        self.propertyStore3._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore3._globalKeys = store._globalKeys

        store = self.propertyStore4
        self.propertyStore4 = yield PropertyStore.load("user01", "user02", "user04", self._txn, 1)
        self.propertyStore4._shadowableKeys = store._shadowableKeys
        self.propertyStore4._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore4._globalKeys = store._globalKeys

    @inlineCallbacks
    def _abort(self, store):
        if hasattr(self, "_txn"):
            yield self._txn.abort()
            delattr(self, "_txn")

        self._txn = self.store.newTransaction()

        store = self.propertyStore1
        self.propertyStore = \
            self.propertyStore1 = yield PropertyStore.load("user01", None, None, self._txn, 1)
        self.propertyStore1._shadowableKeys = store._shadowableKeys
        self.propertyStore1._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore1._globalKeys = store._globalKeys

        store = self.propertyStore2
        self.propertyStore2 = yield PropertyStore.load("user01", "user02", None, self._txn, 1)
        self.propertyStore2._shadowableKeys = store._shadowableKeys
        self.propertyStore2._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore2._globalKeys = store._globalKeys

        store = self.propertyStore3
        self.propertyStore3 = yield PropertyStore.load("user01", None, "user03", self._txn, 1)
        self.propertyStore3._shadowableKeys = store._shadowableKeys
        self.propertyStore3._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore3._globalKeys = store._globalKeys

        store = self.propertyStore4
        self.propertyStore4 = yield PropertyStore.load("user01", "user02", "user04", self._txn, 1)
        self.propertyStore4._shadowableKeys = store._shadowableKeys
        self.propertyStore4._proxyOverrideKeys = store._proxyOverrideKeys
        self.propertyStore4._globalKeys = store._globalKeys

    @inlineCallbacks
    def test_concurrentInsertion(self):
        """
        When two property stores set the same value, both should succeed, and
        update the cache.  Whoever wins the race (i.e. updates last) will set
        the last property value.
        """
        pname = propertyName("concurrent")
        pval1 = propertyValue("alpha")
        pval2 = propertyValue("beta")
        concurrentTxn = self.store.newTransaction()

        @inlineCallbacks
        def maybeAbortIt():
            try:
                yield concurrentTxn.abort()
            except AlreadyFinishedError:
                pass
        self.addCleanup(maybeAbortIt)
        concurrentPropertyStore = yield PropertyStore.load(
            "user01", None, None, concurrentTxn, 1
        )
        concurrentPropertyStore[pname] = pval1
        race = []

        def tiebreaker(label):
            # Let's not get into the business of figuring out who the database
            # concurrency rules are supposed to pick; it might differ.  We just
            # take the answer we're given for who gets to be the final writer,
            # and make sure that matches the property read in the next
            # transaction.
            def breaktie(result):
                race.append(label)
                return result
            return breaktie
        a = concurrentTxn.commit().addCallback(tiebreaker('a'))
        self.propertyStore[pname] = pval2
        b = self._txn.commit().addCallback(tiebreaker('b'))
        del self._txn
        self.assertEquals((yield gatherResults([a, b])), [None, None])
        yield self._abort(self.propertyStore)
        winner = {'a': pval1,
                  'b': pval2}[race[-1]]
        self.assertEquals(self.propertyStore[pname], winner)

    @inlineCallbacks
    def test_copy(self):

        # Existing store
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 2)
        store1_user2 = yield PropertyStore.load("user01", "user02", None, self._txn, 2)

        # Populate current store with data
        props_user1 = (
            (propertyName("dummy1"), propertyValue("value1-user1")),
            (propertyName("dummy2"), propertyValue("value2-user1")),
        )
        props_user2 = (
            (propertyName("dummy1"), propertyValue("value1-user2")),
            (propertyName("dummy3"), propertyValue("value3-user2")),
        )

        for name, value in props_user1:
            store1_user1[name] = value
        for name, value in props_user2:
            store1_user2[name] = value

        yield self._txn.commit()

        self._txn = self.store.newTransaction()

        # Existing store
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 2)

        # New store
        store2_user1 = yield PropertyStore.load("user01", None, None, self._txn, 3)

        # Do copy and check results
        yield store2_user1.copyAllProperties(store1_user1)

        self.assertEqual(store1_user1.keys(), store2_user1.keys())

        store1_user2 = yield PropertyStore.load("user01", "user02", None, self._txn, 2)
        store2_user2 = yield PropertyStore.load("user01", "user02", None, self._txn, 3)
        self.assertEqual(store1_user2.keys(), store2_user2.keys())

    @inlineCallbacks
    def test_insert_delete(self):

        # Existing store
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 2)

        pname = propertyName("dummy1")
        pvalue = propertyValue("value1-user1")

        yield store1_user1.__setitem__(pname, pvalue)
        self.assertEqual(store1_user1[pname], pvalue)

        yield store1_user1.__delitem__(pname)
        self.assertTrue(pname not in store1_user1)

        yield store1_user1.__setitem__(pname, pvalue)
        self.assertEqual(store1_user1[pname], pvalue)

    @inlineCallbacks
    def test_cacher_failure(self):
        """
        Test that properties can still be read and written even when they are too larger for the
        cacher to handle.
        """

        # Existing store - add a normal property
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 10)
        self.assertTrue("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)

        pname1 = propertyName("dummy1")
        pvalue1 = propertyValue("*")

        yield store1_user1.__setitem__(pname1, pvalue1)
        self.assertEqual(store1_user1[pname1], pvalue1)

        self.assertEqual(len(store1_user1._cached), 1)

        yield self._txn.commit()

        # Existing store - add a large property
        self._txn = self.store.newTransaction()
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 10)
        self.assertTrue("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)

        pname2 = propertyName("dummy2")
        pvalue2 = propertyValue("*" * (Memcacher.MEMCACHE_VALUE_LIMIT + 10))

        yield store1_user1.__setitem__(pname2, pvalue2)
        self.assertEqual(store1_user1[pname2], pvalue2)

        self.assertEqual(len(store1_user1._cached), 2)

        yield self._txn.commit()

        # Try again - the cacher will fail large values
        self._txn = self.store.newTransaction()
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 10)
        self.assertFalse("SQL.props:10/user01" in store1_user1._cacher._memcacheProtocol._cache)

        self.assertEqual(store1_user1[pname1], pvalue1)
        self.assertEqual(store1_user1[pname2], pvalue2)
        self.assertEqual(len(store1_user1._cached), 2)

        yield store1_user1.__delitem__(pname1)
        self.assertTrue(pname1 not in store1_user1)

        yield store1_user1.__delitem__(pname2)
        self.assertTrue(pname2 not in store1_user1)

        self.assertEqual(len(store1_user1._cached), 0)
        self.assertFalse("SQL.props:10/user01" in store1_user1._cacher._memcacheProtocol._cache)

    @inlineCallbacks
    def test_cacher_off(self):
        """
        Test that properties can still be read and written when the cacher is disabled.
        """

        self.patch(self.store, "queryCacher", None)

        # Existing store - add a normal property
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 10)
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)

        pname1 = propertyName("dummy1")
        pvalue1 = propertyValue("*")

        yield store1_user1.__setitem__(pname1, pvalue1)
        self.assertEqual(store1_user1[pname1], pvalue1)

        self.assertEqual(len(store1_user1._cached), 1)
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)

        yield self._txn.commit()
        self._txn = self.store.newTransaction()

        # Existing store - check a normal property
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        store1_user1 = yield PropertyStore.load("user01", None, None, self._txn, 10)
        self.assertFalse("SQL.props:10/user01" in PropertyStore._cacher._memcacheProtocol._cache)
        self.assertEqual(store1_user1[pname1], pvalue1)


if PropertyStore is None:
    PropertyStoreTest.skip = importErrorMessage
