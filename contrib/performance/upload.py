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

import sys
import pickle

from urllib import urlencode
from datetime import datetime

from twisted.python.log import err
from twisted.python.usage import UsageError, Options
from twisted.internet import reactor
from twisted.web.client import Agent

from .stats import median, mad
from .benchlib import select
from .httpclient import StringProducer, readBody


class UploadOptions(Options):
    optParameters = [
        ('url', None, None,
         'Location of Codespeed server to which to upload.'),
        ('project', None, 'CalendarServer',
         'Name of the project to which the data relates '
         '(as recognized by the Codespeed server)'),
        ('revision', None, None,
         'Revision number of the code which produced this data.'),
        ('revision-date', None, None,
         'Timestamp at which the specified revision was created.'),
        ('environment', None, None,
         'Name of the environment in which the data was produced.'),
        ('statistic', None, None,
         'Identifier for the file/benchmark/parameter'),
        ('backend', None, None,
         'Which storage backend produced this data.'),
    ]

    def postOptions(self):
        assert self['url']
        assert self['backend'] in ('filesystem', 'postgresql')


def _upload(reactor, url, project, revision, revision_date, benchmark,
            executable, environment, result_value, result_date, std_dev,
            max_value, min_value):
    data = {
        'commitid': str(revision),
        'revision_date': revision_date,
        'project': project,
        'benchmark': benchmark,
        'environment': environment,
        'executable': executable,
        'result_value': str(result_value),
        'result_date': result_date,
        'std_dev': str(std_dev),
        'max': str(max_value),
        'min': str(min_value),
    }
    print('uploading', data)
    agent = Agent(reactor)
    d = agent.request('POST', url, None, StringProducer(urlencode(data)))

    def check(response):
        d = readBody(response)

        def read(body):
            print('body', repr(body))
            if response.code != 200:
                raise Exception("Upload failed: %r" % (response.code,))
        d.addCallback(read)
        return d
    d.addCallback(check)
    return d


def upload(reactor, url, project, revision, revision_date, benchmark, param, statistic, backend, environment, samples):
    d = _upload(
        reactor,
        url=url,
        project=project,
        revision=revision,
        revision_date=revision_date,
        benchmark='%s-%s-%s' % (benchmark, param, statistic),
        executable='%s-backend' % (backend,),
        environment=environment,
        result_value=median(samples),
        result_date=datetime.now(),
        std_dev=mad(samples),  # Not really!
        max_value=max(samples),
        min_value=min(samples))
    d.addErrback(err, "Upload failed")
    return d


def main():
    options = UploadOptions()
    try:
        options.parseOptions(sys.argv[1:])
    except UsageError as e:
        print(e)
        return 1

    fname, benchmark, param, statistic = options['statistic'].split(',')
    stat, samples = select(
        pickle.load(file(fname)), benchmark, param, statistic)
    samples = stat.squash(samples)

    d = upload(
        reactor,
        options['url'], options['project'], options['revision'],
        options['revision-date'],
        benchmark, param, statistic,
        options['backend'], options['environment'],
        samples)
    reactor.callWhenRunning(d.addCallback, lambda ign: reactor.stop())
    reactor.run()
