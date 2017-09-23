# -*- test-case-name: twistedcaldav.test.test_icalendar -*-
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

"""
iCalendar Utilities
"""

__all__ = [
    "InvalidICalendarDataError",
    "iCalendarProductID",
    "allowedComponents",
    "Property",
    "Component",
    "tzexpand",
]

import codecs
import collections
from difflib import unified_diff
import heapq
import itertools
import uuid

from twisted.internet.defer import inlineCallbacks, returnValue
from twext.python.log import Logger
from txweb2.stream import IStream
from txweb2.dav.util import allDataFromStream

from twistedcaldav.accounting import accountingEnabledForCategory, \
    emitAccounting
from twistedcaldav.config import config
from twistedcaldav.dateops import timeRangesOverlap, normalizeForIndex, differenceDateTime, \
    normalizeForExpand
from twistedcaldav.instance import InstanceList, InvalidOverriddenInstanceError
from twistedcaldav.timezones import hasTZ

from txdav.caldav.datastore.scheduling.utils import normalizeCUAddr

from pycalendar.icalendar import definitions
from pycalendar.parameter import Parameter
from pycalendar.icalendar.calendar import Calendar
from pycalendar.icalendar.component import Component as PyComponent
from pycalendar.componentbase import ComponentBase
from pycalendar.datetime import DateTime
from pycalendar.duration import Duration
from pycalendar.exceptions import ErrorBase
from pycalendar.period import Period
from pycalendar.icalendar.property import Property as PyProperty
from pycalendar.timezone import Timezone
from pycalendar.utcoffsetvalue import UTCOffsetValue

log = Logger()

iCalendarProductID = "-//CALENDARSERVER.ORG//NONSGML Version 1//EN"

allowedStoreComponents = ("VEVENT", "VTODO", "VPOLL",)
allowedSchedulingComponents = allowedStoreComponents + ("VFREEBUSY",)
allowedComponents = allowedSchedulingComponents + ("VTIMEZONE",)


def _updateAllowedComponents(allowed):
    global allowedStoreComponents, allowedSchedulingComponents, allowedComponents
    allowedStoreComponents = allowed
    allowedSchedulingComponents = allowedStoreComponents + ("VFREEBUSY",)
    allowedComponents = allowedSchedulingComponents + ("VTIMEZONE",)


# Additional per-user data components - see datafilters.peruserdata.py for details
PERUSER_COMPONENT = "X-CALENDARSERVER-PERUSER"
PERUSER_UID = "X-CALENDARSERVER-PERUSER-UID"
PERINSTANCE_COMPONENT = "X-CALENDARSERVER-PERINSTANCE"

PRIVATE_COMMENT = "X-CALENDARSERVER-PRIVATE-COMMENT"
ATTENDEE_COMMENT = "X-CALENDARSERVER-ATTENDEE-COMMENT"
ATTENDEE_COMMENT_REF = "X-CALENDARSERVER-ATTENDEE-REF"
DTSTAMP_PARAM = "X-CALENDARSERVER-DTSTAMP"

# 2445 default values and parameters
# Structure: propname: (<default value>, <parameter defaults dict>)

normalizeProps = {
    "CALSCALE": ("GREGORIAN", {"VALUE": "TEXT"}),
    "METHOD": (None, {"VALUE": "TEXT"}),
    "PRODID": (None, {"VALUE": "TEXT"}),
    "VERSION": (None, {"VALUE": "TEXT"}),
    "ATTACH": (None, {"VALUE": "URI"}),
    "CATEGORIES": (None, {"VALUE": "TEXT"}),
    "CLASS": (None, {"VALUE": "TEXT"}),
    "COMMENT": (None, {"VALUE": "TEXT"}),
    "DESCRIPTION": (None, {"VALUE": "TEXT"}),
    "GEO": (None, {"VALUE": "FLOAT"}),
    "LOCATION": (None, {"VALUE": "TEXT"}),
    "PERCENT-COMPLETE": (None, {"VALUE": "INTEGER"}),
    "PRIORITY": (0, {"VALUE": "INTEGER"}),
    "RESOURCES": (None, {"VALUE": "TEXT"}),
    "STATUS": (None, {"VALUE": "TEXT"}),
    "SUMMARY": (None, {"VALUE": "TEXT"}),
    "COMPLETED": (None, {"VALUE": "DATE-TIME"}),
    "DTEND": (None, {"VALUE": "DATE-TIME"}),
    "DUE": (None, {"VALUE": "DATE-TIME"}),
    "DTSTART": (None, {"VALUE": "DATE-TIME"}),
    "DURATION": (None, {"VALUE": "DURATION"}),
    "FREEBUSY": (None, {"VALUE": "PERIOD"}),
    "TRANSP": ("OPAQUE", {"VALUE": "TEXT"}),
    "TZID": (None, {"VALUE": "TEXT"}),
    "TZNAME": (None, {"VALUE": "TEXT"}),
    "TZOFFSETFROM": (None, {"VALUE": "UTC-OFFSET"}),
    "TZOFFSETTO": (None, {"VALUE": "UTC-OFFSET"}),
    "TZURL": (None, {"VALUE": "URI"}),
    "ATTENDEE": (None, {
        "VALUE": "CAL-ADDRESS",
        "CUTYPE": "INDIVIDUAL",
        "ROLE": "REQ-PARTICIPANT",
        "PARTSTAT": "NEEDS-ACTION",
        "RSVP": "FALSE",
        "SCHEDULE-AGENT": "SERVER",
    }),
    "CONTACT": (None, {"VALUE": "TEXT"}),
    "ORGANIZER": (None, {"VALUE": "CAL-ADDRESS"}),
    "RECURRENCE-ID": (None, {"VALUE": "DATE-TIME"}),
    "RELATED-TO": (None, {"VALUE": "TEXT"}),
    "URL": (None, {"VALUE": "URI"}),
    "UID": (None, {"VALUE": "TEXT"}),
    "EXDATE": (None, {"VALUE": "DATE-TIME"}),
    "EXRULE": (None, {"VALUE": "RECUR"}),
    "RDATE": (None, {"VALUE": "DATE-TIME"}),
    "RRULE": (None, {"VALUE": "RECUR"}),
    "ACTION": (None, {"VALUE": "TEXT"}),
    "REPEAT": (0, {"VALUE": "INTEGER"}),
    "TRIGGER": (None, {"VALUE": "DURATION"}),
    "CREATED": (None, {"VALUE": "DATE-TIME"}),
    "DTSTAMP": (None, {"VALUE": "DATE-TIME"}),
    "LAST-MODIFIED": (None, {"VALUE": "DATE-TIME"}),
    "SEQUENCE": (0, {"VALUE": "INTEGER"}),
    "REQUEST-STATUS": (None, {"VALUE": "TEXT"}),

    "VOTER": (None, {
        "VALUE": "CAL-ADDRESS",
        "CUTYPE": "INDIVIDUAL",
        "ROLE": "REQ-PARTICIPANT",
        "RSVP": "FALSE",
        "SCHEDULE-AGENT": "SERVER",
    }),
}

# transformations to apply to property values
normalizePropsValue = {
    "ATTENDEE": normalizeCUAddr,
    "ORGANIZER": normalizeCUAddr,
    "VOTER": normalizeCUAddr,
}

ignoredComponents = ("VTIMEZONE", PERUSER_COMPONENT,)

# Used for min/max time-range query limits
minDateTime = DateTime(1900, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
maxDateTime = DateTime(2100, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)


class InvalidICalendarDataError(ValueError):
    pass


class Property (object):
    """
    iCalendar Property
    """

    def __init__(self, name, value, params={}, parent=None, **kwargs):
        """
        @param name: the property's name
        @param value: the property's value
        @param params: a dictionary of parameters, where keys are parameter names and
            values are (possibly empty) lists of parameter values.
        """
        if name is None:
            assert value is None
            assert params is None

            pyobj = kwargs["pycalendar"]

            if not isinstance(pyobj, PyProperty):
                raise TypeError("Not a Property: {0!r}".format(property,))

            self._pycalendar = pyobj
        else:
            # Convert params dictionary to list of lists format used by pycalendar
            valuetype = kwargs.get("valuetype")
            self._pycalendar = PyProperty(name, value, valuetype=valuetype)
            for attrname, attrvalue in params.items():
                self._pycalendar.addParameter(Parameter(attrname, attrvalue))

        self._parent = parent

    def __str__(self):
        return str(self._pycalendar)

    def __repr__(self):
        return (
            "<{self.__class__.__name__}: {name!r}: {value!r}>"
            .format(self=self, name=self.name(), value=self.value())
        )

    def __hash__(self):
        return hash(str(self))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if not isinstance(other, Property):
            return False
        return self._pycalendar == other._pycalendar

    def __gt__(self, other):
        return not (self.__eq__(other) or self.__lt__(other))

    def __lt__(self, other):
        my_name = self.name()
        other_name = other.name()

        if my_name < other_name:
            return True
        if my_name > other_name:
            return False

        return self.value() < other.value()

    def __ge__(self, other):
        return self.__eq__(other) or self.__gt__(other)

    def __le__(self, other):
        return self.__eq__(other) or self.__lt__(other)

    def duplicate(self):
        """
        Duplicate this object and all its contents.
        @return: the duplicated calendar.
        """
        # FIXME: does the parent need to be set in this case?
        return Property(None, None, None, pycalendar=self._pycalendar.duplicate())

    def name(self):
        return self._pycalendar.getName()

    def value(self):
        return self._pycalendar.getValue().getValue()

    def strvalue(self):
        return str(self._pycalendar.getValue())

    def _markAsDirty(self):
        parent = getattr(self, "_parent", None)
        if parent is not None:
            parent._markAsDirty()

    def setValue(self, value):
        self._pycalendar.setValue(value)
        self._markAsDirty()

    def parameterNames(self):
        """
        Returns a set containing parameter names for this property.
        """
        result = set()
        for pyattrlist in self._pycalendar.getParameters().values():
            for pyattr in pyattrlist:
                result.add(pyattr.getName())
        return result

    def parameterValue(self, name, default=None):
        """
        Returns a single value for the given parameter.
        """
        try:
            return self._pycalendar.getParameterValue(name)
        except KeyError:
            return default

    def parameterValues(self, name, default=None):
        """
        Returns a multi-value C{list} for the given parameter.
        """
        try:
            return self._pycalendar.getParameterValues(name)
        except KeyError:
            return default

    def hasParameter(self, paramname):
        return self._pycalendar.hasParameter(paramname)

    def setParameter(self, paramname, paramvalue):
        self._pycalendar.replaceParameter(Parameter(paramname, paramvalue))
        self._markAsDirty()

    def removeParameter(self, paramname):
        self._pycalendar.removeParameters(paramname)
        self._markAsDirty()

    def removeAllParameters(self):
        self._pycalendar.setParameters({})
        self._markAsDirty()

    def removeParameterValue(self, paramname, paramvalue):

        paramname = paramname.upper()
        for attrName in self.parameterNames():
            if attrName.upper() == paramname:
                for attr in tuple(self._pycalendar.getParameters()[attrName]):
                    for value in attr.getValues():
                        if value == paramvalue:
                            if not attr.removeValue(value):
                                self._pycalendar.removeParameters(paramname)
        self._markAsDirty()

    def containsTimeRange(self, start, end, defaulttz=None):
        """
        Determines whether this property contains a date/date-time within the specified
        start/end period.
        The only properties allowed for this query are: COMPLETED, CREATED, DTSTAMP and
        LAST-MODIFIED (caldav -09).
        @param start: a L{DateTime} specifying the beginning of the given time span.
        @param end: a L{DateTime} specifying the end of the given time span.
            C{end} may be None, indicating that there is no end date.
        @param defaulttz: the default L{PyTimezone} to use in datetime comparisons.
        @return: True if the property's date/date-time value is within the given time range,
                 False if not, or the property is not an appropriate date/date-time value.
        """

        # Verify that property name matches the ones allowed
        allowedNames = ["COMPLETED", "CREATED", "DTSTAMP", "LAST-MODIFIED"]
        if self.name() not in allowedNames:
            return False

        # get date/date-time value
        dt = self._pycalendar.getValue().getValue()
        assert isinstance(dt, DateTime), "Not a date/date-time value: {0!r}".format(self,)

        return timeRangesOverlap(dt, None, start, end, defaulttz)


class Component (object):
    """
    X{iCalendar} component.
    """

    # Private Event access levels.
    ACCESS_PROPERTY = "X-CALENDARSERVER-ACCESS"
    ACCESS_PUBLIC = "PUBLIC"
    ACCESS_PRIVATE = "PRIVATE"
    ACCESS_CONFIDENTIAL = "CONFIDENTIAL"
    ACCESS_RESTRICTED = "RESTRICTED"

    accessMap = {
        "PUBLIC": ACCESS_PUBLIC,
        "PRIVATE": ACCESS_PRIVATE,
        "CONFIDENTIAL": ACCESS_CONFIDENTIAL,
        "RESTRICTED": ACCESS_RESTRICTED,
    }

    confidentialPropertiesMap = {
        "VCALENDAR": ("PRODID", "VERSION", "CALSCALE", ACCESS_PROPERTY),
        "VEVENT": ("UID", "RECURRENCE-ID", "SEQUENCE", "DTSTAMP", "STATUS", "TRANSP", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXRULE", "EXDATE",),
        "VTODO": ("UID", "RECURRENCE-ID", "SEQUENCE", "DTSTAMP", "STATUS", "DTSTART", "COMPLETED", "DUE", "DURATION", "RRULE", "RDATE", "EXRULE", "EXDATE",),
        "VJOURNAL": ("UID", "RECURRENCE-ID", "SEQUENCE", "DTSTAMP", "STATUS", "DTSTART", "RRULE", "RDATE", "EXRULE", "EXDATE",),
        "VFREEBUSY": ("UID", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "FREEBUSY",),
        "VTIMEZONE": None,
    }
    extraRestrictedProperties = ("SUMMARY", "LOCATION",)

    # Hidden instance.
    HIDDEN_INSTANCE_PROPERTY = "X-CALENDARSERVER-HIDDEN-INSTANCE"

    allowedTypesList = None

    @classmethod
    def allowedTypes(cls):
        if cls.allowedTypesList is None:
            cls.allowedTypesList = ["text/calendar"]
            if config.EnableJSONData:
                cls.allowedTypesList.append("application/calendar+json")
        return cls.allowedTypesList

    @classmethod
    def allFromString(clazz, string, format=None):
        """
        Just default to reading a single VCALENDAR
        """
        return clazz.fromString(string, format)

    @classmethod
    def allFromStream(clazz, stream, format=None):
        """
        Just default to reading a single VCALENDAR
        """
        return clazz.fromStream(stream, format)

    @classmethod
    def fromString(clazz, string, format=None):
        """
        Construct a L{Component} from a string.
        @param string: a string containing iCalendar data.
        @return: a L{Component} representing the first component described by
            C{string}.
        """
        return clazz._fromData(string, False, format)

    @classmethod
    def fromStream(clazz, stream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: a C{read()}able stream containing iCalendar data.
        @return: a L{Component} representing the first component described by
            C{stream}.
        """
        return clazz._fromData(stream, True, format)

    @classmethod
    def _fromData(clazz, data, isstream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: a C{read()}able stream containing iCalendar data.
        @param format: a C{str} indicating whether the data is iCalendar or jCal
        @return: a L{Component} representing the first component described by
            C{stream}.
        """

        if isstream:
            pass
        else:
            if type(data) is unicode:
                data = data.encode("utf-8")
            else:
                # Valid utf-8 please
                data.decode("utf-8")

            # No BOMs please
            if data[:3] == codecs.BOM_UTF8:
                data = data[3:]

        errmsg = "Unknown"
        try:
            result = Calendar.parseData(data, format)
        except ErrorBase as e:
            errmsg = "{0}: {1}".format(e.mReason, e.mData,)
            result = None
        if not result:
            if isstream:
                data.seek(0)
                data = data.read()
            raise InvalidICalendarDataError("{0}\n{1}".format(errmsg, data,))
        return clazz(None, pycalendar=result)

    @classmethod
    def fromIStream(clazz, stream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: an L{IStream} containing iCalendar data.
        @return: a deferred returning a L{Component} representing the first
            component described by C{stream}.
        """
        #
        # FIXME:
        #   This reads the request body into a string and then parses it.
        #   A better solution would parse directly and incrementally from the
        #   request stream.
        #
        def parse(data):
            return clazz.fromString(data, format)
        return allDataFromStream(IStream(stream), parse)

    @classmethod
    def componentsFromData(cls, data, format):
        """
        Need to split a single VCALENDAR in text form into separate ones based
        on UID with the appropriate VTIEMZONES included.
        """

        # Split into components by UID and TZID
        try:
            vcal = cls.fromString(data, format)
        except InvalidICalendarDataError:
            return None

        return cls.componentsFromComponent(vcal)

    @classmethod
    def componentsFromComponent(cls, sourceComponent):
        """
        Need to split a single VCALENDAR in Component form into separate ones
        based on UID with the appropriate VTIEMZONES included.
        """

        results = []

        by_uid = collections.OrderedDict()
        by_tzid = {}
        for subcomponent in sourceComponent.subcomponents():
            if subcomponent.name() == "VTIMEZONE":
                by_tzid[subcomponent.propertyValue("TZID")] = subcomponent
            else:
                by_uid.setdefault(subcomponent.propertyValue("UID"), []).append(subcomponent)

        # Re-constitute as separate VCALENDAR objects
        for components in by_uid.values():

            newvcal = cls("VCALENDAR")
            newvcal.addProperty(Property("VERSION", "2.0"))
            newvcal.addProperty(Property("PRODID", sourceComponent.propertyValue("PRODID")))

            # Get the set of TZIDs and include them
            tzids = set()
            for component in components:
                tzids.update(component.timezoneIDs())
            for tzid in tzids:
                try:
                    tz = by_tzid[tzid]
                    newvcal.addComponent(tz.duplicate())
                except KeyError:
                    # We ignore the error and generate invalid ics which someone will
                    # complain about at some point
                    pass

            # Now add each component
            for component in components:
                newvcal.addComponent(component.duplicate())

            results.append(newvcal)

        return results

    @classmethod
    def newCalendar(cls):
        """
        Create and return an empty C{VCALENDAR} component.

        @return: a new C{VCALENDAR} component with appropriate metadata
            properties already set (version, product ID).
        @rtype: an instance of this class
        """
        self = cls("VCALENDAR")
        self.addProperty(Property("VERSION", "2.0"))
        self.addProperty(Property("PRODID", iCalendarProductID))
        return self

    def __init__(self, name, **kwargs):
        """
        Use this constructor to initialize an empty L{Component}.
        To create a new L{Component} from X{iCalendar} data, don't use this
        constructor directly.  Use one of the factory methods instead.
        @param name: the name (L{str}) of the X{iCalendar} component type for the
            component.
        """
        if name is None:
            if "pycalendar" in kwargs:
                pyobj = kwargs["pycalendar"]

                if pyobj is not None:
                    if not isinstance(pyobj, ComponentBase):
                        raise TypeError("Not a ComponentBase: {0!r}".format(pyobj,))

                self._pycalendar = pyobj
            else:
                raise AssertionError("name may not be None")

            if "parent" in kwargs:
                parent = kwargs["parent"]

                if parent is not None:
                    if not isinstance(parent, Component):
                        raise TypeError("Not a Component: {0!r}".format(parent,))

                self._parent = parent
            else:
                self._parent = None
        else:
            # FIXME: figure out creating an arbitrary component
            self._pycalendar = Calendar(add_defaults=False) if name == "VCALENDAR" else PyComponent.makeComponent(name, None)
            self._parent = None

    def __str__(self):
        """
        NB This does not automatically include timezones in VCALENDAR objects.
        """
        cachedCopy = getattr(self, "_cachedCopy", None)
        if cachedCopy is not None:
            return cachedCopy
        self._cachedCopy = str(self._pycalendar)
        return self._cachedCopy

    def _markAsDirty(self):
        """
        Invalidate the cached copy of serialized icalendar data
        """
        self._cachedCopy = None
        parent = getattr(self, "_parent", None)
        if parent is not None:
            parent._markAsDirty()

    def __repr__(self):
        return (
            "<{self.__class__.__name__}: {pycal!r}>"
            .format(self=self, pycal=str(self._pycalendar))
        )

    def __hash__(self):
        return hash(str(self))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if not isinstance(other, Component):
            return False
        return self._pycalendar == other._pycalendar

    def getText(self, format=None):
        """
        Return text representation and include non-standard timezones.
        """
        return self._getTextWithTimezones(includeTimezones=Calendar.NONSTD_TIMEZONES, format=format)

    def getTextWithTimezones(self, includeTimezones, format=None):
        """
        Return text representation and include timezones if the option is on.
        """
        includeTimezones = Calendar.ALL_TIMEZONES if includeTimezones else Calendar.NONSTD_TIMEZONES
        return self._getTextWithTimezones(includeTimezones=includeTimezones, format=format)

    def getTextWithoutTimezones(self, format=None):
        """
        Return text representation without including timezones.
        """
        return self._getTextWithTimezones(includeTimezones=Calendar.NO_TIMEZONES, format=format)

    def _getTextWithTimezones(self, includeTimezones, format=None):
        """
        Return text representation and include timezones if the option is on.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        result = self._pycalendar.getText(includeTimezones=includeTimezones, format=format)
        if result is None:
            raise ValueError("Unknown format requested for calendar data.")
        return result

    # FIXME: Should this not be in __eq__?
    def same(self, other):
        return self._pycalendar == other._pycalendar

    def name(self):
        """
        @return: the name of the iCalendar type of this component.
        """
        return self._pycalendar.getType()

    def ignored(self):
        """
        @return: where this component is one of the L{ignoredComponents}.
        @rtype: L{bool}
        """
        return self.name() in ignoredComponents

    def mainType(self):
        """
        Determine the primary type of iCal component in this calendar.
        @return: the name of the primary type.
        @raise: L{InvalidICalendarDataError} if there is more than one primary type.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        mtype = None
        for component in self.subcomponents(ignore=True):
            if mtype and (mtype != component.name()):
                raise InvalidICalendarDataError("Component contains more than one type of primary type: {0!r}".format(self,))
            else:
                mtype = component.name()

        return mtype

    def mainComponent(self):
        """
        Return the primary iCal component in this calendar. If a master component exists, use that,
        otherwise use the first override.

        @return: the L{Component} of the primary type.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        result = None
        for component in self.subcomponents(ignore=True):
            if not component.hasProperty("RECURRENCE-ID"):
                return component
            elif result is None:
                result = component

        return result

    def masterComponent(self):
        """
        Return the master iCal component in this calendar.
        @return: the L{Component} for the master component,
            or C{None} if there isn't one.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        for component in self.subcomponents(ignore=True):
            if not component.hasProperty("RECURRENCE-ID"):
                return component

        return None

    def overriddenComponent(self, recurrence_id):
        """
        Return the overridden iCal component in this calendar matching the supplied RECURRENCE-ID property.
        This also returns the matching master component if recurrence_id is C{None}.

        @param recurrence_id: The RECURRENCE-ID property value to match.
        @type recurrence_id: L{DateTime}
        @return: the L{Component} for the overridden component,
            or C{None} if there isn't one.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        if isinstance(recurrence_id, str):
            recurrence_id = DateTime.parseText(recurrence_id) if recurrence_id else None

        for component in self.subcomponents(ignore=True):
            rid = component.getRecurrenceIDUTC()
            if rid and recurrence_id and rid == recurrence_id:
                return component
            elif rid is None and recurrence_id is None:
                return component

        return None

    def accessLevel(self, default=ACCESS_PUBLIC):
        """
        Return the access level for this component.
        @return: the access level for the calendar data.
        """
        assert self.name() == "VCALENDAR", "Must be a VCALENDAR: {0!r}".format(self,)

        access = self.propertyValue(Component.ACCESS_PROPERTY)
        if access:
            access = access.upper()
        return Component.accessMap.get(access, default)

    def duplicate(self):
        """
        Duplicate this object and all its contents.
        @return: the duplicated calendar.
        """
        result = Component(None, pycalendar=self._pycalendar.duplicate())
        if hasattr(self, "noInstanceIndexing"):
            result.noInstanceIndexing = self.noInstanceIndexing
        return result

    def subcomponents(self, ignore=False):
        """
        @return: an iterable of L{Component} objects, one for each subcomponent
            of this component.
        """
        return (
            Component(None, pycalendar=c, parent=self)
            for c in self._pycalendar.getComponents()
            if not ignore or (c.getType() not in ignoredComponents)
        )

    def addComponent(self, component):
        """
        Adds a subcomponent to this component.
        @param component: the L{Component} to add as a subcomponent of this
            component.
        """
        self._pycalendar.addComponent(component._pycalendar)
        component._parent = self
        self._markAsDirty()

    def removeComponent(self, component):
        """
        Removes a subcomponent from this component.
        @param component: the L{Component} to remove.
        """
        self._pycalendar.removeComponent(component._pycalendar)
        component._parent = None
        self._markAsDirty()

    def hasProperty(self, name):
        """
        @param name: the name of the property whose existence is being tested.
        @return: True if the named property exists, False otherwise.
        """
        return self._pycalendar.hasProperty(name)

    def getProperty(self, name):
        """
        Get one property from the property list.
        @param name: the name of the property to get.
        @return: the L{Property} found or None.
        @raise: L{InvalidICalendarDataError} if there is more than one property of the given name.
        """
        properties = tuple(self.properties(name))
        if len(properties) == 1:
            return properties[0]
        if len(properties) > 1:
            raise InvalidICalendarDataError("More than one {0} property in component {1!r}".format(name, self))
        return None

    def properties(self, name=None):
        """
        @param name: if given and not C{None}, restricts the returned properties
            to those with the given C{name}.
        @return: an iterable of L{Property} objects, one for each property of
            this component.
        """
        properties = []
        if name is None:
            [properties.extend(i) for i in self._pycalendar.getProperties().values()]
        elif self._pycalendar.countProperty(name) > 0:
            properties = self._pycalendar.getProperties(name)

        return (
            Property(None, None, None, parent=self, pycalendar=p)
            for p in properties
        )

    def propertyValue(self, name, default=None):
        properties = tuple(self.properties(name))
        if len(properties) == 1:
            return properties[0].value()
        if len(properties) > 1:
            raise InvalidICalendarDataError("More than one {0} property in component {1!r}".format(name, self))
        return default

    def getStartDateUTC(self):
        """
        Return the start date or date-time for the specified component
        converted to UTC.
        @param component: the Component whose start should be returned.
        @return: the L{DateTime} for the start.
        """
        dtstart = self.propertyValue("DTSTART")
        return dtstart.duplicateAsUTC() if dtstart is not None else None

    def getEndDateUTC(self):
        """
        Return the end date or date-time for the specified component,
        taking into account the presence or absence of DTEND/DURATION properties.
        The returned date-time is converted to UTC.
        @param component: the Component whose end should be returned.
        @return: the L{DateTime} for the end.
        """
        dtend = self.propertyValue("DTEND")
        if dtend is None:
            dtstart = self.propertyValue("DTSTART")
            duration = self.propertyValue("DURATION")
            if duration is not None:
                dtend = dtstart + duration

        return dtend.duplicateAsUTC() if dtend is not None else None

    def getDueDateUTC(self):
        """
        Return the due date or date-time for the specified component
        converted to UTC. Use DTSTART/DURATION if no DUE property.
        @param component: the Component whose start should be returned.
        @return: the L{DateTime} for the start.
        """
        due = self.propertyValue("DUE")
        if due is None:
            dtstart = self.propertyValue("DTSTART")
            duration = self.propertyValue("DURATION")
            if dtstart is not None and duration is not None:
                due = dtstart + duration

        return due.duplicateAsUTC() if due is not None else None

    def getCompletedDateUTC(self):
        """
        Return the completed date or date-time for the specified component
        converted to UTC.
        @param component: the Component whose start should be returned.
        @return: the datetime.date or datetime.datetime for the start.
        """
        completed = self.propertyValue("COMPLETED")
        return completed.duplicateAsUTC() if completed is not None else None

    def getCreatedDateUTC(self):
        """
        Return the created date or date-time for the specified component
        converted to UTC.
        @param component: the Component whose start should be returned.
        @return: the datetime.date or datetime.datetime for the start.
        """
        created = self.propertyValue("CREATED")
        return created.duplicateAsUTC() if created is not None else None

    def getRecurrenceIDUTC(self):
        """
        Return the recurrence-id for the specified component.
        @param component: the Component whose r-id should be returned.
        @return: the L{DateTime} for the r-id.
        """
        rid = self.propertyValue("RECURRENCE-ID")
        return rid.duplicateAsUTC() if rid is not None else None

    def getRange(self):
        """
        Determine whether a RANGE=THISANDFUTURE parameter is present
        on any RECURRENCE-ID property.
        @return: True if the parameter is present, False otherwise.
        """
        ridprop = self.getProperty("RECURRENCE-ID")
        if ridprop is not None:
            range = ridprop.parameterValue("RANGE")
            if range is not None:
                return (range == "THISANDFUTURE")

        return False

    def getExdates(self):
        """
        Get the set of all EXDATEs in this (master) component.
        """
        exdates = set()
        for property in self.properties("EXDATE"):
            for exdate in property.value():
                exdates.add(exdate.getValue())
        return exdates

    def getTriggerDetails(self):
        """
        Return the trigger information for the specified alarm component.
        @param component: the Component whose start should be returned.
        @return: a tuple consisting of:
            trigger : the 'native' trigger value
            related : either True (for START) or False (for END)
            repeat : an integer for the REPEAT count
            duration: the repeat duration if present, otherwise None
        """
        assert self.name() == "VALARM", "Component is not a VAlARM: {0!r}".format(self,)

        # The trigger value
        trigger = self.propertyValue("TRIGGER")
        if trigger is None:
            raise InvalidICalendarDataError("VALARM has no TRIGGER property: {0!r}".format(self,))

        # The related parameter
        related = self.getProperty("TRIGGER").parameterValue("RELATED")
        if related is None:
            related = True
        else:
            related = (related == "START")

        # Repeat property
        repeat = self.propertyValue("REPEAT")
        if repeat is None:
            repeat = 0
        else:
            repeat = int(repeat)

        # Duration property
        duration = self.propertyValue("DURATION")

        if repeat > 0 and duration is None:
            raise InvalidICalendarDataError("VALARM has invalid REPEAT/DURATIOn properties: {0!r}".format(self,))

        return (trigger, related, repeat, duration)

    def getRecurrenceSet(self):
        return self._pycalendar.getRecurrenceSet()

    def getEffectiveStartEnd(self):
        # Get the start/end range needed for instance comparisons

        if self.name() in ("VEVENT", "VJOURNAL",):
            return self.getStartDateUTC(), self.getEndDateUTC()
        elif self.name() == "VTODO":
            start = self.getStartDateUTC()
            due = self.getDueDateUTC()
            if start is None and due is not None:
                return due, due
            else:
                return start, due
        else:
            return None, None

    def getFBType(self):

        # Only VEVENTs block time
        if self.name() not in ("VEVENT",):
            return "FREE"

        # Handle status
        status = self.propertyValue("STATUS")
        if status == "CANCELLED":
            return "FREE"
        elif status == "TENTATIVE":
            return "BUSY-TENTATIVE"
        else:
            return "BUSY"

    def addProperty(self, property):
        """
        Adds a property to this component.
        @param property: the L{Property} to add to this component.
        """
        self._pycalendar.addProperty(property._pycalendar)
        self._pycalendar.finalise()
        property._parent = self
        self._markAsDirty()

    def removeProperty(self, property):
        """
        Remove a property from this component.
        @param property: the L{Property} to remove from this component.
        """

        if isinstance(property, str):
            for property in tuple(self.properties(property)):
                self.removeProperty(property)
        else:
            self._pycalendar.removeProperty(property._pycalendar)
            self._pycalendar.finalise()
            property._parent = None
            self._markAsDirty()

    def removeProperties(self, name):
        """
        remove all properties with name
        @param name: the name of the properties to remove.
        """
        self._pycalendar.removeProperties(name)
        self._pycalendar.finalise()
        self._markAsDirty()

    def removeAllPropertiesWithName(self, pname):
        """
        Remove all properties with the given name from all components.

        @param pname: the property name to remove from all components.
        @type pname: C{str}
        """

        for property in tuple(self.properties(pname)):
            self.removeProperty(property)

        for component in self.subcomponents():
            component.removeAllPropertiesWithName(pname)

    def replaceProperty(self, property):
        """
        Add or replace a property in this component.
        @param property: the L{Property} to add or replace in this component.
        """

        # Remove all existing ones first
        self._pycalendar.removeProperties(property.name())
        self.addProperty(property)
        self._markAsDirty()

    def timezoneIDs(self):
        """
        Returns the set of TZID parameter values appearing in any property in
        this component.
        @return: a set of strings, one for each unique TZID value.
        """
        result = set()

        if self.name() == "VCALENDAR":
            for component in self.subcomponents():
                if component.name() != "VTIMEZONE":
                    result.update(component.timezoneIDs())
        else:
            for property in self.properties():
                tzid = property.parameterValue("TZID")
                if tzid is not None:
                    result.add(tzid)
                    break

        return result

    def timezones(self):
        """
        Returns the set of TZID's for each VTIMEZONE component.

        @return: a set of strings, one for each unique TZID value.
        """

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        results = set()
        for component in self.subcomponents():
            if component.name() == "VTIMEZONE":
                results.add(component.propertyValue("TZID"))

        return results

    def truncateRecurrence(self, maximumCount):
        """
        Truncate RRULEs etc to make sure there are no more than the given number
        of instances.

        @param maximumCount: the maximum number of instances to allow
        @type maximumCount: C{int}
        @return: a C{bool} indicating whether a change was made or not
        """

        changed = False
        master = self.masterComponent()
        if master and master.isRecurring():
            rrules = master._pycalendar.getRecurrenceSet()
            if rrules:
                for rrule in rrules.getRules():
                    if rrule.getUseCount():
                        # Make sure COUNT is less than the limit
                        if rrule.getCount() > maximumCount:
                            rrule.setCount(maximumCount)
                            changed = True
                    elif rrule.getUseUntil():
                        # Need to figure out how to determine number of instances
                        # with this UNTIL and truncate if needed
                        start = master.getStartDateUTC()
                        diff = differenceDateTime(start, rrule.getUntil())
                        diff = diff.getDays() * 24 * 60 * 60 + diff.getSeconds()

                        period = {
                            definitions.eRecurrence_YEARLY: 365 * 24 * 60 * 60,
                            definitions.eRecurrence_MONTHLY: 30 * 24 * 60 * 60,
                            definitions.eRecurrence_WEEKLY: 7 * 24 * 60 * 60,
                            definitions.eRecurrence_DAILY: 1 * 24 * 60 * 60,
                            definitions.eRecurrence_HOURLY: 60 * 60,
                            definitions.eRecurrence_MINUTELY: 60,
                            definitions.eRecurrence_SECONDLY: 1
                        }[rrule.getFreq()] * rrule.getInterval()

                        if diff / period > maximumCount:
                            rrule.setUseUntil(False)
                            rrule.setUseCount(True)
                            rrule.setCount(maximumCount)
                            rrules.changed()
                            changed = True
                    else:
                        # For frequencies other than yearly we will truncate at our limit
                        if rrule.getFreq() != definitions.eRecurrence_YEARLY:
                            rrule.setUseCount(True)
                            rrule.setCount(maximumCount)
                            rrules.changed()
                            changed = True

        if changed:
            self._markAsDirty()
        return changed

    def onlyPastInstances(self, rid):
        """
        Remove all recurrence instances at or beyond the specified recurrence-id. Adjust the bounds of any RRULEs
        to match the new limit, remove RDATEs/EXDATEs and overridden components beyond the limit.

        @param rid: the recurrence-id limit
        @type rid: L{DateTime}
        """

        if not self.isRecurring():
            return
        master = self.masterComponent()
        if master:
            # Adjust any RRULE first
            rrules = master._pycalendar.getRecurrenceSet()
            if rrules:
                for rrule in rrules.getRules():
                    rrule.setUseUntil(True)
                    rrule.setUseCount(False)
                    until = rid.duplicate()
                    if until.isDateOnly():
                        until.offsetDay(-1)
                    else:
                        until.offsetSeconds(-1)
                    rrule.setUntil(until)

            # Remove any RDATEs or EXDATEs in the future
            for property in list(itertools.chain(
                master.properties("RDATE"),
                master.properties("EXDATE"),
            )):
                for value in list(property.value()):
                    if value.getValue() >= rid:
                        property.value().remove(value)
                if len(property.value()) == 0:
                    master.removeProperty(property)

        # Remove overrides in the future
        for component in list(self.subcomponents(ignore=True)):
            c_rid = component.getRecurrenceIDUTC()
            if c_rid is not None and c_rid >= rid:
                self.removeComponent(component)

        # Handle per-user data component by removing ones in the future
        for component in list(self.subcomponents()):
            if component.name() == PERUSER_COMPONENT:
                for subcomponent in list(component.subcomponents()):
                    c_rid = subcomponent.getRecurrenceIDUTC()
                    if c_rid is not None and c_rid >= rid:
                        component.removeComponent(subcomponent)
                if len(list(component.subcomponents())) == 0:
                    self.removeComponent(component)

        self._markAsDirty()

        # We changed the instance set so remove any instance cache
        # TODO: we could be smarter here and truncate the instance list
        if hasattr(self, "cachedInstances"):
            delattr(self, "cachedInstances")

    def onlyFutureInstances(self, rid):
        """
        Remove all recurrence instances from the specified recurrence-id into the past. Adjust the bounds of
        any RRULEs to match the new limit, remove RDATEs/EXDATEs and overridden components beyond the limit.
        This also requires "re-basing" the master component to the new first instance - but noting that has to
        match any RRULE pattern.

        @param rid: the recurrence-id limit
        @type rid: L{DateTime}
        """

        if not self.isRecurring():
            return
        master = self.masterComponent()
        if master:
            # Check if cut-off matches an RDATE
            adjusted_rid = rid
            adjust_rrule = None
            adjust_count = 0
            continuing_rrule = True

            # Need to detect the first valid RRULE instance after the cut-off as that needs to be the new DTSTART
            rrules = master._pycalendar.getRecurrenceSet()
            if rrules and len(rrules.getRules()) != 0:
                rrule = rrules.getRules()[0]
                upperlimit = rid.duplicate()
                upperlimit.offsetYear(1)
                rrule_expanded = []
                rrule.expand(
                    master.propertyValue("DTSTART"),
                    Period(DateTime(1900, 1, 1), upperlimit),
                    rrule_expanded,
                )
                for ctr, i in enumerate(sorted(rrule_expanded)):
                    if i >= rid:
                        adjusted_rid = i
                        adjust_rrule = rrule
                        adjust_count = ctr
                        break
                else:
                    # RRULE not needed in derived master
                    continuing_rrule = False

            # Adjust master to previously derived instance
            derived = self.deriveInstance(adjusted_rid, allowExcluded=True)
            if derived is None:
                return

            # Adjust any COUNT to exclude the earlier instances - note we do this after
            # deriving the instance otherwise it might truncate the instance we care about
            if adjust_rrule is not None and rrule.getUseCount():
                adjust_rrule.setCount(adjust_rrule.getCount() - adjust_count)

            # Fix up recurrence properties so the derived one looks like the master
            derived.removeProperty(derived.getProperty("RECURRENCE-ID"))
            for property in list(itertools.chain(
                master.properties("RRULE") if continuing_rrule else (),
                master.properties("RDATE"),
                master.properties("EXDATE"),
            )):
                derived.addProperty(property)

            # Now switch over to using the new "derived" master
            self.removeComponent(master)
            master = derived
            self.addComponent(master)

            # Remove any RDATEs or EXDATEs in the past
            for property in list(itertools.chain(
                master.properties("RDATE"),
                master.properties("EXDATE"),
            )):
                for value in list(property.value()):
                    # If the derived master was derived from an RDATE we remove the RDATE
                    if value.getValue() < rid or property.name() == "RDATE" and value.getValue() == adjusted_rid:
                        property.value().remove(value)
                if len(property.value()) == 0:
                    master.removeProperty(property)

        # Remove overrides in the past - but do not remove any override matching
        # the cut-off as that is still a valid override after "re-basing" the master.
        for component in list(self.subcomponents(ignore=True)):
            c_rid = component.getRecurrenceIDUTC()
            if c_rid is not None and c_rid < rid:
                self.removeComponent(component)

        # Handle per-user data component by removing ones in the past
        for component in list(self.subcomponents()):
            if component.name() == PERUSER_COMPONENT:
                for subcomponent in list(component.subcomponents()):
                    c_rid = subcomponent.getRecurrenceIDUTC()
                    if c_rid is not None and c_rid < rid:
                        component.removeComponent(subcomponent)
                if len(list(component.subcomponents())) == 0:
                    self.removeComponent(component)

        self._markAsDirty()

        # We changed the instance set so remove any instance cache
        # TODO: we could be smarter here and truncate the instance list
        if hasattr(self, "cachedInstances"):
            delattr(self, "cachedInstances")

    def expand(self, start, end, timezone=None):
        """
        Expand the components into a set of new components, one for each
        instance in the specified range. Date-times are converted to UTC. A
        new calendar object is returned.

        @param start: the L{DateTime} for the start of the range.
        @param end: the L{DateTime} for the end of the range.
        @param timezone: the L{Component} or L{Timezone} of the VTIMEZONE to use for floating/all-day.
        @return: the L{Component} for the new calendar with expanded instances.
        """

        if timezone is not None and isinstance(timezone, Component):
            pytz = Timezone(tzid=timezone.propertyValue("TZID"))
        else:
            pytz = timezone

        # Create new calendar object with same properties as the original, but
        # none of the originals sub-components
        calendar = Component("VCALENDAR")
        for property in calendar.properties():
            calendar.removeProperty(property)
        for property in self.properties():
            calendar.addProperty(property)

        # Expand the instances and add each one - use the normalizeForExpand date/time normalization method here
        # so that all-day date/times remain that way. However, when doing the timeRangesOverlap test below, we
        # Need to convert the all-days to floating (T000000) so that the timezone overlap calculation can be done
        # properly.
        instances = self.expandTimeRanges(end, lowerLimit=start, normalizeFunction=normalizeForExpand)
        first = True
        for key in instances:
            instance = instances[key]
            if timeRangesOverlap(normalizeForIndex(instance.start), normalizeForIndex(instance.end), start, end, pytz):
                calendar.addComponent(self.expandComponent(instance, first))
            first = False

        return calendar

    def expandComponent(self, instance, first):
        """
        Create an expanded component based on the instance provided.
        NB Expansion also requires UTC conversions.
        @param instance: an L{Instance} for the instance being expanded.
        @return: a new L{Component} for the expanded instance.
        """

        # Duplicate the component from the instance
        newcomp = instance.component.duplicate()

        # Strip out unwanted recurrence properties
        for property in tuple(newcomp.properties()):
            if property.name() in ["RRULE", "RDATE", "EXRULE", "EXDATE", "RECURRENCE-ID"]:
                newcomp.removeProperty(property)

        # Convert all datetime properties to UTC unless they are floating
        for property in newcomp.properties():
            value = property.value()
            if isinstance(value, DateTime) and value.local():
                property.removeParameter("TZID")
                property.setValue(value.duplicateAsUTC())

        # Now reset DTSTART, DTEND/DURATION
        for property in newcomp.properties("DTSTART"):
            property.setValue(instance.start)
        for property in newcomp.properties("DTEND"):
            property.setValue(instance.end)
        for property in newcomp.properties("DURATION"):
            property.setValue(instance.end - instance.start)

        # Add RECURRENCE-ID if not master instance
        if not instance.isMasterInstance():
            newcomp.addProperty(Property("RECURRENCE-ID", instance.rid))

        return newcomp

    def cacheExpandedTimeRanges(self, limit, ignoreInvalidInstances=False):
        """
        Expand instances up to the specified limit and cache the results in this object
        so we can return cached results in the future. The limit value is the actual value
        that the requester needs, but we will cache an addition 365-days worth to give us some
        breathing room to return results for future instances.

        @param limit: the max datetime to cache up to.
        @type limit: L{DateTime}
        """

        # Checked for cached values first
        if hasattr(self, "cachedInstances"):
            cachedLimit = self.cachedInstances.limit
            if cachedLimit is None or cachedLimit > limit:
                # We have already fully expanded, or cached up to the requested time,
                # so return cached instances
                return self.cachedInstances

        lookAheadLimit = limit + Duration(days=365)
        self.cachedInstances = self.expandTimeRanges(
            lookAheadLimit,
            ignoreInvalidInstances=ignoreInvalidInstances
        )
        return self.cachedInstances

    def expandTimeRanges(self, limit, lowerLimit=None, ignoreInvalidInstances=False, normalizeFunction=normalizeForIndex):
        """
        Expand the set of recurrence instances for the components
        contained within this VCALENDAR component. We will assume
        that this component has already been validated as a CalDAV resource
        (i.e. only one type of component, all with the same UID)
        @param limit: L{DateTime} value representing the end of the expansion.
        @param ignoreInvalidInstances: C{bool} whether to ignore instance errors.
        @return: a set of Instances for each recurrence in the set.
        """

        componentSet = self.subcomponents()
        return self.expandSetTimeRanges(componentSet, limit, lowerLimit=lowerLimit, ignoreInvalidInstances=ignoreInvalidInstances, normalizeFunction=normalizeFunction)

    def expandSetTimeRanges(self, componentSet, limit, lowerLimit=None, ignoreInvalidInstances=False, normalizeFunction=normalizeForIndex):
        """
        Expand the set of recurrence instances up to the specified date limit.
        What we do is first expand the master instance into the set of generate
        instances. Then we merge the overridden instances, taking into account
        THISANDFUTURE and THISANDPRIOR.

        @param componentSet: the set of components that are to make up the
                recurrence set. These MUST all be components with the same UID
                and type, forming a proper recurring set.
        @param limit: L{DateTime} value representing the end of the expansion.

        @param componentSet: the set of components that are to make up the recurrence set.
            These MUST all be components with the same UID and type, forming a proper
            recurring set.
        @type componentSet: C{list}
        @param limit: the end of the expansion
        @type limit: L{DateTime}
        @param ignoreInvalidInstances: whether or not invalid recurrences raise an exception
        @type ignoreInvalidInstances: C{bool}
        @param normalizeFunction: a function used to normalize date/time values in instances
        @type normalizeFunction: C{function}
        @return: L{InstanceList} containing expanded L{Instance} for each recurrence in the set.
        """

        # Set of instances to return
        instances = InstanceList(ignoreInvalidInstances=ignoreInvalidInstances, normalizeFunction=normalizeFunction)
        try:
            instances.expandTimeRanges(componentSet, limit, lowerLimit=lowerLimit)
        except InvalidOverriddenInstanceError as e:
            if accountingEnabledForCategory("Invalid Instance"):
                emitAccounting(
                    "Invalid Instance",
                    self.resourceUID().encode("base64")[:-1],
                    "{}\n\n{}".format(str(e), str(self)),
                )
            raise
        return instances

    def getComponentInstances(self):
        """
        Get the R-ID value for each component.

        @return: a tuple of recurrence-ids
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            result = ()
            for component in self.subcomponents(ignore=True):
                result += component.getComponentInstances()
            return result
        else:
            rid = self.getRecurrenceIDUTC()
            return (rid,)

    def isRecurring(self):
        """
        Check whether any recurrence properties are present in any component.
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                if component.isRecurring():
                    return True
        else:
            for propname in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID",):
                if self.hasProperty(propname):
                    return True
        return False

    def isRecurringUnbounded(self):
        """
        Check for unbounded recurrence.
        """

        master = self.masterComponent()
        if master:
            rrules = master.properties("RRULE")
            for rrule in rrules:
                if not rrule.value().getUseCount() and not rrule.value().getUseUntil():
                    return True
        return False

    def deriveInstance(self, rid, allowCancelled=False, newcomp=None, allowExcluded=False):
        """
        Derive an instance from the master component that has the provided RECURRENCE-ID, but
        with all other properties, components etc from the master. If the requested override is
        currently marked as an EXDATE in the existing master, allow an option whereby the override
        is added as STATUS:CANCELLED and the EXDATE removed.

        IMPORTANT: all callers of this method MUST check the return value for None. Never assume that
        a valid instance will be derived - no matter how much you think you understand iCalendar recurrence.
        There is always some new thing that will surprise you.

        @param rid: recurrence-id value
        @type rid: L{DateTime} or C{str}
        @param allowCancelled: whether to allow a STATUS:CANCELLED override
        @type allowCancelled: C{bool}
        @param allowExcluded: whether to derive an instance for an existing EXDATE
        @type allowExcluded: C{bool}

        @return: L{Component} for newly derived instance, or C{None} if not a valid override
        """

        if allowCancelled and newcomp is not None:
            raise ValueError("Cannot re-use master component with allowCancelled")

        # Must have a master component
        master = self.masterComponent()
        if master is None:
            return None

        if isinstance(rid, str):
            rid = DateTime.parseText(rid) if rid else None

        # TODO: Check that the recurrence-id is a valid instance
        # For now we just check that there is no matching EXDATE
        didCancel = False
        matchedExdate = False
        for exdate in tuple(master.properties("EXDATE")):
            for exdateValue in exdate.value():
                if exdateValue.getValue() == rid:
                    if allowCancelled:
                        exdate.value().remove(exdateValue)
                        if len(exdate.value()) == 0:
                            master.removeProperty(exdate)
                        didCancel = True

                        # We changed the instance set so remove any instance cache
                        if hasattr(self, "cachedInstances"):
                            delattr(self, "cachedInstances")
                        break
                    elif allowExcluded:
                        matchedExdate = True
                        break
                    else:
                        # Cannot derive from an existing EXDATE
                        return None

        if not matchedExdate:
            # Check whether recurrence-id matches an RDATE - if so it is OK
            rdates = set()
            for rdate in master.properties("RDATE"):
                rdates.update([item.getValue().duplicateAsUTC() for item in rdate.value()])
            if rid not in rdates:
                # Check whether we have a truncated RRULE
                rrules = master.properties("RRULE")
                if len(tuple(rrules)):
                    instances = self.cacheExpandedTimeRanges(rid)
                    instance_rid = normalizeForIndex(rid)
                    if str(instance_rid) not in instances.instances:
                        # No match to a valid RRULE instance
                        return None
                else:
                    # No RRULE and no match to an RDATE => error
                    return None

        # If we were fed an already derived component, use that, otherwise make a new one
        if newcomp is None:
            newcomp = self.masterDerived()

        # New DTSTART is the RECURRENCE-ID we are deriving but adjusted to the
        # original DTSTART's localtime
        dtstart = newcomp.getProperty("DTSTART")
        if newcomp.hasProperty("DTEND"):
            dtend = newcomp.getProperty("DTEND")
            oldduration = dtend.value() - dtstart.value()

        newdtstartValue = rid.duplicate()
        if not dtstart.value().isDateOnly():
            if dtstart.value().local():
                newdtstartValue.adjustTimezone(dtstart.value().getTimezone())
        else:
            newdtstartValue.setDateOnly(True)

        dtstart.setValue(newdtstartValue)
        if newcomp.hasProperty("DTEND"):
            dtend.setValue(newdtstartValue + oldduration)

        newcomp.replaceProperty(Property("RECURRENCE-ID", dtstart.value(), params={}))

        if didCancel:
            newcomp.replaceProperty(Property("STATUS", "CANCELLED"))

        # After creating/changing a component we need to do this to keep PyCalendar happy
        newcomp._pycalendar.finalise()

        return newcomp

    def masterDerived(self):
        """
        Generate a component from the master instance that can be fed repeatedly to
        deriveInstance in the case where the result of deriveInstance is not going
        to be inserted into the component. This provides an optimization for avoiding
        unnecessary .duplicate() calls on the master for each deriveInstance.
        """

        # Must have a master component
        master = self.masterComponent()
        if master is None:
            return None

        # Create the derived instance
        newcomp = master.duplicate()

        # Strip out unwanted recurrence properties
        for property in tuple(newcomp.properties()):
            if property.name() in ("RRULE", "RDATE", "EXRULE", "EXDATE", "RECURRENCE-ID",):
                newcomp.removeProperty(property)

        return newcomp

    def validInstances(self, rids, ignoreInvalidInstances=False):
        """
        Test whether the specified recurrence-ids are valid instances in this event.

        @param rid: recurrence-id values
        @type rid: iterable

        @return: C{set} of valid rids
        """

        valid = set()
        non_master_rids = [rid for rid in rids if rid is not None]
        if non_master_rids:
            # Pre-cache instance expansion up to the highest rid
            highest_rid = max(non_master_rids)
            self.cacheExpandedTimeRanges(
                highest_rid + Duration(days=1),
                ignoreInvalidInstances=ignoreInvalidInstances
            )
        for rid in rids:
            if self.validInstance(rid, clear_cache=False, ignoreInvalidInstances=ignoreInvalidInstances):
                valid.add(rid)
        return valid

    def validInstance(self, rid, clear_cache=True, ignoreInvalidInstances=False):
        """
        Test whether the specified recurrence-id is a valid instance in this event.

        @param rid: recurrence-id value
        @type rid: L{DateTime}

        @return: C{bool}
        """

        if self.masterComponent() is None:
            return rid in set(self.getComponentInstances())

        if rid is None:
            return True

        # Get expansion
        instances = self.cacheExpandedTimeRanges(
            rid,
            ignoreInvalidInstances=ignoreInvalidInstances
        )
        new_rids = set([instances[key].rid for key in instances])
        return rid in new_rids

    def addExdate(self, exdate):
        """
        Add an EXDATE to a master recurring component and ensure the value type, TZID
        etc match the DTSTART of the master. This method assumes that L{self} is the
        master component - no checking of that will be done.

        @param exdate: the exdate to add
        @type exdate: L{DateTime}
        """
        dtstart = self.getProperty("DTSTART")
        if dtstart is not None and not dtstart.value().isDateOnly() and dtstart.value().local():
            exdate.adjustTimezone(dtstart.value().getTimezone())
        self.addProperty(Property("EXDATE", [exdate, ]))

    def removeExdate(self, exdate):
        """
        Remove an EXDATE from a master recurring component if present. If not present,
        do nothing. This assumes L{self} is the master component

        @param exdate: the exdate to add
        @type exdate: L{DateTime}
        """
        for exdateProp in tuple(self.properties("EXDATE")):
            for exdateValue in exdateProp.value():
                if exdateValue.getValue() == exdate:
                    exdateProp.value().remove(exdateValue)
                    if len(exdateProp.value()) == 0:
                        self.removeProperty(exdateProp)

    def resourceUID(self):
        """
        @return: the UID of the subcomponents in this component.
        """
        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        if not hasattr(self, "_resource_uid"):
            for subcomponent in self.subcomponents(ignore=True):
                self._resource_uid = subcomponent.propertyValue("UID")
                break
            else:
                self._resource_uid = None

        return self._resource_uid

    def newUID(self, newUID=None):
        """
        Generate a new UID for all components in this VCALENDAR
        """
        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        newUID = str(uuid.uuid4()) if newUID is None else newUID
        self._pycalendar.changeUID(self.resourceUID(), newUID)
        self._resource_uid = newUID
        self._markAsDirty()
        return self._resource_uid

    def resourceType(self):
        """
        @return: the name of the iCalendar type of the subcomponents in this
            component.
        """
        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        if not hasattr(self, "_resource_type"):
            has_timezone = False

            for subcomponent in self.subcomponents():
                name = subcomponent.name()
                if name == "VTIMEZONE":
                    has_timezone = True
                elif subcomponent.ignored():
                    continue
                else:
                    self._resource_type = name
                    break
            else:
                if has_timezone:
                    self._resource_type = "VTIMEZONE"
                else:
                    raise InvalidICalendarDataError("No component type found for calendar component: {0!r}".format(self,))

        return self._resource_type

    def stripStandardTimezones(self):
        """
        Remove timezones that this server knows about
        """
        return self._pycalendar.stripStandardTimezones()

    def validCalendarData(self, doFix=True, doRaise=True, validateRecurrences=False):
        """
        @return: tuple of fixed, unfixed issues
        @raise InvalidICalendarDataError: if the given calendar data is not valid and
            cannot be fixed.
        """
        if self.name() != "VCALENDAR":
            log.debug("Not a calendar: {s!r}", s=self)
            raise InvalidICalendarDataError("Not a calendar")
        if not self.resourceType():
            log.debug("Unknown resource type: {s!r}", s=self)
            raise InvalidICalendarDataError("Unknown resource type")

        # Do underlying iCalendar library validation with data fix
        fixed, unfixed = self._pycalendar.validate(doFix=doFix)

        # Detect invalid occurrences and fix by adding RDATEs for them
        if validateRecurrences:
            rfixed, runfixed = self.validRecurrenceIDs(doFix=doFix)
            fixed.extend(rfixed)
            unfixed.extend(runfixed)

        if unfixed:
            log.debug("Calendar data had unfixable problems:\n  {d}", d="\n  ".join(unfixed))
            if doRaise:
                raise InvalidICalendarDataError("Calendar data had unfixable problems:\n  {0}".format("\n  ".join(unfixed),))
        if fixed:
            log.debug("Calendar data had fixable problems:\n  {d}", d="\n  ".join(fixed))

        return fixed, unfixed

    def validRecurrenceIDs(self, doFix=True):

        fixed = []
        unfixed = []

        # Detect invalid occurrences and fix by adding RDATEs for them
        master = self.masterComponent()
        if master is not None:
            # Get the set of all recurrence IDs
            all_rids = set(self.getComponentInstances())
            if None in all_rids:
                all_rids.remove(None)

            # If the master has no recurrence properties treat any other components as invalid
            if master.isRecurring():

                # Remove all EXDATEs with a matching RECURRENCE-ID. Do this before we start
                # processing of valid instances just in case the matching R-ID is also not valid and
                # thus will need RDATE added.
                exdates = {}
                for property in list(master.properties("EXDATE")):
                    for exdate in property.value():
                        exdates[exdate.getValue()] = property
                for rid in all_rids:
                    if rid in exdates:
                        if doFix:
                            property = exdates[rid]
                            for value in property.value():
                                if value.getValue() == rid:
                                    property.value().remove(value)
                                    break
                            master.removeProperty(property)
                            if len(property.value()) > 0:
                                master.addProperty(property)
                            del exdates[rid]
                            fixed.append("Removed EXDATE for valid override: {0}".format(rid,))
                        else:
                            unfixed.append("EXDATE for valid override: {0}".format(rid,))

                # Get the set of all valid recurrence IDs
                valid_rids = self.validInstances(all_rids, ignoreInvalidInstances=True)

                # Get the set of all RDATEs and add those to the valid set
                rdates = []
                for property in master.properties("RDATE"):
                    rdates.extend([_rdate.getValue() for _rdate in property.value()])
                valid_rids.update(set(rdates))

                # Remove EXDATEs predating master
                dtstart = master.propertyValue("DTSTART")
                if dtstart is not None:
                    for property in list(master.properties("EXDATE")):
                        newValues = []
                        changed = False
                        for exdate in property.value():
                            exdateValue = exdate.getValue()
                            if exdateValue < dtstart:
                                if doFix:
                                    fixed.append("Removed earlier EXDATE: {0}".format(exdateValue,))
                                else:
                                    unfixed.append("EXDATE earlier than master: {0}".format(exdateValue,))
                                changed = True
                            else:
                                newValues.append(exdateValue)

                        if changed and doFix:
                            # Remove the property...
                            master.removeProperty(property)
                            if newValues:
                                # ...and add it back only if it still has values
                                property.setValue(newValues)
                                master.addProperty(property)

            else:
                valid_rids = set()

            # Determine the invalid recurrence IDs by set subtraction
            invalid_rids = all_rids - valid_rids

            # Add RDATEs for the invalid ones, or remove any EXDATE.
            for invalid_rid in invalid_rids:
                brokenComponent = self.overriddenComponent(invalid_rid)
                brokenRID = brokenComponent.propertyValue("RECURRENCE-ID")
                if doFix:
                    master.addProperty(Property("RDATE", [brokenRID, ]))
                    fixed.append(
                        "Added RDATE for invalid occurrence: {0}".format(
                            brokenRID,
                        )
                    )
                else:
                    unfixed.append("Invalid occurrence: {0}".format(brokenRID,))

        return fixed, unfixed

    def validCalendarForCalDAV(self, methodAllowed):
        """
        @param methodAllowed: True if METHOD property is allowed, False otherwise.
        @raise InvalidICalendarDataError: if the given calendar component is not valid for
            use as a X{CalDAV} resource.
        """

        # Disallowed in CalDAV-Access-08, section 4.1
        if not methodAllowed and self.hasProperty("METHOD"):
            msg = "METHOD property is not allowed in CalDAV iCalendar data"
            log.debug(msg)
            raise InvalidICalendarDataError(msg)

        #
        # Must not contain more than one type of iCalendar component, except for
        # the required timezone components, and component UIDs must match
        #
        ctype = None
        component_id = None
        component_rids = set()
        timezone_refs = set()
        timezones = set()
        got_master = False
        # got_override     = False
        # master_recurring = False

        for subcomponent in self.subcomponents():
            if subcomponent.name() == "VTIMEZONE":
                timezones.add(subcomponent.propertyValue("TZID"))
            elif subcomponent.ignored():
                continue
            else:
                if ctype is None:
                    ctype = subcomponent.name()
                else:
                    if ctype != subcomponent.name():
                        msg = "Calendar resources may not contain more than one type of calendar component ({0} and {1} found)".format(
                            ctype, subcomponent.name()
                        )
                        log.debug(msg)
                        raise InvalidICalendarDataError(msg)

                if ctype not in allowedComponents:
                    msg = "Component type: {0} not allowed".format(ctype,)
                    log.debug(msg)
                    raise InvalidICalendarDataError(msg)

                uid = subcomponent.propertyValue("UID")
                if uid is None:
                    msg = "All components must have UIDs"
                    log.debug(msg)
                    raise InvalidICalendarDataError(msg)
                rid = subcomponent.getRecurrenceIDUTC()

                # Verify that UIDs are the same
                if component_id is None:
                    component_id = uid
                elif component_id != uid:
                    msg = "Calendar resources may not contain components with different UIDs ({0} and {1} found)".format(
                        component_id, subcomponent.propertyValue("UID")
                    )
                    log.debug(msg)
                    raise InvalidICalendarDataError(msg)

                # Verify that there is only one master component
                if rid is None:
                    if got_master:
                        msg = "Calendar resources may not contain components with the same UIDs and no Recurrence-IDs ({0} and {1} found)".format(
                            component_id, subcomponent.propertyValue("UID")
                        )
                        log.debug(msg)
                        raise InvalidICalendarDataError(msg)
                    else:
                        got_master = True
                        # master_recurring = subcomponent.hasProperty("RRULE") or subcomponent.hasProperty("RDATE")
                else:
                    pass  # got_override = True

                # Check that if an override is present then the master is recurring
                # Leopard iCal sometimes does this for overridden instances that an Attendee receives and
                # it creates a "fake" (invalid) master. We are going to skip this test here. Instead implicit
                # scheduling will verify the validity of the components and raise if they don't make sense.
                # If no scheduling is happening then we allow this - that may cause other clients to choke.
                # If it does we will have to reinstate this check but only after we have checked for implicit.
# UNCOMMENT OUT master_recurring AND got_override ASSIGNMENTS ABOVE
#                if got_override and got_master and not master_recurring:
#                    msg = "Calendar resources must have a recurring master component if there is an overridden one ({uid})".format(uid=subcomponent.propertyValue("UID"),)
#                    log.debug(msg)
#                    raise InvalidICalendarDataError(msg)

                # Check for duplicate RECURRENCE-IDs
                if rid in component_rids:
                    msg = "Calendar resources may not contain components with the same Recurrence-IDs ({0})".format(rid,)
                    log.debug(msg)
                    raise InvalidICalendarDataError(msg)
                else:
                    component_rids.add(rid)

                timezone_refs.update(subcomponent.timezoneIDs())

        #
        # Make sure required timezone components are present
        #
        if not config.EnableTimezonesByReference:
            for timezone_ref in timezone_refs:
                if timezone_ref not in timezones:
                    msg = "Timezone ID {0} is referenced but not defined: {1}".format(timezone_ref, self,)
                    log.debug(msg)
                    raise InvalidICalendarDataError(msg)

        #
        # FIXME:
        #   This test is not part of the spec; it appears to be legal (but
        #   goofy?) to have extra timezone components.
        #
        for timezone in timezones:
            if timezone not in timezone_refs:
                log.debug(
                    "Timezone {tz} is not referenced by any non-timezone component", tz=timezone
                )

        # TZIDs without a VTIMEZONE must be available in the server's TZ database
        missing_timezones = timezone_refs - timezones
        for tzid in missing_timezones:
            # Will raise TimezoneException if tzid not present in server's database
            hasTZ(tzid)

        # Control character check - only HTAB, CR, LF allowed for characters in the range 0x00-0x1F
        s = str(self)
        if len(s.translate(None, "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0B\x0C\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\x1B\x1C\x1D\x1E\x1F")) != len(s):
            raise InvalidICalendarDataError("iCalendar contains illegal control character")

    def validOrganizerForScheduling(self, doFix=True):
        """
        Check that the ORGANIZER property is valid for scheduling
        """

        organizers = self.getOrganizersByInstance()
        foundOrganizer = None
        foundRid = None
        missingRids = set()
        for organizer, rid in organizers:
            if organizer:
                if foundOrganizer:
                    if organizer != foundOrganizer:
                        # We have different ORGANIZERs in the same iCalendar object - this is an error
                        msg = "Only one ORGANIZER is allowed in an iCalendar object:\n{0}".format(self,)
                        log.debug(msg)
                        raise InvalidICalendarDataError(msg)
                else:
                    foundOrganizer = organizer
                    foundRid = rid
            else:
                missingRids.add(rid)

        # If there are some components without an ORGANIZER we will fix the data
        if foundOrganizer and missingRids:
            if doFix:
                log.debug("Fixing missing ORGANIZER properties")
                organizerProperty = self.overriddenComponent(foundRid).getProperty("ORGANIZER")
                for rid in missingRids:
                    self.overriddenComponent(rid).addProperty(organizerProperty)
            else:
                raise InvalidICalendarDataError("iCalendar missing ORGANIZER properties in some instances")

        return foundOrganizer

    def gettimezone(self):
        """
        Get the Timezone for a Timezone component.

        @return: L{Timezone} if this is a VTIMEZONE, otherwise None.
        """
        if self.name() == "VTIMEZONE":
            return Timezone(tzid=self._pycalendar.getID())
        elif self.name() == "VCALENDAR":
            for component in self.subcomponents():
                if component.name() == "VTIMEZONE":
                    return component.gettimezone()
            else:
                return None
        else:
            return None

    # #
    # iTIP stuff
    # #

    def isValidMethod(self):
        """
        Verify that this calendar component has a valid iTIP METHOD property.

        @return: True if valid, False if not
        """

        try:
            method = self.propertyValue("METHOD")
            if method not in ("PUBLISH", "REQUEST", "REPLY", "ADD", "CANCEL", "REFRESH", "COUNTER", "DECLINECOUNTER"):
                return False
        except InvalidICalendarDataError:
            return False

        return True

    def isValidITIP(self):
        """
        Verify that this calendar component is valid according to iTIP.

        @return: True if valid, False if not
        """

        try:
            method = self.propertyValue("METHOD")
            if method not in ("PUBLISH", "REQUEST", "REPLY", "ADD", "CANCEL", "REFRESH", "COUNTER", "DECLINECOUNTER"):
                return False

            # First make sure components are all of the same time (excluding VTIMEZONE)
            self.validCalendarForCalDAV(methodAllowed=True)

            # Next we could check the iTIP status for each type of method/component pair, however
            # we can also leave that up to the server except for the REQUEST/VFREEBUSY case which
            # the server will handle itself.

            if (method == "REQUEST") and (self.mainType() == "VFREEBUSY"):
                # TODO: verify REQUEST/VFREEBUSY as being OK

                # Only one VFREEBUSY (actually multiple X-'s are allowed but we will reject)
                if len([c for c in self.subcomponents()]) != 1:
                    return False

        except InvalidICalendarDataError:
            return False

        return True

    def getOrganizer(self):
        """
        Get the organizer value. Works on either a VCALENDAR or on a component.

        @return: the string value of the Organizer property, or None
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                return component.getOrganizer()
        else:
            try:
                # Find the primary subcomponent
                return self.propertyValue("ORGANIZER")
            except InvalidICalendarDataError:
                pass

        return None

    def getOrganizersByInstance(self):
        """
        Get the organizer value for each instance.

        @return: a list of tuples of (organizer value, recurrence-id)
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            result = ()
            for component in self.subcomponents(ignore=True):
                result += component.getOrganizersByInstance()
            return result
        else:
            try:
                # Should be just one ORGANIZER
                org = self.propertyValue("ORGANIZER")
                rid = self.getRecurrenceIDUTC()
                return ((org, rid),)
            except InvalidICalendarDataError:
                pass

        return ()

    def getOrganizerProperties(self):
        """
        Get the organizer value. Works on either a VCALENDAR or on a component.

        @return: the string value of the Organizer property, or None
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            return [component.getOrganizerProperty() for component in self.subcomponents(ignore=True)]
        else:
            try:
                return self.getProperty("ORGANIZER")
            except InvalidICalendarDataError:
                pass

        return None

    def getOrganizerProperty(self):
        """
        Get the organizer value. Works on either a VCALENDAR or on a component.

        @return: the string value of the Organizer property, or None
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                return component.getOrganizerProperty()
        else:
            try:
                # Find the primary subcomponent
                return self.getProperty("ORGANIZER")
            except InvalidICalendarDataError:
                pass

        return None

    def getOrganizerScheduleAgent(self):

        is_server = False
        organizerProp = self.getOrganizerProperty()
        if organizerProp is not None:
            if organizerProp.hasParameter("SCHEDULE-AGENT"):
                if organizerProp.parameterValue("SCHEDULE-AGENT") == "SERVER":
                    is_server = True
            else:
                is_server = True

        return is_server

    def cleanOrganizerScheduleAgent(self):
        """
        Remove components whose ORGANIZER property does not have
        SCHEDULE-AGENT=SERVER.
        """

        changed = False
        for component in tuple(self.subcomponents(ignore=True)):
            organizerProp = component.getOrganizerProperty()
            if organizerProp is not None:
                if organizerProp.parameterValue("SCHEDULE-AGENT", "SERVER") != "SERVER":
                    self.removeComponent(component)
                    changed = True

        return changed

    def recipientPropertyName(self):
        return "VOTER" if self.name() in ("VPOLL", "VVOTER",) else "ATTENDEE"

    def getRecipientProperties(self):
        """
        Get the attendee properties. Works on either a VCALENDAR or on a component.

        @return: a C{list} of the the Attendee properties
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                return component.getRecipientProperties()
        else:
            # Find the property values
            if self.name() == "VPOLL":
                results = []
                for c in self.subcomponents():
                    if c.name() == "VVOTER":
                        results.extend(c.properties(self.recipientPropertyName()))
                return results
            else:
                return list(self.properties(self.recipientPropertyName()))

        return None

    def getAttendees(self):
        """
        Get the attendee value. Works on either a VCALENDAR or on a component.

        @return: a C{list} of the string values of the Attendee property, or None
        """

        return [p.value() for p in self.getRecipientProperties()]

    def getAttendeesByInstance(self, makeUnique=False, onlyScheduleAgentServer=False):
        """
        Get the attendee values for each instance. Optionally remove duplicates.

        @param makeUnique: if C{True} remove duplicate ATTENDEEs in each component
        @type makeUnique: C{bool}
        @param onlyScheduleAgentServer: if C{True} only return ATETNDEEs with SCHEDULE-AGENT=SERVER set
        @type onlyScheduleAgentServer: C{bool}
        @return: a list of tuples of (organizer value, recurrence-id)
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            result = ()
            for component in self.subcomponents(ignore=True):
                result += component.getAttendeesByInstance(makeUnique, onlyScheduleAgentServer)
            return result
        else:
            result = ()
            attendees = set()
            rid = self.getRecurrenceIDUTC()
            for attendee in tuple(self.getRecipientProperties()):

                if onlyScheduleAgentServer:
                    if attendee.hasParameter("SCHEDULE-AGENT"):
                        if attendee.parameterValue("SCHEDULE-AGENT") != "SERVER":
                            continue

                cuaddr = attendee.value()
                if makeUnique and cuaddr in attendees:
                    self.removeProperty(attendee)
                else:
                    result += ((cuaddr, rid),)
                    attendees.add(cuaddr)
            return result

    def getVoterProperty(self, match):
        """
        Get the voters matching a value.

        @param match: a C{list} of calendar user address strings to try and match.
        @return: the matching Voter property, or None
        """

        # Need to normalize http/https cu addresses
        test = set()
        for item in match:
            test.add(normalizeCUAddr(item))

        # Find the primary subcomponent
        for voter in self.properties("VOTER"):
            if normalizeCUAddr(voter.value()) in test:
                return voter

        return None

    def getAttendeeProperty(self, match):
        """
        Get the attendees matching a value. Works on either a VCALENDAR or on a component.

        @param match: a C{list} of calendar user address strings to try and match.
        @return: the matching Attendee property, or None
        """

        # Need to normalize http/https cu addresses
        test = set()
        for item in match:
            test.add(normalizeCUAddr(item))

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                attendee = component.getAttendeeProperty(match)
                if attendee is not None:
                    return attendee
        else:
            # Find the primary subcomponent
            for attendee in self.getRecipientProperties():
                if normalizeCUAddr(attendee.value()) in test:
                    return attendee

        return None

    def getAttendeeProperties(self, match):
        """
        Get all the attendees matching a value in each component. Works on a VCALENDAR component only.

        @param match: a C{list} of calendar user address strings to try and match.
        @return: the string value of the Organizer property, or None
        """

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        # Extract appropriate sub-component if this is a VCALENDAR
        results = []
        for component in self.subcomponents(ignore=True):
            attendee = component.getAttendeeProperty(match)
            if attendee:
                results.append(attendee)

        return results

    def getAllAttendeeProperties(self):
        """
        Yield all attendees as Property objects.  Works on either a VCALENDAR or
        on a component.
        @return: a generator yielding Property objects
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                for attendee in component.getAllAttendeeProperties():
                    yield attendee
        else:
            # Find the primary subcomponent
            for attendee in self.getRecipientProperties():
                yield attendee

    def getAllUniqueAttendees(self, onlyScheduleAgentServer=True):
        attendeesByInstance = self.getAttendeesByInstance(True, onlyScheduleAgentServer=onlyScheduleAgentServer)
        attendees = set()
        for attendee, _ignore in attendeesByInstance:
            attendees.add(attendee)
        return attendees

    def getMaskUID(self):
        """
        Get the X-CALENDARSEREVR-MASK-UID value. Works on either a VCALENDAR or on a component.

        @return: the string value of the X-CALENDARSEREVR-MASK-UID property, or None
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                return component.getMaskUID()
        else:
            try:
                # Find the primary subcomponent
                return self.propertyValue("X-CALENDARSERVER-MASK-UID")
            except InvalidICalendarDataError:
                pass

        return None

    def getExtendedFreeBusy(self):
        """
        Get the X-CALENDARSERVER-EXTENDED-FREEBUSY value. Works on either a VCALENDAR or on a component.

        @return: the string value of the X-CALENDARSERVER-EXTENDED-FREEBUSY property, or None
        """

        # Extract appropriate sub-component if this is a VCALENDAR
        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                return component.getExtendedFreeBusy()
        else:
            try:
                # Find the primary subcomponent
                return self.propertyValue("X-CALENDARSERVER-EXTENDED-FREEBUSY")
            except InvalidICalendarDataError:
                pass

        return None

    def setParameterToValueForPropertyWithValue(self, paramname, paramvalue, propname, propvalue):
        """
        Add or change the parameter to the specified value on the property having the specified value.
        If C{paramvalue} is L{None} remove the parameter.

        @param paramname: the parameter name
        @type paramname: C{str}
        @param paramvalue: the parameter value to set
        @type paramvalue: C{str}
        @param propname: the property name
        @type propname: C{str}
        @param propvalue: the property value to test
        @type propvalue: C{str} or C{None}
        """

        self.setParametersForPropertyWithValue(
            {paramname: paramvalue},
            propname,
            propvalue,
        )

    def setParametersForPropertyWithValue(self, params, propname, propvalue):
        """
        Add, change or remove a set of parameters to the specified value on the property
        having the specified value. Parameters are specified in a name:value L{dict}. If
        the value is L{None} the parameter will be removed.

        @param params: the parameter name/value pairs to set/remove
        @type params: C{dict}
        @param propname: the property name
        @type propname: C{str}
        @param propvalue: the property value to test
        @type propvalue: C{str} or C{None}
        """

        for component in self.subcomponents(ignore=True):
            for property in component.properties(propname):
                if propvalue is None or property.value() == propvalue:
                    for paramname, paramvalue in params.items():
                        if paramvalue is not None:
                            property.setParameter(paramname, paramvalue)
                        else:
                            property.removeParameter(paramname)

    def hasPropertyInAnyComponent(self, properties):
        """
        Test for the existence of one or more properties in any component.

        @param properties: property name(s) to test for
        @type properties: C{list}, C{tuple} or C{str}
        """

        if isinstance(properties, str):
            properties = (properties,)

        for property in properties:
            if self.hasProperty(property):
                return True

        for component in self.subcomponents():
            if component.hasPropertyInAnyComponent(properties):
                return True

        return False

    def getFirstPropertyInAnyComponent(self, properties):
        """
        Get the first of any set of properties in any component.

        @param properties: property name(s) to test for
        @type properties: C{list}, C{tuple} or C{str}
        """

        if isinstance(properties, str):
            properties = (properties,)

        for property in properties:
            props = tuple(self.properties(property))
            if props:
                return props[0]

        for component in self.subcomponents():
            prop = component.getFirstPropertyInAnyComponent(properties)
            if prop:
                return prop

        return None

    def getAllPropertiesInAnyComponent(self, properties, depth=2):
        """
        Get the all of any set of properties in any component down to a
        specified depth.

        @param properties: property name(s) to test for
        @type properties: C{list}, C{tuple} or C{str}
        @param depth: how deep to go in looking at sub-components:
            0: do not go into sub-components, 1: go into one level of sub-components,
            2: two levels (which is effectively all the levels supported in iCalendar)
        @type depth: int
        """

        results = []

        if isinstance(properties, str):
            properties = (properties,)

        for property in properties:
            props = tuple(self.properties(property))
            if props:
                results.extend(props)

        if depth > 0:
            for component in self.subcomponents():
                results.extend(component.getAllPropertiesInAnyComponent(properties, depth - 1))

        return results

    def hasPropertyValueInAllComponents(self, property):
        """
        Test for the existence of a property with a specific value in any sub-component.

        @param property: property to test for
        @type property: L{Property}
        """

        for component in self.subcomponents(ignore=True):
            found = component.getProperty(property.name())
            if not found or found.value() != property.value():
                return False

        return True

    def addPropertyToAllComponents(self, property):
        """
        Add a property to all top-level components except VTIMEZONE.

        @param property: the property to add
        @type property: L{Property}
        """

        for component in self.subcomponents(ignore=True):
            component.addProperty(property)

    def replacePropertyInAllComponents(self, property):
        """
        Replace a property in all components.
        @param property: the L{Property} to replace in this component.
        """

        for component in self.subcomponents(ignore=True):
            component.replaceProperty(property)

    def hasPropertyWithParameterMatch(self, propname, param_name, param_value, param_value_is_default=False):
        """
        See if property whose name, and parameter name, value match in any components.

        @param property: the L{Property} to replace in this component.
        @param param_name: the C{str} of parameter name to match.
        @param param_value: the C{str} of parameter value to match, if C{None} then just match on the
            presence of the parameter name.
        @param param_value_is_default: C{bool} to indicate whether absence of the named parameter
            also implies a match

        @return: C{True} if matching property found, C{False} if not
        @rtype: C{bool}
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                if component.hasPropertyWithParameterMatch(propname, param_name, param_value, param_value_is_default):
                    return True
        else:
            for oldprop in tuple(self.properties(propname)):
                pvalue = oldprop.parameterValue(param_name)
                if pvalue is None and param_value_is_default or pvalue == param_value or param_value is None:
                    return True

        return False

    def replaceAllPropertiesWithParameterMatch(self, property, param_name, param_value, param_value_is_default=False):
        """
        Replace a property whose name, and parameter name, value match in all components.

        @param property: the L{Property} to replace in this component.
        @param param_name: the C{str} of parameter name to match.
        @param param_value: the C{str} of parameter value to match.
        @param param_value_is_default: C{bool} to indicate whether absence of the named parameter
            also implies a match
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.replaceAllPropertiesWithParameterMatch(property, param_name, param_value, param_value_is_default)
        else:
            for oldprop in tuple(self.properties(property.name())):
                pvalue = oldprop.parameterValue(param_name)
                if pvalue is None and param_value_is_default or pvalue == param_value:
                    self.removeProperty(oldprop)
                    self.addProperty(property)

    def removeAllPropertiesWithParameterMatch(self, propname, param_name, param_value, param_value_is_default=False):
        """
        Remove all properties whose name, and parameter name, value match in all components.
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.removeAllPropertiesWithParameterMatch(propname, param_name, param_value, param_value_is_default)
        else:
            for oldprop in tuple(self.properties(propname)):
                pvalue = oldprop.parameterValue(param_name)
                if pvalue is None and param_value_is_default or pvalue == param_value:
                    self.removeProperty(oldprop)

    def transferProperties(self, from_calendar, properties):
        """
        Transfer specified properties from old calendar into all components
        of this calendar, synthesizing any for new overridden instances.

        @param from_calendar: the old calendar to copy from
        @type from_calendar: L{Component}
        @param properties: the property names to copy over
        @type properties: C{tuple} or C{list}
        """

        assert from_calendar.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.transferProperties(from_calendar, properties)
        else:
            # Is there a matching component
            rid = self.getRecurrenceIDUTC()
            matched = from_calendar.overriddenComponent(rid)

            # If no match found, we are processing a new overridden instance so copy from the original master
            if not matched:
                matched = from_calendar.masterComponent()

            if matched:
                for propname in properties:
                    for prop in matched.properties(propname):
                        self.addProperty(prop)

    def attendeesView(self, attendees, onlyScheduleAgentServer=False):
        """
        Filter out any components that all attendees are not present in. Use EXDATEs
        on the master to account for changes.
        """

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        # Modify any components that reference the attendee, make note of the ones that don't
        remove_components = []
        master_component = None
        removed_master = False
        for component in self.subcomponents(ignore=True):
            found_all_attendees = True
            for attendee in attendees:
                foundAttendee = component.getAttendeeProperty((attendee,))
                if foundAttendee is None:
                    found_all_attendees = False
                    break
                if onlyScheduleAgentServer:
                    if foundAttendee.hasParameter("SCHEDULE-AGENT"):
                        if foundAttendee.parameterValue("SCHEDULE-AGENT") != "SERVER":
                            found_all_attendees = False
                            break
            if not found_all_attendees:
                remove_components.append(component)
            if component.getRecurrenceIDUTC() is None:
                master_component = component
                if not found_all_attendees:
                    removed_master = True

        # Now remove the unwanted components - but we may need to EXDATE the master
        exdates = []
        for component in remove_components:
            rid = component.getRecurrenceIDUTC()
            if rid is not None:
                exdates.append(rid)
            self.removeComponent(component)

        if not removed_master and master_component is not None:
            for exdate in exdates:
                master_component.addProperty(Property("EXDATE", [exdate, ]))

    def voterComponentForVoter(self, voter):
        """
        Find the VVOTER subcomponent with a VOTER property matching the specified attendee (voter).

        @param voter: the calendar user address of the attendee (voter) to match
        @type voter: L{str}
        """
        for voterComponent in tuple(self.subcomponents(ignore=True)):
            if voterComponent.name() == "VVOTER" and voterComponent.getVoterProperty((voter,)) is not None:
                return voterComponent
        else:
            return None

    def voteMap(self):
        """
        Get a dict mapping each VOTE component POLL-ITEM-ID to the VOTE component.
        """
        results = {}
        for component in self.subcomponents():
            if component.name() == "VOTE":
                poll_id = component.propertyValue("POLL-ITEM-ID")
                if poll_id is not None:
                    results[poll_id] = component
        return results

    def filterComponents(self, rids):

        # If master is in rids do nothing
        if not rids or None in rids:
            return True

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        # Remove components not in the list
        components = tuple(self.subcomponents(ignore=True))
        remaining = len(components)
        for component in components:
            rid = component.getRecurrenceIDUTC()
            if rid not in rids:
                self.removeComponent(component)
                remaining -= 1

        return remaining != 0

    def removeAllButOneAttendee(self, attendee):
        """
        Remove all ATTENDEE properties except for the one specified.
        """

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        for component in self.subcomponents(ignore=True):
            if component.name() == "VPOLL":
                for vvoter in tuple(self.subcomponents()):
                    if vvoter.name() == "VVOTER":
                        if vvoter.propertyValue(component.recipientPropertyName()).lower() != attendee.lower():
                            component.removeComponent(vvoter)
            else:
                for p in tuple(component.properties(component.recipientPropertyName())):
                    if p.value().lower() != attendee.lower():
                        component.removeProperty(p)

    def removeAllButTheseAttendees(self, attendees):
        """
        Remove all ATTENDEE properties except for the ones specified.
        """

        assert self.name() == "VCALENDAR", "Not a calendar: {0!r}".format(self,)

        attendees = set([attendee.lower() for attendee in attendees])

        for component in self.subcomponents(ignore=True):
            if component.name() == "VPOLL":
                for vvoter in tuple(self.subcomponents()):
                    if vvoter.name() == "VVOTER":
                        if vvoter.propertyValue(component.recipientPropertyName()).lower() not in attendees:
                            component.removeComponent(vvoter)
            else:
                for p in tuple(component.properties(component.recipientPropertyName())):
                    if p.value().lower() not in attendees:
                        component.removeProperty(p)

    def hasAlarm(self):
        """
        Test whether the component has a VALARM as an immediate sub-component.
        """
        assert self.name().upper() in ("VEVENT", "VTODO",), "Not a VEVENT or VTODO: {0!r}".format(self,)

        for component in self.subcomponents():
            if component.name().upper() == "VALARM":
                return True
        return False

    def addAlarms(self, alarm, ignoreActionNone=True):
        """
        Add an alarm to any VEVENT or VTODO subcomponents that do not already have any.

        @param alarm: the text for a VALARM component
        @type alarm: C{str}

        @param ignoreActionNone: whether or not to skip ACTION:NONE alarms
        @type ignoreActionNone: C{bool}

        @return: indicate whether a change was made
        @rtype: C{bool}
        """

        # Create a fake component for the alarm text
        caldata = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:bogus
DTSTART:20110427T000000Z
DURATION:PT1H
DTSTAMP:20110427T000000Z
SUMMARY:bogus
{0}END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").format(alarm,)

        try:
            calendar = Component.fromString(caldata)
            if calendar is None:
                return False
        except ValueError:
            return False

        try:
            valarm = tuple(tuple(calendar.subcomponents())[0].subcomponents())[0]
        except IndexError:
            return False

        # Need to add property to indicate this was added by the server
        valarm.addProperty(Property("X-APPLE-DEFAULT-ALARM", "TRUE"))

        # ACTION:NONE not added
        changed = False
        action = valarm.propertyValue("ACTION")
        if not ignoreActionNone or action and action.upper() != "NONE":
            for component in self.subcomponents():
                if component.name().upper() not in ("VEVENT", "VTODO",):
                    continue
                if component.hasAlarm():
                    continue
                component.addComponent(valarm.duplicate())
                changed = True

        return changed

    def removeAlarms(self):
        """
        Remove all Alarms components
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.removeAlarms()
        else:
            for component in tuple(self.subcomponents()):
                if component.name() == "VALARM":
                    self.removeComponent(component)

    def hasDuplicateAlarms(self, doFix=False):
        """
        Test and optionally remove alarms that have the same ACTION and TRIGGER values in the same component.
        """
        changed = False
        if self.name() in ("VCALENDAR", PERUSER_COMPONENT,):
            for component in self.subcomponents():
                if component.name() in ("VTIMEZONE",):
                    continue
                changed = component.hasDuplicateAlarms(doFix) or changed
        else:
            action_trigger = set()
            for component in tuple(self.subcomponents()):
                if component.name() == "VALARM":
                    item = (component.propertyValue("ACTION"), component.propertyValue("TRIGGER"),)
                    if item in action_trigger:
                        if doFix:
                            self.removeComponent(component)
                        changed = True
                    else:
                        action_trigger.add(item)
        return changed

    def filterProperties(self, remove=None, keep=None, do_subcomponents=True):
        """
        Remove all properties that do not match the provided set.
        """

        if do_subcomponents:
            for component in self.subcomponents():
                component.filterProperties(remove, keep, do_subcomponents=False)
        else:
            if self.ignored():
                return

            for p in tuple(self.properties()):
                if (keep and p.name() not in keep) or (remove and p.name() in remove):
                    self.removeProperty(p)

    def removeXComponents(self, keep_components=()):
        """
        Remove all X- components except the specified ones
        """

        for component in tuple(self.subcomponents()):
            if component.name().startswith("X-") and component.name() not in keep_components:
                self.removeComponent(component)

    def removeXProperties(self, keep_properties=(), keep_parameters={}, do_subcomponents=True):
        """
        Remove all X- properties except the specified ones
        """

        if do_subcomponents and self.name() == "VCALENDAR":
            for component in self.subcomponents():
                component.removeXProperties(keep_properties, keep_parameters, do_subcomponents=False)
        else:
            if self.ignored():
                return
            for p in tuple(self.properties()):
                xpname = p.name().startswith("X-")
                if xpname and p.name() not in keep_properties:
                    self.removeProperty(p)
                elif not xpname:
                    preserve = keep_parameters.get(p.name(), set())
                    preserve.update(keep_parameters.get("", set()))
                    for paramname in p.parameterNames():
                        if paramname.startswith("X-") and paramname not in preserve:
                            p.removeParameter(paramname)

    def removePropertyParameters(self, property, params):
        """
        Remove all specified property parameters
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.removePropertyParameters(property, params)
        else:
            props = self.properties(property)
            for prop in props:
                for param in params:
                    prop.removeParameter(param)
            if self.name() == "VPOLL":
                for component in self.subcomponents(ignore=True):
                    component.removePropertyParameters(property, params)

    def removePropertyParametersByValue(self, property, paramvalues):
        """
        Remove all specified property parameters
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.removePropertyParametersByValue(property, paramvalues)
        else:
            props = self.properties(property)
            for prop in props:
                for param, value in paramvalues:
                    prop.removeParameterValue(param, value)
            if self.name() == "VPOLL":
                for component in self.subcomponents(ignore=True):
                    component.removePropertyParametersByValue(property, paramvalues)

    def getITIPInfo(self):
        """
        Get property value details needed to synchronize iTIP components.

        @return: C{tuple} of (uid, seq, dtstamp, r-id) some of which may be C{None} if property does not exist
        """
        try:
            # Extract items from component
            uid = self.propertyValue("UID")
            seq = self.propertyValue("SEQUENCE", 0)
            dtstamp = self.propertyValue("DTSTAMP")
            rid = self.propertyValue("RECURRENCE-ID")

        except ValueError:
            return (None, None, None, None)

        return (uid, seq, dtstamp, rid)

    @staticmethod
    def compareComponentsForITIP(component1, component2, use_dtstamp=True):
        """
        Compare synchronization information for two components to see if they match according to iTIP.

        @param component1: first component to check.
        @type component1: L{Component}
        @param component2: second component to check.
        @type component2: L{Component}
        @param use_dtstamp: whether DTSTAMP is used in addition to SEQUENCE.
        @type component2: C{bool}

        @return: 0, 1, -1 as per compareSyncInfo.
        """
        info1 = (None,) + Component.getITIPInfo(component1)
        info2 = (None,) + Component.getITIPInfo(component2)
        return Component.compareITIPInfo(info1, info2, use_dtstamp)

    @staticmethod
    def compareITIPInfo(info1, info2, use_dtstamp=True):
        """
        Compare two synchronization information records.

        @param info1: a C{tuple} as returned by L{getSyncInfo}.
        @param info2: a C{tuple} as returned by L{getSyncInfo}.
        @return: 1 if info1 > info2, 0 if info1 == info2, -1 if info1 < info2
        """

        _ignore_name1, uid1, seq1, dtstamp1, _ignore_rid1 = info1
        _ignore_name2, uid2, seq2, dtstamp2, _ignore_rid2 = info2

        # UIDs MUST match
        assert uid1 == uid2

        # Look for sequence
        if seq1 > seq2:
            return 1
        if seq1 < seq2:
            return -1

        # Look for DTSTAMP
        if use_dtstamp:
            if (dtstamp1 is not None) and (dtstamp2 is not None):
                if dtstamp1 > dtstamp2:
                    return 1
                if dtstamp1 < dtstamp2:
                    return -1
            elif (dtstamp1 is not None) and (dtstamp2 is None):
                return 1
            elif (dtstamp1 is None) and (dtstamp2 is not None):
                return -1

        return 0

    def needsiTIPSequenceChange(self, oldcalendar):
        """
        Compare this calendar with the old one and indicate whether the current one has SEQUENCE
        that is always greater than the old.
        """

        for component in self.subcomponents(ignore=True):
            oldcomponent = oldcalendar.overriddenComponent(component.getRecurrenceIDUTC())
            if oldcomponent is None:
                oldcomponent = oldcalendar.masterComponent()
                if oldcomponent is None:
                    continue
            newseq = component.propertyValue("SEQUENCE", 0)
            oldseq = oldcomponent.propertyValue("SEQUENCE", 0)
            if newseq <= oldseq:
                return True

        return False

    def bumpiTIPInfo(self, oldcalendar=None, doSequence=False):
        """
        Change DTSTAMP and optionally SEQUENCE on all components.
        """

        if doSequence:

            def maxSequence(calendar):
                seqs = calendar.getAllPropertiesInAnyComponent("SEQUENCE", depth=1)
                return max(seqs, key=lambda x: x.value()).value() if seqs else 0

            # Determine value to bump to from old calendar (if exists) or self
            newseq = maxSequence(oldcalendar if oldcalendar is not None else self) + 1

            # Bump all components
            self.replacePropertyInAllComponents(Property("SEQUENCE", newseq))

        self.replacePropertyInAllComponents(Property("DTSTAMP", DateTime.getNowUTC()))

    def sequenceInSync(self, oldcalendar):
        """
        Make sure SEQUENCE does not decrease in any components.
        """

        def maxSequence(calendar):
            seqs = calendar.getAllPropertiesInAnyComponent("SEQUENCE", depth=1)
            return max(seqs, key=lambda x: x.value()).value() if seqs else 0

        def minSequence(calendar):
            seqs = calendar.getAllPropertiesInAnyComponent("SEQUENCE", depth=1)
            return min(seqs, key=lambda x: x.value()).value() if seqs else 0

        # Determine value to bump to from old calendar (if exists) or self
        oldseq = maxSequence(oldcalendar)
        currentseq = minSequence(self)

        # Sync all components
        if oldseq and currentseq < oldseq:
            self.replacePropertyInAllComponents(Property("SEQUENCE", oldseq))

    def normalizeAll(self):

        # Normalize all properties
        for prop in tuple(self.properties()):
            result = normalizeProps.get(prop.name())
            if result:
                default_value, default_params = result
            else:
                # Assume default VALUE is TEXT
                default_value = None
                default_params = {"VALUE": "TEXT"}

            # Remove any default parameters
            for name in prop.parameterNames():
                value = prop.parameterValue(name)
                if value == default_params.get(name):
                    prop.removeParameter(name)

            # If there are no parameters, remove the property if it has the default value
            if len(prop.parameterNames()) == 0:
                if default_value is not None and prop.value() == default_value:
                    self.removeProperty(prop)
                    continue

            # Otherwise look for value normalization
            normalize_function = normalizePropsValue.get(prop.name())
            if normalize_function:
                prop.setValue(normalize_function(prop.value()))

        # Do datetime/rrule normalization
        self.normalizeDateTimes()

        # Do to all sub-components too
        for component in self.subcomponents():
            component.normalizeAll()

    def normalizeDateTimes(self):
        """
        Normalize various datetime properties into UTC and handle DTEND/DURATION variants in such
        a way that we can compare objects with slight differences.

        Also normalize the RRULE value parts.

        Strictly speaking we should not need to do this as clients should not be messing with
        these properties - i.e. they should round trip them. Unfortunately some do...
        """

        # TODO: what about VJOURNAL and VTODO?
        if self.name() == "VEVENT":

            # Basic time properties
            dtstart = self.getProperty("DTSTART")
            dtend = self.getProperty("DTEND")
            duration = self.getProperty("DURATION")

            timeRange = Period(
                start=dtstart.value(),
                end=dtend.value() if dtend is not None else None,
                duration=duration.value() if duration is not None else None,
            )

            # Have to fake the TZID value here when we convert date-times to UTC
            # as we need to know what the original one was
            if dtstart.hasParameter("TZID"):
                dtstart.setParameter("_TZID", dtstart.parameterValue("TZID"))
                dtstart.removeParameter("TZID")
            dtstart.value().adjustToUTC()
            if dtend is not None:
                if dtend.hasParameter("TZID"):
                    dtend.setParameter("_TZID", dtend.parameterValue("TZID"))
                    dtend.removeParameter("TZID")
                dtend.value().adjustToUTC()
            elif duration is not None:
                self.removeProperty(duration)
                self.addProperty(Property("DTEND", timeRange.getEnd().duplicateAsUTC()))

            rdates = self.properties("RDATE")
            for rdate in rdates:
                if rdate.hasParameter("TZID"):
                    rdate.setParameter("_TZID", rdate.parameterValue("TZID"))
                    rdate.removeParameter("TZID")
                for value in rdate.value():
                    value.getValue().adjustToUTC()

            exdates = self.properties("EXDATE")
            for exdate in exdates:
                if exdate.hasParameter("TZID"):
                    exdate.setParameter("_TZID", exdate.parameterValue("TZID"))
                    exdate.removeParameter("TZID")
                for value in exdate.value():
                    value.getValue().adjustToUTC()

            rid = self.getProperty("RECURRENCE-ID")
            if rid is not None:
                rid.removeParameter("TZID")
                rid.setValue(rid.value().duplicateAsUTC())

            # Recurrence rules - we need to normalize the order of the value parts
#            for rrule in self._pycalendar.getRecurrenceSet().getRules():
#                indexedTokens = {}
#                indexedTokens.update([valuePart.split("=") for valuePart in rrule.value().split(";")])
#                sortedValue = ";".join(["%s=%s" % (key, value,) for key, value in sorted(indexedTokens.iteritems(), key=lambda x:x[0])])
#                rrule.setValue(sortedValue)

    def normalizePropertyValueLists(self, propname):
        """
        Convert properties that have a list of values into single properties, to make it easier
        to do comparisons between two ical objects.
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.normalizePropertyValueLists(propname)
        else:
            for prop in tuple(self.properties(propname)):
                if type(prop.value()) is list and len(prop.value()) > 1:
                    self.removeProperty(prop)
                    for value in prop.value():
                        self.addProperty(Property(propname, [value.getValue(), ]))

    def normalizeAttachments(self):
        """
        Remove any ATTACH properties that relate to a dropbox.
        """

        if self.name() == "VCALENDAR":
            for component in self.subcomponents(ignore=True):
                component.normalizeAttachments()
        else:
            dropboxPrefix = self.propertyValue("X-APPLE-DROPBOX")
            if dropboxPrefix is None:
                return
            for attachment in tuple(self.properties("ATTACH")):
                valueType = attachment.parameterValue("VALUE")
                if valueType in (None, "URI"):
                    dataValue = attachment.value()
                    if dataValue.find(dropboxPrefix) != -1:
                        self.removeProperty(attachment)

    @inlineCallbacks
    def normalizeCalendarUserAddresses(
        self, lookupFunction, recordFunction, toCanonical=True, toURN_UUID=False,
    ):
        """
        Do the ORGANIZER/ATTENDEE property normalization.

        @param lookupFunction: function returning full name, guid, CUAs for a given CUA
        @type lookupFunction: L{Function}

        @param recordFunction: function taking a CUA and returning a record
        @type recordFunction: L{Function}

        @param toCanonical: whether to convert to the canonical CUA form (True)
            or to the mailto: form (False)
        @type toCanonical: L{bool}

        @param toURN_UUID: whether to convert to urn:x-uid to urn:uuid: for
            compatibility with older servers
        @type toURN_UUID: L{bool}
        """

        # Keep a cache of records because events with lots of recurrence overrides can contain
        # the same attendee cu-address multiple times
        cache = {}

        for component in self.subcomponents(ignore=True):
            for prop in itertools.chain(
                component.properties("ORGANIZER"),
                component.properties("ATTENDEE"),
                component.properties("VOTER")
            ):

                # Check that we can lookup this calendar user address - if not
                # we cannot do anything with it
                cuaddr = normalizeCUAddr(prop.value())
                if cuaddr not in cache:
                    result = yield lookupFunction(cuaddr, recordFunction, config)
                    cache[cuaddr] = result

                name, uid, cutype, cuaddrs = cache[cuaddr]
                if uid is None:
                    continue

                # Get any EMAIL parameter
                oldemail = prop.parameterValue("EMAIL")
                if oldemail:
                    oldemail = "mailto:{0}".format(oldemail,)

                    if config.Scheduling.Options.FakeResourceLocationEmail:
                        if oldemail.endswith("@do_not_reply"):
                            oldemail = None

                # Get any CN parameter
                oldCN = prop.parameterValue("CN")

                if toCanonical:
                    # Always re-write value to urn:x-uid
                    prop.setValue("urn:x-uid:{uid}".format(uid=uid))

                # Look for urn:x-uid: -> urn:uuid: conversion
                elif toURN_UUID and cuaddr.startswith("urn:x-uid:"):
                    prop.setValue(cuaddr.replace("urn:x-uid:", "urn:uuid:"))

                # If it is already a non-x-uid address leave it be
                elif (cuaddr.startswith("urn:x-uid:") or cuaddr.startswith("urn:uuid:")):

                    if oldemail:
                        # Use the EMAIL parameter if it exists
                        newaddr = oldemail
                    else:
                        # Pick the first mailto,
                        # or failing that the first path one,
                        # or failing that the first http one,
                        # or failing that the first one
                        first_mailto = None
                        first_path = None
                        first_http = None
                        first = None
                        for addr in cuaddrs:
                            if addr.startswith("mailto:"):
                                first_mailto = addr
                                break
                            elif addr.startswith("/"):
                                if not first_path:
                                    first_path = addr
                            elif addr.startswith("http:"):
                                if not first_http:
                                    first_http = addr
                            elif not first:
                                first = addr

                        if first_mailto:
                            newaddr = first_mailto
                        elif first_path:
                            newaddr = first_path
                        elif first_http:
                            newaddr = first_http
                        elif first:
                            newaddr = first
                        else:
                            newaddr = None

                    # Make the change
                    if newaddr:
                        prop.setValue(newaddr)

                # Re-write the CN parameter
                if name:
                    if name != oldCN:
                        prop.setParameter("CN", name)
                else:
                    prop.removeParameter("CN")

                # Re-write the EMAIL if its value no longer matches
                if oldemail and oldemail not in cuaddrs or oldemail is None and toCanonical:
                    if cuaddr.startswith("mailto:") and cuaddr in cuaddrs:
                        email = cuaddr[7:]
                    else:
                        for addr in cuaddrs:
                            if addr.startswith("mailto:"):
                                email = addr[7:]
                                break
                        else:
                            email = None

                    if config.Scheduling.Options.FakeResourceLocationEmail:
                        if email and email.endswith("@do_not_reply"):
                            email = ""

                    if email:
                        prop.setParameter("EMAIL", email)
                    else:
                        prop.removeParameter("EMAIL")

                if cutype != prop.parameterValue("CUTYPE"):
                    if cutype and cutype != "INDIVIDUAL":
                        # For groups we need to change the CUTYPE exposed to clients when we have server-managed
                        # group attendee expansion because some clients seem to spontaneous remove CUTYPE=GROUP
                        # for no obvious reason when the event is changed. We can't have that happen for the
                        # server-managed groups so using a different CUTYPE seems to work around that.
                        if config.GroupAttendees.Enabled and cutype == "GROUP":
                            cutype = "X-SERVER-GROUP"
                        prop.setParameter("CUTYPE", cutype)
                    else:
                        prop.removeParameter("CUTYPE")

            # For VPOLL also do immediate children
            if component.name() == "VPOLL":
                yield component.normalizeCalendarUserAddresses(lookupFunction, recordFunction, toCanonical)

    def _reconcileGroupAttendee(self, groupCUA, memberAttendeeProps):
        """
        Make sure there are attendee properties for every member of the group, and no
        other attendee properties marked as a member of the group. Note that attendee
        properties already present with a MEMBER parameter are not given a MEMBER
        parameter if they are in the group. This ensures that manually added attendees
        are not automatically removed when they dissappear from a group.

        @param groupCUA: calendar user address of the group
        @type groupCUA: L{str}
        @param memberAttendeeProps: list of member properties
        @type memberAttendeeProps: L{tuple}
        """

        changed = False
        for component in self.subcomponents(ignore=True):
            oldAttendeeProps = tuple(component.properties("ATTENDEE"))
            oldAttendeeCUAs = set([attendeeProp.value() for attendeeProp in oldAttendeeProps])

            # add new member attendees
            memberCUAs = set()
            for newAttendeeProp in memberAttendeeProps:
                memberCUA = newAttendeeProp.value()
                if memberCUA not in oldAttendeeCUAs:
                    log.debug("Group reconciliation: Adding {m} ({g})", m=memberCUA, g=groupCUA)
                    component.addProperty(newAttendeeProp)
                    changed = True
                memberCUAs.add(memberCUA)

            # remove attendee or update MEMBER attribute for non-primary attendees in this group,
            for attendeeProp in oldAttendeeProps:
                if attendeeProp.hasParameter("MEMBER"):
                    memberValues = attendeeProp.parameterValues("MEMBER")
                    if groupCUA in tuple(memberValues):
                        if attendeeProp.value() not in memberCUAs:
                            memberValues.remove(groupCUA)
                            if len(memberValues) == 0:
                                component.removeProperty(attendeeProp)
                                log.debug("Group reconciliation: removing {a} ({g})", a=attendeeProp.value(), g=groupCUA)
                            changed = True
                    else:
                        if attendeeProp.value() in memberCUAs:
                            memberValues.append(groupCUA)
                            changed = True

        return changed

    def reconcileGroupAttendees(self, groupCUAToAttendeeMemberPropMap):
        """
        Reconcile the attendee properties in this L{Component}.

        @param groupCUAToAttendeeMemberPropMap: map of group to potential attendees
        @type groupCUAToAttendeeMemberPropMap: L{dict}
        """

        # Reconcile the member ship list of each group attendee, keeping track of which
        # groups are actually used
        changed = False
        allMemberCUAs = set()
        nonemptyGroupCUAs = set()
        for groupCUA, memberAttendeeProps in groupCUAToAttendeeMemberPropMap.iteritems():
            changed |= self._reconcileGroupAttendee(groupCUA, memberAttendeeProps)
            allMemberCUAs |= set([memberAttendeeProp.value() for memberAttendeeProp in memberAttendeeProps])
            if memberAttendeeProps:
                nonemptyGroupCUAs.add(groupCUA)

        # Remove attendee properties that have a MEMBER value that contains only groups no longer
        # used in this component
        for component in self.subcomponents(ignore=True):
            for attendeeProp in tuple(component.properties("ATTENDEE")):
                if attendeeProp.hasParameter("MEMBER"):
                    attendeeCUA = attendeeProp.value()
                    if attendeeCUA in allMemberCUAs:
                        # remove orphan member values
                        memberValues = attendeeProp.parameterValues("MEMBER")
                        for orphanGroupCUA in set(memberValues) - nonemptyGroupCUAs:
                            memberValues.remove(orphanGroupCUA)
                            if len(memberValues) == 0:
                                component.removeProperty(attendeeProp)
                            changed = True
                    else:
                        # remove orphaned member property
                        component.removeProperty(attendeeProp)
                        changed = True

        return changed

    def adjustedTransp(self):
        """
        Determine the TRANSP value for this component. Note that for all-day VEVENTs
        we are going to treat the default as TRANSPARENT and not OPAQUE
        """
        transp = self.propertyValue("TRANSP")
        if transp is None and self.name() == "VEVENT" and self.propertyValue("DTSTART").isDateOnly():
            return "TRANSPARENT"
        else:
            return "OPAQUE" if transp is None else transp

    def allPerUserUIDs(self):

        results = set()
        for component in self.subcomponents():
            if component.name() == PERUSER_COMPONENT:
                results.add(component.propertyValue(PERUSER_UID))
        return results

    def perUserData(self, rid):

        # We will create a cache of all user/rid/transparency/adjusted_start/adjusted_end values as we will likely
        # be calling this a lot
        if not hasattr(self, "_perUserData"):
            self._perUserData = {}

            # Do per-user data
            for component in self.subcomponents():
                if component.name() == PERUSER_COMPONENT:
                    uid = component.propertyValue(PERUSER_UID)
                    for subcomponent in component.subcomponents():
                        if subcomponent.name() == PERINSTANCE_COMPONENT:
                            instancerid = subcomponent.propertyValue("RECURRENCE-ID")
                            transp = subcomponent.propertyValue("TRANSP") == "TRANSPARENT"
                            adjusted_start = subcomponent.propertyValue("X-APPLE-TRAVEL-DURATION")
                            adjusted_end = subcomponent.propertyValue("X-APPLE-TRAVEL-RETURN-DURATION")
                            self._perUserData.setdefault(uid, {})[instancerid] = (transp, adjusted_start, adjusted_end,)
                elif not component.ignored():
                    instancerid = component.propertyValue("RECURRENCE-ID")
                    transp = component.propertyValue("TRANSP") == "TRANSPARENT"
                    self._perUserData.setdefault("", {})[instancerid] = (transp, None, None,)

        # Now lookup in cache
        results = []
        for uid, cachedRids in sorted(self._perUserData.items(), key=lambda x: x[0]):
            lookupRid = rid
            if lookupRid not in cachedRids:
                lookupRid = None
            if lookupRid in cachedRids:
                results.append((uid, cachedRids[lookupRid],))
            else:
                results.append((uid, (False, None, None,)))

        return tuple(results)

    def hasInstancesAfter(self, limit):
        """
        Determine whether an event exists completely prior to a given moment.

        @param limit: the moment to compare against.
        @type limit: L{DateTime}

        @return: a C{bool}, True if the event has any instances occurring after
        limit, False otherwise.
        """
        instanceList = self.expandTimeRanges(limit)

        if instanceList.limit is not None:
            # There are instances after the limit
            return True

        # All instances begin prior to limit, but now check their end times to
        # see if they extend beyond limit
        for instance in instanceList.instances.itervalues():
            if instance.end > limit:
                return True

        # Exists completely prior to limit
        return False

    def hasDuplicatePrivateComments(self, doFix=False):
        """
        Test and optionally remove "X-CALENDARSERVER-ATTENDEE-COMMENT" properties that have the same
        "X-CALENDARSERVER-ATTENDEE-REF" parameter values in the same component.

        @return: C{True} if there are duplicates that were not fixed.
        """
        if self.name() == "VCALENDAR":
            for component in self.subcomponents():
                if component.name() in ("VTIMEZONE",):
                    continue
                if component.hasDuplicatePrivateComments(doFix):
                    return True
        else:
            attendee_refs = set()
            for prop in tuple(self.properties(ATTENDEE_COMMENT)):
                ref = prop.parameterValue(ATTENDEE_COMMENT_REF)
                if ref in attendee_refs:
                    if doFix:
                        self.removeProperty(prop)
                    else:
                        return True
                attendee_refs.add(ref)
        return False

    def repairMissingDatestampsFromComments(self):
        """
        Some clients are leaving out the datestamp from comments; this method
        adds this parameter where it's missing (using a value of now in UTC)
        """
        if self.name() == "VCALENDAR":
            for component in self.subcomponents():
                if component.name() in ("VTIMEZONE",):
                    continue
                component.repairMissingDatestampsFromComments()

        else:
            for prop in tuple(self.properties(ATTENDEE_COMMENT)):
                if not prop.hasParameter(DTSTAMP_PARAM):
                    prop.setParameter(DTSTAMP_PARAM, DateTime.getNowUTC().getText())

    def maxAttachmentsPerInstance(self):
        if self.name() == "VCALENDAR":
            count = 0
            for component in self.subcomponents():
                if component.name() in ("VTIMEZONE",):
                    continue
                count = max(count, component.maxAttachmentsPerInstance())
            return count
        else:
            return len(tuple(self.properties("ATTACH")))

# #
# Timezones
# #


def tzexpand(tzdata, start, end):
    """
    Expand a timezone to get onset/utc-offset observance tuples within the specified
    time range.

    @param tzdata: the iCalendar data containing a VTIMEZONE.
    @type tzdata: C{str}
    @param start: date for the start of the expansion.
    @type start: C{date}
    @param end: date for the end of the expansion.
    @type end: C{date}

    @return: a C{list} of tuples of (C{datetime}, C{str})
    """

    icalobj = Component.fromString(tzdata)
    tzcomp = None
    for comp in icalobj.subcomponents():
        if comp.name() == "VTIMEZONE":
            tzcomp = comp
            break
    else:
        raise InvalidICalendarDataError("No VTIMEZONE component in {0}".format(tzdata,))

    tzexpanded = tzcomp._pycalendar.expandAll(start, end)

    results = []

    # Always need to ensure the start appears in the result
    start.setDateOnly(False)
    if tzexpanded:
        if start != tzexpanded[0][0]:
            results.append((str(start), UTCOffsetValue(tzexpanded[0][2]).getText(),))
    else:
        results.append((str(start), UTCOffsetValue(tzcomp._pycalendar.getTimezoneOffsetSeconds(start)).getText(),))
    for tzstart, _ignore_utctzstart, _ignore_tzoffsetfrom, tzoffsetto in tzexpanded:
        results.append((
            tzstart.getText(),
            UTCOffsetValue(tzoffsetto).getText(),
        ))

    return results


def tzexpandlocal(tzdata, start, end, utc_onset=False):
    """
    Expand a timezone to get onset(local)/utc-offset-from/utc-offset-to/name observance tuples within the specified
    time range.

    @param tzdata: the iCalendar data containing a VTIMEZONE.
    @type tzdata: L{Calendar}
    @param start: date for the start of the expansion.
    @type start: C{date}
    @param end: date for the end of the expansion.
    @type end: C{date}
    @param utc_onset: whether or not onset values are in UTC.
    @type utc_onset: C{bool}

    @return: a C{list} of tuples
    """

    icalobj = Component(None, pycalendar=tzdata)
    tzcomp = None
    for comp in icalobj.subcomponents():
        if comp.name() == "VTIMEZONE":
            tzcomp = comp
            break
    else:
        raise InvalidICalendarDataError("No VTIMEZONE component in {0}".format(tzdata,))

    tzexpanded = tzcomp._pycalendar.expandAll(start, end, with_name=True)

    results = []

    # Always need to ensure the start appears in the result
    start.setDateOnly(False)
    if tzexpanded:
        if start != tzexpanded[0][0]:
            results.append((
                start,
                tzexpanded[0][2],
                tzexpanded[0][2],
                tzexpanded[0][4],
            ))
    else:
        results.append((
            start,
            tzcomp._pycalendar.getTimezoneOffsetSeconds(start),
            tzcomp._pycalendar.getTimezoneOffsetSeconds(start),
            tzcomp._pycalendar.getTimezoneDescriptor(start),
        ))
    for tzstart, utctzstart, tzoffsetfrom, tzoffsetto, name in tzexpanded:
        results.append((
            utctzstart if utc_onset else tzstart,
            tzoffsetfrom,
            tzoffsetto,
            name,
        ))

    return results


# #
# Utilities
# #

@inlineCallbacks
def normalizeCUAddress(cuaddr, lookupFunction, recordFunction, toCanonical=True, toURN_UUID=False):
    # Check that we can lookup this calendar user address - if not
    # we cannot do anything with it
    _ignore_name, uid, _ignore_cuType, cuaddrs = (yield lookupFunction(normalizeCUAddr(cuaddr), recordFunction, config))

    if toCanonical:
        # Always re-write value to urn:x-uid
        if uid:
            returnValue("urn:x-uid:{0}".format(uid,))

    # Look for urn:x-uid: -> urn:uuid: conversion
    elif toURN_UUID and cuaddr.startswith("urn:x-uid:"):
        returnValue(cuaddr.replace("urn:x-uid:", "urn:uuid:"))

    # If it is already a non-x-uid address leave it be
    elif (cuaddr.startswith("urn:x-uid:") or cuaddr.startswith("urn:uuid:")):

        # Pick the first mailto,
        # or failing that the first path one,
        # or failing that the first http one,
        # or failing that the first one
        first_mailto = None
        first_path = None
        first_http = None
        first = None
        for addr in cuaddrs:
            if addr.startswith("mailto:"):
                first_mailto = addr
                break
            elif addr.startswith("/"):
                if not first_path:
                    first_path = addr
            elif addr.startswith("http:"):
                if not first_http:
                    first_http = addr
            elif not first:
                first = addr

        if first_mailto:
            newaddr = first_mailto
        elif first_path:
            newaddr = first_path
        elif first_http:
            newaddr = first_http
        elif first:
            newaddr = first
        else:
            newaddr = None

        # Make the change
        if newaddr:
            returnValue(newaddr)

    returnValue(cuaddr)


#
# This function is from "Python Cookbook, 2d Ed., by Alex Martelli, Anna
# Martelli Ravenscroft, and David Ascher (O'Reilly Media, 2005) 0-596-00797-3."
#
def merge(*iterables):
    """
    Merge sorted iterables into one sorted iterable.
    @param iterables: arguments are iterables which yield items in sorted order.
    @return: an iterable of all items generated by every iterable in
    C{iterables} in sorted order.
    """
    heap = []
    for iterable in iterables:
        iterator = iter(iterable)
        for value in iterator:
            heap.append((value, iterator))
            break
    heapq.heapify(heap)
    while heap:
        value, iterator = heap[0]
        yield value
        for value in iterator:
            heapq.heapreplace(heap, (value, iterator))
            break
        else:
            heapq.heappop(heap)


def normalize_iCalStr(icalstr, filter_params=("ATTENDEE;X-CALENDARSERVER-DTSTAMP", "ATTENDEE;X-CALENDARSERVER-RESET-PARTSTAT",)):
    """
    Normalize a string representation of ical data for easy test comparison.
    """

    icalstr = str(icalstr).replace("\r\n ", "")
    icalstr = icalstr.replace("\n ", "")
    lines = [line for line in icalstr.splitlines() if not line.startswith("DTSTAMP")]
    for param in filter_params:
        propname, paramname = param.split(";")
        for ctr, line in enumerate(lines[:]):
            if line.startswith(propname + ";"):
                pos = line.find(";{}=".format(paramname))
                if pos != -1:
                    next_segment = line[pos + len(paramname) + 2:]
                    end_pos = next_segment.find(";")
                    if end_pos == -1:
                        end_pos = next_segment.find(":")
                    lines[ctr] = line[:pos] + line[pos + len(paramname) + 2 + end_pos:]
    icalstr = "\r\n".join(lines)
    return icalstr + "\r\n"


def diff_iCalStrs(icalstr1, icalstr2):

    icalstr1 = normalize_iCalStr(icalstr1).splitlines()
    icalstr2 = normalize_iCalStr(icalstr2).splitlines()
    return "\n".join(unified_diff(icalstr1, icalstr2))
