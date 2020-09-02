#
# Copyright (c) 2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# Functions common to build-srpm-serial and build-srpm-parallel.
#

SRC_BUILD_TYPE_SRPM="srpm"
SRC_BUILD_TYPE_SPEC="spec"
SRC_BUILD_TYPES="$SRC_BUILD_TYPE_SRPM $SRC_BUILD_TYPE_SPEC"

set_build_info () {
    local info_file="$MY_WORKSPACE/BUILD_INFO"
    local layer_prefix="${LAYER^^}_"
    if [ "${LAYER}" == "" ]; then
        layer_prefix=""
    fi
    mkdir -p "$(dirname ${info_file})"
    echo "${layer_prefix}OS=\"centos\"" > "${info_file}"
    echo "${layer_prefix}JOB=\"n/a\"" >> "${info_file}"
    echo "${layer_prefix}BUILD_BY=\"${USER}\"" >> "${info_file}"
    echo "${layer_prefix}BUILD_NUMBER=\"n/a\"" >> "${info_file}"
    echo "${layer_prefix}BUILD_HOST=\"$(hostname)\"" >> "${info_file}"
    echo "${layer_prefix}BUILD_DATE=\"$(date '+%Y-%m-%d %H:%M:%S %z')\"" >> "${info_file}"
}


str_lst_contains() {
    TARGET="$1"
    LST="$2"

    if [[ $LST =~ (^|[[:space:]])$TARGET($|[[:space:]]) ]] ; then
        return 0
    else
        return 1
    fi
}


#
# md5sums_from_input_vars <src-build-type> <srpm-or-spec-path> <work-dir>
#
# Returns md5 data for all input files of a src.rpm.
# Assumes PKG_BASE, ORIG_SRPM_PATH have been defined and the
# build_srpm.data file has already been sourced.
#
# Arguments:
#   src-build-type: Any single value from $SRC_BUILD_TYPES.
#                   e.g. 'srpm' or 'spec'
#   srpm-or-spec-path: Absolute path to an src.rpm, or to a
#                      spec file.
#   work-dir: Optional working directory.  If a path is
#             specified but does not exist, it will be created.
#
# Returns: output of md5sum command with canonical path names
#
md5sums_from_input_vars () {
    local SRC_BUILD_TYPE="$1"
    local SRPM_OR_SPEC_PATH="$2"
    local WORK_DIR="$3"

    local TMP_FLAG=0
    local LINK_FILTER='[/]stx[/]downloads[/]'

    if ! str_lst_contains "$SRC_BUILD_TYPE" "$SRC_BUILD_TYPES" ; then
        >&2  echo "ERROR: $FUNCNAME (${LINENO}): invalid arg: SRC_BUILD_TYPE='$SRC_BUILD_TYPE'"
        return 1
    fi

    if [ -z $WORK_DIR ]; then
        WORK_DIR=$(mktemp -d /tmp/${FUNCNAME}_XXXXXX)
        if [ $? -ne 0 ]; then
            >&2  echo "ERROR: $FUNCNAME (${LINENO}): mktemp -d /tmp/${FUNCNAME}_XXXXXX"
            return 1
        fi
        TMP_FLAG=1
    else
        mkdir -p "$WORK_DIR"
        if [ $? -ne 0 ]; then
            >&2  echo "ERROR: $FUNCNAME (${LINENO}): mkdir -p '$WORK_DIR'"
            return 1
        fi
    fi

    local INPUT_FILES_SORTED="$WORK_DIR/srpm_sorted_input.files"

    # Create lists of input files (INPUT_FILES) and symlinks (INPUT_LINKS).
    srpm_source_file_list "$SRC_BUILD_TYPE" "$SRPM_OR_SPEC_PATH" "$INPUT_FILES_SORTED"
    if [ $? -eq 1 ]; then
        return 1
    fi

    # Remove $MY_REPO prefix from paths
    cat $INPUT_FILES_SORTED | xargs -d '\n'  md5sum | sed "s# $(readlink -f $MY_REPO)/# #"

    if [ $TMP_FLAG -eq 0 ]; then
        \rm -f $INPUT_FILES_SORTED
    else
        \rm -rf $WORK_DIR
    fi

    return 0
}
