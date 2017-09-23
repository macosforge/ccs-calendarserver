#!/usr/bin/env python
##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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

from calendarserver.tools.util import loadConfig

from pycalendar.datetime import DateTime
from pycalendar.icalendar.calendar import Calendar

from twext.python.log import Logger

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.logger import FileLogObserver, formatEventAsClassicLogText
from twisted.python.filepath import FilePath

from twistedcaldav.config import ConfigurationError
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
from twistedcaldav.timezones import TimezoneCache
from twistedcaldav.timezonestdservice import PrimaryTimezoneDatabase, \
    SecondaryTimezoneDatabase

from zonal.tzconvert import tzconvert

import getopt
import os
import sys
import tarfile
import tempfile
import urllib


def _doPrimaryActions(action, tzpath, xmlfile, changed, tzvers):

    tzdb = PrimaryTimezoneDatabase(tzpath, xmlfile)
    if action == "refresh":
        _doRefresh(tzpath, xmlfile, tzdb, tzvers)

    elif action == "create":
        _doCreate(xmlfile, tzdb)

    elif action == "update":
        _doUpdate(xmlfile, tzdb)

    elif action == "list":
        _doList(xmlfile, tzdb)

    elif action == "changed":
        _doChanged(xmlfile, changed, tzdb)

    else:
        usage("Invalid action: {}".format(action,))


def _doRefresh(tzpath, xmlfile, tzdb, tzvers):
    """
    Refresh data from IANA.
    """

    print("Downloading latest data from IANA")
    if tzvers:
        path = "https://www.iana.org/time-zones/repository/releases/tzdata{}.tar.gz".format(tzvers,)
    else:
        path = "https://www.iana.org/time-zones/repository/tzdata-latest.tar.gz"
    data = urllib.urlretrieve(path)
    print("Extract data at: {}".format(data[0]))
    rootdir = tempfile.mkdtemp()
    zonedir = os.path.join(rootdir, "tzdata")
    os.mkdir(zonedir)
    with tarfile.open(data[0], "r:gz") as t:
        t.extractall(zonedir)

    # Get the version from the Makefile
    try:
        with open(os.path.join(zonedir, "Makefile")) as f:
            makefile = f.read()
        lines = makefile.splitlines()
        for line in lines:
            if line.startswith("VERSION="):
                tzvers = line[8:].strip()
                break
    except IOError:
        pass

    # Work around change to MAKEFILE by trying to extract the version from the NEWS file
    if tzvers == "unknown":
        tzvers = None
        try:
            with open(os.path.join(zonedir, "NEWS")) as f:
                makefile = f.read()
            lines = makefile.splitlines()
            for line in lines:
                if line.startswith("Release "):
                    tzvers = line.split()[1]
                    break
        except IOError:
            pass

    if not tzvers:
        tzvers = DateTime.getToday().getText()
    print("Converting data (version: {}) at: {}".format(tzvers, zonedir,))
    startYear = 1800
    endYear = DateTime.getToday().getYear() + 10
    Calendar.sProdID = "-//calendarserver.org//Zonal//EN"
    zonefiles = "northamerica", "southamerica", "europe", "africa", "asia", "australasia", "antarctica", "etcetera", "backward"
    parser = tzconvert()
    for file in zonefiles:
        parser.parse(os.path.join(zonedir, file))

    # Try tzextras
    extras = TimezoneCache._getTZExtrasPath()
    if os.path.exists(extras):
        print("Converting extra data at: {}".format(extras,))
        parser.parse(extras)
    else:
        print("No extra data to convert")

    # Check for windows aliases
    print("Downloading latest data from unicode.org")
    path = "http://unicode.org/repos/cldr/tags/latest/common/supplemental/windowsZones.xml"
    data = urllib.urlretrieve(path)
    wpath = data[0]

    # Generate the iCalendar data
    print("Generating iCalendar data")
    parser.generateZoneinfoFiles(os.path.join(rootdir, "zoneinfo"), startYear, endYear, windowsAliases=wpath, filterzones=())

    print("Copy new zoneinfo to destination: {}".format(tzpath,))
    z = FilePath(os.path.join(rootdir, "zoneinfo"))
    tz = FilePath(tzpath)
    z.copyTo(tz)
    print("Updating XML file at: {}".format(xmlfile,))
    tzdb.readDatabase()
    tzdb.updateDatabase()
    print("Current total: {}".format(len(tzdb.timezones),))
    print("Total Changed: {}".format(tzdb.changeCount,))
    if tzdb.changeCount:
        print("Changed:")
        for k in sorted(tzdb.changed):
            print("  {}".format(k,))

    versfile = os.path.join(os.path.dirname(xmlfile), "version.txt")
    print("Updating version file at: {}".format(versfile,))
    with open(versfile, "w") as f:
        f.write(TimezoneCache.IANA_VERSION_PREFIX + tzvers)


def _doCreate(xmlfile, tzdb):
    """
    Create new xml file.
    """

    print("Creating new XML file at: {}".format(xmlfile,))
    tzdb.createNewDatabase()
    print("Current total: {}".format(len(tzdb.timezones),))


def _doUpdate(xmlfile, tzdb):
    """
    Update xml file.
    """

    print("Updating XML file at: {}".format(xmlfile,))
    tzdb.readDatabase()
    tzdb.updateDatabase()
    print("Current total: {}".format(len(tzdb.timezones),))
    print("Total Changed: {}".format(tzdb.changeCount,))
    if tzdb.changeCount:
        print("Changed:")
        for k in sorted(tzdb.changed):
            print("  {}".format(k,))


def _doList(xmlfile, tzdb):
    """
    List current timezones from xml file.
    """

    print("Listing XML file at: {}".format(xmlfile,))
    tzdb.readDatabase()
    print("Current timestamp: {}".format(tzdb.dtstamp,))
    print("Timezones:")
    for k in sorted(tzdb.timezones.keys()):
        print("  {}".format(k,))


def _doChanged(xmlfile, changed, tzdb):
    """
    Check for local timezone changes.
    """

    print("Changes from XML file at: {}".format(xmlfile,))
    tzdb.readDatabase()
    print("Check timestamp: {}".format(changed,))
    print("Current timestamp: {}".format(tzdb.dtstamp,))
    results = [k for k, v in tzdb.timezones.items() if v.dtstamp > changed]
    print("Total Changed: {}".format(len(results),))
    if results:
        print("Changed:")
        for k in sorted(results):
            print("  {}".format(k,))


@inlineCallbacks
def _runInReactor(tzdb):

    try:
        new, changed = yield tzdb.syncWithServer()
        print("New:           {}".format(new,))
        print("Changed:       {}".format(changed,))
        print("Current total: {}".format(len(tzdb.timezones),))
    except Exception as e:
        print("Could not sync with server: {}".format(str(e),))
    finally:
        reactor.stop()


def _doSecondaryActions(action, tzpath, xmlfile, url):

    tzdb = SecondaryTimezoneDatabase(tzpath, xmlfile, url)
    try:
        tzdb.readDatabase()
    except:
        pass
    if action == "cache":
        print("Caching from secondary server: {}".format(url,))

        observer = FileLogObserver(sys.stdout, lambda event: formatEventAsClassicLogText(event))
        Logger.beginLoggingTo([observer], redirectStandardIO=False)

        reactor.callLater(0, _runInReactor, tzdb)
        reactor.run()
    else:
        usage("Invalid action: {}".format(action,))


def usage(error_msg=None):
    if error_msg:
        print(error_msg)
        print("")

    print("""Usage: managetimezones [options]
Options:
    -h            Print this help and exit
    -f            config file path [REQUIRED]
    -x            XML file path
    -z            zoneinfo file path
    --tzvers      year/release letter of IANA data to refresh
                  default: use the latest release
    --url         URL or domain of secondary service

    # Primary service
    --refresh     refresh data from IANA
    --refreshpkg  refresh package data from IANA
    --create      create new XML file
    --update      update XML file
    --list        list timezones in XML file
    --changed     changed since timestamp


    # Secondary service
    --cache       Cache data from service

Description:
    This utility will create, update, or list an XML timezone database
    summary file, or refresh iCalendar timezone from IANA (Olson). It can
    also be used to update the server's own zoneinfo database from IANA. It
    also creates aliases for the unicode.org windowsZones.

""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)


def main():
    configFileName = DEFAULT_CONFIG_FILE
    primary = False
    secondary = False
    action = None
    tzpath = None
    xmlfile = None
    changed = None
    url = None
    tzvers = None
    updatepkg = False

    # Get options
    options, _ignore_args = getopt.getopt(
        sys.argv[1:],
        "f:hx:z:",
        [
            "refresh",
            "refreshpkg",
            "create",
            "update",
            "list",
            "changed=",
            "url=",
            "cache",
            "tzvers=",
        ]
    )

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-f":
            configFileName = value
        elif option == "-x":
            xmlfile = value
        elif option == "-z":
            tzpath = value
        elif option == "--refresh":
            action = "refresh"
            primary = True
        elif option == "--refreshpkg":
            action = "refresh"
            primary = True
            updatepkg = True
        elif option == "--create":
            action = "create"
            primary = True
        elif option == "--update":
            action = "update"
            primary = True
        elif option == "--list":
            action = "list"
            primary = True
        elif option == "--changed":
            action = "changed"
            primary = True
            changed = value
        elif option == "--url":
            url = value
            secondary = True
        elif option == "--cache":
            action = "cache"
            secondary = True
        elif option == "--tzvers":
            tzvers = value
        else:
            usage("Unrecognized option: {}".format(option,))

    if configFileName is None:
        usage("A configuration file must be specified")
    try:
        loadConfig(configFileName)
    except ConfigurationError as e:
        sys.stdout.write("{}\n".format(e,))
        sys.exit(1)

    if action is None:
        action = "list"
        primary = True
    if tzpath is None:
        if updatepkg:
            try:
                import pkg_resources
            except ImportError:
                tzpath = os.path.join(os.path.dirname(__file__), "zoneinfo")
            else:
                tzpath = pkg_resources.resource_filename("twistedcaldav", "zoneinfo")  # @UndefinedVariable
        else:
            # Setup the correct zoneinfo path based on the config
            tzpath = TimezoneCache.getDBPath()
            TimezoneCache.validatePath()
    if xmlfile is None:
        xmlfile = os.path.join(tzpath, "timezones.xml")

    if primary and not os.path.isdir(tzpath):
        usage("Invalid zoneinfo path: {}".format(tzpath,))
    if primary and not os.path.isfile(xmlfile) and action != "create":
        usage("Invalid XML file path: {}".format(xmlfile,))

    if primary and secondary:
        usage("Cannot use primary and secondary options together")

    if primary:
        _doPrimaryActions(action, tzpath, xmlfile, changed, tzvers)
    else:
        _doSecondaryActions(action, tzpath, xmlfile, url)

if __name__ == '__main__':
    main()
