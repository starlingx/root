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


def get_subdebs(clue, package, logger):
    if not os.path.exists(clue):
        logger.warn("debs_entry:debs clue %s does not exist" % clue)
        return None

    with open(clue, 'rb') as fclue:
        debs = pickle.load(fclue)
        if package in debs.keys():
            return debs[package]
        return None


def set_subdebs(clue, package, debs, logger):
    debmap = {}
    if os.path.exists(clue):
        with open(clue, 'rb') as fclue:
            debmap = pickle.load(fclue)
            logger.debug("debs_entry:loaded the debs clue %s" % clue)
    else:
        logger.debug("debs_entry:%s does not exist" % clue)

    debmap[package] = debs
    with open(clue, 'wb+') as fclue:
        pickle.dump(debmap, fclue, pickle.HIGHEST_PROTOCOL)

    return True
