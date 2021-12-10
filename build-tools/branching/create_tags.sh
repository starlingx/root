#!/bin/bash

#
# Copyright (c) 2020-2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# Create a git tag from the current heads on a subset of git projects
# within a manifest.  The subset of projects affected can be
# identified by project name or remote name.
#
# Optionally a new manifest (<tag>.xml) can be created that selects
# the new tag for the affected projects.  As a further option,
# projects that are not branched can me 'locked down' within the new
# manifest by setting the sha of the current head as the revision.
#
# See also: push_tags.sh

CREATE_TAGS_SH_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${CREATE_TAGS_SH_DIR}/../git-repo-utils.sh"

usage () {
    echo "create_tags.sh --tag=<tag> <options>"
    echo ""
    echo "    Create a tag on selected projects, and optionally update the manifest"
    echo "    to use the the new branch."
    echo ""
    echo "selection options:"
    echo "    [ --remotes=<remotes> ] [ --projects=<projects> ]"
    echo ""
    echo "    Create a branch and a tag in all listed projects, and all"
    echo "    projects hosted by all listed remotes.  Lists are comma separated."
    echo ""
    echo "manifest options:"
    echo "    [ --manifest ]"
    echo "                        Modify the current repo manifest to specify the"
    echo "                        new branch for all select remotes and projects."
    echo "    [ --manifest-file=<file.xml> ]"
    echo "                        Override the manifest file to be updated."
    echo "    [ --default-revision ]"
    echo "                        Set the default revision of the manifest."
    echo "    [ --hard-lock-down | --lockdown ]"
    echo "                        All unselected projects get the current HEAD's"
    echo "                        SHA set as the revision."
    echo "    [ --soft-lock-down ]"
    echo "                        All unselected projects with an revision that"
    echo "                        is unset, or 'master', get the current HEAD's sha"
    echo "                        set as the revision."
    echo "    [ --lock-down-exclude-remotes=<remotes> ]"
    echo "    [ --lock-down-exclude-projects=<projects> ]"
    echo "                        Exclude from lock-down these additional projects, and"
    echo "                        all projects hosted by these additional remotes."
    echo "                        Lists are comma separated."
    echo ""
    echo "other options:"
    echo "    [ --safe-gerrit-host=<host> ]"
    echo "                                allows one to specify host names of gerrit"
    echo "                                servers that are safe to push reviews to."
    echo ""
}

TEMP=$(getopt -o h --long remotes:,projects:,tag:,manifest,manifest-file:,manifest-prefix:,lock-down,hard-lock-down,soft-lock-down,default-revision,safe-gerrit-host:,lock-down-exclude-remotes:,lock-down-exclude-projects:,help -n 'create_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
LOCK_DOWN=0
SET_DEFAULT_REVISION=0
remotes=""
projects=""
ld_exclude_remotes=""
ld_exclude_projects=""
tag=""
manifest=""
new_manifest=""
manifest_prefix=""
repo_root_dir=""

safe_gerrit_hosts=()
while true ; do
    case "$1" in
        -h|--help)           HELP=1 ; shift ;;
        --remotes)           remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)          projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --lock-down-exclude-remotes)  ld_exclude_remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --lock-down-exclude-projects) ld_exclude_projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --tag)               tag=$2; shift 2;;
        --manifest)          MANIFEST=1 ; shift ;;
        --manifest-file)     repo_set_manifest_file "$2"; shift 2;;
        --manifest-prefix)   manifest_prefix=$2; shift 2;;
        --lock-down)         LOCK_DOWN=2 ; shift ;;
        --hard-lock-down)    LOCK_DOWN=2 ; shift ;;
        --soft-lock-down)    LOCK_DOWN=1 ; shift ;;
        --default-revision)  SET_DEFAULT_REVISION=1 ; shift ;;
        --safe-gerrit-host)  safe_gerrit_hosts+=("$2") ; shift 2 ;;
        --)                  shift ; break ;;
        *)                   usage; exit 1 ;;
    esac
done
git_set_safe_gerrit_hosts "${safe_gerrit_hosts[@]}"

if [ $HELP -eq 1 ]; then
    usage
    exit 0
fi

if [ "$tag" == "" ] ; then
    echo_stderr "ERROR: You must specify a tag"
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
    if [ -f $new_manifest ]; then
        echo_stderr "tagged manifest file already present '$new_manifest'."
        exit 1
    fi
fi

for project in $projects $ld_exclude_projects; do
    if ! repo_is_project $project; then
        echo_stderr "Invalid project: $project"
        echo_stderr "Valid projects are: $(repo_project_list | tr '\n' ' ')"
        exit 1
    fi
done

for remote in $remotes $ld_exclude_remotes; do
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

if [ "$ld_exclude_remotes" != "" ]; then
    ld_exclude_projects+="$(repo_project_list $ld_exclude_remotes | tr '\n' ' ')"
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
echo "Applying branched and tags"
(
for subgit in $SUBGITS; do
    (
    cd $subgit

    review_method=$(git_repo_review_method)
    if [ -f .gitreview ] && [ "${review_method}" == "gerrit" ] ; then
        with_retries -d 45 -t 15 -k 5 5 git review -s >&2
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to setup git review in ${subgit}"
            exit 1
        fi
    fi

    tag_check=$(git tag -l $tag)
    if [ -z "$tag_check" ]; then
        echo "Creating tag '$tag' in ${subgit}"
        git tag -s -m "Tag $tag" $tag
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to create tag '$tag' in ${subgit}"
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

    cd "${new_manifest_dir}" || exit 1

    review_method=$(git_review_method)
    if [ -f .gitreview ] && [ "${review_method}" == "gerrit" ] ; then
        with_retries -d 45 -t 15 -k 5 5 git review -s >&2
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to setup git review in ${new_manifest_dir}"
            exit 1
        fi
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

    new_branch="create_manifest_for_tag_${tag}"
    # check if destination branch already exists
    branch_check=$(git branch -a --list $new_branch)
    if [ -z "$branch_check" ]; then
        echo "Creating branch ${new_branch} in ${new_manifest_dir}"
        git checkout -t ${remote}/${remote_branch} -b "${new_branch}"
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: failed to create branch '${new_branch}' in ${new_manifest_dir}"
            exit 1
        fi
    else
        echo "Branch ${branch} already exists in $subgit"
        git checkout ${new_branch}
    fi

    echo "Creating manifest ${new_manifest_name}"
    manifest_set_revision "${manifest}" "${new_manifest}" "refs/tags/$tag" "${LOCK_DOWN}" "${SET_DEFAULT_REVISION}" "${projects// /,}" "${ld_exclude_projects// /,}" || exit 1

    echo "Committing ${new_manifest_name} in ${new_manifest_dir}"
    git add ${new_manifest_name} || exit 1
    git commit -s -m "Create new manifest ${new_manifest_name}"
    if [ $? != 0 ] ; then
        echo_stderr "ERROR: failed to commit new manifest ${new_manifest_name} in ${new_manifest_dir}"
        exit 1
    fi

    echo "Creating tag '$tag' in ${new_manifest_dir}"
    git tag -s -m "Tag $tag" $tag
    if [ $? != 0 ] ; then
        echo_stderr "ERROR: failed to create tag '$tag' in ${new_manifest_dir}"
        exit 1
    fi
    ) || exit 1
fi
