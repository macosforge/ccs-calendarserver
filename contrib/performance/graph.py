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

import sys

from matplotlib import pyplot
import numpy

from .benchlib import load_stats


def main():
    fig = pyplot.figure()
    ax = fig.add_subplot(111)

    data = [samples for (_ignore_stat, samples) in load_stats(sys.argv[1:])]

    bars = []
    color = iter('rgbcmy').next
    w = 1.0 / len(data)
    xs = numpy.arange(len(data[0]))
    for i, s in enumerate(data):
        bars.append(ax.bar(xs + i * w, s, width=w, color=color())[0])

    ax.set_xlabel('sample #')
    ax.set_ylabel('seconds')
    ax.legend(bars, sys.argv[1:])
    pyplot.show()
