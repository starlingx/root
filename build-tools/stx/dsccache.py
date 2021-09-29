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


class DscCache():
    def __init__(self, logger, cache_file):
        self.logger = logger
        self.cache_file = cache_file

    def get_package_digest(self, package):
        if not os.path.exists(self.cache_file):
            self.logger.warn("dscCache:%s does not exist" % self.cache_file)
            return None

        with open(self.cache_file, 'rb') as fcache:
            dsc_cache = pickle.load(fcache)
            if package in dsc_cache.keys():
                return dsc_cache[package]
        return None

    def set_package_digest(self, package, checksum):
        dsc_cache = {}
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'rb') as fcache:
                dsc_cache = pickle.load(fcache)
                self.logger.debug("dscCache:Append or update %s" % package)
        else:
            self.logger.debug("dscCache:Not exist, need to create")

        dsc_cache[package] = checksum
        with open(self.cache_file, 'wb+') as fcache:
            pickle.dump(dsc_cache, fcache, pickle.HIGHEST_PROTOCOL)
        return True
