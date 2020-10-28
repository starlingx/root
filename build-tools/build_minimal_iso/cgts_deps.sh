#!/bin/env bash

#
# Copyright (c) 2018-2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

CGTS_DEPS_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" )" )"

# Set REPOQUERY, REPOQUERY_SUB_COMMAND, REPOQUERY_RESOLVE and 
# REPOQUERY_WHATPROVIDES_DELIM for our build environment.
source ${CGTS_DEPS_DIR}/../pkg-manager-utils.sh

function generate_dep_list {
    TMP_RPM_DB=$(mktemp -d $(pwd)/tmp_rpm_db_XXXXXX)
    mkdir -p $TMP_RPM_DB
    rpm --initdb --dbpath $TMP_RPM_DB
    rpm --dbpath $TMP_RPM_DB --test -Uvh --replacefiles '*.rpm' > $DEPLISTFILE_NEW 2>&1
    cat $DEPLISTFILE_NEW >> $DEPDETAILLISTFILE
    cat $DEPLISTFILE_NEW \
        | grep -v   -e "error:" -e "warning:" -e "Preparing..." \
                    -e "Verifying..." -e "installing package" \
        | sed -e "s/ is needed by.*$//" -e "s/ >=.*$//" \
        | sort -u > $DEPLISTFILE
    \rm -rf $TMP_RPM_DB
}

function install_deps {
    local DEP_LIST=""
    local DEP_LIST_FILE="$1"

    rm -f $TMPFILE

    while read DEP
    do
        DEP_LIST="${DEP_LIST}${REPOQUERY_WHATPROVIDES_DELIM}${DEP}"
    done < $DEP_LIST_FILE

    DEP_LIST=$(echo "$DEP_LIST" | sed "s/^${REPOQUERY_WHATPROVIDES_DELIM}//g")

    echo "Debug: List of deps to resolve: ${DEP_LIST}"

    if [ -z "${DEP_LIST}" ]; then
        return 0
    fi

    # go through each repo and convert deps to packages
    for REPOID in `grep  '^[[].*[]]$' $YUM | grep -v '[[]main[]]' | awk -F '[][]' '{print $2 }'`; do
        echo "TMPDIR=${TMP_DIR}"\
             "${REPOQUERY} --config=${YUM} --repoid=${REPOID}"\
             "${REPOQUERY_SUB_COMMAND} --arch=x86_64,noarch"\
             "--qf='%{name}' --whatprovides ${DEP_LIST}"
        TMPDIR=${TMP_DIR} \
            ${REPOQUERY} --config=${YUM} --repoid=${REPOID} \
            ${REPOQUERY_SUB_COMMAND} --arch=x86_64,noarch \
            --qf='%{name}' --whatprovides ${DEP_LIST} \
            | sed "s/kernel-debug/kernel/g" >> $TMPFILE
        \rm -rf $TMP_DIR/yum-$USER-*
    done
    sort $TMPFILE -u > $TMPFILE1
    rm $TMPFILE

    DEP_LIST=""
    while read DEP
    do
        DEP_LIST="${DEP_LIST} ${DEP}"
    done < $TMPFILE1
    rm $TMPFILE1

    # next go through each repo and install packages
    local TARGETS=${DEP_LIST}
    echo "Debug: Resolved list of deps to install: ${TARGETS}"
    local UNRESOLVED
    for REPOID in `grep  '^[[].*[]]$' $YUM | grep -v '[[]main[]]' | awk -F '[][]' '{print $2 }'`; do
        UNRESOLVED=" $TARGETS "

        if [[ ! -z "${TARGETS// }" ]]; then
            REPO_PATH=$(cat $YUM | sed -n "/^\[$REPOID\]\$/,\$p" | grep '^baseurl=' | head -n 1 | awk -F 'file://' '{print $2}' | sed 's:/$::')
            >&2 echo "TMPDIR=${TMP_DIR}"\
                    "${REPOQUERY} --config=${YUM} --repoid=${REPOID}"\
                    "${REPOQUERY_SUB_COMMAND} --arch=x86_64,noarch"\
                    "--qf='%{name} %{name}-%{version}-%{release}.%{arch}.rpm %{relativepath}'"\
                    "${REPOQUERY_RESOLVE} ${TARGETS}"
            TMPDIR=${TMP_DIR} \
                ${REPOQUERY} --config=${YUM} --repoid=${REPOID} \
                ${REPOQUERY_SUB_COMMAND} --arch=x86_64,noarch \
                --qf="%{name} %{name}-%{version}-%{release}.%{arch}.rpm %{relativepath}" \
                ${REPOQUERY_RESOLVE} ${TARGETS} \
                | sort -r -V >> $TMPFILE

            \rm -rf $TMP_DIR/yum-$USER-*

            while read STR
            do
                >&2 echo "STR=$STR"
                if [ "x$STR" == "x" ]; then
                    continue
                fi

                PKG=`echo $STR | cut -d " " -f 1`
                PKG_FILE=`echo $STR | cut -d " " -f 2`
                PKG_REL_PATH=`echo $STR | cut -d " " -f 3`
                PKG_PATH="${REPO_PATH}/${PKG_REL_PATH}"

                >&2 echo "Installing PKG=$PKG PKG_FILE=$PKG_FILE PKG_REL_PATH=$PKG_REL_PATH PKG_PATH=$PKG_PATH from repo $REPOID"
                cp $PKG_PATH .
                if [ $? -ne 0 ]; then
                    >&2 echo "  Here's what I have to work with..."
                    >&2 echo "  TMPDIR=${TMP_DIR}"\
                            "${REPOQUERY} -c ${YUM} --repoid=${REPOID}"\
                            "${REPOQUERY_SUB_COMMAND} --arch=x86_64,noarch"\
                            "--qf=\"%{name} %{name}-%{version}-%{release}.%{arch}.rpm %{relativepath}\""\
                            "${REPOQUERY_RESOLVE} ${PKG}"
                    >&2 echo "  PKG=$PKG PKG_FILE=$PKG_FILE REPO_PATH=$REPO_PATH PKG_REL_PATH=$PKG_REL_PATH PKG_PATH=$PKG_PATH"
                fi

                echo $UNRESOLVED | grep $PKG
                echo $UNRESOLVED | grep $PKG >> /dev/null
                if [ $? -eq 0 ]; then
                    echo "$PKG found in $REPOID as $PKG" >> $BUILT_REPORT
                    echo "$PKG_PATH" >> $BUILT_REPORT
                    UNRESOLVED=$(echo "$UNRESOLVED" | sed "s# $PKG # #g")
                else
                    echo "$PKG satisfies unknown target in $REPOID" >> $BUILT_REPORT
                    echo "  but it doesn't match targets, $UNRESOLVED" >> $BUILT_REPORT
                    echo "  path $PKG_PATH" >> $BUILT_REPORT
                    FOUND_UNKNOWN=1
                fi
            done < $TMPFILE

            \rm -rf $TMP_DIR/yum-$USER-*
            TARGETS="$UNRESOLVED"
        fi
    done
    >&2 echo "Debug: Packages still unresolved: $UNRESOLVED"
    echo "Debug: Packages still unresolved: $UNRESOLVED" >> $WARNINGS_REPORT
    echo "Debug: Packages still unresolved: $UNRESOLVED" >> $BUILT_REPORT
    >&2 echo ""
}

function check_all_explicit_deps_installed {

    PKGS_TO_CHECK=" "
    while read PKG_TO_ADD
    do
        PKGS_TO_CHECK="$PKGS_TO_CHECK ${PKG_TO_ADD}"
    done < $DEPLISTFILE
    rpm -qp ${INSTALLDIR}/*.rpm --qf="%{name}\n" > $TMPFILE

    echo "checking... $PKGS_TO_CHECK vs ${INSTALLED_PACKAGE}"

    while read INSTALLED_PACKAGE
    do
        echo $PKGS_TO_CHECK | grep -q "${INSTALLED_PACKAGE}"
        if [ $? -eq 0 ]; then
            PKGS_TO_CHECK=`echo $PKGS_TO_CHECK | sed "s/^${INSTALLED_PACKAGE} //"`
            PKGS_TO_CHECK=`echo $PKGS_TO_CHECK | sed "s/ ${INSTALLED_PACKAGE} / /"`
            PKGS_TO_CHECK=`echo $PKGS_TO_CHECK | sed "s/ ${INSTALLED_PACKAGE}\$//"`
            PKGS_TO_CHECK=`echo $PKGS_TO_CHECK | sed "s/^${INSTALLED_PACKAGE}\$//"`
        fi
    done < $TMPFILE

    if [ -z "$PKGS_TO_CHECK" ]; then
        >&2 echo "All explicitly specified packages resolved!"
    else
        >&2 echo "Could not resolve packages: $PKGS_TO_CHECK"
        return 1
    fi
    return 0
}

if [ "x${ROOT}" == "x" ]; then
    ROOT=/localdisk/loadbuild/centos
fi

ATTEMPTED=0
DISCOVERED=0
OUTPUT_DIR=${ROOT}/newDisk
YUM=${ROOT}/yum.conf
TMP_DIR=${ROOT}/tmp
DEPLISTFILE=${ROOT}/deps.txt
DEPLISTFILE_NEW=${ROOT}/deps_new.txt
DEPDETAILLISTFILE=${ROOT}/deps_detail.txt
INSTALLDIR=${ROOT}/newDisk/isolinux/Packages

BUILT_REPORT=${ROOT}/local.txt
WARNINGS_REPORT=${ROOT}/warnings.txt
LAST_TEST=${ROOT}/last_test.txt
TMPFILE=${ROOT}/cgts_deps_tmp.txt
TMPFILE1=${ROOT}/cgts_deps_tmp1.txt

touch "$BUILT_REPORT"
touch "$WARNINGS_REPORT"

for i in "$@"
do
case $i in
    -d=*|--deps=*)
    DEPS="${i#*=}"
    shift # past argument=value
    ;;
esac
done

mkdir -p $TMP_DIR

rm -f "$DEPDETAILLISTFILE"
# FIRST PASS we are being given a list of REQUIRED dependencies
if [ "${DEPS}x" != "x" ]; then
    cat $DEPS | grep -v "^#" > $DEPLISTFILE
    install_deps $DEPLISTFILE
    if [ $? -ne 0 ]; then
        exit 1
    fi
fi

# check that we resolved them all
check_all_explicit_deps_installed
if [ $? -ne 0 ]; then
    >&2 echo "Error -- could not install all explicitly listed packages"
    exit 1
fi

ALL_RESOLVED=0

while [ $ALL_RESOLVED -eq 0 ]; do
    cp $DEPLISTFILE $DEPLISTFILE.old
    generate_dep_list
    if [ ! -s $DEPLISTFILE ]; then
        # no more dependencies!
        ALL_RESOLVED=1
    else
        DIFFLINES=`diff $DEPLISTFILE.old $DEPLISTFILE | wc -l`
        if [ $DIFFLINES -eq 0 ]; then
            >&2 echo "Warning: Infinite loop detected in dependency resolution.  See $DEPLISTFILE for details -- exiting"
            >&2 echo "These RPMS had problems (likely version conflicts)"
            >&2 cat  $DEPLISTFILE

            echo "Warning: Infinite loop detected in dependency resolution See $DEPLISTFILE for details -- exiting" >> $WARNINGS_REPORT
            echo "These RPMS had problems (likely version conflicts)" >> $WARNINGS_REPORT
            cat  $DEPLISTFILE >> $WARNINGS_REPORT

            date > $LAST_TEST

            rm -f $DEPLISTFILE.old
            exit 1 # nothing fixed
        fi
        install_deps $DEPLISTFILE
        if [ $? -ne 0 ]; then
            exit 1
        fi
    fi
done

exit 0
