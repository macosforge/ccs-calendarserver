#!/usr/bin/env python

##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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

# Suppress warning that occurs on Linux
import sys
if sys.platform.startswith("linux"):
    from Crypto.pct_warnings import PowmInsecureWarning
    import warnings
    warnings.simplefilter("ignore", PowmInsecureWarning)


from getopt import getopt, GetoptError

from calendarserver.tools.cmdline import utilityMain, WorkerService
from twext.python.log import Logger
from twext.who.expression import MatchExpression, MatchType
from twext.who.idirectory import RecordType, FieldName
from twext.who.util import uniqueResult
from twisted.internet.defer import inlineCallbacks, returnValue
from twistedcaldav.config import config
from twistedcaldav.directory import calendaruserproxy
from txdav.who.delegates import Delegates

log = Logger()


def usage(e=None):
    if e:
        print(e)
        print("")

    print("")
    print("  Migrates delegate assignments from external Postgres DB to Calendar Server Store")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -f --config <path>: Specify caldavd.plist configuration path")
    print("  -s --server <postgres db server hostname>")
    print("  -u --user <username>")
    print("  -p --password <password>")
    print("  -P --pod <pod name>")
    print("  -d --database <database name> (default = 'proxies')")
    print("  -t --dbtype <database type> (default = 'ProxyDB')")
    print("")

    if e:
        sys.exit(64)
    else:
        sys.exit(0)


class DelegatesMigrationService(WorkerService):
    """
    """

    function = None
    params = []

    @inlineCallbacks
    def doWork(self):
        """
        Calls the function that's been assigned to "function" and passes the root
        resource, directory, store, and whatever has been assigned to "params".
        """
        if self.function is not None:
            yield self.function(self.store, *self.params)


def main():
    try:
        (optargs, _ignore_args) = getopt(
            sys.argv[1:], "d:f:hp:P:s:t:u:", [
                "help",
                "config=",

                "server=",
                "user=",
                "password=",
                "pod=",
                "database=",
                "dbtype=",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    configFileName = None
    server = None
    user = None
    password = None
    pod = None
    database = "proxies"
    dbtype = "ProxyDB"

    for opt, arg in optargs:

        # Args come in as encoded bytes
        arg = arg.decode("utf-8")

        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

        elif opt in ("-s", "--server"):
            server = arg

        elif opt in ("-u", "--user"):
            user = arg

        elif opt in ("-P", "--pod"):
            pod = arg

        elif opt in ("-p", "--password"):
            password = arg

        elif opt in ("-d", "--database"):
            database = arg

        elif opt in ("-t", "--dbtype"):
            dbtype = arg

        else:
            raise NotImplementedError(opt)

    DelegatesMigrationService.function = migrateDelegates
    DelegatesMigrationService.params = [server, user, password, pod, database, dbtype]

    utilityMain(configFileName, DelegatesMigrationService)


@inlineCallbacks
def getAssignments(db):
    """
    Returns all the delegate assignments from the db.

    @return: a list of (delegator group, delegate) tuples
    """
    print("Fetching delegate assignments...")
    rows = yield db.query("select GROUPNAME, MEMBER from GROUPS;")
    print("Fetched {} delegate assignments".format(len(rows)))
    returnValue(rows)


@inlineCallbacks
def copyAssignments(assignments, pod, directory, store):
    """
    Go through the list of assignments from the old db, and selectively copy them
    into the new store.

    @param assignments: the assignments from the old db
    @type assignments: a list of (delegator group, delegate) tuples
    @param pod: the name of the pod you want to migrate assignments for; assignments
        for delegators who don't reside on this pod will be ignored.  Set this
        to None to copy all assignments.
    @param directory: the directory service
    @param store: the store
    """

    delegatorsMissingPodInfo = set()
    numCopied = 0
    numOtherPod = 0
    numDirectoryBased = 0
    numExamined = 0

    # If locations and resources' delegate assignments come from the directory,
    # then we're only interested in copying assignments where the delegator is a
    # user.
    if config.GroupCaching.Enabled and config.GroupCaching.UseDirectoryBasedDelegates:
        delegatorRecordTypes = (RecordType.user,)
    else:
        delegatorRecordTypes = None

    # When faulting in delegates, only worry about users and groups.
    delegateRecordTypes = (RecordType.user, RecordType.group)

    total = len(assignments)

    for groupname, delegateUID in assignments:
        numExamined += 1

        if numExamined % 100 == 0:
            print("Processed: {} of {}...".format(numExamined, total))

        if "#" in groupname:
            delegatorUID, permission = groupname.split("#")
            try:
                delegatorRecords = yield directory.recordsFromExpression(
                    MatchExpression(FieldName.uid, delegatorUID, matchType=MatchType.equals),
                    recordTypes=delegatorRecordTypes
                )
                delegatorRecord = uniqueResult(delegatorRecords)
            except Exception as e:
                print("Failed to look up record for {}: {}".format(delegatorUID, str(e)))
                continue

            if delegatorRecord is None:
                continue

            if config.GroupCaching.Enabled and config.GroupCaching.UseDirectoryBasedDelegates:
                if delegatorRecord.recordType != RecordType.user:
                    print("Skipping non-user")
                    numDirectoryBased += 1
                    continue

            if pod:
                try:
                    if delegatorRecord.serviceNodeUID != pod:
                        numOtherPod += 1
                        continue
                except AttributeError:
                    print("Record missing serviceNodeUID", delegatorRecord.fullNames)
                    delegatorsMissingPodInfo.add(delegatorUID)
                    continue

            try:
                delegateRecords = yield directory.recordsFromExpression(
                    MatchExpression(FieldName.uid, delegateUID, matchType=MatchType.equals),
                    recordTypes=delegateRecordTypes
                )
                delegateRecord = uniqueResult(delegateRecords)

            except Exception as e:
                print("Failed to look up record for {}: {}".format(delegateUID, str(e)))
                continue

            if delegateRecord is None:
                continue

            txn = store.newTransaction(label="DelegatesMigrationService")
            yield Delegates.addDelegate(
                txn, delegatorRecord, delegateRecord,
                (permission == "calendar-proxy-write")
            )
            numCopied += 1
            yield txn.commit()

    print("Total delegate assignments examined: {}".format(numExamined))
    print("Total delegate assignments migrated: {}".format(numCopied))
    if pod:
        print("Total ignored assignments because they're on another pod: {}".format(numOtherPod))
    if numDirectoryBased:
        print("Total ignored assignments because they come from the directory: {}".format(numDirectoryBased))

    if delegatorsMissingPodInfo:
        print("Delegators missing pod info:")
        for uid in delegatorsMissingPodInfo:
            print(uid)


@inlineCallbacks
def migrateDelegates(service, store, server, user, password, pod, database, dbtype):
    print("Migrating from server {}".format(server))
    try:
        calendaruserproxy.ProxyDBService = calendaruserproxy.ProxyPostgreSQLDB(server, database, user, password, dbtype)
        calendaruserproxy.ProxyDBService.open()  # @UndefinedVariable
        assignments = yield getAssignments(calendaruserproxy.ProxyDBService)
        yield copyAssignments(assignments, pod, store.directoryService(), store)
        calendaruserproxy.ProxyDBService.close()  # @UndefinedVariable

    except IOError:
        log.error("Could not start proxydb service")
        raise


if __name__ == "__main__":
    main()
