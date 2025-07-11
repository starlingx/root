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
# Usage: OUTPUT_TOKEN="..." remove-pip-packages.sh package1 package2 ...
#

if [ -z "$OUTPUT_TOKEN" ] ; then
    echo "ERROR: OUTPUT_TOKEN must be defined" >&2
    exit 1
fi

if ! pip3 --version >/dev/null 2>&1 ; then
    echo "WARNING: pip3 not found, can't remove any python packages" >&2
    exit 0
fi

. `dirname "$0"`/utils.sh

sys_pkg_owned() {
    if [ $PKG_MAN = dpkg ] ; then
        dpkg -S "$1" >/dev/null 2>&1
    elif [ $PKG_MAN = rpm ] ; then
        rpm -qf "$1" >/dev/null 2>&1
    else
        apk info -W "$1" >/dev/null 2>&1
    fi
}

in_list() {
    in_list_item="$1"
    shift
    while [ "$#" -gt 0 ] ; do
        if [ "$in_list_item" = "$1" ] ; then
            return 0
        fi
        shift
    done
    return 1
}

trim() {
    sed 's/^[ \t]*//;s/[ \t]*//'
}

sep=
rm_list=
for mod in $* ; do
    mod_info=`pip3 show "$mod" 2>/dev/null` || continue
    rdepends=`echo "$mod_info" | sed -n "s/^Required-by://ip" | tr -d ',' | trim`
    if [ -n "$rdepends" ] ; then
        for rmod in $rdepends ; do
            pip3 show "$rmod" 2>/dev/null 2>&1 || continue
            if ! in_list "$rmod" $modules ; then
                echo "ERROR: can't uninstall pip module "$mod" because another installed module "$rmod" requires it" >&2
                exit 1
            fi
        done
    fi
    location=`echo "$mod_info" | sed -n "s/^Location://ip" | trim`
    if sys_pkg_owned "$location" ; then
        echo "WARNING: can't uninstall pip module "$mod" because it is owned by the OS package manager" >&2
        continue
    fi
    rm_list="${rm_list}${sep}$mod"
    sep=" "
done

if [ -n "$rm_list" ] ; then
    echo "Removing python packages [$rm_list]" >&2
    ( set -x ; pip3 uninstall --yes $rm_list ; ) || exit 1
    echo "

$OUTPUT_TOKEN $rm_list

"
else
    echo "No removable python packages found" >&2
fi

