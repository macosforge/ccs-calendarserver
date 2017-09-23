# Copyright (c) 2009 Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{txweb2.dav.xattrprops}.
"""

from zlib import compress, decompress
from pickle import dumps
from cPickle import UnpicklingError

from twext.python.filepath import CachingFilePath as FilePath
from twisted.trial.unittest import TestCase
from txweb2.responsecode import NOT_FOUND, INTERNAL_SERVER_ERROR
from txweb2.responsecode import FORBIDDEN
from txweb2.http import HTTPError
from txweb2.dav.static import DAVFile
from txdav.xml.element import Depth, WebDAVDocument

try:
    from txweb2.dav.xattrprops import xattrPropertyStore
except ImportError:
    xattrPropertyStore = None
else:
    from xattr import xattr


class ExtendedAttributesPropertyStoreTests(TestCase):
    """
    Tests for L{xattrPropertyStore}.
    """
    if xattrPropertyStore is None:
        skip = "xattr package missing, cannot test xattr property store"

    def setUp(self):
        """
        Create a resource and a xattr property store for it.
        """
        self.resourcePath = FilePath(self.mktemp())
        self.resourcePath.setContent("")
        self.attrs = xattr(self.resourcePath.path)
        self.resource = DAVFile(self.resourcePath.path)
        self.propertyStore = xattrPropertyStore(self.resource)

    def test_getAbsent(self):
        """
        L{xattrPropertyStore.get} raises L{HTTPError} with a I{NOT FOUND}
        response code if passed the name of an attribute for which there is no
        corresponding value.
        """
        error = self.assertRaises(HTTPError, self.propertyStore.get, ("foo", "bar"))
        self.assertEquals(error.response.code, NOT_FOUND)

    def _forbiddenTest(self, method):
        # Remove access to the directory containing the file so that getting
        # extended attributes from it fails with EPERM.
        self.resourcePath.parent().chmod(0)
        # Make sure to restore access to it later so that it can be deleted
        # after the test run is finished.
        self.addCleanup(self.resourcePath.parent().chmod, 0o700)

        # Try to get a property from it - and fail.
        document = self._makeValue()
        error = self.assertRaises(
            HTTPError,
            getattr(self.propertyStore, method),
            document.root_element.qname())

        # Make sure that the status is FORBIDDEN, a roughly reasonable mapping
        # of the EPERM failure.
        self.assertEquals(error.response.code, FORBIDDEN)

    def _missingTest(self, method):
        # Remove access to the directory containing the file so that getting
        # extended attributes from it fails with EPERM.
        self.resourcePath.parent().chmod(0)
        # Make sure to restore access to it later so that it can be deleted
        # after the test run is finished.
        self.addCleanup(self.resourcePath.parent().chmod, 0o700)

        # Try to get a property from it - and fail.
        document = self._makeValue()
        error = self.assertRaises(
            HTTPError,
            getattr(self.propertyStore, method),
            document.root_element.qname())

        # Make sure that the status is FORBIDDEN, a roughly reasonable mapping
        # of the EPERM failure.
        self.assertEquals(error.response.code, FORBIDDEN)

    def test_getErrors(self):
        """
        If there is a problem getting the specified property (aside from the
        property not existing), L{xattrPropertyStore.get} raises L{HTTPError}
        with a status code which is determined by the nature of the problem.
        """
        self._forbiddenTest('get')

    def test_getMissing(self):
        """
        Test missing file.
        """

        resourcePath = FilePath(self.mktemp())
        resource = DAVFile(resourcePath.path)
        propertyStore = xattrPropertyStore(resource)

        # Try to get a property from it - and fail.
        document = self._makeValue()
        error = self.assertRaises(
            HTTPError,
            propertyStore.get,
            document.root_element.qname())

        # Make sure that the status is NOT FOUND.
        self.assertEquals(error.response.code, NOT_FOUND)

    def _makeValue(self, uid=None):
        """
        Create and return any old WebDAVDocument for use by the get tests.
        """
        element = Depth(uid if uid is not None else "0")
        document = WebDAVDocument(element)
        return document

    def _setValue(self, originalDocument, value, uid=None):
        element = originalDocument.root_element
        attribute = (
            self.propertyStore.deadPropertyXattrPrefix +
            (uid if uid is not None else "") +
            element.sname())
        self.attrs[attribute] = value

    def _getValue(self, originalDocument, uid=None):
        element = originalDocument.root_element
        attribute = (
            self.propertyStore.deadPropertyXattrPrefix +
            (uid if uid is not None else "") +
            element.sname())
        return self.attrs[attribute]

    def _checkValue(self, originalDocument, uid=None):
        property = originalDocument.root_element.qname()

        # Try to load it via xattrPropertyStore.get
        loadedDocument = self.propertyStore.get(property, uid)

        # XXX Why isn't this a WebDAVDocument?
        self.assertIsInstance(loadedDocument, Depth)
        self.assertEquals(str(loadedDocument), uid if uid else "0")

    def test_getXML(self):
        """
        If there is an XML document associated with the property name passed to
        L{xattrPropertyStore.get}, that value is parsed into a
        L{WebDAVDocument}, the root element of which C{get} then returns.
        """
        document = self._makeValue()
        self._setValue(document, document.toxml())
        self._checkValue(document)

    def test_getCompressed(self):
        """
        If there is a compressed value associated with the property name passed
        to L{xattrPropertyStore.get}, that value is decompressed and parsed
        into a L{WebDAVDocument}, the root element of which C{get} then
        returns.
        """
        document = self._makeValue()
        self._setValue(document, compress(document.toxml()))
        self._checkValue(document)

    def test_getPickled(self):
        """
        If there is a pickled document associated with the property name passed
        to L{xattrPropertyStore.get}, that value is unpickled into a
        L{WebDAVDocument}, the root element of which is returned.
        """
        document = self._makeValue()
        self._setValue(document, dumps(document))
        self._checkValue(document)

    def test_getUpgradeXML(self):
        """
        If the value associated with the property name passed to
        L{xattrPropertyStore.get} is an uncompressed XML document, it is
        upgraded on access by compressing it.
        """
        document = self._makeValue()
        originalValue = document.toxml()
        self._setValue(document, originalValue)
        self._checkValue(document)
        self.assertEquals(
            decompress(self._getValue(document)), document.root_element.toxml(pretty=False))

    def test_getUpgradeCompressedPickle(self):
        """
        If the value associated with the property name passed to
        L{xattrPropertyStore.get} is a compressed pickled document, it is
        upgraded on access to the compressed XML format.
        """
        document = self._makeValue()
        self._setValue(document, compress(dumps(document)))
        self._checkValue(document)
        self.assertEquals(
            decompress(self._getValue(document)), document.root_element.toxml(pretty=False))

    def test_getInvalid(self):
        """
        If the value associated with the property name passed to
        L{xattrPropertyStore.get} cannot be interpreted, an error is logged and
        L{HTTPError} is raised with the I{INTERNAL SERVER ERROR} response code.
        """
        document = self._makeValue()
        self._setValue(
            document,
            "random garbage goes here! \0 that nul is definitely garbage")

        property = document.root_element.qname()
        error = self.assertRaises(HTTPError, self.propertyStore.get, property)
        self.assertEquals(error.response.code, INTERNAL_SERVER_ERROR)
        self.assertEquals(
            len(self.flushLoggedErrors(UnpicklingError, IndexError)), 1)

    def test_set(self):
        """
        L{xattrPropertyStore.set} accepts a L{WebDAVElement} and stores a
        compressed XML document representing it in an extended attribute.
        """
        document = self._makeValue()
        self.propertyStore.set(document.root_element)
        self.assertEquals(
            decompress(self._getValue(document)), document.root_element.toxml(pretty=False))

    def test_delete(self):
        """
        L{xattrPropertyStore.delete} deletes the named property.
        """
        document = self._makeValue()
        self.propertyStore.set(document.root_element)
        self.propertyStore.delete(document.root_element.qname())
        self.assertRaises(KeyError, self._getValue, document)

    def test_deleteNonExistent(self):
        """
        L{xattrPropertyStore.delete} does nothing if passed a property which
        has no value.
        """
        document = self._makeValue()
        self.propertyStore.delete(document.root_element.qname())
        self.assertRaises(KeyError, self._getValue, document)

    def test_deleteErrors(self):
        """
        If there is a problem deleting the specified property (aside from the
        property not existing), L{xattrPropertyStore.delete} raises
        L{HTTPError} with a status code which is determined by the nature of
        the problem.
        """
        # Remove the file so that deleting extended attributes of it fails with
        # EEXIST.
        self.resourcePath.remove()

        # Try to delete a property from it - and fail.
        document = self._makeValue()
        error = self.assertRaises(
            HTTPError,
            self.propertyStore.delete, document.root_element.qname())

        # Make sure that the status is NOT FOUND, a roughly reasonable mapping
        # of the EEXIST failure.
        self.assertEquals(error.response.code, NOT_FOUND)

    def test_contains(self):
        """
        L{xattrPropertyStore.contains} returns C{True} if the given property
        has a value, C{False} otherwise.
        """
        document = self._makeValue()
        self.assertFalse(
            self.propertyStore.contains(document.root_element.qname()))
        self._setValue(document, document.toxml())
        self.assertTrue(
            self.propertyStore.contains(document.root_element.qname()))

    def test_containsError(self):
        """
        If there is a problem checking if the specified property exists (aside
        from the property not existing), L{xattrPropertyStore.contains} raises
        L{HTTPError} with a status code which is determined by the nature of
        the problem.
        """
        self._forbiddenTest('contains')

    def test_containsMissing(self):
        """
        Test missing file.
        """

        resourcePath = FilePath(self.mktemp())
        resource = DAVFile(resourcePath.path)
        propertyStore = xattrPropertyStore(resource)

        # Try to get a property from it - and fail.
        document = self._makeValue()
        self.assertFalse(propertyStore.contains(document.root_element.qname()))

    def test_list(self):
        """
        L{xattrPropertyStore.list} returns a C{list} of property names
        associated with the wrapped file.
        """
        prefix = self.propertyStore.deadPropertyXattrPrefix
        self.attrs[prefix + '{foo}bar'] = 'baz'
        self.attrs[prefix + '{bar}baz'] = 'quux'
        self.assertEquals(
            set(self.propertyStore.list()),
            set([(u'foo', u'bar'), (u'bar', u'baz')]))

    def test_listError(self):
        """
        If there is a problem checking if the specified property exists (aside
        from the property not existing), L{xattrPropertyStore.contains} raises
        L{HTTPError} with a status code which is determined by the nature of
        the problem.
        """
        # Remove access to the directory containing the file so that getting
        # extended attributes from it fails with EPERM.
        self.resourcePath.parent().chmod(0)
        # Make sure to restore access to it later so that it can be deleted
        # after the test run is finished.
        self.addCleanup(self.resourcePath.parent().chmod, 0o700)

        # Try to get a property from it - and fail.
        self._makeValue()
        error = self.assertRaises(HTTPError, self.propertyStore.list)

        # Make sure that the status is FORBIDDEN, a roughly reasonable mapping
        # of the EPERM failure.
        self.assertEquals(error.response.code, FORBIDDEN)

    def test_listMissing(self):
        """
        Test missing file.
        """

        resourcePath = FilePath(self.mktemp())
        resource = DAVFile(resourcePath.path)
        propertyStore = xattrPropertyStore(resource)

        # Try to get a property from it - and fail.
        self.assertEqual(propertyStore.list(), [])

    def test_get_uids(self):
        """
        L{xattrPropertyStore.get} accepts a L{WebDAVElement} and stores a
        compressed XML document representing it in an extended attribute.
        """

        for uid in (None, "123", "456",):
            document = self._makeValue(uid)
            self._setValue(document, document.toxml(), uid=uid)

        for uid in (None, "123", "456",):
            document = self._makeValue(uid)
            self._checkValue(document, uid=uid)

    def test_set_uids(self):
        """
        L{xattrPropertyStore.set} accepts a L{WebDAVElement} and stores a
        compressed XML document representing it in an extended attribute.
        """

        for uid in (None, "123", "456",):
            document = self._makeValue(uid)
            self.propertyStore.set(document.root_element, uid=uid)
            self.assertEquals(
                decompress(self._getValue(document, uid)), document.root_element.toxml(pretty=False))

    def test_delete_uids(self):
        """
        L{xattrPropertyStore.set} accepts a L{WebDAVElement} and stores a
        compressed XML document representing it in an extended attribute.
        """

        for delete_uid in (None, "123", "456",):
            for uid in (None, "123", "456",):
                document = self._makeValue(uid)
                self.propertyStore.set(document.root_element, uid=uid)
            self.propertyStore.delete(document.root_element.qname(), uid=delete_uid)
            self.assertRaises(KeyError, self._getValue, document, uid=delete_uid)
            for uid in (None, "123", "456",):
                if uid == delete_uid:
                    continue
                document = self._makeValue(uid)
                self.assertEquals(
                    decompress(self._getValue(document, uid)), document.root_element.toxml(pretty=False))

    def test_contains_uids(self):
        """
        L{xattrPropertyStore.contains} returns C{True} if the given property
        has a value, C{False} otherwise.
        """
        for uid in (None, "123", "456",):
            document = self._makeValue(uid)
            self.assertFalse(
                self.propertyStore.contains(document.root_element.qname(), uid=uid))
            self._setValue(document, document.toxml(), uid=uid)
            self.assertTrue(
                self.propertyStore.contains(document.root_element.qname(), uid=uid))

    def test_list_uids(self):
        """
        L{xattrPropertyStore.list} returns a C{list} of property names
        associated with the wrapped file.
        """
        prefix = self.propertyStore.deadPropertyXattrPrefix
        for uid in (None, "123", "456",):
            user = uid if uid is not None else ""
            self.attrs[prefix + '%s{foo}bar' % (user,)] = 'baz%s' % (user,)
            self.attrs[prefix + '%s{bar}baz' % (user,)] = 'quux%s' % (user,)
            self.attrs[prefix + '%s{moo}mar%s' % (user, user,)] = 'quux%s' % (user,)

        for uid in (None, "123", "456",):
            user = uid if uid is not None else ""
            self.assertEquals(
                set(self.propertyStore.list(uid)),
                set([
                    (u'foo', u'bar'),
                    (u'bar', u'baz'),
                    (u'moo', u'mar%s' % (user,)),
                ]))

        self.assertEquals(
            set(self.propertyStore.list(filterByUID=False)),
            set([
                (u'foo', u'bar', None),
                (u'bar', u'baz', None),
                (u'moo', u'mar', None),
                (u'foo', u'bar', "123"),
                (u'bar', u'baz', "123"),
                (u'moo', u'mar123', "123"),
                (u'foo', u'bar', "456"),
                (u'bar', u'baz', "456"),
                (u'moo', u'mar456', "456"),
            ]))
