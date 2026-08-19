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
import repo_manage
import utils


DEFAULT_APT_WORKSPACE = os.path.join(constants.LOADBUILD_ROOT, 'patch_workspace')
TEMP_APT_SRC_PATH = "/tmp/patch_apt_source_list"


logger = logging.getLogger(__name__)
utils.set_logger(logger)


def get_local_repo_names() -> list[str]:
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


def get_apt_fetcher(workspace_dir:str = DEFAULT_APT_WORKSPACE) -> repo_manage.AptFetch:
    """
    Set up apt wrapper configured with the build environment's local apt repo
    """

    # Clean up old apt workspace dir, if it exists
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)

    os.makedirs(workspace_dir)

    apt_repos = get_local_repo_names()

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
