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

from itertools import count, cycle
from urllib2 import HTTPDigestAuthHandler

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from twisted.web.http import CREATED

from contrib.performance.httpauth import AuthHandlerAgent
from contrib.performance.httpclient import StringProducer

from contrib.performance.benchlib import initialize, sample
from contrib.performance.benchmarks.event import makeEvent


@inlineCallbacks
def measure(host, port, dtrace, attendeeCount, samples):
    organizerSequence = 1
    user = password = "user%02d" % (organizerSequence,)
    root = "/"
    principal = "/"

    # Two calendars between which to move the event.
    fooCalendar = "event-move-foo-benchmark"
    barCalendar = "event-move-bar-benchmark"

    authinfo = HTTPDigestAuthHandler()
    authinfo.add_password(
        realm="Test Realm",
        uri="http://%s:%d/" % (host, port),
        user=user,
        passwd=password)
    agent = AuthHandlerAgent(Agent(reactor), authinfo)

    # Set up the calendars first
    for calendar in [fooCalendar, barCalendar]:
        yield initialize(
            agent, host, port, user, password, root, principal, calendar)

    fooURI = 'http://%s:%d/calendars/__uids__/%s/%s/some-event.ics' % (
        host, port, user, fooCalendar)
    barURI = 'http://%s:%d/calendars/__uids__/%s/%s/some-event.ics' % (
        host, port, user, barCalendar)

    # Create the event that will move around
    headers = Headers({"content-type": ["text/calendar"]})
    yield agent.request(
        'PUT', fooURI, headers,
        StringProducer(makeEvent(1, organizerSequence, attendeeCount)))

    # Move it around sooo much
    source = cycle([fooURI, barURI])
    dest = cycle([barURI, fooURI])

    params = (
        ('MOVE', next(source),
         Headers({"destination": [next(dest)], "overwrite": ["F"]}))
        for i in count(1))

    samples = yield sample(dtrace, samples, agent, params.next, CREATED)
    returnValue(samples)
