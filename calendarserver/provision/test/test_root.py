##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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


from twisted.internet.defer import inlineCallbacks, maybeDeferred, returnValue

from txweb2 import http_headers
from txweb2 import responsecode
from txdav.xml import element as davxml
from txweb2.http import HTTPError
from txweb2.iweb import IResponse

from twistedcaldav.test.util import StoreTestCase, SimpleStoreRequest
from twistedcaldav.directory.principal import DirectoryPrincipalProvisioningResource

import calendarserver.provision.root  # for patching checkSACL


TEST_SACLS = {
    "calendar": [
        "dreid"
    ],
    "addressbook": [
        "dreid"
    ],
}


def stubCheckSACL(username, service):
    if service not in TEST_SACLS:
        return True

    if username in TEST_SACLS[service]:
        return True

    return False


class RootTests(StoreTestCase):

    @inlineCallbacks
    def setUp(self):
        yield super(RootTests, self).setUp()

        self.patch(calendarserver.provision.root, "checkSACL", stubCheckSACL)


class ComplianceTests(RootTests):
    """
    Tests to verify CalDAV compliance of the root resource.
    """

    @inlineCallbacks
    def issueRequest(self, segments, method="GET"):
        """
        Get a resource from a particular path from the root URI, and return a
        Deferred which will fire with (something adaptable to) an HTTP response
        object.
        """
        request = SimpleStoreRequest(self, method, ("/".join([""] + segments)))
        rsrc = self.actualRoot
        while segments:
            rsrc, segments = (yield maybeDeferred(
                rsrc.locateChild, request, segments
            ))

        result = yield rsrc.renderHTTP(request)
        returnValue(result)

    @inlineCallbacks
    def test_optionsIncludeCalendar(self):
        """
        OPTIONS request should include a DAV header that mentions the
        addressbook capability.
        """
        response = yield self.issueRequest([""], "OPTIONS")
        self.assertIn("addressbook", response.headers.getHeader("DAV"))


class SACLTests(RootTests):

    @inlineCallbacks
    def test_noSacls(self):
        """
        Test the behaviour of locateChild when SACLs are not enabled.

        should return a valid resource
        """
        self.actualRoot.useSacls = False

        request = SimpleStoreRequest(self, "GET", "/principals/")

        resrc, segments = (yield maybeDeferred(
            self.actualRoot.locateChild, request, ["principals"]
        ))

        self.failUnless(
            isinstance(resrc, DirectoryPrincipalProvisioningResource),
            "Did not get a DirectoryPrincipalProvisioningResource: %s"
            % (resrc,)
        )

        self.assertEquals(segments, [])

    @inlineCallbacks
    def test_inSacls(self):
        """
        Test the behavior of locateChild when SACLs are enabled and the
        user is in the SACL group

        should return a valid resource
        """
        self.actualRoot.useSacls = True

        principal = yield self.actualRoot.findPrincipalForAuthID("dreid")
        request = SimpleStoreRequest(
            self,
            "GET",
            "/principals/",
            authPrincipal=principal
        )

        resrc, segments = (yield maybeDeferred(
            self.actualRoot.locateChild, request, ["principals"]
        ))

        self.failUnless(
            isinstance(resrc, DirectoryPrincipalProvisioningResource),
            "Did not get a DirectoryPrincipalProvisioningResource: %s"
            % (resrc,)
        )

        self.assertEquals(segments, [])

        self.assertEquals(
            request.authzUser.principalElement(),
            davxml.Principal(
                davxml.HRef(
                    "/principals/__uids__/5FF60DAD-0BDE-4508-8C77-15F0CA5C8DD1/"
                )
            )
        )

    @inlineCallbacks
    def test_notInSacls(self):
        """
        Test the behavior of locateChild when SACLs are enabled and the
        user is not in the SACL group

        should return a 403 forbidden response
        """
        self.actualRoot.useSacls = True

        principal = yield self.actualRoot.findPrincipalForAuthID("wsanchez")

        request = SimpleStoreRequest(
            self,
            "GET",
            "/principals/",
            authPrincipal=principal
        )

        try:
            _ignore_resrc, _ignore_segments = (yield maybeDeferred(
                self.actualRoot.locateChild, request, ["principals"]
            ))
            raise AssertionError(
                "RootResource.locateChild did not return an error"
            )
        except HTTPError as e:
            self.assertEquals(e.response.code, 403)

    @inlineCallbacks
    def test_unauthenticated(self):
        """
        Test the behavior of locateChild when SACLs are enabled and the request
        is unauthenticated

        should return a 401 UnauthorizedResponse
        """

        self.actualRoot.useSacls = True
        request = SimpleStoreRequest(
            self,
            "GET",
            "/principals/"
        )

        try:
            _ignore_resrc, _ignore_segments = (yield maybeDeferred(
                self.actualRoot.locateChild, request, ["principals"]
            ))
            raise AssertionError(
                "RootResource.locateChild did not return an error"
            )
        except HTTPError as e:
            self.assertEquals(e.response.code, 401)

    @inlineCallbacks
    def test_badCredentials(self):
        """
        Test the behavior of locateChild when SACLS are enabled, and
        incorrect credentials are given.

        should return a 401 UnauthorizedResponse
        """
        self.actualRoot.useSacls = True

        request = SimpleStoreRequest(
            self,
            "GET",
            "/principals/",
            headers=http_headers.Headers(
                {
                    "Authorization": [
                        "basic", "%s" % ("dreid:dreid".encode("base64"),)
                    ]
                }
            )
        )

        try:
            _ignore_resrc, _ignore_segments = (yield maybeDeferred(
                self.actualRoot.locateChild, request, ["principals"]
            ))
            raise AssertionError(
                "RootResource.locateChild did not return an error"
            )
        except HTTPError as e:
            self.assertEquals(e.response.code, 401)

    def test_DELETE(self):
        def do_test(response):
            response = IResponse(response)

            if response.code != responsecode.FORBIDDEN:
                self.fail("Incorrect response for DELETE /: %s"
                          % (response.code,))

        request = SimpleStoreRequest(self, "DELETE", "/")
        return self.send(request, do_test)

    def test_COPY(self):
        def do_test(response):
            response = IResponse(response)

            if response.code != responsecode.FORBIDDEN:
                self.fail("Incorrect response for COPY /: %s"
                          % (response.code,))

        request = SimpleStoreRequest(
            self,
            "COPY",
            "/",
            headers=http_headers.Headers({"Destination": "/copy/"})
        )
        return self.send(request, do_test)

    def test_MOVE(self):
        def do_test(response):
            response = IResponse(response)

            if response.code != responsecode.FORBIDDEN:
                self.fail("Incorrect response for MOVE /: %s"
                          % (response.code,))

        request = SimpleStoreRequest(
            self,
            "MOVE",
            "/",
            headers=http_headers.Headers({"Destination": "/copy/"})
        )
        return self.send(request, do_test)


class SACLCacheTests(RootTests):

    class StubResponseCacheResource(object):

        def __init__(self):
            self.cache = {}
            self.responseCache = self
            self.cacheHitCount = 0

        def getResponseForRequest(self, request):
            if str(request) in self.cache:
                self.cacheHitCount += 1
                return self.cache[str(request)]

        def cacheResponseForRequest(self, request, response):
            self.cache[str(request)] = response
            return response

    @inlineCallbacks
    def setUp(self):
        yield super(SACLCacheTests, self).setUp()
        self.actualRoot.responseCache = SACLCacheTests.StubResponseCacheResource()

    @inlineCallbacks
    def test_PROPFIND(self):
        self.actualRoot.useSacls = True

        body = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
<D:prop>
<D:getetag/>
<D:displayname/>
</D:prop>
</D:propfind>
"""
        principal = yield self.actualRoot.findPrincipalForAuthID("dreid")

        request = SimpleStoreRequest(
            self,
            "PROPFIND",
            "/principals/users/dreid/",
            headers=http_headers.Headers({
                'Depth': '1',
            }),
            authPrincipal=principal,
            content=body
        )
        response = yield self.send(request)
        response = IResponse(response)

        if response.code != responsecode.MULTI_STATUS:
            self.fail("Incorrect response for PROPFIND /principals/: %s" % (response.code,))

        request = SimpleStoreRequest(
            self,
            "PROPFIND",
            "/principals/users/dreid/",
            headers=http_headers.Headers({
                'Depth': '1',
            }),
            authPrincipal=principal,
            content=body
        )
        response = yield self.send(request)
        response = IResponse(response)

        if response.code != responsecode.MULTI_STATUS:
            self.fail("Incorrect response for PROPFIND /principals/: %s" % (response.code,))
        self.assertEqual(self.actualRoot.responseCache.cacheHitCount, 1)


class WikiTests(RootTests):

    @inlineCallbacks
    def test_oneTime(self):
        """
        Make sure wiki auth lookup is only done once per request;
        request.checkedWiki will be set to True
        """

        request = SimpleStoreRequest(self, "GET", "/principals/")

        _ignore_resrc, _ignore_segments = (yield maybeDeferred(
            self.actualRoot.locateChild, request, ["principals"]
        ))
        self.assertTrue(request.checkedWiki)
