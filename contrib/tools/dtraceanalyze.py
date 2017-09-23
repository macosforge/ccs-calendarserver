#!/usr/bin/env python
# coding=utf-8

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
from __future__ import print_function
from __future__ import with_statement
from __future__ import absolute_import

import collections
import getopt
import os
import re
import sys
from . import tables


class Dtrace(object):

    class DtraceLine(object):

        prefix_maps = {
            "/usr/share/caldavd/lib/python/": "{caldavd}/",
            "/System/Library/Frameworks/Python.framework/Versions/2.7/lib/python2.6": "{Python}",
            "/System/Library/Frameworks/Python.framework/Versions/2.6/lib/python2.6": "{Python}",
            "/System/Library/Frameworks/Python.framework/Versions/2.5/lib/python2.5": "{Python}",
            "/System/Library/Frameworks/Python.framework/Versions/2.7/Extras/lib/python": "{Extras}",
            "/System/Library/Frameworks/Python.framework/Versions/2.6/Extras/lib/python": "{Extras}",
            "/System/Library/Frameworks/Python.framework/Versions/2.5/Extras/lib/python": "{Extras}",
        }
        contains_maps = {
            "/CalendarServer": "{caldavd}",
            "/Twisted": "{Twisted}",
            "/pycalendar": "{pycalendar}",
        }

        def __init__(self, line, lineno):

            self.entering = True
            self.function_name = ""
            self.file_location = ""
            self.parent = None
            self.children = []
            self.lineno = lineno

            re_matched = re.match("(..) ([^ ]+) \(([^\)]+)\)", line)
            if re_matched is None:
                print(line)
            results = re_matched.groups()
            if results[0] == "<-":
                self.entering = False
            elif results[0] == "->":
                self.entering = True
            else:
                raise ValueError("Invalid start of line at %d" % (lineno,))

            self.function_name = results[1]
            self.file_location = results[2]
            for key, value in Dtrace.DtraceLine.prefix_maps.iteritems():
                if self.file_location.startswith(key):
                    self.file_location = value + self.file_location[len(key):]
                    break
            else:
                for key, value in Dtrace.DtraceLine.contains_maps.iteritems():
                    found1 = self.file_location.find(key)
                    if found1 != -1:
                        found2 = self.file_location[found1 + 1:].find('/')
                        if found2 != -1:
                            self.file_location = value + self.file_location[found1 + found2 + 1:]
                        else:
                            self.file_location = value
                        break

        def __repr__(self):
            return "%s (%s)" % self.getKey()

        def getKey(self):
            return (self.file_location, self.function_name,)

        def getPartialKey(self):
            return (self.filePath(), self.function_name,)

        def addChild(self, child):
            child.parent = self
            self.children.append(child)

        def checkForCollapse(self, other):
            if self.entering and not other.entering:
                if self.function_name == other.function_name and self.function_name != "mainLoop":
                    if self.filePath() == other.filePath():
                        return True
            return False

        def filePath(self):
            return self.file_location[0:self.file_location.rfind(':')]

        def prettyPrint(self, indent, indents, sout):

            indenter = ""
            for level in indents:
                if level > 0:
                    indenter += "⎢ "
                elif level < 0:
                    indenter += "⎿ "
                else:
                    indenter += "  "
            sout.write("%s%s (%s)\n" % (indenter, self.function_name, self.file_location,))

        def stackName(self):
            return self.function_name, self.filePath()

    class DtraceStack(object):

        def __init__(self, lines, no_collapse):
            self.start_indent = 0
            self.stack = []
            self.called_by = {}
            self.call_into = {}

            self.processLines(lines, no_collapse)

        def processLines(self, lines, no_collapse):

            new_lines = []
            last_line = None
            for line in lines:
                if last_line:
                    if not no_collapse and line.checkForCollapse(last_line):
                        new_lines.pop()
                        last_line = None
                        continue
                new_lines.append(line)
                last_line = line

            indent = 0
            min_indent = 0
            current_line = None
            blocks = [[]]
            backstack = []
            for line in new_lines:
                stackName = line.stackName()
                if line.entering:
                    if line.function_name == "mainLoop":
                        if min_indent < 0:
                            newstack = []
                            for oldindent, oldline in blocks[-1]:
                                newstack.append((oldindent - min_indent, oldline,))
                            blocks[-1] = newstack
                        min_indent = 0
                        indent = 0
                        blocks.append([])
                        backstack = []
                    else:
                        indent += 1
                        backstack.append(stackName)
                    blocks[-1].append((indent, line,))
                    if current_line:
                        current_line.addChild(line)
                    current_line = line
                else:
                    if len(blocks) == 1 or line.function_name != "mainLoop" and indent:
                        indent -= 1
                        while backstack and indent and stackName != backstack[-1]:
                            indent -= 1
                            backstack.pop()
                        if backstack:
                            backstack.pop()
                        if indent < 0:
                            print("help")
                    current_line = current_line.parent if current_line else None
                min_indent = min(min_indent, indent)

            for block in blocks:
                self.stack.extend(block)
            if min_indent < 0:
                self.start_indent = -min_indent
            else:
                self.start_indent = 0

            self.generateCallInfo()

        def generateCallInfo(self):

            for _ignore, line in self.stack:
                key = line.getKey()

                if line.parent:
                    parent_key = line.parent.getKey()
                    parent_calls = self.called_by.setdefault(key, {}).get(parent_key, 0)
                    self.called_by[key][parent_key] = parent_calls + 1

                for child in line.children:
                    child_key = child.getKey()
                    child_calls = self.call_into.setdefault(key, {}).get(child_key, 0)
                    self.call_into[key][child_key] = child_calls + 1

        def prettyPrint(self, sout):
            indents = [1] * self.start_indent
            ctr = 0
            maxctr = len(self.stack) - 1
            for indent, line in self.stack:
                current_indent = self.start_indent + indent
                next_indent = (self.start_indent + self.stack[ctr + 1][0]) if ctr < maxctr else 10000
                if len(indents) == current_indent:
                    pass
                elif len(indents) < current_indent:
                    indents.append(current_indent)
                else:
                    indents = indents[0:current_indent]
                if next_indent < current_indent:
                    indents = indents[0:next_indent] + [-1] * (current_indent - next_indent)
                line.prettyPrint(self.start_indent + indent, indents, sout)
                ctr += 1

    def __init__(self, filepath):

        self.filepath = filepath
        self.calltimes = collections.defaultdict(lambda: [0, 0, 0])
        self.exclusiveTotal = 0

    def analyze(self, do_stack, no_collapse):

        print("Parsing dtrace output.")

        # Parse the trace lines first and look for the start of the call times
        lines = []
        traces = True
        index = -1
        with file(filepath) as f:
            for lineno, line in enumerate(f):
                if traces:
                    if line.strip() and line[0:3] in ("-> ", "<- "):
                        lines.append(Dtrace.DtraceLine(line, lineno + 1))
                    elif line.startswith("Count,"):
                        traces = False
                else:
                    if line[0] != ' ':
                        continue
                    line = line.strip()
                    if line.startswith("FILE"):
                        index += 1
                    if index >= 0:
                        self.parseCallTimeLine(line, index)

        self.printTraceDetails(lines, do_stack, no_collapse)

        for ctr, title in enumerate(("Sorted by Count", "Sorted by Exclusive", "Sorted by Inclusive",)):
            print(title)
            self.printCallTimeTotals(ctr)

    def printTraceDetails(self, lines, do_stack, no_collapse):

        print("Found %d lines" % (len(lines),))
        print("============================")
        print("")

        self.stack = Dtrace.DtraceStack(lines, no_collapse)
        if do_stack:
            with file("stacked.txt", "w") as f:
                self.stack.prettyPrint(f)
            print("Wrote stack calls to 'stacked.txt'")
            print("============================")
            print("")

        # Get stats for each call
        stats = {}
        last_exit = None
        for line in lines:
            key = line.getKey()
            if line.entering:
                counts = stats.get(key, (0, 0))
                counts = (counts[0] + (1 if no_collapse else 0), counts[1] + (0 if no_collapse else 1))
                if line.getPartialKey() != last_exit:
                    counts = (counts[0] + (0 if no_collapse else 1), counts[1] + (1 if no_collapse else 0))
                stats[key] = counts
            else:
                last_exit = line.getPartialKey()

        print("Function Call Counts")
        print("")
        table = tables.Table()
        table.addHeader(("Count", "Function", "File",))
        for key, value in sorted(stats.iteritems(), key=lambda x: x[1][0], reverse=True):
            table.addRow(("%d (%d)" % value, key[1], key[0],))
        table.printTable()

        print("")
        print("Called By Counts")
        print("")
        table = tables.Table()
        table.addHeader(("Function", "Caller", "Count",))
        for main_key in sorted(self.stack.called_by.keys(), key=lambda x: x[1] + x[0]):
            first = True
            for key, value in sorted(self.stack.called_by[main_key].iteritems(), key=lambda x: x[1], reverse=True):
                table.addRow((
                    ("%s (%s)" % (main_key[1], main_key[0],)) if first else "",
                    "%s (%s)" % (key[1], key[0],),
                    str(value),
                ))
                first = False
        table.printTable()

        print("")
        print("Call Into Counts")
        print("")
        table = tables.Table()
        table.addHeader(("Function", "Calls", "Count",))
        for main_key in sorted(self.stack.call_into.keys(), key=lambda x: x[1] + x[0]):
            first = True
            for key, value in sorted(self.stack.call_into[main_key].iteritems(), key=lambda x: x[1], reverse=True):
                table.addRow((
                    ("%s (%s)" % (main_key[1], main_key[0],)) if first else "",
                    "%s (%s)" % (key[1], key[0],),
                    str(value),
                ))
                first = False
        table.printTable()
        print("")

    def parseCallTimeLine(self, line, index):

        file, type, name, value = line.split()
        if file in ("-", "FILE"):
            return
        else:
            self.calltimes[(file, name)][index] = int(value)
            if index == 1:
                self.exclusiveTotal += int(value)

    def printCallTimeTotals(self, sortIndex):

        table = tables.Table()

        table.setDefaultColumnFormats((
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%s", tables.Table.ColumnFormat.LEFT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
            tables.Table.ColumnFormat("%d", tables.Table.ColumnFormat.RIGHT_JUSTIFY),
        ))

        table.addHeader(("File", "Name", "Count", "Inclusive", "Exclusive", "Children",))
        for key, value in sorted(self.calltimes.items(), key=lambda x: x[1][sortIndex], reverse=True):
            table.addRow((
                key[0],
                key[1],
                value[0],
                value[2],
                "%s (%6.3f%%)" % (value[1], (100.0 * value[1]) / self.exclusiveTotal),
                value[2] - value[1],
            ))
        table.addRow()
        table.addRow((
            "Total:",
            "",
            "",
            "",
            self.exclusiveTotal,
            "",
        ))

        table.printTable()
        print("")


def usage(error_msg=None):
    if error_msg:
        print(error_msg)

    print("""Usage: dtraceanalyze [options] FILE
Options:
    -h          Print this help and exit
    --stack     Save indented stack to file
    --raw-count Display call counts based on full trace,
                else display counts on collapsed values.

Arguments:
    FILE      File name containing dtrace output to analyze

Description:
    This utility will analyze the output of the trace.d dtrace script to produce
    useful statistics, and other performance related data.

    To use this do the following (where PID is the pid of the
    Python process to monitor:

    > sudo ./trace.d PID > results.txt
    ...
    > ./dtraceanalyze.py results.txt
""")

    if error_msg:
        raise ValueError(error_msg)
    else:
        sys.exit(0)

if __name__ == "__main__":

    sys.setrecursionlimit(10000)
    do_stack = False
    no_collapse = False
    try:
        options, args = getopt.getopt(sys.argv[1:], "h", ["stack", "no-collapse"])

        for option, value in options:
            if option == "-h":
                usage()
            elif option == "--stack":
                do_stack = True
            elif option == "--no-collapse":
                no_collapse = True
            else:
                usage("Unrecognized option: %s" % (option,))

        if len(args) == 0:
            fname = "results.txt"
        elif len(args) != 1:
            usage("Must have one argument")
        else:
            fname = args[0]

        filepath = os.path.expanduser(fname)
        if not os.path.exists(filepath):
            usage("File '%s' does not exist" % (filepath,))

        print("CalendarServer dtrace analysis tool tool")
        print("=====================================")
        print("")
        if do_stack:
            print("Generating nested stack call file.")
        if no_collapse:
            print("Consecutive function calls will not be removed.")
        else:
            print("Consecutive function calls will be removed.")
        print("============================")
        print("")

        Dtrace(filepath).analyze(do_stack, no_collapse)

    except Exception as e:
        raise
        sys.exit(str(e))
