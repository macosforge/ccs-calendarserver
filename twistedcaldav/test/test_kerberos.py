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

from twisted.cred.error import LoginFailed
from twisted.cred.error import UnauthorizedLogin
from twisted.internet.defer import inlineCallbacks
from txweb2.test.test_server import SimpleRequest

from twistedcaldav import authkerb
import twistedcaldav.test.util

"""
We can't test kerberos for real without actually having a working Kerberos infrastructure
which we are not guaranteed to have for the test.
"""


class KerberosTests(twistedcaldav.test.util.TestCase):

    def test_BasicKerberosCredentials(self):
        authkerb.BasicKerberosCredentials("test", "test", "HTTP/example.com@EXAMPLE.COM", "EXAMPLE.COM")

    @inlineCallbacks
    def test_BasicKerberosCredentialFactory(self):
        factory = authkerb.BasicKerberosCredentialFactory(principal="HTTP/server.example.com@EXAMPLE.COM")

        challenge = (yield factory.getChallenge("peer"))
        expected_challenge = {'realm': "EXAMPLE.COM"}
        self.assertTrue(challenge == expected_challenge,
                        msg="BasicKerberosCredentialFactory challenge %s != %s" % (challenge, expected_challenge))

    def test_BasicKerberosCredentialFactoryInvalidPrincipal(self):
        self.assertRaises(
            ValueError,
            authkerb.BasicKerberosCredentialFactory,
            principal="HTTP/server.example.com/EXAMPLE.COM"
        )

    def test_NegotiateCredentials(self):
        authkerb.NegotiateCredentials("test@EXAMPLE.COM", "test")

    @inlineCallbacks
    def test_NegotiateCredentialFactory(self):
        factory = authkerb.NegotiateCredentialFactory(principal="HTTP/server.example.com@EXAMPLE.COM")

        challenge = (yield factory.getChallenge("peer"))
        expected_challenge = {}
        self.assertTrue(challenge == expected_challenge,
                        msg="NegotiateCredentialFactory challenge %s != %s" % (challenge, expected_challenge))

        request = SimpleRequest(self.site, "GET", "/")
        try:
            yield factory.decode("Bogus Data".encode("base64"), request)
        except (UnauthorizedLogin, LoginFailed):
            pass
        except Exception as ex:
            self.fail(msg="NegotiateCredentialFactory decode failed with exception: %s" % (ex,))
        else:
            self.fail(msg="NegotiateCredentialFactory decode did not fail")

    def test_NegotiateCredentialFactoryDifferentRealm(self):
        factory = authkerb.NegotiateCredentialFactory(principal="HTTP/server.example.com@EXAMPLE.COM")
        self.assertEquals(factory.realm, "EXAMPLE.COM")
        self.assertEquals(factory.service, "HTTP@SERVER.EXAMPLE.COM")

    def test_NegotiateCredentialFactoryInvalidPrincipal(self):
        self.assertRaises(
            ValueError,
            authkerb.NegotiateCredentialFactory,
            principal="HTTP/server.example.com/EXAMPLE.COM"
        )
