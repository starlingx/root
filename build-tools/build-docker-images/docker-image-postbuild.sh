#!/bin/bash
#
# Copyright (c) 2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

usage() {
    cat <<_END
Usage: $0 OPTIONS... IMAGE [NEW_IMAGE]

Utility to remove packages from a docker container.


  --work-dir=WORK_DIR            storage location for intermediate files,
                                 this directory may be deleted, be careful
                                 Default: /tmp/docker-image-postbuild

  --tmp-image=TMP_IMAGE          intermediate image; this will be
                                 automatically removed
                                 Default: \${IMAGE}-tmp

  --tmp-container=TMP_CONTAINER  intermediate container; this will be
                                 automatically removed
                                 Default: <auto>

Save updated image to NEW_IMAGE, if specified; or else
update IMAGE

ACTIONS
=======
At least one of the following actions must be specified:

  --remove-python-packages="mod1 mod2..."
      Python/pip packages to remove; fail if any downstream dependancies remain

  --remove-os-packages="pkg1 pkg2..."
      OS packages to remove; fail if any downstream dependancies remain

  -c,--command="..."
      Execute an arbitrary shell command, eg: remove some files

_END
}

PROGNAME="$(basename "$0")"
HELPERS_DIR="$(dirname "$0")/docker-image-postbuild"
WORK_DIR=
ORIG_IMAGE=
TMP_IMAGE=
TMP_CONTAINER=
NEW_IMAGE=
OS_PACKAGES=
PYTHON_PACKAGES=
SHELL_COMMAND=

cmdline_error() {
    echo "$PROGNAME: $*" >&2
    echo "Type \`$0 --help' for more info." >&2
    exit 2
}

missing_arg() {
    cmdline_error "missing required option $1"
}

OPTS=$(getopt -o hc: -l help,work-dir:,tmp-image:,tmp-container:,remove-os-packages:,remove-python-packages:,command: -- "$@") || exit 1
eval set -- "${OPTS}"

while true; do
    case $1 in
        --)                 shift ; break ;;
        -h | --help)        usage ; exit 0 ;;
        --work-dir)         WORK_DIR="$2" ; shift 2 ;;
        --tmp-image)        TMP_IMAGE="$2" ; shift 2 ;;
        --tmp-container)    TMP_CONTAINER="$2" ; shift 2 ;;
        --remove-os-packages)     OS_PACKAGES="$2" ; shift 2 ;;
        --remove-python-packages) PYTHON_PACKAGES="$2" ; shift 2 ;;
        -c | --command)           SHELL_COMMAND="$2" ; shift 2 ;;
        -*)                 exit 1 ;;
        *)                  break ;;
    esac
done

[[ "$#" -gt 0 ]] || cmdline_error "not enough arguments"
[[ "$#" -le 2 ]] || cmdline_error "too many arguments"

ORIG_IMAGE="$1" ; shift
[[ -n "$ORIG_IMAGE" ]] || cmdline_error "invalid empty IMAGE"

if [[ "$#" -gt 0 ]] ; then
    NEW_IMAGE="$1" ; shift
    [[ -n "$NEW_IMAGE" ]] || cmdline_error "invalid empty NEW_IMAGE"
else
    NEW_IMAGE="$ORIG_IMAGE"
fi

[[ -n "$TMP_IMAGE" ]] || TMP_IMAGE="${ORIG_IMAGE}-tmp"

if [[ -z "$TMP_CONTAINER" ]] ; then
    TMP_CONTAINER="${ORIG_IMAGE%:*}"
    TMP_CONTAINER="${TMP_CONTAINER##*/}-tmp"
fi

[[ -n "$WORK_DIR" ]] || WORK_DIR="/tmp/docker-image-postbuild"

echo "=== Attempting to remove packages from docker image $ORIG_IMAGE"
echo "\
    WORK_DIR=[$WORK_DIR]
    ORIG_IMAGE=[$ORIG_IMAGE]
    TMP_IMAGE=[$TMP_IMAGE]
    TMP_CONTAINER=[$TMP_CONTAINER]
    NEW_IMAGE=[$NEW_IMAGE]
    PYTHON_PACKAGES=[$PYTHON_PACKAGES]
    OS_PACKAGES=[$OS_PACKAGES]
    SHELL_COMMAND=[$SHELL_COMMAND]
"

function remove_packages_from_docker_image {
    local work_dir="$1"
    local orig_image="$2"
    local tmp_image="$3"
    local tmp_container="$4"
    local new_image="$5"
    local python_packages="$6"
    local os_packages="$7"
    local shell_command="$8"

    # validate python module names
    local pymod
    python_packages=$(
        for pymod in $python_packages ; do
            echo "$pymod"
        done | sort -u
    )
    python_packages=$(echo $python_packages)
    for pymod in $python_packages ; do
        if echo "$pymod" | grep -q '[^a-zA-Z0-9_:.-]' ; then
            echo "ERROR: Invalid python package name \"$pymod\"" >&2
            return 1
        fi
    done

    # validate package names
    local pkg
    os_packages=$(
        for pkg in $os_packages ; do
            echo "$pkg"
        done | sort -u
    )
    os_packages=$(echo $os_packages)
    for pkg in $os_packages ; do
        if echo "$pkg" | grep -q '[^a-zA-Z0-9_:.-]' ; then
            echo "ERROR: Invalid package name \"$pkg\"" >&2
            return 1
        fi
    done

    # Exit early if there's nothing to do
    if [[ -z "$python_packages" && -z "$os_packages" && -z "$shell_command" ]] ; then
        echo "WARNING: no actions specified" >&2
        return 0
    fi

    # Does source image contain /bin/sh ?
    ( set -x ; docker create --name "$tmp_container" "$orig_image" ; ) || return 1
    if ! ( set -x ; docker cp "$tmp_container":/bin/sh - >/dev/null 2>&1 ; ) ; then
        ( set -x ; docker rm "$tmp_container" ; ) || true
        echo "WARNING: /bin/sh doesn't exist in docker image $orig_image" >&2
        return 0
    fi
    ( set -x ; docker rm "$tmp_container" ; ) || return 1


    # Generate a Dockerfile snippet that restores
    # original image parameters that we will change when building the
    # updated image (ie the USER parameter)
    local dockerfile_footer
    dockerfile_footer="$(
        script="\
import json,sys
data = json.load(sys.stdin)
user = data[0]['Config']['User']

# User may be an empty string, but that syntax is not officially
# supported in Dockerfile. Set it to UID 0 in this case.
if not user:
    print('USER 0')
else:
    print('USER %s' % json.dumps(user))
"
        docker image inspect "$orig_image" | python3 -c "$script"
    )" || return 1

    # This string will be printed by helper scripts when
    # they have successfully removed anything
    local output_token="DOCKER_IMAGE_POSTBUILD_"

    # Create docker build context directory
    rm -rf "$work_dir/docker-build-context"
    mkdir -p "$work_dir/docker-build-context"

    # Create a docker file simlar to:
    #
    #    FROM image:tag
    #    USER 0:0
    #
    #    RUN mkdir /tmp/stx-postbuild-work
    #
    #    COPY utils.sh /tmp/stx-postbuild-work/
    #
    #    COPY remove-python-packages.sh /tmp/stx-postbuild-work/
    #    RUN OUTPUT_TOKEN="DOCKER_IMAGE_POSTBUILD__PYTHON_PACKAGES" sh /tmp/stx-postbuild-work/remove-python-packages.sh pip
    #
    #    COPY remove-os-packages.sh /tmp/stx-postbuild-work/
    #    RUN OUTPUT_TOKEN="DOCKER_IMAGE_POSTBUILD__OS_PACKAGES" sh /tmp/stx-postbuild-work/remove-os-packages.sh python3-pip
    #
    #    COPY shell-command.sh /tmp/stx-postbuild-work/
    #    RUN . /tmp/stx-postbuild-work/shell-command.sh
    #
    #    RUN rm -rf /tmp/stx-postbuild-work
    #
    #    USER 0:0
    #
    local dockerfile="$work_dir/Dockerfile.postbuild"
    echo >"$dockerfile" "\
FROM $orig_image
USER 0:0
" \
        || return 1

    # Create the scripts directory
    echo >>"$dockerfile" "\
RUN mkdir /tmp/stx-postbuild-work
"

    # Copy common scripts if necessary
    if [[ -n "$python_packages" || -n "$os_packages" ]] ; then
        cp "$HELPERS_DIR/utils.sh" "$work_dir/docker-build-context/"
        echo >>"$dockerfile" "\
COPY utils.sh /tmp/stx-postbuild-work/
"
    fi

    # Add commands to execute remove-python-packages.sh script
    if [[ -n "$python_packages" ]] ; then
        cp "$HELPERS_DIR/remove-python-packages.sh" "$work_dir/docker-build-context/" || return 1
        echo >>"$dockerfile" "\
COPY remove-python-packages.sh /tmp/stx-postbuild-work/
RUN OUTPUT_TOKEN=\"${output_token}_PYTHON_PACKAGES\" sh /tmp/stx-postbuild-work/remove-python-packages.sh $python_packages
" \
            || return 1
    fi

    # Add commands to execute remove-os-packages.sh script
    if [[ -n "$os_packages" ]] ; then
        cp "$HELPERS_DIR/remove-os-packages.sh" "$work_dir/docker-build-context/" || return 1
        echo >>"$dockerfile" "\
COPY remove-os-packages.sh /tmp/stx-postbuild-work/
RUN OUTPUT_TOKEN=\"${output_token}_OS_PACKAGES\" sh /tmp/stx-postbuild-work/remove-os-packages.sh $os_packages
" \
            || return 1
    fi

    # Add commands to execute the shell command
    if [[ -n "$shell_command" ]] ; then
        echo -n "$shell_command" >"$work_dir/docker-build-context/shell-command.sh" || return 1
        echo >>"$dockerfile" "\
COPY shell-command.sh /tmp/stx-postbuild-work/
RUN . /tmp/stx-postbuild-work/shell-command.sh
" \
            || return 1
    fi

    # Delete helper scripts
    echo >>"$dockerfile" "\
RUN rm -rf /tmp/stx-postbuild-work
"
    # Add the footer
    echo "$dockerfile_footer" >>"$dockerfile"

    # Print it out
    echo "=== Dockerfile for the intermediate image"
    sed -r 's/^/    /' "$dockerfile" || return 1

    # Build this modified image
    local build_stdout
    build_output=$(
        set -x
        docker build --no-cache --tag "$tmp_image" -f "$dockerfile" "$work_dir/docker-build-context" 2>&1
    ) || {
        echo "$build_output" >&2
        rmdir "$work_dir" >/dev/null 2>&1
        return 1
    }
    rmdir "$work_dir" >/dev/null 2>&1
    echo "$build_output" >&2

    # Helper scripts print ${output_token} followed by package names
    # actually removed
    local token_output
    token_output="$(echo "$build_output" | \grep "^${output_token}_")"

    # Python packages actually removed
    local python_rmlist
    python_rmlist=$(
        echo "$token_output" \
            | sed -nr "s/^${output_token}_PYTHON_PACKAGES //gp"
    )

    # OS packages actually removed
    local os_rmlist
    os_rmlist=$(
        echo "$token_output" \
            | sed -nr "s/^${output_token}_OS_PACKAGES //gp"
    )

    # If any packages were removed; or if shell_command was executed
    #  ==> retag the tmp image to the final name
    if [[ ( -n "$python_rmlist" || -n "$os_rmlist" ) || -n "$shell_command" ]] ; then
        ( set -x ; docker image tag "$tmp_image" "$new_image" ; ) || {
            ( set -x ; docker image rm "$tmp_image" >/dev/null 2>&1 ; ) || true
            return 1
        }
        ( set -x ; docker image rm "$tmp_image" >/dev/null 2>&1 ; ) || true
        if [[ -n "$python_rmlist" ]] ; then
            echo "=== Removed python packages [$python_rmlist] in image $new_image" >&2
        fi
        if [[ -n "$os_rmlist" ]] ; then
            echo "=== Removed OS packages [$os_rmlist] in image $new_image" >&2
        fi
        if [[ -n "$shell_command" ]] ; then
            echo "=== Executed user command in in image $new_image" >&2
        fi
    # Otherwise just remove the tmp image
    else
        ( set -x ; docker image rm "$tmp_image" ; ) || return 1
        echo "=== No removable packages found in image $new_image" >&2
    fi

    # done
    return 0
}

remove_packages_from_docker_image \
    "$WORK_DIR" \
    "$ORIG_IMAGE" \
    "$TMP_IMAGE" \
    "$TMP_CONTAINER" \
    "$NEW_IMAGE" \
    "$PYTHON_PACKAGES" \
    "$OS_PACKAGES" \
    "$SHELL_COMMAND"

