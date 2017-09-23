# -*- test-case-name: twistedcaldav.directory.test.test_util -*-
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

"""
Utilities.
"""

__all__ = [
    "normalizeUUID",
    "uuidFromName",
    "NotFoundResource",
]

from twext.enterprise.ienterprise import AlreadyFinishedError
from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.auth.wrapper import UnauthorizedResponse
from txweb2.dav.resource import DAVResource
from txweb2.http import StatusResponse
from twisted.internet.defer import inlineCallbacks, returnValue
from uuid import UUID, uuid5
from twisted.python.failure import Failure
from twisted.web.template import tags


log = Logger()


def uuidFromName(namespace, name):
    """
    Generate a version 5 (SHA-1) UUID from a namespace UUID and a name.
    See http://www.ietf.org/rfc/rfc4122.txt, section 4.3.
    @param namespace: a UUID denoting the namespace of the generated UUID.
    @param name: a byte string to generate the UUID from.
    """
    # We don't want Unicode here; convert to UTF-8
    if type(name) is unicode:
        name = name.encode("utf-8")

    return normalizeUUID(str(uuid5(UUID(namespace), name)))


def normalizeUUID(value):
    """
    Convert strings which the uuid.UUID( ) method can parse into normalized
    (uppercase with hyphens) form.  Any value which is not parsed by UUID( )
    is returned as is.
    @param value: string value to normalize
    """
    try:
        return str(UUID(value)).upper()
    except (ValueError, TypeError):
        return value


TRANSACTION_KEY = '_newStoreTransaction'


def transactionFromRequest(request, newStore):
    """
    Return the associated transaction from the given HTTP request, creating a
    new one from the given data store if none has yet been associated.

    Also, if the request was not previously associated with a transaction, add
    a failsafe transaction-abort response filter to abort any transaction which
    has not been committed or aborted by the resource which responds to the
    request.

    @param request: The request to inspect.
    @type request: L{IRequest}

    @param newStore: The store to create a transaction from.
    @type newStore: L{IDataStore}

    @return: a transaction that should be used to read and write data
        associated with the request.
    @rtype: L{ITransaction} (and possibly L{ICalendarTransaction} and
        L{IAddressBookTransaction} as well.
    """
    transaction = getattr(request, TRANSACTION_KEY, None)
    if transaction is None:
        if hasattr(request, "authzUser") and request.authzUser is not None:
            authz_uid = request.authzUser.record.uid
        else:
            authz_uid = None
        transaction = newStore.newTransaction(repr(request), authz_uid=authz_uid)

        def abortIfUncommitted(request, response):
            try:
                # TODO: missing 'yield' here.  For formal correctness as per
                # the interface, this should be allowed to be a Deferred.  (The
                # actual implementation still raises synchronously, so there's
                # no bug currently.)
                transaction.abort()
            except AlreadyFinishedError:
                pass
            return response
        abortIfUncommitted.handleErrors = True
        request.addResponseFilter(abortIfUncommitted)
        setattr(request, TRANSACTION_KEY, transaction)
    return transaction


def splitIntoBatches(data, size):
    """
    Return a generator of sets consisting of the contents of the data set
    split into parts no larger than size.
    """
    if not data:
        yield set([])
    data = list(data)
    while data:
        yield set(data[:size])
        del data[:size]


class NotFoundResource(DAVResource):
    """
    In order to prevent unauthenticated discovery of existing users via 401/404
    response codes, this resource can be returned from locateChild, and it will
    perform an authentication; if the user is unauthenticated, 404 responses are
    turned into 401s.
    """

    @inlineCallbacks
    def renderHTTP(self, request):

        try:
            _ignore_authnUser, authzUser = yield self.authenticate(request)
        except Exception:
            authzUser = None

        # Turn 404 into 401
        if authzUser is None:
            response = (yield UnauthorizedResponse.makeResponse(
                request.credentialFactories,
                request.remoteAddr
            ))
            returnValue(response)
        else:
            response = StatusResponse(responsecode.NOT_FOUND, "Resource not found")
            returnValue(response)


def formatLink(url):
    """
    Convert a URL string into some twisted.web.template DOM objects for
    rendering as a link to itself.
    """
    return tags.a(href=url)(url)


def formatLinks(urls):
    """
    Format a list of URL strings as a list of twisted.web.template DOM links.
    """
    return formatList(formatLink(link) for link in urls)


def formatPrincipals(principals):
    """
    Format a list of principals into some twisted.web.template DOM objects.
    """
    def recordKey(principal):
        try:
            record = principal.record
        except AttributeError:
            try:
                record = principal.parent.record
            except:
                return None
        try:
            shortName = record.shortNames[0]
        except AttributeError:
            shortName = u""
        return (record.recordType, shortName)

    def describe(principal):
        if hasattr(principal, "record"):
            return " - %s" % (principal.record.displayName,)
        else:
            return ""

    return formatList(
        tags.a(href=principal.principalURL())(
            str(principal), describe(principal)
        )
        for principal in sorted(principals, key=recordKey)
    )


def formatList(iterable):
    """
    Format a list of stuff as an interable.
    """
    thereAreAny = False
    try:
        item = None
        for item in iterable:
            thereAreAny = True
            yield " -> "
            if item is None:
                yield "None"
            else:
                yield item
            yield "\n"
    except Exception as e:
        log.error("Exception while rendering: {ex}", ex=e)
        Failure().printTraceback()
        yield "  ** %s **: %s\n" % (e.__class__.__name__, e)
    if not thereAreAny:
        yield " '()\n"
