#!/bin/bash
#
# Copyright (c) 2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility retrieves StarlingX python wheels
# from the build output
#

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

SUPPORTED_OS_ARGS=('debian')
SUPPORTED_OS_CODENAME_ARGS=('bullseye' 'trixie')
OS=
OS_CODENAME=
BUILD_STREAM=stable

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0) [ --os <os> ] [ --stream <stable|dev> ]

Options:
    --os:          Specify base OS (eg. debian)
    --os-codename: Specify base OS (eg. trixie, bullseye)
    --stream:      Openstack release (default: stable)

EOF
}

OPTS=$(getopt -o h -l help,os:,os-codename:,release:,stream: -- "$@")
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
        --os)
            OS=$2
            shift 2
            ;;
        --os-codename)
            OS_CODENAME=$2
            shift 2
            ;;
        --stream)
            BUILD_STREAM=$2
            shift 2
            ;;
        --release) # Temporarily keep --release support as an alias for --stream
            BUILD_STREAM=$2
            shift 2
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

# Validate the OS option
if [ -z "$OS" ] ; then
    OS="$(ID= && source /etc/os-release 2>/dev/null && echo $ID || true)"
    if ! [ -n "$OS" ]; then
        echo "Unable to determine OS" >&2
        echo "Re-run with \"--os\" option" >&2
        exit 1
    fi
fi
VALID_OS=1
for supported_os in ${SUPPORTED_OS_ARGS[@]}; do
    if [ "$OS" = "${supported_os}" ]; then
        VALID_OS=0
        break
    fi
done
if [ ${VALID_OS} -ne 0 ]; then
    echo "Unsupported OS specified: ${OS}" >&2
    echo "Supported OS options: ${SUPPORTED_OS_ARGS[@]}" >&2
    exit 1
fi

if [ -z "$OS_CODENAME" ] ; then
    if [[ ! -z "$DEBIAN_DISTRIBUTION" ]]; then
        OS_CODENAME="$DEBIAN_DISTRIBUTION"
    else
        OS_CODENAME="$(ID= && source /etc/os-release 2>/dev/null && echo $VERSION_CODENAME || true)"
    fi
    if [[ -z "$OS_CODENAME" ]] ; then
        echo "Unable to determine OS_CODENAME, please re-run with \`--os-codename' option" >&2
        exit 1
    fi
fi
VALID_OS_CODENAME=1
for supported_os_codename in ${SUPPORTED_OS_CODENAME_ARGS[@]}; do
    if [ "$OS_CODENAME" = "${supported_os_codename}" ]; then
        VALID_OS_CODENAME=0
        break
    fi
done
if [ ${VALID_OS_CODENAME} -ne 0 ]; then
    echo "Unsupported OS_CODENAME specified: ${OS_CODENAME}" >&2
    echo "Supported OS_CODENAME options: ${SUPPORTED_OS_CODENAME_ARGS[@]}" >&2
    exit 1
fi

source ${MY_REPO}/build-tools/git-utils.sh

function get_wheels_files {
    find ${GIT_LIST} -maxdepth 1 \! -path "$(git_ctx_root_dir)/do-not-build/*" \
                     \( -name "${OS}_${BUILD_STREAM}_wheels.inc" \
                     -o -name "${OS}_${OS_CODENAME}_${BUILD_STREAM}_wheels.inc" \)
}

function get_lower_layer_wheels_files {
    # FIXME: debian: these are in repomgr pod, can't get to them easily
    if [[ "${OS}" == "debian" ]] ; then
        echo "$OS: lower layer wheels not supported!" >&2
        return 1
    fi
    # find ${DEBIAN_REPO}/layer_wheels_inc -maxdepth 1 -name "*_${OS}_${BUILD_STREAM}_wheels.inc"
}

function find_wheel_deb {
    local wheel="$1"
    local repo=
    # FIXME: debian: we should also scan non-stx packages, but they are in repomgr
    #        pod and we can't easily get to them.
    for repo in ${MY_WORKSPACE}/std ; do
        if [ -d $repo ]; then
            find $repo -name "${wheel}_[^-]*-[^-]*[.][^.]*[.]deb"
        fi
    done | head -n 1
}

declare -a WHEELS_FILES=($(get_wheels_files) $(get_lower_layer_wheels_files))
if [ ${#WHEELS_FILES[@]} -eq 0 ]; then
    echo "Could not find ${OS}-${OS_CODENAME} wheels.inc files" >&2
    exit 1
fi

BUILD_OUTPUT_PATH=${MY_WORKSPACE}/std/build-wheels-${OS}-${OS_CODENAME}-${BUILD_STREAM}/stx
echo "BUILD_OUTPUT_PATH: $BUILD_OUTPUT_PATH" >&2
if [ -d ${BUILD_OUTPUT_PATH} ]; then
    # Wipe out the existing dir to ensure there are no stale files
    rm -rf ${BUILD_OUTPUT_PATH}
fi
mkdir -p ${BUILD_OUTPUT_PATH}
cd ${BUILD_OUTPUT_PATH}

# Extract the wheels
declare -a FAILED
for wheel in $(sed -e 's/#.*//' ${WHEELS_FILES[@]} | sort -u); do
    case $OS in
        debian)
            wheelfile="$(find_wheel_deb ${wheel})"
            if [ ! -e "${wheelfile}" ]; then
                echo "Could not find ${wheel}" >&2
                FAILED+=($wheel)
                continue
            fi

            echo Extracting ${wheelfile}
            ar p ${wheelfile} data.tar.xz | tar -xJ
            if [ ${PIPESTATUS[0]} -ne 0 -o ${PIPESTATUS[1]} -ne 0 ]; then
                echo "Failed to extract content of ${wheelfile}" >&2
                FAILED+=($wheel)
            fi

            ;;
    esac
done

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "Failed to find or extract one or more wheel packages:" >&2
    for wheel in ${FAILED[@]}; do
        echo "${wheel}" >&2
    done
    exit 1
fi

exit 0

