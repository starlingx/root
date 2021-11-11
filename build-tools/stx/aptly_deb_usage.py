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
# Requires aptly-api-client:
# https://github.com/masselstine/aptly-api-client
#
# Old document of relate RESTAPI:
# https://www.aptly.info/doc/api/
#
# Realization of its real RESTAPI(go)
# https://github.com/molior-dbs/aptly
from aptly_api import Client
import os
import time
from typing import Optional

PREFIX_LOCAL = 'deb-local-'
PREFIX_REMOTE = 'deb-remote-'

# Class used to manage aptly data base, it can:
#     create_remote: Create a repository link to a remote mirror
#     deploy_remote: Sync and deploy a remote mirror
#     list_remotes:  List all remote repositories
#     remove_remote: Delete a remote repository
#     create_local: Create a local repository
#     upload_pkg_local: Upload a deb package into a local repository
#     delete_pkg_local: Remove a deb package from a local repository
#     pkg_exist: Search a package in a set of repos
#     deploy_local: Deploy a local repository
#     list_local: List all local repositories
#     remove_local: Delete a local repository
#     clean_all: Clean all meta data including repo, public, distribution, package, task


class Deb_aptly():
    def __init__(self, url, logger):
        '''The basic interface to manage aptly database. '''
        self.logger = logger
        self.url = url
        self.aptly = Client(self.url)
        self.logger.info('Aptly connected, version: %s', self.aptly.misc.version)

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
        # We can't use tasks.wait_for_task_by_id here, for the timeout is
        # 60 seconds while no one knows how long it may take to accomplish
        # the mirror update. So we need to manually os.sleep here.
        # self.aptly.tasks.wait_for_task_by_id(task.id)
        while 1:
            # IDLE, RUNNING, SUCCEEDED, FAILED (What IDLE means?)
            task_state = self.aptly.tasks.show(task.id).state
            if task_state == 'RUNNING':
                time.sleep(5)
                self.logger.debug('Mirror %s updating: %s', name, self.aptly.tasks.show(task.id).state)
                continue
            if task_state == 'FAILED':
                self.logger.warning('Mirror %s update failed: %s', name, self.aptly.tasks.show(task.id).state)
                return False
            if task_state == 'SUCCEEDED':
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
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Remove publication failed %s : %s' % (name, self.aptly.tasks.show(task.id).state))
        # Rename the snapshot if exists
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                backup_name = 'backup-' + name
                self.aptly.tasks.wait_for_task_by_id(self.aptly.snapshots.update(name, backup_name).id)

        # crate a snapshot
        task = None
        if local:
            task = self.aptly.snapshots.create_from_repo(name, name)
        else:
            task = self.aptly.snapshots.create_from_mirror(name, name)
        self.aptly.tasks.wait_for_task_by_id(task.id)
        if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
            if backup_name:
                self.aptly.tasks.wait_for_task_by_id(self.aptly.snapshots.update(backup_name, name).id)
            self.logger.warning('Snapshot for %s creation failed: %s. ' % (name, self.aptly.tasks.show(task.id).state))
            return False
        if backup_name:
            task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
            self.aptly.tasks.wait_for_task_by_id(task.id)
            if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                self.logger.warning('Remove snapshot failed %s : %s' % (backup_name, self.aptly.tasks.show(task.id).state))
        return True

    # Publish a snap called "name" with prefix as name, "bullseye" as the distribution
    # Return None or prefix/distribution
    def __publish_snap(self, name):
        '''Deploy a snapshot.'''
        # Remove a same name publish if exists
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop publish failed %s : %s', name, self.aptly.tasks.show(task.id).state)
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
        else:
            # Only support binary_amd64 and source packages
            extra_param['architectures'] = ['amd64', 'source']
            extra_param['distribution'] = None

        extra_param['source_kind'] = 'snapshot'
        extra_param['sources'] = [{'Name': name}]
        extra_param['sign_skip'] = True
        extra_param['prefix'] = name
        # task = self.aptly.publish.publish(**extra_param)
        task = self.aptly.publish.publish(source_kind='snapshot', sources=extra_param['sources'],
                                          architectures=extra_param['architectures'], prefix=extra_param['prefix'],
                                          distribution=extra_param['distribution'], sign_skip=extra_param['sign_skip'])
        self.aptly.tasks.wait_for_task_by_id(task.id)
        if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
            self.logger.warning('Publish for %s create failed: %s', name, self.aptly.tasks.show(task.id).state)
            return None

        task = None
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
                self.aptly.tasks.wait_for_task_by_id(task.id)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, self.aptly.tasks.show(task.id).state)

        # find and remove the remote(mirror)
        remote_list = self.aptly.mirrors.list()
        for remote in remote_list:
            if remote.name == name:
                task = self.aptly.mirrors.drop(name=name, force=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop mirror failed %s : %s', name, self.aptly.tasks.show(task.id).state)

        # Clean orphans
        task = self.aptly.db.cleanup()
        self.aptly.tasks.wait_for_task_by_id(task.id)

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

        # Static settings: bullseye main
        repo = self.aptly.repos.create(local_name, default_distribution='bullseye', default_component='main')
        return repo

    # upload a Debian package to a local repository
    # Input:
    #       pkg_name: the path-name of the deb file. If the file name
    #                 contains "%3a", it will be replaced by ":".
    #       repo_name: the name of the local repository
    # Output: Bool
    def upload_pkg_local(self, pkg_name, repo_name):
        '''Upload a file into a local repository.
        Please note: here we say file, not package, for binary package, one single
        file is a package; while for source package, it contains several files,
        to upload a source package, you must upload all these files, or, you will
        see nothing in the future, it will not been deployed but just disappeared.
        '''
        # sanity check: pkg_name can be read, repo_name exists and do be local
        if not os.access(pkg_name, os.R_OK):
            self.logger.warning('%s is NOT accessible to read.', pkg_name)
            return False
        if not repo_name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is NOT a well formed name.', repo_name)
            return False

        repo_list = self.aptly.repos.list()
        repo_find = False
        for repo in repo_list:
            if repo_name == repo.name:
                self.logger.debug('%s find, can be used', repo_name)
                repo_find = True
                break

        if not repo_find:
            self.logger.warning('%s not exist, please create it firstly.', repo_name)
            return False

        # For files with ":" in its filename, tools like 'apt' may replace it
        # with '%3a' by mistake, this will cause error in aptly.
        new_name = pkg_name.replace('%3a', ':')
        os.rename(pkg_name, new_name)
        # Add the package into local repo
        self.aptly.files.upload('tmp_folder', new_name)
        task = self.aptly.repos.add_uploaded_file(repo_name, 'tmp_folder', remove_processed_files=True)
        self.aptly.tasks.wait_for_task_by_id(task.id)
        if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
            self.logger.warning('add_upload_file failed %s : %s : %s', new_name, repo_name, self.aptly.tasks.show(task.id).state)
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
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Delete package failed %s : %s' % (pkg_name, self.aptly.tasks.show(task.id).state))

    # Search a package in a set of repos, return True if find, or False
    # repolist: a list of repo names, including local repo and mirror
    # pkg_name: package name
    # architecture: Architecture of the package, now, only check 'source' or not
    # pkg_version:  the version of the package, None means version insensitive
    def pkg_exist(self, repo_list, pkg_name, architecture, pkg_version: Optional[str] = None):
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

    # deploy a loacl repository
    # Input: the name of the local repository
    # Output: None or DebAptDistributionResponse
    def deploy_local(self, name):
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
            self.logger.warning('local repo %s not find.', name)
            return None

        if self.__create_snapshot(name, True):
            return self.__publish_snap(name)
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
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution='bullseye', force_delete=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop publish failed %s : %s', name, self.aptly.tasks.show(task.id).state)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, self.aptly.tasks.show(task.id).state)

        # find and remove the remote(mirror)
        repo_list = self.aptly.repos.list()
        for repo in repo_list:
            if repo.name == name:
                task = self.aptly.repos.delete(reponame=name, force=True)
                self.aptly.tasks.wait_for_task_by_id(task.id)
                if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                    self.logger.warning('Drop repo failed %s : %s', name, self.aptly.tasks.show(task.id).state)

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
            self.aptly.tasks.wait_for_task_by_id(task.id)
            if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                self.logger.warning('Drop publish failed %s : %s', pub.frepix, self.aptly.tasks.show(task.id).state)
        # clean snapshots
        snap_list = self.aptly.snapshots.list()
        self.logger.info('%d snapshot', len(snap_list))
        for snap in snap_list:
            task = self.aptly.snapshots.delete(snapshotname=snap.name, force=True)
            self.aptly.tasks.wait_for_task_by_id(task.id)
            if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                self.logger.warning('Drop snapshot failed %s : %s', snap.name, self.aptly.tasks.show(task.id).state)

        # clean mirrors
        mirror_list = self.aptly.mirrors.list()
        self.logger.info('%d mirror', len(mirror_list))
        for mirror in mirror_list:
            task = self.aptly.mirrors.drop(name=mirror.name, force=True)
            self.aptly.tasks.wait_for_task_by_id(task.id)
            if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                self.logger.warning('Drop mirror failed %s : %s', mirror.name, self.aptly.tasks.show(task.id).state)
        # clean local repos
        repo_list = self.aptly.repos.list()
        self.logger.info('%d repo', len(repo_list))
        for repo in repo_list:
            task = self.aptly.repos.delete(reponame=repo.name, force=True)
            self.aptly.tasks.wait_for_task_by_id(task.id)
            if self.aptly.tasks.show(task.id).state != 'SUCCEEDED':
                self.logger.warning('Drop repo failed %s : %s', repo.name, self.aptly.tasks.show(task.id).state)
        # clean file folders
        file_list = self.aptly.files.list()
        self.logger.info('%d file folder', len(file_list))
        for file in file_list:
            self.aptly.files.delete(file)
        # clean tasks
        self.aptly.tasks.clear()
        # Clean orphans
        task = self.aptly.db.cleanup()
        self.aptly.tasks.wait_for_task_by_id(task.id)
