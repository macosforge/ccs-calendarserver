from __future__ import print_function
##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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


from collections import defaultdict, namedtuple
from contrib.performance.stats import LogNormalDistribution
from scipy.optimize import curve_fit
import itertools
import matplotlib.pyplot as plt
import numpy as np
import os


class LogNormal(object):

    Params = namedtuple("Params", ("mu", "sigma", "scale_x", "scale_y"))

    @staticmethod
    def fn(x, mu, sigma, scale_x, scale_y):
        """
        Calculate the LogNormal F(x) value for the given parameters.

        @param x: x value to use
        @type x: L{float}
        @param mu: mean
        @type mu: L{float}
        @param sigma: standard deviation
        @type sigma: L{float}
        @param scale_x: X-scaling factor (to make mode == 1.0)
        @type scale_x: L{float}
        @param scale_y: Y-scaling factor for peak height
        @type scale_y: L{float}
        """
        return scale_y * (
            np.exp(-(np.log(x / scale_x) - mu) ** 2 / (2 * sigma ** 2)) /
            (x / scale_x * sigma * np.sqrt(2 * np.pi))
        )

    @staticmethod
    def estimate(x, y):
        """
        Given a set of x-y data that is likely a LogNormal distribution, try and estimate the mode
        and median, and then derive mu, sigma, scale_x and scale_y values.

        @param x: sequence of values
        @type x: L{list} of L{float}
        @param y: sequence of values
        @type y: L{list} of L{float}
        """
        estimate_mode = 0.0
        estimate_median = 0.0
        max_y = 0.0
        accumulated_y = 0.0
        half_y = sum(y) / 2.0
        for x_val, y_val in itertools.izip(x, y):
            if y_val > max_y:
                max_y = y_val
                estimate_mode = x_val
            accumulated_y += y_val
            if half_y is not None and accumulated_y > half_y:
                estimate_median = x_val
                half_y = None

        estimate_scale_x = estimate_mode - 0.5
        estimate_mode = 1.0
        estimate_median /= estimate_scale_x
        estimate_mu = np.log(estimate_median)
        estimate_sigma = np.sqrt(np.log(estimate_median) - np.log(estimate_mode))

        peak_y = LogNormal.fn(estimate_scale_x, estimate_mu, estimate_sigma, estimate_scale_x, 1.0)
        estimate_scale_y = max(y) / peak_y
        return (
            estimate_mode, estimate_median,
            LogNormal.Params(estimate_mu, estimate_sigma, estimate_scale_x, estimate_scale_y),
        )

    @staticmethod
    def plot(min_x, max_x, params, color):
        """
        Plot a LogNormal distribution over the specified x-axis range using the supplied
        distribution parameters.

        @param min_x: minimum x-axis value
        @type min_x: L{float}
        @param max_x: maximum x-asix value
        @type max_x: L{float}
        @param params: distribution parameters
        @type params: L{LogNormal.Params}
        @param color: color to use for line in plot
        @type color: L{str}
        """
        xl = np.linspace(min_x, max_x, 10000)
        yl = (
            np.exp(-(np.log(xl / params.scale_x) - params.mu) ** 2 / (2 * params.sigma ** 2)) /
            (xl / params.scale_x * params.sigma * np.sqrt(2 * np.pi))
        )

        plt.plot(xl, params.scale_y * yl, linewidth=2, color=color)

    @staticmethod
    def plotCSV(path, cutoff, bucket, color):
        """
        Plot data from a CSV file.

        @param path: file to read from
        @type path: L{str}
        @param cutoff: maximum x-value to process
        @type cutoff: L{float}
        @param bucket: size of x-value buckets to use
        @type bucket: L{int}
        @param color: color to use for line in plot
        @type color: L{str}
        """
        with open(os.path.expanduser(path)) as f:
            data = f.read()
        result = defaultdict(int)
        for line in data.splitlines():
            sp = line.split(",")
            x_val = int(sp[0])
            y_val = int(sp[1])
            if x_val < cutoff:
                result[(x_val / bucket) * bucket] += y_val

        x, y = zip(*sorted(result.items()))
        plt.plot(x, y)
        return (x, y,)

    @staticmethod
    def distributionPlot(mode, median, maximum, color):
        """
        Plot data from a randomly generated LogNornal distribution.

        @param mode: distribution mode
        @type mode: L{float}
        @param median: distribution median
        @type median: L{float}
        @param maximum: highest x-value to allow
        @type maximum: L{float}
        @param color: color to use for line in plot
        @type color: L{str}
        """
        distribution = LogNormalDistribution(mode=mode, median=median, maximum=maximum)
        result = defaultdict(int)
        for _ignore in range(1000000):
            s = int(distribution.sample()) + 0.5
            result[s] += 1

        x, y = zip(*sorted(result.items()))
        plt.plot(x, y, color=color)

        peak_y = LogNormal.fn(distribution._scale, distribution._mu, distribution._sigma, distribution._scale, 1.0)
        scale_y = sum(sorted(y, reverse=True)[0:100]) / 100.0 / peak_y

        return (x, y, LogNormal.Params(distribution._mu, distribution._sigma, distribution._scale, scale_y),)

    @staticmethod
    def fit(x, y):
        """
        Try and fit a LogNormal distribution to the supplied x-y data.

        @param x: sequence of values
        @type x: L{list} of L{float}
        @param y: sequence of values
        @type y: L{list} of L{float}
        """
        estimate_mode, estimate_median, estimate_params = LogNormal.estimate(x, y)

        print("\n==== Estimates")
        print("mode: {}".format(estimate_mode * estimate_params.scale_x))
        print("median: {}".format(estimate_median * estimate_params.scale_x))
        print("mu: {}".format(estimate_params.mu))
        print("sigma: {}".format(estimate_params.sigma))
        print("scale_x: {}".format(estimate_params.scale_x))
        print("scale_y: {}".format(estimate_params.scale_y))

        popt, _ignore_pcov = curve_fit(LogNormal.fn, x, y, (
            estimate_params.mu, estimate_params.sigma, estimate_params.scale_x, estimate_params.scale_y,
        ))

        print("\n==== Fit results")
        print("mode: {:.2f}".format(popt[2] * np.exp(popt[0] - popt[1] ** 2)))
        print("median: {:.2f}".format(popt[2] * np.exp(popt[0])))
        print("mu: {:.2f}".format(popt[0]))
        print("sigma: {:.2f}".format(popt[1]))
        print("scale_x: {:.2f}".format(popt[2]))
        print("scale_y: {:.2f}".format(popt[3]))

        return (
            LogNormal.Params(*popt),
            estimate_params,
        )


if __name__ == '__main__':

    #x, y = LogNormal.plotCSV("~/data.txt", 10000, 10, color="b")
    #estimate_mode, estimate_median, estimate_params = LogNormal.estimate(x, y)

    mode_val = 450
    median_val = 650
    x, y, estimate_params = LogNormal.distributionPlot(mode_val, median_val, 10000, color="b")

    LogNormal.plot(1, 10000, estimate_params, color="g")

    popt, estimate = LogNormal.fit(x, y)
    LogNormal.plot(1, 10000, popt, color="r")

    plt.xlabel("Samples")
    plt.ylabel("LogNormal")
    plt.show()
