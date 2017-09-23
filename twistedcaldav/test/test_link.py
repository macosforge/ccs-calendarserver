##
# Copyright (c) 2008-2017 Apple Inc. All rights reserved.
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

from txweb2 import responsecode
from txweb2.http import HTTPError
from txweb2.test.test_server import SimpleRequest

from twisted.internet.defer import inlineCallbacks, succeed

from twistedcaldav.linkresource import LinkResource
from twistedcaldav.resource import CalendarHomeResource
from twistedcaldav.test.util import TestCase


class StubProperty(object):

    def qname(self):
        return "StubQnamespace", "StubQname"


class StubHome(object):

    def properties(self):
        return []

    def calendarWithName(self, name):
        return succeed(None)

    def addNotifier(self, factory_name, notifier):
        pass


class StubCalendarHomeResource(CalendarHomeResource):

    def principalForRecord(self):
        return None


class StubShare(object):

    def __init__(self, link):
        self.hosturl = link

    def url(self):
        return self.hosturl


class LinkResourceTests(TestCase):

    @inlineCallbacks
    def test_okLink(self):
        resource = CalendarHomeResource(self.site.resource, "home", object(), StubHome())
        self.site.resource.putChild("home", resource)
        link = LinkResource(resource, "/home/outbox/")
        resource.putChild("link", link)

        request = SimpleRequest(self.site, "GET", "/home/link/")
        linked_to, _ignore = (yield resource.locateChild(request, ["link", ]))
        self.assertTrue(linked_to is resource.getChild("outbox"))

    @inlineCallbacks
    def test_badLink(self):
        resource = CalendarHomeResource(self.site.resource, "home", object(), StubHome())
        self.site.resource.putChild("home", resource)
        link = LinkResource(resource, "/home/outbox/abc")
        resource.putChild("link", link)

        request = SimpleRequest(self.site, "GET", "/home/link/")
        try:
            yield resource.locateChild(request, ["link", ])
        except HTTPError as e:
            self.assertEqual(e.response.code, responsecode.NOT_FOUND)
        else:
            self.fail("HTTPError exception not raised")

    @inlineCallbacks
    def test_recursiveLink(self):
        resource = CalendarHomeResource(self.site.resource, "home", object(), StubHome())
        self.site.resource.putChild("home", resource)
        link1 = LinkResource(resource, "/home/link2/")
        resource.putChild("link1", link1)
        link2 = LinkResource(resource, "/home/link1/")
        resource.putChild("link2", link2)

        request = SimpleRequest(self.site, "GET", "/home/link1/")
        try:
            yield resource.locateChild(request, ["link1", ])
        except HTTPError as e:
            self.assertEqual(e.response.code, responsecode.LOOP_DETECTED)
        else:
            self.fail("HTTPError exception not raised")
