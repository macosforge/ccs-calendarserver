#!/usr/bin/env python

##
# Copyright (c) 2014-2017 Apple Inc. All rights reserved.
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
import sys


from getopt import getopt, GetoptError
import os
from plistlib import readPlist, readPlistFromString
import re
import subprocess
import urllib2


PREFS_PLIST = "/Library/Server/Preferences/Calendar.plist"
ServerHostName = ""


class FileNotFound(Exception):
    """
    Missing file exception
    """


def usage(e=None):
    if e:
        print(e)
        print("")

    name = os.path.basename(sys.argv[0])
    print("usage: {} [options]".format(name))
    print("options:")
    print("  -h --help: print this help and exit")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


def main():

    if os.getuid() != 0:
        usage("This program must be run as root")

    try:
        optargs, _ignore_args = getopt(
            sys.argv[1:], "h", [
                "help",
                "phantom",
            ],
        )
    except GetoptError as e:
        usage(e)

    for opt, arg in optargs:

        # Args come in as encoded bytes
        arg = arg.decode("utf-8")

        if opt in ("-h", "--help"):
            usage()

        elif opt == "--phantom":
            result = detectPhantomVolume()
            sys.exit(result)

    osBuild = getOSBuild()
    print("OS Build: {}".format(osBuild))

    serverBuild = getServerBuild()
    print("Server Build: {}".format(serverBuild))

    print()

    try:
        if checkPlist(PREFS_PLIST):
            print("{} exists and can be parsed".format(PREFS_PLIST))
        else:
            print("{} exists but cannot be parsed".format(PREFS_PLIST))
    except FileNotFound:
        print("{} does not exist (but that's ok)".format(PREFS_PLIST))

    serverRoot = getServerRoot()
    print("Prefs plist says ServerRoot directory is: {}".format(serverRoot.encode("utf-8")))

    result = detectPhantomVolume(serverRoot)
    if result == EXIT_CODE_OK:
        print("ServerRoot volume ok")
    elif result == EXIT_CODE_SERVER_ROOT_MISSING:
        print("ServerRoot directory missing")
    elif result == EXIT_CODE_PHANTOM_DATA_VOLUME:
        print("Phantom ServerRoot volume detected")

    systemPlist = os.path.join(serverRoot, "Config", "caldavd-system.plist")
    try:
        if checkPlist(systemPlist):
            print("{} exists and can be parsed".format(systemPlist.encode("utf-8")))
        else:
            print("{} exists but cannot be parsed".format(systemPlist.encode("utf-8")))
    except FileNotFound:
        print("{} does not exist".format(systemPlist.encode("utf-8")))

    userPlist = os.path.join(serverRoot, "Config", "caldavd-user.plist")
    try:
        if checkPlist(userPlist):
            print("{} exists and can be parsed".format(userPlist.encode("utf-8")))
        else:
            print("{} exists but cannot be parsed".format(userPlist.encode("utf-8")))
    except FileNotFound:
        print("{} does not exist".format(userPlist.encode("utf-8")))

    keys = showConfigKeys()

    showProcesses()

    showServerctlStatus()

    showDiskSpace(serverRoot)

    postgresRunning = showPostgresStatus(serverRoot)

    if postgresRunning:
        showPostgresContent()

    password = getPasswordFromKeychain("com.apple.calendarserver")

    connectToAgent(password)

    connectToCaldavd(keys)

    showWebApps()


EXIT_CODE_OK = 0
EXIT_CODE_SERVER_ROOT_MISSING = 1
EXIT_CODE_PHANTOM_DATA_VOLUME = 2


def detectPhantomVolume(serverRoot=None):
    """
    Check to see if serverRoot directory exists in a "phantom" volume, meaning
    it's simply a directory under /Volumes residing on the boot volume, rather
    that a real separate volume.
    """

    if not serverRoot:
        serverRoot = getServerRoot()

    if not os.path.exists(serverRoot):
        return EXIT_CODE_SERVER_ROOT_MISSING

    if serverRoot.startswith("/Volumes/"):
        bootDevice = os.stat("/").st_dev
        dataDevice = os.stat(serverRoot).st_dev
        if bootDevice == dataDevice:
            return EXIT_CODE_PHANTOM_DATA_VOLUME

    return EXIT_CODE_OK


def showProcesses():

    print()
    print("Calendar and Contacts service processes:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/bin/ps", "ax",
        "-o user",
        "-o pid",
        "-o %cpu",
        "-o %mem",
        "-o rss",
        "-o etime",
        "-o lstart",
        "-o command"
    )
    for line in stdout.split("\n"):
        if "_calendar" in line or "CalendarServer" in line or "COMMAND" in line:
            print(line)


def showServerctlStatus():

    print()
    print("Serverd status:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/sbin/serverctl",
        "list",
    )
    services = {
        "org.calendarserver.agent": False,
        "org.calendarserver.calendarserver": False,
        "org.calendarserver.relocate": False,
    }

    enabledBucket = False

    for line in stdout.split("\n"):
        if "enabledServices" in line:
            enabledBucket = True
        if "disabledServices" in line:
            enabledBucket = False

        for service in services:
            if service in line:
                services[service] = enabledBucket

    for service, enabled in services.iteritems():
        print(
            "{service} is {enabled}".format(
                service=service,
                enabled="enabled" if enabled else "disabled"
            )
        )


def showDiskSpace(serverRoot):

    print()
    print("Disk space on boot volume:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/bin/df",
        "-H",
        "/",
    )
    print(stdout)

    print("Disk space on service data volume:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/bin/df",
        "-H",
        serverRoot
    )
    print(stdout)

    print("Disk space used by Calendar and Contacts service:")
    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/usr/bin/du",
        "-sh",
        os.path.join(serverRoot, "Config"),
        os.path.join(serverRoot, "Data"),
        os.path.join(serverRoot, "Logs"),
    )
    print(stdout)


def showPostgresStatus(serverRoot):

    clusterPath = os.path.join(serverRoot, "Data", "Database.xpg", "cluster.pg")

    print()
    print("Postgres status for cluster {}:".format(clusterPath.encode("utf-8")))

    code, stdout, stderr = runCommand(
        "/usr/bin/sudo",
        "-u",
        "calendar",
        "/Applications/Server.app/Contents/ServerRoot/usr/bin/pg_ctl",
        "status",
        "-D",
        clusterPath
    )
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)
    if code:
        return False
    return True


def runSQLQuery(query):

    _ignore_code, stdout, stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/bin/psql",
        "-h",
        "/var/run/caldavd/PostgresSocket",
        "--dbname=caldav",
        "--username=caldav",
        "--command={}".format(query),
    )
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)


def countFromSQLQuery(query):

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/bin/psql",
        "-h",
        "/var/run/caldavd/PostgresSocket",
        "--dbname=caldav",
        "--username=caldav",
        "--command={}".format(query),
    )
    lines = stdout.split("\n")
    try:
        count = int(lines[2])
    except IndexError:
        count = 0
    return count


def listDatabases():

    _ignore_code, stdout, stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/bin/psql",
        "-h",
        "/var/run/caldavd/PostgresSocket",
        "--dbname=caldav",
        "--username=caldav",
        "--list",
    )
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)


def showPostgresContent():

    print()
    print("Postgres content:")
    print()

    listDatabases()

    print("'calendarserver' table...")
    runSQLQuery("select * from calendarserver;")

    count = countFromSQLQuery("select count(*) from calendar_home;")
    print("Number of calendar homes: {}".format(count))

    count = countFromSQLQuery("select count(*) from calendar_object;")
    print("Number of calendar events: {}".format(count))

    count = countFromSQLQuery("select count(*) from addressbook_home;")
    print("Number of contacts homes: {}".format(count))

    count = countFromSQLQuery("select count(*) from addressbook_object;")
    print("Number of contacts cards: {}".format(count))

    count = countFromSQLQuery("select count(*) from delegates;")
    print("Number of non-group delegate assignments: {}".format(count))

    count = countFromSQLQuery("select count(*) from delegate_groups;")
    print("Number of group delegate assignments: {}".format(count))

    print("'job' table...")
    runSQLQuery("select * from job;")


def showConfigKeys():

    print()
    print("Configuration:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/sbin/calendarserver_config",
        "EnableCalDAV",
        "EnableCardDAV",
        "Notifications.Services.APNS.Enabled",
        "Scheduling.iMIP.Enabled",
        "Authentication.Basic.Enabled",
        "Authentication.Digest.Enabled",
        "Authentication.Kerberos.Enabled",
        "ServerHostName",
        "HTTPPort",
        "SSLPort",
    )
    hidden = [
        "ServerHostName",
    ]
    keys = {}
    for line in stdout.split("\n"):
        if "=" in line:
            key, value = line.strip().split("=", 1)
            keys[key] = value
            if key not in hidden:
                print("{key} : {value}".format(key=key, value=value))
    return keys


def runCommand(commandPath, *args):
    """
    Run a command line tool and return the output
    """
    if not os.path.exists(commandPath):
        raise FileNotFound

    commandLine = [commandPath]
    if args:
        commandLine.extend(args)

    child = subprocess.Popen(
        args=commandLine,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output, error = child.communicate()
    return child.returncode, output, error


def getOSBuild():
    try:
        code, stdout, _ignore_stderr = runCommand("/usr/bin/sw_vers", "-buildVersion")
        if not code:
            return stdout.strip()
    except:
        return "Unknown"


def getServerBuild():
    try:
        code, stdout, _ignore_stderr = runCommand("/usr/sbin/serverinfo", "--buildversion")
        if not code:
            return stdout.strip()
    except:
        pass
    return "Unknown"


def getServerRoot():
    """
    Return the ServerRoot value from the servermgr_calendar.plist.  If not
    present, return the default.

    @rtype: C{unicode}
    """
    try:
        serverRoot = u"/Library/Server/Calendar and Contacts"
        if os.path.exists(PREFS_PLIST):
            serverRoot = readPlist(PREFS_PLIST).get("ServerRoot", serverRoot)
        if isinstance(serverRoot, str):
            serverRoot = serverRoot.decode("utf-8")
        return serverRoot
    except:
        return "Unknown"


def checkPlist(plistPath):
    if not os.path.exists(plistPath):
        raise FileNotFound

    try:
        readPlist(plistPath)
    except:
        return False

    return True


def showWebApps():
    print()
    print("Web apps:")

    _ignore_code, stdout, _ignore_stderr = runCommand(
        "/Applications/Server.app/Contents/ServerRoot/usr/sbin/webappctl",
        "status",
        "-"
    )
    print(stdout)


##
# Keychain access
##

passwordRegExp = re.compile(r'password: "(.*)"')


def getPasswordFromKeychain(account):
    code, _ignore_stdout, stderr = runCommand(
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        account,
        "-g",
    )
    if code:
        return None
    else:
        match = passwordRegExp.search(stderr)
        if not match:
            print(
                "Password for {} not found in keychain".format(account)
            )
            return None
        else:
            return match.group(1)


readCommand = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>readConfig</string>
</dict>
</plist>
"""


def connectToAgent(password):

    print()
    print("Agent:")

    url = "http://localhost:62308/gateway/"
    user = "com.apple.calendarserver"
    auth_handler = urllib2.HTTPDigestAuthHandler()
    auth_handler.add_password(
        realm="/Local/Default",
        uri=url,
        user=user,
        passwd=password
    )
    opener = urllib2.build_opener(auth_handler)
    # ...and install it globally so it can be used with urlopen.
    urllib2.install_opener(opener)

    # Send HTTP POST request
    request = urllib2.Request(url, readCommand)
    try:
        print("Attempting to send a request to the agent...")
        response = urllib2.urlopen(request, timeout=30)
    except Exception as e:
        print("Can't connect to agent: {}".format(e))
        return False

    html = response.read()
    code = response.getcode()
    if code == 200:
        try:
            data = readPlistFromString(html)
        except Exception as e:
            print(
                "Could not parse response from agent: {error}\n{html}".format(
                    error=e, html=html
                )
            )
            return False

        if "result" in data:
            print("...success")
        else:
            print("Error in agent's response:\n{}".format(html))
            return False
    else:
        print("Got an error back from the agent: {code} {html}".format(
            code=code, html=html)
        )

    return True


def connectToCaldavd(keys):

    print()
    print("Server connection:")

    url = "https://{host}/principals/".format(host=keys["ServerHostName"])
    try:
        print("Attempting to send a request to port 443...")
        response = urllib2.urlopen(url, timeout=30)
        html = response.read()
        code = response.getcode()
        print(code, html)
        if code == 200:
            print("Received 200 response")

    except urllib2.HTTPError as e:
        code = e.code
        reason = e.reason

        if code == 401:
            print("Got the expected response")
        else:
            print(
                "Got an unexpected response: {code} {reason}".format(
                    code=code, reason=reason
                )
            )

    except Exception as e:
        print("Can't connect to port 443: {error}".format(error=e))


if __name__ == "__main__":
    main()
