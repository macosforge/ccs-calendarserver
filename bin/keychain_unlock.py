#!/usr/bin/env python
##
# Copyright (c) 2015-2017 Apple Inc. All rights reserved.
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

from __future__ import print_function
from subprocess import Popen, PIPE, STDOUT
import os
import sys

password_file = os.path.expanduser("~/.keychain")


def isKeychainUnlocked():

    child = Popen(
        args=[
            "/usr/bin/security",
            "show-keychain-info",
            "login.keychain",
        ],
        stdout=PIPE, stderr=STDOUT,
    )
    _ignore_output, _ignore_error = child.communicate()

    if child.returncode:
        return False
    else:
        return True


def unlockKeychain():

    if not os.path.isfile(password_file):
        print("Could not unlock login.keychain: no password available.")
        return False

    with open(password_file) as f:
        password = f.read().strip()

    child = Popen(
        args=[
            "/usr/bin/security", "-i",
        ],
        stdin=PIPE, stdout=PIPE, stderr=STDOUT,
    )
    output, error = child.communicate("unlock-keychain -p {} login.keychain\n".format(password))

    if child.returncode:
        print("Could not unlock login.keychain: {}".format(error if error else output))
        return False
    else:
        print("Unlocked login.keychain")
        return True


if __name__ == '__main__':

    if os.path.isfile("/usr/bin/security"):
        if isKeychainUnlocked():
            print("Keychain already unlocked")
        else:
            # If the identity exists we are done
            result = unlockKeychain()
    else:
        print("Keychain access utility ('security') not found")
        result = False
    sys.exit(0 if True else 1)
