#
# Copyright (c) 2023-2025 Wind River Systems, Inc.
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

from exceptions import FetchDebsError

sys.path.append('..')
import debsentry
import repo_manage
import utils
import discovery


STX_DEFAULT_DISTRO_CODENAME = discovery.STX_DEFAULT_DISTRO_CODENAME


logger = logging.getLogger('fetch_debs')
utils.set_logger(logger)


class FetchDebs(object):

    def __init__(self,
                 need_dl_stx_pkgs=None,
                 need_dl_binary_pkgs=None):

        self.need_dl_stx_pkgs = need_dl_stx_pkgs if need_dl_stx_pkgs else []
        self.need_dl_binary_pkgs = need_dl_binary_pkgs if need_dl_binary_pkgs else []

        # In general: /localdisk/designer/<USER>/<PROJECT>
        self.designer_root = utils.get_env_variable('MY_REPO_ROOT_DIR')

        # In general: /localdisk/loadbuild/<USER>/<PROJECT>
        self.loadbuild_root = utils.get_env_variable('MY_BUILD_PKG_DIR')

        # TODO: These directories should be inputs, not hardcoded.
        self.output_dir = os.path.join(self.loadbuild_root, 'dl_debs')
        self.apt_src_file = os.path.join(self.loadbuild_root, 'aptsrc')

        self.dist_codename = os.environ.get('DIST', STX_DEFAULT_DISTRO_CODENAME)

        self.setup_apt_source()
        self.debs_fetcher = repo_manage.AptFetch(logger, self.apt_src_file, self.output_dir)


    def get_debs_clue(self, btype):
        if btype != 'rt':
            btype = 'std'
        return os.path.join(self.loadbuild_root, 'caches', btype + '_debsentry.pkl')


    def get_all_debs(self):
        all_debs = []
        failed_pkgs = []
        debs_clue_std = self.get_debs_clue('std')
        debs_clue_rt = self.get_debs_clue('rt')

        logger.debug("Binaries found for each STX source pkg:")
        for pkg in self.need_dl_stx_pkgs:
            subdebs = []
            subdebs_std = debsentry.get_subdebs(debs_clue_std, pkg, logger)
            subdebs_rt = debsentry.get_subdebs(debs_clue_rt, pkg, logger)
            if not subdebs_std and not subdebs_rt:
                failed_pkgs.append(pkg)
                continue

            if subdebs_std:
                subdebs.extend(subdebs_std)
            if subdebs_rt:
                subdebs.extend(subdebs_rt)

            logger.debug("%s: %s", pkg, ', '.join(subdebs))
            all_debs.extend(set(subdebs))

        if failed_pkgs:
            logger.error("Failed to get binaries for STX source packages: %s", ", ".join(failed_pkgs))
            sys.exit(1)

        return all_debs

    def setup_apt_source(self):
        # clean up the output dir
        if os.path.exists(self.output_dir):
            shutil.rmtree(self.output_dir)

        os.makedirs(self.output_dir, exist_ok=True)

        try:
            with open(self.apt_src_file, 'w') as file:
                repo_url = utils.get_env_variable('REPOMGR_DEPLOY_URL')

                apt_repo = f"deb [trusted=yes] {repo_url}deb-local-build {self.dist_codename} main\n"
                file.write(apt_repo)
                apt_repo = f"deb [trusted=yes] {repo_url}deb-local-binary {self.dist_codename} main\n"
                file.write(apt_repo)

                logger.debug(f'Created apt source file {self.apt_src_file} to download debs')
        except Exception as e:
            logger.error(str(e))
            logger.error('Failed to create the apt source file')
            sys.exit(1)


    def fetch_stx_packages(self):
        '''
        Download all debs and subdebs from the build system
        Save the files to ${BUILD_ROOT}/dl_debs
        '''

        if not self.need_dl_stx_pkgs:
            logger.warning("No STX packages to download")
            return

        dl_debs = self.get_all_debs()
        if not dl_debs:
            msg = f"No STX binaries were found that matched source pkgs: {self.need_dl_stx_pkgs}"
            raise Exception(msg)

        dl_debs_dict = {}
        for deb in dl_debs:
            # dl_debs_with_ver.append(deb.replace('_', ' '))
            name, version = deb.split('_')
            if name not in dl_debs_dict:
                dl_debs_dict[name] = version

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

        logger.info(f'STX packages selected:')
        for name,version in dl_debs_dict.items():
            logger.info('%s  %s', name, version)

        dl_bin_debs_dir = os.path.join(self.output_dir, 'downloads/binary')

        logger.info(f'Fetching STX debs to {dl_bin_debs_dir} \n')
        dl_debs_with_ver = [f'{k} {v}' for k, v in dl_debs_dict.items()]
        fetch_ret = self.download(dl_debs_with_ver)


    def get_debian_pkg_iso_list(self):
        pkgs = []
        cgcs_root_dir = utils.get_env_variable('MY_REPO')
        package_file_name = 'debian_iso_image.inc'

        for root, dirs, files in os.walk(cgcs_root_dir):
            for file in files:
                if file == package_file_name:
                    with open(os.path.join(root, package_file_name), 'r') as f:
                        pkgs.extend(line.strip() for line in f if line.strip() and not line.startswith('#'))
        return pkgs

    def fetch_external_binaries(self):
        '''
        Download all binaries from the build system
        apt_item = apt_item + ' '.join(['deb [trusted=yes]', repo_url + 'deb-local-binary', codename, 'main\n'])
        '''
        # Get debs from base-<dist_codename>.lst
        # Example:
        # https://opendev.org/starlingx/tools/src/branch/master/debian-mirror-tools/config/debian/bullseye/common/base-bullseye.lst
        if not self.need_dl_binary_pkgs:
            logger.debug("No binary packages to download")
            return

        all_debs = set()

        external_binaries_list = os.path.join(
            self.designer_root,
            "stx-tools",
            "debian-mirror-tools", "config", "debian",
            self.dist_codename,
            "common",
            "base-" + self.dist_codename + ".lst")

        if not os.path.isfile(external_binaries_list):
            msg = f"Could not find external binaries list: {external_binaries_list}"
            raise Exception(msg)

        # find pkgs in the list file
        logger.debug(f'Packages to find {self.need_dl_binary_pkgs}')
        for pkg in self.need_dl_binary_pkgs:
            logger.debug(f'checking {pkg}')
            with open(external_binaries_list, 'r') as f:
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

        logger.debug('Third-party binaries to fetch:%s', all_debs)

        dl_bin_debs_dir = os.path.join(self.output_dir, 'downloads/binary')

        logger.info(f'Fetching debs to {dl_bin_debs_dir} \n')
        fetch_ret = self.download(all_debs)


    def download(self, all_debs):
        "Fetch pkgs from aptly"

        logger.debug('Fetching debs from aptly...')

        try:
            result = self.debs_fetcher.fetch_pkg_list(all_debs)

        except Exception as e:
            logger.exception(f"Exception fetching debs: {str(e)}")
            raise

        failed_fetches = result["deb-failed"] + result["dsc-failed"]

        if failed_fetches:
            raise FetchDebsError(f"Failed to fetch: {failed_fetches}")

        return result


if __name__ == '__main__':

    # Usage: Set the packages you want to download here
    fetch_debs = FetchDebs(
        need_dl_stx_pkgs = ['sysinv'],
        need_dl_binary_pkgs = ['tzdata', 'curl', 'apache2'],
    )

    fetch_debs.fetch_stx_packages()
