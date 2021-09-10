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
# Copyright (C) 2021 WindRiver Corporation
#

'''
Scan a set of dsc files and get their build order
-)  Each deb can only be build from one single source package.
-)  The build depend relationship only occurs in this set of source packages,
      any debs not build from these source packages are always available.
-)  For the build depend string:
      Ignore domains: <cross>, version requirements, "|"
-)  Cycle dependent is forbidden. For example, A depends on B, B depends on C
      while C depends on A. Once cycle dependent detected, exception will be
      raised.
'''

import copy
import os
import re


class Dsc_build_order():
    '''
    Class used to manage the build order of a set of dsc files, it can:
        get_build_able_pkg: get a dsc list that can be build now
        pkg_accomplish: announce a package build OK
        pkg_fail: announce a package build failure
        get_state: get statistical data
    '''

    # Construct the build relationship of all those dsc files
    # Input the file name contains all dsc files
    def __init__(self, dsc_list, logger):
        self.logger = logger
        self.depend_on = dict()
        self.depend_by = dict()
        self.wait_on = dict()

        self.__scan_dsc_list(dsc_list)

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

    # Depth first search the dependent tree. Once cycle dependent detected,
    # dump it.
    def __depth_t(self, node, dependents, chain):
        if node in chain:
            self.logger.error('Dependency error!')
            start = False
            for dsc in chain:
                if dsc == node:
                    start = True
                if start is True:
                    self.logger.error('%s build depend on ', dsc)
            self.logger.error('%s', node)
            raise Exception('CYCLE DEPENDENT.')
        chain.append(node)
        if node in list(dependents.keys()) and dependents[node]:
            for nd in dependents[node]:
                self.__depth_t(nd, dependents, chain)
        else:
            chain.pop()

    # Based on build relationships, calculate the priority value of each
    # source package. Once cycle dependency find, dump all related source
    # package and raise an exception.
    def __set_priority(self):
        # Init dictionary prio, set the default value to 10
        for key in self.wait_on:
            self.prio[key] = 10

        # Calculate priority of each dsc based on their build relationships
        tmp_d_by = copy.deepcopy(self.depend_by)
        tmp_d_on = copy.deepcopy(self.depend_on)
        # Each cycle should shrink at least one package, or raise exception
        while tmp_d_on:
            shrink = False
            for key in list(tmp_d_by.keys()):
                self.logger.debug('%s : %s', key, tmp_d_by[key])
                # If no package build depend BY A:
                # get all packages build depend ON A from tmp_d_on
                # add A's priority value to theirs.
                # A has no more value, remove it from tmp_d_on and tmp_d_by.
                if not tmp_d_by[key]:
                    if tmp_d_on[key]:
                        for pkg in self.wait_on[key]:
                            self.prio[pkg] = self.prio[pkg] + self.prio[key]
                            tmp_d_by[pkg].remove(key)
                    tmp_d_on.pop(key)
                    tmp_d_by.pop(key)
                    shrink = True
            # cycle dependent detected, raise exception.
            if not shrink:
                chain = []
                for node in list(tmp_d_on.keys()):
                    self.__depth_t(node, tmp_d_on, chain)

    # Get dependent packages from an input string
    # Input: Build-Depends + Build-Depends-Indep of a dsc file
    # Output: a set of build depend package name
    def __get_depends(self, depend_str):
        depends = set()
        self.logger.debug('%s', depend_str)
        raw_depends = set(depend_str.replace('|', ',').replace(' ', '')
                          .split(','))
        for raw_pkg in raw_depends:
            if -1 != raw_pkg.find('<cross>'):
                continue
            pkg = re.sub(u"\\<.*?\\>|\\(.*?\\)|\\[.*?\\]", "", raw_pkg)
            if 0 != len(pkg):
                depends.add(pkg)
        return depends

    # Scan a dsc file and get its build relationship through domain "Binary",
    # "Build-Depends" and "Build-Depends-Indep".
    # Param:
    #    list_line: INPUT. a string, one single line of the dsc list file,
    #        should contain a dsc's path name.
    #    src: INPUT/OUTPUT. a dictionary: src["a.deb"] = "a.dsc" means binary
    #        package a.deb is build from source package a.dsc.
    #    depend_on_b: INPUT/OUTPUT. a dictionary:
    #        depend_on_b["a.dsc"] = ['b.deb', 'c.deb'] means source package
    #        is build depend on binary package b.deb and c.deb.
    def __scan_dsc_file(self, list_line, src, depend_on_b):
        # remove empty line, comment string/lines
        list_line = list_line.strip()
        dsc_name = list_line
        if -1 != list_line.strip().find('#'):
            dsc_name = list_line[:list_line.find('#')]
        if not dsc_name:
            return None
        if not dsc_name.endswith('dsc'):
            self.logger.error('%s: is not a dsc file.', list_line)
            raise Exception('dsc list error, please check line: %s' % list_line)

        # open and read dsc file
        if not os.access(dsc_name, os.R_OK):
            self.logger.error('dsc file %s does not exist.', dsc_name)
            raise Exception('dsc file %s does not exist' % dsc_name)
        dsc_f = open(dsc_name, 'r')
        # scan the dsc file, get Binary Build-Depends and Build-Depends-Indep
        build_depends_arch = build_depends_indep = build_depends = ''
        build = b_depends = ''
        for dsc_line in dsc_f:
            if dsc_line.startswith('Binary:'):
                build = dsc_line[8:-1]
                self.logger.debug('%s build : %s', dsc_name, build)
            elif dsc_line.startswith('Build-Depends:'):
                build_depends = dsc_line[14:-1]
                self.logger.debug('%s build_depends : %s',
                                  dsc_name, build_depends)
            elif dsc_line.startswith('Build-Depends-Indep:'):
                build_depends_indep = dsc_line[21:-1]
                self.logger.debug('%s build_depends_indep : %s',
                                  dsc_name, build_depends_indep)
            elif dsc_line.startswith('Build-Depends-Arch:'):
                build_depends_arch = dsc_line[20:-1]
                self.logger.debug('%s build_depends_arch : %s',
                                  dsc_name, build_depends_arch)
        dsc_f.close()

        if build_depends_indep:
            b_depends = build_depends + ', ' + build_depends_indep
        if build_depends_arch:
            b_depends = build_depends + ', ' + build_depends_arch
        # Store binary depend_on relationship in dictionary "depend_on_b"
        depend_on_b[dsc_name] = self.__get_depends(b_depends)

        # Deal with "Binary", binary deb build from the dsc, store in "src"
        build_list = build.replace(' ', '').split(',')
        # assert len(depend_on_b[dsc_name]) != 0
        assert len(build_list) != 0
        for deb in build_list:
            src[deb] = dsc_name
        return None

    # Scan a serials of dsc files and get their build relationships
    # Input: file name of dsc list, two empty dictionaries
    # Output: a set of dictionaries: depend_on and depend_by
    #    depend_on['a.dsc'] = {'b.dsc', 'c.dsc'} ==> a build_depend_on b and c
    #    depend_by['a.dsc'] = {'b.dsc', 'c.dsc'} ==> a build_depend_by b and c
    def __scan_dsc_list(self, dsc_list_file):
        # src['a.deb'] = 'b.dsc' ==> a.deb is build from b.dsc
        src = dict()
        depend_on_b = dict()

        if not os.access(dsc_list_file, os.R_OK):
            self.logger.error('dsc list file %s not read-able.', dsc_list_file)
            return None
        dsc_list = open(dsc_list_file, 'r')
        # scan the dsc list
        for list_line in dsc_list:
            self.__scan_dsc_file(list_line, src, depend_on_b)
        dsc_list.close()

        # Here we have two dictionaries: "depend_on_b" and "src"
        # Construct dictionary "depend_on"
        for dsc, deb_list in depend_on_b.items():
            src_list = set()
            for deb in deb_list:
                if deb in src.keys():
                    src_list.add(src.get(deb))
            self.depend_on[dsc] = src_list

        # Construct dictionary "depend_by" from depend_on
        for key, value in self.depend_on.items():
            self.depend_by[key] = set()
        for key, value in self.depend_on.items():
            for pkg_by in value:
                self.depend_by.get(pkg_by).add(key)

        # Now, both depend_on and depend_by accomplished.
        return None

    # dump the build depended of all source packages including all packages of
    # the dsc_list. Debug/develop only
    def __dump_dependent(self):
        self.logger.debug('%d relationshis pof DEPEND_ON', len(self.depend_on))
        for key, value in self.depend_on.items():
            if not value:
                self.logger.debug('NOTHING')
            else:
                for pkg in value:
                    self.logger.debug('%s %d', pkg, self.prio[pkg])
        self.logger.debug('%d relationships of DEPEND_BY', len(self.depend_by))
        for key, value in self.depend_by.items():
            self.logger.debug('%s %d DEPEND-BY', key, self.prio[key])
            if not value:
                self.logger.debug('NOTHING')
            else:
                for pkg in value:
                    self.logger.debug('%s %d', pkg, self.prio[pkg])

    # dump packages can be build now. Debug/develop only
    def __dump_build_able_pkg(self):
        self.logger.info('Build-able source packages:')
        for key, value in self.build_able_pkg.items():
            if value < 0:
                self.logger.info('%s is building', key)
            else:
                self.logger.info('%s can be build, prio is %d', key, value)
        return len(self.build_able_pkg)

    # Get packages from build_able_list.
    # Input: max number of packages want to get(0 < value < 100)
    # Output: A set of dsc file name
    def get_build_able_pkg(self, count):
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
        self.logger.debug('%d Build_able packages, try to get %d From them',
                          len(list_pkg), count)
        while count > 0:
            # prio < 0 ==> in building stage
            if i >= len(list_pkg) or list_pkg[i][1] < 0:
                self.logger.debug('No more packages can be build.')
                break
            pkg = list_pkg[i][0]
            self.logger.debug(pkg)
            self.build_able_pkg[pkg] = self.build_able_pkg[pkg] - 10000
            pkgs.append(pkg)
            self.count['can_build'] = self.count['can_build'] - 1
            self.count['building'] = self.count['building'] + 1
            i = i + 1
            count = count - 1
        self.logger.debug('%d packages will be build', i)
        self.logger.debug(pkgs)
        return pkgs

    # A source package is build OK.
    # Input: the dsc file name that build accomplished
    def pkg_accomplish(self, pkg_name):
        if self.build_able_pkg.get(pkg_name):
            self.build_able_pkg.pop(pkg_name)
            self.count['accomplished'] = self.count['accomplished'] + 1
            self.count['building'] = self.count['building'] - 1
        else:
            self.logger.warning('%s not in building stage.', pkg_name)
            return None

        if self.depend_by[pkg_name]:
            for pkg in self.depend_by[pkg_name]:
                self.logger.debug('%s is depended by %s', pkg, pkg_name)
                self.logger.debug(self.wait_on[pkg])
                self.wait_on[pkg].remove(pkg_name)
                if not self.wait_on[pkg]:
                    self.logger.info('%s can be build.', pkg)
                    self.build_able_pkg[pkg] = self.prio[pkg]
                    self.wait_on.pop(pkg)
                    self.count['can_build'] = self.count['can_build'] + 1
                    self.count['wait'] = self.count['wait'] - 1
        return None

    # A source package build failed, back to build-able package list
    # Input: the dsc file name that build failed
    def pkg_fail(self, pkg_name):
        if self.build_able_pkg.get(pkg_name) \
                and self.build_able_pkg[pkg_name] < 0:
            self.build_able_pkg[pkg_name] += 10000
            self.count['can_build'] += 1
            self.count['building'] -= 1
        else:
            self.logger.warning('%s not in building stage.', pkg_name)

    # Dump packages that can't be build now. Debug/develop only
    def __dump_wait_chain(self):
        self.logger.info('%s packages are waiting for build dependent.',
                         len(self.wait_on))
        for key, value in self.wait_on.items():
            self.logger.info('%s ==> %d', key, value)
        return len(self.wait_on)

    # Get build status
    # Output: [pkg_count,
    #          count_wait,
    #          count_can_build,
    #          count_building,
    #          count_accomplished
    #         ]
    def get_state(self):
        pkg_state = [self.count['pkg'], self.count['wait'],
                     self.count['can_build'], self.count['building'],
                     self.count['accomplished']]
        assert self.count['pkg'] == (self.count['wait'] +
                                     self.count['can_build'] +
                                     self.count['building'] +
                                     self.count['accomplished'])
        self.logger.info('%d packages', self.count['pkg'])
        self.logger.info('%d packages are waiting for build dependent',
                         self.count['wait'])
        self.logger.info('%d packages can be build, waiting for OBS',
                         self.count['can_build'])
        self.logger.info('%d packages in building stage',
                         self.count['building'])
        self.logger.info('%d packages accomplished',
                         self.count['accomplished'])
        return pkg_state
