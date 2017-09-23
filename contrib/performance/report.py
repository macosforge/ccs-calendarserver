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
from .benchlib import select
import sys
import pickle


def main():
    if len(sys.argv) < 5:
        print('Usage: %s <datafile> <benchmark name> <parameter value> <metric> [command]' % (sys.argv[0],))
    else:
        stat, samples = select(pickle.load(file(sys.argv[1])), *sys.argv[2:5])
        if len(sys.argv) == 5:
            print('Samples')
            print('\t' + '\n\t'.join(map(str, stat.squash(samples))))
            print('Commands')
            print('\t' + '\n\t'.join(stat.commands))
        else:
            print(getattr(stat, sys.argv[5])(samples, *sys.argv[6:]))
