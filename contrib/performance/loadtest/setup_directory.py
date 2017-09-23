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
from __future__ import print_function

from contrib.od import dsattributes
from contrib.od import odframework
from getopt import getopt, GetoptError
from getpass import getpass
import os
import sys


def usage(e=None):
    name = os.path.basename(sys.argv[0])
    print("usage: %s [options]" % (name,))
    print("")
    print(" Configures OD master directories for performance simulator")
    print(" testing and generates the simulators accounts CSV file.")
    print("")
    print("options:")
    print(" -n            : number of users to generate [100]")
    print(" -u <user>     : directory admin user name [diradmin]")
    print(" -p <password> : directory admin password")
    print(" -h --help     : print this help and exit")
    if e:
        sys.exit(1)
    else:
        sys.exit(0)


def lookupRecordName(node, recordType, ctr):
    recordName = "user{:02d}".format(ctr)
    query, error = odframework.ODQuery.queryWithNode_forRecordTypes_attribute_matchType_queryValues_returnAttributes_maximumResults_error_(
        node,
        recordType,
        dsattributes.kDSNAttrRecordName,
        dsattributes.eDSExact,
        recordName,
        None,
        0,
        None)
    if error:
        raise ODError(error)
    records, error = query.resultsAllowingPartial_error_(False, None)
    if error:
        raise ODError(error)

    if len(records) < 1:
        return None
    if len(records) > 1:
        raise ODError("Multiple records for '%s' were found" % (recordName,))

    return records[0]


def createRecord(node, recordType, ctr):
    recordName = "user{:02d}".format(ctr)
    attrs = {
        dsattributes.kDS1AttrFirstName: ["User"],
        dsattributes.kDS1AttrLastName: ["{:02d}".format(ctr)],
        dsattributes.kDS1AttrDistinguishedName: ["User {:02d}".format(ctr)],
        dsattributes.kDSNAttrEMailAddress: ["user{:02d}@example.com".format(ctr)],
        dsattributes.kDS1AttrGeneratedUID: ["10000000-0000-0000-0000-000000000{:03d}".format(ctr)],
        dsattributes.kDS1AttrUniqueID: ["33{:03d}".format(ctr)],
        dsattributes.kDS1AttrPrimaryGroupID: ["20"],
    }
    record, error = node.createRecordWithRecordType_name_attributes_error_(
        recordType,
        recordName,
        attrs,
        None)
    if error:
        print(error)
        raise ODError(error)
    return record


def main():

    numUsers = 100
    masterUser = "diradmin"
    masterPassword = ""
    try:
        (optargs, args) = getopt(sys.argv[1:], "hn:p:u:", ["help"])
    except GetoptError as e:
        usage(e)

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()
        elif opt == "-n":
            numUsers = int(arg)
        elif opt == "-u":
            masterUser = arg
        elif opt == "-p":
            masterPassword = arg

    if len(args) != 0:
        usage()

    if not masterPassword:
        masterPassword = getpass("Directory Admin Password: ")

    nodeName = "/LDAPv3/127.0.0.1"

    session = odframework.ODSession.defaultSession()
    userRecords = []

    node, error = odframework.ODNode.nodeWithSession_name_error_(session, nodeName, None)
    if error:
        print(error)
        raise ODError(error)

    result, error = node.setCredentialsWithRecordType_recordName_password_error_(
        dsattributes.kDSStdRecordTypeUsers,
        masterUser,
        masterPassword,
        None
    )
    if error:
        print("Unable to authenticate with directory %s: %s" % (nodeName, error))
        raise ODError(error)

    print("Successfully authenticated with directory %s" % (nodeName,))

    print("Creating users within %s:" % (nodeName,))
    for ctr in range(numUsers):
        recordName = "user{:02d}".format(ctr + 1)
        record = lookupRecordName(node, dsattributes.kDSStdRecordTypeUsers, ctr + 1)
        if record is None:
            print("Creating user %s" % (recordName,))
            try:
                record = createRecord(node, dsattributes.kDSStdRecordTypeUsers, ctr + 1)
                print("Successfully created user %s" % (recordName,))
                result, error = record.changePassword_toPassword_error_(None, recordName, None)
                if error or not result:
                    print("Failed to set password for %s: %s" % (recordName, error))
                else:
                    print("Successfully set password for %s" % (recordName,))
            except ODError as e:
                print("Failed to create user %s: %s" % (recordName, e))
        else:
            print("User %s already exists" % (recordName,))

        if record is not None:
            userRecords.append(record)


class ODError(Exception):

    def __init__(self, error):
        self.message = (str(error), error.code())


if __name__ == "__main__":
    main()
