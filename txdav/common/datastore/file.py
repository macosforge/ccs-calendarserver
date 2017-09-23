# -*- test-case-name: txdav.caldav.datastore.test.test_file -*-
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
Common utility functions for a file based datastore.
"""

import sys
from twext.internet.decorate import memoizedKey
from twext.python.log import Logger
from txdav.xml.rfc2518 import GETContentType, HRef
from txdav.xml.rfc5842 import ResourceID
from txweb2.http_headers import generateContentType, MimeType
from txweb2.dav.resource import TwistedGETContentMD5, \
    TwistedQuotaUsedProperty

from twisted.internet.defer import succeed, inlineCallbacks, returnValue
from twisted.python.util import FancyEqMixin
from twisted.python.failure import Failure

from twistedcaldav import customxml
from twistedcaldav.customxml import NotificationType
from twistedcaldav.notifications import NotificationRecord
from twistedcaldav.notifications import NotificationsDatabase as OldNotificationIndex
from txdav.caldav.icalendarstore import ICalendarStore

from txdav.common.datastore.common import HomeChildBase
from txdav.common.datastore.sql_tables import _BIND_MODE_OWN
from txdav.common.icommondatastore import HomeChildNameNotAllowedError, \
    HomeChildNameAlreadyExistsError, NoSuchHomeChildError, \
    InternalDataStoreError, ObjectResourceNameNotAllowedError, \
    ObjectResourceNameAlreadyExistsError, NoSuchObjectResourceError, \
    ECALENDARTYPE, EADDRESSBOOKTYPE
from txdav.common.idirectoryservice import IStoreDirectoryService
from txdav.common.inotifications import INotificationCollection, \
    INotificationObject
from txdav.base.datastore.file import DataStoreTransaction, DataStore, writeOperation, \
    hidden, isValidName, FileMetaDataMixin
from txdav.base.datastore.util import cached

from txdav.base.propertystore.base import PropertyName
from txdav.base.propertystore.none import PropertyStore as NonePropertyStore
from txdav.base.propertystore.xattr import PropertyStore as XattrPropertyStore

from errno import EEXIST, ENOENT
from zope.interface import implements, directlyProvides

import hashlib
import json
import uuid
from twistedcaldav.sql import AbstractSQLDatabase, db_prefix
import os

TOPPATHS = (
    "calendars",
    "addressbooks"
)
UIDPATH = "__uids__"


class _StubQueuer(object):
    pass


class CommonDataStore(DataStore):
    """
    Shared logic for SQL-based data stores, between calendar and addressbook
    storage.

    @ivar _path: A L{CachingFilePath} referencing a directory on disk that
        stores all calendar and addressbook data for a group of UIDs.

    @ivar quota: the amount of space granted to each calendar home (in bytes)
        for storing attachments, or C{None} if quota should not be enforced.
    @type quota: C{int} or C{NoneType}

    @ivar _propertyStoreClass: The class (or callable object / factory) that
        produces an L{IPropertyStore} provider for a path.  This has the
        signature of the L{XattrPropertyStore} type: take 2 arguments
        C{(default-user-uid, path-factory)}, return an L{IPropertyStore}
        provider.

    @ivar queuer: For compatibility with SQL-based store; currently a
        non-functional implementation just for tests, but could be fixed to be
        backed by SQLite or something.
    """
    implements(ICalendarStore)

    def __init__(
        self,
        path,
        notifierFactories,
        directoryService,
        enableCalendars=True,
        enableAddressBooks=True,
        quota=(2 ** 20),
        propertyStoreClass=XattrPropertyStore
    ):
        """
        Create a store.

        @param path: a L{FilePath} pointing at a directory on disk.
        """
        assert enableCalendars or enableAddressBooks

        super(CommonDataStore, self).__init__(path)
        self._directoryService = IStoreDirectoryService(directoryService) if directoryService is not None else None
        self.enableCalendars = enableCalendars
        self.enableAddressBooks = enableAddressBooks
        self._notifierFactories = notifierFactories if notifierFactories is not None else {}
        self._transactionClass = CommonStoreTransaction
        self._propertyStoreClass = propertyStoreClass
        self.quota = quota
        self._migrating = False
        self._enableNotifications = True
        self._newTransactionCallbacks = set()
        # FIXME: see '@ivar queuer' above.
        self.queuer = _StubQueuer()

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

    def newTransaction(self, name='no name'):
        """
        Create a new transaction.

        @see: L{Transaction}
        """
        txn = self._transactionClass(
            self,
            name,
            self.enableCalendars,
            self.enableAddressBooks,
            self._notifierFactories if self._enableNotifications else None,
            self._migrating,
        )
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

    @inlineCallbacks
    def _withEachHomeDo(self, enumerator, action, batchSize):
        """
        Implementation of L{ICalendarStore.withEachCalendarHomeDo} and
        L{IAddressBookStore.withEachAddressbookHomeDo}.
        """
        for txn, home in enumerator():
            try:
                yield action(txn, home)
            except:
                a, b, c = sys.exc_info()
                yield txn.abort()
                raise a(b, c)
            else:
                yield txn.commit()

    def withEachCalendarHomeDo(self, action, batchSize=None):
        """
        Implementation of L{ICalendarStore.withEachCalendarHomeDo}.
        """
        return self._withEachHomeDo(self._eachCalendarHome, action, batchSize)

    def withEachAddressbookHomeDo(self, action, batchSize=None):
        """
        Implementation of L{ICalendarStore.withEachCalendarHomeDo}.
        """
        return self._withEachHomeDo(self._eachAddressbookHome, action,
                                    batchSize)

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

    def _homesOfType(self, storeType):
        """
        Common implementation of L{_eachCalendarHome} and
        L{_eachAddressbookHome}; see those for a description of the return
        type.

        @param storeType: one of L{EADDRESSBOOKTYPE} or L{ECALENDARTYPE}.
        """
        top = self._path.child(TOPPATHS[storeType]).child(UIDPATH)
        if top.exists() and top.isdir():
            for firstPrefix in top.children():
                if not isValidName(firstPrefix.basename()):
                    continue
                for secondPrefix in firstPrefix.children():
                    if not isValidName(secondPrefix.basename()):
                        continue
                    for actualHome in secondPrefix.children():
                        uid = actualHome.basename()
                        if not isValidName(uid):
                            continue
                        txn = self.newTransaction("enumerate home %r" % (uid,))
                        home = txn.homeWithUID(storeType, uid, False)
                        if home is not None:
                            yield (txn, home)

    def _eachCalendarHome(self):
        return self._homesOfType(ECALENDARTYPE)

    def _eachAddressbookHome(self):
        return self._homesOfType(EADDRESSBOOKTYPE)


class CommonStoreTransaction(DataStoreTransaction):
    """
    In-memory implementation of

    Note that this provides basic 'undo' support, but not truly transactional
    operations.
    """

    _homeClass = {}

    def __init__(self, dataStore, name, enableCalendars, enableAddressBooks, notifierFactories, migrating=False):
        """
        Initialize a transaction; do not call this directly, instead call
        L{DataStore.newTransaction}.

        @param dataStore: The store that created this transaction.

        @type dataStore: L{CommonDataStore}
        """
        from txdav.caldav.icalendarstore import ICalendarTransaction
        from txdav.carddav.iaddressbookstore import IAddressBookTransaction
        from txdav.caldav.datastore.file import CalendarHome
        from txdav.carddav.datastore.file import AddressBookHome

        super(CommonStoreTransaction, self).__init__(dataStore, name)
        self._calendarHomes = {}
        self._addressbookHomes = {}
        self._notificationHomes = {}
        self._notifierFactories = notifierFactories
        self._notifiedAlready = set()
        self._migrating = migrating

        extraInterfaces = []
        if enableCalendars:
            extraInterfaces.append(ICalendarTransaction)
            self._notificationHomeType = ECALENDARTYPE
        else:
            self._notificationHomeType = EADDRESSBOOKTYPE
        if enableAddressBooks:
            extraInterfaces.append(IAddressBookTransaction)
        directlyProvides(self, *extraInterfaces)

        CommonStoreTransaction._homeClass[ECALENDARTYPE] = CalendarHome
        CommonStoreTransaction._homeClass[EADDRESSBOOKTYPE] = AddressBookHome

    def calendarHomeWithUID(self, uid, status=None, create=False):
        return self.homeWithUID(ECALENDARTYPE, uid, status=status, create=create)

    def addressbookHomeWithUID(self, uid, status=None, create=False):
        return self.homeWithUID(EADDRESSBOOKTYPE, uid, status=status, create=create)

    def _determineMemo(self, storeType, uid, status=None, create=False):
        """
        Determine the memo dictionary to use for homeWithUID.
        """
        if storeType == ECALENDARTYPE:
            return self._calendarHomes
        else:
            return self._addressbookHomes

    def homes(self, storeType):
        """
        Load all calendar or addressbook homes.
        """
        uids = self._homeClass[storeType].listHomes(self)
        for uid in uids:
            self.homeWithUID(storeType, uid, create=False)

        # Return the memoized list directly
        returnValue([kv[1] for kv in sorted(self._determineMemo(storeType, None).items(), key=lambda x: x[0])])

    @memoizedKey("uid", _determineMemo, deferredResult=False)
    def homeWithUID(self, storeType, uid, status=None, create=False):
        if uid.startswith("."):
            return None

        if storeType not in (ECALENDARTYPE, EADDRESSBOOKTYPE):
            raise RuntimeError("Unknown home type.")

        return self._homeClass[storeType].homeWithUID(self, uid, create, storeType == ECALENDARTYPE)

    @memoizedKey("uid", "_notificationHomes", deferredResult=False)
    def notificationsWithUID(self, uid, home=None, create=False):

        if home is None:
            home = self.homeWithUID(self._notificationHomeType, uid, create=True)
        return NotificationCollection.notificationsFromHome(self, home)

    # File-based storage of APN subscriptions not implementated.
    def addAPNSubscription(self, token, key, timestamp, subscriber, userAgent, ipAddr):
        return NotImplementedError

    def removeAPNSubscription(self, token, key):
        return NotImplementedError

    def purgeOldAPNSubscriptions(self, purgeSeconds):
        return NotImplementedError

    def apnSubscriptionsByToken(self, token):
        return NotImplementedError

    def apnSubscriptionsByKey(self, key):
        return NotImplementedError

    def apnSubscriptionsBySubscriber(self, guid):
        return NotImplementedError

    def imipCreateToken(self, organizer, attendee, icaluid, token=None):
        return NotImplementedError

    def imipLookupByToken(self, token):
        return NotImplementedError

    def imipGetToken(self, organizer, attendee, icaluid):
        return NotImplementedError

    def imipRemoveToken(self, token):
        return NotImplementedError

    def purgeOldIMIPTokens(self, olderThan):
        return NotImplementedError

    def isNotifiedAlready(self, obj):
        return obj in self._notifiedAlready

    def notificationAddedForObject(self, obj):
        self._notifiedAlready.add(obj)


class StubResource(object):
    """
    Just enough resource to keep the shared sql DB classes going.
    """

    def __init__(self, commonHome):
        self._commonHome = commonHome

    @property
    def fp(self):
        return self._commonHome._path


class SharedCollectionRecord(object):

    def __init__(self, shareuid, sharetype, hosturl, localname, summary):
        self.shareuid = shareuid
        self.sharetype = sharetype
        self.hosturl = hosturl
        self.localname = localname
        self.summary = summary


class SharedCollectionsDatabase(AbstractSQLDatabase):
    log = Logger()

    db_basename = db_prefix + "shares"
    schema_version = "1"
    db_type = "shares"

    def __init__(self, resource):
        """
        @param resource: the L{CalDAVResource} resource for
            the shared collection. C{resource} must be a calendar/addressbook home collection.)
        """
        self.resource = resource
        db_filename = os.path.join(self.resource.fp.path, SharedCollectionsDatabase.db_basename)
        super(SharedCollectionsDatabase, self).__init__(db_filename, True, autocommit=True)

    def get_dbpath(self):
        return self.resource.fp.child(SharedCollectionsDatabase.db_basename).path

    def set_dbpath(self, newpath):
        pass

    dbpath = property(get_dbpath, set_dbpath)

    def create(self):
        """
        Create the index and initialize it.
        """
        self._db()

    def allRecords(self):

        records = self._db_execute("select * from SHARES order by LOCALNAME")
        return [self._makeRecord(row) for row in (records if records is not None else ())]

    def recordForShareUID(self, shareUID):

        row = self._db_execute("select * from SHARES where SHAREUID = :1", shareUID)
        return self._makeRecord(row[0]) if row else None

    def addOrUpdateRecord(self, record):

        self._db_execute(
            """insert or replace into SHARES (SHAREUID, SHARETYPE, HOSTURL, LOCALNAME, SUMMARY)
            values (:1, :2, :3, :4, :5)
            """, record.shareuid, record.sharetype, record.hosturl, record.localname, record.summary,
        )

    def removeRecordForLocalName(self, localname):

        self._db_execute("delete from SHARES where LOCALNAME = :1", localname)

    def removeRecordForShareUID(self, shareUID):

        self._db_execute("delete from SHARES where SHAREUID = :1", shareUID)

    def remove(self):

        self._db_close()
        os.remove(self.dbpath)

    def directShareID(self, shareeHome, sharerCollection):
        return "Direct-%s-%s" % (shareeHome.resourceID(), sharerCollection.resourceID(),)

    def _db_version(self):
        """
        @return: the schema version assigned to this index.
        """
        return SharedCollectionsDatabase.schema_version

    def _db_type(self):
        """
        @return: the collection type assigned to this index.
        """
        return SharedCollectionsDatabase.db_type

    def _db_init_data_tables(self, q):
        """
        Initialise the underlying database tables.
        @param q:           a database cursor to use.
        """
        #
        # SHARES table is the primary table
        #   SHAREUID: UID for this share
        #   SHARETYPE: type of share: "I" for invite, "D" for direct
        #   HOSTURL: URL for data source
        #   LOCALNAME: local path name
        #   SUMMARY: Share summary
        #
        q.execute(
            """
            create table SHARES (
                SHAREUID       text unique,
                SHARETYPE      text(1),
                HOSTURL        text,
                LOCALNAME      text,
                SUMMARY        text
            )
            """
        )

        q.execute(
            """
            create index SHAREUID on SHARES (SHAREUID)
            """
        )
        q.execute(
            """
            create index HOSTURL on SHARES (HOSTURL)
            """
        )
        q.execute(
            """
            create index LOCALNAME on SHARES (LOCALNAME)
            """
        )

    def _db_upgrade_data_tables(self, q, old_version):
        """
        Upgrade the data from an older version of the DB.
        """

        # Nothing to do as we have not changed the schema
        pass

    def _makeRecord(self, row):

        return SharedCollectionRecord(*[str(item) if isinstance(item, unicode) else item for item in row])


class CommonHome(FileMetaDataMixin):
    log = Logger()

    # All these need to be initialized by derived classes for each store type
    _childClass = None
    _topPath = None
    _notifierPrefix = None

    def __init__(self, uid, path, dataStore, transaction):
        self._dataStore = dataStore
        self._uid = uid
        self._path = path
        self._transaction = transaction
        self._notifiers = None
        self._shares = SharedCollectionsDatabase(StubResource(self))
        self._newChildren = {}
        self._removedChildren = set()
        self._cachedChildren = {}

    def quotaAllowedBytes(self):
        return self._transaction.store().quota

    @classmethod
    def listHomes(cls, txn):
        """
        Retrieve the owner UIDs of all existing homes.

        @return: an iterable of C{str}s.
        """
        results = []
        top = txn._dataStore._path.child(cls._topPath)
        if top.exists() and top.isdir() and top.child(UIDPATH).exists():
            for firstPrefix in top.child(UIDPATH).children():
                if not isValidName(firstPrefix.basename()):
                    continue
                for secondPrefix in firstPrefix.children():
                    if not isValidName(secondPrefix.basename()):
                        continue
                    for actualHome in secondPrefix.children():
                        uid = actualHome.basename()
                        if not isValidName(uid):
                            continue
                        results.append(uid)

        return results

    @classmethod
    def homeWithUID(cls, txn, uid, create=False, withNotifications=False):

        assert len(uid) >= 4

        childPathSegments = []
        childPathSegments.append(txn._dataStore._path.child(cls._topPath))
        childPathSegments.append(childPathSegments[-1].child(UIDPATH))
        childPathSegments.append(childPathSegments[-1].child(uid[0:2]))
        childPathSegments.append(childPathSegments[-1].child(uid[2:4]))
        childPath = childPathSegments[-1].child(uid)

        def createDirectory(path):
            try:
                path.createDirectory()
            except (IOError, OSError) as e:
                if e.errno != EEXIST:
                    # Ignore, in case someone else created the
                    # directory while we were trying to as well.
                    raise

        creating = False
        if create:
            # Create intermediate directories
            for child in childPathSegments:
                if not child.isdir():
                    createDirectory(child)

            if childPath.isdir():
                homePath = childPath
            else:
                creating = True
                homePath = childPath.temporarySibling()
                createDirectory(homePath)

                def do():
                    homePath.moveTo(childPath)
                    # do this _after_ all other file operations
                    home._path = childPath
                    return lambda: None
                txn.addOperation(do, "create home UID %r" % (uid,))

        elif not childPath.isdir():
            return None
        else:
            homePath = childPath

        home = cls(uid, homePath, txn._dataStore, txn)
        for factory_name, factory in txn._notifierFactories.items():
            home.addNotifier(factory_name, factory.newNotifier(home))
        if creating:
            home.createdHome()
            if withNotifications:
                txn.notificationsWithUID(uid, home)

        return home

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._path)

    def uid(self):
        return self._uid

    def transaction(self):
        return self._transaction

    def directoryService(self):
        return self._transaction.store().directoryService()

    def directoryRecord(self):
        return self.directoryService().recordWithUID(self.uid().decode("utf-8"))

    def retrieveOldShares(self):
        """
        Retrieve the old Index object.
        """
        return self._shares

    def children(self):
        """
        Return a set of the child resource objects.
        """
        return set(self._newChildren.itervalues()) | set(
            self.childWithName(name)
            for name in self._path.listdir()
            if not name.startswith(".") and
            name not in self._removedChildren
        )

    # For file store there is no efficient "bulk" load of all children so just
    # use the "iterate over each child" method.
    loadChildren = children

    def listChildren(self):
        """
        Return a set of the names of the child resources.
        """
        return sorted(set(
            [child.name() for child in self._newChildren.itervalues()]
        ) | set(
            name
            for name in self._path.listdir()
            if not name.startswith(".") and
            self._path.child(name).isdir() and
            name not in self._removedChildren
        ))

    def listSharedChildren(self):
        """
        Retrieve the names of the children in this home.

        @return: an iterable of C{str}s.
        """
        return [share.localname for share in self._shares.allRecords()]

        if self._childrenLoaded:
            return succeed(self._sharedChildren.keys())
        else:
            return self._childClass.listObjects(self, owned=False)

    def childWithName(self, name):
        child = self._newChildren.get(name)
        if child is not None:
            return child
        if name in self._removedChildren:
            return None
        if name in self._cachedChildren:
            return self._cachedChildren[name]

        if name.startswith("."):
            return None

        child = self._childClass.objectWithName(self, name, True)
        if child is not None:
            self._cachedChildren[name] = child
        return child

    @writeOperation
    def createChildWithName(self, name):
        if name.startswith("."):
            raise HomeChildNameNotAllowedError(name)

        childPath = self._path.child(name)

        if name not in self._removedChildren and childPath.isdir():
            raise HomeChildNameAlreadyExistsError(name)

        temporary = hidden(childPath.temporarySibling())
        temporaryName = temporary.basename()
        temporary.createDirectory()
        # In order for the index to work (which is doing real file ops on disk
        # via SQLite) we need to create a real directory _immediately_.

        # FIXME: some way to roll this back.

        c = self._newChildren[name] = self._childClass(temporary.basename(), self, True, realName=name)
        c.retrieveOldIndex().create()

        def do():
            childPath = self._path.child(name)
            temporary = childPath.sibling(temporaryName)
            try:
                props = c.properties()
                temporary.moveTo(childPath)
                c._name = name
                # FIXME: _lots_ of duplication of work here.
                props.flush()
            except (IOError, OSError) as e:
                if e.errno == EEXIST and childPath.isdir():
                    raise HomeChildNameAlreadyExistsError(name)
                raise
            # FIXME: direct tests, undo for index creation
            # Return undo
            return lambda: self._path.child(childPath.basename()).remove()

        self._transaction.addOperation(do, "create child %r" % (name,))

        self.notifyChanged()
        return c

    @writeOperation
    def removeChildWithName(self, name, useTrash=True):
        if name.startswith(".") or name in self._removedChildren:
            raise NoSuchHomeChildError(name)

        child = self.childWithName(name)
        if child is None:
            raise NoSuchHomeChildError()

        try:
            child.remove()
        finally:
            if name in self._newChildren:
                del self._newChildren[name]
            else:
                self._removedChildren.add(name)

    def getTrash(self, create=False):
        return succeed(None)

    @inlineCallbacks
    def syncToken(self):

        maxrev = 0
        for child in self.children():
            maxrev = max(int((yield child.syncToken()).split("_")[1]), maxrev)

        try:
            urnuuid = str(self.properties()[PropertyName.fromElement(ResourceID)].children[0])
        except KeyError:
            urnuuid = uuid.uuid4().urn
            self.properties()[PropertyName(*ResourceID.qname())] = ResourceID(HRef.fromString(urnuuid))
        returnValue("%s_%s" % (urnuuid[9:], maxrev))

    def resourceNamesSinceToken(self, token, depth):
        deleted = []
        changed = []
        invalid = []
        return succeed((changed, deleted, invalid))

    # @cached
    def properties(self):
        # FIXME: needs tests for actual functionality
        # FIXME: needs to be cached
        # FIXME: transaction tests
        props = self._dataStore._propertyStoreClass(
            self.uid(), lambda: self._path
        )
        self._transaction.addOperation(props.flush, "flush home properties")
        return props

    def objectResourcesWithUID(self, uid, ignore_children=()):
        """
        Return all child object resources with the specified UID, ignoring any in the
        named child collections. The file implementation just iterates all child collections.
        """
        results = []
        for child in self.children():
            if child.name() in ignore_children:
                continue
            object = child.objectResourceWithUID(uid)
            if object:
                results.append(object)
        return results

    def objectResourceWithID(self, rid):
        """
        Return all child object resources with the specified resource-ID.
        """

        # File store does not have resource ids.
        raise NotImplementedError

    def quotaUsedBytes(self):

        try:
            return int(str(self.properties()[PropertyName.fromElement(TwistedQuotaUsedProperty)]))
        except KeyError:
            return 0

    def adjustQuotaUsedBytes(self, delta):
        """
        Adjust quota used. We need to get a lock on the row first so that the adjustment
        is done atomically.
        """

        old_used = self.quotaUsedBytes()
        new_used = old_used + delta
        if new_used < 0:
            self.log.error("Fixing quota adjusted below zero to {used} by change amount {delta}", used=new_used, delta=delta)
            new_used = 0
        self.properties()[PropertyName.fromElement(TwistedQuotaUsedProperty)] = TwistedQuotaUsedProperty(str(new_used))

    def addNotifier(self, factory_name, notifier):
        if self._notifiers is None:
            self._notifiers = {}
        self._notifiers[factory_name] = notifier

    def getNotifier(self, factory_name):
        return self._notifiers.get(factory_name)

    def notifierID(self):
        return (self._notifierPrefix, self.uid(),)

    @inlineCallbacks
    def notifyChanged(self):
        """
        Trigger a notification of a change
        """

        # Only send one set of change notifications per transaction
        if self._notifiers and not self._transaction.isNotifiedAlready(self):
            # cache notifiers run in post commit
            notifier = self._notifiers.get("cache", None)
            if notifier:
                self._transaction.postCommit(notifier.notify)
            # push notifiers add their work items immediately
            notifier = self._notifiers.get("push", None)
            if notifier:
                yield notifier.notify(self._transaction)
            self._transaction.notificationAddedForObject(self)


class CommonHomeChild(FileMetaDataMixin, FancyEqMixin, HomeChildBase):
    """
    Common ancestor class of AddressBooks and Calendars.
    """
    log = Logger()

    compareAttributes = (
        "_name",
        "_home",
        "_transaction",
    )

    _objectResourceClass = None

    def __init__(self, name, home, owned, realName=None):
        """
        Initialize an home child pointing at a path on disk.

        @param name: the subdirectory of home where this child
            resides.
        @type name: C{str}

        @param home: the home containing this child.
        @type home: L{CommonHome}

        @param realName: If this child was just created, the name which it
        will eventually have on disk.
        @type realName: C{str}
        """
        self._name = name
        self._home = home
        self._owned = owned
        self._transaction = home._transaction
        self._newObjectResources = {}
        self._cachedObjectResources = {}
        self._removedObjectResources = set()
        self._index = None  # Derived classes need to set this
        self._invites = None  # Derived classes need to set this
        self._renamedName = realName

        if self._home._notifiers:
            self._notifiers = dict([(factory_name, notifier.clone(self),) for factory_name, notifier in self._home._notifiers.items()])
        else:
            self._notifiers = None

    @classmethod
    def objectWithName(cls, home, name, owned):
        return cls(name, home, owned) if home._path.child(name).isdir() else None

    @property
    def _path(self):
        return self._home._path.child(self._name)

    @property
    def _txn(self):
        return self._transaction

    def directoryService(self):
        return self._transaction.store().directoryService()

    def retrieveOldIndex(self):
        """
        Retrieve the old Index object.
        """
        return self._index._oldIndex

    def retrieveOldInvites(self):
        """
        Retrieve the old Invites DB object.
        """
        return self._invites._oldInvites

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._path.path)

    def name(self):
        if self._renamedName is not None:
            return self._renamedName
        return self._path.basename()

    def shareMode(self):
        """
        Stub implementation of L{ICalendar.shareMode}; always returns L{_BIND_MODE_OWN}.
        """
        return _BIND_MODE_OWN

    def effectiveShareMode(self):
        """
        Stub implementation of L{ICalendar.effectiveShareMode}; always returns L{_BIND_MODE_OWN}.
        """
        return _BIND_MODE_OWN

    def owned(self):
        return self._owned

    _renamedName = None

    @writeOperation
    def rename(self, name):
        oldName = self.name()
        self._renamedName = name
        self._home._newChildren[name] = self
        self._home._removedChildren.add(oldName)

        def doIt():
            self._path.moveTo(self._path.sibling(name))
            return lambda: None  # FIXME: revert
        self._transaction.addOperation(doIt, "rename home child %r -> %r" %
                                       (oldName, name))

        self.retrieveOldIndex().bumpRevision()

        self.notifyChanged()

    @writeOperation
    def remove(self):

        def do(transaction=self._transaction):
            childPath = self._path
            for i in xrange(1000):
                trash = childPath.sibling("._del_%s_%d" % (childPath.basename(), i))
                if not trash.exists():
                    break
            else:
                raise InternalDataStoreError("Unable to create trash target for child at %s" % (childPath,))

            try:
                childPath.moveTo(trash)
            except (IOError, OSError) as e:
                if e.errno == ENOENT:
                    raise NoSuchHomeChildError(self._name)
                raise

            def cleanup():
                try:
                    self.retrieveOldIndex()._db_close()  # Must close sqlite file before it is deleted
                    trash.remove()
                    self.properties()._removeResource()
                except Exception as e:
                    self.log.error("Unable to delete trashed child at {path}: {ex}", path=trash.fp, ex=e)

            self._transaction.addOperation(cleanup, "remove child backup %r" % (self._name,))

            def undo():
                trash.moveTo(childPath)

            return undo

        # FIXME: direct tests
        self._transaction.addOperation(
            do, "prepare child remove %r" % (self._name,)
        )

        self.notifyChanged()

    def ownerHome(self):
        return self._home

    def viewerHome(self):
        return self._home

    def setSharingUID(self, uid):
        self.properties()._setPerUserUID(uid)

    def objectResources(self):
        """
        Return a list of object resource objects.
        """
        return [self.objectResourceWithName(name)
                for name in self.listObjectResources()]

    def objectResourcesWithNames(self, names):
        """
        Return a list of the specified object resource objects.
        """
        results = []
        for name in names:
            obj = self.objectResourceWithName(name)
            if obj is not None:
                results.append(obj)
        return results

    def listObjectResources(self):
        """
        Return a list of object resource names.
        """
        return sorted((
            name
            for name in (
                set(self._newObjectResources.iterkeys()) |
                set(p.basename() for p in self._path.children()
                    if not p.basename().startswith(".") and
                    p.isfile()) -
                set(self._removedObjectResources)
            ))
        )

    def countObjectResources(self):
        return len(self.listObjectResources())

    def objectResourceWithName(self, name):
        if name in self._removedObjectResources:
            return None
        if name in self._newObjectResources:
            return self._newObjectResources[name]
        if name in self._cachedObjectResources:
            return self._cachedObjectResources[name]

        objectResourcePath = self._path.child(name)
        if objectResourcePath.isfile():
            obj = self._objectResourceClass(name, self)
            self._cachedObjectResources[name] = obj
            return obj
        else:
            return None

    def objectResourceWithUID(self, uid):
        rname = self.retrieveOldIndex().resourceNameForUID(uid)
        if rname and rname not in self._removedObjectResources:
            return self.objectResourceWithName(rname)

        return None

    @writeOperation
    def createObjectResourceWithName(self, name, component, metadata=None):
        """
        Create a new resource with component data and optional metadata. We create the
        python object using the metadata then create the actual store object with setComponent.
        """
        if name.startswith("."):
            raise ObjectResourceNameNotAllowedError(name)

        if len(name) > 255:
            raise ObjectResourceNameNotAllowedError(name)

        objectResourcePath = self._path.child(name)
        if objectResourcePath.exists():
            raise ObjectResourceNameAlreadyExistsError(name)

        objectResource = self._objectResourceClass(name, self, metadata)
        objectResource.setComponent(component, inserting=True)
        self._cachedObjectResources[name] = objectResource

        # Note: setComponent triggers a notification, so we don't need to
        # call notify( ) here like we do for object removal.

        return objectResource

    def removedObjectResource(self, child):

        self.retrieveOldIndex().deleteResource(child.name())

        self._removedObjectResources.add(child.name())
        self.notifyChanged()

    def syncToken(self):

        try:
            urnuuid = str(self.properties()[PropertyName.fromElement(ResourceID)].children[0])
        except KeyError:
            urnuuid = uuid.uuid4().urn
            self.properties()[PropertyName(*ResourceID.qname())] = ResourceID(HRef.fromString(urnuuid))
        return succeed("%s_%s" % (urnuuid[9:], self.retrieveOldIndex().lastRevision()))

    def objectResourcesSinceToken(self, token):
        raise NotImplementedError()

    def resourceNamesSinceToken(self, token):
        return succeed(self.retrieveOldIndex().whatchanged(token))

    def objectResourcesHaveProperties(self):
        """
        So filestore objects do need to support properties.
        """
        return True

    # FIXME: property writes should be a write operation
    @cached
    def properties(self):
        # FIXME: needs direct tests - only covered by store tests
        # FIXME: transactions
        propStoreClass = self._home._dataStore._propertyStoreClass
        props = propStoreClass(self._home.uid(), lambda: self._path)
        self.initPropertyStore(props)

        self._transaction.addOperation(props.flush,
                                       "flush object resource properties")
        return props

    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """
        pass

    def addNotifier(self, factory_name, notifier):
        if self._notifiers is None:
            self._notifiers = {}
        self._notifiers[factory_name] = notifier

    def getNotifier(self, factory_name):
        return self._notifiers.get(factory_name) if self._notifiers else None

    def notifierID(self):
        return (self.ownerHome()._notifierPrefix, "%s/%s" % (self.ownerHome().uid(), self.name(),),)

    def parentNotifierID(self):
        return self.ownerHome().notifierID()

    @inlineCallbacks
    def notifyChanged(self):
        """
        Trigger a notification of a change
        """

        # Only send one set of change notifications per transaction
        if self._notifiers and not self._transaction.isNotifiedAlready(self):
            # cache notifiers run in post commit
            notifier = self._notifiers.get("cache", None)
            if notifier:
                self._transaction.postCommit(notifier.notify)
            # push notifiers add their work items immediately
            notifier = self._notifiers.get("push", None)
            if notifier:
                yield notifier.notify(self._transaction)
            self._transaction.notificationAddedForObject(self)

    @inlineCallbacks
    def sharingInvites(self):
        """
        Stub for interface-compliance tests.
        """
        yield None
        returnValue([])


class CommonObjectResource(FileMetaDataMixin, FancyEqMixin):
    """
    @ivar _path: The path of the file on disk

    @type _path: L{FilePath}
    """
    log = Logger()

    compareAttributes = (
        "_name",
        "_parentCollection",
    )

    def __init__(self, name, parent, metadata=None):
        self._name = name
        self._parentCollection = parent
        self._transaction = parent._transaction
        self._objectText = None

    @property
    def _path(self):
        return self._parentCollection._path.child(self._name)

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._path.path)

    @property
    def _txn(self):
        return self._transaction

    def transaction(self):
        return self._transaction

    def directoryService(self):
        return self._transaction.store().directoryService()

    @writeOperation
    def setComponent(self, component, inserting=False):
        raise NotImplementedError

    def component(self):
        raise NotImplementedError

    def remove(self):

        # FIXME: test for undo
        objectResourcePath = self._path

        def do():
            objectResourcePath.remove()
            return lambda: None
        self._transaction.addOperation(do, "remove object resource object %r" % (self._name,))

        self._parentCollection.removedObjectResource(self)

    purge = remove

    def _text(self):
        raise NotImplementedError

    def uid(self):
        raise NotImplementedError

    @cached
    def properties(self):
        home = self._parentCollection._home
        uid = home.uid()
        if self._parentCollection.objectResourcesHaveProperties():
            propStoreClass = home._dataStore._propertyStoreClass
            props = propStoreClass(uid, lambda: self._path)
        else:
            props = NonePropertyStore(uid)
        self.initPropertyStore(props)
        self._transaction.addOperation(props.flush, "object properties flush")
        return props

    def initPropertyStore(self, props):
        """
        A hook for subclasses to override in order to set up their property
        store after it's been created.

        @param props: the L{PropertyStore} from C{properties()}.
        """
        pass


class CommonStubResource(object):
    """
    Just enough resource to keep the collection sql DB classes going.
    """

    def __init__(self, resource):
        self.resource = resource
        self._txn = self.resource._txn
        self.fp = self.resource._path

    def bumpSyncToken(self, reset=False):
        # FIXME: needs direct tests
        return self.resource._updateSyncToken(reset)

    def initSyncToken(self):
        # FIXME: needs direct tests
        self.bumpSyncToken(True)


class NotificationCollection(CommonHomeChild):
    """
    File-based implementation of L{INotificationCollection}.
    """
    implements(INotificationCollection)

    def __init__(self, name, parent, realName=None):
        """
        Initialize an notification collection pointing at a path on disk.

        @param name: the subdirectory of parent where this notification collection
            resides.
        @type name: C{str}

        @param parent: the home containing this notification collection.
        @type parent: L{CommonHome}
        """

        super(NotificationCollection, self).__init__(name, parent, realName)

        self._index = NotificationIndex(self)
        self._invites = None
        self._objectResourceClass = NotificationObject

    @classmethod
    def notificationsFromHome(cls, txn, home):

        notificationCollectionName = "notification"
        if not home._path.child(notificationCollectionName).isdir():
            notifications = cls._create(txn, home, notificationCollectionName)
        else:
            notifications = cls(notificationCollectionName, home)
        return notifications

    @classmethod
    def _create(cls, txn, home, collectionName):
        # FIXME: this is a near-copy of CommonHome.createChildWithName.
        temporary = hidden(home._path.child(collectionName).temporarySibling())
        temporary.createDirectory()
        temporaryName = temporary.basename()

        c = cls(temporary.basename(), home)

        def do():
            childPath = home._path.child(collectionName)
            temporary = childPath.sibling(temporaryName)
            try:
                props = c.properties()
                temporary.moveTo(childPath)
                c._name = collectionName
                # FIXME: _lots_ of duplication of work here.
                props.flush()
            except (IOError, OSError) as e:
                if e.errno == EEXIST and childPath.isdir():
                    raise HomeChildNameAlreadyExistsError(collectionName)
                raise
            # FIXME: direct tests, undo for index creation
            # Return undo
            return lambda: home._path.child(collectionName).remove()

        txn.addOperation(do, "create notification child %r" % (collectionName,))
        return c

    notificationObjects = CommonHomeChild.objectResources
    listNotificationObjects = CommonHomeChild.listObjectResources
    notificationObjectWithName = CommonHomeChild.objectResourceWithName

    def notificationObjectWithUID(self, uid):
        name = uid + ".xml"
        return self.notificationObjectWithName(name)

    def writeNotificationObject(self, uid, notificationtype, notificationdata):
        name = uid + ".xml"
        if name.startswith("."):
            raise ObjectResourceNameNotAllowedError(name)

        objectResource = NotificationObject(name, self)
        objectResource.setData(uid, notificationtype, notificationdata)
        self._cachedObjectResources[name] = objectResource

        # Update database
        self.retrieveOldIndex().addOrUpdateRecord(NotificationRecord(uid, name, notificationtype))

        self.notifyChanged()

    @writeOperation
    def removeNotificationObjectWithName(self, name):
        if name.startswith("."):
            raise NoSuchObjectResourceError(name)

        self.retrieveOldIndex().removeRecordForName(name)

        objectResourcePath = self._path.child(name)
        if objectResourcePath.isfile():
            self._removedObjectResources.add(name)
            # FIXME: test for undo

            def do():
                objectResourcePath.remove()
                return lambda: None
            self._transaction.addOperation(do, "remove object resource object %r" %
                                           (name,))

            self.notifyChanged()
        else:
            raise NoSuchObjectResourceError(name)

    @writeOperation
    def removeNotificationObjectWithUID(self, uid):
        name = uid + ".xml"
        self.removeNotificationObjectWithName(name)


class NotificationObject(CommonObjectResource):
    """
    """
    implements(INotificationObject)

    def __init__(self, name, notifications):
        super(NotificationObject, self).__init__(name, notifications)
        self._uid = name[:-4]

    def notificationCollection(self):
        return self._parentCollection

    def created(self):
        if not self._path.exists():
            from twisted.internet import reactor
            return int(reactor.seconds())
        return super(NotificationObject, self).created()

    def modified(self):
        if not self._path.exists():
            from twisted.internet import reactor
            return int(reactor.seconds())
        return super(NotificationObject, self).modified()

    @writeOperation
    def setData(self, uid, notificationtype, notificationdata, inserting=False):

        rname = uid + ".xml"
        self._parentCollection.retrieveOldIndex().addOrUpdateRecord(
            NotificationRecord(uid, rname, notificationtype)
        )

        self._notificationdata = notificationdata
        notificationtext = json.dumps(self._notificationdata)
        md5 = hashlib.md5(notificationtext).hexdigest()

        def do():
            backup = None
            if self._path.exists():
                backup = hidden(self._path.temporarySibling())
                self._path.moveTo(backup)
            fh = self._path.open("w")
            try:
                # FIXME: concurrency problem; if this write is interrupted
                # halfway through, the underlying file will be corrupt.
                fh.write(notificationtext)
            finally:
                fh.close()

            def undo():
                if backup:
                    backup.moveTo(self._path)
                else:
                    self._path.remove()
            return undo
        self._transaction.addOperation(do, "set notification data %r" % (self.name(),))

        # Mark all properties as dirty, so they will be re-added to the
        # temporary file when the main file is deleted. NOTE: if there were a
        # temporary file and a rename() as there should be, this should really
        # happen after the write but before the rename.
        self.properties().update(self.properties())

        props = self.properties()
        props[PropertyName(*GETContentType.qname())] = GETContentType.fromString(generateContentType(MimeType("text", "xml", params={"charset": "utf-8"})))
        props[PropertyName.fromElement(NotificationType)] = NotificationType(json.dumps(notificationtype))
        props[PropertyName.fromElement(TwistedGETContentMD5)] = TwistedGETContentMD5.fromString(md5)

        # FIXME: the property store's flush() method may already have been
        # added to the transaction, but we need to add it again to make sure it
        # happens _after_ the new file has been written.  we may end up doing
        # the work multiple times, and external callers to property-
        # manipulation methods won't work.
        self._transaction.addOperation(self.properties().flush, "post-update property flush")

    _notificationdata = None

    def notificationData(self):
        if self._notificationdata is not None:
            return self._notificationdata
        try:
            fh = self._path.open()
        except IOError as e:
            if e[0] == ENOENT:
                raise NoSuchObjectResourceError(self)
            else:
                raise

        try:
            text = fh.read()
        finally:
            fh.close()

        return json.loads(text)

    def uid(self):
        return self._uid

    def notificationType(self):
        # NB This is the NotificationType property element
        return self.properties()[PropertyName.fromElement(NotificationType)]

    def initPropertyStore(self, props):
        # Setup peruser special properties
        props.setSpecialProperties(
            (
            ),
            (
                PropertyName.fromElement(customxml.NotificationType),
            ),
            (),
        )


class NotificationIndex(object):
    #
    # OK, here's where we get ugly.
    # The index code needs to be rewritten also, but in the meantime...
    #

    def __init__(self, notificationCollection):
        self.notificationCollection = notificationCollection
        stubResource = CommonStubResource(notificationCollection)
        self._oldIndex = OldNotificationIndex(stubResource)
