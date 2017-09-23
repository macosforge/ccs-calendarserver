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
CalDAV freebusy report
"""

__all__ = ["report_urn_ietf_params_xml_ns_caldav_free_busy_query"]

from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, returnValue, succeed

from txweb2 import responsecode
from txweb2.dav.http import ErrorResponse
from txweb2.dav.method.report import NumberOfMatchesWithinLimits
from txweb2.http import HTTPError, Response, StatusResponse
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream

from twistedcaldav import caldavxml
from twistedcaldav.ical import Component
from twistedcaldav.method import report_common
from twistedcaldav.util import bestAcceptType

from txdav.caldav.icalendarstore import TimeRangeLowerLimit, TimeRangeUpperLimit
from txdav.caldav.datastore.scheduling.cuaddress import LocalCalendarUser
from txdav.caldav.datastore.scheduling.freebusy import FreebusyQuery
from txdav.xml import element as davxml

from pycalendar.period import Period

log = Logger()


@inlineCallbacks
def report_urn_ietf_params_xml_ns_caldav_free_busy_query(self, request, freebusy):
    """
    Generate a free-busy REPORT.
    (CalDAV-access-09, section 7.8)
    """
    if not self.isCollection():
        log.error("freebusy report is only allowed on collection resources {s!r}", s=self)
        raise HTTPError(StatusResponse(responsecode.FORBIDDEN, "Not a calendar collection"))

    if freebusy.qname() != (caldavxml.caldav_namespace, "free-busy-query"):
        raise ValueError("{CalDAV:}free-busy-query expected as root element, not %s." % (freebusy.sname(),))

    timerange = freebusy.timerange
    if not timerange.valid():
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, "Invalid time-range specified"))

    fbset = []

    accepted_type = bestAcceptType(request.headers.getHeader("accept"), Component.allowedTypes())
    if accepted_type is None:
        raise HTTPError(StatusResponse(responsecode.NOT_ACCEPTABLE, "Cannot generate requested data type"))

    def getCalendarList(calresource, uri):  # @UnusedVariable
        """
        Store the calendars that match the query in L{fbset} which will then be used with the
        freebusy query.

        @param calresource: the L{CalDAVResource} for a calendar collection.
        @param uri: the uri for the calendar collection resource.
        """

        fbset.append(calresource._newStoreObject)
        return succeed(True)

    # Run report taking depth into account
    depth = request.headers.getHeader("depth", "0")
    yield report_common.applyToCalendarCollections(self, request, request.uri, depth, getCalendarList, (caldavxml.ReadFreeBusy(),))

    # Do the actual freebusy query against the set of matched calendars
    principal = yield self.resourceOwnerPrincipal(request)
    organizer = recipient = LocalCalendarUser(principal.canonicalCalendarUserAddress(), principal.record)
    timerange = Period(timerange.start, timerange.end)
    try:
        fbresult = yield FreebusyQuery(organizer=organizer, recipient=recipient, timerange=timerange).generateAttendeeFreeBusyResponse(fbset=fbset, method=None)
    except NumberOfMatchesWithinLimits:
        log.error("Too many matching components in free-busy report")
        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            davxml.NumberOfMatchesWithinLimits(),
            "Too many components"
        ))
    except TimeRangeLowerLimit as e:
        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            caldavxml.MinDateTime(),
            "Time-range value too far in the past. Must be on or after %s." % (str(e.limit),)
        ))
    except TimeRangeUpperLimit as e:
        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            caldavxml.MaxDateTime(),
            "Time-range value too far in the future. Must be on or before %s." % (str(e.limit),)
        ))

    response = Response()
    response.stream = MemoryStream(fbresult.getText(accepted_type))
    response.headers.setHeader("content-type", MimeType.fromString("%s; charset=utf-8" % (accepted_type,)))

    returnValue(response)
