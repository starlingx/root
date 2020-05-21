#!/bin/bash

#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A tool to push the tags, and optional manifest created by
# create_tags.sh to the upstream source.
#
# Arguemens should match those passed to create_tags.sh
# with the exception of '--lockdown'.
#

PUSH_TAGS_SH_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${PUSH_TAGS_SH_DIR}/../git-repo-utils.sh"

usage () {
    echo "push_tags.sh --tag=<tag> [ --remotes=<remotes> ] [ --projects=<projects> ] [ --manifest [--manifest-prefix <prefix>]]"
    echo " "
    echo "Push a pre-existing git tag into all listed projects, and all projects"
    echo "hosted by all listed remotes.  Lists are comma separated."
    echo ""
    echo "A manifest push can also be requested."
}

TEMP=$(getopt -o h --long remotes:,projects:,tag:,manifest,manifest-prefix:,help -n 'push_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
remotes=""
projects=""
tag=""
manifest=""
manifest_prefix=""
new_manifest=""
repo_root_dir=""

while true ; do
    case "$1" in
        -h|--help)         HELP=1 ; shift ;;
        --remotes)         remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)        projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --tag)             tag=$2; shift 2;;
        --manifest)        MANIFEST=1 ; shift ;;
        --manifest-prefix) manifest_prefix=$2; shift 2;;
        --)                shift ; break ;;
        *)                 usage; exit 1 ;;
    esac
done

if [ $HELP -eq 1 ]; then
    usage
    exit 0
fi

if [ "$tag" == "" ] ; then
    echo_stderr "ERROR: You must specify a tags"
    usage
    exit 1
fi

repo_root_dir=$(repo_root)
if [ $? -ne 0 ]; then
    echo_stderr "Current directory is not managed by repo."
    exit 1
fi

if [ $MANIFEST -eq 1 ]; then
    manifest=$(repo_manifest $repo_root_dir)
    if [ $? -ne 0 ]; then
        echo_stderr "failed to find current manifest."
        exit 1
    fi

    if [ ! -f $manifest ]; then
        echo_stderr "manifest file missing '$manifest'."
        exit 1
    fi

    new_manifest="$(dirname $manifest)/${manifest_prefix}${tag}-$(basename $manifest)"
    if [ ! -f $new_manifest ]; then
        echo_stderr "Expected a tagged manifest file already present '$new_manifest'."
        exit 1
    fi
fi

for project in $projects; do
    if ! repo_is_project $project; then
        echo_stderr "Invalid project: $project"
        echo_stderr "Valid projects are: $(repo_project_list | tr '\n' ' ')"
        exit 1
    fi
done

for remote in $remotes; do
    if ! repo_is_remote $remote; then
        echo_stderr "Invalid remote: $remote"
        echo_stderr "Valid remotes are: $(repo_remote_list | tr '\n' ' ')"
        exit 1
    fi
done

# Add projects from listed remotes
if [ "$remotes" != "" ]; then
    projects+="$(repo_project_list $remotes | tr '\n' ' ')"
fi

# If no projects or remotes specified, process ALL projects
if [ "$projects" == "" ] && [ "$remotes" == "" ]; then
    projects="$(repo_project_list)"
fi

if [ "$projects" == "" ]; then
    echo_stderr "No projects found"
    exit 1
fi


echo "Finding subgits"
SUBGITS=$(repo forall $projects -c 'echo '"$repo_root_dir"'/$REPO_PATH')

# Go through all subgits and create the tag if it does not already exist
(
for subgit in $SUBGITS; do
    (
    cd $subgit
    tag_check=$(git tag -l $tag)
    if [ "${tag_check}" == "" ]; then
        echo_stderr "ERROR: Expected tag '$tag' to exist in ${subgit}"
        exit 1
    fi

    review_method=$(git_repo_review_method)
    if [ "${review_method}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review method in ${subgit}"
        exit 1
    fi

    if [ "${review_method}" == "gerrit" ]; then
        remote=$(git_repo_review_remote)
    else
        remote=$(git_repo_remote)
    fi

    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${subgit}"
        exit 1
    fi

    echo "Pushing tag $tag in ${subgit}"
    if [ "${review_method}" == "gerrit" ]; then
        echo "git push ${remote} ${tag}"
        git push ${remote} ${tag}
    else
        echo "git push ${remote} ${tag}"
        git push ${remote} ${tag}
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push tag '${tag}' to remote '${remote}' in  ${subgit}"
        exit 1
    fi
    )
done
) || exit 1

if [ $MANIFEST -eq 1 ]; then
    (
    new_manifest_name=$(basename "${new_manifest}")
    new_manifest_dir=$(dirname "${new_manifest}")

    cd "${new_manifest_dir}" || exit 1

    local_branch=$(git_local_branch)
    if [ "${local_branch}" == "" ]; then
        echo_stderr "ERROR: failed to determine local branch in ${new_manifest_dir}"
        exit 1
    fi

    remote_branch=$(git_remote_branch)
    if [ "${remote_branch}" == "" ]; then
        echo_stderr "ERROR: failed to determine remote branch in ${new_manifest_dir}"
        exit 1
    fi

    if [ ! -f ${new_manifest_name} ]; then
        echo_stderr "ERROR: Expected file '${new_manifest_name}' to exist in ${new_manifest_dir}"
        exit 1
    fi

    tag_check=$(git tag -l $tag)
    if [ "${tag_check}" == "" ]; then
        echo_stderr "ERROR: Expected tag '$tag' to exist in ${new_manifest_dir}"
        exit 1
    fi

    review_method=$(git_review_method)
    if [ "${review_method}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review method in ${new_manifest_dir}"
        exit 1
    fi

    remote=$(git_review_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${new_manifest_dir}"
        exit 1
    fi

    echo "Pushing tag $tag in ${new_manifest_dir}"
    if [ "${review_method}" == "gerrit" ]; then
        git review
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: Failed to create git review from ${new_manifest_dir}"
            exit 1
        fi
        echo "When review is merged: please run ..."
        echo "   cd ${new_manifest_dir}"
        echo "   git push ${remote} ${tag}"
    else
        git push ${remote} ${local_branch}:${remote_branch}
        git push ${remote} ${tag}:${tag}
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push tag '${tag}' to branch ${remote_branch} on remote '${remote}' from ${new_manifest_dir}"
        exit 1
    fi
    ) || exit 1
fi
