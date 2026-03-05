# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright (C) 2021-2026 Wind River Systems, Inc.

"""
Debian package version and revision calculation.
"""

import logging
import os
from utils import run_shell_cmd
from package_metadata import PackageMetadata


class PackageRevisionCalculator:
    """
    Calculates Debian package revision numbers based on git history.

    Supports multiple GITREVCOUNT variants:
    - PKG_GITREVCOUNT: Tracks debian/ directory changes
    - FILES_GITREVCOUNT: Tracks specific files/directories
    - SRC_GITREVCOUNT: Tracks src_path directory changes
    - GITREVCOUNT: Tracks custom directory changes
    """

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def _get_gitrevcount_srcdir(self, pkgpath, gitrevcount_obj):
        """Determine the source directory for GITREVCOUNT."""
        src_dir = str(gitrevcount_obj.get("SRC_DIR", ""))
        if src_dir:
            src_dir = os.path.expandvars(src_dir)
            if not src_dir.startswith('/'):
                src_dir = os.path.join(pkgpath, src_dir)
            self.logger.info("SRC_DIR = %s", src_dir)
        else:
            # Default to debian folder
            src_dir = os.path.join(pkgpath, "debian")
            self.logger.info("SRC_DIR = %s (guessed)", src_dir)
        return src_dir

    def calculate(self, pkgpath, meta_data, debfolder):
        """
        Calculate revision number for a package.

        Args:
            pkgpath: Path to the package directory
            meta_data: Package metadata (PackageMetadata or dict)
            debfolder: Path to debian folder

        Returns:
            Tuple of (dist_string, revision_number)
        """

        revision = 0
        dist = ""

        if isinstance(meta_data, PackageMetadata):
            revision_data = meta_data.revision
            src_path = meta_data.src_path
            src_files = meta_data.src_files
        else:
            if "revision" not in meta_data:
                return dist, revision
            revision_data = meta_data["revision"]
            src_path = meta_data.get("src_path")
            src_files = meta_data.get("src_files", [])

        if not revision_data:
            return dist, revision

        # Get distribution string
        if "dist" in revision_data and revision_data["dist"] is not None:
            dist = os.path.expandvars(revision_data["dist"])

        # Git command templates
        git_rev_list = "cd %s;git rev-list --count HEAD ."
        git_rev_list_from = "cd %s;git rev-list --count %s..HEAD ."
        git_status = "cd %s;git status --porcelain . | wc -l"

        # PKG_GITREVCOUNT: debian/ directory
        if "PKG_GITREVCOUNT" in revision_data:
            if "PKG_BASE_SRCREV" in revision_data:
                revision += int(run_shell_cmd(
                    git_rev_list_from % (debfolder, revision_data["PKG_BASE_SRCREV"]),
                    self.logger
                ))
            else:
                revision += int(run_shell_cmd(git_rev_list % debfolder, self.logger))
            revision += int(run_shell_cmd(git_status % debfolder, self.logger))

        # FILES_GITREVCOUNT: specific files/directories
        if "FILES_GITREVCOUNT" in revision_data:
            if not src_files:
                raise Exception("FILES_GITREVCOUNT is set, but no \"src_files\" in meta_data.yaml")

            base_commits = revision_data["FILES_GITREVCOUNT"]
            if len(src_files) > len(base_commits):
                raise Exception("Not all \"src_files\" have \"FILES_BASE_SRCREV\"")

            files_commits = {}
            for i in range(len(src_files)):
                f = src_files[i]
                if os.path.isfile(f):
                    f = os.path.dirname(f)
                if f not in files_commits:
                    files_commits[f] = base_commits[i]

            for f in files_commits:
                revision += int(run_shell_cmd(
                    git_rev_list_from % (f, files_commits[f]),
                    self.logger
                ))
                revision += int(run_shell_cmd(git_status % f, self.logger))

        # SRC_GITREVCOUNT: src_path directory
        if "SRC_GITREVCOUNT" in revision_data:
            if not src_path:
                raise Exception("SRC_GITREVCOUNT is set, but no \"src_path\" in meta_data.yaml")

            src_gitrevcount = revision_data["SRC_GITREVCOUNT"]

            if "SRC_BASE_SRCREV" in src_gitrevcount:
                revision += int(run_shell_cmd(
                    git_rev_list_from % (src_path, src_gitrevcount["SRC_BASE_SRCREV"]),
                    self.logger
                ))
            else:
                revision += int(run_shell_cmd(git_rev_list % src_path, self.logger))
            revision += int(run_shell_cmd(git_status % src_path, self.logger))

        # GITREVCOUNT: custom directory
        if "GITREVCOUNT" in revision_data:
            gitrevcount = revision_data["GITREVCOUNT"]
            src_dir = self._get_gitrevcount_srcdir(pkgpath, gitrevcount)

            if "BASE_SRCREV" not in gitrevcount:
                raise Exception("Not set BASE_SRCREV in GITREVCOUNT")

            revision += int(run_shell_cmd(
                git_rev_list_from % (src_dir, gitrevcount["BASE_SRCREV"]),
                self.logger
            ))
            revision += int(run_shell_cmd(git_status % src_dir, self.logger))

        # Manual patch version
        if "stx_patch" in revision_data:
            if type(revision_data['stx_patch']) is not int:
                raise Exception("The stx_patch in meta_data.yaml is not an int value")
            revision += int(revision_data["stx_patch"])

        return dist, revision
