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

"""
Generic ADAPI database access object.
"""

__all__ = [
    "AbstractADBAPIDatabase",
]

import threading

try:
    from txdav.base.datastore.subpostgres import postgres
except ImportError:
    postgres = None

from twisted.enterprise.adbapi import ConnectionPool
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.threadpool import ThreadPool

from twext.python.log import Logger

from twistedcaldav.config import ConfigurationError

log = Logger()


class ConnectionCloseThread(threading.Thread):
    """
    An L{Thread} that closes its DB connection when it has finished running.
    """

    def __init__(self, group=None, target=None, name=None,
                 args=(), kwargs=None, verbose=None):

        if target is not None:
            self._realTarget = target
            target = self.targetWithConnectionClose
        super(ConnectionCloseThread, self).__init__(group=group, target=target, name=name, args=args, kwargs=kwargs, verbose=verbose)

    def targetWithConnectionClose(self, *args, **kwargs):
        self._realTarget(*args, **kwargs)
        if hasattr(self, "_db_close"):
            self._db_close()


class ConnectionClosingThreadPool(ThreadPool):
    """
    A L{ThreadPool} that closes connections for each worker thread when stopped.
    """

    threadFactory = ConnectionCloseThread

    def stop(self):
        for tid, conn in self.pool.connections.items():
            for thread in self.threads:
                if thread.ident == tid:
                    thread._db_close = lambda: self.pool.disconnect(conn)
        ThreadPool.stop(self)


class AbstractADBAPIDatabase(object):
    """
    A generic SQL database.
    """

    def __init__(self, dbID, dbapiName, dbapiArgs, persistent, **kwargs):
        """

        @param persistent: C{True} if the data in the DB must be perserved during upgrades,
            C{False} if the DB data can be re-created from an external source.
        @type persistent: bool
        """
        self.dbID = dbID
        self.dbapiName = dbapiName
        self.dbapiArgs = dbapiArgs
        self.dbapikwargs = kwargs

        self.persistent = persistent

        self.initialized = False

    def __repr__(self):
        return "<%s %r>" % (self.__class__.__name__, self.pool)

    @inlineCallbacks
    def open(self):
        """
        Access the underlying database.
        @return: a db2 connection object for this index's underlying data store.
        """
        if not self.initialized:

            self.pool = ConnectionPool(self.dbapiName, *self.dbapiArgs, **self.dbapikwargs)

            # sqlite3 is not thread safe which means we have to close the sqlite3 connections in the same thread that
            # opened them. We need a special thread pool class that has a thread worker function that does a close
            # when a thread is closed.
            if self.dbapiName == "sqlite3":
                self.pool.threadpool.stop()
                self.pool.threadpool = ConnectionClosingThreadPool(1, 1)
                self.pool.threadpool.start()
                self.pool.threadpool.pool = self.pool

            #
            # Set up the schema
            #
            # Create CALDAV table if needed

            try:
                test = (yield self._test_schema_table())
                if test:
                    version = (yield self._db_value_for_sql("select VALUE from CALDAV where KEY = 'SCHEMA_VERSION'"))
                    dbtype = (yield self._db_value_for_sql("select VALUE from CALDAV where KEY = 'TYPE'"))

                    if (version != self._db_version()) or (dbtype != self._db_type()):

                        if dbtype != self._db_type():
                            log.error(
                                "Database {db} has different type ({t1} vs. {t2})",
                                db=self.dbID, t1=dbtype, t2=self._db_type()
                            )

                            # Delete this index and start over
                            yield self._db_remove()
                            yield self._db_init()

                        elif version != self._db_version():
                            log.error(
                                "Database {db} has different schema (v.{v1} vs. v.{v2})",
                                db=self.dbID, v1=version, v2=self._db_version()
                            )

                            # Upgrade the DB
                            yield self._db_upgrade(version)

                else:
                    yield self._db_init()
                self.initialized = True
            except:
                # Clean up upon error so we don't end up leaking threads
                self.pool.close()
                self.pool = None
                raise

    def close(self):

        if self.initialized:
            try:
                self.pool.close()
            except Exception as e:
                log.error("Error whilst closing connection pool: {ex}", ex=e)
            self.pool = None
            self.initialized = False

    @inlineCallbacks
    def clean(self):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                yield self._db_empty_data_tables()
            except Exception as e:
                log.error("Error in database clean: {ex}", ex=e)
                self.close()
            else:
                break

    @inlineCallbacks
    def execute(self, sql, *query_params):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                yield self._db_execute(sql, *query_params)
            except Exception as e:
                log.error("Error in database execute: {ex}", ex=e)
                self.close()
            else:
                break

    @inlineCallbacks
    def executescript(self, script):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                yield self._db_execute_script(script)
            except Exception as e:
                log.error("Error in database executescript: {ex}", ex=e)
                self.close()
            else:
                break

    @inlineCallbacks
    def query(self, sql, *query_params):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                result = (yield self._db_all_values_for_sql(sql, *query_params))
            except Exception as e:
                log.error("Error in database query: {ex}", ex=e)
                self.close()
            else:
                break

        returnValue(result)

    @inlineCallbacks
    def queryList(self, sql, *query_params):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                result = (yield self._db_values_for_sql(sql, *query_params))
            except Exception as e:
                log.error("Error in database queryList: {ex}", ex=e)
                self.close()
            else:
                break

        returnValue(result)

    @inlineCallbacks
    def queryOne(self, sql, *query_params):

        # Re-try at least once
        for _ignore in (0, 1):
            if not self.initialized:
                yield self.open()

            try:
                result = (yield self._db_value_for_sql(sql, *query_params))
            except Exception as e:
                log.error("Error in database queryOne: {ex}", ex=e)
                self.close()
            else:
                break

        returnValue(result)

    def _db_version(self):
        """
        @return: the schema version assigned to this DB.
        """
        raise NotImplementedError

    def _db_type(self):
        """
        @return: the collection type assigned to this DB.
        """
        raise NotImplementedError

    def _test_schema_table(self):
        return self._test_table("CALDAV")

    @inlineCallbacks
    def _db_init(self):
        """
        Initialise the underlying database tables.
        """
        log.info("Initializing database {db}", db=self.dbID)

        # TODO we need an exclusive lock of some kind here to prevent a race condition
        # in which multiple processes try to create the tables.

        yield self._db_init_schema_table()
        yield self._db_init_data_tables()
        yield self._db_recreate()

    @inlineCallbacks
    def _db_init_schema_table(self):
        """
        Initialise the underlying database tables.
        @param db_filename: the file name of the index database.
        @param q:           a database cursor to use.
        """

        #
        # CALDAV table keeps track of our schema version and type
        #
        yield self._create_table("CALDAV", (
            ("KEY", "text unique"),
            ("VALUE", "text unique"),
        ), True)

        yield self._db_execute(
            """
            insert or ignore into CALDAV (KEY, VALUE)
            values ('SCHEMA_VERSION', :1)
            """, (self._db_version(),)
        )
        yield self._db_execute(
            """
            insert or ignore into CALDAV (KEY, VALUE)
            values ('TYPE', :1)
            """, (self._db_type(),)
        )

    def _db_init_data_tables(self):
        """
        Initialise the underlying database tables.
        """
        raise NotImplementedError

    def _db_empty_data_tables(self):
        """
        Delete the database tables.
        """

        # Implementations can override this to re-create data
        pass

    def _db_recreate(self):
        """
        Recreate the database tables.
        """

        # Implementations can override this to re-create data
        pass

    @inlineCallbacks
    def _db_upgrade(self, old_version):
        """
        Upgrade the database tables.
        """

        if self.persistent:
            yield self._db_upgrade_data_tables(old_version)
            yield self._db_upgrade_schema()
        else:
            # Non-persistent DB's by default can be removed and re-created. However, for simple
            # DB upgrades they SHOULD override this method and handle those for better performance.
            yield self._db_remove()
            yield self._db_init()

    def _db_upgrade_data_tables(self, old_version):
        """
        Upgrade the data from an older version of the DB.
        """
        # Persistent DB's MUST override this method and do a proper upgrade. Their data
        # cannot be thrown away.
        raise NotImplementedError("Persistent databases MUST support an upgrade method.")

    @inlineCallbacks
    def _db_upgrade_schema(self):
        """
        Upgrade the stored schema version to the current one.
        """
        yield self._db_execute("insert or replace into CALDAV (KEY, VALUE) values ('SCHEMA_VERSION', :1)", (self._db_version(),))

    @inlineCallbacks
    def _db_remove(self):
        """
        Remove all database information (all the tables)
        """
        yield self._db_remove_data_tables()
        yield self._db_remove_schema()

    def _db_remove_data_tables(self):
        """
        Remove all the data from an older version of the DB.
        """
        raise NotImplementedError("Each database must remove its own tables.")

    @inlineCallbacks
    def _db_remove_schema(self):
        """
        Remove the stored schema version table.
        """
        yield self._db_execute("drop table if exists CALDAV")

    @inlineCallbacks
    def _db_all_values_for_sql(self, sql, *query_params):
        """
        Execute an SQL query and obtain the resulting values.
        @param sql: the SQL query to execute.
        @param query_params: parameters to C{sql}.
        @return: an interable of values in the first column of each row
            resulting from executing C{sql} with C{query_params}.
        @raise AssertionError: if the query yields multiple columns.
        """

        sql = self._prepare_statement(sql)
        results = (yield self.pool.runQuery(sql, *query_params))
        returnValue(tuple(results))

    @inlineCallbacks
    def _db_values_for_sql(self, sql, *query_params):
        """
        Execute an SQL query and obtain the resulting values.

        @param sql: the SQL query to execute.
        @param query_params: parameters to C{sql}.
        @return: an interable of values in the first column of each row
            resulting from executing C{sql} with C{query_params}.
        @raise AssertionError: if the query yields multiple columns.
        """

        sql = self._prepare_statement(sql)
        results = (yield self.pool.runQuery(sql, *query_params))
        returnValue(tuple([row[0] for row in results]))

    @inlineCallbacks
    def _db_value_for_sql(self, sql, *query_params):
        """
        Execute an SQL query and obtain a single value.

        @param sql: the SQL query to execute.
        @param query_params: parameters to C{sql}.
        @return: the value resulting from the executing C{sql} with
            C{query_params}.
        @raise AssertionError: if the query yields multiple rows or columns.
        """
        value = None
        for row in (yield self._db_values_for_sql(sql, *query_params)):
            assert value is None, "Multiple values in DB for %s %s" % (sql, query_params)
            value = row
        returnValue(value)

    def _db_execute(self, sql, *query_params):
        """
        Execute an SQL operation that returns None.

        @param sql: the SQL query to execute.
        @param query_params: parameters to C{sql}.
        @return: an iterable of tuples for each row resulting from executing
            C{sql} with C{query_params}.
        """

        sql = self._prepare_statement(sql)
        return self.pool.runOperation(sql, *query_params)

    """
    Since different databases support different types of columns and modifiers on those we need to
    have an "abstract" way of specifying columns in our code and then map the abstract specifiers to
    the underlying DB's allowed types.

    Types we can use are:

    integer
    text
    text(n)
    date
    serial

    The " unique" modifier can be appended to any of those.
    """

    def _map_column_types(self, type):
        raise NotImplementedError

    def _create_table(self, name, columns, ifnotexists=False):
        raise NotImplementedError

    def _test_table(self, name):
        raise NotImplementedError

    def _create_index(self, name, ontable, columns, ifnotexists=False):
        raise NotImplementedError

    def _prepare_statement(self, sql):
        raise NotImplementedError


class ADBAPISqliteMixin(object):

    @classmethod
    def _map_column_types(self, coltype):

        result = ""
        splits = coltype.split()
        if splits[0] == "integer":
            result = "integer"
        elif splits[0] == "text":
            result = "text"
        elif splits[0].startswith("text("):
            result = splits[0]
        elif splits[0] == "date":
            result = "date"
        elif splits[0] == "serial":
            result = "integer primary key autoincrement"

        if len(splits) > 1 and splits[1] == "unique":
            result += " unique"

        return result

    @inlineCallbacks
    def _create_table(self, name, columns, ifnotexists=False):

        colDefs = ["%s %s" % (colname, self._map_column_types(coltype)) for colname, coltype in columns]
        statement = "create table %s%s (%s)" % (
            "if not exists " if ifnotexists else "",
            name,
            ", ".join(colDefs),
        )
        yield self._db_execute(statement)

    @inlineCallbacks
    def _test_table(self, name):
        result = (yield self._db_value_for_sql("""
        select (1) from SQLITE_MASTER
         where TYPE = 'table' and NAME = '%s'
        """ % (name,)))
        returnValue(result)

    @inlineCallbacks
    def _create_index(self, name, ontable, columns, ifnotexists=False):

        statement = "create index %s%s on %s (%s)" % (
            "if not exists " if ifnotexists else "",
            name,
            ontable,
            ", ".join(columns),
        )
        yield self._db_execute(statement)

    def _prepare_statement(self, sql):
        # We are going to use the sqlite syntax of :1, :2 etc for our
        # internal statements so we do not need to remap those
        return sql

if postgres:

    class ADBAPIPostgreSQLMixin(object):

        @classmethod
        def _map_column_types(self, coltype):

            result = ""
            splits = coltype.split()
            if splits[0] == "integer":
                result = "integer"
            elif splits[0] == "text":
                result = "text"
            elif splits[0].startswith("text("):
                result = "char" + splits[0][4:]
            elif splits[0] == "date":
                result = "date"
            elif splits[0] == "serial":
                result = "serial"

            if len(splits) > 1 and splits[1] == "unique":
                result += " unique"

            return result

        @inlineCallbacks
        def _create_table(self, name, columns, ifnotexists=False):

            colDefs = ["%s %s" % (colname, self._map_column_types(coltype)) for colname, coltype in columns]
            statement = "create table %s (%s)" % (
                name,
                ", ".join(colDefs),
            )

            try:
                yield self._db_execute(statement)
            except postgres.DatabaseError:

                if not ifnotexists:
                    raise

                result = (yield self._test_table(name))
                if not result:
                    raise

        @inlineCallbacks
        def _test_table(self, name):
            result = (yield self._db_value_for_sql("""
            select * from pg_tables
             where tablename = '%s'
            """ % (name.lower(),)))
            returnValue(result)

        @inlineCallbacks
        def _create_index(self, name, ontable, columns, ifnotexists=False):

            statement = "create index %s on %s (%s)" % (
                name,
                ontable,
                ", ".join(columns),
            )

            try:
                yield self._db_execute(statement)
            except postgres.DatabaseError:

                if not ifnotexists:
                    raise

                result = (yield self._test_table(name))
                if not result:
                    raise

        @inlineCallbacks
        def _db_init_schema_table(self):
            """
            Initialise the underlying database tables.
            @param db_filename: the file name of the index database.
            @param q:           a database cursor to use.
            """

            #
            # CALDAV table keeps track of our schema version and type
            #
            try:
                yield self._create_table("CALDAV", (
                    ("KEY", "text unique"),
                    ("VALUE", "text unique"),
                ), True)

                yield self._db_execute(
                    """
                    insert into CALDAV (KEY, VALUE)
                    values ('SCHEMA_VERSION', :1)
                    """, (self._db_version(),)
                )
                yield self._db_execute(
                    """
                    insert into CALDAV (KEY, VALUE)
                    values ('TYPE', :1)
                    """, (self._db_type(),)
                )
            except postgres.DatabaseError:
                pass

        def _prepare_statement(self, sql):
            # Convert :1, :2 etc format into %s
            ctr = 1
            while sql.find(":%d" % (ctr,)) != -1:
                sql = sql.replace(":%d" % (ctr,), "%s")
                ctr += 1
            return sql

else:
    class ADBAPIPostgreSQLMixin(object):

        def __init__(self):
            raise ConfigurationError("PostgreSQL module not available.")
