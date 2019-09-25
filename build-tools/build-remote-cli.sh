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

IMAGE_TAG="master-centos-stable-latest"
PLATFORM_IMAGE_TAG="${IMAGE_TAG}"
APPLICATION_IMAGE_TAG="${IMAGE_TAG}"
OUTPUT_FILE="stx-remote-cli"
VERSION="1.0"

CLIENTS_REPO="${MY_REPO}/stx/clients"
REMOTE_CLI_FOLDER="remote_cli"
BUILD_OUTPUT_PATH="${MY_WORKSPACE}/std/build-remote-cli"
TAG_FILE="docker_image_version.sh"
COMMON_TAG=0
SPECIFIC_TAG=0
CUSTOM_PLATFORM_TAG=0
CUSTOM_APPLICATION_TAG=0

function usage {
    echo "Usage:"
    echo "$(basename $0) [--version <version>] [-o, --output <output_file>] [-t. --tag <image_tag>]"
    echo "               [--application-tag <image_tag>] [--platform-tag <image_tag>] [-h]"
    echo "Options:"
    echo "  -h                              show help options"
    echo "  --version <version>             specify remote CLI version"
    echo "                                  (default value is 1.0)"
    echo "  -o,  --output <output_file>     specify tarball output name"
    echo "                                  (default value is stx-remote-cli)"
    echo "  -t, --tag <image_tag>           specify docker image tag for both platform and application."
    echo "                                  cannot be used together with --platform-tag or --application-tag options"
    echo "                                  (default value is mater-centos-stable-latest)"
    echo " --platform-tag <image_tag>       specify platform docker image tag."
    echo "                                  cannot be used together with --tag option"
    echo "                                  (default value is mater-centos-stable-latest)"
    echo " --application-tag <image_tag>    specify application docker image tag."
    echo "                                  cannot be used together with --tag option"
    echo "                                  (default value is mater-centos-stable-latest)"
}

OPTS=$(getopt -o h,o:,t: -l version:,output:,tag:,platform-tag:,application-tag: -- "$@")
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
        -h)
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
        -t | --tag)
            PLATFORM_IMAGE_TAG=$2
            APPLICATION_IMAGE_TAG=$2
            CUSTOM_PLATFORM_TAG=1
            CUSTOM_APPLICATION_TAG=1
            COMMON_TAG=1
            shift 2
            ;;
        --platform-tag)
            PLATFORM_IMAGE_TAG=$2
            CUSTOM_PLATFORM_TAG=1
            SPECIFIC_TAG=1
            shift 2
            ;;
        --application-tag)
            APPLICATION_IMAGE_TAG=$2
            CUSTOM_APPLICATION_TAG=1
            SPECIFIC_TAG=1
            shift 2
            ;;
        *)
            usage
            exit 1
    esac
done

if [[ ${SPECIFIC_TAG} -eq 1 ]] && [[ ${COMMON_TAG} -eq 1 ]]; then
    echo "Cannot use both \"--tag\" and \"--application-tag\"/\"--platform-tag\" options at the same time" >&2
    exit 1
fi

if [ -d ${BUILD_OUTPUT_PATH} ]; then
    # Clean the previous build
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

if [ ${CUSTOM_PLATFORM_TAG} -eq 1 ]; then
    # Replace the platform image tag
    sed -i "s/PLATFORM_DOCKER_IMAGE_TAG=[^ ]*/PLATFORM_DOCKER_IMAGE_TAG=\"${PLATFORM_IMAGE_TAG}\"/" "${REMOTE_CLI_FOLDER}/${TAG_FILE}"
fi

if [ ${CUSTOM_APPLICATION_TAG} -eq 1 ]; then
    # Replace the application image tag
    sed -i "s/APPLICATION_DOCKER_IMAGE_TAG=[^ ]*/APPLICATION_DOCKER_IMAGE_TAG=\"${APPLICATION_IMAGE_TAG}\"/" "${REMOTE_CLI_FOLDER}/${TAG_FILE}"
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
