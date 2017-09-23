from __future__ import absolute_import
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

from twisted.trial.unittest import TestCase

from .stats import (
    SQLDuration, LogNormalDistribution, UniformDiscreteDistribution,
    UniformIntegerDistribution, WorkDistribution, quantize,
    RecurrenceDistribution)
from pycalendar.datetime import DateTime
from pycalendar.timezone import Timezone


class SQLDurationTests(TestCase):

    def setUp(self):
        self.stat = SQLDuration('foo')

    def test_normalize_integer(self):
        self.assertEquals(
            self.stat.normalize('SELECT foo FROM bar WHERE 1'),
            'SELECT foo FROM bar WHERE ?')
        self.assertEquals(
            self.stat.normalize('SELECT foo FROM bar WHERE x = 1'),
            'SELECT foo FROM bar WHERE x = ?')
        self.assertEquals(
            self.stat.normalize('SELECT foo + 1 FROM bar'),
            'SELECT foo + ? FROM bar')

    def test_normalize_boolean(self):
        self.assertEquals(
            self.stat.normalize('SELECT foo FROM bar WHERE True'),
            'SELECT foo FROM bar WHERE ?')


class DistributionTests(TestCase):

    def test_lognormal(self):
        dist = LogNormalDistribution(mu=1, sigma=1)
        for _ignore_i in range(100):
            value = dist.sample()
            self.assertIsInstance(value, float)
            self.assertTrue(value >= 0.0, "negative value %r" % (value,))
            self.assertTrue(value <= 1000, "implausibly high value %r" % (value,))

        dist = LogNormalDistribution(mode=1, median=2)
        for _ignore_i in range(100):
            value = dist.sample()
            self.assertIsInstance(value, float)
            self.assertTrue(value >= 0.0, "negative value %r" % (value,))
            self.assertTrue(value <= 1000, "implausibly high value %r" % (value,))

        dist = LogNormalDistribution(mode=1, mean=2)
        for _ignore_i in range(100):
            value = dist.sample()
            self.assertIsInstance(value, float)
            self.assertTrue(value >= 0.0, "negative value %r" % (value,))
            self.assertTrue(value <= 1000, "implausibly high value %r" % (value,))

        self.assertRaises(ValueError, LogNormalDistribution, mu=1)
        self.assertRaises(ValueError, LogNormalDistribution, sigma=1)
        self.assertRaises(ValueError, LogNormalDistribution, mode=1)
        self.assertRaises(ValueError, LogNormalDistribution, mean=1)
        self.assertRaises(ValueError, LogNormalDistribution, median=1)

    def test_uniformdiscrete(self):
        population = [1, 5, 6, 9]
        counts = dict.fromkeys(population, 0)
        dist = UniformDiscreteDistribution(population)
        for _ignore_i in range(len(population) * 10):
            counts[dist.sample()] += 1
        self.assertEqual(dict.fromkeys(population, 10), counts)

    def test_workdistribution(self):
        tzname = "US/Eastern"
        dist = WorkDistribution(["mon", "wed", "thu", "sat"], 10, 20, tzname)
        dist._helperDistribution = UniformDiscreteDistribution([35 * 60 * 60 + 30 * 60])
        dist.now = lambda tzname = None: DateTime(2011, 5, 29, 18, 5, 36, tzid=tzname)
        value = dist.sample()
        self.assertEqual(
            # Move past three workdays - monday, wednesday, thursday - using 30
            # of the hours, and then five and a half hours into the fourth
            # workday, saturday.  Workday starts at 10am, so the sample value
            # is 3:30pm, ie 1530 hours.
            DateTime(2011, 6, 4, 15, 30, 0, tzid=Timezone(tzid=tzname)),
            value
        )

        dist = WorkDistribution(["mon", "tue", "wed", "thu", "fri"], 10, 20, tzname)
        dist._helperDistribution = UniformDiscreteDistribution([35 * 60 * 60 + 30 * 60])
        value = dist.sample()
        self.assertTrue(isinstance(value, DateTime))

    # twisted.trial.unittest.FailTest: not equal:
    # a = datetime.datetime(2011, 6, 4, 15, 30, tzinfo=<DstTzInfo 'US/Eastern' EST-1 day, 19:00:00 STD>)
    # b = datetime.datetime(2011, 6, 4, 19, 30, tzinfo=<DstTzInfo 'US/Eastern' EDT-1 day, 20:00:00 DST>)
    # test_workdistribution.todo = "Somehow timezones mess this up"

    def test_recurrencedistribution(self):
        dist = RecurrenceDistribution(False)
        for _ignore in range(100):
            value = dist.sample()
            self.assertTrue(value is None)

        dist = RecurrenceDistribution(True, {"daily": 1, "none": 2, "weekly": 1})
        dist._helperDistribution = UniformDiscreteDistribution([0, 3, 2, 1, 0], randomize=False)
        value = dist.sample()
        self.assertTrue(value is not None)
        value = dist.sample()
        self.assertTrue(value is None)
        value = dist.sample()
        self.assertTrue(value is None)
        value = dist.sample()
        self.assertTrue(value is not None)
        value = dist.sample()
        self.assertTrue(value is not None)

    def test_uniform(self):
        dist = UniformIntegerDistribution(-5, 10)
        for _ignore_i in range(100):
            value = dist.sample()
            self.assertTrue(-5 <= value < 10)
            self.assertIsInstance(value, int)


class QuantizationTests(TestCase):
    """
    Tests for L{quantize} which constructs discrete datasets of
    dynamic quantization from continuous datasets.
    """
    skip = "nothing implemented yet, maybe not necessary"

    def test_one(self):
        """
        A single data point is put into a bucket equal to its value and returned.
        """
        dataset = [5.0]
        expected = [(5.0, [5.0])]
        self.assertEqual(quantize(dataset), expected)

    def test_two(self):
        """
        Each of two values are put into buckets the size of the
        standard deviation of the sample.
        """
        dataset = [2.0, 5.0]
        expected = [(1.5, [2.0]), (4.5, [5.0])]
        self.assertEqual(quantize(dataset), expected)

    def xtest_three(self):
        """
        If two out of three values fall within one bucket defined by
        the standard deviation of the sample, that bucket is split in
        half so each bucket has one value.
        """
        pass

    def xtest_alpha(self):
        """
        This exercises one simple case of L{quantize} with a small amount of data.
        """
