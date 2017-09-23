#!/usr/bin/env python
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

from __future__ import print_function
import os
import sys
from Crypto.PublicKey import RSA
from StringIO import StringIO

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.logger import LogLevel, STDLibLogObserver
from twisted.python.usage import Options

from twext.python.log import Logger
from txweb2.http_headers import Headers

from txdav.caldav.datastore.scheduling.ischedule.dkim import RSA256, DKIMRequest, \
    PublicKeyLookup, DKIMVerifier, DKIMVerificationError

log = Logger()


def _doKeyGeneration(options):

    key = RSA.generate(options["key-size"])
    output = key.exportKey()
    lineBreak = False
    if options["key"]:
        with open(options["key"], "w") as f:
            f.write(output)
    else:
        print(output)
        lineBreak = True

    output = key.publickey().exportKey()
    if options["pub-key"]:
        with open(options["pub-key"], "w") as f:
            f.write(output)
    else:
        if lineBreak:
            print()
        print(output)
        lineBreak = True

    if options["txt"]:
        output = "".join(output.splitlines()[1:-1])
        txt = "v=DKIM1; p=%s" % (output,)
        if lineBreak:
            print()
        print(txt)


@inlineCallbacks
def _doRequest(options):

    if options["verbose"]:
        log.levels().setLogLevelForNamespace("txdav.caldav.datastore.scheduling.ischedule.dkim", LogLevel.debug)

    # Parse the HTTP file
    with open(options["request"]) as f:
        request = f.read()
    method, uri, headers, stream = _parseRequest(request)

    # Setup signing headers
    sign_headers = options["signing"]
    if sign_headers is None:
        sign_headers = []
        for hdr in ("Host", "Content-Type", "Originator", "Recipient+"):
            if headers.hasHeader(hdr.rstrip("+")):
                sign_headers.append(hdr)
    else:
        sign_headers = sign_headers.split(":")

    dkim = DKIMRequest(
        method,
        uri,
        headers,
        stream,
        options["domain"],
        options["selector"],
        options["key"],
        options["algorithm"],
        sign_headers,
        True,
        True,
        False,
        int(options["expire"]),
    )
    if options["fake-time"]:
        dkim.time = "100"
        dkim.expire = "200"
        dkim.message_id = "1"
    yield dkim.sign()

    s = StringIO()
    _writeRequest(dkim, s)
    print(s.getvalue())


@inlineCallbacks
def _doVerify(options):
    # Parse the HTTP file
    with open(os.path.expanduser(options["verify"])) as f:
        verify = f.read()
    _method, _uri, headers, body = _parseRequest(verify)

    # Check for local public key
    if options["pub-key"]:
        PublicKeyLookup_File.pubkeyfile = os.path.expanduser(options["pub-key"])
        lookup = (PublicKeyLookup_File,)
    else:
        lookup = None

    dkim = DKIMVerifier(headers, body, lookup)
    if options["fake-time"]:
        dkim.time = 0

    try:
        yield dkim.verify()
    except DKIMVerificationError as e:
        print("Verification Failed: %s" % (e,))
    else:
        print("Verification Succeeded")


def _parseRequest(request):

    lines = request.splitlines(True)

    method, uri, _ignore_version = lines.pop(0).split()

    hdrs = []
    body = None
    for line in lines:
        if body is not None:
            body.append(line)
        elif line.strip() == "":
            body = []
        elif line[0] in (" ", "\t"):
            hdrs[-1] += line
        else:
            hdrs.append(line)

    headers = Headers()
    for hdr in hdrs:
        name, value = hdr.split(':', 1)
        headers.addRawHeader(name, value.strip())

    stream = "".join(body)

    return method, uri, headers, stream


def _writeRequest(request, f):

    f.write("%s %s HTTP/1.1\r\n" % (request.method, request.uri,))
    for name, valuelist in request.headers.getAllRawHeaders():
        for value in valuelist:
            f.write("%s: %s\r\n" % (name, value))
    f.write("\r\n")
    f.write(request.stream.read())


class PublicKeyLookup_File(PublicKeyLookup):

    method = "*"
    pubkeyfile = None

    def getPublicKey(self):
        """
        Do the key lookup using the actual lookup method.
        """
        with open(self.pubkeyfile) as f:
            data = f.read()
        return RSA.importKey(data)


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        DKIMToolOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = """Usage: dkimtool [options]
Options:
    -h            Print this help and exit

    # Key Generation
    --key-gen          Generate private/public key files
    --key FILE         Private key file to create [stdout]
    --pub-key FILE     Public key file to create [stdout]
    --key-size SIZE    Key size [1024]
    --txt              Also generate the public key TXT record
    --fake-time        Use fake t=, x= values when signing and also
                       ignore expiration on verification

    # Request
    --request FILE      An HTTP request to sign
    --algorithm ALGO    Signature algorithm [rsa-sha256]
    --domain DOMAIN     Signature domain [example.com]
    --selector SELECTOR Signature selector [dkim]
    --key FILE          Private key to use
    --signing HEADERS   List of headers to sign [automatic]
    --expire SECONDS    When to expire signature [no expiry]

    # Verify
    --verify FILE       An HTTP request to verify
    --pkey   FILE       Public key to use in place of
                        q= lookup

Description:
    This utility is for testing DKIM signed HTTP requests. Key operations are:

    --key-gen: generate a private/public RSA key.

    --request: sign an HTTP request.

    --verify:  verify a signed HTTP request.

"""


class DKIMToolOptions(Options):
    """
    Command-line options for 'calendarserver_dkimtool'
    """

    synopsis = description

    optFlags = [
        ['verbose', 'v', "Verbose logging."],
        ['key-gen', 'g', "Generate private/public key files"],
        ['txt', 't', "Also generate the public key TXT record"],
        ['fake-time', 'f', "Fake time values for signing/verification"],
    ]

    optParameters = [
        ['key', 'k', None, "Private key file to create [default: stdout]"],
        ['pub-key', 'p', None, 'Public key file to create [default: stdout]'],
        ['key-size', 'x', 1024, 'Key size'],
        ['request', 'r', None, 'An HTTP request to sign'],
        ['algorithm', 'a', RSA256, 'Signature algorithm'],
        ['domain', 'd', 'example.com', 'Signature domain'],
        ['selector', 's', 'dkim', 'Signature selector'],
        ['signing', 'h', None, 'List of headers to sign [automatic]'],
        ['expire', 'e', 3600, 'When to expire signature'],
        ['verify', 'w', None, 'An HTTP request to verify'],
    ]

    def __init__(self):
        super(DKIMToolOptions, self).__init__()
        self.outputName = '-'


@inlineCallbacks
def _runInReactor(fn, options):

    try:
        yield fn(options)
    except Exception as e:
        print(e)
    finally:
        reactor.stop()


def main(argv=sys.argv, stderr=sys.stderr):
    options = DKIMToolOptions()
    options.parseOptions(argv[1:])

    #
    # Send logging output to stdout
    #
    observer = STDLibLogObserver()
    observer.start()

    if options["verbose"]:
        log.levels().setLogLevelForNamespace("txdav.caldav.datastore.scheduling.ischedule.dkim", LogLevel.debug)

    if options["key-gen"]:
        _doKeyGeneration(options)
    elif options["request"]:
        reactor.callLater(0, _runInReactor, _doRequest, options)
        reactor.run()
    elif options["verify"]:
        reactor.callLater(0, _runInReactor, _doVerify, options)
        reactor.run()
    else:
        usage("Invalid options")

if __name__ == '__main__':
    main()
