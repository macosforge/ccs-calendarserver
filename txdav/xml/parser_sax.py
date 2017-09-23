##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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

__all__ = [
    "WebDAVDocument",
]

import xml.dom.minidom
import xml.sax

from txdav.xml.base import WebDAVUnknownElement, PCDATAElement
from txdav.xml.base import _elements_by_qname
from txdav.xml.parser_base import AbstractWebDAVDocument
from txdav.xml.xmlext import Print as xmlPrint


class WebDAVContentHandler(xml.sax.handler.ContentHandler):

    def setDocumentLocator(self, locator):
        self.locator = locator

    locator = None

    def __init__(self):
        xml.sax.handler.ContentHandler.__init__(self)
        self._characterBuffer = None

    def location(self):
        return "line %d, column %d" % (self.locator.getLineNumber(), self.locator.getColumnNumber())

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

    def endDocument(self):
        top = self.stack[-1]

        assert top["name"] is None
        assert top["class"] is None
        assert top["attributes"] is None
        assert len(top["children"]) is 1, "Must have exactly one root element, got %d" % len(top["children"])

        self.dom = WebDAVDocument(top["children"][0])
        del(self.unknownElementClasses)

    def startElementNS(self, name, qname, attributes):
        if self._characterBuffer is not None:
            pcdata = PCDATAElement("".join(self._characterBuffer))
            self.stack[-1]["children"].append(pcdata)
            self._characterBuffer = None

        attributes_dict = {}

        if attributes.getLength() != 0:
            for attr_name in attributes.getQNames():
                attributes_dict[attr_name.encode("utf-8")] = attributes.getValueByQName(attr_name)

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

    def endElementNS(self, name, qname):
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
        try:
            element = top["class"](*top["children"], **top["attributes"])
        except ValueError as e:
            e.args = ("%s at %s" % (e.args[0], self.location()),) + e.args[1:]
            raise  # Re-raises modified e, but preserves traceback

        self.stack[-1]["children"].append(element)

    def characters(self, content):
        # Stash character data away in a list that we will "".join() when done
        if self._characterBuffer is None:
            self._characterBuffer = []
        self._characterBuffer.append(content)

    def ignorableWhitespace(self, whitespace):
        self.characters(self, whitespace)

    def startElement(self, name, attributes):
        raise AssertionError("startElement() should not be called by namespace-aware parser")

    def endElement(self, name):
        raise AssertionError("endElement() should not be called by namespace-aware parser")

    def processingInstruction(self, target, data):
        raise AssertionError("processing instructions are not allowed")

    def skippedEntity(self, name):
        raise AssertionError("skipped entities are not allowed")


class WebDAVDocument(AbstractWebDAVDocument):

    @classmethod
    def fromStream(cls, source):
        handler = WebDAVContentHandler()
        parser = xml.sax.make_parser()

        parser.setContentHandler(handler)
        parser.setFeature(xml.sax.handler.feature_namespaces, True)

        try:
            parser.parse(source)
        except xml.sax.SAXParseException as e:
            raise ValueError(e)

        # handler.dom.root_element.validate()

        return handler.dom

    def writeXML(self, output):
        document = xml.dom.minidom.Document()
        self.root_element.addToDOM(document, None)
        # document.normalize()
        xmlPrint(document, output)
