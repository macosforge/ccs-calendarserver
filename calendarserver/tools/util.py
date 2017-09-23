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

"""
Utility functionality shared between calendarserver tools.
"""
from __future__ import print_function

__all__ = [
    "loadConfig",
    "UsageError",
    "booleanArgument",
]

import datetime
import os
from time import sleep
import socket
from pwd import getpwnam
from grp import getgrnam

from calendarserver.tools import diagnose

from twistedcaldav.config import config, ConfigurationError
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE


from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue

from twistedcaldav import memcachepool
from txdav.base.propertystore.base import PropertyName
from txdav.xml import element
from pycalendar.datetime import DateTime, Timezone

from twext.who.idirectory import RecordType
from txdav.who.idirectory import RecordType as CalRecordType

from txdav.who.delegates import Delegates, RecordType as DelegateRecordType


log = Logger()


def loadConfig(configFileName):
    """
    Helper method for command-line utilities to load configuration plist
    and override certain values.
    """
    if configFileName is None:
        configFileName = DEFAULT_CONFIG_FILE

    if not os.path.isfile(configFileName):
        raise ConfigurationError("No config file: %s" % (configFileName,))

    config.load(configFileName)

    # Command-line utilities always want these enabled:
    config.EnableCalDAV = True
    config.EnableCardDAV = True

    return config


class UsageError (Exception):
    pass


def booleanArgument(arg):
    if arg in ("true", "yes", "yup", "uh-huh", "1", "t", "y"):
        return True
    elif arg in ("false", "no", "nope", "nuh-uh", "0", "f", "n"):
        return False
    else:
        raise ValueError("Not a boolean: %s" % (arg,))


def autoDisableMemcached(config):
    """
    Set ClientEnabled to False for each pool whose memcached is not running
    """

    for pool in config.Memcached.Pools.itervalues():
        if pool.ClientEnabled:
            try:
                if pool.get("MemcacheSocket"):
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(pool.MemcacheSocket)
                    s.close()
                else:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect((pool.BindAddress, pool.Port))
                    s.close()

            except socket.error:
                pool.ClientEnabled = False


def setupMemcached(config):
    #
    # Connect to memcached
    #
    memcachepool.installPools(
        config.Memcached.Pools,
        config.Memcached.MaxClients
    )
    autoDisableMemcached(config)


def checkDirectory(dirpath, description, access=None, create=None, wait=False):
    """
    Make sure dirpath is an existing directory, and optionally ensure it has the
    expected permissions.  Alternatively the function can create the directory or
    can wait for someone else to create it.

    @param dirpath: The directory path we're checking
    @type dirpath: string
    @param description: A description of what the directory path represents, used in
        log messages
    @type description: string
    @param access: The type of access we're expecting, either os.W_OK or os.R_OK
    @param create: A tuple of (file permissions mode, username, groupname) to use
        when creating the directory.  If create=None then no attempt will be made
        to create the directory.
    @type create: tuple
    @param wait: Wether the function should wait in a loop for the directory to be
        created by someone else (or mounted, etc.)
    @type wait: boolean
    """

    # Note: we have to use print here because the logging mechanism has not
    # been set up yet.

    if not os.path.exists(dirpath) or (diagnose.detectPhantomVolume(dirpath) == diagnose.EXIT_CODE_PHANTOM_DATA_VOLUME):

        if wait:

            # If we're being told to wait, post an alert that we can't continue
            # until the volume is mounted
            if not os.path.exists(dirpath) or (diagnose.detectPhantomVolume(dirpath) == diagnose.EXIT_CODE_PHANTOM_DATA_VOLUME):
                from calendarserver.tap.util import AlertPoster
                AlertPoster.postAlert("MissingDataVolumeAlert", 0, ["volumePath", dirpath])

            while not os.path.exists(dirpath) or (diagnose.detectPhantomVolume(dirpath) == diagnose.EXIT_CODE_PHANTOM_DATA_VOLUME):
                if not os.path.exists(dirpath):
                    print("Path does not exist: %s" % (dirpath,))
                else:
                    print("Path is not a real volume: %s" % (dirpath,))
                sleep(5)
        else:
            try:
                mode, username, groupname = create
            except TypeError:
                raise ConfigurationError("%s does not exist: %s"
                                         % (description, dirpath))
            try:
                os.mkdir(dirpath)
            except (OSError, IOError) as e:
                print("Could not create %s: %s" % (dirpath, e))
                raise ConfigurationError(
                    "%s does not exist and cannot be created: %s"
                    % (description, dirpath)
                )

            if username:
                uid = getpwnam(username).pw_uid
            else:
                uid = -1

            if groupname:
                gid = getgrnam(groupname).gr_gid
            else:
                gid = -1

            try:
                os.chmod(dirpath, mode)
                os.chown(dirpath, uid, gid)
            except (OSError, IOError) as e:
                print("Unable to change mode/owner of %s: %s" % (dirpath, e))

            print("Created directory: %s" % (dirpath,))

    if not os.path.isdir(dirpath):
        raise ConfigurationError("%s is not a directory: %s" % (description, dirpath))

    if access and not os.access(dirpath, access):
        raise ConfigurationError(
            "Insufficient permissions for server on %s directory: %s"
            % (description, dirpath)
        )


@inlineCallbacks
def principalForPrincipalID(principalID, checkOnly=False, directory=None):

    # Allow a directory parameter to be passed in, but default to config.directory
    # But config.directory isn't set right away, so only use it when we're doing more
    # than checking.
    if not checkOnly and not directory:
        directory = config.directory

    if principalID.startswith("/"):
        segments = principalID.strip("/").split("/")
        if (
            len(segments) == 3 and
            segments[0] == "principals" and segments[1] == "__uids__"
        ):
            uid = segments[2]
        else:
            raise ValueError("Can't resolve all paths yet")

        if checkOnly:
            returnValue(None)

        returnValue((yield directory.principalCollection.principalForUID(uid)))

    if principalID.startswith("("):
        try:
            i = principalID.index(")")

            if checkOnly:
                returnValue(None)

            recordType = principalID[1:i]
            shortName = principalID[i + 1:]

            if not recordType or not shortName or "(" in recordType:
                raise ValueError()

            returnValue((yield directory.principalCollection.principalForShortName(recordType, shortName)))

        except ValueError:
            pass

    if ":" in principalID:
        if checkOnly:
            returnValue(None)

        recordTypeDescription, shortName = principalID.split(":", 1)
        for recordType in (
            RecordType.user,
            RecordType.group,
            CalRecordType.location,
            CalRecordType.resource,
            CalRecordType.address,
        ):
            if recordType.description == recordTypeDescription:
                break
        else:
            returnValue(None)

        returnValue((yield directory.principalCollection.principalForShortName(recordType, shortName)))

    try:
        if checkOnly:
            returnValue(None)

        returnValue((yield directory.principalCollection.principalForUID(principalID)))
    except ValueError:
        pass

    raise ValueError("Invalid principal identifier: %s" % (principalID,))


@inlineCallbacks
def recordForPrincipalID(directory, principalID, checkOnly=False):

    if principalID.startswith("/"):
        segments = principalID.strip("/").split("/")
        if (
            len(segments) == 3 and
            segments[0] == "principals" and segments[1] == "__uids__"
        ):
            uid = segments[2]
        else:
            raise ValueError("Can't resolve all paths yet")

        if checkOnly:
            returnValue(None)

        returnValue((yield directory.recordWithUID(uid)))

    if principalID.startswith("("):
        try:
            i = principalID.index(")")

            if checkOnly:
                returnValue(None)

            recordType = directory.oldNameToRecordType(principalID[1:i])
            shortName = principalID[i + 1:]

            if not recordType or not shortName or "(" in recordType:
                raise ValueError()

            returnValue((yield directory.recordWithShortName(recordType, shortName)))

        except ValueError:
            pass

    if ":" in principalID:
        if checkOnly:
            returnValue(None)

        recordType, shortName = principalID.split(":", 1)
        recordType = directory.oldNameToRecordType(recordType)
        if recordType is None:
            returnValue(None)

        returnValue((yield directory.recordWithShortName(recordType, shortName)))

    try:
        if checkOnly:
            returnValue(None)

        returnValue((yield directory.recordWithUID(principalID)))
    except ValueError:
        pass

    raise ValueError("Invalid principal identifier: %s" % (principalID,))


@inlineCallbacks
def _addRemoveProxy(msg, fn, store, record, proxyType, *proxyIDs):
    directory = store.directoryService()
    readWrite = (proxyType == "write")
    for proxyID in proxyIDs:
        proxyRecord = yield recordForPrincipalID(directory, proxyID)
        if proxyRecord is None:
            print("Invalid principal ID: %s" % (proxyID,))
        else:
            txn = store.newTransaction()
            yield fn(txn, record, proxyRecord, readWrite)
            yield txn.commit()
            print(
                "{msg} {proxy} as a {proxyType} proxy for {record}".format(
                    msg=msg, proxy=prettyRecord(proxyRecord),
                    proxyType=proxyType, record=prettyRecord(record)
                )
            )


@inlineCallbacks
def action_addProxy(store, record, proxyType, *proxyIDs):
    if config.GroupCaching.Enabled and config.GroupCaching.UseDirectoryBasedDelegates:
        if record.recordType in (
            record.service.recordType.location,
            record.service.recordType.resource,
        ):
            print("You are not allowed to add proxies for locations or resources via command line when their proxy assignments come from the directory service.")
            returnValue(None)

    yield _addRemoveProxy("Added", Delegates.addDelegate, store, record, proxyType, *proxyIDs)


@inlineCallbacks
def action_removeProxy(store, record, *proxyIDs):
    if config.GroupCaching.Enabled and config.GroupCaching.UseDirectoryBasedDelegates:
        if record.recordType in (
            record.service.recordType.location,
            record.service.recordType.resource,
        ):
            print("You are not allowed to remove proxies for locations or resources via command line when their proxy assignments come from the directory service.")
            returnValue(None)

    # Write
    yield _addRemoveProxy("Removed", Delegates.removeDelegate, store, record, "write", *proxyIDs)
    # Read
    yield _addRemoveProxy("Removed", Delegates.removeDelegate, store, record, "read", *proxyIDs)


@inlineCallbacks
def setProxies(record, readProxyRecords, writeProxyRecords):
    """
    Set read/write proxies en masse for a record
    @param record: L{IDirectoryRecord}
    @param readProxyRecords: a list of records
    @param writeProxyRecords: a list of records
    """

    proxyTypes = [
        (DelegateRecordType.readDelegateGroup, readProxyRecords),
        (DelegateRecordType.writeDelegateGroup, writeProxyRecords),
    ]
    for recordType, proxyRecords in proxyTypes:
        if proxyRecords is None:
            continue
        proxyGroup = yield record.service.recordWithShortName(
            recordType, record.uid
        )
        yield proxyGroup.setMembers(proxyRecords)


@inlineCallbacks
def getProxies(record):
    """
    Returns a tuple containing the records for read proxies and write proxies
    of the given record
    """

    allProxies = {
        DelegateRecordType.readDelegateGroup: [],
        DelegateRecordType.writeDelegateGroup: [],
    }
    for recordType in allProxies.iterkeys():
        proxyGroup = yield record.service.recordWithShortName(
            recordType, record.uid
        )
        allProxies[recordType] = yield proxyGroup.members()

    returnValue(
        (
            allProxies[DelegateRecordType.readDelegateGroup],
            allProxies[DelegateRecordType.writeDelegateGroup]
        )
    )


def proxySubprincipal(principal, proxyType):
    return principal.getChild("calendar-proxy-" + proxyType)


def prettyPrincipal(principal):
    return prettyRecord(principal.record)


def prettyRecord(record):
    try:
        shortNames = record.shortNames
    except AttributeError:
        shortNames = []
    return "\"{d}\" {uid} ({rt}) {sn}".format(
        d=record.displayName.encode("utf-8"),
        rt=record.recordType.name,
        uid=record.uid,
        sn=(", ".join([sn.encode("utf-8") for sn in shortNames]))
    )


def displayNameForCollection(collection):
    try:
        displayName = collection.properties()[
            PropertyName.fromElement(element.DisplayName)
        ]
        displayName = displayName.toString()
    except:
        displayName = collection.name()

    return displayName


def agoString(delta):
    if delta.days:
        agoString = "{} days ago".format(delta.days)
    elif delta.seconds:
        if delta.seconds < 60:
            agoString = "{} second{} ago".format(delta.seconds, "s" if delta.seconds > 1 else "")
        else:
            minutesAgo = delta.seconds / 60
            if minutesAgo < 60:
                agoString = "{} minute{} ago".format(minutesAgo, "s" if minutesAgo > 1 else "")
            else:
                hoursAgo = minutesAgo / 60
                agoString = "{} hour{} ago".format(hoursAgo, "s" if hoursAgo > 1 else "")
    else:
        agoString = "Just now"

    return agoString


def locationString(component):
    locationProps = component.properties("LOCATION")
    if locationProps is not None:
        locations = []
        for locationProp in locationProps:
            locations.append(locationProp.value())
        locationString = ", ".join(locations)
    else:
        locationString = ""
    return locationString


@inlineCallbacks
def getEventDetails(event):
    detail = {}

    nowPyDT = DateTime.getNowUTC()
    nowDT = datetime.datetime.utcnow()
    oneYearInFuture = DateTime.getNowUTC()
    oneYearInFuture.offsetDay(365)

    component = yield event.component()
    mainSummary = component.mainComponent().propertyValue("SUMMARY", u"<no title>")
    whenTrashed = event.whenTrashed()
    ago = nowDT - whenTrashed

    detail["summary"] = mainSummary
    detail["whenTrashed"] = agoString(ago)
    detail["recoveryID"] = event._resourceID

    if component.isRecurring():
        detail["recurring"] = True
        detail["instances"] = []
        instances = component.cacheExpandedTimeRanges(oneYearInFuture)
        instances = sorted(instances.instances.values(), key=lambda x: x.start)
        limit = 3
        count = 0
        for instance in instances:
            if instance.start >= nowPyDT:
                summary = instance.component.propertyValue("SUMMARY", u"<no title>")
                location = locationString(instance.component)
                tzid = instance.component.getProperty("DTSTART").parameterValue("TZID", None)
                dtstart = instance.start
                if tzid is not None:
                    timezone = Timezone(tzid=tzid)
                    dtstart.adjustTimezone(timezone)
                detail["instances"].append({
                    "summary": summary,
                    "starttime": dtstart.getLocaleDateTime(DateTime.FULLDATE, False, True, dtstart.getTimezoneID()),
                    "location": location
                })
                count += 1
                limit -= 1
            if limit == 0:
                break

    else:
        detail["recurring"] = False
        dtstart = component.mainComponent().propertyValue("DTSTART")
        detail["starttime"] = dtstart.getLocaleDateTime(DateTime.FULLDATE, False, True, dtstart.getTimezoneID())
        detail["location"] = locationString(component.mainComponent())

    returnValue(detail)


class ProxyError(Exception):
    """
    Raised when proxy assignments cannot be performed
    """


class ProxyWarning(Exception):
    """
    Raised for harmless proxy assignment failures such as trying to add a
    duplicate or remove a non-existent assignment.
    """
