##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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
from contrib.performance.stats import SQLDuration, Bytes
from datetime import datetime
from os.path import dirname
from pickle import dump
from signal import SIGINT
from twisted.internet import reactor
from twisted.internet.defer import Deferred, inlineCallbacks, gatherResults
from twisted.internet.protocol import ProcessProtocol
from twisted.protocols.basic import LineReceiver
from twisted.python.filepath import FilePath
from twisted.python.log import msg
from twisted.python.modules import getModule
from twisted.python.usage import UsageError, Options, portCoerce
import sys
import os
import plistlib


class DTraceBug(Exception):
    """
    Represents some kind of problem related to a shortcoming in dtrace
    itself.
    """


class IOMeasureConsumer(ProcessProtocol):

    def __init__(self, started, done, parser):
        self.started = started
        self.done = done
        self.parser = parser

    def connectionMade(self):
        self._out = ''
        self._err = ''

    def mark(self):
        return self.parser.mark()

    def errReceived(self, bytes):
        self._err += bytes
        if 'Interrupted system call' in self._err:
            started = self.started
            self.started = None
            started.errback(DTraceBug(self._err))

    def outReceived(self, bytes):
        if self.started is None:
            self.parser.dataReceived(bytes)
        else:
            self._out += bytes
            if self._out.startswith('READY\n'):
                self.parser.dataReceived(self._out[len('READY\n'):])
                del self._out
                started = self.started
                self.started = None
                started.callback(None)

    def processEnded(self, reason):
        if self.started is None:
            self.done.callback(None)
        else:
            self.started.errback(RuntimeError("Exited too soon: %r/%r" % (self._out, self._err)))


def masterPID(directory):
    return int(directory.child('caldavd.pid').getContent())


def instancePIDs(directory):
    pids = []
    for pidfile in directory.children():
        if pidfile.basename().startswith('caldav-instance-'):
            pidtext = pidfile.getContent()
            pid = int(pidtext)
            pids.append(pid)
    return pids


class _DTraceParser(LineReceiver):
    delimiter = '\n\1'

    sql = None
    start = None
    _marked = None

    def __init__(self, collector):
        self.collector = collector

    def lineReceived(self, dtrace):
        # dtrace puts some extra newlines in the output sometimes.  Get rid of them.
        dtrace = dtrace.strip()
        if dtrace:
            op, rest = dtrace.split(None, 1)
            getattr(self, '_op_' + op)(op, rest)

    def mark(self):
        self._marked = Deferred()
        return self._marked

    def _op_MARK(self, cmd, rest):
        marked = self._marked
        self._marked = None
        if marked is not None:
            marked.callback(None)

    def _op_EXECUTE(self, cmd, rest):
        try:
            which, when = rest.split(None, 1)
        except ValueError:
            msg('Bad EXECUTE line: %r' % (rest,))
            return

        if which == 'SQL':
            self.sql = when
            return

        when = int(when)
        if which == 'ENTRY':
            if self.start is not None:
                msg('entry without return at %s in %s' % (when, cmd))
            self.start = when
        elif which == 'RETURN':
            if self.start is None:
                msg('return without entry at %s in %s' % (when, cmd))
            elif self.sql is None:
                msg('return without SQL at %s in %s' % (when, cmd))
            else:
                diff = when - self.start
                if diff < 0:
                    msg('Completely bogus EXECUTE %s %s' % (self.start, when))
                else:
                    if cmd == 'EXECUTE':
                        accum = self.collector._execute
                    elif cmd == 'ITERNEXT':
                        accum = self.collector._iternext

                    accum.append((self.sql, diff))
                self.start = None

    _op_ITERNEXT = _op_EXECUTE

    def _op_B_READ(self, cmd, rest):
        self.collector._bread.append(int(rest))

    def _op_B_WRITE(self, cmd, rest):
        self.collector._bwrite.append(int(rest))

    def _op_READ(self, cmd, rest):
        self.collector._read.append(int(rest))

    def _op_WRITE(self, cmd, rest):
        self.collector._write.append(int(rest))


class DTraceCollector(object):

    def __init__(self, script, pids):
        self._dScript = script
        self.pids = pids
        self._init_stats()

    def _init_stats(self):
        self._bread = []
        self._bwrite = []
        self._read = []
        self._write = []
        self._execute = []
        self._iternext = []

    def stats(self):
        results = {
            Bytes('pagein'): self._bread,
            Bytes('pageout'): self._bwrite,
            Bytes('read'): self._read,
            Bytes('write'): self._write,
            SQLDuration('execute'): self._execute,  # Time spent in the execute phase of SQL execution
            SQLDuration('iternext'): self._iternext,  # Time spent fetching rows from the execute phase
            SQLDuration('SQL'): self._execute + self._iternext,  # Combination of the previous two
        }
        self._init_stats()
        return results

    def start(self):
        ready = []
        self.finished = []
        self.dtraces = {}

        # Trace each child process specifically.  Necessary because of
        # the way SQL execution is measured, which requires the
        # $target dtrace variable (which can only take on a single
        # value).
        for p in self.pids:
            started, stopped = self._startDTrace(self._dScript, p)
            ready.append(started)
            self.finished.append(stopped)

        if self.pids:
            # If any tracing is to be done, then also trace postgres
            # i/o operations.  This involves no target, because the
            # dtrace code just looks for processes named "postgres".
            # We skip it if we don't have any pids because that's
            # heuristically the "don't do any dtracing" setting (it
            # might be nice to make this explicit).
            started, stopped = self._startDTrace("pgsql.d", None)
            ready.append(started)
            self.finished.append(stopped)

        return gatherResults(ready)

    def _startDTrace(self, script, pid):
        """
        Launch a dtrace process.

        @param script: A C{str} giving the path to the dtrace program
            to run.

        @param pid: A C{int} to target dtrace at a particular process,
            or C{None} not to.

        @return: A two-tuple of L{Deferred}s.  The first will fire
            when the dtrace process is ready to go, the second will
            fire when it exits.
        """
        started = Deferred()
        stopped = Deferred()
        proto = IOMeasureConsumer(started, stopped, _DTraceParser(self))
        command = [
            "/usr/sbin/dtrace",
            # process preprocessor macros
            "-C",
            # search for include targets in the source directory containing this file
            "-I", dirname(__file__),
            # suppress most implicitly generated output (which would mess up our parser)
            "-q",
            # load this script
            "-s", script]
        if pid is not None:
            # make this pid the target
            command.extend(["-p", str(pid)])

        process = reactor.spawnProcess(proto, command[0], command)

        def eintr(reason):
            reason.trap(DTraceBug)
            msg('Dtrace startup failed (%s), retrying.' % (reason.getErrorMessage().strip(),))
            return self._startDTrace(script, pid)

        def ready(passthrough):
            # Once the dtrace process is ready, save the state and
            # have the stopped Deferred deal with the results.  We
            # don't want to do either of these for failed dtrace
            # processes.
            msg("dtrace tracking pid=%s" % (pid,))
            self.dtraces[pid] = (process, proto)
            stopped.addCallback(self._cleanup, pid)
            return passthrough
        started.addCallbacks(ready, eintr)
        return started, stopped

    def _cleanup(self, passthrough, pid):
        del self.dtraces[pid]
        return passthrough

    def mark(self):
        marks = []
        for (_ignore_process, protocol) in self.dtraces.itervalues():
            marks.append(protocol.mark())
        d = gatherResults(marks)
        d.addCallback(lambda ign: self.stats())
        try:
            os.execve(
                "CalendarServer dtrace benchmarking signal", [], {})
        except OSError:
            pass
        return d

    def stop(self):
        for (process, _ignore_protocol) in self.dtraces.itervalues():
            process.signalProcess(SIGINT)
        d = gatherResults(self.finished)
        d.addCallback(lambda ign: self.stats())
        return d


@inlineCallbacks
def benchmark(host, port, pids, label, scalingParameters, benchmarks):
    # Collect samples for 2 minutes.  This should give plenty of data
    # for quick benchmarks.  It will leave lots of error (due to small
    # sample size) for very slow benchmarks, but the error isn't as
    # interesting as the fact that a single operation takes
    # double-digit seconds or longer to complete.
    sampleTime = 60 * 2

    statistics = {}

    for (name, measure) in benchmarks:
        statistics[name] = {}
        parameters = scalingParameters.get(name, [1, 9, 81])
        for p in parameters:
            print('%s, parameter=%s' % (name, p))
            dtrace = DTraceCollector("io_measure.d", pids)
            data = yield measure(host, port, dtrace, p, sampleTime)
            statistics[name][p] = data

    fObj = file(
        '%s-%s' % (label, datetime.now().isoformat()), 'w')
    dump(statistics, fObj, 2)
    fObj.close()


def logsCoerce(directory):
    path = FilePath(directory)
    if not path.isdir():
        raise ValueError("%r is not a directory" % (path.path,))
    return path


class BenchmarkOptions(Options):
    optParameters = [
        ('host', 'h', 'localhost',
         'Hostname or IPv4 address on which a CalendarServer is listening'),
        ('port', 'p', 8008,
         'Port number on which a CalendarServer is listening', portCoerce),
        ('source-directory', 'd', None,
         'The base of the CalendarServer source checkout being benchmarked '
         '(if and only if the CalendarServer is on the same host as this '
         'benchmark process and dtrace-based metrics are desired)',
         logsCoerce),
        ('label', 'l', 'data', 'A descriptive string to attach to the output filename.'),
        ('hosts-count', None, None, 'For distributed benchmark collection, the number of hosts participating in collection.', int),
        ('host-index', None, None, 'For distributed benchmark collection, the (zero-based) index of this host in the collection.', int),
    ]

    optFlags = [
        ('debug', None, 'Enable various debugging helpers'),
    ]

    def __init__(self):
        Options.__init__(self)
        self['parameters'] = {}

    def _selectBenchmarks(self, benchmarks):
        """
        Select the benchmarks to run, based on those named and on the
        values passed for I{--hosts-count} and I{--host-index}.
        """
        count = self['hosts-count']
        index = self['host-index']
        if (count is None) != (index is None):
            raise UsageError("Specify neither or both of hosts-count and host-index")
        if count is not None:
            if count < 0:
                raise UsageError("Specify a positive integer for hosts-count")
            if index < 0:
                raise UsageError("Specify a positive integer for host-index")
            if index >= count:
                raise UsageError("host-index must be less than hosts-count")
            benchmarks = [
                benchmark
                for (i, benchmark)
                in enumerate(benchmarks)
                if i % self['hosts-count'] == self['host-index']]
        return benchmarks

    def opt_parameters(self, which):
        """
        Specify the scaling parameters for a particular benchmark.
        The format of the value is <benchmark>:<value>,...,<value>.
        The given benchmark will be run with a scaling parameter set
        to each of the given values.  This option may be specified
        multiple times to specify parameters for multiple benchmarks.
        """
        benchmark, values = which.split(':')
        values = map(int, values.split(','))
        self['parameters'][benchmark] = values

    def parseArgs(self, *benchmarks):
        if not benchmarks:
            raise UsageError("Specify at least one benchmark")
        self['benchmarks'] = self._selectBenchmarks(list(benchmarks))


def whichPIDs(source, conf):
    """
    Return a list of PIDs to dtrace.
    """
    run = source.preauthChild(conf['ServerRoot']).preauthChild(conf['RunRoot'])
    return [run.child(conf['PIDFile']).getContent()] + [
        pid.getContent() for pid in run.globChildren('*instance*')]


_benchmarks = getModule("contrib.performance.benchmarks")


def resolveBenchmark(name):
    for module in _benchmarks.iterModules():
        if module.name == ".".join((_benchmarks.name, name)):
            return module.load()
    raise ValueError("Unknown benchmark: %r" % (name,))


def main():
    from twisted.python.log import startLogging, err

    options = BenchmarkOptions()
    try:
        options.parseOptions(sys.argv[1:])
    except UsageError as e:
        print(e)
        return 1

    if options['debug']:
        from twisted.python.failure import startDebugMode
        startDebugMode()

    if options['source-directory']:
        source = options['source-directory']
        conf = source.child('conf').child('caldavd-dev.plist')
        pids = whichPIDs(source, plistlib.PlistParser().parse(conf.open()))
    else:
        pids = []
    msg("Using dtrace to monitor pids %r" % (pids,))

    startLogging(file('benchmark.log', 'a'), False)

    d = benchmark(
        options['host'], options['port'], pids, options['label'],
        options['parameters'],
        [(arg, resolveBenchmark(arg).measure) for arg in options['benchmarks']])
    d.addErrback(err, "Failure at benchmark runner top-level")
    reactor.callWhenRunning(d.addCallback, lambda ign: reactor.stop())
    reactor.run()
