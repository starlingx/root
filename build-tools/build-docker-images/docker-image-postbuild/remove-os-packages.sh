#!/bin/sh
#
# Copyright (c) 2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# This script is called inside a docker container by
# docker-image-postbuild.sh
#
# Usage: OUTPUT_TOKEN="..." remove-os-packages.sh package1 package2 ...
#

if [ -z "$OUTPUT_TOKEN" ] ; then
    echo "ERROR: OUTPUT_TOKEN must be defined" >&2
    exit 1
fi

. `dirname "$0"`/utils.sh

sys_pkg_exists() {
    if [ $PKG_MAN = dpkg ] ; then
        dpkg -s "$1" >/dev/null 2>&1
    elif [ $PKG_MAN = rpm ] ; then
        rpm -q "$1" >/dev/null 2>&1
    else
        apk info "$1" >/dev/null 2>&1
    fi
}

sys_pkg_remove() {
    if [ $PKG_MAN = dpkg ] ; then
        ( set -x ; dpkg -r $* ; )
    elif [ $PKG_MAN = rpm ] ; then
        ( set -x ; rpm -e $* ; )
    else
        # FIXME: it always removes recursively
        #( set -x ; apk del $* ; )
        echo "ERROR: apk not supported" >&2
        exit 1
    fi
}

rm_list=`
    sep=
    for package in $* ; do
        if sys_pkg_exists "$package" ; then
            echo -n "${sep}$package"
            sep=" "
        fi
    done
`
if [ -n "$rm_list" ] ; then
    echo "Removing OS packages [$rm_list]" >&2
    sys_pkg_remove $rm_list
    echo "

$OUTPUT_TOKEN $rm_list

"
else
    echo "No removable OS packages found" >&2
fi
