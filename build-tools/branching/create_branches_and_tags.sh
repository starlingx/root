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
    echo "create_branches_and_tags.sh --branch=<branch> [--tag=<tag>] <options>"
    echo ""
    echo "    The branch name must be provided.  The tag name can also be provided."
    echo "    If the tag is omitted, one is automativally generate by adding the"
    echo "    prefix 'v' to the branch name."
    echo ""
    echo "selection options:"
    echo "    [ --remotes=<remotes> ] [ --projects=<projects> ]"
    echo ""
    echo "    Create a branch and a tag in all listed projects, and all"
    echo "    projects hosted by all listed remotes.  Lists are comma separated."
    echo ""
    echo "gitreview options:"
    echo "    Update any .gitreview files in branched projects."
    echo ""
    echo "    [ --gitreview-host <host> ]"
    echo "                                Set or update 'host' field."
    echo "    [ --gitreview-port <port> ]"
    echo "                                Set or update 'port' field."
    echo "    [ --gitreview-project ]"
    echo "                                Set or update 'project' field."
    echo "    [ --gitreview-default ]"
    echo "                                Set or update 'defaultbranch' field."
    echo "    [ --safe-gerrit-host=<host> ]"
    echo "                                allows one to specify host names of gerrit"
    echo "                                servers that are safe to push reviews to."
    echo ""
    echo "manifest options:"
    echo "    [ --manifest ]"
    echo "                        Modify the current repo manifest to specify the"
    echo "                        new branch for all select remotes and projects."
    echo "    [ --manifest-file=<file.xml> ]"
    echo "                        Override the manifest file to be updated."
    echo "    [ --hard-lock-down | --lockdown ]"
    echo "                        All unselected projects get the current HEAD's"
    echo "                        SHA set as the revision."
    echo "    [ --soft-lock-down ]"
    echo "                        All unselected projects with an revision that"
    echo "                        is unset, or 'master', get the current HEAD's sha"
    echo "                        set as the revision."
    echo "    [ --default-revision ]"
    echo "                        Set the default revision of the manifest."
    echo ""
}

TEMP=$(getopt -o h --long remotes:,projects:,branch:,tag:,manifest,manifest-file:,lock-down,hard-lock-down,soft-lock-down,default-revision,gitreview-default,gitreview-project,gitreview-host:,gitreview-port:,safe-gerrit-host:,help -n 'create_branches_and_tags.sh' -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
MANIFEST=0
LOCK_DOWN=0
GITREVIEW_DEFAULT=0
GITREVIEW_PROJECT=0
GITREVIEW_HOST=""
GITREVIEW_PORT=""
GITREVIEW_CHANGE=0
SET_DEFAULT_REVISION=0
remotes=""
projects=""
branch=""
tag=""
manifest=""
new_manifest=""
repo_root_dir=""

safe_gerrit_hosts=()
while true ; do
    case "$1" in
        -h|--help)           HELP=1 ; shift ;;
        --remotes)           remotes+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --projects)          projects+=$(echo "$2 " | tr ',' ' '); shift 2;;
        --branch)            branch=$2; shift 2;;
        --tag)               tag=$2; shift 2;;
        --manifest)          MANIFEST=1 ; shift ;;
        --manifest-file)     repo_set_manifest_file "$2" ; shift 2 ;;
        --lock-down)         LOCK_DOWN=2 ; shift ;;
        --hard-lock-down)    LOCK_DOWN=2 ; shift ;;
        --soft-lock-down)    LOCK_DOWN=1 ; shift ;;
        --default-revision)  SET_DEFAULT_REVISION=1 ; shift ;;
        --gitreview-default) GITREVIEW_DEFAULT=1 ; shift ;;
        --gitreview-project) GITREVIEW_PROJECT=1 ; shift ;;
        --gitreview-host)    GITREVIEW_HOST=$2 ; shift 2;;
        --gitreview-port)    GITREVIEW_PORT=$2 ; shift 2;;
        --safe-gerrit-host)  safe_gerrit_hosts+=("$2") ; shift 2 ;;
        --)                  shift ; break ;;
        *)                   echo "unknown option $1"; usage; exit 1 ;;
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

if  [ $GITREVIEW_DEFAULT -ne 0 ] ||
    [ $GITREVIEW_PROJECT -ne 0 ] ||
    [ "$GITREVIEW_HOST" != "" ] ||
    [ "$GITREVIEW_PORT" != "" ]; then
    GITREVIEW_CHANGE=1
fi

update_field () {
    local file=$1
    local field=$2
    local value=$3
    local changed=0

    if [ ! -f ${file} ]; then
        echo "File ${file} not found"
        return ${changed}
    fi

    if ! grep -q "^${field}=${value}$" ${file}; then
        echo "Updating ${field} in ${file}"
        if grep -q "^${field}=" ${file}; then
            sed "s#\(${field}=\).*#\1${value}#" -i ${file}
        else
            echo "${field}=${value}" >> ${file}
        fi
        changed=1
    else
        echo "${field} in ${file} already set"
    fi

    return ${changed}
}

update_gitreview () {
    local DIR=$1
    local need_rm=0
    local need_commit=0
    local message="Update .gitreview for ${branch}"
    local new_host=0
    (
    cd ${DIR} || exit 1

    if [ ${GITREVIEW_CHANGE} -eq 1 ] && [ -f .gitreview ]; then
        if [ "${GITREVIEW_HOST}" != "" ]; then
            update_field ${PWD}/.gitreview host ${GITREVIEW_HOST} || need_commit=1
            if [ ${need_commit} -ne 0 ]; then
                new_host=1
            fi
        fi

        if [ "${GITREVIEW_PORT}" != "" ]; then
            update_field ${PWD}/.gitreview port ${GITREVIEW_PORT} || need_commit=1
        fi

        if [ ${GITREVIEW_PROJECT} -eq 1 ]; then
            remote_url=$(git_repo_remote_url)
            pull_url=${remote_url}
            path=$(url_path ${pull_url})
            project=${path%.git}
            update_field ${PWD}/.gitreview project ${project} || need_commit=1
        fi

        if [ ${GITREVIEW_DEFAULT} -eq 1 ]; then
            update_field ${PWD}/.gitreview defaultbranch ${branch} || need_commit=1
        fi

        if [ $need_commit -eq 1 ]; then
            review_method=$(git_repo_review_method)
            if [ "${review_method}" == "gerrit" ] ; then
                timeout 15 git review -s
                if [ $? != 0 ] ; then
                    if [ ${new_host} -eq 0 ]; then
                        echo_stderr "ERROR: failed to setup git review in ${DIR}"
                        exit 1
                    fi

                    need_rm=1
                    message="Delete .gitreview for ${branch}"
                fi
            else
                need_rm=1
                message="Delete .gitreview for ${branch}"
            fi

            if [ ${need_rm} -eq 1 ]; then
                git rm -f .gitreview
                if [ $? != 0 ] ; then
                    echo_stderr "ERROR: failed to add .gitreview in ${DIR}"
                    exit 1
                fi
            else
                git add .gitreview
                if [ $? != 0 ] ; then
                    echo_stderr "ERROR: failed to add .gitreview in ${DIR}"
                    exit 1
                fi
            fi

            git commit -s -m "${message}"
            if [ $? != 0 ] ; then
                echo_stderr "ERROR: failed to commit .gitreview in ${DIR}"
                exit 1
            fi
        fi
    fi
    )
}

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

    # String the leading feature (f/) or branch (b/) from the
    # branch name so we have a valid branch prefix
    if [[ ${branch} =~ .*/.*$ ]]; then
        manifest_prefix="$(basename ${branch})"
    else
        manifest_prefix="${branch}"
    fi

    new_manifest="$(dirname $manifest)/$manifest_prefix-$(basename $manifest)"
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

# Go through all subgits and create the branch and tag if it does not already exist
echo "Applying branches and tags"
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

    # check if destination tag already exists
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

    update_gitreview ${subgit} || exit 1
    ) || exit 1
done
) || exit 1

if [ $MANIFEST -eq 1 ]; then
    (
    echo "Starting manifest update"

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

    update_gitreview ${manifest_dir} || exit 1

    echo "Creating manifest ${new_manifest_name}"
    manifest_set_revision "${manifest}" "${new_manifest}" "$branch" ${LOCK_DOWN} ${SET_DEFAULT_REVISION} $projects || exit 1

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
