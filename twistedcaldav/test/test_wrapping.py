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
Tests for the interaction between model-level and protocol-level logic.
"""


from twext.enterprise.ienterprise import AlreadyFinishedError
from twext.enterprise.locking import NamedLock
from txweb2 import responsecode
from txweb2.http import HTTPError
from txweb2.http_headers import Headers, MimeType
from txweb2.responsecode import INSUFFICIENT_STORAGE_SPACE
from txweb2.responsecode import UNAUTHORIZED
from txweb2.stream import MemoryStream

from twext.who.idirectory import RecordType

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.defer import maybeDeferred

from twistedcaldav.config import config
from twistedcaldav.ical import Component as VComponent
from twistedcaldav.storebridge import DropboxCollection, \
    CalendarCollectionResource
from twistedcaldav.test.util import StoreTestCase, SimpleStoreRequest
from twistedcaldav.vcard import Component as VCComponent

from txdav.caldav.datastore.file import Calendar
from txdav.caldav.datastore.test.test_file import test_event_text
from txdav.caldav.icalendarstore import ICalendarHome
from txdav.carddav.datastore.test.test_file import vcard4_text
from txdav.carddav.iaddressbookstore import IAddressBookHome
from txdav.common.datastore.test.util import assertProvides
from txdav.common.datastore.test.util import deriveQuota
from txdav.idav import IDataStore
from txdav.xml import element as davxml

import hashlib


def _todo(f, why):
    f.todo = why
    return f
rewriteOrRemove = lambda f: _todo(f, "Rewrite or remove")


class FakeChanRequest(object):
    code = 'request-not-finished'

    def writeHeaders(self, code, headers):
        self.code = code
        self.headers = headers

    def registerProducer(self, producer, streaming):
        pass

    def write(self, data):
        pass

    def unregisterProducer(self):
        pass

    def abortConnection(self):
        pass

    def getHostInfo(self):
        return '127.0.0.1', False

    def getRemoteHost(self):
        return '127.0.0.1'

    def finish(self):
        pass

    remoteAddr = '127.0.0.1'


class WrappingTests(StoreTestCase):
    """
    Tests for L{twistedcaldav.static.CalDAVResource} creating the appropriate type
    of txdav.caldav.datastore.file underlying object when it can determine what
    type it really represents.
    """

    @inlineCallbacks
    def populateOneObject(self, objectName, objectText):
        """
        Populate one calendar object in the test user's calendar.

        @param objectName: The name of a calendar object.
        @type objectName: str

        @param objectText: Some iCalendar text to populate it with.
        @type objectText: str
        """
        record = yield self.directory.recordWithShortName(RecordType.user, u"wsanchez")
        uid = record.uid
        txn = self.transactionUnderTest()
        home = yield txn.calendarHomeWithUID(uid, create=True)
        cal = yield home.calendarWithName("calendar")
        yield cal.createCalendarObjectWithName(objectName, VComponent.fromString(objectText))
        yield self.commit()

    @inlineCallbacks
    def populateOneAddressBookObject(self, objectName, objectText):
        """
        Populate one addressbook object in the test user's addressbook.

        @param objectName: The name of a addressbook object.
        @type objectName: str
        @param objectText: Some iVcard text to populate it with.
        @type objectText: str
        """
        record = yield self.directory.recordWithShortName(RecordType.user, u"wsanchez")
        uid = record.uid
        txn = self.transactionUnderTest()
        home = yield txn.addressbookHomeWithUID(uid, create=True)
        adbk = yield home.addressbookWithName("addressbook")
        yield adbk.createAddressBookObjectWithName(objectName, VCComponent.fromString(objectText))
        yield self.commit()

    requestUnderTest = None

    @inlineCallbacks
    def getResource(self, path, method='GET', user=None):
        """
        Retrieve a resource from the site.

        @param path: the path from the root of the site (not starting with a
            slash)
        @type path: C{str}

        @param method: the HTTP method to initialize the request with.
            Defaults to GET.  (This should I{mostly} be irrelevant to path
            traversal, but may be interesting to subsequent operations on
            C{self.requestUnderTest}).

        @param user: the username (shortname in the test XML file) of the user
            to forcibly authenticate this request as.

        @return: a L{Deferred} that fires with an L{IResource}.
        """
        if self.requestUnderTest is None:
            req = self.requestForPath(path, method)
            self.requestUnderTest = req
        else:
            # How should this handle mismatched methods?
            req = self.requestUnderTest
        aResource = yield req.locateResource(
            "http://localhost:8008/" + path
        )
        if user is not None:
            principal = yield self.actualRoot.findPrincipalForAuthID(user)
            req.authnUser = req.authzUser = principal
        returnValue(aResource)

    def requestForPath(self, path, method='GET'):
        """
        Get a L{Request} with a L{FakeChanRequest} for a given path and method.
        """
        headers = Headers()
        headers.addRawHeader("Host", "localhost:8008")
        req = SimpleStoreRequest(self, method, path, headers)

        # 'process()' normally sets these.  Shame on web2, having so much
        # partially-initialized stuff floating around.
        req.remoteAddr = '127.0.0.1'
        req.chanRequest = FakeChanRequest()
        req.credentialFactories = {}
        return req

    pathTypes = ['calendar', 'addressbook']

    def checkPrincipalCollections(self, resource):
        """
        Verify that the C{_principalCollections} attribute of the given
        L{Resource} is accurately set.
        """
        self.assertEquals(
            resource._principalCollections,
            frozenset([self.actualRoot.getChild("principals")])
        )

    @inlineCallbacks
    def test_autoRevertUnCommitted(self):
        """
        Resources that need to read from the back-end in a transaction will be
        reverted by a response filter in the case where the request does not
        commit them.  This can happen, for example, with resources that are
        children of non-existent (proto-)resources.
        """
        for pathType in self.pathTypes:
            req = self.requestForPath('/%ss/users/wsanchez/%s/forget/it'
                                      % (pathType, pathType))
            txn = self.transactionUnderTest()
            yield req.process()
            self.assertEquals(req.chanRequest.code, 404)
            yield self.failUnlessFailure(
                maybeDeferred(txn.commit),
                AlreadyFinishedError
            )

    @inlineCallbacks
    def test_simpleRequest(self):
        """
        Sanity check and integration test: an unauthorized request of calendar
        and addressbook resources results in an L{UNAUTHORIZED} response code.
        """
        for pathType in self.pathTypes:
            req = self.requestForPath('/%ss/users/wsanchez/%s/'
                                      % (pathType, pathType))
            yield req.process()
            self.assertEquals(req.chanRequest.code, UNAUTHORIZED)

    def test_createStore(self):
        """
        Creating a DirectoryCalendarHomeProvisioningResource will create a
        paired CalendarStore.
        """
        assertProvides(self, IDataStore, self._sqlCalendarStore)

    @inlineCallbacks
    def test_lookupCalendarHome(self):
        """
        When a L{CalDAVResource} representing an existing calendar home is looked
        up in a CalendarHomeResource, it will create a corresponding
        L{CalendarHome} via C{newTransaction().calendarHomeWithUID}.
        """
        calDavFile = yield self.getResource("calendars/users/wsanchez/")
        yield self.commit()
        assertProvides(self, ICalendarHome, calDavFile._newStoreHome)

    @inlineCallbacks
    def test_lookupDropboxHome(self):
        """
        When dropboxes are enabled, the 'dropbox' child of the user's calendar
        home should be a L{DropboxCollection} wrapper around the user's
        calendar home, with the dropbox-home resource type.
        """
        self.patch(config, "EnableDropBox", True)
        dropBoxResource = yield self.getResource(
            "calendars/users/wsanchez/dropbox"
        )
        yield self.commit()
        self.assertIsInstance(dropBoxResource, DropboxCollection)
        dropboxHomeType = davxml.ResourceType.dropboxhome  # @UndefinedVariable
        self.assertEquals(dropBoxResource.resourceType(),
                          dropboxHomeType)

    @inlineCallbacks
    def test_lookupExistingCalendar(self):
        """
        When a L{CalDAVResource} representing an existing calendar collection is
        looked up in a L{CalendarHomeResource} representing a calendar home, it
        will create a corresponding L{Calendar} via
        C{CalendarHome.calendarWithName}.
        """
        calDavFile = yield self.getResource("calendars/users/wsanchez/calendar")
        regularCalendarType = davxml.ResourceType.calendar  # @UndefinedVariable
        self.assertEquals(calDavFile.resourceType(),
                          regularCalendarType)
        yield self.commit()

    @inlineCallbacks
    def test_lookupNewCalendar(self):
        """
        When a L{CalDAVResource} which represents a not-yet-created calendar
        collection is looked up in a L{CalendarHomeResource} representing a
        calendar home, it will initially have a new storage backend set to
        C{None}, but when the calendar is created via a protocol action, the
        backend will be initialized to match.
        """
        calDavFile = yield self.getResource("calendars/users/wsanchez/frobozz")
        self.assertIsInstance(calDavFile, CalendarCollectionResource)
        self.assertFalse(calDavFile.exists())
        yield calDavFile.createCalendarCollection()
        self.assertTrue(calDavFile.exists())
        yield self.commit()

    @inlineCallbacks
    def test_lookupSpecial(self):
        """
        When a L{CalDAVResource} I{not} representing a calendar collection - one of
        the special collections, like the dropbox or freebusy URLs - is looked
        up in a L{CalendarHomeResource} representing a calendar home, it will I{not}
        create a corresponding L{Calendar} via C{CalendarHome.calendarWithName}.
        """
        for specialName in ['dropbox', 'freebusy', 'notifications']:
            calDavFile = yield self.getResource(
                "calendars/users/wsanchez/%s" % (specialName,)
            )
            self.assertIdentical(
                getattr(calDavFile, "_newStoreObject", None), None
            )
        yield self.commit()

    @inlineCallbacks
    def test_transactionPropagation(self):
        """
        L{CalendarHomeResource} propagates its transaction to all of its
        children.
        """
        variousNames = ['dropbox', 'freebusy', 'notifications',
                        'inbox', 'outbox', 'calendar']
        homeResource = yield self.getResource("calendars/users/wsanchez")
        homeTransaction = homeResource._associatedTransaction
        self.assertNotIdentical(homeTransaction, None)
        self.addCleanup(self.commit)
        for name in variousNames:
            homeChild = yield self.getResource(
                "calendars/users/wsanchez/" + name)
            self.assertIdentical(
                homeChild._associatedTransaction,
                homeTransaction,
                "transaction mismatch on {n}; {at} is not {ht} ".format(
                    n=name, at=homeChild._associatedTransaction,
                    ht=homeTransaction
                )
            )

    @inlineCallbacks
    def test_lookupCalendarObject(self):
        """
        When a L{CalDAVResource} representing an existing calendar object is
        looked up on a L{CalDAVResource} representing a calendar collection, a
        parallel L{CalendarObject} will be created.  Its principal collections
        and transaction should match.
        """
        yield self.populateOneObject("1.ics", test_event_text)
        calendarHome = yield self.getResource("calendars/users/wsanchez")
        calDavFileCalendar = yield self.getResource(
            "calendars/users/wsanchez/calendar/1.ics"
        )
        yield self.commit()
        self.checkPrincipalCollections(calDavFileCalendar)
        self.assertEquals(calDavFileCalendar._associatedTransaction,
                          calendarHome._associatedTransaction)

    @inlineCallbacks
    def test_attachmentQuotaExceeded(self):
        """
        Exceeding quota on an attachment returns an HTTP error code.
        """
        self.patch(config, "EnableDropBox", True)
        if not hasattr(self._sqlCalendarStore, "_dropbox_ok"):
            self._sqlCalendarStore._dropbox_ok = False
        self.patch(self._sqlCalendarStore, "_dropbox_ok", True)
        self.patch(Calendar, "sharingInvites", lambda self: [])

        yield self.populateOneObject("1.ics", test_event_text)
        calendarObject = yield self.getResource(
            "/calendars/users/wsanchez/dropbox/uid-test.dropbox/too-big-attachment",
            "PUT", "wsanchez"
        )
        self.requestUnderTest.stream = MemoryStream(
            "x" * deriveQuota(self) * 2)
        try:
            result = yield calendarObject.http_PUT(self.requestUnderTest)
        except HTTPError as he:
            self.assertEquals(he.response.code, INSUFFICIENT_STORAGE_SPACE)
        else:
            self.fail("Error not raised, %r returned instead." %
                      (result,))
        finally:
            yield self.commit()

    @inlineCallbacks
    def test_lookupNewCalendarObject(self):
        """
        When a L{CalDAVResource} representing a new calendar object on a
        L{CalDAVResource} representing an existing calendar collection, the list of
        principal collections will be propagated down to it.
        """
        calDavFileCalendar = yield self.getResource(
            "calendars/users/wsanchez/calendar/xyzzy.ics"
        )
        yield self.commit()
        self.checkPrincipalCollections(calDavFileCalendar)

    def test_createAddressBookStore(self):
        """
        Creating a AddressBookHomeProvisioningFile will create a paired
        AddressBookStore.
        """
        assertProvides(self, IDataStore, self.actualRoot.getChild("addressbooks")._newStore)

    @inlineCallbacks
    def test_lookupAddressBookHome(self):
        """
        When a L{CalDAVResource} representing an existing addressbook home is looked up
        in a AddressBookHomeFile, it will create a corresponding L{AddressBookHome}
        via C{newTransaction().addressbookHomeWithUID}.
        """
        calDavFile = yield self.getResource("addressbooks/users/wsanchez/")
        yield self.commit()
        assertProvides(self, IAddressBookHome, calDavFile._newStoreHome)

    @inlineCallbacks
    def test_lookupExistingAddressBook(self):
        """
        When a L{CalDAVResource} representing an existing addressbook collection is
        looked up in a L{AddressBookHomeFile} representing a addressbook home, it will
        create a corresponding L{AddressBook} via C{AddressBookHome.addressbookWithName}.
        """
        calDavFile = yield self.getResource("addressbooks/users/wsanchez/addressbook")
        yield self.commit()
        self.checkPrincipalCollections(calDavFile)

    @inlineCallbacks
    def test_lookupAddressBookObject(self):
        """
        When a L{CalDAVResource} representing an existing addressbook object is looked
        up on a L{CalDAVResource} representing a addressbook collection, a parallel
        L{AddressBookObject} will be created (with a matching FilePath).
        """
        yield self.populateOneAddressBookObject("1.vcf", vcard4_text)
        calDavFileAddressBook = yield self.getResource(
            "addressbooks/users/wsanchez/addressbook/1.vcf"
        )
        yield self.commit()
        self.checkPrincipalCollections(calDavFileAddressBook)

    @inlineCallbacks
    def test_lookupNewAddressBookObject(self):
        """
        When a L{CalDAVResource} representing a new addressbook object on a
        L{CalDAVResource} representing an existing addressbook collection, the list of
        principal collections will be propagated down to it.
        """
        calDavFileAddressBook = yield self.getResource(
            "addressbooks/users/wsanchez/addressbook/xyzzy.ics"
        )
        yield self.commit()
        self.checkPrincipalCollections(calDavFileAddressBook)

    @inlineCallbacks
    def assertCalendarEmpty(self, user, calendarName="calendar"):
        """
        Assert that a user's calendar is empty (their default calendar by default).
        """
        txn = self.transactionUnderTest()
        home = yield txn.calendarHomeWithUID(user, create=True)
        cal = yield home.calendarWithName(calendarName)
        objects = yield cal.calendarObjects()
        self.assertEquals(len(objects), 0)


class DatabaseWrappingTests(WrappingTests):

    @inlineCallbacks
    def test_invalidCalendarPUT(self):
        """
        Exceeding quota on an attachment returns an HTTP error code.
        """
        # yield self.populateOneObject("1.ics", test_event_text)
        @inlineCallbacks
        def putEvt(txt):
            calendarObject = yield self.getResource(
                "/calendars/users/wsanchez/calendar/1.ics",
                "PUT", "wsanchez"
            )
            self.requestUnderTest.stream = MemoryStream(txt)
            returnValue(
                ((yield calendarObject.renderHTTP(self.requestUnderTest)),
                 self.requestUnderTest)
            )

        # see twistedcaldav/directory/test/accounts.xml
        wsanchez = '6423F94A-6B76-4A3A-815B-D52CFD77935D'
        cdaboo = '5A985493-EE2C-4665-94CF-4DFEA3A89500'
        eventTemplate = """\
BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Example Inc.//Example Calendar//EN
VERSION:2.0
BEGIN:VEVENT
UID:20060110T231240Z-4011c71-187-6f73
ORGANIZER:urn:uuid:{wsanchez}
ATTENDEE:urn:uuid:{wsanchez}
DTSTART:20110101T050000Z
DTSTAMP:20110309T185105Z
DURATION:PT1H
SUMMARY:Test
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:20060110T231240Z-4011c71-187-6f73
RECURRENCE-ID:20110102T050000Z
ORGANIZER:urn:uuid:{wsanchez}
ATTENDEE:urn:uuid:{wsanchez}
ATTENDEE:urn:uuid:{cdaboo}
DTSTART:20110102T050000Z
DTSTAMP:20110309T185105Z
DURATION:PT1H
SUMMARY:Test
END:VEVENT{0}
END:VCALENDAR
"""
        CR = "\n"
        CRLF = "\r\n"
        # validEvent = eventTemplate.format("", wsanchez=wsanchez, cdaboo=cdaboo).replace(CR, CRLF)
        invalidInstance = """
BEGIN:VEVENT
UID:20060110T231240Z-4011c71-187-6f73
RECURRENCE-ID:20110110T050000Z
ORGANIZER:urn:uuid:{wsanchez}
ATTENDEE:urn:uuid:{wsanchez}
DTSTART:20110110T050000Z
DTSTAMP:20110309T185105Z
DURATION:PT1H
SUMMARY:Test
END:VEVENT""".format(wsanchez=wsanchez, cdaboo=cdaboo)

        invalidEvent = eventTemplate.format(invalidInstance, wsanchez=wsanchez, cdaboo=cdaboo).replace(CR, CRLF)
        yield putEvt(invalidEvent)
        self.lastTransaction = None
        self.requestUnderTest = None
        yield self.assertCalendarEmpty(wsanchez)
        yield self.assertCalendarEmpty(cdaboo)


class TimeoutTests(StoreTestCase):
    """
    Tests for L{twistedcaldav.storebridge} lock timeouts.
    """

    @inlineCallbacks
    def test_timeoutOnPUT(self):
        """
        PUT gets a 503 on a lock timeout.
        """

        # Create a fake lock
        txn = self.transactionUnderTest()
        yield NamedLock.acquire(txn, "ImplicitUIDLock:%s" % (hashlib.md5("uid1").hexdigest(),))

        # PUT fails
        principal = yield self.actualRoot.findPrincipalForAuthID("wsanchez")
        request = SimpleStoreRequest(
            self,
            "PUT",
            "/calendars/users/wsanchez/calendar/1.ics",
            headers=Headers({"content-type": MimeType.fromString("text/calendar")}),
            authPrincipal=principal
        )
        request.stream = MemoryStream("""BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Apple Computer\, Inc//iCal 2.0//EN
VERSION:2.0
BEGIN:VEVENT
UID:uid1
DTSTART;VALUE=DATE:20020101
DTEND;VALUE=DATE:20020102
DTSTAMP:20020101T121212Z
SUMMARY:New Year's Day
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n"))
        response = yield self.send(request)
        self.assertEqual(response.code, responsecode.SERVICE_UNAVAILABLE)

    @inlineCallbacks
    def test_timeoutOnDELETE(self):
        """
        DELETE gets a 503 on a lock timeout.
        """

        # PUT works
        principal = yield self.actualRoot.findPrincipalForAuthID("wsanchez")
        request = SimpleStoreRequest(
            self,
            "PUT",
            "/calendars/users/wsanchez/calendar/1.ics",
            headers=Headers({"content-type": MimeType.fromString("text/calendar")}),
            authPrincipal=principal
        )
        request.stream = MemoryStream("""BEGIN:VCALENDAR
CALSCALE:GREGORIAN
PRODID:-//Apple Computer\, Inc//iCal 2.0//EN
VERSION:2.0
BEGIN:VEVENT
UID:uid1
DTSTART;VALUE=DATE:20020101
DTEND;VALUE=DATE:20020102
DTSTAMP:20020101T121212Z
ORGANIZER:mailto:wsanchez@example.com
ATTENDEE:mailto:wsanchez@example.com
SUMMARY:New Year's Day
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n"))
        response = yield self.send(request)
        self.assertEqual(response.code, responsecode.CREATED)

        # Create a fake lock
        txn = self.transactionUnderTest()
        yield NamedLock.acquire(txn, "ImplicitUIDLock:%s" % (hashlib.md5("uid1").hexdigest(),))

        request = SimpleStoreRequest(
            self,
            "DELETE",
            "/calendars/users/wsanchez/calendar/1.ics",
            authPrincipal=principal
        )
        response = yield self.send(request)
        self.assertEqual(response.code, responsecode.SERVICE_UNAVAILABLE)
