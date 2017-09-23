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

"""
OpenDirectory live service tests.
"""

from __future__ import print_function

from itertools import chain
from uuid import UUID

from twisted.trial import unittest
from twisted.internet.defer import inlineCallbacks, returnValue


try:
    from twext.who.opendirectory import DirectoryService
    moduleImported = True
except:
    moduleImported = False
    print("Could not import OpenDirectory")

if moduleImported:

    from twext.who.expression import (
        CompoundExpression, Operand, MatchExpression, MatchType, MatchFlags
    )
    from txdav.who.directory import CalendarDirectoryServiceMixin
    from txdav.who.opendirectory import DirectoryService as OpenDirectoryService

    class CalOpenDirectoryService(OpenDirectoryService, CalendarDirectoryServiceMixin):
        pass

    LOCAL_SHORTNAMES = "odtestalbert odtestbill odtestcarl odtestdavid odtestsubgroupa".split()
    NETWORK_SHORTNAMES = "odtestamanda odtestbetty odtestcarlene odtestdenise odtestsubgroupb odtestgrouptop".split()

    def onlyIfPopulated(func):
        """
        Only run the decorated test method if the "odtestamanda" record exists
        """
        @inlineCallbacks
        def checkThenRun(self):
            record = yield self.service.recordWithShortName(self.service.recordType.user, u"odtestamanda")
            if record is not None:
                result = yield func(self)
                returnValue(result)
            else:
                print("OD not populated, skipping {}".format(func.__name__))
        return checkThenRun

    class LiveOpenDirectoryServiceTestCase(unittest.TestCase):
        """
        Live service tests for L{DirectoryService}.
        """

        def setUp(self):
            self.service = DirectoryService()

        def tearDown(self):
            self.service._deletePool()

        def verifyResults(self, records, expected, unexpected):
            shortNames = []
            for record in records:
                for shortName in record.shortNames:
                    shortNames.append(shortName)

            for name in expected:
                self.assertTrue(name in shortNames)
            for name in unexpected:
                self.assertFalse(name in shortNames)

        @onlyIfPopulated
        @inlineCallbacks
        def test_shortNameStartsWith(self):
            records = yield self.service.recordsFromExpression(
                MatchExpression(
                    self.service.fieldName.shortNames, u"odtest",
                    matchType=MatchType.startsWith
                )
            )
            self.verifyResults(
                records,
                chain(LOCAL_SHORTNAMES, NETWORK_SHORTNAMES),
                ["anotherodtestamanda", "anotherodtestalbert"]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_uid(self):
            for uid, name in (
                (u"9DC04A71-E6DD-11DF-9492-0800200C9A66", u"odtestbetty"),
                (u"9DC04A75-E6DD-11DF-9492-0800200C9A66", u"odtestbill"),
            ):
                record = yield self.service.recordWithUID(uid)
                self.assertTrue(record is not None)
                self.assertEquals(record.shortNames[0], name)

        @onlyIfPopulated
        @inlineCallbacks
        def test_guid(self):
            for guid, name in (
                (UUID("9DC04A71-E6DD-11DF-9492-0800200C9A66"), u"odtestbetty"),
                (UUID("9DC04A75-E6DD-11DF-9492-0800200C9A66"), u"odtestbill"),
            ):
                record = yield self.service.recordWithGUID(guid)
                self.assertTrue(record is not None)
                self.assertEquals(record.shortNames[0], name)

        @onlyIfPopulated
        @inlineCallbacks
        def test_compoundWithoutRecordType(self):
            expression = CompoundExpression(
                [
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"be",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"be",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"test",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"test",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                ],
                Operand.AND
            )
            records = yield self.service.recordsFromExpression(expression)

            # We should get back users and groups since we did not specify a type:
            self.verifyResults(
                records,
                [
                    "odtestbetty", "odtestalbert", "anotherodtestalbert",
                    "odtestgroupbetty", "odtestgroupalbert"
                ],
                ["odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb"]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_compoundWithExplicitRecordType(self):
            expression = CompoundExpression(
                [
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"be",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"be",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"test",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"test",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                ],
                Operand.AND
            )
            records = yield self.service.recordsFromExpression(
                expression, recordTypes=[self.service.recordType.user]
            )

            # We should get back users but not groups:
            self.verifyResults(
                records,
                ["odtestbetty", "odtestalbert", "anotherodtestalbert"],
                ["odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb"]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_compoundWithMultipleExplicitRecordTypes(self):
            expression = CompoundExpression(
                [
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"be",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"be",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                    CompoundExpression(
                        [
                            MatchExpression(
                                self.service.fieldName.fullNames, u"test",
                                matchType=MatchType.contains
                            ),
                            MatchExpression(
                                self.service.fieldName.emailAddresses, u"test",
                                matchType=MatchType.startsWith
                            ),
                        ],
                        Operand.OR
                    ),
                ],
                Operand.AND
            )
            records = yield self.service.recordsFromExpression(
                expression, recordTypes=[
                    self.service.recordType.user,
                    self.service.recordType.group
                ]
            )

            # We should get back users and groups:
            self.verifyResults(
                records,
                [
                    "odtestbetty", "odtestalbert", "anotherodtestalbert",
                    "odtestgroupbetty", "odtestgroupalbert"
                ],
                ["odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb"]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingTokens(self):
            self.calService = CalOpenDirectoryService()
            records = yield self.calService.recordsMatchingTokens([u"be", u"test"])
            self.verifyResults(
                records,
                [
                    "odtestbetty", "odtestalbert", "anotherodtestalbert",
                    "odtestgroupbetty", "odtestgroupalbert"
                ],
                ["odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb"]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingTokensWithContextUser(self):
            self.calService = CalOpenDirectoryService()
            records = yield self.calService.recordsMatchingTokens(
                [u"be", u"test"],
                context=self.calService.searchContext_user
            )
            self.verifyResults(
                records,
                [
                    "odtestbetty", "odtestalbert", "anotherodtestalbert",
                ],
                [
                    "odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb",
                    "odtestgroupbetty", "odtestgroupalbert"
                ]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingTokensWithContextGroup(self):
            self.calService = CalOpenDirectoryService()
            records = yield self.calService.recordsMatchingTokens(
                [u"be", u"test"],
                context=self.calService.searchContext_group
            )
            self.verifyResults(
                records,
                [
                    "odtestgroupbetty", "odtestgroupalbert"
                ],
                [
                    "odtestamanda", "odtestbill", "odtestgroupa", "odtestgroupb",
                    "odtestbetty", "odtestalbert", "anotherodtestalbert"
                ]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingMultipleFieldsNoRecordType(self):
            self.calService = CalOpenDirectoryService()
            fields = (
                (u"fullNames", u"be", MatchFlags.caseInsensitive, MatchType.contains),
                (u"fullNames", u"test", MatchFlags.caseInsensitive, MatchType.contains),
            )
            records = (yield self.calService.recordsMatchingFields(
                fields, operand=Operand.AND, recordType=None
            ))
            self.verifyResults(
                records,
                [
                    "odtestgroupbetty", "odtestgroupalbert",
                    "odtestbetty", "odtestalbert", "anotherodtestalbert"
                ],
                [
                    "odtestamanda",
                ]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingSingleFieldNoRecordType(self):
            self.calService = CalOpenDirectoryService()
            fields = (
                (u"fullNames", u"test", MatchFlags.caseInsensitive, MatchType.contains),
            )
            records = (yield self.calService.recordsMatchingFields(
                fields, operand=Operand.AND, recordType=None
            ))
            self.verifyResults(
                records,
                [
                    "odtestgroupbetty", "odtestgroupalbert",
                    "odtestbetty", "odtestalbert", "anotherodtestalbert",
                    "odtestamanda",
                ],
                [
                    "nobody",
                ]
            )

        @onlyIfPopulated
        @inlineCallbacks
        def test_recordsMatchingFieldsWithRecordType(self):
            self.calService = CalOpenDirectoryService()
            fields = (
                (u"fullNames", u"be", MatchFlags.caseInsensitive, MatchType.contains),
                (u"fullNames", u"test", MatchFlags.caseInsensitive, MatchType.contains),
            )
            records = (yield self.calService.recordsMatchingFields(
                fields, operand=Operand.AND, recordType=self.calService.recordType.user
            ))
            self.verifyResults(
                records,
                [
                    "odtestbetty", "odtestalbert", "anotherodtestalbert"
                ],
                [
                    "odtestamanda", "odtestgroupalbert", "odtestgroupbetty",
                ]
            )
