#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Functions for converting an XML file to a dict and vice-versa
and for processing the dict obtained from parsing an XML file
"""

import logging
import sys

from lxml import etree

sys.path.append('..')

import utils


logger = logging.getLogger(__name__)
utils.set_logger(logger)


### Functions for converting an XML file to a dict ###

def xml_to_dict(file_path: str, schema_path: str | None = None) -> dict:
    """
    Parse an XML file, optionally validate it, and convert to a dictionary.

    :param file_path: Path to the XML file
    :param schema_path: Optional path to an XSD schema for validation
    """

    root = file_to_element(file_path)

    if schema_path:
        validate_element(root, schema_path)

    xml_dict = element_to_dict(root)

    if not isinstance(xml_dict, dict):
        raise Exception(f"Error converting XML to dict: {file_path}")

    return xml_dict


def file_to_element(input_path: str) -> etree._Element:
    """
    Parse an XML file and return the root element as an lxml object.

    :param input_path: Path to the XML file
    :return: Root element of the parsed XML tree
    """
    try:
        xml_tree = etree.parse(input_path)
        return xml_tree.getroot()
    except Exception as e:
        logger.error(f"Error while reading input XML: {e}")
        raise


def validate_element(xml_root: etree._Element, schema_path: str) -> None:
    """
    Validate an XML root element against an XSD schema.
    Raises an exception if validation fails.

    :param xml_root: lxml root element to validate
    :param schema_path: Path to the XSD schema file
    :raises Exception: If XML does not conform to the schema
    """

    try:
        xml_schema = etree.XMLSchema(etree.parse(schema_path))
    except Exception as e:
        logger.error(f"Invalid XML schema: {schema_path}")
        raise e

    if not xml_schema.validate(xml_root):
        logger.error("Input XML is not valid against the schema. Validation errors:")
        for error in xml_schema.error_log:
            logger.error(f"  Line {error.line}: {error.message}")
        raise Exception("Input XML is not valid against the schema.")

    logger.debug("Input XML is valid against the schema.")


def element_to_dict(element: etree._Element) -> dict | str:
    """
    Recursively convert an lxml element tree into a nested dictionary.

    Child elements with the same tag are grouped into lists.

    Comments are skipped.

    The XML is assumed to be strictly hierarchical. In other words,
    an element can have either text or children, but not both. The text
    will be ignored if both items are present.

    :param element: lxml element to convert
    :return:
      If the element doesn't have nested elements, text of the element.
      If there are nested elements, dictionary representation.
    """
    if len(element) == 0:
        return element.text.strip() if element.text else ""

    result = {}
    for child in element:
        if child.tag is etree.Comment:
            continue
        child_data = element_to_dict(child)
        if child.tag in result:
            if isinstance(result[child.tag], list):
                result[child.tag].append(child_data)
            else:
                result[child.tag] = [result[child.tag], child_data]
        else:
            result[child.tag] = child_data
    return result


### Functions for handling XML lists ###

def parse_xml_list(xml_dictionary: dict, list_tag: str, item_tag: str) -> list:
    """
    Parse a list extracted from an XML dictionary.
    If the XML list has no items, return an empty list.
    If the XML list has a single item, return it inside a list.

    In simple terms, this removes the item tag. For example:

    <list_tag>
        <item_tag>value</item_tag>
    </list_tag>

    Becomes: list_tag = ['value']

    :param xml_dictionary: Dictionary representation of the XML
    :param list_tag: Key for the list container element
    :param item_tag: Key for individual items within the list
    :return: List of items
    """
    target_list = xml_dictionary.get(list_tag)

    # XML list has no items
    if not target_list:
        return []

    try:
        data = target_list[item_tag]
    except KeyError:
        err_msg = f"'{item_tag}' is not the item tag used for XML list '{list_tag}'"
        raise Exception(err_msg)
    except TypeError:
        err_msg = f"'{list_tag}' contains text instead of an XML list."
        raise Exception(err_msg)

    # XML list has a single item. The conversion to a dict will store it
    # as a string instead of a list with the string inside.
    if isinstance(data, str):
        return [data]

    # XML list with many items
    if isinstance(data, list):
        return data

    raise Exception(f"Failed to extract XML list from patch recipe: {list_tag}")


def compose_xml_list(parent_element: etree._Element, list_tag: str, item_tag: str, items: list) -> None:
    """
    Create an XML list SubElement attached to :parent_element:

    An example output:

    <parent_element>
      <list_tag>
        <item_tag>a</item_tag>
        <item_tag>b</item_tag>
      </list_tag>
    </parent_element>
    """

    list_element = etree.SubElement(parent_element, list_tag)

    for item in items:
        add_nested_element(list_element, item_tag, item)


### Functions to insert data into an XML file ###

def update_tag(xml_filepath: str, tag_name: str, new_value: str, output_path: str | None = None) -> None:
    """
    Update the value of the first matching XML tag in an XML file.

    :param xml_filepath: Path to input XML file
    :param tag_name: Name of XML tag to update
    :param new_value: New value to set
    :param output_path: Optional path to save the updated XML (defaults to overwrite original)

    """
    tree = etree.parse(xml_filepath)
    root = tree.getroot()

    element = root.find(tag_name)
    if element is None:
        raise Exception(f"Failed to update tag '{tag_name}'. Tag not found in XML.")

    element.text = str(new_value)

    save_path = output_path if output_path else xml_filepath
    tree.write(save_path, xml_declaration=True, encoding="unicode", pretty_print=True)


### Misc ###

def add_nested_element(parent: etree.Element, child_name: str, text: str|None = None) -> None:
    """
    Add a child element with a certain text into a parent element

    :param parent: Parent element
    :param child: Child element name
    :param text: Text
    """

    child = etree.SubElement(parent, child_name)
    if text:
        child.text = text


def edit_xml_field(filepath, field, value):
    """
    Given an XML filepath :filepath:, set :field: (if it exists) to a desired :value:
    """

    tree = etree.parse(filepath)
    root = tree.getroot()

    element_list = root.findall(field)

    if not element_list:
        raise Exception(f"XML field '{field}' not found in xml '{filepath}'")

    for element in element_list:
        element.text = value

    tree.write(filepath, encoding="utf-8")
