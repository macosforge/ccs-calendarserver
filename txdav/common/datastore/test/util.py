# -*- test-case-name: txdav.carddav.datastore.test -*-
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
Store test utility functions
"""

from __future__ import print_function

import os

# FIXME: Don't import from calendarserver in txdav
from calendarserver.push.notifier import Notifier

from hashlib import md5

from pycalendar.datetime import DateTime

from random import Random, randint

from twext.python.log import Logger
from twext.python.filepath import CachingFilePath as FilePath
from twext.enterprise.adbapi2 import ConnectionPool
from twext.enterprise.ienterprise import AlreadyFinishedError, POSTGRES_DIALECT, \
    ORACLE_DIALECT, DatabaseType
from twext.enterprise.jobs.jobitem import JobItem
from twext.enterprise.jobs.queue import ControllerQueue
from twext.who.directory import DirectoryRecord

from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.internet.defer import returnValue
from twisted.internet.task import deferLater
from twisted.trial.unittest import TestCase

from twistedcaldav import ical
from twistedcaldav.config import config, ConfigDict
from twistedcaldav.ical import Component as VComponent
from twistedcaldav.stdconfig import DEFAULT_CONFIG
from twistedcaldav.vcard import Component as ABComponent

from txdav.base.datastore.dbapiclient import DiagnosticConnectionWrapper
from txdav.base.datastore.subpostgres import PostgresService
from txdav.base.datastore.suboracle import OracleService
from txdav.base.propertystore.base import PropertyName
from txdav.caldav.icalendarstore import ComponentUpdateState
from txdav.common.datastore.sql import CommonDataStore, current_sql_schema
from txdav.common.datastore.sql_tables import schema
from txdav.common.icommondatastore import NoSuchHomeChildError
from txdav.who.util import buildDirectory

from txweb2.dav.resource import TwistedGETContentMD5

from zope.interface.exceptions import BrokenMethodImplementation, \
    DoesNotImplement
from zope.interface.verify import verifyObject

import gc


DB_TYPE = (POSTGRES_DIALECT, "pyformat", "current.sql",)
#DB_TYPE = (ORACLE_DIALECT, "numeric", "current-oracle-dialect.sql",)

log = Logger()

md5key = PropertyName.fromElement(TwistedGETContentMD5)


def allInstancesOf(cls):
    """
    Use L{gc.get_referrers} to retrieve all instances of a given class.
    """
    for o in gc.get_referrers(cls):
        if isinstance(o, cls):
            yield o


def dumpConnectionStatus():
    """
    Dump all L{DiagnosticConnectionWrapper} objects to standard output.  This
    function is useful for diagnosing connection leaks that corrupt state
    between tests.  (It is currently not invoked anywhere, but may be useful if
    these types of bugs crop up in the future.)
    """
    print("+++ ALL CONNECTIONS +++")
    for connection in allInstancesOf(DiagnosticConnectionWrapper):
        print(connection.label, connection.state)
    print("--- CONNECTIONS END ---")


class SQLStoreBuilder(object):
    """
    Test-fixture-builder which can construct a PostgresStore.
    """

    def __init__(self, count=0, **options):
        self.sharedService = None
        self.currentTestID = None

        self.sharedDBPath = "_test_sql_db-{}-{}".format(
            os.getpid(), count
        )

        self.options = options

    def createService(self, serviceFactory):
        """
        Create a L{PostgresService} to use for building a store.
        """
        dbRoot = FilePath(self.sharedDBPath)
        if DB_TYPE[0] == POSTGRES_DIALECT:
            return PostgresService(
                dbRoot, serviceFactory, current_sql_schema, resetSchema=True,
                databaseName="caldav",
                options=[
                    "-c log_lock_waits=TRUE",
                    "-c log_statement=all",
                    "-c log_line_prefix='%p.%x '",
                    "-c fsync=FALSE",
                    "-c synchronous_commit=off",
                    "-c full_page_writes=FALSE",
                    "-c client-min-messages=warning",
                ],
                testMode=True
            )
        elif DB_TYPE[0] == ORACLE_DIALECT:
            return OracleService(
                dbRoot, serviceFactory,
                testMode=True,
                dsnUser=self.options.get("dsnUser"),
            )

    def childStore(self):
        """
        Create a store suitable for use in a child process, that is hooked up
        to the store that a parent test process is managing.
        """
        disableMemcacheForTest(TestCase())
        staticQuota = 3000
        attachmentRoot = (FilePath(self.sharedDBPath).child("attachments"))
        stubsvc = self.createService(lambda cf: Service())

        cp = ConnectionPool(
            stubsvc.produceConnection,
            maxConnections=1,
            dbtype=DatabaseType(DB_TYPE[0], DB_TYPE[1]),
        )
        # Attach the service to the running reactor.
        cp.startService()
        reactor.addSystemEventTrigger("before", "shutdown", cp.stopService)
        cds = CommonDataStore(
            cp.connection,
            {"push": StubNotifierFactory(), },
            None,
            attachmentRoot, "",
            quota=staticQuota
        )
        return cds

    def buildStore(
        self, testCase, notifierFactory,
        directoryService=None, homes=None, enableJobProcessing=True,
    ):
        """
        Do the necessary work to build a store for a particular test case.

        @return: a L{Deferred} which fires with an L{IDataStore}.
        """
        disableMemcacheForTest(testCase)
        dbRoot = FilePath(self.sharedDBPath)
        attachmentRoot = dbRoot.child("attachments")
        # The directory will be given to us later via setDirectoryService
        if self.sharedService is None:
            ready = Deferred()

            def getReady(connectionFactory, storageService):
                self.makeAndCleanStore(
                    testCase, notifierFactory, directoryService,
                    attachmentRoot, enableJobProcessing
                ).chainDeferred(ready)
                return Service()

            self.sharedService = self.createService(getReady)
            self.sharedService.startService()

            def startStopping():
                log.info("Starting stopping.")
                self.sharedService.unpauseMonitor()
                return self.sharedService.stopService()

            reactor.addSystemEventTrigger(
                "before", "shutdown", startStopping)
            result = ready
        else:
            result = self.makeAndCleanStore(
                testCase, notifierFactory, directoryService,
                attachmentRoot, enableJobProcessing
            )

        def cleanUp():
            def stopit():
                self.sharedService.pauseMonitor()
            return deferLater(reactor, 0.1, stopit)

        testCase.addCleanup(cleanUp)
        return result

    @inlineCallbacks
    def makeAndCleanStore(
        self, testCase, notifierFactory, directoryService,
        attachmentRoot, enableJobProcessing=True
    ):
        """
        Create a L{CommonDataStore} specific to the given L{TestCase}.

        This also creates a L{ConnectionPool} that gets stopped when the test
        finishes, to make sure that any test which fails will terminate
        cleanly.

        @return: a L{Deferred} that fires with a L{CommonDataStore}
        """

        # Always clean-out old attachments
        if attachmentRoot.exists():
            attachmentRoot.remove()
        attachmentRoot.createDirectory()

        currentTestID = testCase.id()
        cp = ConnectionPool(
            self.sharedService.produceConnection,
            maxConnections=4,
            dbtype=DatabaseType(DB_TYPE[0], DB_TYPE[1]),
        )
        quota = deriveQuota(testCase)
        store = CommonDataStore(
            cp.connection,
            {"push": notifierFactory} if notifierFactory is not None else {},
            directoryService,
            attachmentRoot,
            "https://example.com/calendars/__uids__/%(home)s/dropbox/%(dropbox_id)s/%(name)s",
            quota=quota
        )
        store.label = currentTestID
        cp.startService()

        @inlineCallbacks
        def stopIt():

            if enableJobProcessing:
                txn = store.newTransaction()
                jobs = yield JobItem.all(txn)
                yield txn.commit()
                yield pool.stopService()
            else:
                jobs = ()

            # active transactions should have been shut down.
            wasBusy = len(cp._busy)
            busyText = repr(cp._busy)
            result = yield cp.stopService()

            if deriveValue(testCase, _SPECIAL_TXN_CLEAN, lambda tc: False):
                if wasBusy:
                    testCase.fail("Outstanding Transactions: " + busyText)
                returnValue(result)

            if len(jobs):
                testCase.fail("Jobs left in job queue {}: {}".format(
                    testCase,
                    ",".join([job.workType for job in jobs])
                ))

            returnValue(result)

        testCase.addCleanup(stopIt)
        yield self.cleanStore(testCase, store)

        # Start the job queue after store is up and cleaned
        if enableJobProcessing:
            pool = ControllerQueue(
                reactor, store.newTransaction, useWorkerPool=False
            )
            store.queuer = store.pool = pool
            pool.startService()

        returnValue(store)

    @inlineCallbacks
    def cleanStore(self, testCase, storeToClean):

        if "noCleanup" in self.options:
            returnValue(None)

        cleanupTxn = storeToClean.sqlTxnFactory(
            "%s schema-cleanup" % (testCase.id(),)
        )

        # Tables are defined in the schema in the order in which the 'create
        # table' statements are issued, so it's not possible to reference a
        # later table.  Therefore it's OK to drop them in the (reverse) order
        # that they happen to be in.
        tables = [
            t.name for t in schema.model.tables  # @UndefinedVariable
            # All tables with rows _in_ the schema are populated
            # exclusively _by_ the schema and shouldn't be manipulated
            # while the server is running, so we leave those populated.
            if not t.schemaRows
        ][::-1]

        for table in tables:
            try:
                yield cleanupTxn.execSQL("delete from " + table, [])
            except:
                log.failure("delete table {table} failed", table=table)

        # Change the starting values of sequences to random values
        for sequence in schema.model.sequences:  # @UndefinedVariable
            try:
                if cleanupTxn.dbtype.dialect == POSTGRES_DIALECT:
                    curval = (yield cleanupTxn.execSQL("select nextval('{}')".format(sequence.name), []))[0][0]
                    yield cleanupTxn.execSQL("select setval('{}', {})".format(sequence.name, curval + randint(1, 10000)), [])
                elif cleanupTxn.dbtype.dialect == ORACLE_DIALECT:
                    yield cleanupTxn.execSQL("alter sequence {} increment by {}".format(sequence.name, randint(1, 10000)), [])
                    yield cleanupTxn.execSQL("select {}.nextval from dual".format(sequence.name), [])
                    yield cleanupTxn.execSQL("alter sequence {} increment by {}".format(sequence.name, 1), [])
            except:
                log.failure("setval sequence '{sequence}' failed", sequence=sequence.name)
        yield cleanupTxn.execSQL("update CALENDARSERVER set VALUE = '1' where NAME = 'MIN-VALID-REVISION'", [])

        yield cleanupTxn.commit()

        # Deal with memcached items that must be cleared
        storeToClean.queryCacher.flushAll()
        from txdav.base.propertystore.sql import PropertyStore
        PropertyStore._cacher.flushAll()

theStoreBuilder = SQLStoreBuilder()
buildStore = theStoreBuilder.buildStore
cleanStore = theStoreBuilder.cleanStore


_notSet = object()


def deriveValue(testCase, attribute, computeDefault):
    """
    Derive a value for a specific test method, defined by L{withSpecialValue}
    for that test.

    @param testCase: the test case instance.
    @type testCase: L{TestCase}

    @param attribute: the name of the attribute (the same name passed to
        L{withSpecialValue}).
    @type attribute: L{str}

    @param computeDefault: A 1-argument callable, which will be called with
        C{testCase} to compute a default value for the attribute for the given
        test if no custom one was specified.
    @type computeDefault: L{callable}

    @return: the value of the given C{attribute} for the given C{testCase}, as
        decorated with C{withSpecialValue}.
    @rtype: same type as the return type of L{computeDefault}
    """
    testID = testCase.id()
    testMethodName = testID.split(".")[-1]
    method = getattr(testCase, testMethodName)
    value = getattr(method, attribute, _notSet)
    if value is _notSet:
        return computeDefault(testCase)
    else:
        return value


def withSpecialValue(attribute, value):
    """
    Decorator for a test method which has a special value.  Should be used by
    tests which use L{deriveValue} in their C{setUp} method.
    """
    def thunk(function):
        setattr(function, attribute, value)
        return function
    return thunk


def _computeDefaultQuota(testCase):
    """
    Compute a default value for quota in tests.
    """
    h = md5(testCase.id())
    seed = int(h.hexdigest(), 16)
    r = Random(seed)
    baseline = 2000
    fuzz = r.randint(1, 1000)
    return baseline + fuzz


_SPECIAL_QUOTA = "__special_quota__"
_SPECIAL_TXN_CLEAN = "__special_txn_clean__"


def deriveQuota(testCase):
    """
    Derive a distinctive quota number for a specific test, based on its ID.
    This generates a quota which is small enough that tests may trivially
    exceed it if they wish to do so, but distinctive enough that it may be
    compared without the risk of testing only a single value for quota.

    Since SQL stores are generally built during test construction, it's awkward
    to have tests which specifically construct a store to inspect quota-related
    state; this allows us to have the test and the infrastructure agree on a
    number.

    @see: deriveValue

    @param testCase: The test case which may have a special quota value
        assigned.
    @type testCase: L{TestCase}

    @return: the number of quota bytes to use for C{testCase}
    @rtype: C{int}
    """
    return deriveValue(testCase, _SPECIAL_QUOTA, _computeDefaultQuota)


def withSpecialQuota(quotaValue):
    """
    Test method decorator that will cause L{deriveQuota} to return a different
    value for test cases that run that test method.

    @see: L{withSpecialValue}
    """
    return withSpecialValue(_SPECIAL_QUOTA, quotaValue)


def transactionClean(f=None):
    """
    Test method decorator that will cause L{buildStore} to check that no
    transactions were left outstanding at the end of the test, and fail the
    test if they are outstanding rather than terminating them by shutting down
    the connection pool service.

    @see: L{withSpecialValue}
    """
    decorator = withSpecialValue(_SPECIAL_TXN_CLEAN, True)
    if f:
        return decorator(f)
    else:
        return decorator


@inlineCallbacks
def populateCalendarsFrom(requirements, store, migrating=False):
    """
    Populate C{store} from C{requirements}.

    @param requirements: a dictionary of the format described by
        L{txdav.caldav.datastore.test.common.CommonTests.requirements}.

    @param store: the L{IDataStore} to populate with calendar data.
    """
    populateTxn = store.newTransaction()
    if migrating:
        populateTxn._migrating = True
    for homeUID in requirements:
        calendars = requirements[homeUID]
        home = yield populateTxn.calendarHomeWithUID(homeUID, create=True)
        if calendars is not None:
            # We don't want the default calendar or inbox to appear unless it's
            # explicitly listed.
            try:
                if config.RestrictCalendarsToOneComponentType:
                    for name in ical.allowedStoreComponents:
                        yield home.removeCalendarWithName(
                            home._componentCalendarName[name],
                            useTrash=False
                        )
                else:
                    yield home.removeCalendarWithName("calendar", useTrash=False)
                yield home.removeCalendarWithName("inbox", useTrash=False)
            except NoSuchHomeChildError:
                pass

            for calendarName in calendars:
                calendarObjNames = calendars[calendarName]

                if calendarObjNames is not None:
                    # XXX should not be yielding!  this SQL will be executed
                    # first!
                    yield home.createCalendarWithName(calendarName)
                    calendar = yield home.calendarWithName(calendarName)

                    for objectName in calendarObjNames:
                        objData, metadata = calendarObjNames[objectName]

                        yield calendar._createCalendarObjectWithNameInternal(
                            objectName,
                            VComponent.fromString(
                                updateToCurrentYear(objData)
                            ),
                            internal_state=ComponentUpdateState.RAW,
                            options=metadata,
                        )

    yield populateTxn.commit()


def updateToCurrentYear(data):
    """
    Update the supplied iCalendar data so that all dates are updated to the
    current year.
    """

    subs = {}
    nowYear = DateTime.getToday().getYear()
    subs["now"] = nowYear
    for i in range(1, 10):
        subs["now-{}".format(i)] = nowYear - 1
        subs["now+{}".format(i)] = nowYear + 1
    return data % subs


@inlineCallbacks
def resetCalendarMD5s(md5s, store):
    """
    Change MD5s in C{store} from C{requirements}.

    @param requirements: a dictionary of the format described by
        L{txdav.caldav.datastore.test.common.CommonTests.requirements}.

    @param store: the L{IDataStore} to populate with calendar data.
    """
    populateTxn = store.newTransaction()
    for homeUID in md5s:
        calendars = md5s[homeUID]
        if calendars is not None:
            home = yield populateTxn.calendarHomeWithUID(homeUID, create=True)
            for calendarName in calendars:
                calendarObjNames = calendars[calendarName]
                if calendarObjNames is not None:
                    # XXX should not be yielding!  this SQL will be executed
                    # first!
                    calendar = yield home.calendarWithName(calendarName)
                    for objectName in calendarObjNames:
                        md5 = calendarObjNames[objectName]
                        obj = (
                            yield calendar.calendarObjectWithName(objectName)
                        )
                        obj.properties()[md5key] = (
                            TwistedGETContentMD5.fromString(md5)
                        )
    yield populateTxn.commit()


@inlineCallbacks
def populateAddressBooksFrom(requirements, store):
    """
    Populate C{store} from C{requirements}.

    @param requirements: a dictionary of the format described by
        L{txdav.caldav.datastore.test.common.CommonTests.requirements}.

    @param store: the L{IDataStore} to populate with addressbook data.
    """
    populateTxn = store.newTransaction()
    for homeUID in requirements:
        addressbooks = requirements[homeUID]
        if addressbooks is not None:
            home = yield populateTxn.addressbookHomeWithUID(homeUID, create=True)
            # We don't want the default addressbook
            try:
                yield home.removeAddressBookWithName("addressbook")
            except NoSuchHomeChildError:
                pass
            for addressbookName in addressbooks:
                addressbookObjNames = addressbooks[addressbookName]
                if addressbookObjNames is not None:
                    # XXX should not be yielding!  this SQL will be executed
                    # first!
                    yield home.createAddressBookWithName(addressbookName)
                    addressbook = (
                        yield home.addressbookWithName(addressbookName)
                    )
                    for objectName in addressbookObjNames:
                        objData = addressbookObjNames[objectName]
                        yield addressbook.createAddressBookObjectWithName(
                            objectName,
                            ABComponent.fromString(objData),
                        )
    yield populateTxn.commit()


@inlineCallbacks
def resetAddressBookMD5s(md5s, store):
    """
    Change MD5s in C{store} from C{requirements}.

    @param requirements: a dictionary of the format described by
        L{txdav.caldav.datastore.test.common.CommonTests.requirements}.

    @param store: the L{IDataStore} to populate with addressbook data.
    """
    populateTxn = store.newTransaction()
    for homeUID in md5s:
        addressbooks = md5s[homeUID]
        if addressbooks is not None:
            home = yield populateTxn.addressbookHomeWithUID(homeUID, create=True)
            for addressbookName in addressbooks:
                addressbookObjNames = addressbooks[addressbookName]
                if addressbookObjNames is not None:
                    # XXX should not be yielding!  this SQL will be executed
                    # first!
                    addressbook = (
                        yield home.addressbookWithName(addressbookName)
                    )
                    for objectName in addressbookObjNames:
                        md5 = addressbookObjNames[objectName]
                        obj = yield addressbook.addressbookObjectWithName(
                            objectName,
                        )
                        obj.properties()[md5key] = (
                            TwistedGETContentMD5.fromString(md5)
                        )
    yield populateTxn.commit()


def assertProvides(testCase, interface, provider):
    """
    Verify that C{provider} properly provides C{interface}

    @type interface: L{zope.interface.Interface}
    @type provider: C{provider}
    """
    try:
        verifyObject(interface, provider)
    except BrokenMethodImplementation as e:
        testCase.fail(e)
    except DoesNotImplement as e:
        testCase.fail("%r does not provide %s.%s" %
                      (provider, interface.__module__, interface.getName()))


def buildTestDirectory(
    store, dataRoot, accounts=None, resources=None, augments=None,
    proxies=None, serversDB=None, cacheSeconds=0, wikiEnabled=True
):
    """
    @param store: the store for the directory to use

    @param dataRoot: the directory to copy xml files to

    @param accounts: path to the accounts.xml file
    @type accounts: L{FilePath}

    @param resources: path to the resources.xml file
    @type resources: L{FilePath}

    @param augments: path to the augments.xml file
    @type augments: L{FilePath}

    @param proxies: path to the proxies.xml file
    @type proxies: L{FilePath}

    @return: the directory service
    @rtype: L{IDirectoryService}
    """

    defaultDirectory = FilePath(__file__).sibling("accounts")
    if accounts is None:
        accounts = defaultDirectory.child("accounts.xml")
    if resources is None:
        resources = defaultDirectory.child("resources.xml")
    if augments is None:
        augments = defaultDirectory.child("augments.xml")
    if proxies is None:
        proxies = defaultDirectory.child("proxies.xml")

    if not os.path.exists(dataRoot):
        os.makedirs(dataRoot)

    accountsCopy = FilePath(dataRoot).child("accounts.xml")
    accountsCopy.setContent(accounts.getContent())

    resourcesCopy = FilePath(dataRoot).child("resources.xml")
    resourcesCopy.setContent(resources.getContent())

    augmentsCopy = FilePath(dataRoot).child("augments.xml")
    augmentsCopy.setContent(augments.getContent())

    proxiesCopy = FilePath(dataRoot).child("proxies.xml")
    proxiesCopy.setContent(proxies.getContent())

    servicesInfo = (
        ConfigDict(
            {
                "Enabled": True,
                "type": "xml",
                "params": {
                    "xmlFile": "accounts.xml",
                    "recordTypes": ("users", "groups"),
                },
            }
        ),
        ConfigDict(
            {
                "Enabled": True,
                "type": "xml",
                "params": {
                    "xmlFile": "resources.xml",
                    "recordTypes": ("locations", "resources", "addresses"),
                },
            }
        ),
    )
    augmentServiceInfo = ConfigDict(
        {
            "Enabled": True,
            "type": "xml",
            "params": {
                "xmlFiles": ["augments.xml", ],
                "statSeconds": 15,
            },
        }
    )
    wikiServiceInfo = ConfigDict(
        {
            "Enabled": wikiEnabled,
            "EndpointDescriptor": "tcp:host=localhost:port=4444",
        }
    )
    directory = buildDirectory(
        store, dataRoot, servicesInfo, augmentServiceInfo, wikiServiceInfo,
        serversDB, cacheSeconds
    )

    store.setDirectoryService(directory)

    return directory


class CommonCommonTests(object):
    """
    Common utility functionality for file/store combination tests.
    """

    lastTransaction = None
    savedStore = None
    assertProvides = assertProvides

    @inlineCallbacks
    def buildStoreAndDirectory(
        self, accounts=None, resources=None, augments=None, proxies=None,
        extraUids=None, serversDB=None, cacheSeconds=0,
        storeBuilder=theStoreBuilder,
    ):

        self.serverRoot = self.mktemp()
        os.mkdir(self.serverRoot)

        self.counter = 0
        self.notifierFactory = StubNotifierFactory()

        config.reset()
        self.configure()

        self.store = yield self.buildStore(storeBuilder)
        self._sqlCalendarStore = self.store  # FIXME: remove references to this

        self.directory = buildTestDirectory(
            self.store, config.DataRoot,
            accounts=accounts, resources=resources,
            augments=augments, proxies=proxies,
            serversDB=serversDB, cacheSeconds=cacheSeconds,
            wikiEnabled=config.Authentication.Wiki.Enabled
        )
        if extraUids:
            for uid in extraUids:
                yield self.addRecordFromFields(
                    {
                        self.directory.fieldName.uid:
                            uid,
                        self.directory.fieldName.recordType:
                            self.directory.recordType.user,
                    }
                )

    def configure(self):
        """
        Modify the configuration to suit unit tests, with a mktemp-created
        ServerRoot
        """

        config.ServerRoot = os.path.abspath(self.serverRoot)
        config.ConfigRoot = "config"
        config.LogRoot = "logs"
        config.RunRoot = "logs"

        if not os.path.exists(config.DataRoot):
            os.makedirs(config.DataRoot)
        if not os.path.exists(config.DocumentRoot):
            os.makedirs(config.DocumentRoot)
        if not os.path.exists(config.ConfigRoot):
            os.makedirs(config.ConfigRoot)
        if not os.path.exists(config.LogRoot):
            os.makedirs(config.LogRoot)

        # Work queues for implicit scheduling slow down tests a lot and require
        # them all to add "waits" for work to complete.
        # Rewriting all the current tests to do that is not practical right
        # now, so we will turn this off by default.
        # Instead we will have a set of tests dedicated to work queue-based
        # scheduling which will patch this option to True.
        config.Scheduling.Options.WorkQueues.Enabled = False

        self.config = config

    def buildStore(self, storeBuilder=theStoreBuilder, notifierFactory=None):
        """
        Builds and returns a store
        """

        # Build the store before the directory; the directory will be assigned
        # to the store via setDirectoryService()
        return storeBuilder.buildStore(self, notifierFactory if notifierFactory is not None else self.notifierFactory, None)

    def transactionUnderTest(self, txn=None):
        """
        Create a transaction from C{storeUnderTest} and save it as
        C{lastTransaction}.  Also makes sure to use the same store, saving the
        value from C{storeUnderTest}.
        """
        if self.lastTransaction is None:
            self.lastTransaction = self.concurrentTransaction(txn)
        return self.lastTransaction

    def concurrentTransaction(self, txn=None):
        """
        Create a transaction from C{storeUnderTest} and save it for later
        clean-up.
        """
        if self.savedStore is None:
            self.savedStore = self.storeUnderTest()

        self.counter += 1

        if txn is None:
            txn = self.savedStore.newTransaction(
                self.id() + " #" + str(self.counter)
            )
        else:
            txn._label = self.id() + " #" + str(self.counter)

        @inlineCallbacks
        def maybeCommitThis():
            try:
                yield txn.commit()
            except AlreadyFinishedError:
                pass

        self.addCleanup(maybeCommitThis)

        return txn

    def commit(self):
        """
        Commit the last transaction created from C{transactionUnderTest}, and
        clear it.
        """
        result = self.lastTransaction.commit()
        self.lastTransaction = None
        return result

    def abort(self):
        """
        Abort the last transaction created from C{transactionUnderTest}, and
        clear it.
        """
        result = self.lastTransaction.abort()
        self.lastTransaction = None
        return result

    def storeUnderTest(self):
        """
        Create and return the L{CalendarStore} for testing.
        """
        return self.store

    def homeUnderTest(self, txn=None, name="home1", status=None, create=False):
        """
        Get the calendar home detailed by C{requirements['home1']}.
        """
        if txn is None:
            txn = self.transactionUnderTest()
        return txn.calendarHomeWithUID(name, status=status, create=create)

    @inlineCallbacks
    def calendarUnderTest(self, txn=None, name="calendar_1", home="home1", status=None):
        """
        Get the calendar detailed by C{requirements['home1']['calendar_1']}.
        """
        home = yield self.homeUnderTest(txn, home, status=status)
        calendar = yield home.calendarWithName(name)
        returnValue(calendar)

    @inlineCallbacks
    def calendarObjectUnderTest(
        self, txn=None, name="1.ics", calendar_name="calendar_1", home="home1", status=None
    ):
        """
        Get the calendar detailed by
        C{requirements[home][calendar_name][name]}.
        """
        calendar = yield self.calendarUnderTest(
            txn, name=calendar_name, home=home, status=status
        )
        object = yield calendar.calendarObjectWithName(name)
        returnValue(object)

    @inlineCallbacks
    def trashObjectsUnderTest(
        self, txn=None, home="home1", status=None
    ):
        """
        Get the calendar detailed by
        C{requirements[home][calendar_name][name]}.
        """
        home = yield self.homeUnderTest(name=home)
        trash = yield home.getTrash()
        objects = yield trash.objectResources()
        returnValue(objects)

    def addressbookHomeUnderTest(self, txn=None, name="home1"):
        """
        Get the addressbook home detailed by C{requirements['home1']}.
        """
        if txn is None:
            txn = self.transactionUnderTest()
        return txn.addressbookHomeWithUID(name)

    @inlineCallbacks
    def addressbookUnderTest(self, txn=None, name="addressbook", home="home1"):
        """
        Get the addressbook detailed by
        C{requirements['home1']['addressbook']}.
        """
        home = yield self.addressbookHomeUnderTest(txn=txn, name=home)
        addressbook = yield home.addressbookWithName(name)
        returnValue(addressbook)

    @inlineCallbacks
    def addressbookObjectUnderTest(
        self, txn=None, name="1.vcf",
        addressbook_name="addressbook", home="home1",
    ):
        """
        Get the addressbook detailed by
        C{requirements['home1']['addressbook']['1.vcf']}.
        """
        addressBook = yield self.addressbookUnderTest(
            txn=txn, name=addressbook_name, home=home
        )
        object = yield addressBook.addressbookObjectWithName(name)
        returnValue(object)

    def notificationCollectionUnderTest(self, txn=None, name="home1", status=None, create=False):
        if txn is None:
            txn = self.transactionUnderTest()
        return txn.notificationsWithUID(name, status=status, create=create)

    def userRecordWithShortName(self, shortname):
        return self.directory.recordWithShortName(
            self.directory.recordType.user, shortname
        )

    @inlineCallbacks
    def userUIDFromShortName(self, shortname):
        record = yield self.directory.recordWithShortName(
            self.directory.recordType.user, shortname
        )
        returnValue(record.uid if record is not None else None)

    def addRecordFromFields(self, fields):
        updatedRecord = DirectoryRecord(self.directory, fields)
        return self.directory.updateRecords((updatedRecord,), create=True)

    def removeRecord(self, uid):
        return self.directory.removeRecords([uid])

    def changeRecord(self, record, fieldname, value, directory=None):
        if directory is None:
            directory = self.directory
        fields = record.fields.copy()
        fields[fieldname] = value
        updatedRecord = DirectoryRecord(directory, fields)
        return directory.updateRecords((updatedRecord,))


class StubNotifierFactory(object):
    """
    For testing push notifications without an XMPP server.
    """

    def __init__(self):
        self.reset()
        self.hostname = "example.com"

    def newNotifier(self, storeObject):
        return Notifier(self, storeObject)

    def pushKeyForId(self, prefix, id):
        return "/%s/%s/%s/" % (prefix, self.hostname, id)

    def send(self, prefix, id, txn, priority):
        self.history.append((self.pushKeyForId(prefix, id), priority))

    def reset(self):
        self.history = []


def disableMemcacheForTest(aTest):
    """
    Disable all memcache logic for the duration of a test; we shouldn't be
    starting or connecting to any memcache stuff for most tests.
    """

    # These imports are local so that they don't accidentally leak to anything
    # else in this module; nothing else in this module should ever touch global
    # configuration. -glyph

    from twistedcaldav.memcacher import Memcacher

    if not hasattr(config, "Memcached"):
        config.setDefaults(DEFAULT_CONFIG)
    aTest.patch(config.Memcached.Pools.Default, "ClientEnabled", False)
    aTest.patch(config.Memcached.Pools.Default, "ServerEnabled", False)
    aTest.patch(Memcacher, "allowTestCache", True)
