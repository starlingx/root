#!/bin/bash
#
# Copyright (c) 2024 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

source ${MY_REPO}/build-tools/git-utils.sh || exit 1
VERBOSE=false

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0) [ --verbose ]
Options:
    --verbose:
            Verbose output

    --help:
            Give this help list
EOF
}

# Find helm.build
function get_extra_files {
    find ${GIT_LIST} -maxdepth 3 -type f -name "*helm.build" -and -path "*/debian/*.helm.build"
}

function perform_build {
    local extra_build_file=$1

    export SOURCE_PATH=${extra_build_file%%/debian/*}
    local BUILD_COMMAND OUTPUT_PATTERNS

    #Capture the build command
    BUILD_COMMAND=$(source "$extra_build_file" && echo "$BUILD_COMMAND") || exit 1
    if [ -z "$BUILD_COMMAND" ] ; then
        echo "Error: BUILD_COMMAND is empty or not set"
    fi

    #Capture the Output_patterns relative to helm-build directory
    OUTPUT_PATTERNS=$(source "$extra_build_file" && echo "$OUTPUT_PATTERNS") || exit 1
    if [ -z "$BUILD_COMMAND" ] ; then
        echo "Error: OUTPUT_PATTERNS is empty or not set"
    fi


    if [ "$VERBOSE" = true ] ; then
        echo "Build command found: $BUILD_COMMAND"
        echo "Output files will be in the following pattern: $OUTPUT_PATTERNS"
    fi

    echo "Running $BUILD_COMMAND command on $SOURCE_PATH"
    (
        set -e

        mkdir -p $MY_WORKSPACE/std/build-helm
        cd "$SOURCE_PATH"
        $BUILD_COMMAND
    )
    local output_dir
    output_dir=$MY_WORKSPACE/std/build-helm

    # Create extra helm charts list on helm build
    ( cd "$output_dir" && ls $OUTPUT_PATTERNS ; ) >"extra-helm-charts.lst" || exit 1

}

OPTS=$(getopt -o h,a:,A:,B:,r:,i:,l:,p: -l help,verbose -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi

eval set -- "${OPTS}"

while true; do
    case $1 in
        --)
            # End of getopt arguments
            shift
            break
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h | --help )
            usage
            exit 1
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

declare -a EXTRA_FILES
EXTRA_FILES=($(get_extra_files)) || exit 1

if [ ${#EXTRA_FILES[@]} -eq 0 ]; then
    echo "WARNING: Could not find helm.build files" >&2
    exit 0
fi

if [ "$VERBOSE" = true ] ; then
    echo" .helm.build files found: $EXTRA_FILES"
fi

for extra_file in ${EXTRA_FILES}; do
    perform_build $extra_file
done

exit 0

