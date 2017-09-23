#!/usr/bin/env python

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
from __future__ import print_function
from __future__ import with_statement

from getopt import getopt, GetoptError
from subprocess import Popen, PIPE, STDOUT
import datetime
import hashlib
import os
import random
import shutil
import sys
import urllib
import uuid
import xattr
import zlib

from plistlib import readPlistFromString

from pycalendar.icalendar.calendar import Calendar
from pycalendar.parameter import Parameter

COPY_CAL_XATTRS = (
    'WebDAV:{DAV:}resourcetype',
    'WebDAV:{urn:ietf:params:xml:ns:caldav}calendar-timezone',
    'WebDAV:{http:%2F%2Fapple.com%2Fns%2Fical%2F}calendar-color',
    'WebDAV:{http:%2F%2Fapple.com%2Fns%2Fical%2F}calendar-order',
)
COPY_EVENT_XATTRS = ('WebDAV:{DAV:}getcontenttype',)


def usage(e=None):
    if e:
        print(e)
        print("")

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] source destination" % (name,))
    print("")
    print("  Anonymizes calendar data")
    print("")
    print("  source and destination should refer to document root directories")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -n --node <node>: Directory node (defaults to /Search)")
    print("")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():

    try:
        (optargs, args) = getopt(
            sys.argv[1:], "hn:", [
                "help",
                "node=",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    directoryNode = "/Search"

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-n", "--node"):
            directoryNode = arg

    if len(args) != 2:
        usage("Source and destination directories must be specified.")

    sourceDirectory, destDirectory = args

    directoryMap = DirectoryMap(directoryNode)

    anonymizeRoot(directoryMap, sourceDirectory, destDirectory)

    directoryMap.printStats()

    directoryMap.dumpDsImports(os.path.join(destDirectory, "dsimports"))


def anonymizeRoot(directoryMap, sourceDirectory, destDirectory):
    # sourceDirectory and destDirectory are DocumentRoots

    print("Anonymizing calendar data from %s into %s" % (sourceDirectory, destDirectory))

    homes = 0
    calendars = 0
    resources = 0

    if not os.path.exists(sourceDirectory):
        print("Can't find source: %s" % (sourceDirectory,))
        sys.exit(1)

    if not os.path.exists(destDirectory):
        os.makedirs(destDirectory)

    sourceCalRoot = os.path.join(sourceDirectory, "calendars")

    destCalRoot = os.path.join(destDirectory, "calendars")
    if not os.path.exists(destCalRoot):
        os.makedirs(destCalRoot)

    sourceUidHomes = os.path.join(sourceCalRoot, "__uids__")
    if os.path.exists(sourceUidHomes):

        destUidHomes = os.path.join(destCalRoot, "__uids__")
        if not os.path.exists(destUidHomes):
            os.makedirs(destUidHomes)

        homeList = []

        for first in os.listdir(sourceUidHomes):
            if len(first) == 2:
                firstPath = os.path.join(sourceUidHomes, first)
                for second in os.listdir(firstPath):
                    if len(second) == 2:
                        secondPath = os.path.join(firstPath, second)
                        for home in os.listdir(secondPath):
                            record = directoryMap.lookupCUA(home)
                            if not record:
                                print("Couldn't find %s, skipping." % (home,))
                                continue
                            sourceHome = os.path.join(secondPath, home)
                            destHome = os.path.join(
                                destUidHomes,
                                record['guid'][0:2], record['guid'][2:4],
                                record['guid'])
                            homeList.append((sourceHome, destHome, record))

            else:
                home = first
                sourceHome = os.path.join(sourceUidHomes, home)
                if not os.path.isdir(sourceHome):
                    continue
                record = directoryMap.lookupCUA(home)
                if not record:
                    print("Couldn't find %s, skipping." % (home,))
                    continue
                sourceHome = os.path.join(sourceUidHomes, home)
                destHome = os.path.join(destUidHomes, record['guid'])
                homeList.append((sourceHome, destHome, record))

        print("Processing %d calendar homes..." % (len(homeList),))

        for sourceHome, destHome, record in homeList:
            quotaUsed = 0

            if not os.path.exists(destHome):
                os.makedirs(destHome)

            homes += 1

            # Iterate calendars
            freeBusies = []
            for cal in os.listdir(sourceHome):

                # Skip these:
                if cal in ("dropbox", "notifications"):
                    continue

                # Don't include these in freebusy list
                if cal not in ("inbox", "outbox"):
                    freeBusies.append(cal)

                sourceCal = os.path.join(sourceHome, cal)
                destCal = os.path.join(destHome, cal)
                if not os.path.exists(destCal):
                    os.makedirs(destCal)
                calendars += 1

                # Copy calendar xattrs
                for attr, value in xattr.xattr(sourceCal).iteritems():
                    if attr in COPY_CAL_XATTRS:
                        xattr.setxattr(destCal, attr, value)

                # Copy index
                sourceIndex = os.path.join(sourceCal, ".db.sqlite")
                destIndex = os.path.join(destCal, ".db.sqlite")
                if os.path.exists(sourceIndex):
                    shutil.copyfile(sourceIndex, destIndex)

                # Iterate resources
                for resource in os.listdir(sourceCal):

                    if resource.startswith("."):
                        continue

                    sourceResource = os.path.join(sourceCal, resource)

                    # Skip directories
                    if os.path.isdir(sourceResource):
                        continue

                    with open(sourceResource) as res:
                        data = res.read()

                    data = anonymizeData(directoryMap, data)

                    if data is None:
                        # Ignore data we can't parse
                        continue

                    destResource = os.path.join(destCal, resource)
                    with open(destResource, "w") as res:
                        res.write(data)

                    quotaUsed += len(data)

                    for attr, value in xattr.xattr(sourceResource).iteritems():
                        if attr in COPY_EVENT_XATTRS:
                            xattr.setxattr(destResource, attr, value)

                    # Set new etag
                    xml = "<?xml version='1.0' encoding='UTF-8'?>\r\n<getcontentmd5 xmlns='http://twistedmatrix.com/xml_namespace/dav/'>%s</getcontentmd5>\r\n" % (hashlib.md5(data).hexdigest(),)
                    xattr.setxattr(destResource, "WebDAV:{http:%2F%2Ftwistedmatrix.com%2Fxml_namespace%2Fdav%2F}getcontentmd5", zlib.compress(xml))

                    resources += 1

                # Store new ctag on calendar
                xml = "<?xml version='1.0' encoding='UTF-8'?>\r\n<getctag xmlns='http://calendarserver.org/ns/'>%s</getctag>\r\n" % (str(datetime.datetime.now()),)
                xattr.setxattr(destCal, "WebDAV:{http:%2F%2Fcalendarserver.org%2Fns%2F}getctag", zlib.compress(xml))

            # Calendar home quota
            xml = "<?xml version='1.0' encoding='UTF-8'?>\r\n<quota-used xmlns='http://twistedmatrix.com/xml_namespace/dav/private/'>%d</quota-used>\r\n" % (quotaUsed,)
            xattr.setxattr(destHome, "WebDAV:{http:%2F%2Ftwistedmatrix.com%2Fxml_namespace%2Fdav%2Fprivate%2F}quota-used", zlib.compress(xml))

            # Inbox free busy calendars list
            destInbox = os.path.join(destHome, "inbox")
            if not os.path.exists(destInbox):
                os.makedirs(destInbox)
            xml = "<?xml version='1.0' encoding='UTF-8'?><calendar-free-busy-set xmlns='urn:ietf:params:xml:ns:caldav'>\n"
            for freeBusy in freeBusies:
                xml += "<href xmlns='DAV:'>/calendars/__uids__/%s/%s/</href>\n" % (record['guid'], freeBusy)
            xml += "</calendar-free-busy-set>\n"
            xattr.setxattr(
                destInbox,
                "WebDAV:{urn:ietf:params:xml:ns:caldav}calendar-free-busy-set",
                zlib.compress(xml)
            )

            if not (homes % 100):
                print(" %d..." % (homes,))

    print("Done.")
    print("")

    print("Calendar totals:")
    print(" Calendar homes: %d" % (homes,))
    print(" Calendars: %d" % (calendars,))
    print(" Events: %d" % (resources,))
    print("")


def anonymizeData(directoryMap, data):
    try:
        pyobj = Calendar.parseText(data)
    except Exception as e:
        print("Failed to parse (%s): %s" % (e, data))
        return None

    # Delete property from the top level
    try:
        for prop in pyobj.getProperties('x-wr-calname'):
            prop.setValue(anonymize(prop.getValue().getValue()))
    except KeyError:
        pass

    for comp in pyobj.getComponents():

        # Replace with anonymized CUAs:
        for propName in ('organizer', 'attendee'):
            try:
                for prop in tuple(comp.getProperties(propName)):
                    cua = prop.getValue().getValue()
                    record = directoryMap.lookupCUA(cua)
                    if record is None:
                        # print("Can't find record for", cua)
                        record = directoryMap.addRecord(cua=cua)
                        if record is None:
                            comp.removeProperty(prop)
                            continue
                    prop.setValue("urn:uuid:%s" % (record['guid'],))
                    if prop.hasParameter('X-CALENDARSERVER-EMAIL'):
                        prop.replaceParameter(Parameter('X-CALENDARSERVER-EMAIL', record['email']))
                    else:
                        prop.removeParameters('EMAIL')
                        prop.addParameter(Parameter('EMAIL', record['email']))
                    prop.removeParameters('CN')
                    prop.addParameter(Parameter('CN', record['name']))
            except KeyError:
                pass

        # Replace with anonymized text:
        for propName in ('summary', 'location', 'description'):
            try:
                for prop in comp.getProperties(propName):
                    prop.setValue(anonymize(prop.getValue().getValue()))
            except KeyError:
                pass

        # Replace with anonymized URL:
        try:
            for prop in comp.getProperties('url'):
                prop.setValue("http://example.com/%s/" % (anonymize(prop.getValue().getValue()),))
        except KeyError:
            pass

        # Remove properties:
        for propName in ('x-apple-dropbox', 'attach'):
            try:
                for prop in tuple(comp.getProperties(propName)):
                    comp.removeProperty(prop)
            except KeyError:
                pass

    return pyobj.getText(includeTimezones=Calendar.ALL_TIMEZONES)


class DirectoryMap(object):

    def __init__(self, node):

        self.map = {}
        self.byType = {
            'users': [],
            'groups': [],
            'locations': [],
            'resources': [],
        }
        self.counts = {
            'users': 0,
            'groups': 0,
            'locations': 0,
            'resources': 0,
            'unknown': 0,
        }

        self.strings = {
            'users': ('Users', 'user'),
            'groups': ('Groups', 'group'),
            'locations': ('Places', 'location'),
            'resources': ('Resources', 'resource'),
        }

        print("Fetching records from directory: %s" % (node,))

        for internalType, (recordType, _ignore_friendlyType) in self.strings.iteritems():
            print(" %s..." % (internalType,))
            child = Popen(
                args=[
                    "/usr/bin/dscl", "-plist", node, "-readall",
                    "/%s" % (recordType,),
                    "GeneratedUID", "RecordName", "EMailAddress", "GroupMembers"
                ],
                stdout=PIPE, stderr=STDOUT,
            )
            output, error = child.communicate()

            if child.returncode:
                raise DirectoryError(error)
            else:
                records = readPlistFromString(output)
                random.shuffle(records)  # so we don't go alphabetically

                for record in records:
                    origGUID = record.get('dsAttrTypeStandard:GeneratedUID', [None])[0]
                    if not origGUID:
                        continue
                    origRecordNames = record['dsAttrTypeStandard:RecordName']
                    origEmails = record.get('dsAttrTypeStandard:EMailAddress', [])
                    origMembers = record.get('dsAttrTypeStandard:GroupMembers', [])
                    self.addRecord(
                        internalType=internalType, guid=origGUID,
                        names=origRecordNames, emails=origEmails,
                        members=origMembers)

        print("Done.")
        print("")

    def addRecord(
        self, internalType="users", guid=None, names=None,
        emails=None, members=None, cua=None
    ):

        if cua:
            keys = [self.cua2key(cua)]
            self.counts['unknown'] += 1
        else:
            keys = self.getKeys(guid, names, emails)

        if keys:
            self.counts[internalType] += 1
            count = self.counts[internalType]

            namePrefix = randomName(6)
            typeStr = self.strings[internalType][1]
            record = {
                'guid': str(uuid.uuid4()).upper(),
                'name': "%s %s%d" % (namePrefix, typeStr, count,),
                'first': namePrefix,
                'last': "%s%d" % (typeStr, count,),
                'recordName': "%s%d" % (typeStr, count,),
                'email': ("%s%d@example.com" % (typeStr, count,)),
                'type': self.strings[internalType][0],
                'cua': cua,
                'members': members,
            }
            for key in keys:
                self.map[key] = record
            self.byType[internalType].append(record)
            return record
        else:
            return None

    def getKeys(self, guid, names, emails):
        keys = []
        if guid:
            keys.append(guid.lower())
        if names:
            for name in names:
                try:
                    name = name.encode('utf-8')
                    name = urllib.quote(name).lower()
                    keys.append(name)
                except:
                    # print("Failed to urllib.quote( ) %s. Skipping." % (name,))
                    pass
        if emails:
            for email in emails:
                email = email.lower()
                keys.append(email)
        return keys

    def cua2key(self, cua):
        key = cua.lower()

        if key.startswith("mailto:"):
            key = key[7:]

        elif key.startswith("urn:uuid:"):
            key = key[9:]

        elif (key.startswith("/") or key.startswith("http")):
            key = key.rstrip("/")
            key = key.split("/")[-1]

        return key

    def lookupCUA(self, cua):
        key = self.cua2key(cua)

        if key and key in self.map:
            return self.map[key]
        else:
            return None

    def printStats(self):
        print("Directory totals:")
        for internalType, (recordType, ignore) in self.strings.iteritems():
            print(" %s: %d" % (recordType, self.counts[internalType]))

        unknown = self.counts['unknown']
        if unknown:
            print(" Principals not found in directory: %d" % (unknown,))

    def dumpDsImports(self, dirPath):
        if not os.path.exists(dirPath):
            os.makedirs(dirPath)

        uid = 1000000
        filePath = os.path.join(dirPath, "users.dsimport")
        with open(filePath, "w") as out:
            out.write("0x0A 0x5C 0x3A 0x2C dsRecTypeStandard:Users 12 dsAttrTypeStandard:RecordName dsAttrTypeStandard:AuthMethod dsAttrTypeStandard:Password dsAttrTypeStandard:UniqueID dsAttrTypeStandard:GeneratedUID dsAttrTypeStandard:PrimaryGroupID dsAttrTypeStandard:RealName dsAttrTypeStandard:FirstName dsAttrTypeStandard:LastName dsAttrTypeStandard:NFSHomeDirectory dsAttrTypeStandard:UserShell dsAttrTypeStandard:EMailAddress\n")
            for record in self.byType['users']:
                fields = []
                fields.append(record['recordName'])
                fields.append("dsAuthMethodStandard\\:dsAuthClearText")
                fields.append("test")  # password
                fields.append(str(uid))
                fields.append(record['guid'])
                fields.append("20")  # primary group id
                fields.append(record['name'])
                fields.append(record['first'])
                fields.append(record['last'])
                fields.append("/var/empty")
                fields.append("/usr/bin/false")
                fields.append(record['email'])
                out.write(":".join(fields))
                out.write("\n")
                uid += 1

        gid = 2000000
        filePath = os.path.join(dirPath, "groups.dsimport")
        with open(filePath, "w") as out:
            out.write("0x0A 0x5C 0x3A 0x2C dsRecTypeStandard:Groups 5 dsAttrTypeStandard:RecordName dsAttrTypeStandard:PrimaryGroupID dsAttrTypeStandard:GeneratedUID dsAttrTypeStandard:RealName dsAttrTypeStandard:GroupMembership\n")
            for record in self.byType['groups']:
                fields = []
                fields.append(record['recordName'])
                fields.append(str(gid))
                fields.append(record['guid'])
                fields.append(record['name'])
                anonMembers = []
                for member in record['members']:
                    memberRec = self.lookupCUA("urn:uuid:%s" % (member,))
                    if memberRec:
                        anonMembers.append(memberRec['guid'])
                if anonMembers:  # skip empty groups
                    fields.append(",".join(anonMembers))
                    out.write(":".join(fields))
                    out.write("\n")
                    gid += 1

        filePath = os.path.join(dirPath, "resources.dsimport")
        with open(filePath, "w") as out:
            out.write("0x0A 0x5C 0x3A 0x2C dsRecTypeStandard:Resources 3 dsAttrTypeStandard:RecordName dsAttrTypeStandard:GeneratedUID dsAttrTypeStandard:RealName\n")
            for record in self.byType['resources']:
                fields = []
                fields.append(record['recordName'])
                fields.append(record['guid'])
                fields.append(record['name'])
                out.write(":".join(fields))
                out.write("\n")

        filePath = os.path.join(dirPath, "places.dsimport")
        with open(filePath, "w") as out:
            out.write("0x0A 0x5C 0x3A 0x2C dsRecTypeStandard:Places 3 dsAttrTypeStandard:RecordName dsAttrTypeStandard:GeneratedUID dsAttrTypeStandard:RealName\n")
            for record in self.byType['locations']:
                fields = []
                fields.append(record['recordName'])
                fields.append(record['guid'])
                fields.append(record['name'])
                out.write(":".join(fields))
                out.write("\n")


class DirectoryError(Exception):
    """
    Error trying to access dscl
    """


class DatabaseError(Exception):
    """
    Error trying to access sqlite3
    """


def anonymize(text):
    """
    Return a string whose value is the hex digest of text, repeated as needed
    to create a string of the same length as text.

    Useful for anonymizing strings in a deterministic manner.
    """
    if isinstance(text, unicode):
        try:
            text = text.encode('utf-8')
        except UnicodeEncodeError:
            print("Failed to anonymize:", text)
            text = "Anonymize me!"
    h = hashlib.md5(text)
    h = h.hexdigest()
    l = len(text)
    return (h * ((l / 32) + 1))[:-(32 - (l % 32))]


nameChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def randomName(length):
    l = []
    for _ignore in xrange(length):
        l.append(random.choice(nameChars))
    return "".join(l)


if __name__ == "__main__":
    main()
