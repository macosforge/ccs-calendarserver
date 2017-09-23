# -*- test-case-name: twext.enterprise.dal.test.test_record -*-
##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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

from pycalendar.value import Value

from twext.enterprise.dal.syntax import Select, Insert, Delete, Parameter, \
    Update, utcNowSQL
from twext.enterprise.util import parseSQLTimestamp
from twext.python.filepath import CachingFilePath

from twisted.internet.defer import inlineCallbacks, returnValue

from twistedcaldav.config import config
from twistedcaldav.dateops import datetimeMktime
from twistedcaldav.ical import Property

from txdav.caldav.datastore.util import StorageTransportBase, \
    AttachmentRetrievalTransport
from txdav.caldav.icalendarstore import AttachmentSizeTooLarge, QuotaExceeded, \
    IAttachment, AttachmentDropboxNotAllowed, AttachmentMigrationFailed, \
    AttachmentStoreValidManagedID
from txdav.common.datastore.sql_tables import schema

from txweb2.http_headers import MimeType, generateContentType

from zope.interface.declarations import implements

import hashlib
import itertools
import os
import tempfile
import urllib
import uuid

"""
Classes and methods that relate to CalDAV attachments in the SQL store.
"""


class AttachmentStorageTransport(StorageTransportBase):

    _TEMPORARY_UPLOADS_DIRECTORY = "Temporary"

    def __init__(self, attachment, contentType, dispositionName, creating=False, migrating=False):
        super(AttachmentStorageTransport, self).__init__(
            attachment, contentType, dispositionName)

        fileDescriptor, fileName = self._temporaryFile()
        # Wrap the file descriptor in a file object we can write to
        self._file = os.fdopen(fileDescriptor, "w")
        self._path = CachingFilePath(fileName)
        self._hash = hashlib.md5()
        self._creating = creating
        self._migrating = migrating

        self._txn.postAbort(self.aborted)

    def _temporaryFile(self):
        """
        Returns a (file descriptor, absolute path) tuple for a temporary file within
        the Attachments/Temporary directory (creating the Temporary subdirectory
        if it doesn't exist).  It is the caller's responsibility to remove the
        file.
        """
        attachmentRoot = self._txn._store.attachmentsPath
        tempUploadsPath = attachmentRoot.child(self._TEMPORARY_UPLOADS_DIRECTORY)
        if not tempUploadsPath.exists():
            tempUploadsPath.createDirectory()
        return tempfile.mkstemp(dir=tempUploadsPath.path)

    @property
    def _txn(self):
        return self._attachment._txn

    def aborted(self):
        """
        Transaction aborted - clean up temp files.
        """
        if self._path.exists():
            self._path.remove()

    def write(self, data):
        if isinstance(data, buffer):
            data = str(data)
        self._file.write(data)
        self._hash.update(data)

    @inlineCallbacks
    def loseConnection(self):
        """
        Note that when self._migrating is set we only care about the data and don't need to
        do any quota checks/adjustments.
        """

        # FIXME: this should be synchronously accessible; IAttachment should
        # have a method for getting its parent just as CalendarObject/Calendar
        # do.

        # FIXME: If this method isn't called, the transaction should be
        # prevented from committing successfully.  It's not valid to have an
        # attachment that doesn't point to a real file.

        home = (yield self._txn.calendarHomeWithResourceID(self._attachment._ownerHomeID))

        oldSize = self._attachment.size()
        newSize = self._file.tell()
        self._file.close()

        # Check max size for attachment
        if not self._migrating and newSize > config.MaximumAttachmentSize:
            self._path.remove()
            if self._creating:
                yield self._attachment._internalRemove()
            raise AttachmentSizeTooLarge()

        # Check overall user quota
        if not self._migrating:
            allowed = home.quotaAllowedBytes()
            if allowed is not None and allowed < ((yield home.quotaUsedBytes()) + (newSize - oldSize)):
                self._path.remove()
                if self._creating:
                    yield self._attachment._internalRemove()
                raise QuotaExceeded()

        self._path.moveTo(self._attachment._path)

        yield self._attachment.changed(
            self._contentType,
            self._dispositionName,
            self._hash.hexdigest(),
            newSize
        )

        if not self._migrating and home:
            # Adjust quota
            yield home.adjustQuotaUsedBytes(self._attachment.size() - oldSize)

            # Send change notification to home
            yield home.notifyChanged()


class AttachmentLink(object):
    """
    A binding between an L{Attachment} and an L{CalendarObject}.
    """

    _attachmentSchema = schema.ATTACHMENT
    _attachmentLinkSchema = schema.ATTACHMENT_CALENDAR_OBJECT

    @classmethod
    def makeClass(cls, txn, linkData):
        """
        Given the various database rows, build the actual class.

        @param objectData: the standard set of object columns
        @type objectData: C{list}

        @return: the constructed child class
        @rtype: L{CommonHomeChild}
        """

        child = cls(txn)
        for attr, value in zip(child._rowAttributes(), linkData):
            setattr(child, attr, value)
        return child

    @classmethod
    def _allColumns(cls):
        """
        Full set of columns in the object table that need to be loaded to
        initialize the object resource state.
        """
        aco = cls._attachmentLinkSchema
        return [
            aco.ATTACHMENT_ID,
            aco.MANAGED_ID,
            aco.CALENDAR_OBJECT_RESOURCE_ID,
        ]

    @classmethod
    def _rowAttributes(cls):
        """
        Object attributes used to store the column values from L{_allColumns}. This used to create
        a mapping when serializing the object for cross-pod requests.
        """
        return (
            "_attachmentID",
            "_managedID",
            "_calendarObjectID",
        )

    @classmethod
    @inlineCallbacks
    def linksForHome(cls, home):
        """
        Load all attachment<->calendar object mappings for the specified home collection.
        """

        # Load from the main table first
        att = cls._attachmentSchema
        attco = cls._attachmentLinkSchema
        dataRows = yield Select(
            cls._allColumns(),
            From=attco.join(att, on=(attco.ATTACHMENT_ID == att.ATTACHMENT_ID)),
            Where=att.CALENDAR_HOME_RESOURCE_ID == home.id(),
        ).on(home._txn)

        # Create the actual objects
        returnValue([cls.makeClass(home._txn, row) for row in dataRows])

    def __init__(self, txn):
        self._txn = txn
        for attr in self._rowAttributes():
            setattr(self, attr, None)

    def serialize(self):
        """
        Create a dictionary mapping key attributes so this object can be sent over a cross-pod call
        and reconstituted at the other end. Note that the other end may have a different schema so
        the attributes may not match exactly and will need to be processed accordingly.
        """
        return dict([(attr[1:], getattr(self, attr, None)) for attr in self._rowAttributes()])

    @classmethod
    def deserialize(cls, txn, mapping):
        """
        Given a mapping generated by L{serialize}, convert the values into an array of database
        like items that conforms to the ordering of L{_allColumns} so it can be fed into L{makeClass}.
        Note that there may be a schema mismatch with the external data, so treat missing items as
        C{None} and ignore extra items.
        """

        return cls.makeClass(txn, [mapping.get(row[1:]) for row in cls._rowAttributes()])

    def insert(self):
        """
        Insert the object.
        """

        row = dict([(column, getattr(self, attr)) for column, attr in itertools.izip(self._allColumns(), self._rowAttributes())])
        return Insert(row).on(self._txn)


class Attachment(object):

    implements(IAttachment)

    _attachmentSchema = schema.ATTACHMENT
    _attachmentLinkSchema = schema.ATTACHMENT_CALENDAR_OBJECT

    @classmethod
    def makeClass(cls, txn, attachmentData):
        """
        Given the various database rows, build the actual class.

        @param attachmentData: the standard set of attachment columns
        @type attachmentData: C{list}

        @return: the constructed child class
        @rtype: L{Attachment}
        """

        att = cls._attachmentSchema
        dropbox_id = attachmentData[cls._allColumns().index(att.DROPBOX_ID)]
        c = ManagedAttachment if dropbox_id == "." else DropBoxAttachment
        child = c(
            txn,
            attachmentData[cls._allColumns().index(att.ATTACHMENT_ID)],
            attachmentData[cls._allColumns().index(att.DROPBOX_ID)],
            attachmentData[cls._allColumns().index(att.PATH)],
        )

        for attr, value in zip(child._rowAttributes(), attachmentData):
            setattr(child, attr, value)
        child._created = parseSQLTimestamp(child._created)
        child._modified = parseSQLTimestamp(child._modified)
        child._contentType = MimeType.fromString(child._contentType)

        return child

    @classmethod
    def _allColumns(cls):
        """
        Full set of columns in the object table that need to be loaded to
        initialize the object resource state.
        """
        att = cls._attachmentSchema
        return [
            att.ATTACHMENT_ID,
            att.DROPBOX_ID,
            att.CALENDAR_HOME_RESOURCE_ID,
            att.CONTENT_TYPE,
            att.SIZE,
            att.MD5,
            att.CREATED,
            att.MODIFIED,
            att.PATH,
        ]

    @classmethod
    def _rowAttributes(cls):
        """
        Object attributes used to store the column values from L{_allColumns}. This used to create
        a mapping when serializing the object for cross-pod requests.
        """
        return (
            "_attachmentID",
            "_dropboxID",
            "_ownerHomeID",
            "_contentType",
            "_size",
            "_md5",
            "_created",
            "_modified",
            "_name",
        )

    @classmethod
    @inlineCallbacks
    def loadAllAttachments(cls, home):
        """
        Load all attachments assigned to the specified home collection. This should only be
        used when sync'ing an entire home's set of attachments.
        """

        results = []

        # Load from the main table first
        att = cls._attachmentSchema
        dataRows = yield Select(
            cls._allColumns(),
            From=att,
            Where=att.CALENDAR_HOME_RESOURCE_ID == home.id(),
        ).on(home._txn)

        # Create the actual objects
        for row in dataRows:
            child = cls.makeClass(home._txn, row)
            results.append(child)

        returnValue(results)

    @classmethod
    @inlineCallbacks
    def loadAttachmentByID(cls, home, id):
        """
        Load one attachments assigned to the specified home collection. This should only be
        used when sync'ing an entire home's set of attachments.
        """

        # Load from the main table first
        att = cls._attachmentSchema
        rows = yield Select(
            cls._allColumns(),
            From=att,
            Where=(att.CALENDAR_HOME_RESOURCE_ID == home.id()).And(
                att.ATTACHMENT_ID == id),
        ).on(home._txn)

        # Create the actual object
        returnValue(cls.makeClass(home._txn, rows[0]) if len(rows) == 1 else None)

    def serialize(self):
        """
        Create a dictionary mapping key attributes so this object can be sent over a cross-pod call
        and reconstituted at the other end. Note that the other end may have a different schema so
        the attributes may not match exactly and will need to be processed accordingly.
        """
        result = dict([(attr[1:], getattr(self, attr, None)) for attr in self._rowAttributes()])
        result["created"] = result["created"].isoformat(" ")
        result["modified"] = result["modified"].isoformat(" ")
        result["contentType"] = generateContentType(result["contentType"])
        return result

    @classmethod
    def deserialize(cls, txn, mapping):
        """
        Given a mapping generated by L{serialize}, convert the values into an array of database
        like items that conforms to the ordering of L{_allColumns} so it can be fed into L{makeClass}.
        Note that there may be a schema mismatch with the external data, so treat missing items as
        C{None} and ignore extra items.
        """

        return cls.makeClass(txn, [mapping.get(row[1:]) for row in cls._rowAttributes()])

    def __init__(self, txn, a_id, dropboxID, name, ownerHomeID=None, justCreated=False):
        self._txn = txn
        self._attachmentID = a_id
        self._ownerHomeID = ownerHomeID
        self._dropboxID = dropboxID
        self._contentType = None
        self._size = 0
        self._md5 = None
        self._created = None
        self._modified = None
        self._name = name
        self._justCreated = justCreated

    def __repr__(self):
        return (
            "<{self.__class__.__name__}: {self._attachmentID}>"
            .format(self=self)
        )

    def _attachmentPathRoot(self):
        return self._txn._store.attachmentsPath

    @inlineCallbacks
    def initFromStore(self):
        """
        Execute necessary SQL queries to retrieve attributes.

        @return: C{True} if this attachment exists, C{False} otherwise.
        """
        att = self._attachmentSchema
        if self._dropboxID and self._dropboxID != ".":
            where = (att.DROPBOX_ID == self._dropboxID).And(
                att.PATH == self._name)
        else:
            where = (att.ATTACHMENT_ID == self._attachmentID)
        rows = (yield Select(
            self._allColumns(),
            From=att,
            Where=where
        ).on(self._txn))

        if not rows:
            returnValue(None)

        for attr, value in zip(self._rowAttributes(), rows[0]):
            setattr(self, attr, value)
        self._created = parseSQLTimestamp(self._created)
        self._modified = parseSQLTimestamp(self._modified)
        self._contentType = MimeType.fromString(self._contentType)

        returnValue(self)

    def copyRemote(self, remote):
        """
        Copy properties from a remote (external) attachment that is being migrated.

        @param remote: the external attachment
        @type remote: L{Attachment}
        """
        return self.changed(remote.contentType(), remote.name(), remote.md5(), remote.size())

    def id(self):
        return self._attachmentID

    def dropboxID(self):
        return self._dropboxID

    def isManaged(self):
        return self._dropboxID == "."

    def name(self):
        return self._name

    def properties(self):
        pass  # stub

    def store(self, contentType, dispositionName=None, migrating=False):
        if not self._name:
            self._name = dispositionName
        return AttachmentStorageTransport(self, contentType, dispositionName, self._justCreated, migrating=migrating)

    def retrieve(self, protocol):
        return AttachmentRetrievalTransport(self._path).start(protocol)

    def changed(self, contentType, dispositionName, md5, size):
        raise NotImplementedError

    _removeStatement = Delete(
        From=schema.ATTACHMENT,
        Where=(schema.ATTACHMENT.ATTACHMENT_ID == Parameter("attachmentID"))
    )

    @inlineCallbacks
    def remove(self, adjustQuota=True):
        oldSize = self._size
        self._txn.postCommit(self.removePaths)
        yield self._internalRemove()

        # Adjust quota
        if adjustQuota:
            home = (yield self._txn.calendarHomeWithResourceID(self._ownerHomeID))
            if home:
                yield home.adjustQuotaUsedBytes(-oldSize)

                # Send change notification to home
                yield home.notifyChanged()

    def removePaths(self):
        """
        Remove the actual file and up to attachment parent directory if empty.
        """
        if self._path.exists():
            self._path.remove()
        self.removeParentPaths()

    def removeParentPaths(self):
        """
        Remove up to attachment parent directory if empty.
        """
        parent = self._path.parent()
        toppath = self._attachmentPathRoot().path
        while parent.path != toppath:
            if len(parent.listdir()) == 0:
                parent.remove()
                parent = parent.parent()
            else:
                break

    def _internalRemove(self):
        """
        Just delete the row; don't do any accounting / bookkeeping.  (This is
        for attachments that have failed to be created due to errors during
        storage.)
        """
        return self._removeStatement.on(self._txn, attachmentID=self._attachmentID)

    @classmethod
    @inlineCallbacks
    def removedHome(cls, txn, homeID):
        """
        A calendar home is being removed so all of its attachments must go too. When removing,
        we don't care about quota adjustment as there will be no quota once the home is removed.

        TODO: this needs to be transactional wrt the actual file deletes.
        """
        att = cls._attachmentSchema
        attco = cls._attachmentLinkSchema

        rows = (yield Select(
            [att.ATTACHMENT_ID, att.DROPBOX_ID, ],
            From=att,
            Where=(
                att.CALENDAR_HOME_RESOURCE_ID == homeID
            ),
        ).on(txn))

        for attachmentID, dropboxID in rows:
            if dropboxID != ".":
                attachment = DropBoxAttachment(txn, attachmentID, None, None)
            else:
                attachment = ManagedAttachment(txn, attachmentID, None, None)
            attachment = (yield attachment.initFromStore())
            if attachment._path.exists():
                attachment.removePaths()

        yield Delete(
            From=attco,
            Where=(
                attco.ATTACHMENT_ID.In(Select(
                    [att.ATTACHMENT_ID, ],
                    From=att,
                    Where=(
                        att.CALENDAR_HOME_RESOURCE_ID == homeID
                    ),
                ))
            ),
        ).on(txn)

        yield Delete(
            From=att,
            Where=(
                att.CALENDAR_HOME_RESOURCE_ID == homeID
            ),
        ).on(txn)

    # IDataStoreObject
    def contentType(self):
        return self._contentType

    def md5(self):
        return self._md5

    def size(self):
        return self._size

    def created(self):
        return datetimeMktime(self._created)

    def modified(self):
        return datetimeMktime(self._modified)


class DropBoxAttachment(Attachment):

    @classmethod
    @inlineCallbacks
    def create(cls, txn, dropboxID, name, ownerHomeID):
        """
        Create a new Attachment object.

        @param txn: The transaction to use
        @type txn: L{CommonStoreTransaction}
        @param dropboxID: the identifier for the attachment (dropbox id or managed id)
        @type dropboxID: C{str}
        @param name: the name of the attachment
        @type name: C{str}
        @param ownerHomeID: the resource-id of the home collection of the attachment owner
        @type ownerHomeID: C{int}
        """

        # If store has already migrated to managed attachments we will prevent creation of dropbox attachments
        dropbox = (yield txn.store().dropboxAllowed(txn))
        if not dropbox:
            raise AttachmentDropboxNotAllowed

        # Now create the DB entry
        att = cls._attachmentSchema
        rows = (yield Insert({
            att.CALENDAR_HOME_RESOURCE_ID: ownerHomeID,
            att.DROPBOX_ID: dropboxID,
            att.CONTENT_TYPE: "",
            att.SIZE: 0,
            att.MD5: "",
            att.PATH: name,
        }, Return=(att.ATTACHMENT_ID, att.CREATED, att.MODIFIED)).on(txn))

        row_iter = iter(rows[0])
        a_id = next(row_iter)
        created = parseSQLTimestamp(next(row_iter))
        modified = parseSQLTimestamp(next(row_iter))

        attachment = cls(txn, a_id, dropboxID, name, ownerHomeID, True)
        attachment._created = created
        attachment._modified = modified

        # File system paths need to exist
        try:
            attachment._path.parent().makedirs()
        except:
            pass

        returnValue(attachment)

    @classmethod
    @inlineCallbacks
    def load(cls, txn, dropboxID, name):
        attachment = cls(txn, None, dropboxID, name)
        attachment = (yield attachment.initFromStore())
        returnValue(attachment)

    @property
    def _path(self):
        # Use directory hashing scheme based on MD5 of dropboxID
        hasheduid = hashlib.md5(self._dropboxID).hexdigest()
        attachmentRoot = self._attachmentPathRoot().child(hasheduid[0:2]).child(hasheduid[2:4]).child(hasheduid)
        return attachmentRoot.child(self.name())

    @classmethod
    @inlineCallbacks
    def resourceRemoved(cls, txn, resourceID, dropboxID):
        """
        Remove all attachments referencing the specified resource.
        """

        # See if any other resources still reference this dropbox ID
        co = schema.CALENDAR_OBJECT
        rows = (yield Select(
            [co.RESOURCE_ID, ],
            From=co,
            Where=(co.DROPBOX_ID == dropboxID).And(
                co.RESOURCE_ID != resourceID)
        ).on(txn))

        if not rows:
            # Find each attachment with matching dropbox ID
            att = cls._attachmentSchema
            rows = (yield Select(
                [att.PATH],
                From=att,
                Where=(att.DROPBOX_ID == dropboxID)
            ).on(txn))
            for name in rows:
                name = name[0]
                attachment = yield cls.load(txn, dropboxID, name)
                yield attachment.remove()

    @inlineCallbacks
    def changed(self, contentType, dispositionName, md5, size):
        """
        Dropbox attachments never change their path - ignore dispositionName.
        """

        self._contentType = contentType
        self._md5 = md5
        self._size = size

        att = self._attachmentSchema
        self._created, self._modified = map(
            parseSQLTimestamp,
            (yield Update(
                {
                    att.CONTENT_TYPE: generateContentType(self._contentType),
                    att.SIZE: self._size,
                    att.MD5: self._md5,
                    att.MODIFIED: utcNowSQL,
                },
                Where=(att.ATTACHMENT_ID == self._attachmentID),
                Return=(att.CREATED, att.MODIFIED)).on(self._txn))[0]
        )

    @inlineCallbacks
    def convertToManaged(self):
        """
        Convert this dropbox attachment into a managed attachment by updating the
        database and returning a new ManagedAttachment object that does not reference
        any calendar object. Referencing will be added later.

        @return: the managed attachment object
        @rtype: L{ManagedAttachment}
        """

        # Change the DROPBOX_ID to a single "." to indicate a managed attachment.
        att = self._attachmentSchema
        (yield Update(
            {att.DROPBOX_ID: ".", },
            Where=(att.ATTACHMENT_ID == self._attachmentID),
        ).on(self._txn))

        # Create an "orphaned" ManagedAttachment that points to the updated data but without
        # an actual managed-id (which only exists when there is a reference to a calendar object).
        mattach = (yield ManagedAttachment.load(self._txn, None, None, attachmentID=self._attachmentID))
        mattach._managedID = str(uuid.uuid4())
        if mattach is None:
            raise AttachmentMigrationFailed

        # Then move the file on disk from the old path to the new one
        try:
            mattach._path.parent().makedirs()
        except Exception:
            # OK to fail if it already exists, otherwise must raise
            if not mattach._path.parent().exists():
                raise
        oldpath = self._path
        newpath = mattach._path
        oldpath.moveTo(newpath)
        self.removeParentPaths()

        returnValue(mattach)


class ManagedAttachment(Attachment):
    """
    Managed attachments are ones that the server is in total control of. Clients do POSTs on calendar objects
    to store the attachment data and have ATTACH properties added, updated or remove from the calendar objects.
    Each ATTACH property in a calendar object has a MANAGED-ID iCalendar parameter that is used in the POST requests
    to target a specific attachment. The MANAGED-ID values are unique to each calendar object resource, though
    multiple calendar object resources can point to the same underlying attachment as there is a separate database
    table that maps calendar objects/managed-ids to actual attachments.
    """

    @classmethod
    @inlineCallbacks
    def _create(cls, txn, managedID, ownerHomeID):
        """
        Create a new managed Attachment object.

        @param txn: The transaction to use
        @type txn: L{CommonStoreTransaction}
        @param managedID: the identifier for the attachment
        @type managedID: C{str}
        @param ownerHomeID: the resource-id of the home collection of the attachment owner
        @type ownerHomeID: C{int}
        """

        # Now create the DB entry
        att = cls._attachmentSchema
        rows = (yield Insert({
            att.CALENDAR_HOME_RESOURCE_ID: ownerHomeID,
            att.DROPBOX_ID: ".",
            att.CONTENT_TYPE: "",
            att.SIZE: 0,
            att.MD5: "",
            att.PATH: "",
        }, Return=(att.ATTACHMENT_ID, att.CREATED, att.MODIFIED)).on(txn))

        row_iter = iter(rows[0])
        a_id = next(row_iter)
        created = parseSQLTimestamp(next(row_iter))
        modified = parseSQLTimestamp(next(row_iter))

        attachment = cls(txn, a_id, ".", None, ownerHomeID, True)
        attachment._managedID = managedID
        attachment._created = created
        attachment._modified = modified

        # File system paths need to exist
        try:
            attachment._path.parent().makedirs()
        except:
            pass

        returnValue(attachment)

    @classmethod
    @inlineCallbacks
    def create(cls, txn, managedID, ownerHomeID, referencedBy):
        """
        Create a new Attachment object and reference it.

        @param txn: The transaction to use
        @type txn: L{CommonStoreTransaction}
        @param managedID: the identifier for the attachment
        @type managedID: C{str}
        @param ownerHomeID: the resource-id of the home collection of the attachment owner
        @type ownerHomeID: C{int}
        @param referencedBy: the resource-id of the calendar object referencing the attachment
        @type referencedBy: C{int}
        """

        # Now create the DB entry
        attachment = (yield cls._create(txn, managedID, ownerHomeID))
        attachment._objectResourceID = referencedBy

        # Create the attachment<->calendar object relationship for managed attachments
        attco = cls._attachmentLinkSchema
        yield Insert({
            attco.ATTACHMENT_ID: attachment._attachmentID,
            attco.MANAGED_ID: attachment._managedID,
            attco.CALENDAR_OBJECT_RESOURCE_ID: attachment._objectResourceID,
        }).on(txn)

        returnValue(attachment)

    @classmethod
    @inlineCallbacks
    def update(cls, txn, oldManagedID, ownerHomeID, referencedBy, oldAttachmentID):
        """
        Update an Attachment object. This creates a new one and adjusts the reference to the old
        one to point to the new one. If the old one is no longer referenced at all, it is deleted.

        @param txn: The transaction to use
        @type txn: L{CommonStoreTransaction}
        @param oldManagedID: the identifier for the original attachment
        @type oldManagedID: C{str}
        @param ownerHomeID: the resource-id of the home collection of the attachment owner
        @type ownerHomeID: C{int}
        @param referencedBy: the resource-id of the calendar object referencing the attachment
        @type referencedBy: C{int}
        @param oldAttachmentID: the attachment-id of the existing attachment being updated
        @type oldAttachmentID: C{int}
        """

        # Now create the DB entry with a new managed-ID
        managed_id = str(uuid.uuid4())
        attachment = (yield cls._create(txn, managed_id, ownerHomeID))
        attachment._objectResourceID = referencedBy

        # Update the attachment<->calendar object relationship for managed attachments
        attco = cls._attachmentLinkSchema
        yield Update(
            {
                attco.ATTACHMENT_ID: attachment._attachmentID,
                attco.MANAGED_ID: attachment._managedID,
            },
            Where=(attco.MANAGED_ID == oldManagedID).And(
                attco.CALENDAR_OBJECT_RESOURCE_ID == attachment._objectResourceID
            ),
        ).on(txn)

        # Now check whether old attachmentID is still referenced - if not delete it
        rows = (yield Select(
            [attco.ATTACHMENT_ID, ],
            From=attco,
            Where=(attco.ATTACHMENT_ID == oldAttachmentID),
        ).on(txn))
        aids = [row[0] for row in rows] if rows is not None else ()
        if len(aids) == 0:
            oldattachment = ManagedAttachment(txn, oldAttachmentID, None, None)
            oldattachment = (yield oldattachment.initFromStore())
            yield oldattachment.remove()

        returnValue(attachment)

    @classmethod
    @inlineCallbacks
    def load(cls, txn, referencedID, managedID, attachmentID=None):
        """
        Load a ManagedAttachment via either its managedID or attachmentID.
        """

        if managedID:
            attco = cls._attachmentLinkSchema
            where = (attco.MANAGED_ID == managedID)
            if referencedID is not None:
                where = where.And(attco.CALENDAR_OBJECT_RESOURCE_ID == referencedID)
            rows = (yield Select(
                [attco.ATTACHMENT_ID, ],
                From=attco,
                Where=where,
            ).on(txn))
            if len(rows) == 0:
                returnValue(None)
            elif referencedID is not None and len(rows) != 1:
                raise AttachmentStoreValidManagedID
            attachmentID = rows[0][0]

        attachment = cls(txn, attachmentID, None, None)
        attachment = (yield attachment.initFromStore())
        attachment._managedID = managedID
        attachment._objectResourceID = referencedID
        returnValue(attachment)

    @classmethod
    @inlineCallbacks
    def referencesTo(cls, txn, managedID):
        """
        Find all the calendar object resourceIds referenced by this supplied managed-id.
        """
        attco = cls._attachmentLinkSchema
        rows = (yield Select(
            [attco.CALENDAR_OBJECT_RESOURCE_ID, ],
            From=attco,
            Where=(attco.MANAGED_ID == managedID),
        ).on(txn))
        cobjs = set([row[0] for row in rows]) if rows is not None else set()
        returnValue(cobjs)

    @classmethod
    @inlineCallbacks
    def usedManagedID(cls, txn, managedID):
        """
        Return the "owner" home and referencing resource is, and UID for a managed-id.
        """
        att = cls._attachmentSchema
        attco = cls._attachmentLinkSchema
        co = schema.CALENDAR_OBJECT
        rows = (yield Select(
            [
                att.CALENDAR_HOME_RESOURCE_ID,
                attco.CALENDAR_OBJECT_RESOURCE_ID,
                co.ICALENDAR_UID,
            ],
            From=att.join(
                attco, att.ATTACHMENT_ID == attco.ATTACHMENT_ID, "left outer"
            ).join(co, co.RESOURCE_ID == attco.CALENDAR_OBJECT_RESOURCE_ID),
            Where=(attco.MANAGED_ID == managedID),
        ).on(txn))
        returnValue(rows)

    @classmethod
    @inlineCallbacks
    def resourceRemoved(cls, txn, resourceID):
        """
        Remove all attachments referencing the specified resource.
        """

        # Find all reference attachment-ids and dereference
        attco = cls._attachmentLinkSchema
        rows = (yield Select(
            [attco.MANAGED_ID, ],
            From=attco,
            Where=(attco.CALENDAR_OBJECT_RESOURCE_ID == resourceID),
        ).on(txn))
        mids = set([row[0] for row in rows]) if rows is not None else set()
        for managedID in mids:
            attachment = (yield ManagedAttachment.load(txn, resourceID, managedID))
            (yield attachment.removeFromResource(resourceID))

    @classmethod
    @inlineCallbacks
    def copyManagedID(cls, txn, managedID, referencedBy):
        """
        Associate an existing attachment with the new resource.
        """

        # Find the associated attachment-id and insert new reference
        attco = cls._attachmentLinkSchema
        aid = (yield Select(
            [attco.ATTACHMENT_ID, ],
            From=attco,
            Where=(attco.MANAGED_ID == managedID),
        ).on(txn))[0][0]

        yield Insert({
            attco.ATTACHMENT_ID: aid,
            attco.MANAGED_ID: managedID,
            attco.CALENDAR_OBJECT_RESOURCE_ID: referencedBy,
        }).on(txn)

    def managedID(self):
        return self._managedID

    @inlineCallbacks
    def objectResource(self):
        """
        Return the calendar object resource associated with this attachment.
        """

        home = (yield self._txn.calendarHomeWithResourceID(self._ownerHomeID))
        obj = (yield home.objectResourceWithID(self._objectResourceID))
        returnValue(obj)

    @property
    def _path(self):
        # Use directory hashing scheme based on MD5 of attachmentID
        hasheduid = hashlib.md5(str(self._attachmentID)).hexdigest()
        return self._attachmentPathRoot().child(hasheduid[0:2]).child(hasheduid[2:4]).child(hasheduid)

    @inlineCallbacks
    def location(self):
        """
        Return the URI location of the attachment.
        """
        if not hasattr(self, "_ownerName"):
            home = (yield self._txn.calendarHomeWithResourceID(self._ownerHomeID))
            self._ownerName = home.name()
        if not hasattr(self, "_objectDropboxID"):
            if not hasattr(self, "_objectResource"):
                self._objectResource = (yield self.objectResource())
            self._objectDropboxID = self._objectResource._dropboxID

        fname = self.lastSegmentOfUriPath(self._managedID, self._name)
        location = self._txn._store.attachmentsURIPattern % {
            "home": self._ownerName,
            "dropbox_id": urllib.quote(self._objectDropboxID),
            "name": urllib.quote(fname),
        }
        returnValue(location)

    @classmethod
    def lastSegmentOfUriPath(cls, managed_id, name):
        splits = name.rsplit(".", 1)
        fname = splits[0]
        suffix = splits[1] if len(splits) == 2 else "unknown"
        return "{0}-{1}.{2}".format(fname, managed_id[:8], suffix)

    @inlineCallbacks
    def changed(self, contentType, dispositionName, md5, size):
        """
        Always update name to current disposition name.
        """

        self._contentType = contentType
        self._name = dispositionName
        self._md5 = md5
        self._size = size
        att = self._attachmentSchema
        self._created, self._modified = map(
            parseSQLTimestamp,
            (yield Update(
                {
                    att.CONTENT_TYPE: generateContentType(self._contentType),
                    att.SIZE: self._size,
                    att.MD5: self._md5,
                    att.MODIFIED: utcNowSQL,
                    att.PATH: self._name,
                },
                Where=(att.ATTACHMENT_ID == self._attachmentID),
                Return=(att.CREATED, att.MODIFIED)).on(self._txn))[0]
        )

    @inlineCallbacks
    def newReference(self, resourceID):
        """
        Create a new reference of this attachment to the supplied calendar object resource id, and
        return a ManagedAttachment for the new reference.

        @param resourceID: the resource id to reference
        @type resourceID: C{int}

        @return: the new managed attachment
        @rtype: L{ManagedAttachment}
        """

        attco = self._attachmentLinkSchema
        yield Insert({
            attco.ATTACHMENT_ID: self._attachmentID,
            attco.MANAGED_ID: self._managedID,
            attco.CALENDAR_OBJECT_RESOURCE_ID: resourceID,
        }).on(self._txn)

        mattach = (yield ManagedAttachment.load(self._txn, resourceID, self._managedID))
        returnValue(mattach)

    @inlineCallbacks
    def removeFromResource(self, resourceID):

        # Delete the reference
        attco = self._attachmentLinkSchema
        yield Delete(
            From=attco,
            Where=(attco.ATTACHMENT_ID == self._attachmentID).And(
                attco.CALENDAR_OBJECT_RESOURCE_ID == resourceID),
        ).on(self._txn)

        # References still exist - if not remove actual attachment
        rows = (yield Select(
            [attco.CALENDAR_OBJECT_RESOURCE_ID, ],
            From=attco,
            Where=(attco.ATTACHMENT_ID == self._attachmentID),
        ).on(self._txn))
        if len(rows) == 0:
            yield self.remove()

    @inlineCallbacks
    def attachProperty(self):
        """
        Return an iCalendar ATTACH property for this attachment.
        """
        attach = Property("ATTACH", "", valuetype=Value.VALUETYPE_URI)
        location = (yield self.updateProperty(attach))
        returnValue((attach, location,))

    @inlineCallbacks
    def updateProperty(self, attach):
        """
        Update an iCalendar ATTACH property for this attachment.
        """

        location = (yield self.location())

        attach.setParameter("MANAGED-ID", self.managedID())
        attach.setParameter("FMTTYPE", "{0}/{1}".format(self.contentType().mediaType, self.contentType().mediaSubtype))
        attach.setParameter("FILENAME", self.name())
        attach.setParameter("SIZE", str(self.size()))
        attach.setValue(location)

        returnValue(location)
