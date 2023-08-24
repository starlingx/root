#!/bin/bash
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility retrieves StarlingX remote CLI
# wrapper scripts from the REPO and packages
# them in a tarball
#

# Required environment variables
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for build" >&2
    exit 1
fi

PLATFORM_IMAGE=
APPLICATION_IMAGE=
OUTPUT_FILE="stx-remote-cli"
VERSION="1.0"

CLIENTS_REPO="${MY_REPO}/stx/clients"
REMOTE_CLI_FOLDER="remote_cli"
BUILD_OUTPUT_PATH="${MY_WORKSPACE}/std/build-remote-cli"
IMAGE_FILE="docker_image_version.sh"
CUSTOM_PLATFORM_IMAGE=0
CUSTOM_APPLICATION_IMAGE=0

function usage {
    echo "Usage:"
    echo "$(basename $0) [--version <version>] [-o, --output <output_file>] [-t. --tag <image_tag>]"
    echo "               [--application-image <image>] [--platform-image <image>] [-h]"
    echo "Options:"
    echo "  -h,--help                       show help options"
    echo "  --version <version>             specify remote CLI version"
    echo "                                  (default value is 1.0)"
    echo "  -o,  --output <output_file>     specify tarball output name"
    echo "                                  (default value is stx-remote-cli)"
    echo " --platform-image <image>         override platform docker image"
    echo " --application-image <image>      override application docker image"
}

OPTS=$(getopt -o h,o:,t: -l help,version:,output:,tag:,platform-image:,application-image: -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi

eval set -- "${OPTS}"

while true; do
    case $1 in
        --)
            shift
            break
            ;;
        -h|--help)
            usage
            exit 1
            ;;
        --version)
            VERSION=$2
            shift 2
            ;;
        -o | --output)
            OUTPUT_FILE=$2
            shift 2
            ;;
        --platform-image)
            PLATFORM_IMAGE=$2
            CUSTOM_PLATFORM_IMAGE=1
            shift 2
            ;;
        --application-image)
            APPLICATION_IMAGE=$2
            CUSTOM_APPLICATION_IMAGE=1
            shift 2
            ;;
        *)
            usage
            exit 1
    esac
done

# Clean the previous build
if [ -d ${BUILD_OUTPUT_PATH} ]; then
    rm -rf ${BUILD_OUTPUT_PATH}
    if [ $? -ne 0 ]; then
        echo "Failed to cleanup workspace ${BUILD_OUTPUT_PATH}" >&2
        exit 1
    fi
fi

mkdir -p ${BUILD_OUTPUT_PATH}
if [ $? -ne 0 ]; then
    echo "Failed to create the workspace ${BUILD_OUTPUT_PATH}" >&2
    exit 1
fi

cd ${BUILD_OUTPUT_PATH}
cp -r "${CLIENTS_REPO}/${REMOTE_CLI_FOLDER}" .

if [ ${CUSTOM_PLATFORM_IMAGE} -eq 1 ]; then
    # Replace the platform image tag
    # Since the full path to a docker image contains slashes, we must escape them in order to pass them to sed.
    # The "${PLATFORM_IMAGE//\//\\/} takes the path and escapes all the slashes.
    sed -i "s/PLATFORM_DOCKER_IMAGE=[^ ]*/PLATFORM_DOCKER_IMAGE=\"${PLATFORM_IMAGE//\//\\/}\"/" "${REMOTE_CLI_FOLDER}/${IMAGE_FILE}"
fi

if [ ${CUSTOM_APPLICATION_IMAGE} -eq 1 ]; then
    # Replace the application image tag
    sed -i "s/APPLICATION_DOCKER_IMAGE=[^ ]*/APPLICATION_DOCKER_IMAGE=\"${APPLICATION_IMAGE//\//\\/}\"/" "${REMOTE_CLI_FOLDER}/${IMAGE_FILE}"
fi

# Create archive
tar czf ${OUTPUT_FILE}-${VERSION}.tgz ${REMOTE_CLI_FOLDER}
if [ $? -ne 0 ]; then
    echo "Failed to create ${OUTPUT_FILE}-${VERSION}.tgz tarball" >&2
    exit 1
fi

echo ""
echo "Created remote CLI tarball: ${BUILD_OUTPUT_PATH}/${OUTPUT_FILE}-${VERSION}.tgz"
echo ""
