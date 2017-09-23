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

import pickle
from time import time

from twisted.internet.defer import (
    FirstError, DeferredList, inlineCallbacks, returnValue)
from twisted.web.http_headers import Headers
from twisted.python.log import msg
from twisted.web.http import NO_CONTENT, NOT_FOUND

from .stats import Duration
from .httpclient import StringProducer, readBody


class CalDAVAccount(object):

    def __init__(self, agent, netloc, user, password, root, principal):
        self.agent = agent
        self.netloc = netloc
        self.user = user
        self.password = password
        self.root = root
        self.principal = principal

    def _makeURL(self, path):
        if not path.startswith('/'):
            raise ValueError("Pass a relative URL with an absolute path")
        return 'http://%s%s' % (self.netloc, path)

    def deleteResource(self, path):
        url = self._makeURL(path)
        d = self.agent.request('DELETE', url)

        def deleted(response):
            if response.code not in (NO_CONTENT, NOT_FOUND):
                raise Exception(
                    "Unexpected response to DELETE %s: %d" % (
                        url, response.code))
        d.addCallback(deleted)
        return d

    def makeCalendar(self, path):
        return self.agent.request('MKCALENDAR', self._makeURL(path))

    def writeData(self, path, data, contentType):
        return self.agent.request(
            'PUT',
            self._makeURL(path),
            Headers({'content-type': [contentType]}),
            StringProducer(data))


@inlineCallbacks
def _serial(fs):
    for (f, args) in fs:
        yield f(*args)
    returnValue(None)


def initialize(agent, host, port, user, password, root, principal, calendar):
    """
    If the specified calendar exists, delete it.  Then re-create it empty.
    """
    account = CalDAVAccount(
        agent,
        "%s:%d" % (host, port),
        user=user, password=password,
        root=root, principal=principal)
    cal = "/calendars/users/%s/%s/" % (user, calendar)
    d = _serial([
        (account.deleteResource, (cal,)),
        (account.makeCalendar, (cal,))])
    d.addCallback(lambda ignored: account)
    return d


def firstResult(deferreds):
    """
    Return a L{Deferred} which fires when the first L{Deferred} from
    C{deferreds} fires.

    @param deferreds: A sequence of Deferreds to wait on.
    """


@inlineCallbacks
def sample(dtrace, sampleTime, agent, paramgen, responseCode, concurrency=1):
    urlopen = Duration('HTTP')
    data = {urlopen: []}

    def once():
        msg('emitting request')
        before = time()
        params = paramgen()
        d = agent.request(*params)

        def cbResponse(response):
            if response.code != responseCode:
                raise Exception(
                    "Request %r received unexpected response code: %d" % (
                        params, response.code))

            d = readBody(response)

            def cbBody(ignored):
                after = time()
                msg('response received')

                # Give things a moment to settle down.  This is a hack
                # to try to collect the last of the dtrace output
                # which may still be sitting in the write buffer of
                # the dtrace process.  It would be nice if there were
                # a more reliable way to know when we had it all, but
                # no luck on that front so far.  The implementation of
                # mark is supposed to take care of that, but the
                # assumption it makes about ordering of events appears
                # to be invalid.

                # XXX Disabled until I get a chance to seriously
                # measure what affect, if any, it has.
                # d = deferLater(reactor, 0.5, dtrace.mark)
                d = dtrace.mark()

                def cbStats(stats):
                    msg('stats collected')
                    for k, v in stats.iteritems():
                        data.setdefault(k, []).append(v)
                    data[urlopen].append(after - before)
                d.addCallback(cbStats)
                return d
            d.addCallback(cbBody)
            return d
        d.addCallback(cbResponse)
        return d

    msg('starting dtrace')
    yield dtrace.start()
    msg('dtrace started')

    start = time()
    requests = []
    for _ignore_i in range(concurrency):
        requests.append(once())

    while requests:
        try:
            _ignore_result, index = yield DeferredList(requests, fireOnOneCallback=True, fireOnOneErrback=True)
        except FirstError as e:
            e.subFailure.raiseException()

        # Get rid of the completed Deferred
        del requests[index]

        if time() > start + sampleTime:
            # Wait for the rest of the outstanding requests to keep things tidy
            yield DeferredList(requests)
            # And then move on
            break
        else:
            # And start a new operation to replace it
            try:
                requests.append(once())
            except StopIteration:
                # Ran out of work to do, so paramgen raised a
                # StopIteration.  This is pretty sad.  Catch it or it
                # will demolish inlineCallbacks.
                if len(requests) == concurrency - 1:
                    msg('exhausted parameter generator')

    msg('stopping dtrace')
    leftOver = yield dtrace.stop()
    msg('dtrace stopped')
    for (k, v) in leftOver.items():
        if v:
            print('Extra', k, ':', v)
    returnValue(data)


def select(statistics, benchmark, parameter, statistic):
    for stat, samples in statistics[benchmark][int(parameter)].iteritems():
        if stat.name == statistic:
            return (stat, samples)
    raise ValueError("Unknown statistic %r" % (statistic,))


def load_stats(statfiles):
    data = []
    for fname in statfiles:
        fname, bench, param, stat = fname.split(',')
        stats, samples = select(
            pickle.load(file(fname)), bench, param, stat)
        data.append((stats, samples))
        if data:
            assert len(samples) == len(data[0][1])
    return data
