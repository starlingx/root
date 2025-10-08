#
# Copyright (c) 2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Class that holds the patch metadata information
'''

import json
import logging
import os
import sys
sys.path.append('..')
import utils
import xml.etree.ElementTree as ET
from lxml import etree
from xml.dom import minidom

from constants import PATCH_SCRIPTS

logger = logging.getLogger('metadata_parser')
utils.set_logger(logger)

PATCH_BUILDER_PATH = os.environ.get('PATCH_BUILDER_PATH')
INPUT_XML_SCHEMA = f'{PATCH_BUILDER_PATH}/config/patch-recipe-schema.xsd'

# Metadata components
PATCH_ROOT_TAG = 'patch'
PATCH_ID = 'id'
SW_VERSION = 'sw_version'
COMPONENT = 'component'
STATUS = 'status'
SUMMARY = 'summary'
DESCRIPTION = 'description'
INSTALL_INSTRUCTIONS = 'install_instructions'
WARNINGS = 'warnings'
REBOOT_REQUIRED = 'reboot_required'
UNREMOVABLE = 'unremovable'
REQUIRES = 'requires'
REQUIRES_PATCH_ID = 'req_patch_id'
PACKAGES = 'packages'
STX_PACKAGES = 'stx_packages'
BINARY_PACKAGES = 'binary_packages'
SEMANTICS = 'semantics'
ACTIVATION_SCRIPTS = 'activation_scripts'


class PatchMetadata(object):
    def __init__(self, patch_recipe_file):
        self.patch_recipe_file = patch_recipe_file
        self.stx_packages = []
        self.binary_packages = []
        self.requires = []
        self.activation_scripts = []

        # Verify if the path to the patch builder folder is set
        if not PATCH_BUILDER_PATH:
            raise Exception("Environment variable PATCH_BUILDER_PATH is not set.")

    def __str__(self):
        return json.dumps(self.__dict__)

    def __repr__(self):
        return self.__str__()

    def __add_text_tag_to_xml(self, parent, name, text):
        """
        Utility function for adding a text tag to an XML object
        :param parent: Parent element
        :param name: Element name
        :param text: Text value
        :return:The created element
        """
        tag = ET.SubElement(parent, name)
        tag.text = text
        return tag

    def __xml_to_dict(self, element):
        """
        Converts xml into a dict
        :param xml element
        """
        if len(element) == 0:
            return element.text.strip() if element.text else ""
        result = {}
        for child in element:
            child_data = self.__xml_to_dict(child)
            # Verify if child.tag is comment
            if child.tag == etree.Comment:
                continue
            if child.tag in result:
                if isinstance(result[child.tag], list):
                    result[child.tag].append(child_data)
                else:
                    result[child.tag] = [result[child.tag], child_data]
            else:
                result[child.tag] = child_data
        return result

    def generate_patch_metadata(self, file_path):
        # Generate patch metadata.xml
        top_tag = ET.Element(PATCH_ROOT_TAG)
        self.__add_text_tag_to_xml(top_tag, PATCH_ID, self.patch_id)
        self.__add_text_tag_to_xml(top_tag, SW_VERSION, self.sw_version)
        self.__add_text_tag_to_xml(top_tag, COMPONENT, self.component)
        self.__add_text_tag_to_xml(top_tag, SUMMARY, self.summary)
        self.__add_text_tag_to_xml(top_tag, DESCRIPTION, self.description)
        self.__add_text_tag_to_xml(top_tag, INSTALL_INSTRUCTIONS, self.install_instructions)
        self.__add_text_tag_to_xml(top_tag, WARNINGS, self.warnings)
        self.__add_text_tag_to_xml(top_tag, STATUS, self.status)

        if self.unremovable.upper() in ["Y","N"]:
            self.__add_text_tag_to_xml(top_tag, UNREMOVABLE, self.unremovable.upper())
        else:
            raise Exception('Supported values for "Unremovable" are Y or N, for "Yes" or "No" respectively')

        if self.reboot_required.upper() in ["Y","N"]:
            self.__add_text_tag_to_xml(top_tag, REBOOT_REQUIRED, self.reboot_required.upper())
        else:
            raise Exception('Supported values for "Reboot Required" are Y or N, for "Yes" or "No" respectively')

        self.__add_text_tag_to_xml(top_tag, SEMANTICS, self.semantics)

        requires_atg = ET.SubElement(top_tag, REQUIRES)
        for req_patch in sorted(self.requires):
            self.__add_text_tag_to_xml(requires_atg, REQUIRES_PATCH_ID, req_patch)

        for script_id, script_path in self.patch_script_paths.items():
            script_name = ""
            if script_path != None:
                script_name = PATCH_SCRIPTS[script_id]

            self.__add_text_tag_to_xml(top_tag, script_id, script_name)

        if self.activation_scripts:
            activation_scripts_tag = ET.SubElement(top_tag, ACTIVATION_SCRIPTS)
            for script in self.activation_scripts:
                self.__add_text_tag_to_xml(activation_scripts_tag, "script", script.split('/')[-1])
        else:
            self.__add_text_tag_to_xml(top_tag, ACTIVATION_SCRIPTS, "")

        packages_tag = ET.SubElement(top_tag, PACKAGES)
        for package in sorted(self.debs):
            self.__add_text_tag_to_xml(packages_tag, "deb", package)

        # Save xml
        outfile = open(file_path, "w")
        tree = ET.tostring(top_tag)
        outfile.write(minidom.parseString(tree).toprettyxml(indent="  "))

    def __tag_to_list(self, tag_content):
        if type(tag_content) != list:
            return [tag_content]
        return tag_content

    def _validate_activation_script(self, script_list):
        '''
        Validate if scripts filename start with an integer
        '''
        for fullpath_script in script_list:
            try:
                name = os.path.basename(fullpath_script)
                int(name.split("-")[0])
            except Exception:
                logger.error("Error while parsing the activation script:")
                logger.error("Filename '%s' doesn't start with an integer." % fullpath_script)
                sys.exit(1)

    def parse_metadata(self, patch_recipe):
        self.patch_id = f"{patch_recipe[COMPONENT]}-{patch_recipe[SW_VERSION]}"
        self.sw_version = patch_recipe[SW_VERSION]
        self.component = patch_recipe[COMPONENT]
        self.summary = patch_recipe[SUMMARY]
        self.description = patch_recipe[DESCRIPTION]
        if 'package' in patch_recipe[STX_PACKAGES]:
            self.stx_packages = self.__tag_to_list(patch_recipe[STX_PACKAGES]['package'])
        if 'package' in patch_recipe[BINARY_PACKAGES]:
            self.binary_packages = self.__tag_to_list(patch_recipe[BINARY_PACKAGES]['package'])
        self.install_instructions = patch_recipe[INSTALL_INSTRUCTIONS]
        self.warnings = patch_recipe[WARNINGS]
        self.reboot_required = patch_recipe[REBOOT_REQUIRED]

        # For each patch script, validate the path provided
        self.patch_script_paths = {
            script_id: self.check_script_path(patch_recipe.get(script_id, None))
            for script_id in PATCH_SCRIPTS.keys()
        }

        self.unremovable = patch_recipe[UNREMOVABLE]
        self.status = patch_recipe[STATUS]
        if 'id' in patch_recipe[REQUIRES]:
            self.requires = self.__tag_to_list(patch_recipe[REQUIRES]['id'])
        self.semantics = patch_recipe[SEMANTICS]
        if ACTIVATION_SCRIPTS in patch_recipe and 'script' in patch_recipe[ACTIVATION_SCRIPTS]:
            # the xml parser transform the 'script' value in string or in
            # array depending on how much elements we add.
            scripts_lst = []
            if isinstance(patch_recipe[ACTIVATION_SCRIPTS]['script'], str):
                scripts_lst.append(self.check_script_path(patch_recipe[ACTIVATION_SCRIPTS]['script']))
            else:
                for script in patch_recipe[ACTIVATION_SCRIPTS]['script']:
                    scripts_lst.append(self.check_script_path(script))
            self._validate_activation_script(scripts_lst)
            self.activation_scripts = scripts_lst
        self.debs = []

        if self.status != 'DEV' and self.status != 'REL':
            raise Exception('Supported status are DEV and REL, selected')

        logger.debug("Metadata parsed: %s", self)

    def parse_input_xml_data(self):
        # Parse and validate the XML
        try:
            xml_tree = etree.parse(self.patch_recipe_file)
        except Exception as e:
            logger.error(f"Error while parsing the input xml {e}")
            sys.exit(1)

        root = xml_tree.getroot()
        xml_schema = etree.XMLSchema(etree.parse(INPUT_XML_SCHEMA))

        # Validate the XML against the schema
        is_valid = xml_schema.validate(root)
        xml_dict = {}
        if is_valid:
            logger.info("XML is valid against the schema.")
            xml_dict = self.__xml_to_dict(root)
        else:
            logger.error("XML is not valid against the schema. Validation errors:")
            for error in xml_schema.error_log:
                logger.error(f"Line {error.line}: {error.message}")
            sys.exit(1)

        logger.info(xml_dict)
        self.parse_metadata(xml_dict)


    def check_script_path(self, script_path):
        if not script_path:
            # No scripts provided
            return None

        if not os.path.isabs(script_path):
            script_path = os.path.join(os.getcwd(), script_path)

        if not os.path.isfile(script_path):
            erro_msg = f"Install script {script_path} not found"
            logger.error(erro_msg)
            raise FileNotFoundError(erro_msg)

        return script_path


if __name__ == "__main__":
    patch_recipe_file = f"${PATCH_BUILDER_PATH}/EXAMPLES/patch-recipe-sample.xml"
    patch_metadata = PatchMetadata(patch_recipe_file)
    patch_metadata.parse_input_xml_data()
