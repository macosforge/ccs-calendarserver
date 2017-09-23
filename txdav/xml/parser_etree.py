##
# Copyright (c) 2012-2017 Apple Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
##

"""
ElementTree implementation of XML parser/generator for WebDAV documents.
"""

__all__ = [
    "WebDAVDocument",
]

from xml.etree.ElementTree import TreeBuilder, XMLParser, \
    _namespace_map
from txdav.xml.base import WebDAVUnknownElement, PCDATAElement
from txdav.xml.base import _elements_by_qname
from txdav.xml.parser_base import AbstractWebDAVDocument

try:
    from xml.etree.ElementTree import ParseError as XMLParseError
except ImportError:
    from xml.parsers.expat import ExpatError as XMLParseError


def QNameSplit(qname):
    return tuple(qname[1:].split("}", 1)) if "}" in qname else ("", qname,)


class WebDAVContentHandler (TreeBuilder):

    def __init__(self):
        TreeBuilder.__init__(self)
        self._characterBuffer = None

        self.startDocument()

    def doctype(self, name, pubid, system):
        """
        Doctype declaration is ignored.
        """

    def startDocument(self):
        self.stack = [{
            "name": None,
            "class": None,
            "attributes": None,
            "children": [],
        }]

        # Keep a cache of the subclasses we create for unknown XML
        # elements, so that we don't create multiple classes for the
        # same element; it's fairly typical for elements to appear
        # multiple times in a document.
        self.unknownElementClasses = {}

    def close(self):
        top = self.stack[-1]

        assert top["name"] is None
        assert top["class"] is None
        assert top["attributes"] is None
        assert len(top["children"]) is 1, "Must have exactly one root element, got %d" % len(top["children"])

        self.dom = WebDAVDocument(top["children"][0])
        del(self.unknownElementClasses)
        return self.dom

    def data(self, data):
        # Stash character data away in a list that we will "".join() when done
        if self._characterBuffer is None:
            self._characterBuffer = []
        self._characterBuffer.append(data)

    def start(self, tag, attrs):
        name = QNameSplit(tag)

        if self._characterBuffer is not None:
            pcdata = PCDATAElement("".join(self._characterBuffer))
            self.stack[-1]["children"].append(pcdata)
            self._characterBuffer = None

        # Need to convert a "full" namespace in an attribute QName to the form
        # "%s:%s".
        attributes_dict = {}
        for aname, avalue in attrs.items():
            anamespace, aname = QNameSplit(aname)
            if anamespace:
                anamespace = _namespace_map.get(anamespace, anamespace)
                aname = "%s:%s" % (anamespace, aname,)
            attributes_dict[aname] = avalue

        tag_namespace, tag_name = name

        if name in _elements_by_qname:
            element_class = _elements_by_qname[name]
        elif name in self.unknownElementClasses:
            element_class = self.unknownElementClasses[name]
        else:
            def element_class(*args, **kwargs):
                element = WebDAVUnknownElement(*args, **kwargs)
                element.namespace = tag_namespace
                element.name = tag_name
                return element
            self.unknownElementClasses[name] = element_class

        self.stack.append({
            "name": name,
            "class": element_class,
            "attributes": attributes_dict,
            "children": [],
        })

    def end(self, tag):
        name = QNameSplit(tag)

        if self._characterBuffer is not None:
            pcdata = PCDATAElement("".join(self._characterBuffer))
            self.stack[-1]["children"].append(pcdata)
            self._characterBuffer = None

        # Pop the current element from the stack...
        top = self.stack[-1]
        del(self.stack[-1])

        assert top["name"] == name, "Last item on stack is %s while closing %s" % (top["name"], name)

        # ...then instantiate the element and add it to the parent's list of
        # children.
        element = top["class"](*top["children"], **top["attributes"])

        self.stack[-1]["children"].append(element)


class WebDAVDocument(AbstractWebDAVDocument):

    @classmethod
    def fromStream(cls, source):
        parser = XMLParser(target=WebDAVContentHandler())
        try:
            while 1:
                data = source.read(65536)
                if not data:
                    break
                parser.feed(data)
        except XMLParseError as e:
            raise ValueError(e)
        return parser.close()

    def writeXML(self, output):
        self.root_element.writeXML(output)
