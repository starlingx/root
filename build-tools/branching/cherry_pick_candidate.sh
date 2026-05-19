#!/bin/bash

CHERRY_PICK_CANDIDATE_SH_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"
source ${CHERRY_PICK_CANDIDATE_SH_DIR}/cgcs-root/build-tools/git-repo-utils.sh

usage() {
    echo "cherry_pick_candidate.sh --from-branch=<branch> [<optional-arguments>]"
    echo ""
    echo "Scan a git, comparing the current branch versus the from branch."
    echo "Commits in the from branch that are not in the current branch are listed,"
    echo "as candidates for cherry-picking.  Merge commits, and the initial commit"
    echo "to set-up the .gitreview file are ignored."
    echo ""
    echo "Required:"
    echo "--from-branch    branch to scann for missing updates."
    echo ""
    echo "Optional:"
    echo "--git-dir=<dir>       Location of the git.  defaults to current directory."
    echo "--exclue-file=<path>  A file listing commit SHA that should not be cherry-picked."
    echo ""
    echo "Additional optional arguements to specify an alternative remote as the"
    echo "source of the 'from' branch.:"
    echo "--from-remote=<remote-name>   A name to give to the alternative remote."
    echo "--from-url=<url>              The url of the alternative remote, excluding"
    echo "                                 final project/git name."
    echo "--from-project=<name>         The project/git name of the alternative remote."
    echo ""
    echo "e.g."
    echo "TO_BRANCH=master"
    echo "FROM_BRANCH=f/caracal"
    echo "MANIFEST=stx-openstack.xml"
    echo "repo init -u https://opendev.org/starlingx/manifest -b \${TO_BRANCH} -m \${MANIFEST}"
    echo "repo sync -f --force-sync -j8"
    echo "export PATH=\$PWD/cgcs-root/build-tools/branching:\$PATH"
    echo "repo forall -c 'if git branch --all | grep -q \${REPO_REMOTE}\\/\${FROM_BRANCH}; then cherry_pick_candidate.sh --git-dir=\${PWD} --from-branch=\${FROM_BRANCH} ; fi'"
}

die() {
    echo "$@" >&2
    exit 1
}

TEMP=$(getopt -o h --long from-branch:,git-dir:,from-project:,from-url:,from-remote:,exclue-file:,help -n 'cherry_pick_candidate.sh' -- "$@")
if [ $? -ne 0 ]; then
    echo_stderr "ERROR: getopt failure"
    usage
    exit 1
fi
eval set -- "$TEMP"

HELP=0
GIT_DIR="${PWD}"
FROM_URL=""
FROM_PROJECT=""
FROM_REMOTE=""
FROM_BRANCH=""
EXCLUDE_FILE=""

while true ; do
    case "$1" in
        -h|--help)           HELP=1 ; shift ;;
        --from-branch)       FROM_BRANCH="$2"; shift 2;;
        --git-dir)           GIT_DIR="$2"; shift 2;;
        --from-project)      FROM_PROJECT="$2"; shift 2;;
        --from-url)          FROM_URL="$2"; shift 2;;
        --from-remote)       FROM_REMOTE="$2"; shift 2;;
        --exclue-file)       EXCLUDE_FILE="$2"; shift 2;;
        --)                  shift ; break ;;
        *)                   echo "unknown option $1"; usage; exit 1 ;;
    esac
done

if [ $HELP -eq 1 ]; then
    usage
    exit 0
fi

cherry_pick_candidate() {
    local DIR=$1
    local FROM_URL=$2
    local FROM_PROJECT=$3
    local FROM_REMOTE=$4
    local FROM_BRANCH=$5
    local EXCLUDE_FILE=$6

    local COMMON_ANCESTOR=""
    local COUNT=0
    local FOUND=0
    local FROM_LIST=""
    local FROM_SHA=""
    local FROM_SUBJECT=""
    local GIT_REVIEW=""
    local LST_DIR=""
    local MERGE=""
    local REL_DIR=""
    local REPO_ROOT_DIR=""
    local TO_BRANCH=""
    local TO_LIST=""
    local TO_PROJECT=""
    local TO_REMOTE=""
    local TO_SHA=""
    local TO_SUBJECT=""

    echo ""
    cd ${DIR} || exit 1
    REPO_ROOT_DIR=$(repo_root);
    TO_PROJECT=$(git_repo_project);
    REL_DIR=$(echo $DIR | sed "s#^${REPO_ROOT_DIR}/##")

    TO_REMOTE=$(git_repo_remote 2> /dev/null)
    if [ "${TO_REMOTE}" == "" ]; then
         echo "Failed to determine TO_REMOTE: ${DIR}"
         return 1
    fi

    TO_BRANCH=$(git_repo_remote_branch 2> /dev/null)
    if [ "${TO_BRANCH}" == "" ]; then
         echo "Failed to determine TO_BRANCH: ${DIR}"
         return 1
    fi

    if [ "${FROM_REMOTE}" == "" ]; then
        FROM_REMOTE="${TO_REMOTE}"
    fi

    if [ "${FROM_PROJECT}" == "" ]; then
        FROM_PROJECT="${TO_PROJECT}"
    fi

    if [ "${FROM_REMOTE}" != "${TO_REMOTE}" ]; then
        git remote remove  ${FROM_REMOTE} 2> /dev/null || true
        git remote add ${FROM_REMOTE} ${FROM_URL}/${FROM_PROJECT}
        if [ $? -ne 0 ]; then
            echo "Git remote add failed: ${DIR}: ${FROM_REMOTE} ${FROM_URL}/${FROM_PROJECT}"
            return 1
        fi
    fi

    git fetch ${FROM_REMOTE} ${FROM_BRANCH} > /dev/null 2> /dev/null
    if [ $? -ne 0 ]; then
         echo "Git fetch failed: ${DIR}: ${FROM_REMOTE} ${FROM_BRANCH}"
         return 1
    fi

    LST_DIR=$(mktemp -d commit_list_${FROM_PROJECT}.XXXXXX)
    FROM_LIST="${LST_DIR}/${FROM_PROJECT}.from.lst"
    TO_LIST="${LST_DIR}/${TO_PROJECT}.to.lst"
    COMMON_ANCESTOR=$(git merge-base HEAD ${FROM_REMOTE}/${FROM_BRANCH})
    git log --pretty=format:"%H%n" ${COMMON_ANCESTOR}..${FROM_REMOTE}/${FROM_BRANCH} | grep -v "^$" > ${FROM_LIST} || true
    git log --pretty=format:"%H%n" ${COMMON_ANCESTOR}..HEAD | grep -v "^$" > ${TO_LIST} || true
    for FROM_SHA in $(tac ${FROM_LIST}); do
        FOUND=0

        if [ ! -z "${EXCLUDE_FILE}" ] && [ -f "${EXCLUDE_FILE}" ] && grep -q "${FROM_SHA}" "${EXCLUDE_FILE}"; then
            # echo "Skip black listed: ${FROM_SHA}:${FROM_SUBJECT}"
            continue
        fi

        FROM_SUBJECT=$(git log --pretty=format:"%s" ${FROM_SHA}^..${FROM_SHA})
        MERGE=$(echo "${FROM_SUBJECT}" | grep '^Merge "' || true)
        if [ "${MERGE}" != "" ]; then
            # echo "Skip merge: ${FROM_SHA}:${FROM_SUBJECT}"
            continue
        fi

        GIT_REVIEW=$(echo "${FROM_SUBJECT}" | grep '^Update .gitreview for' || true)
        if [ "${GIT_REVIEW}" != "" ]; then
            # echo "Skip merge: ${FROM_SHA}:${FROM_SUBJECT}"
            continue
        fi

        for TO_SHA in $(tac ${TO_LIST}); do
            TO_SUBJECT=$(git log --pretty=format:"%s" ${TO_SHA}^..${TO_SHA})
            if [ "${FROM_SUBJECT}" == "${TO_SUBJECT}" ]; then
                FOUND=1
                break
            fi
        done

        if [ ${FOUND} -eq 1 ]; then
            # echo "Skip found ${FROM_SHA}:${FROM_SUBJECT}"
            continue
        fi

        echo "${REL_DIR}: ${FROM_SHA}:${FROM_SUBJECT}"
    done
}

cherry_pick_candidate "${GIT_DIR}" "${FROM_URL}" "${FROM_PROJECT}" "${FROM_REMOTE}" "${FROM_BRANCH}" "${EXCLUDE_FILE}"
