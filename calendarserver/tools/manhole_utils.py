from __future__ import print_function
##
# Copyright (c) 2017 Apple Inc. All rights reserved.
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

# Useful methods for use in manhole. This can be imported inside a manhole session.

import gc
import collections


class Manhole(object):

    @staticmethod
    def getInstanceOf(cls):
        for obj in gc.get_objects():
            if isinstance(obj, cls):
                return obj
        else:
            return None

    @staticmethod
    def allInstancesOf(cls):
        return filter(lambda x: isinstance(x, cls), gc.get_objects())

    @staticmethod
    def instancesCounts():
        counts = collections.defaultdict(int)
        for item in gc.get_objects():
            if hasattr(item, "__class__"):
                counts[item.__class__.__name__] += 1
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            print("{}\t\t{}".format(v, k))
