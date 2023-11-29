#!/bin/bash

#
# Copyright (c) 2020-2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A collection of utilities that stradle the divide between
# between git and repo. 
#
# These utilites are often the most reliable to use. They will
# try to get the answer from repo, and will fall back to git
# of repo isn't providing a satisfactory answer.  A prime example
# is the repo's manifest, which isn't fully managed by repo,
# but isn't a fully independent git either.
#


GIT_REPO_UTILS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source ${GIT_REPO_UTILS_DIR}/repo-utils.sh
source ${GIT_REPO_UTILS_DIR}/git-utils.sh
source ${GIT_REPO_UTILS_DIR}/url_utils.sh

git_remote_fp="git_repo_remote"


#
# git_repo_rel_dir [<dir>]:
#      Return the relative directory of a git within a repo.
#
git_repo_rel_dir () {
    local DIR="${1:-${PWD}}"

    local GIT_DIR=""
    local REPO_DIR=""
    local GIT_RELATIVE_DIR=""

    GIT_DIR=$(git_root ${DIR})
    REPO_DIR=$(readlink -f $(repo_root ${DIR}))
    GIT_RELATIVE_DIR=${GIT_DIR#${REPO_DIR}/}
    echo ${GIT_RELATIVE_DIR}
}

#
# git_repo_project [<dir>]:
#      Return the repo 'project' of a git.
#

git_repo_project() {
    local DIR="${1:-${PWD}}"

    (
    cd ${DIR}

    GIT_RELATIVE_DIR=$(git_repo_rel_dir)
    repo forall -c "if [ \$REPO_PATH = ${GIT_RELATIVE_DIR} ]; then echo \$REPO_PROJECT; fi"
    )
}

#
# git_repo_remote [<dir>]:
#      Return the repo 'remote' of a git.
#

git_repo_remote() {
    local DIR="${1:-${PWD}}"

    (
    cd ${DIR}

    GIT_RELATIVE_DIR=$(git_repo_rel_dir)
    repo forall -c "if [ \$REPO_PATH = ${GIT_RELATIVE_DIR} ]; then echo \$REPO_REMOTE; fi"
    )
}


#
# git_repo_remote_branch [<dir>]:
#      Return the repo 'remote branch' of a git.
#

git_repo_remote_branch() {
    local DIR="${1:-${PWD}}"

    (
    cd ${DIR}

    GIT_RELATIVE_DIR=$(git_repo_rel_dir)
    REF=$(repo forall -c "if [ \$REPO_PATH = ${GIT_RELATIVE_DIR} ]; then echo \$REPO_RREV; fi")
    if git_is_branch ${REF} ; then
        echo ${REF}
    else
        return 1
    fi
    )
}

#
# git_repo_remote_ref [<dir>]:
#      Return the repo 'remote branch' of a git.
#

git_repo_remote_ref() {
    local DIR="${1:-${PWD}}"

    (
    cd ${DIR}

    GIT_RELATIVE_DIR=$(git_repo_rel_dir)
    repo forall -c "if [ \$REPO_PATH = ${GIT_RELATIVE_DIR} ]; then echo \$REPO_RREV; fi"
    )
}

git_repo_remote_url () {
    local remote=""
    remote=$(git_repo_remote) || return 1
    git config remote.$remote.url
}

git_repo_review_method () {
    local DIR="${1:-${PWD}}"
    local GIT_DIR=""
    local remote_url=""
    local review_host=""
    local remote_host=""

    GIT_DIR=$(git_root ${DIR}) || return 1

    if [ ! -f ${GIT_DIR}/.gitreview ]; then
        # No .gitreview file
        echo 'default'
        return 0
    fi

    if ! grep -q '\[gerrit\]' ${GIT_DIR}/.gitreview; then
        # .gitreview file has no gerrit entry
        echo 'default'
        return 0
    fi

    review_host="$(grep host= ${GIT_DIR}/.gitreview | sed 's#^host=##' | head -n 1)"
    remote_url="$(git_repo_remote_url)" || return 1
    remote_host="$(url_to_host "${remote_url}")"
    if [ "${review_host}" == "{remote_host}" ]; then
        # Will review against same host as we pulled from.  All is well
        echo 'gerrit'
        return 0
    else
        review_domain="$(host_to_domain "${review_host}")"
        remote_domain="$(host_to_domain "${remote_host}")"
        if [ "${review_domain}" == "${remote_domain}" ]; then
            # Will review and remote hosts share a commom domain.  Close enough

            echo 'gerrit'
            return 0
        else
            # review host is one of the globally-configured hosts that
            # we know are safe
            if git_match_safe_gerrit_host "${review_host}" ; then
                echo 'gerrit'
                return 0
            fi

            # Domains don't match.  Not close enough to say gerrit is safe.
            # Did someone forget to update .gitreview?
            # Are we not pulling from the authoritative source?

            echo 'default'
            return 0
        fi
    fi

    # Shouldn't get here
    return 1
}

git_repo_review_remote () {
    local method=""
    method=$(git_repo_review_method)
    if [ "${method}" == "gerrit" ]; then
        git config remote.gerrit.url > /dev/null
        if [ $? -ne 0 ]; then
            # Perhaps we need to run git review -s' and try again

            with_retries -d 45 -t 15 -k 5 5 git review -s >&2 || return 1
            git config remote.gerrit.url > /dev/null || return 1
        fi
        echo "gerrit"
    else
        git_repo_remote
    fi
}

git_repo_review_url () {
    local DIR="${1:-${PWD}}"
    local GIT_DIR=""

    GIT_DIR=$(git_root ${DIR})

    local method=""
    method=$(git_repo_review_method)
    if [ "${method}" == "gerrit" ]; then
        git config remote.gerrit.url
        if [ $? -ne 0 ]; then
            # Perhaps we need to run git review -s' and try again

            with_retries -d 45 -t 15 -k 5 5 git review -s >&2 || return 1
            git config remote.gerrit.url || return 1
        fi
    else
        return 1
    fi
}

