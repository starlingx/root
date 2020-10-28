#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# BASH utilities to select package manager
#
# Currently just sets some environment variables
#

# Yum vs DNF compatibility
YUM=$(which yum 2>> /dev/null)
DNF=$(which dnf 2>> /dev/null)
PKG_MANAGER=""
REPOQUERY=$(which repoquery 2>> /dev/null)
REPOQUERY_SUB_COMMAND=""
REPOQUERY_RESOLVE="--resolve"
REPOQUERY_WHATPROVIDES_DELIM=" "
if [ ! -z ${DNF} ]; then
    PKG_MANAGER="dnf"
    REPOQUERY=${DNF}
    REPOQUERY_SUB_COMMAND="repoquery --disable-modular-filtering"
    REPOQUERY_RESOLVE=""
    REPOQUERY_WHATPROVIDES_DELIM=","
elif [ ! -z ${YUM} ]; then
    PKG_MANAGER="yum"
else
    >&2 echo "ERROR: Couldn't find a supported package manager"
    exit 1
fi

