#
# Copyright (c) 2023-2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Functions for handling the input parameters provided by the user
in the patch recipe file and creating the patch metadata file.
'''

import logging
import os
import sys

from lxml import etree

import constants
import xml_parsing

sys.path.append('..')

import utils

# TODO: By default, utils.set_logger() uses /localdisk/builder.log as the
# target log file. Move all patching related modules to a "build-tools/patching"
# And create a __init__.py file to set up the logger appropriately.

# TODO: Convert the patch recipe from XML to yaml.

logger = logging.getLogger(__name__)
utils.set_logger(logger)

# Verify if the path to the patch builder folder is set
try:
    PATCH_BUILDER_PATH = os.environ['PATCH_BUILDER_PATH']
except KeyError:
    raise Exception("Environment variable PATCH_BUILDER_PATH is not set.")

INPUT_XML_SCHEMA = f'{PATCH_BUILDER_PATH}/config/patch-recipe-schema.xsd'

# Metadata components
COMPONENT = 'component'
DESCRIPTION = 'description'
EXTRA_CONTENT = 'extra_content'
ID = 'id'
INSTALL_INSTRUCTIONS = 'install_instructions'
METAPACKAGES = "metapackages"
PACKAGES = "packages"
PRODUCT_ROOT_TAG = 'product'
REQUIRES = 'requires'
STATUS = 'status'
STX_SOURCE_PACKAGES = 'stx_source_packages'
SUMMARY = 'summary'
SW_VERSION = 'sw_version'
THIRD_PARTY_PACKAGES = 'third_party_packages'
WARNINGS = 'warnings'

# Patch Statuses
RELEASE_STATUS = "REL"
DESIGNER_STATUS = "DEV"
VALID_STATUSES = [RELEASE_STATUS, DESIGNER_STATUS]

# Default values
DEFAULT_STATUS = DESIGNER_STATUS

# Metadata list tags
ITEM = "item"                       # For EXTRA_CONTENT
PKG = "pkg"                         # For STX_SOURCE_PACKAGES and THIRD_PARTY_PACKAGES
DEB = "deb"                         # For listing debs
REQUIRES_PATCH_ID = 'req_patch_id'  # For REQUIRES

# Backwards compatibility
# Replaced for STX_SOURCE_PACKAGES and THIRD_PARTY_PACKAGES for clarity
BINARY_PACKAGES = 'binary_packages'
STX_PACKAGES = 'stx_packages'
PACKAGE = 'package'


class PatchMetadata(object):
    """
    Class for modeling the patch metadata file.
    All object attributes go into the metadata file.
    """

    def __init__(self, patch_recipe_path):

        input_dict = xml_parsing.xml_to_dict(file_path=patch_recipe_path,
                                             schema_path=INPUT_XML_SCHEMA)

        self.parse_input_dict(input_dict)

        # Product metadata values not copy-pasted from patch recipe
        self.debs = []
        self.id = f"{input_dict.get(COMPONENT)}-{input_dict.get(SW_VERSION)}"
        self.metapackages = []

    def __str__(self):
        return str(self.__dict__)

    def __repr__(self):
        return self.__str__()

    def parse_input_dict(self, input_dict: dict) -> None:
        """
        Parse the input parameters in :input_dict:.
        Apply default values, validation rules and process XML lists.
        """

        # Straight copy from inputs to patch metadata
        self.description = input_dict.get(DESCRIPTION)
        self.install_instructions = input_dict.get(INSTALL_INSTRUCTIONS)
        self.summary = input_dict.get(SUMMARY)
        self.sw_version = input_dict.get(SW_VERSION)
        self.warnings = input_dict.get(WARNINGS)

        # Fields with default values
        self.status = input_dict.get(STATUS) or DEFAULT_STATUS

        # Lists (removing the xml item tag)
        self.stx_source_packages = xml_parsing.parse_xml_list(input_dict, STX_SOURCE_PACKAGES, PKG)
        self.third_party_packages = xml_parsing.parse_xml_list(input_dict, THIRD_PARTY_PACKAGES, PKG)
        self.requires = xml_parsing.parse_xml_list(input_dict, REQUIRES, ID)

        # Deprecated lists, kept for backwards compatibility
        self.stx_source_packages += xml_parsing.parse_xml_list(input_dict, STX_PACKAGES, PACKAGE)
        self.third_party_packages += xml_parsing.parse_xml_list(input_dict, BINARY_PACKAGES, PACKAGE)

        # Input validations

        # Extra content
        # Paths provided need some processing and validation
        self.extra_content = []
        extra_content_inputs = xml_parsing.parse_xml_list(input_dict, EXTRA_CONTENT, ITEM)
        for path in extra_content_inputs:
            processed_path = self._process_path(path)
            if processed_path is not None:
                self.extra_content.append(processed_path)

        # Check if 'status' is valid
        if self.status not in VALID_STATUSES:
            raise Exception(f"Supported '{STATUS}' values are {VALID_STATUSES}. "
                            "Received: {self.status}")

        logger.debug(f"Patch recipe parsed: {self}")

    def _process_path(self, path):
        """
        Check if :path: corresponds to existing file/dir

        If path is relative, look for content using as parent dir
        the current directory, then fallback to MY_REPO_ROOT_DIR
        (ie.: /localdisk/designer/USER/PROJECT/)
        """
        if not path:
            # No input provided
            return None

        # Cases: Absolute path and path relative to curdir
        candidate = os.path.abspath(path)
        if os.path.exists(candidate):
            return candidate

        # Case: Path relative to MY_REPO_ROOT_DIR
        parent = utils.get_env_variable('MY_REPO_ROOT_DIR')
        candidate = os.path.join(parent, path)
        if os.path.exists(candidate):
            return candidate

        logger.error(f"File or Directory not found: {path}")
        raise FileNotFoundError(path)

    def generate_patch_metadata(self, file_path):
        """
        Generate patch metadata XML
        """

        top_element = etree.Element(PRODUCT_ROOT_TAG)

        xml_parsing.add_nested_element(top_element, ID, self.id)
        xml_parsing.add_nested_element(top_element, SW_VERSION, self.sw_version)
        xml_parsing.add_nested_element(top_element, STATUS, self.status)
        xml_parsing.add_nested_element(top_element, SUMMARY, self.summary)
        xml_parsing.add_nested_element(top_element, DESCRIPTION, self.description)
        xml_parsing.add_nested_element(top_element, INSTALL_INSTRUCTIONS, self.install_instructions)
        xml_parsing.add_nested_element(top_element, WARNINGS, self.warnings)

        # XML list: Requires
        list_items = sorted(self.requires)
        xml_parsing.compose_xml_list(top_element, REQUIRES, REQUIRES_PATCH_ID, list_items)

        # XML list: Metapackages
        list_items = [pkg if not pkg.startswith(constants.METAPACKAGE_NAME_PREFIX)
                      else pkg.replace(constants.METAPACKAGE_NAME_PREFIX, "")
                      for pkg in sorted(self.metapackages)]
        xml_parsing.compose_xml_list(top_element, METAPACKAGES, PKG, list_items)

        # XML list: Debs
        list_items = sorted(self.debs)
        xml_parsing.compose_xml_list(top_element, PACKAGES, DEB, list_items)

        # XML list: Extra content
        list_items = [os.path.basename(item) for item in self.extra_content]
        xml_parsing.compose_xml_list(top_element, EXTRA_CONTENT, ITEM, list_items)

        # Save metadata xml
        etree.indent(top_element, space="  ")
        tree = etree.ElementTree(top_element)
        tree.write(file_path, xml_declaration=False, encoding="utf-8", pretty_print=True)


# Running this will generate logs that reflect how the patch recipe was parsed.
#
# It cannot generate accurate patch metadata (using generate_patch_metadata())
# without running the full patch-builder tool, as some info comes from
# processing that takes place in other modules.
#
# Usage:
#
# python3 metadata.py <patch_recipe_path>
#
if __name__ == "__main__":

    patch_recipe_path = sys.argv[1]

    try:
        patch_metadata = PatchMetadata(patch_recipe_path)
    except Exception:
        logger.exception(f"Invalid input: {patch_recipe_path}")
        sys.exit(1)
