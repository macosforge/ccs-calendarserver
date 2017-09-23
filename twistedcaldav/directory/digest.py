# -*- test-case-name: twistedcaldav.directory.test.test_digest -*-
##
# Copyright (c) 2006-2017 Apple Inc. All rights reserved.
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

from twisted.cred import error
from twisted.internet.defer import inlineCallbacks, returnValue
from txweb2.auth.digest import DigestCredentialFactory
from txweb2.auth.digest import DigestedCredentials
from txweb2.http_headers import Token
from txweb2.http_headers import parseKeyValue
from txweb2.http_headers import split
from txweb2.http_headers import tokenize

from twext.python.log import Logger

from twistedcaldav.memcacher import Memcacher

from zope.interface import implements, Interface

import time

log = Logger()

"""
Overrides txweb2.auth.digest to allow specifying a qop value as a configuration parameter.
Also adds an sqlite-based credentials cache that is multi-process safe.

"""


class IDigestCredentialsDatabase(Interface):
    """
    An interface to a digest credentials database that is used to hold per-client digest credentials so that fast
    re-authentication can be done with replay attacks etc prevented.
    """

    def has_key(self, key):
        """
        See whether the matching key exists.

        @param key:    the key to check.
        @type key:     C{str}.

        @return:       C{True} if the key exists, C{False} otherwise.
        """
        pass

    def set(self, key, value):
        """
        Store per-client credential information the first time a nonce is generated and used.

        @param key:        the key for the data to store.
        @type key:         C{str}
        @param value:      the data to store.
        @type value:       any.
        """
        pass

    def get(self, key):
        """
        Validate client supplied credentials by comparing with the cached values. If valid, store the new
        cnonce value in the database so that it can be used on the next validate.

        @param key:    the key to check.
        @type key:     C{str}.

        @return:       the value for the corresponding key, or C{None} if the key is not found.
        """
        pass

    def delete(self, key):
        """
        Remove the record associated with the supplied key.

        @param key:        the key to remove.
        @type key:         C{str}
        """
        pass


class DigestCredentialsMemcache(Memcacher):

    implements(IDigestCredentialsDatabase)

    CHALLENGE_MAXTIME_SECS = 8 * 60 * 60    # 8 hrs

    def __init__(self, namespace):
        super(DigestCredentialsMemcache, self).__init__(
            namespace=namespace,
            pickle=True,
        )

    def has_key(self, key):
        """
        See IDigestCredentialsDatabase.
        """
        d = self.get(key)
        d.addCallback(lambda value: value is not None)
        return d

    def set(self, key, value):
        """
        See IDigestCredentialsDatabase.
        """
        super(DigestCredentialsMemcache, self).set(
            key,
            value,
            expireTime=self.CHALLENGE_MAXTIME_SECS
        )


class QopDigestCredentialFactory(DigestCredentialFactory):
    """
    See txweb2.auth.digest.DigestCredentialFactory
    """

    def __init__(self, algorithm, qop, realm, namespace="DIGESTCREDENTIALS"):
        """
        @type algorithm: C{str}
        @param algorithm: case insensitive string that specifies
            the hash algorithm used, should be either, md5, md5-sess
            or sha

        @type qop: C{str}
        @param qop: case insensitive string that specifies
            the qop to use


        @type realm: C{str}
        @param realm: case sensitive string that specifies the realm
            portion of the challenge
        """
        super(QopDigestCredentialFactory, self).__init__(algorithm, realm)
        self.qop = qop
        self.db = DigestCredentialsMemcache(namespace)

    @inlineCallbacks
    def getChallenge(self, peer):
        """
        Generate the challenge for use in the WWW-Authenticate header Do the
        default behavior but then strip out any 'qop' from the challenge fields
        if no qop was specified.

        @param peer: The L{IAddress} of the requesting client.

        @return: The C{dict} that can be used to generate a WWW-Authenticate
            header.
        """

        challenge = yield (super(QopDigestCredentialFactory, self).getChallenge(peer))
        c = challenge['nonce']

        # Make sure it is not a duplicate
        result = (yield c in self.db)  # @IgnorePep8
        if result:
            raise AssertionError("nonce value already cached in credentials database: %s" % (c,))

        # The database record is a tuple of (nonce-count, timestamp)
        yield self.db.set(c, (0, time.time()))

        if self.qop:
            challenge['qop'] = self.qop
        else:
            del challenge['qop']

        # If stale was marked when decoding this request's Authorization header, add that to the challenge
        if hasattr(peer, 'stale') and peer.stale:
            challenge['stale'] = 'true'

        returnValue(challenge)

    @inlineCallbacks
    def decode(self, response, request):
        """
        Do the default behavior but then strip out any 'qop' from the credential fields
        if no qop was specified.
        """

        """
        Decode the given response and attempt to generate a
        L{DigestedCredentials} from it.

        @type response: C{str}
        @param response: A string of comma seperated key=value pairs

        @type request: L{txweb2.server.Request}
        @param request: the request being processed

        @return: L{DigestedCredentials}

        @raise: L{error.LoginFailed} if the response does not contain a
            username, a nonce, an opaque, or if the opaque is invalid.
        """

        response = ' '.join(response.splitlines())

        try:
            parts = split(tokenize((response,), foldCase=False), Token(","))

            auth = {}

            for (k, v) in [parseKeyValue(p) for p in parts]:
                auth[k.strip()] = v.strip()
        except ValueError:
            raise error.LoginFailed('Invalid response.')

        username = auth.get('username')
        if not username:
            raise error.LoginFailed('Invalid response, no username given.')

        if 'nonce' not in auth:
            raise error.LoginFailed('Invalid response, no nonce given.')

        # Now verify the nonce/cnonce values for this client
        result = (yield self._validate(auth, request))
        if result:
            if hasattr(request, "originalMethod"):
                originalMethod = request.originalMethod
            else:
                originalMethod = None

            credentials = DigestedCredentials(username,
                                              originalMethod or request.method,
                                              self._real.authenticationRealm,
                                              auth)

            if not self.qop and 'qop' in credentials.fields:
                del credentials.fields['qop']

            returnValue(credentials)
        else:
            raise error.LoginFailed('Invalid nonce/cnonce values')

    @inlineCallbacks
    def _validate(self, auth, request):
        """
        Check that the parameters in the response represent a valid set of credentials that
        may be being re-used.

        @param auth:        the response parameters.
        @type auth:         C{dict}
        @param request:     the request being processed.
        @type request:      L{txweb2.server.Request}

        @return:            C{True} if validated.
        @raise LoginFailed: if validation fails.
        """

        nonce = auth.get('nonce')
        nonce_count = auth.get('nc')

        # First check we have this nonce
        result = (yield self.db.get(nonce))
        if result is None:
            raise error.LoginFailed('Invalid nonce value: %s' % (nonce,))
        db_nonce_count, db_timestamp = result

        # cnonce and nonce-count MUST be present if qop is present
        if auth.get('qop') is not None:
            if auth.get('cnonce') is None:
                yield self._invalidate(nonce)
                raise error.LoginFailed('cnonce is required when qop is specified')
            if nonce_count is None:
                yield self._invalidate(nonce)
                raise error.LoginFailed('nonce-count is required when qop is specified')

            # Next check the nonce-count is one greater than the previous one and update it in the DB
            try:
                nonce_count = int(nonce_count, 16)
            except ValueError:
                yield self._invalidate(nonce)
                raise error.LoginFailed('nonce-count is not a valid hex string: %s' % (auth.get('nonce-count'),))
            if nonce_count != db_nonce_count + 1:
                yield self._invalidate(nonce)
                raise error.LoginFailed('nonce-count value out of sequence: %s should be one more than %s' % (nonce_count, db_nonce_count,))
            yield self.db.set(nonce, (nonce_count, db_timestamp))
        else:
            # When not using qop the stored nonce-count must always be zero.
            # i.e. we can't allow a qop auth then a non-qop auth with the same nonce
            if db_nonce_count != 0:
                yield self._invalidate(nonce)
                raise error.LoginFailed('nonce-count was sent with this nonce: %s' % (nonce,))

        # Now check timestamp
        if db_timestamp + DigestCredentialFactory.CHALLENGE_LIFETIME_SECS <= time.time():
            yield self._invalidate(nonce)
            if request.remoteAddr:
                request.remoteAddr.stale = True
            raise error.LoginFailed('Digest credentials expired')

        returnValue(True)

    def _invalidate(self, nonce):
        """
        Invalidate cached credentials for the specified nonce value.

        @param nonce:    the nonce for the record to _invalidate.
        @type nonce:     C{str}
        """
        return self.db.delete(nonce)
