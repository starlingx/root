#
# Copyright (c) 2023-2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Helper functions to fetch debs from aptly
and process them for inclusion in STX patches
'''

import logging
import os
import sys

import constants
from apt_utils import get_apt_fetcher

sys.path.append('..')
import debsentry
import repo_manage
import utils
import discovery


EXTERNAL_BINARIES_DIR = os.path.join(
    constants.DESIGNER_ROOT, "stx-tools", "debian-mirror-tools", "config", "debian",
    constants.STX_DEFAULT_DISTRO_CODENAME)

EXTERNAL_BINARIES_LISTS = [
    os.path.join(EXTERNAL_BINARIES_DIR, "common", "base-" + constants.STX_DEFAULT_DISTRO_CODENAME + ".lst"),
    os.path.join(EXTERNAL_BINARIES_DIR, "distro", "os-std.lst"),
    os.path.join(EXTERNAL_BINARIES_DIR, "flock", "os-std.lst")
]


logger = logging.getLogger(__name__)
utils.set_logger(logger)


# TODO: There should be a "SourcePackage" and a "BinaryPackage"
#       object and these should be used as interfaces between the build tool scripts.

# TODO: get_debs_clue() and get_binaries_for_source_pkgs() need to be replaced
#       for calls to an apt object pointing at the local aptly.
#       Currently, they parse cached pickle files from build-pkgs.
#       A couple valid options:
#       - Write functions to parse the dsc files in loadbuild
#       - Use build-tools/stx/debian_package.py for this


class FetchDebs(object):

    def __init__(self,
                 apt_fetcher:repo_manage.AptFetch = None,
                 stx_source_packages:list[str]|None = None,
                 third_party_packages:list[str]|None = None,
                 external_binaries_lists:list[str] = EXTERNAL_BINARIES_LISTS
                 ) -> None:
        """
        Class for downloading debs from the local aptly.

        Download debs corresponding to the list of STX source packages :stx_source_packages:.
        Debs found are filtered by the *iso_image* files across the repos.

        Download debs in :third_party_packages:, all of which must be mentioned
        in the external_binaries_list, which is the list used to fill in the local aptly
        in the first place.
        """

        self.stx_source_packages = stx_source_packages
        self.third_party_packages = third_party_packages

        self.apt_fetcher = apt_fetcher

        # Validate lists of external third-party packages available in local apt repo
        if not all(os.path.isfile(item) for item in external_binaries_lists):
            msg = f"One or more external binaries list is invalid: {external_binaries_lists}"
            raise Exception(msg)
        self.external_binaries_lists = external_binaries_lists

        # Dict of binaries to download, mapping name to version
        self.binaries_to_download = {}

        # Dict linking available metapackages to a list of their deps as (name,version) tuples
        self.metapackage_info = self.get_metapackage_info()


    @staticmethod
    def get_debs_clue(build_type: str) -> str:
        if build_type != 'rt':
            build_type = 'std'
        return os.path.join(constants.LOADBUILD_ROOT, 'caches', build_type + '_debsentry.pkl')


    @staticmethod
    def get_binaries_for_source_pkgs(source_pkgs: list) -> list:
        """
        Takes a list of source packages and returns a list with all
        the corresponding binary packages' names and versions concatenated with '_'.
        """

        all_binaries = []
        failed_source_pkgs = []

        # These are "pickle" files created by build-pkgs with the association
        # between source pkgs and their respective binaries
        debs_clue_std = FetchDebs.get_debs_clue('std')
        debs_clue_rt = FetchDebs.get_debs_clue('rt')

        logger.debug("Binaries found for each source pkg:")
        for source_pkg in source_pkgs:

            binary_pkgs = []

            binary_pkgs_std = debsentry.get_subdebs(debs_clue_std, source_pkg, logger)
            if binary_pkgs_std:
                binary_pkgs += binary_pkgs_std

            binary_pkgs_rt = debsentry.get_subdebs(debs_clue_rt, source_pkg, logger)
            if binary_pkgs_rt:
                binary_pkgs += binary_pkgs_rt

            if not binary_pkgs:
                # Error: Couldn't find any binaries for this source pkg
                failed_source_pkgs.append(source_pkg)
                continue

            logger.debug("%s: %s", source_pkg, ', '.join(binary_pkgs))
            all_binaries.extend(set(binary_pkgs))

        if failed_source_pkgs:
            msg = f"Failed to get binaries for source packages: {', '.join(failed_source_pkgs)}"
            raise Exception(msg)

        return all_binaries


    def identify_stx_packages(self):
        """
        From a list of STX source packages, determine the names and versions
        of the corresponding binaries that get installed into the STX ISO.
        """

        if not self.stx_source_packages:
            logger.warning("No STX packages to download")
            return

        # Get a list of STX binaries. Each item has the name and version concatenated
        binaries_to_download = FetchDebs.get_binaries_for_source_pkgs(self.stx_source_packages)
        if not binaries_to_download:
            msg = f"No STX binaries were found that matched source pkgs: {self.stx_source_packages}"
            raise Exception(msg)

        # Convert the concatenated list into a mapping of binary pkg name to version
        binaries_to_download_dict = {}
        for deb in binaries_to_download:
            name, version = deb.split('_')
            if name not in binaries_to_download_dict:
                binaries_to_download_dict[name] = version

        # TODO: Replace this check on *iso_image* for the metapackages dependencies

        # Get list of STX binary packages that are installed into the ISO
        stx_pkg_list_file = []
        for build_type in discovery.get_all_build_types():
            stx_pkg_list_file.extend(discovery.package_iso_list(build_type=build_type))

        binaries_to_ignore = []
        for deb in binaries_to_download_dict.keys():
            # try to find the deb in the package list
            if deb not in stx_pkg_list_file:
                # remove if not found in all lines
                binaries_to_ignore.append(deb)

        if binaries_to_ignore:
            logger.debug("These binaries are not installed into the STX ISO, so they will be removed from the selection:")
            logger.debug(binaries_to_ignore)

        for deb in binaries_to_ignore:
            # If package is explicitly in the patch recipe it should NOT be removed
            if deb not in self.stx_source_packages:
                binaries_to_download_dict.pop(deb)

        logger.info('STX packages selected:')
        for name, version in binaries_to_download_dict.items():
            logger.info('%s  %s', name, version)

        self.binaries_to_download.update(binaries_to_download_dict)


    def identify_third_party_packages(self):
        """
        Confirm that the third party packages requested are present in the build system's aptly repo

        Validate request against 'base-<dist_codename>.lst' and the 'os-std.lst' files in the tools repo.
        Default list: EXTERNAL_BINARIES_LISTS

        Examples:
        https://opendev.org/starlingx/tools/src/branch/master/debian-mirror-tools/config/debian/trixie/common/base-trixie.lst
        https://opendev.org/starlingx/tools/src/branch/master/debian-mirror-tools/config/debian/trixie/containers/os-std.lst
        """

        if not self.third_party_packages:
            logger.debug("No third party packages to download")
            return

        # TODO: Replace this check on the *.lst files in stx-tools for
        # a check on the metapackages dependencies

        # Import the third party package list files into a dict
        valid_external_binaries = {}
        for external_binaries_list in self.external_binaries_lists:
            with open(external_binaries_list, mode='r', encoding="utf-8") as f:
                for line in f.readlines():
                    line = line.strip()

                    if line.startswith('#'):
                        continue

                    pkg_name, pkg_version = line.split()[:2]

                    if pkg_name in valid_external_binaries:
                        msg = f"More than one version value defined for the same third-party binary in reference lists: {pkg_name}"
                        raise Exception(msg)

                    valid_external_binaries.update({pkg_name: pkg_version})

        # Confirm requested third-party packages are in the reference lists
        # and get the target version
        logger.debug(f'Third-party packages requested: {self.third_party_packages}')

        pkgs_not_in_reference_lists = []
        for pkg_name in self.third_party_packages:
            pkg_version = valid_external_binaries.get(pkg_name)

            if pkg_version is None:
                pkgs_not_in_reference_lists.append(pkg_name)

            self.binaries_to_download[pkg_name] = pkg_version

        if pkgs_not_in_reference_lists:
            msg = f"Requested third-party packages not defined in reference lists: {pkgs_not_in_reference_lists}"
            raise Exception(msg)


    def get_metapackage_info(self):
        """
        Get a dict with info on metapackages available for download from local aptly
        Return a dict mapping metapackage names to a list of (dep_name, dep_version) tuples
        """

        aptcache = self.apt_fetcher.aptcache

        result = {}

        for pkg in aptcache:

            # Consider only STX metapackages. Skip otherwise.
            if not pkg.candidate \
               or not pkg.name.startswith(constants.METAPACKAGE_NAME_PREFIX) \
               or pkg.candidate.section != constants.METAPACKAGE_SECTION:
                continue

            deps = []

            for dep_group in pkg.candidate.dependencies:

                # STX metapackage dependency OR-groups only list one package each,
                # so just take the first item
                base_dep = dep_group.or_dependencies[0]

                # The base_dep object also has a 'relation' attribute, but for
                # real deps we can assume it's '='
                deps.append((base_dep.name, base_dep.version))

            result[pkg.name] = deps

        logger.debug(f"STX metapackages currently available: {result.keys()}")

        return result


    def identify_metapackages(self):
        """
        Given a list of binaries in :self.binaries_to_download:, identify which
        metapackage binaries correspond to those "real package" binaries
        and add them to the :self.binaries_to_download: list
        """

        logger.debug(f"Selecting corresponding metapackages...")

        orphan_packages = []
        metapackages_to_include = set()

        # Remember this dict only accounts for metapackages currently available in aptly
        dep_to_metapackage_dict = {
            pkg_name: metapackage
            for metapackage, dep_list in self.metapackage_info.items()
            for pkg_name,pkg_version in dep_list
        }

        for pkg_name,pkg_version in self.binaries_to_download.items():

            # Assume STX metapackages are the only pkgs starting with constants.METAPACKAGE_NAME_PREFIX.
            # For these, the corresponding metapackage is itself
            if pkg_name.startswith(constants.METAPACKAGE_NAME_PREFIX):
                logger.debug(f"Metapackage for '{pkg_name}': '{pkg_name}'")
                metapackages_to_include.add(pkg_name)
                continue

            # For all others, check the STX metapackages dependency mapping

            metapackage_candidate = dep_to_metapackage_dict.get(pkg_name)

            if metapackage_candidate:
                logger.debug(f"Metapackage for '{pkg_name}': '{metapackage_candidate}'")
                metapackages_to_include.add(metapackage_candidate)
            else:
                orphan_packages.append(pkg_name)

        if orphan_packages:
            msg = f"Could not find metapackages corresponding to these packages: {orphan_packages}"
            raise Exception(msg)

        # No need to find or record the metapackage version
        self.binaries_to_download.update(
            {metapackage:"" for metapackage in metapackages_to_include}
        )

        metapackages_to_include = list(metapackages_to_include)
        logger.info(f"Metapackages selected: {metapackages_to_include}")
        return metapackages_to_include


    def download(self):
        """
        Download binary packages from local aptly
        """

        if not self.binaries_to_download:
            # Pre-process and validate request
            self.identify_stx_packages()
            self.identify_third_party_packages()
            self.identify_metapackages()

        if not self.binaries_to_download:
            # Nothing to download was found in pre-processing
            logger.warning("No packages to download!")

        logger.info('Downloading debs from aptly...')
        logger.info(f'Download dir: {self.apt_fetcher.binary_downloads_dir}')

        failed_fetches = []

        for pkg_name,pkg_version in self.binaries_to_download.items():
            if pkg_version:
                result = self.apt_fetcher.fetch_deb(pkg_name=pkg_name,
                                                    pkg_version=pkg_version)
            else:
                result = self.apt_fetcher.fetch_deb(pkg_name=pkg_name,
                                                    fetch_latest=True)

            if "DEB-F" in result:
                failed_fetches.append((pkg_name,pkg_version))

        if failed_fetches:
            raise Exception(f"Failed to fetch debs: {failed_fetches}")


if __name__ == '__main__':

    # Usage: Set packages you want to download here
    fetch_debs = FetchDebs(
        apt_fetcher= get_apt_fetcher(),

        stx_source_packages= ['sysinv'],
        third_party_packages= ['tzdata', 'curl']
    )

    # Download:
    # - Binaries corresponding to STX source packages, if any;
    # - Third party packages requested, if available;
    # - Metapackages corresponding to the binaries previously selected.
    fetch_debs.download()
