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

REPO_MANIFEST_FILE=
repo_set_manifest_file() {
    REPO_MANIFEST_FILE="$1"
}

repo_manifest () {
    local query_dir="${1:-${PWD}}"
    local root_dir=""
    local repo_manifest=""

    root_dir="$(repo_root "${query_dir}")"
    if [ $? -ne 0 ]; then
        return 1
    fi

    if [[ -n "$REPO_MANIFEST_FILE" ]] ; then
        if [[ "$REPO_MANIFEST_FILE" =~ ^/ ]] ; then
            echo "$REPO_MANIFEST_FILE"
        else
            echo "${root_dir}/.repo/manifests/$REPO_MANIFEST_FILE"
        fi
        return 0
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
# manifest_get_revision_of_project <manifest> <project-name>
#
# Extract the revision of a project within the manifest.
# The default revision is supplied in the absence
# of an explicit project revision.
#
#    manifest = Path to manifest.
#    project-name = name of project.
#
manifest_get_revision_of_project () {
    local manifest="${1}"
    local project="${2}"

    local default_revision=""
    local revision=""

    default_revision=$(manifest_get_default_revision "${manifest}")
    revision=$(grep '<project' "${manifest}" | \
                grep -e "name=${project}" -e "name=\"${project}\"" | \
                grep 'revision=' | \
                sed -e 's#.*revision=\([^ ]*\).*#\1#' -e 's#"##g' -e "s#'##g")
    if [ "${revision}" != "" ]; then
        echo "${revision}"
    elif [ "${default_revision}" != "" ]; then
        echo "${default_revision}"
    else
        return 1
    fi
}

#
# manifest_get_default_revision <manifest>
#
# Extract the default revision of the manifest, if any.
#
#    manifest = Path to manifest.
#
manifest_get_default_revision () {
    local manifest="${1}"

    grep '<default' $manifest |sed -e 's#.*revision=\([^ /]*\).*#\1#' -e 's#"##g' -e "s#'##g"
}

#
# manifest_set_revision <old_manifest> <new_manifest> <revision> <lock_down> <project-list> <excluded_project_list>
#
#    old_manifest = Path to original manifest.
#    new_manifest = Path to modified manifest. It will not overwrite an
#                   existing file.
#    revision     = A branch, tag ,or sha.  Branch and SHA can be used
#                   directly, but repo requires that a tag be in the form
#                   "refs/tags/<tag-name>".
#    lock_down    = 0,1 or 2.  If 2, set a revision on all other non-listed
#                   projects to equal the SHA of the current git head.
#                   If 1, similar to 2, but only if the project doesn't have
#                   some other form of revision specified.
#    project-list = A comma seperated list of projects.  Listed projects
#                   will have their revision set to the provided revision
#                   value.
#    excluded_project-list = A comma seperated list of projects.  Listed
#                   projects will not be subject to lock-down.
#
manifest_set_revision () {
    local old_manifest="${1}"
    local new_manifest="${2}"
    local revision="${3}"
    local lock_down="${4}"
    local set_default="${5}"
    local projects="${6//,/ }"
    local ld_exclude_projects="${7//,/ }"

    local old_default_revision=""
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

    old_default_revision=$(manifest_get_default_revision "${old_manifest}")
    if [ ${set_default} -eq 1 ] && [ "${old_default_revision}" == "" ]; then
        # We only know how to alter an existing default revision, not set a
        # new one, so continue without setting a default.
        set_default=0
    fi

    while IFS= read -r line; do
        echo "${line}" | grep -q '<project'
        if [ $? -ne 0 ]; then
            # Line does not define a project
            if [ ${set_default} -eq 0 ] || [ "${old_default_revision}" == "" ]; then
                # No further processing, do not modify
                echo "${line}"
                continue
            fi

            # ok setting the default
            echo "${line}" | grep -q '<default'
            if [ $? -ne 0 ]; then
                # Line does not set defaults, do not modify
                echo "${line}"
                continue
            fi

            echo "${line}" | sed "s#${old_default_revision}#${revision}#"
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

        LD_EXCLUDE_FOUND=0
        for project in ${ld_exclude_projects}; do
            echo "${line}" | grep -q 'name="'${project}'"'
            if [ $? -eq 0 ]; then
                LD_EXCLUDE_FOUND=1
                break
            fi
        done

        rev=${revision}
        old_rev=$(echo "${line}" | grep 'revision=' | sed -e 's#.*revision=\([^ ]*\).*#\1#' -e 's#"##g' -e "s#'##g")
        if [ $FOUND -eq 0 ]; then
            # A non-selected project
            if [ ${lock_down} -eq 2 ] && [ $LD_EXCLUDE_FOUND -eq 0 ]; then
                # Hard lock-down
                # Set the revision to current HEAD SHA.
                path="${repo_root_dir}/$(echo "${line}" | sed 's#.*path="\([^"]*\)".*#\1#')"
                rev=$(cd "${path}"; git rev-parse HEAD)
            elif [ ${lock_down} -eq 1 ] && [ $LD_EXCLUDE_FOUND -eq 0 ] && [ "${old_rev}" == "" ]; then
                # Soft lock-down but no revision is currently set on the project.
                # Set the revision to current HEAD SHA.
                path="${repo_root_dir}/$(echo "${line}" | sed 's#.*path="\([^"]*\)".*#\1#')"
                rev=$(cd "${path}"; git rev-parse HEAD)
            elif [ ${lock_down} -eq 1 ] && [ $LD_EXCLUDE_FOUND -eq 0 ] && [ "${old_rev}" == "master" ]; then
                # Soft lock-down and project has revision set to 'master' which is definitly unstable.
                # Set the revision to current HEAD SHA.
                path="${repo_root_dir}/$(echo "${line}" | sed 's#.*path="\([^"]*\)".*#\1#')"
                rev=$(cd "${path}"; git rev-parse HEAD)
            else
                if [ ${set_default} -eq 0 ] || [ "${old_default_revision}" == "${revision}" ]; then
                    # default revision unchanged, leave it be
                    echo "${line}"
                    continue
                fi

                if [ "${old_rev}" != "" ]; then
                    # Non-selected project has an explicit revision, leave it be
                    echo "${line}"
                    continue
                fi

                # The default revision will change, but this project, which
                # relied on the old default, is not supposed to change,
                # so it's revision must now be explicitly set to point to
                # the old default revision.
                rev="${old_default_revision}"
            fi
        else
            # A selected project
            if [ ${set_default} -eq 1 ]; then
                # Selected project does not need to set a revision.
                # The revision will come from the default
                if [ "${old_rev}" == "" ]; then
                    # project has no revision to delete
                    echo "${line}"
                    continue
                fi

                # delete any revision present
                echo "${line}" | sed 's#\(.*\)revision=[^ ]*\(.*\)#\1\2#'
                continue
            fi
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

