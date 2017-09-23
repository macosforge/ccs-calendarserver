##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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

__all__ = [
    "Config",
    "ConfigDict",
    "ConfigProvider",
    "ConfigurationError",
    "config",
]

import copy
import hashlib
import os


class ConfigurationError(RuntimeError):
    """
    Invalid server configuration.
    """


class ConfigDict(dict):
    """
    Dictionary which can be accessed using attribute syntax, because
    that reads and writes nicer in code.  For example:
      C{config.Thingo.Tiny.Tweak}
    instead of:
      C{config["Thingo"]["Tiny"]["Tweak"]}
    """

    def __init__(self, mapping=None):
        if mapping is not None:
            for key, value in mapping.iteritems():
                self[key] = value

    def __repr__(self):
        return "*" + dict.__repr__(self)

    def __setitem__(self, key, value):
        if key.startswith("_"):
            # Names beginning with "_" are reserved for real attributes
            raise KeyError("Keys may not begin with '_': %s" % (key,))

        if isinstance(value, dict) and not isinstance(value, self.__class__):
            dict.__setitem__(self, key, self.__class__(value))
        else:
            dict.__setitem__(self, key, value)

    def __setattr__(self, attr, value):
        if attr.startswith("_"):
            dict.__setattr__(self, attr, value)
        else:
            self[attr] = value

    def __getattr__(self, attr):
        if not attr.startswith("_") and attr in self:
            return self[attr]
        else:
            return dict.__getattribute__(self, attr)

    def __delattr__(self, attr):
        if not attr.startswith("_") and attr in self:
            del self[attr]
        else:
            dict.__delattr__(self, attr)


class ConfigProvider(object):
    """
    Configuration provider, abstraction for config storage/format/defaults.
    """

    def __init__(self, defaults=None):
        """
        Create configuration provider with given defaults.
        """
        self._configFileName = None
        if defaults is None:
            self._defaults = ConfigDict()
        else:
            self._defaults = ConfigDict(copy.deepcopy(defaults))
        self.importedFiles = []
        self.includedFiles = []
        self.missingFiles = []

    def getDefaults(self):
        """
        Return defaults.
        """
        return self._defaults

    def setDefaults(self, defaults):
        """
        Change defaults.
        """
        self._defaults = ConfigDict(copy.deepcopy(defaults))

    def getConfigFileName(self):
        """
        Return current configuration file path and name.
        """
        return self._configFileName

    def setConfigFileName(self, configFileName):
        """
        Change configuration file path and name for next load operations.
        """
        self._configFileName = configFileName
        if self._configFileName:
            self._configFileName = os.path.abspath(configFileName)

    def hasErrors(self):
        """
        Return true if last load operation encountered any errors.
        """
        return False

    def loadConfig(self):
        """
        Load the configuration, return a dictionary of settings.
        """
        return self._defaults


class Config(object):

    def __init__(self, provider=None):
        if not provider:
            self._provider = ConfigProvider()
        else:
            self._provider = provider
        self._updating = False
        self._beforeResetHook = None
        self._afterResetHook = None
        self._preUpdateHooks = []
        self._postUpdateHooks = []
        self.reset()

    def __setattr__(self, attr, value):
        if "_data" in self.__dict__ and attr in self.__dict__["_data"]:
            self._data[attr] = value
        else:
            self.__dict__[attr] = value

        # So as not to cause a flurry of updates, don't mark ourselves
        # dirty when the attribute begins with an underscore
        if not attr.startswith("_"):
            self.__dict__["_dirty"] = True

    _dirty = False
    _data = ()

    def __getattr__(self, attr):
        if self._dirty:
            self.update()

        if attr in self._data:
            return self._data[attr]
        raise AttributeError(attr)

    def __hasattr__(self, attr):
        return attr in self._data

    def __str__(self):
        return str(self._data)

    def get(self, attr, defaultValue):
        parts = attr.split(".")
        lastDict = self._data
        for part in parts[:-1]:
            if part not in lastDict:
                lastDict[attr] = ConfigDict()
            lastDict = lastDict.__getattr__(part)
        configItem = parts[-1]

        if configItem in lastDict:
            return lastDict[configItem]
        else:
            lastDict[configItem] = defaultValue
            return defaultValue

    def addResetHooks(self, before, after):
        """
        Hooks for preserving config across reload( ) + reset( )

        Each hook will be passed the config data; whatever the before hook
        returns will be passed as the second arg to the after hook.
        """
        self._beforeResetHook = before
        self._afterResetHook = after

    def addPreUpdateHooks(self, hooks):
        self._preUpdateHooks.extend(hooks)

    def addPostUpdateHooks(self, hooks):
        self._postUpdateHooks.extend(hooks)

    def getProvider(self):
        return self._provider

    def setProvider(self, provider):
        self._provider = provider
        self.reset()

    def setDefaults(self, defaults):
        self._provider.setDefaults(defaults)
        self.reset()

    def updateDefaults(self, items):
        mergeData(self._provider.getDefaults(), items)
        self.update(items)

    def update(self, items=None, reloading=False):
        if self._updating:
            return
        self._updating = True

        if not isinstance(items, ConfigDict):
            items = ConfigDict(items)

        # Call hooks
        for hook in self._preUpdateHooks:
            hook(self._data, items, reloading=reloading)
        mergeData(self._data, items)
        for hook in self._postUpdateHooks:
            hook(self._data, reloading=reloading)

        self._updating = False
        self._dirty = False
        self._cachedSyncToken = None

    def load(self, configFile):
        self._provider.setConfigFileName(configFile)
        configDict = self._provider.loadConfig()
        if not self._provider.hasErrors():
            self.update(configDict)
        else:
            raise ConfigurationError("Invalid configuration in %s"
                                     % (self._provider.getConfigFileName(),))

    def reload(self):
        configDict = self._provider.loadConfig()
        if not self._provider.hasErrors():
            if self._beforeResetHook:
                # Give the beforeResetHook a chance to stash away values we want
                # to preserve across the reload( )
                preserved = self._beforeResetHook(self._data)
            else:
                preserved = None
            self.reset()
            if preserved and self._afterResetHook:
                # Pass the preserved data back to the afterResetHook
                self._afterResetHook(self._data, preserved)
            self.update(configDict, reloading=True)
        else:
            raise ConfigurationError(
                "Invalid configuration in %s"
                % (self._provider.getConfigFileName(), ))

    def reset(self):
        self._data = ConfigDict(copy.deepcopy(self._provider.getDefaults()))
        self._dirty = True
        self._syncTokenKeys = []
        self._cachedSyncToken = None

    def getKeyPath(self, keyPath):
        """
        Allows the getting of arbitrary nested dictionary keys via a single
        dot-separated string.  For example, getKeyPath(self, "foo.bar.baz")
        would fetch parent["foo"]["bar"]["baz"].  If any of the keys don't
        exist, None is returned instead.

        @param keyPath: a dot-delimited string specifying the path of keys to
            traverse
        @type keyPath: C{str}
        @return: the value at keyPath
        """
        parent = self
        parts = keyPath.split(".")
        for part in parts[:-1]:
            child = parent.get(part, None)
            if child is None:
                return None
            parent = child
        return parent.get(parts[-1], None)

    def addSyncTokenKey(self, keyPath):
        """
        Indicates the specified key should be taken into account when generating
        the sync token.  Also invalidates the (possibly) cached syncToken.

        @param keyPath: a dot-delimited string specifying the path of keys to
            traverse
        @type keyPath: C{str}
        """
        if keyPath not in self._syncTokenKeys:
            self._syncTokenKeys.append(keyPath)
        self._cachedSyncToken = None

    def syncToken(self):
        """
        Iterates the previously registered keys (sorted, so the order in which
        the keys were registered doesn't affect the hash) and generates an MD5
        hash of the combined values.  The hash is cached, and is invalidated
        during a reload or if invalidateSyncToken is called.

        @return: the sync token
        @rtype: C{str}
        """
        if self._cachedSyncToken is None:
            pieces = []
            self._syncTokenKeys.sort()
            for key in self._syncTokenKeys:
                value = self.getKeyPath(key)
                if value is None:
                    value = ""
                pieces.append(key + ":" + str(value))
            whole = "|".join(pieces)
            self._cachedSyncToken = hashlib.md5(whole).hexdigest()
        return self._cachedSyncToken

    def invalidateSyncToken(self):
        """
        Invalidates the cached copy of the sync token.
        """
        self._cachedSyncToken = None

    def joinToken(self, dataToken):
        """
        Joins the config sync token with the dataToken.  If EnableConfigSyncToken
        is False, the original dataToken is just returned
        """
        if self.EnableConfigSyncToken:
            configToken = self.syncToken()
            return "{}/{}".format(dataToken, configToken)
        else:
            return dataToken


def mergeData(oldData, newData):
    """
    Merge two ConfigDict objects; oldData will be updated with all the keys
    and values from newData
    @param oldData: the object to modify
    @type oldData: ConfigDict
    @param newData: the object to copy data from
    @type newData: ConfigDict
    """
    for key, value in newData.iteritems():
        if isinstance(value, dict):
            if key in oldData:
                assert isinstance(oldData[key], ConfigDict), \
                    "%r in %r is not a ConfigDict" % (oldData[key], oldData)
            else:
                oldData[key] = {}
            mergeData(oldData[key], value)
        else:
            oldData[key] = value


def fullServerPath(base, path):
    if type(path) is str:
        return os.path.join(base, path) if path and path[0] not in ('/', '.',) else path
    else:
        return path

config = Config()
