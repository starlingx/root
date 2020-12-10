#!/bin/bash

#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A collection of utilities relating to 'repo'
#


#
# Echo to stderr
#    echo_stderr [any text you want]
#

echo_stderr ()
{
    echo "$@" >&2
}

#
# Get the root dir of a repo managed repository
#    repo_root [<dir_path>]
#

repo_root () {
    local query_dir="${1:-${PWD}}"
    local work_dir

    if [ ! -d "${query_dir}" ]; then
        echo_stderr "not a valid directory: ${query_dir}"
        return 1
    fi

    if [ "${query_dir:0:1}" != "/" ]; then
        query_dir=$(readlink -f ${query_dir})
        if [ $? -ne 0 ]; then
            return 1
        fi
    fi

    work_dir="${query_dir}"
    while true; do
        if [ -d "$work_dir/.repo/manifests" ]; then
            echo $work_dir
            return 0
        fi

        if [ "${work_dir}" == "/" ]; then
            break
        fi

        work_dir="$(dirname "${work_dir}")"
    done

    echo_stderr "directory is not controlled by repo: ${query_dir}"
    return 1
}

#
# Get the active manifest file of a repo managed repository
#    repo_manifest [<dir_path>]
#

repo_manifest () {
    local query_dir="${1:-${PWD}}"
    local root_dir=""
    local repo_manifest=""

    root_dir="$(repo_root "${query_dir}")"
    if [ $? -ne 0 ]; then
        return 1
    fi

    repo_manifest="${root_dir}/.repo/manifest.xml"

    # Depending on repo version, ${repo_manifest} is either a symlink to
    # the real manifest, or a wrapper manifest that includes the real manifest
    if [ -L "${repo_manifest}" ]; then
        readlink -f "${repo_manifest}"
    else
        grep "<include " ${repo_manifest} | head -n 1 | sed "s#^.*name=\"\([^\"]*\)\".*#${root_dir}/.repo/manifests/\1#"
    fi
}

#
# Get a list of repo remotes.
#    repo_manifest
#
# Current directory must be withing the repo.

repo_remote_list () {
    repo forall -c 'echo $REPO_REMOTE' | sort --unique
}

repo_is_remote () {
    local query_remote=$1
    local remote
    for remote in $(repo_remote_list); do
        if [ "$query_remote" == "$remote" ]; then
            return 0
        fi
    done
    return 1
}

#
# Get a list of repo projects.
# Optionally restrict the list to projects from listed remotes.
#    repo_manifest [remote_1 remote_2 ...]
#
# Current directory must be withing the repo.

repo_project_list () {
    local remote_list=( $@ )

    if [ ${#remote_list[@]} -eq 0 ]; then
        repo forall -c 'echo $REPO_PROJECT' | sort --unique
    else
        for remote in ${remote_list[@]}; do
            repo forall -c \
                'if [ "$REPO_REMOTE" = "'${remote}'" ]; then echo $REPO_PROJECT; fi' \
                | sort --unique
        done
    fi
}

repo_is_project () {
    local query_project=$1
    local project
    for project in $(repo_project_list); do
        if [ "$query_project" == "$project" ]; then
            return 0
        fi
    done
    return 1
}


#
# manifest_set_revision <old_manifest> <new_manifest> <revision> <lock_down> <project-list>
#
#    old_manifest = Path to original manifest.
#    new_manifest = Path to modified manifest. It will not overwrite an
#                   existing file.
#    revision     = A branch, tag ,or sha.  Branch and SHA can be used
#                   directly, but repo requires that a tag be in the form
#                   "refs/tags/<tag-name>".
#    lock_down    = 0 or 1.  If 1, set a revision on all other non-listed
#                   projects to equal the SHA of the current git head.
#    project-list = A space seperated list of projects.  Listed projects
#                   will have their revision set to the provided revision
#                   value.
#
manifest_set_revision () {
    local old_manifest="${1}"
    local new_manifest="${2}"
    local revision="${3}"
    local lock_down="${4}"
    shift 4
    local projects="${@}"

    local repo_root_dir=""
    local line=""
    local FOUND=0
    local path=""
    local project=""
    local rev=""

    repo_root_dir=$(repo_root)
    if [ $? -ne 0 ]; then
        echo_stderr "Current directory is not managed by repo."
        return 1
    fi

    if [ ! -f "${old_manifest}" ]; then
        echo_stderr "Old manifest file is missing '${old_manifest}'."
        return 1
    fi

    if [ -f "${new_manifest}" ]; then
        echo_stderr "New manifest file already present '${new_manifest}'."
        return 1
    fi

    mkdir -p "$(dirname "${new_manifest}")"
    if [ $? -ne 0 ]; then
        echo_stderr "Failed to create directory '$(dirname "${new_manifest}")'"
        return 1
    fi

    while IFS= read -r line; do
        echo "${line}" | grep -q '<project'
        if [ $? -ne 0 ]; then
            # Line does not define a project, do not modify
            echo "${line}"
            continue
        fi

        # check if project name is selected
        FOUND=0
        for project in ${projects}; do
            echo "${line}" | grep -q 'name="'${project}'"'
            if [ $? -eq 0 ]; then
                FOUND=1
                break
            fi
        done

        rev=${revision}
        if [ $FOUND -eq 0 ]; then
            # A non-selected project
            if [ ${lock_down} -eq 0 ]; then
                echo "${line}"
                continue
            fi

            path="${repo_root_dir}/$(echo "${line}" | sed 's#.*path="\([^"]*\)".*#\1#')"
            rev=$(cd "${path}"; git rev-parse HEAD)
        fi

        # Need to set revision on selected project
        if echo "${line}" | grep -q 'revision='; then
            echo "${line}" | sed "s#revision=\"[^\"]*\"#revision=\"${rev}\"#"
        else
            # No prior revision
            # Set revision prior to name
            echo "${line}" | sed "s#name=#revision=\"${rev}\" name=#"
        fi
    done < "${old_manifest}" > "${new_manifest}"

    return 0
}

