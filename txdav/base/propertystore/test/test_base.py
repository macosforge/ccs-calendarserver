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

from zope.interface.verify import verifyObject, BrokenMethodImplementation

from twisted.trial import unittest

from txdav.idav import IPropertyName
from txdav.base.propertystore.base import PropertyName


class PropertyNameTest(unittest.TestCase):

    def test_interface(self):
        name = PropertyName("http://calendarserver.org/", "bleargh")
        try:
            verifyObject(IPropertyName, name)
        except BrokenMethodImplementation as e:
            self.fail(e)

    def test_init(self):
        name = PropertyName("http://calendarserver.org/", "bleargh")

        self.assertEquals(name.namespace, "http://calendarserver.org/")
        self.assertEquals(name.name, "bleargh")

    def test_fromString(self):
        name = PropertyName.fromString("{http://calendarserver.org/}bleargh")

        self.assertEquals(name.namespace, "http://calendarserver.org/")
        self.assertEquals(name.name, "bleargh")

    def test_toString(self):
        name = PropertyName("http://calendarserver.org/", "bleargh")

        self.assertEquals(
            name.toString(),
            "{http://calendarserver.org/}bleargh"
        )
