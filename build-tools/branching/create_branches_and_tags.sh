#!/bin/bash

#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# Create a git branch from the current heads on a subset of git projects
# within a manifest.  A tag is also created to mark the commit where
# the branch was forked from.  The subset of projects affected can be
# identified by project name or remote name.
#
# Optionally a new manifest (<branch>.xml) can be created that selects
# the new branch for the affected projects.  As a further option,
# projects that are not branched can me 'locked down' within the new
# manifest by setting the sha of the current head as the revision.
#
# See also: push_branches_tags.sh

CREATE_BRANCHES_AND_TAGS_SH_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${CREATE_BRANCHES_AND_TAGS_SH_DIR}/../git-repo-utils.sh"

usage () {
    echo "create_branches_and_tags.sh --branch=<branch> [--tag=<tag>] [ --remotes=<remotes> ] [ --projects=<projects> ] [ --manifest [ --lock-down ]]"
    echo ""
    echo "Create a branch and a tag in all listed projects, and all"
    echo "projects hosted by all listed remotes.  Lists are comma separated."
    echo ""
    echo "The branch name must be provided.  The tag name can also be provided."
    echo "If the tag is omitted, one is automativally generate by adding the"
    echo "prefix 'v' to the branch name."
    echo ""
    echo "If a manifest is requested, it will recieve the name '<branch>.xml' and"
    echo "it will specify the branch as the revision for all tagged projects."
    echo "If lockdown is requested, all other projects get the current"
    echo "HEAD's sha set as the revision."
}

TEMP=$(getopt -o h --long remotes:,projects:,branch:,tag:,manifest,lock-down,help -n 'create_branches_and_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
LOCK_DOWN=0
remotes=""
projects=""
branch=""
tag=""
manifest=""
new_manifest=""
repo_root_dir=""

while true ; do
    case "$1" in
        -h|--help)         HELP=1 ; shift ;;
        --remotes)         remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)        projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --branch)          branch=$2; shift 2;;
        --tag)             tag=$2; shift 2;;
        --manifest)        MANIFEST=1 ; shift ;;
        --lock-down)       LOCK_DOWN=1 ; shift ;;
        --)                shift ; break ;;
        *)                 usage; exit 1 ;;
    esac
done

if [ $HELP -eq 1 ]; then
    usage
    exit 0
fi

if [ "$branch" == "" ] ; then
    echo_stderr "ERROR: You must specify a branch"
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

    new_manifest="$(dirname $manifest)/${branch}-$(basename $manifest)"
    if [ -f $new_manifest ]; then
        echo_stderr "branched manifest file already present '$new_manifest'."
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

# Provide a default tag name if not otherwise provided
if [ "$tag" == "" ]; then
    tag="v$branch"
fi



echo "Finding subgits"
SUBGITS=$(repo forall $projects -c 'echo '"$repo_root_dir"'/$REPO_PATH')

# Go through all subgits and create the branch and tag if they does not already exist
echo "Applying branched and tags"
(
for subgit in $SUBGITS; do
    (
    cd $subgit

    review_method=$(git_repo_review_method)
    if [ -f .gitreview ] && [ "${review_method}" == "gerrit" ] ; then
        git review -s > /dev/null
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to setup git review in ${subgit}"
            exit 1
        fi
    fi

    remote=$(git_repo_remote)
    if [ "${remote}" == "" ]; then
        remote=$(git_remote)
        if [ "${remote}" == "" ]; then
            echo_stderr "ERROR: Failed to determine remote in ${subgit}"
            exit 1
        fi
    fi

    remote_branch=$(git_repo_remote_branch)
    if [ "${remote_branch}" == "" ]; then
        remote_branch=$(git_remote_branch)
        if [ "${remote_branch}" == "" ]; then
            echo_stderr "ERROR: failed to determine remote branch in ${subgit}"
            exit 1
        fi
    fi

    # check if destination branch already exists
    branch_check=$(git branch -a --list $branch)
    if [ -z "$branch_check" ]; then
        echo "Creating branch $branch in $subgit"
        echo "   git checkout -t ${remote}/${remote_branch} -b $branch"
        git checkout -t ${remote}/${remote_branch} -b $branch
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to create branch '$branch' in ${subgit}"
            exit 1
        fi
    else
        echo "Branch $branch already exists in $subgit"
        git checkout $branch
    fi

    tag_check=$(git tag -l $tag)
    if [ -z "$tag_check" ]; then
        echo "Creating tag $tag in ${subgit}"
        git tag -s -m "Branch $branch" $tag
        if [ $? != 0 ] ; then
            echo "ERROR: failed to create tag '$tag' in ${subgit}"
            exit 1
        fi
    else
        echo "Tag '$tag' already exists in ${subgit}"
    fi
    ) || exit 1
done
) || exit 1

if [ $MANIFEST -eq 1 ]; then
    (
    new_manifest_name=$(basename "${new_manifest}")
    new_manifest_dir=$(dirname "${new_manifest}")
    manifest_name=$(basename "${manifest}")
    manifest_dir=$(dirname "${manifest}")

    cd "${new_manifest_dir}" || exit 1

    review_method=$(git_review_method)
    if [ -f .gitreview ] && [ "${review_method}" == "gerrit" ] ; then
        git review -s > /dev/null
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to setup git review in ${new_manifest_dir}"
            exit 1
        fi
    fi

    branch_check=$(git branch -a --list $branch)
    if [ ! -z "$branch_check" ]; then
        echo "Branch $branch already exists in ${new_manifest_dir}"
        exit 1
    fi

    tag_check=$(git tag -l $tag)
    if [ ! -z "$tag_check" ]; then
        echo "Tag '$tag' already exists in ${new_manifest_dir}"
        exit 1
    fi

    remote=$(git_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${new_manifest_dir}"
        exit 1
    fi

    remote_branch=$(git_remote_branch)
    if [ "${remote_branch}" == "" ]; then
        echo_stderr "ERROR: failed to determine remote branch in ${new_manifest_dir}"
        exit 1
    fi

    echo "Creating branch '$branch' in ${new_manifest_dir}"
    git checkout -t ${remote}/${remote_branch} -b $branch
    if [ $? != 0 ] ; then
        echo_stderr "ERROR: failed to create branch '$branch' in ${new_manifest_dir}"
        exit 1
    fi

    echo "Creating tag '$tag' in ${new_manifest_dir}"
    git tag -s -m "Branch $branch" $tag
    if [ $? != 0 ] ; then
        echo "ERROR: failed to create tag '$tag' in ${new_manifest_dir}"
        exit 1
    fi

    echo "Creating manifest ${new_manifest_name}"
    manifest_set_revision "${manifest}" "${new_manifest}" "$branch" ${LOCK_DOWN} $projects || exit 1

    echo "Move manifest ${new_manifest_name}, overwriting ${manifest_name}"
    \cp -f "${manifest}" "${manifest}.save"
    \mv -f "${new_manifest}" "${manifest}"

    echo "Committing ${manifest_name} in ${manifest_dir}"
    git add ${manifest_name} || exit 1
    git commit -s -m "Modified manifest ${manifest_name} for branch ${branch}"
    if [ $? != 0 ] ; then
        echo_stderr "ERROR: failed to commit new manifest ${manifest_name} in ${manifest_dir}"
        exit 1
    fi

    ) || exit 1
fi
