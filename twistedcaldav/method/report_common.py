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

__all__ = [
    "applyToCalendarCollections",
    "applyToAddressBookCollections",
    "responseForHref",
    "allPropertiesForResource",
    "propertyNamesForResource",
    "propertyListForResource",
    "validPropertyListCalendarDataTypeVersion",
    "validPropertyListAddressDataTypeVersion",
]

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.failure import Failure

from txweb2 import responsecode
from txweb2.dav.http import statusForFailure
from txweb2.dav.method.propfind import propertyName
from txweb2.dav.resource import AccessDeniedError
from txweb2.http import HTTPError

from twext.python.log import Logger

from twistedcaldav import caldavxml
from twistedcaldav import carddavxml
from twistedcaldav.caldavxml import CalendarData
from twistedcaldav.carddavxml import AddressData
from twistedcaldav.datafilters.calendardata import CalendarDataFilter
from twistedcaldav.datafilters.hiddeninstance import HiddenInstanceFilter
from twistedcaldav.datafilters.privateevents import PrivateEventFilter
from twistedcaldav.datafilters.addressdata import AddressDataFilter

from txdav.xml import element

log = Logger()

COLLECTION_TYPE_REGULAR = "collection"
COLLECTION_TYPE_CALENDAR = "calendar"
COLLECTION_TYPE_ADDRESSBOOK = "addressbook"


@inlineCallbacks
def applyToCalendarCollections(resource, request, request_uri, depth, apply, privileges):
    """
    Run an operation on all calendar collections, starting at the specified
    root, to the specified depth. This involves scanning the URI hierarchy
    down from the root. Return a MultiStatus element of all responses.

    @param request: the L{IRequest} for the current request.
    @param resource: the L{CalDAVResource} representing the root to start scanning
        for calendar collections.
    @param depth: the depth to do the scan.
    @param apply: the function to apply to each calendar collection located
        during the scan.
    @param privileges: the privileges that must exist on the calendar collection.
    """

    # First check the privilege on this resource
    if privileges:
        try:
            yield resource.checkPrivileges(request, privileges)
        except AccessDeniedError:
            return

    # When scanning we only go down as far as a calendar collection - not into one
    if resource.isPseudoCalendarCollection():
        resources = [(resource, request_uri)]
    elif not resource.isCollection():
        resources = [(resource, request_uri)]
    else:
        resources = []
        yield resource.findCalendarCollections(depth, request, lambda x, y: resources.append((x, y)), privileges=privileges)

    for calresource, uri in resources:
        result = (yield calresource(*uri))
        if not result:
            break


@inlineCallbacks
def applyToAddressBookCollections(resource, request, request_uri, depth, apply, privileges):
    """
    Run an operation on all address book collections, starting at the specified
    root, to the specified depth. This involves scanning the URI hierarchy
    down from the root. Return a MultiStatus element of all responses.

    @param request: the L{IRequest} for the current request.
    @param resource: the L{CalDAVResource} representing the root to start scanning
        for address book collections.
    @param depth: the depth to do the scan.
    @param apply: the function to apply to each address book collection located
        during the scan.
    @param privileges: the privileges that must exist on the address book collection.
    """

    # First check the privilege on this resource
    if privileges:
        try:
            yield resource.checkPrivileges(request, privileges)
        except AccessDeniedError:
            returnValue(None)

    # When scanning we only go down as far as an address book collection - not into one
    if resource.isAddressBookCollection():
        resources = [(resource, request_uri)]
    elif not resource.isCollection():
        resources = [(resource, request_uri)]
    else:
        resources = []
        yield resource.findAddressBookCollections(depth, request, lambda x, y: resources.append((x, y)), privileges=privileges)

    for addrresource, uri in resources:
        result = yield addrresource(*uri)
        if not result:
            break


def responseForHref(request, responses, href, resource, propertiesForResource, propertyreq, isowner=True, calendar=None, timezone=None, vcard=None):
    """
    Create an appropriate property status response for the given resource.

    @param request: the L{IRequest} for the current request.
    @param responses: the list of responses to append the result of this method to.
    @param href: the L{HRef} element of the resource being targeted.
    @param resource: the L{CalDAVResource} for the targeted resource.
    @param calendar: the L{Component} for the calendar for the resource. This may be None
        if the calendar has not already been read in, in which case the resource
        will be used to get the calendar if needed.
    @param vcard: the L{Component} for the vcard for the resource. This may be None
        if the vcard has not already been read in, in which case the resource
        will be used to get the vcard if needed.

    @param propertiesForResource: the method to use to get the list of
        properties to return.  This is a callable object with a signature
        matching that of L{allPropertiesForResource}.

    @param propertyreq: the L{PropertyContainer} element for the properties of interest.
    @param isowner: C{True} if the authorized principal making the request is the DAV:owner,
        C{False} otherwise.
    """

    def _defer(properties_by_status):
        propstats = []

        for status in properties_by_status:
            properties = properties_by_status[status]
            if properties:
                xml_status = element.Status.fromResponseCode(status)
                xml_container = element.PropertyContainer(*properties)
                xml_propstat = element.PropertyStatus(xml_container, xml_status)

                propstats.append(xml_propstat)

        # Always need to have at least one propstat present (required by Prefer header behavior)
        if len(propstats) == 0:
            propstats.append(element.PropertyStatus(
                element.PropertyContainer(),
                element.Status.fromResponseCode(responsecode.OK)
            ))

        if propstats:
            responses.append(element.PropertyStatusResponse(href, *propstats))

    d = propertiesForResource(request, propertyreq, resource, calendar, timezone, vcard, isowner)
    d.addCallback(_defer)
    return d


def allPropertiesForResource(request, prop, resource, calendar=None, timezone=None, vcard=None, isowner=True):
    """
    Return all (non-hidden) properties for the specified resource.

    @param request: the L{IRequest} for the current request.
    @param prop: the L{PropertyContainer} element for the properties of interest.
    @param resource: the L{CalDAVResource} for the targeted resource.
    @param calendar: the L{Component} for the calendar for the resource. This may be None
        if the calendar has not already been read in, in which case the resource
        will be used to get the calendar if needed.
    @param timezone: the L{Component} the VTIMEZONE to use for floating/all-day.
    @param vcard: the L{Component} for the vcard for the resource. This may be None
        if the vcard has not already been read in, in which case the resource
        will be used to get the vcard if needed.
    @param isowner: C{True} if the authorized principal making the request is the DAV:owner,
        C{False} otherwise.
    @return: a map of OK and NOT FOUND property values.
    """

    def _defer(props):
        return _namedPropertiesForResource(request, props, resource, calendar, timezone, vcard, isowner)

    d = resource.listAllprop(request)
    d.addCallback(_defer)
    return d


def propertyNamesForResource(request, prop, resource, calendar=None, timezone=None, vcard=None, isowner=True):  # @UnusedVariable
    """
    Return property names for all properties on the specified resource.
    @param request: the L{IRequest} for the current request.
    @param prop: the L{PropertyContainer} element for the properties of interest.
    @param resource: the L{CalDAVResource} for the targeted resource.
    @param calendar: the L{Component} for the calendar for the resource. This may be None
        if the calendar has not already been read in, in which case the resource
        will be used to get the calendar if needed.
    @param timezone: the L{Component} the VTIMEZONE to use for floating/all-day.
    @param isowner: C{True} if the authorized principal making the request is the DAV:owner,
        C{False} otherwise.
    @return: a map of OK and NOT FOUND property values.
    """

    def _defer(props):
        properties_by_status = {
            responsecode.OK: [propertyName(p) for p in props]
        }
        return properties_by_status

    d = resource.listProperties(request)
    d.addCallback(_defer)
    return d


def propertyListForResource(request, prop, resource, calendar=None, timezone=None, vcard=None, isowner=True):
    """
    Return the specified properties on the specified resource.
    @param request: the L{IRequest} for the current request.
    @param prop: the L{PropertyContainer} element for the properties of interest.
    @param resource: the L{CalDAVResource} for the targeted resource.
    @param calendar: the L{Component} for the calendar for the resource. This may be None
        if the calendar has not already been read in, in which case the resource
        will be used to get the calendar if needed.
    @param timezone: the L{Component} the VTIMEZONE to use for floating/all-day.
    @param isowner: C{True} if the authorized principal making the request is the DAV:owner,
        C{False} otherwise.
    @return: a map of OK and NOT FOUND property values.
    """

    return _namedPropertiesForResource(request, prop.children, resource, calendar, timezone, vcard, isowner)


def validPropertyListCalendarDataTypeVersion(prop):
    """
    If the supplied prop element includes a calendar-data element, verify that
    the type/version on that matches what we can handle..

    @param prop: the L{PropertyContainer} element for the properties of interest.
    @return:     a tuple: (True/False if the calendar-data element is one we can handle or not present,
                           error message).
    """

    result = True
    message = ""
    generate_calendar_data = False
    for property in prop.children:
        if isinstance(property, caldavxml.CalendarData):
            if not property.verifyTypeVersion():
                result = False
                message = "Calendar-data element type/version not supported: content-type: %s, version: %s" % (property.content_type, property.version)
            generate_calendar_data = True
            break

    return result, message, generate_calendar_data


def validPropertyListAddressDataTypeVersion(prop):
    """
    If the supplied prop element includes an address-data element, verify that
    the type/version on that matches what we can handle..

    @param prop: the L{PropertyContainer} element for the properties of interest.
    @return:     a tuple: (True/False if the address-data element is one we can handle or not present,
                           error message).
    """

    result = True
    message = ""
    generate_address_data = False
    for property in prop.children:
        if isinstance(property, carddavxml.AddressData):
            if not property.verifyTypeVersion():
                result = False
                message = "Address-data element type/version not supported: content-type: %s, version: %s" % (property.content_type, property.version)
            generate_address_data = True
            break

    return result, message, generate_address_data


@inlineCallbacks
def _namedPropertiesForResource(request, props, resource, calendar=None, timezone=None, vcard=None, isowner=True, dataAllowed=True, forbidden=False):
    """
    Return the specified properties on the specified resource.
    @param request: the L{IRequest} for the current request.
    @param props: a list of property elements or qname tuples for the properties of interest.
    @param resource: the L{CalDAVResource} for the targeted resource.
    @param calendar: the L{Component} for the calendar for the resource. This may be None
        if the calendar has not already been read in, in which case the resource
        will be used to get the calendar if needed.
    @param timezone: the L{Component} the VTIMEZONE to use for floating/all-day.
    @param vcard: the L{Component} for the vcard for the resource. This may be None
        if the vcard has not already been read in, in which case the resource
        will be used to get the vcard if needed.
    @param isowner: C{True} if the authorized principal making the request is the DAV:owner,
        C{False} otherwise.
    @param dataAllowed: C{True} if calendar/address data is allowed to be returned,
        C{False} otherwise.
    @param forbidden: if C{True} then return 403 status for all properties,
        C{False} otherwise.
    @return: a map of OK and NOT FOUND property values.
    """
    properties_by_status = {
        responsecode.OK: [],
        responsecode.FORBIDDEN: [],
        responsecode.NOT_FOUND: [],
    }

    # Look for Prefer header first, then try Brief
    prefer = request.headers.getHeader("prefer", {})
    returnMinimal = any([key == "return" and value == "minimal" for key, value, _ignore_args in prefer])
    if not returnMinimal:
        returnMinimal = request.headers.getHeader("brief", False)

    for property in props:
        if isinstance(property, element.WebDAVElement):
            qname = property.qname()
        else:
            qname = property

        if forbidden:
            properties_by_status[responsecode.FORBIDDEN].append(propertyName(qname))
            continue

        if isinstance(property, caldavxml.CalendarData):
            if dataAllowed:
                # Handle private events access restrictions
                if calendar is None:
                    calendar = (yield resource.componentForUser())
                filtered = HiddenInstanceFilter().filter(calendar)
                filtered = PrivateEventFilter(resource.accessMode, isowner).filter(filtered)
                filtered = CalendarDataFilter(property, timezone).filter(filtered)
                propvalue = CalendarData.fromCalendar(filtered, format=property.content_type)
                properties_by_status[responsecode.OK].append(propvalue)
            else:
                properties_by_status[responsecode.FORBIDDEN].append(propertyName(qname))
            continue

        if isinstance(property, carddavxml.AddressData):
            if dataAllowed:
                if vcard is None:
                    vcard = (yield resource.vCard())
                filtered = AddressDataFilter(property).filter(vcard)
                propvalue = AddressData.fromAddress(filtered, format=property.content_type)
                properties_by_status[responsecode.OK].append(propvalue)
            else:
                properties_by_status[responsecode.FORBIDDEN].append(propertyName(qname))
            continue

        has = (yield resource.hasProperty(property, request))

        if has:
            try:
                prop = (yield resource.readProperty(property, request))
                if prop is not None:
                    properties_by_status[responsecode.OK].append(prop)
                elif not returnMinimal:
                    properties_by_status[responsecode.NOT_FOUND].append(propertyName(qname))
            except HTTPError:
                f = Failure()
                status = statusForFailure(f, "getting property: %s" % (qname,))
                if status not in properties_by_status:
                    properties_by_status[status] = []
                if not returnMinimal or status != responsecode.NOT_FOUND:
                    properties_by_status[status].append(propertyName(qname))
        elif not returnMinimal:
            properties_by_status[responsecode.NOT_FOUND].append(propertyName(qname))

    returnValue(properties_by_status)
