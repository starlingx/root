#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A place for any functions related to wheels.inc files
#

WHEEL_UTILS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${WHEEL_UTILS_DIR}/git-utils.sh"

#
# wheels_inc_list <stream> <distro> [<layer>]
#
# Parameters:
#    stream:    One of 'stable', 'dev'
#    distro:    One of 'centos', ...
#
# Returns: A list of unique rpm packages that contain needed wheel
#          files.  This is the union per git wheels.inc files.

wheels_inc_list () {
    local stream=$1
    local distro=$2

    local search_target=${distro}_${stream}_wheels.inc

    (
    for d in $GIT_LIST; do
        find $d/ -maxdepth 1 -name "${search_target}" -exec grep '^[^#]' {} +
    done
    ) | sort --unique
}
