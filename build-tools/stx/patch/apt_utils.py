#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import os
import shutil
import sys

import constants

sys.path.append('..')
import discovery
import repo_manage
import utils


DEFAULT_APT_WORKSPACE = os.path.join(constants.LOADBUILD_ROOT, 'patch_workspace')
TEMP_APT_SRC_PATH = "/tmp/patch_apt_source_list"

# TODO: Logic for parsing image-layers.conf is duplicated by build-image.
#       It should be centralized in some utility module, like discovery.py
# TODO: REPO_BUILD and REPO_BINARY should be defined in discovery.py or a similar module.
IMAGE_LAYERS_PATH = os.path.join(constants.DESIGNER_ROOT, "cgcs-root", "build-tools", "stx", "image-layers.conf")
ALL_LAYERS = discovery.get_all_layers()

# Contains STX binary packages
REPO_BUILD = "deb-local-build"

# Contains third-party packages
# There are more specific aptly repos named as REPO_BINARY-<specifier>
REPO_BINARY = "deb-local-binary"


logger = logging.getLogger(__name__)
utils.set_logger(logger)


def get_all_local_repo_names() -> list[str]:
    """Get list of local aptly repo names from the running aptly instance."""

    try:
        repomgr = repo_manage.RepoMgr(
            'aptly',
            utils.get_env_variable('REPOMGR_URL'),
            '/tmp',
            utils.get_env_variable('REPOMGR_ORIGIN'),
            logger
        )
    except Exception:
        logger.error("Failed to get a list of aptly repos")
        raise

    return repomgr.repo.list_local(quiet=True)


def get_stx_local_repo_names(config_filepath:str = IMAGE_LAYERS_PATH) -> list[str]:
    """
    Get a list of local aptly repo names relevant to STX patches.
    This is done by taking all available aptly repos and filtering them based on a config file.

    The REPO_BUILD and REPO_BINARY repos are considered as always included.

    The config file then defines some additional repos with names in the format REPO_BINARY-<specifier>,
    where the specifier corresponds to a build layer.

    The files listing third-party packages for each build layer are in the tools repo
    at debian-mirror-tools/config/debian/<DISTRO>/
    """

    # Basic repositories
    # Note the basic "REPO_BINARY" corresponds to the "common" layer
    stx_local_repos = [REPO_BUILD, REPO_BINARY]

    all_valid_repos = get_all_local_repo_names()

    try:
        with open(config_filepath, "r") as f:
            contents = f.readlines()
    except IOError:
        logger.error(f"Failed to filter aptly repos relevant to WRCP patches")
        raise

    for line in contents:
        # Ignore if it's comment or white space.
        if not line.strip() or line.startswith("#"):
            continue

        # Check if it's a valid layer.
        candidate_layer = line.strip().lower()
        candidate_repo = f"{REPO_BINARY}-{candidate_layer}"
        if candidate_repo in all_valid_repos:
            stx_local_repos.append(candidate_repo)
        else:
            msg = f"Invalid aptly repo layer '{candidate_layer}' found in '{config_filepath}'. The layer must be one of {ALL_LAYERS}."
            raise Exception(msg)

    return stx_local_repos


def get_apt_fetcher(workspace_dir:str = DEFAULT_APT_WORKSPACE) -> repo_manage.AptFetch:
    """
    Set up apt wrapper configured with the build environment's local apt repo
    """

    # Clean up old apt workspace dir, if it exists
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)

    os.makedirs(workspace_dir)

    apt_repos = get_stx_local_repo_names()

    # Setup input apt source file
    with open(TEMP_APT_SRC_PATH, 'w', encoding="utf-8") as apt_src_file:
        repo_url = utils.get_env_variable('REPOMGR_DEPLOY_URL')

        for apt_repo in apt_repos:
            apt_repo = f"deb [trusted=yes] {repo_url}{apt_repo} {constants.STX_DEFAULT_DISTRO_CODENAME} main\n"
            apt_src_file.write(apt_repo)

    # Initialize the apt wrapper object
    apt_fetcher = repo_manage.AptFetch(logger, TEMP_APT_SRC_PATH, workspace_dir)

    # Clean up the temp file
    os.remove(TEMP_APT_SRC_PATH)

    return apt_fetcher
