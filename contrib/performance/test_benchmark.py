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
Unit tests for L{benchmark}.
"""
from __future__ import absolute_import

from twisted.trial.unittest import TestCase
from twisted.python.usage import UsageError

from .benchmark import BenchmarkOptions


class BenchmarkOptionsTests(TestCase):
    """
    Tests for L{benchmark.BenchmarkOptions}.
    """

    def setUp(self):
        """
        Create a L{BenchmarkOptions} instance to test.
        """
        self.options = BenchmarkOptions()

    def test_parameters(self):
        """
        The I{--parameters} option can be specified multiple time and
        each time specifies the parameters for a particular benchmark
        as a comma separated list of integers.
        """
        self.options.parseOptions(["--parameters", "foo:1,10,100", "foo"])
        self.assertEquals(
            self.options['parameters'], {"foo": [1, 10, 100]})

    def test_filterBenchmarksWithoutDistribution(self):
        """
        If neither I{--hosts-count} nor I{--host-index} are supplied,
        L{BenchmarkOptions} takes all positional arguments as the
        benchmarks to run.
        """
        self.options.parseOptions(["foo", "bar", "baz"])
        self.assertEquals(self.options['benchmarks'], ["foo", "bar", "baz"])

    def test_hostsCountWithoutIndex(self):
        """
        If I{--hosts-count} is provided but I{--host-index} is not, a
        L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions, ["--hosts-count=3", "foo"])
        self.assertEquals(
            str(exc),
            "Specify neither or both of hosts-count and host-index")

    def test_hostIndexWithoutCount(self):
        """
        If I{--host-index} is provided by I{--hosts-count} is not, a
        L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions, ["--host-index=3", "foo"])
        self.assertEquals(
            str(exc),
            "Specify neither or both of hosts-count and host-index")

    def test_negativeHostsCount(self):
        """
        If a negative value is provided for I{--hosts-count}, a
        L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions,
            ["--host-index=1", "--hosts-count=-1", "foo"])
        self.assertEquals(
            str(exc),
            "Specify a positive integer for hosts-count")

    def test_nonIntegerHostsCount(self):
        """
        If a string which cannot be converted to an integer is
        provided for I{--hosts-count}, a L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions,
            ["--host-index=1", "--hosts-count=hello", "foo"])
        self.assertEquals(
            str(exc),
            "Parameter type enforcement failed: invalid literal for int() with base 10: 'hello'")

    def test_negativeHostIndex(self):
        """
        If a negative value is provided for I{--host-index}, a
        L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions,
            ["--host-index=-1", "--hosts-count=2", "foo"])
        self.assertEquals(
            str(exc),
            "Specify a positive integer for host-index")

    def test_nonIntegerHostIndex(self):
        """
        If a string which cannot be converted to an integer is
        provided for I{--host-index}, a L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions,
            ["--host-index=hello", "--hosts-count=2", "foo"])
        self.assertEquals(
            str(exc),
            "Parameter type enforcement failed: invalid literal for int() with base 10: 'hello'")

    def test_largeHostIndex(self):
        """
        If the integer supplied to I{--host-index} is greater than or
        equal to the integer supplied to I{--hosts-count}, a
        L{UsageError} is raised.
        """
        exc = self.assertRaises(
            UsageError,
            self.options.parseOptions,
            ["--hosts-count=2", "--host-index=2", "foo"])
        self.assertEquals(
            str(exc),
            "host-index must be less than hosts-count")

    def test_hostIndexAndCount(self):
        """
        If I{--hosts-count} and I{--host-index} are supplied, of the
        benchmarks supplied as positional arguments, only a subset is
        taken as the benchmarks to run.  The subset is constructed so
        that for a particular value of I{hosts-count}, each benchmark
        will only appear in the subset returned for a single value of
        I{--host-index}, and all benchmarks will appear in one such
        subset.
        """
        self.options.parseOptions([
            "--hosts-count=3", "--host-index=0",
            "foo", "bar", "baz", "quux"])
        self.assertEquals(self.options['benchmarks'], ["foo", "quux"])

        self.options.parseOptions([
            "--hosts-count=3", "--host-index=1",
            "foo", "bar", "baz", "quux"])
        self.assertEquals(self.options['benchmarks'], ["bar"])

        self.options.parseOptions([
            "--hosts-count=3", "--host-index=2",
            "foo", "bar", "baz", "quux"])
        self.assertEquals(self.options['benchmarks'], ["baz"])
