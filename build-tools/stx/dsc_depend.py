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
# Copyright (C) 2021-2022 WindRiver Corporation
#

'''
Scan a set of dsc files and get their build order
-)  Each deb can only be built from one single source package.
-)  The build dependency relationship only occurs in this set of source packages,
      any debs not built from these source packages are always available.
-)  For the build dependency string:
      Ignore domains: <cross>, version requirements, "|"
-)  Circular dependencies are dealt via config file. Any circular dependency not listed
      in the config file is forbidden, once detected, an exception will be raised.
'''

import apt
import copy
import os
import re
import shutil

# Debian repository. You can also choose a nearby mirror site, see web page below:
# https://www.debian.org/mirror/list
mirror_0 = 'http://deb.debian.org/debian/ bullseye main contrib'
mirror_1 = 'http://security.debian.org/debian-security bullseye-security main contrib'
mirrors = [mirror_0, mirror_1]
apt_rootdir = '/tmp/dsc_depend'
DEFAULT_CIRCULAR_CONFIG = os.path.join(os.environ.get('MY_BUILD_TOOLS_DIR'), 'stx/circular_dep.conf')


def get_aptcache(rootdir):
    '''
    `apt update` for specified Debian repositories.
    '''
    try:
        if os.path.exists(rootdir):
            if os.path.isdir(rootdir):
                shutil.rmtree(rootdir)
            else:
                os.remove(rootdir)

        os.makedirs(rootdir + '/etc/apt')
        f_sources = open(rootdir + '/etc/apt/sources.list', 'w')
        for mirror in mirrors:
            f_sources.write('deb [trusted=yes] ' + mirror + '\n')
        f_sources.close()
    except Exception as e:
        print(e)
        raise Exception('APT root dir build error')
    try:
        apt_cache = apt.Cache(rootdir=rootdir)
        ret = apt_cache.update()
    except Exception as e:
        print(e)
        raise Exception('APT update failed')
    if not ret:
        raise Exception('APT update error')
    apt_cache.open()
    return apt_cache


def get_direct_depends(pkg_name, aptcache):
    '''
    Get direct runtime depend packages of a binary package
    '''
    pkgs_set = set()
    # For package doesn't exist in apt cache, it must belong to StarlingX.
    # The relationship of StarligX's packages will be scaned by other method
    # like scan_meta_info...
    if pkg_name not in aptcache.keys():
        return pkgs_set

    pkg = aptcache[pkg_name]
    # No package version provided, just use the 'candidate' one as 'i'
    for i in pkg.candidate.dependencies:
        [pkgs_set.add(j.name) for j in i]
    return pkgs_set


def get_runtime_depends(bin_pkg_set, aptcache):
    '''
    Get all runtime depend packages of a bundle of packages
    '''
    pkgs_set = bin_pkg_set.copy()
    # Now, pkgs_t0 is a set of packages need to be checked
    pkgs_t0 = pkgs_set.copy()
    while True:
        pkgs_t1 = set()
        # pkgs_t0 contains all packages not cheked, for each packages in it,
        # find their 'depen_on' packages and insert into pkgs_t1
        for pkg in pkgs_t0:
            pkgs_t1 = pkgs_t1.union(get_direct_depends(pkg, aptcache))
        # Get packages do not exist in pkgs_set, store them in pkgs_t0
        pkgs_t0 = pkgs_t1 - pkgs_set
        # No new package, pkgs_set is alreay complete
        if not pkgs_t0:
            return pkgs_set
        pkgs_set = pkgs_set.union(pkgs_t1)


def scan_meta_info(meta_info):
    '''
    Scan meta data of source packages, get relationships between them.
        meta_info = [src_build_bin, src_depend_on_bin]
        src_build_bin/src_depend_on_bin = {src:{bin,bin}, src:{bin,bin} ...}
        src_build_bin: binary packages build from the source package
        src_depend_on_bin: binary packages the source package build depend on
    '''
    depend_on = dict()
    depend_by = dict()

    assert len(meta_info) == 2
    assert bool(meta_info[0])
    assert meta_info[0].keys() == meta_info[1].keys()

    # Construct dictionary 'src' from 'meta_info[0]'
    src = dict()
    for src_pkg, bin_pkg_set in meta_info[0].items():
        for bin_pkg in bin_pkg_set:
            src[bin_pkg] = src_pkg

    # Here we have "meta_info[1]" and "src"
    # Construct dictionary depend_on and depend_by.
    for dsc, deb_list in meta_info[1].items():
        src_set = {src.get(deb) for deb in deb_list if deb in src.keys()}
        depend_on[dsc] = src_set

    # Construct dictionary "depend_by" from depend_on
    for key, value in depend_on.items():
        depend_by[key] = set()
    for key, value in depend_on.items():
        for pkg_by in value:
            depend_by.get(pkg_by).add(key)

    # Now, both depend_on and depend_by accomplished.
    return depend_on, depend_by


# Class used to deal with circular dependency source packages
class Circular_dsc_order():
    '''
    Manage the build order of a set of circular dependency source packages.
    The build order is defined by input parameter, strictly.
    '''
    def __init__(self, circular_meta_info, logger):
        '''
        circular_meta_info = [set, list]
        circular_meta_info[0] defines all related source packages
        circular_meta_info[1] defines the build order of these packages

        Packages in circular group should never build depend on packages out of
        the group. In other words, all source packages they build depend on
        out of the grop should already been build.
        '''
        if circular_meta_info[0] != set(circular_meta_info[1]):
            logger.error('Input meta data error, packages of pkg-set and build-order are not same:')
            logger.error(circular_meta_info)
            raise Exception('CIRCULAR META INFO ERROR')
        self.logger = logger
        self.pkgs = circular_meta_info[0]
        self.build_order = circular_meta_info[1]
        # self.next_index: Next pkg will be chosen/built.
        # -1: No more pkg can be built.
        # -2: Group build accomplished
        self.next_index = 0
        # The index of the building package. -1 means no package in build stage.
        self.building_index = -1

    def get_build_able_pkg(self):
        # Get packages can be built. Currently, CIRCULAR group does not support parallel build.
        if self.building_index >= 0:
            self.logger.info('Previous package still in building stage...')
            return None
        if self.next_index < 0:
            self.logger.debug('Circular group, no more package need to build')
            return None

        self.building_index = self.next_index
        self.next_index += 1
        if self.next_index == len(self.build_order):
            self.logger.debug('Circular group will build the last package')
            self.next_index = -1

        return [self.build_order[self.building_index]]

    def pkg_accomplish(self, pkg_name):
        # A package build OK. Building_num set to -1 thus no package in building stage
        if pkg_name != self.build_order[self.building_index]:
            self.logger.error('Circular group, %s does not in building stage' % pkg_name)
            return False
        self.building_index = -1
        if self.next_index == -1:
            self.next_index = -2
        return True

    def pkg_fail(self, pkg_name):
        # A package build failed.
        if pkg_name != self.build_order[self.building_index]:
            self.logger.error('Circular group, %s is not in building stage' % pkg_name)
            return False
        self.building_index = -1
        if -1 == self.next_index:
            self.next_index = len(self.build_order) - 1
        else:
            self.next_index -= 1

        return True

    def get_state(self):
        # Get group status.
        pkg_state = {'pkg_count': len(self.pkgs),
                     'build_count': len(self.build_order),
                     'building_index': self.building_index,
                     'next_index': self.next_index}
        self.logger.info('%d packages in current group' % len(self.pkgs))
        self.logger.info('%d packages need to be built' % len(self.build_order))
        self.logger.info('The next number to be built is %d' % self.next_index)
        return pkg_state

    def group_accomplished(self):
        # True if all packages build accomplished, or False
        if self.next_index == -2:
            return True
        return False


class Simple_dsc_order():
    '''
    Manage the build order of a set of source packages, without circular dependency.
    '''
    def __init__(self, meta_info, logger):
        '''
        Construct the build relationship of all related source packages
        meta_info = [dict, dict]
        meta_info[0] defines binary packages can be built from a source package
        meta_info[1] defines binary packages that depend on by a source package
        '''
        self.logger = logger
        self.depend_on, self.depend_by = scan_meta_info(meta_info)
        self.wait_on = dict()

        self.wait_on = copy.deepcopy(self.depend_on)
        self.prio = dict()
        self.__set_priority()

        # Init build_able_pkg and dictionary d_on
        self.build_able_pkg = dict()
        self.wait_on = copy.deepcopy(self.depend_on)
        for key in list(self.wait_on.keys()):
            if not self.wait_on[key]:
                self.build_able_pkg[key] = self.prio[key]
                self.wait_on.pop(key)

        # Init statistical data
        self.count = dict()
        self.count['building'] = 0
        self.count['accomplished'] = 0
        self.count['pkg'] = len(self.depend_on)
        self.count['can_build'] = len(self.build_able_pkg)
        self.count['wait'] = len(self.wait_on)
        assert self.count['pkg'] == \
               self.count['can_build'] + self.count['wait']

    def __depth_t(self, node, dependencies, chain):
        '''
        Search the dependency tree. Once circular dependency detected, dump it.
        '''
        if node in chain:
            self.logger.error('Dependency error!')
            start = False
            for dsc in chain:
                if dsc == node:
                    start = True
                if start is True:
                    self.logger.error('%s build depend on ' % dsc)
            self.logger.error('%s' % node)
            raise Exception('UNEXPECTED CIRCULAR DEPENDENCY.')
        chain.append(node)
        if node in list(dependencies.keys()) and dependencies[node]:
            for nd in dependencies[node]:
                self.__depth_t(nd, dependencies, chain)
        else:
            chain.pop()

    def __set_priority(self):
        '''
        Based on build relationships, calculate the priority value of each
        source package. Once circular dependency find, dump all related source
        packages and raise an exception.
        '''

        # Init dictionary prio, set to 10, for possible optimization later
        for key in self.wait_on:
            self.prio[key] = 10

        # Calculate priority of each dsc based on their build relationships
        tmp_d_by = copy.deepcopy(self.depend_by)
        tmp_d_on = copy.deepcopy(self.depend_on)
        # Each circular should shrink at least one package, or raise exception.
        # OP:
        # 1, Find package that build depend by nothing for example P_A. Here
        #    P_A is a top level source package that no other package build
        #    depend on it;
        # 2, For packages that P_A build depend on, like P_B and P_C, Add
        #    P_A's priority value to P_B and P_C's priority value. Remove P_A
        #    from P_B and P_C's depend on package set;
        # 3, Remove P_A from the whole package set. If no P_A find in any
        #    circular, there must be circular dependency.
        while tmp_d_on:
            shrink = False
            for key in list(tmp_d_by.keys()):
                self.logger.debug('%s : %s' % (key, tmp_d_by[key]))
                if not tmp_d_by[key]:
                    if tmp_d_on[key]:
                        for pkg in self.wait_on[key]:
                            self.prio[pkg] += self.prio[key]
                            tmp_d_by[pkg].remove(key)
                    tmp_d_on.pop(key)
                    tmp_d_by.pop(key)
                    shrink = True
            # circular dependency detected,dump it and raise an exception.
            if not shrink:
                chain = []
                for node in list(tmp_d_on.keys()):
                    self.__depth_t(node, tmp_d_on, chain)

    def __dump_dependency(self):
        # dump the build depended of all source packages. Debug/develop only
        self.logger.debug('%d relationship of DEPEND_ON' % len(self.depend_on))
        for key, value in self.depend_on.items():
            if not value:
                self.logger.debug('NOTHING')
            else:
                for pkg in value:
                    self.logger.debug('%s %d' % (pkg, self.prio[pkg]))
        self.logger.debug('%d relationship of DEPEND_BY' % len(self.depend_by))
        for key, value in self.depend_by.items():
            self.logger.debug('%s %d DEPEND-BY' % (key, self.prio[key]))
            if not value:
                self.logger.debug('NOTHING')
            else:
                for pkg in value:
                    self.logger.debug('%s %d' % (pkg, self.prio[pkg]))

    def __dump_build_able_pkg(self):
        # dump packages can be built now. Debug/develop only
        self.logger.info('Build-able source packages:')
        for key, value in self.build_able_pkg.items():
            if value < 0:
                self.logger.info('%s is building' % key)
            else:
                self.logger.info('%s can be built, prio is %d' % (key, value))
        return len(self.build_able_pkg)

    def get_build_able_pkg(self, count):
        '''
        Get packages can be built.
        Input: max number of packages want to get(0 < value < 100)
        Output: A list of source packages
        '''
        pkgs = []
        i = 0
        if count < 1 or count > 99:
            self.logger.warning('Need a positive integer smaller than 100')
            return None
        list_pkg = sorted(self.build_able_pkg.items(), key=lambda kv: (kv[1],
                          kv[0]), reverse=True)
        if len(list_pkg) == 0:
            self.logger.warning('No build-able package in list.')
            return None
        self.logger.debug('%d Build_able packages, try to get %d From them' % (len(list_pkg), count))
        while count > 0:
            # prio < 0 ==> package in building stage
            if i >= len(list_pkg) or list_pkg[i][1] < 0:
                self.logger.debug('No more packages can be built.')
                break
            pkg = list_pkg[i][0]
            self.logger.debug(pkg)
            self.build_able_pkg[pkg] -= 10000
            pkgs.append(pkg)
            self.count['can_build'] -= 1
            self.count['building'] += 1
            i += 1
            count -= 1
        self.logger.debug('%d packages will be built' % i)
        self.logger.debug(pkgs)
        return pkgs

    def pkg_accomplish(self, pkg_name):
        '''
        Announce a source package build accomplished
        '''
        if self.build_able_pkg.get(pkg_name) and self.build_able_pkg[pkg_name] < 0:
            self.build_able_pkg.pop(pkg_name)
            self.count['accomplished'] += 1
            self.count['building'] -= 1
        else:
            self.logger.warning('%s not in building stage.' % pkg_name)
            return False

        if self.depend_by[pkg_name]:
            for pkg in self.depend_by[pkg_name]:
                self.logger.debug('%s is depended by %s' % (pkg, pkg_name))
                self.logger.debug(self.wait_on[pkg])
                self.wait_on[pkg].remove(pkg_name)
                if not self.wait_on[pkg]:
                    self.logger.info('%s can be built.' % pkg)
                    self.build_able_pkg[pkg] = self.prio[pkg]
                    self.wait_on.pop(pkg)
                    self.count['can_build'] += 1
                    self.count['wait'] -= 1
        return True

    def pkg_fail(self, pkg_name):
        '''
        Announce a source package build failed
        '''
        if self.build_able_pkg.get(pkg_name) and self.build_able_pkg[pkg_name] < 0:
            # Mark it not in building stage
            self.build_able_pkg[pkg_name] += 10000
            self.count['can_build'] += 1
            self.count['building'] -= 1
        else:
            self.logger.warning('%s not in building stage.' % pkg_name)

    def __dump_wait_chain(self):
        # Dump packages that can't be built now. Debug/develop only
        self.logger.info('%s packages are waiting for build depend packages.' % len(self.wait_on))
        for key, value in self.wait_on.items():
            self.logger.info('%s ==> %d' % (key, value))
        return len(self.wait_on)

    def get_state(self):
        '''
        Dump group state
        '''
        pkg_state = {'pkg_count': self.count['pkg'],
                     'pkg_wait': self.count['wait'],
                     'pkg_can_build': self.count['can_build'],
                     'pkg_building': self.count['building'],
                     'pkg_accomplished': self.count['accomplished']}
        assert self.count['pkg'] == (self.count['wait'] +
                                     self.count['can_build'] +
                                     self.count['building'] +
                                     self.count['accomplished'])
        self.logger.info('%d packages' % self.count['pkg'])
        self.logger.info('%d packages are waiting for build dependency' % self.count['wait'])
        self.logger.info('%d packages can be built, waiting for OBS' % self.count['can_build'])
        self.logger.info('%d packages are in building stage' % self.count['building'])
        self.logger.info('%d packages accomplished' % self.count['accomplished'])
        return pkg_state

    def group_accomplished(self):
        # True if all packages build accomplished, or False
        if self.count['pkg'] == self.count['accomplished']:
            return True
        return False


class Circular_break():
    '''
    Class used to deal with source packages that may have circular dependency
    1) separate source packages into different sets:
        Common: No circular dependency, build order defined by Simple_dsc_order
        Circular: packages of a circular dependency, build order defined by config file
    2) Let OBS get correct packages
    '''
    def __init__(self, logger, meta_info, circular_conf_file=None):
        '''
        package_grp: seperate all packages in groups, define them as dictionaries:
        package_grp: [group_0, group_1, group_2...]
        group_x:{
                'grp_type': 'Simple'/'Circular'
                'grp_meta_info': A list, base information of the group
                'grp_order': Object of Circular_dsc_order/Simple_dsc_order
                'grp_state': A dictionary, build state of the group
                }
        Groups must be built one by one, no parallel build between groups.
        '''
        self.package_grp = []

        self.meta_info = meta_info.copy()
        self.logger = logger
        # The index of current group(in building stage)
        # -1: build not started; -2: build accomplished
        self.current_group_index = -1
        # depend_on/by: {dsc: [dsc, dsc ...], ...}
        self.depend_on, self.depend_by = scan_meta_info(meta_info)
        self.circular_conf = []
        if circular_conf_file:
            self.__get_circular_conf(circular_conf_file)
        self.__grouping(meta_info)
        self.current_group_index = 0
        self.package_grp[0]['grp_state']['build_state'] = 'building'

    def __get_circular_conf(self, circular_conf_file):
        '''
        Read file "circular_conf_file" and store circular info into self.circular_conf
        self.circular_conf[n][0]: set. A set of packages construt a circular dependency
        self.circular_conf[n][1]: list. Build order of those packages above
        '''
        src_set = 'SRC SET:'
        build_order = 'BUILD ORDER:'
        if not os.access(circular_conf_file, os.R_OK):
            self.logger.error('Circular conf file %s not read-able.' % circular_conf_file)
            return
        try:
            f_circular_conf = open(circular_conf_file, 'r')
        except Exception as e:
            print(e)
            raise Exception('Circular config file open failed')
        # scan the circular config file
        want_set = True
        for line in f_circular_conf:
            line = line.strip().split('#')[0]
            if line.startswith(src_set):
                if not want_set:
                    self.logger.error('Want key word "BUILD ORDER:": %s' % line)
                    raise Exception('CIRCULAR CONFIG FILE ERROR')
                meta_str = line[len(src_set):]
                srcs = set(meta_str.strip().split(' '))
                want_set = False
            elif line.startswith(build_order):
                if want_set:
                    self.logger.error('Want key word "SRC SET:": %s' % line)
                    raise Exception('CIRCULAR CONFIG FILE ERROR')
                meta_str = line[len(build_order):]
                src_list = meta_str.strip().split(' ')
                if set(src_list) != srcs or not srcs:
                    self.logger.error('SRC packages must align with the build order, must not be empty: %s' % line)
                    raise Exception('CIRCULAR CONFIG FILE ERROR')
                want_set = True
                self.circular_conf.append([srcs, src_list])

    def __get_pkg_dependency(self, meta_info, pkgs):
        # Like scan_meta_info but only check packages in "pkgs"
        self.logger.debug('Scan pkgs meta info for Simple dependency.')
        tmp_build_bin = dict()
        tmp_depend_on_b = dict()
        for pkg in pkgs:
            tmp_build_bin[pkg] = meta_info[0][pkg].copy()
            tmp_depend_on_b[pkg] = meta_info[1][pkg].copy()
        return scan_meta_info([tmp_build_bin, tmp_depend_on_b])

    def __get_simple_group(self, pkgs, meta_info):
        '''
        Get a simple group from a set of source packages. Simulate real build
        process and select all build-able packages one by one. Construct a
        'Simple_group' dictionary and append it into self.package_grp.
        pkgs: The original group of the source packages
        meta_info: Meta_info of all relate packages. Packages in "pkgs" must
                   exist in "meta_info", but packages in "meta_infp" may not
                   exist in "pkgs"
        Return a set of packages, not been selected into the Simple group.
        '''
        ret_pkgs = pkgs.copy()
        dep_on = self.__get_pkg_dependency(meta_info, pkgs)[0]
        group = set()
        # simulate the real build process, get build-able packages one by one.
        while len(ret_pkgs) != 0:
            tmp_set = ret_pkgs.copy()
            find_pkg = False
            for pkg in tmp_set:
                # If it depends on nothing, it can be built now.
                if not dep_on[pkg]:
                    find_pkg = True
                    group.add(pkg)
                    ret_pkgs.remove(pkg)
                    # Remove this very package from other package's 'dep_on'
                    # for it has already been build.
                    for package in ret_pkgs:
                        if pkg in dep_on[package]:
                            dep_on[package].remove(pkg)
            if not find_pkg:
                break
        # no build-able package find, just return
        if len(ret_pkgs) == len(pkgs):
            return ret_pkgs

        # Construct the Simple package set data structure
        tmp_build_bin = dict()
        tmp_depend_on_b = dict()
        for pkg in group:
            tmp_build_bin[pkg] = meta_info[0][pkg].copy()
            tmp_depend_on_b[pkg] = meta_info[1][pkg].copy()
        pkg_group = Simple_dsc_order([tmp_build_bin, tmp_depend_on_b], self.logger)
        group_dict = dict()
        group_dict['grp_type'] = 'Simple'
        group_dict['grp_meta_info'] = [tmp_build_bin.copy(), tmp_depend_on_b.copy()]
        group_dict['grp_order'] = pkg_group
        group_dict['grp_state'] = dict()
        group_dict['grp_state']['build_state'] = 'crude'
        group_dict['grp_state']['num_pkg'] = len(group)
        group_dict['grp_state']['num_build'] = len(group)
        group_dict['grp_state']['num_accomplish'] = 0
        # Append it at the end of the self.package_grp.
        self.package_grp.append(group_dict)
        return ret_pkgs

    def __get_pkgname_dependency(self, meta_info, pkgs):
        '''
        Like scan_meta_info but only check packages in "pkgs", transmit dsc's
        pathname to source package name.
        '''
        self.logger.debug('Scan pkgs meta info for circular dependency.')
        tmp_build_bin = dict()
        tmp_depend_on_b = dict()
        for pkg in pkgs:
            tmp_build_bin[os.path.basename(pkg).split('_')[0]] = meta_info[0][pkg].copy()
            tmp_depend_on_b[os.path.basename(pkg).split('_')[0]] = meta_info[1][pkg].copy()
        return scan_meta_info([tmp_build_bin, tmp_depend_on_b])

    def __get_circular_group(self, pkgs, meta_info):
        '''
        Get a circular group from a set of source packages. Scan the self.circular_conf
        get the fist set of packages that not build depend on others in "pkgs"
        Construct this set of packages as a 'Circular_group' dictionary and append
        it into self.package_grp.
        pkgs: The original group of the source packages
        meta_info: Meta_info of all relate packages. Packages in "pkgs" must
                   exist in "meta_info", but packages in "meta_infp" may not
                   exist in "pkgs"
        Return a set of packages, not been selected into the circular group.
        checked_set: a set, each object in it.
        These set are subset of 'pkgs' but can't be built now.
        '''
        checked_set = []
        # dict_pkg_meta: {'xyz':'/a/b/xyz.dsc', ...} Or {'xyz':'xyz', ...}
        dict_pkg_meta = dict()
        ret_pkgs = pkgs.copy()
        # construct the "dep_on" and "dep_by" of "pkgs
        for pkg in pkgs:
            # Get source package name from 'pkg'
            # Here 'pkg' maybe source package name, or the pathname of a dsc file
            dict_pkg_meta[os.path.basename(pkg).split('_')[0]] = pkg
        # Here the key/value of dep_on is source package NAME, not dsc's pathname
        dep_on = self.__get_pkgname_dependency(meta_info, pkgs)[0]
        # Scan self.circular_conf to find a build-able package set in it
        for circular_pkgs in self.circular_conf:
            # circular_pkgs : [set, list]
            # checked_set: a serail of package-groups that already been checked
            # (subset of pkgs but can't be built now). If the new set(pkgs) is
            # subset of an checked_set, it should be ignored.
            # For example: pkgs contains {a,b,c,d} but 'b' can't be built now,
            # So we add {a,b,c,d} into the checked_set to mark it can't be built.
            # Later, there is a set in circular_conf {a,d}, for {a,b,c,d} had already
            # been checked and failed, so wee shouldn't check {a,d} any more.
            superset_checked = False
            for checked in checked_set:
                if checked.issuperset(circular_pkgs[0]):
                    superset_checked = True
                    break
            if superset_checked:
                checked_set.append(circular_pkgs[0])
                continue

            # circular_pkgs[0]: a set of source package name, all of them exist in
            # "pkgs". Check to see if they can build now.
            if circular_pkgs[0].issubset(set(dep_on.keys())):
                dep_on_set = set()
                for pkg in circular_pkgs[0]:
                    dep_on_set = dep_on_set.union(dep_on[pkg])
                # A set of circular dependency packages should only depend on themselves.
                # Or wait a while for other packages.
                if dep_on_set == circular_pkgs[0]:
                    # find build-able packages, remove them from ret_pkgs
                    for pkg in circular_pkgs[0]:
                        ret_pkgs.remove(dict_pkg_meta[pkg])
                    # Transmit package_name to real meta_info
                    real_meta_info = [set(), list()]
                    for pkg in circular_pkgs[0]:
                        real_meta_info[0].add(dict_pkg_meta[pkg])
                    for pkg in circular_pkgs[1]:
                        real_meta_info[1].append(dict_pkg_meta[pkg])
                    group_order = Circular_dsc_order(real_meta_info, self.logger)
                    group_dict = dict()
                    group_dict['grp_type'] = 'Circular'
                    group_dict['grp_meta_info'] = real_meta_info.copy()
                    group_dict['grp_order'] = group_order
                    group_dict['grp_state'] = dict()
                    group_dict['grp_state']['build_state'] = 'crude'
                    group_dict['grp_state']['num_pkg'] = len(circular_pkgs[0])
                    group_dict['grp_state']['num_build'] = len(circular_pkgs[1])
                    group_dict['grp_state']['num_accomplish'] = 0
                    self.package_grp.append(group_dict)
                    return ret_pkgs
        return ret_pkgs

    def __depth_t(self, node, dependencies, circular_chain):
        # Search the dependency tree. Once a circular dependency detected, raise exception.
        if node in circular_chain:
            while node is not circular_chain[0]:
                circular_chain.remove(circular_chain[0])
            raise Exception('CIRCULAR DEPENDENCY DETECTED.')
        circular_chain.append(node)
        if node in set(dependencies.keys()) and dependencies[node]:
            for nd in dependencies[node]:
                self.__depth_t(nd, dependencies, circular_chain)
                circular_chain.pop()

    def __get_all_deps(self, node, depends):
        # Get all packages depend on/by(Based on parameter "depends") "node"
        if not node:
            self.logger.error('No node sepcified.')
        pkgs_set = set(depends[node])
        pkgs_t0 = pkgs_set.copy()
        while True:
            pkgs_t1 = set()
            for pkg in pkgs_t0:
                pkgs_t1 = pkgs_t1.union(depends[pkg])
            pkgs_t0 = pkgs_t1 - pkgs_set
            if not pkgs_t0:
                return pkgs_set
            pkgs_set = pkgs_set.union(pkgs_t1)
        return pkgs_set

    def __get_one_circular_grp(self, depends):
        # Try to find a circular dependency
        find_circular = False
        circular_chain = list()
        depend_on = depends.copy()
        depend_by = dict()
        for node in depend_on.keys():
            depend_by[node] = set()
        for node, pkgs in depend_on.items():
            for pkg in pkgs:
                depend_by[pkg].add(node)
        try:
            for node in list(depend_on.keys()):
                circular_chain.clear()
                self.__depth_t(node, depend_on, circular_chain)
        except Exception as e:
            # Find a circular group
            self.logger.debug('%s' % e)
            find_circular = True
        if not find_circular:
            # self.logger.debug('No circular dependency found')
            return set()
        # Find all packages belong to this circular group.
        # For any package, in case both its depend_on and depend_by packages
        # contains any package of this circular group, this package is also part of
        # this circular group.
        new_pkgs = set()
        for pkg in depend_on.keys():
            if pkg not in circular_chain:
                deps_on = self.__get_all_deps(pkg, depend_on)
                deps_by = self.__get_all_deps(pkg, depend_by)
                dep_on_and_circular = deps_on & set(circular_chain)
                dep_by_and_circular = deps_by & set(circular_chain)
                if dep_on_and_circular and dep_by_and_circular:
                    new_pkgs.add(pkg)
        return new_pkgs.union(set(circular_chain))

    def __dump_circular_dep(self, pkgs, meta_info):
        '''Unexpected circular dependency detected. Find and dump them all.'''
        checking_meta_info = [dict(), dict()]
        for index in range(0, 2):
            for pkg in pkgs:
                checking_meta_info[index][pkg] = meta_info[index][pkg].copy()
        depend_on, depend_by = scan_meta_info(checking_meta_info)
        # Find and dump all circular dependency
        while True:
            # Get one circular dependency
            pkgs = self.__get_one_circular_grp(depend_on)
            if not pkgs:
                break
            self.logger.error('Circular dependency: %s' % pkgs)
            # remove related pakages from current packge set("depend_on")
            for node in pkgs:
                depend_on.pop(node)
            for pkg in depend_on.keys():
                depend_on[pkg] = depend_on[pkg] - pkgs
            # refresh "depend_by" based on current "depend_on"
            depend_by.clear()
            for node in depend_on.keys():
                depend_by[node] = set()
            for node, packages in depend_on.items():
                for pkg in packages:
                    depend_by[pkg].add(node)

    def __grouping(self, meta_info):
        # init the whole set of all related source packages(set)
        pkgs = set(meta_info[0].keys())
        while(len(pkgs) != 0):
            orig_len = len(pkgs)
            pkgs = self.__get_simple_group(pkgs, meta_info)
            if not pkgs:
                return
            pkgs = self.__get_circular_group(pkgs, meta_info)
            if orig_len == len(pkgs):
                self.__dump_circular_dep(pkgs, meta_info)
                self.logger.error('There are unexpected circular dependency.')
                raise Exception('UNEXPECTED CIRCULAR DEPENDENCY.')

    def get_build_able_pkg(self, count):
        '''
        Get packages to be built. Return a list of source packages
        '''
        if count <= 0:
            self.logger.error('Input count %d error.' % count)
            return None
        if self.current_group_index == -2:
            self.logger.warning('Build accomplished, no more package need to be built')
            return None
        if self.current_group_index == -1:
            self.logger.info('Build started.')
            self.current_group_index = 0
        pkg_group = self.package_grp[self.current_group_index]
        # Get pkgs from current group
        if self.package_grp[self.current_group_index]['grp_type'] == 'Simple':
            # Simple group
            return pkg_group['grp_order'].get_build_able_pkg(count)
        # Circular group
        return pkg_group['grp_order'].get_build_able_pkg()

    def pkg_accomplish(self, pkg_name):
        '''
        Announce a source package build accomplished
        '''
        # First step, get current group
        if self.current_group_index == -2:
            self.logger.warning('Build accomplished, no more package need to be built')
            return
        if self.current_group_index == -1:
            self.logger.warning('Build not started, pkg accomplished?')
            return
        pkg_group = self.package_grp[self.current_group_index]
        if not pkg_group['grp_order'].pkg_accomplish(pkg_name):
            return
        pkg_group['grp_state']['num_accomplish'] += 1
        if pkg_group['grp_order'].group_accomplished():
            if pkg_group['grp_state']['num_accomplish'] != pkg_group['grp_state']['num_build']:
                self.logger.warning('Previous group not build enough pkgs')
            pkg_group['grp_state']['build_state'] = 'accomplish'
            # All groups build accomplished?
            if self.current_group_index == (len(self.package_grp) - 1):
                self.current_group_index = -2
            else:
                self.current_group_index += 1
                if self.package_grp[self.current_group_index]['grp_state']['build_state'] != 'crude':
                    self.logger.warning('Next group not crude.')
                self.package_grp[self.current_group_index]['grp_state']['build_state'] = 'building'

    def pkg_fail(self, pkg_name):
        '''
        Announce a source package build failed
        '''
        # First step, get current group
        if self.current_group_index == -2:
            self.logger.warning('Build accomplished, no more package need to be built')
            return None
        if self.current_group_index == -1:
            self.logger.warning('Build not started, pkg build failed?')
            return None
        pkg_group = self.package_grp[self.current_group_index]
        return pkg_group['grp_order'].pkg_fail(pkg_name)

    def get_state(self):
        '''
        Get the build state
        '''
        build_state = dict()
        pkg_num = build_num = acomplish_num = s_grp = l_grp = 0
        for grp in self.package_grp:
            pkg_num += grp['grp_state']['num_pkg']
            build_num += grp['grp_state']['num_build']
            acomplish_num += grp['grp_state']['num_accomplish']
            if grp['grp_type'] == 'Simple':
                s_grp += 1
            else:
                l_grp += 1
        build_state['pkg_num'] = pkg_num
        build_state['build_num'] = build_num
        build_state['acomplish_num'] = acomplish_num
        build_state['group_num'] = len(self.package_grp)
        build_state['simple_group_num'] = s_grp
        build_state['circular_group_num'] = l_grp
        return build_state


class Dsc_build_order(Circular_break):
    '''
    Manage the build order of a set of dsc files.
    '''

    def __init__(self, dsc_list, logger, circular_conf_file=DEFAULT_CIRCULAR_CONFIG):
        '''
        Construct the build relationship of all those dsc files in "dsc_list"
        '''
        self.logger = logger
        self.aptcache = get_aptcache(apt_rootdir)
        self.meta_info = [dict(), dict()]
        self.__scan_dsc_list(dsc_list)
        super().__init__(logger, self.meta_info, circular_conf_file)

    def __get_depends(self, depend_str):
        '''
        Get build depend packages from an input string
        Input: Build-Depends + Build-Depends-Indep of a dsc file
        Output: a set of build depend package name
        '''
        depends = set()
        self.logger.debug('%s' % depend_str)
        raw_depends = set(depend_str.replace('|', ',').replace(' ', '')
                          .split(','))
        for raw_pkg in raw_depends:
            if -1 != raw_pkg.find('<cross>'):
                continue
            pkg = re.sub(u"\\<.*?\\>|\\(.*?\\)|\\[.*?\\]", "", raw_pkg)
            if 0 != len(pkg):
                depends.add(pkg)
        return depends

    def __scan_dsc_file(self, list_line, build_bin, depend_on_b):
        '''
        Scan a dsc file and get its build relationship from domain "Binary",
        "Build-Depends", "Build-Depends-Arch" and "Build-Depends-Indep".
        Param:
           list_line: INPUT. a string, one single line of the dsc list file,
               should contain a dsc's path name.
           build_bin: INPUT/OUTPUT. a dictionary:
               build_bin["a.dsc"] = ['a1.deb', 'a2.deb'] means source package
               can build binary packages a1.deb and a2.deb.
           depend_on_b: INPUT/OUTPUT. a dictionary:
               depend_on_b["a.dsc"] = ['b.deb', 'c.deb'] means source package
               is build depend on binary package b.deb and c.deb.
        '''
        # remove empty line, comment string/lines
        dsc_name = list_line.strip().split('#')[0]
        if not dsc_name:
            return None
        if not dsc_name.endswith('dsc'):
            self.logger.error('%s: is not a dsc file.' % list_line)
            raise Exception('dsc list error, please check line: %s' % list_line)

        # open and read dsc file
        if not os.access(dsc_name, os.R_OK):
            self.logger.error('dsc file %s does not exist.' % dsc_name)
            raise Exception('dsc file %s does not exist' % dsc_name)
        dsc_f = open(dsc_name, 'r')
        # scan the dsc file, get Binary Build-Depends and Build-Depends-Indep
        build_depends_arch = build_depends_indep = build_depends = ''
        build = b_depends = ''
        for dsc_line in dsc_f:
            dsc_line = dsc_line.strip()
            if dsc_line.startswith('Binary:'):
                build = dsc_line[8:]
                self.logger.debug('%s build : %s' % (dsc_name, build))
            elif dsc_line.startswith('Build-Depends:'):
                build_depends = dsc_line[14:]
                self.logger.debug('%s build_depends : %s' % (dsc_name, build_depends))
            elif dsc_line.startswith('Build-Depends-Indep:'):
                build_depends_indep = dsc_line[21:]
                self.logger.debug('%s build_depends_indep : %s' % (dsc_name, build_depends_indep))
            elif dsc_line.startswith('Build-Depends-Arch:'):
                build_depends_arch = dsc_line[20:]
                self.logger.debug('%s build_depends_arch : %s' % (dsc_name, build_depends_arch))
        dsc_f.close()

        b_depends = build_depends
        if build_depends_indep:
            b_depends = b_depends + ', ' + build_depends_indep
        if build_depends_arch:
            b_depends = b_depends + ', ' + build_depends_arch
        # Store binary depend_on relationship in dictionary "depend_on_b"
        direct_depends = self.__get_depends(b_depends)
        depend_on_b[dsc_name] = get_runtime_depends(direct_depends, self.aptcache)

        # Deal with "Binary", binary deb build from the dsc, store in "src"
        build_list = build.replace(' ', '').split(',')
        # assert len(depend_on_b[dsc_name]) != 0
        assert len(build_list) != 0
        build_bin[dsc_name] = set(build_list)
        return None

    def __scan_dsc_list(self, dsc_list_file):
        build_bin = self.meta_info[0]
        depend_on_b = self.meta_info[1]

        if not os.access(dsc_list_file, os.R_OK):
            self.logger.error('dsc list file %s not read-able.' % dsc_list_file)
            return
        dsc_list = open(dsc_list_file, 'r')
        # scan the dsc list
        for list_line in dsc_list:
            self.__scan_dsc_file(list_line, build_bin, depend_on_b)
        dsc_list.close()


class Pkg_build(Circular_break):
    '''
    Choose packages need to be built and manage the build order of them.
    '''
    def __init__(self, meta_info, target_pkgs, logger, circular_conf_file=DEFAULT_CIRCULAR_CONFIG):
        '''
        meta_info = [dict, dict]
        meta_info[0] defines binary packages can be built from a source package
        meta_info[1] defines binary packages that depend on by a source package

        target_pkgs: a SET of source package that need to be built
        '''
        self.logger = logger
        self.meta_info = [dict(), dict()]
        self.aptcache = get_aptcache(apt_rootdir)
        self.__get_meta_info(meta_info, set(target_pkgs))
        super().__init__(logger, self.meta_info, circular_conf_file)

    def __depth_t(self, node, dependencies, set_pkgs):
        '''
        Search the dependency tree. Add dependencies[node] into "set_pkgs"
        '''
        if node in set_pkgs:
            return
        set_pkgs.add(node)
        if node in list(dependencies.keys()) and dependencies[node]:
            for nd in dependencies[node]:
                self.__depth_t(nd, dependencies, set_pkgs)

    def __get_build_pkgs(self, depend_on, target_pkgs, build_pkgs):
        '''
        Based on target packages and the build relationships, find all source
        package need to be built. Add them into "build_pkgs"
        '''
        for pkg in target_pkgs:
            self.__depth_t(pkg, depend_on, build_pkgs)

    def __get_meta_info(self, meta_info, target_pkgs):
        '''
        Construct meta_info for Circular_break
        '''
        if not target_pkgs.issubset(set(meta_info[0].keys())):
            self.logger.error('Target packages not in meta data.')
            raise Exception('TARGET PACKAGE NOT EXIST IN META DATA')
        for pkg in meta_info[1].keys():
            meta_info[1][pkg] = get_runtime_depends(meta_info[1][pkg], self.aptcache)
        depend_on, depend_by = scan_meta_info(meta_info)
        build_pkgs = set()
        self.__get_build_pkgs(depend_on, target_pkgs, build_pkgs)
        for pkg in build_pkgs:
            self.meta_info[0][pkg] = meta_info[0][pkg].copy()
            self.meta_info[1][pkg] = meta_info[1][pkg].copy()
