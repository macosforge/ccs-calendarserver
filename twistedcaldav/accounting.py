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

"""
Extended account-specific logging.
Allows different sub-systems to log data on a per-principal basis.
"""

__all__ = [
    "accountingEnabled",
    "emitAccounting",
]

import datetime
import os

from twext.python.log import Logger

from twistedcaldav.config import config

log = Logger()


def accountingEnabled(category, record):
    """
    Determine if accounting is enabled for the given category and record.
    """
    return (
        accountingEnabledForCategory(category) and
        accountingEnabledForRecord(record)
    )


def accountingEnabledForCategory(category):
    """
    Determine if accounting is enabled for the given category.
    """
    AccountingCategories = getattr(config, "AccountingCategories", None)
    if AccountingCategories is None:
        return False
    return AccountingCategories.get(category, False)


def accountingEnabledForRecord(record):
    """
    Determine if accounting is enabled for the given record.
    """
    enabledRecordGUIDs = config.AccountingPrincipals

    if "*" in enabledRecordGUIDs:
        return True

    return record.uid in enabledRecordGUIDs


def emitAccounting(category, record, data, tag=None, filename=None):
    """
    Write the supplied data to the appropriate location for the given
    category and record.

    @param record: the record for whom a log entry is to be created.
    @type record: L{DirectoryRecord}
    @param category: accounting category
    @type category: C{tuple}
    @param data: data to write.
    @type data: C{str}
    """
    if isinstance(record, str):
        principalLogPath = record
    elif accountingEnabled(category, record):
        ruid = record.uid.encode("utf-8")
        principalLogPath = os.path.join(
            ruid[0:2],
            ruid[2:4],
            ruid
        )
    else:
        return None

    try:
        #
        # Obtain the accounting log file name
        #
        logRoot = config.AccountingLogRoot
        logDirectory = category
        if principalLogPath:
            logDirectory = os.path.join(
                logDirectory,
                principalLogPath,
            )
        logFilename = os.path.join(
            logDirectory,
            datetime.datetime.now().isoformat() if filename is None else filename
        )

        if not os.path.isdir(os.path.join(logRoot, logDirectory)):
            os.makedirs(os.path.join(logRoot, logDirectory))
            if filename is None:
                logFilename = "%s-01" % (logFilename,)
                if tag:
                    logFilename += " (%s)" % (tag,)
                logFilename += ".txt"
        else:
            if filename is None:
                index = 1
                while True:
                    path = "%s-%02d" % (logFilename, index)
                    if tag:
                        path += " (%s)" % (tag,)
                    path += ".txt"
                    if not os.path.isfile(os.path.join(logRoot, path)):
                        logFilename = path
                        break
                    if index == 1000:
                        log.error("Too many {cat} accounting files for {rec}", cat=category, rec=record)
                        return None
                    index += 1

        #
        # Now write out the data to the log file
        #
        with open(os.path.join(logRoot, logFilename), "a") as logFile:
            logFile.write(data)

        return logFilename

    except OSError as e:
        # No failures in accounting should propagate out
        log.error("Failed to write accounting data due to: {ex}", ex=str(e))
        return None
