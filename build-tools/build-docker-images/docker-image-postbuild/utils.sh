#
# Copyright (c) 2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

PKG_MAN=   # dpkg|rpm|apk

if [ -f /etc/redhat-release ] ; then
    PKG_MAN="rpm"
elif [ -f /etc/debian_version ] ; then
    PKG_MAN="dpkg"
elif [ -f /etc/alpine-release ] ; then
    PKG_MAN="apk"
elif [ -f /etc/os-release ] ; then
    case `ID= ; cat /etc/os-release && echo $ID` in
        debian|ubuntu)
            PKG_MAN="dpkg"
            ;;
        centos|rhel|fedora)
            PKG_MAN="rpm"
            ;;
        alpine)
            PKG_MAN="apk"
            ;;
    esac
elif dpkg --version >/dev/null >&2 ; then
    PKG_MAN="dpkg"
elif rpm --version >/dev/null >&2 ; then
    PKG_MAN="rpm"
elif apk --version >/dev/null >&2 ; then
    PKG_MAN="apk"
fi
if [ -z "$PKG_MAN" ] ; then
    echo "WARNING: unsupported OS package manager, bailing out" >&2
    exit 0
fi
if ! $PKG_MAN --version >/dev/null 2>&1 ; then
    echo "WARNING: $PKG_MAN not found, bailing out" >&2
    exit 0
fi

