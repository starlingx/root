#!/bin/bash
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This runs inside a container to update the image
#

UPDATES_DIR=/image-update
PIP_PACKAGES_DIR=${UPDATES_DIR}/pip-packages
DIST_PACKAGES_DIR=${UPDATES_DIR}/dist-packages
CUSTOMIZATION_SCRIPT=${UPDATES_DIR}/customize.sh

OS_NAME=$(source /etc/os-release && echo ${NAME})

OPTS=$(getopt -o h -l help: -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

    This utility is called from update-stx-image.sh to update an image,
    and is not intended to be run manually.
EOF
}

eval set -- "${OPTS}"

while true; do
    case $1 in
        --)
            # End of getopt arguments
            shift
            break
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


function install_centos_dist_packages {
    yum install -y --cacheonly --disablerepo=* ${DIST_PACKAGES_DIR}/*.rpm

    if [ $? -ne 0 ]; then
        echo "Failed yum install" >&2
        exit 1
    fi
}

function install_dist_packages {
    local -i file_count=0

    file_count=$(find ${DIST_PACKAGES_DIR} -type f 2>/dev/null | wc -l)

    if [ ${file_count} -eq 0 ]; then
        # No files, nothing to do
        return 0
    fi

    case ${OS_NAME} in
        "CentOS Linux")
            install_centos_dist_packages
            ;;
        *)
            echo "Unsupported OS for DIST_PACKAGES: ${OS_NAME}" >&2
            exit 1
            ;;
    esac
}

function install_pip_packages {
    local modules
    local wheels
    modules=$(find ${PIP_PACKAGES_DIR}/modules/* -maxdepth 0 -type d 2>/dev/null)
    wheels=$(find ${PIP_PACKAGES_DIR}/wheels/ -type f -name '*.whl' 2>/dev/null)

    if [ -z "${modules}" -a -z "${wheels}" ]; then
        # Nothing to do
        return 0
    fi

    pip install -vvv --no-deps --no-index --pre --no-cache-dir --only-binary :all: --no-compile --force-reinstall \
        ${modules} ${wheels}

    if [ $? -ne 0 ]; then
        echo "Failed pip install" >&2
        exit 1
    fi
}

function run_customization_script {
    if [ -x "${CUSTOMIZATION_SCRIPT}" ]; then
        bash -x ${CUSTOMIZATION_SCRIPT}

        if [ $? -ne 0 ]; then
            echo "Failed customization script" >&2
            exit 1
        fi
    fi
}

# Update the image
install_dist_packages
install_pip_packages
run_customization_script

exit 0

