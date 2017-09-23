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
from __future__ import absolute_import
from .benchmark import DTraceCollector, instancePIDs
from twisted.internet import reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.python.failure import Failure
from twisted.python.log import err
import sys
import signal
import time


class Stop(Exception):
    pass


interrupted = 0.0


def waitForInterrupt():
    if signal.getsignal(signal.SIGINT) != signal.default_int_handler:
        raise RuntimeError("Already waiting")

    d = Deferred()

    def fire(*ignored):
        global interrupted
        signal.signal(signal.SIGINT, signal.default_int_handler)
        now = time.time()
        if now - interrupted < 4:
            reactor.callFromThread(lambda: d.errback(Failure(Stop())))
        else:
            interrupted = now
            reactor.callFromThread(d.callback, None)
    signal.signal(signal.SIGINT, fire)
    return d


@inlineCallbacks
def collect(directory):
    while True:
        pids = instancePIDs(directory)
        dtrace = DTraceCollector("sql_measure.d", pids)
        print('Starting')
        yield dtrace.start()
        print('Started')
        try:
            yield waitForInterrupt()
        except Stop:
            yield dtrace.stop()
            break
        print('Stopping')
        stats = yield dtrace.stop()
        for s in stats:
            if s.name == 'execute':
                s.statements(stats[s])
        print('Stopped')


def main():
    from twisted.python.failure import startDebugMode
    startDebugMode()
    d = collect(sys.argv[1])
    d.addErrback(err, "Problem collecting SQL")
    d.addBoth(lambda ign: reactor.stop())
    reactor.run(installSignalHandlers=False)
