#!/bin/bash
#
# Copyright (c) 2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility retrieves StarlingX helm-charts
# from the build output and re-packages them
# in a single openstack-helm.tgz tarball
#

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

SUPPORTED_OS_ARGS=('centos')
OS=centos
LABEL=""
APP_NAME="stx-openstack"
APP_VERSION=""
declare -a IMAGE_RECORDS
declare -a PATCH_DEPENDENCIES

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0) [ --os <os> ] [-a, --app <app-name>] [-i, --image-record <image-record>] [--label <label>] [-p, --patch-dependency <patch-dependency>] [ --verbose ]
Options:
    --os:
            Specify base OS (eg. centos)

    -a, --app:
            Specify the application name

    -i, --image-record:
            Specify the path to image record file(s) or url(s).
            Multiple files/urls can be specified with a comma-separated
            list, or with multiple --image-record arguments.
            Note: Files are in order of priority. Images may appear
            in multiple files, the last image reference has higher
            priority.

    -l, --label:
            Specify the label of the application tarball. The label
            is used to construct the name of tarball.

    -p, --patch-dependency:
            Specify the patch dependency of the application tarball.
            Multiple patches can be specified with a comma-separated
            list, or with multiple --patch-dependency arguments.

    --verbose:
            Verbose output

    --help:
            Give this help list
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

# Read the image versions from the passed image
# record files and build them into armada manifest
function build_image_versions_to_manifest {
    local manifest_file=$1

    for image_record in ${IMAGE_RECORDS[@]}; do

        if [[ ${image_record} =~ ^https?://.*(.lst|.txt)$ ]]; then
            wget --quiet --no-clobber ${image_record} \
                 --directory-prefix ${IMAGE_RECORD_PATH}

            if [ $? -ne 0 ]; then
                echo "Failed to download image record file from ${image_record}" >&2
                exit 1
            fi
        elif [[ -f ${image_record} && ${image_record} =~ .lst|.txt ]]; then
            cp ${image_record} ${IMAGE_RECORD_PATH}
            if [ $? -ne 0 ]; then
                echo "Failed to copy ${image_record} to ${IMAGE_RECORD_PATH}" >&2
                exit 1
            fi
        else
            echo "Cannot recognize the provided image record file:${image_record}" >&2
            exit 1
        fi

        # An image record file contains a list of images with the following format:
        # <docker-registry>/<repository>/<repository>/.../<image-name>:<tag>
        #
        # An example of the content of an image record file:
        # e.g. images-centos-dev-latest.lst
        # docker.io/starlingx/stx-aodh:master-centos-dev-latest
        # docker.io/starlingx/stx-ceilometer:master-centos-dev-latest
        # docker.io/starlingx/stx-cinder:master-centos-dev-latest
        # ...
        #
        # An example of the usage of an image reference in manifest file:
        # e.g. manifest.yaml
        # images:
        #   tags:
        #     aodh_api: docker.io/starlingx/stx-aodh:master-centos-stable-latest
        #     aodh_db_sync: docker.io/starlingx/stx-aodh:master-centos-stable-latest
        #     ...
        #
        # To replace the images in the manifest file with the images in image record file:
        # For each image reference in the image record file,
        # 1. extract image name
        #    e.g. image_name = stx-aodh
        #
        # 2. search the image reference in manifest yaml via image_name
        #    e.g. old_image_reference = docker.io/starlingx/stx-aodh:master-centos-stable-latest
        #
        # 3. update the manifest file to replace the old image references with the new one
        #    e.g. manifest.yaml
        #    images:
        #      tags:
        #        aodh_api: docker.io/starlingx/stx-aodh:master-centos-dev-latest
        #        aodh_db_sync: docker.io/starlingx/stx-aodh:master-centos-dev-latest
        #
        image_record=${IMAGE_RECORD_PATH}/$(basename ${image_record})
        for image_reference in $(cat ${image_record}); do
            image_name=$(echo ${image_reference} | sed -n 's/.*\/\(.*\):.*$/\1/p')
            old_image_reference="\([a-zA-Z0-9.]*\|[0-9.:]*\)\/.*${image_name}:.*"

            sed -i "s#${old_image_reference}#${image_reference}#" ${manifest_file}
            if [ $? -ne 0 ]; then
                echo "Failed to update manifest file" >&2
                exit 1
            fi
        done
    done
}

# Extract the helm charts from a rpm
function extract_chartfile {
    local helm_rpm=$1

    case $OS in
        centos)
            # Bash globbing does not handle [^-] like regex
            # so grep needed to be used
            rpm_file=$(ls ${RPMS_DIR} | grep "^${helm_rpm}${GREP_GLOB}")
            chartfile=${RPMS_DIR}/${rpm_file}
            if [ ! -f ${chartfile} ]; then
                echo "Failed to find helm package: ${helm_rpm}" >&2
                exit 1
            else
                rpm2cpio ${chartfile} | cpio ${CPIO_FLAGS}
                if [ ${PIPESTATUS[0]} -ne 0 -o ${PIPESTATUS[1]} -ne 0 ]; then
                    echo "Failed to extract content of helm package: ${chartfile}" >&2
                    exit 1
                fi
            fi

            ;;
        *)
            echo "Unsupported OS ${OS}" >&2
            ;;
    esac
}

# Extract the helm charts and information from the application rpm
function extract_application_rpm {
    extract_chartfile ${APP_RPM}

    APP_VERSION=$(rpm -qp --qf '%{VERSION}-%{RELEASE}' ${chartfile} | sed 's/\.tis//')
    if [ -z "${APP_VERSION}" ]; then
        echo "Failed to get the application version" >&2
        exit 1
    fi

    helm_files=$(rpm -qpR ${chartfile})
    if [ $? -ne 0 ]; then
        echo "Failed to get the helm rpm dependencies for ${APP_RPM}" >&2
        exit 1
    fi

    # Get rid of the rpmlib dependencies
    APP_HELM_FILES=($(echo ${helm_files} | sed 's/rpmlib([a-zA-Z0-9]*)[[:space:]]\?[><=!]\{0,2\}[[:space:]]\?[0-9.-]*//g'))
}

# TODO(awang): remove the deprecated image-file option
OPTS=$(getopt -o h,a:,i:,l:,p: -l help,os:,app:,image-record:,image-file:,label:,patch-dependency:,verbose -- "$@")
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
        -a | --app)
            APP_NAME=$2
            shift 2
            ;;
        -i | --image-record | --image-file)
            # Read comma-separated values into array
            IMAGE_RECORDS+=(${2//,/ })
            shift 2
            ;;
        -l | --label)
            LABEL=$2
            shift 2
            ;;
        -p | --patch-dependency)
            # Read comma-separated values into array
            PATCH_DEPENDENCIES+=(${2//,/ })
            shift 2
            ;;
        --verbose)
            VERBOSE=true
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

if [ "$VERBOSE" = true ] ; then
    CPIO_FLAGS=-vidu
    TAR_FLAGS=-zcvf
else
    CPIO_FLAGS="-idu --quiet"
    TAR_FLAGS=-zcf
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

# Validate application
APP_REPO=${MY_REPO}/stx/stx-config/kubernetes/applications/
if [ ! -d ${APP_REPO} ];then
    echo "Unable to find the applications directory: ${APP_REPO}" >&2
    exit 1
fi
AVALIABLE_APPS=($(ls ${APP_REPO}))
if [ ${#AVALIABLE_APPS[@]} -eq 0 ]; then
    echo "No application found" >&2
    exit 1
fi
if ! is_in ${APP_NAME} ${AVALIABLE_APPS[@]}; then
    echo "Invalid application: ${APP_NAME}" >&2
    exit 1
fi

# Cleanup the previous chart build workspace
BUILD_OUTPUT_PATH=${MY_WORKSPACE}/std/build-helm/stx
if [ -d ${BUILD_OUTPUT_PATH} ]; then
    # Wipe out the existing dir to ensure there are no stale files
    rm -rf ${BUILD_OUTPUT_PATH}
    if [ $? -ne 0 ]; then
        echo "Failed to cleanup the workspace ${BUILD_OUTPUT_PATH}" >&2
        exit 1
    fi
fi
mkdir -p ${BUILD_OUTPUT_PATH}
if [ $? -ne 0 ]; then
    echo "Failed to create the workspace ${BUILD_OUTPUT_PATH}" >&2
    exit 1
fi
cd ${BUILD_OUTPUT_PATH}

# Create a directory to store image record files
IMAGE_RECORD_PATH=${BUILD_OUTPUT_PATH}/image_record
if [ ${#IMAGE_RECORDS[@]} -ne 0 ]; then
    mkdir -p ${IMAGE_RECORD_PATH}
    if [ $? -ne 0 ]; then
        echo "Failed to create the ${IMAGE_RECORD_PATH}" >&2
        exit 1
    fi
fi

# Extract helm charts and app version from the application rpm
APP_RPM=${APP_NAME}-helm
RPMS_DIR=${MY_WORKSPACE}/std/rpmbuild/RPMS
GREP_GLOB="-[^-]*-[^-]*.tis.noarch.rpm"
extract_application_rpm
# Extract helm charts from the application dependent rpms
if [ ${#APP_HELM_FILES[@]} -gt 0 ]; then
    for helm_rpm in ${APP_HELM_FILES[@]}; do
        extract_chartfile ${helm_rpm}
    done
fi

# Create a new tarball containing all the contents we extracted
# tgz files under helm are relocated to subdir charts.
# Files under armada are left at the top level
mkdir -p staging
if [ $? -ne 0 ]; then
    echo "Failed to create ${BUILD_OUTPUT_PATH}/staging" >&2
    exit 1
fi

if [ ! -d "usr/lib/armada" ] || [ ! -d "usr/lib/helm" ]; then
    echo "Failed to create the tarball. Mandatory files are missing." >&2
    exit 1
fi

# Stage all the charts
cp -R usr/lib/helm staging/charts
if [ $? -ne 0 ]; then
    echo "Failed to copy the charts from ${BUILD_OUTPUT_PATH}/usr/lib/helm to ${BUILD_OUTPUT_PATH}/staging/charts" >&2
    exit 1
fi

# Build tarballs for each armada yaml file
echo "Results:"
for manifest in usr/lib/armada/*.yaml; do
    manifest_file=$(basename ${manifest})
    manifest_name=${manifest_file%.yaml}
    deprecated_tarball_name="helm-charts-${manifest_name}"
    build_image_versions_to_manifest ${manifest}

    cp ${manifest} staging/.
    if [ $? -ne 0 ]; then
        echo "Failed to copy the manifests to ${BUILD_OUTPUT_PATH}/staging" >&2
        exit 1
    fi

    cd staging
    # Add metadata file
    touch metadata.yaml
    if [ -n "${LABEL}" ]; then
        APP_VERSION=${APP_VERSION}-${LABEL}
        deprecated_tarball_name=${deprecated_tarball_name}-${LABEL}
    fi
    echo "app_name: ${APP_NAME}" >> metadata.yaml
    echo "app_version: ${APP_VERSION}" >> metadata.yaml
    if [ -n "${PATCH_DEPENDENCIES}" ]; then
        echo "patch_dependencies:" >> metadata.yaml
        for patch in ${PATCH_DEPENDENCIES[@]}; do
            echo "  - ${patch}" >> metadata.yaml
        done
    fi

    # Add an md5
    find . -type f ! -name '*.md5' -print0 | xargs -0 md5sum > checksum.md5

    cd ..
    tarball_name="${APP_NAME}-${APP_VERSION}.tgz"
    tar ${TAR_FLAGS} ${tarball_name} -C staging/ .
    if [ $? -ne 0 ]; then
        echo "Failed to create the tarball" >&2
        exit 1
    fi

    rm staging/${manifest_file}
    rm staging/checksum.md5
    echo "    ${BUILD_OUTPUT_PATH}/${tarball_name}"

    # Create a symbolic link to point to the generated tarball
    # TODO: Remove the symboblic link once the community has an
    # opportunity to adapt to the changes in filenames
    if [ "${APP_NAME}" = "stx-openstack" ]; then
        ln -s ${BUILD_OUTPUT_PATH}/${tarball_name} ${BUILD_OUTPUT_PATH}/${deprecated_tarball_name}.tgz
        echo "    ${BUILD_OUTPUT_PATH}/${deprecated_tarball_name}.tgz"
        echo "Warning: The tarball ${deprecated_tarball_name}.tgz is a symbolic link for ${tarball_name}. It's deprecated and will be removed shortly."
    fi

done

exit 0

