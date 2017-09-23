# -*- test-case-name: twistedcaldav.test.test_upgrade -*-
##
# Copyright (c) 2008-2017 Apple Inc. All rights reserved.
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

import xattr
import os
import zlib
import hashlib
import datetime
import pwd
import grp
import shutil
import errno
import time
import uuid
from zlib import compress
from cPickle import loads as unpickle, UnpicklingError

from xml.etree.ElementTree import (
    parse as parseXML, ParseError as XMLParseError,
    tostring as etreeToString, Element as XMLElement
)

from txweb2.dav.fileop import rmdir

from twistedcaldav import caldavxml
from twistedcaldav.directory.calendaruserproxy import ProxySqliteDB
from twistedcaldav.directory.calendaruserproxyloader import XMLCalendarUserProxyLoader
from twistedcaldav.directory.principal import DirectoryCalendarPrincipalResource
from twistedcaldav.directory.resourceinfo import ResourceInfoDatabase
from twistedcaldav.directory.xmlaugmentsparser import ELEMENT_AUGMENTS
from twistedcaldav.ical import Component
from twistedcaldav.xmlutil import readXML, writeXML, addSubElement

from txdav.caldav.datastore.index_file import db_basename
from txdav.caldav.datastore.scheduling.cuaddress import LocalCalendarUser
from txdav.caldav.datastore.scheduling.imip.mailgateway import migrateTokensToStore
from txdav.caldav.datastore.scheduling.scheduler import DirectScheduler
from txdav.caldav.datastore.util import normalizationLookup
from txdav.who.delegates import Delegates
from txdav.who.idirectory import RecordType as CalRecordType
from txdav.xml import element

from twisted.internet.defer import (
    inlineCallbacks, succeed, returnValue
)
from twisted.python.filepath import FilePath
from twisted.python.reflect import namedAny
from twisted.python.reflect import namedClass

from calendarserver.tap.util import getRootResource, FakeRequest

from twext.python.log import Logger
from twext.who.idirectory import RecordType


deadPropertyXattrPrefix = namedAny(
    "txdav.base.propertystore.xattr.PropertyStore.deadPropertyXattrPrefix"
)

INBOX_ITEMS = "inboxitems.txt"
TRIGGER_FILE = "trigger_resource_migration"

log = Logger()


def xattrname(n):
    return deadPropertyXattrPrefix + n


def getCalendarServerIDs(config):

    # Determine uid/gid for ownership of directories we create here
    uid = -1
    if config.UserName:
        try:
            uid = pwd.getpwnam(config.UserName).pw_uid
        except KeyError:
            log.error("User not found: {user}", user=config.UserName)

    gid = -1
    if config.GroupName:
        try:
            gid = grp.getgrnam(config.GroupName).gr_gid
        except KeyError:
            log.error("Group not found: {grp}", grp=config.GroupName)

    return uid, gid


def fixBadQuotes(data):
    if (
        data.find('\\"') != -1 or
        data.find('\\\r\n "') != -1 or
        data.find('\r\n \r\n "') != -1
    ):
        # Fix by continuously replacing \" with " until no more
        # replacements occur
        while True:
            newData = data.replace('\\"', '"').replace('\\\r\n "', '\r\n "').replace('\r\n \r\n "', '\r\n "')
            if newData == data:
                break
            else:
                data = newData

        return data, True
    else:
        return data, False


@inlineCallbacks
def upgradeCalendarCollection(calPath, directory, cuaCache):
    errorOccurred = False
    collectionUpdated = False

    for resource in os.listdir(calPath):

        if resource.startswith("."):
            continue

        resPath = os.path.join(calPath, resource)

        if os.path.isdir(resPath):
            # Skip directories
            continue

        log.debug("Processing: {path}", path=resPath)
        needsRewrite = False
        with open(resPath) as res:
            data = res.read()

            try:
                data, fixed = fixBadQuotes(data)
                if fixed:
                    log.warn("Fixing bad quotes in {path}", path=resPath)
                    needsRewrite = True
            except Exception as e:
                log.error(
                    "Error while fixing bad quotes in {path}: {ex}",
                    path=resPath, ex=e,
                )
                errorOccurred = True
                continue

            try:
                data, fixed = removeIllegalCharacters(data)
                if fixed:
                    log.warn("Removing illegal characters in {path}", path=resPath)
                    needsRewrite = True
            except Exception as e:
                log.error(
                    "Error while removing illegal characters in {path}: {ex}",
                    path=resPath, ex=e,
                )
                errorOccurred = True
                continue

            try:
                data, fixed = (yield normalizeCUAddrs(data, directory, cuaCache))
                if fixed:
                    log.debug("Normalized CUAddrs in {path}", path=resPath)
                    needsRewrite = True
            except Exception as e:
                log.error(
                    "Error while normalizing {path}: {ex}",
                    path=resPath, ex=e,
                )
                errorOccurred = True
                continue

        if needsRewrite:
            with open(resPath, "w") as res:
                res.write(data)

            md5value = "<?xml version='1.0' encoding='UTF-8'?>\r\n<getcontentmd5 xmlns='http://twistedmatrix.com/xml_namespace/dav/'>%s</getcontentmd5>\r\n" % (hashlib.md5(data).hexdigest(),)
            md5value = zlib.compress(md5value)
            try:
                xattr.setxattr(resPath, xattrname("{http:%2F%2Ftwistedmatrix.com%2Fxml_namespace%2Fdav%2F}getcontentmd5"), md5value)
            except IOError as ioe:
                if ioe.errno == errno.EOPNOTSUPP:
                    # On non-native xattr systems we cannot do this,
                    # but those systems will typically not be migrating
                    # from pre-v1
                    pass
            except:
                raise

            collectionUpdated = True

    if collectionUpdated:
        ctagValue = "<?xml version='1.0' encoding='UTF-8'?>\r\n<getctag xmlns='http://calendarserver.org/ns/'>%s</getctag>\r\n" % (str(datetime.datetime.now()),)
        ctagValue = zlib.compress(ctagValue)
        try:
            xattr.setxattr(calPath, xattrname("{http:%2F%2Fcalendarserver.org%2Fns%2F}getctag"), ctagValue)
        except IOError as ioe:
            if ioe.errno == errno.EOPNOTSUPP:
                # On non-native xattr systems we cannot do this,
                # but those systems will typically not be migrating
                # from pre-v1
                pass
        except:
            raise

    returnValue(errorOccurred)


@inlineCallbacks
def upgradeCalendarHome(homePath, directory, cuaCache):

    errorOccurred = False

    log.debug("Upgrading calendar home: {path}", path=homePath)

    try:
        for cal in os.listdir(homePath):
            calPath = os.path.join(homePath, cal)
            if not os.path.isdir(calPath):
                # Skip non-directories; these might have been uploaded by a
                # random DAV client, they can't be calendar collections.
                continue
            if cal == 'notifications':
                # Delete the old, now obsolete, notifications directory.
                rmdir(calPath)
                continue
            log.debug("Upgrading calendar: {path}", path=calPath)
            if not (yield upgradeCalendarCollection(calPath, directory, cuaCache)):
                errorOccurred = True

            # Change the calendar-free-busy-set xattrs of the inbox to the
            # __uids__/<guid> form
            if cal == "inbox":
                try:
                    for attr, value in xattr.xattr(calPath).iteritems():
                        if attr == xattrname("{urn:ietf:params:xml:ns:caldav}calendar-free-busy-set"):
                            value = yield updateFreeBusySet(value, directory)
                            if value is not None:
                                # Need to write the xattr back to disk
                                xattr.setxattr(calPath, attr, value)
                except IOError as ioe:
                    if ioe.errno == errno.EOPNOTSUPP:
                        # On non-native xattr systems we cannot do this,
                        # but those systems will typically not be migrating
                        # from pre-v1
                        pass
                except:
                    raise
    except Exception as e:
        log.error("Failed to upgrade calendar home {path}: {ex}", path=homePath, ex=e)
        raise

    returnValue(errorOccurred)


@inlineCallbacks
def upgrade_to_1(config, directory):
    """
    Upconvert data from any calendar server version prior to data format 1.
    """
    errorOccurred = []

    def setError(f=None):
        if f is not None:
            log.error(f)
        errorOccurred.append(True)

    def doProxyDatabaseMoveUpgrade(config, uid=-1, gid=-1):
        # See if the new one is already present
        oldFilename = ".db.calendaruserproxy"
        newFilename = "proxies.sqlite"

        newDbPath = os.path.join(config.DataRoot, newFilename)
        if os.path.exists(newDbPath):
            # Nothing to be done, it's already in the new location
            return

        # See if the old DB is present
        oldDbPath = os.path.join(config.DocumentRoot, "principals", oldFilename)
        if not os.path.exists(oldDbPath):
            # Nothing to be moved
            return

        # Now move the old one to the new location
        try:
            if not os.path.exists(config.DataRoot):
                makeDirsUserGroup(config.DataRoot, uid=uid, gid=gid)
            try:
                os.rename(oldDbPath, newDbPath)
            except OSError:
                # Can't rename, must copy/delete
                shutil.copy2(oldDbPath, newDbPath)
                os.remove(oldDbPath)

        except Exception as e:
            raise UpgradeError(
                "Upgrade Error: unable to move the old calendar user proxy database at '%s' to '%s' due to %s."
                % (oldDbPath, newDbPath, str(e))
            )

        log.debug(
            "Moved the calendar user proxy database from '{old}' to '{new}'.",
            old=oldDbPath, new=newDbPath
        )

    def moveCalendarHome(oldHome, newHome, uid=-1, gid=-1):
        if os.path.exists(newHome):
            # Both old and new homes exist; stop immediately to let the
            # administrator fix it
            raise UpgradeError(
                "Upgrade Error: calendar home is in two places: %s and %s.  Please remove one of them and restart calendar server."
                % (oldHome, newHome)
            )

        makeDirsUserGroup(
            os.path.dirname(newHome.rstrip("/")), uid=uid, gid=gid
        )
        os.rename(oldHome, newHome)

    cuaCache = {}

    docRoot = config.DocumentRoot

    uid, gid = getCalendarServerIDs(config)

    if not os.path.exists(config.DataRoot):
        makeDirsUserGroup(config.DataRoot, uid=uid, gid=gid)

    if os.path.exists(docRoot):

        # Look for the /principals/ directory on disk
        oldPrincipals = os.path.join(docRoot, "principals")
        if os.path.exists(oldPrincipals):
            # First move the proxy database and rename it
            doProxyDatabaseMoveUpgrade(config, uid=uid, gid=gid)

            # Now delete the on disk representation of principals
            rmdir(oldPrincipals)
            log.debug(
                "Removed the old principal directory at '{path}'.",
                path=oldPrincipals,
            )

        calRoot = os.path.join(docRoot, "calendars")
        if os.path.exists(calRoot):

            uidHomes = os.path.join(calRoot, "__uids__")

            # Move calendar homes to new location:

            log.warn("Moving calendar homes to {path}", path=uidHomes)

            if os.path.exists(uidHomes):
                for home in os.listdir(uidHomes):

                    # MOR: This assumes no UID is going to be 2 chars or less
                    if len(home) <= 2:
                        continue

                    oldHome = os.path.join(uidHomes, home)
                    if not os.path.isdir(oldHome):
                        # Skip non-directories
                        continue

                    newHome = os.path.join(uidHomes, home[0:2], home[2:4], home)
                    moveCalendarHome(oldHome, newHome, uid=uid, gid=gid)

            else:
                os.mkdir(uidHomes)
                os.chown(uidHomes, uid, gid)

            for recordType, dirName in (
                (RecordType.user, u"users"),
                (RecordType.group, u"groups"),
                (CalRecordType.location, u"locations"),
                (CalRecordType.resource, u"resources"),
            ):
                dirPath = os.path.join(calRoot, dirName)
                if os.path.exists(dirPath):
                    for shortName in os.listdir(dirPath):
                        record = yield directory.recordWithShortName(
                            recordType, shortName
                        )
                        oldHome = os.path.join(dirPath, shortName)
                        if record is not None:
                            newHome = os.path.join(
                                uidHomes, record.uid[0:2],
                                record.uid[2:4], record.uid
                            )
                            moveCalendarHome(oldHome, newHome, uid=uid, gid=gid)
                        else:
                            # an orphaned calendar home (principal no longer
                            # exists in the directory)
                            archive(config, oldHome, uid, gid)

                    os.rmdir(dirPath)

            # Count how many calendar homes we'll be processing, and build
            # list of pending inbox items
            total = 0
            inboxItems = set()
            # Remove any inbox items created more than MigratedInboxDaysCutoff days in the past
            cutoffTimestamp = time.time() - (config.MigratedInboxDaysCutoff * 24 * 60 * 60)
            for first in os.listdir(uidHomes):
                if len(first) == 2:
                    firstPath = os.path.join(uidHomes, first)
                    for second in os.listdir(firstPath):
                        if len(second) == 2:
                            secondPath = os.path.join(firstPath, second)
                            for home in os.listdir(secondPath):
                                homePath = os.path.join(secondPath, home)
                                if not os.path.isdir(homePath):
                                    # Skip non-directories
                                    continue
                                total += 1
                                inboxPath = os.path.join(homePath, "inbox")
                                if os.path.exists(inboxPath):
                                    for inboxItem in os.listdir(inboxPath):
                                        if not inboxItem.startswith("."):
                                            itemPath = os.path.join(inboxPath, inboxItem)
                                            timestamp = os.path.getmtime(itemPath)
                                            if timestamp < cutoffTimestamp:
                                                os.remove(itemPath)
                                            else:
                                                inboxItems.add(itemPath)

            if inboxItems:
                inboxItemsFile = os.path.join(config.DataRoot, INBOX_ITEMS)
                with open(inboxItemsFile, "w") as out:
                    for item in inboxItems:
                        out.write("%s\n" % (item))
                os.chown(inboxItemsFile, uid, gid)

            if total:
                log.warn("Processing {total} calendar homes in {path}", total=total, path=uidHomes)

                # Upgrade calendar homes in the new location:
                count = 0
                for first in os.listdir(uidHomes):
                    if len(first) == 2:
                        firstPath = os.path.join(uidHomes, first)
                        for second in os.listdir(firstPath):
                            if len(second) == 2:
                                secondPath = os.path.join(firstPath, second)
                                for home in os.listdir(secondPath):
                                    homePath = os.path.join(secondPath, home)

                                    if not os.path.isdir(homePath):
                                        # Skip non-directories
                                        continue

                                    if not (yield upgradeCalendarHome(
                                        homePath, directory, cuaCache
                                    )):
                                        setError()

                                    count += 1
                                    if count % 10 == 0:
                                        log.warn(
                                            "Processed calendar home {count} of {total}",
                                            count=count, total=total,
                                        )
                log.warn("Done processing calendar homes")

    if errorOccurred:
        log.warn("Data upgrade encountered errors but will proceed; see error.log for details")


@inlineCallbacks
def normalizeCUAddrs(data, directory, cuaCache):
    """
    Normalize calendar user addresses in data.  Cache CUA lookup results for
    great speed.

    @param data: the calendar data to convert
    @type data: C{str}

    @param directory: the directory service to lookup CUAs with
    @type directory: L{DirectoryService}

    @param cuaCache: the dictionary to use as a cache across calls, which is
        updated as a side-effect
    @type cuaCache: C{dict}

    @return: tuple of (converted calendar data, boolean signaling whether there
        were any changes to the data)
    """
    cal = Component.fromString(data)

    @inlineCallbacks
    def lookupFunction(cuaddr, recordFunction, config):

        # Return cached results, if any.
        if cuaddr in cuaCache:
            returnValue(cuaCache[cuaddr])

        result = yield normalizationLookup(cuaddr, recordFunction, config)

        # Cache the result
        cuaCache[cuaddr] = result
        returnValue(result)

    yield cal.normalizeCalendarUserAddresses(
        lookupFunction,
        directory.recordWithCalendarUserAddress
    )

    newData = str(cal)
    returnValue((newData, not newData == data))


@inlineCallbacks
def upgrade_to_2(config, directory):

    def renameProxyDB():
        #
        # Rename proxy DB
        #
        oldFilename = "calendaruserproxy.sqlite"
        newFilename = "proxies.sqlite"

        oldDbPath = os.path.join(config.DataRoot, oldFilename)
        newDbPath = os.path.join(config.DataRoot, newFilename)
        if os.path.exists(oldDbPath) and not os.path.exists(newDbPath):
            os.rename(oldDbPath, newDbPath)

    def flattenHome(calHome):

        log.debug("Flattening calendar home: {path}", path=calHome)

        try:
            for cal in os.listdir(calHome):
                calPath = os.path.join(calHome, cal)
                if not os.path.isdir(calPath):
                    # Skip non-directories; these might have been uploaded by a
                    # random DAV client, they can't be calendar collections.
                    continue

                if cal in ("dropbox",):
                    continue
                if os.path.exists(os.path.join(calPath, db_basename)):
                    continue

                # Commented this out because it is only needed if there are calendars nested inside of regular collections.
                # Whilst this is technically possible in early versions of the servers the main clients did not support it.
                # Also, the v1 upgrade does not look at nested calendars for cu-address normalization.
                # However, we do still need to "ignore" regular collections in the calendar home so what we do is rename them
                # with a ".collection." prefix.
#                def scanCollection(collection):
#
#                    for child in os.listdir(collection):
#                        childCollection = os.path.join(collection, child)
#                        if os.path.isdir(childCollection):
#                            if os.path.exists(os.path.join(childCollection, db_basename)):
#                                newPath = os.path.join(calHome, child)
#                                if os.path.exists(newPath):
#                                    newPath = os.path.join(calHome, str(uuid.uuid4()))
#                                log.debug("Moving up calendar: {path}", path=childCollection)
#                                os.rename(childCollection, newPath)
#                            else:
#                                scanCollection(childCollection)

                if os.path.isdir(calPath):
                    # log.debug("Regular collection scan: {path}", path=calPath)
                    # scanCollection(calPath)
                    log.warn("Regular collection hidden: {path}", path=calPath)
                    os.rename(calPath, os.path.join(calHome, ".collection." + os.path.basename(calPath)))

        except Exception as e:
            log.error("Failed to upgrade calendar home {path}: {ex}", path=calHome, ex=e)
            return succeed(False)

        return succeed(True)

    def flattenHomes():
        """
        Make sure calendars inside regular collections are all moved to the top level.
        """
        errorOccurred = False

        log.debug("Flattening calendar homes")

        docRoot = config.DocumentRoot
        if os.path.exists(docRoot):
            calRoot = os.path.join(docRoot, "calendars")
            if os.path.exists(calRoot) and os.path.isdir(calRoot):
                uidHomes = os.path.join(calRoot, "__uids__")
                if os.path.isdir(uidHomes):
                    for path1 in os.listdir(uidHomes):
                        uidLevel1 = os.path.join(uidHomes, path1)
                        if not os.path.isdir(uidLevel1):
                            continue
                        for path2 in os.listdir(uidLevel1):
                            uidLevel2 = os.path.join(uidLevel1, path2)
                            if not os.path.isdir(uidLevel2):
                                continue
                            for home in os.listdir(uidLevel2):
                                calHome = os.path.join(uidLevel2, home)
                                if not os.path.isdir(calHome):
                                    continue
                                if not flattenHome(calHome):
                                    errorOccurred = True

        return errorOccurred

    renameProxyDB()

    # Move auto-schedule from resourceinfo sqlite to augments:
    yield migrateAutoSchedule(config, directory)

    if flattenHomes():
        raise UpgradeError("Data upgrade failed, see error.log for details")


def upgradeResourcesXML(resourcesFilePath):
    """
    Convert the old XML format to the twext.who.xml format

    @param resourcesFilePath: the file to convert
    @type resourcesFilePath: L{FilePath}
    """
    try:
        with resourcesFilePath.open() as fh:
            try:
                etree = parseXML(fh)
            except XMLParseError:
                log.error("Cannot parse {path}", path=resourcesFilePath.path)
                return
    except (OSError, IOError):
        # Can't read the file
        log.error("Cannot read {path}", path=resourcesFilePath.path)
        return

    accountsNode = etree.getroot()
    if accountsNode.tag != "accounts":
        return

    tagMap = {
        "uid": ("short-name",),
        "guid": ("guid", "uid"),
        "name": ("full-name",),
    }
    log.info("Converting resources.xml")
    directoryNode = XMLElement("directory")
    directoryNode.set("realm", accountsNode.get("realm"))
    for sourceNode in accountsNode:
        recordType = sourceNode.tag
        destNode = XMLElement("record")
        destNode.set("type", recordType)
        for sourceFieldNode in sourceNode:
            tags = tagMap.get(sourceFieldNode.tag, None)
            if tags:
                for tag in tags:
                    destFieldNode = XMLElement(tag)
                    value = sourceFieldNode.text
                    try:
                        # Normalize UUID values to uppercase
                        value = str(uuid.UUID(value)).upper()
                    except ValueError:
                        pass
                    destFieldNode.text = value
                    destNode.append(destFieldNode)

        directoryNode.append(destNode)

    resourcesFilePath.setContent(etreeToString(directoryNode, "utf-8"))


def upgradeAugmentsXML(augmentsFilePath):
    """
    Convert the old augments XML auto-schedule related elements to the twext.who.xml format

    @param augmentsFilePath: the file to convert
    @type augmentsFilePath: L{FilePath}
    """
    # Read in XML
    try:
        _ignore_tree, augments_node = readXML(augmentsFilePath.path, ELEMENT_AUGMENTS)
    except ValueError:
        log.error("Cannot parse {path}", path=augmentsFilePath.path)
        return

    log.info("Converting augments.xml")
    changed = False
    for recordNode in augments_node:

        autoScheduleElement = recordNode.find("auto-schedule")
        if autoScheduleElement is not None:
            # This is a legacy augment record; if auto-schedule was true we
            # need to set mode to "automatic", if false then set mode to "none".
            # If auto-schedule was true and auto-schedule-mode existed, keep mode.

            autoScheduleModeElement = recordNode.find("auto-schedule-mode")
            if autoScheduleModeElement is None:
                autoScheduleModeElement = addSubElement(recordNode, "auto-schedule-mode", text="automatic")

            if autoScheduleElement.text == "false":
                autoScheduleModeElement.text = "none"

            recordNode.remove(autoScheduleElement)
            changed = True

        enableElement = recordNode.find("enable")
        if enableElement is not None:
            recordNode.remove(enableElement)
            changed = True

    if changed:
        writeXML(augmentsFilePath.path, augments_node)


# The on-disk version number (which defaults to zero if .calendarserver_version
# doesn't exist), is compared with each of the numbers in the upgradeMethods
# array.  If it is less than the number, the associated method is called.

upgradeMethods = [
    (1, upgrade_to_1),
    (2, upgrade_to_2),
]


@inlineCallbacks
def upgradeData(config, directory):

    triggerPath = os.path.join(config.ServerRoot, TRIGGER_FILE)
    if os.path.exists(triggerPath):
        try:
            # Migrate locations/resources now because upgrade_to_1 depends
            # on them being in resources.xml
            yield migrateFromOD(directory)
        except Exception as e:
            raise UpgradeError("Unable to migrate locations and resources from OD: %s" % (e,))

    docRoot = config.DocumentRoot

    if not os.path.exists(docRoot):
        log.info("DocumentRoot ({path}) doesn't exist; skipping migration", path=docRoot)
        return

    versionFilePath = os.path.join(docRoot, ".calendarserver_version")

    onDiskVersion = 0
    if os.path.exists(versionFilePath):
        try:
            with open(versionFilePath) as versionFile:
                onDiskVersion = int(versionFile.read().strip())
        except IOError:
            log.error(
                "Cannot open {path}; skipping migration",
                path=versionFilePath,
            )
        except ValueError:
            log.error(
                "Invalid version number in {path}; skipping migration",
                path=versionFilePath,
            )

    uid, gid = getCalendarServerIDs(config)

    for version, method in upgradeMethods:
        if onDiskVersion < version:
            log.warn("Upgrading to version {vers}", vers=version)
            (yield method(config, directory))
            log.warn("Upgraded to version {vers}", vers=version)
            with open(versionFilePath, "w") as verFile:
                verFile.write(str(version))
            os.chown(versionFilePath, uid, gid)

    # Clean up the resource migration trigger file
    if os.path.exists(triggerPath):
        os.remove(triggerPath)


class UpgradeError(RuntimeError):
    """
    Generic upgrade error.
    """
    pass


#
# Utility functions
#
@inlineCallbacks
def updateFreeBusyHref(href, directory):
    pieces = href.split("/")
    if pieces[2] == "__uids__":
        # Already updated
        returnValue(None)

    recordType = pieces[2]
    shortName = pieces[3]
    record = yield directory.recordWithShortName(directory.oldNameToRecordType(recordType), shortName)
    if record is None:
        # We will simply ignore this and not write out an fb-set entry
        log.error("Can't update free-busy href; {user} is not in the directory", user=shortName)
        returnValue("")

    uid = record.uid
    newHref = "/calendars/__uids__/%s/%s/" % (uid, pieces[4])
    returnValue(newHref)


@inlineCallbacks
def updateFreeBusySet(value, directory):

    try:
        value = zlib.decompress(value)
    except zlib.error:
        # Legacy data - not zlib compressed
        pass

    try:
        doc = element.WebDAVDocument.fromString(value)
        freeBusySet = doc.root_element
    except ValueError:
        try:
            freeBusySet = unpickle(value)
        except UnpicklingError:
            log.error("Invalid free/busy property value")
            returnValue(None)

    fbset = set()
    didUpdate = False
    for href in freeBusySet.children:
        href = str(href)
        newHref = yield updateFreeBusyHref(href, directory)
        if newHref is None:
            fbset.add(href)
        else:
            didUpdate = True
            if newHref != "":
                fbset.add(newHref)

    if didUpdate:
        property = caldavxml.CalendarFreeBusySet(
            *[element.HRef(fbhref) for fbhref in fbset]
        )
        value = compress(property.toxml())
        returnValue(value)

    returnValue(None)  # no update required


def makeDirsUserGroup(path, uid=-1, gid=-1):
    parts = path.split("/")
    if parts[0] == "":  # absolute path
        parts[0] = "/"

    path = ""
    for part in parts:
        if not part:
            continue
        path = os.path.join(path, part)
        if not os.path.exists(path):
            os.mkdir(path)
            os.chown(path, uid, gid)


def archive(config, srcPath, uid, gid):
    """
    Move srcPath into dataroot/archived, giving the destination a unique
    (sequentially numbered) name in the case of duplicates.
    """

    archiveDir = os.path.join(config.DataRoot, "archived")

    if not os.path.exists(archiveDir):
        os.mkdir(archiveDir)
    os.chown(archiveDir, uid, gid)

    baseName = os.path.basename(srcPath)
    newName = baseName
    count = 0
    destPath = os.path.join(archiveDir, newName)
    while os.path.exists(destPath):
        count += 1
        newName = "%s.%d" % (baseName, count)
        destPath = os.path.join(archiveDir, newName)

    try:
        os.rename(srcPath, destPath)
    except OSError:
        # Can't rename, must copy/delete
        shutil.copy2(srcPath, destPath)
        os.remove(srcPath)


DELETECHARS = ''.join(chr(i) for i in xrange(32) if i not in (9, 10, 13))


def removeIllegalCharacters(data):
    """
    Remove all characters below ASCII 32 except HTAB, LF and CR

    Return tuple with the processed data, and a boolean indicating whether
    the data changed.
    """
    beforeLen = len(data)
    data = data.translate(None, DELETECHARS)
    afterLen = len(data)
    if afterLen != beforeLen:
        return data, True
    else:
        return data, False


# Deferred
def migrateFromOD(directory):
    #
    # Migrates locations and resources from OD
    #
    log.warn("Migrating locations and resources")

    # Create internal copies of resources and locations based on what is
    # found in OD
    from twext.who.idirectory import RecordType as BaseRecordType
    from twext.who.opendirectory import (
        DirectoryService as OpenDirectoryService
    )
    from twext.who.util import ConstantsContainer
    from calendarserver.tools.resources import migrateResources
    from txdav.who.opendirectory import _CSRecordType  # Use this module for the import to make sure constants are setup properly

    # We need to "patch" twext.who.opendirectory._service.DirectoryService to include resources and locations as supported record types
    # during migration
    class EnhancedDirectoryService(OpenDirectoryService):
        recordType = ConstantsContainer((
            BaseRecordType.user, BaseRecordType.group, _CSRecordType.location, _CSRecordType.resource
        ))

    return migrateResources(EnhancedDirectoryService(), directory)


@inlineCallbacks
def migrateAutoSchedule(config, directory):
    # Fetch the autoSchedule assignments from resourceinfo.sqlite and store
    # the values in augments
    augmentService = None
    serviceClass = {
        "xml": "twistedcaldav.directory.augment.AugmentXMLDB",
    }
    augmentClass = namedClass(serviceClass[config.AugmentService.type])
    try:
        augmentService = augmentClass(**config.AugmentService.params)
    except:
        log.error("Could not start augment service")

    if augmentService:
        augmentRecords = []
        dbPath = os.path.join(config.DataRoot, ResourceInfoDatabase.dbFilename)
        if os.path.exists(dbPath):
            log.warn("Migrating auto-schedule settings")
            resourceInfoDatabase = ResourceInfoDatabase(config.DataRoot)
            results = resourceInfoDatabase._db_execute(
                "select GUID, AUTOSCHEDULE from RESOURCEINFO"
            )
            for uid, autoSchedule in results:
                if uid is not None:
                    record = yield directory.recordWithUID(uid)
                    if record is not None:
                        augmentRecord = (
                            yield augmentService.getAugmentRecord(
                                uid,
                                directory.recordTypeToOldName(record.recordType)
                            )
                        )
                        augmentRecord.autoScheduleMode = (
                            "automatic" if autoSchedule else "default"
                        )
                        augmentRecords.append(augmentRecord)

            if augmentRecords:
                yield augmentService.addAugmentRecords(augmentRecords)
            log.warn("Migrated {len} auto-schedule settings", len=len(augmentRecords))


def loadDelegatesFromXMLintoProxyDB(xmlFile, service):
    loader = XMLCalendarUserProxyLoader(xmlFile)
    return loader.updateProxyDB(service)


def loadDelegatesFromXMLintoStore(xmlFile, store):
    loader = XMLCalendarUserProxyLoader(xmlFile)
    return loader.updateProxyStore(store)


@inlineCallbacks
def migrateDelegatesToStore(store):
    """
    Migrate the old sqlite proxyDB data into the store. Remove the sqlite file
    afterwards.

    @param store: the store to migrate into
    @type store: L{CommonDataStore}
    """

    log.warn("Migrating delegates to the store")

    service = ProxySqliteDB("proxies.sqlite")
    directory = store.directoryService()
    txn = store.newTransaction(label="migrateDelegatesToStore")
    for groupName, memberUID in (
        yield service.query(
            "select GROUPNAME, MEMBER from GROUPS"
        )
    ):
        if "#" not in groupName:
            continue

        delegatorUID, groupType = groupName.split("#")
        delegatorRecord = yield directory.recordWithUID(delegatorUID)
        if delegatorRecord is None:
            continue

        delegateRecord = yield directory.recordWithUID(memberUID)
        if delegateRecord is None:
            continue

        readWrite = (groupType == "calendar-proxy-write")
        yield Delegates.addDelegate(txn, delegatorRecord, delegateRecord, readWrite)

    yield txn.commit()

    # Remove the old file
    service.close()
    os.remove(service.dbpath)
    journalPath = service.dbpath + "-journal"
    if os.path.exists(journalPath):
        os.remove(journalPath)


class UpgradeFileSystemFormatStep(object):
    """
    Upgrade filesystem from previous versions.
    """

    def __init__(self, config, store):
        """
        Initialize the service.
        """
        self.config = config
        self.store = store

    @inlineCallbacks
    def doUpgrade(self):
        """
        Do the upgrade.  Called by C{startService}, but a different method
        because C{startService} should return C{None}, not a L{Deferred}.

        @return: a Deferred which fires when the upgrade is complete.
        """

        # Don't try to use memcached during upgrade; it's not necessarily
        # running yet.
        memcacheEnabled = self.config.Memcached.Pools.Default.ClientEnabled
        self.config.Memcached.Pools.Default.ClientEnabled = False

        yield upgradeData(self.config, self.store.directoryService())

        # Restore memcached client setting
        self.config.Memcached.Pools.Default.ClientEnabled = memcacheEnabled

        returnValue(None)

    def stepWithResult(self, result):
        """
        Execute the step.
        """
        return self.doUpgrade()


class PostDBImportStep(object):
    """
    Step which runs after database import but before workers are spawned
    (except memcached will be running at this point)

    The jobs carried out here are:

        1. Populating the group-membership cache
        2. Processing non-implicit inbox items
        3. Migrate IMIP tokens into the store
        4. Migrating delegate assignments into the store
    """

    def __init__(self, store, config, doPostImport):
        """
        Initialize the service.
        """
        self.store = store
        self.config = config
        self.doPostImport = doPostImport

    @inlineCallbacks
    def stepWithResult(self, result):
        if self.doPostImport:

            # Migrate any proxyDB file that exists - remove it after migration
            loadDoneFilePath = os.path.join(self.config.DataRoot, "proxies-loaded")
            if os.path.exists(
                os.path.join(self.config.DataRoot, "proxies.sqlite")
            ):
                # Migrate delegate assignments from sqlite to store
                yield migrateDelegatesToStore(
                    self.store
                )

                # If migration happened and the XML load option is on, write a stub file to prevent
                # it being loaded on the next restart
                if (
                    self.config.ProxyLoadFromFile and
                    os.path.exists(self.config.ProxyLoadFromFile) and
                    not os.path.exists(loadDoneFilePath)
                ):
                    # Write stub file as indicator loading is done
                    FilePath(loadDoneFilePath).touch()

            # If no migration, see if there is an XML file to load into the store (once only)
            else:
                if (
                    self.config.ProxyLoadFromFile and
                    os.path.exists(self.config.ProxyLoadFromFile) and
                    not os.path.exists(loadDoneFilePath)
                ):
                    log.warn("Loading delegate assignments from XML")
                    yield loadDelegatesFromXMLintoStore(
                        self.config.ProxyLoadFromFile,
                        self.store
                    )

                    # Write stub file as indicator loading is done
                    FilePath(loadDoneFilePath).touch()

            # Process old inbox items
            self.store.setMigrating(True)
            yield self.processInboxItems()
            self.store.setMigrating(False)

            # Migrate mail tokens from sqlite to store
            yield migrateTokensToStore(self.config.DataRoot, self.store)

    @inlineCallbacks
    def processInboxItems(self):
        """
        When data is migrated from a non-implicit scheduling server there can
        be inbox items that clients have not yet processed.  This method
        runs those inbox items through the implicit scheduling mechanism.
        """

        inboxItemsList = os.path.join(self.config.DataRoot, INBOX_ITEMS)
        if os.path.exists(inboxItemsList):

            log.info("Starting inbox item processing.")

            root = getRootResource(self.config, self.store)
            directory = root.getDirectory()
            principalCollection = directory.principalCollection

            inboxItems = set()
            with open(inboxItemsList) as input:
                for inboxItem in input:
                    inboxItem = inboxItem.strip()
                    inboxItems.add(inboxItem)

            try:
                itemsToProcess = list(inboxItems)
                totalItems = len(itemsToProcess)
                ignoreUUIDs = set()
                for ctr, inboxItem in enumerate(itemsToProcess):
                    log.info("Processing {ctr}/{total} inbox item: {item}", ctr=ctr + 1, total=totalItems, item=inboxItem)
                    ignore, uuid, ignore, fileName = inboxItem.rsplit("/", 3)

                    if uuid in ignoreUUIDs:
                        log.debug("Ignored inbox item - uuid ignored: {item}", item=inboxItem)
                        inboxItems.remove(inboxItem)
                        continue

                    record = yield directory.recordWithUID(uuid)
                    if record is None:
                        log.debug("Ignored inbox item - no record: {item}", item=inboxItem)
                        inboxItems.remove(inboxItem)
                        ignoreUUIDs.add(uuid)
                        continue

                    principal = yield principalCollection.principalForRecord(record)
                    if principal is None or not isinstance(principal, DirectoryCalendarPrincipalResource):
                        log.debug("Ignored inbox item - no principal: {item}", item=inboxItem)
                        inboxItems.remove(inboxItem)
                        ignoreUUIDs.add(uuid)
                        continue

                    request = FakeRequest(root, "PUT", None)
                    request.noAttendeeRefresh = True  # tell scheduling to skip refresh
                    request.checkedSACL = True
                    request.authnUser = request.authzUser = principal

                    # The request may end up with an associated transaction and we must make sure that is
                    # either committed or aborted, so use try/finally to handle that case.
                    txnCommitted = False
                    try:
                        calendarHome = yield principal.calendarHome(request)
                        if calendarHome is None:
                            log.debug("Ignored inbox item - no calendar home: {item}", item=inboxItem)
                            inboxItems.remove(inboxItem)
                            ignoreUUIDs.add(uuid)
                            continue

                        inbox = yield calendarHome.getChild("inbox")
                        if inbox is not None and inbox.exists():

                            inboxItemResource = yield inbox.getChild(fileName)
                            if inboxItemResource is not None and inboxItemResource.exists():

                                uri = "/calendars/__uids__/%s/inbox/%s" % (uuid, fileName)
                                request.path = uri
                                request._rememberResource(inboxItemResource, uri)

                                try:
                                    txnCommitted = yield self.processInboxItem(
                                        root,
                                        directory,
                                        principal,
                                        request,
                                        inbox,
                                        inboxItemResource,
                                        uuid,
                                        uri
                                    )
                                except Exception as e:
                                    log.error(
                                        "Error processing inbox item: {item} ({ex})",
                                        item=inboxItem, ex=e,
                                    )
                            else:
                                log.debug("Ignored inbox item - no resource: {item}", item=inboxItem)
                        else:
                            log.debug("Ignored inbox item - no inbox: {item}", item=inboxItem)

                        inboxItems.remove(inboxItem)

                    finally:
                        if not txnCommitted and hasattr(request, "_newStoreTransaction"):
                            request._newStoreTransaction.abort()

            # FIXME: Some generic exception handlers to deal with unexpected errors that for some reason
            # we are not logging properly.
            except Exception as e:
                log.error("Exception during inbox item processing: {ex}", ex=e)

            except:
                log.error("Unknown exception during inbox item processing.")

            finally:
                if inboxItems:
                    # Rewrite the inbox items file in case we exit before we're
                    # done so we'll pick up where we left off next time we start up.
                    with open(inboxItemsList + ".tmp", "w") as output:
                        for inboxItem in inboxItems:
                            output.write("%s\n" % (inboxItem,))
                    os.rename(inboxItemsList + ".tmp", inboxItemsList)
                    log.info("Inbox item processing did not finish.")
                    log.error("Restart calendar service to re-attempt inbox item processing")
                else:
                    # Remove the inbox items file - nothing more to do
                    os.remove(inboxItemsList)
                    log.info("Completed inbox item processing.")

    @inlineCallbacks
    def processInboxItem(
        self, root, directory, principal, request, inbox,
        inboxItem, uuid, uri
    ):
        """
        Run an individual inbox item through implicit scheduling and remove
        the inbox item.
        """

        log.debug("Processing inbox item {item}", item=inboxItem)

        txn = request._newStoreTransaction

        ownerPrincipal = principal
        cua = "urn:x-uid:%s" % (uuid,)
        owner = LocalCalendarUser(
            cua, ownerPrincipal,
            inbox, ownerPrincipal.scheduleInboxURL()
        )

        calendar = yield inboxItem.componentForUser()
        if calendar.mainType() is not None:
            try:
                method = calendar.propertyValue("METHOD")
                log.info("Inbox item method is {method}", method=method)
            except ValueError:
                returnValue(None)

            if method == "REPLY":
                # originator is attendee sending reply
                originator = calendar.getAttendees()[0]
            else:
                # originator is the organizer
                originator = calendar.getOrganizer()

            principalCollection = directory.principalCollection
            originatorPrincipal = yield principalCollection.principalForCalendarUserAddress(originator)
            originator = LocalCalendarUser(originator, originatorPrincipal)
            recipients = (owner,)

            scheduler = DirectScheduler(request, inboxItem)
            # Process inbox item
            yield scheduler.doSchedulingViaPUT(
                originator, recipients, calendar,
                internal_request=False, noAttendeeRefresh=True
            )
        else:
            log.warn("Removing invalid inbox item: {uri}", uri=uri)

        #
        # Remove item
        #
        yield inboxItem.storeRemove(request, True, uri)
        yield txn.commit()
        returnValue(True)
