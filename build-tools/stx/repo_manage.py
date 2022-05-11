#!/usr/bin/python3
# codeing = utf-8

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

import apt
import apt_pkg
import aptly_deb_usage
import argparse
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
import debian.deb822
import debian.debfile
import logging
import os
import requests
from requests.compat import urljoin
import shutil
import urllib.request
import utils


REPOMGR_URL = os.environ.get('REPOMGR_URL')
REPOMGR_DEPLOY_URL = os.environ.get('REPOMGR_DEPLOY_URL')

APTFETCH_JOBS = 10


def get_pkg_ver(pkg_line):
    '''Get package name and package version from a string.'''
    # remove comment string/lines
    if -1 == pkg_line.find('#'):
        line = pkg_line[:-1]
    else:
        line = pkg_line[:pkg_line.find('#')]

    if 2 == len(line.split(' ')):
        pkg_name = line.split(' ')[0]
        pkg_ver = line.split(' ')[1]
    elif 1 == len(line.split(' ')):
        pkg_name = line.split(' ')[0]
        pkg_ver = ''
    else:
        pkg_name = pkg_ver = ''
    return pkg_name, pkg_ver


class AptFetch():
    '''
    Fetch Debian packages from a set of repositories.
    It needs a file contains all trusted upstream repositories, later we
    will search and get packages from these repos.
    Python module apt is used to search and download binary packages and
    apt_pkg for searching source packages. Python module requests is used
    to download source package files.
    '''
    def __init__(self, sources_list, workdir, logger):
        self.logger = logger
        self.aptcache = None
        self.workdir = workdir
        self.__construct_workdir(sources_list)

    def __construct_workdir(self, sources_list):
        '''construct some directories for repo and temporary files'''
        #
        #
        # ├── apt-root               # For apt cache
        # │   └── etc
        # │       └── apt
        # │           └── sources.list
        # └── downloads              # Sub directory to store downloaded packages
        #
        basedir = self.workdir

        # check to see if meta file exist
        if not os.path.exists(sources_list):
            raise Exception('Upstream source list file %s does not exist' % sources_list)

        if os.path.exists(basedir):
            shutil.rmtree(basedir)
        os.makedirs(basedir)

        aptdir = basedir + '/apt-root'
        if not os.path.exists(aptdir + '/etc/apt/'):
            os.makedirs(aptdir + '/etc/apt/')
        destdir = basedir + '/downloads/'
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        shutil.copyfile(sources_list, aptdir + '/etc/apt/sources.list')

    def apt_update(self):
        '''Construct APT cache based on specified rootpath. Just like `apt update` on host'''
        self.aptcache = apt.Cache(rootdir=os.path.join(self.workdir, 'apt-root'))
        ret = self.aptcache.update()
        if not ret:
            raise Exception('APT cache update failed')
        self.aptcache.open()

    # Download a binary package into downloaded folder
    def fetch_deb(self, pkg_name, pkg_version=''):
        '''Download a binary package'''
        if not pkg_name:
            raise Exception('Package name empty')

        destdir = self.workdir + '/downloads/'
        pkg = self.aptcache[pkg_name]
        if not pkg:
            raise Exception('Binary package "%s" not found' % pkg_name)
        if not pkg_version:
            uri = pkg.candidate.uri
        else:
            vers = pkg.versions
            vers_find = False
            for ver in vers:
                if ver.version == pkg_version:
                    uri = ver.uri
                    vers_find = True
                    break
            if not vers_find:
                raise Exception('Binary package "%s %s" not found.' % (pkg_name, pkg_version))
        res = requests.get(uri, stream=True)
        self.logger.debug('Fetch package file %s' % uri)
        with open(os.path.join(destdir, os.path.basename(uri)), 'wb') as download_file:
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    download_file.write(chunk)
        self.logger.info('Binary package %s downloaded.' % pkg_name)

    # Download a source package into downloaded folder
    def fetch_dsc(self, pkg_name, pkg_version=''):
        '''Download a source package'''
        if not pkg_name:
            raise Exception('Package name empty')

        destdir = self.workdir + '/downloads/'
        src = apt_pkg.SourceRecords()
        source_lookup = src.lookup(pkg_name)
        while source_lookup:
            if pkg_version in ['', src.version]:
                break
            source_lookup = src.lookup(pkg_name)
        if not source_lookup:
            raise ValueError("Source package %s not found" % pkg_name)

        # Here the src.files is a list, each one point to a source file
        # Download those source files one by one with requests
        for src_file in src.files:
            res = requests.get(src.index.archive_uri(src_file.path), stream=True)
            self.logger.info('Fetch package file %s', src.index.archive_uri(src_file.path))
            with open(os.path.join(destdir, os.path.basename(src_file.path)), 'wb') as download_file:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        download_file.write(chunk)
        self.logger.info('Source package %s downloaded.' % pkg_name)

    # Download a ubndle of packages into downloaded folder
    # deb_list: binary package list file
    # dsc_list: source package list file
    def Fetch_pkg_list(self, deb_list='', dsc_list=''):
        '''Download a bundle of packages specified through deb_list and dsc_list.'''
        if not deb_list and not dsc_list:
            raise Exception('deb_list and dsc_list, at least one is required.')

        with ThreadPoolExecutor(max_workers=APTFETCH_JOBS) as threads:
            obj_list = []
            # Scan binary package list and download them
            if deb_list:
                if os.path.exists(deb_list):
                    deb_count = 0
                    pkg_list = open(deb_list, 'r')
                    for pkg_line in pkg_list:
                        pkg_name, pkg_version = get_pkg_ver(pkg_line)
                        if not pkg_name:
                            continue
                        deb_count = deb_count + 1
                        # self.fetch_deb(pkg_name, pkg_version)
                        obj = threads.submit(self.fetch_deb, pkg_name, pkg_version)
                        obj_list.append(obj)
                    self.logger.debug('%d binary packages downloaded.' % deb_count)
                else:
                    raise Exception('deb_list file specified but does not exist')

            # Scan source package list and download them
            if dsc_list:
                if os.path.exists(dsc_list):
                    dsc_count = 0
                    pkg_list = open(dsc_list, 'r')
                    for pkg_line in pkg_list:
                        pkg_name, pkg_version = get_pkg_ver(pkg_line)
                        if not pkg_name:
                            continue
                        dsc_count = dsc_count + 1
                        obj = threads.submit(self.fetch_dsc, pkg_name, pkg_version)
                        obj_list.append(obj)
                    self.logger.debug('%d source packages downloaded.' % dsc_count)
                else:
                    raise Exception('dsc_list file specified but does not exist')
            for future in as_completed(obj_list):
                self.logger.debug('download result %s' % future.result())


class RepoMgr():
    '''
    Repository management, based on pulp or aptly, mainly used for OBS and LAT
    Two kind of repositories: local repo and remote one.
    remote repo: mirror of another repository. shouldn't insert or remove packages from it..
    local repo: a local repository, we can insert/remove packages into/from them.
    '''
    def __init__(self, repoType, repoURL, workdir, logger):
        if repoType == 'aptly':
            self.repo = aptly_deb_usage.Deb_aptly(repoURL, logger)
        else:
            raise Exception('Currently, only aptly repository supported')

        self.aptcache = None
        self.logger = logger
        self.workdir = workdir

    def __sync_deb(self, check_list, deb_list, apt_fetch):
        '''Sync binary packages'''
        deb_count = 0
        pkg_list = open(deb_list, 'r')
        # scan the deb list
        for pkg_line in pkg_list:
            pkg_name, pkg_ver = get_pkg_ver(pkg_line)
            if '' == pkg_name:
                continue

            # Search the package in check_list, if not find, download it
            if self.repo.pkg_exist(check_list, pkg_name, 'binary', pkg_ver):
                continue
            # print(pkg_name, pkg_ver)
            deb_count = deb_count + 1
            apt_fetch.fetch_deb(pkg_name, pkg_ver)
        pkg_list.close()
        self.logger.info('%d binary packages downloaded.' % deb_count)

    def __sync_dsc(self, check_list, dsc_list, apt_fetch):
        '''Sync source packages'''
        dsc_count = 0
        # scan the dsc list
        for pkg_line in open(dsc_list, 'r'):
            pkg_name, pkg_ver = get_pkg_ver(pkg_line)
            if '' == pkg_name:
                continue
            # Search the package in check_list, if not find, download it
            if self.repo.pkg_exist(check_list, pkg_name, 'source', pkg_ver):
                continue
            # print(pkg_name, pkg_ver)
            dsc_count = dsc_count + 1
            apt_fetch.fetch_dsc(pkg_name, pkg_ver)
        self.logger.info('%d source packages downloaded.' % dsc_count)

    # Download a bundle of packages and deployed them through repository
    # repo_name: the repository used to deploy these packages
    # no_clear: Not delete downloaded package files, for debug
    # kwarge:sources_list: Where we should fetch packages from.
    # kwarge:deb_list: file contains binary package list
    # kwarge:dsc_ist: file contains source package list
    # Output: None
    def download(self, repo_name, no_clear=False, **kwargs):
        '''Download specified packages and deploy them through a specified local repo.'''
        sources_list = kwargs['sources_list']
        if 'deb_list' not in kwargs.keys():
            deb_list = ''
        else:
            deb_list = kwargs['deb_list']
        if 'dsc_list' not in kwargs.keys():
            dsc_list = ''
        else:
            dsc_list = kwargs['dsc_list']
        # print(sources_list)
        if not deb_list and not dsc_list:
            raise Exception('deb_list and dsc_list, at least one is required.')
        if not self.repo.create_local(repo_name):
            raise Exception('Local repo created failed, Please double check'
                            ' if the repo exist already.')

        # Download packages from remote repo
        apt_fetch = AptFetch(sources_list, self.workdir, self.logger)
        apt_fetch.apt_update()
        apt_fetch.Fetch_pkg_list(deb_list=deb_list, dsc_list=dsc_list)

        # Add packages into local repo
        destdir = self.workdir + '/downloads/'
        package_files = set()
        for filename in os.listdir(destdir):
            package_files.add(os.path.join(destdir, filename))
        self.repo.upload_pkg_local(package_files, repo_name)

        # Deploy local repo
        repo_str = self.repo.deploy_local(repo_name)
        if not no_clear:
            shutil.rmtree(self.workdir)
        self.logger.info('New local repo can be accessed through: %s' % repo_str)

    # We need a bundle packages been deployed through a serials of repositories
    # We have:
    # -) a serials of atply/pulp repositories already contains some packages;
    # -) an aptly/pulp repository can be used to deploy downloaded packages
    # -) a serials of upstream Debian repository
    # -) Two text files list binary and source packages we need
    #
    # SYNC will download all packages we haven't and deployed them through
    # a local repository.
    # reponame: name of a local repository used to deploy downloaded packages
    # repo_list: String separated with space, contains serials of aptly/pulp
    #            repos can be used for OBS/LAT.
    # no_clear: do not delete downloaded packages. For debug
    # kwargs:sources_list: file contains trusted upstream repositories
    # kwargs:deb_list: file lists all needed binary packages
    # kwargs:dsc_list: file lists all needed source packages
    # Output: None
    def sync(self, repo_name, repo_list, no_clear=False, **kwargs):
        '''
        Sync a set of repositories with spaecified package lists, any package
        missed, download and deploy through a specified local repo
        '''
        if 'deb_list' not in kwargs.keys():
            deb_list = ''
        else:
            deb_list = kwargs['deb_list']
        if 'dsc_list' not in kwargs.keys():
            dsc_list = ''
        else:
            dsc_list = kwargs['dsc_list']

        if not deb_list and not dsc_list:
            raise Exception('deb_list and dsc_list, at least one is required.')
        # construct repo list will be checkd
        local_list = self.repo.list_local(quiet=True)
        remote_list = self.repo.list_remotes(quiet=True)
        # Specified local repo must exist, or failed
        if repo_name not in local_list:
            raise Exception('Sync failed, local repo does not exist, create it firstly')
        # Make sure all repos in check_list exist in aptly/pulp database.
        check_list = [repo_name]
        for repo in repo_list.split():
            if repo in local_list or repo in remote_list:
                check_list.append(repo)
            else:
                self.logger.warn('%s in the list but does not exists.' % repo)

        # Download missing packages from remote repo
        apt_fetch = AptFetch(kwargs['sources_list'], self.workdir, self.logger)
        apt_fetch.apt_update()
        # if os.path.exists(deb_list):
        self.__sync_deb(check_list, deb_list, apt_fetch)
        # if os.path.exists(dsc_list):
        self.__sync_dsc(check_list, dsc_list, apt_fetch)

        # Add packages into local repo
        destdir = self.workdir + '/downloads/'
        pkg_files = set()
        for filename in os.listdir(destdir):
            pkg_files.add(os.path.join(destdir, filename))
        self.repo.upload_pkg_local(pkg_files, repo_name)

        # Deploy local repo
        repo_str = self.repo.deploy_local(repo_name)
        if not no_clear:
            shutil.rmtree(self.workdir)
        self.logger.info('local repo can be accessed through: %s ' % repo_str)

    # Merge all packages of several repositories into a new publication(aptly)
    # NOTE: aptly only. Not find similar feature in pulp...
    def merge(self, name, source_snapshots):
        '''Merge several repositories into a new aptly publication.'''
        return self.repo.merge_repos(name, source_snapshots.split(','))

    # Construct a repository mirror to an upstream Debian repository
    # kwargs:url: URL of the upstream repo (http://deb.debian.org/debian)
    # kwargs:distribution: the distribution of the repo (bullseye)
    # kwargs:component: component of the repo (main)
    # kwargs:architecture: architecture of the repo, "all" is always enabled. (amd64)
    # kwargs:with_sources: include source packages, default is False.
    # Output: None
    def mirror(self, repo_name, **kwargs):
        '''Construct a mirror based on a debian repository.'''
        url = kwargs['url']
        distribution = kwargs['distribution']
        component = kwargs['component']
        architectures = kwargs['architectures']
        if 'with_sources' not in kwargs.keys():
            with_sources = False
        else:
            with_sources = kwargs['with_sources']
        self.repo.remove_remote(repo_name)
        if with_sources:
            self.repo.create_remote(repo_name, url, distribution,
                                    components=[component],
                                    architectures=[architectures],
                                    with_sources=True)
        else:
            self.repo.create_remote(repo_name, url, distribution,
                                    components=[component],
                                    architectures=[architectures])
        repo_str = self.repo.deploy_remote(repo_name)
        self.logger.info('New mirror can be accessed through: %s' % repo_str)

    # List all repositories
    # Output: None
    def list(self):
        '''List all repos.'''
        self.repo.list_remotes()
        self.repo.list_local()

    # Clean all packages and repositories
    def clean(self):
        '''Clear all meta files. Construct a clean environment'''
        self.repo.clean_all()

    # list a repository
    # repo_name: the name of the repo been listed.
    # Output: True is all works in order
    def list_pkgs(self, repo_name, quiet=False):
        '''List a specified repository.'''
        local_list = self.repo.list_local(quiet=True)
        remote_list = self.repo.list_remotes(quiet=True)
        pkg_list = []
        for repo in local_list:
            if repo == repo_name:
                self.logger.info('List a local repo')
                pkgs = self.repo.pkg_list([repo])
                pkg_list.extend(pkgs)
                if not quiet:
                    self.logger.info("Local repo %s:" % repo_name)
                    for pkg in sorted(pkgs):
                        self.logger.info("  %s" % pkg)
        for repo in remote_list:
            if repo == repo_name:
                self.logger.info('List a remote mirror')
                pkgs = self.repo.pkg_list([repo])
                pkg_list.extend(pkgs)
                if not quiet:
                    self.logger.info("Remote repo %s:" % repo_name)
                    for pkg in sorted(pkgs):
                        self.logger.info("  %s" % pkg)
        return pkg_list

    # delete a repository
    # repo_name: the name of the repo been deleted.
    # Output: True is all works in order
    def remove_repo(self, repo_name):
        '''Remove a specified repository.'''
        local_list = self.repo.list_local(quiet=True)
        remote_list = self.repo.list_remotes(quiet=True)
        for repo in local_list:
            if repo == repo_name:
                self.logger.info('Remove a local repo')
                self.repo.remove_local(repo)
                return True
        for repo in remote_list:
            if repo == repo_name:
                self.logger.info('Remove a remote mirror')
                self.repo.remove_remote(repo)
                return True
        self.logger.warn("Remove repo failed: repo '%s' not found" % repo_name)
        return False

    # Before uploading a source package into a local repo, scan all repos,
    # find all duplicate files with different size.
    # dsc: Dsc data of the source package. <class 'debian.deb822.Dsc'>
    # Return a dictionary: {repo_1: {file_a, ...}, ...}
    #   Input dsc contains file_a, while repo_1 also contains such a file
    #   with different size.
    def __check_orig_files(self, dsc):
        different_files = {}
        repo_list = self.repo.list_local(quiet=True)
        repo_list += self.repo.list_remotes(quiet=True)
        for repo in repo_list:
            for meta_file in dsc['Files']:
                if meta_file['name'].find('.orig.'):
                    new_file_size = meta_file['size']
                    if dsc['Source'].startswith('lib'):
                        prefix_dir = dsc['Source'][:4] + '/' + dsc['Source']
                    else:
                        prefix_dir = dsc['Source'][0] + '/' + dsc['Source']
                    target_path = repo + '/pool/main/' + prefix_dir + '/' + meta_file['name']
                    target_url = urljoin(REPOMGR_DEPLOY_URL, target_path)
                    try:
                        orig_file = urllib.request.urlopen(target_url)
                    except Exception:
                        # no such file in repo, that is good
                        self.logger.debug('%s does not contain %s' % (repo, meta_file['name']))
                        continue
                    if orig_file.length != int(new_file_size):
                        self.logger.debug('File %s is not same as the one in %s.' %
                                          (meta_file['name'], repo))
                        if repo not in different_files.keys():
                            different_files[repo] = {meta_file['name']}
                        else:
                            different_files[repo].add(meta_file['name'])
        return different_files

    # upload a Debian package and deploy it
    # repo_name: the name of the repository used to contain and deploy the package
    # package: pathname of the package(xxx.deb or xxx.dsc) to be uploaded
    # Output: True if all works.
    def upload_pkg(self, repo_name, package):
        '''Upload a Debian package into a specified repository.'''
        local_list = self.repo.list_local(quiet=True)
        if repo_name not in local_list:
            self.logger.info('upload_pkg: repository %s does not exist, creating it.' % repo_name)
            self.repo.create_local(repo_name)

        if not package:
            # No repo found, no package specified, just create & deploy the repo and return
            self.repo.deploy_local(repo_name)
            return True

        self.logger.debug('upload_pkg: upload package %s into %s' % (package, repo_name))
        if '.deb' == os.path.splitext(package)[-1]:
            pkg_type = 'binary'
            try:
                deb = debian.debfile.DebFile(package, 'r').debcontrol()
            except Exception as e:
                self.logger.error('Error: %s' % e)
                self.logger.error('Binary package %s read error.' % package)
                raise Exception('Binary package error.')
            pkg_version = deb['Version']
            pkg_name = deb['Package']
            self.repo.upload_pkg_local({package}, repo_name)
        elif '.dsc' == os.path.splitext(package)[-1]:
            pkg_type = 'source'
            try:
                dsc = debian.deb822.Dsc(open(package, 'r'))
            except Exception as e:
                self.logger.error('Error: %s' % e)
                self.logger.error('Source package %s read error.' % package)
                raise Exception('Source package error.')
            pkg_name = dsc['Source']
            pkg_version = dsc['Version']

            # In case there is already an *.orig.* file with different size in any repos,
            # refuse to upload it.
            different_files = self.__check_orig_files(dsc)
            if different_files:
                for repo, meta_files in different_files.items():
                    self.logger.error('%s contains different file: %s' % (repo, str(meta_files)))
                    self.logger.error('Package %s upload failed.  Repo %s already contains '
                                      'file %s with different content.' %
                                      (package, repo, str(meta_files)))
                return False

            pkg_files = set()
            for meta_file in dsc['Files']:
                pkg_files.add(os.path.join(os.path.dirname(package), meta_file['name']))
            pkg_files.add(package)
            self.repo.upload_pkg_local(pkg_files, repo_name)
        else:
            self.logger.warning('Only Debian style files, like deb and dsc, are supported.')
            return False
        self.logger.debug('upload_pkg: package %s been uploaded into %s' % (package, repo_name))
        self.repo.deploy_local(repo_name)

        # Double check if the package been uploaded successfully
        if not self.search_pkg(repo_name, pkg_name, pkg_version, pkg_type == 'binary'):
            self.logger.error('upload_pkg: verify failed, no %s package %s %s in %s'
                              % (pkg_type, pkg_name, pkg_version, repo_name))
            return False
        return True

    # search a package from a repository
    # repo_name: name of the repository to search the package in
    # pkg_name: name of the Debian package to be searched
    # pkg_version: version number of the package to be searched
    # binary: binary package or source one?
    # Output: True if find, or False
    def search_pkg(self, repo_name, pkg_name, pkg_version=None, binary=True):
        '''Find a package from a specified repo.'''
        repo_find = False
        repo = None
        r_list = self.repo.list_local(quiet=True)
        for repo in r_list:
            if repo == repo_name:
                repo_find = True
        r_list = self.repo.list_remotes(quiet=True)
        for repo in r_list:
            if repo == repo_name:
                repo_find = True
        if not repo_find:
            self.logger.error('Search package, repository does not exist.')
            return False

        if binary:
            pkg_type = 'binary'
        else:
            pkg_type = 'source'
        if not self.repo.pkg_exist([repo_name], pkg_name, pkg_type, pkg_version):
            self.logger.info('Search %s package %s, not found.' % (pkg_type, pkg_name))
            return False
        return True

    # Delete a Debian package from a local repository
    # repo_name: name of the LOCAL repository to delete the package from
    # pkg_name: name of the binary package to be deleted
    # pkg_type: 'source' or 'binary'
    # pkg_version: version number of the package to be deleted
    # Output: True if find and delete, or False
    def delete_pkg(self, repo_name, pkg_name, pkg_type, pkg_version=''):
        '''Find and delete a binary package from a specified local repo.'''
        repo_find = False
        repo = None
        if pkg_type not in {'binary', 'source'}:
            self.logger.error('Delete package, pkg_type must be one of '
                              'either "binary" or "source".')
            return False
        if not repo_name.startswith(aptly_deb_usage.PREFIX_LOCAL):
            self.logger.error('Delete package, only local repositories support this operation.')
            return False
        local_list = self.repo.list_local(quiet=True)
        for repo in local_list:
            if repo == repo_name:
                repo_find = True
        if not repo_find:
            self.logger.error('Delete package, repository does not exist.')
            return False

        if not self.repo.pkg_exist([repo_name], pkg_name, pkg_type, pkg_version):
            self.logger.info('Delete package, package not found.')
            return False

        self.repo.delete_pkg_local(repo_name, pkg_name, pkg_type, pkg_version)
        self.repo.deploy_local(repo_name)
        return True


# Simple example on using this class.
applogger = logging.getLogger('repomgr')
utils.set_logger(applogger)


def _handleDownload(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, args.basedir, applogger)
    kwargs = {'sources_list': args.sources_list, 'deb_list': args.deb_list,
              'dsc_list': args.dsc_list}
    repomgr.download(args.name, **kwargs, no_clear=args.no_clear)


def _handleSync(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, args.basedir, applogger)
    kwargs = {'sources_list': args.sources_list, 'deb_list': args.deb_list,
              'dsc_list': args.dsc_list}
    repomgr.sync(args.name, args.repo_list, **kwargs, no_clear=args.no_clear)


def _handleMirror(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    kwargs = {'url': args.url, 'distribution': args.distribution, 'component': args.component,
              'architectures': args.architectures, 'with_sources': args.with_sources}
    repomgr.mirror(args.name, **kwargs)


def _handleMerge(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.merge(args.name, args.repo_list)


def _handleUploadPkg(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.upload_pkg(args.repository, args.package)


def _handleDeletePkg(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.delete_pkg(args.repository, args.package_name, args.package_type,
                       pkg_version=args.package_version)


def _handleSearchPkg(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    if args.package_type == 'binary':
        repomgr.search_pkg(args.repository, args.package_name, pkg_version=args.package_version,
                           binary=True)
    else:
        repomgr.search_pkg(args.repository, args.package_name, pkg_version=args.package_version,
                           binary=False)


def _handleRemoveRope(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.remove_repo(args.repository)


def _handleList(_args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.list()


def _handleListPkgs(args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.list_pkgs(args.repository)


def _handleClean(_args):
    repomgr = RepoMgr('aptly', REPOMGR_URL, '/tmp', applogger)
    repomgr.clean()


def subcmd_download(subparsers):
    download_parser = subparsers.add_parser('download',
                                            help='Download specified packages and deploy them '
                                                 'through a new repository.\n\n')
    download_parser.add_argument('--name', '-n', help='Name of the local repo', required=False,
                                 default='deb-local-down')
    download_parser.add_argument('--deb_list', help='Binary package list file', required=False,
                                 default='')
    download_parser.add_argument('--dsc_list', help='Source package list file', required=False,
                                 default='')
    download_parser.add_argument('--basedir', help='Temporary folder to store packages',
                                 required=False, default='/tmp/repomgr')
    download_parser.add_argument('--sources_list', help='Upstream sources list file',
                                 default='./sources.list')
    download_parser.add_argument('--no-clear', help='Not remove temporary files',
                                 action='store_true')
    download_parser.set_defaults(handle=_handleDownload)


def subcmd_sync(subparsers):
    sync_parser = subparsers.add_parser('sync',
                                        help='Sync a set of repositories with specified package'
                                             'lists..\n\n')
    sync_parser.add_argument('--name', '-n', help='Name of the local repo', required=False,
                             default='deb-local-sync')
    sync_parser.add_argument('--repo_list', '-l', help='a set of local repos', required=False,
                             default=[])
    sync_parser.add_argument('--deb_list', help='Binary package list file', required=False,
                             default='')
    sync_parser.add_argument('--dsc_list', help='Source package list file', required=False,
                             default='')
    sync_parser.add_argument('--basedir', help='Temporary folder to store packages',
                             required=False, default='/tmp/repomgr')
    sync_parser.add_argument('--sources_list', help='Upstream sources list file',
                             default='./sources.list')
    sync_parser.add_argument('--no-clear', help='Not remove temporary files',
                             action='store_true')
    sync_parser.set_defaults(handle=_handleSync)


def subcmd_mirror(subparsers):
    mirror_parser = subparsers.add_parser('mirror',
                                          help='Construct a mirror based on a remote'
                                               'repository.\n\n')
    mirror_parser.add_argument('--name', '-n', help='Name of the mirror', required=False,
                               default='deb-remote-tmp')
    mirror_parser.add_argument('--url', help='URL of remote repository', required=False,
                               default='http://nginx.org/packages/debian/')
    mirror_parser.add_argument('--distribution', '-d', help='distribution name', required=False,
                               default='buster')
    mirror_parser.add_argument('--component', '-c', help='component name', required=False,
                               default='nginx')
    mirror_parser.add_argument('--architectures', '-a', help='architectures', required=False,
                               default='amd64')
    mirror_parser.add_argument('--with-sources', '-s', help='include source packages',
                               action='store_true')
    mirror_parser.set_defaults(handle=_handleMirror)


def subcmd_merge(subparsers):
    merge_parser = subparsers.add_parser('merge',
                                         help='Merge several repositories into a new publication.\n\n')
    merge_parser.add_argument('--name', '-n', help='Name of the new merged publication', required=True)
    merge_parser.add_argument('--repo_list', '-l', help='a set of repos, seperate by comma', required=True)
    merge_parser.set_defaults(handle=_handleMerge)


def main():
    # command line arguments
    parser = argparse.ArgumentParser(add_help=True,
                                     description='Repository management Tool',
                                     epilog='''Tips: Use %(prog)s --help to get help for all of '
                                            'parameters\n\n''')
    subparsers = parser.add_subparsers(title='Repo control Commands:',
                                       help='sub-command for repo-ctl\n\n')

    # Three functions for three sub commands: pylint checking(too-many-statements)
    subcmd_download(subparsers)
    subcmd_sync(subparsers)
    subcmd_mirror(subparsers)
    subcmd_merge(subparsers)

    clean_parser = subparsers.add_parser('clean', help='Clear all aptly repos.\n\n')
    clean_parser.set_defaults(handle=_handleClean)

    remove_repo_parser = subparsers.add_parser('remove_repo',
                                               help='Remove a specific repository.\n\n')
    remove_repo_parser.add_argument('--repository', '-r',
                                    help='Name of the repo to be removed')
    remove_repo_parser.set_defaults(handle=_handleRemoveRope)

    upload_pkg_parser = subparsers.add_parser('upload_pkg',
                                              help='Upload a Debian package into a specific '
                                                   'repository.\n\n')
    upload_pkg_parser.add_argument('--package', '-p',
                                   help='Debian package to be uploaded.', required=False)
    upload_pkg_parser.add_argument('--repository', '-r',
                                   help='Repository used to deploy this package.')
    upload_pkg_parser.set_defaults(handle=_handleUploadPkg)

    delete_pkg_parser = subparsers.add_parser('delete_pkg',
                                              help='Delete a specified Debian package from a '
                                                   'specified repository.\n\n')
    delete_pkg_parser.add_argument('--package_name', '-p', help='Package name to be deleted.')
    delete_pkg_parser.add_argument('--package_type', '-t',
                                   help='Package type to be deleted, "binary" or "source".')
    delete_pkg_parser.add_argument('--package_version', '-v',
                                   help='Version number of the package.', required=False)
    delete_pkg_parser.add_argument('--repository', '-r', help='Local Repository to delete the '
                                   'package from.')
    delete_pkg_parser.set_defaults(handle=_handleDeletePkg)

    search_pkg_parser = subparsers.add_parser('search_pkg',
                                              help='Search a specified package from a specified '
                                                   'repository.\n\n')
    search_pkg_parser.add_argument('--package_name', '-p', help='package to be looking for.')
    search_pkg_parser.add_argument('--package_version', '-v', help='The version of the package.',
                                   required=False)
    search_pkg_parser.add_argument('--repository', '-r',
                                   help='The Repository to search the package in.')
    search_pkg_parser.add_argument('--package_type', '-t', help='binary or source',
                                   required=False)
    search_pkg_parser.set_defaults(handle=_handleSearchPkg)

    list_parser = subparsers.add_parser('list',
                                        help='List all aptly repos, including local repo and '
                                             'remote mirror.\n\n')
    list_parser.set_defaults(handle=_handleList)

    list_pkgs_parser = subparsers.add_parser('list_pkgs',
                                        help='List contents of a specific repo.\n\n')
    list_pkgs_parser.add_argument('--repository', '-r',
                                  help='Name of the repo to be listed')
    list_pkgs_parser.set_defaults(handle=_handleListPkgs)

    args = parser.parse_args()

    if hasattr(args, 'handle'):
        args.handle(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
