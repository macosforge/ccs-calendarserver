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

from sys import stdout
import types


class Table(object):
    """
    Class that allows pretty printing ascii tables.

    The table supports multiline headers and footers, independent
    column formatting by row, alternative tab-delimited output.
    """

    class ColumnFormat(object):
        """
        Defines the format string, justification and span for a column.
        """

        LEFT_JUSTIFY = 0
        RIGHT_JUSTIFY = 1
        CENTER_JUSTIFY = 2

        def __init__(self, strFormat="%s", justify=LEFT_JUSTIFY, span=1):

            self.format = strFormat
            self.justify = justify
            self.span = span

    def __init__(self, table=None):

        self.headers = []
        self.headerColumnFormats = []
        self.rows = []
        self.footers = []
        self.footerColumnFormats = []
        self.columnCount = 0
        self.defaultColumnFormats = []
        self.columnFormatsByRow = {}

        if table:
            self.setData(table)

    def setData(self, table):

        self.hasTitles = True
        self.headers.append(table[0])
        self.rows = table[1:]
        self._getMaxColumnCount()

    def setDefaultColumnFormats(self, columnFormats):

        self.defaultColumnFormats = columnFormats

    def addDefaultColumnFormat(self, columnFormat):

        self.defaultColumnFormats.append(columnFormat)

    def setHeaders(self, rows, columnFormats=None):

        self.headers = rows
        self.headerColumnFormats = columnFormats if columnFormats else [None, ] * len(self.headers)
        self._getMaxColumnCount()

    def addHeader(self, row, columnFormats=None):

        self.headers.append(row)
        self.headerColumnFormats.append(columnFormats)
        self._getMaxColumnCount()

    def addHeaderDivider(self, skipColumns=()):

        self.headers.append((None, skipColumns,))
        self.headerColumnFormats.append(None)

    def setFooters(self, row, columnFormats=None):

        self.footers = row
        self.footerColumnFormats = columnFormats if columnFormats else [None, ] * len(self.footers)
        self._getMaxColumnCount()

    def addFooter(self, row, columnFormats=None):

        self.footers.append(row)
        self.footerColumnFormats.append(columnFormats)
        self._getMaxColumnCount()

    def addRow(self, row=None, columnFormats=None):

        self.rows.append(row)
        if columnFormats:
            self.columnFormatsByRow[len(self.rows) - 1] = columnFormats
        self._getMaxColumnCount()

    def addDivider(self, skipColumns=()):

        self.rows.append((None, skipColumns,))

    def printTable(self, os=stdout):

        maxWidths = self._getMaxWidths()

        self.printDivider(os, maxWidths, False)
        if self.headers:
            for header, format in zip(self.headers, self.headerColumnFormats):
                self.printRow(os, header, self._getHeaderColumnFormat(format), maxWidths)
            self.printDivider(os, maxWidths)
        for ctr, row in enumerate(self.rows):
            self.printRow(os, row, self._getColumnFormatForRow(ctr), maxWidths)
        if self.footers:
            self.printDivider(os, maxWidths, double=True)
            for footer, format in zip(self.footers, self.footerColumnFormats):
                self.printRow(os, footer, self._getFooterColumnFormat(format), maxWidths)
        self.printDivider(os, maxWidths, False)

    def printRow(self, os, row, format, maxWidths):

        if row is None or type(row) is tuple and row[0] is None:
            self.printDivider(os, maxWidths, skipColumns=row[1] if type(row) is tuple else ())
        else:
            if len(row) != len(maxWidths):
                row = list(row)
                row.extend([""] * (len(maxWidths) - len(row)))

            t = "|"
            ctr = 0
            while ctr < len(row):
                startCtr = ctr
                maxWidth = 0
                for _ignore_span in xrange(format[startCtr].span if format else 1):
                    maxWidth += maxWidths[ctr]
                    ctr += 1
                maxWidth += 3 * ((format[startCtr].span - 1) if format else 0)
                text = self._columnText(row, startCtr, format, width=maxWidth)
                t += " " + text + " |"
            t += "\n"
            os.write(t)

    def printDivider(self, os, maxWidths, intermediate=True, double=False, skipColumns=()):
        t = "|" if intermediate else "+"
        for widthctr, width in enumerate(maxWidths):
            if widthctr in skipColumns:
                c = " "
            else:
                c = "=" if double else "-"
            t += c * (width + 2)
            t += "+" if widthctr < len(maxWidths) - 1 else ("|" if intermediate else "+")
        t += "\n"
        os.write(t)

    def printTabDelimitedData(self, os=stdout, footer=True):

        if self.headers:
            titles = [""] * len(self.headers[0])
            for row, header in enumerate(self.headers):
                for col, item in enumerate(header):
                    titles[col] += (" " if row and item else "") + item
            self.printTabDelimitedRow(os, titles, self._getHeaderColumnFormat(self.headerColumnFormats[0]))
        for ctr, row in enumerate(self.rows):
            self.printTabDelimitedRow(os, row, self._getColumnFormatForRow(ctr))
        if self.footers and footer:
            for footer in self.footers:
                self.printTabDelimitedRow(os, footer, self._getFooterColumnFormat(self.footerColumnFormats[0]))

    def printTabDelimitedRow(self, os, row, format):

        if row is None:
            row = [""] * self.columnCount

        if len(row) != self.columnCount:
            row = list(row)
            row.extend([""] * (self.columnCount - len(row)))

        textItems = [self._columnText(row, ctr, format) for ctr in xrange((len(row)))]
        os.write("\t".join(textItems) + "\n")

    def _getMaxColumnCount(self):

        self.columnCount = 0
        if self.headers:
            for header in self.headers:
                self.columnCount = max(self.columnCount, len(header) if header else 0)
        for row in self.rows:
            self.columnCount = max(self.columnCount, len(row) if row else 0)
        if self.footers:
            for footer in self.footers:
                self.columnCount = max(self.columnCount, len(footer) if footer else 0)

    def _getMaxWidths(self):

        maxWidths = [0] * self.columnCount

        if self.headers:
            for header, format in zip(self.headers, self.headerColumnFormats):
                self._updateMaxWidthsFromRow(header, self._getHeaderColumnFormat(format), maxWidths)

        for ctr, row in enumerate(self.rows):
            self._updateMaxWidthsFromRow(row, self._getColumnFormatForRow(ctr), maxWidths)

        if self.footers:
            for footer, format in zip(self.footers, self.footerColumnFormats):
                self._updateMaxWidthsFromRow(footer, self._getFooterColumnFormat(format), maxWidths)

        return maxWidths

    def _updateMaxWidthsFromRow(self, row, format, maxWidths):

        if row and (type(row) is not tuple or row[0] is not None):
            ctr = 0
            while ctr < len(row):

                text = self._columnText(row, ctr, format)
                startCtr = ctr
                for _ignore_span in xrange(format[startCtr].span if format else 1):
                    maxWidths[ctr] = max(maxWidths[ctr], len(text) / (format[startCtr].span if format else 1))
                    ctr += 1

    def _getHeaderColumnFormat(self, format):

        if format:
            return format
        else:
            justify = Table.ColumnFormat.CENTER_JUSTIFY if len(self.headers) == 1 else Table.ColumnFormat.LEFT_JUSTIFY
            return [Table.ColumnFormat(justify=justify)] * self.columnCount

    def _getFooterColumnFormat(self, format):

        if format:
            return format
        else:
            return self.defaultColumnFormats

    def _getColumnFormatForRow(self, ctr):

        if ctr in self.columnFormatsByRow:
            return self.columnFormatsByRow[ctr]
        else:
            return self.defaultColumnFormats

    def _columnText(self, row, column, format, width=0):

        if row is None or column >= len(row):
            return ""

        colData = row[column]
        if colData is None:
            colData = ""

        columnFormat = format[column] if format and column < len(format) else Table.ColumnFormat()
        if type(colData) in (str,):
            text = colData
        else:
            text = columnFormat.format % colData
        if width:
            if columnFormat.justify == Table.ColumnFormat.LEFT_JUSTIFY:
                text = text.ljust(width)
            elif columnFormat.justify == Table.ColumnFormat.RIGHT_JUSTIFY:
                text = text.rjust(width)
            elif columnFormat.justify == Table.ColumnFormat.CENTER_JUSTIFY:
                text = text.center(width)
        return text
