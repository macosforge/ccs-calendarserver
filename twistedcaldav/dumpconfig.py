from __future__ import print_function
##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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

from cStringIO import StringIO
from collections import OrderedDict
from json.encoder import encode_basestring
from plistlib import PlistWriter, _escapeAndEncode
import json
import os
import re
import textwrap

DEBUG = False

COPYRIGHT = """
<!--
    Copyright (c) 2006-2017 Apple Inc. All rights reserved.

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
  -->
"""


def parseConfigItem(item):
    """
    Read the definition of a "DEFAULT_*" value from the stdconfig.py file so that we get
    the literal Python source as a L{str} that we then then process into JSON.

    @param item: the "DEFAULT_*" item to read
    @type item: L{str}

    @return: the "DEFAULT_*" value
    @rtype: L{str}
    """
    with open(os.path.join(os.path.dirname(__file__), "stdconfig.py")) as f:
        # Read up to the first line containing DEFAULT_*
        while f.readline() != "{} = {{\n".format(item):
            continue

        # Build list of all lines up to the end of the DEFAULT_* definition and
        # make it look like a JSON object
        lines = ['{']
        line = f.readline()
        while line != "}\n":
            lines.append(line[:-1])
            line = f.readline()
        lines.append('}')

    return "\n".join(lines)


def processConfig(configlines, with_comments=False, verbose=False, substitutions=None):
    """
    Process the "raw" config lines from stdconfig.py into a JSON object
    (a Python L{dict}) that is ordered and contains commentary based on
    the Python comments.

    @param configlines: config data lines
    @type configlines: L{list} of L{str}
    @param with_comments: whether to include comments or not
    @type with_comments: L{bool}
    @param verbose: print out intermediate state
    @type verbose: L{bool}

    @return: the serialized JSON object
    @rtype: L{OrderedDict}
    """

    # Comments will either be "block" (as in section dividers) or "inline"
    # (as in appended to the end of the line). We treat these slightly
    # differently wrt to whitespace and where they appear.
    lines = []
    ctr = 0
    block_comment = []
    inline_comment = []

    # Regular expression to match an inline comment and a
    # value containing a numeric expression that needs to be
    # evaluated (e.g. "60 * 60")
    comments = re.compile("([ ]*.*?,?)[ ]*#[ ]*(.*)[ ]*$")
    value = re.compile("([^:]+:[ ]+)([0-9 \*]+)(.*)")

    for line in configlines.splitlines():

        if line.strip() and line.strip()[0] == "#":
            # Line with just a comment is a block comment unless the
            # previous comment was inline (in which case it is a multi-line
            # inline). Aggregate block and inline comments into one overall
            # comment.
            comment = line.strip()[1:].strip()
            if len(comment) == 0 and len(block_comment) == 0 and len(inline_comment) == 0:
                pass
            elif inline_comment:
                inline_comment.append(comment if comment else "\n")
            else:
                block_comment.append(comment if comment else "\n")
            continue
        elif block_comment:
            # Generate a block comment JSON member
            if with_comments:
                comment_type = "comment_" if line.strip() and block_comment[-1] != "\n" else "section_"
                while block_comment[-1] == "\n":
                    block_comment.pop()
                lines.append("\"{}{}\": {},".format(comment_type, ctr, encode_basestring(" ".join(block_comment))))
            ctr += 1
            block_comment = []
        elif inline_comment:
            # Generate an inline comment JSON member
            if with_comments:
                lines.insert(-1, "\"comment_{}\": {},".format(ctr, encode_basestring(" ".join(inline_comment))))
            ctr += 1
            inline_comment = []

        # Check if the current line contains an inline comment, if so extract
        # the comment and add to the current inline comments list
        m = comments.match(line)
        if m:
            inline_comment.append(m.group(2))
            append = m.group(1)
        else:
            append = line

        # Do some simple value conversions
        append = append.rstrip().replace(" None", ' ""').replace(" True", " true").replace(" False", " false").replace("\\", "\\\\")

        # Look for special substitutions
        if substitutions:
            for subskey in substitutions.keys():
                pos = append.find(subskey)
                if pos >= 0:
                    actual = append[pos + len(subskey) + 2:]
                    comma = ""
                    if actual[-1] == ",":
                        actual = actual[:-1]
                        comma = ","
                    actual = actual[:-2]
                    append = "{}{}{}".format(
                        append[:pos],
                        json.dumps(substitutions[subskey][actual]),
                        comma,
                    )
                    break

        # Look for numeric expressions in the value and eval() those to get a value
        # that is compatible with JSON
        m = value.match(append)
        if m:
            expression = eval(m.group(2))
            append = "{}{}{}".format(m.group(1), expression, m.group(3))

        # Remove trailing commas for the last items in an array
        # or object as JSON does not like that
        if append.strip() and append.strip()[0] in ("]", "}"):
            if lines[-1][-1] == ",":
                lines[-1] = lines[-1][:-1]

        # Line is ready to use
        lines.append(append)

    newj = "\n".join(lines)
    if verbose:
        print(newj)

    # Created an ordered JSON object
    j = json.loads(newj, object_pairs_hook=OrderedDict)
    return j


class OrderedPlistWriter(PlistWriter):
    """
    L{PlistWriter} that maintains the order of dict items. It also handles special keys
    "section_" and "comment_" which are used to insert XML comments in the plist output.
    Some additional blank lines are also added for readability of the plist.
    """

    def writeDict(self, d):
        """
        Basically a copy of L{PlistWriter.writeDict} that does not sort the dict keys
        if the dict type is L{OrderedDict}.
        """
        self.beginElement("dict")
        items = d.items()
        if not isinstance(d, OrderedDict):
            items.sort()
        newline = False
        for key, value in items:
            if not isinstance(key, (str, unicode)):
                raise TypeError("keys must be strings")
            if newline:
                self.writeln("")
            if key.startswith("section_"):
                self.writeComment(value)
                newline = True
            elif key.startswith("comment_"):
                self.writeComment(value)
                newline = False
            else:
                self.simpleElement("key", key)
                self.writeValue(value)
                newline = True
        self.endElement("dict")

    def writeComment(self, comment):

        indent = self.indentLevel * self.indent

        paragraphs = comment.splitlines()
        for ctr, paragraph in enumerate(comment.splitlines()):
            line = _escapeAndEncode(paragraph).strip()
            initial_indent = ("{}<!-- " if ctr == 0 else "{}     ").format(indent)
            subsequent_indent = "{}     ".format(indent)

            line = textwrap.fill(
                line,
                width=80,
                initial_indent=initial_indent,
                subsequent_indent=subsequent_indent,
            )

            if ctr == len(paragraphs) - 1:
                self.file.write("{} -->\n".format(line))
            else:
                self.file.write("{}\n\n".format(line))


def writeOrderedPlist(rootObject, pathOrFile):
    """
    A copy of L{plistlib.writePlist} that uses an L{OrderedPlistWriter} to
    write the plist.
    """

    """Write 'rootObject' to a .plist file. 'pathOrFile' may either be a
    file name or a (writable) file object.
    """
    didOpen = 0
    if isinstance(pathOrFile, (str, unicode)):
        pathOrFile = open(pathOrFile, "w")
        didOpen = 1
    writer = OrderedPlistWriter(pathOrFile)
    writer.writeln(COPYRIGHT)
    writer.writeln("<plist version=\"1.0\">")
    writer.writeValue(rootObject)
    writer.writeln("</plist>")
    if didOpen:
        pathOrFile.close()


def writeOrderedPlistToString(rootObject):
    """
    A copy of L{plistlib.writePlistToString} that uses an L{writeOrderedPlist} to
    write the plist.
    """

    """Return 'rootObject' as a plist-formatted string.
    """
    f = StringIO()
    writeOrderedPlist(rootObject, f)
    return f.getvalue()


def writeStdConfig(data):
    """
    Write the actual plist data to conf/caldavd-stdconfig.plist.

    @param data: plist data
    @type data: L{str}
    """

    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "conf", "caldavd-stdconfig.plist"), "w") as f:
        f.write(data)


def dumpConfig():
    """
    Dump the full stdconfig to a string.
    """

    # Generate a set of serialized JSON objects for the *_PARAMS config items
    maps = {
        "DEFAULT_SERVICE_PARAMS": "",
        "DEFAULT_RESOURCE_PARAMS": "",
        "DEFAULT_AUGMENT_PARAMS": "",
        "DEFAULT_DIRECTORY_ADDRESSBOOK_PARAMS": "",
    }

    for item in maps.keys():
        if DEBUG:
            print(item)
        lines = parseConfigItem(item)
        maps[item] = processConfig(lines, with_comments=True, verbose=DEBUG)

    # Generate the plist for the default config, substituting for the *_PARAMS items
    lines = parseConfigItem("DEFAULT_CONFIG")
    j = processConfig(lines, with_comments=True, verbose=DEBUG, substitutions=maps)
    return writeOrderedPlistToString(j)


if __name__ == '__main__':

    data = dumpConfig()
    writeStdConfig(data)
    print(data)
