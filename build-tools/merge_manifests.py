#!/usr/bin/python
#
# Copyright (c) 2024 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import sys
from ruamel.yaml import YAML

def merge_dicts(main: dict, custom: dict) -> None:
    for key, value in custom.items():
        if key in main and isinstance(main[key], dict) and isinstance(value, dict):
            merge_dicts(main[key], value)
        else:
            main[key] = value

def handle_manifests_merge(main_manifests_path: str, custom_manifests_path: str):
    with open(main_manifests_path, 'r') as main_file, open(custom_manifests_path, 'r') as custom_file:
        yaml_parser = YAML(typ='safe', pure=True)
        main_data = yaml_parser.load(main_file)
        custom_data = yaml_parser.load(custom_file)
    # If both main file data and custom data are identical, no merge is needed
    if main_data == custom_data:
        return

    # Handle empty YAML files as empty dictionaries for comparison purposes
    main_data = {} if main_data is None else main_data
    custom_data = {} if custom_data is None else custom_data

    merged_data = main_data.copy()

    merge_dicts(merged_data, custom_data)

    with open(main_manifests_path, 'w') as main_file:
        yaml_writer = YAML()
        yaml_writer.default_flow_style = False
        yaml_writer.dump(merged_data, main_file)

if __name__ == "__main__":

    main_manifests_path = sys.argv[1]
    custom_manifests_path = sys.argv[2]

    try:
        handle_manifests_merge(main_manifests_path, custom_manifests_path)
    except Exception as e:
        print(f"Error trying to merge {main_manifests_path}: {e}")
        sys.exit(1)
