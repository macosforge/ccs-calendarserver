##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##
from __future__ import print_function

from calendarserver.tools.util import loadConfig
from datetime import datetime
from getopt import getopt, GetoptError
from getpass import getpass
from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import client
from twisted.words.protocols.jabber import xmlstream
from twisted.words.protocols.jabber.client import XMPPAuthenticator, IQAuthInitializer
from twisted.words.protocols.jabber.jid import JID
from twisted.words.protocols.jabber.xmlstream import IQ
from twisted.words.xish import domish

from twext.internet.gaiendpoint import GAIEndpoint
from twext.internet.adaptendpoint import connect
from twext.internet.ssl import simpleClientContextFactory

from twistedcaldav.config import config, ConfigurationError
from twistedcaldav.util import AuthorizedHTTPGetter
from xml.etree import ElementTree
import os
import signal
import sys
import uuid


def usage(e=None):
    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] username" % (name,))
    print("")
    print(" Monitor push notification events from calendar server")
    print("")
    print("options:")
    print("  -a --admin <username>: Specify an administrator username")
    print("  -f --config <path>: Specify caldavd.plist configuration path")
    print("  -h --help: print this help and exit")
    print("  -H --host <hostname>: calendar server host name")
    print("  -n --node <pubsub node>: pubsub node to subscribe to *")
    print("  -p --port <port number>: calendar server port number")
    print("  -s --ssl: use https (default is http)")
    print("  -v --verbose: print additional information including XMPP traffic")
    print("")
    print(" * The --node option is only required for calendar servers that")
    print("   don't advertise the push-transports DAV property (such as a Snow")
    print("   Leopard server).  In this case, --host should specify the name")
    print("   of the XMPP server and --port should specify the port XMPP is")
    print("   is listening on.")
    print("")

    if e:
        sys.stderr.write("%s\n" % (e,))
        sys.exit(64)
    else:
        sys.exit(0)


def main():
    try:
        (optargs, args) = getopt(
            sys.argv[1:], "a:f:hH:n:p:sv", [
                "admin=",
                "config=",
                "help",
                "host=",
                "node=",
                "port=",
                "ssl",
                "verbose",
            ],
        )
    except GetoptError as e:
        usage(e)

    admin = None
    configFileName = None
    host = None
    nodes = None
    port = None
    useSSL = False
    verbose = False

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()
        elif opt in ("-f", "--config"):
            configFileName = arg
        elif opt in ("-a", "--admin"):
            admin = arg
        elif opt in ("-H", "--host"):
            host = arg
        elif opt in ("-n", "--node"):
            nodes = [arg]
        elif opt in ("-p", "--port"):
            port = int(arg)
        elif opt in ("-s", "--ssl"):
            useSSL = True
        elif opt in ("-v", "--verbose"):
            verbose = True
        else:
            raise NotImplementedError(opt)

    if len(args) != 1:
        usage("Username not specified")

    username = args[0]

    if host is None:
        # No host specified, so try loading config to look up settings
        try:
            loadConfig(configFileName)
        except ConfigurationError as e:
            print("Error in configuration: %s" % (e,))
            sys.exit(1)

        useSSL = config.EnableSSL or config.BehindTLSProxy
        host = config.ServerHostName
        port = config.SSLPort if useSSL else config.HTTPPort

    if port is None:
        usage("Must specify a port number")

    if admin:
        password = getpass("Password for administrator %s: " % (admin,))
    else:
        password = getpass("Password for %s: " % (username,))
        admin = username

    monitorService = PushMonitorService(
        useSSL, host, port, nodes, admin,
        username, password, verbose)
    reactor.addSystemEventTrigger(
        "during", "startup",
        monitorService.startService)
    reactor.addSystemEventTrigger(
        "before", "shutdown",
        monitorService.stopService)

    reactor.run()


class PubSubClientFactory(xmlstream.XmlStreamFactory):
    """
    An XMPP pubsub client that subscribes to nodes and prints a message
    whenever a notification arrives.
    """

    pubsubNS = 'http://jabber.org/protocol/pubsub'

    def __init__(self, jid, password, service, nodes, verbose, sigint=True):
        resource = "pushmonitor.%s" % (uuid.uuid4().hex,)
        self.jid = "%s/%s" % (jid, resource)
        self.service = service
        self.nodes = nodes
        self.verbose = verbose

        if self.verbose:
            print("JID:", self.jid, "Pubsub service:", self.service)

        self.presenceSeconds = 60
        self.presenceCall = None
        self.xmlStream = None
        self.doKeepAlive = True

        xmlstream.XmlStreamFactory.__init__(
            self,
            XMPPAuthenticator(JID(self.jid), password))

        self.addBootstrap(xmlstream.STREAM_CONNECTED_EVENT, self.connected)
        self.addBootstrap(xmlstream.STREAM_END_EVENT, self.disconnected)
        self.addBootstrap(xmlstream.INIT_FAILED_EVENT, self.initFailed)

        self.addBootstrap(xmlstream.STREAM_AUTHD_EVENT, self.authenticated)
        self.addBootstrap(
            IQAuthInitializer.INVALID_USER_EVENT,
            self.authFailed)
        self.addBootstrap(
            IQAuthInitializer.AUTH_FAILED_EVENT,
            self.authFailed)

        if sigint:
            signal.signal(signal.SIGINT, self.sigint_handler)

    @inlineCallbacks
    def sigint_handler(self, num, frame):
        print(" Shutting down...")
        yield self.unsubscribeAll()
        reactor.stop()

    @inlineCallbacks
    def unsubscribeAll(self):
        if self.xmlStream is not None:
            for node, (_ignore_url, name, kind) in self.nodes.iteritems():
                yield self.unsubscribe(node, name, kind)

    def connected(self, xmlStream):
        self.xmlStream = xmlStream
        if self.verbose:
            print("XMPP connection successful")
            xmlStream.rawDataInFn = self.rawDataIn
            xmlStream.rawDataOutFn = self.rawDataOut
        xmlStream.addObserver("/message/event/items",
                              self.handleMessageEventItems)

    def disconnected(self, xmlStream):
        self.xmlStream = None
        if self.presenceCall is not None:
            self.presenceCall.cancel()
            self.presenceCall = None
        if self.verbose:
            print("XMPP disconnected")

    def initFailed(self, failure):
        self.xmlStream = None
        print("XMPP connection failure: %s" % (failure,))
        reactor.stop()

    @inlineCallbacks
    def authenticated(self, xmlStream):
        if self.verbose:
            print("XMPP authentication successful")
        self.sendPresence()
        for node, (_ignore_url, name, kind) in self.nodes.iteritems():
            yield self.subscribe(node, name, kind)

        print("Awaiting notifications (hit Control-C to end)")

    def authFailed(self, e):
        print("XMPP authentication failed")
        reactor.stop()

    def sendPresence(self):
        if self.doKeepAlive and self.xmlStream is not None:
            presence = domish.Element(('jabber:client', 'presence'))
            self.xmlStream.send(presence)
            self.presenceCall = reactor.callLater(
                self.presenceSeconds,
                self.sendPresence)

    def handleMessageEventItems(self, iq):
        item = iq.firstChildElement().firstChildElement()
        if item:
            node = item.getAttribute("node")
            if node:
                node = str(node)
                url, name, kind = self.nodes.get(node, ("Not subscribed", "Unknown", "Unknown"))
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print('%s | Notification for "%s" (%s)' % (timestamp, name, kind))
                if self.verbose:
                    print(" node = %s" % (node,))
                    print(" url = %s" % (url,))

    @inlineCallbacks
    def subscribe(self, node, name, kind):
        iq = IQ(self.xmlStream)
        pubsubElement = iq.addElement("pubsub", defaultUri=self.pubsubNS)
        subElement = pubsubElement.addElement("subscribe")
        subElement["node"] = node
        subElement["jid"] = self.jid
        print('Subscribing to "%s" (%s)' % (name, kind))
        if self.verbose:
            print(node)
        try:
            yield iq.send(to=self.service)
            print("OK")
        except Exception as e:
            print("Subscription failure: %s %s" % (node, e))

    @inlineCallbacks
    def unsubscribe(self, node, name, kind):
        iq = IQ(self.xmlStream)
        pubsubElement = iq.addElement("pubsub", defaultUri=self.pubsubNS)
        subElement = pubsubElement.addElement("unsubscribe")
        subElement["node"] = node
        subElement["jid"] = self.jid
        print('Unsubscribing from "%s" (%s)' % (name, kind))
        if self.verbose:
            print(node)
        try:
            yield iq.send(to=self.service)
            print("OK")
        except Exception as e:
            print("Unsubscription failure: %s %s" % (node, e))

    @inlineCallbacks
    def retrieveSubscriptions(self):
        # This isn't supported by Apple's pubsub service
        iq = IQ(self.xmlStream)
        pubsubElement = iq.addElement("pubsub", defaultUri=self.pubsubNS)
        pubsubElement.addElement("subscriptions")
        print("Requesting list of subscriptions")
        try:
            yield iq.send(to=self.service)
        except Exception as e:
            print("Subscription list failure: %s" % (e,))

    def rawDataIn(self, buf):
        print("RECV: %s" % unicode(buf, 'utf-8').encode('ascii', 'replace'))

    def rawDataOut(self, buf):
        print("SEND: %s" % unicode(buf, 'utf-8').encode('ascii', 'replace'))


class PropfindRequestor(AuthorizedHTTPGetter):
    handleStatus_207 = lambda self: self.handleStatus_200


class PushMonitorService(Service):
    """
    A service which uses CalDAV to determine which pubsub node(s) correspond
    to any calendar the user has access to (including any belonging to users
    who have delegated access to the user).  Those nodes are subscribed to
    using XMPP and monitored for updates.
    """

    def __init__(
        self, useSSL, host, port, nodes, authname, username, password,
        verbose
    ):
        self.useSSL = useSSL
        self.host = host
        self.port = port
        self.nodes = nodes
        self.authname = authname
        self.username = username
        self.password = password
        self.verbose = verbose

    @inlineCallbacks
    def startService(self):
        try:
            subscribeNodes = {}
            if self.nodes is None:
                paths = set()
                principal = "/principals/users/%s/" % (self.username,)
                name, homes = (yield self.getPrincipalDetails(principal))
                if self.verbose:
                    print(name, homes)
                for home in homes:
                    paths.add(home)
                for principal in (yield self.getProxyFor()):
                    name, homes = (yield self.getPrincipalDetails(
                        principal,
                        includeCardDAV=False))
                    if self.verbose:
                        print(name, homes)
                    for home in homes:
                        if home.startswith("/"):
                            # Only support homes on the same server for now.
                            paths.add(home)
                for path in paths:
                    host, port, nodes = (yield self.getPushInfo(path))
                    subscribeNodes.update(nodes)
            else:
                for node in self.nodes:
                    subscribeNodes[node] = ("Unknown", "Unknown", "Unknown")
                host = self.host
                port = self.port

            # TODO: support talking to multiple hosts (to support delegates
            # from other calendar servers)
            if subscribeNodes:
                self.startMonitoring(host, port, subscribeNodes)
            else:
                print("No nodes to monitor")
                reactor.stop()

        except Exception as e:
            print("Error:", e)
            reactor.stop()

    @inlineCallbacks
    def getPrincipalDetails(self, path, includeCardDAV=True):
        """
        Given a principal path, retrieve and return the corresponding
        displayname and set of calendar/addressbook homes.
        """

        name = ""
        homes = []

        headers = {
            "Depth": "0",
        }
        body = """<?xml version="1.0" encoding="UTF-8"?>
            <A:propfind xmlns:A="DAV:"
                        xmlns:CARD="urn:ietf:params:xml:ns:carddav"
                        xmlns:CAL="urn:ietf:params:xml:ns:caldav">
                <A:prop>
                    <CAL:calendar-home-set/>
                    <CARD:addressbook-home-set/>
                    <A:displayname/>
                </A:prop>
            </A:propfind>
        """

        try:
            responseBody = (yield self.makeRequest(
                path, "PROPFIND", headers,
                body))
            try:
                doc = ElementTree.fromstring(responseBody)
                for response in doc.findall("{DAV:}response"):
                    href = response.find("{DAV:}href")
                    for propstat in response.findall("{DAV:}propstat"):
                        status = propstat.find("{DAV:}status")
                        if "200 OK" in status.text:
                            for prop in propstat.findall("{DAV:}prop"):
                                calendarHomeSet = prop.find("{urn:ietf:params:xml:ns:caldav}calendar-home-set")
                                if calendarHomeSet is not None:
                                    for href in calendarHomeSet.findall("{DAV:}href"):
                                        href = href.text
                                        if href:
                                            homes.append(href)
                                if includeCardDAV:
                                    addressbookHomeSet = prop.find("{urn:ietf:params:xml:ns:carddav}addressbook-home-set")
                                    if addressbookHomeSet is not None:
                                        for href in addressbookHomeSet.findall("{DAV:}href"):
                                            href = href.text
                                            if href:
                                                homes.append(href)
                                displayName = prop.find("{DAV:}displayname")
                                if displayName is not None:
                                    displayName = displayName.text
                                    if displayName:
                                        name = displayName

            except Exception as e:
                print("Unable to parse principal details", e)
                print(responseBody)
                raise

        except Exception as e:
            print("Unable to look up principal details", e)
            raise

        returnValue((name, homes))

    @inlineCallbacks
    def getProxyFor(self):
        """
        Retrieve and return the principal paths for any user that has delegated
        calendar access to this user.
        """

        proxies = set()

        headers = {
            "Depth": "0",
        }
        body = """<?xml version="1.0" encoding="UTF-8"?>
            <A:propfind xmlns:A="DAV:">
                <A:prop>
                    <C:calendar-proxy-read-for xmlns:C="http://calendarserver.org/ns/"/>
                    <C:calendar-proxy-write-for xmlns:C="http://calendarserver.org/ns/"/>
                </A:prop>
            </A:propfind>
        """
        path = "/principals/users/%s/" % (self.username,)

        try:
            responseBody = (yield self.makeRequest(
                path, "PROPFIND", headers,
                body))
            try:
                doc = ElementTree.fromstring(responseBody)
                for response in doc.findall("{DAV:}response"):
                    href = response.find("{DAV:}href")
                    for propstat in response.findall("{DAV:}propstat"):
                        status = propstat.find("{DAV:}status")
                        if "200 OK" in status.text:
                            for prop in propstat.findall("{DAV:}prop"):
                                for element in (
                                    "{http://calendarserver.org/ns/}calendar-proxy-read-for",
                                    "{http://calendarserver.org/ns/}calendar-proxy-write-for",
                                ):
                                    proxyFor = prop.find(element)
                                    if proxyFor is not None:
                                        for href in proxyFor.findall("{DAV:}href"):
                                            href = href.text
                                            if href:
                                                proxies.add(href)

            except Exception as e:
                print("Unable to parse proxy information", e)
                print(responseBody)
                raise

        except Exception as e:
            print("Unable to look up who %s is a proxy for" % (self.username,))
            raise

        returnValue(proxies)

    @inlineCallbacks
    def getPushInfo(self, path):
        """
        Given a calendar home path, retrieve push notification info including
        xmpp hostname and port, the pushkey for the home and for each shared
        collection bound into the home.
        """

        headers = {
            "Depth": "1",
        }
        body = """<?xml version="1.0" encoding="UTF-8"?>
            <A:propfind xmlns:A="DAV:">
                <A:prop>
                    <A:displayname/>
                    <A:resourcetype/>
                    <C:pushkey xmlns:C="http://calendarserver.org/ns/"/>
                    <C:push-transports xmlns:C="http://calendarserver.org/ns/"/>
                </A:prop>
            </A:propfind>
        """

        try:
            responseBody = (yield self.makeRequest(
                path, "PROPFIND", headers,
                body))
            host = None
            port = None
            nodes = {}
            try:
                doc = ElementTree.fromstring(responseBody)
                for response in doc.findall("{DAV:}response"):
                    href = response.find("{DAV:}href")
                    key = None
                    name = None
                    if path.startswith("/calendars"):
                        kind = "Calendar home"
                    else:
                        kind = "AddressBook home"
                    for propstat in response.findall("{DAV:}propstat"):
                        status = propstat.find("{DAV:}status")
                        if "200 OK" in status.text:
                            for prop in propstat.findall("{DAV:}prop"):
                                displayName = prop.find("{DAV:}displayname")
                                if displayName is not None:
                                    displayName = displayName.text
                                    if displayName:
                                        name = displayName
                                resourceType = prop.find("{DAV:}resourcetype")
                                if resourceType is not None:
                                    shared = resourceType.find("{http://calendarserver.org/ns/}shared")
                                    if shared is not None:
                                        kind = "Shared calendar"
                                pushKey = prop.find("{http://calendarserver.org/ns/}pushkey")
                                if pushKey is not None:
                                    pushKey = pushKey.text
                                    if pushKey:
                                        key = pushKey

                                pushTransports = prop.find("{http://calendarserver.org/ns/}push-transports")
                                if pushTransports is not None:
                                    if self.verbose:
                                        print("push-transports:\n\n", ElementTree.tostring(pushTransports))
                                    for transport in pushTransports.findall("{http://calendarserver.org/ns/}transport"):
                                        if transport.attrib["type"] == "XMPP":
                                            xmppServer = transport.find("{http://calendarserver.org/ns/}xmpp-server")
                                            if xmppServer is not None:
                                                xmppServer = xmppServer.text
                                                if xmppServer:
                                                    if ":" in xmppServer:
                                                        host, port = xmppServer.split(":")
                                                        port = int(port)
                                                    else:
                                                        host = xmppServer
                                                        port = 5222

                    if key and key not in nodes:
                        nodes[key] = (href.text, name, kind)

            except Exception as e:
                print("Unable to parse push information", e)
                print(responseBody)
                raise

        except Exception as e:
            print("Unable to look up push information for %s" % (self.username,))
            raise

        if host is None:
            raise Exception("Unable to determine xmpp server name")
        if port is None:
            raise Exception("Unable to determine xmpp server port")

        returnValue((host, port, nodes))

    def startMonitoring(self, host, port, nodes):
        service = "pubsub.%s" % (host,)
        jid = "%s@%s" % (self.authname, host)

        pubsubFactory = PubSubClientFactory(
            jid, self.password, service, nodes,
            self.verbose)
        connect(GAIEndpoint(reactor, host, port), pubsubFactory)

    def makeRequest(self, path, method, headers, body):
        scheme = "https:" if self.useSSL else "http:"
        url = "%s//%s:%d%s" % (scheme, self.host, self.port, path)
        caldavFactory = client.HTTPClientFactory(
            url, method=method,
            headers=headers, postdata=body, agent="Push Monitor")
        caldavFactory.username = self.authname
        caldavFactory.password = self.password
        caldavFactory.noisy = False
        caldavFactory.protocol = PropfindRequestor
        if self.useSSL:
            connect(GAIEndpoint(reactor, self.host, self.port, simpleClientContextFactory(self.host)),
                    caldavFactory)
        else:
            connect(GAIEndpoint(reactor, self.host, self.port), caldavFactory)

        return caldavFactory.deferred


if __name__ == "__main__":
    main()
