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

from twext.python.log import Logger
from txweb2.dav.util import allDataFromStream
from txweb2.http import Response
from txweb2.iweb import IResource
from txweb2.stream import MemoryStream

from twisted.internet.defer import succeed, inlineCallbacks, returnValue

from twistedcaldav.config import config
from twistedcaldav.memcachepool import CachePoolUserMixIn, defaultCachePool

from txdav.idav import IStoreNotifierFactory, IStoreNotifier

from zope.interface import implements

import cPickle
import hashlib
import urllib
import uuid

"""
The basic principals of the PROPFIND cache are this:

(1) In RootResource.locateChild we "intercept" request processing at a very early stage (before traversing the resource
hierarchy for the request URI). If the request is a PROPFIND we check to see whether a cache entry exists and if so immediately
return that. If no cache entry exists, normal PROPFIND processing occurs.

(2) The PropfindCacheMixin class is mixed into calendar/address book homes. That causes all valid PROPFIND responses to be
cached, and also provides a cache invalidation api to allow signaling of changes that need to invalidate the cache. The main
and child resources need to cause that api to be called when appropriate changes occur.

(3) The response cache entries consist of a key, derived from the request only, and a value. The value contains the set of tokens
in effect at the time the entry was cached, together with the response that was cached. The tokens are:

  - principalToken - a token for the authenticated user's principal
  - directoryToken - a hash of that principal's directory record
  - uriToken - a token for the request uri
  - childTokens - tokens for any child resources the request uri depends on (for depth:1)

  The current principalToken, uriToken and childTokens values are themselves stored in the cache using the key prefix 'cacheToken:'.

(4) When a request is being checked in the cache, the response cache entry key is first computed and any value extracted. The
tokens in the value are then checked against the current set of tokens in the cache. If there is any mismatch between tokens, the
cache entry is considered invalid and the cached response is not returned. If everything matches up, the cached response is returned
to the caller and ultimately sent directly back to the client.

(5) Because of shared calendars/address books that can affect the calendar/address book homes of several different users at once, we
need to keep track of the separate childTokens for each child resource. The tokens for shared resources are keyed of the sharer's uri,
so sharee's homes use that token. That way a single token for all shared instances is used and changed just once.

(6) Principals and directory records need to be included as tokens to take account of variations in access control based on who
is making the request (including proxy state changes etc).

"""


class DisabledCacheNotifier(object):

    def __init__(self, *args, **kwargs):
        pass

    def changed(self, url=None):
        return succeed(None)


class DisabledCache(object):

    def getResponseForRequest(self, request):
        return succeed(None)

    def cacheResponseForRequest(self, request, response):
        return succeed(response)


class URINotFoundException(Exception):

    def __init__(self, uri):
        self.uri = uri

    def __repr__(self):
        return "%s: Could not find URI %r" % (
            self.__class__.__name__,
            self.uri)


class MemcacheChangeNotifier(CachePoolUserMixIn):
    """
    A change notifier used by resources (not store objects).
    """
    log = Logger()

    def __init__(self, resource, cachePool=None, cacheHandle="Default"):
        self._resource = resource
        self._cachePool = cachePool
        self._cachePoolHandle = cacheHandle

    def _newCacheToken(self):
        return str(uuid.uuid4())

    def changed(self, url=None):
        """
        Change the cache token for a resource

        @param url: the url whose token is being changed, or C{None} to determine
            the URL from the supplied resource
        @type: L{str}

        return: A L{Deferred} that fires when the token has been changed.
        """

        # For shared resources we use the owner URL as the cache key
        if url is None:
            url = self._resource.url()

        self.log.debug("Changing Cache Token for {url}", url=url)
        return self.getCachePool().set(
            'cacheToken:%s' % (url,),
            self._newCacheToken(), expireTime=config.ResponseCacheTimeout * 60)


class MemcacheURLPatternChangeNotifier(CachePoolUserMixIn):
    """
    A change notifier used to target arbitrary tokens.
    """
    log = Logger()

    def __init__(self, urlPattern, cachePool=None, cacheHandle="Default"):
        self._urlPattern = urlPattern
        self._cachePool = cachePool
        self._cachePoolHandle = cacheHandle

    def _newCacheToken(self):
        return str(uuid.uuid4())

    def changed(self, token):
        """
        Change the cache token for a resource

        return: A L{Deferred} that fires when the token has been changed.
        """

        url = self._urlPattern.format(token=token)

        self.log.debug("Changing Cache Token for {url}", url=url)
        return self.getCachePool().set(
            'cacheToken:{url}'.format(url=url),
            self._newCacheToken(),
            expireTime=config.ResponseCacheTimeout * 60,
        )


class BaseResponseCache(object):
    """
    A base class which provides some common operations
    """
    log = Logger()

    def _principalURI(self, principal):
        return principal.principalURL() if principal is not None else "unauthenticated"

    def _uriNotFound(self, f, uri):
        f.trap(AttributeError)
        raise URINotFoundException(uri)

    def _getRecordForURI(self, uri, request):
        """
        Return the directory record for the specified principal uri.
        """
        def _getRecord(resrc):
            if hasattr(resrc, 'record'):
                return resrc.record

        try:
            return request.locateResource(uri).addCallback(
                _getRecord).addErrback(self._uriNotFound, uri)
        except AssertionError:
            raise URINotFoundException(uri)

    @inlineCallbacks
    def _canonicalizeURIForRequest(self, uri, request):
        """
        Always use canonicalized forms of the URIs for caching (i.e. __uids__ paths).

        Do this without calling locateResource which may cause a query on the store.
        """

        uribits = uri.split("/")
        if len(uribits) > 1 and uribits[1] in ("principals", "calendars", "addressbooks"):
            if uribits[2] == "__uids__":
                returnValue(uri)
            else:
                recordType = uribits[2]
                recordName = uribits[3]
                directory = request.site.resource.getDirectory()
                record = yield directory.recordWithShortName(
                    directory.oldNameToRecordType(recordType),
                    recordName
                )
                if record is not None:
                    uribits[2] = "__uids__"
                    uribits[3] = record.uid.encode("utf-8")
                    returnValue("/".join(uribits))

        # Fall back to the locateResource approach
        try:
            resrc = yield request.locateResource(uri)
            returnValue(resrc.url())
        except AssertionError:
            raise URINotFoundException(uri)

    def _getURIs(self, request):
        """
        Get principal and resource URIs from the request.
        """
        def _getSecondURI(rURI):
            return self._canonicalizeURIForRequest(
                self._principalURI(request.authnUser),
                request).addCallback(lambda pURI: (pURI, rURI))

        d = self._canonicalizeURIForRequest(request.uri, request)
        d.addCallback(_getSecondURI)

        return d

    @inlineCallbacks
    def _requestKey(self, request):
        """
        Get a key for this request. This depends on the method, Depth: header, authn user principal,
        request uri and a hash of the request body (the body being normalized for property order).
        """
        requestBody = (yield allDataFromStream(request.stream))
        if requestBody is not None:
            # Give it back to the request so it can be read again
            request.stream = MemoryStream(requestBody)
            request.stream.doStartReading = None

            # Normalize the property order by doing a "dumb" sort on lines
            requestLines = requestBody.splitlines()
            requestLines.sort()
            requestBody = "\n".join(requestLines)

        request.cacheKey = (request.method,
                            self._principalURI(request.authnUser),
                            request.uri,
                            request.headers.getHeader('depth'),
                            hash(requestBody))

        returnValue(request.cacheKey)

    def _getResponseBody(self, key, response):
        d1 = allDataFromStream(response.stream)
        d1.addCallback(lambda responseBody: (key, responseBody))
        return d1


class MemcacheResponseCache(BaseResponseCache, CachePoolUserMixIn):

    def __init__(self, docroot, cachePool=None):
        self._docroot = docroot
        self._cachePool = cachePool

    @inlineCallbacks
    def _tokenForURI(self, uri, cachePoolHandle=None):
        """
        Get the current token for a particular URI.
        """
        if isinstance(uri, unicode):
            uri = uri.encode("utf-8")
        if cachePoolHandle:
            result = (yield defaultCachePool(cachePoolHandle).get('cacheToken:%s' % (uri,)))
        else:
            result = (yield self.getCachePool().get('cacheToken:%s' % (uri,)))
        if result is not None:
            _ignore_flags, result = result
        returnValue(result)

    @inlineCallbacks
    def _tokenForRecord(self, uri, request):
        """
        Get the current token for a particular principal URI's directory record.
        """

        record = (yield self._getRecordForURI(uri, request))
        returnValue(record.cacheToken())

    @inlineCallbacks
    def _tokensForChildren(self, rURI, request):
        """
        Create a dict of child resource tokens for any "recorded" during this request in the childCacheURIs attribute.
        """

        if hasattr(request, "childCacheURIs"):
            tokens = dict([(uri, (yield self._tokenForURI(uri)),) for uri in request.childCacheURIs])
            returnValue(tokens)
        else:
            returnValue({})

    @inlineCallbacks
    def _getTokens(self, request):
        """
        Tokens are a principal token, directory record token, resource token and list
        of child resource tokens. A change to any one of those will cause cache invalidation.
        """
        tokens = []
        pURI, rURI = (yield self._getURIs(request))
        tokens.append((yield self._tokenForURI(pURI, "PrincipalToken")))
        tokens.append((yield self._tokenForRecord(pURI, request)))
        tokens.append((yield self._tokenForURI(rURI)))
        tokens.append((yield self._tokensForChildren(rURI, request)))
        returnValue(tokens)

    @inlineCallbacks
    def _hashedRequestKey(self, request):
        """
        Make a key for a response cache entry. This depends on various request parameters
        (see _requestKey for details).
        """
        oldkey = (yield self._requestKey(request))
        request.cacheKey = key = hashlib.md5(
            ':'.join([str(t) for t in oldkey])).hexdigest()
        self.log.debug("hashing key for get: {old!r} to {new!r}", old=oldkey, new=key)
        returnValue(request.cacheKey)

    @inlineCallbacks
    def getResponseForRequest(self, request):
        """
        Try to match a request and a response cache entry. We first get the request key and match that, then pull
        the cache entry and decompose it into tokens and response. We then compare the cached tokens with their current values.
        If all match, we can return the cached response data.
        """
        try:
            key = (yield self._hashedRequestKey(request))

            self.log.debug("Checking cache for: {key!r}", key=key)
            _ignore_flags, value = (yield self.getCachePool().get(key))

            if value is None:
                self.log.debug("Not in cache: {key!r}", key=key)
                returnValue(None)

            (principalToken, directoryToken, uriToken, childTokens, (code, headers, body)) = cPickle.loads(value)
            self.log.debug(
                "Found in cache: {key!r} = {value!r}",
                key=key,
                value=(
                    principalToken,
                    directoryToken,
                    uriToken,
                    childTokens,
                )
            )

            currentTokens = (yield self._getTokens(request))

            if currentTokens[0] != principalToken:
                self.log.debug(
                    "Principal token doesn't match for {key!r}: {currentToken!r} != {principalToken!r}",
                    key=request.cacheKey,
                    currentToken=currentTokens[0],
                    principalToken=principalToken,
                )
                returnValue(None)

            if currentTokens[1] != directoryToken:
                self.log.debug(
                    "Directory Record Token doesn't match for {key!r}: {currentToken!r} != {directoryToken!r}",
                    key=request.cacheKey,
                    currentToken=currentTokens[1],
                    directoryToken=directoryToken,
                )
                returnValue(None)

            if currentTokens[2] != uriToken:
                self.log.debug(
                    "URI token doesn't match for {key!r}: {currentToken!r} != {uriToken!r}",
                    key=request.cacheKey,
                    currentToken=currentTokens[2],
                    uriToken=uriToken,
                )
                returnValue(None)

            for childuri, token in childTokens.items():
                currentToken = (yield self._tokenForURI(childuri))
                if currentToken != token:
                    self.log.debug(
                        "Child {uri} token doesn't match for {key!r}: {currentToken!r} != {token!r}",
                        uri=childuri,
                        key=request.cacheKey,
                        currentToken=currentToken,
                        token=token,
                    )
                    returnValue(None)

            self.log.debug("Response cache matched")
            r = Response(code, stream=MemoryStream(body))

            for key, value in headers.iteritems():
                r.headers.setRawHeaders(key, value)

            returnValue(r)

        except URINotFoundException as e:
            self.log.debug("Could not locate URI: {e!r}", e=e)
            returnValue(None)

    @inlineCallbacks
    def cacheResponseForRequest(self, request, response):
        """
        Given a request and its response, make a response cache entry that encodes the response and various
        cache tokens. Later, when getResponseForRequest is called we retrieve this entry and compare the
        old cache tokens with the current ones. If any have changed the response cache entry is removed.
        """
        try:
            if hasattr(request, 'cacheKey'):
                key = request.cacheKey
            else:
                key = (yield self._hashedRequestKey(request))

            key, responseBody = (yield self._getResponseBody(key, response))

            response.headers.removeHeader('date')
            response.stream = MemoryStream(responseBody)
            pToken, dToken, uToken, cTokens = (yield self._getTokens(request))

            cacheEntry = cPickle.dumps((
                pToken,
                dToken,
                uToken,
                cTokens,
                (
                    response.code,
                    dict(list(response.headers.getAllRawHeaders())),
                    responseBody
                )
            ))
            self.log.debug(
                "Adding to cache: {key!r} = tokens - {tokens!r}",
                key=key,
                tokens=(
                    pToken,
                    dToken,
                    uToken,
                    cTokens,
                )
            )
            yield self.getCachePool().set(
                key, cacheEntry, expireTime=config.ResponseCacheTimeout * 60
            )

        except URINotFoundException as e:
            self.log.debug("Could not locate URI: {e!r}", e=e)

        returnValue(response)


class _CachedResponseResource(object):
    implements(IResource)

    def __init__(self, response):
        self._response = response

    def renderHTTP(self, request):
        if not hasattr(request, "extendedLogItems"):
            request.extendedLogItems = {}
        request.extendedLogItems["cached"] = "1"
        return self._response

    def locateChild(self, request, segments):
        return self, []


class PropfindCacheMixin(object):
    """
    A mixin that causes a resource's PROPFIND response to be cached. It also adds an api to change the
    resource's uriToken - this must be used whenever something changes to cause the cache to be invalidated.
    """

    @inlineCallbacks
    def renderHTTP(self, request):
        response = (yield super(PropfindCacheMixin, self).renderHTTP(request))

        if request.method == 'PROPFIND':
            resource = (yield request.locateResource("/"))

            # responseCache might not be present during unit tests
            if hasattr(resource, "responseCache"):
                yield resource.responseCache.cacheResponseForRequest(request, response)

        returnValue(response)


class CacheStoreNotifierFactory(CachePoolUserMixIn):
    """
    A notifier factory specifically for store object notifications. This is handed of to
    the data store object, which calls .newNotifier() each time a home object is created
    and gives the new notifier to the home. That object is also inherited by home child
    objects created from the home.

    This object uses a memcachepool for setting new cache tokens.
    """
    log = Logger()

    implements(IStoreNotifierFactory)

    def newNotifier(self, storeObject):
        return CacheStoreNotifier(self, storeObject)

    def _newCacheToken(self):
        return str(uuid.uuid4())

    def changed(self, cache_id):
        """
        Change the cache token for a store object.

        return: A L{Deferred} that fires when the token has been changed.
        """

        self.log.debug("Changing Cache Token for {id!r}", id=cache_id)
        return self.getCachePool().set(
            'cacheToken:%s' % (cache_id,),
            self._newCacheToken(), expireTime=config.ResponseCacheTimeout * 60)


class CacheStoreNotifier(object):
    """
    A notifier for store objects. Store objects will call .notify() when they change.
    """

    implements(IStoreNotifier)

    def __init__(self, notifierFactory, storeObject):
        self._notifierFactory = notifierFactory
        self._storeObject = storeObject

    @inlineCallbacks
    def notify(self):
        """
        We need to convert the store object notifier ID into a URI, since the cache uses URIs.
        Note that for a home child resource we also need to change the token for the home as the
        sync token on the home changes implicitly without a direct notification.
        """

        prefix, id = self._storeObject.notifierID()
        if prefix == "CalDAV":
            uri = "/calendars/__uids__/%s/" % (id,)
        elif prefix == "CardDAV":
            uri = "/addressbooks/__uids__/%s/" % (id,)
        uris = (urllib.quote(uri),)

        # Also add home if needed
        if "/" in id:
            id = id.split("/")[0]
            if prefix == "CalDAV":
                uri = "/calendars/__uids__/%s/" % (id,)
            elif prefix == "CardDAV":
                uri = "/addressbooks/__uids__/%s/" % (id,)
            uris += (urllib.quote(uri),)

        for uri in uris:
            yield self._notifierFactory.changed(uri)

    def clone(self, storeObject):
        return self.__class__(self._notifierFactory, storeObject)
