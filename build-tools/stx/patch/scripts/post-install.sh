#!/bin/bash
#
# Copyright (c) 2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
#
# The patching subsystem provides a patch-functions bash source file
# with useful function and variable definitions.
#
. /etc/patching/patch-functions

#
# Declare an overall script return code
#
declare -i GLOBAL_RC=$PATCH_STATUS_OK

echo "Post-install hook script"

#
# Exit the script with the overall return code
#
exit $GLOBAL_RC
