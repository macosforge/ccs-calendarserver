##
# Copyright (c) 2009-2017 Apple Inc. All rights reserved.
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
XML based calendar user proxy loader.
"""

__all__ = [
    "XMLCalendarUserProxyLoader",
]

import types

from twisted.internet.defer import inlineCallbacks

from twext.python.log import Logger

from twistedcaldav.config import config, fullServerPath
from twistedcaldav.xmlutil import readXML

from txdav.who.delegates import Delegates

log = Logger()

ELEMENT_PROXIES = "proxies"
ELEMENT_RECORD = "record"

ELEMENT_GUID = "guid"
ELEMENT_WRITE_PROXIES = "write-proxies"
ELEMENT_READ_PROXIES = "read-proxies"
ELEMENT_MEMBER = "member"

ATTRIBUTE_REPEAT = "repeat"


class XMLCalendarUserProxyLoader(object):
    """
    XML calendar user proxy configuration file parser and loader.
    """

    def __repr__(self):
        return "<%s %r>" % (self.__class__.__name__, self.xmlFile)

    def __init__(self, xmlFile):

        self.items = []
        self.xmlFile = fullServerPath(config.DataRoot, xmlFile)

        # Read in XML
        try:
            _ignore_tree, proxies_node = readXML(self.xmlFile, ELEMENT_PROXIES)
        except ValueError:
            log.failure("XML parse error for proxy data file {xmlfile}", xmlfile=self.xmlFile)

        # FIXME: RuntimeError is dumb.
        self._parseXML(proxies_node)

    def _parseXML(self, rootnode):
        """
        Parse the XML root node from the augments configuration document.
        @param rootnode: the L{Element} to parse.
        """
        for child in rootnode:

            if child.tag != ELEMENT_RECORD:
                raise RuntimeError("Unknown augment type: '%s' in augment file: '%s'" % (child.tag, self.xmlFile,))

            repeat = int(child.get(ATTRIBUTE_REPEAT, "1"))

            guid = None
            write_proxies = set()
            read_proxies = set()
            for node in child:

                if node.tag == ELEMENT_GUID:
                    guid = node.text

                elif node.tag in (
                    ELEMENT_WRITE_PROXIES,
                    ELEMENT_READ_PROXIES,
                ):
                    self._parseMembers(node, write_proxies if node.tag == ELEMENT_WRITE_PROXIES else read_proxies)
                else:
                    raise RuntimeError("Invalid element '%s' in proxies file: '%s'" % (node.tag, self.xmlFile,))

            # Must have at least a guid
            if not guid:
                raise RuntimeError("Invalid record '%s' without a guid in proxies file: '%s'" % (child, self.xmlFile,))

            if repeat > 1:
                for i in xrange(1, repeat + 1):
                    self._buildRecord(guid, write_proxies, read_proxies, i)
            else:
                self._buildRecord(guid, write_proxies, read_proxies)

    def _parseMembers(self, node, addto):
        for child in node:
            if child.tag == ELEMENT_MEMBER:
                addto.add(child.text)

    def _buildRecord(self, guid, write_proxies, read_proxies, count=None):

        def expandCount(value, count):

            if type(value) in (str,):
                return value % (count,) if count and "%" in value else value
            else:
                return value

        guid = expandCount(guid, count)
        write_proxies = set([expandCount(member, count) for member in write_proxies])
        read_proxies = set([expandCount(member, count) for member in read_proxies])

        self.items.append((guid, write_proxies, read_proxies,))

    @inlineCallbacks
    def updateProxyDB(self, db):
        """
        Move the XML file proxies over to the proxyDB.
        """

        for item in self.items:
            guid, write_proxies, read_proxies = item
            yield db.setGroupMembers("%s#%s" % (guid, "calendar-proxy-write"), write_proxies)
            yield db.setGroupMembers("%s#%s" % (guid, "calendar-proxy-read"), read_proxies)

    @inlineCallbacks
    def updateProxyStore(self, store):
        """
        Move the XML file proxies over to the store database.
        """

        directory = store.directoryService()
        txn = store.newTransaction(label="xmlDelegatesToStore")
        for item in self.items:
            guid, write_proxies, read_proxies = item
            delegatorRecord = yield directory.recordWithUID(guid)
            if delegatorRecord is None:
                continue
            for proxy in write_proxies:
                delegateRecord = yield directory.recordWithUID(proxy)
                if delegateRecord is None:
                    continue

                yield Delegates.addDelegate(txn, delegatorRecord, delegateRecord, True)

            for proxy in read_proxies:
                delegateRecord = yield directory.recordWithUID(proxy)
                if delegateRecord is None:
                    continue

                yield Delegates.addDelegate(txn, delegatorRecord, delegateRecord, False)

        yield txn.commit()
