#!/bin/bash
#
# Copyright (c) 2018-2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility builds the StarlingX base image
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../utils.sh
source ${MY_SCRIPT_DIR}/../git-utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

# make this process nice
renice -n 10 -p $$
ionice -c 3 -p $$

SUPPORTED_OS_ARGS=( 'debian' )
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
declare -i RETRY_DELAY=30

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
    --attempts <count>
                  Max attempts, in case of failure (default: 1)
    --retry-delay <seconds>
                  Sleep this many seconds between retries (default: 30)
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

OPTS=$(getopt -o h -l help,os:,os-version:,version:,stream:,release:,repo:,push,proxy:,latest,latest-tag:,user:,registry:,local,clean,cache,hostname:,attempts:,retry-delay:,config-file: -- "$@")
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
            REPO_LIST+=("$2")
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
        --retry-delay)
            RETRY_DELAY=$2
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

SRC_DOCKER_DIR="${MY_SCRIPT_DIR}/stx-${OS}"
SRC_DOCKERFILE="${SRC_DOCKER_DIR}"/Dockerfile.${BUILD_STREAM}
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
    if [ "${LOCAL}" != "yes" -a "${BUILD_STREAM}" != "dev" -a "${BUILD_STREAM}" != "master" ]; then
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
if [[ "$OS" == "debian" ]] ; then
    # These env vars must be defined in debian builder pods
    for var in DEBIAN_SNAPSHOT DEBIAN_SECURITY_SNAPSHOT DEBIAN_DISTRIBUTION REPOMGR_DEPLOY_URL REPOMGR_ORIGIN ; do
        if [[ -z "${!var}" ]] ; then
            echo "$var must be defined in the environment!" >&2
            exit 1
        fi
    done
    unset var

    # Replace "@...@" tokens in apt template files
    function replace_vars {
        sed -e "s!@DEBIAN_SNAPSHOT@!${DEBIAN_SNAPSHOT}!g" \
            -e "s!@DEBIAN_SECURITY_SNAPSHOT@!${DEBIAN_SECURITY_SNAPSHOT}!g" \
            -e "s!@DEBIAN_DISTRIBUTION@!${DEBIAN_DISTRIBUTION}!g" \
            -e "s!@REPOMGR_DEPLOY_URL@!${REPOMGR_DEPLOY_URL}!g" \
            -e "s!@REPOMGR_ORIGIN@!${REPOMGR_ORIGIN}!g" \
            -e "s!@LAYER@!${LAYER}!g" \
            "$@"
    }

    # create apt/ files for the docker file
    mkdir -p "${BUILDDIR}/apt"

    # debian.sources.list
    replace_vars "${SRC_DOCKER_DIR}/apt/debian.sources.list.in" >"${BUILDDIR}/apt/debian.sources.list"

    # <layer>.sources.list
    # These can be optionally used if it is necessary to build an image that
    # requires dependencies that are in repositories not listed in
    # `stx.sources.list`.
    layer_cfg_name="${OS}_build_layer.cfg"
    layer_cfgs=($(find ${GIT_LIST} -maxdepth 1 -name ${layer_cfg_name}))
    LAYERS=($(
      for layer_cfg in "${layer_cfgs[@]}"; do
        echo $(cat "${layer_cfg}")
      done | sort --unique
    ))

    for LAYER in "${LAYERS[@]}"; do
        replace_vars "${SRC_DOCKER_DIR}/apt/layer.sources.list.in" >"${BUILDDIR}/apt/${LAYER}.layer.sources.list"
    done

    # stx.sources: if user provided any --repo's use them instead of the template
    if [[ "${#REPO_LIST[@]}" -gt 0 ]] ; then
        rm -f "${BUILDDIR}/apt/stx.sources.list"
        for repo in "${REPO_LIST[@]}" ; do
            echo "$repo" >>"${BUILDDIR}/apt/stx.sources.list"
        done
        unset repo
    # otherwise use the template file
    else
        replace_vars "${SRC_DOCKER_DIR}/apt/stx.sources.list.in" >"${BUILDDIR}/apt/stx.sources.list"
    fi

    # preferences: instantiate template with REPOMGR_ORIGIN from environment
    replace_vars "${SRC_DOCKER_DIR}/apt/stx.preferences.part.in" >>"${BUILDDIR}/apt/stx.preferences"
    unset -f replace_vars
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
if [[ "$OS" == "debian" ]] ; then
    BUILD_ARGS+=(--build-arg "DIST=${DEBIAN_DISTRIBUTION}")
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
with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} docker build "${BUILD_ARGS[@]}"
if [ $? -ne 0 ]; then
    echo "Failed running docker build command" >&2
    exit 1
fi

if [ "${PUSH}" = "yes" ]; then
    # Push the image
    echo "Pushing image: ${IMAGE_NAME}"
    with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} docker push ${IMAGE_NAME}
    if [ $? -ne 0 ]; then
        echo "Failed running docker push command" >&2
        exit 1
    fi

    if [ "$TAG_LATEST" = "yes" ]; then
        docker tag ${IMAGE_NAME} ${IMAGE_NAME_LATEST}
        echo "Pushing image: ${IMAGE_NAME_LATEST}"
        with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} docker push ${IMAGE_NAME_LATEST}
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

