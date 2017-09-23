# -*- test-case-name: txdav.common.datastore.test.test_sql_tables -*-
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
SQL Table definitions.
"""
from __future__ import print_function

from twisted.python.modules import getModule
from twext.enterprise.dal.syntax import SchemaSyntax, QueryGenerator
from twext.enterprise.dal.model import NO_DEFAULT
from twext.enterprise.dal.model import Sequence, ProcedureCall
from twext.enterprise.dal.parseschema import schemaFromPath
from twext.enterprise.dal.syntax import FixedPlaceholder
from twext.enterprise.ienterprise import ORACLE_DIALECT, POSTGRES_DIALECT, \
    DatabaseType
from twext.enterprise.dal.syntax import Insert
from twext.enterprise.ienterprise import ORACLE_TABLE_NAME_MAX
import hashlib
import itertools


def _schemaFiles(version=None):
    """
    Find the set of files to process, either the current set if C{version} is L{None}, otherwise look
    for the specific version.

    @param version: version identifier (e.g., "v1", "v2" etc)
    @type version: L{str}
    """
    if version is None:
        currentObj = getModule(__name__).filePath.sibling("sql_schema").child("current.sql")
        extrasObj = getModule(__name__).filePath.sibling("sql_schema").child("current-{}-extras.sql".format(ORACLE_DIALECT))
        outObj = getModule(__name__).filePath.sibling("sql_schema").child("current-{}.sql".format(ORACLE_DIALECT))
    else:
        currentObj = getModule(__name__).filePath.sibling("sql_schema").child("old").child(POSTGRES_DIALECT).child("%s.sql" % (version,))
        extrasObj = getModule(__name__).filePath.sibling("sql_schema").child("old").child(ORACLE_DIALECT).child("{}-extras.sql" % (version,))
        outObj = getModule(__name__).filePath.sibling("sql_schema").child("old").child(ORACLE_DIALECT).child("{}.sql" % (version,))

    return currentObj, extrasObj, outObj


def _populateSchema(pathObj=None):
    """
    Generate the global L{SchemaSyntax}.
    """

    if pathObj is None:
        pathObj = _schemaFiles()[0]
    return SchemaSyntax(schemaFromPath(pathObj))


def _schemaExtras(out, extras):
    """
    If an extras file exists, add its entire content to the output stream..
    """

    if extras.exists():
        out.write("\n".join(itertools.dropwhile(lambda x: not x.startswith("-- Extra schema to add"), extras.getContent().splitlines())) + "\n")


schema = _populateSchema()


# Column aliases, defined so that similar tables (such as CALENDAR_OBJECT and
# ADDRESSBOOK_OBJECT) can be used according to a polymorphic interface.

schema.CALENDAR_BIND.RESOURCE_NAME = schema.CALENDAR_BIND.CALENDAR_RESOURCE_NAME
schema.CALENDAR_BIND.RESOURCE_ID = schema.CALENDAR_BIND.CALENDAR_RESOURCE_ID
schema.CALENDAR_BIND.HOME_RESOURCE_ID = schema.CALENDAR_BIND.CALENDAR_HOME_RESOURCE_ID

schema.SHARED_ADDRESSBOOK_BIND.RESOURCE_NAME = schema.SHARED_ADDRESSBOOK_BIND.ADDRESSBOOK_RESOURCE_NAME
schema.SHARED_ADDRESSBOOK_BIND.RESOURCE_ID = schema.SHARED_ADDRESSBOOK_BIND.OWNER_HOME_RESOURCE_ID
schema.SHARED_ADDRESSBOOK_BIND.HOME_RESOURCE_ID = schema.SHARED_ADDRESSBOOK_BIND.ADDRESSBOOK_HOME_RESOURCE_ID

schema.SHARED_GROUP_BIND.RESOURCE_NAME = schema.SHARED_GROUP_BIND.GROUP_ADDRESSBOOK_NAME
schema.SHARED_GROUP_BIND.RESOURCE_ID = schema.SHARED_GROUP_BIND.GROUP_RESOURCE_ID
schema.SHARED_GROUP_BIND.HOME_RESOURCE_ID = schema.SHARED_GROUP_BIND.ADDRESSBOOK_HOME_RESOURCE_ID

schema.CALENDAR_OBJECT_REVISIONS.RESOURCE_ID = schema.CALENDAR_OBJECT_REVISIONS.CALENDAR_RESOURCE_ID
schema.CALENDAR_OBJECT_REVISIONS.HOME_RESOURCE_ID = schema.CALENDAR_OBJECT_REVISIONS.CALENDAR_HOME_RESOURCE_ID
schema.CALENDAR_OBJECT_REVISIONS.COLLECTION_NAME = schema.CALENDAR_OBJECT_REVISIONS.CALENDAR_NAME

schema.ADDRESSBOOK_OBJECT_REVISIONS.RESOURCE_ID = schema.ADDRESSBOOK_OBJECT_REVISIONS.OWNER_HOME_RESOURCE_ID
schema.ADDRESSBOOK_OBJECT_REVISIONS.HOME_RESOURCE_ID = schema.ADDRESSBOOK_OBJECT_REVISIONS.ADDRESSBOOK_HOME_RESOURCE_ID
schema.ADDRESSBOOK_OBJECT_REVISIONS.COLLECTION_NAME = schema.ADDRESSBOOK_OBJECT_REVISIONS.ADDRESSBOOK_NAME

schema.NOTIFICATION_OBJECT_REVISIONS.HOME_RESOURCE_ID = schema.NOTIFICATION_OBJECT_REVISIONS.NOTIFICATION_HOME_RESOURCE_ID
schema.NOTIFICATION_OBJECT_REVISIONS.RESOURCE_ID = schema.NOTIFICATION_OBJECT_REVISIONS.NOTIFICATION_HOME_RESOURCE_ID

schema.CALENDAR_OBJECT.TEXT = schema.CALENDAR_OBJECT.ICALENDAR_TEXT
schema.CALENDAR_OBJECT.UID = schema.CALENDAR_OBJECT.ICALENDAR_UID
schema.CALENDAR_OBJECT.PARENT_RESOURCE_ID = schema.CALENDAR_OBJECT.CALENDAR_RESOURCE_ID

schema.ADDRESSBOOK_OBJECT.TEXT = schema.ADDRESSBOOK_OBJECT.VCARD_TEXT
schema.ADDRESSBOOK_OBJECT.UID = schema.ADDRESSBOOK_OBJECT.VCARD_UID
schema.ADDRESSBOOK_OBJECT.PARENT_RESOURCE_ID = schema.ADDRESSBOOK_OBJECT.ADDRESSBOOK_HOME_RESOURCE_ID


def _combine(**kw):
    """
    Combine two table dictionaries used in a join to produce a single dictionary
    that can be used in formatting.
    """
    result = {}
    for tableRole, tableDictionary in kw.items():
        result.update([("%s:%s" % (tableRole, k), v)
                       for k, v in tableDictionary.items()])
    return result


def _S(tableSyntax):
    """
    Construct a dictionary of strings from a L{TableSyntax} for those queries
    that are still constructed via string interpolation.
    """
    result = {}
    result['name'] = tableSyntax.model.name
    # pkey = tableSyntax.model.primaryKey
    # if pkey is not None:
    #    default = pkey.default
    #    if isinstance(default, Sequence):
    #        result['sequence'] = default.name
    result['sequence'] = schema.model.sequenceNamed('REVISION_SEQ').name
    for columnSyntax in tableSyntax:
        result['column_' + columnSyntax.model.name] = columnSyntax.model.name
    for alias, realColumnSyntax in tableSyntax.columnAliases().items():
        result['column_' + alias] = realColumnSyntax.model.name
    return result


def _schemaConstants(nameColumn, valueColumn):
    """
    Get a constant value from the rows defined in the schema.
    """
    def get(name):
        for row in nameColumn.model.table.schemaRows:
            if row[nameColumn.model] == name:
                return row[valueColumn.model]
    return get


def _schemaConstantsMaps(nameColumn, valueColumn):
    """
    Generate two dicts that map back and forth between SQL enum values and their
    programmatic values.
    """

    toSQL = {}
    fromSQL = {}
    for row in nameColumn.model.table.schemaRows:
        toSQL[row[nameColumn.model]] = row[valueColumn.model]
        fromSQL[row[valueColumn.model]] = row[nameColumn.model]

    return (toSQL, fromSQL)


# Various constants

_homeStatus = _schemaConstants(
    schema.HOME_STATUS.DESCRIPTION,
    schema.HOME_STATUS.ID
)


_HOME_STATUS_NORMAL = _homeStatus('normal')
_HOME_STATUS_EXTERNAL = _homeStatus('external')
_HOME_STATUS_PURGING = _homeStatus('purging')
_HOME_STATUS_MIGRATING = _homeStatus('migrating')
_HOME_STATUS_DISABLED = _homeStatus('disabled')

_childType = _schemaConstants(
    schema.CHILD_TYPE.DESCRIPTION,
    schema.CHILD_TYPE.ID
)


_CHILD_TYPE_NORMAL = _childType('normal')
_CHILD_TYPE_INBOX = _childType('inbox')
_CHILD_TYPE_TRASH = _childType('trash')


_bindStatus = _schemaConstants(
    schema.CALENDAR_BIND_STATUS.DESCRIPTION,
    schema.CALENDAR_BIND_STATUS.ID
)

_BIND_STATUS_INVITED = _bindStatus('invited')
_BIND_STATUS_ACCEPTED = _bindStatus('accepted')
_BIND_STATUS_DECLINED = _bindStatus('declined')
_BIND_STATUS_INVALID = _bindStatus('invalid')
_BIND_STATUS_DELETED = _bindStatus('deleted')


_transpValues = _schemaConstants(
    schema.CALENDAR_TRANSP.DESCRIPTION,
    schema.CALENDAR_TRANSP.ID
)

_TRANSP_OPAQUE = _transpValues('opaque')
_TRANSP_TRANSPARENT = _transpValues('transparent')


_attachmentsMode = _schemaConstants(
    schema.CALENDAR_OBJ_ATTACHMENTS_MODE.DESCRIPTION,
    schema.CALENDAR_OBJ_ATTACHMENTS_MODE.ID
)

_ATTACHMENTS_MODE_NONE = _attachmentsMode('none')
_ATTACHMENTS_MODE_READ = _attachmentsMode('read')
_ATTACHMENTS_MODE_WRITE = _attachmentsMode('write')


_bindMode = _schemaConstants(
    schema.CALENDAR_BIND_MODE.DESCRIPTION,
    schema.CALENDAR_BIND_MODE.ID
)


_BIND_MODE_OWN = _bindMode('own')
_BIND_MODE_READ = _bindMode('read')
_BIND_MODE_WRITE = _bindMode('write')
_BIND_MODE_DIRECT = _bindMode('direct')
_BIND_MODE_INDIRECT = _bindMode('indirect')
_BIND_MODE_GROUP = _bindMode('group')
_BIND_MODE_GROUP_READ = _bindMode('group_read')
_BIND_MODE_GROUP_WRITE = _bindMode('group_write')


_addressBookObjectKind = _schemaConstants(
    schema.ADDRESSBOOK_OBJECT_KIND.DESCRIPTION,
    schema.ADDRESSBOOK_OBJECT_KIND.ID
)

_ABO_KIND_PERSON = _addressBookObjectKind('person')
_ABO_KIND_GROUP = _addressBookObjectKind('group')
_ABO_KIND_RESOURCE = _addressBookObjectKind('resource')
_ABO_KIND_LOCATION = _addressBookObjectKind('location')

scheduleActionToSQL, scheduleActionFromSQL = _schemaConstantsMaps(
    schema.SCHEDULE_ACTION.DESCRIPTION,
    schema.SCHEDULE_ACTION.ID
)


class SchemaBroken(Exception):
    """
    The schema is broken and cannot be translated.
    """


_translatedTypes = {
    'text': 'nclob',
    'boolean': 'integer',
    'varchar': 'nvarchar2',
    'char': 'nchar',
}


def _quoted(x):
    """
    Quote an object for inclusion into an SQL string.

    @note: Do not use this function with untrusted input, as it may be a
        security risk.  This is only for translating local, trusted input from
        the schema.

    @return: the translated SQL string.
    @rtype: C{str}
    """
    if isinstance(x, (str, unicode)):
        return ''.join(["'", x.replace("'", "''"), "'"])
    else:
        return str(x)


def _staticSQL(sql, doquote=False):
    """
    Statically generate some SQL from some DAL syntax objects, interpolating
    any parameters into the body of the string.

    @note: Do not use this function with untrusted input, as it may be a
        security risk.  This is only for translating local, trusted input from
        the schema.

    @param sql: something from L{twext.enterprise.dal}, either a top-level
        statement like L{Insert} or L{Select}, or a fragment such as an
        expression.

    @param doquote: Force all identifiers to be double-quoted, whether they
        conflict with database identifiers or not, for consistency.

    @return: the generated SQL string.
    @rtype: C{str}
    """
    qgen = QueryGenerator(DatabaseType(ORACLE_DIALECT, "pyformat"), FixedPlaceholder('%s'))
    if doquote:
        qgen.shouldQuote = lambda name: True
    if hasattr(sql, 'subSQL'):
        fragment = sql.subSQL(qgen, [])
    else:
        fragment = sql.toSQL(qgen)
    params = tuple([_quoted(param) for param in fragment.parameters])
    result = fragment.text % params
    return result


def _translateSchema(out, schema=schema):
    """
    When run as a script, translate the schema to another dialect.  Currently
    only postgres and oracle are supported, and native format is postgres, so
    emit in oracle format.
    """
    for sequence in schema.model.sequences:
        out.write('create sequence %s;\n' % (sequence.name,))
    out.write("\n\n")

    for table in schema:
        # The only table name which actually exceeds the length limit right now
        # is CALENDAR_OBJECT_ATTACHMENTS_MODE, which isn't actually _used_
        # anywhere, so we can fake it for now.
        if len(table.model.name) > ORACLE_TABLE_NAME_MAX:
            raise SchemaBroken("Table name too long: %s" % (table.model.name,))
        out.write('create table %s (\n' % (table.model.name[:ORACLE_TABLE_NAME_MAX],))
        first = True
        for column in table:
            if first:
                first = False
            else:
                out.write(",\n")

            if len(column.model.name) > ORACLE_TABLE_NAME_MAX:
                raise SchemaBroken("Column name too long: %s" % (column.model.name,))

            typeName = column.model.type.name
            typeName = _translatedTypes.get(typeName, typeName)
            out.write('    "%s" %s' % (column.model.name, typeName))
            if column.model.type.length:
                out.write("(%s)" % (column.model.type.length,))
            if [column.model] == table.model.primaryKey:
                out.write(' primary key')
            default = column.model.default
            if default is not NO_DEFAULT:
                # Can't do default sequence types in Oracle, so don't bother.
                if not isinstance(default, Sequence):
                    out.write(' default')
                    if default is None:
                        out.write(' null')
                    elif isinstance(default, ProcedureCall):
                        # Cheating, because there are currently no other
                        # functions being used.
                        out.write(" CURRENT_TIMESTAMP at time zone 'UTC'")
                    else:
                        if default is True:
                            default = 1
                        elif default is False:
                            default = 0
                        out.write(" " + repr(default))
            if (
                (not column.model.canBeNull()) and
                # Oracle treats empty strings as NULLs, so we have to accept
                # NULL values in columns of a string type.  Other types should
                # be okay though.
                typeName not in ('varchar', 'nclob', 'char', 'nchar', 'nvarchar', 'nvarchar2')
            ):
                out.write(' not null')
            if [column.model] in list(table.model.uniques()):
                out.write(' unique')
            if column.model.references is not None:
                out.write(" references %s" % (column.model.references.name,))
            if column.model.deleteAction is not None:
                out.write(" on delete %s" % (column.model.deleteAction,))

        def writeConstraint(name, cols):
            out.write(", \n")  # the table has to have some preceding columns
            out.write("    %s(%s)" % (
                name, ", ".join('"' + col.name + '"' for col in cols)
            ))

        pk = table.model.primaryKey
        if pk is not None and len(pk) > 1:
            writeConstraint("primary key ", pk)

        for uniqueColumns in table.model.uniques():
            if len(uniqueColumns) == 1:
                continue  # already done inline, skip
            writeConstraint("unique ", uniqueColumns)

        for checkConstraint in table.model.constraints:
            if checkConstraint.type == 'CHECK':
                out.write(", \n    ")
                if checkConstraint.name is not None:
                    out.write('constraint "%s" ' % (checkConstraint.name,))
                out.write("check (%s)" %
                          (_staticSQL(checkConstraint.expression, True)))

        out.write('\n);\n\n')

        for row in table.model.schemaRows:
            cmap = dict([(getattr(table, cmodel.name), val)
                         for (cmodel, val) in row.items()])
            out.write(_staticSQL(Insert(cmap)))
            out.write(";\n")
        out.write("\n\n" if len(table.model.schemaRows) else "\n")

    for index in schema.model.indexes:
        # Index names combine and repeat multiple table names and column names,
        # so several of them conflict once oracle's length limit is applied.
        # To keep them unique within the limit we truncate and append 8 characters
        # of the md5 hash of the full name.
        shortIndexName = "%s_%s" % (
            index.name[:21],
            str(hashlib.md5(index.name).hexdigest())[:8],
        )
        shortTableName = index.table.name[:30]
        out.write(
            'create index %s on %s (\n    ' % (shortIndexName, shortTableName)
        )
        out.write(',\n    '.join(["\"{}\"".format(column.name) for column in index.columns]))
        out.write('\n);\n\n')

    # Functions are skipped as they likely use dialect specific syntax. Instead, functions
    # for other dialects need to be written in an "extras" file which will be appended to
    # the output.
    for function in schema.model.functions:
        out.write("-- Skipped Function {}\n".format(function.name))


if __name__ == '__main__':
    import sys
    version = sys.argv[1] if len(sys.argv) == 2 else None
    current, extras, out = _schemaFiles(version)

    print("Reading from {}".format(current.path))
    print("Extras from  {}".format(extras.path) if extras.exists() else "No extras")
    print("Writing to   {}".format(out.path))

    with out.open("w") as outfd:
        schema = _populateSchema(current)
        _translateSchema(outfd, schema=schema)
        _schemaExtras(outfd, extras)

    print("Done")
