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


def get_pkg_by_deb(clue, debname, logger):
    try:
        with open(clue, 'rb') as fclue:
            try:
                debs = pickle.load(fclue)
                for pkgname, subdebs in debs.items():
                    if debname in subdebs:
                        return pkgname
            except (EOFError, ValueError, AttributeError, ImportError, IndexError, pickle.UnpicklingError) as e:
                logger.error(str(e))
                logger.warn(f"debs_entry:failed to load {clue}, return None")
    except IOError:
        logger.warn(f"debs_entry:{clue} does not exist")
    return None


def get_subdebs(clue, package, logger):
    try:
        with open(clue, 'rb') as fclue:
            try:
                debs = pickle.load(fclue)
                if package in debs.keys():
                    return debs[package]
            except (EOFError, ValueError, AttributeError, ImportError, IndexError, pickle.UnpicklingError) as e:
                logger.warn(f"debs_entry:failed to load {clue}, return None")
    except IOError:
        logger.warn(f"debs_entry:{clue} does not exist")
    return None


def set_subdebs(clue, package, debs, logger):
    debmap = {}
    try:
        with open(clue, 'rb') as fclue:
            try:
                debmap = pickle.load(fclue)
                logger.debug(f"debs_entry:loaded the debs clue {clue}")
            except (EOFError, ValueError, AttributeError, ImportError, IndexError, pickle.UnpicklingError) as e:
                logger.warn(f"debs_entry:failed to load {clue}, recreate it")
                os.remove(clue)
                debmap = {}
    except IOError:
        logger.debug(f"debs_entry:{clue} does not exist")

    debmap[package] = debs
    try:
        with open(clue, 'wb+') as fclue:
            pickle.dump(debmap, fclue, pickle.HIGHEST_PROTOCOL)
    except IOError:
        raise Exception(f"debs_entry:failed to write {clue}")

    return True
