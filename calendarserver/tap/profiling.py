##
# Copyright (c) 2010-2017 Apple Inc. All rights reserved.
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

import time

from twisted.application.app import CProfileRunner, AppProfiler


class CProfileCPURunner(CProfileRunner):
    """
    Runner for the cProfile module which uses C{time.clock} to measure
    CPU usage instead of the default wallclock time.
    """

    def run(self, reactor):
        """
        Run reactor under the cProfile profiler.
        """
        try:
            import cProfile
            import pstats
        except ImportError as e:
            self._reportImportError("cProfile", e)

        p = cProfile.Profile(time.clock)
        p.runcall(reactor.run)
        if self.saveStats:
            p.dump_stats(self.profileOutput)
        else:
            with open(self.profileOutput, 'w') as stream:
                s = pstats.Stats(p, stream=stream)
                s.strip_dirs()
                s.sort_stats(-1)
                s.print_stats()


AppProfiler.profilers["cprofile-cpu"] = CProfileCPURunner
