# -*- test-case-name: txdav.caldav.datastore.test.test_sql,txdav.carddav.datastore.test.test_sql -*-
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
SQL data store.
"""
from __future__ import print_function

__all__ = [
    "CommonDataStore",
    "CommonStoreTransaction",
    "CommonHome",
]

from cStringIO import StringIO

from pycalendar.datetime import DateTime

from twext.enterprise.dal.parseschema import splitSQLString
from twext.enterprise.dal.syntax import (
    Delete, utcNowSQL, Union, Insert, Len, Max, Parameter, SavepointAction,
    Select, Update, Count, ALL_COLUMNS, Sum,
    DatabaseLock, DatabaseUnlock)
from twext.enterprise.ienterprise import AlreadyFinishedError
from twext.enterprise.jobs.queue import LocalQueuer
from twext.enterprise.util import parseSQLTimestamp
from twext.internet.decorate import Memoizable
from twext.python.clsprop import classproperty
from twext.python.log import Logger

from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from twisted.python.failure import Failure
from twisted.python.modules import getModule
from twisted.python.util import FancyEqMixin

from twistedcaldav.config import config
from twistedcaldav.dateops import datetimeMktime, pyCalendarToSQLTimestamp

from txdav.base.datastore.util import QueryCacher
from txdav.base.propertystore.none import PropertyStore as NonePropertyStore
from txdav.base.propertystore.sql import PropertyStore
from txdav.caldav.icalendarstore import ICalendarTransaction, ICalendarStore
from txdav.carddav.iaddressbookstore import IAddressBookTransaction
from txdav.common.datastore.common import HomeChildBase
from txdav.common.datastore.podding.conduit import PoddingConduit
from txdav.common.datastore.podding.migration.work import MigratedHomeCleanupWork
from txdav.common.datastore.sql_apn import APNSubscriptionsMixin
from txdav.common.datastore.sql_directory import DelegatesAPIMixin, \
    GroupsAPIMixin, GroupCacherAPIMixin
from txdav.common.datastore.sql_dump import dumpSchema
from txdav.common.datastore.sql_imip import imipAPIMixin
from txdav.common.datastore.sql_notification import NotificationCollection
from txdav.common.datastore.sql_tables import _BIND_MODE_OWN, _BIND_STATUS_ACCEPTED, \
    _HOME_STATUS_EXTERNAL, _HOME_STATUS_NORMAL, \
    _HOME_STATUS_PURGING, schema, _HOME_STATUS_MIGRATING, \
    _HOME_STATUS_DISABLED, _CHILD_TYPE_NORMAL
from txdav.common.datastore.sql_util import _SharedSyncLogic
from txdav.common.datastore.sql_sharing import SharingHomeMixIn, SharingMixIn
from txdav.common.icommondatastore import ConcurrentModification, \
    RecordNotAllowedError, ShareNotAllowed, \
    IndexedSearchException, EADDRESSBOOKTYPE, ECALENDARTYPE
from txdav.common.icommondatastore import HomeChildNameNotAllowedError, \
    HomeChildNameAlreadyExistsError, NoSuchHomeChildError, \
    ObjectResourceNameNotAllowedError, ObjectResourceNameAlreadyExistsError, \
    NoSuchObjectResourceError, AllRetriesFailed, \
    TooManyObjectResourcesError, SyncTokenValidException, AlreadyInTrashError
from txdav.common.idirectoryservice import IStoreDirectoryService, \
    DirectoryRecordNotFoundError
from txdav.idav import ChangeCategory
from calendarserver.tools.util import displayNameForCollection, getEventDetails, agoString

from zope.interface import implements, directlyProvides

from collections import defaultdict
import datetime
import inspect
import itertools
import os
import sys
import time
from uuid import uuid4

current_sql_schema = getModule(__name__).filePath.sibling("sql_schema").child("current.sql").getContent()

log = Logger()


class CommonDataStore(Service, object):
    """
    Shared logic for SQL-based data stores, between calendar and addressbook
    storage.

    @ivar sqlTxnFactory: A 0-arg factory callable that produces an
        L{IAsyncTransaction}.

    @ivar _notifierFactories: a C{dict} of L{IStoreNotifierFactory} objects
        from which the store can create notifiers for store objects. The keys
        are "tokens" that determine the type of notifier.

    @ivar attachmentsPath: a L{FilePath} indicating a directory where
        attachments may be stored.

    @ivar enableCalendars: a boolean, C{True} if this data store should provide
        L{ICalendarStore}, C{False} if not.

    @ivar enableAddressBooks: a boolean, C{True} if this data store should
        provide L{IAddressbookStore}, C{False} if not.

    @ivar label: A string, used for tagging debug messages in the case where
        there is more than one store.  (Useful mostly for unit tests.)

    @ivar quota: the amount of space granted to each calendar home (in bytes)
        for storing attachments, or C{None} if quota should not be enforced.
    @type quota: C{int} or C{NoneType}

    @ivar queuer: An object with an C{enqueueWork} method, from
        L{twext.enterprise.jobs.queue}.  Initially, this is a L{LocalQueuer}, so it
        is always usable, but in a properly configured environment it will be
        upgraded to a more capable object that can distribute work throughout a
        cluster.
    """

    implements(ICalendarStore)

    def __init__(
        self,
        sqlTxnFactory,
        notifierFactories,
        directoryService,
        attachmentsPath,
        attachmentsURIPattern,
        enableCalendars=True,
        enableAddressBooks=True,
        enableManagedAttachments=True,
        label="unlabeled",
        quota=(2 ** 20),
        logLabels=False,
        logStats=False,
        logStatsLogFile=None,
        logSQL=False,
        logTransactionWaits=0,
        timeoutTransactions=0,
        cacheQueries=True,
        cachePool="Default",
        cacheExpireSeconds=3600
    ):
        assert enableCalendars or enableAddressBooks

        self.sqlTxnFactory = sqlTxnFactory
        self._notifierFactories = notifierFactories if notifierFactories is not None else {}
        self._directoryService = IStoreDirectoryService(directoryService) if directoryService is not None else None
        self.attachmentsPath = attachmentsPath
        self.attachmentsURIPattern = attachmentsURIPattern
        self.enableCalendars = enableCalendars
        self.enableAddressBooks = enableAddressBooks
        self.enableManagedAttachments = enableManagedAttachments
        self.label = label
        self.quota = quota
        self.logLabels = logLabels
        self.logStats = logStats
        self.logStatsLogFile = logStatsLogFile
        self.logSQL = logSQL
        self.logTransactionWaits = logTransactionWaits
        self.timeoutTransactions = timeoutTransactions
        self.queuer = LocalQueuer(self.newTransaction)
        self._migrating = False
        self._enableNotifications = True
        self._newTransactionCallbacks = set()

        if cacheQueries:
            self.queryCacher = QueryCacher(
                cachePool=cachePool,
                cacheExpireSeconds=cacheExpireSeconds
            )
        else:
            self.queryCacher = None

        self.conduit = PoddingConduit(self)

        # Always import these here to trigger proper "registration" of the calendar and address book
        # home classes
        __import__("txdav.caldav.datastore.sql")
        __import__("txdav.carddav.datastore.sql")

    def availablePrimaryStoreTypes(self):
        """
        The list of store home types supported.
        """
        return (ECALENDARTYPE, EADDRESSBOOKTYPE,)

    def directoryService(self):
        return self._directoryService

    def setDirectoryService(self, directoryService):
        self._directoryService = directoryService

    def callWithNewTransactions(self, callback):
        """
        Registers a method to be called whenever a new transaction is
        created.

        @param callback: callable taking a single argument, a transaction
        """
        self._newTransactionCallbacks.add(callback)

    @inlineCallbacks
    def _withEachHomeDo(self, homeTable, homeFromTxn, action, batchSize, processExternal=False):
        """
        Implementation of L{ICalendarStore.withEachCalendarHomeDo} and
        L{IAddressbookStore.withEachAddressbookHomeDo}.
        """
        txn = yield self.newTransaction(label="CommonDataStore._withEachHomeDo")
        try:
            allUIDs = yield (Select([homeTable.OWNER_UID], From=homeTable).on(txn))
            for [uid] in allUIDs:
                home = yield homeFromTxn(txn, uid)
                if not processExternal and home.external():
                    continue
                yield action(txn, (yield homeFromTxn(txn, uid)))
        except:
            a, b, c = sys.exc_info()
            yield txn.abort()
            raise a(b, c)
        else:
            yield txn.commit()

    def withEachCalendarHomeDo(self, action, batchSize=None, processExternal=False):
        """
        Implementation of L{ICalendarStore.withEachCalendarHomeDo}.
        """
        return self._withEachHomeDo(
            schema.CALENDAR_HOME,
            lambda txn, uid: txn.calendarHomeWithUID(uid),
            action, batchSize, processExternal
        )

    def withEachAddressbookHomeDo(self, action, batchSize=None, processExternal=False):
        """
        Implementation of L{IAddressbookStore.withEachAddressbookHomeDo}.
        """
        return self._withEachHomeDo(
            schema.ADDRESSBOOK_HOME,
            lambda txn, uid: txn.addressbookHomeWithUID(uid),
            action, batchSize, processExternal
        )

    def newTransaction(self, label="unlabeled", disableCache=False, authz_uid=None):
        """
        @see: L{IDataStore.newTransaction}
        """
        txn = CommonStoreTransaction(
            self,
            self.sqlTxnFactory(label=label),
            self.enableCalendars,
            self.enableAddressBooks,
            self._notifierFactories if self._enableNotifications else {},
            label,
            self._migrating,
            disableCache,
            authz_uid,
        )
        if self.logTransactionWaits or self.timeoutTransactions:
            CommonStoreTransactionMonitor(txn, self.logTransactionWaits,
                                          self.timeoutTransactions)
        for callback in self._newTransactionCallbacks:
            callback(txn)
        return txn

    @inlineCallbacks
    def inTransaction(self, label, operation, transactionCreator=None):
        """
        Perform the given operation in a transaction, committing or aborting as
        required.

        @param label: the label to pass to the transaction creator

        @param operation: a 1-arg callable that takes an L{IAsyncTransaction} and
            returns a value.

        @param transactionCreator: a 1-arg callable that takes a "label" arg and
            returns a transaction

        @return: a L{Deferred} that fires with C{operation}'s result or fails with
            its error, unless there is an error creating, aborting or committing
            the transaction.
        """

        if transactionCreator is None:
            transactionCreator = self.newTransaction

        txn = transactionCreator(label=label)

        try:
            result = yield operation(txn)
        except:
            f = Failure()
            yield txn.abort()
            returnValue(f)
        else:
            yield txn.commit()
            returnValue(result)

    def setMigrating(self, state):
        """
        Set the "migrating" state
        """
        self._migrating = state
        self._enableNotifications = not state

    def setUpgrading(self, state):
        """
        Set the "upgrading" state
        """
        self._enableNotifications = not state

    @inlineCallbacks
    def dropboxAllowed(self, txn):
        """
        Determine whether dropbox attachments are allowed. Once we have migrated to managed attachments,
        we should never allow dropbox-style attachments to be created.
        """
        if not hasattr(self, "_dropbox_ok"):
            already_migrated = (yield txn.calendarserverValue("MANAGED-ATTACHMENTS", raiseIfMissing=False))
            self._dropbox_ok = already_migrated is None
        returnValue(self._dropbox_ok)

    def queryCachingEnabled(self):
        """
        Indicate whether SQL statement query caching is enabled. Also controls whether propstore caching is done.

        @return: C{True} if enabled, else C{False}
        @rtype: C{bool}
        """
        return self.queryCacher is not None

    @inlineCallbacks
    def uidInStore(self, txn, uid):
        """
        Indicate whether the specified user UID is hosted in the current store, or
        possibly in another pod.

        @param txn: transaction to use
        @type txn: L{CommonStoreTransaction}
        @param uid: the user UID to test
        @type uid: L{str}

        @return: a tuple of L{bool}, L{str} - the first indicates whether the user is
            hosted, the second the serviceNodeUID of the pod hosting the user or
            C{None} if on this pod.
        @rtype: L{tuple}
        """

        # Check if locally stored first
        for storeType in self.availablePrimaryStoreTypes():
            home = yield txn.homeWithUID(storeType, uid)
            if home is not None:
                if home.external():
                    # TODO: locate the pod where the user is hosted
                    returnValue((True, "unknown",))
                else:
                    returnValue((True, None,))
        else:
            returnValue((False, None,))

    @inlineCallbacks
    def checkSchema(self, expected_schema, schema_name):
        """
        Check the schema actually in the database against the one in the supplied
        schema file.

        @param expected_schema: the expected schema
        @type expected_schema: L{Schema}
        """
        txn = yield self.newTransaction(label="CommonDataStore.checkSchema")
        try:
            actual_schema = yield dumpSchema(txn, "actual", schema_name)
            results = actual_schema.compare(expected_schema)
            self.__class__.checkSchemaResults = results
            if results:
                log.warn("Schema comparison mismatch:\n{}".format("\n".join(results)))
            else:
                log.warn("Schema comparison match")
        except Exception as e:
            log.error("Schema comparison match failed: {}".format(e))
            yield txn.abort()
        else:
            yield txn.commit()


class TransactionStatsCollector(object):
    """
    Used to log each SQL query and statistics about that query during the course of a single transaction.
    Results can be printed out where ever needed at the end of the transaction.
    """

    def __init__(self, label, logFileName=None):
        self.label = label
        self.logFileName = logFileName
        self.statements = []
        self.startTime = time.time()

    def startStatement(self, sql, args):
        """
        Called prior to an SQL query being run.

        @param sql: the SQL statement to execute
        @type sql: C{str}
        @param args: the arguments (binds) to the SQL statement
        @type args: C{list}

        @return: C{tuple} containing the index in the statement list for this statement, and the start time
        """
        args = ["%s" % (arg,) for arg in args]
        args = [((arg[:10] + "...") if len(arg) > 40 else arg) for arg in args]
        self.statements.append(["%s %s" % (sql, args,), 0, 0, 0])
        return len(self.statements) - 1, time.time()

    def endStatement(self, context, rows):
        """
        Called after an SQL query has executed.

        @param context: the tuple returned from startStatement
        @type context: C{tuple}
        @param rows: number of rows returned from the query
        @type rows: C{int}
        """
        index, tstamp = context
        t = time.time()
        self.statements[index][1] = len(rows) if rows else 0
        self.statements[index][2] = t - tstamp
        self.statements[index][3] = t

    def printReport(self):
        """
        Print a report of all the SQL statements executed to date.
        """

        total_statements = len(self.statements)
        total_rows = sum([statement[1] for statement in self.statements])
        total_time = sum([statement[2] for statement in self.statements]) * 1000.0

        toFile = StringIO()
        toFile.write("*** SQL Stats ***\n")
        toFile.write("\n")
        toFile.write("Label: %s\n" % (self.label,))
        toFile.write("Unique statements: %d\n" % (len(set([statement[0] for statement in self.statements]),),))
        toFile.write("Total statements: %d\n" % (total_statements,))
        toFile.write("Total rows: %d\n" % (total_rows,))
        toFile.write("Total time (ms): %.3f\n" % (total_time,))
        t_last_end = self.startTime
        for sql, rows, t_taken, t_end in self.statements:
            toFile.write("\n")
            toFile.write("SQL: %s\n" % (sql,))
            toFile.write("Rows: %s\n" % (rows,))
            toFile.write("Time (ms): %.3f\n" % (t_taken * 1000.0,))
            toFile.write("Idle (ms): %.3f\n" % ((t_end - t_taken - t_last_end) * 1000.0,))
            toFile.write("Elapsed (ms): %.3f\n" % ((t_end - self.startTime) * 1000.0,))
            t_last_end = t_end
        toFile.write("Commit (ms): %.3f\n" % ((time.time() - t_last_end) * 1000.0,))
        toFile.write("***\n\n")

        if self.logFileName:
            with open(self.logFileName, "a") as f:
                f.write(toFile.getvalue())
        else:
            log.error(toFile.getvalue())

        return (total_statements, total_rows, total_time,)


class CommonStoreTransactionMonitor(object):
    """
    Object that monitors the state of a transaction over time and logs or times out
    the transaction.
    """

    callLater = reactor.callLater

    def __init__(self, txn, logTimerSeconds, timeoutSeconds):
        self.txn = txn
        self.delayedLog = None
        self.delayedTimeout = None
        self.logTimerSeconds = logTimerSeconds
        self.timeoutSeconds = timeoutSeconds

        self.txn.postCommit(self._cleanTxn)
        self.txn.postAbort(self._cleanTxn)

        self._installLogTimer()
        self._installTimeout()

    def _cleanTxn(self):
        self.txn = None
        if self.delayedLog:
            self.delayedLog.cancel()
            self.delayedLog = None
        if self.delayedTimeout:
            self.delayedTimeout.cancel()
            self.delayedTimeout = None
        return succeed(None)

    def _installLogTimer(self):
        def _logTransactionWait():
            if self.txn is not None:
                log.error(
                    "Transaction wait: {me.txn}, "
                    "Statements: {me.txn.statementCount:d}, "
                    "IUDs: {me.txn.iudCount:d}, "
                    "Statement: {me.txn.currentStatement}",
                    me=self
                )
                self.delayedLog = self.callLater(
                    self.logTimerSeconds, _logTransactionWait
                )

        if self.logTimerSeconds:
            self.delayedLog = self.callLater(
                self.logTimerSeconds, _logTransactionWait
            )

    def _installTimeout(self):
        def _forceAbort():
            if self.txn is not None:
                log.error(
                    "Transaction abort too long: {me.txn}, "
                    "Statements: {me.txn.statementCount:d}, "
                    "IUDs: {me.txn.iudCount:d}, "
                    "Statement: {me.txn.currentStatement}",
                    me=self
                )
                self.delayedTimeout = None
                if self.delayedLog:
                    self.delayedLog.cancel()
                    self.delayedLog = None
                self.txn.timeout()

        if self.timeoutSeconds:
            self.delayedTimeout = self.callLater(
                self.timeoutSeconds, _forceAbort
            )


class CommonStoreTransaction(
    GroupsAPIMixin, GroupCacherAPIMixin, DelegatesAPIMixin,
    imipAPIMixin, APNSubscriptionsMixin,
):
    """
    Transaction implementation for SQL database.
    """
    _homeClass = {}

    id = 0

    def __init__(
        self, store, sqlTxn,
        enableCalendars, enableAddressBooks,
        notifierFactories, label, migrating=False, disableCache=False,
        authz_uid=None,
    ):
        if label == "unlabeled" or not label:
            tr = inspect.getframeinfo(inspect.currentframe().f_back.f_back)
            label = "{}#{}${}".format(tr.filename, tr.lineno, tr.function)

        self._store = store
        self._queuer = self._store.queuer
        self._cachedHomes = {
            ECALENDARTYPE: {
                "byUID": defaultdict(dict),
                "byID": defaultdict(dict),
            },
            EADDRESSBOOKTYPE: {
                "byUID": defaultdict(dict),
                "byID": defaultdict(dict),
            },
        }
        self._notificationHomes = {
            "byUID": defaultdict(dict),
            "byID": defaultdict(dict),
        }
        self._notifierFactories = notifierFactories
        self._notifiedAlready = set()
        self._bumpedRevisionAlready = set()
        self._label = label
        self._migrating = migrating
        self._allowDisabled = False
        self._primaryHomeType = None
        self._disableCache = disableCache or not store.queryCachingEnabled()
        if disableCache:
            self._queryCacher = None
        else:
            self._queryCacher = store.queryCacher
        self._authz_uid = authz_uid

        CommonStoreTransaction.id += 1
        self._txid = CommonStoreTransaction.id

        extraInterfaces = []
        if enableCalendars:
            extraInterfaces.append(ICalendarTransaction)
            self._primaryHomeType = ECALENDARTYPE
        if enableAddressBooks:
            extraInterfaces.append(IAddressBookTransaction)
            if self._primaryHomeType is None:
                self._primaryHomeType = EADDRESSBOOKTYPE
        directlyProvides(self, *extraInterfaces)

        self._sqlTxn = sqlTxn
        self.dbtype = sqlTxn.dbtype

        self._stats = (
            TransactionStatsCollector(self._label, self._store.logStatsLogFile)
            if self._store.logStats else None
        )
        self.statementCount = 0
        self.iudCount = 0
        self.currentStatement = None
        self.timedout = False

        self.logItems = {}

    def enqueue(self, workItem, **kw):
        """
        Enqueue a L{twext.enterprise.jobs.workitem.WorkItem} for later execution.

        For example::

            yield (txn.enqueue(MyWorkItem, workDescription="some work to do"))
        """
        return self._store.queuer.enqueueWork(self, workItem, **kw)

    def store(self):
        return self._store

    def directoryService(self):
        return self._store.directoryService()

    def __repr__(self):
        return 'PG-TXN<%s>' % (self._label,)

    @classproperty
    def _calendarserver(cls):
        cs = schema.CALENDARSERVER
        return Select(
            [cs.VALUE, ],
            From=cs,
            Where=cs.NAME == Parameter('name'),
        )

    @inlineCallbacks
    def calendarserverValue(self, key, raiseIfMissing=True):
        result = yield self._calendarserver.on(self, name=key)
        if result and len(result) == 1:
            returnValue(result[0][0])
        if raiseIfMissing:
            raise RuntimeError("Database key %s cannot be determined." % (key,))
        else:
            returnValue(None)

    @inlineCallbacks
    def setCalendarserverValue(self, key, value):
        cs = schema.CALENDARSERVER
        yield Insert(
            {cs.NAME: key, cs.VALUE: value},
        ).on(self)

    @inlineCallbacks
    def updateCalendarserverValue(self, key, value):
        cs = schema.CALENDARSERVER
        yield Update(
            {cs.VALUE: value},
            Where=cs.NAME == key,
        ).on(self)

    def _determineMemo(self, storeType, lookupMode, status):
        """
        Determine the memo dictionary to use for homeWithUID.
        """
        return self._cachedHomes[storeType][lookupMode][status]

    @inlineCallbacks
    def homes(self, storeType):
        """
        Load all calendar or addressbook homes.
        """

        # Get all UIDs and load them - this will memoize all existing ones
        uids = (yield self._homeClass[storeType].listHomes(self))
        for uid in uids:
            yield self.homeWithUID(storeType, uid, create=False)

        # Return the memoized list directly
        returnValue([kv[1] for kv in sorted(self._determineMemo(storeType, "byUID", _HOME_STATUS_NORMAL).items(), key=lambda x: x[0])])

    @inlineCallbacks
    def homeWithUID(self, storeType, uid, status=None, create=False, authzUID=None):
        """
        We need to distinguish between various different users "looking" at a home and its
        child resources because we have per-user properties that depend on which user is "looking".
        By default the viewer is set to the authz_uid on the transaction, or the owner if no authz,
        but it can be overridden using L{authzUID}. This is useful when the store needs to get to
        other user's homes with the viewer being the owner of that home as opposed to authz_uid. That
        often happens when manipulating shares.
        """
        if storeType not in (ECALENDARTYPE, EADDRESSBOOKTYPE):
            raise RuntimeError("Unknown home type.")

        if status is None:
            # Look for any that match possible status and return the first one.
            # The order of status here matches that in L{CommonHome.homeWith}.
            for possible_status in (_HOME_STATUS_NORMAL, _HOME_STATUS_DISABLED, _HOME_STATUS_EXTERNAL,):
                result = self._determineMemo(storeType, "byUID", possible_status).get(uid)
                if result is not None:
                    break
        else:
            result = self._determineMemo(storeType, "byUID", status).get(uid)
        if result is None:
            result = yield self._homeClass[storeType].homeWithUID(self, uid, status, create, authzUID)
            if result:
                self._determineMemo(storeType, "byUID", result.status())[uid] = result
                self._determineMemo(storeType, "byID", None)[result.id()] = result
        returnValue(result)

    def calendarHomeWithUID(self, uid, status=None, create=False, authzUID=None):
        return self.homeWithUID(ECALENDARTYPE, uid, status=status, create=create, authzUID=authzUID)

    def addressbookHomeWithUID(self, uid, status=None, create=False, authzUID=None):
        return self.homeWithUID(EADDRESSBOOKTYPE, uid, status=status, create=create, authzUID=authzUID)

    @inlineCallbacks
    def homeWithResourceID(self, storeType, rid):
        """
        Load a calendar or addressbook home by its integer resource ID.
        """
        if storeType not in (ECALENDARTYPE, EADDRESSBOOKTYPE):
            raise RuntimeError("Unknown home type.")

        result = self._determineMemo(storeType, "byID", None).get(rid)
        if result is None:
            result = yield self._homeClass[storeType].homeWithResourceID(self, rid)
            if result:
                self._determineMemo(storeType, "byID", None)[rid] = result
                self._determineMemo(storeType, "byUID", result.status())[result.uid()] = result
        returnValue(result)

    def calendarHomeWithResourceID(self, rid):
        return self.homeWithResourceID(ECALENDARTYPE, rid)

    def addressbookHomeWithResourceID(self, rid):
        return self.homeWithResourceID(EADDRESSBOOKTYPE, rid)

    def resetHomeCache(self):
        """
        Initialize the home caches, wiping any that existed before. This is only
        used in testing when we want to be sure all cached objects have been
        removed.
        """
        self._cachedHomes = {
            ECALENDARTYPE: {
                "byUID": defaultdict(dict),
                "byID": defaultdict(dict),
            },
            EADDRESSBOOKTYPE: {
                "byUID": defaultdict(dict),
                "byID": defaultdict(dict),
            },
        }
        self._notificationHomes = {
            "byUID": defaultdict(dict),
            "byID": defaultdict(dict),
        }

    def cleanHomeCache(self, home):
        """
        Remove the specified home from the home cache.
        @param home: home to remove
        @type home: L{CommonHome}
        """
        rid_memo = self._determineMemo(home._homeType, "byID", None)
        if home.id() in rid_memo:
            del rid_memo[home.id()]
        uid_memo = self._determineMemo(home._homeType, "byUID", home.status())
        if home.uid() in uid_memo:
            del uid_memo[home.uid()]

    @inlineCallbacks
    def notificationsWithUID(self, uid, status=None, create=False):
        """
        Implement notificationsWithUID.
        """

        result = self._notificationHomes["byUID"][status].get(uid)
        if result is None:
            result = yield NotificationCollection.notificationsWithUID(self, uid, status=status, create=create)
            if result:
                self._notificationHomes["byUID"][status][uid] = result
                self._notificationHomes["byID"][None][result.id()] = result
        returnValue(result)

    @inlineCallbacks
    def notificationsWithResourceID(self, rid):
        """
        Implement notificationsWithResourceID.
        """

        result = self._notificationHomes["byID"][None].get(rid)
        if result is None:
            result = yield NotificationCollection.notificationsWithResourceID(self, rid)
            if result:
                self._notificationHomes["byID"][None][rid] = result
                self._notificationHomes["byUID"][result.status()][result.uid()] = result
        returnValue(result)

    def migratedHome(self, ownerUID):
        """
        This pod is being told that user data migration to another pod has been completed, and the old data now
        needs to be removed.

        @param ownerUID: directory UID of the user whose data has been migrated
        @type ownerUID: L{str}
        """

        # All we do is schedule a work item to run the actual clean-up
        return MigratedHomeCleanupWork.reschedule(
            self,
            MigratedHomeCleanupWork.notBeforeDelay,
            ownerUID=ownerUID,
        )

    def preCommit(self, operation):
        """
        Run things before C{commit}.  (Note: only provided by SQL
        implementation, used only for cleaning up database state.)
        """
        return self._sqlTxn.preCommit(operation)

    def postCommit(self, operation):
        """
        Run things after C{commit}.
        """
        return self._sqlTxn.postCommit(operation)

    def postAbort(self, operation):
        """
        Run things after C{abort}.
        """
        return self._sqlTxn.postAbort(operation)

    def isNotifiedAlready(self, obj):
        return obj.id() in self._notifiedAlready

    def notificationAddedForObject(self, obj):
        self._notifiedAlready.add(obj.id())

    def isRevisionBumpedAlready(self, obj):
        """
        Indicates whether or not bumpRevisionForObject has already been
        called for the given object, in order to facilitate changing the
        revision only once per object.
        """
        return obj.id() in self._bumpedRevisionAlready

    def bumpRevisionForObject(self, obj):
        """
        Records the fact that a revision token for the object has been bumped.
        """
        self._bumpedRevisionAlready.add(obj.id())

    _savepointCounter = 0

    def _savepoint(self):
        """
        Generate a new SavepointAction whose name is unique in this transaction.
        """
        self._savepointCounter += 1
        return SavepointAction('sp%d' % (self._savepointCounter,))

    @inlineCallbacks
    def subtransaction(self, thunk, retries=1, failureOK=False):
        """
        Create a limited transaction object, which provides only SQL execution,
        and run a function in a sub-transaction (savepoint) context, with that
        object to execute SQL on.

        @param thunk: a 1-argument callable which returns a Deferred when it is
            done.  If this Deferred fails, the sub-transaction will be rolled
            back.
        @type thunk: L{callable}

        @param retries: the number of times to re-try C{thunk} before deciding
            that it's legitimately failed.
        @type retries: L{int}

        @param failureOK: it is OK if this subtransaction fails so do not log.
        @type failureOK: L{bool}

        @return: a L{Deferred} which fires or fails according to the logic in
            C{thunk}.  If it succeeds, it will return the value that C{thunk}
            returned.  If C{thunk} fails or raises an exception more than
            C{retries} times, then the L{Deferred} resulting from
            C{subtransaction} will fail with L{AllRetriesFailed}.
        """
        # Right now this code is covered mostly by the automated property store
        # tests.  It should have more direct test coverage.

        # TODO: we should really have a list of acceptable exceptions for
        # failure and not blanket catch, but that involves more knowledge of
        # the database driver in use than we currently possess at this layer.
        block = self._sqlTxn.commandBlock()
        sp = self._savepoint()
        failuresToMaybeLog = []

        def end():
            block.end()
            for f in failuresToMaybeLog:
                # TODO: direct tests, to make sure error logging
                # happens correctly in all cases.
                log.error("in subTransaction()", failure=f)
            raise AllRetriesFailed()

        triesLeft = retries
        try:
            while True:
                yield sp.acquire(block)
                try:
                    result = yield thunk(block)
                except:
                    f = Failure()
                    if not failureOK:
                        failuresToMaybeLog.append(f)
                    yield sp.rollback(block)
                    if triesLeft:
                        triesLeft -= 1
                        # Important to get the new block before the old one has
                        # been completed; since we almost certainly have some
                        # writes to do, the caller of commit() will expect that
                        # they actually get done, even if they didn't actually
                        # block or yield to wait for them!  (c.f. property
                        # store writes.)
                        newBlock = self._sqlTxn.commandBlock()
                        block.end()
                        block = newBlock
                        sp = self._savepoint()
                    else:
                        end()
                else:
                    yield sp.release(block)
                    block.end()
                    returnValue(result)
        except AlreadyFinishedError:
            # Interfering agents may disrupt our plans by calling abort()
            # halfway through trying to do this subtransaction.  In that case -
            # and only that case - acquire() or release() or commandBlock() may
            # raise an AlreadyFinishedError (either synchronously, or in the
            # case of the first two, possibly asynchronously as well).  We can
            # safely ignore this error, because it can't have any effect on
            # what gets written; our caller will just get told that it failed
            # in a way they have to be prepared for anyway.
            end()

    @inlineCallbacks
    def execSQL(self, *a, **kw):
        """
        Execute some SQL (delegate to L{IAsyncTransaction}).
        """
        if self._stats:
            statsContext = self._stats.startStatement(a[0], a[1] if len(a) > 1 else ())
        self.currentStatement = a[0]
        if self._store.logTransactionWaits and a[0].split(" ", 1)[0].lower() in ("insert", "update", "delete",):
            self.iudCount += 1
        self.statementCount += 1
        if self._store.logLabels:
            a = ("-- Label: %s\n" % (self._label.replace("%", "%%"),) + a[0],) + a[1:]
        if self._store.logSQL:
            log.error("SQL: {a!r} {kw!r}", a=a, kw=kw)
        results = None
        try:
            results = (yield self._sqlTxn.execSQL(*a, **kw))
        finally:
            self.currentStatement = None
            if self._stats:
                self._stats.endStatement(statsContext, results)
        returnValue(results)

    @inlineCallbacks
    def execSQLBlock(self, sql):
        """
        Execute SQL statements parsed by splitSQLString.
        FIXME: temporary measure for handling large schema upgrades. This should NOT be used
        for regular SQL operations - only upgrades.
        """
        for stmt in splitSQLString(sql):
            if not stmt.startswith("--"):
                try:
                    yield self.execSQL(stmt)
                except (RuntimeError, Exception) as e:
                    e.stmt = "SQLBlock statement failed: {}".format(stmt)
                    raise

    def commit(self):
        """
        Commit the transaction and execute any post-commit hooks.
        """

        # Do stats logging as a postCommit because there might be some pending preCommit SQL we want to log
        if self._stats:
            self.postCommit(self.statsReport)
        return self._sqlTxn.commit()

    def abort(self):
        """
        Abort the transaction.
        """
        return self._sqlTxn.abort()

    def timeout(self):
        """
        Abort the transaction due to time out.
        """
        self.timedout = True
        return self.abort()

    def statsReport(self):
        """
        Print the stats report and record log items
        """
        sql_statements, sql_rows, sql_time = self._stats.printReport()
        self.logItems["sql-s"] = str(sql_statements)
        self.logItems["sql-r"] = str(sql_rows)
        self.logItems["sql-t"] = "%.1f" % (sql_time,)

    def _oldEventsBase(self, limit):
        ch = schema.CALENDAR_HOME
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        tr = schema.TIME_RANGE
        kwds = {}
        if limit:
            kwds["Limit"] = limit
        return Select(
            [
                ch.OWNER_UID,
                cb.CALENDAR_RESOURCE_NAME,
                co.RESOURCE_NAME,
                Max(tr.END_DATE)
            ],
            From=ch.join(co).join(cb).join(tr),
            Where=(
                ch.RESOURCE_ID == cb.CALENDAR_HOME_RESOURCE_ID).And(
                tr.CALENDAR_OBJECT_RESOURCE_ID == co.RESOURCE_ID).And(
                cb.CALENDAR_RESOURCE_ID == tr.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN
            ),
            GroupBy=(
                ch.OWNER_UID,
                cb.CALENDAR_RESOURCE_NAME,
                co.RESOURCE_NAME
            ),
            Having=Max(tr.END_DATE) < Parameter("CutOff"),
            OrderBy=Max(tr.END_DATE),
            **kwds
        )

    @inlineCallbacks
    def eventsOlderThan(self, cutoff, batchSize=None):
        """
        Return up to the oldest batchSize events which exist completely earlier
        than "cutoff" (DateTime)

        Returns a deferred to a list of (uid, calendarName, eventName, maxDate)
        tuples.
        """

        # Make sure cut off is after any lower limit truncation in the DB
        if config.FreeBusyIndexLowerLimitDays:
            truncateLowerLimit = DateTime.getToday()
            truncateLowerLimit.offsetDay(-config.FreeBusyIndexLowerLimitDays)
            if cutoff < truncateLowerLimit:
                raise ValueError("Cannot query events older than %s" % (truncateLowerLimit.getText(),))

        kwds = {"CutOff": pyCalendarToSQLTimestamp(cutoff)}
        rows = yield self._oldEventsBase(batchSize).on(self, **kwds)
        returnValue([[row[0], row[1], row[2], parseSQLTimestamp(row[3])] for row in rows])

    @inlineCallbacks
    def removeOldEvents(self, cutoff, batchSize=None):
        """
        Remove up to batchSize events older than "cutoff" and return how
        many were removed.
        """

        # Make sure cut off is after any lower limit truncation in the DB
        if config.FreeBusyIndexLowerLimitDays:
            truncateLowerLimit = DateTime.getToday()
            truncateLowerLimit.offsetDay(-config.FreeBusyIndexLowerLimitDays)
            if cutoff < truncateLowerLimit:
                raise ValueError("Cannot query events older than %s" % (truncateLowerLimit.getText(),))

        results = (yield self.eventsOlderThan(cutoff, batchSize=batchSize))
        count = 0
        for uid, calendarName, eventName, _ignore_maxDate in results:
            home = (yield self.calendarHomeWithUID(uid))
            calendar = (yield home.childWithName(calendarName))
            resource = (yield calendar.objectResourceWithName(eventName))
            yield resource.purge(implicitly=False)
            count += 1
        returnValue(count)

    def orphanedAttachments(self, uuid=None, batchSize=None):
        """
        Find attachments no longer referenced by any events.

        Returns a deferred to a list of (calendar_home_owner_uid, quota used, total orphan size, total orphan count) tuples.
        """
        kwds = {}
        if uuid:
            kwds["uuid"] = uuid

        options = {}
        if batchSize:
            options["Limit"] = batchSize

        ch = schema.CALENDAR_HOME
        chm = schema.CALENDAR_HOME_METADATA
        co = schema.CALENDAR_OBJECT
        at = schema.ATTACHMENT

        where = (co.DROPBOX_ID == None).And(at.DROPBOX_ID != ".")
        if uuid:
            where = where.And(ch.OWNER_UID == Parameter('uuid'))

        return Select(
            [ch.OWNER_UID, chm.QUOTA_USED_BYTES, Sum(at.SIZE), Count(at.DROPBOX_ID)],
            From=at.join(
                co, at.DROPBOX_ID == co.DROPBOX_ID, "left outer").join(
                ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID).join(
                chm, ch.RESOURCE_ID == chm.RESOURCE_ID
            ),
            Where=where,
            GroupBy=(ch.OWNER_UID, chm.QUOTA_USED_BYTES),
            **options
        ).on(self, **kwds)

    @inlineCallbacks
    def removeOrphanedAttachments(self, uuid=None, batchSize=None):
        """
        Remove attachments that no longer have any references to them
        """

        # TODO: see if there is a better way to import Attachment
        from txdav.caldav.datastore.sql import DropBoxAttachment

        kwds = {}
        if uuid:
            kwds["uuid"] = uuid

        options = {}
        if batchSize:
            options["Limit"] = batchSize

        ch = schema.CALENDAR_HOME
        co = schema.CALENDAR_OBJECT
        at = schema.ATTACHMENT

        sfrom = at.join(co, at.DROPBOX_ID == co.DROPBOX_ID, "left outer")
        where = (co.DROPBOX_ID == None).And(at.DROPBOX_ID != ".")
        if uuid:
            sfrom = sfrom.join(ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID)
            where = where.And(ch.OWNER_UID == Parameter('uuid'))

        results = (yield Select(
            [at.DROPBOX_ID, at.PATH],
            From=sfrom,
            Where=where,
            **options
        ).on(self, **kwds))

        count = 0
        for dropboxID, path in results:
            attachment = (yield DropBoxAttachment.load(self, dropboxID, path))
            yield attachment.remove()
            count += 1
        returnValue(count)

    def oldDropboxAttachments(self, cutoff, uuid):
        """
        Find managed attachments attached to only events whose last instance is older than the specified cut-off.

        Returns a deferred to a list of (calendar_home_owner_uid, quota used, total old size, total old count) tuples.
        """
        kwds = {"CutOff": pyCalendarToSQLTimestamp(cutoff)}
        if uuid:
            kwds["uuid"] = uuid

        ch = schema.CALENDAR_HOME
        chm = schema.CALENDAR_HOME_METADATA
        co = schema.CALENDAR_OBJECT
        tr = schema.TIME_RANGE
        at = schema.ATTACHMENT

        where = at.DROPBOX_ID.In(Select(
            [at.DROPBOX_ID],
            From=at.join(co, at.DROPBOX_ID == co.DROPBOX_ID, "inner").join(
                tr, co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID
            ),
            GroupBy=(at.DROPBOX_ID,),
            Having=Max(tr.END_DATE) < Parameter("CutOff"),
        ))

        if uuid:
            where = where.And(ch.OWNER_UID == Parameter('uuid'))

        return Select(
            [ch.OWNER_UID, chm.QUOTA_USED_BYTES, Sum(at.SIZE), Count(at.DROPBOX_ID)],
            From=at.join(
                ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID).join(
                chm, ch.RESOURCE_ID == chm.RESOURCE_ID
            ),
            Where=where,
            GroupBy=(ch.OWNER_UID, chm.QUOTA_USED_BYTES),
        ).on(self, **kwds)

    @inlineCallbacks
    def removeOldDropboxAttachments(self, cutoff, uuid, batchSize=None):
        """
        Remove dropbox attachments attached to events in the past.
        """

        # TODO: see if there is a better way to import Attachment
        from txdav.caldav.datastore.sql import DropBoxAttachment

        kwds = {"CutOff": pyCalendarToSQLTimestamp(cutoff)}
        if uuid:
            kwds["uuid"] = uuid

        options = {}
        if batchSize:
            options["Limit"] = batchSize

        ch = schema.CALENDAR_HOME
        co = schema.CALENDAR_OBJECT
        tr = schema.TIME_RANGE
        at = schema.ATTACHMENT

        sfrom = at.join(
            co, at.DROPBOX_ID == co.DROPBOX_ID, "inner").join(
            tr, co.RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID
        )
        where = None
        if uuid:
            sfrom = sfrom.join(ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID)
            where = (ch.OWNER_UID == Parameter('uuid'))

        results = (yield Select(
            [at.DROPBOX_ID, at.PATH, ],
            From=sfrom,
            Where=where,
            GroupBy=(at.DROPBOX_ID, at.PATH,),
            Having=Max(tr.END_DATE) < Parameter("CutOff"),
            **options
        ).on(self, **kwds))

        count = 0
        for dropboxID, path in results:
            attachment = (yield DropBoxAttachment.load(self, dropboxID, path))
            yield attachment.remove()
            count += 1
        returnValue(count)

    def oldManagedAttachments(self, cutoff, uuid):
        """
        Find managed attachments attached to only events whose last instance is older than the specified cut-off.

        Returns a deferred to a list of (calendar_home_owner_uid, quota used, total old size, total old count) tuples.
        """
        kwds = {"CutOff": pyCalendarToSQLTimestamp(cutoff)}
        if uuid:
            kwds["uuid"] = uuid

        ch = schema.CALENDAR_HOME
        chm = schema.CALENDAR_HOME_METADATA
        tr = schema.TIME_RANGE
        at = schema.ATTACHMENT
        atco = schema.ATTACHMENT_CALENDAR_OBJECT

        where = at.ATTACHMENT_ID.In(Select(
            [at.ATTACHMENT_ID],
            From=at.join(
                atco, at.ATTACHMENT_ID == atco.ATTACHMENT_ID, "inner").join(
                tr, atco.CALENDAR_OBJECT_RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID
            ),
            GroupBy=(at.ATTACHMENT_ID,),
            Having=Max(tr.END_DATE) < Parameter("CutOff"),
        ))

        if uuid:
            where = where.And(ch.OWNER_UID == Parameter('uuid'))

        return Select(
            [ch.OWNER_UID, chm.QUOTA_USED_BYTES, Sum(at.SIZE), Count(at.ATTACHMENT_ID)],
            From=at.join(
                ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID).join(
                chm, ch.RESOURCE_ID == chm.RESOURCE_ID
            ),
            Where=where,
            GroupBy=(ch.OWNER_UID, chm.QUOTA_USED_BYTES),
        ).on(self, **kwds)

    @inlineCallbacks
    def removeOldManagedAttachments(self, cutoff, uuid, batchSize=None):
        """
        Remove attachments attached to events in the past.
        """

        # TODO: see if there is a better way to import Attachment
        from txdav.caldav.datastore.sql import ManagedAttachment

        kwds = {"CutOff": pyCalendarToSQLTimestamp(cutoff)}
        if uuid:
            kwds["uuid"] = uuid

        options = {}
        if batchSize:
            options["Limit"] = batchSize

        ch = schema.CALENDAR_HOME
        tr = schema.TIME_RANGE
        at = schema.ATTACHMENT
        atco = schema.ATTACHMENT_CALENDAR_OBJECT

        sfrom = atco.join(
            at, atco.ATTACHMENT_ID == at.ATTACHMENT_ID, "inner").join(
            tr, atco.CALENDAR_OBJECT_RESOURCE_ID == tr.CALENDAR_OBJECT_RESOURCE_ID
        )
        where = None
        if uuid:
            sfrom = sfrom.join(ch, at.CALENDAR_HOME_RESOURCE_ID == ch.RESOURCE_ID)
            where = (ch.OWNER_UID == Parameter('uuid'))

        results = (yield Select(
            [atco.ATTACHMENT_ID, atco.MANAGED_ID, ],
            From=sfrom,
            Where=where,
            GroupBy=(atco.ATTACHMENT_ID, atco.MANAGED_ID,),
            Having=Max(tr.END_DATE) < Parameter("CutOff"),
            **options
        ).on(self, **kwds))

        count = 0
        for _ignore, managedID in results:
            attachment = (yield ManagedAttachment.load(self, None, managedID))
            yield attachment.remove()
            count += 1
        returnValue(count)

    def acquireUpgradeLock(self):
        return DatabaseLock().on(self)

    def releaseUpgradeLock(self):
        return DatabaseUnlock().on(self)

    @inlineCallbacks
    def deleteRevisionsBefore(self, minRevision):
        """
        Delete revisions before minRevision
        """
        # Delete old revisions
        for table in (
            schema.CALENDAR_OBJECT_REVISIONS,
            schema.NOTIFICATION_OBJECT_REVISIONS,
            schema.ADDRESSBOOK_OBJECT_REVISIONS,
        ):
            yield Delete(
                From=table,
                Where=(table.REVISION < minRevision)
            ).on(self)

        # get groups where this object was once a member and version info
        aboMembers = schema.ABO_MEMBERS
        groupRows = yield Select(
            [aboMembers.GROUP_ID,
             aboMembers.MEMBER_ID,
             aboMembers.REMOVED,
             aboMembers.REVISION],
            From=aboMembers,
        ).on(self)

        # group results by group, member, and revisionInfo
        groupIDToMemberIDMap = {}
        for groupRow in groupRows:
            groupID, memberID, removed, revision = groupRow
            revisionInfo = [removed, revision]
            if groupID not in groupIDToMemberIDMap:
                groupIDToMemberIDMap[groupID] = {}
            memberIDToRevisionsMap = groupIDToMemberIDMap[groupID]
            if memberID not in memberIDToRevisionsMap:
                memberIDToRevisionsMap[memberID] = []
            revisionInfoList = memberIDToRevisionsMap[memberID]
            revisionInfoList.append(revisionInfo)

        # go though list an delete old revisions, leaving at least one unremoved member
        for groupID, memberIDToRevisionsMap in groupIDToMemberIDMap.iteritems():
            for memberID, revisionInfoList in memberIDToRevisionsMap.iteritems():
                revisionInfosToRemove = []
                revisionInfosToSave = []
                for revisionInfo in revisionInfoList:
                    if revisionInfo[1] < minRevision:
                        revisionInfosToRemove.append(revisionInfo)
                    else:
                        revisionInfosToSave.append(revisionInfo)

                # save at least one revision
                if revisionInfosToRemove and len(revisionInfosToRemove) == len(revisionInfoList):
                    maxRevisionInfoToRemove = max(revisionInfosToRemove, key=lambda info: info[1])
                    revisionInfosToSave.append(maxRevisionInfoToRemove)
                    revisionInfosToRemove.remove(maxRevisionInfoToRemove)

                # get rid of extra removed member revisions
                if revisionInfosToSave and max(revisionInfosToSave, key=lambda info: not info[0])[0]:
                    revisionInfosToRemove += revisionInfosToSave

                if revisionInfosToRemove:
                    revisionsToRemove = [revisionInfoToRemove[1] for revisionInfoToRemove in revisionInfosToRemove]
                    yield Delete(
                        aboMembers,
                        Where=(aboMembers.GROUP_ID == groupID).And(
                            aboMembers.MEMBER_ID == memberID).And(
                            aboMembers.REVISION.In(Parameter("revisionsToRemove", len(revisionsToRemove)))
                        )
                    ).on(self, revisionsToRemove=revisionsToRemove)

    @classproperty
    def _inboxItemsInHomeIDCreatedBeforeCutoffQuery(cls):
        """
        DAL query to select inbox items created before a given date.
        """
        co = schema.CALENDAR_OBJECT
        cb = schema.CALENDAR_BIND
        return Select(
            [co.RESOURCE_NAME],
            From=co.join(cb),
            Where=(
                cb.CALENDAR_HOME_RESOURCE_ID == Parameter("homeID")).And(
                cb.CALENDAR_RESOURCE_ID == co.CALENDAR_RESOURCE_ID).And(
                cb.BIND_MODE == _BIND_MODE_OWN).And(
                cb.CALENDAR_RESOURCE_NAME == 'inbox').And(
                co.CREATED < Parameter("cutoff")),
        )

    @inlineCallbacks
    def listInboxItemsInHomeCreatedBefore(self, homeID, cutoff):
        """
        return a list of inbox item names that created before a given date in a
        given home.

        Returns a deferred to a list of orphaned inbox item names
        """
        rows = yield self._inboxItemsInHomeIDCreatedBeforeCutoffQuery.on(
            self, homeID=homeID, cutoff=cutoff)
        names = [row[0] for row in rows]
        returnValue(names)


class CommonHome(SharingHomeMixIn):
    log = Logger()

    # All these need to be initialized by derived classes for each store type
    _homeType = None
    _homeSchema = None
    _homeMetaDataSchema = None

    _externalClass = None
    _childClass = None
    _trashClass = None

    _bindSchema = None
    _revisionsSchema = None
    _objectSchema = None

    _notifierPrefix = None

    _dataVersionKey = None
    _dataVersionValue = None

    @classmethod
    def makeClass(cls, transaction, homeData, authzUID=None):
        """
        Build the actual home class taking into account the possibility that we might need to
        switch in the external version of the class.

        @param transaction: transaction
        @type transaction: L{CommonStoreTransaction}
        @param homeData: home table column data
        @type homeData: C{list}
        """

        status = homeData[cls.homeColumns().index(cls._homeSchema.STATUS)]
        if status == _HOME_STATUS_EXTERNAL:
            home = cls._externalClass(transaction, homeData)
        else:
            home = cls(transaction, homeData, authzUID=authzUID)
        return home.initFromStore()

    def __init__(self, transaction, homeData, authzUID=None):
        self._txn = transaction

        for attr, value in zip(self.homeAttributes(), homeData):
            setattr(self, attr, value)

        self._authzUID = authzUID
        if self._authzUID is None:
            if self._txn._authz_uid is not None:
                self._authzUID = self._txn._authz_uid
            else:
                self._authzUID = self._ownerUID
        self._dataVersion = None
        self._childrenLoaded = False
        self._children = defaultdict(dict)
        self._notifiers = None
        self._quotaUsedBytes = None
        self._created = None
        self._modified = None
        self._syncTokenRevision = None

        # This is used to track whether the originating request is from the store associated
        # by the transaction, or from a remote store. We need to be able to distinguish store
        # objects that are locally hosted (_HOME_STATUS_NORMAL) or remotely hosted
        # (_HOME_STATUS_EXTERNAL). For the later we need to know whether the object is being
        # accessed from the local store (in which case requests for child objects etc will be
        # directed at a remote store) or whether it is being accessed as the result of a remote
        # request (in which case requests for child objects etc will be directed at the local store).
        self._internalRequest = True

    @classmethod
    def _register(cls, homeType):
        """
        Register a L{CommonHome} subclass as its respective home type constant
        with L{CommonStoreTransaction}.
        """
        cls._homeType = homeType
        CommonStoreTransaction._homeClass[cls._homeType] = cls

    def quotaAllowedBytes(self):
        return self._txn.store().quota

    @classproperty
    def _homeColumnsFromOwnerQuery(cls):
        home = cls._homeSchema
        return Select(
            cls.homeColumns(),
            From=home,
            Where=(home.OWNER_UID == Parameter("ownerUID")).And(
                home.STATUS == Parameter("status")
            )
        )

    @classproperty
    def _ownerFromResourceID(cls):
        home = cls._homeSchema
        return Select([home.OWNER_UID, home.STATUS],
                      From=home,
                      Where=home.RESOURCE_ID == Parameter("resourceID"))

    @classproperty
    def _metaDataQuery(cls):
        metadata = cls._homeMetaDataSchema
        return Select(cls.metadataColumns(),
                      From=metadata,
                      Where=metadata.RESOURCE_ID == Parameter("resourceID"))

    @classmethod
    def homeColumns(cls):
        """
        Return a list of column names to retrieve when doing an ownerUID->home lookup.
        """

        # Common behavior is to have created and modified

        return (
            cls._homeSchema.RESOURCE_ID,
            cls._homeSchema.OWNER_UID,
            cls._homeSchema.STATUS,
        )

    @classmethod
    def homeAttributes(cls):
        """
        Return a list of attributes names to map L{homeColumns} to.
        """

        # Common behavior is to have created and modified

        return (
            "_resourceID",
            "_ownerUID",
            "_status",
        )

    @classmethod
    def metadataColumns(cls):
        """
        Return a list of column name for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            cls._homeMetaDataSchema.CREATED,
            cls._homeMetaDataSchema.MODIFIED,
        )

    @classmethod
    def metadataAttributes(cls):
        """
        Return a list of attribute names for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            "_created",
            "_modified",
        )

    @inlineCallbacks
    def initFromStore(self):
        """
        Initialize this object from the store. We read in and cache all the
        extra meta-data from the DB to avoid having to do DB queries for those
        individually later.
        """

        yield self.initMetaDataFromStore()
        yield self._loadPropertyStore()

        for factory_type, factory in self._txn._notifierFactories.items():
            self.addNotifier(factory_type, factory.newNotifier(self))

        returnValue(self)

    @inlineCallbacks
    def initMetaDataFromStore(self):
        """
        Load up the metadata and property store
        """

        queryCacher = self._txn._queryCacher
        if queryCacher:
            # Get cached copy
            cacheKey = queryCacher.keyForHomeMetaData(self._resourceID)
            data = yield queryCacher.get(cacheKey)
        else:
            data = None
        if data is None:
            # Don't have a cached copy
            data = (yield self._metaDataQuery.on(self._txn, resourceID=self._resourceID))[0]
            if queryCacher:
                # Cache the data
                yield queryCacher.setAfterCommit(self._txn, cacheKey, data)

        for attr, value in zip(self.metadataAttributes(), data):
            setattr(self, attr, value)
        self._created = parseSQLTimestamp(self._created)
        self._modified = parseSQLTimestamp(self._modified)

    def serialize(self):
        """
        Create a dictionary mapping metadata attributes so this object can be sent over a cross-pod call
        and reconstituted at the other end. Note that the other end may have a different schema so
        the attributes may not match exactly and will need to be processed accordingly.
        """
        data = dict([(attr[1:], getattr(self, attr, None)) for attr in self.metadataAttributes()])
        data["created"] = data["created"].isoformat(" ")
        data["modified"] = data["modified"].isoformat(" ")
        return data

    def deserialize(self, mapping):
        """
        Given a mapping generated by L{serialize}, convert the values to attributes on this object.
        """

        for attr in self.metadataAttributes():
            setattr(self, attr, mapping.get(attr[1:]))
        self._created = parseSQLTimestamp(self._created)
        self._modified = parseSQLTimestamp(self._modified)

    @classmethod
    @inlineCallbacks
    def listHomes(cls, txn):
        """
        Retrieve the owner UIDs of all existing homes.

        @return: an iterable of C{str}s.
        """
        rows = yield Select(
            [cls._homeSchema.OWNER_UID],
            From=cls._homeSchema,
        ).on(txn)
        rids = [row[0] for row in rows]
        returnValue(rids)

    @classmethod
    def homeWithUID(cls, txn, uid, status=None, create=False, authzUID=None):
        return cls.homeWith(txn, None, uid, status, create=create, authzUID=authzUID)

    @classmethod
    def homeWithResourceID(cls, txn, rid):
        return cls.homeWith(txn, rid, None)

    @classmethod
    @inlineCallbacks
    def homeWith(cls, txn, rid, uid, status=None, create=False, authzUID=None):
        """
        Lookup or create a home based in either its resource id or uid. If a status is given,
        return only the one matching that status. If status is L{None} we lookup any regular
        status type (normal, external or purging). When creating with status L{None} we create
        one with a status matching the current directory record thisServer() value. The only
        other status that can be directly created is migrating.
        """

        # Setup the SQL query and query cacher keys
        queryCacher = txn._queryCacher
        cacheKeys = []
        if rid is not None:
            query = cls._homeSchema.RESOURCE_ID == rid
            if queryCacher:
                cacheKeys.append(queryCacher.keyForHomeWithID(cls._homeType, rid, status))
        elif uid is not None:
            query = cls._homeSchema.OWNER_UID == uid
            if status is not None:
                query = query.And(cls._homeSchema.STATUS == status)
                if queryCacher:
                    cacheKeys.append(queryCacher.keyForHomeWithUID(cls._homeType, uid, status))
            else:
                statusSet = (_HOME_STATUS_NORMAL, _HOME_STATUS_EXTERNAL, _HOME_STATUS_PURGING)
                if txn._allowDisabled:
                    statusSet += (_HOME_STATUS_DISABLED,)
                query = query.And(cls._homeSchema.STATUS.In(statusSet))
                if queryCacher:
                    for item in statusSet:
                        cacheKeys.append(queryCacher.keyForHomeWithUID(cls._homeType, uid, item))
        else:
            raise AssertionError("One of rid or uid must be set")

        # Try to fetch a result from the query cache first
        for cacheKey in cacheKeys:
            result = (yield queryCacher.get(cacheKey))
            if result is not None:
                break
        else:
            result = None

        # If nothing in the cache, do the SQL query and cache the result
        if result is None:
            results = yield Select(
                cls.homeColumns(),
                From=cls._homeSchema,
                Where=query,
            ).on(txn)

            if len(results) > 1:
                # Pick the best one in order: normal, disabled and external
                byStatus = dict([(item[cls.homeColumns().index(cls._homeSchema.STATUS)], item) for item in results])
                result = byStatus.get(_HOME_STATUS_NORMAL)
                if result is None:
                    result = byStatus.get(_HOME_STATUS_DISABLED)
                if result is None:
                    result = byStatus.get(_HOME_STATUS_EXTERNAL)
            elif results:
                result = results[0]
            else:
                result = None

            if result and queryCacher:
                if rid is not None:
                    cacheKey = cacheKeys[0]
                elif uid is not None:
                    cacheKey = queryCacher.keyForHomeWithUID(cls._homeType, uid, result[cls.homeColumns().index(cls._homeSchema.STATUS)])
                yield queryCacher.set(cacheKey, result)

        if result:
            # Return object that already exists in the store
            homeObject = yield cls.makeClass(txn, result, authzUID=authzUID)
            returnValue(homeObject)
        else:
            # Can only create when uid is specified
            if not create or uid is None:
                returnValue(None)

            # Determine if the user is local or external
            record = yield txn.directoryService().recordWithUID(uid.decode("utf-8"))
            if record is None:
                raise DirectoryRecordNotFoundError("Cannot create home for UID since no directory record exists: {}".format(uid))

            if status is None:
                createStatus = _HOME_STATUS_NORMAL if record.thisServer() else _HOME_STATUS_EXTERNAL
                if record.server() is not None and record.server().v5:
                    raise RecordNotAllowedError("Cannot create external home for user on v5 pod: {}".format(uid))
            elif status == _HOME_STATUS_MIGRATING:
                if record.thisServer():
                    raise RecordNotAllowedError("Cannot migrate user data for a user already hosted on this server")
                createStatus = status
            elif status in (_HOME_STATUS_NORMAL, _HOME_STATUS_EXTERNAL,):
                createStatus = status
            else:
                raise RecordNotAllowedError("Cannot create home with status {}: {}".format(status, uid))

            # Use savepoint so we can do a partial rollback if there is a race condition
            # where this row has already been inserted
            savepoint = SavepointAction("homeWithUID")
            yield savepoint.acquire(txn)

            if cls._dataVersionValue is None:
                cls._dataVersionValue = yield txn.calendarserverValue(cls._dataVersionKey)
            try:
                resourceid = (yield Insert(
                    {
                        cls._homeSchema.OWNER_UID: uid,
                        cls._homeSchema.STATUS: createStatus,
                        cls._homeSchema.DATAVERSION: cls._dataVersionValue,
                    },
                    Return=cls._homeSchema.RESOURCE_ID
                ).on(txn))[0][0]
                yield Insert({cls._homeMetaDataSchema.RESOURCE_ID: resourceid}).on(txn)
            except Exception:  # FIXME: Really want to trap the pg.DatabaseError but in a non-DB specific manner
                yield savepoint.rollback(txn)

                # Retry the query - row may exist now, if not re-raise
                results = yield Select(
                    cls.homeColumns(),
                    From=cls._homeSchema,
                    Where=query,
                ).on(txn)
                if results:
                    homeObject = yield cls.makeClass(txn, results[0], authzUID=authzUID)
                    returnValue(homeObject)
                else:
                    raise
            else:
                yield savepoint.release(txn)

                # Note that we must not cache the owner_uid->resource_id
                # mapping in the query cacher when creating as we don't want that to appear
                # until AFTER the commit
                results = yield Select(
                    cls.homeColumns(),
                    From=cls._homeSchema,
                    Where=cls._homeSchema.RESOURCE_ID == resourceid,
                ).on(txn)
                homeObject = yield cls.makeClass(txn, results[0], authzUID=authzUID)
                if homeObject.normal():
                    yield homeObject.createdHome()
                returnValue(homeObject)

    def __repr__(self):
        return "<%s: %s, %s>" % (self.__class__.__name__, self._resourceID, self._ownerUID)

    def cacheKey(self):
        return "{}-{}".format(self._status, self._ownerUID)

    def id(self):
        """
        Retrieve the store identifier for this home.

        @return: store identifier.
        @rtype: C{int}
        """
        return self._resourceID

    def uid(self):
        """
        Retrieve the unique identifier for this home.

        @return: a string.
        """
        return self._ownerUID

    def authzuid(self):
        """
        Retrieve the unique identifier of the user accessing the data in this home.

        @return: a string.
        """
        return self._authzUID

    def status(self):
        return self._status

    def normal(self):
        """
        Is this an normal (internal) home.

        @return: a L{bool}.
        """
        return self._status == _HOME_STATUS_NORMAL

    def external(self):
        """
        Is this an external home.

        @return: a L{bool}.
        """
        return self._status == _HOME_STATUS_EXTERNAL

    def externalClass(self):
        """
        Is this an external home which also needs to have any requests directed
        to a remote pod via the external (conduit using) implementation of this
        class

        @return: a L{bool}.
        """
        return self._status == _HOME_STATUS_EXTERNAL and self._internalRequest

    def purging(self):
        """
        Is this an external home.

        @return: a string.
        """
        return self._status == _HOME_STATUS_PURGING

    def migrating(self):
        """
        Is this an external home.

        @return: a string.
        """
        return self._status == _HOME_STATUS_MIGRATING

    def purge(self):
        """
        Mark this home as being purged.
        """
        return self.setStatus(_HOME_STATUS_PURGING)

    def migrate(self):
        """
        Mark this home as being purged.
        """
        return self.setStatus(_HOME_STATUS_MIGRATING)

    @inlineCallbacks
    def setStatus(self, newStatus):
        """
        Mark this home as being purged.
        """
        # Only if different
        if self._status != newStatus:
            yield Update(
                {self._homeSchema.STATUS: newStatus},
                Where=(self._homeSchema.RESOURCE_ID == self._resourceID),
            ).on(self._txn)
            if self._txn._queryCacher:
                yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithUID(
                    self._homeType,
                    self.uid(),
                    self._status,
                ))
                yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithID(
                    self._homeType,
                    self.id(),
                    self._status,
                ))
            self._status = newStatus

    @inlineCallbacks
    def remove(self):

        # Removing the home table entry does NOT remove the child class entry - it does remove
        # the associated bind entry. So manually remove each child.
        yield self.removeAllChildren()

        r = self._childClass._revisionsSchema
        yield Delete(
            From=r,
            Where=r.HOME_RESOURCE_ID == self._resourceID,
        ).on(self._txn)

        h = self._homeSchema
        yield Delete(
            From=h,
            Where=h.RESOURCE_ID == self._resourceID,
        ).on(self._txn)

        yield self.properties()._removeResource()

        if self._txn._queryCacher:
            yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithUID(
                self._homeType,
                self.uid(),
                self._status,
            ))
            yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithID(
                self._homeType,
                self.id(),
                self._status,
            ))

    @inlineCallbacks
    def removeAllChildren(self):
        """
        Remove each child.
        """

        children = yield self.loadChildren()
        for child in children:
            yield child.remove()
            self._children.pop(child.name(), None)
            self._children.pop(child.id(), None)

    @inlineCallbacks
    def purgeAll(self):
        """
        Do a complete purge of all data associated with this calendar home. For now this will assume
        a "silent" non-implicit behavior. In the future we will want to build in some of the options
        the current set of "purge" CLI tools have to allow for cancels of future events etc.
        """

        # Removing the home table entry does NOT remove the child class entry - it does remove
        # the associated bind entry. So manually remove each child.
        yield self.purgeAllChildren()

        r = self._childClass._revisionsSchema
        yield Delete(
            From=r,
            Where=r.HOME_RESOURCE_ID == self._resourceID,
        ).on(self._txn)

        h = self._homeSchema
        yield Delete(
            From=h,
            Where=h.RESOURCE_ID == self._resourceID,
        ).on(self._txn)

        yield self.properties()._removeResource()

        if self._txn._queryCacher:
            yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithUID(
                self._homeType,
                self.uid(),
                self._status,
            ))
            yield self._txn._queryCacher.delete(self._txn._queryCacher.keyForHomeWithID(
                self._homeType,
                self.id(),
                self._status,
            ))

    @inlineCallbacks
    def purgeAllChildren(self):
        """
        Purge each child (non-implicit).
        """

        children = yield self.loadChildren()
        for child in children:
            yield child.unshare()
            if child.owned():
                yield child.purge()
            self._children.pop(child.name(), None)
            self._children.pop(child.id(), None)

        yield self.removeUnacceptedShares()

    def transaction(self):
        return self._txn

    def directoryService(self):
        return self._txn.store().directoryService()

    def directoryRecord(self):
        return self.directoryService().recordWithUID(self.uid().decode("utf-8"))

    @inlineCallbacks
    def invalidateQueryCache(self):
        queryCacher = self._txn._queryCacher
        if queryCacher is not None:
            cacheKey = queryCacher.keyForHomeMetaData(self._resourceID)
            yield queryCacher.invalidateAfterCommit(self._txn, cacheKey)

    @classproperty
    def _dataVersionQuery(cls):
        ch = cls._homeSchema
        return Select(
            [ch.DATAVERSION], From=ch,
            Where=ch.RESOURCE_ID == Parameter("resourceID")
        )

    @inlineCallbacks
    def dataVersion(self):
        if self._dataVersion is None:
            self._dataVersion = (yield self._dataVersionQuery.on(
                self._txn, resourceID=self._resourceID))[0][0]
        returnValue(self._dataVersion)

    def name(self):
        """
        Implement L{IDataStoreObject.name} to return the uid.
        """
        return self.uid()

    @inlineCallbacks
    def children(self, onlyInTrash=False):
        """
        Retrieve children contained in this home.
        """
        x = []
        names = yield self.listChildren(onlyInTrash=onlyInTrash)
        for name in names:
            x.append((yield self.childWithName(name, onlyInTrash=onlyInTrash)))
        returnValue(x)

    def _childrenKey(self, onlyInTrash):
        return "TRASHED" if onlyInTrash else "NOTTRASHED"

    @inlineCallbacks
    def loadChildren(self):
        """
        Load and cache all children - Depth:1 optimization
        """
        results = (yield self._childClass.loadAllObjects(self))
        for result in results:
            if not config.ExposeTrashCollection:
                if result.isTrash():
                    continue
            key = self._childrenKey(result.isInTrash())
            if result.name() not in self._children[key]:
                self._children[key][result.name()] = result
                self._children[key][result._resourceID] = result
        self._childrenLoaded = True
        returnValue(results)

    @inlineCallbacks
    def listChildren(self, onlyInTrash=False):
        """
        Retrieve the names of the children in this home.

        @return: an iterable of C{str}s.
        """

        if not self._childrenLoaded:
            yield self.loadChildren()
        names = [k for k in self._children[self._childrenKey(onlyInTrash)].keys() if not isinstance(k, int)]
        returnValue(names)

    @inlineCallbacks
    def childWithName(self, name, onlyInTrash=False):
        """
        Retrieve the child with the given C{name} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        childrenKey = self._childrenKey(onlyInTrash)
        if name not in self._children[childrenKey]:
            child = yield self._childClass.objectWithName(self, name, onlyInTrash=onlyInTrash)
            if child is not None:
                self._children[childrenKey][name] = child
                self._children[childrenKey][child.id()] = child
        returnValue(self._children[childrenKey].get(name, None))

    def anyObjectWithShareUID(self, shareUID, onlyInTrash=False):
        """
        Retrieve the child accepted or otherwise with the given bind identifier contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        return self._childClass.objectWithName(self, shareUID, accepted=None, onlyInTrash=onlyInTrash)

    @inlineCallbacks
    def childWithID(self, resourceID, onlyInTrash=False):
        """
        Retrieve the child with the given C{resourceID} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        childrenKey = self._childrenKey(onlyInTrash)
        if resourceID not in self._children[childrenKey]:
            child = yield self._childClass.objectWithID(self, resourceID, onlyInTrash=onlyInTrash)
            if child is not None:
                self._children[childrenKey][resourceID] = child
                self._children[childrenKey][child.name()] = child
        returnValue(self._children[childrenKey].get(resourceID, None))

    def childWithBindUID(self, bindUID, onlyInTrash=False):
        """
        Retrieve the child with the given C{bindUID} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        return self._childClass.objectWithBindUID(self, bindUID, onlyInTrash=onlyInTrash)

    def allChildWithID(self, resourceID, onlyInTrash=False):
        """
        Retrieve the child with the given C{resourceID} contained in this
        home.

        @param name: a string.
        @return: an L{ICalendar} or C{None} if no such child exists.
        """
        return self._childClass.objectWithID(self, resourceID, accepted=None, onlyInTrash=onlyInTrash)

    @inlineCallbacks
    def createChildWithName(self, name, bindUID=None):
        if name.startswith("."):
            raise HomeChildNameNotAllowedError(name)

        child = yield self._childClass.create(self, name, bindUID=bindUID)
        if child is not None:
            key = self._childrenKey(False)
            self._children[key][name] = child
            self._children[key][child.id()] = child
        returnValue(child)

    @inlineCallbacks
    def removeChildWithName(self, name, useTrash=True):
        child = yield self.childWithName(name)
        if child is None:
            raise NoSuchHomeChildError()
        key = self._childrenKey(child.isInTrash())
        resourceID = child._resourceID

        if useTrash:
            yield child.remove()
        else:
            yield child.purge()

        self._children[key].pop(name, None)
        self._children[key].pop(resourceID, None)

    @inlineCallbacks
    def getTrash(self, create=False):
        child = None
        if hasattr(self, "_trashObject"):
            child = self._trashObject
        elif hasattr(self, "_trash"):
            if self._trash:
                child = yield self._childClass.objectWithID(self, self._trash)
                self._trashObject = child
            elif create:
                schema = self._homeMetaDataSchema

                # Use a lock to prevent others from creating at the same time
                yield Select(
                    From=schema,
                    Where=(schema.RESOURCE_ID == self.id()),
                    ForUpdate=True,
                ).on(self._txn)

                # Re-check to see if someone else created it whilst we were trying to lock
                self._trash = (yield Select(
                    [schema.TRASH],
                    From=schema,
                    Where=(schema.RESOURCE_ID == self.id()),
                ).on(self._txn))[0][0]
                if self._trash:
                    child = yield self._childClass.objectWithID(self, self._trash)
                else:
                    child = yield self._trashClass.create(self, str(uuid4()))
                    self._trash = child.id()
                    schema = self._homeMetaDataSchema
                    yield Update(
                        {schema.TRASH: self._trash},
                        Where=(schema.RESOURCE_ID == self.id())
                    ).on(self._txn)
                self._trashObject = child
        returnValue(child)

    @classproperty
    def _syncTokenQuery(cls):
        """
        DAL Select statement to find the sync token.

        This is the max(REVISION) from the union of:

        1) REVISION's for all object resources in all home child collections in the targeted home
        2) REVISION's for all child collections in the targeted home

        Note the later is needed to track changes directly to the home child themselves (e.g.
        property changes, deletion etc).
        """
        rev = cls._revisionsSchema
        bind = cls._bindSchema
        return Select(
            [Max(rev.REVISION)],
            From=Select(
                [rev.REVISION],
                From=rev,
                Where=(
                    rev.RESOURCE_ID.In(
                        Select(
                            [bind.RESOURCE_ID],
                            From=bind,
                            Where=bind.HOME_RESOURCE_ID == Parameter("resourceID"),
                        )
                    )
                ),
                SetExpression=Union(
                    Select(
                        [rev.REVISION],
                        From=rev,
                        Where=(rev.HOME_RESOURCE_ID == Parameter("resourceID")).And(rev.RESOURCE_ID == None),
                    ),
                    optype=Union.OPTYPE_ALL,
                )
            ),
        )

    def revisionFromToken(self, token):
        if token is None:
            return 0
        elif isinstance(token, str):
            _ignore_uuid, revision = token.split("_", 1)
            return int(revision)
        else:
            return token

    @inlineCallbacks
    def syncToken(self):
        """
        Return the current sync token for the home. This is an aggregate of sync tokens for child
        collections. We will cache this value to avoid doing the query more than necessary. Care must be
        taken to invalid the cached value properly.
        """
        if self._syncTokenRevision is None:
            self._syncTokenRevision = yield self.syncTokenRevision()
        returnValue("%s_%s" % (self._resourceID, self._syncTokenRevision))

    @inlineCallbacks
    def syncTokenRevision(self):
        revision = (yield self._syncTokenQuery.on(self._txn, resourceID=self._resourceID))[0][0]
        if revision is None:
            revision = int((yield self._txn.calendarserverValue("MIN-VALID-REVISION")))
        returnValue(revision)

    @classproperty
    def _changesQuery(cls):
        bind = cls._bindSchema
        rev = cls._revisionsSchema
        return Select(
            [
                bind.RESOURCE_NAME,
                rev.COLLECTION_NAME,
                rev.RESOURCE_NAME,
                rev.DELETED,
            ],
            From=rev.join(
                bind,
                (bind.HOME_RESOURCE_ID == Parameter("resourceID")).And
                (rev.RESOURCE_ID == bind.RESOURCE_ID),
                'left outer'
            ),
            Where=(rev.REVISION > Parameter("revision")).And
                  (rev.HOME_RESOURCE_ID == Parameter("resourceID"))
        )

    @inlineCallbacks
    def doChangesQuery(self, revision):
        """
            Do the changes query.
            Subclasses may override.
        """
        result = yield self._changesQuery.on(
            self._txn,
            resourceID=self._resourceID,
            revision=revision)
        returnValue(result)

    def resourceNamesSinceToken(self, token, depth):
        """
        Return the changed and deleted resources since a particular sync-token. This simply extracts
        the revision from from the token then calls L{resourceNamesSinceRevision}.

        @param revision: the revision to determine changes since
        @type revision: C{int}
        """

        return self.resourceNamesSinceRevision(self.revisionFromToken(token), depth)

    @inlineCallbacks
    def resourceNamesSinceRevision(self, revision, depth):
        """
        Determine the list of child resources that have changed since the specified sync revision.
        We do the same SQL query for both depth "1" and "infinity", but filter the results for
        "1" to only account for a collection change.

        Now that we are truncating the revision table, we need to handle the full sync (revision == 0)
        case a little differently as the revision table will not contain data for resources that exist,
        but were last modified before the revision cut-off. Instead for revision == 0 we need to list
        all existing child resources.

        We need to handle shared collection a little differently from owned ones. When a shared collection
        is bound into a home we record a revision for it using the sharee home id and sharee collection name.
        That revision is the "starting point" for changes: so if sync occurs with a revision earlier than
        that, we return the list of all resources in the shared collection since they are all "new" as far
        as the client is concerned since the shared collection has just appeared. For a later revision, we
        just report the changes since that one. When a shared collection is removed from a home, we again
        record a revision for the sharee home and sharee collection name with the "deleted" flag set. That way
        the shared collection can be reported as removed.

        For external shared collections we need to report them as invalid as we cannot aggregate the sync token
        for this home with the sync token from the external share which is under the control of the other pod.
        Reporting it as invalid means that clients should do requests directly on the share itself to sync it.

        @param revision: the sync revision to compare to
        @type revision: C{str}
        @param depth: depth for determine what changed
        @type depth: C{str}
        """

        if revision:
            minValidRevision = yield self._txn.calendarserverValue("MIN-VALID-REVISION")
            if revision < int(minValidRevision):
                raise SyncTokenValidException
        else:
            results = yield self.resourceNamesSinceRevisionZero(depth)
            returnValue(results)

        # Use revision table to find changes since the last revision - this will not include
        # changes to child resources of shared collections - those we will get later
        results = [
            (
                path if path else (collection if collection else ""),
                name if name else "",
                wasdeleted
            )
            for path, collection, name, wasdeleted in
            (yield self.doChangesQuery(revision))
        ]

        if not config.ExposeTrashCollection:
            trash = yield self.getTrash(create=False)
            trashName = trash.name() if trash else None
        else:
            trashName = None

        changed = set()
        deleted = set()
        invalid = set()
        deleted_collections = set()
        for path, name, wasdeleted in results:

            # Don't report the trash if it is hidden
            if trashName and path == trashName:
                continue

            if wasdeleted:
                if name:
                    # Resource deleted - for depth "1" report collection as changed,
                    # otherwise report resource as deleted
                    if depth == "1":
                        changed.add("%s/" % (path,))
                    else:
                        deleted.add("%s/%s" % (path, name,))
                else:
                    # Collection was deleted
                    deleted.add("%s/" % (path,))
                    deleted_collections.add(path)

            if path not in deleted_collections:
                # Always report collection as changed
                changed.add("%s/" % (path,))

                # Resource changed - for depth "infinity" report resource as changed
                if name and depth != "1":
                    changed.add("%s/%s" % (path, name,))

        # Now deal with existing shared collections
        # TODO: think about whether this can be done in one query rather than looping over each share
        for share in (yield self.children()):
            if not share.owned():
                sharedChanged, sharedDeleted, sharedInvalid = yield share.sharedChildResourceNamesSinceRevision(revision, depth)
                changed |= sharedChanged
                changed -= sharedInvalid
                deleted |= sharedDeleted
                deleted -= sharedInvalid
                invalid |= sharedInvalid

        changed = sorted(changed)
        deleted = sorted(deleted)
        invalid = sorted(invalid)
        returnValue((changed, deleted, invalid,))

    @inlineCallbacks
    def resourceNamesSinceRevisionZero(self, depth):
        """
        Revision == 0 specialization of L{resourceNamesSinceRevision} .

        @param depth: depth for determine what changed
        @type depth: C{str}
        """

        # Scan each child
        changed = set()
        deleted = set()
        invalid = set()
        for child in (yield self.children()):
            if child.owned():
                path = child.name()
                # Always report collection as changed
                changed.add("%s/" % (path,))

                # Resource changed - for depth "infinity" report resource as changed
                if depth != "1":
                    for name in (yield child.listObjectResources()):
                        changed.add("%s/%s" % (path, name,))
            else:
                sharedChanged, sharedDeleted, sharedInvalid = yield child.sharedChildResourceNamesSinceRevisionZero(depth)
                changed |= sharedChanged
                changed -= sharedInvalid
                deleted |= sharedDeleted
                deleted -= sharedInvalid
                invalid |= sharedInvalid

        changed = sorted(changed)
        deleted = sorted(deleted)
        invalid = sorted(invalid)
        returnValue((changed, deleted, invalid,))

    @inlineCallbacks
    def _loadPropertyStore(self):

        # Use any authz uid in place of the viewer uid so delegates have their own
        # set of properties
        props = yield PropertyStore.load(
            self.uid(),
            self.uid(),
            self.authzuid(),
            self._txn,
            self._resourceID,
            notifyCallback=self.notifyChanged
        )
        self._propertyStore = props

    def properties(self):
        return self._propertyStore

    # IDataStoreObject
    def contentType(self):
        """
        The content type of objects
        """
        return None

    def md5(self):
        return None

    def size(self):
        return 0

    def created(self):
        return datetimeMktime(self._created) if self._created else None

    def modified(self):
        return datetimeMktime(self._modified) if self._modified else None

    @classmethod
    def _objectResourceQuery(cls, checkBindMode):
        obj = cls._objectSchema
        bind = cls._bindSchema
        where = ((obj.UID == Parameter("uid"))
                 .And(bind.HOME_RESOURCE_ID == Parameter("resourceID")))
        if checkBindMode:
            where = where.And(bind.BIND_MODE == Parameter("bindMode"))
        return Select(
            [obj.PARENT_RESOURCE_ID, obj.RESOURCE_ID],
            From=obj.join(bind, obj.PARENT_RESOURCE_ID == bind.RESOURCE_ID),
            Where=where
        )

    @classproperty
    def _resourceByUIDQuery(cls):
        return cls._objectResourceQuery(checkBindMode=False)

    @classproperty
    def _resourceByUIDBindQuery(cls):
        return cls._objectResourceQuery(checkBindMode=True)

    @inlineCallbacks
    def objectResourcesWithUID(self, uid, ignore_children=[], allowShared=True):
        """
        Return all child object resources with the specified UID, ignoring any
        in the named child collections.
        """
        results = []
        if allowShared:
            rows = (yield self._resourceByUIDQuery.on(
                self._txn, uid=uid, resourceID=self._resourceID
            ))
        else:
            rows = (yield self._resourceByUIDBindQuery.on(
                self._txn, uid=uid, resourceID=self._resourceID,
                bindMode=_BIND_MODE_OWN
            ))
        if rows:
            for childID, objectID in rows:
                child = (yield self.childWithID(childID))
                if child and child.name() not in ignore_children:
                    objectResource = (
                        yield child.objectResourceWithID(objectID)
                    )
                    results.append(objectResource)

        returnValue(results)

    @classmethod
    def _objectResourceIDQuery(cls):
        obj = cls._objectSchema
        return Select(
            [obj.PARENT_RESOURCE_ID],
            From=obj,
            Where=(obj.RESOURCE_ID == Parameter("resourceID")),
        )

    @inlineCallbacks
    def objectResourceWithID(self, rid):
        """
        Return all child object resources with the specified resource-ID.
        """
        rows = (yield self._objectResourceIDQuery().on(
            self._txn, resourceID=rid
        ))
        if rows and len(rows) == 1:
            child = (yield self.childWithID(rows[0][0]))
            objectResource = (
                yield child.objectResourceWithID(rid)
            )
            returnValue(objectResource)

        returnValue(None)

    @classproperty
    def _quotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Select(
            [meta.QUOTA_USED_BYTES], From=meta,
            Where=meta.RESOURCE_ID == Parameter("resourceID")
        )

    @inlineCallbacks
    def quotaUsedBytes(self):
        if self._quotaUsedBytes is None:
            self._quotaUsedBytes = (yield self._quotaQuery.on(
                self._txn, resourceID=self._resourceID))[0][0]
        returnValue(self._quotaUsedBytes)

    @classproperty
    def _preLockResourceIDQuery(cls):
        meta = cls._homeMetaDataSchema
        return Select(From=meta,
                      Where=meta.RESOURCE_ID == Parameter("resourceID"),
                      ForUpdate=True)

    @classproperty
    def _increaseQuotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Update({meta.QUOTA_USED_BYTES: meta.QUOTA_USED_BYTES +
                       Parameter("delta")},
                      Where=meta.RESOURCE_ID == Parameter("resourceID"),
                      Return=meta.QUOTA_USED_BYTES)

    @classproperty
    def _resetQuotaQuery(cls):
        meta = cls._homeMetaDataSchema
        return Update({meta.QUOTA_USED_BYTES: 0},
                      Where=meta.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def adjustQuotaUsedBytes(self, delta):
        """
        Adjust quota used. We need to get a lock on the row first so that the
        adjustment is done atomically. It is import to do the 'select ... for
        update' because a race also exists in the 'update ... x = x + 1' case
        as seen via unit tests.
        """
        yield self._preLockResourceIDQuery.on(self._txn,
                                              resourceID=self._resourceID)

        self._quotaUsedBytes = (yield self._increaseQuotaQuery.on(
            self._txn, delta=delta, resourceID=self._resourceID))[0][0]

        # Double check integrity
        if self._quotaUsedBytes < 0:
            log.error(
                "Fixing quota adjusted below zero to {used} by change amount "
                "{delta}",
                used=self._quotaUsedBytes, delta=delta
            )
            yield self._resetQuotaQuery.on(self._txn,
                                           resourceID=self._resourceID)
            self._quotaUsedBytes = 0

    def addNotifier(self, factory_name, notifier):
        if self._notifiers is None:
            self._notifiers = {}
        self._notifiers[factory_name] = notifier

    def getNotifier(self, factory_name):
        return self._notifiers.get(factory_name) if self._notifiers else None

    def notifierID(self):
        return (self._notifierPrefix, self.uid(),)

    @inlineCallbacks
    def notifyChanged(self, category=ChangeCategory.default):
        """
        Send notifications, change sync token and bump last modified because
        the resource has changed.  We ensure we only do this once per object
        per transaction.
        """

        if self._txn.isNotifiedAlready(self):
            returnValue(None)
        self._txn.notificationAddedForObject(self)

        # Update modified if object still exists
        if self._resourceID:
            yield self.bumpModified()

        # Send notifications
        if self._notifiers:
            # cache notifiers run in post commit
            notifier = self._notifiers.get("cache", None)
            if notifier:
                self._txn.postCommit(notifier.notify)
            # push notifiers add their work items immediately
            notifier = self._notifiers.get("push", None)
            if notifier:
                yield notifier.notify(self._txn, priority=category.value)

    @classproperty
    def _lockLastModifiedQuery(cls):
        meta = cls._homeMetaDataSchema
        return Select(
            From=meta,
            Where=meta.RESOURCE_ID == Parameter("resourceID"),
            ForUpdate=True,
            NoWait=True
        )

    @classproperty
    def _changeLastModifiedQuery(cls):
        meta = cls._homeMetaDataSchema
        return Update({meta.MODIFIED: utcNowSQL},
                      Where=meta.RESOURCE_ID == Parameter("resourceID"),
                      Return=meta.MODIFIED)

    @inlineCallbacks
    def bumpModified(self):
        """
        Bump the MODIFIED value. A possible deadlock could happen here if two
        or more simultaneous changes are happening. In that case it is OK for
        the MODIFIED change to fail so long as at least one works. We will use
        SAVEPOINT logic to handle ignoring the deadlock error. We use SELECT
        FOR UPDATE NOWAIT to ensure we do not delay the transaction whilst
        waiting for deadlock detection to kick in.
        """

        # NB if modified is bumped we know that sync token will have changed
        # too, so invalidate the cached value
        self._syncTokenRevision = None

        @inlineCallbacks
        def _bumpModified(subtxn):
            yield self._lockLastModifiedQuery.on(
                subtxn, resourceID=self._resourceID
            )
            result = yield self._changeLastModifiedQuery.on(
                subtxn, resourceID=self._resourceID
            )
            returnValue(result)

        try:
            self._modified = parseSQLTimestamp((
                yield self._txn.subtransaction(_bumpModified, retries=0, failureOK=True)
            )[0][0])
            yield self.invalidateQueryCache()

        except AllRetriesFailed:
            log.debug("CommonHome.bumpModified failed")

    @inlineCallbacks
    def removeUnacceptedShares(self):
        """
        Unbinds any collections that have been shared to this home but not yet
        accepted.  Associated invite entries are also removed.
        """
        bind = self._bindSchema
        kwds = {"homeResourceID": self._resourceID}
        yield Delete(
            From=bind,
            Where=(bind.HOME_RESOURCE_ID == Parameter("homeResourceID")).And(bind.BIND_STATUS != _BIND_STATUS_ACCEPTED)
        ).on(self._txn, **kwds)

    @inlineCallbacks
    def ownerHomeAndChildNameForChildID(self, resourceID):
        """
        Get the owner home for a shared child ID and the owner's name for that bound child.
        Subclasses may override.
        """
        rows = yield self._childClass._ownerHomeWithResourceID.on(self._txn, resourceID=resourceID)
        if rows:
            ownerHomeID, ownerName = rows[0]
            ownerHome = yield self._txn.homeWithResourceID(self._homeType, ownerHomeID)
            returnValue((ownerHome, ownerName))
        else:
            returnValue((None, None))

    @inlineCallbacks
    def emptyTrash(self, days=0, verbose=False):
        trash = yield self.getTrash()
        if trash is None:
            if verbose:
                msg = "No trash collection for principal"
                log.info(msg)
            returnValue(None)

        endTime = datetime.datetime.utcnow() - datetime.timedelta(days=days)

        untrashedCollections = yield self.children(onlyInTrash=False)
        for collection in untrashedCollections:
            displayName = displayNameForCollection(collection)
            children = yield trash.trashForCollection(
                collection._resourceID, end=endTime
            )
            if len(children) == 0:
                continue

            if verbose:
                msg = "Collection \"{}\":".format(displayName.encode("utf-8"))
                log.info(msg)
            for child in children:
                if verbose:
                    component = yield child.component()
                    summary = component.mainComponent().propertyValue("SUMMARY", "<no title>")
                    msg = "   Removing \"{}\"...".format(summary)
                    log.info(msg)
                yield child.purge(implicitly=False)
            if verbose:
                print("")

        trashedCollections = yield self.children(onlyInTrash=True)
        for collection in trashedCollections:
            displayName = displayNameForCollection(collection)
            children = yield trash.trashForCollection(
                collection._resourceID, end=endTime
            )
            if verbose:
                msg = "Collection \"{}\":".format(displayName.encode("utf-8"))
                log.info(msg)
            for child in children:
                if verbose:
                    component = yield child.component()
                    summary = component.mainComponent().propertyValue("SUMMARY", "<no title>")
                    msg = "   Removing \"{}\"...".format(summary)
                    log.info(msg)
                yield child.purge(implicitly=False)
            if verbose:
                print("")

            if collection.whenTrashed() < endTime:
                if verbose:
                    msg = "Removing collection \"{}\"...".format(displayName.encode("utf-8"))
                    log.info(msg)
                yield collection.purge()

    @inlineCallbacks
    def getTrashContents(self):
        result = {
            "trashedcollections": [],
            "untrashedcollections": []
        }

        trash = yield self.getTrash()
        if trash is None:
            returnValue(result)

        nowDT = datetime.datetime.utcnow()

        trashedCollections = yield self.children(onlyInTrash=True)
        for collection in trashedCollections:
            whenTrashed = collection.whenTrashed()
            detail = {
                "displayName": displayNameForCollection(collection),
                "recoveryID": collection._resourceID,
                "whenTrashed": agoString(nowDT - whenTrashed),
                "children": [],
            }
            startTime = whenTrashed - datetime.timedelta(minutes=5)
            children = yield trash.trashForCollection(
                collection._resourceID, start=startTime
            )
            for child in children:
                component = yield child.component()
                summary = component.mainComponent().propertyValue("SUMMARY", "<no title>")
                detail["children"].append(summary)
            result["trashedcollections"].append(detail)

        untrashedCollections = yield self.children(onlyInTrash=False)
        for collection in untrashedCollections:
            children = yield trash.trashForCollection(collection._resourceID)
            if len(children) == 0:
                continue
            detail = {
                "displayName": displayNameForCollection(collection),
                "children": [],
            }
            for child in children:
                childDetail = yield getEventDetails(child)
                detail["children"].append(childDetail)
            result["untrashedcollections"].append(detail)

        returnValue(result)

    @inlineCallbacks
    def recoverTrash(self, mode, recoveryID):
        trash = yield self.getTrash()
        if trash is not None:

            if mode == "event":
                if recoveryID:
                    child = yield trash.objectResourceWithID(recoveryID)
                    if child is not None:
                        yield child.fromTrash()
                else:
                    # Recover all trashed events
                    untrashedCollections = yield self.children(onlyInTrash=False)
                    for collection in untrashedCollections:
                        children = yield trash.trashForCollection(
                            collection._resourceID
                        )
                        for child in children:
                            yield child.fromTrash()
            else:
                if recoveryID:
                    collection = yield self.childWithID(recoveryID, onlyInTrash=True)
                    if collection is not None:
                        yield collection.fromTrash(
                            restoreChildren=True, delta=datetime.timedelta(minutes=5)
                        )
                else:
                    # Recover all trashed collections (and their events)
                    trashedCollections = yield self.children(onlyInTrash=True)
                    for collection in trashedCollections:
                        yield collection.fromTrash(restoreChildren=True)


class CommonHomeChild(FancyEqMixin, Memoizable, _SharedSyncLogic, HomeChildBase, SharingMixIn):
    """
    Common ancestor class of AddressBooks and Calendars.
    """
    log = Logger()

    compareAttributes = (
        "_name",
        "_home",
        "_resourceID",
    )

    _externalClass = None
    _homeRecordClass = None
    _metadataRecordClass = None
    _bindRecordClass = None
    _bindHomeIDAttributeName = None
    _bindResourceIDAttributeName = None
    _objectResourceClass = None

    _bindSchema = None
    _homeSchema = None
    _homeChildSchema = None
    _homeChildMetaDataSchema = None
    _revisionsSchema = None
    _objectSchema = None

    _childType = _CHILD_TYPE_NORMAL

    @classmethod
    @inlineCallbacks
    def makeClass(cls, home, bindData, additionalBindData, metadataData, propstore=None, ownerHome=None):
        """
        Given the various database rows, build the actual class.

        @param home: the parent home object
        @type home: L{CommonHome}
        @param bindData: the standard set of bind columns
        @type bindData: C{list}
        @param additionalBindData: additional bind data specific to sub-classes
        @type additionalBindData: C{list}
        @param metadataData: metadata data
        @type metadataData: C{list}
        @param propstore: a property store to use, or C{None} to load it automatically
        @type propstore: L{PropertyStore}
        @param ownerHome: the home of the owner, or C{None} to figure it out automatically
        @type ownerHome: L{CommonHome}

        @return: the constructed child class
        @rtype: L{CommonHomeChild}
        """

        _ignore_homeID, resourceID, name, bindMode, bindStatus, bindRevision, bindUID, bindMessage = bindData

        if ownerHome is None:
            if bindMode == _BIND_MODE_OWN:
                ownerHome = home
                ownerName = name
            else:
                ownerHome, ownerName = yield home.ownerHomeAndChildNameForChildID(resourceID)
        else:
            ownerName = None

        c = cls._externalClass if ownerHome and ownerHome.externalClass() else cls
        child = c(
            home=home,
            name=name,
            resourceID=resourceID,
            mode=bindMode,
            status=bindStatus,
            revision=bindRevision,
            message=bindMessage,
            ownerHome=ownerHome,
            ownerName=ownerName,
            bindUID=bindUID,
        )

        if additionalBindData:
            for attr, value in zip(child.additionalBindAttributes(), additionalBindData):
                setattr(child, attr, value)

        if metadataData:
            for attr, value in zip(child.metadataAttributes(), metadataData):
                setattr(child, attr, value)
            child._created = parseSQLTimestamp(child._created)
            child._modified = parseSQLTimestamp(child._modified)

        # We have to re-adjust the property store object to account for possible shared
        # collections as previously we loaded them all as if they were owned
        if ownerHome and propstore and bindMode != _BIND_MODE_OWN:
            propstore._setDefaultUserUID(ownerHome.uid())
        yield child._loadPropertyStore(propstore)

        returnValue(child)

    @classmethod
    @inlineCallbacks
    def _getDBData(cls, home, name, resourceID, bindUID):
        """
        Given a set of identifying information, load the data rows for the object. Only one of
        L{name}, L{resourceID} or L{bindUID} is specified - others are C{None}.

        @param home: the parent home object
        @type home: L{CommonHome}
        @param name: the resource name
        @type name: C{str}
        @param resourceID: the resource ID
        @type resourceID: C{int}
        @param bindUID: the unique ID of the external (cross-pod) referenced item
        @type bindUID: C{int}
        """

        # Get the bind row data
        row = None
        queryCacher = home._txn._queryCacher

        if queryCacher:
            # Retrieve data from cache
            if name:
                cacheKey = queryCacher.keyForObjectWithName(home._resourceID, name)
            elif resourceID:
                cacheKey = queryCacher.keyForObjectWithResourceID(home._resourceID, resourceID)
            elif bindUID:
                cacheKey = queryCacher.keyForObjectWithBindUID(home._resourceID, bindUID)
            row = yield queryCacher.get(cacheKey)

        if row is None:
            # No cached copy
            if name:
                rows = yield cls._bindForNameAndHomeID.on(home._txn, name=name, homeID=home._resourceID)
            elif resourceID:
                rows = yield cls._bindForResourceIDAndHomeID.on(home._txn, resourceID=resourceID, homeID=home._resourceID)
            elif bindUID:
                rows = yield cls._bindForBindUIDAndHomeID.on(home._txn, bindUID=bindUID, homeID=home._resourceID)
            row = rows[0] if rows else None

        if not row:
            returnValue(None)

        if queryCacher:
            # Cache the result
            queryCacher.setAfterCommit(home._txn, queryCacher.keyForObjectWithName(home._resourceID, name), row)
            queryCacher.setAfterCommit(home._txn, queryCacher.keyForObjectWithResourceID(home._resourceID, resourceID), row)
            queryCacher.setAfterCommit(home._txn, queryCacher.keyForObjectWithBindUID(home._resourceID, bindUID), row)

        bindData = row[:cls.bindColumnCount]
        additionalBindData = row[cls.bindColumnCount:cls.bindColumnCount + len(cls.additionalBindColumns())]
        resourceID = bindData[cls.bindColumns().index(cls._bindSchema.RESOURCE_ID)]

        # Get the matching metadata data
        metadataData = None
        if queryCacher:
            # Retrieve from cache
            cacheKey = queryCacher.keyForHomeChildMetaData(resourceID)
            metadataData = yield queryCacher.get(cacheKey)

        if metadataData is None:
            # No cached copy
            metadataData = (yield cls._metadataByIDQuery.on(home._txn, resourceID=resourceID))[0]
            if queryCacher:
                # Cache the results
                yield queryCacher.setAfterCommit(home._txn, cacheKey, metadataData)

        returnValue((bindData, additionalBindData, metadataData,))

    def __init__(self, home, name, resourceID, mode, status, revision=0, message=None, ownerHome=None, ownerName=None, bindUID=None):

        self._home = home
        self._name = name
        self._resourceID = resourceID
        self._bindMode = mode
        self._bindStatus = status
        self._bindRevision = revision
        self._bindUID = bindUID
        self._bindMessage = message
        self._ownerHome = home if ownerHome is None else ownerHome
        self._ownerName = name if ownerName is None else ownerName
        self._created = None
        self._modified = None
        self._objects = {}
        self._objectNames = None
        self._syncTokenRevision = None

        # Always use notifiers based off the owner home so that shared collections use tokens common
        # to the owner - and thus will be the same for each sharee. Without that, each sharee would have
        # a different token to subscribe to and thus would each need a separate push - whereas a common
        # token only requires one push (to multiple subscribers).
        if self._ownerHome._notifiers:
            self._notifiers = dict([(factory_name, notifier.clone(self),) for factory_name, notifier in self._ownerHome._notifiers.items()])
        else:
            self._notifiers = None

    def isTrash(self):
        return False

    def originalParentForResource(self, objectResource):
        return succeed(objectResource._parentCollection)

    def memoMe(self, key, memo):
        """
        Add this object to the memo dictionary in whatever fashion is appropriate.

        @param key: key used for lookup
        @type key: C{object} (typically C{str} or C{int})
        @param memo: the dict to store to
        @type memo: C{dict}
        """
        memo[self._name] = self
        memo[self._resourceID] = self

    @classmethod
    @inlineCallbacks
    def loadAllObjects(cls, home):
        """
        Load all L{CommonHomeChild} instances which are children of a given
        L{CommonHome} and return a L{Deferred} firing a list of them.  This must
        create the child classes and initialize them using "batched" SQL
        operations to keep this constant wrt the number of children.  This is an
        optimization for Depth:1 operations on the home.
        """
        results = []

        # Load from the main table first
        dataRows = (yield cls._childrenAndMetadataForHomeID.on(home._txn, homeID=home._resourceID))

        resourceID_index = cls.bindColumns().index(cls._bindSchema.RESOURCE_ID)
        if dataRows:
            # Get property stores
            childResourceIDs = [dataRow[resourceID_index] for dataRow in dataRows]

            propertyStores = yield PropertyStore.forMultipleResourcesWithResourceIDs(
                home.uid(), None, home.authzuid(), home._txn, childResourceIDs
            )

            # Get revisions
            revisions = yield cls.childSyncTokenRevisions(home, childResourceIDs)

        # Create the actual objects merging in properties
        for dataRow in dataRows:
            bindData = dataRow[:cls.bindColumnCount]
            resourceID = bindData[resourceID_index]
            additionalBindData = dataRow[cls.bindColumnCount:cls.bindColumnCount + len(cls.additionalBindColumns())]
            metadataData = dataRow[cls.bindColumnCount + len(cls.additionalBindColumns()):]
            propstore = propertyStores.get(resourceID, None)

            child = yield cls.makeClass(home, bindData, additionalBindData, metadataData, propstore)
            child._syncTokenRevision = revisions.get(resourceID, None)
            results.append(child)

        returnValue(results)

    @classmethod
    def objectWithName(cls, home, name, accepted=True, onlyInTrash=False):
        return cls.objectWith(home, name=name, accepted=accepted, onlyInTrash=onlyInTrash)

    @classmethod
    def objectWithID(cls, home, resourceID, accepted=True, onlyInTrash=False):
        return cls.objectWith(home, resourceID=resourceID, accepted=accepted, onlyInTrash=onlyInTrash)

    @classmethod
    def objectWithBindUID(cls, home, bindUID, accepted=True, onlyInTrash=False):
        return cls.objectWith(home, bindUID=bindUID, accepted=accepted, onlyInTrash=onlyInTrash)

    @classmethod
    @inlineCallbacks
    def objectWith(cls, home, name=None, resourceID=None, bindUID=None, accepted=True, onlyInTrash=False):
        """
        Create the object using one of the specified arguments as the key to load it. One
        and only one of the keyword arguments must be set.

        @param home: home
        @type home: L{CommonHome}
        @param name: name of the resource, or C{None}
        @type name: C{str}
        @param uid: resource data UID, or C{None}
        @type uid: C{str}
        @param resourceID: resource id
        @type resourceID: C{int}
        @param accepted: if C{True} only load owned or accepted share items
        @type accepted: C{bool}

        @return: the new object or C{None} if not found
        @rtype: C{CommonHomeChild}
        """

        dbData = yield cls._getDBData(home, name, resourceID, bindUID)
        if dbData is None:
            returnValue(None)
        bindData, additionalBindData, metadataData = dbData

        bindStatus = bindData[cls.bindColumns().index(cls._bindSchema.BIND_STATUS)]
        if accepted is not None and (bindStatus == _BIND_STATUS_ACCEPTED) != bool(accepted):
            returnValue(None)

        # Suppress if the collection is trash-capable and is in the trash
        try:
            isInTrash = metadataData[cls.metadataColumns().index(cls._homeChildMetaDataSchema.IS_IN_TRASH)]
        except (AttributeError, ValueError):
            isInTrash = False
        if onlyInTrash:
            if not isInTrash:
                returnValue(None)
        else:
            if isInTrash:
                returnValue(None)

        child = yield cls.makeClass(home, bindData, additionalBindData, metadataData)
        returnValue(child)

    @classproperty
    def _insertHomeChild(cls):
        """
        DAL statement to create a home child with all default values.
        """
        child = cls._homeChildSchema
        return Insert(
            {child.RESOURCE_ID: schema.RESOURCE_ID_SEQ},
            Return=(child.RESOURCE_ID)
        )

    @classproperty
    def _insertHomeChildMetaData(cls):
        """
        DAL statement to create a home child with all default values.
        """
        child = cls._homeChildMetaDataSchema
        return Insert(
            {
                child.RESOURCE_ID: Parameter("resourceID"),
                child.CHILD_TYPE: Parameter("childType"),
            },
            Return=(child.CREATED, child.MODIFIED)
        )

    @classmethod
    @inlineCallbacks
    def create(cls, home, name, bindUID=None):

        if (yield cls._bindForNameAndHomeID.on(home._txn, name=name, homeID=home._resourceID)):
            raise HomeChildNameAlreadyExistsError(name)

        if name.startswith("."):
            raise HomeChildNameNotAllowedError(name)

        # Create this object
        resourceID = (yield cls._insertHomeChild.on(home._txn))[0][0]

        # Initialize this object
        yield cls._insertHomeChildMetaData.on(
            home._txn, resourceID=resourceID, childType=cls._childType,
        )

        # Bind table needs entry
        yield cls._bindInsertQuery.on(
            home._txn, homeID=home._resourceID, resourceID=resourceID, bindUID=bindUID,
            name=name, mode=_BIND_MODE_OWN, bindStatus=_BIND_STATUS_ACCEPTED,
            message=None,
        )

        # Initialize other state
        child = yield cls.objectWithID(home, resourceID)

        yield child._initSyncToken()

        # Change notification for a create is on the home collection
        yield home.notifyChanged()
        yield child.notifyPropertyChanged()
        returnValue(child)

    @classproperty
    def _metadataByIDQuery(cls):
        """
        DAL query to retrieve created/modified dates based on a resource ID.
        """
        child = cls._homeChildMetaDataSchema
        return Select(cls.metadataColumns(),
                      From=child,
                      Where=child.RESOURCE_ID == Parameter("resourceID"))

    def id(self):
        """
        Retrieve the store identifier for this collection.

        @return: store identifier.
        @rtype: C{int}
        """
        return self._resourceID

    def external(self):
        """
        Is this an external home.

        @return: a string.
        """
        return self.ownerHome().external()

    def externalClass(self):
        """
        Is this an external home.

        @return: a string.
        """
        return self.ownerHome().externalClass()

    def serialize(self):
        """
        Create a dictionary mapping key attributes so this object can be sent over a cross-pod call
        and reconstituted at the other end. Note that the other end may have a different schema so
        the attributes may not match exactly and will need to be processed accordingly.
        """
        data = {}
        data["bindData"] = dict([(attr[1:], getattr(self, attr, None)) for attr in self.bindAttributes()])
        data["additionalBindData"] = dict([(attr[1:], getattr(self, attr, None)) for attr in self.additionalBindAttributes()])
        data["metadataData"] = dict([(attr[1:], getattr(self, attr, None)) for attr in self.metadataAttributes()])
        data["metadataData"]["created"] = data["metadataData"]["created"].isoformat(" ")
        data["metadataData"]["modified"] = data["metadataData"]["modified"].isoformat(" ")
        return data

    @classmethod
    @inlineCallbacks
    def deserialize(cls, parent, mapping):
        """
        Given a mapping generated by L{serialize}, convert the values into an array of database
        like items that conforms to the ordering of L{_allColumns} so it can be fed into L{makeClass}.
        Note that there may be a schema mismatch with the external data, so treat missing items as
        C{None} and ignore extra items.
        """

        bindData = [mapping["bindData"].get(row[1:]) for row in cls.bindAttributes()]
        additionalBindData = [mapping["additionalBindData"].get(row[1:]) for row in cls.additionalBindAttributes()]
        metadataData = [mapping["metadataData"].get(row[1:]) for row in cls.metadataAttributes()]
        child = yield cls.makeClass(parent, bindData, additionalBindData, metadataData)
#        for attr in cls._otherSerializedAttributes():
#            setattr(child, attr, mapping.get(attr[1:]))
        returnValue(child)

    @property
    def _txn(self):
        return self._home._txn

    def directoryService(self):
        return self._txn.store().directoryService()

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)

    def exists(self):
        """
        An empty resource-id means this object does not yet exist in the DB.
        """
        return self._resourceID is not None

    def name(self):
        return self._name

    @classproperty
    def _renameQuery(cls):
        """
        DAL statement to rename a L{CommonHomeChild}
        """
        bind = cls._bindSchema
        return Update({bind.RESOURCE_NAME: Parameter("name")},
                      Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                          bind.HOME_RESOURCE_ID == Parameter("homeID")))

    @inlineCallbacks
    def rename(self, name):
        """
        Change the name of this L{CommonHomeChild} and update its sync token to
        reflect that change.

        @return: a L{Deferred} which fires when the modification is complete.
        """

        if not self.owned() or self.external():
            raise ShareNotAllowed("Cannot rename a shared collection")

        oldName = self._name

        yield self.invalidateQueryCache()

        yield self._renameQuery.on(self._txn, name=name,
                                   resourceID=self._resourceID,
                                   homeID=self._home._resourceID)
        self._name = name
        # update memos
        key = self._home._childrenKey(self.isInTrash())
        del self._home._children[key][oldName]
        self._home._children[key][name] = self
        yield self._renameSyncToken()

        yield self.notifyPropertyChanged()
        yield self._home.notifyChanged()

    @classproperty
    def _deleteQuery(cls):
        """
        DAL statement to delete a L{CommonHomeChild} by its resource ID.
        """
        child = cls._homeChildSchema
        return Delete(child, Where=child.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def remove(self):
        """
        If trash is enabled, move the collection to trash, otherwise fully
        delete it.
        """

        if config.EnableTrashCollection:
            isInTrash = self.isInTrash()
            if isInTrash:
                raise AlreadyInTrashError
            else:
                yield self.toTrash()
        else:
            yield self._reallyRemove()

    @inlineCallbacks
    def _reallyRemove(self):
        """
        Actually remove this collection from the database. All the child resources will be automatically
        removed by virtue of an on delete cascade. Note that means no implicit scheduling cancels will
        occur.
        """

        # Stop sharing first
        yield self.ownerDeleteShare()

        # Do before setting _resourceID making changes
        yield self.notifyPropertyChanged()

        yield self.invalidateQueryCache()

        yield self._deletedSyncToken()
        yield self._deleteQuery.on(self._txn, NoSuchHomeChildError, resourceID=self._resourceID)
        yield self.properties()._removeResource()

        # Set to non-existent state
        self._resourceID = None
        self._created = None
        self._modified = None
        self._objects = {}

        yield self._home.notifyChanged()

    @classproperty
    def _updateIsInTrashQuery(cls):
        table = cls._homeChildMetaDataSchema
        return Update(
            {table.IS_IN_TRASH: Parameter("isInTrash"), table.TRASHED: Parameter("trashed")},
            Where=table.RESOURCE_ID == Parameter("resourceID"),
        )

    @inlineCallbacks
    def toTrash(self):
        yield self.ownerDeleteShare()

        for resource in (yield self.objectResources()):
            yield resource.toTrash()

        whenTrashed = datetime.datetime.utcnow()
        yield self._updateIsInTrashQuery.on(
            self._txn, isInTrash=True, trashed=whenTrashed, resourceID=self._resourceID
        )
        yield self._deletedSyncToken()

        # Rename after calling _deletedSyncToken
        newName = "{}-{}".format(self._name[:36], str(uuid4()))
        yield self.rename(newName)

        # Update _children cache to reflect moving to trash
        try:
            del self._home._children[self._home._childrenKey(False)][newName]
        except KeyError:
            pass
        try:
            del self._home._children[self._home._childrenKey(False)][self._resourceID]
        except KeyError:
            pass
        self._home._children[self._home._childrenKey(True)][newName] = self
        self._home._children[self._home._childrenKey(True)][self._resourceID] = self

        self._isInTrash = True
        self._trashed = str(whenTrashed)

    @inlineCallbacks
    def fromTrash(
        self, restoreChildren=True, delta=datetime.timedelta(minutes=5),
        verbose=False
    ):
        if not self._isInTrash:
            returnValue(None)

        startTime = self.whenTrashed()
        if delta is not None:
            startTime = startTime - delta

        yield self._updateIsInTrashQuery.on(
            self._txn, isInTrash=False, trashed=None, resourceID=self._resourceID
        )
        yield self._initSyncToken()
        yield self.notifyPropertyChanged()
        yield self.invalidateQueryCache()
        yield self._home.notifyChanged()
        self._isInTrash = False
        self._trashed = None

        # Update _children cache to reflect moving from trash
        try:
            del self._home._children[self._home._childrenKey(True)][self._name]
        except KeyError:
            pass
        try:
            del self._home._children[self._home._childrenKey(True)][self._resourceID]
        except KeyError:
            pass
        self._home._children[self._home._childrenKey(False)][self._name] = self
        self._home._children[self._home._childrenKey(False)][self._resourceID] = self

        if restoreChildren:
            trash = yield self._home.getTrash()
            if trash is not None:
                childrenToRestore = yield trash.trashForCollection(
                    self._resourceID, start=startTime
                )
                for child in childrenToRestore:
                    if verbose:
                        component = yield child.component()
                        summary = component.mainComponent().propertyValue("SUMMARY", "<no title>")
                        print("Recovering \"{}\"".format(summary))

                    try:
                        yield child.fromTrash()
                    except Exception as e:
                        # Don't let one failed child stop the others, but alert the user
                        print("Failed to restore \"{}\" due to {}".format(child.name(), str(e)))

    def isInTrash(self):
        return getattr(self, "_isInTrash", False)

    def whenTrashed(self):
        if self._trashed is None:
            return None
        return parseSQLTimestamp(self._trashed)

    def purge(self):
        """
        Do a "silent" removal of this child.
        """
        return self._reallyRemove()

    def ownerHome(self):
        """
        @see: L{ICalendar.ownerCalendarHome}
        @see: L{IAddressbook.ownerAddessbookHome}
        """
        return self._ownerHome

    def viewerHome(self):
        """
        @see: L{ICalendar.viewerCalendarHome}
        @see: L{IAddressbook.viewerAddressbookHome}
        """
        return self._home

    @classproperty
    def _ownerHomeWithResourceID(cls):
        """
        DAL query to retrieve the home resource ID and resource name of the owner from the bound
        home-child ID.
        """
        bind = cls._bindSchema
        return Select(
            [bind.HOME_RESOURCE_ID, bind.RESOURCE_NAME, ],
            From=bind,
            Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                bind.BIND_MODE == _BIND_MODE_OWN)
        )

    @inlineCallbacks
    def objectResources(self):
        """
        Load and cache all children - Depth:1 optimization
        """
        results = (yield self._objectResourceClass.loadAllObjects(self))
        for result in results:
            self._objects[result.name()] = result
            self._objects[result.uid()] = result
            self._objects[result.id()] = result
        self._objectNames = sorted([result.name() for result in results])
        returnValue(results)

    @inlineCallbacks
    def objectResourcesWithNames(self, names):
        """
        Load and cache all named children - set of names optimization
        """
        results = (yield self._objectResourceClass.loadAllObjectsWithNames(self, names))
        for result in results:
            self._objects[result.name()] = result
            self._objects[result.uid()] = result
            self._objects[result.id()] = result
        self._objectNames = sorted([result.name() for result in results])
        returnValue(results)

    @inlineCallbacks
    def listObjectResources(self):
        """
        Returns a list of names of object resources in this collection
        """
        if self._objectNames is None:
            self._objectNames = yield self._objectResourceClass.listObjects(self)
        returnValue(self._objectNames)

    @inlineCallbacks
    def countObjectResources(self):
        if self._objectNames is None:
            count = (yield self._objectResourceClass.countObjects(self))
            returnValue(count)
        returnValue(len(self._objectNames))

    def objectResourceWithName(self, name):
        if name in self._objects:
            return succeed(self._objects[name])
        else:
            return self._makeObjectResource(name=name)

    def objectResourceWithUID(self, uid):
        if uid in self._objects:
            return succeed(self._objects[uid])
        else:
            return self._makeObjectResource(uid=uid)

    def objectResourceWithID(self, resourceID):
        if resourceID in self._objects:
            return succeed(self._objects[resourceID])
        else:
            return self._makeObjectResource(resourceID=resourceID)

    @inlineCallbacks
    def _makeObjectResource(self, name=None, uid=None, resourceID=None):
        """
        We create the empty object first then have it initialize itself from the
        store.
        """
        objectResource = (
            yield self._objectResourceClass.objectWith(self, name=name, uid=uid, resourceID=resourceID)
        )
        if objectResource:
            self._objects[objectResource.name()] = objectResource
            self._objects[objectResource.uid()] = objectResource
            self._objects[objectResource.id()] = objectResource
        else:
            if resourceID:
                self._objects[resourceID] = None
            else:
                self._objects[name if name else uid] = None
        returnValue(objectResource)

    @inlineCallbacks
    def resourceNameForUID(self, uid):
        try:
            resource = self._objects[uid]
            returnValue(resource.name() if resource else None)
        except KeyError:
            pass
        name = yield self._objectResourceClass.resourceNameForUID(self, uid)
        if name:
            returnValue(name)
        else:
            self._objects[uid] = None
            returnValue(None)

    @inlineCallbacks
    def resourceUIDForName(self, name):
        try:
            resource = self._objects[name]
            returnValue(resource.uid() if resource else None)
        except KeyError:
            pass
        uid = yield self._objectResourceClass.resourceUIDForName(self, name)
        if uid:
            returnValue(uid)
        else:
            self._objects[name] = None
            returnValue(None)

    @inlineCallbacks
    def createObjectResourceWithName(self, name, component, options=None):
        """
        Create a new resource with component data and optional metadata. We
        create the python object using the metadata then create the actual store
        object with setComponent.
        """

        # Create => a new resource name
        if name in self._objects and self._objects[name]:
            raise ObjectResourceNameAlreadyExistsError()

        # Apply check to the size of the collection
        if config.MaxResourcesPerCollection:
            child_count = (yield self.countObjectResources())
            if child_count >= config.MaxResourcesPerCollection:
                raise TooManyObjectResourcesError()

        objectResource = (
            yield self._objectResourceClass.create(self, name, component, options)
        )
        self._objects[objectResource.name()] = objectResource
        self._objects[objectResource.uid()] = objectResource
        self._objects[objectResource.id()] = objectResource
        self._objectNames = None

        # Note: create triggers a notification when the component is set, so we
        # don't need to call notify() here like we do for object removal.
        returnValue(objectResource)

    @inlineCallbacks
    def addedObjectResource(self, child):
        """
        When a resource is put back from the trash to the original collection,
        this method updates/invalidates caches and triggers a notification.
        """
        self._objects[child.name()] = child
        self._objects[child.uid()] = child
        self._objects[child.id()] = child
        # Invalidate _objectNames so it will get reloaded
        self._objectNames = None
        yield self.notifyChanged()

    @inlineCallbacks
    def removedObjectResource(self, child):
        self._objects.pop(child.name(), None)
        self._objects.pop(child.uid(), None)
        self._objects.pop(child.id(), None)
        if self._objectNames and child.name() in self._objectNames:
            self._objectNames.remove(child.name())
        yield self._deleteRevision(child.name())
        yield self.notifyChanged(category=child.removeNotifyCategory())

    @classproperty
    def _moveParentUpdateQuery(cls, adjustName=False):
        """
        DAL query to update a child to be in a new parent.
        """
        obj = cls._objectSchema
        cols = {
            obj.PARENT_RESOURCE_ID: Parameter("newParentID")
        }
        if adjustName:
            cols[obj.RESOURCE_NAME] = Parameter("newName")
        return Update(
            cols,
            Where=obj.RESOURCE_ID == Parameter("resourceID")
        )

    def _movedObjectResource(self, child, newparent):
        """
        Method that subclasses can override to do an extra DB adjustments when a resource
        is moved.
        """
        return succeed(True)

    @inlineCallbacks
    def _validObjectResource(self, child, newparent, newname=None):
        """
        Check that the move operation is valid

        TODO: check that the resource name does not exist in the new parent, or that the UID
        does not exist there too.

        @param child: the child resource to move
        @type child: L{CommonObjectResource}
        @param newparent: the parent to move to
        @type newparent: L{CommonHomeChild}
        @param newname: new name to use in new parent
        @type newname: C{str} or C{None} for existing name
        """

        name = child.name()

        if newname is None:
            newname = name

        # Create => a new resource name
        if newname.startswith("."):
            raise ObjectResourceNameNotAllowedError(newname)

        # Make sure name is not already used - i.e., overwrite not allowed
        if (yield newparent.objectResourceWithName(newname)) is not None:
            raise ObjectResourceNameAlreadyExistsError(newname)

        # Apply check to the size of the collection
        if config.MaxResourcesPerCollection:
            child_count = (yield self.countObjectResources())
            if child_count >= config.MaxResourcesPerCollection:
                raise TooManyObjectResourcesError()

        returnValue(newname)

    @inlineCallbacks
    def moveObjectResource(self, child, newparent, newname=None):
        """
        Move a child of this collection into another collection without actually removing/re-inserting the data.
        Make sure sync and cache details for both collections are updated.

        TODO: check that the resource name does not exist in the new parent, or that the UID
        does not exist there too.

        @param child: the child resource to move
        @type child: L{CommonObjectResource}
        @param newparent: the parent to move to
        @type newparent: L{CommonHomeChild}
        @param newname: new name to use in new parent
        @type newname: C{str} or C{None} for existing name
        """

        name = child.name()
        newname = yield self._validObjectResource(child, newparent, newname)
        uid = child.uid()

        # Clean this collections cache and signal sync change
        self._objects.pop(name, None)
        self._objects.pop(uid, None)
        self._objects.pop(child.id(), None)
        yield self._deleteRevision(name)
        yield self.notifyChanged()

        # Handle cases where move is within the same collection or to a different collection
        # with/without a name change
        obj = self._objectSchema
        cols = {}
        if newparent._resourceID != self._resourceID:
            cols[obj.PARENT_RESOURCE_ID] = Parameter("newParentID")
        if newname != name:
            cols[obj.RESOURCE_NAME] = Parameter("newName")
        yield Update(
            cols,
            Where=obj.RESOURCE_ID == Parameter("resourceID")
        ).on(
            self._txn,
            resourceID=child._resourceID,
            newParentID=newparent._resourceID,
            newName=newname,
        )

        # Only signal a move when parent is different
        if newparent._resourceID != self._resourceID:
            yield self._movedObjectResource(child, newparent)

        child._parentCollection = newparent

        # Signal sync change on new collection
        newparent._objects.pop(name, None)
        newparent._objects.pop(uid, None)
        newparent._objects.pop(child.id(), None)
        yield newparent._insertRevision(newname)
        yield newparent.notifyChanged()

    @inlineCallbacks
    def moveObjectResourceCreateDelete(self, child, newparent, newname=None):
        """
        Move a child of this collection into another collection by doing a create/delete.

        TODO: check that the resource name does not exist in the new parent, or that the UID
        does not exist there too.

        @param child: the child resource to move
        @type child: L{CommonObjectResource}
        @param newparent: the parent to move to
        @type newparent: L{CommonHomeChild}
        @param newname: new name to use in new parent
        @type newname: C{str} or C{None} for existing name
        """

        name = child.name()
        newname = yield self._validObjectResource(child, newparent, newname)

        # Do a move as a create/delete
        component = yield child.component()
        yield newparent.moveObjectResourceHere(name, component)
        yield self.moveObjectResourceAway(child.id(), child)

    @inlineCallbacks
    def moveObjectResourceHere(self, name, component):
        """
        Create a new child in this collection as part of a move operation. This needs to be split out because
        behavior differs for sub-classes and cross-pod operations.

        @param name: new name to use in new parent
        @type name: C{str} or C{None} for existing name
        @param component: data for new resource
        @type component: L{Component}
        """

        yield self.createObjectResourceWithName(name, component)

    @inlineCallbacks
    def moveObjectResourceAway(self, rid, child=None):
        """
        Remove the child as the result of a move operation. This needs to be split out because
        behavior differs for sub-classes and cross-pod operations.

        @param rid: the child resource-id to move
        @type rid: C{int}
        @param child: the child resource to move - might be C{None} for cross-pod
        @type child: L{CommonObjectResource}
        """

        if child is None:
            child = yield self.objectResourceWithID(rid)
        yield child.remove()

    def objectResourcesHaveProperties(self):
        return False

    def search(self, filter):
        """
        Do a query of the contents of this collection.

        @param filter: the query filter to use
        @type filter: L{Filter}

        @return: the names of the matching resources
        @rtype: C{list}
        """

        # This implementation raises - sub-classes override to do the actual query
        raise IndexedSearchException()

    @inlineCallbacks
    def sharedChildResourceNamesSinceRevision(self, revision, depth):
        """
        Determine the list of child resources that have changed since the specified sync revision.
        We do the same SQL query for both depth "1" and "infinity", but filter the results for
        "1" to only account for a collection change.

        We need to handle shared collection a little differently from owned ones. When a shared collection
        is bound into a home we record a revision for it using the sharee home id and sharee collection name.
        That revision is the "starting point" for changes: so if sync occurs with a revision earlier than
        that, we return the list of all resources in the shared collection since they are all "new" as far
        as the client is concerned since the shared collection has just appeared. For a later revision, we
        just report the changes since that one. When a shared collection is removed from a home, we again
        record a revision for the sharee home and sharee collection name with the "deleted" flag set. That way
        the shared collection can be reported as removed.

        @param revision: the sync revision to compare to
        @type revision: C{str}
        @param depth: depth for determine what changed
        @type depth: C{str}
        """
        changed = set()
        deleted = set()
        invalid = set()
        if self.external():
            if depth == "1":
                pass
            else:
                invalid.add(self.name() + "/")
        else:
            # If revision is prior to when the share was created, then treat as a full sync of the share
            if revision != 0 and revision < self._bindRevision:
                if depth != "1":
                    # This should never happen unless the client the share existed, was removed and then
                    # re-added and the client has a token from before the remove. In that case the token is no
                    # longer valid - a full sync has to be done.
                    raise SyncTokenValidException
                else:
                    results = yield self.sharedChildResourceNamesSinceRevisionZero(depth)
                    returnValue(results)

            rev = self._revisionsSchema
            results = [
                (
                    self.name(),
                    name if name else "",
                    wasdeleted
                )
                for name, wasdeleted in
                (yield Select(
                    [rev.RESOURCE_NAME, rev.DELETED],
                    From=rev,
                    Where=(rev.REVISION > revision).And(
                        rev.RESOURCE_ID == self._resourceID)
                ).on(self._txn))
                if name
            ]

            for path, name, wasdeleted in results:
                if wasdeleted:
                    if depth == "1":
                        changed.add("%s/" % (path,))
                    else:
                        deleted.add("%s/%s" % (path, name,))

                # Always report collection as changed
                changed.add("%s/" % (path,))

                # Resource changed - for depth "infinity" report resource as changed
                if name and depth != "1":
                    changed.add("%s/%s" % (path, name,))

        returnValue((changed, deleted, invalid,))

    @inlineCallbacks
    def sharedChildResourceNamesSinceRevisionZero(self, depth):
        """
        Revision == 0 specialization of L{sharedChildResourceNamesSinceRevision}. We report on all
        existing resources -= this collection and children (if depth == infinite).

        @param depth: depth for determine what changed
        @type depth: C{str}
        """
        changed = set()
        deleted = set()
        invalid = set()
        path = self.name()
        if self.external():
            if depth == "1":
                changed.add("{}/".format(path))
            else:
                invalid.add("{}/".format(path))
        else:
            path = self.name()
            # Always report collection as changed
            changed.add(path + "/")

            # Resource changed - for depth "infinity" report resource as changed
            if depth != "1":
                for name in (yield self.listObjectResources()):
                    changed.add("%s/%s" % (path, name,))

        returnValue((changed, deleted, invalid,))

    @inlineCallbacks
    def _loadPropertyStore(self, props=None):
        if props is None:
            # Use any authz uid in place of the viewer uid so delegates have their own
            # set of properties
            props = yield PropertyStore.load(
                self.ownerHome().uid(),
                self.viewerHome().uid(),
                self.viewerHome().authzuid(),
                self._txn,
                self._resourceID,
                notifyCallback=self.notifyPropertyChanged
            )
        self.initPropertyStore(props)
        self._properties = props

    def properties(self):
        return self._properties

    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """
        pass

    # IDataStoreObject
    def contentType(self):
        raise NotImplementedError()

    def md5(self):
        return None

    def size(self):
        return 0

    def created(self):
        return datetimeMktime(self._created) if self._created else None

    def modified(self):
        return datetimeMktime(self._modified) if self._modified else None

    def addNotifier(self, factory_name, notifier):
        if self._notifiers is None:
            self._notifiers = {}
        self._notifiers[factory_name] = notifier

    def getNotifier(self, factory_name):
        return self._notifiers.get(factory_name) if self._notifiers else None

    def notifierID(self):
        return (self.ownerHome()._notifierPrefix, "%s/%s" % (self.ownerHome().uid(), self._ownerName,),)

    def parentNotifierID(self):
        return self.ownerHome().notifierID()

    def notifyChanged(self, category=ChangeCategory.default):
        """
        Send notifications when a child resource is changed.
        """
        return self._notifyChanged(property_change=False, category=category)

    def notifyPropertyChanged(self):
        """
        Send notifications when properties on this object change.
        """
        return self._notifyChanged(property_change=True)

    @inlineCallbacks
    def _notifyChanged(self, property_change=False, category=ChangeCategory.default):
        """
        Send notifications, change sync token and bump last modified because
        the resource has changed.  We ensure we only do this once per object
        per transaction.

        Note that we distinguish between property changes and child resource changes. For property
        changes we need to bump the collections revision token, but we must not do that for child
        changes because that can cause a deadlock (plus it is not needed as the overall revision
        token includes the child token via the max() construct in the query).

        @param property_change: indicates whether this is the result of a property change as opposed to
            a child resource being added, changed or removed.
        @type property_change: C{bool}
        """
        if self._txn.isNotifiedAlready(self):
            returnValue(None)
        self._txn.notificationAddedForObject(self)

        # Update modified if object still exists
        if self._resourceID:
            yield self.bumpModified()

            # Bump the collection level sync token only for property change
            if property_change:
                yield self._bumpSyncToken()

        # Send notifications
        if self._notifiers:
            # cache notifiers run in post commit
            notifier = self._notifiers.get("cache", None)
            if notifier:
                self._txn.postCommit(notifier.notify)
            # push notifiers add their work items immediately
            notifier = self._notifiers.get("push", None)
            if notifier:
                yield notifier.notify(self._txn, priority=category.value)

        if not self.external():
            yield self.notifyExternalShare(category)

    @classproperty
    def _lockLastModifiedQuery(cls):
        schema = cls._homeChildMetaDataSchema
        return Select(
            From=schema,
            Where=schema.RESOURCE_ID == Parameter("resourceID"),
            ForUpdate=True,
            NoWait=True
        )

    @classproperty
    def _changeLastModifiedQuery(cls):
        schema = cls._homeChildMetaDataSchema
        return Update({schema.MODIFIED: utcNowSQL},
                      Where=schema.RESOURCE_ID == Parameter("resourceID"),
                      Return=schema.MODIFIED)

    @inlineCallbacks
    def bumpModified(self):
        """
        Bump the MODIFIED value. A possible deadlock could happen here if two
        or more simultaneous changes are happening. In that case it is OK for
        the MODIFIED change to fail so long as at least one works. We will use
        SAVEPOINT logic to handle ignoring the deadlock error. We use SELECT
        FOR UPDATE NOWAIT to ensure we do not delay the transaction whilst
        waiting for deadlock detection to kick in.
        """

        @inlineCallbacks
        def _bumpModified(subtxn):
            yield self._lockLastModifiedQuery.on(
                subtxn, resourceID=self._resourceID
            )
            result = yield self._changeLastModifiedQuery.on(
                subtxn, resourceID=self._resourceID
            )
            returnValue(result)

        try:
            self._modified = parseSQLTimestamp((
                yield self._txn.subtransaction(
                    _bumpModified, retries=0, failureOK=True
                )
            )[0][0])

            queryCacher = self._txn._queryCacher
            if queryCacher is not None:
                cacheKey = queryCacher.keyForHomeChildMetaData(
                    self._resourceID
                )
                yield queryCacher.invalidateAfterCommit(self._txn, cacheKey)
        except AllRetriesFailed:
            log.debug("CommonHomeChild.bumpModified failed")


class CommonObjectResource(FancyEqMixin, object):
    """
    Base class for object resources.
    """
    log = Logger()

    compareAttributes = (
        "_name",
        "_parentCollection",
    )

    _externalClass = None
    _objectSchema = None
    _componentClass = None

    # Sub-classes must override and set their version number. This is used for
    # on-demand data upgrades - i.e., any time old data is read it will be
    # converted to the latest format and written back.
    _currentDataVersion = 0

    BATCH_LOAD_SIZE = 50

    @classmethod
    @inlineCallbacks
    def makeClass(cls, parent, objectData, propstore=None):
        """
        Given the various database rows, build the actual class.

        @param parent: the parent collection object
        @type parent: L{CommonHomeChild}

        @param objectData: the standard set of object columns
        @type objectData: C{list}

        @param propstore: a property store to use, or C{None} to load it
            automatically
        @type propstore: L{PropertyStore}

        @return: the constructed child class
        @rtype: L{CommonHomeChild}
        """

        c = cls._externalClass if parent.externalClass() else cls
        child = c(
            parent,
            objectData[cls._allColumns().index(cls._objectSchema.RESOURCE_NAME)],
            objectData[cls._allColumns().index(cls._objectSchema.UID)],
        )

        for attr, value in zip(child._rowAttributes(), objectData):
            setattr(child, attr, value)
        child._created = parseSQLTimestamp(child._created)
        child._modified = parseSQLTimestamp(child._modified)

        yield child._loadPropertyStore(propstore)

        returnValue(child)

    @classmethod
    @inlineCallbacks
    def _getDBData(cls, parent, name, uid, resourceID):
        """
        Given a set of identifying information, load the data rows for the object. Only one of
        L{name}, L{uid} or L{resourceID} is specified - others are C{None}.

        @param parent: the parent collection object
        @type parent: L{CommonHomeChild}
        @param name: the resource name
        @type name: C{str}
        @param uid: the UID of the data
        @type uid: C{str}
        @param resourceID: the resource ID
        @type resourceID: C{int}
        """

        rows = None
        parentID = parent._resourceID
        if name:
            rows = yield cls._allColumnsWithParentAndName.on(
                parent._txn,
                name=name,
                parentID=parentID
            )
        elif uid:
            rows = yield cls._allColumnsWithParentAndUID.on(
                parent._txn,
                uid=uid,
                parentID=parentID
            )
        elif resourceID:
            rows = yield cls._allColumnsWithParentAndID.on(
                parent._txn,
                resourceID=resourceID,
                parentID=parentID
            )

        returnValue(rows[0] if rows else None)

    def __init__(self, parent, name, uid, resourceID=None, options=None):
        self._parentCollection = parent
        self._resourceID = resourceID
        self._name = name
        self._uid = uid
        self._md5 = None
        self._size = None
        self._created = None
        self._modified = None
        self._dataversion = None
        self._textData = None
        self._cachedComponent = None

        self._locked = False

    @classproperty
    def _allColumnsWithParentQuery(cls):
        obj = cls._objectSchema
        return Select(cls._allColumns(), From=obj,
                      Where=obj.PARENT_RESOURCE_ID == Parameter("parentID"))

    @classmethod
    @inlineCallbacks
    def _allColumnsWithParent(cls, parent):
        returnValue((yield cls._allColumnsWithParentQuery.on(
            parent._txn, parentID=parent._resourceID)))

    @classmethod
    @inlineCallbacks
    def loadAllObjects(cls, parent):
        """
        Load all child objects and return a list of them. This must create the
        child classes and initialize them using "batched" SQL operations to keep
        this constant wrt the number of children. This is an optimization for
        Depth:1 operations on the collection.
        """

        results = []

        # Load from the main table first
        dataRows = yield cls._allColumnsWithParent(parent)

        if dataRows:
            # Get property stores for all these child resources (if any found)
            if parent.objectResourcesHaveProperties():
                propertyStores = (yield PropertyStore.forMultipleResources(
                    parent.ownerHome().uid(),
                    parent.viewerHome().uid(),
                    parent._home.authzuid(),
                    parent._txn,
                    cls._objectSchema.RESOURCE_ID,
                    cls._objectSchema.PARENT_RESOURCE_ID,
                    parent._resourceID
                ))
            else:
                propertyStores = {}

        # Create the actual objects merging in properties
        for row in dataRows:
            resourceID = row[cls._allColumns().index(cls._objectSchema.RESOURCE_ID)]
            propstore = propertyStores.get(resourceID, None)

            child = yield cls.makeClass(parent, row, propstore=propstore)
            results.append(child)

        returnValue(results)

    @classmethod
    @inlineCallbacks
    def loadAllObjectsWithNames(cls, parent, names):
        """
        Load all child objects with the specified names, doing so in batches (because we need to match
        using SQL "resource_name in (...)" where there might be a character length limit on the number
        of items in the set).
        """
        names = tuple(names)
        results = []
        while(len(names)):
            result_batch = (yield cls._loadAllObjectsWithNames(parent, names[:cls.BATCH_LOAD_SIZE]))
            results.extend(result_batch)
            names = names[cls.BATCH_LOAD_SIZE:]

        returnValue(results)

    @classmethod
    @inlineCallbacks
    def listObjects(cls, parent):
        """
        Query to load all object resource names for a home child.
        """
        obj = cls._objectSchema
        rows = yield Select(
            [obj.RESOURCE_NAME],
            From=obj,
            Where=(obj.PARENT_RESOURCE_ID == Parameter('parentID'))
        ).on(parent._txn, parentID=parent.id())
        returnValue(sorted([row[0] for row in rows]))

    @classmethod
    @inlineCallbacks
    def countObjects(cls, parent):
        obj = cls._objectSchema
        rows = yield Select(
            [Count(ALL_COLUMNS)],
            From=obj,
            Where=obj.PARENT_RESOURCE_ID == Parameter('parentID')
        ).on(parent._txn, parentID=parent.id())
        returnValue(rows[0][0])

    @classmethod
    def _allColumnsWithParentAndNamesQuery(cls, names):
        obj = cls._objectSchema
        return Select(cls._allColumns(), From=obj,
                      Where=(obj.PARENT_RESOURCE_ID == Parameter("parentID")).And(
                          obj.RESOURCE_NAME.In(Parameter("names", len(names)))))

    @classmethod
    @inlineCallbacks
    def _allColumnsWithParentAndNames(cls, parent, names):
        returnValue((yield cls._allColumnsWithParentAndNamesQuery(names).on(
            parent._txn, parentID=parent._resourceID, names=names)))

    @classmethod
    @inlineCallbacks
    def _loadAllObjectsWithNames(cls, parent, names):
        """
        Load all child objects with the specified names. This must create the
        child classes and initialize them using "batched" SQL operations to keep
        this constant wrt the number of children. This is an optimization for
        Depth:1 operations on the collection.
        """

        # Optimize case of single name to load
        if len(names) == 1:
            obj = yield cls.objectWithName(parent, names[0])
            returnValue([obj] if obj else [])

        results = []

        # Load from the main table first
        dataRows = yield cls._allColumnsWithParentAndNames(parent, names)

        if dataRows:
            # Get property stores for all these child resources
            if parent.objectResourcesHaveProperties():
                propertyStores = (yield PropertyStore.forMultipleResourcesWithResourceIDs(
                    parent.ownerHome().uid(),
                    parent.viewerHome().uid(),
                    parent.ownerHome().authzuid(),
                    parent._txn,
                    tuple([row[0] for row in dataRows]),
                ))
            else:
                propertyStores = {}

        # Create the actual objects merging in properties
        for row in dataRows:
            resourceID = row[cls._allColumns().index(cls._objectSchema.RESOURCE_ID)]
            propstore = propertyStores.get(resourceID, None)

            child = yield cls.makeClass(parent, row, propstore=propstore)
            results.append(child)

        returnValue(results)

    @classmethod
    def objectWithName(cls, parent, name):
        return cls.objectWith(parent, name=name)

    @classmethod
    def objectWithUID(cls, parent, uid):
        return cls.objectWith(parent, uid=uid)

    @classmethod
    def objectWithID(cls, parent, resourceID):
        return cls.objectWith(parent, resourceID=resourceID)

    @classmethod
    @inlineCallbacks
    def objectWith(cls, parent, name=None, uid=None, resourceID=None):
        """
        Create the object using one of the specified arguments as the key to load it. One
        and only one of the keyword arguments must be set.

        @param parent: parent collection
        @type parent: L{CommonHomeChild}
        @param name: name of the resource, or C{None}
        @type name: C{str}
        @param uid: resource data UID, or C{None}
        @type uid: C{str}
        @param resourceID: resource id
        @type resourceID: C{int}

        @return: the new object or C{None} if not found
        @rtype: C{CommonObjectResource}
        """

        row = yield cls._getDBData(parent, name, uid, resourceID)

        if row:
            child = yield cls.makeClass(parent, row)
            returnValue(child)
        else:
            returnValue(None)

    @classmethod
    @inlineCallbacks
    def resourceNameForUID(cls, parent, uid):
        """
        Query to retrieve the resource name for an object resource based on
        its UID column.
        """
        obj = cls._objectSchema
        rows = yield Select(
            [obj.RESOURCE_NAME],
            From=obj,
            Where=(obj.UID == Parameter("uid")).And(
                obj.PARENT_RESOURCE_ID == Parameter("parentID"))
        ).on(parent._txn, uid=uid, parentID=parent.id())
        returnValue(rows[0][0] if rows else "")

    @classmethod
    @inlineCallbacks
    def resourceUIDForName(cls, parent, name):
        """
        Query to retrieve the UID for an object resource based on its
        resource name column.
        """
        obj = cls._objectSchema
        rows = yield Select(
            [obj.UID],
            From=obj,
            Where=(obj.RESOURCE_NAME == Parameter("name")).And(
                obj.PARENT_RESOURCE_ID == Parameter("parentID"))
        ).on(parent._txn, name=name, parentID=parent.id())
        returnValue(rows[0][0] if rows else "")

    @classmethod
    @inlineCallbacks
    def create(cls, parent, name, component, options=None):

        child = (yield parent.objectResourceWithName(name))
        if child:
            raise ObjectResourceNameAlreadyExistsError(name)

        if name.startswith("."):
            raise ObjectResourceNameNotAllowedError(name)

        if len(name) > 255:
            raise ObjectResourceNameNotAllowedError(name)

        c = cls._externalClass if parent.externalClass() else cls
        objectResource = c(parent, name, None, None, options=options)
        yield objectResource.setComponent(component, inserting=True)
        yield objectResource._loadPropertyStore(created=True)

        # Note: setComponent triggers a notification, so we don't need to
        # call notify( ) here like we do for object removal.
        returnValue(objectResource)

    @classmethod
    def _allColumnsWithParentAnd(cls, column, paramName):
        """
        DAL query for all columns where PARENT_RESOURCE_ID matches a parentID
        parameter and a given instance column matches a given parameter name.
        """
        return Select(
            cls._allColumns(), From=cls._objectSchema,
            Where=(column == Parameter(paramName)).And(
                cls._objectSchema.PARENT_RESOURCE_ID == Parameter("parentID"))
        )

    @classproperty
    def _allColumnsWithParentAndName(cls):
        return cls._allColumnsWithParentAnd(cls._objectSchema.RESOURCE_NAME, "name")

    @classproperty
    def _allColumnsWithParentAndUID(cls):
        return cls._allColumnsWithParentAnd(cls._objectSchema.UID, "uid")

    @classproperty
    def _allColumnsWithParentAndID(cls):
        return cls._allColumnsWithParentAnd(cls._objectSchema.RESOURCE_ID, "resourceID")

    @classmethod
    def _allColumns(cls):
        """
        Full set of columns in the object table that need to be loaded to
        initialize the object resource state.
        """
        obj = cls._objectSchema
        return [
            obj.RESOURCE_ID,
            obj.RESOURCE_NAME,
            obj.UID,
            obj.MD5,
            Len(obj.TEXT),
            obj.CREATED,
            obj.MODIFIED,
            obj.DATAVERSION,
        ]

    @classmethod
    def _rowAttributes(cls):
        """
        Object attributes used to store the column values from L{_allColumns}. This used to create
        a mapping when serializing the object for cross-pod requests.
        """
        return (
            "_resourceID",
            "_name",
            "_uid",
            "_md5",
            "_size",
            "_created",
            "_modified",
            "_dataversion",
        )

    @classmethod
    def _otherSerializedAttributes(cls):
        """
        Other object attributes used for serialization.
        """
        return (
            "_componentChanged",
        )

    def serialize(self):
        """
        Create a dictionary mapping key attributes so this object can be sent over a cross-pod call
        and reconstituted at the other end. Note that the other end may have a different schema so
        the attributes may not match exactly and will need to be processed accordingly.
        """
        data = dict([(attr[1:], getattr(self, attr, None)) for attr in itertools.chain(self._rowAttributes(), self._otherSerializedAttributes())])
        if data["trashed"]:
            data["trashed"] = data["trashed"].isoformat(" ")
        data["created"] = data["created"].isoformat(" ")
        data["modified"] = data["modified"].isoformat(" ")
        return data

    @classmethod
    @inlineCallbacks
    def deserialize(cls, parent, mapping):
        """
        Given a mapping generated by L{serialize}, convert the values into an array of database
        like items that conforms to the ordering of L{_allColumns} so it can be fed into L{makeClass}.
        Note that there may be a schema mismatch with the external data, so treat missing items as
        C{None} and ignore extra items.
        """

        child = yield cls.makeClass(parent, [mapping.get(row[1:]) for row in cls._rowAttributes()])
        for attr in cls._otherSerializedAttributes():
            setattr(child, attr, mapping.get(attr[1:]))
        returnValue(child)

    @inlineCallbacks
    def _loadPropertyStore(self, props=None, created=False):
        if props is None:
            if self._parentCollection.objectResourcesHaveProperties():
                props = yield PropertyStore.load(
                    self._parentCollection.ownerHome().uid(),
                    self._parentCollection.viewerHome().uid(),
                    self._parentCollection.viewerHome().authzuid(),
                    self._txn,
                    self._resourceID,
                    created=created
                )
            else:
                props = NonePropertyStore(self._parentCollection.ownerHome().uid())
        self.initPropertyStore(props)
        self._propertyStore = props

    def properties(self):
        return self._propertyStore

    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """
        pass

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._resourceID)

    def id(self):
        """
        Retrieve the store identifier for this object resource.

        @return: store identifier.
        @rtype: C{int}
        """
        return self._resourceID

    @property
    def _txn(self):
        return self._parentCollection._txn

    @property
    def _home(self):
        return self._parentCollection._home

    def transaction(self):
        return self._parentCollection._txn

    def directoryService(self):
        return self._txn.store().directoryService()

    def parentCollection(self):
        return self._parentCollection

    def owned(self):
        return self._parentCollection.owned()

    def ownerHome(self):
        return self._parentCollection.ownerHome()

    def viewerHome(self):
        return self._parentCollection.viewerHome()

    @classmethod
    def _selectForUpdateQuery(cls, nowait):
        """
        DAL statement to lock a L{CommonObjectResource} by its resource ID.
        """
        return Select(From=cls._objectSchema, ForUpdate=True, NoWait=nowait, Where=cls._objectSchema.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def lock(self, wait=True, txn=None):
        """
        Attempt to obtain a row lock on the object resource. 'wait' determines whether the DB call will
        block on any existing lock held by someone else. Lock will remain until
        transaction is complete, or fail if resource is missing, or it is already locked
        and wait=False is used. Occasionally we need to lock via a separate transaction so we
        pass that in too.

        @param wait: whether or not to wait on someone else's lock
        @type wait: C{bool}
        @param txn: alternative transaction to use
        @type txn: L{CommonStoreTransaction}

        @raise: L{NoSuchObjectResourceError} if resource does not exist, other L{Exception}
                if already locked and NOWAIT is used.
        """

        txn = txn if txn is not None else self._txn
        yield self._selectForUpdateQuery(not wait).on(txn, NoSuchObjectResourceError, resourceID=self._resourceID)
        self._locked = True

    def setComponent(self, component, inserting=False, options=None):
        raise NotImplementedError

    def component(self):
        raise NotImplementedError

    @inlineCallbacks
    def componentType(self):
        returnValue((yield self.component()).mainType())

    @classproperty
    def _deleteQuery(cls):
        """
        DAL statement to delete a L{CommonObjectResource} by its resource ID.
        """
        return Delete(cls._objectSchema, Where=cls._objectSchema.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def moveTo(self, destination, name=None):
        """
        Move object to another collection.

        @param destination: parent collection to move to
        @type destination: L{CommonHomeChild}
        @param name: new name in destination
        @type name: C{str} or C{None} to use existing name
        """

        yield self.moveValidation(destination, name)

        # If possible we do a "fast" move by simply fixing up the database information directly rather than
        # re-writing any data. That is only possible when the source and destination are on this pod.
        if not self._parentCollection.external() and not destination.external():
            yield self._parentCollection.moveObjectResource(self, destination, name)
        else:
            yield self._parentCollection.moveObjectResourceCreateDelete(self, destination, name)

    def moveValidation(self, destination, name):
        raise NotImplementedError

    def remove(self):
        """
        If trash is enabled move the object to the trash, otherwise fully delete it
        """

        if config.EnableTrashCollection:
            if self._parentCollection.isTrash():
                raise AlreadyInTrashError
            else:
                return self.toTrash()
        else:
            return self._reallyRemove()

    @inlineCallbacks
    def _reallyRemove(self):
        """
        Remove, bypassing the trash
        """
        yield self._deleteQuery.on(self._txn, NoSuchObjectResourceError,
                                   resourceID=self._resourceID)
        yield self.properties()._removeResource()

        yield self._parentCollection.removedObjectResource(self)

        # Set to non-existent state
        self._resourceID = None
        self._name = None
        self._uid = None
        self._md5 = None
        self._size = None
        self._created = None
        self._modified = None
        self._textData = None
        self._cachedComponent = None

    @classproperty
    def _updateToTrashQuery(cls):
        obj = cls._objectSchema
        return Update(
            {obj.ORIGINAL_COLLECTION: Parameter("originalCollection"), obj.TRASHED: Parameter("trashed")},
            Where=obj.RESOURCE_ID == Parameter("resourceID"),
        )

    @classproperty
    def _updateFromTrashQuery(cls):
        obj = cls._objectSchema
        return Update(
            {obj.ORIGINAL_COLLECTION: None, obj.TRASHED: None},
            Where=obj.RESOURCE_ID == Parameter("resourceID"),
        )

    @classproperty
    def _selectTrashDataQuery(cls):
        obj = cls._objectSchema
        return Select((obj.ORIGINAL_COLLECTION, obj.TRASHED), From=obj, Where=obj.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def originalCollection(self):
        originalCollectionID, _ignore_whenTrashed = (
            yield self._selectTrashDataQuery.on(
                self._txn, resourceID=self._resourceID
            )
        )[0]
        originalCollection = yield self._parentCollection._home.childWithID(originalCollectionID)
        returnValue(originalCollection)

    @inlineCallbacks
    def toTrash(self):

        # Preserve existing resource name extension
        newName = str(uuid4()) + os.path.splitext(self.name())[1]

        originalCollection = self._parentCollection._resourceID
        trash = yield self._parentCollection.ownerHome().getTrash(create=True)
        yield self.moveTo(trash, name=newName)

        self._original_collection = originalCollection
        self._trashed = datetime.datetime.utcnow()
        yield self._updateToTrashQuery.on(
            self._txn, originalCollection=self._original_collection, trashed=self._trashed, resourceID=self._resourceID
        )
        returnValue(newName)

    @inlineCallbacks
    def fromTrash(self):
        originalCollection = yield self.originalCollection()
        yield self.moveTo(originalCollection)

        self._original_collection = None
        self._trashed = None
        yield self._updateFromTrashQuery.on(
            self._txn, resourceID=self._resourceID
        )
        returnValue(self._name)

    def isInTrash(self):
        return (getattr(self, "_original_collection", None) is not None) or getattr(self, "_isInTrash", False)

    def whenTrashed(self):
        if self._trashed is None:
            return None
        return parseSQLTimestamp(self._trashed)

    def purge(self):
        """
        Delete this object, bypassing trash
        """
        return self._reallyRemove()

    def removeNotifyCategory(self):
        """
        Indicates what category to use when determining the priority of push
        notifications when this object is removed.

        @returns: The "default" category (but should be overridden to return
            values such as "inbox")
        @rtype: L{ChangeCategory}
        """
        return ChangeCategory.default

    def uid(self):
        return self._uid

    def name(self):
        return self._name

    # IDataStoreObject
    def contentType(self):
        raise NotImplementedError()

    def md5(self):
        return self._md5

    def size(self):
        return self._size

    def created(self):
        return datetimeMktime(self._created)

    def modified(self):
        return datetimeMktime(self._modified)

    @classproperty
    def _textByIDQuery(cls):
        """
        DAL query to load iCalendar/vCard text via an object's resource ID.
        """
        obj = cls._objectSchema
        return Select([obj.TEXT], From=obj,
                      Where=obj.RESOURCE_ID == Parameter("resourceID"))

    @inlineCallbacks
    def _text(self):
        if self._textData is None:
            texts = (
                yield self._textByIDQuery.on(self._txn,
                                             resourceID=self._resourceID)
            )
            if texts:
                text = texts[0][0]
                self._textData = text
                returnValue(text)
            else:
                raise ConcurrentModification()
        else:
            returnValue(self._textData)
