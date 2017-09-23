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

"""
Benchmark a server's handling of event summary changes.
"""
from __future__ import absolute_import

from itertools import count

from urllib2 import HTTPDigestAuthHandler

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from twisted.web.http import NO_CONTENT

from .httpauth import AuthHandlerAgent
from .httpclient import StringProducer

from .benchlib import initialize, sample

from ._event_create import makeEvent


@inlineCallbacks
def measure(host, port, dtrace, attendeeCount, samples, fieldName,
            replacer, eventPerSample=False):
    user = password = "user01"
    root = "/"
    principal = "/"
    calendar = "event-%s-benchmark" % (fieldName,)

    authinfo = HTTPDigestAuthHandler()
    authinfo.add_password(
        realm="Test Realm",
        uri="http://%s:%d/" % (host, port),
        user=user,
        passwd=password)
    agent = AuthHandlerAgent(Agent(reactor), authinfo)

    # Set up the calendar first
    yield initialize(agent, host, port, user, password, root, principal, calendar)

    if eventPerSample:
        # Create an event for each sample that will be taken, so that no event
        # is used for two different samples.
        f = _selfish_sample
    else:
        # Just create one event and re-use it for all samples.
        f = _generous_sample

    data = yield f(
        dtrace, replacer, agent, host, port, user, calendar, fieldName,
        attendeeCount, samples)
    returnValue(data)


@inlineCallbacks
def _selfish_sample(dtrace, replacer, agent, host, port, user, calendar, fieldName, attendeeCount, samples):
    url = 'http://%s:%s/calendars/__uids__/%s/%s/%s-change-%%d.ics' % (
        host, port, user, calendar, fieldName)

    headers = Headers({"content-type": ["text/calendar"]})

    events = [
        # The organizerSequence here (1) may need to be a parameter.
        # See also the makeEvent call below.
        (makeEvent(i, 1, attendeeCount), url % (i,))
        for i in range(samples)]

    for (event, url) in events:
        yield agent.request('PUT', url, headers, StringProducer(event))

    # Sample changing the event according to the replacer.
    samples = yield sample(
        dtrace, samples,
        agent, (('PUT', url, headers, StringProducer(replacer(event, i)))
                for i, (event, url)
                in enumerate(events)).next,
        NO_CONTENT)
    returnValue(samples)


@inlineCallbacks
def _generous_sample(dtrace, replacer, agent, host, port, user, calendar, fieldName, attendeeCount, samples):
    url = 'http://%s:%s/calendars/__uids__/%s/%s/%s-change.ics' % (
        host, port, user, calendar, fieldName)

    headers = Headers({"content-type": ["text/calendar"]})

    # See the makeEvent call above.
    event = makeEvent(0, 1, attendeeCount)

    yield agent.request('PUT', url, headers, StringProducer(event))

    # Sample changing the event according to the replacer.
    samples = yield sample(
        dtrace, samples,
        agent, (('PUT', url, headers, StringProducer(replacer(event, i)))
                for i in count(1)).next,
        NO_CONTENT)
    returnValue(samples)
