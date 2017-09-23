##
# Copyright (c) 2009-2017 Apple Inc. All rights reserved.
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

from twistedcaldav.database import AbstractADBAPIDatabase, ADBAPISqliteMixin
import twistedcaldav.test.util

from twisted.internet.defer import inlineCallbacks

import os
import time


class Database (twistedcaldav.test.util.TestCase):
    """
    Test abstract SQL DB class
    """

    class TestDB(ADBAPISqliteMixin, AbstractADBAPIDatabase):

        def __init__(self, path, persistent=False, version="1"):
            self.version = version
            self.dbpath = path
            super(Database.TestDB, self).__init__("sqlite", "sqlite3", (path,), persistent, cp_min=3, cp_max=3)

        def _db_version(self):
            """
            @return: the schema version assigned to this index.
            """
            return self.version

        def _db_type(self):
            """
            @return: the collection type assigned to this index.
            """
            return "TESTTYPE"

        def _db_init_data_tables(self):
            """
            Initialise the underlying database tables.
            @param q:           a database cursor to use.
            """

            #
            # TESTTYPE table
            #
            return self._db_execute(
                """
                create table TESTTYPE (
                    KEY         text unique,
                    VALUE       text
                )
                """
            )

        def _db_remove_data_tables(self):
            return self._db_execute("drop table TESTTYPE")

    class TestDBRecreateUpgrade(TestDB):

        class RecreateDBException(Exception):
            pass

        class UpgradeDBException(Exception):
            pass

        def __init__(self, path, persistent=False):
            super(Database.TestDBRecreateUpgrade, self).__init__(path, persistent, version="2")

        def _db_recreate(self):
            raise self.RecreateDBException()

    class TestDBCreateIndexOnUpgrade(TestDB):

        def __init__(self, path, persistent=False):
            super(Database.TestDBCreateIndexOnUpgrade, self).__init__(path, persistent, version="2")

        def _db_upgrade_data_tables(self, old_version):
            return self._db_execute(
                """
                create index TESTING on TESTTYPE (VALUE)
                """
            )

    class TestDBPauseInInit(TestDB):

        def _db_init(self):

            time.sleep(1)
            super(Database.TestDBPauseInInit, self)._db_init()

    @inlineCallbacks
    def inlineCallbackRaises(self, exc, f, *args, **kwargs):
        try:
            yield f(*args, **kwargs)
        except exc:
            pass
        except Exception as e:
            self.fail("Wrong exception raised: %s" % (e,))
        else:
            self.fail("%s not raised" % (exc,))

    @inlineCallbacks
    def test_connect(self):
        """
        Connect to database and create table
        """
        db = Database.TestDB(self.mktemp())
        self.assertFalse(db.initialized)
        yield db.open()
        self.assertTrue(db.initialized)
        db.close()

    @inlineCallbacks
    def test_connectFailure(self):
        """
        Failure to connect cleans up the pool
        """
        db = Database.TestDB(self.mktemp())
        # Make _db_init fail
        db._db_init = lambda: 1 / 0
        self.assertFalse(db.initialized)
        try:
            yield db.open()
        except:
            pass
        self.assertFalse(db.initialized)
        self.assertEquals(db.pool, None)

    @inlineCallbacks
    def test_readwrite(self):
        """
        Add a record, search for it
        """
        db = Database.TestDB(self.mktemp())
        yield db.execute("INSERT into TESTTYPE (KEY, VALUE) values (:1, :2)", ("FOO", "BAR",))
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        items = (yield db.queryList("SELECT * from TESTTYPE"))
        self.assertEqual(items, ("FOO",))
        db.close()

    @inlineCallbacks
    def test_close(self):
        """
        Close database
        """
        db = Database.TestDB(self.mktemp())
        self.assertFalse(db.initialized)
        yield db.open()
        db.close()
        self.assertFalse(db.initialized)
        db.close()

    @inlineCallbacks
    def test_version_upgrade_nonpersistent(self):
        """
        Connect to database and create table
        """

        db_file = self.mktemp()

        db = Database.TestDB(db_file)
        yield db.open()
        yield db.execute("INSERT into TESTTYPE (KEY, VALUE) values (:1, :2)", ("FOO", "BAR",))
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        db.close()
        db = None

        db = Database.TestDBRecreateUpgrade(db_file)
        yield self.inlineCallbackRaises(Database.TestDBRecreateUpgrade.RecreateDBException, db.open)
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, ())
        db.close()

    @inlineCallbacks
    def test_version_upgrade_persistent(self):
        """
        Connect to database and create table
        """
        db_file = self.mktemp()
        db = Database.TestDB(db_file, persistent=True)
        yield db.open()
        yield db.execute("INSERT into TESTTYPE (KEY, VALUE) values (:1, :2)", ("FOO", "BAR",))
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        db.close()
        db = None

        db = Database.TestDBRecreateUpgrade(db_file, persistent=True)
        yield self.inlineCallbackRaises(NotImplementedError, db.open)
        self.assertTrue(os.path.exists(db_file))
        db.close()
        db = None

        db = Database.TestDB(db_file, persistent=True)
        yield db.open()
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        db.close()

    @inlineCallbacks
    def test_version_upgrade_persistent_add_index(self):
        """
        Connect to database and create table
        """
        db_file = self.mktemp()
        db = Database.TestDB(db_file, persistent=True)
        yield db.open()
        yield db.execute("INSERT into TESTTYPE (KEY, VALUE) values (:1, :2)", ("FOO", "BAR",))
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        db.close()
        db = None

        db = Database.TestDBCreateIndexOnUpgrade(db_file, persistent=True)
        yield db.open()
        items = (yield db.query("SELECT * from TESTTYPE"))
        self.assertEqual(items, (("FOO", "BAR"),))
        db.close()
