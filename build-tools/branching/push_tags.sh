#!/bin/bash

#
# Copyright (c) 2020-2022 Wind River Systems, Inc.
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
    echo "push_tags.sh --tag=<tag> [ --remotes=<remotes> ] [ --projects=<projects> ]"
    echo "             [ --exclude-projects=<projects> ]"
    echo "             [ --manifest [ --manifest-file=<manifest.xml> ] [--manifest-prefix <prefix>]]"
    echo "             [ --bypass-gerrit ] [--safe-gerrit-host=<host>]"
    echo "             [ --access-token=<remote>:<token> ] [ --dry-run ]"
    echo " "
    echo "Push a pre-existing git tag into all listed projects, and all projects"
    echo "hosted by all listed remotes, minus excluded projects."
    echo "Lists are comma separated."
    echo ""
    echo "--access-token can be used to supply an access token for direct (non-gerrit) push attempts"
    echo "               to specific remotes.   e.g. github now requires this"
    echo ""
    echo "A manifest push can also be requested."
    echo ""
    echo "--manifest-file may be used to override the manifest file to be updated."
    echo ""
    echo "--safe-gerrit-host allows one to specify host names of gerrit servers"
    echo "that are safe to push reviews to."

}

TEMP=$(getopt -o h,n --long remotes:,projects:,exclude-projects:,tag:,manifest,manifest-file:,manifest-prefix:,bypass-gerrit,safe-gerrit-host:,access-token:,help,dry-run -n 'push_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    echo_stderr "ERROR: getopt failure"
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
DRY_RUN=
MANIFEST=0
BYPASS_GERRIT=0
remotes=""
projects=""
excluded_projects=""
tag=""
manifest=""
manifest_prefix=""
new_manifest=""
repo_root_dir=""
declare -A access_token

safe_gerrit_hosts=()
while true ; do
    case "$1" in
        -h|--help)         HELP=1 ; shift ;;
        -n|--dry-run)      DRY_RUN="--dry-run" ; shift ;;
        --bypass-gerrit)   BYPASS_GERRIT=1 ; shift ;;
        --remotes)         remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)        projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --exclude-projects)    excluded_projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --tag)             tag=$2; shift 2;;
        --manifest)        MANIFEST=1 ; shift ;;
        --manifest-file)   repo_set_manifest_file "$2"; shift 2;;
        --manifest-prefix) manifest_prefix=$2; shift 2;;
        --safe-gerrit-host) safe_gerrit_hosts+=("$2") ; shift 2 ;;
        --access-token)    val=$2
                           at_remote=$(echo "$val" | cut -d ':' -f 1)
                           at_token=$(echo "$val" | cut -d ':' -f 2)
                           if [ -z "$at_token" ]; then
                               usage
                               exit 1
                           fi
                           access_token["$at_remote"]="$at_token"
                           shift 2 ;;
        --)                shift ; break ;;
        *)                 usage; exit 1 ;;
    esac
done
git_set_safe_gerrit_hosts "${safe_gerrit_hosts[@]}"

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

for project in $projects $excluded_projects; do
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

if [ "$projects" != "" ] && [ "$exclude_projects" != "" ]; then
    for project in $exclude_projects; do
        projects=$(echo $projects | sed -e "s# $project # #" -e "s#^$project ##" -e "s# $project\$##" -e "s#^$project\$##")
    done
fi

if [ "$projects" == "" ]; then
    echo_stderr "No projects found"
    exit 1
fi

echo "List of projects to be pushed"
echo "============================="
for project in $projects; do
        echo $project
done
echo "============================="
echo

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

    remote=$(git_repo_review_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${subgit}"
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

    echo "Pushing tag $tag in ${subgit}"
    if [ "${review_method}" == "gerrit" ] && [ $BYPASS_GERRIT -eq 0 ]; then
        echo "git push ${review_remote} ${tag}"
        with_retries -d 45 -t 15 -k 5 5 git push ${review_remote} ${tag} ${DRY_RUN}
    else
        if [ "${access_token[${remote}]}" != "" ]; then
            echo "Trying remote '${remote}' with access token"
            git_set_push_url_with_access_token "${remote}" "${access_token[${remote}]}"
            if [ $? != 0 ]; then
                echo_stderr "ERROR: Failed to set url with access token for remote '${remote}' in  ${subgit}"
                exit 1
            fi
        fi
        echo "git push ${remote} ${tag}"
        with_retries -d 45 -t 15 -k 5 2 git push ${remote} ${tag} ${DRY_RUN}
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

    remote=$(git_remote)
    if [ "${remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine remote in ${new_manifest_dir}"
        exit 1
    fi

    review_remote=$(git_review_remote)
    if [ "${review_remote}" == "" ]; then
        echo_stderr "ERROR: Failed to determine review remote in ${new_manifest_dir}"
        exit 1
    fi

    echo "Pushing tag $tag in ${new_manifest_dir}"
    if [ "${review_method}" == "gerrit" ] && [ $BYPASS_GERRIT -eq 0 ]; then
        with_retries -d 45 -t 15 -k 5 5 git review
        if [ $? != 0 ] ; then
            echo_stderr "ERROR: Failed to create git review from ${new_manifest_dir}"
            exit 1
        fi
        echo "When review is merged: please run ..."
        echo "   cd ${new_manifest_dir}"
        echo "   git push ${review_remote} ${tag}"
    else
        echo "git push ${remote} ${local_branch}:${remote_branch}" && \
        with_retries -d 45 -t 15 -k 5 5 git push ${remote} ${local_branch}:${remote_branch} ${DRY_RUN} && \
        echo "git push ${remote} ${tag}:${tag}" && \
        with_retries -d 45 -t 15 -k 5 5 git push ${remote} ${tag}:${tag} ${DRY_RUN}
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push tag '${tag}' to branch ${remote_branch} on remote '${remote}' from ${new_manifest_dir}"
        exit 1
    fi
    ) || exit 1
fi
