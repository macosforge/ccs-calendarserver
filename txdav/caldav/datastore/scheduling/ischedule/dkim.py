##
# Copyright (c) 2012-2017 Apple Inc. All rights reserved.
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
from txweb2.client.http import ClientRequest
from txweb2.dav.util import allDataFromStream, joinURL
from txweb2.http import Response
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream

from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twistedcaldav.client.geturl import getURL
from twistedcaldav.config import ConfigurationError
from twistedcaldav.simpleresource import SimpleResource, SimpleDataResource
from txdav.caldav.datastore.scheduling.ischedule.utils import lookupDataViaTXT, \
    lookupServerViaSRV

from Crypto.Hash import SHA, SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

import base64
import hashlib
import os
import textwrap
import time
import uuid

"""
DKIM HTTP message generation and validation,
"""

log = Logger()

# DKIM/iSchedule Constants
RSA1 = "rsa-sha1"
RSA256 = "rsa-sha256"
Q_DNS = "dns/txt"
Q_HTTP = "http/well-known"
Q_PRIVATE = "private-exchange"

KEY_SERVICE_TYPE = "ischedule"

# Headers
DKIM_SIGNATURE = "DKIM-Signature"
ISCHEDULE_VERSION = "iSchedule-Version"
ISCHEDULE_VERSION_VALUE = "1.0"
ISCHEDULE_MESSAGE_ID = "iSchedule-Message-ID"
ISCHEDULE_CAPABILITIES = "iSchedule-Capabilities"


class DKIMUtils(object):
    """
    Some useful functions.
    """

    @staticmethod
    def validConfiguration(config):
        if config.Scheduling.iSchedule.DKIM.Enabled:

            if not config.Scheduling.iSchedule.DKIM.Domain and not config.ServerHostName:
                msg = "DKIM: No domain specified"
                log.error(msg)
                raise ConfigurationError(msg)

            if not config.Scheduling.iSchedule.DKIM.KeySelector:
                msg = "DKIM: No selector specified"
                log.error(msg)
                raise ConfigurationError(msg)

            if config.Scheduling.iSchedule.DKIM.SignatureAlgorithm not in (RSA1, RSA256):
                msg = "DKIM: Invalid algorithm: %s" % (config.Scheduling.iSchedule.SignatureAlgorithm,)
                log.error(msg)
                raise ConfigurationError(msg)

            try:
                with open(config.Scheduling.iSchedule.DKIM.PrivateKeyFile) as f:
                    key_data = f.read()
            except IOError as e:
                msg = "DKIM: Cannot read private key file: %s %s" % (config.Scheduling.iSchedule.DKIM.PrivateKeyFile, e,)
                log.error(msg)
                raise ConfigurationError(msg)
            try:
                RSA.importKey(key_data)
            except:
                msg = "DKIM: Invalid private key file: %s" % (config.Scheduling.iSchedule.DKIM.PrivateKeyFile,)
                log.error(msg)
                raise ConfigurationError(msg)

            try:
                with open(config.Scheduling.iSchedule.DKIM.PublicKeyFile) as f:
                    key_data = f.read()
            except IOError as e:
                msg = "DKIM: Cannot read public key file: %s %s" % (config.Scheduling.iSchedule.DKIM.PublicKeyFile, e,)
                log.error(msg)
                raise ConfigurationError(msg)
            try:
                RSA.importKey(key_data)
            except:
                msg = "DKIM: Invalid public key file: %s" % (config.Scheduling.iSchedule.DKIM.PublicKeyFile,)
                log.error(msg)
                raise ConfigurationError(msg)

            if config.Scheduling.iSchedule.DKIM.PrivateExchanges:
                if not os.path.exists(config.Scheduling.iSchedule.DKIM.PrivateExchanges):
                    try:
                        os.makedirs(config.Scheduling.iSchedule.DKIM.PrivateExchanges)
                    except IOError as e:
                        msg = "DKIM: Cannot create public key private exchange directory: %s" % (config.Scheduling.iSchedule.DKIM.PrivateExchanges,)
                        log.error(msg)
                        raise ConfigurationError(msg)
                if not os.path.isdir(config.Scheduling.iSchedule.DKIM.PrivateExchanges):
                    msg = "DKIM: Invalid public key private exchange directory: %s" % (config.Scheduling.iSchedule.DKIM.PrivateExchanges,)
                    log.error(msg)
                    raise ConfigurationError(msg)
                PublicKeyLookup_PrivateExchange.directory = config.Scheduling.iSchedule.DKIM.PrivateExchanges

            log.info("DKIM: Enabled")
        else:
            log.info("DKIM: Disabled")

    @staticmethod
    def getConfiguration(config):
        """
        Return a tuple of the parameters derived from the config that are used to initialize the DKIMRequest.

        @param config: configuration to look at
        @type config: L{Config}
        """

        domain = config.Scheduling.iSchedule.DKIM.Domain if config.Scheduling.iSchedule.DKIM.Domain else config.ServerHostName
        selector = config.Scheduling.iSchedule.DKIM.KeySelector
        key_file = config.Scheduling.iSchedule.DKIM.PrivateKeyFile
        algorithm = config.Scheduling.iSchedule.DKIM.SignatureAlgorithm
        useDNSKey = config.Scheduling.iSchedule.DKIM.UseDNSKey
        useHTTPKey = config.Scheduling.iSchedule.DKIM.UseHTTPKey
        usePrivateExchangeKey = config.Scheduling.iSchedule.DKIM.UsePrivateExchangeKey
        expire = config.Scheduling.iSchedule.DKIM.ExpireSeconds

        return domain, selector, key_file, algorithm, useDNSKey, useHTTPKey, usePrivateExchangeKey, expire

    @staticmethod
    def hashlib_method(algorithm):
        """
        Return hashlib function for DKIM algorithm.
        """
        return {
            RSA1: hashlib.sha1,
            RSA256: hashlib.sha256,
        }[algorithm]

    @staticmethod
    def hash_name(algorithm):
        """
        Return RSA hash name for DKIM algorithm.
        """
        return {
            RSA1: "SHA-1",
            RSA256: "SHA-256",
        }[algorithm]

    @staticmethod
    def hash_func(algorithm):
        """
        Return RSA hash name for DKIM algorithm.
        """
        return {
            RSA1: SHA,
            RSA256: SHA256,
        }[algorithm]

    @staticmethod
    def extractTags(data):
        """
        Split a DKIM tag list into a dict, removing unneeded whitespace.
        """
        # Extract tags from the data
        splits = [item.strip() for item in data.split(";")]
        dkim_tags = {}
        for item in splits:
            try:
                name, value = item.split("=", 1)
                dkim_tags[name.strip()] = value.strip()
            except ValueError:
                pass
        return dkim_tags

    @staticmethod
    def canonicalizeHeader(name, value, dkim_tags=None, method="ischedule-relaxed"):

        if method == "relaxed":
            return DKIMUtils.relaxedHeader(name, value, dkim_tags)
        elif method == "ischedule-relaxed":
            return DKIMUtils.ischeduleHeader(name, value, dkim_tags)
        else:
            assert "Invalid header canonicalization method: %s" % (method,)

    @staticmethod
    def relaxedHeader(name, value, dkim_tags=None):
        """
        Canonicalize the header using "relaxed" method. Optionally remove the b= value from
        any DKIM-Signature present.

        FIXME: this needs to be smarter about where valid WSP can occur in a header. Right now it will
        blindly collapse all runs of SP/HTAB into a single SP. That could be wrong if a legitimate sequence of
        SP/HTAB occurs in a header value.

        @param name: header name
        @type name: C{str}
        @param value: header value
        @type value: C{str}
        @param dkim_tags: the extracted DKIM tags, or C{None} if no removal needed
        @type dkim_tags: C{dict} or C{None}
        """

        # Special case DKIM-Signature: remove the b= value for signature
        name = name.lower()
        if dkim_tags is not None and name == DKIM_SIGNATURE.lower():
            value = DKIMUtils.canonicalizeDKIMHeaderFields(value, dkim_tags)

        # Basic relaxed behavior
        value = " ".join(value.split())

        crlf = "" if name == DKIM_SIGNATURE.lower() else "\r\n"
        return "%s:%s%s" % (name, value, crlf)

    @staticmethod
    def ischeduleHeader(name, value, dkim_tags=None):
        """
        Canonicalize the header using "ischedule-relaxed" method. Optionally remove the b= value from
        any DKIM-Signature present.

        FIXME: this needs to be smarter about where valid WSP can occur in a header. Right now it will
        blindly collapse all runs of SP/HTAB into a single SP. That could be wrong if a legitimate sequence of
        SP/HTAB occurs in a header value.

        @param name: header name
        @type name: C{str}
        @param value: header value
        @type value: C{str}
        @param dkim_tags: the extracted DKIM tags, or C{None} if no removal needed
        @type dkim_tags: C{dict} or C{None}
        """

        # Special case DKIM-Signature: remove the b= value for signature
        name = name.lower()
        if dkim_tags is not None and name == DKIM_SIGNATURE.lower():
            value = DKIMUtils.canonicalizeDKIMHeaderFields(value, dkim_tags)

        # Basic relaxed behavior
        value = " ".join(value.split())
        value = value.replace(" ,", ",")
        value = value.replace(", ", ",")

        crlf = "" if name == DKIM_SIGNATURE.lower() else "\r\n"
        return "%s:%s%s" % (name, value, crlf)

    @staticmethod
    def canonicalizeDKIMHeaderFields(value, dkim_tags):
        """
        DKIM-Signature b= value needs to be stripped.

        @param value: header value to process
        @type value: C{str}
        """

        pos = value.find(dkim_tags["b"])
        value = value[:pos] + value[pos + len(dkim_tags["b"]):]
        value = " ".join(value.split())
        return value

    @staticmethod
    def canonicalizeBody(data):
        """
        DKIM simple body canonicalization: remove empty lines at the end
        and ensure it ends with one \r\n.

        @param data: data to canonicalize
        @type data: L{str}
        """
        while data.endswith("\r\n"):
            data = data[:-2]
        data += "\r\n"
        return data

    @staticmethod
    def sign(data, privkey, hashfunc):
        h = hashfunc.new(data)
        signer = PKCS1_v1_5.new(privkey)
        return base64.b64encode(signer.sign(h))

    @staticmethod
    def verify(data, signature, pubkey, hashfunc):
        h = hashfunc.new(data)
        verifier = PKCS1_v1_5.new(pubkey)
        if not verifier.verify(h, base64.b64decode(signature)):
            raise ValueError()


class DKIMRequest(ClientRequest):
    """
    A ClientRequest that optionally creates a DKIM signature.
    """

    keys = {}

    def __init__(
        self,
        method,
        uri,
        headers,
        stream,
        domain,
        selector,
        key_file,
        algorithm,
        sign_headers,
        useDNSKey,
        useHTTPKey,
        usePrivateExchangeKey,
        expire,
    ):
        """
        Create a DKIM request, which is a regular client request with the additional information needed to sign the message.

        @param method: HTTP method to use
        @type method: C{str}
        @param uri: request-URI
        @type uri: C{str}
        @param headers: request headers
        @type headers: L{http_headers}
        @param stream: body data
        @type stream: L{Stream}
        @param domain: the signing domain
        @type domain: C{str}
        @param selector: the signing key selector
        @type selector: C{str}
        @param key_file: path to a private key file
        @type key_file: C{str}
        @param algorithm: the signing algorithm to use
        @type algorithm: C{str}
        @param sign_headers: list of header names to sign - to "over sign" a header append a "+" to the name
        @type sign_headers: C{tuple}
        @param useDNSKey: whether or not to add DNS TXT lookup as a key lookup option
        @type useDNSKey: C{bool}
        @param useHTTPKey: whether or not to add HTTP .well-known as a key lookup option
        @type useHTTPKey: C{bool}
        @param usePrivateExchangeKey: whether or not to add private-exchange as a key lookup option
        @type usePrivateExchangeKey: C{bool}
        @param expire: number of seconds to expiration of signature
        @type expire: C{int}
        """
        super(DKIMRequest, self).__init__(method, uri, headers, stream)
        self.domain = domain
        self.selector = selector
        self.algorithm = algorithm
        self.key_file = key_file
        self.sign_headers = sign_headers
        self.time = str(int(time.time()))
        self.expire = str(int(time.time() + expire))

        assert self.domain
        assert self.selector
        assert self.algorithm in (RSA1, RSA256,)
        assert useDNSKey or useHTTPKey or usePrivateExchangeKey

        self.hash_method = DKIMUtils.hashlib_method(self.algorithm)
        self.hash_name = DKIMUtils.hash_name(self.algorithm)
        self.hash_func = DKIMUtils.hash_func(self.algorithm)

        self.keyMethods = []
        if usePrivateExchangeKey:
            self.keyMethods.append(Q_PRIVATE)
        if useHTTPKey:
            self.keyMethods.append(Q_HTTP)
        if useDNSKey:
            self.keyMethods.append(Q_DNS)

        self.message_id = str(uuid.uuid4())

    @inlineCallbacks
    def sign(self):
        """
        Generate the DKIM headers by signing the request. This should only be called once on the request and there must
        be no changes to the request (no headers, no body change) after it is called.
        """

        # Get the headers and the DKIM-Signature tags
        headers, dkim_tags = (yield self.signatureHeaders())

        # Sign the hash
        signature = self.generateSignature(headers)

        # Complete the header
        dkim_tags[-1] = ("b", signature,)
        dkim_header = "; ".join(["%s=%s" % item for item in dkim_tags])
        self.headers.addRawHeader(DKIM_SIGNATURE, dkim_header)

        log.debug("DKIM: Generated header: DKIM-Signature:{hdr}", hdr=dkim_header)
        log.debug("DKIM: Signed headers:\n{hdrs}", hdrs=headers)

        returnValue(signature)

    @inlineCallbacks
    def bodyHash(self):
        """
        Generate the hash of the request body data.
        """

        # We need to play a trick with the request stream as we can only read it once. So we
        # read it, store the value in a MemoryStream, and replace the request's stream with that,
        # so the data can be read again.
        data = (yield allDataFromStream(self.stream))
        self.stream = MemoryStream(data if data is not None else "")
        self.stream.doStartReading = None

        returnValue(base64.b64encode(self.hash_method(DKIMUtils.canonicalizeBody(data)).digest()))

    @inlineCallbacks
    def signatureHeaders(self):
        """
        Generate the headers that are going to be signed as well as the DKIM-Signature tags.
        """

        # Make sure we have the required iSchedule headers
        self.headers.addRawHeader(ISCHEDULE_VERSION, ISCHEDULE_VERSION_VALUE)
        self.headers.addRawHeader(ISCHEDULE_MESSAGE_ID, self.message_id)
        self.sign_headers += (ISCHEDULE_VERSION, ISCHEDULE_MESSAGE_ID,)

        # Need Cache-Control
        self.headers.setRawHeaders("Cache-Control", ("no-cache", "no-transform",))

        # Figure out all the existing headers to sign
        headers = []
        sign_headers = []
        raw = dict([(name.lower(), values) for name, values in self.headers.getAllRawHeaders()])
        for name in self.sign_headers:
            # ischedule-relaxed canonicalization requires headers with the same name concatenated
            # with a comma in between
            value = ",".join(raw.get(name.lower(), ()))
            headers.append(DKIMUtils.canonicalizeHeader(name, value))
            sign_headers.append(name)

        # Generate the DKIM header tags we care about
        dkim_tags = []
        dkim_tags.append(("v", "1",))
        dkim_tags.append(("d", self.domain,))
        dkim_tags.append(("s", self.selector,))
        dkim_tags.append(("t", self.time,))
        dkim_tags.append(("x", self.expire,))
        dkim_tags.append(("a", self.algorithm,))
        dkim_tags.append(("q", ":".join(self.keyMethods),))
        dkim_tags.append(("c", "ischedule-relaxed/simple",))
        dkim_tags.append(("h", ":".join(sign_headers),))
        dkim_tags.append(("bh", (yield self.bodyHash()),))
        dkim_tags.append(("b", "",))
        dkim_header = "; ".join(["%s=%s" % item for item in dkim_tags])

        headers.append(DKIMUtils.canonicalizeHeader(DKIM_SIGNATURE, dkim_header))
        headers = "".join(headers)

        returnValue((headers, dkim_tags,))

    def generateSignature(self, headers):
        # Sign the hash
        if self.key_file not in self.keys:
            with open(self.key_file) as f:
                key = f.read()
            self.keys[self.key_file] = RSA.importKey(key)
        return DKIMUtils.sign(headers, self.keys[self.key_file], self.hash_func)


class DKIMMissingError(Exception):
    """
    Used to indicate that the DKIM-Signature header is not present when
    attempting verification.
    """
    pass


class DKIMVerificationError(Exception):
    """
    Used to indicate a DKIM verification error.
    """
    pass


class DKIMVerifier(object):
    """
    Class used to verify an DKIM-signed HTTP request.
    """

    def __init__(self, headers, body, key_lookup=None, protocol_debug=False):
        """
        @param headers: The HTTP request headers to process
        @type headers: L{txweb2.http_headers.Headers}
        @param body: The HTTP request body to process
        @type body: C{str}
        """
        self.headers = headers
        self.body = body
        self._debug = protocol_debug
        self.dkim_tags = {}

        # Prefer private exchange over HTTP over DNS when multiple are present
        self.key_lookup_methods = (
            PublicKeyLookup_PrivateExchange,
            PublicKeyLookup_HTTP_WellKnown,
            PublicKeyLookup_DNSTXT,
        ) if key_lookup is None else key_lookup

        self.time = int(time.time())

    @inlineCallbacks
    def verify(self):
        """
        @raise: DKIMVerificationError
        """

        # Check presence of DKIM header
        self.processDKIMHeader()

        # Extract the set of canonicalized headers being signed
        headers = self.extractSignedHeaders()
        log.debug("DKIM: Signed headers:\n{hdrs}", hdrs=headers)

        # Locate the public key
        pubkey = (yield self.locatePublicKey())
        if pubkey is None:
            raise DKIMVerificationError("No public key to verify the DKIM signature")

        # Do header verification
        try:
            DKIMUtils.verify(headers, self.dkim_tags["_b"], pubkey, self.hash_func)
        except ValueError:
            msg = "Could not verify signature"
            _debug_msg = """
DKIM-Signature:%s

Headers to evaluate:
%s

Public key used:
%s
""" % (self.headers.getRawHeaders(DKIM_SIGNATURE)[0], headers, pubkey._original_data,)
            log.debug("DKIM: {msg}:{debug}", msg=msg, debug=_debug_msg)
            if self._debug:
                msg = "%s:%s" % (msg, _debug_msg,)
            raise DKIMVerificationError(msg)

        # Do body validation
        body = DKIMUtils.canonicalizeBody(self.body)
        bh = base64.b64encode(self.hash_method(body).digest())
        if bh != self.dkim_tags["_bh"]:
            msg = "Could not verify the DKIM body hash"
            _debug_msg = """
DKIM-Signature:%s

Hash Method: %s

Base64 encoded body:
%s
""" % (self.headers.getRawHeaders(DKIM_SIGNATURE), self.hash_method.__name__, base64.b64encode(body),)
            log.debug("DKIM: {msg}:{debug}", msg=msg, debug=_debug_msg)
            if self._debug:
                msg = "%s:%s" % (msg, _debug_msg,)
            raise DKIMVerificationError(msg)

    def processDKIMHeader(self):
        """
        Extract the DKIM-Signature header and process the tags.

        @raise: DKIMVerificationError
        """

        # Check presence of header
        dkim = self.headers.getRawHeaders(DKIM_SIGNATURE)
        if dkim is None:
            msg = "No DKIM-Signature header present in the request"
            log.debug("DKIM: {msg}", msg=msg)
            raise DKIMMissingError(msg)
        if len(dkim) != 1:
            # TODO: This might need to be changed if we ever support forwarding of iSchedule messages - the forwarder
            # might also sign the message and add its own header
            msg = "Only one DKIM-Signature allowed in the request"
            log.debug("DKIM: {msg}", msg=msg)
            raise DKIMVerificationError(msg)
        dkim = dkim[0]
        log.debug("DKIM: Found header: DKIM-Signature:{hdr}", hdr=dkim)

        # Extract tags from the header
        self.dkim_tags = DKIMUtils.extractTags(dkim)

        # Verify validity of tags
        required_tags = ("v", "a", "b", "bh", "c", "d", "h", "s",)
        for tag in required_tags:
            if tag not in self.dkim_tags:
                msg = "Missing DKIM-Signature tag: %s" % (tag,)
                log.debug("DKIM: {msg}", msg=msg)
                raise DKIMVerificationError(msg)

        check_values = {
            "v": ("1",),
            "a": (RSA1, RSA256,),
            "c": ("ischedule-relaxed", "ischedule-relaxed/simple",),
            "q": (Q_DNS, Q_HTTP, Q_PRIVATE,),
        }
        for tag, values in check_values.items():
            if tag not in required_tags and tag not in self.dkim_tags:
                pass

            # Handle some structured values
            if tag == "q":
                test = self.dkim_tags[tag].split(":")
            else:
                test = (self.dkim_tags[tag],)
            for item in test:
                if item not in values:
                    msg = "Tag: %s has incorrect value: %s" % (tag, self.dkim_tags[tag],)
                    log.debug("DKIM: {msg}", msg=msg)
                    raise DKIMVerificationError(msg)

        # Check time stamp
        if "t" in self.dkim_tags and self.time:
            diff_time = self.time - int(self.dkim_tags["t"])
            if diff_time < -360:
                msg = "Signature time too far in the future: %d seconds" % (diff_time,)
                log.debug("DKIM: {msg}", msg=msg)
                raise DKIMVerificationError(msg)

        # Check expiration
        if "x" in self.dkim_tags and self.time:
            diff_time = self.time - int(self.dkim_tags["x"])
            if diff_time > 0:
                msg = "Signature expired: %d seconds" % (diff_time,)
                log.debug("DKIM: {msg}", msg=msg)
                raise DKIMVerificationError(msg)

        # Base64 encoded tags might include WSP which we need to ignore
        for tag in ("b", "bh",):
            self.dkim_tags["_%s" % (tag,)] = "".join(self.dkim_tags[tag].split())

        # Some useful bits
        self.hash_method = DKIMUtils.hashlib_method(self.dkim_tags["a"])
        self.hash_func = DKIMUtils.hash_func(self.dkim_tags["a"])
        self.key_methods = self.dkim_tags["q"].split(":")

    def extractSignedHeaders(self):
        """
        Extract the set of headers from the request that are supposed to be signed. Canonicalize them
        and return the expected signed data.
        """

        # Extract all the expected signed headers taking into account multiple occurrences of a header
        # which get concatenated with a single comma in between.
        header_list = [hdr.strip() for hdr in self.dkim_tags["h"].split(":")]

        headers = []
        for header in header_list:
            actual_headers = self.headers.getRawHeaders(header)
            if actual_headers:
                headers.append((header, ",".join(actual_headers),))

        # DKIM-Signature is always included at the end
        headers.append((DKIM_SIGNATURE, self.headers.getRawHeaders(DKIM_SIGNATURE)[0],))

        # Now canonicalize the values
        return "".join([DKIMUtils.canonicalizeHeader(name, value, dkim_tags=self.dkim_tags) for name, value in headers])

    @inlineCallbacks
    def locatePublicKey(self):
        """
        Try to lookup the public key matching the signature.
        """

        for lookup in self.key_lookup_methods:
            if lookup.method in self.key_methods or lookup.method == "*":
                pubkey = (yield lookup(self.dkim_tags).getPublicKey())
                if pubkey is not None:
                    returnValue(pubkey)
        else:
            returnValue(None)


class PublicKeyLookup(object):
    """
    Abstract base class for public key lookup methods.

    The L{method} attribute indicated the DKIM q= lookup method that the class will support, or if set to "*",
    the class will handle any q= value.
    """

    keyCache = {}
    method = None

    def __init__(self, dkim_tags):
        self.dkim_tags = dkim_tags

    @inlineCallbacks
    def getPublicKey(self, useCache=True):
        """
        Get key from cache or directly do query.

        @param useCache: whether or not to use the cache
        @type useCache: C{bool}
        """
        key = self._getSelectorKey()
        if key not in PublicKeyLookup.keyCache or not useCache:
            pubkeys = (yield self._lookupKeys())
            PublicKeyLookup.keyCache[key] = pubkeys

        returnValue(self._selectKey())

    def _getSelectorKey(self):
        """
        Get a token used to uniquely identify the key being looked up. Token format will
        depend on the lookup method.
        """
        raise NotImplementedError

    def _lookupKeys(self):
        """
        Do the key lookup using the actual lookup method. Return a C{list} of C{dict}
        that contains the key tag-list. Return a L{Deferred}.
        """
        raise NotImplementedError

    def _selectKey(self):
        """
        Select a specific key from the list that best matches the DKIM-Signature tags
        """

        pubkeys = PublicKeyLookup.keyCache.get(self._getSelectorKey(), [])
        for pkey in pubkeys:
            # Check validity
            if pkey.get("v", "DKIM1") != "DKIM1":
                continue

            # Check key type
            if pkey.get("k", "rsa") != "rsa":
                continue

            # Check valid hash algorithms
            hashes = set([hash.strip() for hash in pkey.get("h", "sha1:sha256").split(":")])
            if self.dkim_tags["a"][4:] not in hashes:
                continue

            # Service type
            if pkey.get("s", KEY_SERVICE_TYPE) not in ("*", KEY_SERVICE_TYPE,):
                continue

            # Non-revoked key
            if len(pkey.get("p", "")) == 0:
                continue

            return self._makeKey(pkey)

        log.debug("DKIM: No valid public key: {sel} {keys}", sel=self._getSelectorKey(), keys=pubkeys)
        return None

    def _makeKey(self, pkey):
        """
        Turn the key tag list into an actual RSA public key object

        @param pkey: key tag list
        @type pkey: C{list}
        """
        key_data = """-----BEGIN PUBLIC KEY-----
%s
-----END PUBLIC KEY-----
""" % ("\n".join(textwrap.wrap(pkey["p"], 64)),)

        try:
            key = RSA.importKey(key_data)
            key._original_data = key_data
            return key
        except:
            log.debug("DKIM: Unable to make public key:\n{key}", key=key_data)
            return None

    @staticmethod
    def flushCache():
        PublicKeyLookup.keyCache = {}


class PublicKeyLookup_DNSTXT(PublicKeyLookup):

    method = Q_DNS

    def _getSelectorKey(self):
        """
        Get a token used to uniquely identify the key being looked up. Token format will
        depend on the lookup method.
        """
        return "%s._domainkey.%s" % (self.dkim_tags["s"], self.dkim_tags["d"],)

    @inlineCallbacks
    def _lookupKeys(self):
        """
        Do the key lookup using the actual lookup method.
        """
        log.debug("DKIM: TXT lookup: {key}", key=self._getSelectorKey())
        data = (yield lookupDataViaTXT(self._getSelectorKey()))
        log.debug("DKIM: TXT lookup results: {key}\n{data}", key=self._getSelectorKey(), data="\n".join(data))
        returnValue(tuple([DKIMUtils.extractTags(line) for line in data]))


class PublicKeyLookup_HTTP_WellKnown(PublicKeyLookup):

    method = Q_HTTP

    def _getSelectorKey(self):
        """
        Get a token used to uniquely identify the key being looked up. Token format will
        depend on the lookup method.
        """

        host = ".".join(self.dkim_tags["d"].split(".")[-2:])
        return "https://%s/.well-known/domainkey/%s/%s" % (host, self.dkim_tags["d"], self.dkim_tags["s"],)

    @inlineCallbacks
    def _getURI(self):
        """
        Determine the well-known URI for the public key service.
        """

        # First we do an SRV lookup for _domainkey to get the public key server host/port
        result = (yield lookupServerViaSRV(self.dkim_tags["d"], service="_domainkey_lookup"))
        if result is None:
            log.debug("DKIM: SRV _domainkey failed on: {tag} trying domain directly", tag=self.dkim_tags["d"])
            host = self.dkim_tags["d"]
            port = ""
            scheme = "https"
        else:
            host, port = result
            scheme = "http" if port in (80, 8008, 8080,) else "https"
            if port == 80 and scheme == "http" or port == 443 and scheme == "https":
                port = ""
            else:
                port = ":%s" % (port,)

        returnValue("%s://%s%s/.well-known/domainkey/%s/%s" % (scheme, host, port, self.dkim_tags["d"], self.dkim_tags["s"],))

    @inlineCallbacks
    def _lookupKeys(self):
        """
        Do the key lookup using the actual lookup method.
        """

        # First we do an SRV lookup for _domainkey to get the public key server URI
        uri = (yield self._getURI())

        log.debug("DKIM: HTTP/.well-known lookup: {uri}", uri=uri)
        response = (yield getURL(uri))
        if response is None or response.code / 100 != 2:
            log.debug("DKIM: Failed http/well-known lookup: {uri} {resp}", uri=uri, resp=response)
            returnValue(())

        ct = response.headers.getRawHeaders("content-type", ("bogus/type",))[0]
        ct = ct.split(";", 1)
        ct = ct[0].strip()
        if ct not in ("text/plain",):
            log.debug("DKIM: Failed http/well-known lookup: wrong content-type returned {uri} {ct}", uri=uri, ct=ct)
            returnValue(())

        log.debug("DKIM: HTTP/.well-known lookup results: {uri}\n{resp}", uri=uri, resp=response.data)
        returnValue(tuple([DKIMUtils.extractTags(line) for line in response.data.splitlines()]))


class PublicKeyLookup_PrivateExchange(PublicKeyLookup):

    method = Q_PRIVATE
    directory = None

    def _getSelectorKey(self):
        """
        Get a token used to uniquely identify the key being looked up. Token format will
        depend on the lookup method.
        """
        return "%s#%s" % (self.dkim_tags["d"], self.dkim_tags["s"],)

    def _lookupKeys(self):
        """
        Key information is stored in a file, one record per line.
        """

        # Check validity of paths
        if PublicKeyLookup_PrivateExchange.directory is None:
            log.debug("DKIM: Failed private-exchange lookup: no directory configured")
            return succeed(())
        keyfile = os.path.join(PublicKeyLookup_PrivateExchange.directory, self._getSelectorKey())
        if not os.path.exists(keyfile):
            log.debug("DKIM: Failed private-exchange lookup: no path {path}", path=keyfile)
            return succeed(())

        # Now read the data
        log.debug("DKIM: Private exchange lookup: {path}", path=keyfile)
        try:
            with open(keyfile) as f:
                keys = f.read()
        except IOError as e:
            log.debug("DKIM: Failed private-exchange lookup: could not read {path} {ex}", path=keyfile, ex=e)
            return succeed(())

        log.debug("DKIM: Private exchange lookup results: {path}\n{keys}", path=keyfile, keys=keys)
        return succeed(tuple([DKIMUtils.extractTags(line) for line in keys.splitlines()]))


class DomainKeyResource (SimpleResource):
    """
    Domainkey well-known resource.
    """

    def __init__(self, domain, selector, pubkeyfile):
        """
        """
        assert domain
        assert selector

        SimpleResource.__init__(self, principalCollections=None, isdir=True, defaultACL=SimpleResource.allReadACL)
        self.makeKeyData(domain, selector, pubkeyfile)
        self.domain = domain
        self.selector = selector

    def makeKeyData(self, domain, selector, pubkeyfile):
        """
        Check that a valid key exists, create the TXT record format data and make the needed child resources.
        """

        # Get data from file
        try:
            with open(pubkeyfile) as f:
                key_data = f.read()
        except IOError as e:
            log.error("DKIM: Unable to open the public key file: {path} because of {ex}", path=pubkeyfile, ex=e)
            raise

        # Make sure we can parse a valid public key
        try:
            RSA.importKey(key_data)
        except:
            log.error("DKIM: Invalid public key file: {path}", path=pubkeyfile)
            raise

        # Make the TXT record
        key_data = "".join(key_data.strip().splitlines()[1:-1])
        txt_data = "v=DKIM1; s=ischedule; p=%s\n" % (key_data,)

        # Setup resource hierarchy
        domainResource = SimpleResource(principalCollections=None, isdir=True, defaultACL=SimpleResource.allReadACL)
        self.putChild(domain, domainResource)

        selectorResource = SimpleDataResource(principalCollections=None, content_type=MimeType.fromString("text/plain"), data=txt_data, defaultACL=SimpleResource.allReadACL)
        domainResource.putChild(selector, selectorResource)

    def contentType(self):
        return MimeType.fromString("text/html; charset=utf-8")

    def render(self, request):
        output = """<html>
<head>
<title>DomainKey Resource</title>
</head>
<body>
<h1>DomainKey Resource.</h1>
<a href="%s">Domain: %s<br>
Selector: %s</a>
</body
</html>""" % (joinURL(request.uri, self.domain, self.selector), self.domain, self.selector,)

        response = Response(200, {}, output)
        response.headers.setHeader("content-type", MimeType("text", "html"))
        return response
