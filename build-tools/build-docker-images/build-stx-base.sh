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

SUPPORTED_OS_ARGS=('centos' 'debian')
OS=                      # default: autodetect
OS_VERSION=              # default: lookup "ARG RELEASE" in Dockerfile
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
USE_DOCKER_CACHE=no
declare -i MAX_ATTEMPTS=1

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

Options:
    --os:         Specify base OS (valid options: ${SUPPORTED_OS_ARGS[@]})
    --os-version: Specify OS version
    --version:    Specify version for output image
    --stream:     Build stream, stable or dev (default: stable)
    --repo:       Software repository, can be specified multiple times
                    * CentOS format: "NAME,BASEURL"
                    * Debian format: "TYPE [OPTION=VALUE...] URL DISTRO COMPONENTS..."
                      This will be added to /etc/apt/sources.list as is,
                      see also sources.list(5) manpage.
    --local:      Use local build for software repository (cannot be used with --repo)
    --push:       Push to docker repo
    --proxy:      Set proxy <URL>:<PORT>
    --latest:     Add a 'latest' tag when pushing
    --latest-tag: Use the provided tag when pushing latest.
    --user:       Docker repo userid
    --registry:   Docker registry
    --clean:      Remove image(s) from local registry
    --hostname:   build repo host
    --attempts:   Max attempts, in case of failure (default: 1)
    --config-file:Specify a path to a config file which will specify additional arguments to be passed into the command

    --cache:      Allow docker to use cached filesystem layers when building
                    CAUTION: this option may ignore locally-generated packages
                             and is meant for debugging the build scripts.
EOF
}

function get_args_from_file {
    # get additional args from specified file.
    local line key value

    echo "Get args from file: $1"
    while read line
    do
        # skip comments & empty lines
        if echo "$line" | grep -q -E '^\s*(#.*)?$' ; then
            continue
        fi
        key="${line%%=*}"
        value=${line#*=}
        echo "--$key '$value'"
        case "$key" in
            version)
                if [ -z "${IMAGE_VERSION}" ]; then
                    IMAGE_VERSION="$value"
                fi
                ;;
            user)
                if [ -z "${DOCKER_USER}" ]; then
                    DOCKER_USER="$value"
                fi
                ;;
            proxy)
                if [ -z "${PROXY}" ]; then
                    PROXY="$value"
                fi
                ;;
            registry)
                if [ -z "${DOCKER_REGISTRY}" ]; then
                    # Add a trailing / if needed
                    DOCKER_REGISTRY="${value%/}/"
                fi
                ;;
            repo)
                REPO_LIST+=("$value")
                ;;
            *)
                echo "WARNING: $line: ignoring unknown option \"$key\"" >&2
                ;;
        esac
    done <"$1"
}

OPTS=$(getopt -o h -l help,os:,os-version:,version:,stream:,release:,repo:,push,proxy:,latest,latest-tag:,user:,registry:,local,clean,cache,hostname:,attempts:,config-file: -- "$@")
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
        --cache)
            USE_DOCKER_CACHE=yes
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
    if [[ -z "$OS" ]] ; then
        echo "Unable to determine OS, please re-run with \`--os' option" >&2
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

SRC_DOCKERFILE=${MY_SCRIPT_DIR}/stx-${OS}/Dockerfile.${BUILD_STREAM}
if [[ -z "$OS_VERSION" ]]; then
    OS_VERSION=$(
        sed -n -r 's/^\s*ARG\s+RELEASE\s*=\s*([^ \t#]+).*/\1/ip' $SRC_DOCKERFILE | head -n 1
        [[ ${PIPESTATUS[0]} -eq 0 ]]
    )
    if [[ -z "$OS_VERSION" ]] ; then
        echo "$SRC_DOCKERFILE: failed to determine OS_VERSION" >&2
        exit 1
    fi
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
        if [[ "$OS" == "centos" ]] ; then
            REPO_LIST+=("local-std,http://${HOST}:8088${MY_WORKSPACE}/std/rpmbuild/RPMS")
            REPO_LIST+=("stx-distro,http://${HOST}:8089${MY_REPO}/cgcs-${OS}-repo/Binary")
        else
            if [[ -z "$REPOMGR_DEPLOY_URL" ]] ; then
                echo "Required env variable REPOMGR_DEPLOY_URL is not defined!" >&2
                exit 1
            fi
            REPO_LIST+=("deb [trusted=yes] $REPOMGR_DEPLOY_URL/deb-local-binary bullseye main")
            REPO_LIST+=("deb [trusted=yes] $REPOMGR_DEPLOY_URL/deb-local-build bullseye main")
        fi
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
cp ${SRC_DOCKERFILE} ${BUILDDIR}/Dockerfile

# Generate the stx.repo file
if [[ "$OS" == "centos" ]] ; then
    STX_REPO_FILE=${BUILDDIR}/stx.repo
    for repo in ${REPO_LIST[@]}; do
        repo_name=$(echo $repo | awk -F, '{print $1}')
        repo_baseurl=$(echo $repo | awk -F, '{print $2}')

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

EOF

        REPO_OPTS="${REPO_OPTS} --enablerepo=${repo_name}"
    done
else
    STX_APT_SOURCES_FILE=${BUILDDIR}/stx.apt.sources.list
    rm -f "$STX_APT_SOURCES_FILE"
    for repo in "${REPO_LIST[@]}" ; do
        echo "$repo" >>"$STX_APT_SOURCES_FILE"
    done
fi

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
if [[ "$OS" == "centos" ]] ; then
    BUILD_ARGS+=(--build-arg "REPO_OPTS=${REPO_OPTS}")
fi

# Add proxy to docker build
if [ ! -z "$PROXY" ]; then
    BUILD_ARGS+=(--build-arg http_proxy=$PROXY)
fi

# Don't use docker cache
if [[ "$USE_DOCKER_CACHE" != "yes" ]] ; then
    BUILD_ARGS+=("--no-cache")
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

