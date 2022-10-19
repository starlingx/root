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

BUILD_HELM_CHARTS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source $BUILD_HELM_CHARTS_DIR/utils.sh || exit 1

SUPPORTED_OS_ARGS=('centos' 'debian')
OS=
LABEL=""
APP_NAME="stx-openstack"
APP_VERSION_BASE="helm-charts-release-info.inc"
APP_VERSION_FILE=""
APP_VERSION=""
declare -a IMAGE_RECORDS
declare -a PATCH_DEPENDENCIES
declare -a APP_PACKAGES
declare -a CHART_PACKAGE_FILES
# PYTHON_2_OR_3: initialized below

VERBOSE=false
CPIO_FLAGS=
TAR_FLAGS=

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0) [--os <os>] [-a, --app <app-name>]
               [-A, --app-version-file /path/to/$APP_VERSION_BASE]
               [-B, --app-version <version>]
               [-r, --rpm <rpm-name>] [-i, --image-record <image-record>] [--label <label>]
               [-p, --patch-dependency <patch-dependency>] [ --verbose ]
Options:
    --os:
            Specify base OS (eg. centos)

    -a, --app NAME:
            Specify the application name

    -A, --app-version-file FILENAME:
            Specify the file containing version information for the helm
            charts. By default we will search for a file named
            $APP_VERSION_BASE in all git repos.

    -B, --app-version VERSION:
            Specify application (tarball) version, this overrides any other
            version information.

    -r, --package PACKAGE_NAME,... :
            Top-level package(s) containing the helm chart(s), comma-separated.
            Default: ${APP_NAME}-helm

    --rpm PACKAGE_NAME,... :
            (Deprecated) same as --package

    -i, --image-record FILENAME :
            Specify the path to image record file(s) or url(s).
            Multiple files/urls can be specified with a comma-separated
            list, or with multiple --image-record arguments.
            Note: Files are in order of priority. Images may appear
            in multiple files, the last image reference has higher
            priority.

    -l, --label LABEL:
            Specify the label of the application tarball. The label
            will be appended to the version string in tarball name.

    -p, --patch-dependency DEPENDENCY,... :
            Specify the patch dependency of the application tarball,
            comma-separated
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


function get_image_record_file {
  local image_record=$1

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
}

# Read the image versions from the passed image
# record files and build them into armada manifest
function build_image_versions_to_armada_manifest {
    local manifest_file=$1

    for image_record in ${IMAGE_RECORDS[@]}; do
        get_image_record_file ${image_record}

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
        ${PYTHON_2_OR_3} $BUILD_HELM_CHARTS_DIR/helm_chart_modify.py ${manifest_file} ${manifest_file}.tmp ${image_record}
        if [ $? -ne 0 ]; then
            echo "Failed to update manifest file" >&2
            exit 1
        fi
        \mv -f ${manifest_file}.tmp ${manifest_file}
    done
}


# Read the image versions from the passed image
# record files and build them into fluxcd manifests
function build_image_versions_to_fluxcd_manifests {
    local manifest_folder=$1

    for image_record in ${IMAGE_RECORDS[@]}; do
        get_image_record_file ${image_record}

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
        find ${manifest_folder} -name "*.yaml" | while read manifest_file; do
          ${PYTHON_2_OR_3} $BUILD_HELM_CHARTS_DIR/helm_chart_modify.py ${manifest_file} ${manifest_file}.tmp ${image_record}
          if [ $? -ne 0 ]; then
              echo "Failed to update manifest file" >&2
              exit 1
          fi
          \mv -f ${manifest_file}.tmp ${manifest_file}
        done
    done
}


function build_application_tarball {

    if [ -n "$1" ] ; then
        build_application_tarball_armada $1
    else
        build_application_tarball_fluxcd
    fi
}

function build_application_tarball_armada {
    local manifest=$1
    manifest_file=$(basename ${manifest})
    manifest_name=${manifest_file%.yaml}
    deprecated_tarball_name="helm-charts-${manifest_name}"
    build_image_versions_to_armada_manifest ${manifest}

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
    if ! grep -q "^app_name:" metadata.yaml ; then
        echo "app_name: ${APP_NAME}" >> metadata.yaml
    fi
    echo "app_version: ${APP_VERSION}" >> metadata.yaml
    if [ -n "${PATCH_DEPENDENCIES}" ]; then
        echo "patch_dependencies:" >> metadata.yaml
        for patch in ${PATCH_DEPENDENCIES[@]}; do
            echo "  - ${patch}" >> metadata.yaml
        done
    fi
    # Add the tarball build date: For consistency with tooling that might use
    # this metadata, match the date format used for BUILD_DATE in
    # /etc/build.info
    echo "build_date: $(date '+%Y-%m-%d %H:%M:%S %z')" >> metadata.yaml

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
        ln -s ${tarball_name} ${BUILD_OUTPUT_PATH}/${deprecated_tarball_name}.tgz
        echo "    ${BUILD_OUTPUT_PATH}/${deprecated_tarball_name}.tgz"
        echo "Warning: The tarball ${deprecated_tarball_name}.tgz is a symbolic link for ${tarball_name}. It's deprecated and will be removed shortly."
    fi
}

function build_application_tarball_fluxcd {

    FLUXCD_MANIFEST_DIR='fluxcd-manifests'

    # Stage all the fluxcd manifests
    cp -R usr/lib/fluxcd staging/${FLUXCD_MANIFEST_DIR}
    if [ $? -ne 0 ]; then
        echo "Failed to copy the FluxCD manifests from ${BUILD_OUTPUT_PATH}/usr/lib/fluxcd to ${BUILD_OUTPUT_PATH}/staging/fluxcd_manifests" >&2
        exit 1
    fi

    cd staging
    build_image_versions_to_fluxcd_manifests ${FLUXCD_MANIFEST_DIR}

    # Add metadata file
    touch metadata.yaml
    if [ -n "${LABEL}" ]; then
        APP_VERSION=${APP_VERSION}-${LABEL}
    fi
    if ! grep -q "^app_name:" metadata.yaml ; then
        echo "app_name: ${APP_NAME}" >> metadata.yaml
    fi
    echo "app_version: ${APP_VERSION}" >> metadata.yaml
    if [ -n "${PATCH_DEPENDENCIES}" ]; then
        echo "patch_dependencies:" >> metadata.yaml
        for patch in ${PATCH_DEPENDENCIES[@]}; do
            echo "  - ${patch}" >> metadata.yaml
        done
    fi

    # Add the tarball build date: For consistency with tooling that might use
    # this metadata, match the date format used for BUILD_DATE in
    # /etc/build.info
    echo "build_date: $(date '+%Y-%m-%d %H:%M:%S %z')" >> metadata.yaml

    # Add an md5
    find . -type f ! -name '*.md5' -print0 | xargs -0 md5sum > checksum.md5

    cd ..
    tarball_name="${APP_NAME}-${APP_VERSION}.tgz"
    tar ${TAR_FLAGS} ${tarball_name} -C staging/ .
    if [ $? -ne 0 ]; then
        echo "Failed to create the tarball" >&2
        exit 1
    fi

    rm -fr staging/${FLUXCD_MANIFEST_DIR}
    rm staging/checksum.md5
    echo "    ${BUILD_OUTPUT_PATH}/${tarball_name}"
}

function parse_yaml {
    # Create a new yaml file based on sequentially merging a list of given yaml files
    local yaml_script="
import sys
import collections
import ruamel.yaml as yaml

yaml_files = sys.argv[2:]
yaml_output = sys.argv[1]

def merge_yaml(yaml_merged, yaml_new):
    for k in yaml_new.keys():
        if not isinstance(yaml_new[k], dict):
            yaml_merged[k] = yaml_new[k]
        elif k not in yaml_merged:
            yaml_merged[k] = yaml_new[k]
        else:
            merge_yaml(yaml_merged[k], yaml_new[k])

yaml_out = collections.OrderedDict()
for yaml_file in yaml_files:
    print('Merging yaml from file: %s' % yaml_file)
    for document in yaml.load_all(open(yaml_file), Loader=yaml.RoundTripLoader, preserve_quotes=True, version=(1, 1)):
        document_name = (document['schema'], document['metadata']['schema'], document['metadata']['name'])
        if document_name in yaml_out:
            merge_yaml(yaml_out[document_name], document)
        else:
            yaml_out[document_name] = document
print('Writing merged yaml file: %s' % yaml_output)
yaml.dump_all(yaml_out.values(), open(yaml_output, 'w'), Dumper=yaml.RoundTripDumper, default_flow_style=False)
    "
    $PYTHON_2_OR_3 -c "${yaml_script}" ${@} || exit 1
}

# Find a file named $APP_VERSION_BASE at top-level of each git repo
function find_app_version_file {
    echo "searching for $APP_VERSION_BASE" >&2
    local dir file version_file root_dir
    root_dir="$(cd "$MY_REPO"/.. && pwd)"
    for dir in $(cd "$root_dir" && repo forall -c 'echo $REPO_PATH') ; do
        file="$root_dir/$dir/$APP_VERSION_BASE"
        [[ -f "$file" ]] || continue
        if [[ -n "$version_file" ]] ; then
            echo "Multiple $APP_VERSION_BASE files found:" >&2
            echo "    $version_file" >&2
            echo "    $file" >&2
            return 1
        fi
        version_file="$file"
    done
    if [[ -z "$version_file" && -f "$MY_REPO/stx/utilities/utilities/build-info/$APP_VERSION_BASE" ]] ; then
        version_file="$MY_REPO/stx/utilities/utilities/build-info/$APP_VERSION_BASE"
    fi
    if [[ -n "$version_file" ]] ; then
        echo "$version_file"
    fi
    return 0
}

filter_existing_dirs() {
    local d
    for d in "$@" ; do
        if [[ -d "$d" ]] ; then
            echo "$d"
        fi
    done
}

#
# Usage:
#    find_package_files
#
# Print noarch package files that might contain helm charts
#
function find_package_files {
    local -a dirlist
    local dir
    if [[ "$OS" == "centos" ]] ; then
        local centos_repo="${MY_REPO}/centos-repo"
        if [[ ! -d "${centos_repo}" ]] ; then
            centos_repo="${MY_REPO}/cgcs-centos-repo"
            if [[ ! -d "${centos_repo}" ]] ; then
                echo "ERROR: directory ${MY_REPO}/centos-repo not found." >&2
                exit 1
            fi
        fi
        readarray -t dirlist < <(filter_existing_dirs \
            "${MY_WORKSPACE}/std/rpmbuild/RPMS" \
            "${centos_repo}/Binary/noarch")
        if [[ "${#dirlist[@]}" -gt 0 ]] ; then
            echo "looking for packages in ${dirlist[*]}" >&2
            find "${dirlist[@]}" -xtype f -name "*.tis.noarch.rpm"
        fi
    else
        # FIXME: can't search 3rd-party binary debs because they are not accessible
        # on the filesystem, but only as remote files in apt repos
        readarray -t dirlist < <(filter_existing_dirs "${MY_WORKSPACE}/std")
        if [[ "${#dirlist[@]}" -gt 0 ]] ; then
            echo "looking for packages in ${dirlist[*]}" >&2
            find "${dirlist[@]}" \
                -mindepth 2 \
                -maxdepth 2 \
                "(" \
                    "(" \
                           -path "${MY_WORKSPACE}/build-wheels" \
                        -o -path "${MY_WORKSPACE}/build-images" \
                        -o -path "${MY_WORKSPACE}/build-helm" \
                    ")" -prune \
                ")" \
                -o \
                "(" -xtype f -name "*.stx.*_all.deb" ")"
        fi
    fi
}

# Usage:
#     find_helm_chart_packages PACKAGE_NAMES...
#
# Find helm chart packages and print their "NAME FILENAME" one per line
#
function find_helm_chart_package_files {

    # hash: package files => package names
    local -A package_files
    # hash: package names => package files
    local -A package_names

    # load package files and names
    echo "searching for package files" >&2
    local package_file package_name
    local failed=0
    for package_file in $(find_package_files) ; do
        package_name="$(
            if [[ "$OS" == "centos" ]] ; then
                rpm_get_name "$package_file" || exit 1
            else
                deb_get_control "$package_file" | deb_get_field "Package"
                check_pipe_status
            fi
        )" || exit 1
        if [[ -n "${package_names[$package_name]}" && "${package_names[$package_name]}" != "$package_file" ]] ; then
            echo "ERROR: found multiple packages named ${package_name}:" >&2
            echo "         $package_file" >&2
            echo "         ${package_names[$package_name]}" >&2
            failed=1
            continue
        fi
        package_names["$package_name"]="$package_file"
        package_files["$package_file"]="$package_name"
    done
    [[ $failed -eq 0 ]] || exit 1

    echo "looking for chart packages" >&2

    # Make sure top-level chart packages requested by user exist
    local failed=0
    for package_name in "$@" ; do
        if [[ -z "${package_names[$package_name]}" ]] ; then
            echo "ERROR: required package ${package_name} not found" >&2
            failed=1
        fi
    done
    [[ $failed -eq 0 ]] || exit 1

    # all chart package files
    local -A chart_package_files
    local -a ordered_chart_package_files

    # Find immediate dependencies of each package as well
    failed=0
    for package_name in "$@" ; do
        package_file="${package_names[$package_name]}"

        # seen this file before, skip
        if [[ -n "${chart_package_files[$package_file]}" ]] ; then
            continue
        fi

        local -a dep_package_names=($(
            if [[ "$OS" == "centos" ]] ; then
                rpm -qRp "$package_file" | sed 's/rpmlib([a-zA-Z0-9]*)[[:space:]]\?[><=!]\{0,2\}[[:space:]]\?[0-9.-]*//g' | grep -E -v -e '/' -e '^\s*$'
                check_pipe_status || exit 1
            else
                deb_get_control "$package_file" | deb_get_simple_depends
                check_pipe_status || exit 1
            fi
        )) || exit 1

        # save top-level package
        chart_package_files["$package_file"]=1
        ordered_chart_package_files+=("$package_file")

        # make sure all dep_packages exist & save them as well
        local dep_package_name dep_package_file
        for dep_package_name in "${dep_package_names[@]}" ; do
            dep_package_file="${package_names[$dep_package_name]}"
            if [[ -z "$dep_package_file" ]] ; then
                echo "ERROR: package ${package_file} requires package ${dep_package_name}, which does not exist" >&2
                failed=1
                continue
            fi
            # save dep_package_file, unless we've seen it before
            if [[ -z "${chart_package_files[$dep_package_file]}" ]] ; then
                chart_package_files["$dep_package_file"]=1
                ordered_chart_package_files+=("$dep_package_file")
            fi
        done
    done
    [[ $failed -eq 0 ]] || exit 1

    # make sure there's at least one
    if [[ "${#chart_package_files[@]}" -eq 0 ]] ; then
        echo "ERROR: could not find any chart packages" >&2
        exit 1
    fi

    # print them
    echo "found chart packages:" >&2
    for package_file in "${ordered_chart_package_files[@]}" ; do
        echo "    $package_file" >&2
        echo "$package_file"
    done

}

#
# Usage:
#     extract_chart_from_package PACKAGE_FILE
#
function extract_chart_from_package {
    local package_file=$1
    echo "extracting charts from package $package_file" >&2
    case $OS in
        centos)
            rpm2cpio "$package_file" | cpio ${CPIO_FLAGS}
            if ! check_pipe_status ; then
                echo "Failed to extract content of helm package: ${package_file}" >&2
                exit 1
            fi
            ;;

        debian)
            deb_extract_content "$package_file" $([[ "$VERBOSE" == "true" ]] && echo --verbose || true)
            if ! check_pipe_status ; then
                echo "Failed to extract content of helm package: ${package_file}" >&2
                exit 1
            fi
            ;;

        *)
            echo "Unsupported OS ${OS}" >&2
            ;;
    esac
}

# Usage: extract_charts CHART_PACKAGE_FILE...
function extract_charts {
    local package_file
    for package_file in "$@" ; do
        extract_chart_from_package "$package_file"
    done
}

#
# Usage:
#   get_app_version CHART_PACKAGE_FILE...
#
# Print the app (tarball) version, based on command-line
# arguments, the version .inc file or the chart package files
#
function get_app_version {

    # version provided on command line: use it
    if [[ -n "$APP_VERSION" ]] ; then
        echo "APP_VERSION=$APP_VERSION" >&2
        echo "$APP_VERSION"
        return 0
    fi

    # find app version file
    local app_version_file="$APP_VERSION_FILE"
    if [[ -z "$app_version_file" ]] ; then
        app_version_file="$(find_app_version_file)" || exit 1
    fi
    if [[ -n "$app_version_file" ]] ; then
        echo "reading $app_version_file" >&2
        local app_version
        app_version="$(
            VERSION= RELEASE=
            source "$app_version_file" || exit 1
            if [[ -z "$VERSION" ]] ; then
                echo "$app_version_file: missing VERSION" >&2
                exit 1
            fi
            echo "${VERSION}-${RELEASE:-0}"
        )" || exit 1
        echo "APP_VERSION=$app_version" >&2
        echo "$app_version"
        return 0
    fi

    # this should never happen because we exit early if there are no chart
    # packages
    if [[ "$#" -eq 0 ]] ; then
        echo "ERROR: unable to determine APP_VERSION" >&2
        exit 1
    fi

    # app version file doesn't exist: use the version of
    # the 1st chart package
    echo "extracting version from $1" >&2
    local app_version
    app_version="$(
        if [[ "$OS" == "centos" ]] ; then
            rpm -q --qf '%{VERSION}-%{RELEASE}' -p "$1" | sed 's![.]tis!!g'
            check_pipe_status || exit 1
        else
            control="$(deb_get_control "$1")" || exit 1
            version="$(echo "$control" | deb_get_field "Version" | sed -r -e 's/^[^:]+:+//')"
            if [[ -z "$version" ]] ; then
                echo "ERROR: failed to determine the version of package $1" >&2
                exit 1
            fi
            echo "${version}"
        fi
    )" || exit 1
    echo "APP_VERSION=$app_version" >&2
    echo "$app_version"
}

# TODO(awang): remove the deprecated image-file option
OPTS=$(getopt -o h,a:,A:,B:,r:,i:,l:,p: -l help,os:,app:,app-version-file:,app-version:,rpm:,image-record:,image-file:,label:,patch-dependency:,verbose -- "$@")
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
        -A | --app-version-file)
            APP_VERSION_FILE="$2"
            shift 2
            ;;
        -B | --app-version)
            APP_VERSION="$2"
            shift 2
            ;;
        -r | --rpm | --package)
            if [[ "$1" == "--rpm" ]] ; then
                echo "WARNING: option $1 is deprecated, use --package instead" >&2
            fi
            APP_PACKAGES+=(${2//,/ })
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

# Validate OS
if [ -z "$OS" ] ; then
    OS="$(ID= && source /etc/os-release 2>/dev/null && echo $ID || true)"
    if [[ -z "$OS" ]] ; then
        echo "Unable to determine OS, please re-run with \`--os' option" >&2
        exit 1
    elif [[ "$OS" != "debian" ]] ; then
        OS="centos"
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

# Required env vars
if [ -z "${MY_WORKSPACE}" -o -z "${MY_REPO}" ]; then
    echo "Environment not setup for builds" >&2
    exit 1
fi

# find a python interpreter
function find_python_2_or_3 {
    local python python_found
    for python in ${PYTHON2:-python2} ${PYTHON:-python} ${PYTHON3:-python3} ; do
        if $python -c 'import ruamel.yaml' >/dev/null 2>&1 ; then
            python_found=true
            break
        fi
    done
    if [[ -z "$python_found" ]] ; then
        echo "ERROR: can't find python!" >&2
        exit 1
    fi
    echo "$python"
}
PYTHON_2_OR_3="$(find_python_2_or_3)" || exit 1

# include SRPM utils
if [[ "$OS" == "centos" ]] ; then
    source $BUILD_HELM_CHARTS_DIR/srpm-utils || exit 1
else
    source $BUILD_HELM_CHARTS_DIR/deb-utils.sh || exit 1
fi

# Commenting out this code that attempts to validate the APP_NAME.
# It makes too many assumptions about the location and naming of apps.
#
# # Validate application
# APP_REPO=${MY_REPO}/stx/stx-config/kubernetes/applications/
# if [ ! -d ${APP_REPO} ];then
#     echo "Unable to find the applications directory: ${APP_REPO}" >&2
#     exit 1
# fi
# AVAILABLE_APPS=($(ls ${APP_REPO}))
# if [ ${#AVAILABLE_APPS[@]} -eq 0 ]; then
#     echo "No application found" >&2
#     exit 1
# fi
# if ! is_in ${APP_NAME} ${AVAILABLE_APPS[@]}; then
#     echo "Invalid application: ${APP_NAME}" >&2
#     exit 1
# fi

# Cleanup the previous chart build workspace
BUILD_OUTPUT_PATH=${MY_WORKSPACE}/std/build-helm/stx
echo "BUILD_OUTPUT_PATH=$BUILD_OUTPUT_PATH" >&2
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

# Find chart packages
CHART_PACKAGE_FILES=($(
    [[ "${#APP_PACKAGES[@]}" -gt 0 ]] || APP_PACKAGES+=("${APP_NAME}-helm")
    find_helm_chart_package_files "${APP_PACKAGES[@]}"
)) || exit 1

# Initialize APP_VERSION
APP_VERSION="$(get_app_version "${CHART_PACKAGE_FILES[@]}")" || exit 1

# Extract chart files from packages
extract_charts "${CHART_PACKAGE_FILES[@]}" || exit 1

# Create a new tarball containing all the contents we extracted
# tgz files under helm are relocated to subdir charts.
# Files under armada are left at the top level
mkdir -p staging
if [ $? -ne 0 ]; then
    echo "Failed to create ${BUILD_OUTPUT_PATH}/staging" >&2
    exit 1
fi

if [ ! -d "usr/lib/fluxcd" ] || [ ! -d "usr/lib/helm" ]; then
    # Armada Check: Remove with last supported Armada application
    if [ ! -d "usr/lib/armada" ] || [ ! -d "usr/lib/helm" ]; then
        echo "Failed to create the tarball. Mandatory files are missing." >&2
        exit 1
    fi
fi

# Stage all the charts
cp -R usr/lib/helm staging/charts
if [ $? -ne 0 ]; then
    echo "Failed to copy the charts from ${BUILD_OUTPUT_PATH}/usr/lib/helm to ${BUILD_OUTPUT_PATH}/staging/charts" >&2
    exit 1
fi

# Stage all the plugin wheels, if present
if [ -d "plugins" ]; then
    cp -R plugins staging/plugins
    if [ $? -ne 0 ]; then
        echo "Failed to copy the wheels from ${BUILD_OUTPUT_PATH}/wheels to ${BUILD_OUTPUT_PATH}/staging/plugins" >&2
        exit 1
    fi
fi

# Stage metadata file, if present
if [ -e usr/lib/application/metadata.yaml ]; then
    cp usr/lib/application/metadata.yaml staging/.
fi

if [ ! -d "usr/lib/fluxcd" ] ; then
    # Merge yaml files:
    APP_YAML=${APP_NAME}.yaml
    parse_yaml $APP_YAML `ls -rt usr/lib/armada/*.yaml`
    echo "Results:"
    # Build tarballs for merged yaml
    build_application_tarball $APP_YAML
else
    echo
    echo "WARNING: Merging yaml manifests is currently not supported for FluxCD applications" >&2
    echo
    echo "Results:"
    build_application_tarball
fi

exit 0

