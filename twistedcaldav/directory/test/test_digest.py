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

import sys
from hashlib import md5

from twisted.cred import error
from twisted.internet import address
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import failure
from txweb2.auth import digest
from txweb2.auth.wrapper import UnauthorizedResponse
from txweb2.test.test_server import SimpleRequest

from twistedcaldav.directory.digest import QopDigestCredentialFactory
from twistedcaldav.test.util import TestCase
from twistedcaldav.config import config
from txweb2.auth.digest import DigestCredentialFactory
from txweb2.test.test_httpauth import makeDigestDeterministic
from txweb2.test.test_httpauth import FAKE_STATIC_NONCE


class FakeDigestCredentialFactory(QopDigestCredentialFactory):
    """
    A Fake Digest Credential Factory that generates a predictable
    nonce and opaque
    """

    def __init__(self, *args, **kwargs):
        super(FakeDigestCredentialFactory, self).__init__(*args, **kwargs)
        makeDigestDeterministic(self._real)


clientAddress = address.IPv4Address('TCP', '127.0.0.1', 80)

challengeOpaque = ('75c4bd95b96b7b7341c646c6502f0833-MTc4Mjg4NzU'
                   '4NzE2MTIyMzkyODgxMjU0NzcwNjg1LHJlbW90ZWhvc3Q'
                   'sMA==')

challengeNonce = '178288758716122392881254770685'

challengeResponse = (
    'digest',
    {
        'nonce': challengeNonce,
        'qop': 'auth',
        'realm': 'test realm',
        'algorithm': 'md5',
    }
)

cnonce = "29fc54aa1641c6fa0e151419361c8f23"

authRequest1 = (
    (
        'username="username", realm="test realm", nonce="%s", '
        'uri="/write/", response="%s", algorithm="md5", '
        'cnonce="29fc54aa1641c6fa0e151419361c8f23", nc=00000001, '
        'qop="auth"'
    ),
    (
        'username="username", realm="test realm", nonce="%s", '
        'uri="/write/", response="%s", algorithm="md5"'
    )
)

authRequest2 = (
    (
        'username="username", realm="test realm", nonce="%s", '
        'uri="/write/", response="%s", algorithm="md5", '
        'cnonce="29fc54aa1641c6fa0e151419361c8f23", nc=00000002, '
        'qop="auth"'
    ),
    (
        'username="username", realm="test realm", nonce="%s", '
        'uri="/write/", response="%s", algorithm="md5"'
    )
)

authRequest3 = (
    'username="username", realm="test realm", nonce="%s", '
    'uri="/write/", response="%s", algorithm="md5"'
)

authRequestComma = (
    (
        'username="user,name", realm="test realm", nonce="%s", '
        'uri="/write/1,2.txt", response="%s", algorithm="md5", '
        'cnonce="29fc54aa1641c6fa0e151419361c8f23", nc=00000001, '
        'qop="auth"'
    ),
    (
        'username="user,name", realm="test realm", nonce="%s", '
        'uri="/write/1,2.txt", response="%s", algorithm="md5"'
    )
)

namelessAuthRequest = 'realm="test realm",nonce="doesn\'t matter"'

emtpyAttributeAuthRequest = 'realm="",nonce="doesn\'t matter"'


class DigestAuthTestCase(TestCase):
    """
    Test the behavior of DigestCredentialFactory
    """

    def setUp(self):
        """
        Create a DigestCredentialFactory for testing
        """
        TestCase.setUp(self)
        config.ProcessType = "Single"

        self.namespace1 = "DIGEST1"
        self.namespace2 = "DIGEST2"

        self.credentialFactories = (
            QopDigestCredentialFactory(
                'md5',
                'auth',
                'test realm',
                self.namespace1
            ),
            QopDigestCredentialFactory(
                'md5',
                '',
                'test realm',
                self.namespace2
            )
        )

    def getDigestResponse(self, challenge, ncount):
        """
        Calculate the response for the given challenge
        """
        nonce = challenge.get('nonce')
        algo = challenge.get('algorithm').lower()
        qop = challenge.get('qop')

        if qop:
            expected = digest.calcResponse(
                digest.calcHA1(
                    algo,
                    "username",
                    "test realm",
                    "password",
                    nonce,
                    cnonce
                ),
                algo, nonce, ncount, cnonce, qop, "GET", "/write/", None
            )
        else:
            expected = digest.calcResponse(
                digest.calcHA1(
                    algo,
                    "username",
                    "test realm",
                    "password",
                    nonce,
                    cnonce
                ),
                algo, nonce, None, None, None, "GET", "/write/", None
            )
        return expected

    def getDigestResponseComma(self, challenge, ncount):
        """
        Calculate the response for the given challenge
        """
        nonce = challenge.get('nonce')
        algo = challenge.get('algorithm').lower()
        qop = challenge.get('qop')

        if qop:
            expected = digest.calcResponse(
                digest.calcHA1(
                    algo,
                    "user,name",
                    "test realm",
                    "password",
                    nonce,
                    cnonce
                ),
                algo, nonce, ncount, cnonce, qop, "GET", "/write/1,2.txt", None
            )
        else:
            expected = digest.calcResponse(
                digest.calcHA1(
                    algo,
                    "user,name",
                    "test realm",
                    "password",
                    nonce,
                    cnonce
                ),
                algo, nonce, None, None, None, "GET", "/write/1,2.txt", None
            )
        return expected

    @inlineCallbacks
    def assertRaisesDeferred(self, exception, f, *args, **kwargs):
        try:
            result = (yield f(*args, **kwargs))
        except exception as inst:
            returnValue(inst)
        except:
            raise self.failureException('%s raised instead of %s:\n %s'
                                        % (sys.exc_info()[0],
                                           exception.__name__,
                                           failure.Failure().getTraceback()))
        else:
            raise self.failureException('%s not raised (%r returned)'
                                        % (exception.__name__, result))

    @inlineCallbacks
    def test_getChallenge(self):
        """
        Test that all the required fields exist in the challenge,
        and that the information matches what we put into our
        DigestCredentialFactory
        """

        challenge = (yield self.credentialFactories[0].getChallenge(clientAddress))
        self.assertEquals(challenge['qop'], 'auth')
        self.assertEquals(challenge['realm'], 'test realm')
        self.assertEquals(challenge['algorithm'], 'md5')
        self.assertTrue("nonce" in challenge)

        challenge = (yield self.credentialFactories[1].getChallenge(clientAddress))
        self.assertFalse('qop' in challenge)
        self.assertEquals(challenge['realm'], 'test realm')
        self.assertEquals(challenge['algorithm'], 'md5')
        self.assertTrue("nonce" in challenge)

    @inlineCallbacks
    def test_response(self):
        """
        Test that we can decode a valid response to our challenge
        """

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))
            self.failUnless(creds.checkPassword('password'))

    @inlineCallbacks
    def test_multiResponse(self):
        """
        Test that multiple responses to to a single challenge are handled
        successfully.
        """

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))
            self.failUnless(creds.checkPassword('password'))

            clientResponse = authRequest2[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000002"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))
            self.failUnless(creds.checkPassword('password'))

    @inlineCallbacks
    def test_failsWithDifferentMethod(self):
        """
        Test that the response fails if made for a different request method
        than it is being issued for.
        """

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            creds = (yield factory.decode(
                clientResponse,
                SimpleRequest(None, 'POST', '/')
            ))
            self.failIf(creds.checkPassword('password'))

    @inlineCallbacks
    def test_noUsername(self):
        """
        Test that login fails when our response does not contain a username,
        or the username field is empty.
        """

        # Check for no username
        for factory in self.credentialFactories:
            e = (yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                namelessAuthRequest,
                _trivial_GET()
            ))
            self.assertEquals(str(e), "Invalid response, no username given.")

            # Check for an empty username
            e = (yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                namelessAuthRequest + ',username=""',
                _trivial_GET()
            ))
            self.assertEquals(str(e), "Invalid response, no username given.")

    @inlineCallbacks
    def test_noNonce(self):
        """
        Test that login fails when our response does not contain a nonce
        """

        for factory in self.credentialFactories:
            e = (yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                'realm="Test",username="Foo",opaque="bar"',
                _trivial_GET()
            ))
            self.assertEquals(str(e), "Invalid response, no nonce given.")

    @inlineCallbacks
    def test_emptyAttribute(self):
        """
        Test that login fails when our response contains an attribute
        with no value,
        """

        # Check for no username
        for factory in self.credentialFactories:
            e = (yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                emtpyAttributeAuthRequest,
                _trivial_GET()
            ))
            self.assertEquals(str(e), "Invalid response, no username given.")

    @inlineCallbacks
    def test_checkHash(self):
        """
        Check that given a hash of the form 'username:realm:password'
        we can verify the digest challenge
        """

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))

            self.failUnless(creds.checkHash(
                md5('username:test realm:password').hexdigest()))

            self.failIf(creds.checkHash(
                md5('username:test realm:bogus').hexdigest()))

    @inlineCallbacks
    def test_invalidNonceCount(self):
        """
        Test that login fails when the nonce-count is repeated.
        """

        credentialFactories = (
            FakeDigestCredentialFactory('md5', 'auth', 'test realm', self.namespace1),
            FakeDigestCredentialFactory('md5', '', 'test realm', self.namespace2)
        )

        for ctr, factory in enumerate(credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse1 = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            clientResponse2 = authRequest2[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000002"),
            )

            yield factory.decode(clientResponse1, _trivial_GET())
            yield factory.decode(clientResponse2, _trivial_GET())

            if challenge.get('qop') is not None:
                yield self.assertRaisesDeferred(
                    error.LoginFailed,
                    factory.decode,
                    clientResponse2,
                    _trivial_GET()
                )

                challenge = (yield factory.getChallenge(clientAddress))

                clientResponse1 = authRequest1[ctr] % (
                    challenge['nonce'],
                    self.getDigestResponse(challenge, "00000001"),
                )
                del challenge['qop']
                clientResponse3 = authRequest3 % (
                    challenge['nonce'],
                    self.getDigestResponse(challenge, "00000002"),
                )
                yield factory.decode(clientResponse1, _trivial_GET())
                yield self.assertRaisesDeferred(
                    error.LoginFailed,
                    factory.decode,
                    clientResponse3,
                    _trivial_GET()
                )

    @inlineCallbacks
    def test_invalidNonce(self):
        """
        Test that login fails when the given nonce from the response, does not
        match the nonce encoded in the opaque.
        """

        credentialFactories = (
            FakeDigestCredentialFactory('md5', 'auth', 'test realm', self.namespace1),
            FakeDigestCredentialFactory('md5', '', 'test realm', self.namespace2)
        )

        for ctr, factory in enumerate(credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))
            challenge['nonce'] = "noNoncense"

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            request = _trivial_GET()
            yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                clientResponse,
                request
            )

            factory._invalidate(FAKE_STATIC_NONCE)
            response = (yield UnauthorizedResponse.makeResponse(
                {"Digest": factory},
                request.remoteAddr
            ))
            response.headers.getHeader("www-authenticate")[0][1]

    @inlineCallbacks
    def test_oldNonce(self):
        """
        Test that the login fails when the given opaque is older than
        DigestCredentialFactory.CHALLENGE_LIFETIME_SECS
        """

        credentialFactories = (
            FakeDigestCredentialFactory('md5', 'auth', 'test realm', self.namespace1),
            FakeDigestCredentialFactory('md5', '', 'test realm', self.namespace2)
        )

        for ctr, factory in enumerate(credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))
            nonce_count, timestamp = (yield factory.db.get(challenge['nonce']))
            factory.db.set(challenge['nonce'], (nonce_count, timestamp - 2 * digest.DigestCredentialFactory.CHALLENGE_LIFETIME_SECS))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            request = _trivial_GET()
            yield self.assertRaisesDeferred(
                error.LoginFailed,
                factory.decode,
                clientResponse,
                request
            )

            response = (yield UnauthorizedResponse.makeResponse(
                {"Digest": factory},
                request.remoteAddr,
            ))
            wwwhdrs = response.headers.getHeader("www-authenticate")[0][1]
            self.assertTrue('stale' in wwwhdrs, msg="No stale parameter in Digest WWW-Authenticate headers: %s" % (wwwhdrs,))
            self.assertEquals(wwwhdrs['stale'], 'true', msg="stale parameter not set to true in Digest WWW-Authenticate headers: %s" % (wwwhdrs,))

    def test_incompatibleCalcHA1Options(self):
        """
        Test that the appropriate error is raised when any of the
        pszUsername, pszRealm, or pszPassword arguments are specified with
        the preHA1 keyword argument.
        """

        arguments = (
            ("user", "realm", "password", "preHA1"),
            (None, "realm", None, "preHA1"),
            (None, None, "password", "preHA1"),
        )

        for pszUsername, pszRealm, pszPassword, preHA1 in arguments:
            self.assertRaises(
                TypeError,
                digest.calcHA1,
                "md5",
                pszUsername,
                pszRealm,
                pszPassword,
                "nonce",
                "cnonce",
                preHA1=preHA1
            )

    @inlineCallbacks
    def test_commaURI(self):
        """
        Check that commas in valued are parsed out properly.
        """

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequestComma[ctr] % (
                challenge['nonce'],
                self.getDigestResponseComma(challenge, "00000001"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))
            self.failUnless(creds.checkPassword('password'))

    @inlineCallbacks
    def test_stale_response(self):
        """
        Test that we can decode a valid response to our challenge
        """
        theTime = 0

        class newtime(object):

            def time(self):
                return theTime
        from twistedcaldav.directory import digest as ddigest
        self.patch(ddigest, "time", newtime())

        for ctr, factory in enumerate(self.credentialFactories):
            challenge = (yield factory.getChallenge(clientAddress))

            clientResponse = authRequest1[ctr] % (
                challenge['nonce'],
                self.getDigestResponse(challenge, "00000001"),
            )

            creds = (yield factory.decode(clientResponse, _trivial_GET()))
            self.failUnless(creds.checkPassword('password'))

            theTime += DigestCredentialFactory.CHALLENGE_LIFETIME_SECS + 1
            request = _trivial_GET()
            try:
                clientResponse = authRequest2[ctr] % (
                    challenge['nonce'],
                    self.getDigestResponse(challenge, "00000002"),
                )
                creds = (yield factory.decode(clientResponse, request))
                self.fail("Nonce should have timed out")
            except error.LoginFailed:
                self.assertTrue(hasattr(request.remoteAddr, "stale"))
            except Exception as e:
                self.fail("Invalid exception from nonce timeout: %s" % e)
            challenge = (yield factory.getChallenge(request.remoteAddr))
            self.assertTrue(challenge.get("stale") == "true")


def _trivial_GET():
    return SimpleRequest(None, 'GET', '/')
