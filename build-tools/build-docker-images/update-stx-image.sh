#!/bin/bash
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# Utility for incremental updates to an image
#

MY_SCRIPT_DIR=$(dirname $(readlink -f $0))

source ${MY_SCRIPT_DIR}/../build-wheels/utils.sh

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

PUSH=no
PROXY=""
DOCKER_USER=
DOCKER_REGISTRY=
FILE_BASEDIR=${PWD}
FROM=
CLEAN=no
DIST_PACKAGES=
CUSTOMIZATION_SCRIPT=
UPDATE_ID="unnamed-update"
declare -i IMAGE_UPDATE_VER=
declare -a WHEELS
declare -a DIST_PACKAGES
declare -a MODULE_SRC
declare -a EXTRA_FILES
declare -i MAX_ATTEMPTS=1


function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

This utility installs incremental updates to an existing image, allowing
the user to update or install python modules and software packages, or
to provide a customization script to make changes to the image.

Options to specify files or source can be used more than once, as needed,
or with wildcards if in quotes.

Options:
    --version:    Image update version
    --file:       Read update directives from a file
    --from:       Specify image to update
    --wheel:      Specify python wheel file
    --module-src: Specify path to module source to install/update (dir or git repo)
                  Formats: dir[|version]
                           url[|branch][|version]
    --pkg:        Specify path to distro package to install/update (ie. rpm)
    --customize:  Customization script
    --extra:      Extra file (to be accessible to customization script)
    --push:       Push to docker repo
    --proxy:      Set proxy <URL>:<PORT>
    --user:       Docker repo userid
    --registry:   Docker registry
    --clean:      Remove image(s) from local registry
    --attempts:   Max attempts, in case of failure (default: 1)
    --update-id:  Update ID


EOF
}

function copy_files_to_workdir {
    #
    # Utility function to copy files to the workdir
    #
    local destdir=$1
    shift

    if [ ${#@} -le 0 ]; then
        # No files in list, nothing to do
        return 0
    fi

    mkdir -p ${destdir}
    if [ $? -ne 0 ]; then
        echo "Failed to create dir: ${destdir}" >&2
        exit 1
    fi

    for f in $*; do
        if [[ ${f} =~ ^(http|https|git): ]]; then
            pushd ${destdir}
            with_retries ${MAX_ATTEMPTS} wget ${f}
            if [ $? -ne 0 ]; then
                echo "Failed to download $f to ${destdir}" >&2
                exit 1
            fi
        else
            cp -v ${f} ${destdir}/
            if [ $? -ne 0 ]; then
                echo "Failed to copy files to ${destdir}" >&2
                exit 1
            fi
        fi
    done
}

function hardcode_python_module_version {
    #
    # Update a python module's setup.py to hardcode the version,
    # allowing for pip to read the version without git installed
    # inside the container.
    #
    local module_dir=$1
    local module_ver=$2

    if [ ! -f ${module_dir}/setup.py ]; then
        # Nothing to do
        return 0
    fi

    pushd ${module_dir}
    grep -q 'pbr=True' ./setup.py
    if [ $? -eq 0 ]; then
        if [ -z "${module_ver}" ]; then
            # Get the calculated version
            module_ver=$(python ./setup.py --version)
        fi
        chmod u+w ./setup.py # just in case
        sed -i "s/pbr=True/version='${module_ver}'/" ./setup.py
    else
        # This function can be extended in the future to support
        # hardcoding/updating the version in modules that don't
        # use PBR, if required.
        echo "Module ($(basename ${module_dir})) does not have pbr=True." >&2
        echo "Skipping updating version in code." >&2
    fi
    popd
}

function update_image_record {
    # Update the image record file with a new/updated entry
    local LABEL=$1
    local TAG=$2
    local FILE=$3

    touch ${FILE}

    grep -q "/${LABEL}:" ${FILE}
    if [ $? -eq 0 ]; then
        # Update the existing record
        sed -i "s#.*/${LABEL}:.*#${TAG}#" ${FILE}
    else
        # Add a new record
        echo "${TAG}" >> ${FILE}
    fi
}

function read_params_from_file {
    local FILE=$1

    if [ ! -f "${FILE}" ]; then
        echo "Specified file does not exist: ${FILE}" >&2
        exit 1
    fi

    # Get parameters from file
    #
    # To avoid polluting the environment and impacting
    # other builds, we're going to explicitly grab specific
    # variables from the directives file. While this does
    # mean the file is sourced repeatedly, it ensures we
    # don't get junk.
    FROM=$(source ${FILE} && echo ${FROM})
    IMAGE_UPDATE_VER=$(source ${FILE} && echo ${IMAGE_UPDATE_VER})
    CUSTOMIZATION_SCRIPT=$(source ${FILE} && echo ${CUSTOMIZATION_SCRIPT})

    WHEELS=($(source ${FILE} && echo ${WHEELS}))
    DIST_PACKAGES=($(source ${FILE} && echo ${DIST_PACKAGES}))
    MODULE_SRC=($(source ${FILE} && echo ${MODULE_SRC}))
    EXTRA_FILES=($(source ${FILE} && echo ${EXTRA_FILES}))

    FILE_BASEDIR=$(dirname ${FILE})
}

OPTS=$(getopt -o h -l help,file:,from:,wheel:,module-src:,pkg:,customize:,extra:,push,proxy:,user:,registry:,clean,attempts:,update-id: -- "$@")
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
        --version)
            IMAGE_UPDATE_VER=$2
            shift 2
            ;;
        --file)
            read_params_from_file $2
            shift 2
            ;;
        --from)
            FROM=$2
            shift 2
            ;;
        --wheel)
            WHEELS+=($2)
            shift 2
            ;;
        --module-src)
            MODULE_SRC+=($2)
            shift 2
            ;;
        --pkg)
            DIST_PACKAGES+=($2)
            shift 2
            ;;
        --customize)
            CUSTOMIZATION_SCRIPT=$2
            shift 2
            ;;
        --extra)
            EXTRA_FILES+=($2)
            shift 2
            ;;
        --push)
            PUSH=yes
            shift
            ;;
        --proxy)
            PROXY=$2
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
        --attempts)
            MAX_ATTEMPTS=$2
            shift 2
            ;;
        --update-id)
            UPDATE_ID=$2
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

UPDATE_DIR=${MY_WORKSPACE}/std/update-images/${UPDATE_ID}


if [ -z "${FROM}" ]; then
    echo "Image must be specified with --from option." >&2
    exit 1
fi

LABEL=$(basename ${FROM} | sed 's/:.*//')

# Update the image version
CUR_IMAGE_UPDATE_VER=$(echo "${FROM}" | sed -r 's/.*\.([0-9][0-9]*)$/\1/')
if [ -z "${IMAGE_UPDATE_VER}" -o ${IMAGE_UPDATE_VER} = 0 ]; then
    # IMAGE_UPDATE_VER is not set, so increment the current version
    IMAGE_UPDATE_VER=$((${CUR_IMAGE_UPDATE_VER}+1))
fi

# Determine new tag for updated image
if [ -z "${CUR_IMAGE_UPDATE_VER}" ]; then
    # The original image doesn't have a .VER at the end of the tag,
    # so append the original tag with .IMAGE_UPDATE_VER
    UPDATED_IMAGE="${FROM}.${IMAGE_UPDATE_VER}"
else
    # Replace the .VER in the original image tag with .IMAGE_UPDATE_VER
    UPDATED_IMAGE=$(echo ${FROM} | sed "s/\.[0-9][0-9]*$/\.${IMAGE_UPDATE_VER}/")
fi

UPDATED_IMAGE_TAG=$(echo "${UPDATED_IMAGE}" | sed 's/.*://')

# If DOCKER_USER and DOCKER_REGISTRY are specified, modify the UPDATED_IMAGE accordingly
if [ -n "${DOCKER_REGISTRY}" -o -n "${DOCKER_USER}" ]; then
    UPDATED_IMAGE="${DOCKER_REGISTRY}${DOCKER_USER:-${USER}}/${LABEL}:${UPDATED_IMAGE_TAG}"
fi

# Prepare the workspace for internal-update-stx-image.sh.
# The workspace will contain all files needed to install updates,
# structured in pip-packages, dist-packages, and extras directories
# as appropriate.

WORKDIR=${UPDATE_DIR}/$(basename ${UPDATED_IMAGE} | tr ':' '_')
if [ -e ${WORKDIR} ]; then
    rm -rf ${WORKDIR}
fi

mkdir -p ${WORKDIR}
if [ $? -ne 0 ]; then
    echo "Failed to create workdir: ${WORKDIR}" >&2
    exit 1
fi

# Change dir in case relative file locations were used
pushd ${FILE_BASEDIR}

if [ -n "${CUSTOMIZATION_SCRIPT}" ]; then
    if [ ! -f "${CUSTOMIZATION_SCRIPT}" ]; then
        echo "Customization script not found: ${CUSTOMIZATION_SCRIPT}" >&2
        exit 1
    fi

    # Copy the customization script
    cp ${CUSTOMIZATION_SCRIPT} ${WORKDIR}/customize.sh
fi

copy_files_to_workdir ${WORKDIR}/extras ${EXTRA_FILES[@]}
copy_files_to_workdir ${WORKDIR}/pip-packages/wheels ${WHEELS[@]}
copy_files_to_workdir ${WORKDIR}/dist-packages ${DIST_PACKAGES[@]}

if [ ${#MODULE_SRC[@]} -gt 0 ]; then
    MODULES_DIR=${WORKDIR}/pip-packages/modules
    mkdir -p ${MODULES_DIR}
    if [ $? -ne 0 ]; then
        echo "Failed to create dir: ${MODULES_DIR}" >&2
        exit 1
    fi

    for module_src in ${MODULE_SRC[@]}; do
        src_location=$(echo "${module_src}" | awk -F'|' '{print $1}')

        if [ -d "${src_location}" ]; then
            # Module source is a directory, so copy it to the workspace
            cp --recursive --dereference ${src_location} ${MODULES_DIR}
            if [ $? -ne 0 ]; then
                echo "Failed to copy dir: ${src_location}" >&2
                exit 1
            fi

            module=$(basename ${src_location})
            module_ver=$(echo "${module_src}" | awk -F'|' '{print $2}')
            hardcode_python_module_version ${MODULES_DIR}/${module} ${module_ver}
        elif [[ ${src_location} =~ ^(http|https|git): ]]; then
            # Module source is a URL, so use git to clone it.
            # For a git repo, the module_src is specified as:
            #     src_location|module_ref|module_ver
            # where:
            #     src_location - the URL of the repo to be cloned
            #     module_ref - optional specification of branch or tag to be fetched
            #     module_ver - optional specification of version to hardcode

            pushd ${MODULES_DIR}

            git clone ${src_location}
            if [ $? -ne 0 ]; then
                echo "Failed to clone src: ${src_location}" >&2
                exit 1
            fi
            popd

            module=$(basename ${src_location} | sed 's/\.git$//')

            if [ ! -d "${MODULES_DIR}/${module}" ]; then
                echo "Module directory doesn't exist: ${MODULES_DIR}/${module}" >&2
                exit 1
            fi

            module_ref=$(echo "${module_src}" | awk -F'|' '{print $2}')
            if [ -n "${module_ref}" ]; then
                pushd ${MODULES_DIR}/${module}

                git fetch ${src_location} ${module_ref}
                if [ $? -ne 0 ]; then
                    echo "Failed to fetch repo branch: ${module} ${module_ref}" >&2
                    exit 1
                fi

                git checkout FETCH_HEAD
                if [ $? -ne 0 ]; then
                    echo "Failed to checkout FETCH_HEAD: ${module} ${module_ref}" >&2
                    exit 1
                fi
                popd
            fi

            module_ver=$(echo "${module_src}" | awk -F'|' '{print $3}')
            hardcode_python_module_version ${MODULES_DIR}/${module} ${module_ver}
        else
            echo "Invalid module source reference: ${src_location}" >&2
            exit 1
        fi
    done
fi

popd

# Finally, copy the internal-update-stx-image.sh script
cp ${MY_SCRIPT_DIR}/internal-update-stx-image.sh ${WORKDIR}/

# WORKDIR is setup, let's pull the image and update it

# Pull the image, even if already present, to ensure it's up to date
with_retries ${MAX_ATTEMPTS} docker image pull ${FROM}
if [ $? -ne 0 ]; then
    echo "Failed to pull image: ${FROM}" >&2
    exit 1
fi

# Get the OS NAME from /etc/os-release
OS_NAME=$(docker run --rm ${FROM} bash -c 'source /etc/os-release && echo ${NAME}')

# Run a container to install updates
UPDATE_CONTAINER=${USER}_${LABEL}_updater_$$
docker run --name ${UPDATE_CONTAINER} \
    -v "${WORKDIR}":/image-update \
    ${FROM} \
    bash -x -c ' bash -x /image-update/internal-update-stx-image.sh '
if [ $? -ne 0 ]; then
    echo "Failed to update image: ${FROM}" >&2
    exit 1
fi

# Commit the updated image
docker commit --change='CMD ["bash"]' ${UPDATE_CONTAINER} ${UPDATED_IMAGE}
if [ $? -ne 0 ]; then
    echo "Failed to commit updated image: ${UPDATE_CONTAINER}" >&2
    docker rm ${UPDATE_CONTAINER} >/dev/null
    exit 1
fi

# Remove the update container
docker rm ${UPDATE_CONTAINER} >/dev/null

if [ "${OS_NAME}" = "CentOS Linux" ]; then
    # Record python modules and packages
    docker run --rm ${UPDATED_IMAGE} bash -c 'rpm -qa' \
        | sort > ${UPDATE_DIR}/${LABEL}-${UPDATED_IMAGE_TAG}.rpmlst
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        echo "Failed to query RPMs from: ${UPDATED_IMAGE}" >&2
        exit 1
    fi

    docker run --rm ${UPDATED_IMAGE} bash -c 'pip freeze 2>/dev/null' \
        | sort > ${UPDATE_DIR}/${LABEL}-${UPDATED_IMAGE_TAG}.piplst
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        echo "Failed to query python modules from: ${UPDATED_IMAGE}" >&2
        exit 1
    fi
fi

IMAGE_RECORD_FILE=${UPDATE_DIR}/image-updates.lst
update_image_record ${LABEL} ${UPDATED_IMAGE} ${IMAGE_RECORD_FILE}

if [ "${PUSH}" = "yes" ]; then
    docker push ${UPDATED_IMAGE}
fi

if [ "${CLEAN}" = "yes" ]; then
    docker image rm ${FROM} ${UPDATED_IMAGE}
    if [ $? -ne 0 ]; then
        echo "Failed to clean images from docker: ${FROM} ${UPDATED_IMAGE}" >&2
    fi
fi

echo "Updated image: ${UPDATED_IMAGE}"

exit 0

