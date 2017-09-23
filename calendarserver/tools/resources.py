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

__all__ = [
    "migrateResources",
]

from getopt import getopt, GetoptError
import os
import sys

from calendarserver.tools.cmdline import utilityMain, WorkerService
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks, returnValue
from txdav.who.directory import CalendarDirectoryRecordMixin
from twext.who.directory import DirectoryRecord as BaseDirectoryRecord
from txdav.who.idirectory import RecordType


log = Logger()


class ResourceMigrationService(WorkerService):

    @inlineCallbacks
    def doWork(self):
        try:
            from txdav.who.opendirectory import (
                DirectoryService as OpenDirectoryService
            )
        except ImportError:
            returnValue(None)
        sourceService = OpenDirectoryService()
        sourceService.recordType = RecordType

        destService = self.store.directoryService()
        yield migrateResources(sourceService, destService)


def usage():

    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] " % (name,))
    print("")
    print("  Migrates resources and locations from OD to Calendar Server")
    print("")
    print("options:")
    print("  -h --help: print this help and exit")
    print("  -f --config <path>: Specify caldavd.plist configuration path")
    print("  -v --verbose: print debugging information")
    print("")

    sys.exit(0)


def main():
    try:
        (optargs, _ignore_args) = getopt(
            sys.argv[1:], "hf:", [
                "help",
                "config=",
            ],
        )
    except GetoptError as e:
        usage(e)

    #
    # Get configuration
    #
    configFileName = None
    verbose = False

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()

        elif opt in ("-f", "--config"):
            configFileName = arg

        else:
            raise NotImplementedError(opt)

    utilityMain(configFileName, ResourceMigrationService, verbose=verbose)


class DirectoryRecord(BaseDirectoryRecord, CalendarDirectoryRecordMixin):
    pass


@inlineCallbacks
def migrateResources(sourceService, destService, verbose=False):
    """
    Fetch all the locations and resources from sourceService that are not
    already in destService and copy them into destService.
    """

    destRecords = []

    for recordType in (
        RecordType.resource,
        RecordType.location,
    ):
        records = yield sourceService.recordsWithRecordType(recordType)
        for sourceRecord in records:
            destRecord = yield destService.recordWithUID(sourceRecord.uid)
            if destRecord is None:
                if verbose:
                    print(
                        "Migrating {recordType} {uid}".format(
                            recordType=recordType.name,
                            uid=sourceRecord.uid
                        )
                    )
                fields = sourceRecord.fields.copy()
                fields[destService.fieldName.recordType] = destService.recordType.lookupByName(recordType.name)

                # Only interested in these fields:
                fn = destService.fieldName
                interestingFields = [
                    fn.recordType, fn.shortNames, fn.uid, fn.fullNames, fn.guid
                ]
                for key in fields.keys():
                    if key not in interestingFields:
                        del fields[key]

                destRecord = DirectoryRecord(destService, fields)
                destRecords.append(destRecord)

    if destRecords:
        yield destService.updateRecords(destRecords, create=True)


if __name__ == "__main__":
    main()
