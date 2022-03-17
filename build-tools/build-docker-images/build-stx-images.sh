#!/bin/bash
#
# Copyright (c) 2018-2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility builds the StarlingX container images
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../build-wheels/utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

source ${MY_REPO}/build-tools/git-utils.sh

SUPPORTED_OS_ARGS=('centos' 'debian' 'distroless')
OS=
BUILD_STREAM=stable
IMAGE_VERSION=$(date --utc '+%Y.%m.%d.%H.%M') # Default version, using timestamp
PREFIX=dev
LATEST_PREFIX=""
PUSH=no
HTTP_PROXY=""
HTTPS_PROXY=""
NO_PROXY=""
DOCKER_USER=${USER}
DOCKER_REGISTRY=
PULL_BASE=yes
BASE=
WHEELS=
WHEELS_PY2=
CLEAN=no
TAG_LATEST=no
TAG_LIST_FILE=
TAG_LIST_LATEST_FILE=
DEFAULT_SPICE_REPO=https://github.com/freedesktop/spice-html5.git
declare -a ONLY
declare -a SKIP
declare -i MAX_ATTEMPTS=1

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

Options:
    --os:            Specify base OS (valid options: ${SUPPORTED_OS_ARGS[@]})
    --version:       Specify version for output image
    --stream:        Build stream, stable or dev (default: stable)
    --base:          Specify base docker image (required option)
 -N,--no-pull-base:  Don't pull base image before building; this will use
                     your local base image if one exists, without overwriting
                     it by "docker pull"
    --wheels:        Specify path to wheels tarball or image, URL or docker tag
                     (required when building loci projects)
    --wheels-py2:    Use this wheels tarball for Python2 projects
                     (default: work out from --wheels)
    --wheels-alternate: same as --wheels-py2
    --push:          Push to docker repo
    --http_proxy:    Set proxy <URL>:<PORT>, urls splitted with ","
    --https_proxy:   Set proxy <URL>:<PORT>, urls splitted with ","
    --no_proxy:      Set proxy <URL>, urls splitted with ","
    --user:          Docker repo userid
    --registry:      Docker registry
    --prefix:        Prefix on the image tag (default: dev)
    --latest:        Add a 'latest' tag when pushing
    --latest-prefix: Alternative prefix on the latest image tag
    --clean:         Remove image(s) from local registry
    --only <image> : Only build the specified image(s). Multiple images
                     can be specified with a comma-separated list, or with
                     multiple --only arguments.
    --skip <image> : Skip building the specified image(s). Multiple images
                     can be specified with a comma-separated list, or with
                     multiple --skip arguments.
    --attempts:      Max attempts, in case of failure (default: 1)


EOF
}

function is_in {
    local search=$1
    shift

    for v in $*; do
        if [ "${search}" = "${v}" ]; then
            return 0
        fi
    done
    return 1
}

function is_empty {
    test $# -eq 0
}

function url_basename {
    # http://foo/bar.tar?xxx#yyy => bar.tar
    echo "$1" | sed -r -e 's/[?#].*//' -e 's#.*/##'
}

# Usage: download $URL $OUTPUT_FILE
function download_file {
    local url="$1"
    local out_file="$2"
    if echo "$url" | grep -E -q -e '^https?:' -e '^ftp:' ; then
        \rm -f "$out_file.tmp" || return 1
        with_retries 5 wget -O "$out_file.tmp" "$url" || return 1
        \mv -f "$out_file.tmp" "$out_file" || exit 1
    else
        local src_file
        src_file="$(echo "$url" | sed -e 's#^file:/+##')"
        \cp -a "$src_file" "$out_file" || return 1
    fi
    return 0
}

#
# get_git: Clones a git into a subdirectory of ${WORKDIR}, and
#          leaves you in that directory.  On error the directory
#          is undefined.
#
function get_git {
    local git_repo=${1}
    local git_ref=${2}
    local git_patches=${@:3} # Take remaining args as patch list

    local git_name
    git_name=$(basename ${git_repo} | sed 's/[.]git$//')

    if [ -z ${git_name} ] || \
       [ "${git_name}" == "." ] || \
       [ "${git_name}" == ".." ] || \
       [ "${git_name}" == "*" ]; then
        echo "git repo appears to be invalid: ${git_repo}. Aborting..." >&2
        return 1
    fi

    if [ ! -d ${WORKDIR}/${git_name} ]; then
        cd ${WORKDIR}

        git clone --recursive ${git_repo}
        if [ $? -ne 0 ]; then
            echo "Failed to clone ${git_repo}. Aborting..." >&2
            return 1
        fi

        cd $git_name
        git checkout ${git_ref}
        if [ $? -ne 0 ]; then
            echo "Failed to checkout '${git_name}' base ref: ${git_ref}" >&2
            echo "Aborting..." >&2
            return 1
        fi

        # Apply any patches
        for p in ${git_patches}; do
            git am ${p}
            if [ $? -ne 0 ]; then
                echo "Failed to apply ${p} in ${git_name}" >&2
                echo "Aborting..." >&2
                return 1
            fi
        done
    else
        cd ${WORKDIR}/${git_name}

        git fetch
        if [ $? -ne 0 ]; then
            echo "Failed to fetch '${git_name}'. Aborting..." >&2
            return 1
        fi

        git checkout ${git_ref}
        if [ $? -ne 0 ]; then
            echo "Failed to checkout '${git_name}' base ref: ${git_ref}" >&2
            echo "Aborting..." >&2
            return 1
        fi

        # Apply any patches
        for p in ${git_patches}; do
            git am ${p}
            if [ $? -ne 0 ]; then
                echo "Failed to apply ${p} in ${git_name}" >&2
                echo "Aborting..." >&2
                return 1
            fi
        done
    fi

    return 0
}

function get_loci {
    # Use a specific HEAD of loci, to provide a stable builder
    local LOCI_REF="efccd0a853879ac6af6066eda09792d0d3afe9c0"
    local LOCI_REPO="https://github.com/openstack/loci.git"

    local ORIGWD=${PWD}

    get_git ${LOCI_REPO} ${LOCI_REF}
    if [ $? -ne 0 ]; then
        echo "Failed to clone or update loci. Aborting..." >&2
        cd ${ORIGWD}
        return 1
    fi

    cd ${ORIGWD}

    return 0
}

function patch_loci {
    # Loci contains a Docker file, which runs a script that installs
    # Python modules etc based on build parameters. We replace the call
    # to Loci's install script with our own, so that we can perform
    # additional actions within the Dockerfile before Loci does its thing.
    #
    # loci/                <-- clone of Loci git repo
    #   Dockerfile
    #   ...
    #   Dockerfile.stx     <-- create this by replacing "RUN..." in Dockerfile
    #                          with the contents of loci/docker/Dockerfile.part
    #   stx-scripts/       <-- copy of build-docker-images/loci/docker/stx-scripts
    #   stx-wheels/        <-- copy of wheels tarball(s) specified on cmd line
    #

    echo "Patching ${WORKDIR}/loci/Dockerfile" >&2

    # Make a copy of Dockerfile
    \cp -f "${WORKDIR}/loci/Dockerfile" "${WORKDIR}/loci/Dockerfile.stx" || exit 1

    # Replace "RUN .../install.sh" with our own commands
    sed -i -r  "${WORKDIR}/loci/Dockerfile.stx" -e "
        \\%^\\s*(RUN|run)\\s+/opt/loci/scripts/install.sh\\s*\$% {
            s/^(.*)$/#\\1/
            r ${MY_SCRIPT_DIR}/loci/docker/Dockerfile.part
        }
    " || exit 1
    if diff -q "${WORKDIR}/loci/Dockerfile" "${WORKDIR}/loci/Dockerfile.stx" >/dev/null ; then
        echo "${WORKDIR}/loci/Dockerfile: failed to patch Loci Dockerfile" >&2
        exit 1
    fi

    # Copy stx-scripts to Loci dir
    \rm -rf "${WORKDIR}/loci/stx-scripts" || exit 1
    \cp -ar "${MY_SCRIPT_DIR}/loci/docker/stx-scripts" "${WORKDIR}/loci/" || exit 1

    #diff -u "${WORKDIR}/loci/Dockerfile" "${WORKDIR}/loci/Dockerfile.stx" >&2

    # clear wheels dir
    \rm -rf "${WORKDIR}/loci/stx-wheels" || exit 1
    mkdir -p "${WORKDIR}/loci/stx-wheels" || exit 1

}

function download_loci_wheels {
    local url="$1"
    out_file="${WORKDIR}/loci/stx-wheels/$(url_basename "$url")"
    if [[ ! -f "$out_file" ]] ; then
        echo "Downloading $url => $out_file" >&2
        download_file "$url" "$out_file" || return 1
    fi
    return 0
}

function update_image_record {
    # Update the image record file with a new/updated entry
    local LABEL=$1
    local TAG=$2
    local FILE=$3

    grep -q "/${LABEL}:" ${FILE}
    if [ $? -eq 0 ]; then
        # Update the existing record
        sed -i "s#.*/${LABEL}:.*#${TAG}#" ${FILE}
    else
        # Add a new record
        echo "${TAG}" >> ${FILE}
    fi
}

function post_build {
    #
    # Common utility function called from image build functions to run post-build steps.
    #
    local image_build_file=$1
    local LABEL=$2
    local build_image_name=$3

    # Get additional supported args
    #
    # To avoid polluting the environment and impacting
    # other builds, we're going to explicitly grab specific
    # variables from the directives file. While this does
    # mean the file is sourced repeatedly, it ensures we
    # don't get junk.
    local CUSTOMIZATION
    CUSTOMIZATION=$(source ${image_build_file} && echo ${CUSTOMIZATION})
    # Default IMAGE_UPDATE_VER to 0, if not set
    local -i IMAGE_UPDATE_VER
    IMAGE_UPDATE_VER=$(source ${image_build_file} && echo ${IMAGE_UPDATE_VER:-0})

    local IMAGE_TAG_VERSIONED="${IMAGE_TAG}.${IMAGE_UPDATE_VER}"


    if [ -n "${CUSTOMIZATION}" ]; then
        local -a PROXY_ARGS=
        if [ ! -z "$HTTP_PROXY" ]; then
            PROXY_ARGS+=(--env http_proxy=$HTTP_PROXY)
        fi

        if [ ! -z "$HTTPS_PROXY" ]; then
            PROXY_ARGS+=(--env https_proxy=$HTTPS_PROXY)
        fi

        if [ ! -z "$NO_PROXY" ]; then
            PROXY_ARGS+=(--env no_proxy=$NO_PROXY)
        fi

        docker run ${PROXY_ARGS[@]} --entrypoint /bin/bash --name ${USER}_update_img ${build_image_name} -c "${CUSTOMIZATION}"
        if [ $? -ne 0 ]; then
            echo "Failed to add customization for ${LABEL}... Aborting"
            RESULTS_FAILED+=(${LABEL})
            docker rm ${USER}_update_img
            return 1
        fi

        docker commit --change='CMD ["bash"]' ${USER}_update_img ${build_image_name}
        if [ $? -ne 0 ]; then
            echo "Failed to commit customization for ${LABEL}... Aborting"
            RESULTS_FAILED+=(${LABEL})
            docker rm ${USER}_update_img
            return 1
        fi

        docker rm ${USER}_update_img
    fi

    if [ "${OS}" = "centos" ]; then
        # Record python modules and packages
        docker run --entrypoint /bin/bash --rm ${build_image_name} -c 'rpm -qa | sort' \
            > ${WORKDIR}/${LABEL}-${OS}-${BUILD_STREAM}.rpmlst
        docker run --entrypoint /bin/bash --rm ${build_image_name} -c 'pip freeze 2>/dev/null | sort' \
            > ${WORKDIR}/${LABEL}-${OS}-${BUILD_STREAM}.piplst
    fi

    RESULTS_BUILT+=(${build_image_name})

    if [ "${PUSH}" = "yes" ]; then
        local push_tag="${DOCKER_REGISTRY}${DOCKER_USER}/${LABEL}:${IMAGE_TAG_VERSIONED}"
        docker tag ${build_image_name} ${push_tag}
        docker push ${push_tag}
        RESULTS_PUSHED+=(${push_tag})

        update_image_record ${LABEL} ${push_tag} ${TAG_LIST_FILE}

        if [ "$TAG_LATEST" = "yes" ]; then
            local latest_tag="${DOCKER_REGISTRY}${DOCKER_USER}/${LABEL}:${IMAGE_TAG_LATEST}"
            docker tag ${push_tag} ${latest_tag}
            docker push ${latest_tag}
            RESULTS_PUSHED+=(${latest_tag})

            update_image_record ${LABEL} ${latest_tag} ${TAG_LIST_LATEST_FILE}
        fi
    fi
}

function cleanup_loci_failure {
    # When loci fails, it leaves behind a stopped container and a none:none image.
    # This function looks for those stopped containers to clean up after a failure.
    local container
    local image
    local extra_fields

    docker ps --no-trunc -f status=exited | grep /opt/loci/scripts/install.sh \
    | while read container image extra_fields; do
        echo "Cleaning loci build container and image: ${container} ${image}"
        docker rm ${container}
        docker image rm ${image}
    done
}

function build_image_loci {
    local image_build_file=$1

    # Get the supported args
    #
    # To avoid polluting the environment and impacting
    # other builds, we're going to explicitly grab specific
    # variables from the directives file. While this does
    # mean the file is sourced repeatedly, it ensures we
    # don't get junk.
    local LABEL
    LABEL=$(source ${image_build_file} && echo ${LABEL})
    local PROJECT
    PROJECT=$(source ${image_build_file} && echo ${PROJECT})
    local PROJECT_REPO
    PROJECT_REPO=$(source ${image_build_file} && echo ${PROJECT_REPO})
    local PROJECT_REF
    PROJECT_REF=$(source ${image_build_file} && echo ${PROJECT_REF})
    local PIP_PACKAGES
    PIP_PACKAGES=$(source ${image_build_file} && echo ${PIP_PACKAGES})
    local DIST_PACKAGES
    DIST_PACKAGES=$(source ${image_build_file} && echo ${DIST_PACKAGES})
    local PROFILES
    PROFILES=$(source ${image_build_file} && echo ${PROFILES})
    local PYTHON3
    PYTHON3=$(source ${image_build_file} && echo ${PYTHON3})
    local MIRROR_LOCAL
    MIRROR_LOCAL=$(source ${image_build_file} && echo ${MIRROR_LOCAL})
    local SPICE_REPO
    SPICE_REPO=$(source ${image_build_file} && echo ${SPICE_REPO})
    local SPICE_REF
    SPICE_REF=$(source ${image_build_file} && echo ${SPICE_REF})
    local DIST_REPOS
    DIST_REPOS=$(source ${image_build_file} && echo ${DIST_REPOS})

    echo "Building ${LABEL}"

    local ORIGWD=${PWD}

    if [ "${MIRROR_LOCAL}" = "yes" ]; then
        # Setup a local mirror of PROJECT_REPO

        local BARE_CLONES=${WORKDIR}/bare_clones
        mkdir -p ${BARE_CLONES}
        if [ $? -ne 0 ]; then
            echo "Failed to create ${BARE_CLONES}" >&2
            RESULTS_FAILED+=(${LABEL})
            return 1
        fi

        local CLONE_DIR=${BARE_CLONES}/${PROJECT}.git

        # Remove prior clone dir, if it exists
        \rm -rf ${CLONE_DIR}

        echo "Creating bare clone of ${PROJECT_REPO} for ${LABEL} build..."
        if [ -n "${PROJECT_REF}" ]; then
            echo "PROJECT_REF specified is ${PROJECT_REF}..."
            git clone --bare ${PROJECT_REPO} ${CLONE_DIR} \
                && cd ${PROJECT_REPO} \
                && git push --force ${CLONE_DIR} HEAD:refs/heads/${PROJECT_REF} \
                && mv ${CLONE_DIR}/hooks/post-update.sample ${CLONE_DIR}/hooks/post-update \
                && chmod a+x ${CLONE_DIR}/hooks/post-update \
                && cd ${CLONE_DIR} \
                && git update-server-info \
                && cd ${ORIGWD}
        else
            git clone --bare ${PROJECT_REPO} ${CLONE_DIR} \
                && cd ${PROJECT_REPO} \
                && mv ${CLONE_DIR}/hooks/post-update.sample ${CLONE_DIR}/hooks/post-update \
                && chmod a+x ${CLONE_DIR}/hooks/post-update \
                && cd ${CLONE_DIR} \
                && git update-server-info \
                && cd ${ORIGWD}
        fi

        if [ $? -ne 0 ]; then
            echo "Failed to clone ${PROJECT_REPO}... Aborting ${LABEL} build"
            RESULTS_FAILED+=(${LABEL})
            cd ${ORIGWD}
            return 1
        fi

        PROJECT_REPO=http://${HOSTNAME}:8088/${CLONE_DIR}
    fi

    local -a BUILD_ARGS=
    BUILD_ARGS=(--build-arg PROJECT=${PROJECT})
    BUILD_ARGS+=(--build-arg PROJECT_REPO=${PROJECT_REPO})
    BUILD_ARGS+=(--build-arg FROM=${BASE})

    if [ "${PYTHON3}" == "no" ] ; then
        echo "Python2 service ${LABEL}"
        download_loci_wheels "$WHEELS_PY2" || exit 1
        #BUILD_ARGS+=(--build-arg WHEELS=${WHEELS_PY2})
        BUILD_ARGS+=(--build-arg WHEELS="/opt/loci/stx-wheels/$(url_basename "$WHEELS_PY2")")
    else
        echo "Python3 service ${LABEL}"
        download_loci_wheels "$WHEELS" || exit 1
        #BUILD_ARGS+=(--build-arg WHEELS=${WHEELS})
        BUILD_ARGS+=(--build-arg WHEELS="/opt/loci/stx-wheels/$(url_basename "$WHEELS")")
    fi

    if [ ! -z "$HTTP_PROXY" ]; then
        BUILD_ARGS+=(--build-arg http_proxy=$HTTP_PROXY)
    fi

    if [ ! -z "$HTTPS_PROXY" ]; then
        BUILD_ARGS+=(--build-arg https_proxy=$HTTPS_PROXY)
    fi

    if [ ! -z "$NO_PROXY" ]; then
        BUILD_ARGS+=(--build-arg no_proxy=$NO_PROXY)
    fi

    if [ -n "${PROJECT_REF}" ]; then
        BUILD_ARGS+=(--build-arg PROJECT_REF=${PROJECT_REF})
    fi

    if [ -n "${PIP_PACKAGES}" ]; then
        BUILD_ARGS+=(--build-arg PIP_PACKAGES="${PIP_PACKAGES}")
    fi

    if [ -n "${DIST_PACKAGES}" ]; then
        BUILD_ARGS+=(--build-arg DIST_PACKAGES="${DIST_PACKAGES}")
    fi

    if [ -n "${PROFILES}" ]; then
        BUILD_ARGS+=(--build-arg PROFILES="${PROFILES}")
    fi

    if [ -n "${PYTHON3}" ]; then
        BUILD_ARGS+=(--build-arg PYTHON3="${PYTHON3}")
    fi

    if [ -n "${SPICE_REPO}" ]; then
        BUILD_ARGS+=(--build-arg SPICE_REPO="${SPICE_REPO}")
    else
        BUILD_ARGS+=(--build-arg SPICE_REPO="${DEFAULT_SPICE_REPO}")
    fi

    if [ -n "${SPICE_REF}" ]; then
        BUILD_ARGS+=(--build-arg SPICE_REF="${SPICE_REF}")
    fi

    if [ -n "${DIST_REPOS}" ]; then
        BUILD_ARGS+=(--build-arg DIST_REPOS="${DIST_REPOS}")
    fi

    # Use patched docker file
    BUILD_ARGS+=(--file "${WORKDIR}/loci/Dockerfile.stx")

    local build_image_name="${USER}/${LABEL}:${IMAGE_TAG_BUILD}"

    with_retries ${MAX_ATTEMPTS} docker build ${WORKDIR}/loci --no-cache \
        "${BUILD_ARGS[@]}" \
        --tag ${build_image_name}  2>&1 | tee ${WORKDIR}/docker-${LABEL}-${OS}-${BUILD_STREAM}.log
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        echo "Failed to build ${LABEL}... Aborting"
        RESULTS_FAILED+=(${LABEL})
        cleanup_loci_failure
        return 1
    fi

    if [ ${OS} = "centos" ]; then
        # For images with apache, we need a workaround for paths
        echo "${PROFILES}" | grep -q apache
        if [ $? -eq 0 ]; then
            docker run --entrypoint /bin/bash --name ${USER}_update_img ${build_image_name} -c '\
                ln -s /var/log/httpd /var/log/apache2 && \
                ln -s /var/run/httpd /var/run/apache2 && \
                ln -s /etc/httpd /etc/apache2 && \
                ln -s /etc/httpd/conf.d /etc/apache2/conf-enabled && \
                ln -s /etc/httpd/conf.modules.d /etc/apache2/mods-available && \
                ln -s /usr/sbin/httpd /usr/sbin/apache2 && \
                ln -s /etc/httpd/conf.d /etc/apache2/sites-enabled \
            '
            if [ $? -ne 0 ]; then
                echo "Failed to add apache workaround for ${LABEL}... Aborting"
                RESULTS_FAILED+=(${LABEL})
                docker rm ${USER}_update_img
                return 1
            fi

            docker commit --change='CMD ["bash"]' ${USER}_update_img ${build_image_name}
            if [ $? -ne 0 ]; then
                echo "Failed to commit apache workaround for ${LABEL}... Aborting"
                RESULTS_FAILED+=(${LABEL})
                docker rm ${USER}_update_img
                return 1
            fi

            docker rm ${USER}_update_img
        fi
    fi

    post_build ${image_build_file} ${LABEL} ${build_image_name}
}

function build_image_docker {
    local image_build_file=$1

    # Get the supported args
    #
    local LABEL
    LABEL=$(source ${image_build_file} && echo ${LABEL})
    local DOCKER_CONTEXT
    DOCKER_CONTEXT=$(source ${image_build_file} && echo ${DOCKER_CONTEXT})
    local DOCKER_FILE
    DOCKER_FILE=$(source ${image_build_file} && echo ${DOCKER_FILE})
    local DOCKER_REPO
    DOCKER_REPO=$(source ${image_build_file} && echo ${DOCKER_REPO})
    local DOCKER_REF
    DOCKER_REF=$(source ${image_build_file} && echo ${DOCKER_REF:-master})

    # DOCKER_PATCHES is a list of patch files, relative to the local dir
    local DOCKER_PATCHES
    DOCKER_PATCHES=$(source ${image_build_file} && for p in ${DOCKER_PATCHES}; do echo $(dirname ${image_build_file})/${p}; done)

    echo "Building ${LABEL}"

    local real_docker_context
    local real_docker_file

    if [ -n "${DOCKER_REPO}" ]; then
        local ORIGWD=${PWD}

        echo "get_git '${DOCKER_REPO}' '${DOCKER_REF}' '${DOCKER_PATCHES}'"
        get_git "${DOCKER_REPO}" "${DOCKER_REF}" "${DOCKER_PATCHES}"
        if [ $? -ne 0 ]; then
            echo "Failed to clone or update ${DOCKER_REPO}. Aborting..." >&2
            cd ${ORIGWD}
            return 1
        fi

        real_docker_file="${PWD}/Dockerfile"
        if [ ! -f ${real_docker_file} ]; then
            real_docker_file=$(find ${PWD} -type f -name Dockerfile | head -n 1)
        fi
        real_docker_context=$(dirname ${real_docker_file})
        cd ${ORIGWD}
    else
        if [ -n "${DOCKER_CONTEXT}" ]; then
            real_docker_context=$(dirname ${image_build_file})/${DOCKER_CONTEXT}
        else
            real_docker_context=$(dirname ${image_build_file})/docker
        fi

        if [ -n "${DOCKER_FILE}" ]; then
            real_docker_file=$(dirname ${image_build_file})/${DOCKER_FILE}
        else
            real_docker_file=${real_docker_context}/Dockerfile
        fi
    fi

    # Check for a Dockerfile
    if [ ! -f ${real_docker_file} ]; then
        echo "${real_docker_file} not found" >&2
        RESULTS_FAILED+=(${LABEL})
        return 1
    fi

    # Possible design option: Make a copy of the real_docker_context dir in BUILDDIR

    local build_image_name="${USER}/${LABEL}:${IMAGE_TAG_BUILD}"

    local -a BASE_BUILD_ARGS
    BASE_BUILD_ARGS+=(${real_docker_context} --no-cache)
    BASE_BUILD_ARGS+=(--file ${real_docker_file})
    BASE_BUILD_ARGS+=(--build-arg "BASE=${BASE}")
    if [ ! -z "$HTTP_PROXY" ]; then
        BASE_BUILD_ARGS+=(--build-arg http_proxy=$HTTP_PROXY)
    fi

    if [ ! -z "$HTTPS_PROXY" ]; then
        BASE_BUILD_ARGS+=(--build-arg https_proxy=$HTTPS_PROXY)
    fi

    if [ ! -z "$NO_PROXY" ]; then
        BASE_BUILD_ARGS+=(--build-arg no_proxy=$NO_PROXY)
    fi

    BASE_BUILD_ARGS+=(--tag ${build_image_name})
    with_retries ${MAX_ATTEMPTS} docker build ${BASE_BUILD_ARGS[@]} 2>&1 | tee ${WORKDIR}/docker-${LABEL}-${OS}-${BUILD_STREAM}.log

    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        echo "Failed to build ${LABEL}... Aborting"
        RESULTS_FAILED+=(${LABEL})
        return 1
    fi

    post_build ${image_build_file} ${LABEL} ${build_image_name}
}

function build_image_script {
    local image_build_file=$1

    # Get the supported args
    #
    local LABEL
    LABEL=$(source ${image_build_file} && echo ${LABEL})
    local SOURCE_REPO
    SOURCE_REPO=$(source ${image_build_file} && echo ${SOURCE_REPO})
    local SOURCE_REF
    SOURCE_REF=$(source ${image_build_file} && echo ${SOURCE_REF:-master})
    local COMMAND
    COMMAND=$(source ${image_build_file} && echo ${COMMAND})
    local SCRIPT
    SCRIPT=$(source ${image_build_file} && echo ${SCRIPT})
    local ARGS
    ARGS=$(source ${image_build_file} && echo ${ARGS})

    # SOURCE_PATCHES is a list of patch files, relative to the local dir
    local SOURCE_PATCHES
    SOURCE_PATCHES=$(source ${image_build_file} && for p in ${SOURCE_PATCHES}; do echo $(dirname ${image_build_file})/${p}; done)

    # Validate the COMMAND option
    SUPPORTED_COMMAND_ARGS=('bash')
    local VALID_COMMAND=1
    for supported_command in ${SUPPORTED_COMMAND_ARGS[@]}; do
        if [ "$COMMAND" = "${supported_command}" ]; then
            VALID_COMMAND=0
            break
        fi
    done
    if [ ${VALID_COMMAND} -ne 0 ]; then
        echo "Unsupported build command specified: ${COMMAND}" >&2
        echo "Supported command options: ${SUPPORTED_COMMAND_ARGS[@]}" >&2
        RESULTS_FAILED+=(${LABEL})
        return 1
    fi

    # Validate the SCRIPT file existed
    if [ ! -f $(dirname ${image_build_file})/${SCRIPT} ]; then
        echo "${SCRIPT} not found" >&2
        RESULTS_FAILED+=(${LABEL})
        return 1
    fi

    echo "Building ${LABEL}"

    local ORIGWD=${PWD}

    echo "get_git '${SOURCE_REPO}' '${SOURCE_REF}' '${SOURCE_PATCHES}'"
    get_git "${SOURCE_REPO}" "${SOURCE_REF}" "${SOURCE_PATCHES}"
    if [ $? -ne 0 ]; then
        echo "Failed to clone or update ${SOURCE_REPO}. Aborting..." >&2
        cd ${ORIGWD}
        return 1
    fi

    cp $(dirname ${image_build_file})/${SCRIPT} ${SCRIPT}
    local build_image_name="${USER}/${LABEL}:${IMAGE_TAG_BUILD}"

    with_retries ${MAX_ATTEMPTS} ${COMMAND} ${SCRIPT} ${ARGS} ${build_image_name} $HTTP_PROXY $HTTPS_PROXY $NO_PROXY 2>&1 | tee ${WORKDIR}/docker-${LABEL}-${OS}-${BUILD_STREAM}.log

    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        echo "Failed to build ${LABEL}... Aborting"
        RESULTS_FAILED+=(${LABEL})
        return 1
    fi

    # check docker image

    cd ${ORIGWD}

    post_build ${image_build_file} ${LABEL} ${build_image_name}
}

function build_image {
    local image_build_file=$1

    # Get the builder
    local BUILDER
    BUILDER=$(source ${image_build_file} && echo ${BUILDER})

    case ${BUILDER} in
        loci)
            build_image_loci ${image_build_file}
            return $?
            ;;
        docker)
            build_image_docker ${image_build_file}
            return $?
            ;;
        script)
            build_image_script ${image_build_file}
            return $?
            ;;
        *)
            echo "Unsupported BUILDER in ${image_build_file}: ${BUILDER}" >&2
            return 1
            ;;
    esac
}

OPTS=$(getopt -o hN -l help,os:,version:,release:,stream:,push,http_proxy:,https_proxy:,no_proxy:,user:,registry:,base:,wheels:,wheels-alternate:,wheels-py2:,only:,skip:,prefix:,latest,latest-prefix:,clean,attempts:,no-pull-base -- "$@")
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
        --base)
            BASE=$2
            shift 2
            ;;
        --os)
            OS=$2
            shift 2
            ;;
        --wheels)
            WHEELS=$2
            shift 2
            ;;
        --wheels-alternate|--wheels-py2)
            WHEELS_PY2=$2
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
        --prefix)
            PREFIX=$2
            shift 2
            ;;
        --latest-prefix)
            LATEST_PREFIX=$2
            shift 2
            ;;
        --push)
            PUSH=yes
            shift
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
        --only)
            # Read comma-separated values into array
            ONLY+=(${2//,/ })
            shift 2
            ;;
        --skip)
            # Read comma-separated values into array
            SKIP+=(${2//,/ })
            shift 2
            ;;
        --latest)
            TAG_LATEST=yes
            shift
            ;;
        --attempts)
            MAX_ATTEMPTS=$2
            shift 2
            ;;
        -N|--no-pull-base)
            PULL_BASE=no
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

if [ -z "${BASE}" ]; then
    echo "Base image must be specified with --base option." >&2
    exit 1
fi

# Guess WHEELS_PY2 if missing
if [[ -z "$WHEELS_PY2" && -n "$WHEELS" ]]; then
    # http://foo/bar.tar?xxx#yyy => http://foo/bar-py2.tar?xxx#yyy
    WHEELS_PY2="$(echo "$WHEELS" | sed -r 's,^([^#?]*)(\.tar)(\.gz|\.bz2|\.xz)?([#?].*)?$,\1-py2\2\3\4,i')"
    if [[ "$WHEELS" == "$WHEELS_PY2" ]]; then
        echo "Unable to guess --wheels-py2, please specify it explicitly" >&2
        exit 1
    fi
fi

# Resolve local wheel file names to absolute paths
for var in WHEELS WHEELS_PY2 ; do
    # skip empty vars
    [[ -n "${!var}" ]] || continue
    # skip network urls
    echo "${!var}" | grep -E -q -e '^https?:' -e '^ftp:' && continue
    # remove file:/ prefix if any
    declare "$var=$(echo "${!var}" | sed -r 's#^file:/+##')"
    # resolve it to an absolute path
    declare "$var=$(readlink -f "${!var}")" || exit 1
done

# Find the directives files
IMAGE_BUILD_FILES=()
function find_image_build_files {
    local image_build_inc_file image_build_dir image_build_file
    local -A all_labels

    for image_build_inc_file in $(find ${GIT_LIST} -maxdepth 1 -name "${OS}_${BUILD_STREAM}_docker_images.inc"); do
        basedir=$(dirname ${image_build_inc_file})
        for image_build_dir in $(sed -e 's/#.*//' ${image_build_inc_file} | sort -u); do
            for image_build_file in ${basedir}/${image_build_dir}/${OS}/*.${BUILD_STREAM}_docker_image; do

                # reset & read image build directive vars
                local BUILDER=
                local PROJECT=
                local LABEL=
                local PYTHON3=
                PROJECT="$(source ${image_build_file} && echo ${PROJECT})"
                BUILDER="$(source ${image_build_file} && echo ${BUILDER})"
                LABEL="$(source ${image_build_file} && echo ${LABEL})"
                PYTHON3="$(source ${image_build_file} && echo ${PYTHON3})"

                # make sure labels are unique
                if [[ -n "${all_labels["$LABEL"]}" ]] ; then
                    echo "The following files define the same LABEL $LABEL" >&2
                    echo "  ${all_labels["$LABEL"]}" >&2
                    echo "  ${image_build_file}" >&2
                    exit 1
                fi
                all_labels["$LABEL"]="$image_build_file"

                # skip images we don't want to build
                if is_in ${PROJECT} ${SKIP[@]} || is_in ${LABEL} ${SKIP[@]}; then
                    continue
                fi
                if ! is_empty ${ONLY[@]} && ! is_in ${PROJECT} ${ONLY[@]} && ! is_in ${LABEL} ${ONLY[@]}; then
                    continue
                fi

                # loci builders require a wheels tarball
                if [[ "${BUILDER}" == "loci" ]] ; then
                    # python3 projects require $WHEELS
                    if [[ ( -z "${PYTHON3}" || "${PYTHON3}" != "no" ) && -z "${WHEELS}" ]] ; then
                        echo "You are building python3 services with loci, but you didn't specify --wheels!" >&2
                        exit 1
                    # python2 projects require WHEELS_PY2
                    elif [[ "${PYTHON3}" == "no" && -z "${WHEELS_PY2}" ]] ; then
                        echo "You are building python2 services with loci, but you didn't specify --wheels-py2!" >&2
                        exit 1
                    fi
                fi

                # Save image build file in the global list
                IMAGE_BUILD_FILES+=("$image_build_file")
            done
        done
    done
}
find_image_build_files

IMAGE_TAG="${OS}-${BUILD_STREAM}"
IMAGE_TAG_LATEST="${IMAGE_TAG}-latest"

if [ -n "${LATEST_PREFIX}" ]; then
    IMAGE_TAG_LATEST="${LATEST_PREFIX}-${IMAGE_TAG_LATEST}"
elif [ -n "${PREFIX}" ]; then
    IMAGE_TAG_LATEST="${PREFIX}-${IMAGE_TAG_LATEST}"
fi

if [ -n "${PREFIX}" ]; then
    IMAGE_TAG="${PREFIX}-${IMAGE_TAG}"
fi

IMAGE_TAG_BUILD="${IMAGE_TAG}-build"

if [ -n "${IMAGE_VERSION}" ]; then
    IMAGE_TAG="${IMAGE_TAG}-${IMAGE_VERSION}"
fi

WORKDIR=${MY_WORKSPACE}/std/build-images
mkdir -p ${WORKDIR}
if [ $? -ne 0 ]; then
    echo "Failed to create ${WORKDIR}" >&2
    exit 1
fi

TAG_LIST_FILE=${WORKDIR}/images-${OS}-${BUILD_STREAM}-versioned.lst
TAG_LIST_LATEST_FILE=${WORKDIR}/images-${OS}-${BUILD_STREAM}-latest.lst
if [ "${PUSH}" = "yes" ]; then
    if is_empty ${ONLY[@]} && is_empty ${SKIP[@]}; then
        # Reset image record files, since we're building everything
        echo -n > ${TAG_LIST_FILE}

        if [ "$TAG_LATEST" = "yes" ]; then
            echo -n > ${TAG_LIST_LATEST_FILE}
        fi
    fi
fi

# Check to see if the BASE image is already pulled
docker images --format '{{.Repository}}:{{.Tag}}' ${BASE} | grep -q "^${BASE}$"
BASE_IMAGE_PRESENT=$?

# Pull the image anyway, to ensure it's up to date
if [[ "$PULL_BASE" == "yes" ]] ; then
    docker pull ${BASE}
fi

# Download loci, if needed.
get_loci
if [ $? -ne 0 ]; then
    # Error is reported by the function already
    exit 1
fi

patch_loci

# Replace mod_wsgi dependency and add rh_python36_mod_wsgi in loci/bindep.txt for python3 package
# refer to patch https://review.opendev.org/#/c/718603/
sed -i 's/mod_wsgi                    \[platform\:rpm apache\]/mod_wsgi                    \[platform\:rpm apache \!python3\]/g'  ${WORKDIR}/loci/bindep.txt
if ! (grep -q rh-python36-mod_wsgi ${WORKDIR}/loci/bindep.txt); then
    echo 'rh-python36-mod_wsgi        [platform:rpm !platform:suse (apache python3)]' >>  ${WORKDIR}/loci/bindep.txt
fi

# Build everything
for image_build_file in "${IMAGE_BUILD_FILES[@]}" ; do
    # Failures are reported by the build functions
    build_image ${image_build_file}
done

if [ "${CLEAN}" = "yes" -a ${#RESULTS_BUILT[@]} -gt 0 ]; then
    # Delete the images
    echo "Deleting images"
    docker image rm ${RESULTS_BUILT[@]} ${RESULTS_PUSHED[@]}
    if [ $? -ne 0 ]; then
        # We don't want to fail the overall build for this, so just log it
        echo "Failed to clean up images" >&2
    fi

    if [ ${BASE_IMAGE_PRESENT} -ne 0 ]; then
        # The base image was not already present, so delete it
        echo "Removing docker image ${BASE}"
        docker image rm ${BASE}
        if [ $? -ne 0 ]; then
            echo "Failed to delete base image from docker" >&2
        fi
    fi
fi

RC=0
if [ ${#RESULTS_BUILT[@]} -gt 0 ]; then
    echo "#######################################"
    echo
    echo "The following images were built:"
    for i in ${RESULTS_BUILT[@]}; do
        echo $i
    done | sort

    if [ ${#RESULTS_PUSHED[@]} -gt 0 ]; then
        echo
        echo "The following tags were pushed:"
        for i in ${RESULTS_PUSHED[@]}; do
            echo $i
        done | sort
    fi
fi

if [ ${#RESULTS_FAILED[@]} -gt 0 ]; then
    echo
    echo "#######################################"
    echo
    echo "There were ${#RESULTS_FAILED[@]} failures:"
    for i in ${RESULTS_FAILED[@]}; do
        echo $i
    done | sort
    RC=1
fi

exit ${RC}

