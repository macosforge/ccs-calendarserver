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
from twistedcaldav.config import config

"""
CalDAV MKCALENDAR method.
"""

__all__ = ["http_MKCALENDAR"]

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.failure import Failure
from txweb2 import responsecode
from txdav.xml import element as davxml
from txweb2.dav.http import MultiStatusResponse, PropertyStatusResponseQueue
from txweb2.dav.util import davXMLFromStream
from txweb2.dav.util import parentForURL
from txweb2.http import HTTPError, StatusResponse

from twext.python.log import Logger
from txweb2.dav.http import ErrorResponse

from twistedcaldav import caldavxml

log = Logger()


@inlineCallbacks
def http_MKCALENDAR(self, request):
    """
    Respond to a MKCALENDAR request.
    (CalDAV-access-09, section 5.3.1)
    """

    #
    # Check authentication and access controls
    #
    parent = (yield request.locateResource(parentForURL(request.uri)))
    yield parent.authorize(request, (davxml.Bind(),))

    if self.exists():
        log.error("Attempt to create collection where resource exists: {s!r}", s=self)
        raise HTTPError(ErrorResponse(
            responsecode.FORBIDDEN,
            (davxml.dav_namespace, "resource-must-be-null"),
            "Resource already exists",
        ))

    if not parent.isCollection():
        log.error("Attempt to create collection with non-collection parent: {s!r}", s=self)
        raise HTTPError(ErrorResponse(
            responsecode.CONFLICT,
            (caldavxml.caldav_namespace, "calendar-collection-location-ok"),
            "Cannot create calendar inside another calendar",
        ))

    #
    # Read request body
    #
    try:
        doc = (yield davXMLFromStream(request.stream))
        yield self.createCalendar(request)
    except ValueError as e:
        log.error("Error while handling MKCALENDAR: {ex}", ex=e)
        raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, str(e)))

    set_supported_component_set = False
    if doc is not None:
        makecalendar = doc.root_element
        if not isinstance(makecalendar, caldavxml.MakeCalendar):
            error = ("Non-%s element in MKCALENDAR request body: %s"
                     % (caldavxml.MakeCalendar.name, makecalendar))
            log.error("Error: {err}", err=error)
            raise HTTPError(StatusResponse(responsecode.UNSUPPORTED_MEDIA_TYPE, error))

        errors = PropertyStatusResponseQueue("PROPPATCH", request.uri, responsecode.NO_CONTENT)
        got_an_error = False

        if makecalendar.children:
            # mkcalendar -> set -> prop -> property*
            for property in makecalendar.children[0].children[0].children:
                try:
                    if property.qname() == (caldavxml.caldav_namespace, "supported-calendar-component-set"):
                        yield self.setSupportedComponentSet(property)
                        set_supported_component_set = True
                    else:
                        yield self.writeProperty(property, request)
                except HTTPError:
                    errors.add(Failure(), property)
                    got_an_error = True
                else:
                    errors.add(responsecode.OK, property)

        if got_an_error:
            # Force a transaction error and proper clean-up
            errors.error()
            raise HTTPError(MultiStatusResponse([errors.response()]))

    # When calendar collections are single component only, default MKCALENDAR is VEVENT only
    if not set_supported_component_set and config.RestrictCalendarsToOneComponentType:
        yield self.setSupportedComponents(("VEVENT",))

    returnValue(responsecode.CREATED)
