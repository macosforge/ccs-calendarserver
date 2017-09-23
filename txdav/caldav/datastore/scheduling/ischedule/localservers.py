##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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
from twisted.internet.abstract import isIPAddress, isIPv6Address
from twistedcaldav.client.pool import installPool
from twistedcaldav.config import config, fullServerPath
from twistedcaldav.xmlutil import readXML

from txdav.caldav.datastore.scheduling.ischedule.utils import getIPsFromHost
import socket
import urlparse

"""
XML based server configuration file handling.

This is used in an environment where more than one server is being used within a single domain. i.e., all
the principals across the whole domain need to be able to directly schedule each other and know of each others
existence. A common scenario would be a production server and a development/test server.

Each server is identified by an id and url. The id is used when assigning principals to a specific server.

These servers support the concept of "podding".

A "podded" service is one where different groups of users are hosted on different servers, which may be of
different versions etc.
"""

__all__ = [
    "buildServersDB",
]

log = Logger()

SERVER_SECRET_HEADER = "X-CALENDARSERVER-ISCHEDULE"


class ServersDB(object):
    """
    Represents the set of servers within the same domain.
    """

    def __init__(self):

        self._servers = {}
        self._xmlFile = None
        self._thisServer = None

    def load(self, xmlFile=None, ignoreIPLookupFailures=False):
        if self._xmlFile is None or xmlFile is not None:
            self._servers = {}
            if xmlFile:
                self._xmlFile = xmlFile
            else:
                self._xmlFile = fullServerPath(
                    config.ConfigRoot,
                    config.Servers.ConfigFile
                )
        self._servers = ServersParser.parse(self._xmlFile, ignoreIPLookupFailures=ignoreIPLookupFailures)
        for server in self._servers.values():
            if server.thisServer:
                self._thisServer = server
                break
        else:
            raise ValueError("No server in {} matches this server.".format(self._xmlFile,))

    def clear(self):
        self._servers = {}
        self._xmlFile = None
        self._thisServer = None

    def addServer(self, server):
        self._servers[server.id] = server
        if server.thisServer:
            self._thisServer = server

    def getServerById(self, id):
        return self._servers.get(id)

    def getServerURIById(self, id):
        try:
            return self._servers[id].uri
        except KeyError:
            return None

    def getThisServer(self):
        return self._thisServer

    def allServersExceptThis(self, filter_v5=False):
        return filter(lambda x: x != self._thisServer and not (filter_v5 and x.v5), self._servers.values())

    def installReverseProxies(self, maxClients):
        """
        Install a reverse proxy for each of the other servers in the "pod".

        @param maxClients: maximum number of clients in the pool.
        @type maxClients: C{int}
        """

        for server in self._servers.values():
            if server.thisServer:
                continue
            installPool(
                server.id,
                server.uri,
                maxClients,
            )


def buildServersDB(maxClients=5):
    serversDB = ServersDB()
    serversDB.load()
    serversDB.installReverseProxies(maxClients)
    return serversDB


class Server(object):
    """
    Represents a server.
    """

    def __init__(self, id=None, uri=None, sharedSecret=None, thisServer=False):
        self.id = id
        self.uri = uri
        self.thisServer = thisServer
        self.ips = set()
        self.allowed_from_ips = set()
        self.shared_secret = sharedSecret
        self.v5 = False   # Needs old style urn:uuid cu-address

    def details(self):
        if not hasattr(self, "ssl"):
            self._parseDetails()
        return (self.ssl, self.host, self.port, self.path,)

    def check(self, ignoreIPLookupFailures=False):
        # Check whether this matches the current server
        parsed_uri = urlparse.urlparse(self.uri)
        if parsed_uri.hostname == config.ServerHostName:
            if parsed_uri.scheme == "http":
                if config.HTTPPort:
                    self.thisServer = parsed_uri.port in (config.HTTPPort,) + tuple(config.BindHTTPPorts)
            elif parsed_uri.scheme == "https":
                if config.SSLPort:
                    self.thisServer = parsed_uri.port in (config.SSLPort,) + tuple(config.BindSSLPorts)

        # Need to cache IP addresses
        try:
            ips = getIPsFromHost(parsed_uri.hostname)
        except socket.gaierror as e:
            msg = "Unable to lookup ip-addr for server '{}': {}".format(parsed_uri.hostname, str(e))
            log.error(msg)
            if ignoreIPLookupFailures:
                ips = ()
            else:
                raise ValueError(msg)
        self.ips = set(ips)

        actual_ips = set()
        for item in self.allowed_from_ips:
            if not isIPAddress(item) and not isIPv6Address(item):
                try:
                    ips = getIPsFromHost(item)
                except socket.gaierror as e:
                    msg = "Unable to lookup ip-addr for allowed-from '{}': {}".format(item, str(e))
                    log.error(msg)
                    if not ignoreIPLookupFailures:
                        raise ValueError(msg)
                else:
                    actual_ips.update(ips)
            else:
                actual_ips.add(item)
        self.allowed_from_ips = actual_ips

    def checkThisIP(self, ip):
        """
        Check that the passed in IP address corresponds to this server.
        """
        return (ip in self.ips)

    def hasAllowedFromIP(self):
        return len(self.allowed_from_ips) > 0

    def checkAllowedFromIP(self, ip):
        return ip in self.allowed_from_ips

    def checkSharedSecret(self, headers):

        # Get header from the request
        request_secret = headers.getRawHeaders(SERVER_SECRET_HEADER)

        if request_secret is not None and self.shared_secret is None:
            log.error(
                "iSchedule request included unexpected {hdr} header",
                hdr=SERVER_SECRET_HEADER,
            )
            return False
        elif request_secret is None and self.shared_secret is not None:
            log.error(
                "iSchedule request did not include required {hdr} header",
                hdr=SERVER_SECRET_HEADER,
            )
            return False
        elif (request_secret[0] if request_secret else None) != self.shared_secret:
            log.error(
                "iSchedule request {hdr} header did not match",
                hdr=SERVER_SECRET_HEADER,
            )
            return False
        else:
            return True

    def secretHeader(self):
        """
        Return a tuple of header name, header value
        """
        return (SERVER_SECRET_HEADER, self.shared_secret,)

    def _parseDetails(self):
        # Extract scheme, host, port and path
        if self.uri.startswith("http://"):
            self.ssl = False
            rest = self.uri[7:]
        elif self.uri.startswith("https://"):
            self.ssl = True
            rest = self.uri[8:]

        splits = rest.split("/", 1)
        hostport = splits[0].split(":")
        self.host = hostport[0]
        if len(hostport) > 1:
            self.port = int(hostport[1])
        else:
            self.port = {False: 80, True: 443}[self.ssl]
        self.path = "/"
        if len(splits) > 1:
            self.path += splits[1]


ELEMENT_SERVERS = "servers"
ELEMENT_SERVER = "server"
ELEMENT_ID = "id"
ELEMENT_URI = "uri"
ELEMENT_ALLOWED_FROM = "allowed-from"
ELEMENT_SHARED_SECRET = "shared-secret"
ATTR_V5 = "v5"
ATTR_VALUE_YES = "yes"
ATTR_VALUE_NO = "no"


class ServersParser(object):
    """
    Servers configuration file parser.
    """
    @staticmethod
    def parse(xmlFile, ignoreIPLookupFailures=False):

        results = {}

        # Read in XML
        try:
            _ignore_tree, servers_node = readXML(xmlFile, ELEMENT_SERVERS)
        except ValueError as e:
            raise RuntimeError("XML parse error for '{}' because: {}".format(xmlFile, e,))

        for child in servers_node:

            if child.tag != ELEMENT_SERVER:
                raise RuntimeError("Unknown server type: '{}' in servers file: '{}'".format(child.tag, xmlFile,))

            server = Server()
            server.v5 = child.get(ATTR_V5, ATTR_VALUE_NO) == ATTR_VALUE_YES

            for node in child:
                if node.tag == ELEMENT_ID:
                    server.id = node.text
                elif node.tag == ELEMENT_URI:
                    server.uri = node.text
                elif node.tag == ELEMENT_ALLOWED_FROM:
                    server.allowed_from_ips.add(node.text)
                elif node.tag == ELEMENT_SHARED_SECRET:
                    server.shared_secret = node.text
                else:
                    raise RuntimeError("Invalid element '{}' in servers file: '{}'".format(node.tag, xmlFile,))

            if server.id is None or server.uri is None:
                raise RuntimeError("Invalid server '{}' in servers file: '{}'".format(child.tag, xmlFile,))

            server.check(ignoreIPLookupFailures=ignoreIPLookupFailures)
            results[server.id] = server

        return results
