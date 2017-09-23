#!/usr/bin/env python
##
# Copyright (c) 2012-2017 Apple Inc. All rights reserved.
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

"""
This tool takes data from stdin and validates it as iCalendar data suitable
for the server.
"""

from calendarserver.tools.cmdline import utilityMain, WorkerService
from twisted.internet.defer import succeed
from twisted.python.text import wordWrap
from twisted.python.usage import Options
from twistedcaldav.config import config
from twistedcaldav.ical import Component, InvalidICalendarDataError
from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE
import os
import sys


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        ValidOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


description = '\n'.join(
    wordWrap(
        """
        Usage: validcalendardata [options] [input specifiers]\n
        """,
        int(os.environ.get('COLUMNS', '80'))
    )
)


class ValidOptions(Options):
    """
    Command-line options for 'validcalendardata'
    """

    synopsis = description

    optFlags = [
        ['verbose', 'v', "Verbose logging."],
        ['debug', 'D', "Debug logging."],
        ['parse-only', 'p', "Only validate parsing of the data."],
    ]

    optParameters = [
        ['config', 'f', DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
    ]

    def __init__(self):
        super(ValidOptions, self).__init__()
        self.outputName = '-'
        self.inputName = '-'

    def opt_output(self, filename):
        """
        Specify output file path (default: '-', meaning stdout).
        """
        self.outputName = filename

    opt_o = opt_output

    def openOutput(self):
        """
        Open the appropriate output file based on the '--output' option.
        """
        if self.outputName == '-':
            return sys.stdout
        else:
            return open(self.outputName, "wb")

    def opt_input(self, filename):
        """
        Specify output file path (default: '-', meaning stdin).
        """
        self.inputName = filename

    opt_i = opt_input

    def openInput(self):
        """
        Open the appropriate output file based on the '--input' option.
        """
        if self.inputName == '-':
            return sys.stdin
        else:
            return open(os.path.expanduser(self.inputName), "rb")


errorPrefix = "Calendar data had unfixable problems:\n  "


class ValidService(WorkerService, object):
    """
    Service which runs, exports the appropriate records, then stops the reactor.
    """

    def __init__(self, store, options, output, input, reactor, config):
        super(ValidService, self).__init__(store)
        self.options = options
        self.output = output
        self.input = input
        self.reactor = reactor
        self.config = config
        self._directory = None

    def doWork(self):
        """
        Start the service.
        """
        super(ValidService, self).startService()
        if self.options["parse-only"]:
            result, message = self.parseCalendarData()
        else:
            result, message = self.validCalendarData()

        if result:
            print("Calendar data OK")
        else:
            print(message)
        return succeed(None)

    def parseCalendarData(self):
        """
        Check the calendar data for valid iCalendar data.
        """

        result = True
        message = ""
        try:
            component = Component.fromString(self.input.read())

            # Do underlying iCalendar library validation with data fix
            fixed, unfixed = component._pycalendar.validate(doFix=True)

            if unfixed:
                raise InvalidICalendarDataError("Calendar data had unfixable problems:\n  %s" % ("\n  ".join(unfixed),))
            if fixed:
                print("Calendar data had fixable problems:\n  %s" % ("\n  ".join(fixed),))

        except ValueError as e:
            result = False
            message = str(e)
            if message.startswith(errorPrefix):
                message = message[len(errorPrefix):]

        return (result, message,)

    def validCalendarData(self):
        """
        Check the calendar data for valid iCalendar data.
        """

        result = True
        message = ""
        truncated = False
        try:
            component = Component.fromString(self.input.read())
            if getattr(self.config, "MaxInstancesForRRULE", 0) != 0:
                truncated = component.truncateRecurrence(config.MaxInstancesForRRULE)
            component.validCalendarData(doFix=False, validateRecurrences=True)
            component.validCalendarForCalDAV(methodAllowed=True)
            component.validOrganizerForScheduling(doFix=False)
        except ValueError as e:
            result = False
            message = str(e)
            if message.startswith(errorPrefix):
                message = message[len(errorPrefix):]
            if truncated:
                message = "Calendar data RRULE truncated\n" + message

        return (result, message,)


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    """
    Do the export.
    """
    if reactor is None:
        from twisted.internet import reactor
    options = ValidOptions()
    options.parseOptions(argv[1:])
    try:
        output = options.openOutput()
    except IOError as e:
        stderr.write("Unable to open output file for writing: %s\n" % (e))
        sys.exit(1)
    try:
        input = options.openInput()
    except IOError as e:
        stderr.write("Unable to open input file for reading: %s\n" % (e))
        sys.exit(1)

    def makeService(store):
        return ValidService(store, options, output, input, reactor, config)

    utilityMain(options["config"], makeService, reactor, verbose=options["debug"])


if __name__ == "__main__":
    main()
