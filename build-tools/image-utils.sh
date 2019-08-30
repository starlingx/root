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

    for dir in utilities integ stx-utilities stx-integ; do
        path="$MY_REPO/stx/$dir/utilities/build-info/release-info.inc"
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

    for dir in stx-metal metal; do
        path="$MY_REPO/stx/$dir/bsp-files"
        if [ -d "$path" ]; then
            echo "$path"
            return 0
        fi
    done

    echo "/invalid-path-to-bsp-files"
    return 1
}

#
# image_inc_list <build_target> <build_type> <distro>
#
# Parameters:
#    build_target: One of 'iso', 'guest' ...
#    build_type:   One of 'std', 'rt', 'dev' ...
#    distro:       One of 'centos', ...
#
# Returns: A list of unique package that must be included for
#          the desired distro's build target and build type.
#          This is the union of the global and per git 
#          image.inc files.

image_inc_list () {
    local build_target=$1
    local build_type=$2
    local distro=$3

    local root_file=""
    local build_type_extension=""
    local search_target=""

    if [ "${build_type}" != "std" ]; then
        build_type_extension="_${build_type}"
        build_type_extension_bt="-${build_type}"
    fi

    root_dir="${MY_REPO}/build-tools/build_${build_target}"
    root_file="${root_dir}/image${build_type_extension_bt}.inc"
    search_target=${distro}_${build_target}_image${build_type_extension}.inc

    (
    if [ -f ${root_file} ]; then
        grep '^[^#]' ${root_file}
    fi
    for d in $GIT_LIST; do
        find $d/ -maxdepth 1 -name "${search_target}" -exec grep '^[^#]' {} +
    done
    ) | sort --unique
}
