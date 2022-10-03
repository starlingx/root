#!/usr/bin/python3

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright (C) 2021 Wind River Systems,Inc

import os
import pickle
import re


class DscCache():
    def __init__(self, logger, cache_file):
        self.logger = logger
        self.cache_file = cache_file

    def get_package(self, package):
        if not os.path.exists(self.cache_file):
            self.logger.warn("dscCache:%s does not exist" % self.cache_file)
            return None, None

        try:
            with open(self.cache_file, 'rb') as fcache:
                dsc_cache = pickle.load(fcache)
        except Exception as e:
            logger.error(str(e))
            logger.error("DscCache failed to open the cache file")
        else:
            if package in dsc_cache.keys():
                dsc_file = dsc_cache[package].split(':')[0]
                checksum = dsc_cache[package].split(':')[1]
                return dsc_file, checksum
        return None, None

    def get_package_re(self, package):
        if not os.path.exists(self.cache_file):
            self.logger.warn("dscCache:%s does not exist" % self.cache_file)
            return None, None

        try:
            with open(self.cache_file, 'rb') as fcache:
                dsc_cache = pickle.load(fcache)
        except Exception as e:
            logger.error(str(e))
            logger.error("DscCache failed to open the cache file")
        else:
            for pkg in dsc_cache.keys():
                ret = re.search(package, pkg)
                if not ret:
                    continue
                match_item = dsc_cache[pkg]
                self.logger.debug("dscCache: Matched item %s" % match_item)
                dsc_file = match_item.split(':')[0]
                checksum = match_item.split(':')[1]
                return dsc_file, checksum
        return None, None

    def set_package(self, package, checksum):
        dsc_cache = {}
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'rb') as fcache:
                dsc_cache = pickle.load(fcache)
                self.logger.debug("dscCache:Append or update %s" % package)
        else:
            self.logger.debug("dscCache:Not exist, need to create")

        if checksum:
            dsc_cache[package] = checksum
        else:
            del dsc_cache[package]

        with open(self.cache_file, 'wb+') as fcache:
            pickle.dump(dsc_cache, fcache, pickle.HIGHEST_PROTOCOL)
        return True

    def load(self, show=False):
        dsc_cache = None

        if not os.path.exists(self.cache_file):
            self.logger.warn("dscCache:%s does not exist" % self.cache_file)
            return None

        try:
            with open(self.cache_file, 'rb') as fcache:
                dsc_cache = pickle.load(fcache)
        except Exception as e:
            self.logger.error("Failed to load dsc cache: %s", str(e))

        if show and dsc_cache:
            for pdir, pval in dsc_cache.items():
                self.logger.debug("dscCache display: %s -> %s", pdir, pval)
            self.logger.debug("dscCache display: Total dscs count: %d", len(dsc_cache))

        return dsc_cache
