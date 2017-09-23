##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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
#
##
from __future__ import print_function
from __future__ import absolute_import

from caldavclientlibrary.protocol.caldav.definitions import caldavxml
from caldavclientlibrary.protocol.caldav.definitions import csxml
from caldavclientlibrary.protocol.calendarserver.invite import AddInvitees, RemoveInvitee, InviteUser
from caldavclientlibrary.protocol.calendarserver.notifications import InviteNotification
from caldavclientlibrary.protocol.url import URL
from caldavclientlibrary.protocol.utils.xmlhelpers import BetterElementTree
from caldavclientlibrary.protocol.webdav.definitions import davxml
from caldavclientlibrary.protocol.webdav.propfindparser import PropFindParser

from calendarserver.tools.notifications import PubSubClientFactory

from contrib.performance.loadtest.amphub import AMPHub
from contrib.performance.httpauth import AuthHandlerAgent
from contrib.performance.httpclient import StringProducer, readBody
from contrib.performance.loadtest.subscribe import Periodical

from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from pycalendar.timezone import Timezone

from twext.internet.adaptendpoint import connect
from twext.internet.gaiendpoint import GAIEndpoint
from twisted.internet.ssl import ClientContextFactory

from twisted.internet.defer import Deferred, inlineCallbacks, returnValue, \
    succeed
from twisted.internet.task import LoopingCall
from twisted.python.filepath import FilePath
from twisted.python.log import addObserver, err, msg
from twisted.python.util import FancyEqMixin
from twisted.web.client import Agent, ContentDecoderAgent, GzipDecoder, \
    _DeprecatedToCurrentPolicyForHTTPS
from twisted.web.http import (
    OK, MULTI_STATUS, CREATED, NO_CONTENT, PRECONDITION_FAILED,
    MOVED_PERMANENTLY, FORBIDDEN, FOUND, NOT_FOUND
)
from twisted.web.http_headers import Headers

from twistedcaldav.ical import Component, Property

from urlparse import urlparse, urlunparse, urlsplit, urljoin
from uuid import uuid4
from xml.etree.ElementTree import ElementTree, Element, SubElement, QName

from StringIO import StringIO

import json
import os
import random
import shutil

QName.__repr__ = lambda self: '<QName %r>' % (self.text,)


def loadRequestBody(clientType, label):
    return FilePath(__file__).sibling('request-data').child(clientType).child(label + '.request').getContent()


SUPPORTED_REPORT_SET = '{DAV:}supported-report-set'


class IncorrectResponseCode(Exception):
    """
    Raised when a response has a code other than the one expected.

    @ivar expected: The response codes which was expected.
    @type expected: C{tuple} of C{int}

    @ivar response: The response which was received
    @type response: L{twisted.web.client.Response}

    @ivar responseBody: The body of the received response
    @type responseBody: C{str}
    """

    def __init__(self, expected, response, responseBody):
        self.expected = expected
        self.response = response
        self.responseBody = responseBody


class MissingCalendarHome(Exception):
    """
    Raised when the calendar home for a user is 404
    """


class XMPPPush(object, FancyEqMixin):
    """
    This represents an XMPP PubSub location where push notifications for
    particular calendar home might be received.
    """
    compareAttributes = ('server', 'uri', 'pushkey')

    def __init__(self, server, uri, pushkey):
        self.server = server
        self.uri = uri
        self.pushkey = pushkey


def u2str(data):
    return data.encode("utf-8") if type(data) is unicode else data


class Event(object):

    def __init__(self, serializeBasePath, url, etag, component=None):
        self.serializeBasePath = serializeBasePath
        self.url = url
        self.etag = etag
        self.scheduleTag = None
        if component is not None:
            self.component = component
        self.uid = component.resourceUID() if component is not None else None

    def getUID(self):
        """
        Return the UID of the calendar resource.
        """
        return self.uid

    def serializePath(self):
        if self.serializeBasePath:
            calendar = os.path.join(self.serializeBasePath, self.url.split("/")[-2])
            if not os.path.exists(calendar):
                os.makedirs(calendar)
            return os.path.join(calendar, self.url.split("/")[-1])
        else:
            return None

    def serialize(self):
        """
        Create a dict of the data so we can serialize as JSON.
        """

        result = {}
        for attr in ("url", "etag", "scheduleTag", "uid",):
            result[attr] = getattr(self, attr)
        return result

    @staticmethod
    def deserialize(serializeLocation, data):
        """
        Convert dict (deserialized from JSON) into an L{Event}.
        """

        event = Event(serializeLocation, None, None)
        for attr in ("url", "etag", "scheduleTag", "uid",):
            setattr(event, attr, u2str(data[attr]))
        return event

    @property
    def component(self):
        """
        Data always read from disk - never cached in the object.
        """
        path = self.serializePath()
        if path and os.path.exists(path):
            with open(path) as f:
                comp = Component.fromString(f.read())
            return comp
        else:
            return None

    @component.setter
    def component(self, component):
        """
        Data always written to disk - never cached on the object.
        """
        path = self.serializePath()
        if path:
            if component is None:
                os.remove(path)
            else:
                with open(path, "w") as f:
                    f.write(str(component))
        self.uid = component.resourceUID() if component is not None else None

    def removed(self):
        """
        Resource no longer exists on the server - remove associated data.
        """
        path = self.serializePath()
        if path and os.path.exists(path):
            os.remove(path)


class Calendar(object):

    def __init__(
        self, resourceType, componentTypes, name, url, changeToken,
        shared=False, sharedByMe=False, invitees=None
    ):
        self.resourceType = resourceType
        self.componentTypes = componentTypes
        self.name = name
        self.url = url
        self.changeToken = changeToken
        self.events = {}
        self.shared = shared
        self.sharedByMe = sharedByMe
        self.invitees = invitees if invitees else []

        if self.name is None and self.url is not None:
            self.name = self.url.rstrip("/").split("/")[-1]

    def serialize(self):
        """
        Create a dict of the data so we can serialize as JSON.
        """

        result = {}
        for attr in ("resourceType", "name", "url", "changeToken", "shared", "sharedByMe"):
            result[attr] = getattr(self, attr)
        result["componentTypes"] = list(sorted(self.componentTypes))
        result["events"] = sorted(self.events.keys())
        result["invitees"] = sorted(self.invitees)
        return result

    @staticmethod
    def deserialize(data, events):
        """
        Convert dict (deserialized from JSON) into an L{Calendar}.
        """

        calendar = Calendar(None, None, None, None, None)
        for attr in ("resourceType", "name", "url", "changeToken", "shared", "sharedByMe"):
            setattr(calendar, attr, u2str(data[attr]))
        calendar.componentTypes = set(map(u2str, data["componentTypes"]))
        calendar.invitees = map(u2str, data["invitees"])

        for event in data["events"]:
            url = urljoin(calendar.url, event)
            if url in events:
                calendar.events[event] = events[url]
            else:
                # Ughh - an event is missing - force changeToken to empty to trigger full resync
                calendar.changeToken = ""
        return calendar

    @staticmethod
    def addInviteeXML(uid, summary, readwrite=True):
        return AddInvitees(None, '/', [uid], readwrite, summary=summary).request_data.text

    @staticmethod
    def removeInviteeXML(uid):
        invitee = InviteUser()
        # Usually an InviteUser is populated through .parseFromUser, but we only care about a uid
        invitee.user_uid = uid
        return RemoveInvitee(None, '/', invitee).request_data.text


class NotificationCollection(object):

    def __init__(self, url, changeToken):
        self.url = url
        self.changeToken = changeToken
        self.notifications = {}
        self.name = "notification"

    def serialize(self):
        """
        Create a dict of the data so we can serialize as JSON.
        """

        result = {}
        for attr in ("url", "changeToken"):
            result[attr] = getattr(self, attr)
        result["notifications"] = sorted(self.notifications.keys())
        return result

    @staticmethod
    def deserialize(data, notifications):
        """
        Convert dict (deserialized from JSON) into an L{Calendar}.
        """

        coll = NotificationCollection(None, "")
        for attr in ("url", "changeToken"):
            if attr in data:
                setattr(coll, attr, u2str(data[attr]))

        if "notifications" in data:
            for notification in data["notifications"]:
                url = urljoin(coll.url, notification)
                if url in notifications:
                    coll.notifications[notification] = notifications[url]
                else:
                    # Ughh - a notification is missing - force changeToken to empty to trigger full resync
                    coll.changeToken = ""
        return coll


class BaseClient(object):
    """
    Base interface for all simulated clients.
    """

    user = None                     # User account details
    _events = None                  # Cache of events keyed by href
    _calendars = None               # Cache of calendars keyed by href
    _notificationCollection = None  # Cache of the notification collection
    started = False                 # Whether or not startup() has been executed
    _client_type = None             # Type of this client used in logging
    _client_id = None               # Unique id for the client itself

    def _setEvent(self, href, event):
        """
        Cache the provided event
        """
        self._events[href] = event
        calendar, basePath = href.rsplit('/', 1)
        self._calendars[calendar + '/'].events[basePath] = event

    def _removeEvent(self, href):
        """
        Remove event from local cache.
        """
        self._events[href].removed()
        del self._events[href]
        calendar, basePath = href.rsplit('/', 1)
        del self._calendars[calendar + '/'].events[basePath]

    def eventByHref(self, href):
        """
        Return the locally cached event by its href
        """
        return self._events[href]

    def addEvent(self, href, calendar):
        """
        Called when a profile needs to add an event (no scheduling).
        """
        raise NotImplementedError("%r does not implement addEvent" % (self.__class__,))

    def addInvite(self, href, calendar):
        """
        Called when a profile needs to add a new invite. The iCalendar data will already
        contain ATTENDEEs.
        """
        raise NotImplementedError("%r does not implement addInvite" % (self.__class__,))

    def changeEvent(self, href, calendar):
        """
        Called when a profile needs to change an event (no scheduling).
        """
        raise NotImplementedError("%r does not implement changeEvent" % (self.__class__,))

    def deleteEvent(self, href):
        """
        Called when a profile needs to delete an event.
        """
        raise NotImplementedError("%r does not implement deleteEvent" % (self.__class__,))

    def addEventAttendee(self, href, attendee):
        """
        Called when a profile needs to add an attendee to an existing event.
        """
        raise NotImplementedError("%r does not implement addEventAttendee" % (self.__class__,))

    def changeEventAttendee(self, href, oldAttendee, newAttendee):
        """
        Called when a profile needs to change an attendee on an existing event.
        Used when an attendee is accepting.
        """
        raise NotImplementedError("%r does not implement changeEventAttendee" % (self.__class__,))


class _PubSubClientFactory(PubSubClientFactory):
    """
    Factory for XMPP pubsub functionality.
    """

    def __init__(self, client, *args, **kwargs):
        PubSubClientFactory.__init__(self, *args, **kwargs)
        self._client = client

    def initFailed(self, reason):
        print('XMPP initialization failed', reason)

    def authFailed(self, reason):
        print('XMPP Authentication failed', reason)

    def handleMessageEventItems(self, iq):
        item = iq.firstChildElement().firstChildElement()
        if item:
            node = item.getAttribute("node")
            if node:
                url, _ignore_name, _ignore_kind = self.nodes.get(node, (None, None, None))
                if url is not None:
                    self._client._checkCalendarsForEvents(url, push=True)


class WebClientContextFactory(ClientContextFactory):
    """
    A web context factory which ignores the hostname and port and does no
    certificate verification.
    """

    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)


class BaseAppleClient(BaseClient):
    """
    Implementation of common OS X/iOS client behavior.
    """

    _client_type = "Generic"

    _managed_attachments_server_url = None

    USER_AGENT = None   # Override this for specific clients

    # The default interval, used if none is specified in external
    # configuration.
    CALENDAR_HOME_POLL_INTERVAL = 15 * 60

    # The maximum number of resources to retrieve in a single multiget
    MULTIGET_BATCH_SIZE = 200

    # Override and turn on if client supports Sync REPORT
    _SYNC_REPORT = False

    # Override and turn on if client syncs using time-range queries
    _SYNC_TIMERANGE = False

    # Override and turn off if client does not support attendee lookups
    _ATTENDEE_LOOKUPS = True

    # Request body data
    _LOAD_PATH = None

    _STARTUP_WELL_KNOWN = None
    _STARTUP_PRINCIPAL_PROPFIND_INITIAL = None
    _STARTUP_PRINCIPAL_PROPFIND = None
    _STARTUP_PRINCIPALS_REPORT = None
    _STARTUP_PRINCIPAL_EXPAND = None
    _STARTUP_PROPPATCH_CALENDAR_COLOR = None
    _STARTUP_PROPPATCH_CALENDAR_ORDER = None
    _STARTUP_PROPPATCH_CALENDAR_TIMEZONE = None

    _POLL_CALENDARHOME_PROPFIND = None
    _POLL_CALENDAR_PROPFIND = None
    _POLL_CALENDAR_PROPFIND_D1 = None
    _POLL_CALENDAR_MULTIGET_REPORT = None
    _POLL_CALENDAR_MULTIGET_REPORT_HREF = None
    _POLL_CALENDAR_SYNC_REPORT = None
    _POLL_NOTIFICATION_PROPFIND = None
    _POLL_NOTIFICATION_PROPFIND_D1 = None
    _POLL_INBOX_PROPFIND = None

    _NOTIFICATION_SYNC_REPORT = None
    _CALENDARHOME_SYNC_REPORT = None

    _USER_LIST_PRINCIPAL_PROPERTY_SEARCH = None
    _POST_AVAILABILITY = None

    _CALENDARSERVER_PRINCIPAL_SEARCH_REPORT = None

    email = None

    def __init__(
        self,
        reactor,
        server,
        principalPathTemplate,
        serializePath,
        record,
        auth,
        instanceNumber,
        title=None,
        calendarHomePollInterval=None,
        supportPush=True,
        supportAmpPush=True,
        unauthenticatedPercentage=30,
    ):

        uuid = uuid4()
        self._client_id = str(uuid)
        self._deviceToken = uuid.hex + uuid.hex  # 64 character hex for fake APNS token
        self._instanceNumber = instanceNumber

        self.reactor = reactor

        # The server might use gzip encoding
        agent = Agent(
            self.reactor,
            contextFactory=_DeprecatedToCurrentPolicyForHTTPS(WebClientContextFactory()),
        )
        self.noAuthAgent = ContentDecoderAgent(agent, [("gzip", GzipDecoder)])
        self.agent = AuthHandlerAgent(self.noAuthAgent, auth)
        self.unauthenticatedPercentage = unauthenticatedPercentage

        self.server = server
        self.principalPathTemplate = principalPathTemplate
        self.record = record

        self.title = title if title else self._client_type

        if calendarHomePollInterval is None:
            calendarHomePollInterval = self.CALENDAR_HOME_POLL_INTERVAL
        self.calendarHomePollInterval = calendarHomePollInterval

        self.calendarHomeHref = None
        self.calendarHomeToken = ""

        self.supportPush = supportPush

        self.supportAmpPush = supportAmpPush
        ampPushHosts = self.server.get("ampPushHosts")
        if ampPushHosts is None:
            ampPushHosts = [urlparse(self.server["uri"])[1].split(":")[0]]
        self.ampPushHosts = ampPushHosts
        self.ampPushPort = self.server.get("ampPushPort", 62311)

        self.serializePath = serializePath

        self.supportSync = self._SYNC_REPORT
        self.supportNotificationSync = self._NOTIFICATION_SYNC_REPORT
        self.supportCalendarHomeSync = self._CALENDARHOME_SYNC_REPORT

        self.supportEnhancedAttendeeAutoComplete = self._CALENDARSERVER_PRINCIPAL_SEARCH_REPORT

        # Keep track of the calendars on this account, keys are
        # Calendar URIs, values are Calendar instances.
        self._calendars = {}

        # The principalURL found during discovery
        self.principalURL = None

        # The principal collection found during startup
        self.principalCollection = None

        # Keep track of the events on this account, keys are event
        # URIs (which are unambiguous across different calendars
        # because they start with the uri of the calendar they are
        # part of), values are Event instances.
        self._events = {}

        # Keep track of which calendar homes are being polled
        self._checking = set()

        # Keep track of XMPP parameters for calendar homes we encounter.  This
        # dictionary has calendar home URLs as keys and XMPPPush instances as
        # values.
        self.xmpp = {}

        self.ampPushKeys = {}
        self.subscribedAmpPushKeys = set()

        self._busyWithPush = False

        # Keep track of push factories so we can unsubscribe at shutdown
        self._pushFactories = []

        # Allow events to go out into the world.
        self.catalog = {
            "eventChanged": Periodical(),
        }

        # Keep track of previously downloaded attachments
        self._attachments = {}

    def _addDefaultHeaders(self, headers):
        """
        Add the clients default set of headers to ones being used in a request.
        Default is to add User-Agent, sub-classes should override to add other
        client specific things, Accept etc.
        """
        headers.setRawHeaders(
            'User-Agent', ["{} {} {}".format(
                self.USER_AGENT, self.title, self._instanceNumber
            )]
        )

    @inlineCallbacks
    def _request(self, expectedResponseCodes, method, url, headers=None, body=None, method_label=None):
        """
        Execute a request and check against the expected response codes.
        """
        if type(expectedResponseCodes) is int:
            expectedResponseCodes = (expectedResponseCodes,)
        if headers is None:
            headers = Headers({})
        self._addDefaultHeaders(headers)
        msg(
            type="request",
            method=method_label if method_label else method,
            url=url,
            user=self.record.uid,
            client_type="({} {})".format(self.title, self._instanceNumber),
            client_id=self._client_id,
        )

        choice = random.randint(1, 100)
        if choice < self.unauthenticatedPercentage:
            # First send an unauthenticated request
            response = yield self.noAuthAgent.request(method, url, headers, body)
            yield readBody(response)

        before = self.reactor.seconds()
        response = yield self.agent.request(method, url, headers, body)

        responseBody = yield readBody(response)

        after = self.reactor.seconds()

        success = response.code in expectedResponseCodes

        msg(
            type="response",
            success=success,
            method=method_label if method_label else method,
            headers=headers,
            body=body,
            responseBody=responseBody,
            code=response.code,
            user=self.record.uid,
            client_type="({} {})".format(self.title, self._instanceNumber),
            client_id=self._client_id,
            duration=(after - before),
            url=url,
        )

        if success:
            returnValue((response, responseBody))

        raise IncorrectResponseCode(expectedResponseCodes, response, responseBody)

    def _parseMultiStatus(self, responseBody, otherTokens=False):
        """
        Parse a <multistatus> - might need to return other top-level elements
        in the response - e.g. DAV:sync-token
        I{PROPFIND} request for the principal URL.

        @type responseBody: C{str}
        @rtype: C{cls}
        """
        parser = PropFindParser()
        try:
            parser.parseData(responseBody)
        except:
            print("=" * 80)
            print("COULD NOT PARSE RESPONSE:")
            print(responseBody)
            print("=" * 80)
            raise

        if otherTokens:
            return (parser.getResults(), parser.getOthers(),)
        else:
            return parser.getResults()

    _CALENDAR_TYPES = set([
        caldavxml.calendar,
        caldavxml.schedule_inbox,
    ])

    @inlineCallbacks
    def _propfind(self, url, body, depth='0', allowedStatus=(MULTI_STATUS,), method_label=None):
        """
        Issue a PROPFIND on the chosen URL
        """
        hdrs = Headers({'content-type': ['text/xml']})
        if depth is not None:
            hdrs.addRawHeader('depth', depth)
        response, responseBody = yield self._request(
            allowedStatus,
            'PROPFIND',
            self.server["uri"] + url.encode('utf-8'),
            hdrs,
            StringProducer(body),
            method_label=method_label,
        )
        result = self._parseMultiStatus(responseBody) if response.code == MULTI_STATUS else None

        returnValue((response, result,))

    @inlineCallbacks
    def _proppatch(self, url, body, method_label=None):
        """
        Issue a PROPPATCH on the chosen URL
        """
        hdrs = Headers({'content-type': ['text/xml']})
        response, responseBody = yield self._request(
            (OK, MULTI_STATUS,),
            'PROPPATCH',
            self.server["uri"] + url.encode('utf-8'),
            hdrs,
            StringProducer(body),
            method_label=method_label,
        )
        if response.code == MULTI_STATUS:
            result = self._parseMultiStatus(responseBody)
            returnValue(result)
        else:
            returnValue(None)

    @inlineCallbacks
    def _get(self, url, allowedStatus=(MULTI_STATUS,), method_label=None):
        """
        Issue a GET on the chosen URL
        """
        _ignore_response, responseBody = yield self._request(
            allowedStatus,
            'GET',
            url,
            method_label=method_label,
        )

        returnValue(responseBody)

    @inlineCallbacks
    def _report(self, url, body, depth='0', allowedStatus=(MULTI_STATUS,), otherTokens=False, method_label=None):
        """
        Issue a REPORT on the chosen URL
        """
        hdrs = Headers({'content-type': ['text/xml']})
        if depth is not None:
            hdrs.addRawHeader('depth', depth)
        response, responseBody = yield self._request(
            allowedStatus,
            'REPORT',
            self.server["uri"] + url.encode('utf-8'),
            hdrs,
            StringProducer(body),
            method_label=method_label,
        )

        result = self._parseMultiStatus(responseBody, otherTokens) if response.code == MULTI_STATUS else None

        returnValue(result)

    @inlineCallbacks
    def _startupPropfindWellKnown(self):
        """
        Issue a PROPFIND on the /.well-known/caldav/ URL
        """

        location = "/.well-known/caldav/"
        response, result = yield self._propfind(
            location,
            self._STARTUP_WELL_KNOWN,
            allowedStatus=(MULTI_STATUS, MOVED_PERMANENTLY, FOUND,),
            method_label="PROPFIND{well-known}",
        )

        # Follow any redirect
        if response.code in (MOVED_PERMANENTLY, FOUND,):
            location = response.headers.getRawHeaders("location")[0]
            location = urlsplit(location)[2]
            response, result = yield self._propfind(
                location,
                self._STARTUP_WELL_KNOWN,
                allowedStatus=(MULTI_STATUS),
                method_label="PROPFIND{well-known}",
            )

        returnValue(result[location])

    @inlineCallbacks
    def _principalPropfindInitial(self, user):
        """
        Issue a PROPFIND on the /principals/users/<uid> URL to retrieve
        the /principals/__uids__/<guid> principal URL
        """
        principalPath = self.principalPathTemplate % (user,)
        _ignore_response, result = yield self._propfind(
            principalPath,
            self._STARTUP_PRINCIPAL_PROPFIND_INITIAL,
            method_label="PROPFIND{find-principal}",
        )
        returnValue(result[principalPath])

    @inlineCallbacks
    def _principalPropfind(self):
        """
        Issue a PROPFIND on the likely principal URL for the given
        user and return a L{Principal} instance constructed from the
        response.
        """
        _ignore_response, result = yield self._propfind(
            self.principalURL,
            self._STARTUP_PRINCIPAL_PROPFIND,
            method_label="PROPFIND{principal}",
        )
        returnValue(result[self.principalURL])

    def _principalSearchPropertySetReport(self, principalCollectionSet):
        """
        Issue a principal-search-property-set REPORT against the chosen URL
        """
        return self._report(
            principalCollectionSet,
            self._STARTUP_PRINCIPALS_REPORT,
            allowedStatus=(OK,),
            method_label="REPORT{pset}",
        )

    @inlineCallbacks
    def _calendarHomePropfind(self, calendarHomeSet):
        """
        Do the poll Depth:1 PROPFIND on the calendar home.
        """
        if not calendarHomeSet.endswith('/'):
            calendarHomeSet = calendarHomeSet + '/'
        _ignore_response, result = yield self._propfind(
            calendarHomeSet,
            self._POLL_CALENDARHOME_PROPFIND,
            depth='1',
            method_label="PROPFIND{home}",
        )
        calendars, notificationCollection, calendarHomeToken = self._extractCalendars(
            result, calendarHomeSet
        )
        returnValue((calendars, notificationCollection, calendarHomeToken, result,))

    @inlineCallbacks
    def _calendarHomeSync(self, calendarHomeSet):
        if not calendarHomeSet.endswith('/'):
            calendarHomeSet = calendarHomeSet + '/'

        result = yield self._report(
            calendarHomeSet,
            self._CALENDARHOME_SYNC_REPORT % {'sync-token': self.calendarHomeToken},
            depth='1',
            allowedStatus=(MULTI_STATUS, FORBIDDEN,),
            otherTokens=True,
            method_label="REPORT{sync-home}",
        )
        responseHrefs, others = result

        calendars, notificationCollection, ignored = self._extractCalendars(
            responseHrefs, calendarHomeSet
        )

        # if notificationCollection is None that means it hasn't changed, so
        # we'll return the notificationCollection we already have
        if not notificationCollection:
            notificationCollection = self._notificationCollection

        # by default we'll keep our old calendars...
        newCalendars = {}
        for href, oldCalendar in self._calendars.iteritems():
            newCalendars[href] = oldCalendar

        # ...but if a calendar shows up from _extractCalendars we know it's
        # either new or modified, so replace our old copy with the new
        for cal in calendars:
            # print("SYNC DETECTED CHANGE IN", cal.url)
            newCalendars[cal.url] = cal

        # Now scan through the responses for 404s which let us know a collection
        # has been removed
        for responseHref in responseHrefs:
            if responseHrefs[responseHref].getStatus() == 404:
                if responseHref in newCalendars:
                    del newCalendars[responseHref]
                    # print("DELETED CALENDAR", responseHref)

        # get the new token
        newCalendarHomeToken = ""
        for other in others:
            if other.tag == davxml.sync_token:
                newCalendarHomeToken = other.text
                break

        returnValue((newCalendars.values(), notificationCollection, newCalendarHomeToken))

    def timeRangeQuery(self, url, start, end):

        requestBody = """<?xml version="1.0" encoding="utf-8" ?>
        <C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop>
                <D:getetag/>
                <C:calendar-data>
                    <C:expand start="{start}" end="{end}"/>
                    <C:comp name="VCALENDAR">
                        <C:prop name="VERSION"/>
                        <C:comp name="VEVENT">
                            <C:prop name="SUMMARY"/>
                            <C:prop name="DTSTART"/>
                            <C:prop name="DTEND"/>
                            <C:prop name="LOCATION"/>
                            <C:prop name="UID"/>
                            <C:prop name="RECURRENCE-ID"/>
                        </C:comp>
                    </C:comp>
                </C:calendar-data>
            </D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT">
                        <C:time-range start="{start}" end="{end}"/>
                    </C:comp-filter>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>""".format(start=start, end=end)

        return self._report(
            url,
            requestBody,
            allowedStatus=(MULTI_STATUS,),
            method_label="REPORT{calendar-query}"
        )

    @inlineCallbacks
    def deepRefresh(self):
        calendars, _ignore_notificationCollection, _ignore_calendarHomeToken, _ignore_results = yield self._calendarHomePropfind(self.calendarHomeHref)
        for calendar in calendars:
            yield self._propfind(
                calendar.url,
                self._POLL_CALENDAR_PROPFIND_D1,
                depth='1',
                method_label="PROPFIND{calendar}"
            )

    @inlineCallbacks
    def apnsSubscribe(self):
        url = "{}/apns/".format(self.server["uri"])
        headers = Headers({
            'Content-Type': ['application/x-www-form-urlencoded']
        })
        content = "token={}&key=/CalDAV/example".format(self._deviceToken)
        _ignore_response, _ignore_responseBody = yield self._request(
            OK,
            'POST',
            url,
            headers=headers,
            body=StringProducer(content),
            method_label="POST{apns}"
        )

    @inlineCallbacks
    def unshareAll(self):
        for calendar in self._calendars.values():
            for invitee in calendar.invitees:
                body = Calendar.removeInviteeXML(invitee)
                yield self.postXML(
                    calendar.url,
                    body,
                    label="POST{unshare-calendar}"
                )

    @inlineCallbacks
    def _extractPrincipalDetails(self):
        # Using the actual principal URL, retrieve principal information
        principal = yield self._principalPropfind()

        hrefs = principal.getHrefProperties()

        # Remember our outbox and ignore notifications
        self.outbox = hrefs[caldavxml.schedule_outbox_URL].toString()
        self.notificationURL = None

        # Remember our own email-like principal address
        self.email = None
        self.uuid = None
        cuaddrs = hrefs[caldavxml.calendar_user_address_set]
        if isinstance(cuaddrs, URL):
            cuaddrs = (cuaddrs,)
        for cuaddr in cuaddrs:
            if cuaddr.toString().startswith(u"mailto:"):
                self.email = cuaddr.toString()
            elif cuaddr.toString().startswith(u"urn:x-uid"):
                self.uuid = cuaddr.toString()
            elif cuaddr.toString().startswith(u"urn:uuid") and self.uuid is None:
                self.uuid = cuaddr.toString()
        if self.email is None:
            raise ValueError("Cannot operate without a mail-style principal URL")

        # Do another kind of thing I guess
        self.principalCollection = hrefs[davxml.principal_collection_set].toString()
        yield self._principalSearchPropertySetReport(self.principalCollection)

        returnValue(principal)

    def _extractCalendars(self, results, calendarHome=None):
        """
        Parse a calendar home PROPFIND response and create local state
        representing the calendars it contains.

        If XMPP push is enabled, also look for and record information about
        that from the response.
        """
        calendars = []
        notificationCollection = None
        calendarHomeToken = ""

        changeTag = davxml.sync_token if self.supportSync else csxml.getctag

        for href in results:

            if href == calendarHome:
                text = results[href].getTextProperties()
                hrefs = results[href].getHrefProperties()

                # Extract managed attachments url
                self._managed_attachments_server_url = None
                try:
                    url = hrefs[caldavxml.managed_attachments_server_url]
                    if url.toString():
                        self._managed_attachments_server_url = url.toString()
                except KeyError:
                    pass
                if self._managed_attachments_server_url is None:
                    self._managed_attachments_server_url = self.server['uri']

                try:
                    pushkey = text[csxml.pushkey]
                except KeyError:
                    pass
                else:
                    if pushkey:
                        self.ampPushKeys[href] = pushkey

                try:
                    server = text[csxml.xmpp_server]
                    uri = text[csxml.xmpp_uri]
                    pushkey = text[csxml.pushkey]
                except KeyError:
                    pass
                else:
                    if server and uri:
                        self.xmpp[href] = XMPPPush(server, uri, pushkey)

                try:
                    calendarHomeToken = text[davxml.sync_token]
                except KeyError:
                    pass

            if results[href].getStatus() == 404:
                continue

            nodes = results[href].getNodeProperties()
            isCalendar = False
            isNotifications = False
            isShared = False
            isSharedByMe = False
            resourceType = None
            for nodeType in nodes[davxml.resourcetype]:
                if nodeType.tag in self._CALENDAR_TYPES:
                    isCalendar = True
                    resourceType = nodeType.tag
                elif nodeType.tag == csxml.notification:
                    isNotifications = True
                elif nodeType.tag.startswith("{http://calendarserver.org/ns/}shared"):
                    isShared = True
                    if nodeType.tag == "{http://calendarserver.org/ns/}shared-owner":
                        isSharedByMe = True

            if isCalendar:
                textProps = results[href].getTextProperties()
                componentTypes = set()
                if caldavxml.supported_calendar_component_set in nodes:
                    for comp in nodes[caldavxml.supported_calendar_component_set]:
                        componentTypes.add(comp.get("name").upper())

                # Keep track of sharing invitees
                invitees = []
                if isShared and isSharedByMe:
                    try:
                        invite = nodes[csxml.invite]
                        for user in invite.findall(str(csxml.user)):
                            invitee = user.find(str(davxml.href)).text
                            invitees.append(invitee)
                    except KeyError:
                        pass

                calendars.append(Calendar(
                    resourceType,
                    componentTypes,
                    textProps.get(davxml.displayname, None),
                    href,
                    textProps.get(changeTag, None),
                    shared=isShared,
                    sharedByMe=isSharedByMe,
                    invitees=invitees
                ))

                # Also monitor shared-to-me calendars
                if isShared and not isSharedByMe:
                    try:
                        pushkey = textProps[csxml.pushkey]
                    except KeyError:
                        pass
                    else:
                        if pushkey:
                            self.ampPushKeys[href] = pushkey

            elif isNotifications:
                textProps = results[href].getTextProperties()
                notificationCollection = NotificationCollection(
                    href,
                    textProps.get(changeTag, None)
                )

        return calendars, notificationCollection, calendarHomeToken

    def _updateCalendar(self, calendar, newToken, fetchEvents=True):
        """
        Update the local cached data for a calendar in an appropriate manner.
        """
        if self.supportSync:
            return self._updateCalendar_SYNC(calendar, newToken, fetchEvents=fetchEvents)
        else:
            return self._updateCalendar_PROPFIND(calendar, newToken)

    @inlineCallbacks
    def _updateCalendar_PROPFIND(self, calendar, newToken):
        """
        Sync a collection by doing a full PROPFIND Depth:1 on it and then sync
        the results with local cached data.
        """

        # Grab old hrefs prior to the PROPFIND so we sync with the old state. We need this because
        # the sim can fire a PUT between the PROPFIND and when process the removals.
        old_hrefs = set([calendar.url + child for child in calendar.events.keys()])

        _ignore_response, result = yield self._propfind(
            calendar.url,
            self._POLL_CALENDAR_PROPFIND_D1,
            depth='1',
            method_label="PROPFIND{calendar}"
        )

        yield self._updateApplyChanges(calendar, result, old_hrefs)

        # Now update calendar to the new token
        self._calendars[calendar.url].changeToken = newToken

    @inlineCallbacks
    def _updateCalendar_SYNC(self, calendar, newToken, fetchEvents=True):
        """
        Execute a sync REPORT against a calendar and apply changes to the local cache.
        The new token from the changed collection is passed in and must be applied to
        the existing calendar once sync is done.
        """

        # Grab old hrefs prior to the REPORT so we sync with the old state. We need this because
        # the sim can fire a PUT between the REPORT and when process the removals.
        old_hrefs = set([calendar.url + child for child in calendar.events.keys()])

        # Get changes from sync REPORT (including the other nodes at the top-level
        # which will have the new sync token.
        fullSync = not calendar.changeToken
        result = yield self._report(
            calendar.url,
            self._POLL_CALENDAR_SYNC_REPORT % {'sync-token': calendar.changeToken},
            depth='1',
            allowedStatus=(MULTI_STATUS, FORBIDDEN,),
            otherTokens=True,
            method_label="REPORT{sync-coll}" if calendar.changeToken else "REPORT{sync-coll-init}",
        )
        if result is None:
            if not fullSync:
                fullSync = True
                result = yield self._report(
                    calendar.url,
                    self._POLL_CALENDAR_SYNC_REPORT % {'sync-token': ''},
                    depth='1',
                    otherTokens=True,
                    method_label="REPORT{sync-coll}" if calendar.changeToken else "REPORT{sync-coll-init}",
                )
            else:
                raise IncorrectResponseCode((MULTI_STATUS,), None, None)

        result, others = result

        if fetchEvents:
            changed = []
            for responseHref in result:
                if responseHref == calendar.url:
                    continue

                try:
                    etag = result[responseHref].getTextProperties()[davxml.getetag]
                except KeyError:
                    # XXX Ignore things with no etag?  Seems to be dropbox.
                    continue

                # Differentiate a remove vs new/update result
                if result[responseHref].getStatus() / 100 == 2:
                    if responseHref not in self._events:
                        self._setEvent(responseHref, Event(self.serializeLocation(), responseHref, None))

                    event = self._events[responseHref]
                    if event.etag != etag:
                        changed.append(responseHref)
                elif result[responseHref].getStatus() == 404:
                    self._removeEvent(responseHref)

            yield self._updateChangedEvents(calendar, changed)

            # Handle removals only when doing an initial sync
            if fullSync:
                # Detect removed items and purge them
                remove_hrefs = old_hrefs - set(changed)
                for href in remove_hrefs:
                    self._removeEvent(href)

        # Now update calendar to the new token taken from the report
        for node in others:
            if node.tag == davxml.sync_token:
                newToken = node.text
                break
        self._calendars[calendar.url].changeToken = newToken

    @inlineCallbacks
    def _updateApplyChanges(self, calendar, multistatus, old_hrefs):
        """
        Given a multistatus for an entire collection, sync the reported items
        against the cached items.
        """

        # Detect changes and new items
        all_hrefs = []
        changed_hrefs = []
        for responseHref in multistatus:
            if responseHref == calendar.url:
                continue
            all_hrefs.append(responseHref)
            try:
                etag = multistatus[responseHref].getTextProperties()[davxml.getetag]
            except KeyError:
                # XXX Ignore things with no etag?  Seems to be dropbox.
                continue

            if responseHref not in self._events:
                self._setEvent(responseHref, Event(self.serializeLocation(), responseHref, None))

            event = self._events[responseHref]
            if event.etag != etag:
                changed_hrefs.append(responseHref)

        # Retrieve changes
        yield self._updateChangedEvents(calendar, changed_hrefs)

        # Detect removed items and purge them
        remove_hrefs = old_hrefs - set(all_hrefs)
        for href in remove_hrefs:
            self._removeEvent(href)

    @inlineCallbacks
    def _updateChangedEvents(self, calendar, changed):
        """
        Given a set of changed hrefs, batch multiget them all to update the
        local cache.
        """

        changed.sort()
        while changed:
            batchedHrefs = changed[:self.MULTIGET_BATCH_SIZE]
            changed = changed[self.MULTIGET_BATCH_SIZE:]

            multistatus = yield self._eventReport(calendar.url, batchedHrefs)
            for responseHref in batchedHrefs:
                try:
                    res = multistatus[responseHref]
                except KeyError:
                    # Resource might have been deleted
                    continue
                if res.getStatus() == 200:
                    text = res.getTextProperties()
                    etag = text[davxml.getetag]
                    try:
                        scheduleTag = text[caldavxml.schedule_tag]
                    except KeyError:
                        scheduleTag = None
                    body = text[caldavxml.calendar_data]
                    self.eventChanged(responseHref, etag, scheduleTag, body)

    def eventChanged(self, href, etag, scheduleTag, body):
        event = self._events[href]
        event.etag = etag
        if scheduleTag is not None:
            event.scheduleTag = scheduleTag
        event.component = Component.fromString(body)
        self.catalog["eventChanged"].issue(href)

    def _eventReport(self, calendar, events):
        # Next do a REPORT on events that might have information
        # we don't know about.
        hrefs = "".join([self._POLL_CALENDAR_MULTIGET_REPORT_HREF % {'href': event} for event in events])

        label_suffix = "small"
        if len(events) > 5:
            label_suffix = "medium"
        if len(events) > 20:
            label_suffix = "large"
        if len(events) > 75:
            label_suffix = "huge"

        return self._report(
            calendar,
            self._POLL_CALENDAR_MULTIGET_REPORT % {'hrefs': hrefs},
            depth=None,
            method_label="REPORT{multiget-%s}" % (label_suffix,),
        )

    @inlineCallbacks
    def _checkCalendarsForEvents(self, calendarHomeSet, firstTime=False, push=False):
        """
        The actions a client does when polling for changes, or in response to a
        push notification of a change. There are some actions done on the first poll
        we should emulate.
        """

        result = True
        try:
            result = yield self._newOperation("push" if push else "poll", self._poll(calendarHomeSet, firstTime))
        finally:
            if result:
                try:
                    self._checking.remove(calendarHomeSet)
                except KeyError:
                    pass
        returnValue(result)

    @inlineCallbacks
    def _updateNotifications(self, oldToken, newToken):

        fullSync = not oldToken

        # Get the list of notification xml resources

        result = yield self._report(
            self._notificationCollection.url,
            self._NOTIFICATION_SYNC_REPORT % {'sync-token': oldToken},
            depth='1',
            allowedStatus=(MULTI_STATUS, FORBIDDEN,),
            otherTokens=True,
            method_label="REPORT{sync-notif}" if oldToken else "REPORT{sync-notif-init}",
        )
        if result is None:
            if not fullSync:
                fullSync = True
                result = yield self._report(
                    self._notificationCollection.url,
                    self._NOTIFICATION_SYNC_REPORT % {'sync-token': ''},
                    depth='1',
                    otherTokens=True,
                    method_label="REPORT{sync-notif}" if oldToken else "REPORT{sync-notif-init}",
                )
            else:
                raise IncorrectResponseCode((MULTI_STATUS,), None, None)

        result, _ignore_others = result

        # Scan for the sharing invites
        inviteNotifications = []
        toDelete = []
        for responseHref in result:
            if responseHref == self._notificationCollection.url:
                continue

            # try:
            #     etag = result[responseHref].getTextProperties()[davxml.getetag]
            # except KeyError:
            #     # XXX Ignore things with no etag?  Seems to be dropbox.
            #     continue

            toDelete.append(responseHref)

            if result[responseHref].getStatus() / 100 == 2:
                # Get the notification
                response, responseBody = yield self._request(
                    (OK, NOT_FOUND),
                    'GET',
                    self.server["uri"] + responseHref.encode('utf-8'),
                    method_label="GET{notification}",
                )
                if response.code == OK:
                    node = ElementTree(file=StringIO(responseBody)).getroot()
                    if node.tag == str(csxml.notification):
                        nurl = URL(url=responseHref)
                        for child in node.getchildren():
                            if child.tag == str(csxml.invite_notification):
                                if child.find(str(csxml.invite_noresponse)) is not None:
                                    inviteNotifications.append(
                                        InviteNotification().parseFromNotification(
                                            nurl, child
                                        )
                                    )

        # Accept the invites
        for notification in inviteNotifications:
            # Create an invite-reply
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <C:invite-reply xmlns:C="http://calendarserver.org/ns/">
              <A:href xmlns:A="DAV:">urn:x-uid:10000000-0000-0000-0000-000000000002</A:href>
              <C:invite-accepted/>
              <C:hosturl>
                <A:href xmlns:A="DAV:">/calendars/__uids__/10000000-0000-0000-0000-000000000001/A1DDC58B-651E-4B1C-872A-C6588CA09ADB</A:href>
              </C:hosturl>
              <C:in-reply-to>d2683fa9-7a50-4390-82bb-cbcea5e0fa86</C:in-reply-to>
              <C:summary>to share</C:summary>
            </C:invite-reply>
            """
            reply = Element(csxml.invite_reply)
            href = SubElement(reply, davxml.href)
            href.text = notification.user_uid
            SubElement(reply, csxml.invite_accepted)
            hosturl = SubElement(reply, csxml.hosturl)
            href = SubElement(hosturl, davxml.href)
            href.text = notification.hosturl
            inReplyTo = SubElement(reply, csxml.in_reply_to)
            inReplyTo.text = notification.uid
            summary = SubElement(reply, csxml.summary)
            summary.text = notification.summary

            xmldoc = BetterElementTree(reply)
            os = StringIO()
            xmldoc.writeUTF8(os)
            # Post to my calendar home
            response = yield self.postXML(
                self.calendarHomeHref,
                os.getvalue(),
                "POST{invite-accept}"
            )

        # Delete all the notification resources
        for responseHref in toDelete:
            response, responseBody = yield self._request(
                (NO_CONTENT, NOT_FOUND),
                'DELETE',
                self.server["uri"] + responseHref.encode('utf-8'),
                method_label="DELETE{invite}",
            )

        self._notificationCollection.changeToken = newToken

    @inlineCallbacks
    def _poll(self, calendarHomeSet, firstTime):
        """
        This gets called during a normal poll or in response to a push
        """

        if calendarHomeSet in self._checking:
            returnValue(False)
        self._checking.add(calendarHomeSet)

        if firstTime or not self.calendarHomeToken:
            calendars, notificationCollection, newCalendarHomeToken, results = yield self._calendarHomePropfind(calendarHomeSet)
        else:
            calendars, notificationCollection, newCalendarHomeToken = yield self._calendarHomeSync(calendarHomeSet)
            results = None

        self.calendarHomeToken = newCalendarHomeToken

        # First time operations
        if firstTime and results:
            yield self._pollFirstTime1(results[calendarHomeSet], calendars)

        # Normal poll
        for cal in calendars:
            newToken = cal.changeToken
            if cal.url not in self._calendars:
                # Calendar seen for the first time - reload it
                self._calendars[cal.url] = cal
                cal.changeToken = ""
                # If this is the first time this run and we have no cached copy
                # of this calendar, do an update but don't fetch the events.
                # We'll only take notice of ongoing activity and not bother
                # with existing events.
                yield self._updateCalendar(
                    self._calendars[cal.url], newToken,
                    fetchEvents=(not firstTime)
                )
            elif self._calendars[cal.url].changeToken != newToken:
                # Calendar changed - reload it
                yield self._updateCalendar(self._calendars[cal.url], newToken)
                self._calendars[cal.url].invitees = cal.invitees

            if self._instanceNumber == 0 and cal.name == "inbox":
                yield self._inboxPropfind()

        # Clean out previously seen collections that are no longer on the server
        currentCalendarUris = [c.url for c in calendars]
        for previouslySeenCalendarUri in self._calendars.keys():
            if previouslySeenCalendarUri not in currentCalendarUris:
                calendarToDelete = self._calendars[previouslySeenCalendarUri]
                for eventUri in calendarToDelete.events.keys():
                    eventUri = urljoin(previouslySeenCalendarUri, eventUri)
                    self._removeEvent(eventUri)
                del self._calendars[previouslySeenCalendarUri]

        if notificationCollection is not None:
            if self.supportNotificationSync:
                if self._notificationCollection:
                    oldToken = self._notificationCollection.changeToken
                else:
                    oldToken = ""
                self._notificationCollection = notificationCollection
                newToken = notificationCollection.changeToken
                if oldToken != newToken:
                    yield self._updateNotifications(oldToken, newToken)
            else:
                # When there is no sync REPORT, clients have to do a full PROPFIND
                # on the notification collection because there is no ctag
                self._notificationCollection = notificationCollection
                yield self._notificationPropfind(self._notificationCollection.url)
                yield self._notificationChangesPropfind(self._notificationCollection.url)

        # One time delegate expansion
        if firstTime:
            yield self._pollFirstTime2()

        pushKeys = self.ampPushKeys.values()
        self._monitorAmpPush(calendarHomeSet, pushKeys)

        # if firstTime:
        #     # clear out shared calendars
        #     yield self.unshareAll()

        returnValue(True)

    @inlineCallbacks
    def _pollFirstTime1(self, homeNode, calendars):
        # Detect sync report if needed
        if self.supportSync:
            nodes = homeNode.getNodeProperties()
            syncnodes = nodes[davxml.supported_report_set].findall(
                str(davxml.supported_report) + "/" +
                str(davxml.report) + "/" +
                str(davxml.sync_collection)
            )
            self.supportSync = len(syncnodes) != 0

        # Patch calendar properties
        for cal in calendars:
            if cal.name != "inbox":
                yield self._proppatch(
                    cal.url,
                    self._STARTUP_PROPPATCH_CALENDAR_COLOR,
                    method_label="PROPPATCH{calendar}",
                )
                yield self._proppatch(
                    cal.url,
                    self._STARTUP_PROPPATCH_CALENDAR_ORDER,
                    method_label="PROPPATCH{calendar}",
                )
                yield self._proppatch(
                    cal.url,
                    self._STARTUP_PROPPATCH_CALENDAR_TIMEZONE,
                    method_label="PROPPATCH{calendar}",
                )

    def _pollFirstTime2(self):
        return self._principalExpand(self.principalURL)

    @inlineCallbacks
    def _notificationPropfind(self, notificationURL):
        _ignore_response, result = yield self._propfind(
            notificationURL,
            self._POLL_NOTIFICATION_PROPFIND,
            method_label="PROPFIND{notification}",
        )
        returnValue(result)

    @inlineCallbacks
    def _notificationChangesPropfind(self, notificationURL):
        _ignore_response, result = yield self._propfind(
            notificationURL,
            self._POLL_NOTIFICATION_PROPFIND_D1,
            depth='1',
            method_label="PROPFIND{notification-items}",
        )
        returnValue(result)

    @inlineCallbacks
    def _inboxPropfind(self):
        for cal in self._calendars.itervalues():
            if cal.name == "inbox":
                inboxURL = cal.url
                yield self._propfind(
                    inboxURL,
                    self._POLL_INBOX_PROPFIND,
                    depth='1',
                    method_label="PROPFIND{inbox}",
                )
                break

    @inlineCallbacks
    def _principalExpand(self, principalURL):
        result = yield self._report(
            principalURL,
            self._STARTUP_PRINCIPAL_EXPAND,
            depth=None,
            method_label="REPORT{expand}",
        )
        returnValue(result)

    def startup(self):
        raise NotImplementedError

    def _calendarCheckLoop(self, calendarHome):
        """
        Periodically check the calendar home for changes to calendars.
        """
        pollCalendarHome = LoopingCall(
            self._checkCalendarsForEvents, calendarHome)
        return pollCalendarHome.start(self.calendarHomePollInterval, now=False)

    @inlineCallbacks
    def _newOperation(self, label, deferred):
        before = self.reactor.seconds()
        msg(
            type="operation",
            phase="start",
            user=self.record.uid,
            client_type="({} {})".format(self.title, self._instanceNumber),
            client_id=self._client_id,
            label=label,
        )

        try:
            result = yield deferred
        except IncorrectResponseCode:
            # Let this through
            success = False
            result = None
        except:
            # Anything else is fatal
            raise
        else:
            success = True

        after = self.reactor.seconds()
        msg(
            type="operation",
            phase="end",
            duration=after - before,
            user=self.record.uid,
            client_type="({} {})".format(self.title, self._instanceNumber),
            client_id=self._client_id,
            label=label,
            success=success,
        )
        returnValue(result)

    def _monitorPubSub(self, home, params):
        """
        Start monitoring the
        """
        host, port = params.server.split(':')
        port = int(port)

        service, _ignore_stuff = params.uri.split('?')
        service = service.split(':', 1)[1]

        # XXX What is the domain of the 2nd argument supposed to be?  The
        # hostname we use to connect, or the same as the email address in the
        # user record?
        factory = _PubSubClientFactory(
            self, "%s@%s" % (self.record.uid, host),
            self.record.password, service,
            {params.pushkey: (home, home, "Calendar home")}, False,
            sigint=False)
        self._pushFactories.append(factory)
        connect(GAIEndpoint(self.reactor, host, port), factory)

    @inlineCallbacks
    def _receivedPush(self, inboundID, dataChangedTimestamp, priority=5):
        if not self._busyWithPush:
            self._busyWithPush = True
            try:
                for _ignore_href, myId in self.ampPushKeys.iteritems():
                    if inboundID == myId:
                        yield self._checkCalendarsForEvents(self.calendarHomeHref, push=True)
                        break
                else:
                    # somehow we are not subscribed to this id
                    pass
            finally:
                self._busyWithPush = False

    def _monitorAmpPush(self, home, pushKeys):
        """
        Start monitoring for AMP-based push notifications
        """

        # Only subscribe to keys we haven't previously subscribed to
        subscribeTo = []
        for pushKey in pushKeys:
            if pushKey not in self.subscribedAmpPushKeys:
                subscribeTo.append(pushKey)
                self.subscribedAmpPushKeys.add(pushKey)

        if subscribeTo:
            AMPHub.subscribeToIDs(subscribeTo, self._receivedPush)

    @inlineCallbacks
    def _unsubscribePubSub(self):
        for factory in self._pushFactories:
            yield factory.unsubscribeAll()

    @inlineCallbacks
    def run(self):
        """
        Emulate a CalDAV client.
        """
        @inlineCallbacks
        def startup():
            principal = yield self.startup()
            hrefs = principal.getHrefProperties()
            calendarHome = hrefs[caldavxml.calendar_home_set].toString()
            if calendarHome is None:
                raise MissingCalendarHome
            else:
                self.calendarHomeHref = calendarHome
            yield self._checkCalendarsForEvents(
                calendarHome,
                # If we already have calendarHomeToken from deserialize,
                # it's not our first time
                firstTime=(not self.calendarHomeToken)
            )
            returnValue(calendarHome)
        calendarHome = yield self._newOperation("startup", startup())

        self.started = True

        # Start monitoring PubSub notifications, if possible.
        # _checkCalendarsForEvents populates self.xmpp if it finds
        # anything.
        if self.supportPush and calendarHome in self.xmpp:
            self._monitorPubSub(calendarHome, self.xmpp[calendarHome])
            # Run indefinitely.
            yield Deferred()
        elif self.supportAmpPush and calendarHome in self.ampPushKeys:
            pushKeys = self.ampPushKeys.values()
            self._monitorAmpPush(calendarHome, pushKeys)
            # Run indefinitely.
            yield Deferred()
        else:
            # This completes when the calendar home poll loop completes, which
            # currently it never will except due to an unexpected error.
            yield self._calendarCheckLoop(calendarHome)

    def stop(self):
        """
        Called before connections are closed, giving a chance to clean up
        """

        self.serialize()
        return self._unsubscribePubSub()

    def serializeLocation(self):
        """
        Return the path to the directory where data for this user is serialized.
        """
        if self.serializePath is None or not os.path.isdir(self.serializePath):
            return None

        key = "%s-%s" % (self.record.uid, self.title.replace(" ", "_"))
        path = os.path.join(self.serializePath, key)
        if not os.path.exists(path):
            os.mkdir(path)
        elif not os.path.isdir(path):
            return None

        return path

    def serialize(self):
        """
        Write current state to disk.
        """

        path = self.serializeLocation()
        if path is None:
            return

        # Create dict for all the data we need to store
        data = {
            "homeToken": self.calendarHomeToken,
            "attachmentsUrl": self._managed_attachments_server_url,
            "principalURL": self.principalURL,
            "calendars": [calendar.serialize() for calendar in sorted(self._calendars.values(), key=lambda x:x.name)],
            "events": [event.serialize() for event in sorted(self._events.values(), key=lambda x:x.url)],
            "notificationCollection": self._notificationCollection.serialize() if self._notificationCollection else {},
            "attachments": self._attachments
        }
        # Write JSON data
        with open(os.path.join(path, "index.json"), "w") as f:
            json.dump(data, f, indent=2)

    def deserialize(self):
        """
        Read state from disk.
        """

        self._calendars = {}
        self._events = {}
        self._attachments = {}

        path = self.serializeLocation()
        if path is None:
            return

        # Parse JSON data for calendars
        try:
            with open(os.path.join(path, "index.json")) as f:
                data = json.load(f)
        except IOError:
            return

        self.principalURL = data["principalURL"]
        self.calendarHomeToken = data["homeToken"].encode("utf-8")
        self._managed_attachments_server_url = data["attachmentsUrl"].encode("utf-8")

        # Extract all the events first, then do the calendars (which reference the events)
        for event in data["events"]:
            event = Event.deserialize(self.serializeLocation(), event)
            self._events[event.url] = event
        for calendar in data["calendars"]:
            calendar = Calendar.deserialize(calendar, self._events)
            self._calendars[calendar.url] = calendar
        if data.get("notificationCollection"):
            self._notificationCollection = NotificationCollection.deserialize(data, {})
        self._attachments = data.get("attachments", {})

    @inlineCallbacks
    def reset(self):
        path = self.serializeLocation()
        if path is not None and os.path.exists(path):
            shutil.rmtree(path)
        yield self.startup()
        yield self._checkCalendarsForEvents(self.calendarHomeHref)

    def _makeSelfAttendee(self):
        attendee = Property(
            name=u'ATTENDEE',
            value=self.email,
            params={
                'CN': self.record.commonName,
                'CUTYPE': 'INDIVIDUAL',
                'PARTSTAT': 'ACCEPTED',
            },
        )
        return attendee

    def _makeSelfOrganizer(self):
        organizer = Property(
            name=u'ORGANIZER',
            value=self.email,
            params={
                'CN': self.record.commonName,
            },
        )
        return organizer

    @inlineCallbacks
    def addEventAttendee(self, href, attendee):

        event = self._events[href]
        component = event.component

        # Trigger auto-complete behavior
        yield self._attendeeAutoComplete(component, attendee)

        # If the event has no attendees, add ourselves as an attendee.
        attendees = list(component.mainComponent().properties('ATTENDEE'))
        if len(attendees) == 0:
            # First add ourselves as a participant and as the
            # organizer.  In the future for this event we should
            # already have those roles.
            component.mainComponent().addProperty(self._makeSelfOrganizer())
            component.mainComponent().addProperty(self._makeSelfAttendee())
        attendees.append(attendee)
        component.mainComponent().addProperty(attendee)

        label_suffix = "small"
        if len(attendees) > 5:
            label_suffix = "medium"
        if len(attendees) > 20:
            label_suffix = "large"
        if len(attendees) > 75:
            label_suffix = "huge"

        headers = Headers({
            'content-type': ['text/calendar'],
            'if-match': [event.etag]})
        if event.etag is None:
            headers.removeHeader('if-match')

        # At last, upload the new event definition
        response, _ignore_responseBody = yield self._request(
            (NO_CONTENT, PRECONDITION_FAILED,),
            'PUT',
            self.server["uri"] + href.encode('utf-8'),
            headers,
            StringProducer(component.getTextWithTimezones(includeTimezones=True)),
            method_label="PUT{organizer-%s}" % (label_suffix,)
        )

        # Finally, re-retrieve the event to update the etag
        if not response.headers.hasHeader("etag"):
            response = yield self.updateEvent(href)

    @inlineCallbacks
    def _attendeeAutoComplete(self, component, attendee):

        if self._ATTENDEE_LOOKUPS:
            # Temporarily use some non-test names (some which will return
            # many results, and others which will return fewer) because the
            # test account names are all too similar
            # name = attendee.parameterValue('CN').encode("utf-8")
            # prefix = name[:4].lower()
            prefix = random.choice([
                "chris", "cyru", "dre", "eric", "morg",
                "well", "wilfr", "witz"
            ])

            email = attendee.value()
            if email.startswith("mailto:"):
                email = email[7:]
            elif attendee.hasParameter('EMAIL'):
                email = attendee.parameterValue('EMAIL').encode("utf-8")

            if self.supportEnhancedAttendeeAutoComplete:
                # New calendar server enhanced query
                search = "<C:search-token>{}</C:search-token>".format(prefix)
                body = self._CALENDARSERVER_PRINCIPAL_SEARCH_REPORT.format(
                    context="attendee", searchTokens=search)
                yield self._report(
                    self.principalCollection,
                    body,
                    depth=None,
                    method_label="REPORT{cpsearch}",
                )
            else:
                # First try to discover some names to supply to the
                # auto-completion
                yield self._report(
                    self.principalCollection,
                    self._USER_LIST_PRINCIPAL_PROPERTY_SEARCH % {
                        'displayname': prefix,
                        'email': prefix,
                        'firstname': prefix,
                        'lastname': prefix,
                    },
                    depth=None,
                    method_label="REPORT{psearch}",
                )

            # Now learn about the attendee's availability
            yield self.requestAvailability(
                component.mainComponent().getStartDateUTC(),
                component.mainComponent().getEndDateUTC(),
                [self.email, u'mailto:' + email],
                [component.resourceUID()]
            )

    @inlineCallbacks
    def changeEventAttendee(self, href, oldAttendee, newAttendee):
        event = self._events[href]
        component = event.component

        # Change the event to have the new attendee instead of the old attendee
        component.mainComponent().removeProperty(oldAttendee)
        component.mainComponent().addProperty(newAttendee)
        okCodes = NO_CONTENT
        headers = Headers({
            'content-type': ['text/calendar'],
        })
        if event.scheduleTag is not None:
            headers.addRawHeader('if-schedule-tag-match', event.scheduleTag)
            okCodes = (NO_CONTENT, PRECONDITION_FAILED,)

        attendees = list(component.mainComponent().properties('ATTENDEE'))
        label_suffix = "small"
        if len(attendees) > 5:
            label_suffix = "medium"
        if len(attendees) > 20:
            label_suffix = "large"
        if len(attendees) > 75:
            label_suffix = "huge"

        response, _ignore_responseBody = yield self._request(
            okCodes,
            'PUT',
            self.server["uri"] + href.encode('utf-8'),
            headers, StringProducer(component.getTextWithTimezones(includeTimezones=True)),
            method_label="PUT{attendee-%s}" % (label_suffix,),
        )

        # Finally, re-retrieve the event to update the etag
        if not response.headers.hasHeader("etag"):
            response = yield self.updateEvent(href)

    @inlineCallbacks
    def deleteEvent(self, href):
        """
        Issue a DELETE for the given URL and remove local state
        associated with that event.
        """

        self._removeEvent(href)

        response, _ignore_responseBody = yield self._request(
            (NO_CONTENT, NOT_FOUND),
            'DELETE',
            self.server["uri"] + href.encode('utf-8'),
            method_label="DELETE{event}",
        )
        returnValue(response)

    @inlineCallbacks
    def addEvent(self, href, component, invite=False, attachmentSize=0):
        headers = Headers({
            'content-type': ['text/calendar'],
        })

        attendees = list(component.mainComponent().properties('ATTENDEE'))
        label_suffix = "small"
        if len(attendees) > 5:
            label_suffix = "medium"
        if len(attendees) > 20:
            label_suffix = "large"
        if len(attendees) > 75:
            label_suffix = "huge"

        response, _ignore_responseBody = yield self._request(
            CREATED,
            'PUT',
            self.server["uri"] + href.encode('utf-8'),
            headers,
            StringProducer(component.getTextWithTimezones(includeTimezones=True)),
            method_label="PUT{organizer-%s}" % (label_suffix,) if invite else "PUT{event}",
        )
        self._localUpdateEvent(response, href, component)

        if not response.headers.hasHeader("etag"):
            response = yield self.updateEvent(href)

        # Add optional attachment
        if attachmentSize:
            yield self.postAttachment(href, 'x' * attachmentSize)

    @inlineCallbacks
    def addInvite(self, href, component, attachmentSize=0, lookupPercentage=100):
        """
        Add an event that is an invite - i.e., has attendees. We will do attendee lookups and freebusy
        checks on each attendee to simulate what happens when an organizer creates a new invite.
        """

        # Do lookup and free busy of each attendee (not self)
        attendees = list(component.mainComponent().properties('ATTENDEE'))
        for attendee in attendees:
            if attendee.value() in (self.uuid, self.email):
                continue
            choice = random.randint(1, 100)
            if choice <= lookupPercentage:
                yield self._attendeeAutoComplete(component, attendee)

        # Now do a normal PUT
        yield self.addEvent(href, component, invite=True, attachmentSize=attachmentSize)

    @inlineCallbacks
    def changeEvent(self, href):

        event = self._events[href]
        component = event.component

        headers = Headers({
            'content-type': ['text/calendar'],
            'if-match': [event.etag]})
        if event.etag is None:
            headers.removeHeader('if-match')

        # At last, upload the new event definition
        response, _ignore_responseBody = yield self._request(
            (NO_CONTENT, PRECONDITION_FAILED,),
            'PUT',
            self.server["uri"] + href.encode('utf-8'),
            headers,
            StringProducer(component.getTextWithTimezones(includeTimezones=True)),
            method_label="PUT{update}"
        )

        # Finally, re-retrieve the event to update the etag
        if not response.headers.hasHeader("etag"):
            response = yield self.updateEvent(href)

    def _localUpdateEvent(self, response, href, component):
        headers = response.headers
        etag = headers.getRawHeaders("etag", [None])[0]
        scheduleTag = headers.getRawHeaders("schedule-tag", [None])[0]

        event = Event(self.serializeLocation(), href, etag, component)
        event.scheduleTag = scheduleTag
        self._setEvent(href, event)

    @inlineCallbacks
    def updateEvent(self, href):
        response, responseBody = yield self._request(
            OK,
            'GET',
            self.server["uri"] + href.encode('utf-8'),
            method_label="GET{event}",
        )
        headers = response.headers
        etag = headers.getRawHeaders('etag')[0]
        scheduleTag = headers.getRawHeaders('schedule-tag', [None])[0]
        self.eventChanged(href, etag, scheduleTag, responseBody)
        returnValue(response)

    @inlineCallbacks
    def requestAvailability(self, start, end, users, mask=set()):
        """
        Issue a VFREEBUSY request for I{roughly} the given date range for the
        given users.  The date range is quantized to one day.  Because of this
        it is an error for the range to span more than 24 hours.

        @param start: A C{datetime} instance giving the beginning of the
            desired range.

        @param end: A C{datetime} instance giving the end of the desired range.

        @param users: An iterable of user UUIDs which will be included in the
            request.

        @param mask: An iterable of event UIDs which are to be ignored for the
            purposes of this availability lookup.

        @return: A C{Deferred} which fires with a C{dict}.  Keys in the dict
            are user UUIDs (those requested) and values are something else.
        """
        outbox = self.server["uri"] + self.outbox

        if mask:
            maskStr = u'\r\n'.join(['X-CALENDARSERVER-MASK-UID:' + uid
                                    for uid in mask]) + u'\r\n'
        else:
            maskStr = u''
        maskStr = maskStr.encode('utf-8')

        attendeeStr = '\r\n'.join(['ATTENDEE:' + uuid.encode('utf-8')
                                   for uuid in users]) + '\r\n'

        # iCal issues 24 hour wide vfreebusy requests, starting and ending at 4am.
        if start.compareDate(end):
            msg("Availability request spanning multiple days (%r to %r), "
                "dropping the end date." % (start, end))

        start.setTimezone(Timezone.UTCTimezone)
        start.setHHMMSS(0, 0, 0)
        end = start + Duration(hours=24)

        start = start.getText()
        end = end.getText()
        now = DateTime.getNowUTC().getText()

        label_suffix = "small"
        if len(users) > 5:
            label_suffix = "medium"
        if len(users) > 20:
            label_suffix = "large"
        if len(users) > 75:
            label_suffix = "huge"

        _ignore_response, responseBody = yield self._request(
            OK, 'POST', outbox,
            Headers({
                    'content-type': ['text/calendar'],
                    'originator': [self.email],
                    'recipient': [u', '.join(users).encode('utf-8')]}),
            StringProducer(self._POST_AVAILABILITY % {
                'attendees': attendeeStr,
                'summary': (u'Availability for %s' % (', '.join(users),)).encode('utf-8'),
                'organizer': self.email.encode('utf-8'),
                'vfreebusy-uid': str(uuid4()).upper(),
                'event-mask': maskStr,
                'start': start,
                'end': end,
                'now': now,
            }),
            method_label="POST{fb-%s}" % (label_suffix,),
        )
        returnValue(responseBody)

    @inlineCallbacks
    def postAttachment(self, href, content):
        url = self.server["uri"] + "{0}?{1}".format(href, "action=attachment-add")
        filename = 'file-{}.txt'.format(len(content))
        headers = Headers({
            'Content-Disposition': ['attachment; filename="{}"'.format(filename)]
        })
        response, responseBody = yield self._request(
            CREATED,
            'POST',
            url,
            headers=headers,
            body=StringProducer(content),
            method_label="POST{attachment}"
        )

        # We don't want to download an attachment we uploaded, so look for the
        # Cal-Managed-Id: and Location: headers and remember those
        managedId = response.headers.getRawHeaders("Cal-Managed-Id")[0]
        location = response.headers.getRawHeaders("Location")[0]
        self._attachments[managedId] = location

        yield self.updateEvent(href)
        returnValue(responseBody)

    @inlineCallbacks
    def getAttachment(self, href, managedId):

        # If we've already downloaded this managedId, skip it.
        if managedId not in self._attachments:
            self._attachments[managedId] = href
            yield self._newOperation(
                "download",
                self._get(href, (OK, FORBIDDEN), method_label="GET{attachment}")
            )

    @inlineCallbacks
    def postXML(self, href, content, label):
        headers = Headers({
            'content-type': ['text/xml']
        })
        _ignore_response, responseBody = yield self._request(
            (OK, CREATED, MULTI_STATUS),
            'POST',
            self.server["uri"] + href,
            headers=headers,
            body=StringProducer(content),
            method_label=label
        )
        returnValue(responseBody)


class OS_X_10_6(BaseAppleClient):
    """
    Implementation of the OS X 10.6 iCal network behavior.

    Anything OS X 10.6 iCal does on its own, or any particular
    network behaviors it takes in response to a user action, belong on
    this class.

    Usage-profile based behaviors ("the user modifies an event every
    3.2 minutes") belong elsewhere.
    """

    _client_type = "OS X 10.6"

    USER_AGENT = "DAVKit/4.0.3 (732); CalendarStore/4.0.3 (991); iCal/4.0.3 (1388); Mac OS X/10.6.4 (10F569)"

    # The default interval, used if none is specified in external
    # configuration.  This is also the actual value used by Snow
    # Leopard iCal.
    CALENDAR_HOME_POLL_INTERVAL = 15 * 60

    # The maximum number of resources to retrieve in a single multiget
    MULTIGET_BATCH_SIZE = 200

    # Override and turn on if client supports Sync REPORT
    _SYNC_REPORT = False

    # Override and turn on if client syncs using time-range queries
    _SYNC_TIMERANGE = False

    # Override and turn off if client does not support attendee lookups
    _ATTENDEE_LOOKUPS = True

    # Request body data
    _LOAD_PATH = "OS_X_10_6"

    _STARTUP_WELL_KNOWN = loadRequestBody(_LOAD_PATH, 'startup_well_known')
    _STARTUP_PRINCIPAL_PROPFIND_INITIAL = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind_initial')
    _STARTUP_PRINCIPAL_PROPFIND = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind')
    _STARTUP_PRINCIPALS_REPORT = loadRequestBody(_LOAD_PATH, 'startup_principals_report')
    _STARTUP_PRINCIPAL_EXPAND = loadRequestBody(_LOAD_PATH, 'startup_principal_expand')
    _STARTUP_PROPPATCH_CALENDAR_COLOR = loadRequestBody(_LOAD_PATH, 'startup_calendar_color_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_ORDER = loadRequestBody(_LOAD_PATH, 'startup_calendar_order_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_TIMEZONE = loadRequestBody(_LOAD_PATH, 'startup_calendar_timezone_proppatch')

    _POLL_CALENDARHOME_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendarhome_propfind')
    _POLL_CALENDAR_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_CALENDAR_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind_d1')
    _POLL_CALENDAR_MULTIGET_REPORT = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget')
    _POLL_CALENDAR_MULTIGET_REPORT_HREF = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget_hrefs')
    _POLL_CALENDAR_SYNC_REPORT = None
    _POLL_NOTIFICATION_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_NOTIFICATION_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_notification_propfind_d1')

    _USER_LIST_PRINCIPAL_PROPERTY_SEARCH = loadRequestBody(_LOAD_PATH, 'user_list_principal_property_search')
    _POST_AVAILABILITY = loadRequestBody(_LOAD_PATH, 'post_availability')

    @inlineCallbacks
    def startup(self):

        # Try to read data from disk - if it succeeds self.principalURL will be set
        self.deserialize()

        if self.principalURL is None:
            # PROPFIND principal path to retrieve actual principal-URL
            response = yield self._principalPropfindInitial(self.record.uid)
            hrefs = response.getHrefProperties()
            self.principalURL = hrefs[davxml.principal_URL].toString()

        # Using the actual principal URL, retrieve principal information
        principal = (yield self._extractPrincipalDetails())
        returnValue(principal)


class OS_X_10_7(BaseAppleClient):
    """
    Implementation of the OS X 10.7 iCal network behavior.
    """

    _client_type = "OS X 10.7"

    USER_AGENT = "CalendarStore/5.0.2 (1166); iCal/5.0.2 (1571); Mac OS X/10.7.3 (11D50)"

    # The default interval, used if none is specified in external
    # configuration.  This is also the actual value used by Snow
    # Leopard iCal.
    CALENDAR_HOME_POLL_INTERVAL = 15 * 60

    # The maximum number of resources to retrieve in a single multiget
    MULTIGET_BATCH_SIZE = 50

    # Override and turn on if client supports Sync REPORT
    _SYNC_REPORT = True

    # Override and turn on if client syncs using time-range queries
    _SYNC_TIMERANGE = False

    # Override and turn off if client does not support attendee lookups
    _ATTENDEE_LOOKUPS = True

    # Request body data
    _LOAD_PATH = "OS_X_10_7"

    _STARTUP_WELL_KNOWN = loadRequestBody(_LOAD_PATH, 'startup_well_known')
    _STARTUP_PRINCIPAL_PROPFIND_INITIAL = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind_initial')
    _STARTUP_PRINCIPAL_PROPFIND = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind')
    _STARTUP_PRINCIPALS_REPORT = loadRequestBody(_LOAD_PATH, 'startup_principals_report')
    _STARTUP_PRINCIPAL_EXPAND = loadRequestBody(_LOAD_PATH, 'startup_principal_expand')
    _STARTUP_PROPPATCH_CALENDAR_COLOR = loadRequestBody(_LOAD_PATH, 'startup_calendar_color_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_ORDER = loadRequestBody(_LOAD_PATH, 'startup_calendar_order_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_TIMEZONE = loadRequestBody(_LOAD_PATH, 'startup_calendar_timezone_proppatch')

    _POLL_CALENDARHOME_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendarhome_propfind')
    _POLL_CALENDAR_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_CALENDAR_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind_d1')
    _POLL_CALENDAR_MULTIGET_REPORT = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget')
    _POLL_CALENDAR_MULTIGET_REPORT_HREF = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget_hrefs')
    _POLL_CALENDAR_SYNC_REPORT = loadRequestBody(_LOAD_PATH, 'poll_calendar_sync')
    _POLL_NOTIFICATION_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_NOTIFICATION_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_notification_propfind_d1')

    _USER_LIST_PRINCIPAL_PROPERTY_SEARCH = loadRequestBody(_LOAD_PATH, 'user_list_principal_property_search')
    _POST_AVAILABILITY = loadRequestBody(_LOAD_PATH, 'post_availability')

    def _addDefaultHeaders(self, headers):
        """
        Add the clients default set of headers to ones being used in a request.
        Default is to add User-Agent, sub-classes should override to add other
        client specific things, Accept etc.
        """

        super(OS_X_10_7, self)._addDefaultHeaders(headers)
        headers.setRawHeaders('Accept', ['*/*'])
        headers.setRawHeaders('Accept-Language', ['en-us'])
        headers.setRawHeaders('Accept-Encoding', ['gzip,deflate'])
        headers.setRawHeaders('Connection', ['keep-alive'])

    @inlineCallbacks
    def startup(self):

        # Try to read data from disk - if it succeeds self.principalURL will be set
        self.deserialize()

        if self.principalURL is None:
            # PROPFIND well-known with redirect
            response = yield self._startupPropfindWellKnown()
            hrefs = response.getHrefProperties()
            if davxml.current_user_principal in hrefs:
                self.principalURL = hrefs[davxml.current_user_principal].toString()
            elif davxml.principal_URL in hrefs:
                self.principalURL = hrefs[davxml.principal_URL].toString()
            else:
                # PROPFIND principal path to retrieve actual principal-URL
                response = yield self._principalPropfindInitial(self.record.uid)
                hrefs = response.getHrefProperties()
                self.principalURL = hrefs[davxml.principal_URL].toString()

        # Using the actual principal URL, retrieve principal information
        principal = yield self._extractPrincipalDetails()
        returnValue(principal)


class OS_X_10_11(BaseAppleClient):
    """
    Implementation of the OS X 10.11 Calendar.app network behavior.
    """

    _client_type = "OS X 10.11"

    USER_AGENT = "Mac+OS+X/10.11 (15A283) CalendarAgent/361 Simulator"

    # The default interval, used if none is specified in external
    # configuration.  This is also the actual value used by El
    # Capital Calendar.app.
    CALENDAR_HOME_POLL_INTERVAL = 15 * 60  # in seconds

    # The maximum number of resources to retrieve in a single multiget
    MULTIGET_BATCH_SIZE = 50

    # Override and turn on if client supports Sync REPORT
    _SYNC_REPORT = True

    # Override and turn off if client does not support attendee lookups
    _ATTENDEE_LOOKUPS = True

    # Request body data
    _LOAD_PATH = "OS_X_10_11"

    _STARTUP_WELL_KNOWN = loadRequestBody(_LOAD_PATH, 'startup_well_known_propfind')
    _STARTUP_PRINCIPAL_PROPFIND_INITIAL = loadRequestBody(_LOAD_PATH, 'startup_principal_initial_propfind')
    _STARTUP_PRINCIPAL_PROPFIND = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind')
    _STARTUP_PRINCIPALS_REPORT = loadRequestBody(_LOAD_PATH, 'startup_principals_report')
    _STARTUP_PRINCIPAL_EXPAND = loadRequestBody(_LOAD_PATH, 'startup_principal_expand')

    _STARTUP_CREATE_CALENDAR = loadRequestBody(_LOAD_PATH, 'startup_create_calendar')
    _STARTUP_PROPPATCH_CALENDAR_COLOR = loadRequestBody(_LOAD_PATH, 'startup_calendar_color_proppatch')
    # _STARTUP_PROPPATCH_CALENDAR_NAME = loadRequestBody(_LOAD_PATH, 'startup_calendar_displayname_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_ORDER = loadRequestBody(_LOAD_PATH, 'startup_calendar_order_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_TIMEZONE = loadRequestBody(_LOAD_PATH, 'startup_calendar_timezone_proppatch')

    _POLL_CALENDARHOME_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendarhome_depth1_propfind')
    _POLL_CALENDAR_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_CALENDAR_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_calendar_depth1_propfind')
    _POLL_CALENDAR_MULTIGET_REPORT = loadRequestBody('OS_X_10_7', 'poll_calendar_multiget')
    _POLL_CALENDAR_MULTIGET_REPORT_HREF = loadRequestBody('OS_X_10_7', 'poll_calendar_multiget_hrefs')
    _POLL_CALENDAR_SYNC_REPORT = loadRequestBody('OS_X_10_7', 'poll_calendar_sync')
    _POLL_NOTIFICATION_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_NOTIFICATION_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_notification_depth1_propfind')
    _POLL_INBOX_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_inbox_propfind')

    _NOTIFICATION_SYNC_REPORT = loadRequestBody(_LOAD_PATH, 'notification_sync')
    _CALENDARHOME_SYNC_REPORT = loadRequestBody(_LOAD_PATH, 'poll_calendarhome_sync')

    _USER_LIST_PRINCIPAL_PROPERTY_SEARCH = loadRequestBody('OS_X_10_7', 'user_list_principal_property_search')
    _POST_AVAILABILITY = loadRequestBody('OS_X_10_7', 'post_availability')

    _CALENDARSERVER_PRINCIPAL_SEARCH_REPORT = loadRequestBody(_LOAD_PATH, 'principal_search_report')

    def _addDefaultHeaders(self, headers):
        """
        Add the clients default set of headers to ones being used in a request.
        Default is to add User-Agent, sub-classes should override to add other
        client specific things, Accept etc.
        """

        super(OS_X_10_11, self)._addDefaultHeaders(headers)
        headers.setRawHeaders('Accept', ['*/*'])
        headers.setRawHeaders('Accept-Language', ['en-us'])
        headers.setRawHeaders('Accept-Encoding', ['gzip,deflate'])
        headers.setRawHeaders('Connection', ['keep-alive'])

    @inlineCallbacks
    def startup(self):
        # Try to read data from disk - if it succeeds self.principalURL will be set
        self.deserialize()

        if self.principalURL is None:
            # PROPFIND well-known with redirect
            response = yield self._startupPropfindWellKnown()
            hrefs = response.getHrefProperties()
            if davxml.current_user_principal in hrefs:
                self.principalURL = hrefs[davxml.current_user_principal].toString()
            elif davxml.principal_URL in hrefs:
                self.principalURL = hrefs[davxml.principal_URL].toString()
            else:
                # PROPFIND principal path to retrieve actual principal-URL
                response = yield self._principalPropfindInitial(self.record.uid)
                hrefs = response.getHrefProperties()
                self.principalURL = hrefs[davxml.principal_URL].toString()

        # Using the actual principal URL, retrieve principal information
        principal = yield self._extractPrincipalDetails()
        returnValue(principal)


class iOS_5(BaseAppleClient):
    """
    Implementation of the iOS 5 network behavior.
    """

    _client_type = "iOS 5"

    USER_AGENT = "iOS/5.1 (9B179) dataaccessd/1.0"

    # The default interval, used if none is specified in external
    # configuration.  This is also the actual value used by Snow
    # Leopard iCal.
    CALENDAR_HOME_POLL_INTERVAL = 15 * 60

    # The maximum number of resources to retrieve in a single multiget
    MULTIGET_BATCH_SIZE = 50

    # Override and turn on if client supports Sync REPORT
    _SYNC_REPORT = False

    # Override and turn on if client syncs using time-range queries
    _SYNC_TIMERANGE = True

    # Override and turn off if client does not support attendee lookups
    _ATTENDEE_LOOKUPS = False

    # Request body data
    _LOAD_PATH = "iOS_5"

    _STARTUP_WELL_KNOWN = loadRequestBody(_LOAD_PATH, 'startup_well_known')
    _STARTUP_PRINCIPAL_PROPFIND_INITIAL = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind_initial')
    _STARTUP_PRINCIPAL_PROPFIND = loadRequestBody(_LOAD_PATH, 'startup_principal_propfind')
    _STARTUP_PRINCIPALS_REPORT = loadRequestBody(_LOAD_PATH, 'startup_principals_report')
    _STARTUP_PROPPATCH_CALENDAR_COLOR = loadRequestBody(_LOAD_PATH, 'startup_calendar_color_proppatch')
    _STARTUP_PROPPATCH_CALENDAR_ORDER = loadRequestBody(_LOAD_PATH, 'startup_calendar_order_proppatch')

    _POLL_CALENDARHOME_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendarhome_propfind')
    _POLL_CALENDAR_PROPFIND = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind')
    _POLL_CALENDAR_VEVENT_TR_QUERY = loadRequestBody(_LOAD_PATH, 'poll_calendar_vevent_tr_query')
    _POLL_CALENDAR_VTODO_QUERY = loadRequestBody(_LOAD_PATH, 'poll_calendar_vtodo_query')
    _POLL_CALENDAR_PROPFIND_D1 = loadRequestBody(_LOAD_PATH, 'poll_calendar_propfind_d1')
    _POLL_CALENDAR_MULTIGET_REPORT = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget')
    _POLL_CALENDAR_MULTIGET_REPORT_HREF = loadRequestBody(_LOAD_PATH, 'poll_calendar_multiget_hrefs')

    def _addDefaultHeaders(self, headers):
        """
        Add the clients default set of headers to ones being used in a request.
        Default is to add User-Agent, sub-classes should override to add other
        client specific things, Accept etc.
        """

        super(iOS_5, self)._addDefaultHeaders(headers)
        headers.setRawHeaders('Accept', ['*/*'])
        headers.setRawHeaders('Accept-Language', ['en-us'])
        headers.setRawHeaders('Accept-Encoding', ['gzip,deflate'])
        headers.setRawHeaders('Connection', ['keep-alive'])

    @inlineCallbacks
    def _pollFirstTime1(self, homeNode, calendars):
        # Patch calendar properties
        for cal in calendars:
            if cal.name != "inbox":
                yield self._proppatch(
                    cal.url,
                    self._STARTUP_PROPPATCH_CALENDAR_COLOR,
                    method_label="PROPPATCH{calendar}",
                )
                yield self._proppatch(
                    cal.url,
                    self._STARTUP_PROPPATCH_CALENDAR_ORDER,
                    method_label="PROPPATCH{calendar}",
                )

    def _pollFirstTime2(self):
        # Nothing here
        return succeed(None)

    def _updateCalendar(self, calendar, newToken):
        """
        Update the local cached data for a calendar in an appropriate manner.
        """
        if calendar.name == "inbox":
            # Inbox is done as a PROPFIND Depth:1
            return self._updateCalendar_PROPFIND(calendar, newToken)
        elif "VEVENT" in calendar.componentTypes:
            # VEVENTs done as time-range VEVENT-only queries
            return self._updateCalendar_VEVENT(calendar, newToken)
        elif "VTODO" in calendar.componentTypes:
            # VTODOs done as VTODO-only queries
            return self._updateCalendar_VTODO(calendar, newToken)

    @inlineCallbacks
    def _updateCalendar_VEVENT(self, calendar, newToken):
        """
        Sync all locally cached VEVENTs using a VEVENT-only time-range query.
        """

        # Grab old hrefs prior to the PROPFIND so we sync with the old state. We need this because
        # the sim can fire a PUT between the PROPFIND and when process the removals.
        old_hrefs = set([calendar.url + child for child in calendar.events.keys()])

        now = DateTime.getNowUTC()
        now.setDateOnly(True)
        now.offsetMonth(-1)  # 1 month back default
        result = yield self._report(
            calendar.url,
            self._POLL_CALENDAR_VEVENT_TR_QUERY % {"start-date": now.getText()},
            depth='1',
            method_label="REPORT{vevent}",
        )

        yield self._updateApplyChanges(calendar, result, old_hrefs)

        # Now update calendar to the new token
        self._calendars[calendar.url].changeToken = newToken

    @inlineCallbacks
    def _updateCalendar_VTODO(self, calendar, newToken):
        """
        Sync all locally cached VTODOs using a VTODO-only query.
        """

        # Grab old hrefs prior to the PROPFIND so we sync with the old state. We need this because
        # the sim can fire a PUT between the PROPFIND and when process the removals.
        old_hrefs = set([calendar.url + child for child in calendar.events.keys()])

        result = yield self._report(
            calendar.url,
            self._POLL_CALENDAR_VTODO_QUERY,
            depth='1',
            method_label="REPORT{vtodo}",
        )

        yield self._updateApplyChanges(calendar, result, old_hrefs)

        # Now update calendar to the new token
        self._calendars[calendar.url].changeToken = newToken

    @inlineCallbacks
    def startup(self):

        # Try to read data from disk - if it succeeds self.principalURL will be set
        self.deserialize()

        if self.principalURL is None:
            # PROPFIND well-known with redirect
            response = yield self._startupPropfindWellKnown()
            hrefs = response.getHrefProperties()
            if davxml.current_user_principal in hrefs:
                self.principalURL = hrefs[davxml.current_user_principal].toString()
            elif davxml.principal_URL in hrefs:
                self.principalURL = hrefs[davxml.principal_URL].toString()
            else:
                # PROPFIND principal path to retrieve actual principal-URL
                response = yield self._principalPropfindInitial(self.record.uid)
                hrefs = response.getHrefProperties()
                self.principalURL = hrefs[davxml.principal_URL].toString()

        # Using the actual principal URL, retrieve principal information
        principal = yield self._extractPrincipalDetails()
        returnValue(principal)


class RequestLogger(object):
    format = u"%(user)s %(client)s request %(code)s%(success)s[%(duration)5.2f s] %(method)8s %(url)s"
    success = u"\N{CHECK MARK}"
    failure = u"\N{BALLOT X}"

    def observe(self, event):
        if event.get("type") == "response":
            formatArgs = dict(
                user=event['user'],
                method=event['method'],
                url=urlunparse(('', '') + urlparse(event['url'])[2:]),
                code=event['code'],
                duration=event['duration'],
                client=event['client_type'],
            )

            if event['success']:
                formatArgs['success'] = self.success
            else:
                formatArgs['success'] = self.failure
            print((self.format % formatArgs).encode('utf-8'))

            if not event['success']:
                body = event['body']
                if isinstance(body, StringProducer):
                    body = body._body
                if body:
                    body = body[:5000]

                responseBody = event['responseBody']
                if responseBody:
                    responseBody = responseBody[:5000]

                print("=" * 80)
                print("INCORRECT ERROR CODE: {}".format(event['code']))
                print("URL: {}".format(event['url']))
                print("METHOD: {}".format(event['method']))
                print("USER: {}".format(event['user']))
                print("REQUEST BODY:\n{}".format(body))
                print("RESPONSE BODY:\n{}".format(responseBody))
                print("=" * 80)

            # elif event["duration"] > 9.0:
            #     body = event['body']
            #     if isinstance(body, StringProducer):
            #         body = body._body
            #     if body:
            #         body = body[:5000]

            #     responseBody = event['responseBody']
            #     if responseBody:
            #         responseBody = responseBody[:5000]

            #     print("=" * 80)
            #     print("LONG RESPONSE: {}".format(event['duration']))
            #     print("URL: {}".format(event['url']))
            #     print("METHOD: {}".format(event['method']))
            #     print("USER: {}".format(event['user']))
            #     print("REQUEST BODY:\n{}".format(body))
            #     print("RESPONSE BODY:\n{}".format(responseBody))
            #     print("=" * 80)

    def report(self, output):
        pass

    def failures(self):
        return []


class ErrorLogger(object):
    """
    Requests which get an incorrect response code or take too long are logged
    """

    def __init__(self, directory, durationThreshold):
        self.directory = directory
        self.durationThreshold = durationThreshold
        if os.path.isdir(directory):
            shutil.rmtree(directory)
        os.mkdir(directory)
        self.errorCounter = 0
        self.durationCounter = 0

    def _getBodies(self, event):
        body = event['body']
        if isinstance(body, StringProducer):
            body = body._body
        if body:
            body = body[:5000]
        responseBody = event['responseBody']
        return body, responseBody

    def observe(self, event):
        if event.get("type") == "response":
            if not event['success']:
                body, responseBody = self._getBodies(event)
                self.errorCounter += 1
                filename = "error-{:08d}.txt".format(self.errorCounter)
                fullname = os.path.join(self.directory, filename)
                with open(fullname, "w") as f:
                    f.write("RESPONSE CODE: {}\n".format(event['code']))
                    f.write("URL: {}\n".format(event['url']))
                    f.write("METHOD: {}\n".format(event['method']))
                    f.write("USER: {}\n".format(event['user']))
                    f.write("REQUEST BODY:\n{}\n".format(body))
                    f.write("RESPONSE BODY:\n{}\n".format(responseBody))

                print("Incorrect Response Code logged to {}".format(fullname))

            elif event["duration"] > self.durationThreshold:
                body, responseBody = self._getBodies(event)
                self.durationCounter += 1

                filename = "duration-{:08d}.txt".format(self.durationCounter)
                fullname = os.path.join(self.directory, filename)
                with open(fullname, "w") as f:
                    f.write("LONG RESPONSE: {:.1f} sec\n".format(event['duration']))
                    f.write("RESPONSE CODE: {}\n".format(event['code']))
                    f.write("URL: {}\n".format(event['url']))
                    f.write("METHOD: {}\n".format(event['method']))
                    f.write("USER: {}\n".format(event['user']))
                    f.write("REQUEST BODY:\n{}\n".format(body))
                    f.write("RESPONSE BODY:\n{}\n".format(responseBody))

                print("Long Duration logged to {}".format(fullname))

    def report(self, output):
        pass

    def failures(self):
        return []


def main():
    from urllib2 import HTTPDigestAuthHandler
    from twisted.internet import reactor
    auth = HTTPDigestAuthHandler()
    auth.add_password(
        realm="Test Realm",
        uri="http://127.0.0.1:8008/",
        user="user01",
        passwd="user01")

    addObserver(RequestLogger().observe)

    from .sim import _DirectoryRecord
    client = OS_X_10_6(
        reactor, 'http://127.0.0.1:8008/',
        _DirectoryRecord(
            u'user01', u'user01', u'User 01', u'user01@example.org'),
        auth)
    d = client.run()
    d.addErrback(err, "10.6 client run() problem")
    d.addCallback(lambda ignored: reactor.stop())
    reactor.run()


if __name__ == '__main__':
    main()
