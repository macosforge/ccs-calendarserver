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
##

from __future__ import with_statement

import cStringIO as StringIO
import xml.etree.ElementTree as XML

try:
    from xml.etree.ElementTree import ParseError as XMLParseError
except ImportError:
    from xml.parsers.expat import ExpatError as XMLParseError


# Utilities for working with ElementTree

def readXMLString(xmldata, expectedRootTag=None):
    io = StringIO.StringIO(xmldata)
    return readXML(io, expectedRootTag)


def readXML(xmlfile, expectedRootTag=None):
    """
    Read in XML data from a file and parse into ElementTree. Optionally verify
    the root node is what we expect.

    @param xmlfile: file to read from
    @type xmlfile: C{File}
    @param expectedRootTag: root tag (qname) to test or C{None}
    @type expectedRootTag: C{str}
    @return: C{tuple} of C{ElementTree}, C{Element}
    """

    # Read in XML
    try:
        etree = XML.ElementTree(file=xmlfile)
    except XMLParseError as e:
        raise ValueError("Unable to parse file '%s' because: %s" % (xmlfile, e,))

    if expectedRootTag:
        root = etree.getroot()
        if root.tag != expectedRootTag:
            raise ValueError("Ignoring file '%s' because it is not a %s file" % (xmlfile, expectedRootTag,))

    return etree, etree.getroot()


def elementToXML(element):
    return XML.tostring(element, "utf-8")


def writeXML(xmlfile, root):

    data = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE %s SYSTEM "%s.dtd">

""" % (root.tag, root.tag)

    INDENT = 2

    # Generate indentation
    def _indentNode(node, level=0):

        if node.text is not None and node.text.strip():
            return
        elif len(node):
            indent = "\n" + " " * (level + 1) * INDENT
            node.text = indent
            for child in node:
                child.tail = indent
                _indentNode(child, level + 1)
            if len(node):
                node[-1].tail = "\n" + " " * level * INDENT

    _indentNode(root, 0)
    data += XML.tostring(root) + "\n"

    with open(xmlfile, "w") as f:
        f.write(data)


def newElementTreeWithRoot(roottag):

    root = createElement(roottag)
    etree = XML.ElementTree(root)

    return etree, root


def createElement(tag, text=None, **attrs):

    child = XML.Element(tag, attrs)
    child.text = text
    return child


def addSubElement(parent, tag, text=None):

    child = XML.SubElement(parent, tag)
    child.text = text
    return child


def changeSubElementText(parent, tag, text):

    child = parent.find(tag)
    if child is not None:
        child.text = text
    else:
        addSubElement(parent, tag, text)
