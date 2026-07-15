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
Checksum calculation for Debian package source tracking and reuse detection.

NOTE: This implementation calculates checksums based ONLY on file contents,
not git history. This is more consistent and predictable for reuse detection.
"""

import hashlib
import logging
import os
import re
from package_metadata import PackageMetadata


def get_str_md5(text):
    """Calculate MD5 hash of a string."""
    md5obj = hashlib.md5()
    md5obj.update(text.encode())
    return md5obj.hexdigest()


class PackageChecksumCalculator:
    """
    Calculates checksums for Debian package sources to enable build reuse detection.

    The checksum is based on file contents only:
    - All files in debian/ directory
    - Source files from src_path (if specified)
    - Additional source files from src_files (if specified)

    Git history is NOT included in the checksum. Version numbers (via *REVCOUNT)
    already track git changes, so including git history in checksums is redundant
    and makes reuse detection less predictable.
    """

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def _collect_file_list(self, pkgpath, meta_data):
        """Collect all files that contribute to the checksum."""
        if isinstance(meta_data, PackageMetadata):
            return meta_data.checksum_file_list()

        # Legacy dict fallback
        files_list = []
        debfolder = os.path.join(pkgpath, "debian")
        if not os.path.isdir(debfolder):
            raise Exception(f"{debfolder}: no such directory")
        for root, _, files in os.walk(debfolder):
            for name in files:
                files_list.append(os.path.abspath(os.path.join(root, name)))
        src_path = meta_data.get("src_path")
        if src_path:
            for root, _, files in os.walk(src_path):
                for name in filter(lambda f: not f.startswith('.git'), files):
                    files_list.append(os.path.join(root, name))
        for src_file in meta_data.get("src_files", []):
            if os.path.isdir(src_file):
                for root, dirs, files in os.walk(src_file):
                    dirs[:] = [d for d in dirs if d not in ('.git', '.tox')]
                    for name in files:
                        files_list.append(os.path.join(root, name))
            else:
                files_list.append(src_file)
        return files_list

    def _read_file_contents(self, files_list):
        """Read and concatenate contents of all files."""
        content = ""
        for f in sorted(files_list):
            # Skip compiled Python files
            if f.endswith(".pyc") or f.endswith(".pyo"):
                continue
            try:
                with open(f, 'r', encoding="ISO-8859-1") as fd:
                    content += fd.read()
            except Exception as e:
                self.logger.warning(f"Failed to read {f}: {e}")
        return content

    # Known build tools that never affect binary content
    _BUILD_TOOLS = {
        'debhelper', 'debhelper-compat', 'dh-python', 'dh-autoreconf',
        'dh-strip-nondeterminism', 'dh-exec', 'dh-golang', 'dh-make',
        'dpkg-dev', 'build-essential', 'fakeroot', 'cmake', 'meson',
        'ninja-build', 'pkg-config', 'autoconf', 'automake', 'libtool',
        'quilt', 'gem2deb', 'javahelper', 'ant', 'maven-debian-helper',
        'sphinx-common', 'python3-sphinx', 'doxygen', 'helm',
        'python3-pytest', 'python3-nose', 'python3-mock',
        'python3-setuptools', 'python3-all', 'python3-wheel', 'python3-pip',
        'python3-build', 'python3-installer', 'pybuild-plugin-pyproject',
    }

    def _derive_content_deps_from_control(self, pkgpath, meta_data):
        """Auto-derive content-affecting deps from deb_folder/control.

        Returns sorted list of package names whose versions should be
        included in the checksum.
        """
        # Find the control file in the deb_folder
        control_path = None
        if isinstance(meta_data, PackageMetadata) and meta_data._meta_file:
            deb_dir = meta_data._meta_file.parent
            candidate = deb_dir / 'deb_folder' / 'control'
            if candidate.is_file():
                control_path = str(candidate)

        if not control_path:
            # Try standard location relative to pkgpath
            candidate = os.path.join(pkgpath, 'debian', 'control')
            if os.path.isfile(candidate):
                control_path = candidate

        if not control_path:
            return []

        deps = []
        try:
            with open(control_path, 'r') as f:
                content = f.read()
            # Parse only the source paragraph (before first empty line after Package:)
            # Simple approach: read Build-Depends lines
            for field in ('Build-Depends', 'Build-Depends-Arch', 'Build-Depends-Indep'):
                match = re.search(
                    rf'^{field}:\s*(.+?)(?=^\S|\Z)',
                    content, re.MULTILINE | re.DOTALL)
                if match:
                    field_text = match.group(1)
                    for dep_str in field_text.split(','):
                        dep_str = dep_str.split('|')[0].strip()
                        name = re.split(r'[\s(:[]', dep_str)[0].strip()
                        if name and name not in self._BUILD_TOOLS:
                            # Only -dev, -wheels, and build-info affect binary content
                            if name.endswith('-dev') or name.endswith('-wheels') or name == 'build-info':
                                deps.append(name)
        except Exception as e:
            self.logger.debug("Failed to parse Build-Depends from %s: %s",
                             control_path, e)

        # Deduplicate and sort
        pkg_name = meta_data._raw.get('debname', '') if isinstance(meta_data, PackageMetadata) else ''
        return sorted(set(d for d in deps if d and d != pkg_name))

    def calculate(self, pkgpath, meta_data, debfolder=None, version_resolver=None):
        """
        Calculate checksum for a package based on file contents only.

        Args:
            pkgpath: Path to the package directory
            meta_data: Package metadata dictionary or PackageMetadata
            debfolder: Optional debian folder path (ignored, kept for compatibility)
            version_resolver: Optional callable(pkg_name) -> version_string.
                Used to resolve current versions of rebuild_triggers dependencies.
                Their versions are included in the hash so that a dep version
                change produces a different checksum, triggering a rebuild.
                If None, rebuild_triggers are ignored.

        Returns:
            MD5 checksum string
        """
        if not os.path.isdir(pkgpath):
            raise Exception(f"{pkgpath}: No such file or directory")

        # Collect all relevant files
        files_list = self._collect_file_list(pkgpath, meta_data)

        # Read file contents
        content = self._read_file_contents(files_list)

        # Append content-affecting dependency versions to hash input.
        # Uses explicit content_depends/rebuild_triggers if set, otherwise
        # auto-derives from Build-Depends in deb_folder/control.
        if version_resolver and isinstance(meta_data, PackageMetadata):
            trigger_list = None
            cd = meta_data._raw.get('content_depends')
            if cd:
                trigger_list = cd
            elif meta_data.rebuild_triggers:
                trigger_list = meta_data.rebuild_triggers

            if trigger_list:
                for dep in sorted(trigger_list):
                    ver = version_resolver(dep)
                    if ver:
                        content += f"\n__rebuild_trigger__:{dep}={ver}"
            else:
                # Auto-derive from deb_folder/control Build-Depends
                auto_deps = self._derive_content_deps_from_control(pkgpath, meta_data)
                for dep in auto_deps:
                    ver = version_resolver(dep)
                    if ver:
                        content += f"\n__content_dep__:{dep}={ver}"

        # Calculate and return MD5 hash (no git history)
        return get_str_md5(content)
