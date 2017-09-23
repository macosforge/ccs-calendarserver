##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
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
Interactive shell for terminals.
"""

__all__ = [
    "usage",
    "ShellOptions",
    "ShellService",
    "ShellProtocol",
    "main",
]


import string
import os
import sys
import tty
import termios
from shlex import shlex

from twisted.python.failure import Failure
from twisted.python.text import wordWrap
from twisted.python.usage import Options, UsageError
from twisted.internet.defer import Deferred, succeed
from twisted.internet.defer import inlineCallbacks
from twisted.internet.stdio import StandardIO
from twisted.conch.recvline import HistoricRecvLine as ReceiveLineProtocol
from twisted.conch.insults.insults import ServerProtocol

from twext.python.log import Logger

from txdav.common.icommondatastore import NotFoundError

from twistedcaldav.stdconfig import DEFAULT_CONFIG_FILE

from calendarserver.tools.cmdline import utilityMain, WorkerService
from calendarserver.tools.shell.cmd import Commands, UsageError as CommandUsageError

log = Logger()


def usage(e=None):
    if e:
        print(e)
        print("")
    try:
        ShellOptions().opt_help()
    except SystemExit:
        pass
    if e:
        sys.exit(64)
    else:
        sys.exit(0)


class ShellOptions(Options):
    """
    Command line options for "calendarserver_shell".
    """
    synopsis = "\n".join(
        wordWrap(
            """
            Usage: calendarserver_shell [options]\n
            """ + __doc__,
            int(os.environ.get("COLUMNS", "80"))
        )
    )

    optParameters = [
        ["config", "f", DEFAULT_CONFIG_FILE, "Specify caldavd.plist configuration path."],
    ]

    def __init__(self):
        super(ShellOptions, self).__init__()


class ShellService(WorkerService, object):
    """
    A L{ShellService} collects all the information that a shell needs to run;
    when run, it invokes the shell on stdin/stdout.

    @ivar store: the calendar / addressbook store.
    @type store: L{txdav.idav.IDataStore}

    @ivar directory: the directory service, to look up principals' names
    @type directory: L{twistedcaldav.directory.idirectory.IDirectoryService}

    @ivar options: the command-line options used to create this shell service
    @type options: L{ShellOptions}

    @ivar reactor: the reactor under which this service is running
    @type reactor: L{IReactorTCP}, L{IReactorTime}, L{IReactorThreads} etc

    @ivar config: the configuration associated with this shell service.
    @type config: L{twistedcaldav.config.Config}
    """

    def __init__(self, store, options, reactor, config):
        super(ShellService, self).__init__(store)
        self.directory = store.directoryService()
        self.options = options
        self.reactor = reactor
        self.config = config
        self.terminalFD = None
        self.protocol = None

    def doWork(self):
        """
        Service startup.
        """

        # Set up the terminal for interactive action
        self.terminalFD = sys.__stdin__.fileno()
        self._oldTerminalSettings = termios.tcgetattr(self.terminalFD)
        tty.setraw(self.terminalFD)

        self.protocol = ServerProtocol(lambda: ShellProtocol(self))
        StandardIO(self.protocol)
        return succeed(None)

    def postStartService(self):
        """
        Don't quit right away
        """
        pass

    def stopService(self):
        """
        Stop the service.
        """
        # Restore terminal settings
        termios.tcsetattr(self.terminalFD, termios.TCSANOW, self._oldTerminalSettings)
        os.write(self.terminalFD, "\r\x1bc\r")


class ShellProtocol(ReceiveLineProtocol):
    """
    Data store shell protocol.

    @ivar service: a service representing the running shell
    @type service: L{ShellService}
    """

    # FIXME:
    # * Received lines are being echoed; find out why and stop it.
    # * Backspace transposes characters in the terminal.

    ps = ("ds% ", "... ")

    emulation_modes = ("emacs", "none")

    def __init__(self, service, commandsClass=Commands):
        ReceiveLineProtocol.__init__(self)
        self.service = service
        self.inputLines = []
        self.commands = commandsClass(self)
        self.activeCommand = None
        self.emulate = "emacs"

    def reloadCommands(self):
        # FIXME: doesn't work for alternative Commands classes passed
        # to __init__.
        self.terminal.write("Reloading commands class...\n")

        import calendarserver.tools.shell.cmd
        reload(calendarserver.tools.shell.cmd)
        self.commands = calendarserver.tools.shell.cmd.Commands(self)

    #
    # Input handling
    #

    def connectionMade(self):
        ReceiveLineProtocol.connectionMade(self)

        self.keyHandlers['\x03'] = self.handle_INT   # Control-C
        self.keyHandlers['\x04'] = self.handle_EOF   # Control-D
        self.keyHandlers['\x1c'] = self.handle_QUIT  # Control-\
        self.keyHandlers['\x0c'] = self.handle_FF    # Control-L
        # self.keyHandlers['\t'  ] = self.handle_TAB   # Tab

        if self.emulate == "emacs":
            # EMACS key bindinds
            self.keyHandlers['\x10'] = self.handle_UP     # Control-P
            self.keyHandlers['\x0e'] = self.handle_DOWN   # Control-N
            self.keyHandlers['\x02'] = self.handle_LEFT   # Control-B
            self.keyHandlers['\x06'] = self.handle_RIGHT  # Control-F
            self.keyHandlers['\x01'] = self.handle_HOME   # Control-A
            self.keyHandlers['\x05'] = self.handle_END    # Control-E

        def observer(event):
            if not event["isError"]:
                return

            text = log.textFromEventDict(event)
            if text is None:
                return

            self.service.reactor.callFromThread(self.terminal.write, text)

        log.startLoggingWithObserver(observer)

    def handle_INT(self):
        return self.resetInputLine()

    def handle_EOF(self):
        if self.lineBuffer:
            if self.emulate == "emacs":
                self.handle_DELETE()
            else:
                self.terminal.write("\a")
        else:
            self.handle_QUIT()

    def handle_FF(self):
        """
        Handle a "form feed" byte - generally used to request a screen
        refresh/redraw.
        """
        # FIXME: Clear screen != redraw screen.
        return self.clearScreen()

    def handle_QUIT(self):
        return self.exit()

    def handle_TAB(self):
        return self.completeLine()

    #
    # Utilities
    #

    def clearScreen(self):
        """
        Clear the display.
        """
        self.terminal.eraseDisplay()
        self.terminal.cursorHome()
        self.drawInputLine()

    def resetInputLine(self):
        """
        Reset the current input variables to their initial state.
        """
        self.pn = 0
        self.lineBuffer = []
        self.lineBufferIndex = 0
        self.terminal.nextLine()
        self.drawInputLine()

    @inlineCallbacks
    def completeLine(self):
        """
        Perform auto-completion on the input line.
        """
        # Tokenize the text before the cursor
        tokens = self.tokenize("".join(self.lineBuffer[:self.lineBufferIndex]))

        if tokens:
            if len(tokens) == 1 and self.lineBuffer[-1] in string.whitespace:
                word = ""
            else:
                word = tokens[-1]
            cmd = tokens.pop(0)
        else:
            word = cmd = ""

        if cmd and (tokens or word == ""):
            # Completing arguments

            m = getattr(self.commands, "complete_%s" % (cmd,), None)
            if not m:
                return
            try:
                completions = tuple((yield m(tokens)))
            except Exception as e:
                self.handleFailure(Failure(e))
                return
            log.info("COMPLETIONS: {comp}", comp=completions)
        else:
            # Completing command name
            completions = tuple(self.commands.complete_commands(cmd))

        if len(completions) == 1:
            for c in next(completions.__iter__()):
                self.characterReceived(c, True)

            # FIXME: Add a space only if we know we've fully completed the term.
            # self.characterReceived(" ", False)
        else:
            self.terminal.nextLine()
            for completion in completions:
                # FIXME Emitting these in columns would be swell
                self.terminal.write("%s%s\n" % (word, completion))
            self.drawInputLine()

    def exit(self):
        """
        Exit.
        """
        self.terminal.loseConnection()
        self.service.reactor.stop()

    def handleFailure(self, f):
        """
        Handle a failure raises in the interpreter by printing a
        traceback and resetting the input line.
        """
        if self.lineBuffer:
            self.terminal.nextLine()
        self.terminal.write("Error: %s !!!" % (f.value,))
        if not f.check(NotImplementedError, NotFoundError):
            log.info(f.getTraceback())
        self.resetInputLine()

    #
    # Command dispatch
    #

    def lineReceived(self, line):
        if self.activeCommand is not None:
            self.inputLines.append(line)
            return

        tokens = self.tokenize(line)

        if tokens:
            cmd = tokens.pop(0)
            # print("Arguments: %r" % (tokens,))

            m = getattr(self.commands, "cmd_%s" % (cmd,), None)
            if m:
                def handleUsageError(f):
                    f.trap(CommandUsageError)
                    self.terminal.write("%s\n" % (f.value,))
                    doc = self.commands.documentationForCommand(cmd)
                    if doc:
                        self.terminal.nextLine()
                        self.terminal.write(doc)
                        self.terminal.nextLine()

                def next(_):
                    self.activeCommand = None
                    if self.inputLines:
                        line = self.inputLines.pop(0)
                        self.lineReceived(line)

                d = self.activeCommand = Deferred()
                d.addCallback(lambda _: m(tokens))
                if True:
                    d.callback(None)
                else:
                    # Add time to test callbacks
                    self.service.reactor.callLater(4, d.callback, None)
                d.addErrback(handleUsageError)
                d.addCallback(lambda _: self.drawInputLine())
                d.addErrback(self.handleFailure)
                d.addCallback(next)
            else:
                self.terminal.write("Unknown command: %s\n" % (cmd,))
                self.drawInputLine()
        else:
            self.drawInputLine()

    @staticmethod
    def tokenize(line):
        """
        Tokenize input line.
        @return: an iterable of tokens
        """
        lexer = shlex(line)
        lexer.whitespace_split = True

        tokens = []
        while True:
            token = lexer.get_token()
            if not token:
                break
            tokens.append(token)

        return tokens


def main(argv=sys.argv, stderr=sys.stderr, reactor=None):
    if reactor is None:
        from twisted.internet import reactor

    options = ShellOptions()
    try:
        options.parseOptions(argv[1:])
    except UsageError as e:
        usage(e)

    def makeService(store):
        from twistedcaldav.config import config
        return ShellService(store, options, reactor, config)

    print("Initializing shell...")

    utilityMain(options["config"], makeService, reactor)
