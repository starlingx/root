#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import sys

sys.path.append('..')
import discovery
import utils

# Source code directory
# In general: /localdisk/designer/<USER>/<PROJECT>
DESIGNER_ROOT = utils.get_env_variable('MY_REPO_ROOT_DIR')

# Package binaries output directory
# In general: /localdisk/loadbuild/<USER>/<PROJECT>
LOADBUILD_ROOT = utils.get_env_variable('MY_BUILD_PKG_DIR')

# Product (patches and ISOs) output directory
DEPLOY_ROOT = "/localdisk/deploy"

# Distro
STX_DEFAULT_DISTRO_CODENAME = discovery.STX_DEFAULT_DISTRO_CODENAME

# Metapackages
METAPACKAGE_NAME_PREFIX = "meta-"
METAPACKAGE_SECTION = "metapackages"
