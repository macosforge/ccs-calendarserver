# -*- test-case-name: txdav.common.datastore.upgrade.sql.test -*-
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

"""
Utilities, mostly related to upgrading, common to calendar and addressbook
data stores.
"""

import re

from twext.enterprise.dal.parseschema import schemaFromPath
from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.failure import Failure
from twisted.python.modules import getModule
from twisted.python.reflect import namedObject

from twistedcaldav.config import config

from txdav.common.datastore.upgrade.sql.others import attachment_migration


class UpgradeAcquireLockStep(object):
    """
    A Step which acquires the upgrade lock, blocking later Steps until it's
    been acquired.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore):
        self.sqlStore = sqlStore

    @inlineCallbacks
    def stepWithResult(self, result):
        sqlTxn = self.sqlStore.newTransaction(label="UpgradeAcquireLockStep.stepWithResult")
        yield sqlTxn.acquireUpgradeLock()
        yield sqlTxn.commit()


class UpgradeReleaseLockStep(object):
    """
    A Step which releases the upgrade lock.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore):
        self.sqlStore = sqlStore

    @inlineCallbacks
    def stepWithResult(self, result):
        sqlTxn = self.sqlStore.newTransaction(label="UpgradeReleaseLockStep.stepWithResult")
        yield sqlTxn.releaseUpgradeLock()
        yield sqlTxn.commit()


class NotAllowedToUpgrade(Exception):
    """
    Exception indicating an upgrade is needed but we're not configured to
    perform it.
    """


class UpgradeDatabaseCoreStep(object):
    """
    Base class for either schema or data upgrades on the database.

    upgrade files in sql syntax that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """
    log = Logger()

    def __init__(self, sqlStore, uid=None, gid=None, failIfUpgradeNeeded=False, checkExistingSchema=False):
        """
        Initialize the service.
        """
        self.sqlStore = sqlStore
        self.uid = uid
        self.gid = gid
        self.failIfUpgradeNeeded = failIfUpgradeNeeded
        self.checkExistingSchema = checkExistingSchema
        self.schemaLocation = getModule(__name__).filePath.parent().parent().sibling("sql_schema")
        self.pyLocation = getModule(__name__).filePath.parent()

        self.versionKey = None
        self.versionDescriptor = ""
        self.upgradeFilePrefix = ""
        self.upgradeFileSuffix = ""
        self.defaultKeyValue = None

    def stepWithResult(self, result):
        """
        Start the service.
        """
        return self.databaseUpgrade()

    @inlineCallbacks
    def databaseUpgrade(self):
        """
        Do a database schema upgrade.
        """
        self.log.warn("Beginning database {vers} check.", vers=self.versionDescriptor)

        # Retrieve information from schema and database
        dialect, required_version, actual_version = yield self.getVersions()

        if required_version == actual_version:
            self.log.warn("{vers} version check complete: no upgrade needed.", vers=self.versionDescriptor.capitalize())
            if self.checkExistingSchema:
                if dialect == "postgres-dialect":
                    expected_schema = self.schemaLocation.child("current.sql")
                    schema_name = "public"
                else:
                    expected_schema = self.schemaLocation.child("current-oracle-dialect.sql")
                    schema_name = config.DatabaseConnection.user
                yield self.sqlStore.checkSchema(schemaFromPath(expected_schema), schema_name)
        elif required_version < actual_version:
            msg = "Actual %s version %s is more recent than the expected version %s. The service cannot be started" % (
                self.versionDescriptor, actual_version, required_version,
            )
            self.log.error(msg)
            raise RuntimeError(msg)
        elif self.failIfUpgradeNeeded:
            if self.checkExistingSchema:
                expected_schema = self.schemaLocation.child("old").child(dialect).child("v{}.sql".format(actual_version))
                if dialect == "postgres-dialect":
                    schema_name = "public"
                else:
                    schema_name = config.DatabaseConnection.user
                yield self.sqlStore.checkSchema(schemaFromPath(expected_schema), schema_name)
            raise NotAllowedToUpgrade()
        else:
            self.sqlStore.setUpgrading(True)
            yield self.upgradeVersion(actual_version, required_version, dialect)
            self.sqlStore.setUpgrading(False)

        self.log.warn("Database {vers} check complete.", vers=self.versionDescriptor)

        returnValue(None)

    @inlineCallbacks
    def getVersions(self):
        """
        Extract the expected version from the database schema and get the actual version in the current
        database, along with the DB dialect.
        """

        # Retrieve the version number from the schema file
        current_schema = self.schemaLocation.child("current.sql").getContent()
        found = re.search("insert into CALENDARSERVER values \('%s', '(\d+)'\);" % (self.versionKey,), current_schema)
        if found is None:
            msg = "Schema is missing required database key %s insert statement: %s" % (self.versionKey, current_schema,)
            self.log.error(msg)
            raise RuntimeError(msg)
        else:
            required_version = int(found.group(1))
            self.log.warn("Required database key {key}: {vers}.", key=self.versionKey, vers=required_version)

        # Get the schema version in the current database
        sqlTxn = self.sqlStore.newTransaction(label="UpgradeDatabaseCoreStep.getVersions")
        dialect = sqlTxn.dbtype.dialect
        try:
            actual_version = yield sqlTxn.calendarserverValue(self.versionKey)
            actual_version = int(actual_version)
            yield sqlTxn.commit()
        except (RuntimeError, ValueError):
            f = Failure()
            self.log.error("Database key {key} cannot be determined.", key=self.versionKey)
            yield sqlTxn.abort()
            if self.defaultKeyValue is None:
                f.raiseException()
            else:
                actual_version = self.defaultKeyValue

        self.log.warn("Actual database key {key}: {vers}.", key=self.versionKey, vers=actual_version)

        returnValue((dialect, required_version, actual_version,))

    @inlineCallbacks
    def upgradeVersion(self, fromVersion, toVersion, dialect):
        """
        Update the database from one version to another (the current one). Do this by
        looking for upgrade_from_X_to_Y.sql files that cover the full range of upgrades.
        """

        self.log.warn("Starting {vers} upgrade from version {fr} to {to}.", vers=self.versionDescriptor, fr=fromVersion, to=toVersion)

        # Scan for all possible upgrade files - returned sorted
        files = self.scanForUpgradeFiles(dialect)

        # Determine upgrade sequence and run each upgrade
        upgrades = self.determineUpgradeSequence(fromVersion, toVersion, files, dialect)

        # Use one transaction for the entire set of upgrades
        try:
            for fp in upgrades:
                yield self.applyUpgrade(fp)
        except RuntimeError:
            self.log.error("Database {vers} upgrade failed using: {path}", vers=self.versionDescriptor, path=fp.basename())
            raise

        self.log.warn("{vers} upgraded from version {fr} to {to}.", vers=self.versionDescriptor.capitalize(), fr=fromVersion, to=toVersion)

    def getPathToUpgrades(self, dialect):
        """
        Return the path where appropriate upgrade files can be found.
        """
        raise NotImplementedError

    def scanForUpgradeFiles(self, dialect):
        """
        Scan for upgrade files with the require name.
        """

        fp = self.getPathToUpgrades(dialect)
        upgrades = []
        regex = re.compile("%supgrade_from_(\d+)_to_(\d+)%s" % (self.upgradeFilePrefix, self.upgradeFileSuffix,))
        for child in fp.globChildren("%supgrade_*%s" % (self.upgradeFilePrefix, self.upgradeFileSuffix,)):
            matched = regex.match(child.basename())
            if matched is not None:
                fromV = int(matched.group(1))
                toV = int(matched.group(2))
                upgrades.append((fromV, toV, child))

        upgrades.sort(key=lambda x: (x[0], x[1]))
        return upgrades

    def determineUpgradeSequence(self, fromVersion, toVersion, files, dialect):
        """
        Determine the upgrade_from_X_to_Y(.sql|.py) files that cover the full range of upgrades.
        Note that X and Y may not be consecutive, e.g., we might have an upgrade from 3 to 4,
        4 to 5, and 3 to 5 - the later because it is more efficient to jump over the intermediate
        step. As a result we will always try and pick the upgrade file that gives the biggest
        jump from one version to another at each step.
        """

        # Now find the path from the old version to the current one
        filesByFromVersion = {}
        for fromV, toV, fp in files:
            if fromV not in filesByFromVersion or filesByFromVersion[fromV][1] < toV:
                filesByFromVersion[fromV] = fromV, toV, fp

        upgrades = []
        nextVersion = fromVersion
        while nextVersion != toVersion:
            if nextVersion not in filesByFromVersion:
                msg = "Missing upgrade file from version %d with dialect %s" % (nextVersion, dialect,)
                self.log.error(msg)
                raise RuntimeError(msg)
            else:
                upgrades.append(filesByFromVersion[nextVersion][2])
                nextVersion = filesByFromVersion[nextVersion][1]

        return upgrades

    def applyUpgrade(self, fp):
        """
        Apply the supplied upgrade to the database. Always return an L{Deferred"
        """
        raise NotImplementedError


class UpgradeDatabaseSchemaStep(UpgradeDatabaseCoreStep):
    """
    Checks and upgrades the database schema. This assumes there are a bunch of
    upgrade files in sql syntax that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore, **kwargs):
        """
        Initialize the service.

        @param sqlStore: The store to operate on. Can be C{None} when doing unit tests.
        """
        super(UpgradeDatabaseSchemaStep, self).__init__(sqlStore, **kwargs)

        self.versionKey = "VERSION"
        self.versionDescriptor = "schema"
        self.upgradeFileSuffix = ".sql"

    def getPathToUpgrades(self, dialect):
        return self.schemaLocation.child("upgrades").child(dialect)

    @inlineCallbacks
    def applyUpgrade(self, fp):
        """
        Apply the schema upgrade .sql file to the database.
        """
        self.log.warn("Applying schema upgrade: {path}", path=fp.basename())
        sqlTxn = self.sqlStore.newTransaction(label="UpgradeDatabaseSchemaStep.applyUpgrade")
        try:
            sql = fp.getContent()
            yield sqlTxn.execSQLBlock(sql)
            yield sqlTxn.commit()
        except (RuntimeError, Exception) as e:
            if hasattr(e, "stmt"):
                self.log.error("Apply upgrade failed for '{basename}' on statement: {stmt}\n{err}".format(
                    basename=fp.basename(),
                    stmt=e.stmt,
                    err=e,
                ))
            f = Failure()
            yield sqlTxn.abort()
            f.raiseException()


class _UpgradeDatabaseDataStep(UpgradeDatabaseCoreStep):
    """
    Checks and upgrades the database data. This assumes there are a bunch of
    upgrade python modules that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def getPathToUpgrades(self, dialect):
        return self.pyLocation.child("upgrades")

    @inlineCallbacks
    def applyUpgrade(self, fp):
        """
        Apply the data upgrade .py files to the database.
        """

        # Find the module function we need to execute
        try:
            module = getModule(__name__)
            module = ".".join(module.name.split(".")[:-1]) + ".upgrades." + fp.basename()[:-3] + ".doUpgrade"
            doUpgrade = namedObject(module)
        except ImportError:
            msg = "Failed data upgrade: %s" % (fp.basename()[:-4],)
            self.log.error(msg)
            raise RuntimeError(msg)

        self.log.warn("Applying data upgrade: {module}", module=module)
        yield doUpgrade(self.sqlStore)


class UpgradeDatabaseAddressBookDataStep(_UpgradeDatabaseDataStep):
    """
    Checks and upgrades the database data. This assumes there are a bunch of
    upgrade python modules that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore, **kwargs):
        """
        Initialize the Step.

        @param sqlStore: The store to operate on. Can be C{None} when doing unit tests.
        """
        super(UpgradeDatabaseAddressBookDataStep, self).__init__(sqlStore, **kwargs)

        self.versionKey = "ADDRESSBOOK-DATAVERSION"
        self.versionDescriptor = "addressbook data"
        self.upgradeFilePrefix = "addressbook_"
        self.upgradeFileSuffix = ".py"


class UpgradeDatabaseCalendarDataStep(_UpgradeDatabaseDataStep):
    """
    Checks and upgrades the database data. This assumes there are a bunch of
    upgrade python modules that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore, **kwargs):
        """
        Initialize the service.

        @param sqlStore: The store to operate on. Can be C{None} when doing unit tests.
        @param service:  Wrapped service. Can be C{None} when doing unit tests.
        """
        super(UpgradeDatabaseCalendarDataStep, self).__init__(sqlStore, **kwargs)

        self.versionKey = "CALENDAR-DATAVERSION"
        self.versionDescriptor = "calendar data"
        self.upgradeFilePrefix = "calendar_"
        self.upgradeFileSuffix = ".py"


class UpgradeDatabaseNotificationDataStep(_UpgradeDatabaseDataStep):
    """
    Checks and upgrades the database data. This assumes there are a bunch of
    upgrade python modules that we can execute against the database to
    accomplish the upgrade.

    @ivar sqlStore: The store to operate on.

    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore, **kwargs):
        """
        Initialize the service.

        @param sqlStore: The store to operate on. Can be C{None} when doing unit tests.
        @param service:  Wrapped service. Can be C{None} when doing unit tests.
        """
        super(UpgradeDatabaseNotificationDataStep, self).__init__(sqlStore, **kwargs)

        self.versionKey = "NOTIFICATION-DATAVERSION"
        self.versionDescriptor = "notification data"
        self.upgradeFilePrefix = "notification_"
        self.upgradeFileSuffix = ".py"
        self.defaultKeyValue = 0


class UpgradeDatabaseOtherStep(UpgradeDatabaseCoreStep):
    """
    Do any other upgrade behaviors once all the schema, data, file migration upgraders
    are done.

    @ivar sqlStore: The store to operate on.
    @type sqlStore: L{txdav.idav.IDataStore}
    """

    def __init__(self, sqlStore, **kwargs):
        """
        Initialize the Step.

        @param sqlStore: The store to operate on. Can be C{None} when doing unit tests.
        """
        super(UpgradeDatabaseOtherStep, self).__init__(sqlStore, **kwargs)

        self.versionDescriptor = "other upgrades"

    @inlineCallbacks
    def databaseUpgrade(self):
        """
        Do upgrades.
        """
        self.log.warn("Beginning database {vers} check.", vers=self.versionDescriptor)

        # Do each upgrade in our own predefined order
        self.sqlStore.setUpgrading(True)

        # Migration from dropbox to managed attachments
        yield attachment_migration.doUpgrade(self)

        self.sqlStore.setUpgrading(False)

        self.log.warn("Database {vers} check complete.", vers=self.versionDescriptor)

        returnValue(None)
