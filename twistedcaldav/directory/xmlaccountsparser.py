##
# Copyright (c) 2006 Apple Computer, Inc. All rights reserved.
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
# DRI: Cyrus Daboo, cdaboo@apple.com
##


"""
XML based user/group/resource configuration file handling.
"""

__all__ = [
    "XMLAccountsParser",
]

import xml.dom.minidom

from twisted.python.filepath import FilePath

from twistedcaldav.resource import CalDAVResource

ELEMENT_ACCOUNTS    = "accounts"
ELEMENT_USER        = "user"
ELEMENT_GROUP       = "group"
ELEMENT_RESOURCE    = "resource"

ELEMENT_USERID      = "uid"
ELEMENT_PASSWORD    = "pswd"
ELEMENT_NAME        = "name"
ELEMENT_MEMBERS     = "members"
ELEMENT_CUADDR      = "cuaddr"
ELEMENT_CANPROXY    = "canproxy"

ATTRIBUTE_REPEAT    = "repeat"

class XMLAccountsParser(object):
    """
    XML account configuration file parser.
    """
    def __repr__(self):
        return "<%s %r>" % (self.__class__.__name__, self.xmlFile)

    def __init__(self, xmlFile):
        if type(xmlFile) is str:
            xmlFile = FilePath(xmlFile)

        self.xmlFile = xmlFile
        self.items = {}

        # Read in XML
        fd = open(self.xmlFile.path, "r")
        doc = xml.dom.minidom.parse( fd )
        fd.close()

        # Verify that top-level element is correct
        accounts_node = doc._get_documentElement()
        if accounts_node._get_localName() != ELEMENT_ACCOUNTS:
            self.log("Ignoring file %r because it is not a repository builder file" % (self.xmlFile,))
            return
        self._parseXML(accounts_node)
        
    def _parseXML(self, node):
        """
        Parse the XML root node from the accounts configuration document.
        @param node: the L{Node} to parse.
        """
        for child in node._get_childNodes():
            if child._get_localName() in (ELEMENT_USER, ELEMENT_GROUP, ELEMENT_RESOURCE):
                if child.hasAttribute( ATTRIBUTE_REPEAT ):
                    repeat = int(child.getAttribute( ATTRIBUTE_REPEAT ))
                else:
                    repeat = 1

                recordType = {
                    ELEMENT_USER:    "user",
                    ELEMENT_GROUP:   "group",
                    ELEMENT_RESOURCE:"resource",}[child._get_localName()]
                
                principal = XMLAccountRecord(recordType)
                principal.parseXML( child )
                if repeat > 1:
                    for ctr in range(repeat):
                        newprincipal = principal.repeat(ctr + 1)
                        self.items[newprincipal.uid] = newprincipal
                        if recordType == "group":
                            self._updateMembership(newprincipal)
                else:
                    self.items[principal.uid] = principal
                    if recordType == "group":
                        self._updateMembership(principal)

    def _updateMembership(self, group):
        # Update group membership
        for member in group.members:
            if self.items.has_key(member):
                self.items[member].groups.append(group.uid)
        
class XMLAccountRecord (object):
    """
    Contains provision information for one user.
    """
    def __init__(self, recordType):
        """
        @param recordType:    record type for directory entry.
        """
        
        self.recordType = recordType
        self.uid = None
        self.pswd = None
        self.name = None
        self.members = []
        self.groups = []
        self.cuaddrs = []

    def repeat(self, ctr):
        """
        Create another object like this but with all text items having % substitution
        done on them with the numeric value provided.
        @param ctr: an integer to substitute into text.
        """
        
        if self.uid.find("%") != -1:
            uid = self.uid % ctr
        else:
            uid = self.uid
        if self.pswd.find("%") != -1:
            pswd = self.pswd % ctr
        else:
            pswd = self.pswd
        if self.name.find("%") != -1:
            name = self.name % ctr
        else:
            name = self.name
        cuaddrs = []
        for cuaddr in self.cuaddrs:
            if cuaddr.find("%") != -1:
                cuaddrs.append(cuaddr % ctr)
            else:
                cuaddrs.append(cuaddr)
        
        result = XMLAccountRecord(self.recordType)
        result.uid = uid
        result.pswd = pswd
        result.name = name
        result.members = self.members
        result.cuaddrs = cuaddrs
        return result

    def parseXML( self, node ):

        for child in node._get_childNodes():
            if child._get_localName() == ELEMENT_USERID:
                if child.firstChild is not None:
                   self.uid = child.firstChild.data.encode("utf-8")
            elif child._get_localName() == ELEMENT_PASSWORD:
                if child.firstChild is not None:
                    self.pswd = child.firstChild.data.encode("utf-8")
            elif child._get_localName() == ELEMENT_NAME:
                if child.firstChild is not None:
                   self.name = child.firstChild.data.encode("utf-8")
            elif child._get_localName() == ELEMENT_MEMBERS:
                self._parseMembers(child)
            elif child._get_localName() == ELEMENT_CUADDR:
                if child.firstChild is not None:
                   self.cuaddrs.append(child.firstChild.data.encode("utf-8"))
            elif child._get_localName() == ELEMENT_CANPROXY:
                CalDAVResource.proxyUsers.add(self.uid)

    def _parseMembers( self, node ):

        for child in node._get_childNodes():
            if child._get_localName() == ELEMENT_USERID:
                if child.firstChild is not None:
                   self.members.append(child.firstChild.data.encode("utf-8"))
