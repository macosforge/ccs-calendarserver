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
Generic property store tests.
"""

__all__ = [
    "PropertyStoreTest",
    "propertyName",
    "propertyValue",
]


from zope.interface.verify import verifyObject, BrokenMethodImplementation

from twisted.internet.defer import inlineCallbacks
from twisted.trial import unittest

from txdav.xml import element as davxml

from txdav.idav import IPropertyStore
from txdav.base.propertystore.base import PropertyName


class NonePropertyStoreTest(unittest.TestCase):
    # Subclass must define self.propertyStore in setUp().

    def test_interface(self):
        try:
            verifyObject(IPropertyStore, self.propertyStore)
        except BrokenMethodImplementation as e:
            self.fail(e)

    def test_delete_none(self):
        def doDelete():
            del self.propertyStore[propertyName("xyzzy")]

        self.assertRaises(KeyError, doDelete)

    def test_keyInPropertyName(self):
        def doGet():
            self.propertyStore["xyzzy"]

        def doSet():
            self.propertyStore["xyzzy"] = propertyValue("Hello, World!")

        def doDelete():
            del self.propertyStore["xyzzy"]

        def doContains():
            return "xyzzy" in self.propertyStore

        self.assertRaises(TypeError, doGet)
        self.assertRaises(TypeError, doSet)
        self.assertRaises(TypeError, doDelete)
        self.assertRaises(TypeError, doContains)


class PropertyStoreTest(NonePropertyStoreTest):
    # Subclass must define self.propertyStore in setUp().

    def _changed(self, store):
        store.flush()

    def _abort(self, store):
        store.abort()

    @inlineCallbacks
    def test_set_get_contains(self):

        name = propertyName("test")
        value = propertyValue("Hello, World!")

        # Test with commit after change
        self.propertyStore[name] = value
        yield self._changed(self.propertyStore)
        self.assertEquals(self.propertyStore.get(name, None), value)
        self.failUnless(name in self.propertyStore)

        # Test without commit after change
        value = propertyValue("Hello, Universe!")
        self.propertyStore[name] = value
        self.assertEquals(self.propertyStore.get(name, None), value)
        self.failUnless(name in self.propertyStore)

    @inlineCallbacks
    def test_delete_get_contains(self):

        # Test with commit after change
        name = propertyName("test")
        value = propertyValue("Hello, World!")

        self.propertyStore[name] = value
        yield self._changed(self.propertyStore)

        del self.propertyStore[name]
        yield self._changed(self.propertyStore)

        self.assertEquals(self.propertyStore.get(name, None), None)
        self.failIf(name in self.propertyStore)

        # Test without commit after change
        name = propertyName("test")
        value = propertyValue("Hello, Universe!")

        self.propertyStore[name] = value
        yield self._changed(self.propertyStore)

        del self.propertyStore[name]

        self.assertEquals(self.propertyStore.get(name, None), None)
        self.failIf(name in self.propertyStore)

    @inlineCallbacks
    def test_peruser(self):

        name = propertyName("test")
        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        self.propertyStore2[name] = value2
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value2)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore2[name]
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        del self.propertyStore1[name]
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_peruserShadow(self):

        name = propertyName("shadow")

        self.propertyStore1.setSpecialProperties((name,), (), ())
        self.propertyStore2.setSpecialProperties((name,), (), ())
        self.propertyStore3.setSpecialProperties((name,), (), ())
        self.propertyStore4.setSpecialProperties((name,), (), ())

        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        self.propertyStore2[name] = value2
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value2)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore2[name]
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore1[name]
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_peruserShadow_delete(self):
        """
        Delete a shadowable property that has not been overridden by the sharee.
        """

        name = propertyName("shadow")

        self.propertyStore1.setSpecialProperties((name,), (), ())
        self.propertyStore2.setSpecialProperties((name,), (), ())
        self.propertyStore3.setSpecialProperties((name,), (), ())
        self.propertyStore4.setSpecialProperties((name,), (), ())

        value1 = propertyValue("Hello, World1!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore2[name]
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore1[name]
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_peruser_global(self):

        name = propertyName("global")

        self.propertyStore1.setSpecialProperties((), (name,), ())
        self.propertyStore2.setSpecialProperties((), (name,), ())
        self.propertyStore3.setSpecialProperties((), (name,), ())
        self.propertyStore4.setSpecialProperties((), (name,), ())

        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        self.propertyStore2[name] = value2
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value2)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value2)
        self.assertEquals(self.propertyStore4.get(name, None), value2)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore2[name]
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_proxy(self):

        name = propertyName("test")
        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")

        self.propertyStore3[name] = value1
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        self.propertyStore4[name] = value2
        yield self._changed(self.propertyStore4)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value2)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore4[name]
        yield self._changed(self.propertyStore4)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        del self.propertyStore3[name]
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_proxyOverride(self):

        name = propertyName("override")

        self.propertyStore1.setSpecialProperties((), (), (name,))
        self.propertyStore2.setSpecialProperties((), (), (name,))
        self.propertyStore3.setSpecialProperties((), (), (name,))
        self.propertyStore4.setSpecialProperties((), (), (name,))

        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        self.propertyStore3[name] = value2
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value2)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        del self.propertyStore3[name]
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failUnless(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

        del self.propertyStore1[name]
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    @inlineCallbacks
    def test_proxyOverrideShadow(self):

        name = propertyName("override")

        self.propertyStore1.setSpecialProperties((name,), (), (name,))
        self.propertyStore2.setSpecialProperties((name,), (), (name,))
        self.propertyStore3.setSpecialProperties((name,), (), (name,))
        self.propertyStore4.setSpecialProperties((name,), (), (name,))

        value1 = propertyValue("Hello, World1!")
        value2 = propertyValue("Hello, World2!")
        value3 = propertyValue("Hello, World3!")
        value4 = propertyValue("Hello, World4!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        self.propertyStore3[name] = value3
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value3)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        self.propertyStore4[name] = value4
        yield self._changed(self.propertyStore4)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value3)
        self.assertEquals(self.propertyStore4.get(name, None), value4)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        self.propertyStore2[name] = value2
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value3)
        self.assertEquals(self.propertyStore4.get(name, None), value4)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore3[name]
        yield self._changed(self.propertyStore3)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value4)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore4[name]
        yield self._changed(self.propertyStore4)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value2)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value2)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore2[name]
        yield self._changed(self.propertyStore2)
        self.assertEquals(self.propertyStore1.get(name, None), value1)
        self.assertEquals(self.propertyStore2.get(name, None), value1)
        self.assertEquals(self.propertyStore3.get(name, None), value1)
        self.assertEquals(self.propertyStore4.get(name, None), value1)
        self.failUnless(name in self.propertyStore1)
        self.failUnless(name in self.propertyStore2)
        self.failUnless(name in self.propertyStore3)
        self.failUnless(name in self.propertyStore4)

        del self.propertyStore1[name]
        yield self._changed(self.propertyStore1)
        self.assertEquals(self.propertyStore1.get(name, None), None)
        self.assertEquals(self.propertyStore2.get(name, None), None)
        self.assertEquals(self.propertyStore3.get(name, None), None)
        self.assertEquals(self.propertyStore4.get(name, None), None)
        self.failIf(name in self.propertyStore1)
        self.failIf(name in self.propertyStore2)
        self.failIf(name in self.propertyStore3)
        self.failIf(name in self.propertyStore4)

    def test_iteration(self):

        value = propertyValue("Hello, World!")

        names = set(propertyName(str(i)) for i in (1, 2, 3, 4))

        for name in names:
            self.propertyStore[name] = value

        self.assertEquals(set(self.propertyStore.keys()), names)
        self.assertEquals(len(self.propertyStore), len(names))

    @inlineCallbacks
    def test_flush(self):

        name = propertyName("test")
        value = propertyValue("Hello, World!")

        #
        # Set value flushes correctly
        #
        self.propertyStore[name] = value

        yield self._changed(self.propertyStore)
        yield self._abort(self.propertyStore)

        self.assertEquals(self.propertyStore.get(name, None), value)
        self.assertEquals(len(self.propertyStore), 1)

        #
        # Deleted value flushes correctly
        #
        del self.propertyStore[name]

        yield self._changed(self.propertyStore)
        yield self._abort(self.propertyStore)

        self.assertEquals(self.propertyStore.get(name, None), None)
        self.assertEquals(len(self.propertyStore), 0)

    @inlineCallbacks
    def test_abort(self):
        name = propertyName("test")
        value = propertyValue("Hello, World!")

        self.propertyStore[name] = value

        yield self._abort(self.propertyStore)

        self.assertEquals(self.propertyStore.get(name, None), None)
        self.assertEquals(len(self.propertyStore), 0)
        self.flushLoggedErrors()

    @inlineCallbacks
    def test_peruser_keys(self):

        name = propertyName("shadow")

        self.propertyStore1.setSpecialProperties((name,), (), ())
        self.propertyStore2.setSpecialProperties((name,), (), ())

        value1 = propertyValue("Hello, World1!")

        self.propertyStore1[name] = value1
        yield self._changed(self.propertyStore1)

        self.failUnless(name in self.propertyStore2.keys())


def propertyName(name):
    return PropertyName("http://calendarserver.org/ns/test/", name)


def propertyValue(value):
    return davxml.ResponseDescription(value)
