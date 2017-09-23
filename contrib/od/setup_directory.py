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
from __future__ import absolute_import

import os
import sys
from . import odframework
from . import dsattributes
from getopt import getopt, GetoptError

# TODO: Nested groups
# TODO: GroupMembership

masterNodeName = "/LDAPv3/127.0.0.1"
localNodeName = "/Local/Default"

saclGroupNodeName = "/Local/Default"
saclGroupNames = ("com.apple.access_calendar", "com.apple.access_addressbook")

masterUsers = [
    (
        "odtestamanda",
        {
            dsattributes.kDS1AttrFirstName: ["Amanda"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Amanda Test"],
            dsattributes.kDSNAttrEMailAddress: ["amanda@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A70-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33300"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestbetty",
        {
            dsattributes.kDS1AttrFirstName: ["Betty"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Betty Test"],
            dsattributes.kDSNAttrEMailAddress: ["betty@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A71-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33301"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestcarlene",
        {
            dsattributes.kDS1AttrFirstName: ["Carlene"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Carlene Test"],
            dsattributes.kDSNAttrEMailAddress: ["carlene@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A72-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33302"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestdenise",
        {
            dsattributes.kDS1AttrFirstName: ["Denise"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Denise Test"],
            dsattributes.kDSNAttrEMailAddress: ["denise@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A73-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33303"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestunicode",
        {
            dsattributes.kDS1AttrFirstName: ["Unicode " + unichr(208)],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Unicode Test " + unichr(208)],
            dsattributes.kDSNAttrEMailAddress: ["unicodetest@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["CA795296-D77A-4E09-A72F-869920A3D284"],
            dsattributes.kDS1AttrUniqueID: ["33304"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestat@sign",
        {
            dsattributes.kDS1AttrFirstName: ["AtSign"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["At Sign Test"],
            dsattributes.kDSNAttrEMailAddress: ["attsign@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["71646A3A-1CEF-4744-AB1D-0AC855E25DC8"],
            dsattributes.kDS1AttrUniqueID: ["33305"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestsatou",
        {
            dsattributes.kDS1AttrFirstName: ["\xe4\xbd\x90\xe8\x97\xa4\xe4\xbd\x90\xe8\x97\xa4\xe4\xbd\x90\xe8\x97\xa4".decode("utf-8")],
            dsattributes.kDS1AttrLastName: ["Test \xe4\xbd\x90\xe8\x97\xa4".decode("utf-8")],
            dsattributes.kDS1AttrDistinguishedName: ["\xe4\xbd\x90\xe8\x97\xa4\xe4\xbd\x90\xe8\x97\xa4\xe4\xbd\x90\xe8\x97\xa4 Test \xe4\xbd\x90\xe8\x97\xa4".decode("utf-8")],
            dsattributes.kDSNAttrEMailAddress: ["satou@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["C662F833-75AD-4589-9879-5FF102943CEF"],
            dsattributes.kDS1AttrUniqueID: ["33306"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "anotherodtestamanda",
        {
            dsattributes.kDS1AttrFirstName: ["Amanda"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Amanda Test"],
            dsattributes.kDSNAttrEMailAddress: ["anotheramanda@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["E7666814-6D92-49EC-8562-8C4C3D64A4B0"],
            dsattributes.kDS1AttrUniqueID: ["33307"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
]

masterGroups = [
    (
        "odtestsubgroupb",
        {
            dsattributes.kDS1AttrGeneratedUID: ["6C6CD282-E6E3-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrDistinguishedName: ["OD Test Subgroup B"],
            dsattributes.kDSNAttrGroupMembers: ["9DC04A72-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrPrimaryGroupID: ["33401"],
        },
    ),
    (
        "odtestgrouptop",
        {
            dsattributes.kDS1AttrGeneratedUID: ["6C6CD280-E6E3-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrDistinguishedName: ["OD Test Group Top"],
            dsattributes.kDSNAttrGroupMembers: ["9DC04A70-E6DD-11DF-9492-0800200C9A66", "9DC04A71-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDSNAttrNestedGroups: ["6C6CD282-E6E3-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrPrimaryGroupID: ["33400"],
        },
    ),
    (
        "odtestgroupbetty",
        {
            dsattributes.kDS1AttrGeneratedUID: ["2A1F3ED9-D1B3-40F2-8FC4-05E197C1F90C"],
            dsattributes.kDS1AttrDistinguishedName: ["OD Test Group Betty"],
            dsattributes.kDSNAttrGroupMembers: [],
            dsattributes.kDSNAttrNestedGroups: [],
            dsattributes.kDS1AttrPrimaryGroupID: ["33403"],
        },
    ),
]

localUsers = [
    (
        "odtestalbert",
        {
            dsattributes.kDS1AttrFirstName: ["Albert"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Albert Test"],
            dsattributes.kDSNAttrEMailAddress: ["albert@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A74-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33350"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestbill",
        {
            dsattributes.kDS1AttrFirstName: ["Bill"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Bill Test"],
            dsattributes.kDSNAttrEMailAddress: ["bill@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A75-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33351"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestcarl",
        {
            dsattributes.kDS1AttrFirstName: ["Carl"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Carl Test"],
            dsattributes.kDSNAttrEMailAddress: ["carl@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A76-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33352"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "odtestdavid",
        {
            dsattributes.kDS1AttrFirstName: ["David"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["David Test"],
            dsattributes.kDSNAttrEMailAddress: ["david@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["9DC04A77-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrUniqueID: ["33353"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
    (
        "anotherodtestalbert",
        {
            dsattributes.kDS1AttrFirstName: ["Albert"],
            dsattributes.kDS1AttrLastName: ["Test"],
            dsattributes.kDS1AttrDistinguishedName: ["Albert Test"],
            dsattributes.kDSNAttrEMailAddress: ["anotheralbert@example.com"],
            dsattributes.kDS1AttrGeneratedUID: ["8F059F1B-1CD0-42B5-BEA2-6A36C9B5620F"],
            dsattributes.kDS1AttrUniqueID: ["33354"],
            dsattributes.kDS1AttrPrimaryGroupID: ["20"],
        },
    ),
]

localGroups = [
    (
        "odtestsubgroupa",
        {
            dsattributes.kDS1AttrGeneratedUID: ["6C6CD281-E6E3-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrDistinguishedName: ["OD Test Subgroup A"],
            dsattributes.kDSNAttrGroupMembers: ["9DC04A74-E6DD-11DF-9492-0800200C9A66", "9DC04A75-E6DD-11DF-9492-0800200C9A66"],
            dsattributes.kDS1AttrPrimaryGroupID: ["33402"],
        },
    ),
    (
        "odtestgroupalbert",
        {
            dsattributes.kDS1AttrGeneratedUID: ["3F4D01B8-FDFD-4805-A853-DE9879A2D951"],
            dsattributes.kDS1AttrDistinguishedName: ["OD Test Group Albert"],
            dsattributes.kDSNAttrGroupMembers: [],
            dsattributes.kDS1AttrPrimaryGroupID: ["33404"],
        },
    ),
]


def usage(e=None):
    name = os.path.basename(sys.argv[0])
    print("usage: %s [options] local_user local_password odmaster_user odmaster_password" % (name,))
    print("")
    print(" Configures local and OD master directories for testing")
    print("")
    print("options:")
    print(" -h --help: print this help and exit")
    if e:
        sys.exit(1)
    else:
        sys.exit(0)


def lookupRecordName(node, recordType, name):
    query, error = odframework.ODQuery.queryWithNode_forRecordTypes_attribute_matchType_queryValues_returnAttributes_maximumResults_error_(
        node,
        recordType,
        dsattributes.kDSNAttrRecordName,
        dsattributes.eDSExact,
        name,
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
        raise ODError("Multiple records for '%s' were found" % (name,))

    return records[0]


def createRecord(node, recordType, recordName, attrs):
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

    try:
        (optargs, args) = getopt(sys.argv[1:], "h", ["help"])
    except GetoptError as e:
        usage(e)

    for opt, _ignore_arg in optargs:
        if opt in ("-h", "--help"):
            usage()

    if len(args) != 4:
        usage()

    localUser, localPassword, masterUser, masterPassword = args

    userInfo = {
        masterNodeName: {
            "user": masterUser,
            "password": masterPassword,
            "users": masterUsers,
            "groups": masterGroups,
        },
        localNodeName: {
            "user": localUser,
            "password": localPassword,
            "users": localUsers,
            "groups": localGroups,
        },
    }

    session = odframework.ODSession.defaultSession()
    userRecords = []

    for nodeName, info in userInfo.iteritems():

        userName = info["user"]
        password = info["password"]
        users = info["users"]
        groups = info["groups"]

        node, error = odframework.ODNode.nodeWithSession_name_error_(session, nodeName, None)
        if error:
            print(error)
            raise ODError(error)

        result, error = node.setCredentialsWithRecordType_recordName_password_error_(
            dsattributes.kDSStdRecordTypeUsers,
            userName,
            password,
            None
        )
        if error:
            print("Unable to authenticate with directory %s: %s" % (nodeName, error))
            raise ODError(error)

        print("Successfully authenticated with directory %s" % (nodeName,))

        print("Creating users within %s:" % (nodeName,))
        for recordName, attrs in users:
            record = lookupRecordName(node, dsattributes.kDSStdRecordTypeUsers, recordName)
            if record is None:
                print("Creating user %s" % (recordName,))
                try:
                    record = createRecord(node, dsattributes.kDSStdRecordTypeUsers, recordName, attrs)
                    print("Successfully created user %s" % (recordName,))
                    result, error = record.changePassword_toPassword_error_(
                        None, "password", None)
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

        print("Creating groups within %s:" % (nodeName,))
        for recordName, attrs in groups:
            record = lookupRecordName(node, dsattributes.kDSStdRecordTypeGroups, recordName)
            if record is None:
                print("Creating group %s" % (recordName,))
                try:
                    record = createRecord(node, dsattributes.kDSStdRecordTypeGroups, recordName, attrs)
                    print("Successfully created group %s" % (recordName,))
                except ODError as e:
                    print("Failed to create group %s: %s" % (recordName, e))
            else:
                print("Group %s already exists" % (recordName,))

        print()

    # Populate SACL groups
    node, error = odframework.ODNode.nodeWithSession_name_error_(session, saclGroupNodeName, None)
    result, error = node.setCredentialsWithRecordType_recordName_password_error_(
        dsattributes.kDSStdRecordTypeUsers,
        userInfo[saclGroupNodeName]["user"],
        userInfo[saclGroupNodeName]["password"],
        None
    )
    if not error:
        for saclGroupName in saclGroupNames:
            saclGroupRecord = lookupRecordName(node, dsattributes.kDSStdRecordTypeGroups, saclGroupName)
            if saclGroupRecord:
                print("Populating %s SACL group:" % (saclGroupName,))
                for userRecord in userRecords:
                    details, error = userRecord.recordDetailsForAttributes_error_(None, None)
                    recordName = details.get(dsattributes.kDSNAttrRecordName, [None])[0]
                    result, error = saclGroupRecord.isMemberRecord_error_(userRecord, None)
                    if result:
                        print("%s is already in the %s SACL group" % (recordName, saclGroupName))
                    else:
                        result, error = saclGroupRecord.addMemberRecord_error_(userRecord, None)
                        print("Adding %s to the %s SACL group" % (recordName, saclGroupName))

            print("")


class ODError(Exception):

    def __init__(self, error):
        self.message = (str(error), error.code())

if __name__ == "__main__":
    main()
