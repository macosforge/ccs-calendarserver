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

"""
Data store commands.
"""

__all__ = [
    "UsageError",
    "UnknownArguments",
    "CommandsBase",
    "Commands",
]

from getopt import getopt

from twext.python.log import Logger
from twisted.internet.defer import succeed
from twisted.internet.defer import inlineCallbacks, returnValue

from twisted.conch.manhole import ManholeInterpreter

from txdav.common.icommondatastore import NotFoundError

from calendarserver.version import version
from calendarserver.tools.tables import Table
from calendarserver.tools.purge import PurgePrincipalService
from calendarserver.tools.shell.vfs import Folder, RootFolder
from calendarserver.tools.shell.directory import findRecords, summarizeRecords, recordInfo

log = Logger()


class UsageError(Exception):
    """
    Usage error.
    """


class UnknownArguments(UsageError):
    """
    Unknown arguments.
    """

    def __init__(self, arguments):
        UsageError.__init__(self, "Unknown arguments: %s" % (arguments,))
        self.arguments = arguments


class InsufficientArguments(UsageError):
    """
    Insufficient arguments.
    """

    def __init__(self):
        UsageError.__init__(self, "Insufficient arguments.")


class CommandsBase(object):
    """
    Base class for commands.

    @ivar protocol: a protocol for parsing the incoming command line.
    @type protocol: L{calendarserver.tools.shell.terminal.ShellProtocol}
    """

    def __init__(self, protocol):
        self.protocol = protocol

        self.wd = RootFolder(protocol.service)

    @property
    def terminal(self):
        return self.protocol.terminal

    #
    # Utilities
    #

    def documentationForCommand(self, command):
        """
        @return: the documentation for the given C{command} as a
        string.
        """
        m = getattr(self, "cmd_%s" % (command,), None)
        if m:
            doc = m.__doc__.split("\n")

            # Throw out first and last line if it's empty
            if doc:
                if not doc[0].strip():
                    doc.pop(0)
                if not doc[-1].strip():
                    doc.pop()

            if doc:
                # Get length of indentation
                i = len(doc[0]) - len(doc[0].lstrip())

                result = []
                for line in doc:
                    result.append(line[i:])

                return "\n".join(result)
            else:
                self.terminal.write("(No documentation available for %s)\n" % (command,))
        else:
            raise NotFoundError("Unknown command: %s" % (command,))

    def getTarget(self, tokens, wdFallback=False):
        """
        Pop's the first token from tokens and locates the File
        indicated by that token.
        @return: a C{File}.
        """
        if tokens:
            return self.wd.locate(tokens.pop(0).split("/"))
        else:
            if wdFallback:
                return succeed(self.wd)
            else:
                return succeed(None)

    @inlineCallbacks
    def getTargets(self, tokens, wdFallback=False):
        """
        For each given C{token}, locate a File to operate on.
        @return: iterable of C{File} objects.
        """
        if tokens:
            result = []
            for token in tokens:
                try:
                    target = (yield self.wd.locate(token.split("/")))
                except NotFoundError:
                    raise UsageError("No such target: %s" % (token,))

                result.append(target)

            returnValue(result)
        else:
            if wdFallback:
                returnValue((self.wd,))
            else:
                returnValue(())

    @inlineCallbacks
    def directoryRecordWithID(self, id):
        """
        Obtains a directory record corresponding to the given C{id}.
        C{id} is assumed to be a record UID.  For convenience, may
        also take the form C{type:name}, where C{type} is a record
        type and C{name} is a record short name.
        @return: an C{IDirectoryRecord}
        """
        directory = self.protocol.service.directory

        record = yield directory.recordWithUID(id)

        if not record:
            # Try type:name form
            try:
                recordType, shortName = id.split(":")
            except ValueError:
                pass
            else:
                record = yield directory.recordWithShortName(recordType, shortName)

        returnValue(record)

    def commands(self, showHidden=False):
        """
        @return: an iterable of C{(name, method)} tuples, where
        C{name} is the name of the command and C{method} is the method
        that implements it.
        """
        for attr in dir(self):
            if attr.startswith("cmd_"):
                m = getattr(self, attr)
                if showHidden or not hasattr(m, "hidden"):
                    yield (attr[4:], m)

    @staticmethod
    def complete(word, items):
        """
        List completions for the given C{word} from the given
        C{items}.

        Completions are the remaining portions of words in C{items}
        that start with C{word}.

        For example, if C{"foobar"} and C{"foo"} are in C{items}, then
        C{""} and C{"bar"} are completions when C{word} C{"foo"}.

        @return: an iterable of completions.
        """
        for item in items:
            if item.startswith(word):
                yield item[len(word):]

    def complete_commands(self, word):
        """
        @return: an iterable of command name completions.
        """
        def complete(showHidden):
            return self.complete(
                word,
                (name for name, method in self.commands(showHidden=showHidden))
            )

        completions = tuple(complete(False))

        # If no completions are found, try hidden commands.
        if not completions:
            completions = complete(True)

        return completions

    @inlineCallbacks
    def complete_files(self, tokens, filter=None):
        """
        @return: an iterable of C{File} path completions.
        """
        if filter is None:
            filter = lambda item: True

        if tokens:
            token = tokens[-1]

            i = token.rfind("/")
            if i == -1:
                # No "/" in token
                base = self.wd
                word = token
            else:
                base = (yield self.wd.locate(token[:i].split("/")))
                word = token[i + 1:]

        else:
            base = self.wd
            word = ""

        files = (
            entry.toString()
            for entry in (yield base.list())
            if filter(entry)
        )

        if len(tokens) == 0:
            returnValue(files)
        else:
            returnValue(self.complete(word, files))


class Commands(CommandsBase):
    """
    Data store commands.
    """

    #
    # Basic CLI tools
    #

    def cmd_exit(self, tokens):
        """
        Exit the shell.

        usage: exit
        """
        if tokens:
            raise UnknownArguments(tokens)

        self.protocol.exit()

    def cmd_help(self, tokens):
        """
        Show help.

        usage: help [command]
        """
        if tokens:
            command = tokens.pop(0)
        else:
            command = None

        if tokens:
            raise UnknownArguments(tokens)

        if command:
            self.terminal.write(self.documentationForCommand(command))
            self.terminal.nextLine()
        else:
            self.terminal.write("Available commands:\n")

            result = []
            max_len = 0

            for name, m in self.commands():
                for line in m.__doc__.split("\n"):
                    line = line.strip()
                    if line:
                        doc = line
                        break
                else:
                    doc = "(no info available)"

                if len(name) > max_len:
                    max_len = len(name)

                result.append((name, doc))

            format = "  %%%ds - %%s\n" % (max_len,)

            for info in sorted(result):
                self.terminal.write(format % (info))

    def complete_help(self, tokens):
        if len(tokens) == 0:
            return (name for name, method in self.commands())
        elif len(tokens) == 1:
            return self.complete_commands(tokens[0])
        else:
            return ()

    def cmd_emulate(self, tokens):
        """
        Emulate editor behavior.
        The only correct argument is: emacs
        Other choices include: none

        usage: emulate editor
        """
        if not tokens:
            if self.protocol.emulate:
                self.terminal.write("Emulating %s.\n" % (self.protocol.emulate,))
            else:
                self.terminal.write("Emulation disabled.\n")
            return

        editor = tokens.pop(0).lower()

        if tokens:
            raise UnknownArguments(tokens)

        if editor == "none":
            self.terminal.write("Disabling emulation.\n")
            editor = None
        elif editor in self.protocol.emulation_modes:
            self.terminal.write("Emulating %s.\n" % (editor,))
        else:
            raise UsageError("Unknown editor: %s" % (editor,))

        self.protocol.emulate = editor

        # FIXME: Need to update key registrations

    cmd_emulate.hidden = "incomplete"

    def complete_emulate(self, tokens):
        if len(tokens) == 0:
            return self.protocol.emulation_modes
        elif len(tokens) == 1:
            return self.complete(tokens[0], self.protocol.emulation_modes)
        else:
            return ()

    def cmd_log(self, tokens):
        """
        Enable logging.

        usage: log [file]
        """
        if hasattr(self, "_logFile"):
            self.terminal.write("Already logging to file: %s\n" % (self._logFile,))
            return

        if tokens:
            fileName = tokens.pop(0)
        else:
            fileName = "/tmp/shell.log"

        if tokens:
            raise UnknownArguments(tokens)

        from twisted.python.log import startLogging
        try:
            f = open(fileName, "w")
        except (IOError, OSError) as e:
            self.terminal.write("Unable to open file %s: %s\n" % (fileName, e))
            return

        startLogging(f)

        self._logFile = fileName

    cmd_log.hidden = "debug tool"

    def cmd_version(self, tokens):
        """
        Print version.

        usage: version
        """
        if tokens:
            raise UnknownArguments(tokens)

        self.terminal.write("%s\n" % (version,))

    #
    # Filesystem tools
    #

    def cmd_pwd(self, tokens):
        """
        Print working folder.

        usage: pwd
        """
        if tokens:
            raise UnknownArguments(tokens)

        self.terminal.write("%s\n" % (self.wd,))

    @inlineCallbacks
    def cmd_cd(self, tokens):
        """
        Change working folder.

        usage: cd [folder]
        """
        if tokens:
            dirname = tokens.pop(0)
        else:
            return

        if tokens:
            raise UnknownArguments(tokens)

        wd = (yield self.wd.locate(dirname.split("/")))

        if not isinstance(wd, Folder):
            raise NotFoundError("Not a folder: %s" % (wd,))

        # log.info("wd -> {wd}", wd=wd)
        self.wd = wd

    @inlineCallbacks
    def complete_cd(self, tokens):
        returnValue((yield self.complete_files(
            tokens,
            filter=lambda item: True  # issubclass(item[0], Folder)
        )))

    @inlineCallbacks
    def cmd_ls(self, tokens):
        """
        List target.

        usage: ls [target ...]
        """
        targets = (yield self.getTargets(tokens, wdFallback=True))
        multiple = len(targets) > 0

        for target in targets:
            entries = sorted((yield target.list()), key=lambda e: e.fileName)
            #
            # FIXME: this can be ugly if, for example, there are zillions
            # of entries to output. Paging would be good.
            #
            table = Table()
            for entry in entries:
                table.addRow(entry.toFields())

            if multiple:
                self.terminal.write("%s:\n" % (target,))
            if table.rows:
                table.printTable(self.terminal)
            self.terminal.nextLine()

    complete_ls = CommandsBase.complete_files

    @inlineCallbacks
    def cmd_info(self, tokens):
        """
        Print information about a target.

        usage: info [target]
        """
        target = (yield self.getTarget(tokens, wdFallback=True))

        if tokens:
            raise UnknownArguments(tokens)

        description = (yield target.describe())
        self.terminal.write(description)
        self.terminal.nextLine()

    complete_info = CommandsBase.complete_files

    @inlineCallbacks
    def cmd_cat(self, tokens):
        """
        Show contents of target.

        usage: cat target [target ...]
        """
        targets = (yield self.getTargets(tokens))

        if not targets:
            raise InsufficientArguments()

        for target in targets:
            if hasattr(target, "text"):
                text = (yield target.text())
                self.terminal.write(text)

    complete_cat = CommandsBase.complete_files

    @inlineCallbacks
    def cmd_rm(self, tokens):
        """
        Remove target.

        usage: rm target [target ...]
        """
        options, tokens = getopt(tokens, "", ["no-implicit"])

        implicit = True

        for option, _ignore_value in options:
            if option == "--no-implicit":
                # Not in docstring; this is really dangerous.
                implicit = False
            else:
                raise AssertionError("We should't be here.")

        targets = (yield self.getTargets(tokens))

        if not targets:
            raise InsufficientArguments()

        for target in targets:
            if hasattr(target, "delete"):
                target.delete(implicit=implicit)
            else:
                self.terminal.write("Can not delete read-only target: %s\n" % (target,))

    cmd_rm.hidden = "Incomplete"

    complete_rm = CommandsBase.complete_files

    #
    # Principal tools
    #

    @inlineCallbacks
    def cmd_find_principals(self, tokens):
        """
        Search for matching principals

        usage: find_principal search_term
        """
        if not tokens:
            raise UsageError("No search term")

        directory = self.protocol.service.directory

        records = (yield findRecords(directory, tokens))

        if records:
            self.terminal.write((yield summarizeRecords(directory, records)))
        else:
            self.terminal.write("No matching principals found.")

        self.terminal.nextLine()

    @inlineCallbacks
    def cmd_print_principal(self, tokens):
        """
        Print information about a principal.

        usage: print_principal principal_id
        """
        if tokens:
            id = tokens.pop(0)
        else:
            raise UsageError("Principal ID required")

        if tokens:
            raise UnknownArguments(tokens)

        directory = self.protocol.service.directory

        record = yield self.directoryRecordWithID(id)

        if record:
            self.terminal.write((yield recordInfo(directory, record)))
        else:
            self.terminal.write("No such principal.")

        self.terminal.nextLine()

    #
    # Data purge tools
    #

    @inlineCallbacks
    def cmd_purge_principals(self, tokens):
        """
        Purge data associated principals.

        usage: purge_principals principal_id [principal_id ...]
        """
        dryRun = True
        completely = False
        doimplicit = True

        directory = self.protocol.service.directory

        records = []
        for id in tokens:
            record = yield self.directoryRecordWithID(id)
            records.append(record)

            if not record:
                self.terminal.write("Unknown UID: %s\n" % (id,))

        if None in records:
            self.terminal.write("Aborting.\n")
            return

        if dryRun:
            toPurge = "to purge"
        else:
            toPurge = "purged"

        total = 0
        for record in records:
            count, _ignore_assignments = (yield PurgePrincipalService.purgeUIDs(
                self.protocol.service.store,
                directory,
                (record.uid,),
                verbose=False,
                dryrun=dryRun,
                completely=completely,
                doimplicit=doimplicit,
            ))
            total += count

            self.terminal.write(
                "%d events %s for UID %s.\n"
                % (count, toPurge, record.uid)
            )

        self.terminal.write(
            "%d total events %s.\n"
            % (total, toPurge)
        )

    cmd_purge_principals.hidden = "incomplete"

    #
    # Sharing
    #

    @inlineCallbacks
    def cmd_share(self, tokens):
        """
        Share a resource with a principal.

        usage: share mode principal_id target [target ...]

            mode: r (read) or rw (read/write)
        """
        if len(tokens) < 3:
            raise InsufficientArguments()

        mode = tokens.pop(0)
        principalID = tokens.pop(0)

        record = yield self.directoryRecordWithID(principalID)

        if not record:
            self.terminal.write("Principal not found: %s\n" % (principalID,))

        targets = yield self.getTargets(tokens)

        if mode == "r":
            mode = None
        elif mode == "rw":
            mode = None
        else:
            raise UsageError("Unknown mode: %s" % (mode,))

        for _ignore_target in targets:
            raise NotImplementedError()

    cmd_share.hidden = "incomplete"

    #
    # Python prompt, for the win
    #

    def cmd_python(self, tokens):
        """
        Switch to a python prompt.

        usage: python
        """
        if tokens:
            raise UnknownArguments(tokens)

        if not hasattr(self, "_interpreter"):
            # Bring in some helpful local variables.
            from txdav.common.datastore.sql_tables import schema
            from twext.enterprise.dal import syntax

            localVariables = dict(
                self=self,
                store=self.protocol.service.store,
                schema=schema,
            )

            # FIXME: Use syntax.__all__, which needs to be defined
            for key, value in syntax.__dict__.items():
                if not key.startswith("_"):
                    localVariables[key] = value

            class Handler(object):

                def addOutput(innerSelf, bytes, async=False):  # @NoSelf
                    """
                    This is a delegate method, called by ManholeInterpreter.
                    """
                    if async:
                        self.terminal.write("... interrupted for Deferred ...\n")
                    self.terminal.write(bytes)
                    if async:
                        self.terminal.write("\n")
                        self.protocol.drawInputLine()

            self._interpreter = ManholeInterpreter(Handler(), localVariables)

        def evalSomePython(line):
            if line == "exit":
                # Return to normal command mode.
                del self.protocol.lineReceived
                del self.protocol.ps
                try:
                    del self.protocol.pn
                except AttributeError:
                    pass
                self.protocol.drawInputLine()
                return

            more = self._interpreter.push(line)
            self.protocol.pn = bool(more)

            lw = self.terminal.lastWrite
            if not (lw.endswith("\n") or lw.endswith("\x1bE")):
                self.terminal.write("\n")
            self.protocol.drawInputLine()

        self.protocol.lineReceived = evalSomePython
        self.protocol.ps = (">>> ", "... ")

    cmd_python.hidden = "debug tool"

    #
    # SQL prompt, for not as winning
    #

    def cmd_sql(self, tokens):
        """
        Switch to an SQL prompt.

        usage: sql
        """
        if tokens:
            raise UnknownArguments(tokens)

        raise NotImplementedError("Command not implemented")

    cmd_sql.hidden = "not implemented"

    #
    # Test tools
    #

    def cmd_raise(self, tokens):
        """
        Raises an exception.

        usage: raise [message ...]
        """
        raise RuntimeError(" ".join(tokens))

    cmd_raise.hidden = "test tool"

    def cmd_reload(self, tokens):
        """
        Reloads code.

        usage: reload
        """
        if tokens:
            raise UnknownArguments(tokens)

        import calendarserver.tools.shell.vfs
        reload(calendarserver.tools.shell.vfs)

        import calendarserver.tools.shell.directory
        reload(calendarserver.tools.shell.directory)

        self.protocol.reloadCommands()

    cmd_reload.hidden = "test tool"

    def cmd_xyzzy(self, tokens):
        """
        """
        self.terminal.write("Nothing happens.")
        self.terminal.nextLine()

    cmd_sql.hidden = ""
