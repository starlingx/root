# Copyright (c) 2021 Wind River Systems, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fnmatch
import os
import re
import glob
import yaml

from git_utils import git_list
from repo_utils import repo_root
from utils import bc_safe_fetch

LAYER_PRIORITY_DEFAULT = 99
BUILD_TYPE_PRIORITY_DEFAULT = 99

# Supported distros + codenames
STX_DISTRO_DEBIAN = 'debian'
STX_DISTRO_DEBIAN_BULLSEYE = 'bullseye'
STX_DISTRO_DEBIAN_TRIXIE = 'trixie'

#STX_DISTRO_XXXX = 'XXXX'

STX_DISTRO_DICT = {
    STX_DISTRO_DEBIAN : [
        STX_DISTRO_DEBIAN_BULLSEYE,
        STX_DISTRO_DEBIAN_TRIXIE,
    ]
}

# Default distro/codename build
STX_DEFAULT_DISTRO = STX_DISTRO_DEBIAN
STX_DEFAULT_DISTRO_CODENAME = os.environ.get('DIST', STX_DISTRO_DEBIAN_BULLSEYE)

STX_DEFAULT_BUILD_TYPE = "std"
STX_DEFAULT_BUILD_TYPE_LIST = [ STX_DEFAULT_BUILD_TYPE ]


def get_all_distros():
    distro_lst = list(STX_DISTRO_DICT.keys())
    return sorted(distro_lst)

def get_build_type_priority(build_type, layer,
                            distro=STX_DEFAULT_DISTRO,
                            codename=STX_DEFAULT_DISTRO_CODENAME):
    prio = BUILD_TYPE_PRIORITY_DEFAULT
    if build_type is None:
        return BUILD_TYPE_PRIORITY_DEFAULT
    dir = os.environ.get('MY_REPO_ROOT_DIR')
    if dir is None:
        return BUILD_TYPE_PRIORITY_DEFAULT
    if not os.path.isdir(dir):
        return BUILD_TYPE_PRIORITY_DEFAULT
    build_type_priority_file = os.path.join(dir, "stx-tools",
                                            "%s%s" % (distro, "-mirror-tools"),
                                            "config", distro,
                                            codename, layer, build_type, "priority")
    if not os.path.isfile(build_type_priority_file):
        return BUILD_TYPE_PRIORITY_DEFAULT
    prio = int(bc_safe_fetch(build_type_priority_file, None)[0])
    return prio


def get_layer_priority(layer,
                       distro=STX_DEFAULT_DISTRO,
                       codename=STX_DEFAULT_DISTRO_CODENAME):
    prio = LAYER_PRIORITY_DEFAULT
    if layer is None:
        return LAYER_PRIORITY_DEFAULT
    dir = os.environ.get('MY_REPO_ROOT_DIR')
    if dir is None:
        return LAYER_PRIORITY_DEFAULT
    if not os.path.isdir(dir):
        return LAYER_PRIORITY_DEFAULT
    layer_priority_file = os.path.join(dir, "stx-tools",
                                       "%s%s" % (distro, "-mirror-tools"),
                                       "config", distro,
                                       codename, layer, "priority")
    if not os.path.isfile(layer_priority_file):
        return LAYER_PRIORITY_DEFAULT
    prio = int(bc_safe_fetch(layer_priority_file, None)[0])
    return prio


def sort_layer_list (layer_list,
                     distro=STX_DEFAULT_DISTRO,
                     codename=STX_DEFAULT_DISTRO_CODENAME):
    layer_dict = {}
    for layer in layer_list:
        prio = get_layer_priority(layer, distro=distro, codename=codename)
        layer_dict[prio] = layer
    keys = sorted(layer_dict.keys())
    result = []
    for key in keys:
        result.append(layer_dict[key])
    return result


def get_all_layers (distro=STX_DEFAULT_DISTRO,
                    codename=STX_DEFAULT_DISTRO_CODENAME,
                    skip_non_buildable=True):
    layer_lst = []
    project_dir_list_all = project_dir_list(distro=distro, layer="all", skip_non_buildable=skip_non_buildable)
    for proj_dir in project_dir_list_all:
        layer_file = os.path.join(proj_dir, "%s%s" % (distro, "_build_layer.cfg"))
        if not os.path.isfile(layer_file):
            continue
        layer_lst.extend(bc_safe_fetch(layer_file, None))

    # also add any layers defined in stx-tools
    tools_layers_root = os.path.join(os.environ.get('MY_REPO_ROOT_DIR'),
                                     "stx-tools","%s%s" % (distro,"-mirror-tools"),
                                     "config", distro, codename)
    for dir_entry in os.scandir (tools_layers_root):
        if dir_entry.name != "common":
            layer_lst.append (dir_entry.name)

    # remove duplicates
    layer_lst = list(set(layer_lst))
    return sort_layer_list(layer_lst, distro=distro, codename=codename)


def sort_build_type_list (build_type_list, layer,
                          distro=STX_DEFAULT_DISTRO,
                          codename=STX_DEFAULT_DISTRO_CODENAME):
    build_type_dict = {}
    for build_type in build_type_list:
        prio = get_build_type_priority(build_type, layer, distro=distro, codename=codename)
        build_type_dict[prio] = build_type
    keys = sorted(build_type_dict.keys())
    result = []
    for key in keys:
        result.append(build_type_dict[key])
    return result


def get_pkg_dirs_files (layer='all', distro=STX_DEFAULT_DISTRO,
                        codename=STX_DEFAULT_DISTRO_CODENAME,
                        skip_non_buildable=True):
    pkg_dirs_list = []
    project_dir_list_all = project_dir_list(distro=distro, layer=layer, skip_non_buildable=skip_non_buildable)
    for proj_dir in project_dir_list_all:
        # Assumes:
        #   Old Style: {distro}_pkg_dirs_*
        #   New Sytle: {distro}_{codename}_pkg_dirs_*
        files_list = None
        if codename == STX_DISTRO_DEBIAN_BULLSEYE:
            # Look for the non-codenamed old-style file configuration
            files_list = glob.glob("{}/{}_pkg_dirs*".format(proj_dir,distro))
        if not files_list:
            files_list = glob.glob("{}/{}_{}_pkg_dirs*".format(proj_dir,distro,codename))
        pkg_dirs_list.extend(files_list)
    return pkg_dirs_list

def get_layer_build_types (layer, distro=STX_DEFAULT_DISTRO,
                           codename=STX_DEFAULT_DISTRO_CODENAME,
                           skip_non_buildable=True):
    bt_lst = [ "std" ]
    for pkg_dir_file in get_pkg_dirs_files (layer=layer, distro=distro,
                                            codename=codename,
                                            skip_non_buildable=skip_non_buildable):
        parts = os.path.basename(pkg_dir_file).split("_pkg_dirs_")
        if len(parts) > 1:
            bt = parts[1]
            if not bt in bt_lst:
                bt_lst.append(bt)
    return sort_build_type_list(bt_lst, layer, distro=distro, codename=codename)


def get_all_build_types (distro=STX_DEFAULT_DISTRO, 
                         codename=STX_DEFAULT_DISTRO_CODENAME,
                         skip_non_buildable=True):
    bt_lst = [ "std" ]
    for pkg_dir_file in get_pkg_dirs_files (layer="all", distro=distro,
                                            codename=codename,
                                            skip_non_buildable=skip_non_buildable):
        parts = os.path.basename(pkg_dir_file).split("_pkg_dirs_")
        if len(parts) > 1:
            bt = parts[1]
            if not bt in bt_lst:
                bt_lst.append(bt)
    return sorted(bt_lst)




def project_dir_list_handler (element, data):
    if element not in data['layer']:
        return []
    return [ data['proj_dir'] ]

# project_dir_list
#      Return a list of git root directories for the current project.
#      Optionally, the list can be filtered by distro and layer.
def project_dir_list (distro=STX_DEFAULT_DISTRO, layer="all", skip_non_buildable=True):
    if layer is None:
        layer = "all"
    dir = os.environ.get('MY_REPO_ROOT_DIR')
    if dir is None:
        return []
    if not os.path.isdir(dir):
        return []
    project_dir_list_all = git_list(repo_root(dir))
    if skip_non_buildable:
        # keep only dirs that do not contain "/do-not-build"
        project_dir_list_all = list(filter(lambda dir: dir.find ("/do-not-build") == -1, project_dir_list_all))
    # print("project_dir_list_all=%s" % project_dir_list_all)
    if layer == "all":
        return project_dir_list_all
    # A specific layer is requested.
    project_dir_list_layer = []
    for proj_dir in project_dir_list_all:
        # Does this project provide content to the desired layer?
        layer_file = os.path.join(proj_dir, "%s%s" % (distro, "_build_layer.cfg"))
        if not os.path.isfile(layer_file):
            continue
        # print("project_dir_list: considering proj_dir=%s" % proj_dir)
        project_dir_list_layer.extend(bc_safe_fetch(layer_file, project_dir_list_handler, {'layer': layer, 'proj_dir': proj_dir}))
    return project_dir_list_layer


def package_dir_list_handler(entry, proj_dir):
    path = os.path.join(proj_dir, entry)
    if not os.path.isdir(path):
        return []
    return [ path ]


def package_iso_list (distro=STX_DEFAULT_DISTRO, codename=STX_DEFAULT_DISTRO_CODENAME, layer="all",
                      build_type="std", skip_non_buildable=True):
    pkg_iso_list = []
    if layer is None:
        layer = "all"
    for proj_dir in project_dir_list(distro=distro, layer=layer, skip_non_buildable=skip_non_buildable):
        if codename == STX_DISTRO_DEBIAN_BULLSEYE:
            # Look for the non-codenamed old-style file configuration for a specific build type
            iso_file = '{}/{}_iso_image_{}.inc'.format(proj_dir, distro, build_type)
            if not os.path.isfile(iso_file):
                # Look for the codenamed new-style file configuration for a build type
                iso_file = '{}/{}_{}_iso_image_{}.inc'.format(proj_dir, distro, codename, build_type)
                if not os.path.isfile(iso_file) and build_type == "std":
                    # Look for the non-codenamed old-style file configuration without build_type (is allowed)
                    iso_file = '{}/{}_iso_image.inc'.format(proj_dir, distro)
                    if not os.path.isfile(iso_file):
                        continue
                else:
                    continue
        else:
            # Require a consistent format for all new distro/codename combos
            iso_file = '{}/{}_{}_iso_image_{}.inc'.format(proj_dir, distro, codename, build_type)
            if not os.path.isfile(iso_file):
                continue
        pkg_iso_list.extend(bc_safe_fetch(iso_file))
    return pkg_iso_list


def package_dir_list (distro=STX_DEFAULT_DISTRO, codename=STX_DEFAULT_DISTRO_CODENAME, layer="all",
                      build_type="std", skip_non_buildable=True):

    pkg_dir_list = []
    if layer is None:
        layer = "all"
    for proj_dir in project_dir_list(distro=distro, layer=layer, skip_non_buildable=skip_non_buildable):
        if codename == STX_DISTRO_DEBIAN_BULLSEYE:
            # Look for the non-codenamed old-style file configuration for a specific build type
            pkg_file = '{}/{}_pkg_dirs_{}'.format(proj_dir, distro, build_type)
            if not os.path.isfile(pkg_file):
                # Look for the codenamed new-style file configuration for a build type
                pkg_file = '{}/{}_{}_pkg_dirs_{}'.format(proj_dir, distro, codename, build_type)
                if not os.path.isfile(pkg_file) and build_type == "std":
                    # Look for the non-codenamed old-style file configuration without build_type (is allowed)
                    pkg_file = '{}/{}_pkg_dirs'.format(proj_dir, distro)
                    if not os.path.isfile(pkg_file):
                        continue
                else:
                    continue
        else:
            # Require a consistent format for all new distro/codename combos
            pkg_file = '{}/{}_{}_pkg_dirs_{}'.format(proj_dir, distro, codename, build_type)
            if not os.path.isfile(pkg_file):
                continue
        pkg_dir_list.extend(bc_safe_fetch(pkg_file, package_dir_list_handler, proj_dir))
    return pkg_dir_list


def get_relocated_package_dir (pkg_dir, distro=STX_DEFAULT_DISTRO, codename=STX_DEFAULT_DISTRO_CODENAME):
    # Looks for:
    # - Old Syle  (debian)         : pkg_dir/debian/meta_data.yaml
    # - New Style (distro/codename): pkg_dir/debian/bullseye/meta_data.yaml
    # - New Style (distro/all)     : pkg_dir/debian/all/meta_data.yaml
    meta_data_file = None
    if codename == STX_DISTRO_DEBIAN_BULLSEYE:
        old_file = os.path.join(pkg_dir, distro, 'meta_data.yaml')
        new_all_file = os.path.join(pkg_dir, distro, 'all', 'meta_data.yaml')
        new_codename_file = os.path.join(pkg_dir, distro, codename, 'meta_data.yaml')
        if os.path.isfile(old_file):
            meta_data_file = old_file
        elif os.path.isfile(new_codename_file):
            meta_data_file = new_codename_file
        elif os.path.isfile(new_all_file):
            meta_data_file = new_all_file
        else:
            raise Exception(f"{pkg_dir}: No meta_data.yaml found. Valid [ {new_all_file}, {new_codename_file} ]")
    else:
        all_file = os.path.join(pkg_dir, distro, 'all', 'meta_data.yaml')
        codename_file = os.path.join(pkg_dir, distro, codename, 'meta_data.yaml')
        if os.path.isfile(codename_file):
            meta_data_file = codename_file
        elif os.path.isfile(all_file):
            meta_data_file = all_file
        else:
            raise Exception(f"{pkg_dir}: No meta_data.yaml found. Valid [ {all_file}, {codename_file} ]")

    return os.path.dirname(meta_data_file)

def package_dir_to_package_name (pkg_dir, distro=STX_DEFAULT_DISTRO, codename=STX_DEFAULT_DISTRO_CODENAME):
    pkg_name = os.path.basename(pkg_dir)
    current_pkg_dir = get_relocated_package_dir(pkg_dir, distro=distro, codename=codename)
    meta_data_file = os.path.join(current_pkg_dir, 'meta_data.yaml')
    if os.path.isfile(meta_data_file):
        with open(meta_data_file) as f:
            meta_data = yaml.full_load(f)
            if "debname" in meta_data:
                pkg_name = meta_data["debname"]
    return pkg_name

def package_dirs_to_package_names (pkg_dirs, distro=STX_DEFAULT_DISTRO,
                                   codename=STX_DEFAULT_DISTRO_CODENAME):
    pkg_names = []
    for pkg_dir in pkg_dirs:
        pkg_names.append(package_dir_to_package_name(pkg_dir, distro=distro, codename=codename))
    return pkg_names

def package_dirs_to_names_dict (pkg_dirs, distro=STX_DEFAULT_DISTRO,
                                codename=STX_DEFAULT_DISTRO_CODENAME):
    pkg_names = {}
    for pkg_dir in pkg_dirs:
        pkg_names[pkg_dir]=package_dir_to_package_name(pkg_dir, distro=distro, codename=codename)
    return pkg_names

def filter_package_dirs_by_package_names (pkg_dirs, package_names, distro=STX_DEFAULT_DISTRO,
                                          codename=STX_DEFAULT_DISTRO_CODENAME):
    pkgs_found = {}
    if not package_names:
        return pkg_dirs
    filtered_pkg_dirs = []
    for pkg_dir in pkg_dirs:
        pkg_name = package_dir_to_package_name(pkg_dir, distro=distro, codename=codename)
        if pkg_name in package_names:
            filtered_pkg_dirs.append(pkg_dir)
            pkgs_found[pkg_dir] = pkg_name
    return filtered_pkg_dirs, pkgs_found
