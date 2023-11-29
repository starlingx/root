#!/bin/bash

#
# Copyright (c) 2020-2022 Wind River Systems, Inc.
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
    echo "push_branches_tags.sh --branch=<branch> [--tag=<tag>]"
    echo "                      [ --remotes=<remotes> ] [ --projects=<projects> ]"
    echo "                      [ --exclude-projects=<projects> ]"
    echo "                      [ --manifest [ --manifest-file=<file.xml> ] ]"
    echo "                      [ --bypass-gerrit] [--safe-gerrit-host=<host>]"
    echo "                      [ --access-token=<remote>:<token> ] [ --dry-run ]"
    echo ""
    echo "Push a pre-existing branch and tag into all listed projects, and all"
    echo "projects hosted by all listed remotes, minus excluded projects."
    echo "Lists are comma separated."
    echo ""
    echo "The branch name must be provided.  The tag name can also be provided."
    echo "If the tag is omitted, one is automativally generate by adding the"
    echo "prefix 'v' to the branch name."
    echo ""
    echo "A manifest push can also be requested.vision."
    echo ""
    echo "--manifest-file may be used to override the manifest file to be updated."
    echo ""
    echo "--safe-gerrit-host allows one to specify host names of gerrit servers"
    echo "that are safe to push reviews to."
    echo ""
    echo "--access-token can be used to supply an access token for direct (non-gerrit) push attempts"
    echo "               to specific remotes.   e.g. github now requires this"
    echo ""
    echo "--dry-run will print out git push commands without executing them"
}

TEMP=$(getopt -o h,n --long remotes:,projects:,exclude-projects:,branch:,tag:,bypass-gerrit,manifest,manifest-file:,safe-gerrit-host:,help,access-token:,dry-run -n 'push_branches_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    echo_stderr "ERROR: getopt failure"
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
BYPASS_GERRIT=0
DRY_RUN=
remotes=""
projects=""
excluded_projects=""
branch=""
tag=""
manifest=""
repo_root_dir=""
declare -A access_token

safe_gerrit_hosts=()
while true ; do
    case "$1" in
        -h|--help)        HELP=1 ; shift ;;
        -n|--dry-run)     DRY_RUN="--dry-run" ; DRY_RUN_CMD=":" ; shift ;;
        --bypass-gerrit)  BYPASS_GERRIT=1 ; shift ;;
        --remotes)        remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)       projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --exclude-projects)       excluded_projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --branch)         branch=$2; shift 2;;
        --tag)            tag=$2; shift 2;;
        --manifest)       MANIFEST=1 ; shift ;;
        --manifest-file)  repo_set_manifest_file "$2" ; shift 2;;
        --safe-gerrit-host) safe_gerrit_hosts+=("$2") ; shift 2;;
        --access-token)    val=$2
                           at_remote=$(echo "$val" | cut -d ':' -f 1)
                           at_token=$(echo "$val" | cut -d ':' -f 2)
                           if [ -z "$at_token" ]; then
                               usage
                               exit 1
                           fi
                           access_token["$at_remote"]="$at_token"
                           shift 2 ;;
        --)               shift ; break ;;
        *)                usage; exit 1 ;;
    esac
done
git_set_safe_gerrit_hosts "${safe_gerrit_hosts[@]}"

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

    if [ $BYPASS_GERRIT -eq 0 ]; then
        review_method=$(git_repo_review_method)
        if [ "${review_method}" == "" ]; then
            echo_stderr "ERROR: Failed to determine review method in ${subgit}"
            exit 1
        fi
    else
        review_method='default'
    fi

    remote=$(git_repo_remote)
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
        if [ "${host}" == "review.opendev.org" ] || git_match_safe_gerrit_host "${host}" ; then
            echo "git push ${review_remote} ${tag}" && \
            with_retries -d 45 -t 15 -k 5 5 git push $DRY_RUN ${review_remote} ${tag} && \
            echo "ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag}" && \
            $DRY_RUN_CMD ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag} && \
            echo "git config --local --replace-all branch.${branch}.merge refs/heads/${branch}" && \
            $DRY_RUN_CMD git config --local --replace-all "branch.${branch}.merge" refs/heads/${branch} && \
            echo "git review --topic=${branch/\//.}" && \
            $DRY_RUN_CMD with_retries -d 45 -t 15 -k 5 5 git review --topic="${branch/\//.}"
        else
            if [ "${access_token[${review_remote}]}" != "" ]; then
                git_set_push_url_with_access_token "${review_remote}" "${access_token[${review_remote}]}"
                if [ $? != 0 ]; then
                    echo_stderr "ERROR: Failed to set url with access token for remote '${review_remote}' in  ${subgit}"
                    exit 1
                fi
            fi
            echo "git push ${review_remote} ${branch}:${branch} $DRY_RUN" && \
            with_retries -d 45 -t 15 -k 5 5 git push ${review_remote} ${branch}:${branch} $DRY_RUN && \
            echo "git push ${review_remote} ${tag}:${tag} $DRY_RUN" && \
            with_retries -d 45 -t 15 -k 5 5 git push ${review_remote} ${tag}:${tag} $DRY_RUN
        fi
    else
        if [ "${access_token[${remote}]}" != "" ]; then
            git_set_push_url_with_access_token "${remote}" "${access_token[${remote}]}"
            if [ $? != 0 ]; then
                echo_stderr "ERROR: Failed to set url with access token for remote '${remote}' in  ${subgit}"
                exit 1
            fi
        fi
        echo "git push ${remote} ${branch}:${branch} $DRY_RUN" && \
        with_retries -d 45 -t 15 -k 5 5 git push ${remote} ${branch}:${branch} $DRY_RUN && \
        echo "git push ${remote} ${tag}:${tag} $DRY_RUN" && \
        with_retries -d 45 -t 15 -k 5 5 git push ${remote} ${tag}:${tag} $DRY_RUN
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
        if [ "${host}" == "review.opendev.org" ] || git_match_safe_gerrit_host "${host}" ; then
            echo "git push ${review_remote} ${tag}" && \
            with_retries -d 45 -t 15 -k 5 5 git push ${review_remote} ${tag} $DRY_RUN && \
            echo "ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag}" && \
            $DRY_RUN_CMD ssh -p ${port} ${host} gerrit create-branch ${path} ${branch} ${tag} && \
            echo "git config --local --replace-all branch.${branch}.merge refs/heads/${branch}" && \
            $DRY_RUN_CMD git config --local --replace-all "branch.${branch}.merge" refs/heads/${branch} && \
            echo "git review --yes --topic=${branch/\//.}" && \
            $DRY_RUN_CMD with_retries -d 45 -t 15 -k 5 5 git review --yes --topic="${branch/\//.}"
        else
            echo git push --tags ${review_remote} ${branch} $DRY_RUN && \
            with_retries -d 45 -t 15 -k 5 5 git push --tags ${review_remote} ${branch} $DRY_RUN
        fi
    else
        echo git push --tags --set-upstream ${review_remote} ${branch} $DRY_RUN && \
        with_retries -d 45 -t 15 -k 5 5 git push --tags --set-upstream ${review_remote} ${branch} $DRY_RUN
    fi

    if [ $? != 0 ] ; then
        echo_stderr "ERROR: Failed to push tag '${tag}' to remote '${review_remote}' in  ${manifest_dir}"
        exit 1
    fi
    ) || exit 1
fi
