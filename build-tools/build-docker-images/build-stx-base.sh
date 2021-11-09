#!/bin/bash
#
# Copyright (c) 2018-2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility builds the StarlingX base image
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../build-wheels/utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

SUPPORTED_OS_ARGS=('centos')
OS=centos
OS_VERSION=7.5.1804
BUILD_STREAM=stable
IMAGE_VERSION=
PUSH=no
PROXY=""
CONFIG_FILE=""
DEFAULT_CONFIG_FILE_DIR="${MY_REPO}/build-tools/build-docker-images"
DEFAULT_CONFIG_FILE_PREFIX="base-image-build"
DOCKER_USER=${USER}
DOCKER_REGISTRY=
declare -a REPO_LIST
REPO_OPTS=
LOCAL=no
CLEAN=no
TAG_LATEST=no
LATEST_TAG=latest
HOST=${HOSTNAME}
declare -i MAX_ATTEMPTS=1
declare -A REPO_PRIORITY_LIST

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

Options:
    --os:            Specify base OS (valid options: ${SUPPORTED_OS_ARGS[@]})
    --os-version:    Specify OS version
    --version:       Specify version for output image
    --stream:        Build stream, stable or dev (default: stable)
    --repo:          Software repository (Format: name,baseurl), can be specified multiple times
    --repo-priority: Define priority for added repo (Format: name,priority). The priority must be an integer from 1 to 99 (The default is 99). The lowest number have the highest priority.
    --local:         Use local build for software repository (cannot be used with --repo)
    --push:          Push to docker repo
    --proxy:         Set proxy <URL>:<PORT>
    --latest:        Add a 'latest' tag when pushing
    --latest-tag:    Use the provided tag when pushing latest.
    --user:          Docker repo userid
    --registry:      Docker registry
    --clean:         Remove image(s) from local registry
    --hostname:      build repo host
    --attempts:      Max attempts, in case of failure (default: 1)
    --config-file:   Specify a path to a config file which will specify additional arguments to be passed into the command

EOF
}

function get_args_from_file {
    # get additional args from specified file.
    local -a config_items

    echo "Get args from file: $1"
    for i in $(cat $1)
    do
        config_items=($(echo $i | sed s/=/\ /g))
        echo "--${config_items[0]} ${config_items[1]}"
        case ${config_items[0]} in
            version)
                if [ -z "${IMAGE_VERSION}" ]; then
                    IMAGE_VERSION=${config_items[1]}
                fi
                ;;
            user)
                if [ -z "${DOCKER_USER}" ]; then
                    DOCKER_USER=${config_items[1]}
                fi
                ;;
            proxy)
                if [ -z "${PROXY}" ]; then
                    PROXY=${config_items[1]}
                fi
                ;;
            registry)
                if [ -z "${DOCKER_REGISTRY}" ]; then
                    # Add a trailing / if needed
                    DOCKER_REGISTRY="${config_items[1]%/}/"
                fi
                ;;
            repo)
                REPO_LIST+=(${config_items[1]})
                ;;
            repo-priority)
                priority_value=(${config_items[1]//,/ })
                REPO_PRIORITY_LIST[${priority_value[0]}]=${priority_value[1]}
                ;;
        esac
    done
}

OPTS=$(getopt -o h -l help,os:,os-version:,version:,stream:,release:,repo:,repo-priority:,push,proxy:,latest,latest-tag:,user:,registry:,local,clean,hostname:,attempts:,config-file: -- "$@")
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
        --os-version)
            OS_VERSION=$2
            shift 2
            ;;
        --version)
            IMAGE_VERSION=$2
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
        --repo)
            REPO_LIST+=($2)
            shift 2
            ;;
        --local)
            LOCAL=yes
            shift
            ;;
        --push)
            PUSH=yes
            shift
            ;;
        --proxy)
            PROXY=$2
            shift 2
            ;;
        --latest)
            TAG_LATEST=yes
            shift
            ;;
        --latest-tag)
            LATEST_TAG=$2
            shift 2
            ;;
        --user)
            DOCKER_USER=$2
            shift 2
            ;;
        --registry)
            # Add a trailing / if needed
            DOCKER_REGISTRY="${2%/}/"
            shift 2
            ;;
        --clean)
            CLEAN=yes
            shift
            ;;
        --hostname)
            HOST=$2
            shift 2
            ;;
        --attempts)
            MAX_ATTEMPTS=$2
            shift 2
            ;;
        --config-file)
            CONFIG_FILE=$2
            shift 2
            ;;
        --repo-priority)
            priority_value=(${2//,/ })
            REPO_PRIORITY_LIST[${priority_value[0]}]=${priority_value[1]}
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

if [ -z "${IMAGE_VERSION}" ]; then
    IMAGE_VERSION=${OS_VERSION}
fi

DEFAULT_CONFIG_FILE="${DEFAULT_CONFIG_FILE_DIR}/${DEFAULT_CONFIG_FILE_PREFIX}-${OS}-${BUILD_STREAM}.cfg"

# Read additional auguments from config file if it exists.
if [[ -z "$CONFIG_FILE" ]] && [[ -f ${DEFAULT_CONFIG_FILE} ]]; then
    CONFIG_FILE=${DEFAULT_CONFIG_FILE}
fi
if [[ ! -z  ${CONFIG_FILE} ]]; then
    if [[ -f ${CONFIG_FILE} ]]; then
        get_args_from_file ${CONFIG_FILE}
    else
        echo "Config file not found: ${CONFIG_FILE}"
        exit 1
    fi
fi

if [ ${#REPO_LIST[@]} -eq 0 ]; then
    # Either --repo or --local must be specified
    if [ "${LOCAL}" = "yes" ]; then
        REPO_LIST+=("local-std,http://${HOST}:8088${MY_WORKSPACE}/std/rpmbuild/RPMS")
        REPO_LIST+=("stx-distro,http://${HOST}:8088${MY_REPO}/cgcs-${OS}-repo/Binary")
    elif [ "${BUILD_STREAM}" != "dev" -a "${BUILD_STREAM}" != "master" ]; then
        echo "Either --local or --repo must be specified" >&2
        exit 1
    fi
else
    if [ "${LOCAL}" = "yes" ]; then
        echo "Cannot specify both --local and --repo" >&2
        exit 1
    fi
fi

BUILDDIR=${MY_WORKSPACE}/std/build-images/stx-${OS}
if [ -d ${BUILDDIR} ]; then
    # Leftover from previous build
    rm -rf ${BUILDDIR}
fi

mkdir -p ${BUILDDIR}
if [ $? -ne 0 ]; then
    echo "Failed to create ${BUILDDIR}" >&2
    exit 1
fi

# Get the Dockerfile
SRC_DOCKERFILE=${MY_SCRIPT_DIR}/stx-${OS}/Dockerfile.${BUILD_STREAM}
cp ${SRC_DOCKERFILE} ${BUILDDIR}/Dockerfile

# Generate the stx.repo file
STX_REPO_FILE=${BUILDDIR}/stx.repo
for repo in ${REPO_LIST[@]}; do
    repo_name=$(echo $repo | awk -F, '{print $1}')
    repo_baseurl=$(echo $repo | awk -F, '{print $2}')
    priority=''
    if [[ ! -z "${REPO_PRIORITY_LIST[$repo_name]}" ]] ; then
        priority="priority=${REPO_PRIORITY_LIST[$repo_name]}"
    fi

    if [ -z "${repo_name}" -o -z "${repo_baseurl}" ]; then
        echo "Invalid repo specified: ${repo}" >&2
        echo "Expected format: name,baseurl" >&2
        exit 1
    fi

    cat >>${STX_REPO_FILE} <<EOF
[${repo_name}]
name=${repo_name}
baseurl=${repo_baseurl}
enabled=1
gpgcheck=0
skip_if_unavailable=1
metadata_expire=0
${priority}

EOF

    REPO_OPTS="${REPO_OPTS} --enablerepo=${repo_name}"
done

# Check to see if the OS image is already pulled
docker images --format '{{.Repository}}:{{.Tag}}' ${OS}:${OS_VERSION} | grep -q "^${OS}:${OS_VERSION}$"
BASE_IMAGE_PRESENT=$?

# Pull the image anyway, to ensure it is up to date
docker pull ${OS}:${OS_VERSION}

# Build the image
IMAGE_NAME=${DOCKER_REGISTRY}${DOCKER_USER}/stx-${OS}:${IMAGE_VERSION}
IMAGE_NAME_LATEST=${DOCKER_REGISTRY}${DOCKER_USER}/stx-${OS}:${LATEST_TAG}

declare -a BUILD_ARGS
BUILD_ARGS+=(--build-arg RELEASE=${OS_VERSION})
BUILD_ARGS+=(--build-arg REPO_OPTS=${REPO_OPTS})

# Add proxy to docker build
if [ ! -z "$PROXY" ]; then
    BUILD_ARGS+=(--build-arg http_proxy=$PROXY)
fi
BUILD_ARGS+=(--tag ${IMAGE_NAME} ${BUILDDIR})

# Build base image
with_retries ${MAX_ATTEMPTS} docker build "${BUILD_ARGS[@]}"
if [ $? -ne 0 ]; then
    echo "Failed running docker build command" >&2
    exit 1
fi

if [ "${PUSH}" = "yes" ]; then
    # Push the image
    echo "Pushing image: ${IMAGE_NAME}"
    docker push ${IMAGE_NAME}
    if [ $? -ne 0 ]; then
        echo "Failed running docker push command" >&2
        exit 1
    fi

    if [ "$TAG_LATEST" = "yes" ]; then
        docker tag ${IMAGE_NAME} ${IMAGE_NAME_LATEST}
        echo "Pushing image: ${IMAGE_NAME_LATEST}"
        docker push ${IMAGE_NAME_LATEST}
        if [ $? -ne 0 ]; then
            echo "Failed running docker push command on latest" >&2
            exit 1
        fi
    fi
fi

if [ "${CLEAN}" = "yes" ]; then
    # Delete the images
    echo "Deleting image: ${IMAGE_NAME}"
    docker image rm ${IMAGE_NAME}
    if [ $? -ne 0 ]; then
        echo "Failed running docker image rm command" >&2
        exit 1
    fi

    if [ "$TAG_LATEST" = "yes" ]; then
        echo "Deleting image: ${IMAGE_NAME_LATEST}"
        docker image rm ${IMAGE_NAME_LATEST}
        if [ $? -ne 0 ]; then
            echo "Failed running docker image rm command" >&2
            exit 1
        fi
    fi

    if [ ${BASE_IMAGE_PRESENT} -ne 0 ]; then
        # The base image was not already present, so delete it
        echo "Removing docker image ${OS}:${OS_VERSION}"
        docker image rm ${OS}:${OS_VERSION}
        if [ $? -ne 0 ]; then
            echo "Failed to delete base image from docker" >&2
        fi
    fi
fi

