##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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

from StringIO import StringIO

from calendarserver.version import version

from twext.internet.gaiendpoint import GAIEndpoint, MultiFailure
from twext.python.log import Logger
from txweb2 import responsecode
from txweb2.client.http import ClientRequest
from txweb2.client.http import HTTPClientProtocol
from txweb2.dav.http import ErrorResponse
from txweb2.dav.util import davXMLFromStream, joinURL, allDataFromStream
from txweb2.http import HTTPError
from txweb2.http_headers import Headers
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream

from twisted.internet.defer import inlineCallbacks, DeferredList, returnValue
from twisted.internet.error import ConnectionDone, ConnectionRefusedError
from twisted.internet.protocol import Factory
from twisted.python.failure import Failure

from twistedcaldav.accounting import accountingEnabledForCategory, emitAccounting
from twistedcaldav.client.pool import _configuredClientContextFactory
from twistedcaldav.config import config
from twistedcaldav.ical import normalizeCUAddress, Component
from twistedcaldav.util import utf8String

from txdav.caldav.datastore.scheduling.cuaddress import RemoteCalendarUser, OtherServerCalendarUser
from txdav.caldav.datastore.scheduling.delivery import DeliveryService
from txdav.caldav.datastore.scheduling.ischedule.dkim import DKIMRequest, DKIMUtils
from txdav.caldav.datastore.scheduling.ischedule.remoteservers import IScheduleServerRecord
from txdav.caldav.datastore.scheduling.ischedule.remoteservers import IScheduleServers
from txdav.caldav.datastore.scheduling.ischedule.utils import lookupServerViaSRV
from txdav.caldav.datastore.scheduling.ischedule.xml import ScheduleResponse, Response, \
    RequestStatus, Recipient, ischedule_namespace, CalendarData, \
    ResponseDescription, Error
from txdav.caldav.datastore.scheduling.itip import iTIPRequestStatus
from txdav.caldav.datastore.scheduling.utils import extractEmailDomain
from txdav.caldav.datastore.util import normalizationLookup

from urlparse import urlsplit

"""
Handles the sending of iSchedule scheduling messages. Used for both cross-domain scheduling,
as well as internal podding.
"""

__all__ = [
    "ScheduleViaISchedule",
]

log = Logger()


class ScheduleViaISchedule(DeliveryService):

    domainServerMap = {}
    servermgr = None

    @classmethod
    def serviceType(cls):
        return DeliveryService.serviceType_ischedule

    @classmethod
    @inlineCallbacks
    def matchCalendarUserAddress(cls, cuaddr):

        # Handle mailtos:
        if cuaddr.lower().startswith("mailto:"):
            domain = extractEmailDomain(cuaddr)
            server = (yield cls.serverForDomain(domain))
            returnValue(server is not None)

        # Do default match
        result = (yield super(ScheduleViaISchedule, cls).matchCalendarUserAddress(cuaddr))
        returnValue(result)

    @classmethod
    @inlineCallbacks
    def serverForDomain(cls, domain):
        if domain not in cls.domainServerMap:

            if config.Scheduling.iSchedule.Enabled:

                # First check built-in list of remote servers
                if cls.servermgr is None:
                    cls.servermgr = IScheduleServers()
                server = cls.servermgr.mapDomain(domain)
                if server is not None:
                    cls.domainServerMap[domain] = server
                else:
                    # Lookup domain
                    result = (yield lookupServerViaSRV(domain))
                    if result is None:
                        # Lookup domain
                        result = (yield lookupServerViaSRV(domain, service="_ischedule"))
                        if result is None:
                            cls.domainServerMap[domain] = None
                        else:
                            # Create the iSchedule server record for this server
                            cls.domainServerMap[domain] = IScheduleServerRecord(uri="http://{}:{}/.well-known/ischedule".format(*result))
                    else:
                        # Create the iSchedule server record for this server
                        cls.domainServerMap[domain] = IScheduleServerRecord(uri="https://{}:{}/.well-known/ischedule".format(*result))
            else:
                cls.domainServerMap[domain] = None

        returnValue(cls.domainServerMap[domain])

    @inlineCallbacks
    def generateSchedulingResponses(self, refreshOnly=False):
        """
        Generate scheduling responses for remote recipients.
        """

        # Group recipients by server so that we can do a single request with multiple recipients
        # to each different server.
        groups = {}
        for recipient in self.recipients:
            if isinstance(recipient, RemoteCalendarUser):
                # Map the recipient's domain to a server
                server = (yield self.serverForDomain(recipient.domain))
            elif isinstance(recipient, OtherServerCalendarUser):
                server = self._getServerForOtherServerUser(recipient)
            else:
                assert False, "Incorrect calendar user address class"
            if not server:
                # Cannot do server-to-server for this recipient.
                err = HTTPError(ErrorResponse(
                    responsecode.NOT_FOUND,
                    (ischedule_namespace, "recipient-allowed"),
                    "No server for recipient",
                ))
                self.responses.add(recipient.cuaddr, Failure(exc_value=err), reqstatus=iTIPRequestStatus.NO_USER_SUPPORT)

                # Process next recipient
                continue

            if not server.allow_to:
                # Cannot do server-to-server outgoing requests for this server.
                err = HTTPError(ErrorResponse(
                    responsecode.NOT_FOUND,
                    (ischedule_namespace, "recipient-allowed"),
                    "Cannot send to recipient's server",
                ))
                self.responses.add(recipient.cuaddr, Failure(exc_value=err), reqstatus=iTIPRequestStatus.SERVICE_UNAVAILABLE)

                # Process next recipient
                continue

            groups.setdefault(server, []).append(recipient)

        if len(groups) == 0:
            returnValue(None)

        # Now we process each server: let's use a DeferredList to aggregate all the Deferred's
        # we will generate for each request. That way we can have parallel requests in progress
        # rather than serialize them.
        deferreds = []
        for server, recipients in groups.iteritems():
            requestor = IScheduleRequest(self.scheduler, server, recipients, self.responses, refreshOnly)
            deferreds.append(requestor.doRequest())

        yield DeferredList(deferreds)

    def _getServerForOtherServerUser(self, recipient):

        if not hasattr(self, "otherServers"):
            self.otherServers = {}

        serverURI = recipient.record.serverURI()
        if serverURI not in self.otherServers:
            self.otherServers[serverURI] = IScheduleServerRecord(
                uri=joinURL(serverURI, config.Servers.InboxName),
                rewriteCUAddresses=recipient.record.server().v5,
                moreHeaders=[recipient.record.server().secretHeader(), ],
                podding=True,
            )

        return self.otherServers[serverURI]


class IScheduleRequest(object):

    def __init__(self, scheduler, server, recipients, responses, refreshOnly=False):

        self.scheduler = scheduler
        self.server = server
        self.recipients = recipients
        self.responses = responses
        self.refreshOnly = refreshOnly
        self.headers = None
        self.data = None
        self.original_organizer = None

    @inlineCallbacks
    def doRequest(self):

        # Generate an HTTP client request
        try:
            if self.scheduler.logItems is not None:
                if "itip.ischedule" not in self.scheduler.logItems:
                    self.scheduler.logItems["itip.ischedule"] = 0
                self.scheduler.logItems["itip.ischedule"] += 1

            # Loop over at most 3 redirects
            ssl, host, port, path = self.server.details()
            for _ignore in xrange(3):
                yield self._prepareRequest(host, port)
                response = (yield self._processRequest(ssl, host, port, path))
                if response.code not in (responsecode.MOVED_PERMANENTLY, responsecode.TEMPORARY_REDIRECT,):
                    break
                if response.code == responsecode.MOVED_PERMANENTLY:
                    self.server.redirect(response.headers.getRawHeaders("location")[0])
                    ssl, host, port, path = self.server.details()
                else:
                    scheme, netloc, path, _ignore_query, _ignore_fragment = urlsplit(response.headers.getRawHeaders("location")[0])
                    ssl = scheme.lower() == "https"
                    host = netloc.split(":")
                    if ":" in netloc:
                        host, port = netloc.split(":")
                        port = int(port)
                    else:
                        host = netloc
                        port = 443 if ssl else 80

                if accountingEnabledForCategory("iSchedule"):
                    self.loggedResponse = yield self.logResponse(response)
                    emitAccounting("iSchedule", "", self.loggedRequest + "\n" + self.loggedResponse, "POST")
            else:
                raise ValueError("Too many redirects")

            if accountingEnabledForCategory("iSchedule"):
                self.loggedResponse = yield self.logResponse(response)
                emitAccounting("iSchedule", "", self.loggedRequest + "\n" + self.loggedResponse, "POST")

            if response.code in (responsecode.OK,):
                xml = (yield davXMLFromStream(response.stream))
                self._parseResponse(xml)
            else:
                raise ValueError("Incorrect server response status code: {code}".format(code=response.code))

        except Exception as e:
            # Check for connection failure
            if isinstance(e, MultiFailure) and not self.scheduler.isfreebusy:
                all_connections_failed = all([isinstance(err.value, ConnectionRefusedError) for err in e.failures])
            else:
                all_connections_failed = False

            # We will return MESSAGE_PENDING if we failed to connect to the remote server, otherwise SERVICE_UNAVAILABLE
            failed_status = iTIPRequestStatus.MESSAGE_PENDING if all_connections_failed else iTIPRequestStatus.SERVICE_UNAVAILABLE

            # Generated failed responses for each recipient
            log.error(
                "Could not do server-to-server request : {req} {exc}",
                req=self,
                exc=e,
            )
            for recipient in self.recipients:
                err = HTTPError(ErrorResponse(
                    responsecode.FORBIDDEN,
                    (ischedule_namespace, "recipient-failed"),
                    "Server-to-server request failed",
                ))
                self.responses.add(recipient.cuaddr, Failure(exc_value=err), reqstatus=failed_status)

    @inlineCallbacks
    def logRequest(self, request):
        """
        Log an HTTP request.
        """

        iostr = StringIO()
        iostr.write(">>>> Request start\n\n")
        if hasattr(request, "clientproto"):
            protocol = "HTTP/{:d}.{:d}".format(request.clientproto[0], request.clientproto[1])
        else:
            protocol = "HTTP/1.1"
        iostr.write("{} {} {}\n".format(request.method, request.uri, protocol,))
        for name, valuelist in request.headers.getAllRawHeaders():
            for value in valuelist:
                # Do not log authorization details
                if name not in ("Authorization",):
                    iostr.write("{}: {}\n".format(name, value))
                else:
                    iostr.write("{}: xxxxxxxxx\n".format(name,))
        iostr.write("\n")

        # We need to play a trick with the request stream as we can only read it once. So we
        # read it, store the value in a MemoryStream, and replace the request's stream with that,
        # so the data can be read again.
        data = (yield allDataFromStream(request.stream))
        iostr.write(data)
        request.stream = MemoryStream(data if data is not None else "")
        request.stream.doStartReading = None

        iostr.write("\n\n>>>> Request end\n")
        returnValue(iostr.getvalue())

    @inlineCallbacks
    def logResponse(self, response):
        """
        Log an HTTP request.
        """
        iostr = StringIO()
        iostr.write(">>>> Response start\n\n")
        code_message = responsecode.RESPONSES.get(response.code, "Unknown Status")
        iostr.write("HTTP/1.1 {} {}\n".format(response.code, code_message,))
        for name, valuelist in response.headers.getAllRawHeaders():
            for value in valuelist:
                # Do not log authorization details
                if name not in ("WWW-Authenticate",):
                    iostr.write("{}: {}\n".format(name, value))
                else:
                    iostr.write("{}: xxxxxxxxx\n".format(name,))
        iostr.write("\n")

        # We need to play a trick with the response stream to ensure we don't mess it up. So we
        # read it, store the value in a MemoryStream, and replace the response's stream with that,
        # so the data can be read again.
        try:
            data = (yield allDataFromStream(response.stream))
        except ConnectionDone:
            data = ""
        iostr.write(data)
        response.stream = MemoryStream(data if data is not None else "")
        response.stream.doStartReading = None

        iostr.write("\n\n>>>> Response end\n")
        returnValue(iostr.getvalue())

    @inlineCallbacks
    def _prepareRequest(self, host, port):
        """
        Setup the request for sending. We might need to do this several times
        whilst following redirects.
        """

        component, method = (yield self._prepareData())
        yield self._prepareHeaders(host, port, component, method)

    @inlineCallbacks
    def _prepareHeaders(self, host, port, component, method):
        """
        Always generate a new set of headers because the Host may varying during redirects,
        or we may need to dump DKIM added headers during a redirect.
        """
        self.sign_headers = []

        self.headers = Headers()
        self.headers.setHeader("Host", utf8String(host + ":{}".format(port,)))

        # The Originator must be the ORGANIZER (for a request) or ATTENDEE (for a reply)
        originator = self.scheduler.organizer.cuaddr if self.scheduler.isiTIPRequest else self.scheduler.attendee
        if self.server.rewriteCUAddresses:
            originator = yield normalizeCUAddress(
                originator,
                normalizationLookup,
                self.scheduler.txn.directoryService().recordWithCalendarUserAddress,
                toCanonical=False,
                toURN_UUID=self.server.podding(),
            )
        self.headers.addRawHeader("Originator", utf8String(originator))
        self.sign_headers.append("Originator")

        for recipient in self.recipients:
            cuaddr = recipient.cuaddr
            if self.server.rewriteCUAddresses and self.server.podding():
                cuaddr = yield normalizeCUAddress(
                    cuaddr,
                    normalizationLookup,
                    self.scheduler.txn.directoryService().recordWithCalendarUserAddress,
                    toCanonical=False,
                    toURN_UUID=True,
                )
            self.headers.addRawHeader("Recipient", utf8String(cuaddr))

        # Only one Recipient header as they get concatenated in ischedule-relaxed canonicalization
        self.sign_headers.append("Recipient")

        self._doAuthentication()

        self.headers.setHeader("Content-Type", MimeType(
            "text", "calendar",
            params={
                "charset": "utf-8",
                "component": component,
                "method": method,
            }
        ))
        self.sign_headers.append("Content-Type")

        self.headers.setHeader("User-Agent", "CalendarServer/{}".format(version,))
        self.sign_headers.append("User-Agent")

        # Add any additional headers
        for name, value in self.server.moreHeaders:
            self.headers.addRawHeader(name, value)

        if self.refreshOnly:
            self.headers.addRawHeader("X-CALENDARSERVER-ITIP-REFRESHONLY", "T")

    def _doAuthentication(self):
        if self.server.authentication and self.server.authentication[0] == "basic":
            self.headers.setHeader(
                "Authorization",
                ('Basic', ("{}:{}".format(self.server.authentication[1], self.server.authentication[2],)).encode('base64')[:-1])
            )
            self.sign_headers.append("Authorization")

    @inlineCallbacks
    def _prepareData(self):
        """
        Prepare data via normalization etc. Only need to do this once even when
        redirects occur.
        """

        if self.data is None:

            # Need to remap cuaddrs from urn:uuid
            normalizedCalendar = self.scheduler.calendar.duplicate()
            self.original_organizer = normalizedCalendar.getOrganizer()
            if self.server.rewriteCUAddresses:
                yield normalizedCalendar.normalizeCalendarUserAddresses(
                    normalizationLookup,
                    self.scheduler.txn.directoryService().recordWithCalendarUserAddress,
                    toCanonical=False,
                    toURN_UUID=self.server.podding(),
                )

            # For VFREEBUSY we need to strip out ATTENDEEs that do not match the recipient list
            if self.scheduler.isfreebusy:
                normalizedCalendar.removeAllButTheseAttendees([recipient.cuaddr for recipient in self.recipients])

            component = normalizedCalendar.mainType()
            method = normalizedCalendar.propertyValue("METHOD")
            self.data = str(normalizedCalendar)
            returnValue((component, method,))
        else:
            cal = Component.fromString(self.data)
            component = cal.mainType()
            method = cal.propertyValue("METHOD")
            returnValue((component, method,))

    @inlineCallbacks
    def _processRequest(self, ssl, host, port, path):
        if not self.server.podding() and config.Scheduling.iSchedule.DKIM.Enabled:
            domain, selector, key_file, algorithm, useDNSKey, useHTTPKey, usePrivateExchangeKey, expire = DKIMUtils.getConfiguration(config)
            request = DKIMRequest(
                "POST",
                path,
                self.headers,
                self.data,
                domain,
                selector,
                key_file,
                algorithm,
                self.sign_headers,
                useDNSKey,
                useHTTPKey,
                usePrivateExchangeKey,
                expire,
            )
            yield request.sign()
        else:
            request = ClientRequest("POST", path, self.headers, self.data)

        if accountingEnabledForCategory("iSchedule"):
            self.loggedRequest = yield self.logRequest(request)

        response = yield self._submitRequest(ssl, host, port, request)
        returnValue(response)

    @inlineCallbacks
    def _submitRequest(self, ssl, host, port, request):
        from twisted.internet import reactor
        f = Factory()
        f.protocol = HTTPClientProtocol
        if ssl:
            ep = GAIEndpoint(reactor, host, port, _configuredClientContextFactory(host))
        else:
            ep = GAIEndpoint(reactor, host, port)
        proto = (yield ep.connect(f))

        response = (yield proto.submitRequest(request))

        returnValue(response)

    def _parseResponse(self, xml):

        # Check for correct root element
        schedule_response = xml.root_element
        if not isinstance(schedule_response, ScheduleResponse) or not schedule_response.children:
            raise HTTPError(responsecode.BAD_REQUEST)

        # Parse each response - do this twice: once looking for errors that will
        # result in all recipients shown as failures; the second loop adds all the
        # valid responses to the actual result.
        for response in schedule_response.children:
            if not isinstance(response, Response) or not response.children:
                raise HTTPError(responsecode.BAD_REQUEST)
            recipient = response.childOfType(Recipient)
            request_status = response.childOfType(RequestStatus)
            if not recipient or not request_status:
                raise HTTPError(responsecode.BAD_REQUEST)
        for response in schedule_response.children:
            recipient = str(response.childOfType(Recipient))
            request_status = str(response.childOfType(RequestStatus))
            calendar_data = response.childOfType(CalendarData)
            if calendar_data:
                calendar_data = str(calendar_data)
                if self.server.rewriteCUAddresses and self.original_organizer is not None:
                    # Need to restore original ORGANIZER value if it got unnormalized
                    calendar = Component.fromString(calendar_data)
                    organizers = calendar.getAllPropertiesInAnyComponent("ORGANIZER", depth=1)
                    for organizer in organizers:
                        organizer.setValue(self.original_organizer)
                    calendar_data = str(calendar)

            error = response.childOfType(Error)
            if error:
                error = error.children
            desc = response.childOfType(ResponseDescription)
            if desc:
                desc = str(desc)
            self.responses.clone(
                recipient,
                request_status,
                calendar_data,
                error,
                desc,
            )
