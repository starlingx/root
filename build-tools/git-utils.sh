#!/bin/bash

#
# Copyright (c) 2018-2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# A place for any functions relating to git, or the git hierarchy created
# by repo manifests.
#

GIT_UTILS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

source ${GIT_UTILS_DIR}/utils.sh


git_ctx_root_dir () {
    dirname "${MY_REPO}"
}

#
# git_list <dir>:
#      Return a list of git root directories found under <dir>
#
git_list () {
    local DIR=${1}

    find -L "${DIR}" -maxdepth 5 -type d -name '.git' -exec dirname {} \; | grep -v '[.]repo[/]repo$' | sort -V
}


# GIT_LIST: A list of root directories for all the gits under $MY_REPO/..
#           as absolute paths.
export GIT_LIST=$(git_list "$(git_ctx_root_dir)")


# GIT_LIST_REL: A list of root directories for all the gits under $MY_REPO/..
#               as relative paths.
export GIT_LIST_REL=$(for p in $GIT_LIST; do echo .${p#$(git_ctx_root_dir)}; done)

#
# git_list_containing_branch <dir> <branch>:
#      Return a list of git root directories found under <dir> and
#      having branch <branch>.  The branch need not be current branch.
#

git_list_containing_branch () {
    local DIR="${1}"
    local BRANCH="${2}"

    local d
    for d in $(git_list "${DIR}"); do
        (
        cd "$d"
        git branch --all | grep -q "$BRANCH"
        if [ $? -eq 0 ]; then
            echo "$d"
        fi
        )
    done
}


#
# git_list_containing_tag <dir> <tag>:
#      Return a list of git root directories found under <dir> and
#      having tag <tag>.
#

git_list_containing_tag () {
    local DIR="${1}"
    local TAG="${2}"

    local d
    for d in $(git_list "${DIR}"); do
        (
        cd "$d"
        git tag | grep -q "$TAG"
        if [ $? -eq 0 ]; then
            echo "$d"
        fi
        )
    done
}


#
# git_root [<dir>]:
#      Return the root directory of a git
#      Note: symlinks are fully expanded.
#

git_root () {
    local DIR="${1:-${PWD}}"

    if [ ! -d "${DIR}" ]; then
        DIR="$(dirname "${DIR}")"
    fi
    if [ ! -d "${DIR}" ]; then
        echo_stderr "No such directory: ${DIR}"
        return 1
    fi

        (
        cd "${DIR}"
        ROOT_DIR="$(git rev-parse --show-toplevel)" || exit 1
        readlink -f "${ROOT_DIR}"
        )
}

#
# git_list_tags [<dir>]:
#      Return a list of all git tags.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_list_tags () {
    local DIR="${1:-${PWD}}"

    (
    cd "$DIR"
    git tag
    )
}


#
# git_list_branches [<dir>]:
#      Return a list of all git branches.
#      Non-local branches will be prefixed by 'remote/<remote-name>'
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_list_branches () {
    local DIR="${1:-${PWD}}"

    (
    cd "$DIR"
    git branch --list --all | sed 's#^..##'
    )
}


#
# git_list_remote_branches <remote> [<dir>]:
#      Return a list of all git branches defined for <remote>.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_list_remote_branches () {
    local REMOTE="${1}"
    local DIR="${2:-${PWD}}"

    (
    cd "$DIR"
    git branch --list --all "${REMOTE}/*" | sed "s#^.*/${REMOTE}/##"
    )
}


#
# git_is_tag <tag> [<dir>]:
#      Test if a <tag> is defined within a git.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_tag () {
    local TAG="${1}"
    local DIR="${2:-${PWD}}"

    # remove a trailing ^0 if present
    TAG=${TAG%^0}

    if [ "$TAG" == "" ]; then
        return 1;
    fi

    (
    cd "$DIR"
    git show-ref ${TAG} | cut -d ' ' -f 2 | grep -q '^refs/tags/'
    )
}


#
# git_is_local_branch <branch> [<dir>]:
#      Test if a <branch> is defined locally within a git.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_local_branch () {
    local BRANCH="${1}"
    local DIR="${2:-${PWD}}"

    if [ "$BRANCH" == "" ]; then
        return 1;
    fi

    (
    cd "$DIR"
    git show-ref ${BRANCH} | cut -d ' ' -f 2 | grep -q '^refs/heads/'
    )
}


#
# git_is_remote_branch <branch> [<dir>]:
#      Test if a <branch> is defined in any of the remotes of the git.
#      The branche does NOT need to be prefixed by the remore name.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_remote_branch () {
    local BRANCH="${1}"
    local DIR="${2:-${PWD}}"

    if [ "$BRANCH" == "" ]; then
        return 1;
    fi

    (
    cd "$DIR"
    git show-ref ${BRANCH} | cut -d ' ' -f 2 | grep -q '^refs/remotes/'
    )
}


#
# git_is_branch <branch> [<dir>]:
#      Test if a <branch> is defined in the git.
#      The branch can be local or remote.
#      Remote branches do NOT need to be prefixed by the remore name.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_branch () {
    local BRANCH="${1}"
    local DIR="${2:-${PWD}}"

    if [ "$BRANCH" == "" ]; then
        return 1;
    fi

    git_is_local_branch ${BRANCH} "${DIR}" || git_is_remote_branch ${BRANCH} "${DIR}"
}


#
# git_is_ref <ref> [<dir>]:
#      Test if a <ref> is a valid name for a commit.
#      The reference can be a sha, tag, or branch.
#      Remote branches must be prefixed by the remore name,
#      as in <remote-name>/<branch> .
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_ref () {
    local REF="${1}"
    local DIR="${2:-${PWD}}"

    if [ "$REF" == "" ]; then
        return 1;
    fi

    # test "$(git cat-file -t ${REF})" == "commit"
    local TYPE=""
    TYPE="$(git cat-file -t ${REF} 2> /dev/null)" && test "${TYPE}" == "commit"
}


#
# git_is_sha <sha> [<dir>]:
#      Test if a <sha> is defined in the git.  The sha can be abreviated.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.
#

git_is_sha () {
    local SHA="${1}"
    local DIR="${2:-${PWD}}"

    if [ "$SHA" == "" ]; then
        return 1;
    fi

    git_is_ref ${SHA} "${DIR}" && ! ( git_is_branch ${SHA} "${DIR}" || git_is_tag ${SHA} "${DIR}")
}


#
# git_ref_type <ref> [<dir>]:
#      Determine the type of the git reference <ref>.
#      The result, via stdout,  will be one of ("sha", "tag", "branch" or "invalid")
#      Remote branches do NOT need to be prefixed by the remore name.
#      Either specify a directory <dir> in the git,
#      or use current directory if unspecified.

git_ref_type () {
    local REF="${1}"
    local DIR="${2:-${PWD}}"

    if git_is_branch ${REF} ${DIR}; then
        echo 'branch'
        return 0
    fi
    if git_is_tag ${REF} ${DIR}; then
        echo 'tag'
        return 0
    fi
    if git_is_sha ${REF} ${DIR}; then
        echo 'sha'
        return 0
    fi
    echo 'invalid'
    return 1
}

#
#
# git_context:
#     Returns a bash script that can be used to recreate the current git context,
#
# Note: all paths are relative to $MY_REPO/..
#

git_context () {
    (
    cd $(git_ctx_root_dir)

    local d
    for d in $GIT_LIST_REL; do
        (
        cd ${d}
        echo -n "(cd ${d} && git checkout -f "
        echo "$(git rev-list HEAD -1))"
        )
    done
    )
}

#
# git_test_context <context>:
#
# Test if all commits referenced in the context are present
# in the history of the gits in their current checkout state.
#
# Returns: 0 = context is present in git history
#          1 = At least one element of context is not present
#          2 = error
#
git_test_context () {
    local context="$1"
    local query=""
    local target_hits=0
    local actual_hits=0

    if [ ! -f "$context" ]; then
        return 2
    fi

    query=$(mktemp "/tmp/git_test_context_XXXXXX")
    if [ "$query" == "" ]; then
        return 2
    fi

    # Transform a checkout context into a query that prints
    # all the commits that are found in the git history.
    #
    # Limit search to last 500 commits in the interest of speed.
    # I don't expect to be using contexts more than a few weeks old.
    cat "$context" | \
        sed -e "/\.repo\/repo/d" \
            -e "s#checkout -f \([a-e0-9]*\)#rev-list --max-count=500 HEAD | \
        grep \1#" > $query

    target_hits=$(cat "$context" | grep -v '[.]repo[/]repo ' | wc -l)
    actual_hits=$(cd $(git_ctx_root_dir); source $query 2> /dev/null | wc -l)
    \rm $query

    if [ $actual_hits -eq $target_hits ]; then
        return 0
    fi

    return 1
}

git_local_branch () {
    local result=""
    local sha=""

    # Older gits don't support this
    result=$(git branch  --show-current 2> /dev/null)
    if [ "$result" != "" ]; then
        echo $result
        return 0
    fi

    # Might not work if detached and there are local commits
    result=$(git branch | grep '^[*] ' | cut -b 3- | grep -v HEAD)
    if [ "$result" != "" ]; then
        echo $result
        return 0
    fi

    # Find 'nearest' local branch that we detached from and/or added commits to
    sha=$(git rev-parse HEAD)
    while [ $? -eq 0 ]; do
        result=$(git show-ref --head | grep -e "$sha refs/heads/" | sed "s#$sha refs/heads/##" | head -n 1)
        if [ "$result" != "" ]; then
            echo $result
            return 0
        fi

        sha=$(git rev-parse $sha^ 2> /dev/null)
    done

    # This used to work on older git versions
    result=$(git name-rev --name-only HEAD)
    if [ "$result" == "" ] || [ "$result" == "undefined" ]; then
        return 1
    fi

    # Handle the case where a tag is returned by looking at the parent.
    # This weird case when a local commit is tagged and we were in
    # detached head state, or on 'default' branch.
    while git_is_tag $result; do
        result=$(git name-rev --name-only $result^1 )
        if [ "$result" == "" ] || [ "$result" == "undefined" ]; then
            return 1
        fi
    done

    echo $result
}

git_list_remotes () {
    git remote | grep -v gerrit
}

git_remote () {
    local DIR="${1:-${PWD}}"

    (
    cd ${DIR}
    local_branch=$(git_local_branch) || return 1

    # Return remote of current local branch, else default remote.
    git config branch.${local_branch}.remote || git_list_remotes
    )
}

git_remote_url () {
    local remote=""
    remote=$(git_remote) || return 1
    git config remote.$remote.url
}

git_remote_branch () {
    local local_branch=""
    local sha=""
    local remote=""

    # Our best bet is if the git config shows the local
    # branch is tracking a remote branch.
    local_branch=$(git_local_branch) || return 1
    git config branch.${local_branch}.merge | sed 's#^refs/heads/##'
    if [ ${PIPESTATUS[0]} -eq 0  ]; then
        return 0
    fi

    # Before we can select a remote branch, we need to know which remote.
    remote=$(git_remote)
    if [ $? -ne 0 ] || [ "$remote" == "" ]; then
        return 1
    fi

    # Find 'nearest' remote branch that we detached from and/or added commits to
    sha=$(git rev-parse HEAD)
    while [ $? -eq 0 ]; do
        result=$(git show-ref --head | grep -e "$sha refs/remotes/$remote/" | sed "s#$sha refs/remotes/$remote/##" | head -n 1)
        if [ "$result" != "" ]; then
            echo $result
            return 0
        fi

        sha=$(git rev-parse $sha^ 2> /dev/null)
    done
    return 1
}

# Usage: git_set_safe_gerrit_hosts HOST1 HOST2...
# Set the host names that are safe to push reviews to
GIT_SAFE_GERRIT_HOSTS=()
git_set_safe_gerrit_hosts() {
    GIT_SAFE_GERRIT_HOSTS=()
    while [ "$#" -gt 0 ] ; do
        GIT_SAFE_GERRIT_HOSTS+=("$1")
        shift
    done
}

# Usage: git_match_safe_gerrit_host HOSTNAME
# Return true if given host name is safe to push reviews to
# You have to call git_set_safe_gerrit_hosts() first
git_match_safe_gerrit_host() {
    local review_host="$1"
    local host
    for host in "${GIT_SAFE_GERRIT_HOSTS[@]}" ; do
        if [ "${review_host}" == "${host}" ]; then
            return 0
        fi
    done
    return 1
}

git_review_method () {
    local GIT_DIR
    local url="" host=""
    url=$(git_remote_url) || exit 1
    if [[ "${url}" =~ "/git.starlingx.io/" || "${url}" =~ "/opendev.org/" ]]; then
        echo 'gerrit'
        return 0
    fi

    GIT_DIR=$(git_root ${PWD}) || return 1

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
    if git_match_safe_gerrit_host "${review_host}" ; then
        echo "gerrit"
        return 0
    fi
    echo "default"

}

git_review_url () {
    local method=""
    method=$(git_review_method)
    if [ "${method}" == "gerrit" ]; then
        git config remote.gerrit.url
        if [ $? -ne 0 ]; then
            # Perhaps we need to run git review -s' and try again
            with_retries -d 45 -t 15 -k 5 5 git review -s >&2 || return 1
            git config remote.gerrit.url
        fi
    else
        git_remote_url
    fi
}

git_review_remote () {
    local method=""
    method=$(git_review_method)
    if [ "${method}" == "gerrit" ]; then
        git config remote.gerrit.url > /dev/null
        if [ $? -ne 0 ]; then
            # Perhaps we need to run git review -s' and try again
            with_retries -d 45 -t 15 -k 5 5 git review -s >&2 || return 1
            git config remote.gerrit.url > /dev/null || return 1
        fi
        echo "gerrit"
    else
        git_remote
    fi
}
