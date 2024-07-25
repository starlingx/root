#
# Copyright (c) 2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A place for any functions related to image.inc files
#

IMAGE_UTILS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${IMAGE_UTILS_DIR}/git-utils.sh"

get_release_info () {
    local dir=""
    local path=""

    for dir in $GIT_LIST; do
        path="$dir/utilities/build-info/release-info.inc"
        if [ -f "$path" ]; then
            echo "$path"
            return 0
        fi
    done

    echo "/invalid-path-to-release-info.inc"
    return 1
}

get_bsp_dir () {
    local dir=""
    local path=""

    for dir in $GIT_LIST; do
        path="$dir/bsp-files"
        if [ -d "$path" ]; then
            echo "$path"
            return 0
        fi
    done

    echo "/invalid-path-to-bsp-files"
    return 1
}

#
# image_inc_list <build_target> <list_type> <distro> [<layer>]
#
# Parameters:
#    build_target: One of 'iso', 'guest' ...
#    list_type:    One of 'std', 'dev', 'layer'
#    distro:       One of 'debian', ...
#    layer:        One of 'compiler', 'distro', 'flock', ...
#                  Only required if list_type == layer
#
# Returns: A list of unique package that must be included for
#          the desired distro's build target and build type.
#          This is the union of the global and per git 
#          image.inc files.

image_inc_list () {
    local build_target=$1
    local list_type=$2
    local distro=$3
    local layer=$4

    if [ "${list_type}" = "layer" ]; then
        local required_layer_cfg_name="required_layer_${build_target}_inc.cfg"
        local layer_cfg_name="${distro}_build_layer.cfg"
        local root_dir="${MY_REPO}/../stx-tools/${distro}-mirror-tools/config/${distro}/${layer}"
        local layer_cfgs=""

        layer_cfgs=$(find $(for x in $GIT_LIST; do echo $x/; done) -maxdepth 1 -name ${layer_cfg_name})

        if [ -f ${root_dir}/${required_layer_cfg_name} ]; then
            for line in $(grep -v '^#' ${root_dir}/${required_layer_cfg_name}); do
                local lower_layer=${line%%,*}
                local url=${line##*,}
                grep -q "^${lower_layer}$" $layer_cfgs
                if [ $? -ne 0 ]; then
                    curl ${url}
                fi
            done | sort --unique
        fi
    else
        local root_dir=""
        local root_file=""
        local list_type_extension=""
        local list_type_extension_bt=""
        local search_target=""

        if [ "${list_type}" != "std" ]; then
            list_type_extension="_${list_type}"
            list_type_extension_bt="-${list_type}"
        fi

        root_dir="${MY_REPO}/build-tools/build_${build_target}"
        root_file="${root_dir}/image${list_type_extension_bt}.inc"
        search_target=${distro}_${build_target}_image${list_type_extension}.inc

        (
        if [ -f ${root_file} ]; then
            grep '^[^#]' ${root_file}
        fi

        for d in $GIT_LIST; do
            find $d/ -maxdepth 1 -name "${search_target}" -exec grep '^[^#]' {} +
        done
        ) | sort --unique
    fi
}
