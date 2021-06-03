#!/bin/bash

#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A tool to push the branches, tags, and optional manifest created by
# create_branches_and_tags.sh to the upstream source.
#
# Arguemens should match those passed to create_branches_and_tags.sh
# with the exception of '--lockdown'.
#

PUSH_BRANCHES_TAGS_SH_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source "${PUSH_BRANCHES_TAGS_SH_DIR}/../git-repo-utils.sh"
source "${PUSH_BRANCHES_TAGS_SH_DIR}/../url_utils.sh"

usage () {
    echo "push_branches_tags.sh --branch=<branch> [--tag=<tag>] [ --remotes=<remotes> ] [ --projects=<projects> ] [ --manifest ] [ --bypass-gerrit ]"
    echo ""
    echo "Push a pre-existing branch and tag into all listed projects, and all"
    echo "projects hosted by all listed remotes.  Lists are comma separated."
    echo ""
    echo "The branch name must be provided.  The tag name can also be provided."
    echo "If the tag is omitted, one is automativally generate by adding the"
    echo "prefix 'v' to the branch name."
    echo ""
    echo "A manifest push can also be requested.vision."
}

TEMP=$(getopt -o h --long remotes:,projects:,branch:,tag:,bypass-gerrit,manifest,help -n 'push_branches_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
BYPASS_GERRIT=0
remotes=""
projects=""
branch=""
tag=""
manifest=""
repo_root_dir=""

while true ; do
    case "$1" in
        -h|--help)        HELP=1 ; shift ;;
        --bypass-gerrit)  BYPASS_GERRIT=1 ; shift ;;
        --remotes)        remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)       projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --branch)         branch=$2; shift 2;;
        --tag)            tag=$2; shift 2;;
        --manifest)       MANIFEST=1 ; shift ;;
        --)               shift ; break ;;
        *)                usage; exit 1 ;;
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

    if [ ! -f "${manifest}" ]; then
        echo_stderr "manifest file missing '${manifest}'."
        exit 1
    fi

    if [ ! -f "${manifest}.save" ]; then
        echo_stderr "manifest file missing '${manifest}.save'."
        exit 1
    fi

    # The new manifest referes to branches that are not yet available on the remotes.
    # This will break some repo commands, e.g repo forall'.
    #
    # To get arround this we swap in the old manifest until we get passed the
    # problematic commands.
    \cp -f "${manifest}" "${manifest}.new"
    \cp -f "${manifest}.save" "${manifest}"
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
(
for subgit in $SUBGITS; do
    (
    cd $subgit

    git fetch --all

    branch_check=$(git branch -a --list $branch)
    if [ -z "$branch_check" ]; then
        echo_stderr "ERROR: Expected branch '$branch' to exist in ${subgit}"
        exit 1
    fi

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

    remote=$(git_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${manifest_dir}"
        exit 1
    fi

    if [ "${review_method}" == "gerrit" ]; then
        review_remote=$(git_repo_review_remote)
    else
        review_remote=${remote}
    fi

    if [ "${review_remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review_remote in ${subgit}"
        exit 1
    fi

    branch_check=$(git branch -a --list $remote/$branch)
    if [ "${branch_check}" != "" ]; then
        echo "Branch $branch already exists in ${subgit}"
        exit 0
    fi

    echo "Pushing branch $branch in ${subgit}"
    if [ "${review_method}" == "gerrit" ] && [ $BYPASS_GERRIT -eq 0 ]; then
        url=$(git_repo_review_url)
        if [ "${review_remote}" == "" ]; then
            echo_stderr "ERROR: Failed to determine review_url in ${subgit}"
            exit 1
        fi

        host=$(url_server "${url}")
        port=$(url_port "${url}")
        path=$(url_path "${url}")
        if [ "${host}" == "review.opendev.org" ]; then
            git push ${review_remote} ${tag} && \
            ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag} && \
            git config --local --replace-all "branch.${branch}.merge" refs/heads/${branch} && \
            git review --topic="${branch}"
        else
            echo "git push --tags ${review_remote} ${branch}"
            git push --tags ${review_remote} ${branch}
        fi
    else
        echo "git push --tags --set-upstream ${remote} ${branch}"
        git push --tags --set-upstream ${remote} ${branch}
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push branch '${branch}' to remote '${remote}' in  ${subgit}"
        exit 1
    fi
    )
done
) || exit 1

if [ $MANIFEST -eq 1 ]; then
    # restore manifest
    \cp -f "${manifest}.new" "${manifest}"
fi

if [ $MANIFEST -eq 1 ]; then
    (
    manifest_name=$(basename "${manifest}")
    manifest_dir=$(dirname "${manifest}")

    cd "${manifest_dir}" || exit 1

    if [ ! -f ${manifest_name} ]; then
        echo_stderr "ERROR: Expected file '${manifest_name} to exist in ${manifest_dir}"
        exit 1
    fi

    branch_check=$(git branch -a --list $branch)
    if [ -z "$branch_check" ]; then
        echo_stderr "ERROR: Expected branch '$branch' to exist in ${manifest_dir}"
        exit 1
    fi

    tag_check=$(git tag -l $tag)
    if [ "${tag_check}" == "" ]; then
        echo_stderr "ERROR: Expected tag '$tag' to exist in ${manifest_dir}"
        exit 1
    fi

    review_method=$(git_review_method)
    if [ "${review_method}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review method in ${manifest_dir}"
        exit 1
    fi

    remote=$(git_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${manifest_dir}"
        exit 1
    fi

    review_remote=$(git_review_remote)
    if [ "${review_remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review_remote in ${manifest_dir}"
        exit 1
    fi

    echo "Pushing branch $branch in ${manifest_dir}"
    if [ "${review_method}" == "gerrit" ] && [ $BYPASS_GERRIT -eq 0 ]; then
        # Is a reviewless push possible as part of creating a new branch in gerrit?
        url=$(git_review_url)
        if [ "${review_remote}" == "" ]; then
            echo_stderr "ERROR: Failed to determine review_url in ${subgit}"
            exit 1
        fi

        host=$(url_server "${url}")
        port=$(url_port "${url}")
        path=$(url_path "${url}")
        if [ "${host}" == "review.opendev.org" ]; then
            git push ${review_remote} ${tag} && \
            ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag} && \
            git config --local --replace-all "branch.${branch}.merge" refs/heads/${branch} && \
            git review --yes --topic="${branch}"
        else
            git push --tags ${review_remote} ${branch}
        fi
    else
        git push --tags --set-upstream ${review_remote} ${branch}
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push tag '${tag}' to remote '${review_remote}' in  ${manifest_dir}"
        exit 1
    fi
    ) || exit 1
fi
