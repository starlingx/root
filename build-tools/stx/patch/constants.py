#
# Copyright (c) 2024 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

### Patch Scripts ###
# Script IDs and their default names inside the patch

# Custom scripts executed at different steps in the patch apply process
# TODO: This variable can be replaced by a function that replaces the '_' for
# '-' and appends ".sh"
# TODO: Can't these SCRIPT variables be turned into lists?
PATCH_SCRIPTS = {
    "pre_start": "pre-start.sh",
    "post_start": "post-start.sh",
    "pre_install": "pre-install.sh",
    "post_install": "post-install.sh",
}

# Required when there are patching framework updates
PRECHECK_SCRIPTS = {
    "DEPLOY_PRECHECK": "deploy-precheck",
    "UPGRADE_UTILS": "upgrade_utils.py",
}


### Signing ###

# Default path to the script that generates the upload path
GET_UPLOAD_PATH = "/opt/signing/sign.sh"
# Default path to the script that sign the patch
REQUEST_SIGN = "/opt/signing/sign_patch.sh"
