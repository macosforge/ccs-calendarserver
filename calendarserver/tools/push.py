#!/usr/bin/env python
##
# Copyright (c) 2012-2017 Apple Inc. All rights reserved.
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

from calendarserver.tools.cmdline import utilityMain, WorkerService
from argparse import ArgumentParser
from twext.python.log import Logger
from twisted.internet.defer import inlineCallbacks
from twext.who.idirectory import RecordType
import time

log = Logger()


class DisplayAPNSubscriptions(WorkerService):

    users = []

    def doWork(self):
        rootResource = self.rootResource()
        directory = rootResource.getDirectory()
        return displayAPNSubscriptions(self.store, directory, rootResource,
                                       self.users)


def main():

    parser = ArgumentParser(description='Display Apple Push Notification subscriptions')
    parser.add_argument('-f', '--config', dest='configFileName', metavar='CONFIGFILE', help='caldavd.plist configuration file path')
    parser.add_argument('-d', '--debug', action='store_true', help='show debug logging')
    parser.add_argument('user', help='one or more users to display', nargs='+')  # Required
    args = parser.parse_args()

    DisplayAPNSubscriptions.users = args.user

    utilityMain(
        args.configFileName,
        DisplayAPNSubscriptions,
        verbose=args.debug,
    )


@inlineCallbacks
def displayAPNSubscriptions(store, directory, root, users):
    for user in users:
        print()
        record = yield directory.recordWithShortName(RecordType.user, user)
        if record is not None:
            print("User %s (%s)..." % (user, record.uid))
            txn = store.newTransaction(label="Display APN Subscriptions")
            subscriptions = (yield txn.apnSubscriptionsBySubscriber(record.uid))
            (yield txn.commit())
            if subscriptions:
                byKey = {}
                for apnrecord in subscriptions:
                    byKey.setdefault(apnrecord.resourceKey, []).append(apnrecord)
                for key, apnsrecords in byKey.iteritems():
                    print()
                    protocol, _ignore_host, path = key.strip("/").split("/", 2)
                    resource = {
                        "CalDAV": "calendar",
                        "CardDAV": "addressbook",
                    }[protocol]
                    if "/" in path:
                        uid, collection = path.split("/")
                    else:
                        uid = path
                        collection = None
                    record = yield directory.recordWithUID(uid)
                    user = record.shortNames[0]
                    if collection:
                        print("...is subscribed to a share from %s's %s home" % (user, resource),)
                    else:
                        print("...is subscribed to %s's %s home" % (user, resource),)
                        # print("   (key: %s)\n" % (key,))
                    print("with %d device(s):" % (len(apnsrecords),))
                    for apnrecords in apnsrecords:
                        print(" %s\n   '%s' from %s\n   %s" % (
                            apnrecords.token, apnrecords.userAgent, apnrecords.ipAddr,
                            time.strftime(
                                "on %a, %d %b %Y at %H:%M:%S %z(%Z)",
                                time.localtime(apnrecords.modified)
                            )
                        ))
            else:
                print(" ...is not subscribed to anything.")
        else:
            print("User %s not found" % (user,))
