#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This module holds functions for handling STX metapackages.
#

import logging
import os
import re
import subprocess
import sys
import tempfile

import constants

sys.path.append('..')

import utils


logger = logging.getLogger(__name__)
utils.set_logger(logger)


METAPACKAGE_METADATA_FILENAME = "metadata.xml"
METAPACKAGE_CONTENT_DIR = "usr/local/share/metapackages"

# Pattern for extracting deps from the dpkg-deb output.
DEBIAN_CONTROL_FILE_DEPENDENCY_PATTERN = re.compile(r'^\s*([^\s]+)\s*(?:\(=\s([^)]+)\))?$')
# ^\s* => optional leading whitespace
# ([^\s]+) => "capture/match" everything except whitespace to capture pkg name
# \s* => optional whitespace
# (?: ... )? => Non-capturing group marking everything inside as optional
# \(=\s([^)]+)\) => "capture/match" the version inside "(= <version>)"
# $ => End of input string

# TODO: This module implementation may be simpler as a Class
# TODO: A deb manipulation module may be useful


def find(target_name:str, reference_path:str, type:str|None = None) -> str:
    """
    Search for :target_name: file or directory in :reference_path:
    Return the first matching result found as absolute path.

    Optionally, filter by :type:, either "file" or "dir"

    If :target_name: can't be found, raise an exception.
    """

    for basedir, dirs, files in os.walk(reference_path):
        if target_name in files and type in [None, "file"]:
            return os.path.join(basedir, target_name)

        if target_name in dirs and type in [None, "dir"]:
            return os.path.join(basedir, target_name)

    raise FileNotFoundError(f"Could not find '{target_name}' in '{reference_path}'")


def edit_metapackage(apt_fetcher, metapackage_deb_path, target_dependencies=None, target_version=None):
    """
    Edit a metapackage deb, filling in missing version requirements of its dependencies.

    The target versions are obtained by checking the default candidate version in the :apt_fetcher: provided.

    Only the packages listed in :target_dependencies: have a version requirement set.
    If no :target_dependencies: are provided, all metapackage dependencies have their version requirement set
    (except for dependencies on other metapackages, which have names starting with constants.METAPACKAGE_NAME_PREFIX)

    If a version requirement is already present for some dependency, it is not changed.

    The metapackage's version is also updated.
    """

    with tempfile.TemporaryDirectory() as temporary_directory:

        try:
            # Extract deb contents
            cmd = ['dpkg-deb', '--raw-extract', metapackage_deb_path, temporary_directory]
            subprocess.check_output(cmd)

            # Remove original deb
            os.remove(metapackage_deb_path)

            # This assumes the metapackage does not have another "control" file.
            control_path = find("control", temporary_directory, type="file")

            # Read the control file
            with open(control_path, 'r', encoding="utf-8") as f:
                control_content = f.readlines()

            control_content = set_fixed_dependency_versions(apt_fetcher, control_content, target_dependencies)

            if target_version:
                version_line_idx = None
                for idx, line in enumerate(control_content):
                    if line.startswith('Version:'):
                        version_line_idx = idx
                        break

                if version_line_idx is None:
                    raise Exception("No Version field found in control file")

                old_version = control_content[version_line_idx].replace('Version: ', '').strip()
                control_content[version_line_idx] = f'Version: {target_version}\n'

                # Replace version in the metapackage filename
                metapackage_deb_dirpath = os.path.dirname(metapackage_deb_path)
                metapackage_deb_basename = os.path.basename(metapackage_deb_path)

                metapackage_deb_path = os.path.join(
                    metapackage_deb_dirpath,
                    metapackage_deb_basename.replace(old_version,target_version)
                )

                # Replace version numbering for the metapackage contents dir
                parent_dir = os.path.join(temporary_directory, METAPACKAGE_CONTENT_DIR)
                old_contents_dirpath = os.path.join(parent_dir, old_version)
                new_contents_dirpath = os.path.join(parent_dir, target_version)
                os.rename(old_contents_dirpath, new_contents_dirpath)

                # Replace old version string in the contents of all files in the contents dir
                for basedir, _, files in os.walk(new_contents_dirpath):
                    for filename in files:
                        filepath = os.path.join(basedir, filename)
                        with open(filepath, 'r', encoding="utf-8") as f:
                            content = f.read()
                        if old_version in content:
                            content = content.replace(old_version, target_version)
                            with open(filepath, 'w', encoding="utf-8") as f:
                                f.write(content)

            # Write back the modified control file
            with open(control_path, 'w', encoding="utf-8") as f:
                f.write("".join(control_content))

            # Rebuild the deb
            cmd = ['dpkg-deb', '--build', temporary_directory, metapackage_deb_path]
            subprocess.check_output(cmd)

        except Exception:
            logger.exception(f"Failed to edit metapackage: '{os.path.basename(metapackage_deb_path)}'")
            raise


def set_fixed_dependency_versions(apt_fetcher, control_content, target_dependencies=None):

    # Parse out the Depends line
    depends_line_idx = None
    for idx, line in enumerate(control_content):
        if line.startswith('Depends:'):
            depends_line_idx = idx
            break

    if depends_line_idx is None:
        raise Exception("No Depends field found in the metapackage's control file")

    depends_line = control_content[depends_line_idx]
    depends_value = depends_line.replace('Depends:', '', 1).strip()

    # Parse individual dependencies
    dep_entries = [d.strip() for d in depends_value.split(',')]
    updated_entries = []

    for entry in dep_entries:
        match = DEBIAN_CONTROL_FILE_DEPENDENCY_PATTERN.match(entry)
        if not match:
            # Keep entries we can't parse as-is
            updated_entries.append(entry)
            continue

        dep_name, dep_version = match.groups()

        # If a version requirement is already present, keep it unchanged
        if dep_version:
            updated_entries.append(entry)
            continue

        # Determine whether this dependency should be pinned
        if target_dependencies is not None:
            # Only pin if explicitly listed in target_dependencies
            if dep_name not in target_dependencies:
                updated_entries.append(entry)
                continue
        else:
            # Skip dependencies on other metapackages
            if dep_name.startswith(constants.METAPACKAGE_NAME_PREFIX):
                updated_entries.append(entry)
                continue

        # Look up the default candidate version
        version = apt_fetcher.get_default_candidate_version(dep_name)
        updated_entries.append(f"{dep_name} (= {version})")

    # Reconstruct the Depends line
    new_depends_line = 'Depends: ' + ', '.join(updated_entries) + '\n'
    control_content[depends_line_idx] = new_depends_line

    return control_content


# The functions below correspond to old design requirements and are no longer used.
# Since requirements are still being refined, this old code is left here for future reference
# for the time being. Once the feature stabilizes, this can be removed.

###
###def set_proper_metapackage_metadata_values(deb_path, target_values = PATCH_METAPACKAGE_METADATA):
###    """
###    Given a metapackage deb filepath :deb_path:, set the fields in its metadata xml
###    to the target values defined in :target_values:, which is a list of dictionaries,
###    each defining a field by its "field" and "value"
###
###    This may replace the binary with a new one.
###    """
###
###    with tempfile.TemporaryDirectory() as temporary_directory:
###
###        try:
###            # Extract deb recipe files
###            cmd = ['dpkg-deb', '--raw-extract', deb_path, temporary_directory]
###            subprocess.check_output(cmd)
###
###            metadata_path = find(METAPACKAGE_METADATA_FILENAME, temporary_directory)
###
###            for metadata_item in target_values:
###                edit_xml_field(metadata_path,
###                        metadata_item['field'],
###                        metadata_item['value'])
###
###            # Remove original deb
###            os.remove(deb_path)
###
###            # Rebuild the deb
###            cmd = ['dpkg-deb', '--build', temporary_directory, deb_path]
###            subprocess.check_output(cmd)
###
###        except Exception:
###            logger.exception(f"Failed to edit metapackage deb '{deb_path}'")
###            raise
###
###
#### TODO: This implementation does not take advantage of the tools that apt
####       or our apt wrapper objects already provide
###def identify_metapackages(dir):
###    """
###    Given an input directory, return a dictionary matching
###    each metapackage deb file found in there to a list of their respective dependencies.
###    Each dep is represented by a tuple with the name and version.
###
###    For example:
###    {"meta-swmgmt_1.2.3_all.deb": [("software", "1.0-1.stx.1075"), ...]}
###    """
###
###    all_debs = [os.path.join(dir, item)
###                for item in os.listdir(dir)
###                if os.path.isfile(os.path.join(dir, item))
###                and item.endswith(".deb")]
###
###    metapackages_info = {}
###    for deb in all_debs:
###        cmd = ["dpkg-deb", "-I", deb]
###        deb_info = subprocess.check_output(cmd, text=True)
###
###        if "Section: metapackages" in deb_info:
###            lines = [line.strip() for line in deb_info.split("\n")]
###            try:
###                depends_line = [line for line in lines if line.startswith("Depends:")][0]
###                depends_line = depends_line.replace("Depends: ", "")
###            except IndexError:
###                raise Exception(f"Metapackage without any dependencies: {deb}")
###
###            dependencies_list = []
###            for dep in depends_line.split(","):
###                try:
###                    dependencies_list.append(DEBIAN_CONTROL_FILE_DEPENDENCY_PATTERN.match(dep).groups())
###                except:
###                    raise Exception("Could not parse metapackages dependencies")
###
###            metapackages_info.update( {deb: dependencies_list} )
###
###    return metapackages_info
###
###
###    def fetch_metapackage_dependencies(self):
###        """
###        Find metapackage binaries in the download directory,
###        extract dependency information from them,
###        then pull their dependencies to the download directory.
###        """
###
###        metapackages_info = metapackages.identify_metapackages(self.download_dir)
###
###        # Check that all metapackage dependencies have a version specified.
###        # This info is required in the STX runtime.
###        # if it's missing, fail execution.
###        incomplete_metapackages = set()
###        for metapackage,dependencies in metapackages_info.items():
###            for _,version in dependencies:
###                if not version:
###                    incomplete_metapackages.add(os.path.basename(metapackage))
###                    continue
###
###        if incomplete_metapackages:
###            msg = f"Some metapackages are missing version info for their dependencies: {incomplete_metapackages}"
###            logger.error(msg)
###            raise Exception(msg)
###
###        logger.info(f'Fetching metapackage dependencies to {self.download_dir} \n')
###
###        all_debs = [f"{name} {version}"
###                    for _,dependencies in metapackages_info.items()
###                    for name,version in dependencies]
###
###        self.download(all_debs)
