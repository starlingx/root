#!/bin/bash
#
# Copyright (c) 2018-2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility builds the StarlingX wheel tarball
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi


# make this process nice
renice -n 10 -p $$
ionice -c 3 -p $$

SUPPORTED_OS_ARGS=( 'debian' )
SUPPORTED_OS_CODENAME_ARGS=('bullseye' 'trixie')
OS=
OS_CODENAME=
OS_VERSION=
BUILD_STREAM=stable
VERSION=$(date --utc '+%Y.%m.%d.%H.%M') # Default version, using timestamp
PUSH=no
HTTP_PROXY=""
HTTPS_PROXY=""
NO_PROXY=""
CLEAN=no
KEEP_IMAGE=no
DOCKER_USER=${USER}
declare -i MAX_ATTEMPTS=1
declare -i RETRY_DELAY=30
PYTHON2=no
USE_DOCKER_CACHE=no
EXTRA_WHEELS_DIR=

# Requirement/constraint URLs -- these will be read from openstack.cfg
STABLE_OPENSTACK_REQ_URL=
MASTER_OPENSTACK_REQ_URL=
STABLE_OPENSTACK_REQ_URL_PY2=
MASTER_OPENSTACK_REQ_URL_PY2=

# List of top-level services for images, which should not be listed in upper-constraints.txt
SKIP_CONSTRAINTS=(
    ceilometer
    cinder
    glance
    gnocchi
    heat
    horizon
    ironic
    keystone
    magnum
    murano
    neutron
    nova
    panko
)
SKIP_CONSTRAINTS_PY2=("${SKIP_CONSTRAINTS_PY[@]}")

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

Options:
    --os:          Specify base OS (valid options: ${SUPPORTED_OS_ARGS[@]})
    --os-codename: Specify base OS Codename (valid options: ${SUPPORTED_OS_CODENAME_ARGS[@]})
    --os-version:  Specify OS version
    --stream:      Build stream, stable or dev (default: stable)
    --push:        Push to docker repo
    --http_proxy:  Set http proxy <URL>:<PORT>, urls splitted by ","
    --https_proxy: Set https proxy <URL>:<PORT>, urls splitted by ","
    --no_proxy:    Set bypass list for proxy <URL>, urls splitted by ","
    --user:        Docker repo userid
    --version:     Version for pushed image (if used with --push)
    --attempts:    Max attempts, in case of failure (default: 1)
    --retry-delay: Sleep this many seconds between retries (default: 30)
    --python2:     Build a python2 tarball
    --extra-wheels-dir: Directory containing additional .whl files
    --keep-image:  Don't delete wheel builder image at the end

    --cache:       Allow docker to use filesystem cache when building
                     CAUTION: this option may ignore locally-generated
                              packages and is meant for debugging the build
                              scripts.
EOF
}

OPTS=$(getopt -o h -l help,os:,os-codename:,os-version:,push,clean,user:,release:,stream:,http_proxy:,https_proxy:,no_proxy:,version:,attempts:,retry-delay:,python2,extra-wheels-dir:,keep-image,cache -- "$@")
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
        --os-version)
            OS_VERSION=$2
            shift 2
            ;;
        --push)
            PUSH=yes
            shift
            ;;
        --clean)
            CLEAN=yes
            shift
            ;;
        --user)
            DOCKER_USER=$2
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
        --version)
            VERSION=$2
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
        --python2)
            PYTHON2=yes
            shift
            ;;
        --extra-wheels-dir)
            EXTRA_WHEELS_DIR="$2"
            shift 2
            ;;
        --keep-image)
            KEEP_IMAGE=yes
            shift
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
    OS_CODENAME="$(ID= && source /etc/os-release 2>/dev/null && echo $VERSION_CODENAME || true)"
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

# Read openstack URLs
OPENSTACK_CFG="${MY_SCRIPT_DIR}/${OS}-${OS_CODENAME}/openstack.cfg"
source "$OPENSTACK_CFG" || exit 1

# Set python version-specific variables
if [ "${PYTHON2}" = "yes" ]; then
    SKIP_CONSTRAINTS=("${SKIP_CONSTRAINTS_PY2[@]}")
    PY_SUFFIX="-py2"
fi

# Resolve $EXTRA_WHEELS_DIR
if [[ -n "$EXTRA_WHEELS_DIR" ]] ; then
    EXTRA_WHEELS_DIR="$(readlink -ev "$EXTRA_WHEELS_DIR")" || exit 1
fi

# Build the base wheels and retrieve the StarlingX wheels
declare -a BUILD_BASE_WL_ARGS
BUILD_BASE_WL_ARGS+=(--os ${OS} --os-codename ${OS_CODENAME} --stream ${BUILD_STREAM})
if [ -n "$OS_VERSION" ]; then
    BUILD_BASE_WL_ARGS+=(--os-version "${OS_VERSION}")
fi
if [ ! -z "$HTTP_PROXY" ]; then
    BUILD_BASE_WL_ARGS+=(--http_proxy ${HTTP_PROXY})
fi

if [ ! -z "$HTTPS_PROXY" ]; then
    BUILD_BASE_WL_ARGS+=(--https_proxy ${HTTPS_PROXY})
fi

if [ ! -z "$NO_PROXY" ]; then
    BUILD_BASE_WL_ARGS+=(--no_proxy ${NO_PROXY})
fi

if [ "$KEEP_IMAGE" = "yes" ]; then
    BUILD_BASE_WL_ARGS+=(--keep-image)
fi

if [[ "$USE_DOCKER_CACHE" == "yes" ]] ; then
    BUILD_BASE_WL_ARGS+=(--cache)
fi

${MY_SCRIPT_DIR}/build-base-wheels.sh ${BUILD_BASE_WL_ARGS[@]} --attempts "${MAX_ATTEMPTS}" --retry-delay "${RETRY_DELAY}"
if [ $? -ne 0 ]; then
    echo "Failure running build-base-wheels.sh" >&2
    exit 1
fi

${MY_SCRIPT_DIR}/get-stx-wheels.sh --os ${OS} --os-codename ${OS_CODENAME} --stream ${BUILD_STREAM}
if [ $? -ne 0 ]; then
    echo "Failure running get-stx-wheels.sh" >&2
    exit 1
fi

BUILD_OUTPUT_PATH=${MY_WORKSPACE}/std/build-wheels-${OS}-${OS_CODENAME}-${BUILD_STREAM}/tarball${PY_SUFFIX}
if [ -d ${BUILD_OUTPUT_PATH} ]; then
    # Wipe out the existing dir to ensure there are no stale files
    rm -rf ${BUILD_OUTPUT_PATH}
fi
mkdir -p ${BUILD_OUTPUT_PATH}
cd ${BUILD_OUTPUT_PATH}

IMAGE_NAME=stx-${OS}-${BUILD_STREAM}-wheels${PY_SUFFIX}

TARBALL_FNAME=${MY_WORKSPACE}/std/build-wheels-${OS}-${OS_CODENAME}-${BUILD_STREAM}/${IMAGE_NAME}.tar
if [ -f ${TARBALL_FNAME} ]; then
    rm -f ${TARBALL_FNAME}
fi

# Download the global-requirements.txt and upper-constraints.txt files
if [ "${PYTHON2}" = "yes" ]; then
    if [ "${BUILD_STREAM}" = "dev" -o "${BUILD_STREAM}" = "master" ]; then
        OPENSTACK_REQ_URL="${MASTER_OPENSTACK_REQ_URL_PY2}"
    else
        OPENSTACK_REQ_URL="${STABLE_OPENSTACK_REQ_URL_PY2}"
    fi
else
    if [ "${BUILD_STREAM}" = "dev" -o "${BUILD_STREAM}" = "master" ]; then
        OPENSTACK_REQ_URL="${MASTER_OPENSTACK_REQ_URL}"
    else
        OPENSTACK_REQ_URL="${STABLE_OPENSTACK_REQ_URL}"
    fi
fi

for url in "${OPENSTACK_REQ_URL}/global-requirements.txt" "${OPENSTACK_REQ_URL}/upper-constraints.txt" ; do
    if echo "$url" | grep -q -E '^(https?|ftp):' >/dev/null ; then
        with_retries ${MAX_ATTEMPTS} wget "$url"
        if [ $? -ne 0 ]; then
            echo "Failed to download $url" >&2
            exit 1
        fi
    else
        # Remove "file:" from url and treat what remains as a file name.
        # Local files should be relative to the location of openstack.cfg,
        # so leading slashes are menaingless, remove them as well.
        url="$(echo "$url" | sed -r 's,^(file:)?/*,,')"
        \cp "$(dirname "$OPENSTACK_CFG")"/"$url" ./ || exit 1
    fi
done

# Delete $SKIP_CONSTRAINTS from upper-constraints.txt, if any present
for name in ${SKIP_CONSTRAINTS[@]}; do
    grep -q "^${name}===" upper-constraints.txt
    if [ $? -eq 0 ]; then
        # Delete the module
        sed -i "/^${name}===/d" upper-constraints.txt
    fi
done

# Set nullglob so wildcards will return empty string if no match
shopt -s nullglob

# Copy the base and stx wheels, updating upper-constraints.txt as necessary
# FIXME: debian packages install *.whl files under /usr/share/..., rather than
#        /wheels. Do a deep search on that platform
if [ "${OS}" == "debian" ]; then
    stx_find_wheels_cmd=(find ../stx -name '*.whl')
else
    stx_find_wheels_cmd=(find ../stx/wheels -mindepth 1 -maxdepth 1 -name '*.whl')
fi
if [[ -d "$EXTRA_WHEELS_DIR" ]] ; then
    find_extra_wheels_cmd=(find "$EXTRA_WHEELS_DIR" -mindepth 1 -maxdepth 1 -name '*.whl')
else
    find_extra_wheels_cmd=()
fi
for wheel in ../base${PY_SUFFIX}/*.whl $("${stx_find_wheels_cmd[@]}") $("${find_extra_wheels_cmd[@]}") ; do
    if [[ ! -f "$wheel" ]] ; then
        echo "WARNING: $wheel doesn't exist or is not a regular file" >&2
        continue
    fi
    # Get the wheel name and version from the METADATA
    METADATA=$(unzip -p ${wheel} '*/METADATA')
    name=$(echo "${METADATA}" | grep '^Name:' | awk '{print $2}')
    version=$(echo "${METADATA}" | grep '^Version:' | awk '{print $2}')

    if [ -z "${name}" -o -z "${version}" ]; then
        echo "Failed to parse name or version from $(readlink -f ${wheel})" >&2
        exit 1
    fi

    echo "Adding ${name}-${version}..."

    cp ${wheel} .
    if [ $? -ne 0 ]; then
        echo "Failed to copy $(readlink -f ${wheel})" >&2
        exit 1
    fi

    # Update the upper-constraints file, if necessary
    skip_constraint=1
    for skip in ${SKIP_CONSTRAINTS[@]}; do
        if [ "${name}" = "${skip}" ]; then
            skip_constraint=0
            continue
        fi
    done

    if [ ${skip_constraint} -eq 0 ]; then
        continue
    fi

    grep -q "^${name}===${version}\(;.*\)*$" upper-constraints.txt
    if [ $? -eq 0 ]; then
        # This version already exists in the upper-constraints.txt
        continue
    fi

    grep -q "^${name}===" upper-constraints.txt
    if [ $? -eq 0 ]; then
        # Update the version
        sed -i "s/^${name}===.*/${name}===${version}/" upper-constraints.txt
    else
        # Add the module
        echo "${name}===${version}" >> upper-constraints.txt
    fi
done

shopt -u nullglob

echo "Creating $(basename ${TARBALL_FNAME})..."
tar cf ${TARBALL_FNAME} *
if [ $? -ne 0 ]; then
    echo "Failed to create the tarball" >&2
    exit 1
fi

echo "Done."

if [ "${PUSH}" = "yes" ]; then
    #
    # Push generated wheels tarball to docker registry
    #
    docker import ${TARBALL_FNAME} ${DOCKER_USER}/${IMAGE_NAME}:${VERSION}
    if [ $? -ne 0 ]; then
        echo "Failed command:" >&2
        echo "docker import ${TARBALL_FNAME} ${DOCKER_USER}/${IMAGE_NAME}:${VERSION}" >&2
        exit 1
    fi

    docker tag ${DOCKER_USER}/${IMAGE_NAME}:${VERSION} ${DOCKER_USER}/${IMAGE_NAME}:latest
    if [ $? -ne 0 ]; then
        echo "Failed command:" >&2
        echo "docker tag ${DOCKER_USER}/${IMAGE_NAME}:${VERSION} ${DOCKER_USER}/${IMAGE_NAME}:latest" >&2
        exit 1
    fi

    docker push ${DOCKER_USER}/${IMAGE_NAME}:${VERSION}
    if [ $? -ne 0 ]; then
        echo "Failed command:" >&2
        echo "docker push ${DOCKER_USER}/${IMAGE_NAME}:${VERSION}" >&2
        exit 1
    fi

    docker push ${DOCKER_USER}/${IMAGE_NAME}:latest
    if [ $? -ne 0 ]; then
        echo "Failed command:" >&2
        echo "docker import ${TARBALL_FNAME} ${DOCKER_USER}/${IMAGE_NAME}:${VERSION}" >&2
        exit 1
    fi

    if [ "${CLEAN}" = "yes" ]; then
        echo "Deleting docker images ${DOCKER_USER}/${IMAGE_NAME}:${VERSION} ${DOCKER_USER}/${IMAGE_NAME}:latest"
        docker image rm ${DOCKER_USER}/${IMAGE_NAME}:${VERSION} ${DOCKER_USER}/${IMAGE_NAME}:latest
        if [ $? -ne 0 ]; then
            echo "Failed command:" >&2
            echo "docker image rm ${DOCKER_USER}/${IMAGE_NAME}:${VERSION} ${DOCKER_USER}/${IMAGE_NAME}:latest" >&2
            exit 1
        fi
    fi
fi

exit 0

