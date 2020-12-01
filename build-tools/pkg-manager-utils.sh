#
# BASH utilities to select package manager
#
# Currently just sets some environmnet variables
#

#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

# Yum vs DNF compatibility
YUM=$(which yum)
DNF=$(which dnf)
PKG_MANAGER=""
REPOQUERY=$(which repoquery)
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

