#!/bin/bash

set -ex

#
# This script enables or disables package repos specified
# by the DIST_REPOS environment variable, which must contain
# a space-separated list of repos (in CentOS) or list files
# (Debian) to enable or disable.
#
# In CentOS repo names refer to the names in square brackets
# in any repo files under /etc/yum.repos.d.
#
# In Debian repo names refer to individual files under
# /etc/apt/sources.list.d/$NAME.list.
#
# Repo names may be prefixed with
# a "+" (enable) or a "-" (disable). The leading "+" may be
# omitted.
#
# Additionally, the following keywords are treated specially:
#
#   STX   - enable or disable all StarlingX repos, ie
#           the locally-built package repos, the mirror/download
#           repo, and any repo's passed on the command-line
#           to "build-stx-image.sh" script.
#
#   OS    - same as "base updates extras" in CentOS
#           same as "debian" in Debian
#
#
# These keywords have the same meaning in all distros, while actual
# repo names are distro-specific.
#
# Any repos not included in $DIST_REPOS will remain unchanged (ie
# they will remain enabled or disabled as defined in the base image).
#
# If a repo doesn't match an existing repository, this script will
# fail.
#
# CentOS Example
# ==============
#   DIST_REPOS="-base -updates"
#      disable "base" and "updates" repos normally defined
#      in /etc/yum.repos.d/CentOS-Base.repo
#
#   DIST_REPOS="-STX +OS -updates"
#      disable all local repos, enable core OS repos, except "updates"
#
# Debian Example
# ==============
#   DIST_REPOS="debian"
#      enable core OS repos (ie /etc/apt/sources.list.d/debian.list)
#
#   DIST_REPOS="OS -STX"
#      enable core OS repos (ie /etc/apt/sources.list.d/debian.list),
#      disable STX repos (ie /etc/apt/sources.list.d/stx.list)
#
#

if [[ -n "$DIST_REPOS" ]] ; then
    # basenames of files under /etc/apt/sources.list.d
    declare -A DEBIAN_REPO_GROUPS=(
        [OS]="debian"
        [STX]="stx"
    )
    # yum repo IDs
    declare -A CENTOS_REPO_GROUPS=(
        [OS]="base updates extras"
        [STX]="/etc/yum.repos.d/stx.repo"   # ie, all repos defined in this file
    )

    distro=$(awk -F= '/^ID=/ {gsub(/\"/, "", $2); print $2}' /etc/*release)
    # enable or disable each repo
    for base in $DIST_REPOS ; do
        # starts with "-": disable this repo
        if [[ "${base#-}" != "$base" ]] ; then
            base="${base#-}"
            enable=0
        # starts with "+": enable this repo
        elif [[ "${base#+}" != "$base" ]] ; then
            base="${base#+}"
            enable=1
        # doesn't start with +/-: assume "+"
        else
            enable=1
        fi

        # enable or disable a repo
        case ${distro} in
            debian)
                list_files="${DEBIAN_REPO_GROUPS[$base]:-$base}"
                for list_file in $list_files ; do
                    if [[ $enable -eq 1 ]] ; then
                        cp -f /etc/apt/sources.list.d/${list_file}.list.disabled /etc/apt/sources.list.d/${list_file}.list
                    else
                        rm /etc/apt/sources.list.d/${list_file}.list
                    fi
                done
                ;;
            centos)
                specs="${CENTOS_REPO_GROUPS[$base]:-$base}"
                for spec in $specs ; do
                    # repo id begins with a "/" - assume its a full path to a .repo file
                    # and enable/disable all repos defined in that file
                    if [[ "${spec#/}" != "$spec" ]] ; then
                        repos=$(sed -r -n 's/^\s*[[]([^]]+)[]]\s*$/\1/gp' "$spec")
                    else
                        repos=$spec
                    fi
                    for repo in $repos ; do
                        if [[ $enable -eq 1 ]] ; then
                            yum-config-manager --enable "$repo"
                        else
                            yum-config-manager --disable "$repo"
                        fi
                    done
                done
                ;;
            *)
                echo "error: unsupported OS \"$distro\"" >&2
                exit 1
        esac
    done
fi

