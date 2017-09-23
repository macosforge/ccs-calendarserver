# -*- test-case-name: txdav.carddav.datastore.test.test_file -*-
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
File addressbook store.
"""

__all__ = [
    "AddressBookStore",
    "AddressBookStoreTransaction",
    "AddressBookHome",
    "AddressBook",
    "AddressBookObject",
]

from errno import ENOENT

from txdav.xml.rfc2518 import GETContentType
from txweb2.dav.resource import TwistedGETContentMD5
from txweb2.http_headers import MimeType


from twistedcaldav.vcard import Component as VComponent, InvalidVCardDataError
from txdav.carddav.datastore.index_file import AddressBookIndex as OldIndex

from txdav.carddav.datastore.util import validateAddressBookComponent
from txdav.carddav.iaddressbookstore import IAddressBook, IAddressBookObject
from txdav.carddav.iaddressbookstore import IAddressBookHome

from txdav.common.datastore.file import CommonDataStore, CommonHome, \
    CommonStoreTransaction, CommonHomeChild, CommonObjectResource, \
    CommonStubResource
from txdav.common.icommondatastore import NoSuchObjectResourceError, InternalDataStoreError
from txdav.base.datastore.file import hidden, writeOperation
from txdav.base.propertystore.base import PropertyName

from twistedcaldav import customxml, carddavxml

from zope.interface import implements

import hashlib

contentTypeKey = PropertyName.fromElement(GETContentType)
md5key = PropertyName.fromElement(TwistedGETContentMD5)

AddressBookStore = CommonDataStore

AddressBookStoreTransaction = CommonStoreTransaction


class AddressBookHome(CommonHome):

    implements(IAddressBookHome)

    _topPath = "addressbooks"
    _notifierPrefix = "CardDAV"

    def __init__(self, uid, path, addressbookStore, transaction):
        super(AddressBookHome, self).__init__(uid, path, addressbookStore, transaction)

        self._childClass = AddressBook

    addressbooks = CommonHome.children
    listAddressbooks = CommonHome.listChildren
    loadAddressbooks = CommonHome.loadChildren
    addressbookWithName = CommonHome.childWithName
    createAddressBookWithName = CommonHome.createChildWithName
    removeAddressBookWithName = CommonHome.removeChildWithName

    @property
    def _addressbookStore(self):
        return self._dataStore

    def createdHome(self):
        self.createAddressBookWithName("addressbook")


class AddressBook(CommonHomeChild):
    """
    File-based implementation of L{IAddressBook}.
    """
    implements(IAddressBook)

    def __init__(self, name, addressbookHome, owned, realName=None):
        """
        Initialize an addressbook pointing at a path on disk.

        @param name: the subdirectory of addressbookHome where this addressbook
            resides.
        @type name: C{str}

        @param addressbookHome: the home containing this addressbook.
        @type addressbookHome: L{AddressBookHome}

        @param realName: If this addressbook was just created, the name which it
        will eventually have on disk.
        @type realName: C{str}
        """

        super(AddressBook, self).__init__(name, addressbookHome, owned, realName=realName)

        self._index = Index(self)
        self._objectResourceClass = AddressBookObject

    @property
    def _addressbookHome(self):
        return self._home

    ownerAddressBookHome = CommonHomeChild.ownerHome
    addressbookObjects = CommonHomeChild.objectResources
    listAddressbookObjects = CommonHomeChild.listObjectResources
    addressbookObjectWithName = CommonHomeChild.objectResourceWithName
    addressbookObjectWithUID = CommonHomeChild.objectResourceWithUID
    createAddressBookObjectWithName = CommonHomeChild.createObjectResourceWithName
    addressbookObjectsSinceToken = CommonHomeChild.objectResourcesSinceToken

    def initPropertyStore(self, props):
        # Setup peruser special properties
        props.setSpecialProperties(
            (
                PropertyName.fromElement(carddavxml.AddressBookDescription),
            ),
            (
                PropertyName.fromElement(customxml.GETCTag),
            ),
            (),
        )

    def contentType(self):
        """
        The content type of Addresbook objects is text/vcard.
        """
        return MimeType.fromString("text/vcard; charset=utf-8")


class AddressBookObject(CommonObjectResource):
    """
    """
    implements(IAddressBookObject)

    def __init__(self, name, addressbook, metadata=None):

        super(AddressBookObject, self).__init__(name, addressbook)

    @property
    def _addressbook(self):
        return self._parentCollection

    def addressbook(self):
        return self._addressbook

    @writeOperation
    def setComponent(self, component, inserting=False):

        validateAddressBookComponent(self, self._addressbook, component, inserting)

        self._addressbook.retrieveOldIndex().addResource(
            self.name(), component
        )

        componentText = str(component)
        self._objectText = componentText

        def do():
            # Mark all properties as dirty, so they can be added back
            # to the newly updated file.
            self.properties().update(self.properties())

            backup = None
            if self._path.exists():
                backup = hidden(self._path.temporarySibling())
                self._path.moveTo(backup)

            fh = self._path.open("w")
            try:
                # FIXME: concurrency problem; if this write is interrupted
                # halfway through, the underlying file will be corrupt.
                fh.write(componentText)
            finally:
                fh.close()

            md5 = hashlib.md5(componentText).hexdigest()
            self.properties()[md5key] = TwistedGETContentMD5.fromString(md5)

            # Now re-write the original properties on the updated file
            self.properties().flush()

            def undo():
                if backup:
                    backup.moveTo(self._path)
                else:
                    self._path.remove()
            return undo
        self._transaction.addOperation(do, "set addressbook component %r" % (self.name(),))

        self._addressbook.notifyChanged()

    def component(self):
        """
        Read address data and validate/fix it. Do not raise a store error here if there are unfixable
        errors as that could prevent the overall request to fail. Instead we will hand bad data off to
        the caller - that is not ideal but in theory we should have checked everything on the way in and
        only allowed in good data.
        """
        text = self._text()
        try:
            component = VComponent.fromString(text)
        except InvalidVCardDataError as e:
            # This is a really bad situation, so do raise
            raise InternalDataStoreError(
                "File corruption detected (%s) in file: %s"
                % (e, self._path.path)
            )

        # Fix any bogus data we can
        fixed, unfixed = component.validVCardData(doFix=True, doRaise=False)

        if unfixed:
            self.log.error("Address data at {path} had unfixable problems:\n  {problems}", path=self._path.path, problems="\n  ".join(unfixed))

        if fixed:
            self.log.error("Address data at {path} had fixable problems:\n  {problems}", path=self._path.path, problems="\n  ".join(fixed))

        return component

    def _text(self):
        if self._objectText is not None:
            return self._objectText

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

        if not (
            text.startswith("BEGIN:VCARD\r\n") or
            text.endswith("\r\nEND:VCARD\r\n")
        ):
            raise InternalDataStoreError(
                "File corruption detected (improper start) in file: %s"
                % (self._path.path,)
            )

        self._objectText = text
        return text

    def uid(self):
        if not hasattr(self, "_uid"):
            self._uid = self.component().resourceUID()
        return self._uid

    # IDataStoreObject
    def contentType(self):
        """
        The content type of Addressbook objects is text/x-vcard.
        """
        return MimeType.fromString("text/vcard; charset=utf-8")


class AddressBookStubResource(CommonStubResource):
    """
    Just enough resource to keep the addressbook's sql DB classes going.
    """

    def isAddressBookCollection(self):
        return True

    def getChild(self, name):
        addressbookObject = self.resource.addressbookObjectWithName(name)
        if addressbookObject:
            class ChildResource(object):

                def __init__(self, addressbookObject):
                    self.addressbookObject = addressbookObject

                def iAddressBook(self):
                    return self.addressbookObject.component()

            return ChildResource(addressbookObject)
        else:
            return None


class Index(object):
    #
    # OK, here's where we get ugly.
    # The index code needs to be rewritten also, but in the meantime...
    #

    def __init__(self, addressbook):
        self.addressbook = addressbook
        stubResource = AddressBookStubResource(addressbook)
        self._oldIndex = OldIndex(stubResource)

    def addressbookObjects(self):
        addressbook = self.addressbook
        for name, uid, componentType in self._oldIndex.bruteForceSearch():
            addressbookObject = addressbook.addressbookObjectWithName(name)

            # Precache what we found in the index
            addressbookObject._uid = uid
            addressbookObject._componentType = componentType

            yield addressbookObject
