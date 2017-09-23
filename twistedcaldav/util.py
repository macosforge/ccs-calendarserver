##
# Copyright (c) 2007-2017 Apple Inc. All rights reserved.
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

import os
import re
import sys
import base64
import itertools

from subprocess import Popen, PIPE, STDOUT
from hashlib import md5, sha1

from twisted.internet import reactor
from twisted.web import client
from twisted.python import failure
from twext.python.log import Logger

log = Logger()
from twext.internet.gaiendpoint import GAIEndpoint
from twext.internet.adaptendpoint import connect
from twext.internet.ssl import simpleClientContextFactory

##
# System Resources (Memory size and processor count)
##

try:
    from ctypes import (
        cdll,
        c_int, c_uint64, c_ulong,
        c_char_p, c_void_p,
        addressof, sizeof, c_size_t,
    )
    from ctypes.util import find_library
    hasCtypes = True
except ImportError:
    hasCtypes = False

if (
        sys.platform == "darwin" or
        sys.platform.startswith("freebsd")
) and hasCtypes:
    libc = cdll.LoadLibrary(find_library("libc"))

    def getNCPU():
        """
        Returns the number of processors detected
        """
        ncpu = c_int(0)
        size = c_size_t(sizeof(ncpu))

        libc.sysctlbyname.argtypes = [
            c_char_p, c_void_p, c_void_p, c_void_p, c_ulong
        ]
        libc.sysctlbyname(
            "hw.ncpu",
            c_void_p(addressof(ncpu)),
            c_void_p(addressof(size)),
            None,
            0
        )

        return int(ncpu.value)

    def getMemorySize():
        """
        Returns the physical amount of RAM installed, in bytes
        """
        memsize = c_uint64(0)
        size = c_size_t(sizeof(memsize))

        libc.sysctlbyname.argtypes = [
            c_char_p, c_void_p, c_void_p, c_void_p, c_ulong
        ]
        libc.sysctlbyname(
            "hw.memsize",
            c_void_p(addressof(memsize)),
            c_void_p(addressof(size)),
            None,
            0
        )

        return int(memsize.value)


elif sys.platform == "linux2" and hasCtypes:
    libc = cdll.LoadLibrary(find_library("libc"))

    def getNCPU():
        return libc.get_nprocs()

    def getMemorySize():
        return libc.getpagesize() * libc.get_phys_pages()

else:
    def getNCPU():
        if not hasCtypes:
            msg = " without ctypes"
        else:
            msg = ""

        raise NotImplementedError("getNCPU not supported on %s%s" % (sys.platform, msg))

    def getMemorySize():
        raise NotImplementedError("getMemorySize not yet supported on %s" % (sys.platform))


def computeProcessCount(minimum, perCPU, perGB, cpuCount=None, memSize=None):
    """
    Determine how many process to spawn based on installed RAM and CPUs,
    returning at least "mininum"
    """

    if cpuCount is None:
        try:
            cpuCount = getNCPU()
        except NotImplementedError as e:
            log.error("Unable to detect number of CPUs: {ex}", ex=str(e))
            return minimum

    if memSize is None:
        try:
            memSize = getMemorySize()
        except NotImplementedError as e:
            log.error("Unable to detect amount of installed RAM: {ex}", ex=str(e))
            return minimum

    countByCore = perCPU * cpuCount
    countByMemory = perGB * (memSize / (1024 * 1024 * 1024))

    # Pick the smaller of the two:
    count = min(countByCore, countByMemory)

    # ...but at least "minimum"
    return max(count, minimum)


##
# Module management
##

def submodule(module, name):
    fullname = module.__name__ + "." + name

    try:
        submodule = __import__(fullname)
    except ImportError as e:
        raise ImportError("Unable to import submodule %s from module %s: %s" % (name, module, e))

    for m in fullname.split(".")[1:]:
        submodule = getattr(submodule, m)

    return submodule

##
# Tracebacks
##

from twisted.python.failure import Failure


def printTracebacks(f):
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except:
            Failure().printTraceback()
            raise
    return wrapper


##
# Helpers
##

class Alternator (object):
    """
    Object that alternates between True and False states.
    """

    def __init__(self, state=False):
        self._state = bool(state)

    def state(self):
        """
        @return: the current state
        """
        state = self._state
        self._state = not state
        return state


def utf8String(s):
    if isinstance(s, unicode):
        s = s.encode("utf-8")
    return s


##
# Keychain access
##

class KeychainPasswordNotFound(Exception):
    """
    Exception raised when the password does not exist
    """


class KeychainAccessError(Exception):
    """
    Exception raised when not able to access keychain
    """

passwordRegExp = re.compile(r'password: "(.*)"')


def getPasswordFromKeychain(account):
    if os.path.isfile("/usr/bin/security"):
        child = Popen(
            args=[
                "/usr/bin/security", "find-generic-password",
                "-a", account, "-g",
            ],
            stdout=PIPE, stderr=STDOUT,
        )
        output, error = child.communicate()

        if child.returncode:
            raise KeychainPasswordNotFound(error)
        else:
            match = passwordRegExp.search(output)
            if not match:
                error = "Password for %s not found in keychain" % (account,)
                raise KeychainPasswordNotFound(error)
            else:
                return match.group(1)

    else:
        error = "Keychain access utility ('security') not found"
        raise KeychainAccessError(error)


##
# Digest/Basic-capable HTTP GET factory
##

algorithms = {
    'md5': md5,
    'md5-sess': md5,
    'sha': sha1,
}

# DigestCalcHA1


def calcHA1(
    pszAlg,
    pszUserName,
    pszRealm,
    pszPassword,
    pszNonce,
    pszCNonce,
    preHA1=None
):
    """
    @param pszAlg: The name of the algorithm to use to calculate the digest.
        Currently supported are md5 md5-sess and sha.

    @param pszUserName: The username
    @param pszRealm: The realm
    @param pszPassword: The password
    @param pszNonce: The nonce
    @param pszCNonce: The cnonce

    @param preHA1: If available this is a str containing a previously
       calculated HA1 as a hex string. If this is given then the values for
       pszUserName, pszRealm, and pszPassword are ignored.
    """

    if (preHA1 and (pszUserName or pszRealm or pszPassword)):
        raise TypeError(("preHA1 is incompatible with the pszUserName, "
                         "pszRealm, and pszPassword arguments"))

    if preHA1 is None:
        # We need to calculate the HA1 from the username:realm:password
        m = algorithms[pszAlg]()
        m.update(pszUserName)
        m.update(":")
        m.update(pszRealm)
        m.update(":")
        m.update(pszPassword)
        HA1 = m.digest()
    else:
        # We were given a username:realm:password
        HA1 = preHA1.decode('hex')

    if pszAlg == "md5-sess":
        m = algorithms[pszAlg]()
        m.update(HA1)
        m.update(":")
        m.update(pszNonce)
        m.update(":")
        m.update(pszCNonce)
        HA1 = m.digest()

    return HA1.encode('hex')


# DigestCalcResponse
def calcResponse(
    HA1,
    algo,
    pszNonce,
    pszNonceCount,
    pszCNonce,
    pszQop,
    pszMethod,
    pszDigestUri,
    pszHEntity,
):
    m = algorithms[algo]()
    m.update(pszMethod)
    m.update(":")
    m.update(pszDigestUri)
    if pszQop == "auth-int":
        m.update(":")
        m.update(pszHEntity)
    HA2 = m.digest().encode('hex')

    m = algorithms[algo]()
    m.update(HA1)
    m.update(":")
    m.update(pszNonce)
    m.update(":")
    if pszNonceCount and pszCNonce and pszQop:
        m.update(pszNonceCount)
        m.update(":")
        m.update(pszCNonce)
        m.update(":")
        m.update(pszQop)
        m.update(":")
    m.update(HA2)
    respHash = m.digest().encode('hex')
    return respHash


class Unauthorized(Exception):
    pass


class AuthorizedHTTPGetter(client.HTTPPageGetter):
    log = Logger()

    def handleStatus_401(self):

        self.quietLoss = 1
        self.transport.loseConnection()

        if not hasattr(self.factory, "username"):
            self.factory.deferred.errback(failure.Failure(Unauthorized("Authentication required")))
            return self.factory.deferred

        if hasattr(self.factory, "retried"):
            self.factory.deferred.errback(failure.Failure(Unauthorized("Could not authenticate user %s with calendar server" % (self.factory.username,))))
            return self.factory.deferred

        self.factory.retried = True

        # self.log.debug("Got a 401 trying to inject [{hdrs}]", hdrs=self.headers)
        details = {}
        basicAvailable = digestAvailable = False
        wwwauth = self.headers.get("www-authenticate")
        for item in wwwauth:
            if item.startswith("basic "):
                basicAvailable = True
            if item.startswith("digest "):
                digestAvailable = True
                wwwauth = item[7:]

                def unq(s):
                    if s[0] == s[-1] == '"':
                        return s[1:-1]
                    return s
                parts = wwwauth.split(',')
                for (k, v) in [p.split('=', 1) for p in parts]:
                    details[k.strip()] = unq(v.strip())

        user = self.factory.username
        pswd = self.factory.password

        if digestAvailable and details:
            digest = calcResponse(
                calcHA1(
                    details.get('algorithm'),
                    user,
                    details.get('realm'),
                    pswd,
                    details.get('nonce'),
                    details.get('cnonce')
                ),
                details.get('algorithm'),
                details.get('nonce'),
                details.get('nc'),
                details.get('cnonce'),
                details.get('qop'),
                self.factory.method,
                self.factory.url,
                None
            )

            if details.get('qop'):
                response = (
                    'Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                    'response=%s, algorithm=%s, cnonce="%s", qop=%s, nc=%s' %
                    (
                        user,
                        details.get('realm'),
                        details.get('nonce'),
                        self.factory.url,
                        digest,
                        details.get('algorithm'),
                        details.get('cnonce'),
                        details.get('qop'),
                        details.get('nc'),
                    )
                )
            else:
                response = (
                    'Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                    'response=%s, algorithm=%s' %
                    (
                        user,
                        details.get('realm'),
                        details.get('nonce'),
                        self.factory.url,
                        digest,
                        details.get('algorithm'),
                    )
                )

            self.factory.headers['Authorization'] = response

            if self.factory.scheme == 'https':
                connect(
                    GAIEndpoint(reactor, self.factory.host, self.factory.port, simpleClientContextFactory(self.factory.host)),
                    self.factory)
            else:
                connect(
                    GAIEndpoint(reactor, self.factory.host, self.factory.port),
                    self.factory)
            # self.log.debug("Retrying with digest after 401")

            return self.factory.deferred

        elif basicAvailable:
            basicauth = "%s:%s" % (user, pswd)
            basicauth = "Basic " + base64.encodestring(basicauth)
            basicauth = basicauth.replace("\n", "")

            self.factory.headers['Authorization'] = basicauth

            if self.factory.scheme == 'https':
                connect(
                    GAIEndpoint(reactor, self.factory.host, self.factory.port, simpleClientContextFactory(self.factory.host)),
                    self.factory)
            else:
                connect(
                    GAIEndpoint(reactor, self.factory.host, self.factory.port),
                    self.factory)
            # self.log.debug("Retrying with basic after 401")

            return self.factory.deferred

        else:
            self.factory.deferred.errback(failure.Failure(Unauthorized("Mail gateway not able to process reply; calendar server returned 401 and doesn't support basic or digest")))
            return self.factory.deferred


def bestAcceptType(accepts, allowedTypes):
    """
    Given a set of Accept headers and the set of types the server can return, determine the best choice
    of format to return to the client.

    @param accepts: parsed accept headers
    @type accepts: C{dict}
    @param allowedTypes: list of allowed types in server preferred order
    @type allowedTypes: C{list}
    """

    # If no Accept present just use the first allowed type - the server's preference
    if not accepts:
        return allowedTypes[0]

    # Get mapping for ordered top-level types for use in subtype wildcard match
    toptypes = {}
    for allowed in allowedTypes:
        mediaType = allowed.split("/")[0]
        if mediaType not in toptypes:
            toptypes[mediaType] = allowed

    result = None
    result_qval = 0.0
    for content_type, qval in accepts.items():
        # Exact match
        ctype = "%s/%s" % (content_type.mediaType, content_type.mediaSubtype,)
        if ctype in allowedTypes:
            if qval > result_qval:
                result = ctype
                result_qval = qval

        # Subtype wildcard match
        elif content_type.mediaType != "*" and content_type.mediaSubtype == "*":
            if content_type.mediaType in toptypes:
                if qval > result_qval:
                    result = toptypes[content_type.mediaType]
                    result_qval = qval

        # Full wildcard match
        elif content_type.mediaType == "*" and content_type.mediaSubtype == "*":
            if qval > result_qval:
                result = allowedTypes[0]
                result_qval = qval

    return result


def userAgentProductTokens(user_agent):
    """
    Parse an HTTP User-Agent header to extract the product tokens and ignore
    any parenthesized comment strings in the header.

    @param user_agent: text of User-Agent header value
    @type user_agent: L{str}

    @return: list of product tokens extracted from the header
    @rtype: L{list}
    """

    ua_hdr = user_agent.split()
    ua_tokens = []
    comment = False
    for token in ua_hdr:
        if comment:
            if token.endswith(")"):
                comment = False
        elif token.startswith("("):
            if not token.endswith(")"):
                comment = True
        else:
            ua_tokens.append(token)

    return ua_tokens


def matchClientFixes(config, user_agent):
    """
    Given a user-agent string, see if it matches any of the configured client fixes.

    @param config: the L{config} to match against.
    @type config: L{ConfigDict}
    @param user_agent: the HTTP User-Agent header value to test.
    @type user_agent: L{str}
    """

    if len(config.ClientFixesCompiled) == 0 or not user_agent:
        return set()

    ua_tokens = userAgentProductTokens(user_agent)

    client_fixes = set()
    for fix, patterns in config.ClientFixesCompiled.items():
        for pattern, token in itertools.product(patterns, ua_tokens):
            if pattern.match(token) is not None:
                client_fixes.add(fix)
                break
    return client_fixes
