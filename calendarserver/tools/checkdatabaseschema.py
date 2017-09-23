##
# Copyright (c) 2014-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##
from __future__ import print_function

from getopt import getopt, GetoptError

from twext.enterprise.dal.model import Schema, Table, Column, Sequence, SQLType, \
    Constraint, Function
from twext.enterprise.dal.parseschema import addSQLToSchema, schemaFromPath

from twisted.python.filepath import FilePath

from txdav.common.datastore.sql_dump import DTYPE_MAP_POSTGRES, \
    DEFAULTVALUE_MAP_POSTGRES

import os
import re
import subprocess
import sys

USERNAME = "caldav"
DATABASENAME = "caldav"
PGSOCKETDIR = "127.0.0.1"
SCHEMADIR = "./txdav/common/datastore/sql_schema/"
CONNECTIONURI = ""

# Executables:
PSQL = "../postgresql/_root/bin/psql"


def usage(e=None):
    name = os.path.basename(sys.argv[0])
    print("usage: %s [options]" % (name,))
    print("")
    print(" Check calendar server postgres database and schema")
    print("")
    print("options:")
    print("  -c: connection URI [postgresql://user:password@hostname:port/dbname]")
    print("  -d: path to server's sql_schema directory [./txdav/common/datastore/sql_schema/]")
    print("  -k: postgres socket path (value for psql -h argument [127.0.0.1])")
    print("  -p: location of psql tool if not on PATH already [psql]")
    print("  -x: use default values for OS X server")
    print("  -h --help: print this help and exit")
    print("  -v --verbose: print additional information")
    print("")

    if e:
        sys.stderr.write("%s\n" % (e,))
        sys.exit(64)
    else:
        sys.exit(0)


def execSQL(title, stmt, verbose=False):
    """
    Execute the provided SQL statement, return results as a list of rows.

    @param stmt: the SQL to execute
    @type stmt: L{str}
    """

    cmdArgs = [
        PSQL,
        "-h", PGSOCKETDIR,
        "-d", DATABASENAME,
        "-U", USERNAME,
        "-t",
        "-c", stmt,
    ]
    if CONNECTIONURI:
        cmdArgs = [
            PSQL,
            "-t",
            CONNECTIONURI,
            "-c", stmt,
        ]
    try:
        if verbose:
            print("\n{}".format(title))
            print("Executing: {}".format(" ".join(cmdArgs)))
        out = subprocess.check_output(cmdArgs, stderr=subprocess.STDOUT)
        if verbose:
            print(out)
    except subprocess.CalledProcessError as e:
        if verbose:
            print(e.output)
        raise CheckSchemaError(
            "%s failed:\n%s (exit code = %d)" %
            (PSQL, e.output, e.returncode)
        )

    return [map(lambda x: x.strip(), s.split("|")) for s in out.splitlines()[:-1]]


def getSchemaVersion(verbose=False):
    """
    Return the version number for the schema installed in the database.
    Raise CheckSchemaError if there is an issue.
    """

    out = execSQL(
        "Reading schema version...",
        "select value from calendarserver where name='VERSION';",
        verbose
    )

    try:
        version = int(out[0][0])
    except ValueError as e:
        raise CheckSchemaError(
            "Failed to parse schema version: %s" % (e,)
        )
    return version


def dumpCurrentSchema(verbose=False):

    schemaname = "public"

    schema = Schema("Dumped schema")

    # Sequences
    seqs = {}
    rows = execSQL(
        "Schema sequences...",
        "select sequence_name from information_schema.sequences where sequence_schema = '%s';" % (schemaname,),
        verbose
    )
    for row in rows:
        name = row[0]
        seqs[name.upper()] = Sequence(schema, name.upper())

    # Tables
    tables = {}
    rows = execSQL(
        "Schema tables...",
        "select table_name from information_schema.tables where table_schema = '%s';" % (schemaname,),
        verbose
    )
    for row in rows:
        name = row[0]
        table = Table(schema, name.upper())
        tables[name.upper()] = table

        # Columns
        rows = execSQL(
            "Reading table '{}' columns...".format(name),
            "select column_name, data_type, is_nullable, character_maximum_length, column_default from information_schema.columns where table_schema = '%s' and table_name = '%s';" % (schemaname, name,),
            verbose,
        )
        for name, datatype, is_nullable, charlen, default in rows:
            # TODO: figure out the type
            column = Column(table, name.upper(), SQLType(DTYPE_MAP_POSTGRES.get(datatype, datatype), int(charlen) if charlen else 0))
            table.columns.append(column)
            if default:
                if default.startswith("nextval("):
                    dname = default.split("'")[1].split(".")[-1]
                    column.default = seqs[dname.upper()]
                elif default in DEFAULTVALUE_MAP_POSTGRES:
                    column.default = DEFAULTVALUE_MAP_POSTGRES[default]
                else:
                    try:
                        column.default = int(default)
                    except ValueError:
                        column.default = default
            if is_nullable == "NO":
                table.tableConstraint(Constraint.NOT_NULL, [column.name, ])

    # Key columns
    keys = {}
    rows = execSQL(
        "Schema key columns...",
        "select constraint_name, table_name, column_name from information_schema.key_column_usage where constraint_schema = '%s';" % (schemaname,),
        verbose
    )
    for conname, tname, cname in rows:
        keys[conname] = (tname, cname)

    # Constraints
    constraints = {}
    rows = execSQL(
        "SChema constraints...",
        "select constraint_name, table_name, column_name from information_schema.constraint_column_usage where constraint_schema = '%s';" % (schemaname,),
        verbose
    )
    for conname, tname, cname in rows:
        constraints[conname] = (tname, cname)

    # References - referential_constraints
    rows = execSQL(
        "Schema referential constraints...",
        "select constraint_name, unique_constraint_name, delete_rule from information_schema.referential_constraints where constraint_schema = '%s';" % (schemaname,),
        verbose
    )
    for conname, uconname, delete in rows:
        table = tables[keys[conname][0].upper()]
        column = table.columnNamed(keys[conname][1].upper())
        column.doesReferenceName(constraints[uconname][0].upper())
        if delete != "NO ACTION":
            column.deleteAction = delete.lower()

    # Indexes
    # TODO: handle implicit indexes created via primary key() and unique() statements within CREATE TABLE
    rows = execSQL(
        "Schema indexes...",
        "select indexdef from pg_indexes where schemaname = '%s';" % (schemaname,),
        verbose
    )
    for indexdef in rows:
        addSQLToSchema(schema, indexdef[0].replace("%s." % (schemaname,), "").upper())

    # Functions
    rows = execSQL(
        "Schema functions",
        "select routine_name from information_schema.routines where routine_schema = '%s';" % (schemaname,),
        verbose
    )
    for row in rows:
        name = row[0]
        Function(schema, name)

    return schema


def checkSchema(dbversion, verbose=False):
    """
    Compare schema in the database with the expected schema file.
    """

    dbschema = dumpCurrentSchema(verbose)

    # Find current schema
    fp = FilePath(SCHEMADIR)
    fpschema = fp.child("old").child("postgres-dialect").child("v{}.sql".format(dbversion))
    if not fpschema.exists():
        fpschema = fp.child("current.sql")
    expectedSchema = schemaFromPath(fpschema)

    mismatched = dbschema.compare(expectedSchema)
    if mismatched:
        print("\nCurrent schema in database is mismatched:\n\n" + "\n".join(mismatched))
    else:
        print("\nCurrent schema in database is a match to the expected server version")


class CheckSchemaError(Exception):
    pass


def error(s):
    sys.stderr.write("%s\n" % (s,))
    sys.exit(1)


def main():
    try:
        (optargs, _ignore_args) = getopt(
            sys.argv[1:], "c:d:hk:p:vx", [
                "help",
                "verbose",
            ],
        )
    except GetoptError as e:
        usage(e)

    verbose = False

    global SCHEMADIR, PGSOCKETDIR, PSQL, CONNECTIONURI

    for opt, arg in optargs:
        if opt in ("-h", "--help"):
            usage()
        elif opt in ("-c",):
            CONNECTIONURI = arg
        elif opt in ("-d",):
            SCHEMADIR = arg
        elif opt in ("-k",):
            PGSOCKETDIR = arg
        elif opt in ("-p",):
            PSQL = arg
        elif opt in ("-x",):
            sktdir = FilePath("/var/run/caldavd")
            for skt in sktdir.children():
                if skt.basename().startswith("ccs_postgres_"):
                    PGSOCKETDIR = skt.path
            PSQL = "/Applications/Server.app/Contents/ServerRoot/usr/bin/psql"
            SCHEMADIR = "/Applications/Server.app/Contents/ServerRoot/Library/CalendarServer/lib/python2.7/site-packages/txdav/common/datastore/sql_schema/"
        elif opt in ("-v", "--verbose"):
            verbose = True
        else:
            raise NotImplementedError(opt)

    if not os.access(PSQL, os.X_OK):
        sys.stderr.write("Executable copy of PSQL not found at {}. Exiting\n".format(PSQL))
        sys.exit(0)
        
    # Retrieve the db_version number of the installed schema
    try:
        db_version = getSchemaVersion(verbose=verbose)
    except CheckSchemaError as e:
        db_version = 0

    # Retrieve the version number from the schema file
    currentschema = FilePath(SCHEMADIR).child("current.sql")

    try:
        data = currentschema.getContent()
    except IOError:
        print("Unable to open the current schema file: %s" % (currentschema.path,))
    else:
        found = re.search("insert into CALENDARSERVER values \('VERSION', '(\d+)'\);", data)
        if found is None:
            print("Schema is missing required schema VERSION insert statement: %s" % (currentschema.path,))
        else:
            current_version = int(found.group(1))
            if db_version == current_version:
                print("Schema version {} is current".format(db_version))

            else:  # upgrade needed
                print("Schema needs to be upgraded from {} to {}".format(db_version, current_version))

    checkSchema(db_version, verbose)

if __name__ == "__main__":
    main()
