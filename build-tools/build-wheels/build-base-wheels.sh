#!/bin/bash
#
# Copyright (c) 2018-2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility sets up a docker image to build wheels
# for a set of upstream python modules.
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

KEEP_IMAGE=no
KEEP_CONTAINER=no
SUPPORTED_OS_LIST=( 'debian' )
OS=
OS_VERSION=
BUILD_STREAM=stable
HTTP_PROXY=""
HTTPS_PROXY=""
NO_PROXY=""
USE_DOCKER_CACHE=no
: ${PYTHON3:=python3}
declare -i MAX_ATTEMPTS=1
declare -i RETRY_DELAY=30

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0) [ --os <os> ] [ --keep-image ] [ --keep-container ] [ --stream <stable|dev> ]

Options:
    --os:             Override base OS (eg. debian; default: auto)
    --os-version:     Override OS version (default: auto)
    --keep-image:     Skip deletion of the wheel build image in docker
    --keep-container: Skip deletion of container used for the build
    --http_proxy:     Set http proxy <URL>:<PORT>, urls splitted by ","
    --https_proxy:    Set https proxy <URL>:<PORT>, urls splitted by ","
    --no_proxy:       Set bypass list for proxy <URL>, urls splitted by ","
    --stream:         Build stream, stable or dev (default: stable)
    --attempts <count>
                      Max attempts, in case of failure (default: 1)
    --retry-delay <seconds>
                     Sleep this many seconds between retries (default: 30)

    --cache:          Allow docker to use filesystem cache when building
                        CAUTION: this option may ignore locally-generated
                                 packages and is meant for debugging the build
                                 scripts.

EOF
}

OPTS=$(getopt -o h -l help,os:,os-version:,keep-image,keep-container,release:,stream:,http_proxy:,https_proxy:,no_proxy:,attempts:,retry-delay:,cache -- "$@")
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
        --keep-image)
            KEEP_IMAGE=yes
            shift
            ;;
        --keep-container)
            KEEP_CONTAINER=yes
            shift
            ;;
        --stream)
            BUILD_STREAM=$2
            shift 2
            ;;
        --release) # Temporarily keep --release support as an alias for --stream
            BUILD_STREAM=$2
            shift 2
            ;;
        --http_proxy)
            HTTP_PROXY=$2
            shift 2
            ;;
        --https_proxy)
            HTTPS_PROXY=$2
            shift 2
            ;;
        --no_proxy)
            NO_PROXY=$2
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
        --cache)
            USE_DOCKER_CACHE=yes
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

if [ -z "$OS" ] ; then
    OS="$(ID= && source /etc/os-release 2>/dev/null && echo $ID || true)"
    if ! [ -n "$OS" ]; then
        echo "Unable to determine OS" >&2
        echo "Re-run with \"--os\" option" >&2
        exit 1
    fi
fi

BUILD_IMAGE_NAME="${USER}-$(basename ${MY_WORKSPACE})-wheelbuilder:${OS}-${BUILD_STREAM}"

# BUILD_IMAGE_NAME can't have caps if it's passed to docker build -t $BUILD_IMAGE_NAME.
# The following will substitute caps with lower case.
BUILD_IMAGE_NAME="${BUILD_IMAGE_NAME,,}"

DOCKER_FILE=${MY_SCRIPT_DIR}/${OS}/Dockerfile

if [ ! -f ${DOCKER_FILE} ]; then
    echo "Unsupported OS specified: ${OS}" >&2
    echo "Supported OS options: ${SUPPORTED_OS_LIST[@]}" >&2
    exit 1
fi

if [ -z "$OS_VERSION" ]; then
    OS_VERSION="$(sed -r -n 's/^\s*ARG\s+RELEASE\s*=\s*(\S+).*/\1/p' "$DOCKER_FILE")"
    if [ -z "$OS_VERSION" ]; then
        echo "Unable to determine OS_VERSION" >&2
        echo "Re-run with \"--os-version\" option" >&2
        exit 1
    fi
fi

# Print a loud message
function notice {
    (
        set +x
        echo
        echo ======================================
        for s in "$@" ; do
            echo "$s"
        done
        echo ======================================
        echo
    ) 2>&1
}

# prefix each line of a command's output
# also redirects command's STDERR to STDOUT
log_prefix() {
    local prefix="$1" ; shift
    "$@" 2>&1 | awk -v prefix="$prefix" '{print prefix $0}'
    # return false if the command (rather than awk) failed
    [ ${PIPESTATUS[0]} -eq 0 ]
}


# Make sure a file exists, exit otherwise
function require_file {
    if [ ! -f "${1}" ]; then
        echo "Required file does not exist: ${1}" >&2
        exit 1
    fi
}

# Check build output directory for unexpected files,
# ie. wheels from old builds that are no longer in wheels.cfg
function prepare_output_dir {
    local output_dir="$1"
    local wheels_cfg="$2"
    if [ -d ${output_dir} ]; then
        local f
        for f in ${output_dir}/*; do
            if [ -f $f ] ; then
                grep -q "^$(basename $f)|" ${wheels_cfg}
                if [ $? -ne 0 ]; then
                    echo "Deleting stale file: $f"
                    rm -f $f
                fi
            fi
        done
    else
        mkdir -p ${output_dir}
        if [ $? -ne 0 ]; then
            echo "Failed to create directory: ${output_dir}" >&2
            exit 1
        fi
    fi
}

DOCKER_BUILD_PATH=${MY_WORKSPACE}/std/build-wheels-${OS}-${BUILD_STREAM}/docker
BUILD_OUTPUT_PATH=${MY_WORKSPACE}/std/build-wheels-${OS}-${BUILD_STREAM}/base
BUILD_OUTPUT_PATH_PY2=${MY_WORKSPACE}/std/build-wheels-${OS}-${BUILD_STREAM}/base-py2
WHEELS_CFG=${MY_SCRIPT_DIR}/${OS}/${BUILD_STREAM}-wheels.cfg
WHEELS_CFG_PY2=${MY_SCRIPT_DIR}/${OS}/${BUILD_STREAM}-wheels-py2.cfg

# make sure .cfg files exist
require_file "${WHEELS_CFG}"
require_file "${WHEELS_CFG_PY2}"

# prepare output directories
prepare_output_dir "${BUILD_OUTPUT_PATH}" "${WHEELS_CFG}"
prepare_output_dir "${BUILD_OUTPUT_PATH_PY2}" "${WHEELS_CFG_PY2}"

if [ "${BUILD_STREAM}" = "dev" -o "${BUILD_STREAM}" = "master" ]; then
    # Download the master wheel from loci, so we're only building pieces not covered by it
    MASTER_WHEELS_IMAGE="loci/requirements:master-${OS}"

    # Check to see if the wheels are already present.
    # If so, we'll still pull to ensure the image is updated,
    # but we won't delete it after
    docker images --format '{{.Repository}}:{{.Tag}}' ${MASTER_WHEELS_IMAGE} | grep -q "^${MASTER_WHEELS_IMAGE}$"
    MASTER_WHEELS_PRESENT=$?

    with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} docker pull ${MASTER_WHEELS_IMAGE}
    if [ $? -ne 0 ]; then
        echo "Failed to pull ${MASTER_WHEELS_IMAGE}" >&2
        exit 1
    fi

    # Export the image to a tarball.
    # The "docker run" will always fail, due to the construct of the wheels image,
    # so just ignore it
    docker run --name ${USER}_inspect_wheels ${MASTER_WHEELS_IMAGE} noop 2>/dev/null

    echo "Extracting wheels from ${MASTER_WHEELS_IMAGE}"
    rm -rf "${BUILD_OUTPUT_PATH}-loci"
    mkdir -p "$BUILD_OUTPUT_PATH-loci"
    docker export ${USER}_inspect_wheels | tar x -C "${BUILD_OUTPUT_PATH}-loci" '*.whl'
    if [ ${PIPESTATUS[0]} -ne 0 -o ${PIPESTATUS[1]} -ne 0 ]; then
        echo "Failed to extract wheels from ${MASTER_WHEELS_IMAGE}" >&2
        docker rm ${USER}_inspect_wheels
        if [ ${MASTER_WHEELS_PRESENT} -ne 0 ]; then
            docker image rm ${MASTER_WHEELS_IMAGE}
        fi
        rm -rf "${BUILD_OUTPUT_PATH}-loci"
        exit 1
    fi

    # copy loci wheels in base and base-py2 directories
    if ! cp "${BUILD_OUTPUT_PATH}-loci"/*.whl "${BUILD_OUTPUT_PATH}"/ ; then
        echo "Failed to copy wheels to ${BUILD_OPUTPUT_PATH}" >&2
        exit 1
    fi
    if ! cp "${BUILD_OUTPUT_PATH}-loci"/*.whl "${BUILD_OUTPUT_PATH_PY2}"/ ; then
        echo "Failed to copy wheels to ${BUILD_OPUTPUT_PATH_PY2}" >&2
        exit 1
    fi
    rm -rf "${BUILD_OUTPUT_PATH}-loci"

    docker rm ${USER}_inspect_wheels

    if [ ${MASTER_WHEELS_PRESENT} -ne 0 ]; then
        docker image rm ${MASTER_WHEELS_IMAGE}
    fi
fi

# check if there are any wheels missing
function all_wheels_exist {
    local output_dir="$1"
    local wheels_cfg="$2"
    local wheel
    for wheel in $(cat "${wheels_cfg}" | sed 's/#.*//' | awk -F '|' '{print $1}'); do
        if [[ "${wheel}" =~ \* || ! -f ${output_dir}/${wheel} ]]; then
            return 1
        fi
    done
    return 0
}

if all_wheels_exist "${BUILD_OUTPUT_PATH}" "${WHEELS_CFG}" && \
   all_wheels_exist "${BUILD_OUTPUT_PATH_PY2}" "${WHEELS_CFG_PY2}" ; then
    echo "All base wheels are already present. Skipping build."
    exit 0
fi

# Create a directory containing docker files
\rm -rf "${DOCKER_BUILD_PATH}"
mkdir -p "${DOCKER_BUILD_PATH}"
\cp -r "${MY_SCRIPT_DIR}/docker-common" "${MY_SCRIPT_DIR}/${OS}" "${DOCKER_BUILD_PATH}" || exit 1
# Replace "@...@" vars in apt/*.in files
if [[ "${OS}" == "debian" ]] ; then
    (
        # These are normally defined by the helm chart of stx tools
        for var in REPOMGR_DEPLOY_URL DEBIAN_SNAPSHOT DEBIAN_SECURITY_SNAPSHOT DEBIAN_DISTRIBUTION ; do
            if [[ -z "${!var}" ]] ; then
                echo "$var must be defined in the environment!" >&2
                exit 1
            fi
        done

        # Make sure pyhon3 exists
        $PYTHON3 --version >/dev/null || exit 1

        # Extract host name from repomgr URL
        REPOMGR_HOST=$(
            $PYTHON3 -c '
import sys
from urllib.parse import urlparse
print (urlparse (sys.argv[1]).hostname)
' "$REPOMGR_DEPLOY_URL"
        )
        if [[ $? -ne 0 || -z "$REPOMGR_HOST" ]] ; then
            echo "failed to parse REPOMGR_DEPLOY_URL !" >&2
            exit 1
        fi

        # replace @...@ vars in apt/*.in files
        count=0
        for src in "${DOCKER_BUILD_PATH}/${OS}/apt"/*.in ; do
            dst="${src%.in}"
            sed -e "s#@REPOMGR_DEPLOY_URL@#$REPOMGR_DEPLOY_URL#g" \
                -e "s#@REPOMGR_HOST@#$REPOMGR_HOST#g" \
                -e "s#@DEBIAN_SNAPSHOT@#$DEBIAN_SNAPSHOT#g" \
                -e "s#@DEBIAN_SECURITY_SNAPSHOT@#$DEBIAN_SECURITY_SNAPSHOT#g" \
                -e "s#@DEBIAN_DISTRIBUTION@#$DEBIAN_DISTRIBUTION#g" \
                "$src" >"$dst" || exit 1
            let ++count
        done
        if [[ $count -eq 0 ]] ; then
            echo "No *.in files found in ${DOCKER_BUILD_PATH}/${OS}/apt !" >&2
            exit 1
        fi
    ) || exit 1
fi

# Check to see if the OS image is already pulled
docker images --format '{{.Repository}}:{{.Tag}}' ${OS}:${OS_VERSION} | grep -q "^${OS}:${OS_VERSION}$"
BASE_IMAGE_PRESENT=$?

# Create the builder image
declare -a BUILD_ARGS
BUILD_ARGS+=(--build-arg RELEASE=${OS_VERSION})
BUILD_ARGS+=(--build-arg BUILD_STREAM=${BUILD_STREAM})
if [ ! -z "$HTTP_PROXY" ]; then
    BUILD_ARGS+=(--build-arg http_proxy=$HTTP_PROXY)
fi

if [ ! -z "$HTTPS_PROXY" ]; then
    BUILD_ARGS+=(--build-arg https_proxy=$HTTPS_PROXY)
fi

if [ ! -z "$NO_PROXY" ]; then
    BUILD_ARGS+=(--build-arg no_proxy=$NO_PROXY)
fi

if [[ "$USE_DOCKER_CACHE" != "yes" ]] ; then
    BUILD_ARGS+=("--no-cache")
fi

BUILD_ARGS+=(-t ${BUILD_IMAGE_NAME})
BUILD_ARGS+=(-f ${DOCKER_BUILD_PATH}/${OS}/Dockerfile ${DOCKER_BUILD_PATH})

# Build image
with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} docker build "${BUILD_ARGS[@]}"
if [ $? -ne 0 ]; then
    echo "Failed to create build image in docker" >&2
    exit 1
fi

# Run the image, executing the build-wheel.sh script
declare -a RUN_ARGS
if [ "${KEEP_CONTAINER}" = "no" ]; then
    RUN_ARGS+=(--rm)
fi
if [ ! -z "$HTTP_PROXY" ]; then
    RUN_ARGS+=(--env http_proxy=$HTTP_PROXY)
fi
if [ ! -z "$HTTPS_PROXY" ]; then
    RUN_ARGS+=(--env https_proxy=$HTTPS_PROXY)
fi
if [ ! -z "$NO_PROXY" ]; then
    RUN_ARGS+=(--env no_proxy=$NO_PROXY)
fi
RUN_ARGS+=(--env DISPLAY_RESULT=no)
RUN_ARGS+=(--env CPUCOUNT=2)

# Run container to build wheels
rm -f ${BUILD_OUTPUT_PATH}/failed.lst
rm -f ${BUILD_OUTPUT_PATH_PY2}/failed.lst

notice "building python3 wheels"
log_prefix "[python3] " \
    with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} \
        docker run ${RUN_ARGS[@]} -v ${BUILD_OUTPUT_PATH}:/wheels ${BUILD_IMAGE_NAME} /docker-build-wheel.sh
BUILD_STATUS=$?

notice "building python2 wheels"
log_prefix "[python2] " \
    with_retries -d ${RETRY_DELAY} ${MAX_ATTEMPTS} \
        docker run ${RUN_ARGS[@]} -v ${BUILD_OUTPUT_PATH_PY2}:/wheels --env PYTHON=python2 ${BUILD_IMAGE_NAME} /docker-build-wheel.sh
BUILD_STATUS_PY2=$?

if [ "${KEEP_IMAGE}" = "no" ]; then
    # Delete the builder image
    echo "Removing docker image ${BUILD_IMAGE_NAME}"
    docker image rm ${BUILD_IMAGE_NAME}
    if [ $? -ne 0 ]; then
        echo "Failed to delete build image from docker" >&2
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

# Check for failures
check_result() {
    local python="$1"
    local status="$2"
    local dir="$3"

    # There's a failed images list
    if [ -f "${dir}/failed.lst" ]; then
        let failures=$(cat "${dir}/failed.lst" | wc -l)

        cat <<EOF

############################################################
The following ${python} module(s) failed to build:
$(cat ${dir}/failed.lst)

Summary:
${failures} build failure(s).

EOF
        return 1
    fi

    # No failed images list, but build script failed nonetheless
    if [ "${status}" != 0 ] ; then
        cat <<EOF

############################################################
Build script failed for ${python}

EOF
        return 1
    fi

    # success
    cat <<EOF

############################################################
All ${python} wheels have been successfully built.

EOF
    return 0
}

if ! check_result "python3" "${BUILD_STATUS}" "${BUILD_OUTPUT_PATH}" || \
   ! check_result "python2" "${BUILD_STATUS_PY2}" "${BUILD_OUTPUT_PATH_PY3}" ; then
    exit 1
fi
exit 0

