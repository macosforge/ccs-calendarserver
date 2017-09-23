# -*- test-case-name: calendarserver.push.test.test_applepush -*-
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

from twext.internet.ssl import ChainingOpenSSLContextFactory
from twext.python.log import Logger
from twext.enterprise.dal.record import fromTable
from twext.enterprise.jobs.workitem import RegeneratingWorkItem
from txdav.common.datastore.sql_tables import schema

from txweb2 import responsecode
from txdav.xml import element as davxml
from txweb2.dav.noneprops import NonePropertyStore
from txweb2.http import Response
from txweb2.http_headers import MimeType
from txweb2.server import parsePOSTData
from twisted.application import service
from twisted.internet.protocol import Protocol
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.internet.protocol import ClientFactory, ReconnectingClientFactory
from twistedcaldav.extensions import DAVResource, DAVResourceWithoutChildrenMixin
from twistedcaldav.resource import ReadOnlyNoCopyResourceMixIn
import json
import OpenSSL
import struct
import time
from txdav.common.icommondatastore import InvalidSubscriptionValues
from calendarserver.push.ipush import PushPriority
from calendarserver.push.util import validToken, TokenHistory, PushScheduler
from twext.internet.adaptendpoint import connect
from twext.internet.gaiendpoint import GAIEndpoint
from twisted.python.constants import Values, ValueConstant

log = Logger()


class ApplePushPriority(Values):
    """
    Maps calendarserver.push.util.PushPriority values to APNS-specific values
    """
    low = ValueConstant(PushPriority.low.value)
    medium = ValueConstant(PushPriority.medium.value)
    high = ValueConstant(PushPriority.high.value)


class ApplePushNotifierService(service.MultiService):
    """
    ApplePushNotifierService is a MultiService responsible for
    setting up the APN provider and feedback connections.  Once
    connected, calling its enqueue( ) method sends notifications
    to any device token which is subscribed to the enqueued key.

    The Apple Push Notification protocol is described here:

    https://developer.apple.com/library/ios/documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/Chapters/CommunicatingWIthAPS.html
    """
    log = Logger()

    @classmethod
    def makeService(
        cls, settings, store, testConnectorClass=None,
        reactor=None
    ):
        """
        Creates the various "subservices" that work together to implement
        APN, including "provider" and "feedback" services for CalDAV and
        CardDAV.

        @param settings: The portion of the configuration specific to APN
        @type settings: C{dict}

        @param store: The db store for storing/retrieving subscriptions
        @type store: L{IDataStore}

        @param testConnectorClass: Used for unit testing; implements
            connect( ) and receiveData( )
        @type testConnectorClass: C{class}

        @param reactor: Used for unit testing; allows tests to advance the
            clock in order to test the feedback polling service.
        @type reactor: L{twisted.internet.task.Clock}

        @return: instance of L{ApplePushNotifierService}
        """

        service = cls()

        service.store = store
        service.providers = {}
        service.feedbacks = {}
        service.purgeCall = None
        service.purgeIntervalSeconds = settings["SubscriptionPurgeIntervalSeconds"]
        service.purgeSeconds = settings["SubscriptionPurgeSeconds"]

        for protocol in ("CalDAV", "CardDAV"):

            if settings[protocol].Enabled:

                providerTestConnector = None
                feedbackTestConnector = None
                if testConnectorClass is not None:
                    providerTestConnector = testConnectorClass()
                    feedbackTestConnector = testConnectorClass()

                provider = APNProviderService(
                    service.store,
                    settings["ProviderHost"],
                    settings["ProviderPort"],
                    settings[protocol]["CertificatePath"],
                    settings[protocol]["PrivateKeyPath"],
                    chainPath=settings[protocol]["AuthorityChainPath"],
                    passphrase=settings[protocol]["Passphrase"],
                    keychainIdentity=settings[protocol]["KeychainIdentity"],
                    staggerNotifications=settings["EnableStaggering"],
                    staggerSeconds=settings["StaggerSeconds"],
                    testConnector=providerTestConnector,
                    reactor=reactor,
                )
                provider.setServiceParent(service)
                service.providers[protocol] = provider
                service.log.info(
                    "APNS {proto} topic: {topic}",
                    proto=protocol, topic=settings[protocol]["Topic"]
                )

                feedback = APNFeedbackService(
                    service.store,
                    settings["FeedbackUpdateSeconds"],
                    settings["FeedbackHost"],
                    settings["FeedbackPort"],
                    settings[protocol]["CertificatePath"],
                    settings[protocol]["PrivateKeyPath"],
                    chainPath=settings[protocol]["AuthorityChainPath"],
                    passphrase=settings[protocol]["Passphrase"],
                    keychainIdentity=settings[protocol]["KeychainIdentity"],
                    testConnector=feedbackTestConnector,
                    reactor=reactor,
                )
                feedback.setServiceParent(service)
                service.feedbacks[protocol] = feedback

        return service

    def startService(self):
        """
        In addition to starting the provider and feedback sub-services, start a
        LoopingCall whose job it is to purge old subscriptions
        """
        self.log.debug("ApplePushNotifierService startService")
        APNPurgingWork.purgeSeconds = self.purgeSeconds
        APNPurgingWork.purgeIntervalSeconds = self.purgeIntervalSeconds

        service.MultiService.startService(self)

    def stopService(self):
        """
        In addition to stopping the provider and feedback sub-services, stop the
        LoopingCall
        """
        self.log.debug("ApplePushNotifierService stopService")
        service.MultiService.stopService(self)

    @inlineCallbacks
    def enqueue(
        self, transaction, pushKey, dataChangedTimestamp=None,
        priority=PushPriority.high
    ):
        """
        Sends an Apple Push Notification to any device token subscribed to
        this pushKey.

        @param pushKey: The identifier of the resource that was updated, including
            a prefix indicating whether this is CalDAV or CardDAV related.

            "/CalDAV/abc/def/"

        @type pushKey: C{str}
        @param dataChangedTimestamp: Timestamp (epoch seconds) for the data change
            which triggered this notification (Only used for unit tests)
        @type key: C{int}
        @param priority: the priority level
        @type priority: L{PushPriority}
        """

        try:
            protocol = pushKey.split("/")[1]
        except ValueError:
            # pushKey has no protocol, so we can't do anything with it
            self.log.error("Push key '{key}' is missing protocol", key=pushKey)
            return

        # Unit tests can pass this value in; otherwise it defaults to now
        if dataChangedTimestamp is None:
            dataChangedTimestamp = int(time.time())

        provider = self.providers.get(protocol, None)
        if provider is not None:

            # Look up subscriptions for this key
            subscriptions = (yield transaction.apnSubscriptionsByKey(pushKey))

            numSubscriptions = len(subscriptions)
            if numSubscriptions > 0:
                self.log.debug(
                    "Sending {num} APNS notifications for {key}",
                    num=numSubscriptions, key=pushKey
                )
                tokens = [record.token for record in subscriptions if record.token and record.subscriberGUID]
                if tokens:
                    provider.scheduleNotifications(
                        tokens, pushKey,
                        dataChangedTimestamp, priority)


class APNProviderProtocol(Protocol):
    """
    Implements the Provider portion of APNS
    """
    log = Logger()

    # Sent by provider
    COMMAND_PROVIDER = 2

    # Received by provider
    COMMAND_ERROR = 8

    # Returned only for an error.  Successful notifications get no response.
    STATUS_CODES = {
        0: "No errors encountered",
        1: "Processing error",
        2: "Missing device token",
        3: "Missing topic",
        4: "Missing payload",
        5: "Invalid token size",
        6: "Invalid topic size",
        7: "Invalid payload size",
        8: "Invalid token",
        255: "None (unknown)",
    }

    # If error code comes back as one of these, remove the associated device
    # token
    TOKEN_REMOVAL_CODES = (5, 8)

    MESSAGE_LENGTH = 6

    def makeConnection(self, transport):
        self.history = TokenHistory()
        self.log.debug("ProviderProtocol makeConnection")
        Protocol.makeConnection(self, transport)

    def connectionMade(self):
        self.log.debug("ProviderProtocol connectionMade")
        self.buffer = ""
        # Store a reference to ourself on the factory so the service can
        # later call us
        self.factory.connection = self
        self.factory.clientConnectionMade()

    def connectionLost(self, reason=None):
        # self.log.debug("ProviderProtocol connectionLost: {reason}", reason=reason)
        # Clear the reference to us from the factory
        self.factory.connection = None

    @inlineCallbacks
    def dataReceived(self, data, fn=None):
        """
        Buffer and divide up received data into error messages which are
        always 6 bytes long
        """

        if fn is None:
            fn = self.processError

        self.log.debug("ProviderProtocol dataReceived {len} bytes", len=len(data))
        self.buffer += data

        while len(self.buffer) >= self.MESSAGE_LENGTH:
            message = self.buffer[:self.MESSAGE_LENGTH]
            self.buffer = self.buffer[self.MESSAGE_LENGTH:]

            try:
                command, status, identifier = struct.unpack("!BBI", message)
                if command == self.COMMAND_ERROR:
                    yield fn(status, identifier)
            except Exception as e:
                self.log.warn(
                    "ProviderProtocol could not process error: {code} ({ex})",
                    code=message.encode("hex"), ex=e
                )

    @inlineCallbacks
    def processError(self, status, identifier):
        """
        Handles an error message we've received on the provider channel.
        If the error code is one that indicates a bad token, remove all
        subscriptions corresponding to that token.

        @param status: The status value returned from APN Feedback server
        @type status: C{int}

        @param identifier: The identifier of the outbound push notification
            message which had a problem.
        @type status: C{int}
        """
        msg = self.STATUS_CODES.get(status, "Unknown status code")
        self.log.info("Received APN error {status} on identifier {id}: {msg}", status=status, id=identifier, msg=msg)
        if status in self.TOKEN_REMOVAL_CODES:
            token = self.history.extractIdentifier(identifier)
            if token is not None:
                self.log.debug(
                    "Removing subscriptions for bad token: {token}",
                    token=token,
                )
                txn = self.factory.store.newTransaction(label="APNProviderProtocol.processError")
                subscriptions = (yield txn.apnSubscriptionsByToken(token))
                for record in subscriptions:
                    self.log.debug(
                        "Removing subscription: {token} {key}",
                        token=token, key=record.resourceKey
                    )
                    yield txn.removeAPNSubscription(token, record.resourceKey)
                yield txn.commit()

    def sendNotification(self, token, key, dataChangedTimestamp, priority):
        """
        Sends a push notification message for the key to the device associated
        with the token.

        @param token: The device token subscribed to the key
        @type token: C{str}
        @param key: The key we're sending a notification about
        @type key: C{str}
        @param dataChangedTimestamp: Timestamp (epoch seconds) for the data change
            which triggered this notification
        @type key: C{int}
        """

        if not (token and key and dataChangedTimestamp):
            return

        try:
            binaryToken = token.replace(" ", "").decode("hex")
        except:
            self.log.error("Invalid APN token in database: {token}", token=token)
            return
        tokenLength = len(binaryToken)

        identifier = self.history.add(token)
        apnsPriority = ApplePushPriority.lookupByValue(priority.value).value
        payload = json.dumps(
            {
                "key": key,
                "dataChangedTimestamp": dataChangedTimestamp,
                "pushRequestSubmittedTimestamp": int(time.time()),
            }
        )
        payloadLength = len(payload)
        self.log.debug(
            "Sending APNS notification to {token}: id={id} payload={payload} priority={priority}",
            token=token, id=identifier, payload=payload, priority=apnsPriority)

        """
        Notification format

        Top level:  Command (1 byte), Frame length (4 bytes), Frame data (variable)
        Within Frame data:  Item ...
        Item: Item number (1 byte), Item data length (2 bytes), Item data (variable)
        Item 1: Device token (variable length)
        Item 2: Payload (variable length) in JSON format, not null-terminated
        Item 3: Notification ID (4 bytes) an opaque value used for reporting errors
        Item 4: Expiration date (4 bytes) UNIX epoch in secondcs UTC
        Item 5: Priority (1 byte): 10 (push sent immediately) or 5 (push sent
            at a time that conservces power on the device receiving it)
        """

        # Frame struct.pack format                ! Network byte order
        command = self.COMMAND_PROVIDER             # B
        frameLength = (  # I
            # Item 1 (Device token)
            1 +  # Item number                       # B
            2 +  # Item length                       # H
            tokenLength +  # device token            # %d s
            # Item 2 (Payload)
            1 +  # Item number                       # B
            2 +  # Item length                       # H
            payloadLength +  # the JSON payload      # %d s
            # Item 3 (Notification ID)
            1 +  # Item number                       # B
            2 +  # Item length                       # H
            4 +  # Notification ID                   # I
            # Item 4 (Expiration)
            1 +  # Item number                       # B
            2 +  # Item length                       # H
            4 +  # Expiration seconds since epoch    # I
            # Item 5 (Priority)
            1 +  # Item number                       # B
            2 +  # Item length                       # H
            1    # Priority                         # B
        )

        self.transport.write(
            struct.pack(
                "!BIBH%dsBH%dsBHIBHIBHB" % (tokenLength, payloadLength,),

                command,                         # Command
                frameLength,                     # Frame length

                1,                               # Item 1 (Device token)
                tokenLength,                     # Token Length
                binaryToken,                     # Token

                2,                               # Item 2 (Payload)
                payloadLength,                   # Payload length
                payload,                         # Payload

                3,                               # Item 3 (Notification ID)
                4,                               # Notification ID Length
                identifier,                      # Notification ID

                4,                               # Item 4 (Expiration)
                4,                               # Expiration length
                int(time.time()) + 72 * 60 * 60,  # Expires in 72 hours

                5,                               # Item 5 (Priority)
                1,                               # Priority length
                apnsPriority,                    # Priority

            )
        )


class APNProviderFactory(ReconnectingClientFactory):
    log = Logger()

    protocol = APNProviderProtocol

    def __init__(self, service, store):
        self.service = service
        self.store = store
        self.noisy = True
        self.maxDelay = 30  # max seconds between connection attempts
        self.shuttingDown = False

    def clientConnectionMade(self):
        self.log.info("Connection to APN server made")
        self.service.clientConnectionMade()
        self.delay = 1.0

    def clientConnectionLost(self, connector, reason):
        if not self.shuttingDown:
            self.log.error("Connection to APN server lost: {reason}", reason=reason)
            if reason.type == OpenSSL.SSL.Error:
                # If we're failing due to a certificate issue, stop retrying.
                self.log.error("Ensure APNS certificate is not expired")
                ReconnectingClientFactory.stopTrying(self)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        self.log.error("Unable to connect to APN server: {reason}", reason=reason)
        self.connected = False
        ReconnectingClientFactory.clientConnectionFailed(
            self, connector,
            reason)

    def retry(self, connector=None):
        self.log.info("Reconnecting to APN server")
        ReconnectingClientFactory.retry(self, connector)

    def stopTrying(self):
        self.shuttingDown = True
        ReconnectingClientFactory.stopTrying(self)


class APNConnectionService(service.Service):
    log = Logger()

    def __init__(
        self, host, port, certPath, keyPath, chainPath="",
        passphrase="", keychainIdentity="", sslMethod="TLSv1_METHOD", testConnector=None,
        reactor=None
    ):

        self.host = host
        self.port = port
        self.certPath = certPath
        self.keyPath = keyPath
        self.chainPath = chainPath
        self.passphrase = passphrase
        self.keychainIdentity = keychainIdentity
        self.sslMethod = sslMethod
        self.testConnector = testConnector

        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor

    def connect(self, factory):
        if self.testConnector is not None:
            # For testing purposes
            self.testConnector.connect(self, factory)
        else:
            if self.passphrase:
                passwdCallback = lambda *ignored: self.passphrase
            else:
                passwdCallback = None
            context = ChainingOpenSSLContextFactory(
                self.keyPath,
                self.certPath,
                certificateChainFile=self.chainPath,
                passwdCallback=passwdCallback,
                keychainIdentity=self.keychainIdentity,
                sslmethod=getattr(OpenSSL.SSL, self.sslMethod),
                peerName=self.host,
            )
            connect(GAIEndpoint(self.reactor, self.host, self.port, context),
                    factory)


class APNProviderService(APNConnectionService):

    def __init__(
        self, store, host, port, certPath, keyPath, chainPath="",
        passphrase="", keychainIdentity="", sslMethod="TLSv1_METHOD",
        staggerNotifications=False, staggerSeconds=3,
        testConnector=None, reactor=None
    ):

        APNConnectionService.__init__(
            self, host, port, certPath, keyPath,
            chainPath=chainPath, passphrase=passphrase,
            keychainIdentity=keychainIdentity, sslMethod=sslMethod,
            testConnector=testConnector, reactor=reactor)

        self.store = store
        self.factory = None
        self.queue = []
        if staggerNotifications:
            self.scheduler = PushScheduler(
                self.reactor, self.sendNotification,
                staggerSeconds=staggerSeconds)
        else:
            self.scheduler = None

    def startService(self):
        self.log.debug("APNProviderService startService")
        self.factory = APNProviderFactory(self, self.store)
        self.reactor.callWhenRunning(self.connect, self.factory)

    def stopService(self):
        self.log.debug("APNProviderService stopService")
        if self.factory is not None:
            self.factory.stopTrying()
        if self.scheduler is not None:
            self.scheduler.stop()

    def clientConnectionMade(self):
        # Service the queue
        if self.queue:
            # Copy and clear the queue.  Any notifications that don't get
            # sent will be put back into the queue.
            queued = list(self.queue)
            self.queue = []
            for (token, key), dataChangedTimestamp, priority in queued:
                if token and key and dataChangedTimestamp and priority:
                    self.sendNotification(
                        token, key, dataChangedTimestamp,
                        priority)

    def scheduleNotifications(self, tokens, key, dataChangedTimestamp, priority):
        """
        The starting point for getting notifications to the APNS server.  If there is
        a connection to the APNS server, these notifications are scheduled (or directly
        sent if there is no scheduler).  If there is no connection, the notifications
        are saved for later.

        @param tokens: The device tokens to schedule notifications for
        @type tokens: List of strings
        @param key: The key to use for this batch of notifications
        @type key: String
        @param dataChangedTimestamp: Timestamp (epoch seconds) for the data change
            which triggered this notification
        @type key: C{int}
        """
        # Service has reference to factory has reference to protocol instance
        connection = getattr(self.factory, "connection", None)
        if connection is not None:
            if self.scheduler is not None:
                self.scheduler.schedule(tokens, key, dataChangedTimestamp, priority)
            else:
                for token in tokens:
                    self.sendNotification(token, key, dataChangedTimestamp, priority)
        else:
            self._saveForWhenConnected(tokens, key, dataChangedTimestamp, priority)

    def _saveForWhenConnected(self, tokens, key, dataChangedTimestamp, priority):
        """
        Called in order to save notifications that can't be sent now because there
        is no connection to the APNS server.  (token, key) tuples are appended to
        the queue which is serviced during clientConnectionMade()

        @param tokens: The device tokens to schedule notifications for
        @type tokens: List of C{str}
        @param key: The key to use for this batch of notifications
        @type key: C{str}
        @param dataChangedTimestamp: Timestamp (epoch seconds) for the data change
            which triggered this notification
        @type key: C{int}
        """
        for token in tokens:
            tokenKeyPair = (token, key)
            for existingPair, _ignore_timstamp, priority in self.queue:
                if tokenKeyPair == existingPair:
                    self.log.debug("APNProviderService has no connection; skipping duplicate: {token} {key}", token=token, key=key)
                    break  # Already scheduled
            else:
                self.log.debug("APNProviderService has no connection; queuing: {token} {key}", token=token, key=key)
                self.queue.append(((token, key), dataChangedTimestamp, priority))

    def sendNotification(self, token, key, dataChangedTimestamp, priority):
        """
        If there is a connection the notification is sent right away, otherwise
        the notification is saved for later.

        @param token: The device token to send a notifications to
        @type token: C{str}
        @param key: The key to use for this notification
        @type key: C{str}
        @param dataChangedTimestamp: Timestamp (epoch seconds) for the data change
            which triggered this notification
        @type key: C{int}
        """
        if not (token and key and dataChangedTimestamp, priority):
            return

        # Service has reference to factory has reference to protocol instance
        connection = getattr(self.factory, "connection", None)
        if connection is None:
            self._saveForWhenConnected([token], key, dataChangedTimestamp, priority)
        else:
            connection.sendNotification(token, key, dataChangedTimestamp, priority)


class APNFeedbackProtocol(Protocol):
    """
    Implements the Feedback portion of APNS
    """
    log = Logger()

    PREFIX_LENGTH = 6

    def connectionMade(self):
        self.log.debug("FeedbackProtocol connectionMade")
        self.buffer = ""

    @inlineCallbacks
    def dataReceived(self, data, fn=None):
        """
        Buffer and divide up received data into feedback messages.  Once we've
        received enough data and can read a device token, we call processFeedback( )
        on it.
        """

        if fn is None:
            fn = self.processFeedback

        self.log.debug("FeedbackProtocol dataReceived {len} bytes", len=len(data))
        self.buffer += data

        while len(self.buffer) >= self.PREFIX_LENGTH:
            prefix = self.buffer[:self.PREFIX_LENGTH]

            try:
                # Get the length of the token
                timestamp, tokenLength = struct.unpack(
                    "!IH",
                    prefix)

                messageLength = self.PREFIX_LENGTH + tokenLength

                if len(self.buffer) >= messageLength:
                    # Now we can get the token itself
                    data = struct.unpack(
                        "!%ds" % (tokenLength,),
                        self.buffer[self.PREFIX_LENGTH:messageLength])
                    token = data[0].encode("hex").lower()
                    yield fn(timestamp, token)
                    self.buffer = self.buffer[messageLength:]

                else:
                    # We had enough for the prefix, but not enough containing
                    # the token itself
                    return

            except Exception as e:
                self.log.warn(
                    "FeedbackProtocol could not process message: ({ex})", ex=e
                )
                return

    @inlineCallbacks
    def processFeedback(self, timestamp, token):
        """
        Handles a feedback message indicating that the given token is no
        longer active as of the timestamp, and its subscription should be
        removed as long as that device has not re-subscribed since the
        timestamp.

        @param timestamp: Seconds since the epoch
        @type timestamp: C{int}

        @param token: The device token to unsubscribe
        @type token: C{str}
        """

        self.log.debug(
            "FeedbackProtocol processFeedback time={time} token={token}",
            time=timestamp, token=token
        )
        txn = self.factory.store.newTransaction(label="APNFeedbackProtocol.processFeedback")
        subscriptions = (yield txn.apnSubscriptionsByToken(token))

        for record in subscriptions:
            if timestamp > record.modified:
                self.log.debug(
                    "FeedbackProtocol removing subscription: {token} {key}",
                    token=token, key=record.resourceKey,
                )
                yield txn.removeAPNSubscription(token, record.resourceKey)
        yield txn.commit()


class APNFeedbackFactory(ClientFactory):
    log = Logger()

    protocol = APNFeedbackProtocol

    def __init__(self, store):
        self.store = store

    def clientConnectionFailed(self, connector, reason):
        self.log.error(
            "Unable to connect to APN feedback server: {reason}",
            reason=reason,
        )
        self.connected = False
        ClientFactory.clientConnectionFailed(self, connector, reason)


class APNFeedbackService(APNConnectionService):

    def __init__(
        self, store, updateSeconds, host, port,
        certPath, keyPath, chainPath="",
        passphrase="", keychainIdentity="", sslMethod="TLSv1_METHOD",
        testConnector=None, reactor=None
    ):

        APNConnectionService.__init__(
            self, host, port, certPath, keyPath,
            chainPath=chainPath, passphrase=passphrase,
            keychainIdentity=keychainIdentity, sslMethod=sslMethod,
            testConnector=testConnector, reactor=reactor)

        self.store = store
        self.updateSeconds = updateSeconds

    def startService(self):
        self.log.debug("APNFeedbackService startService")
        self.factory = APNFeedbackFactory(self.store)
        self.reactor.callWhenRunning(self.checkForFeedback)

    def stopService(self):
        self.log.debug("APNFeedbackService stopService")
        if self.nextCheck is not None:
            self.nextCheck.cancel()

    def checkForFeedback(self):
        self.nextCheck = None
        self.log.debug("APNFeedbackService checkForFeedback")
        self.connect(self.factory)
        self.nextCheck = self.reactor.callLater(
            self.updateSeconds,
            self.checkForFeedback)


class APNSubscriptionResource(
    ReadOnlyNoCopyResourceMixIn,
    DAVResourceWithoutChildrenMixin, DAVResource
):
    """
    The DAV resource allowing clients to subscribe to Apple push notifications.
    To subscribe, a client should first determine the key they are interested
    in my examining the "pushkey" DAV property on the home or collection they
    want to monitor.  Next the client sends an authenticated HTTP GET or POST
    request to this resource, passing their device token and the key in either
    the URL params or in the POST body.
    """
    log = Logger()

    def __init__(self, parent, store):
        DAVResource.__init__(
            self, principalCollections=parent.principalCollections()
        )
        self.parent = parent
        self.store = store

    def deadProperties(self):
        if not hasattr(self, "_dead_properties"):
            self._dead_properties = NonePropertyStore(self)
        return self._dead_properties

    def etag(self):
        return succeed(None)

    def checkPreconditions(self, request):
        return None

    def defaultAccessControlList(self):
        return succeed(
            davxml.ACL(
                # DAV:Read for authenticated principals
                davxml.ACE(
                    davxml.Principal(davxml.Authenticated()),
                    davxml.Grant(
                        davxml.Privilege(davxml.Read()),
                    ),
                    davxml.Protected(),
                ),
                # DAV:Write for authenticated principals
                davxml.ACE(
                    davxml.Principal(davxml.Authenticated()),
                    davxml.Grant(
                        davxml.Privilege(davxml.Write()),
                    ),
                    davxml.Protected(),
                ),
            )
        )

    def contentType(self):
        return MimeType.fromString("text/html; charset=utf-8")

    def resourceType(self):
        return None

    def isCollection(self):
        return False

    def isCalendarCollection(self):
        return False

    def isPseudoCalendarCollection(self):
        return False

    @inlineCallbacks
    def http_POST(self, request):
        yield self.authorize(request, (davxml.Write(),))
        yield parsePOSTData(request)
        code, msg = (yield self.processSubscription(request))
        returnValue(self.renderResponse(code, body=msg))

    http_GET = http_POST

    @inlineCallbacks
    def processSubscription(self, request):
        """
        Given an authenticated request, use the token and key arguments
        to add a subscription entry to the database.

        @param request: The request to process
        @type request: L{txweb2.server.Request}
        """

        token = request.args.get("token", ("",))[0].replace(" ", "").lower()
        key = request.args.get("key", ("",))[0]

        userAgent = request.headers.getHeader("user-agent", "-")
        host = request.remoteAddr.host
        fwdHeaders = request.headers.getRawHeaders("x-forwarded-for", [])
        if fwdHeaders:
            host = fwdHeaders[0]

        if not (key and token):
            code = responsecode.BAD_REQUEST
            msg = "Invalid request: both 'token' and 'key' must be provided"

        elif not validToken(token):
            code = responsecode.BAD_REQUEST
            msg = "Invalid request: bad 'token' %s" % (token,)

        else:
            uid = request.authnUser.record.uid
            try:
                yield self.addSubscription(token, key, uid, userAgent, host)
                code = responsecode.OK
                msg = None
            except InvalidSubscriptionValues:
                code = responsecode.BAD_REQUEST
                msg = "Invalid subscription values"

        returnValue((code, msg))

    @inlineCallbacks
    def addSubscription(self, token, key, uid, userAgent, host):
        """
        Add a subscription (or update its timestamp if already there).

        @param token: The device token, must be lowercase
        @type token: C{str}

        @param key: The push key
        @type key: C{str}

        @param uid: The uid of the subscriber principal
        @type uid: C{str}

        @param userAgent: The user-agent requesting the subscription
        @type key: C{str}

        @param host: The host requesting the subscription
        @type key: C{str}
        """
        now = int(time.time())  # epoch seconds
        txn = self.store.newTransaction(label="APNSubscriptionResource.addSubscription")
        yield txn.addAPNSubscription(token, key, now, uid, userAgent, host)
        yield txn.commit()

    def renderResponse(self, code, body=None):
        response = Response(code, {}, body)
        response.headers.setHeader("content-type", MimeType("text", "html"))
        return response


class APNPurgingWork(RegeneratingWorkItem, fromTable(schema.APN_PURGING_WORK)):

    group = "apn_purging"
    purgeIntervalSeconds = 12 * 60 * 60  # 12 hours by default
    purgeSeconds = 14 * 24 * 60 * 60  # 14 days by default

    @classmethod
    def initialSchedule(cls, store, seconds):
        def _enqueue(txn):
            return APNPurgingWork.reschedule(txn, seconds)

        return store.inTransaction("APNPurgingWork.initialSchedule", _enqueue)

    def regenerateInterval(self):
        """
        Return the interval in seconds between regenerating instances.
        """
        return self.purgeIntervalSeconds

    def doWork(self):
        return self.transaction.purgeOldAPNSubscriptions(
            int(time.time()) - self.purgeSeconds
        )
