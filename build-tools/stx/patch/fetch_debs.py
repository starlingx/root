#
# Copyright (c) 2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Fetch deb and subdebs from the build system
'''
import os
import sys
import logging
import shutil

sys.path.append('..')
import debsentry
import repo_manage
import utils
import discovery

STX_DEFAULT_DISTRO_CODENAME = discovery.STX_DEFAULT_DISTRO_CODENAME
STX_DISTRO_DEBIAN_BULLSEYE = discovery.STX_DISTRO_DEBIAN_BULLSEYE
BUILD_ROOT = os.environ.get('MY_BUILD_PKG_DIR')

DEB_CONFIG_DIR = 'stx-tools/debian-mirror-tools/config/'
PKG_LIST_DIR = os.path.join(os.environ.get('MY_REPO_ROOT_DIR'), DEB_CONFIG_DIR, 'debian/distro')

logger = logging.getLogger('fetch_debs')
utils.set_logger(logger)


class FetchDebs(object):
    def __init__(self):
        self.need_dl_stx_pkgs = []
        self.need_dl_binary_pkgs = []
        self.output_dir = os.path.join(BUILD_ROOT, 'dl_debs')
        self.apt_src_file = os.path.join(BUILD_ROOT, 'aptsrc')

        self.setup_apt_source()
        self.debs_fetcher = repo_manage.AptFetch(logger, self.apt_src_file, self.output_dir)

    def get_debs_clue(self, btype):
        if btype != 'rt':
            btype = 'std'
        return os.path.join(BUILD_ROOT, 'caches', btype + '_debsentry.pkl')

    def get_all_debs(self):
        all_debs = set()
        debs_clue_std = self.get_debs_clue('std')
        debs_clue_rt = self.get_debs_clue('rt')
        for pkg in self.need_dl_stx_pkgs:
            subdebs_std = debsentry.get_subdebs(debs_clue_std, pkg, logger)
            subdebs_rt = debsentry.get_subdebs(debs_clue_rt, pkg, logger)
            if not subdebs_std and not subdebs_rt:
                logger.error(f"Failed to get subdebs for package {pkg} from local debsentry cache")
                sys.exit(1)

            if subdebs_std:
                all_debs.update(set(subdebs_std))
            if subdebs_rt:
                all_debs.update(set(subdebs_rt))

        return all_debs

    def setup_apt_source(self):
        # clean up the output dir
        if os.path.exists(self.output_dir):
            shutil.rmtree(self.output_dir)

        os.makedirs(self.output_dir, exist_ok=True)

        try:
            with open(self.apt_src_file, 'w') as f:
                repo_url = os.environ.get('REPOMGR_DEPLOY_URL')
                apt_item = ' '.join(['deb [trusted=yes]', repo_url + 'deb-local-build', 'bullseye', 'main\n'])
                f.write(apt_item)
                apt_item = ' '.join(['deb [trusted=yes]', repo_url + 'deb-local-binary', 'bullseye', 'main\n'])
                f.write(apt_item)
                logger.debug(f'Created apt source file {self.apt_src_file} to download debs')
        except Exception as e:
            logger.error(str(e))
            logger.error('Failed to create the apt source file')
            sys.exit(1)

    def fetch_stx_packages(self):
        '''
        Download all debs and subdebs from the build system
        Save the files to $BUILD_ROOT/dl_debs
        '''
        dl_debs = self.get_all_debs()
        if not dl_debs:
            logger.warning('No STX packages were found')
            return
        else:
            dl_debs_dict = {}
            for deb in dl_debs:
                # dl_debs_with_ver.append(deb.replace('_', ' '))
                name, version = deb.split('_')
                if name not in dl_debs_dict:
                    dl_debs_dict[name] = version
            logger.debug('Debs found: %s', dl_debs_dict)

        # filter list based on stx-std.lst - Depecrated on master, replaced by debian_iso_image.inc on each repo
        stx_pkg_list_file = self.get_debian_pkg_iso_list()

        debs_to_remove = []
        for deb in dl_debs_dict.keys():
            # try to find the deb in the package list
            if deb not in stx_pkg_list_file:
                # remove if not found in all lines
                debs_to_remove.append(deb)

        for deb in debs_to_remove:
            # If package is explicitly in the patch recipe it should NOT be removed
            if deb not in self.need_dl_stx_pkgs:
                dl_debs_dict.pop(deb)

        logger.debug(f'Package list after filtering:{dl_debs_dict}')

        logger.info(f'Total debs need to be downloaded: {len(dl_debs_dict)}')
        dl_debs_with_ver = [f'{k} {v}' for k, v in dl_debs_dict.items()]
        fetch_ret = self.download(dl_debs_with_ver)
        dl_bin_debs_dir = os.path.join(self.output_dir, 'downloads/binary')
        if len(fetch_ret['deb-failed']) == 0:
            logger.info(f'Successfully downloaded STX debs to {dl_bin_debs_dir}')
        else:
            logger.error(f'Failed to downloaded STX debs to {dl_bin_debs_dir}')

    def get_debian_pkg_iso_list(self):
        pkgs = []
        cgcs_root_dir = os.environ.get('MY_REPO')
        package_file_name = 'debian_iso_image.inc'

        for root, dirs, files in os.walk(cgcs_root_dir):
            for file in files:
                if file == package_file_name:
                    with open(os.path.join(root, package_file_name), 'r') as f:
                        pkgs.extend(line.strip() for line in f if line.strip() and not line.startswith('#'))
        return pkgs

    def fetch_external_binaries(self, codename=STX_DEFAULT_DISTRO_CODENAME):
        '''
        Download all binaries from the build system
        apt_item = apt_item + ' '.join(['deb [trusted=yes]', repo_url + 'deb-local-binary', 'bullseye', 'main\n'])
        '''
        # Get debs from base-bullseye.lst
        # https://opendev.org/starlingx/tools/src/branch/master/debian-mirror-tools/config/debian/common/base-bullseye.lst
        if not self.need_dl_binary_pkgs:
            logger.debug("No binary packages to download")
            return

        all_debs = set()
        
        package_list - Nnoe
        if codename == STX_DISTRO_DEBIAN_BULLSEYE:
            package_list = os.path.join(os.environ.get('MY_REPO_ROOT_DIR'),
                                        'stx-tools/debian-mirror-tools/config/debian',
                                        'common',
                                        base-' + codename + 'bullseye.lst')
        if package_list is None or not os.path.exists(package_list):
            package_list = os.path.join(os.environ.get('MY_REPO_ROOT_DIR'),
                                        'stx-tools/debian-mirror-tools/config/debian',
                                        codename,
                                        'common',
                                        'base-' + codename + 'bullseye.lst')

        # find pkgs in the list file
        logger.debug(f'Packages to find {self.need_dl_binary_pkgs}')
        for pkg in self.need_dl_binary_pkgs:
            logger.debug(f'checking {pkg}')
            with open(package_list, 'r') as f:
                for line in f.readlines():
                    if pkg == line.split()[0]:
                        logger.debug(f'Line for package {pkg} found')
                        pkg_entry = ' '.join(line.split()[:2])
                        logger.debug(f'Adding "{pkg_entry}" to be downloaded')
                        all_debs.add(pkg_entry)
                        break
                else:
                    logger.error(f"Package '{pkg}' not found in the package list")
                    sys.exit(1)

        logger.debug('Binary packages to download:%s', all_debs)
        fetch_ret = self.download(all_debs)
        dl_bin_debs_dir = os.path.join(self.output_dir, 'downloads/binary')
        if len(fetch_ret['deb-failed']) == 0:
            logger.info(f'Successfully downloaded external debs to {dl_bin_debs_dir} \n')
        else:
            logger.info(f'Failed to downloaded external debs to {dl_bin_debs_dir} \n')

    def download(self, all_debs):
        try:
            logger.debug('Downloading debs...')
            fetch_ret = self.debs_fetcher.fetch_pkg_list(all_debs)
        except Exception as e:
            logger.error(str(e))
            logger.error('Exception has when fetching debs with repo_manage')
            sys.exit(1)
        return fetch_ret


if __name__ == '__main__':
    fetch_debs = FetchDebs()
    # set the packages you want to download
    fetch_debs.need_dl_std_pkgs = ['sysinv']
    fetch_debs.need_dl_rt_pkgs = ['']
    fetch_debs.need_dl_binary_pkgs = ['tzdata', 'curl', 'apache2']

    fetch_debs.fetch_stx_packages()
