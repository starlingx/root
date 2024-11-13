#
# Copyright (c) 2024 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

# Default names for the script inside the patch
PATCH_SCRIPTS = {
   "PRE_INSTALL": "pre-install.sh",
   "POST_INSTALL": "post-install.sh",
   "DEPLOY_PRECHECK": "deploy-precheck",
   "UPGRADE_UTILS": "upgrade_utils.py",
}

# Default path to the script that generates the upload path
GET_UPLOAD_PATH = "/opt/signing/sign.sh"
# Default path to the script that sign the patch
REQUEST_SIGN = "/opt/signing/sign_patch.sh"