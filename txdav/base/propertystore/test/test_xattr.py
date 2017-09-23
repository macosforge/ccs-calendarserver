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
Property store tests.
"""

from twext.python.filepath import CachingFilePath as FilePath
from txdav.xml.base import WebDAVTextElement
from txdav.base.propertystore.base import PropertyName
from txdav.base.propertystore.test import base

try:
    from txdav.base.propertystore.xattr import PropertyStore
    from xattr import xattr
except ImportError as e:
    PropertyStore = None
    importErrorMessage = str(e)


class PropertyStoreTest(base.PropertyStoreTest):

    def setUp(self):
        tempDir = FilePath(self.mktemp())
        tempDir.makedirs()
        tempFile = tempDir.child("test")
        tempFile.touch()
        self.propertyStore = PropertyStore("user01", lambda: tempFile)
        self.propertyStore1 = self.propertyStore
        self.propertyStore2 = PropertyStore("user01", lambda: tempFile)
        self.propertyStore2._setPerUserUID("user02")
        self.propertyStore2._setProxyUID("user02")
        self.propertyStore3 = PropertyStore("user01", lambda: tempFile)
        self.propertyStore3._setProxyUID("user03")
        self.propertyStore4 = PropertyStore("user01", lambda: tempFile)
        self.propertyStore4._setPerUserUID("user02")
        self.propertyStore4._setProxyUID("user04")

    def test_init(self):
        store = self.propertyStore
        self.failUnless(isinstance(store.attrs, xattr))
        self.assertEquals(store.removed, set())
        self.assertEquals(store.modified, {})

    def test_abort(self):
        super(PropertyStoreTest, self).test_abort()
        store = self.propertyStore
        self.assertEquals(store.removed, set())
        self.assertEquals(store.modified, {})

    def test_compress(self):

        class DummyProperty (WebDAVTextElement):
            namespace = "http://calendarserver.org/ns/"
            name = "dummy"

        name = PropertyName.fromElement(DummyProperty)
        compressedKey = self.propertyStore._encodeKey((name, self.propertyStore._defaultUser))
        uncompressedKey = self.propertyStore._encodeKey((name, self.propertyStore._defaultUser), compressNamespace=False)

        self.propertyStore[name] = DummyProperty.fromString("data")
        self.propertyStore.flush()
        self.assertEqual(self.propertyStore[name], DummyProperty.fromString("data"))
        self.assertTrue(compressedKey in self.propertyStore.attrs)
        self.assertFalse(uncompressedKey in self.propertyStore.attrs)

    def test_compress_upgrade(self):

        class DummyProperty (WebDAVTextElement):
            namespace = "http://calendarserver.org/ns/"
            name = "dummy"

        name = PropertyName.fromElement(DummyProperty)
        uncompressedKey = self.propertyStore._encodeKey((name, self.propertyStore._defaultUser), compressNamespace=False)
        self.propertyStore.attrs[uncompressedKey] = DummyProperty.fromString("data").toxml()
        self.assertEqual(self.propertyStore[name], DummyProperty.fromString("data"))
        self.assertRaises(KeyError, lambda: self.propertyStore.attrs[uncompressedKey])

    def test_copy(self):

        tempDir = FilePath(self.mktemp())
        tempDir.makedirs()
        tempFile1 = tempDir.child("test1")
        tempFile1.touch()
        tempFile2 = tempDir.child("test2")
        tempFile2.touch()

        # Existing store
        store1_user1 = PropertyStore("user01", lambda: tempFile1)
        store1_user2 = PropertyStore("user01", lambda: tempFile1)
        store1_user2._setPerUserUID("user02")

        # New store
        store2_user1 = PropertyStore("user01", lambda: tempFile2)
        store2_user2 = PropertyStore("user01", lambda: tempFile2)
        store2_user2._setPerUserUID("user02")

        # Populate current store with data
        class DummyProperty1(WebDAVTextElement):
            namespace = "http://calendarserver.org/ns/"
            name = "dummy1"

        class DummyProperty2(WebDAVTextElement):
            namespace = "http://calendarserver.org/ns/"
            name = "dummy2"

        class DummyProperty3(WebDAVTextElement):
            namespace = "http://calendarserver.org/ns/"
            name = "dummy3"

        props_user1 = (
            DummyProperty1.fromString("value1-user1"),
            DummyProperty2.fromString("value2-user1"),
        )
        props_user2 = (
            DummyProperty1.fromString("value1-user2"),
            DummyProperty3.fromString("value3-user2"),
        )

        for prop in props_user1:
            store1_user1[PropertyName.fromElement(prop)] = prop
        for prop in props_user2:
            store1_user2[PropertyName.fromElement(prop)] = prop
        store1_user1.flush()
        store1_user2.flush()

        # Do copy and check results
        store2_user1.copyAllProperties(store1_user1)
        store2_user1.flush()

        self.assertEqual(store1_user1.attrs.items(), store2_user1.attrs.items())
        self.assertEqual(store1_user2.attrs.items(), store2_user2.attrs.items())

if PropertyStore is None:
    PropertyStoreTest.skip = importErrorMessage
