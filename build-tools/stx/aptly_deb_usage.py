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
# Copyright (C) 2021-2022  WindRiver Corporation
#
# Requires aptly-api-client:
# https://github.com/masselstine/aptly-api-client
#
# Old document of relate RESTAPI:
# https://www.aptly.info/doc/api/
#
# Realization of its real RESTAPI(go)
# https://github.com/molior-dbs/aptly
from aptly_api import Client
from debian import debian_support
import os
import time
from typing import Optional

PREFIX_LOCAL = 'deb-local-'
PREFIX_REMOTE = 'deb-remote-'
PREFIX_MERGE = 'deb-merge-'
SIGN_KEY = '8C58D092AD39022571D1F57AFA689A0116E3E718'
SIGN_PASSWD = 'starlingx'
DEFAULT_TIMEOUT_COUNT = 1
STX_DIST = os.environ.get('STX_DIST')
DEBIAN_DISTRIBUTION = os.environ.get('DEBIAN_DISTRIBUTION')

# Class used to manage aptly data base, it can:
#     create_remote: Create a repository link to a remote mirror
#     deploy_remote: Sync and deploy a remote mirror
#     list_remotes:  List all remote repositories
#     remove_remote: Delete a remote repository
#     create_local: Create a local repository
#     upload_pkg_local: Upload a deb package into a local repository
#     delete_pkg_local: Remove a deb package from a local repository
#     pkg_exist: Search a package in a set of repos
#     copy_pkgs: Copy packages from one repo to another
#     deploy_local: Deploy a local repository
#     list_local: List all local repositories
#     remove_local: Delete a local repository
#     clean_all: Clean all meta data including repo, public, distribution, package, task


class Deb_aptly():
    def __init__(self, url, origin, logger):
        '''The basic interface to manage aptly database. '''
        self.logger = logger
        self.url = url
        self.aptly = Client(self.url)
        self.logger.info('Aptly connected, version: %s', self.aptly.misc.version())
        if origin:
            self.origin = origin.strip() or None
        else:
            self.origin = None

    # Create a remote mirror(make sure the name has specified prefix)
    # Input
    #       name: the name of the remote repo : PREFIX_REMOTE-xxx
    #       url: the base url of the remote mirror: http://nginx.org/packages/debian
    #       distributions: the distribution: buster
    #       components: components=['nginx']
    #       architectures: architectures=['i386', 'arm64']
    #       with_sources: with_sources=True
    # Output: None or Class 'aptly_api.parts.mirrors.Mirror'
    def create_remote(self, name, url, distribution, **kwargs):
        '''Base on a Debian repository, construct a repo as its mirror'''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.error('%s is not started with %s, Failed.', name, PREFIX_REMOTE)
            raise ValueError('remote repository create failed: prefix error')

        remote_list = self.aptly.mirrors.list()
        for remote in remote_list:
            if remote.name == name:
                self.logger.warning('mirror %s already exists.', remote.name)
                return None
        extra_param = {}
        extra_param['distribution'] = distribution
        extra_param['ignore_signatures'] = True
        for key, value in kwargs.items():
            if key == 'components':
                extra_param['components'] = value
            if key == 'with_sources':
                extra_param['with_sources'] = True
            if key == 'architectures':
                extra_param['architectures'] = value
            # Not find good/small repository with udebs, not verified
            # if key == 'with_udebs':
            #    extra_param['with_udebs'] = True
        remote = self.aptly.mirrors.create(name, url, **extra_param)
        return remote

    # update a mirror called "name". Mirror exist.
    # Return False if failed
    def __update_mirror(self, name):
        '''Sync the mirror, may take minutes, depends on the size of the mirror ans the network. '''
        mirror_list = self.aptly.mirrors.list()
        # Add variable mirror_find just to avoid W0631
        mirror_find = False
        for mirror in mirror_list:
            if mirror.name == name:
                mirror_find = True
                break
        if not mirror_find:
            self.logger.warning('Publish failed for mirror %s not find', name)
            return False
        # Please do NOT add any parameters here beside "ignore_signatures=True", that may
        # overwrite previous settings and get strange results.
        task = self.aptly.mirrors.update(name=name, ignore_signatures=True)
        task_state = self.__wait_for_task(task, 15)
        if task_state == 'SUCCEEDED':
            return True
        else:
            self.logger.warning('Mirror %s update failed: %s', name, task_state)
            return False

    # Create a snapshot based on several others
    # name : string, the name of new build snapshot
    # source_snapshots: list of snapshots to be merge, order matters, snapshot at front of
    #                   list has higher priority than snapshot later in the list.
    # For each package, only the one with higher version can be selected:
    # Return False on failure
    def __merge_snapshot(self, name, source_snapshots):
        '''Merge several snapshots into one, prepare for later deploy.'''
        if not name.startswith(PREFIX_MERGE):
            self.logger.error('%s did not start with %s, Failed.' % (name, PREFIX_MERGE))
            return False
        package_refs = []
        # package_uniq_dict[pkgname_arch] = [package.key, snapshot]
        package_uniq_dict = dict()
        source_snapshots = [x.strip() for x in source_snapshots if x.strip() != '']
        # remove duplicates (keep order)
        source_snapshots = list(dict.fromkeys(source_snapshots))
        snap_list = self.aptly.snapshots.list()
        for snapshot in source_snapshots:
            snap_exist = False
            for snap in snap_list:
                if snap.name == snapshot:
                    snap_exist = True
                    package_list = self.aptly.snapshots.list_packages(snap.name, with_deps=False, detailed=False)
                    # Debug only
                    # package_list.sort()
                    # self.logger.debug('%s packages in repo %s' % (len(package_list), snapshot))
                    for package in package_list:
                        key_list = package.key.split()
                        # 0: pkg_arch  1: pkg_name  2: pkg_version 3: pkg_key of aptly
                        pkgname_arch = '_'.join([key_list[1], key_list[0]])
                        # Source packages are useless for LAT, ignore them.
                        if "Psource" == key_list[0]:
                            continue
                        # Check and drop duplicate packages
                        if pkgname_arch in package_uniq_dict.keys():
                            need_replace = False
                            orig_version = package_uniq_dict[pkgname_arch][0].split()[2]
                            if STX_DIST in orig_version and STX_DIST not in key_list[2]:
                                self.logger.warn('STX package %s %s has been eclipsed by upstream version %s' %
                                                 (pkgname_arch, orig_version, key_list[2]))
                            if debian_support.version_compare(key_list[2], orig_version) > 0:
                                self.logger.warn('Drop duplicate package: %s.' %
                                                 ' of '.join(package_uniq_dict[pkgname_arch]))
                                package_refs.remove(package_uniq_dict[pkgname_arch][0])
                                package_refs.append(package.key)
                                package_uniq_dict[pkgname_arch] = [package.key, snapshot]
                            else:
                                self.logger.warn('Drop duplicate package: %s of %s.' % (package.key, snapshot))
                            continue
                        package_uniq_dict[pkgname_arch] = [package.key, snapshot]
                        package_refs.append(package.key)
                    break
            if not snap_exist:
                self.logger.error('snapshot %s does not exist, merge failed.' % snapshot)
                return False

        # Remove a same name publish if exists
        # For exist snapshot called NAME, we will:
        # 1, rename it to backup-NAME
        # 2, Create a new snapshot: NAME
        # 3, delete snapshot backup-name
        backup_name = None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop publication failed %s : %s' % (name, task_state))
                    return False
        # Remove the backup snapshot if it exists
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == 'backup-' + name:
                backup_name = 'backup-' + name
                task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s' % (backup_name, task_state))
                    return False
        # Rename the snapshot if it exists
        for snap in snap_list:
            if snap.name == name:
                backup_name = 'backup-' + name
                self.__wait_for_task(self.aptly.snapshots.update(name, backup_name))

        # crate a snapshot with package_refs. Duplicate package_refs is harmless.
        # Note: The key is "package_refs" instead of "source_snapshots", for function
        #       "create_from_packages", parameter "source_snapshots" almost has no means.
        task = None
        task = self.aptly.snapshots.create_from_packages(name, source_snapshots=source_snapshots, package_refs=package_refs)
        task_state = self.__wait_for_task(task)
        if task_state != 'SUCCEEDED':
            if backup_name:
                self.__wait_for_task(self.aptly.snapshots.update(backup_name, name))
            self.logger.warning('merge_snapshot: Snapshot for %s creation failed: %s. ' % (name, task_state))
            return False
        # Remove the backup snapshot if it is created above
        if backup_name:
            task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop snapshot failed %s : %s' % (backup_name, task_state))
        return True

    # Create a snapshot based on "name" with same name
    # local: True ==> local_repo False ==> remote_mirror
    # Return False if failed
    def __create_snapshot(self, name, local):
        '''For local-repo or remote-repo, create a snapshot for it, prepare for later deploy.'''
        # Remove a same name publish if exists
        # For exist snapshot called NAME, we will:
        # 1, rename it to backup-NAME
        # 2, Create a new snapshot: NAME
        # 3, delete snapshot backup-name
        backup_name = None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Remove publication failed %s : %s' % (name, task_state))
        # Rename the snapshot if exists
        snap_list = self.aptly.snapshots.list()

        exists = [snap for snap in snap_list if snap.name == name]
        backup_exists = [snap for snap in snap_list if snap.name == 'backup-' + name]
        if exists:
            backup_name = 'backup-' + name
            if backup_exists:
                self.__wait_for_task(self.aptly.snapshots.delete(backup_name, force=True))
            self.__wait_for_task(self.aptly.snapshots.update(name, backup_name))

        # crate a snapshot
        task = None
        if local:
            task = self.aptly.snapshots.create_from_repo(name, name)
        else:
            task = self.aptly.snapshots.create_from_mirror(name, name)
        task_state = self.__wait_for_task(task)
        if task_state != 'SUCCEEDED':
            if backup_name:
                self.__wait_for_task(self.aptly.snapshots.update(backup_name, name))
            self.logger.warning('create_snapshot: Snapshot for %s creation failed: %s.' % (name, task_state))
            return False
        if backup_name:
            task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Remove snapshot failed %s : %s' % (backup_name, task_state))
        return True

    # Wait for an aptly task up to a maximum of "count" minutes.
    # By dafault, wait for DEFAULT_TIMEOUT_COUNT minute(s).
    # Return: SUCCEEDED, FAILED, TIMEOUTED, EINVAL
    def __wait_for_task(self, task, count=DEFAULT_TIMEOUT_COUNT):
        '''Wait for an aptly task for one or more minutes'''
        if count not in range(1, 30):
            self.logger.error('Requested wait of % minutes is greater than 30 minutes max wait.', count)
            return 'EINVAL'
        timeout_factor = os.environ.get('REPOMGR_REQ_TIMEOUT_FACTOR')
        if timeout_factor and timeout_factor.isdigit() and int(timeout_factor) != 0:
            count *= int(timeout_factor)
        while count > 0:
            count -= 1
            try:
                # Function wait_for_task_by_id will return in 60 seconds, or timeout.
                self.aptly.tasks.wait_for_task_by_id(task.id)
            except Exception as e:
                if count > 0:
                    self.logger.debug('Aptly task %d(%s) is still running' % (task.id, task.name))
                else:
                    self.logger.debug('%s' % e)
                continue
            else:
                # return 'SUCCEEDED' or 'FAILED'
                return self.aptly.tasks.show(task.id).state
        self.logger.warn('Aptly task %d(%s) timeouts.' % (task.id, task.name))
        self.logger.info('Environment variable REPOMGR_REQ_TIMEOUT_FACTOR can be used to increase timeout value.')
        self.logger.info('For example, set it to "5" can increase the timeout value by 5 times.')
        return 'TIMEOUTED'

    # Publish a local repository directly, without snapshot or signature
    # If an old publish exists, drop it firstly and then create a new one.
    # Do not use publish.update just for safety.
    # (repo)repo_name ==> (publish)repo_name-suffix
    def __quick_publish_repo(self, repo_name, suffix):
        '''Create a publish based on a local repository directly, without snapshot.'''
        # Caller already checked the repo_name, no need to check again
        if not suffix:
            self.logger.error('Quick publish needs suffix, none provided')
            return
        publish_name = '-'.join([repo_name, suffix])
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == publish_name:
                task = self.aptly.publish.drop(prefix=publish_name, distribution=publish.distribution, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop failed publication %s : %s', publish_name, task_state)
                    return None
        task = self.aptly.publish.publish(source_kind='local', sources=[{'Name': repo_name}],
                                          architectures=['amd64', 'source'], prefix=publish_name,
                                          distribution=None, sign_skip=True)
        task_state = self.__wait_for_task(task, 10)
        if task_state != 'SUCCEEDED':
            self.logger.warning('Quick publish for %s create failed: %s', publish_name, task_state)
            return None
        return publish_name + ' ' + DEBIAN_DISTRIBUTION

    # Publish a snap called "name" with prefix as name, DEBIAN_DISTRIBUTION as the distribution
    # Return None or prefix/distribution
    def __publish_snap(self, name):
        '''Deploy a snapshot.'''
        # Remove a same name publish if exists
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop publish failed %s : %s', name, task_state)
                    return None

        # is_remote: True => remote repo; False => local repo
        is_remote = False
        mirror = None
        mirror_list = self.aptly.mirrors.list()
        for mirror in mirror_list:
            if mirror.name == name:
                is_remote = True
                break

        # crate a publish
        extra_param = {}
        if is_remote:
            # it is a remote repo: info storied in "mirror"
            # Add 'source' to publish source packages, if no source packages, that is also harmless.
            extra_param['architectures'] = mirror.architectures.append('source')
            extra_param['distribution'] = mirror.distribution
            extra_param['origin'] = None
        else:
            # Only support binary_amd64 and source packages
            extra_param['architectures'] = ['amd64', 'source']
            extra_param['distribution'] = None
            extra_param['origin'] = self.origin

        extra_param['source_kind'] = 'snapshot'
        extra_param['sources'] = [{'Name': name}]
        extra_param['sign_skip'] = True
        extra_param['prefix'] = name
        task = self.aptly.publish.publish(source_kind='snapshot', sources=extra_param['sources'],
                                          architectures=extra_param['architectures'], prefix=extra_param['prefix'],
                                          distribution=extra_param['distribution'],
                                          sign_gpgkey=SIGN_KEY, sign_passphrase=SIGN_PASSWD,
                                          origin=extra_param['origin'])
        task_state = self.__wait_for_task(task, 10)
        if task_state != 'SUCCEEDED':
            self.logger.warning('Publication %s failed: %s' % (name, task_state))
            return None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                repo_str = publish.prefix + ' ' + publish.distribution
                return repo_str
        return None

    # sync a remote mirror and deploy it
    # Input: the name of the remote
    # Output: bool
    def deploy_remote(self, name):
        '''Deploy a mirror, it will sync/update, snapshot and publish at last.
        It may take minutes, depends on the size of the mirror and the bandwidth,
        '''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.warning('%s has no %s prefix, not a remote repository.', name, PREFIX_REMOTE)
            return None

        remote_list = self.aptly.mirrors.list()
        remote = None
        for remote in remote_list:
            if remote.name == name:
                break

        if not remote:
            self.logger.warning('mirror %s not find, please create it firstly.', name)
            return None

        if self.__update_mirror(name):
            if self.__create_snapshot(name, False):
                return self.__publish_snap(name)
        return None

    # info all remote repositories through logger
    def list_remotes(self, quiet=False):
        '''List all remote repositories/mirrors.'''
        r_list = []
        remote_list = self.aptly.mirrors.list()
        if not len(remote_list):
            if not quiet:
                self.logger.info('No remote repo')
            return r_list
        if not quiet:
            self.logger.info('%d remotes:', len(remote_list))
        for remote in remote_list:
            r_list.append(remote.name)
            if not quiet:
                self.logger.info('%s : %s : %s', remote.name, remote.archive_root, remote.distribution)
        return r_list

    # find and remove a remote
    # Input: the name of the remote
    # Output: Bool
    def remove_remote(self, name):
        '''Delete a remote repository/mirror and all related publish and snapshot.'''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.warning('%s is not a correct remote name', name)
            return False

        # find and remove related publish
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                self.__wait_for_task(task)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, task_state)

        # find and remove the remote(mirror)
        remote_list = self.aptly.mirrors.list()
        for remote in remote_list:
            if remote.name == name:
                task = self.aptly.mirrors.drop(name=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop mirror failed %s : %s', name, task_state)

        # Delete orphan files, wait up to 5 minutes for the cleanup to complete
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)

        return True

    # info all local repositories through logger
    def list_local(self, quiet=False):
        '''List all local repository.'''
        local_list = []
        repo_list = self.aptly.repos.list()
        if not len(repo_list):
            self.logger.info('No local repo')
            return local_list
        if not quiet:
            self.logger.info('%d local repos:', len(repo_list))
        for repo in repo_list:
            # rpo.name, repo.url, repo.distributions, repo.components
            local_list.append(repo.name)
            if not quiet:
                self.logger.info('%s : %s : %s', repo.name, repo.default_distribution, repo.default_component)
        return local_list

    # Create a local repository
    # Input:the name of the repo
    # Output: None or repo
    def create_local(self, local_name):
        '''Create an empty local repository.'''
        if not local_name.startswith(PREFIX_LOCAL):
            self.logger.error('%s  is not started with %s, Failed.', local_name, PREFIX_LOCAL)
            raise ValueError('local repository create failed: prefix error.')

        repo_list = self.aptly.repos.list()
        for repo in repo_list:
            if local_name == repo.name:
                self.logger.warning('%s exists, please choose another name', local_name)
                return None

        # Static settings: DEBIAN_DISTRIBUTION main
        repo = self.aptly.repos.create(local_name, default_distribution=DEBIAN_DISTRIBUTION, default_component='main')
        return repo

    # Upload a bundle of Debian package files into a local repository.
    # For source package, all its package files need to be uploaded in one
    # function call, or, uploaded files will not be inserted into repository
    # but just deleted.
    # Input:
    #       pkg_files: the path-name of the package files. If the file name
    #                  contains "%3a", it will be replaced by ":".
    #       repo_name: the name of the local repository
    # Output: Bool
    def upload_pkg_local(self, pkg_files, repo_name):
        '''Upload a bundle of package files into a local repository.'''
        # sanity check: every package file is readable, local repository exists
        if not pkg_files:
            self.logger.warning('pkg_files should not be empty!')
            return False
        for pkg_file in set(pkg_files):
            if not os.access(pkg_file, os.R_OK):
                self.logger.warning('%s is NOT accessible to read.', pkg_file)
                return False
        if not repo_name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is NOT a well formed name.', repo_name)
            return False

        repo_list = self.aptly.repos.list()
        repo_found = False
        for repo in repo_list:
            if repo_name == repo.name:
                self.logger.debug('repo %s was found and can be used', repo_name)
                repo_found = True
                break

        if not repo_found:
            self.logger.warning('repo %s does not exist, please create it first.', repo_name)
            return False

        # If the process was interrupted, leaving behind a file folder,
        # clean it up by removing it before we start.
        for file in self.aptly.files.list():
            self.aptly.files.delete(file)
        for pkg_file in set(pkg_files):
            # For files with ":" in its filename, tools like 'apt' may replace it
            # with '%3a' by mistake, this will cause error in aptly.
            if pkg_file.find('%3a') >= 0:
                rename_file = pkg_file.replace('%3a', ':')
                try:
                    os.rename(pkg_file, rename_file)
                except Exception as e:
                    self.logger.error('Error: %s' % e)
                    self.logger.error('Package file %s rename error.' % pkg_file)
                    raise Exception('Package file %s rename error, upload failed.' % pkg_file)
                else:
                    # Upload package file into related file folder.
                    self.aptly.files.upload(repo_name, rename_file)
            else:
                self.aptly.files.upload(repo_name, pkg_file)

        # Add uploaded file into local repository.
        task = self.aptly.repos.add_uploaded_file(repo_name, repo_name, remove_processed_files=True)
        task_state = self.__wait_for_task(task)
        if task_state != 'SUCCEEDED':
            self.logger.warning('add_upload_file failed %s : %s : %s', list(pkg_files)[0], repo_name, task_state)
        return True

    # Delete a Debian package from a local repository.
    # Input:
    #       local_repo: the name of the local repository
    #       pkg_name: the path-name of the deb file
    #       pkg_type: 'binary' or 'source'
    #       pkg_version: version of the deb file
    # Output: None
    def delete_pkg_local(self, local_repo, pkg_name, pkg_type, pkg_version=None):
        '''Delete a binary package from a local repository.'''
        # self.logger.debug('delete_pkg_local not supported yet.')
        if pkg_type not in {'binary', 'source'}:
            self.logger.error('package type must be one of either "binary" or "source"')
            return
        if not pkg_version:
            query = pkg_name
        else:
            query = pkg_name + ' (' + pkg_version + ')'
        # If we want more detailed info, add "detailed=True, with_deps=True" for search_packages.
        search_result = self.aptly.repos.search_packages(local_repo, query=query)
        self.logger.debug('delete_pkg_local find %d packages.' % len(search_result))
        for pkg in search_result:
            if (pkg_type == 'source' and pkg.key.split()[0] == 'Psource') or \
                (pkg_type != 'source' and pkg.key.split()[0] != 'Psource'):
                task = self.aptly.repos.delete_packages_by_key(local_repo, pkg.key)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Delete package failed %s : %s' % (pkg_name, task_state))

    def pkg_list(self, repo_list):
        '''list packages available from any of the listed repos, local or remote.'''
        pkg_list=[]
        for repo_name in repo_list:
            if repo_name.startswith(PREFIX_LOCAL):
                query = 'Name'
                pkgs_raw = self.aptly.repos.search_packages(repo_name, query=query)
                pkgs_key = [pkg.key for pkg in pkgs_raw]
            elif repo_name.startswith(PREFIX_REMOTE):
                pkgs_key = self.aptly.mirrors.packages(repo_name)
            for key in pkgs_key:
                pkg_name = key.split()[1]
                pkg_ver = key.split()[2]
                pkg_arch = key.split()[0][1:]
                if pkg_arch == 'source':
                    pkg_list.append("%s_%s.dsc" % (pkg_name, pkg_ver))
                else:
                    pkg_list.append("%s_%s_%s.deb" % (pkg_name, pkg_ver, pkg_arch))
        return pkg_list


    # Search a package in a set of repos, return True if find, or False
    # repolist: a list of repo names, including local repo and mirror
    # pkg_name: package name
    # architecture: Architecture of the package, now, only check 'source' or not
    # pkg_version:  the version of the package, None means version insensitive
    def pkg_exist(self, repo_list, pkg_name, architecture, pkg_version=None):
        '''Search a package in a bundle of repositories including local repo and remote one.'''
        for repo_name in repo_list:
            if repo_name.startswith(PREFIX_LOCAL):
                if not pkg_version:
                    query = pkg_name
                else:
                    query = pkg_name + ' (' + pkg_version + ')'
                # If we want more detailed info, add "detailed=True, with_deps=True" for search_packages.
                search_result = self.aptly.repos.search_packages(repo_name, query=query)
                for pkg in search_result:
                    if architecture != 'source' and pkg.key.split()[0] != 'Psource':
                        self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                        return True
                    if architecture == 'source' and pkg.key.split()[0] == 'Psource':
                        self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                        return True
            elif repo_name.startswith(PREFIX_REMOTE):
                pkgs = self.aptly.mirrors.packages(repo_name)
                for pkg in pkgs:
                    if pkg.split()[1] == pkg_name:
                        if architecture != 'source' and pkg.split()[0] != 'Psource' and (not pkg_version or pkg_version == pkg.split()[2]):
                            self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                            return True
                        if architecture == 'source' and pkg.split()[0] == 'Psource' and (not pkg_version or pkg_version == pkg.split()[2]):
                            self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                            return True
        return False

    # Copy a set of packages from one repository into another
    # source: the repository name that packages been copied from
    # dest: the repository name that packages been copied to
    # pkg_list: list of package name to be copied
    # pkg_type: binary or source. Default is binary
    # overwrite: True or False. Overwrite existing packages or not
    def copy_pkgs(self, source, dest, pkg_list, pkg_type='binary', overwrite=True):
        '''Copy package from one repository to another local repository'''
        dest_exist = False
        source_exist = False
        # package key list of destination and source repository
        dest_pkg_keys = list()
        src_pkg_keys = list()
        if source == dest:
            self.logger.error('%s and %s are the same repository.' % (source, dest))
            return False
        for repo in self.aptly.repos.list():
            if dest == repo.name:
                dest_exist = True
                pkgs = self.aptly.repos.search_packages(dest, query='Name')
                dest_pkg_keys = [pkg.key for pkg in pkgs]
            if source == repo.name:
                source_exist = True
                pkgs = self.aptly.repos.search_packages(source, query='Name')
                src_pkg_keys = [pkg.key for pkg in pkgs]
        if not dest_exist:
            self.logger.warning('Destination repository %s does not exist.', dest)
            return False
        if not source_exist:
            for repo in self.aptly.mirrors.list():
                if source == repo.name:
                    source_exist = True
                    src_pkg_keys = self.aptly.mirrors.packages(source)
                    break
        if not source_exist:
            self.logger.warning('Source repository %s dose not exist.', source)
            return False
        del_keys = list()
        add_keys = list()
        for key in src_pkg_keys:
            package_name = key.split()[1]
            package_type = key.split()[0]
            if package_name not in pkg_list:
                continue
            if (pkg_type == 'source' and package_type != 'Psource') or (pkg_type == 'binary' and package_type == 'Psource'):
                continue
            # Find a package in source repository to be copied.
            pkg_list.remove(package_name)
            # Already exists in destination repository
            if key in dest_pkg_keys:
                continue
            pkg_in_dest = False
            for dest_key in dest_pkg_keys:
                # [0] package type/arch: Psource, Pamd64, Pall. [1] package name
                if package_type == dest_key.split()[0] and package_name == dest_key.split()[1]:
                    pkg_in_dest = True
                    if overwrite:
                        del_keys.append(dest_key)
                        add_keys.append(key)
                    break
            if not pkg_in_dest:
                add_keys.append(key)
            if not pkg_list:
                break

        # check to see if any packages not find in source repository
        if pkg_list:
            self.logger.warning('Copy package error, %s package %s not exist in %s' % (pkg_type, ' '.join(pkg_list), source))
            return False
        # Remove duplicate packages from destination repository
        if del_keys:
            task = self.aptly.repos.delete_packages_by_key(dest, *del_keys)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Delete packages failed: %s\n%s' % (task_state, '\n'.join(del_keys)))
                return False
        # Insert packages into destination repository
        if add_keys:
            task = self.aptly.repos.add_packages_by_key(dest, *add_keys)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Copy packages failed: %s\n%s' % (task_state, '\n'.join(add_keys)))
                return False
        return True

    # Merge several repositories into a new one(just snapshot and publish)
    # name: the name of the new build snapshot/publish
    # source_snapshots: list, snapshots to be merged
    def merge_repos(self, name, source_snapshots):
        '''Merge several repositories into a new publish.'''
        if not name.startswith(PREFIX_MERGE):
            self.logger.warning('The name should started with %s.', PREFIX_MERGE)
            return None

        if self.__merge_snapshot(name, source_snapshots):
            ret = self.__publish_snap(name)
            return ret

    # deploy a local repository
    # Input
    #   name: the name of the local repository
    #   suffix: suffix of the publish name
    # Output: None or DebAptDistributionResponse
    def deploy_local(self, name, suffix=''):
        '''Deploy a local repository.'''
        if not name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is NOT a well formed name.', name)
            return None

        repo_list = self.aptly.repos.list()
        repo_find = False
        for repo in repo_list:
            if name == repo.name:
                self.logger.debug('%s find, can be used', name)
                repo_find = True
                break
        if not repo_find:
            self.logger.warning('local repo %s not found.', name)
            return None
        if suffix:
            return self.__quick_publish_repo(name, suffix)

        if self.__create_snapshot(name, True):
            ret = self.__publish_snap(name)
            # Delete orphan files, wait up to 5 minutes for the cleanup to complete
            task = self.aptly.db.cleanup()
            self.__wait_for_task(task, 5)
            return ret
        return None

    # remove a local repository
    # Input: the name of the local repository
    # Output: None
    def remove_local(self, name):
        '''Delete a local repository, including related publish and snapshot.'''
        if not name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is not a correct name', name)
            return None

        # find and remove related publish
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            # Remove all related publish including quick publish
            if publish.prefix.startswith(name + '-') or publish.prefix == name:
                task = self.aptly.publish.drop(prefix=publish.prefix, distribution=DEBIAN_DISTRIBUTION, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop publish failed %s : %s', name, task_state)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, task_state)

        # find and remove the remote(mirror)
        repo_list = self.aptly.repos.list()
        for repo in repo_list:
            if repo.name == name:
                task = self.aptly.repos.delete(reponame=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop repo failed %s : %s', name, task_state)

        # Delete orphan files, wait up to 5 minutes for the cleanup to complete
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)

        return None

    # clean all metadata including remote, repository, public, distribution, task and content
    # In theory, with this operation, there should be nothing left in aptly_deb
    # database. Please use it carefully.
    def clean_all(self):
        '''Clean all metadata including remote, repository, public, distribution, task and content.'''
        # clean publishes
        pub_list = self.aptly.publish.list()
        self.logger.info('%d publish', len(pub_list))
        for pub in pub_list:
            task = self.aptly.publish.drop(prefix=pub.prefix, distribution=pub.distribution, force_delete=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop publish failed %s : %s', pub.frepix, task_state)
        # clean snapshots
        snap_list = self.aptly.snapshots.list()
        self.logger.info('%d snapshot', len(snap_list))
        for snap in snap_list:
            task = self.aptly.snapshots.delete(snapshotname=snap.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop snapshot failed %s : %s', snap.name, task_state)

        # clean mirrors
        mirror_list = self.aptly.mirrors.list()
        self.logger.info('%d mirror', len(mirror_list))
        for mirror in mirror_list:
            task = self.aptly.mirrors.drop(name=mirror.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop mirror failed %s : %s', mirror.name, task_state)
        # clean local repos
        repo_list = self.aptly.repos.list()
        self.logger.info('%d repo', len(repo_list))
        for repo in repo_list:
            task = self.aptly.repos.delete(reponame=repo.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop repo failed %s : %s', repo.name, task_state)
        # clean file folders
        file_list = self.aptly.files.list()
        self.logger.info('%d file folder', len(file_list))
        for file in file_list:
            self.aptly.files.delete(file)
        # clean tasks
        self.aptly.tasks.clear()
        # Delete orphan files, up to 5 minutes
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)
