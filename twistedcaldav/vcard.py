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
vCard Utilities
"""

__all__ = [
    "InvalidVCardDataError",
    "Property",
    "Component",
]

import cStringIO as StringIO
import codecs

from twext.python.log import Logger
from txweb2.stream import IStream
from txweb2.dav.util import allDataFromStream

from twistedcaldav.config import config

from pycalendar.parameter import Parameter
from pycalendar.componentbase import ComponentBase
from pycalendar.exceptions import ErrorBase
from pycalendar.vcard.card import Card
from pycalendar.vcard.property import Property as pyProperty

log = Logger()

vCardProductID = "-//CALENDARSERVER.ORG//NONSGML Version 1//EN"


class InvalidVCardDataError(ValueError):
    pass


class Property (object):
    """
    vCard Property
    """

    def __init__(self, name, value, params={}, group=None, **kwargs):
        """
        @param name: the property's name
        @param value: the property's value
        @param params: a dictionary of parameters, where keys are parameter names and
            values are (possibly empty) lists of parameter values.
        """
        if name is None:
            assert value is None
            assert params is None

            pyobj = kwargs["pycard"]

            if not isinstance(pyobj, pyProperty):
                raise TypeError("Not a pyProperty: %r" % (property,))

            self._pycard = pyobj
        else:
            # Convert params dictionary to list of lists format used by pycalendar
            if isinstance(value, unicode):
                value = value.encode("utf-8")
            self._pycard = pyProperty(group=group, name=name, value=value)
            for attrname, attrvalue in params.items():
                if isinstance(attrvalue, unicode):
                    attrvalue = attrvalue.encode("utf-8")
                self._pycard.addParameter(Parameter(attrname, attrvalue))

    def __str__(self):
        return str(self._pycard)

    def __repr__(self):
        return "<%s: %r: %r>" % (self.__class__.__name__, self.name(), self.value())

    def __hash__(self):
        return hash(str(self))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if not isinstance(other, Property):
            return False
        return self._pycard == other._pycard

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
        @return: the duplicated vcard.
        """
        return Property(None, None, params=None, pycard=self._pycard.duplicate())

    def name(self):
        return self._pycard.getName()

    def value(self):
        return self._pycard.getValue().getValue()

    def strvalue(self):
        return str(self._pycard.getValue())

    def setValue(self, value):
        self._pycard.setValue(value)

    def parameterNames(self):
        """
        Returns a set containing parameter names for this property.
        """
        result = set()
        for pyattrlist in self._pycard.getParameters().values():
            for pyattr in pyattrlist:
                result.add(pyattr.getName())
        return result

    def parameterValue(self, name, default=None):
        """
        Returns a single value for the given parameter.  Raises
        InvalidICalendarDataError if the parameter has more than one value.
        """
        try:
            return self._pycard.getParameterValue(name)
        except KeyError:
            return default

    def parameterValues(self, name):
        """
        Returns a single value for the given parameter.  Raises
        InvalidICalendarDataError if the parameter has more than one value.
        """
        results = []
        try:
            attrs = self._pycard.getParameters()[name.upper()]
        except KeyError:
            return []

        for attr in attrs:
            results.extend(attr.getValues())
        return results

    def hasParameter(self, paramname):
        return self._pycard.hasParameter(paramname)

    def setParameter(self, paramname, paramvalue):
        self._pycard.replaceParameter(Parameter(paramname, paramvalue))

    def removeParameter(self, paramname):
        self._pycard.removeParameters(paramname)

    def removeAllParameters(self):
        self._pycard.setParameters({})

    def removeParameterValue(self, paramname, paramvalue):

        paramname = paramname.upper()
        for attrName in self.parameterNames():
            if attrName.upper() == paramname:
                for attr in tuple(self._pycard.getParameters()[attrName]):
                    for value in attr.getValues():
                        if value == paramvalue:
                            if not attr.removeValue(value):
                                self._pycard.removeParameters(paramname)


class Component (object):
    """
    X{vCard} component.
    """
    allowedTypesList = None

    @classmethod
    def allowedTypes(cls):
        if cls.allowedTypesList is None:
            cls.allowedTypesList = ["text/vcard"]
            if config.EnableJSONData:
                cls.allowedTypesList.append("application/vcard+json")
        return cls.allowedTypesList

    @classmethod
    def allFromString(clazz, string, format=None):
        """
        FIXME: Just default to reading a single VCARD - actually need more
        """
        if type(string) is unicode:
            string = string.encode("utf-8")
        else:
            # Valid utf-8 please
            string.decode("utf-8")

        # No BOMs please
        if string[:3] == codecs.BOM_UTF8:
            string = string[3:]

        return clazz.allFromStream(StringIO.StringIO(string), format)

    @classmethod
    def allFromStream(clazz, stream, format=None):
        """
        FIXME: Just default to reading a single VCARD - actually need more
        """
        try:
            results = Card.parseMultipleData(stream, format)
        except ErrorBase:
            results = None
        if not results:
            stream.seek(0)
            raise InvalidVCardDataError("%s" % (stream.read(),))
        return [clazz(None, pycard=result) for result in results]

    @classmethod
    def fromString(clazz, string, format=None):
        """
        Construct a L{Component} from a string.
        @param string: a string containing vCard data.
        @return: a L{Component} representing the first component described by
            C{string}.
        """
        return clazz._fromData(string, False, format)

    @classmethod
    def fromStream(clazz, stream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: a C{read()}able stream containing vCard data.
        @return: a L{Component} representing the first component described by
            C{stream}.
        """
        return clazz._fromData(stream, True, format)

    @classmethod
    def _fromData(clazz, data, isstream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: a C{read()}able stream containing vCard data.
        @param format: a C{str} indicating whether the data is vCard or jCard
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
            result = Card.parseData(data, format)
        except ErrorBase as e:
            errmsg = "%s: %s" % (e.mReason, e.mData,)
            result = None
        if not result:
            if isstream:
                data.seek(0)
                data = data.read()
            raise InvalidVCardDataError("%s\n%s" % (errmsg, data,))
        return clazz(None, pycard=result)

    @classmethod
    def fromIStream(clazz, stream, format=None):
        """
        Construct a L{Component} from a stream.
        @param stream: an L{IStream} containing vCard data.
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

    def __init__(self, name, **kwargs):
        """
        Use this constructor to initialize an empty L{Component}.
        To create a new L{Component} from X{vCard} data, don't use this
        constructor directly.  Use one of the factory methods instead.
        @param name: the name (L{str}) of the X{iCalendar} component type for the
            component.
        """
        if name is None:
            if "pycard" in kwargs:
                pyobj = kwargs["pycard"]

                if pyobj is not None:
                    if not isinstance(pyobj, ComponentBase):
                        raise TypeError("Not a ComponentBase: %r" % (pyobj,))

                self._pycard = pyobj
            else:
                raise AssertionError("name may not be None")

            # FIXME: _parent is not use internally, and appears to be used elsewhere,
            # even though it's names as a private variable.
            if "parent" in kwargs:
                parent = kwargs["parent"]

                if parent is not None:
                    if not isinstance(parent, Component):
                        raise TypeError("Not a Component: %r" % (parent,))

                self._parent = parent
            else:
                self._parent = None
        elif name == "VCARD":
            self._pycard = Card(add_defaults=False)
            self._parent = None
        else:
            raise ValueError("VCards have no child components")

    def __str__(self):
        return str(self._pycard)

    def __repr__(self):
        return "<%s: %r>" % (self.__class__.__name__, str(self._pycard))

    def __hash__(self):
        return hash(str(self))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __eq__(self, other):
        if not isinstance(other, Component):
            return False
        return self._pycard == other._pycard

    def getText(self, format=None):
        """
        Return text representation
        """
        assert self.name() == "VCARD", "Must be a VCARD: %r" % (self,)

        result = self._pycard.getText(format)
        if result is None:
            raise ValueError("Unknown format requested for address data.")
        return result

    # FIXME: Should this not be in __eq__?
    def same(self, other):
        return self._pycard == other._pycard

    def name(self):
        """
        @return: the name of the vCard type of this component.
        """
        return self._pycard.getType()

    def duplicate(self):
        """
        Duplicate this object and all its contents.
        @return: the duplicated vcard.
        """
        return Component(None, pycard=self._pycard.duplicate())

    def hasProperty(self, name):
        """
        @param name: the name of the property whose existence is being tested.
        @return: True if the named property exists, False otherwise.
        """
        return self._pycard.hasProperty(name)

    def getProperty(self, name):
        """
        Get one property from the property list.
        @param name: the name of the property to get.
        @return: the L{Property} found or None.
        @raise: L{ValueError} if there is more than one property of the given name.
        """
        properties = tuple(self.properties(name))
        if len(properties) == 1:
            return properties[0]
        if len(properties) > 1:
            raise InvalidVCardDataError("More than one %s property in component %r" % (name, self))
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
            [properties.extend(i) for i in self._pycard.getProperties().values()]
        elif self._pycard.countProperty(name) > 0:
            properties = self._pycard.getProperties(name)

        return (
            Property(None, None, None, pycard=p)
            for p in properties
        )

    def propertyValue(self, name):
        properties = tuple(self.properties(name))
        if len(properties) == 1:
            return properties[0].value()
        if len(properties) > 1:
            raise InvalidVCardDataError("More than one %s property in component %r" % (name, self))
        return None

    def addProperty(self, property):
        """
        Adds a property to this component.
        @param property: the L{Property} to add to this component.
        """
        self._pycard.addProperty(property._pycard)
        self._pycard.finalise()

    def removeProperty(self, property):
        """
        Remove a property from this component.
        @param property: the L{Property} to remove from this component.
        """
        self._pycard.removeProperty(property._pycard)
        self._pycard.finalise()

    def removeProperties(self, name):
        """
        remove all properties with name
        @param name: the name of the properties to remove.
        """
        self._pycard.removeProperties(name)

    def replaceProperty(self, property):
        """
        Add or replace a property in this component.
        @param property: the L{Property} to add or replace in this component.
        """

        # Remove all existing ones first
        self._pycard.removeProperties(property.name())
        self.addProperty(property)

    def resourceUID(self):
        """
        @return: the UID of the subcomponents in this component.
        """
        assert self.name() == "VCARD", "Not a vcard: %r" % (self,)

        if not hasattr(self, "_resource_uid"):
            self._resource_uid = self.propertyValue("UID")

        return self._resource_uid

    def resourceKind(self):
        """
        @return: the kind of the subcomponents in this component.
        """
        assert self.name() == "VCARD", "Not a vcard: %r" % (self,)

        if not hasattr(self, "_resource_kind"):
            self._resource_kind = self.propertyValue("X-ADDRESSBOOKSERVER-KIND")

        return self._resource_kind

    def resourceMemberAddresses(self):
        """
        @return: an iterable of X-ADDRESSBOOKSERVER-MEMBER property values
        """
        assert self.name() == "VCARD", "Not a vcard: %r" % (self,)

        return [prop.value() for prop in list(self.properties("X-ADDRESSBOOKSERVER-MEMBER"))]

    def validVCardData(self, doFix=True, doRaise=True):
        """
        @return: tuple of fixed, unfixed issues
        @raise InvalidVCardDataError: if the given vcard data is not valid.
        """
        if self.name() != "VCARD":
            log.debug("Not a vcard: {s!r}", s=self)
            raise InvalidVCardDataError("Not a vcard")

        # Do underlying vCard library validation with data fix
        fixed, unfixed = self._pycard.validate(doFix=doFix)
        if unfixed:
            log.debug("vCard data had unfixable problems:\n  {problems}", problems="\n  ".join(unfixed))
            if doRaise:
                raise InvalidVCardDataError("Address data had unfixable problems:\n  %s" % ("\n  ".join(unfixed),))
        if fixed:
            log.debug("vCard data had fixable problems:\n  {problems}", problems="\n  ".join(fixed))

        return fixed, unfixed

    def validForCardDAV(self):
        """
        @raise ValueError: if the given vcard data is not valid.
        """
        if self.name() != "VCARD":
            raise InvalidVCardDataError("Not a vcard")

        version = self.propertyValue("VERSION")
        if version != "3.0":
            raise InvalidVCardDataError("Not a version 2.0 vCard (version=%s)" % (version,))

        uid = self.propertyValue("UID")
        if uid is None:
            raise InvalidVCardDataError("All vCards must have UIDs")

        # Control character check - only HTAB, CR, LF allowed for characters in the range 0x00-0x1F
        s = str(self)
        if len(s.translate(None, "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0B\x0C\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\x1B\x1C\x1D\x1E\x1F")) != len(s):
            raise InvalidVCardDataError("vCard contains illegal control character")
