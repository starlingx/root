#!/usr/bin/python
#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# helm_chart_modify.py: Modifies docker image references in a yaml file
#                       such that it pulls from the host and tag we want.
#
#                       Substitution is based on matching image names.
#
#                       Five types of image reference are supported:
#
# 1)
#      image: <host>[:<port>]/<path>/<image-name>[:<tag>]
# 2)
#      image: <host>[:<port>]/<path>/<image-name>
#      imageTag: <tag>
# 3)
#      image:
#          repository: <host>[:<port>]/<path>/<image-name>[:<tag>]
# 4)
#      image:
#          repository: <host>[:<port>]/<path>/<image-name>
#          tag: <tag>
# 5)
#      images:
#         tags:
#             <key>: <host>[:<port>]/<path>/<image-name>[:<tag>]
#
# Usage:
#
#   helm_chart_modify.py <input-yaml-file> <output-yaml-file> <list-of-image-record-files>
#
#     input-yaml-file:  Path to input yaml file
#     output-yaml-file: Path to output yaml file
#     list-of-image-record-files: one or more files containing image records
#
#     e.g.
#     cat $MY_WORKSPACE/std/build-images/images-centos-stable-versioned.lst
#     docker.io/starlingx/stx-keystone-api-proxy:master-centos-stable-20200811T002300Z.0
#     docker.io/starlingx/stx-nova-client:master-centos-stable-20200811T002300Z.0
#     ...
#
# Sample usage:
#    helm_chart_modify.py <input-yaml-file> <output-yaml-file> \
#         $MY_WORKSPACE/std/build-images/images-centos-stable-versioned.lst

import collections
import sys
import ruamel.yaml as yaml


def get_image_tag(image):
    i = image.rfind('/')
    j = image[i+1:].rfind(':')
    if j < 0:
        return ''
    return image[i+j+2:]


def get_image_name(image):
    i = image.rfind('/')
    j = image[i+1:].rfind(':')
    if j < 0:
        return image[i+1:]
    return image[i+1:i+j+1]


def get_image_without_tag(image):
    i = image.rfind('/')
    j = image[i+1:].rfind(':')
    if j < 0:
        return image
    return image[:i+j+1]


def modify_image_and_tag(document, image_key, tag_key, new_image):
    k = image_key
    new_tag = ''
    old_tag = get_image_tag(document[k])
    independent_tag = tag_key != '' and \
                      tag_key in document and \
                      not isinstance(document[tag_key], dict)

    new_tag = get_image_tag(new_image)
    if independent_tag and old_tag == '':
        print("modify tagless url for key %s -> %s" %
              (k, get_image_without_tag(new_image)))
        document[k] = get_image_without_tag(new_image)
    else:
        print("modify url for key %s -> %s" % (k, new_image))
        document[k] = new_image

    if independent_tag:
        k = tag_key
        if new_tag != '':
            # replace tag to match replaced image
            print("modify tag for key %s -> %s" % (k, new_tag))
            document[k] = new_tag


def modify_yaml(document, grand_parent_key, parent_key, new_image_dict):
    image_key = 'image'
    tag_key = 'imageTag'

    if parent_key == 'image':
        image_key = 'repository'
        tag_key = 'tag'

    for k in document.keys():
        # modify/copy sub-dictionaries
        if isinstance(document[k], dict):
            modify_yaml(document[k], parent_key, k, new_image_dict)
            continue

        if document[k] is None:
            continue

        if grand_parent_key == 'images' and parent_key == 'tags':
            match_found = False
            name = get_image_name(document[k])
            if name in new_image_dict:
                modify_image_and_tag(document, k, '', new_image_dict[name])
                break
        else:
            # copy values that are not keyed by image_key or tag_key
            if k not in (image_key, tag_key):
                continue

    if grand_parent_key == 'images' and parent_key == 'keys':
        return

    k = image_key
    if k in document and not isinstance(document[k], dict):
        match_found = False
        name = get_image_name(document[k])
        if name in new_image_dict:
            modify_image_and_tag(document, k, tag_key, new_image_dict[name])
            break


def main(argv):
    yaml_file = argv[1]
    yaml_output = argv[2]
    image_record_files = argv[3:]
    document_out = collections.OrderedDict()
    new_image_dict = {}
    image_records = []

    # Read all lines from all files in image_records list
    for image_record_file in image_record_files:
        with open(image_record_file) as ir_file:
            new_records = [line.rstrip() for line in ir_file.readlines()]
            image_records.extend(new_records)

    # Create a dictionary to map image name to image location/tag
    for image in image_records:
        name = get_image_name(image)
        if name != '':
            new_image_dict[name] = image

    # Load chart into dictionary(s) and then modify any image locations/tags if required
    for document in yaml.load_all(open(yaml_file), Loader=yaml.RoundTripLoader):
        document_name = (document['schema'],
                         document['metadata']['schema'],
                         document['metadata']['name'])
        modify_yaml(document, '', '', new_image_dict)
        document_out[document_name] = document

    # Save modified yaml to file
    yaml.dump_all(document_out.values(),
                  open(yaml_output, 'w'),
                  Dumper=yaml.RoundTripDumper,
                  default_flow_style=False)

if __name__ == "__main__":
    main(sys.argv[0:])
