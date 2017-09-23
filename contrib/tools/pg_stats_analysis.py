##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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
from __future__ import absolute_import

import sqlparse
import os
from gzip import GzipFile
import collections
from . import tables
import textwrap
import sys
import getopt


def safePercent(x, y, multiplier=100):
    return ((multiplier * x) / y) if y else 0


def _is_literal(token):
    if token.ttype in sqlparse.tokens.Literal:
        return True
    if token.ttype == sqlparse.tokens.Keyword and token.value in (u'True', u'False'):
        return True
    return False


def _substitute(expression, replacement):
    try:
        expression.tokens
    except AttributeError:
        return

    for i, token in enumerate(expression.tokens):
        if _is_literal(token):
            expression.tokens[i] = replacement
        elif token.is_whitespace():
            expression.tokens[i] = sqlparse.sql.Token('Whitespace', ' ')
        else:
            _substitute(token, replacement)


def sqlnormalize(sql):
    try:
        statements = sqlparse.parse(sql)
    except ValueError as e:
        print(e)
    # Replace any literal values with placeholders
    qmark = sqlparse.sql.Token('Operator', '?')
    _substitute(statements[0], qmark)
    return sqlparse.format(statements[0].to_unicode().encode('ascii'))

COLUMN_userid = 0
COLUMN_dbid = 1
COLUMN_query = 2
COLUMN_calls = 3
COLUMN_total_time = 4
COLUMN_rows = 5
COLUMN_shared_blks_hit = 6
COLUMN_shared_blks_read = 7
COLUMN_shared_blks_written = 8
COLUMN_local_blks_hit = 9
COLUMN_local_blks_read = 10
COLUMN_local_blks_written = 11
COLUMN_temp_blks_read = 12
COLUMN_temp_blks_written = 13


def sqlStatementsReport(entries):

    dcount = collections.defaultdict(int)
    dtime = collections.defaultdict(float)
    drows = collections.defaultdict(int)
    for entry in entries:
        dcount[entry[COLUMN_query]] += int(entry[COLUMN_calls])
        dtime[entry[COLUMN_query]] += float(entry[COLUMN_total_time])
        drows[entry[COLUMN_query]] += int(entry[COLUMN_rows])

    daverage = {}
    for k in dcount.keys():
        daverage[k] = dtime[k] / dcount[k]

    counttotal = sum(dcount.values())
    timetotal = sum(dtime.values())
    averagetotal = sum(daverage.values())

    for sorttype, sortedkeys in (
        ("total time", [i[0] for i in sorted(dtime.iteritems(), key=lambda x:x[1], reverse=True)],),
        ("count", [i[0] for i in sorted(dcount.iteritems(), key=lambda x:x[1], reverse=True)],),
        ("average time", [i[0] for i in sorted(daverage.iteritems(), key=lambda x:x[1], reverse=True)],),
    ):
        table = tables.Table()
        table.addHeader(("Statement", "Count", "Count %", "Total Time", "Total Time %", "Av. Time", "Av. Time %", "Av. rows",))
        table.setDefaultColumnFormats((
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.2f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.2f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.2f%%", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%.1f", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        ))

        for key in sortedkeys:

            keylines = textwrap.wrap(key, 72, subsequent_indent="  ")
            table.addRow((
                keylines[0],
                dcount[key],
                safePercent(dcount[key], counttotal, 100.0),
                dtime[key],
                safePercent(dtime[key], timetotal, 100.0),
                daverage[key],
                safePercent(daverage[key], averagetotal, 100.0),
                float(drows[key]) / dcount[key],
            ))

            for keyline in keylines[1:]:
                table.addRow((
                    keyline,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ))

        print("Queries sorted by %s" % (sorttype,))
        table.printTable()
        print("")


def parseStats(logFilePath, donormlize=True, verbose=False):

    fpath = os.path.expanduser(logFilePath)
    if fpath.endswith(".gz"):
        f = GzipFile(fpath)
    else:
        f = open(fpath)

    # Punt past data
    for line in f:
        if line.startswith("---"):
            break

    f.close()

    entries = []
    for line in f:
        bits = line.split("|")
        if len(bits) > COLUMN_query:
            while bits[COLUMN_query].endswith("+"):
                line = next(f)
                newbits = line.split("|")
                bits[COLUMN_query] = bits[COLUMN_query][:-1] + newbits[COLUMN_query]

            pos = bits[COLUMN_query].find("BEGIN:VCALENDAR")
            if pos != -1:
                bits[COLUMN_query] = bits[COLUMN_query][:pos]

            if donormlize:
                bits[COLUMN_query] = sqlnormalize(bits[COLUMN_query].strip())

            if bits[COLUMN_query] not in (
                "BEGIN",
                "COMMIT",
                "ROLLBACK",
            ) and bits[COLUMN_query].find("pg_catalog") == -1:
                bits = [bit.strip() for bit in bits]
                entries.append(bits)
                if verbose and divmod(len(entries), 1000)[1] == 0:
                    print("%d entries" % (len(entries),))
                # if float(bits[COLUMN_total_time]) > 1:
                #     print(bits[COLUMN_total_time], bits[COLUMN_query])

    if verbose:
        print("Read %d entries" % (len(entries,)))

    sqlStatementsReport(entries)


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: pg_stats_analysis.py [options] FILE
Options:
    -h             Print this help and exit
    -v             Generate progress information
    --no-normalize Do not normalize SQL statements

Arguments:
    FILE      File name for pg_stat_statements output to analyze.

Description:
This utility will analyze the output of s pg_stat_statement table.
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == '__main__':

    normalize = True
    verbose = False
    options, args = getopt.getopt(sys.argv[1:], "hv", ["no-normalize", ])

    for option, value in options:
        if option == "-h":
            usage()
        elif option == "-v":
            verbose = True
        elif option == "--no-normalize":
            normalize = False
        else:
            usage("Unrecognized option: %s" % (option,))

    # Process arguments
    if len(args) != 1:
        usage("Must have a file argument")

    parseStats(args[0], normalize, verbose)
