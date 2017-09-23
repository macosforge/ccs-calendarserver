# -*- test-case-name: txdav.caldav.datastore.test.test_sql -*-
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
Tests for the SQL Table definitions in txdav.common.datastore.sql_tables: sample
a couple of tables to make sure the schema is adequately parsed.

These aren't unit tests, they're integration tests to verify the behavior tested
by L{txdav.base.datastore.test.test_parseschema}.
"""

from cStringIO import StringIO

from twisted.python.modules import getModule
from twisted.trial.unittest import TestCase

from twext.enterprise.dal.syntax import SchemaSyntax

from txdav.common.datastore.sql_tables import schema, _translateSchema
from txdav.common.datastore.sql_tables import SchemaBroken

from twext.enterprise.dal.parseschema import splitSQLString
from twext.enterprise.dal.test.test_parseschema import SchemaTestHelper

from textwrap import dedent


class SampleSomeColumns(TestCase, SchemaTestHelper):
    """
    Sample some columns from the tables defined by L{schema} and verify that
    they look correct.
    """

    def translated(self, *schema):
        """
        Translate the given schema (or the default schema if no schema given)
        and return the resulting SQL as a string.
        """
        io = StringIO()
        _translateSchema(io, *schema)
        return io.getvalue()

    def assertSortaEquals(self, a, b):
        """
        Assert that two strings are equals, modulo whitespace differences.
        """
        sortaA = " ".join(a.split())
        sortaB = " ".join(b.split())
        self.assertEquals(sortaA, sortaB)

    def test_addressbookObjectResourceID(self):
        ao = schema.ADDRESSBOOK_OBJECT
        self.assertEquals(ao.RESOURCE_ID.model.name,
                          "RESOURCE_ID")

    def test_schemaTranslation(self):
        """
        Basic integration test to make sure that the current, production schema
        can be translated without errors.
        """
        self.translated()

    def test_schemaTranslationIncludesVersion(self):
        """
        _translateSchema includes 'insert' rows too.
        """

        pathObj = (
            getModule(__name__).filePath
            .parent().sibling("sql_schema").child("current.sql")
        )
        schema = pathObj.getContent()
        pos = schema.find("('VERSION', '")
        version = int(schema[pos + 13:pos + 15])
        self.assertIn("insert into CALENDARSERVER (NAME, VALUE) "
                      "values ('VERSION', '%s');" % version,
                      self.translated())

    def test_translateSingleUnique(self):
        """
        L{_translateSchema} translates single-column 'unique' statements inline.
        """
        self.assertSortaEquals(
            self.translated(
                SchemaSyntax(
                    self.schemaFromString(
                        "create table alpha (beta integer unique)"
                    )
                )
            ),
            'create table alpha ( "beta" integer unique );'
        )

    def test_translateSingleTableUnique(self):
        """
        L{_translateSchema} translates single-column 'unique' statements inline,
        even if they were originally at the table level.
        """
        stx = SchemaSyntax(
            self.schemaFromString(
                "create table alpha (beta integer, unique (beta))"
            )
        )
        self.assertSortaEquals(
            self.translated(stx),
            'create table alpha ( "beta" integer unique );'
        )

    def test_multiTableUnique(self):
        """
        L{_translateSchema} translates multi-column 'unique' statements.
        """

        stx = SchemaSyntax(
            self.schemaFromString(
                "create table alpha ("
                "beta integer, gamma text, unique (beta, gamma))"
            )
        )
        self.assertSortaEquals(
            self.translated(stx),
            'create table alpha ( "beta" integer, "gamma" nclob, '
            'unique ("beta", "gamma") );'
        )

    def test_translateSinglePrimaryKey(self):
        """
        L{_translateSchema} translates single-column 'primary key' statements
        inline.
        """
        self.assertSortaEquals(
            self.translated(
                SchemaSyntax(
                    self.schemaFromString(
                        "create table alpha (beta integer primary key)"
                    )
                )
            ),
            'create table alpha ( "beta" integer primary key );'
        )

    def test_translateSingleTablePrimaryKey(self):
        """
        L{_translateSchema} translates single-column 'primary key' statements
        inline, even if they were originally at the table level.
        """
        stx = SchemaSyntax(
            self.schemaFromString(
                "create table alpha (beta integer, primary key (beta))"
            )
        )
        self.assertSortaEquals(
            self.translated(stx),
            'create table alpha ( "beta" integer primary key );'
        )

    def test_multiTablePrimaryKey(self):
        """
        L{_translateSchema} translates multi-column 'primary key' statements.
        """

        stx = SchemaSyntax(
            self.schemaFromString(
                "create table alpha ("
                "beta integer, gamma text, primary key (beta, gamma))"
            )
        )
        self.assertSortaEquals(
            self.translated(stx),
            'create table alpha ( "beta" integer, "gamma" nclob, '
            'primary key ("beta", "gamma") );'
        )

    def test_primaryKeyUniqueOrdering(self):
        """
        If a table specifies both a PRIMARY KEY and a UNIQUE constraint, the
        PRIMARY KEY will always be emitted first.
        """
        stx = SchemaSyntax(
            self.schemaFromString(
                "create table alpha ("
                "beta integer, gamma text, delta integer, "
                "unique (beta, delta), primary key (beta, gamma))"
            )
        )
        self.assertSortaEquals(
            self.translated(stx),
            'create table alpha ( '
            '"beta" integer, "gamma" nclob, "delta" integer, '
            'primary key ("beta", "gamma"), unique ("beta", "delta") );'
        )

    def test_anonymousCheckConstraint(self):
        """
        Named 'check' constraints are propagated through translation without
        modification.
        """
        self.assertSortaEquals(
            self.translated(SchemaSyntax(self.schemaFromString(
                "create table alpha ( "
                'beta integer, check (beta > 3)'
                " );"
            ))),
            "create table alpha ( "
            '"beta" integer, check ("beta" > 3)'
            " );"
        )

    def test_namedCheckConstraint(self):
        """
        Named 'check' constraints are propagated through translation without
        modification.
        """
        self.assertSortaEquals(
            self.translated(SchemaSyntax(self.schemaFromString(
                "create table alpha ( "
                'beta integer, constraint beta_lt_3 check (beta > 3)'
                " );"
            ))),
            "create table alpha ( "
            '"beta" integer, constraint "beta_lt_3" check ("beta" > 3)'
            " );"
        )

    def test_youBrokeTheSchema(self):
        """
        Oracle table names have a 30-character limit.  Our schema translator
        simply truncates the names if necessary, but that means that you can't
        have tables whose first 30 characters differ.  L{_translateSchema}
        raises a SchemaBroken() exception if this happens.  (This test is to
        make sure L{test_schemaTranslation}, above, will actually fail if this
        happens.)
        """
        # TODO: same thing for sequences.
        schema = self.schemaFromString(
            """
            create table same_012345678012345678990123456789_1 (foo integer);
            create table same_012345678012345678990123456789_2 (bar text);
            """
        )
        self.assertRaises(
            SchemaBroken, self.translated, SchemaSyntax(schema)
        )


class SQLSplitterTests(TestCase):
    """
    Test that strings which mix zero or more sql statements with zero or more
    pl/sql statements are split into individual statements.
    """

    def test_dontSplitOneStatement(self):
        """
        A single sql statement yields a single string
        """
        result = splitSQLString("select * from foo;")
        r1 = next(result)
        self.assertEquals(r1, "select * from foo")
        self.assertRaises(StopIteration, result.next)

    def test_returnTwoSimpleStatements(self):
        """
        Two simple sql statements yield two separate strings
        """
        result = splitSQLString("select count(*) from baz; select bang from boop;")
        r1 = next(result)
        self.assertEquals(r1, "select count(*) from baz")
        r2 = next(result)
        self.assertEquals(r2, "select bang from boop")
        self.assertRaises(StopIteration, result.next)

    def test_returnOneComplexStatement(self):
        """
        One complex sql statement yields a single string
        """
        bigSQL = dedent(
            '''SELECT
                  CL.CODE,
                  CL.CATEGORY,
               FROM
                  CLIENTS_SUPPLIERS CL
                  INVOICES I
               WHERE
                  CL.CODE = I.CODE AND
                  CL.CATEGORY = I.CATEGORY AND
                  CL.UP_DATE =
                    (SELECT
                       MAX(CL2.UP_DATE)
                     FROM
                       CLIENTS_SUPPLIERS CL2
                     WHERE
                       CL2.CODE = I.CODE AND
                       CL2.CATEGORY = I.CATEGORY AND
                       CL2.UP_DATE <= I.EMISSION
                    ) AND
                    I.EMISSION BETWEEN DATE1 AND DATE2;''')
        result = splitSQLString(bigSQL)
        r1 = next(result)
        self.assertEquals(r1, bigSQL.rstrip(";"))
        self.assertRaises(StopIteration, result.next)

    def test_returnOnePlSQL(self):
        """
        One pl/sql block yields a single string
        """
        plsql = dedent(
            '''BEGIN
               LOOP
                   INSERT INTO T1 VALUES(i,i);
                   i := i+1;
                   EXIT WHEN i>100;
               END LOOP;
               END;''')
        s1 = 'BEGIN\nLOOP\nINSERT INTO T1 VALUES(i,i);i := i+1;EXIT WHEN i>100;END LOOP;END;'
        result = splitSQLString(plsql)
        r1 = next(result)
        self.assertEquals(r1, s1)
        self.assertRaises(StopIteration, result.next)

    def test_returnOnePlSQLAndOneSQL(self):
        """
        One sql statement and one pl/sql statement yields two separate strings
        """
        sql = dedent(
            '''SELECT EGM.Name, BioEntity.BioEntityId INTO AUX
                FROM EGM
                INNER JOIN BioEntity
                    ON EGM.name LIKE BioEntity.Name AND EGM.TypeId = BioEntity.TypeId
                OPTION (MERGE JOIN);''')
        plsql = dedent(
            '''BEGIN
               FOR i IN 1..10 LOOP
                   IF MOD(i,2) = 0 THEN
                       INSERT INTO temp VALUES (i, x, 'i is even');
                   ELSE
                       INSERT INTO temp VALUES (i, x, 'i is odd');
                   END IF;
                   x := x + 100;
               END LOOP;
               COMMIT;
               END;''')
        s2 = "BEGIN\nFOR i IN 1..10 LOOP\nIF MOD(i,2) = 0 THEN\nINSERT INTO temp VALUES (i, x, 'i is even');ELSE\nINSERT INTO temp VALUES (i, x, 'i is odd');END IF;x := x + 100;END LOOP;COMMIT;END;"
        result = splitSQLString(sql + plsql)
        r1 = next(result)
        self.assertEquals(r1, sql.rstrip(";"))
        r2 = next(result)
        self.assertEquals(r2, s2)
        self.assertRaises(StopIteration, result.next)

    def test_returnOnePlSQLAndOneSQLAndOneFunction(self):
        """
        One sql statement and one pl/sql statement yields two separate strings
        """
        sql = dedent(
            '''SELECT EGM.Name, BioEntity.BioEntityId INTO AUX
                FROM EGM
                INNER JOIN BioEntity
                    ON EGM.name LIKE BioEntity.Name AND EGM.TypeId = BioEntity.TypeId
                OPTION (MERGE JOIN);''')
        sqlfn = dedent(
            '''CREATE or REPLACE FUNCTION foobar() RETURNS integer as $$
               DECLARE
                 result integer;
               BEGIN
                SELECT ID into result from JOB;
                RETURN result;
               END
               $$ LANGUAGE plpgsql;''')
        plsql = dedent(
            '''BEGIN
               FOR i IN 1..10 LOOP
                   IF MOD(i,2) = 0 THEN
                       INSERT INTO temp VALUES (i, x, 'i is even');
                   ELSE
                       INSERT INTO temp VALUES (i, x, 'i is odd');
                   END IF;
                   x := x + 100;
               END LOOP;
               COMMIT;
               END;''')
        s3 = "BEGIN\nFOR i IN 1..10 LOOP\nIF MOD(i,2) = 0 THEN\nINSERT INTO temp VALUES (i, x, 'i is even');ELSE\nINSERT INTO temp VALUES (i, x, 'i is odd');END IF;x := x + 100;END LOOP;COMMIT;END;"
        result = splitSQLString(sql + sqlfn + plsql)
        r1 = next(result)
        self.assertEquals(r1, sql.rstrip(";"))
        r2 = next(result)
        self.assertEquals(r2, sqlfn.rstrip(";"))
        r3 = next(result)
        self.assertEquals(r3, s3)
        self.assertRaises(StopIteration, result.next)

    def test_returnTwoPlSQLCreateOrReplace(self):
        """
        One pl/sql block yields a single string
        """
        plsql = """create or replace function next_job_all(now timestamp)
  return integer is
  cursor c1 is
   select JOB_ID from JOB
   where ASSIGNED is NULL and PAUSE = 0 and NOT_BEFORE <= now
   order by PRIORITY desc
   for update skip locked;
  result integer;
begin
  open c1;
  fetch c1 into result;
  close c1;
  return result;
end;
/

create or replace function next_job_medium_high(now timestamp)
  return integer is
  cursor c1 is
    select JOB_ID from JOB
    where PRIORITY != 0 and ASSIGNED is NULL and PAUSE = 0 and NOT_BEFORE <= now
    order by PRIORITY desc
    for update skip locked;
  result integer;
begin
  open c1;
  fetch c1 into result;
  close c1;
  return result;
end;
/
"""
        s1 = """create or replace function next_job_all(now timestamp)
return integer is
cursor c1 is
select JOB_ID from JOB
where ASSIGNED is NULL and PAUSE = 0 and NOT_BEFORE <= now
order by PRIORITY desc
for update skip locked;result integer;begin
open c1;fetch c1 into result;close c1;return result;end;"""
        s2 = """create or replace function next_job_medium_high(now timestamp)
return integer is
cursor c1 is
select JOB_ID from JOB
where PRIORITY != 0 and ASSIGNED is NULL and PAUSE = 0 and NOT_BEFORE <= now
order by PRIORITY desc
for update skip locked;result integer;begin
open c1;fetch c1 into result;close c1;return result;end;"""

        result = splitSQLString(plsql)
        r1 = next(result)
        self.assertEquals(r1, s1)
        r2 = next(result)
        self.assertEquals(r2, s2)
        self.assertRaises(StopIteration, result.next)

    def test_actualSchemaUpgrade(self):
        """
        A real-world schema upgrade is split into the expected number of statements,
        ignoring comments
        """
        realsql = dedent(
            '''
            ----
            -- Copyright (c) 2011-2017 Apple Inc. All rights reserved.
            --
            -- Licensed under the Apache License, Version 2.0 (the "License");
            -- you may not use this file except in compliance with the License.
            -- You may obtain a copy of the License at
            --
            -- http://www.apache.org/licenses/LICENSE-2.0
            --
            -- Unless required by applicable law or agreed to in writing, software
            -- distributed under the License is distributed on an "AS IS" BASIS,
            -- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
            -- See the License for the specific language governing permissions and
            -- limitations under the License.
            ----

            ---------------------------------------------------
            -- Upgrade database schema from VERSION 16 to 17 --
            ---------------------------------------------------


            ------------------------------
            -- CALENDAR_OBJECT clean-up --
            ------------------------------

            begin
            for i in (select constraint_name from user_cons_columns where column_name = 'ORGANIZER_OBJECT')
            loop
            execute immediate 'alter table calendar_object drop constraint ' || i.constraint_name;
            end loop;
            end;

            alter table CALENDAR_OBJECT
             drop (ORGANIZER_OBJECT);

            create index CALENDAR_OBJECT_ICALE_82e731d5 on CALENDAR_OBJECT (
                ICALENDAR_UID
            );


            -- Now update the version
            update CALENDARSERVER set VALUE = '17' where NAME = 'VERSION';
            ''')
        s1 = "begin\nfor i in (select constraint_name from user_cons_columns where column_name = 'ORGANIZER_OBJECT')\nloop\nexecute immediate 'alter table calendar_object drop constraint ' || i.constraint_name;end loop;end;"
        s2 = 'alter table CALENDAR_OBJECT\n drop (ORGANIZER_OBJECT)'
        s3 = 'create index CALENDAR_OBJECT_ICALE_82e731d5 on CALENDAR_OBJECT (\n    ICALENDAR_UID\n)'
        s4 = "update CALENDARSERVER set VALUE = '17' where NAME = 'VERSION'"
        result = splitSQLString(realsql)
        r1 = next(result)
        self.assertEquals(r1, s1)
        r2 = next(result)
        self.assertEquals(r2, s2)
        r3 = next(result)
        self.assertEquals(r3, s3)
        r4 = next(result)
        self.assertEquals(r4, s4)
        self.assertRaises(StopIteration, result.next)
