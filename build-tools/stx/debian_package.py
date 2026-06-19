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
# Copyright (C) 2024-2026 Wind River Systems, Inc.

#!/usr/bin/env python3
# VERSION: 2026-02-17-16:20 - Fixed Build-Depends-Arch reading

import os
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

from debian import deb822
from debian.debfile import DebFile

from package_metadata import PackageMetadata
from isolated_apt import IsolatedApt


class DebianBinaryPackage:
    VALID_STATES = {
        'unknown',
        'runtime-req-checking', 'runtime-req-downloading', 'runtime-req-downloaded',
        'checksumming', 'checksummed',
        'uploading', 'uploaded', 'published'
    }
    def __init__(self, apt_cache=None):
        self.name = None
        self.version = None
        self.architecture = None
        self.source_package = None
        self.depends = []
        self.pre_depends = []
        self.provides = []
        self.origin = None  # 'source', 'apt', or 'deb'
        self._apt_cache = apt_cache

    def read_from_repository(self, package_name, repo_url=None, suite=None):
        """Read package data from a Debian repository using isolated apt cache."""
        if self._apt_cache:
            pkg = self._apt_cache.show(package_name)
            if not pkg or not pkg.candidate:
                raise ValueError(f"Package {package_name} not found in repository")
            self._parse_from_apt_package(pkg)
            self.origin = 'apt'
        else:
            cmd = ['apt-cache', 'show', package_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f"Package {package_name} not found in repository")
            self._parse_from_deb822(deb822.Deb822(result.stdout))
            self.origin = 'apt'

    def read_from_deb_file(self, deb_path):
        """Read package data from a local .deb file."""
        deb = DebFile(deb_path)
        control = deb.debcontrol()
        self._parse_from_deb822(control)
        self.origin = 'deb'

    def set_from_control_paragraph(self, para, source_name=None, source_version=None):
        """Populate from a debian/control binary package paragraph."""
        self.name = para.get('Package')
        self.architecture = para.get('Architecture')
        self.source_package = source_name
        self.version = source_version
        self.origin = 'source'

        if 'Depends' in para:
            self.depends = [d.strip() for d in str(para['Depends']).split(',')]
        if 'Pre-Depends' in para:
            self.pre_depends = [d.strip() for d in str(para['Pre-Depends']).split(',')]
        if 'Provides' in para:
            self.provides = [p.strip() for p in str(para['Provides']).split(',')]

    def _parse_from_apt_package(self, pkg):
        """Parse data from python-apt Package object."""
        self.name = pkg.name
        self.version = pkg.candidate.version
        self.architecture = pkg.candidate.architecture
        self.source_package = pkg.candidate.source_name

        for dep_group in pkg.candidate.dependencies:
            dep_list = [d.name for d in dep_group]
            if dep_group.rawtype == 'PreDepends':
                self.pre_depends.extend(dep_list)
            elif dep_group.rawtype == 'Depends':
                self.depends.extend(dep_list)

        if pkg.candidate.provides:
            self.provides = [p if isinstance(p, str) else p.name for p in pkg.candidate.provides]

    def _parse_from_deb822(self, control):
        """Parse data from deb822 object."""
        self.name = control.get('Package')
        self.version = control.get('Version')
        self.architecture = control.get('Architecture')
        self.source_package = control.get('Source', self.name)  # Default to package name

        if 'Depends' in control:
            self.depends = [d.strip() for d in str(control['Depends']).split(',')]
        if 'Pre-Depends' in control:
            self.pre_depends = [d.strip() for d in str(control['Pre-Depends']).split(',')]
        if 'Provides' in control:
            self.provides = [p.strip() for p in str(control['Provides']).split(',')]


class DebianSourcePackage:
    # Valid status states
    VALID_STATES = {
        'unknown',
        'source-checking', 'source-downloading', 'source-downloaded',
        'source-download-failed',
        'repacking', 'repacked', 'repack-failed',
        'build-req-checking', 'build-req-downloading', 'build-req-downloaded',
        'runtime-req-checking', 'runtime-req-downloading', 'runtime-req-downloaded',
        'checksumming', 'checksummed',
        'need-build', 'reusable',
        'reuse-downloading', 'reuse-downloaded',
        'unbuilt', 'building', 'built', 'build-failed',
        'dep-blocked', 'dep-failed',
        'uploading', 'uploaded', 'published'
    }

    def __init__(self, apt_cache=None):
        self.name = None
        self.version = None
        self.architecture = None
        self.binary_packages = []  # List of DebianBinaryPackage objects
        self.build_depends = []
        self.build_depends_indep = []
        self.status = 'unknown'
        self.checksum = None  # Checksum of the source package
        self.code_size = None  # Size in bytes of source code
        self.compile_complexity = None  # Estimated compile time in minutes
        self.compile_priority_boost = None  # Priority boost to force early build
        self._apt_cache = apt_cache
        self._status_lock = None  # For thread-safe status updates

        # StarlingX metadata
        self.meta_data = None # Dict read from meta_data.yaml
        self.layer = None  # Layer name (e.g., 'distro', 'flock')
        self.build_type = None  # Build type (e.g., 'std', 'rt')
        self.package_dir = None  # Package root directory path

        # Build state (used by build-pkgs-2.0)
        self.dsc_path = None        # Path to .dsc after repack
        self.build_count = 0        # Build retry counter
        self.reuse_source = None    # 'local_cache', 'remote_shared', None

    def get_status(self):
        """Get the current status."""
        return self.status

    def set_status(self, old_status, new_status):
        """Atomically set status from old_status to new_status.
        Returns True if successful, False if current status doesn't match old_status."""
        if new_status not in self.VALID_STATES:
            raise ValueError(f"Invalid status: {new_status}. Must be one of {self.VALID_STATES}")

        if self._status_lock is None:
            self._status_lock = threading.Lock()

        with self._status_lock:
            if self.status == old_status:
                self.status = new_status
                return True
            return False

    def get_layer(self):
        """Get the layer name."""
        return self.layer

    def set_layer(self, layer):
        """Set the layer name."""
        self.layer = layer

    def get_build_type(self):
        """Get the build type."""
        return self.build_type

    def set_build_type(self, build_type):
        """Set the build type."""
        self.build_type = build_type

    def get_package_dir(self):
        """Get the package directory."""
        return self.package_dir

    def set_package_dir(self, package_dir):
        """Set the package directory."""
        self.package_dir = package_dir

    def set_checksum(self, checksum):
        """Set the checksum of the source package."""
        self.checksum = checksum

    def read_from_repository(self, package_name, repo_url=None, suite=None):
        """Read source package data from repository.

        Note: Uses apt-cache subprocess rather than self._apt_cache because
        python-apt has limited support for source package metadata.
        The _apt_cache attribute is used in debian_package_set.py by
        load_from_apt_cache() for source_name lookups from binary packages.
        """
        cmd = ['apt-cache', 'showsrc', package_name]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(f"Source package {package_name} not found")
        dsc = deb822.Dsc(result.stdout)
        self._parse_from_dsc(dsc)

    def read_from_dsc_file(self, dsc_path):
        """Read source package data from a .dsc file."""
        with open(dsc_path, 'r') as f:
            dsc = deb822.Dsc(f)
        self._parse_from_dsc(dsc)

    def read_from_extracted_source(self, source_dir):
        """Read source package data from extracted source directory."""
        control_path = Path(source_dir) / 'debian' / 'control'
        if not control_path.exists():
            raise ValueError(f"No debian/control found in {source_dir}")

        with open(control_path, 'r') as f:
            self._parse_control_file(f)

    def read_from_starlingx_source(self, starlingx_root_path, git_relative_path, package_relative_path, os_id, os_codename):
        """Read source package from StarlingX package source directory."""

        pkg_dir = Path(starlingx_root_path) / git_relative_path / package_relative_path
        meta = PackageMetadata.load(str(pkg_dir), os_id, os_codename)
        self.meta_data = meta

        self.name = meta.name
        self.version = meta.version

        # Read control file if it exists to get binary packages and dependencies.
        control_path = meta.recipes_dir / 'deb_folder' / 'control'
        if control_path.exists():
            with open(control_path, 'r') as f:
                self._parse_control_file(f)
            # Override with meta_data values (control file might have different name)
            self.name = meta.name
            self.version = meta.version

    def extract_and_read_from_dsc(self, dsc_path, skip_extract=False):
        """Extract source package and read control data."""
        dsc_path = Path(dsc_path)

        # Always read .dsc file for metadata
        with open(dsc_path, 'r') as f:
            dsc = deb822.Dsc(f)

        # Store version and build-depends from .dsc
        self.name = dsc.get('Source')
        self.version = dsc.get('Version')
        self.architecture = dsc.get('Architecture')

        # Combine all build dependencies
        # Note: entries may contain arch qualifiers [amd64], build profiles <!stage1>,
        # version constraints (>= 1.0), and alternatives (foo | bar).
        # These are stripped by DependencyAnalyzer._clean_dependency() at analysis time.
        all_build_deps = []
        if 'Build-Depends' in dsc:
            all_build_deps.extend([d.strip() for d in str(dsc['Build-Depends']).split(',')])
        if 'Build-Depends-Arch' in dsc:
            all_build_deps.extend([d.strip() for d in str(dsc['Build-Depends-Arch']).split(',')])
        if 'Build-Depends-Indep' in dsc:
            all_build_deps.extend([d.strip() for d in str(dsc['Build-Depends-Indep']).split(',')])

        self.build_depends = all_build_deps

        if skip_extract:
            # Look for extracted directory - try multiple naming patterns
            dsc_name = dsc_path.stem
            parts = dsc_name.split('_')
            if len(parts) >= 2:
                pkg_name = parts[0]
                full_version = parts[1]

                # Try different directory name patterns
                possible_dirs = [
                    dsc_path.parent / f"{pkg_name}-{full_version}",  # Full version
                    dsc_path.parent / f"{pkg_name}-{full_version.split('-')[0]}",  # Without debian revision
                ]

                # Also try version without .stx.N suffix
                if '.stx.' in full_version:
                    base_version = full_version.split('.stx.')[0]
                    possible_dirs.append(dsc_path.parent / f"{pkg_name}-{base_version}")

                for source_dir in possible_dirs:
                    if source_dir.exists() and (source_dir / 'debian' / 'control').exists():
                        with open(source_dir / 'debian' / 'control', 'r') as f:
                            self._parse_control_file(f)
                        return

                # Fallback: look for any directory starting with package name
                for item in dsc_path.parent.iterdir():
                    if item.is_dir() and item.name.startswith(f"{pkg_name}-"):
                        if (item / 'debian' / 'control').exists():
                            with open(item / 'debian' / 'control', 'r') as f:
                                self._parse_control_file(f)
                            return

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = ['dpkg-source', '-x', str(dsc_path), tmpdir]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=dsc_path.parent)
            if result.returncode != 0:
                raise ValueError(f"Failed to extract {dsc_path}")
            self.read_from_extracted_source(tmpdir)

    def _parse_from_dsc(self, dsc):
        """Parse .dsc deb822 object."""
        self.name = dsc.get('Source')
        self.version = dsc.get('Version')
        self.architecture = dsc.get('Architecture')

        if 'Binary' in dsc:
            self.binary_packages = [p.strip() for p in str(dsc['Binary']).split(',')]
        if 'Build-Depends' in dsc:
            self.build_depends = [d.strip() for d in str(dsc['Build-Depends']).split(',')]
        if 'Build-Depends-Indep' in dsc:
            self.build_depends_indep = [d.strip() for d in str(dsc['Build-Depends-Indep']).split(',')]

    def _parse_control_file(self, control_file):
        """Parse debian/control file using deb822."""
        self.binary_packages = []

        for para in deb822.Deb822.iter_paragraphs(control_file):
            if 'Source' in para:
                # Source paragraph
                if not self.name:
                    self.name = para.get('Source')
                if not self.version and 'Version' in para:
                    self.version = para.get('Version')
                if not self.architecture:
                    self.architecture = para.get('Architecture')
                # Only set build_depends if not already set from .dsc file
                # (.dsc has more complete info including Build-Depends-Arch)
                if not self.build_depends and 'Build-Depends' in para:
                    self.build_depends = [d.strip() for d in str(para['Build-Depends']).split(',')]
                if not self.build_depends_indep and 'Build-Depends-Indep' in para:
                    self.build_depends_indep = [d.strip() for d in str(para['Build-Depends-Indep']).split(',')]
            elif 'Package' in para:
                # Binary package paragraph
                bin_pkg = DebianBinaryPackage()
                bin_pkg.set_from_control_paragraph(para, self.name, self.version)
                self.binary_packages.append(bin_pkg)

    def calculate_code_size(self, source_paths=None):
        """Calculate total source code size in bytes.

        Args:
            source_paths: List of additional source paths not otherwise available from the meta_data

        Returns:
            Total size in bytes
        """

        # If previously calculated, just return that
        if self.code_size is not None:
            return self.code_size

        total_size = 0
        paths_to_scan = []

        # Collect paths from various sources
        if source_paths:
            paths_to_scan.extend(source_paths)

        if self.meta_data:
            meta = self.meta_data if isinstance(self.meta_data, PackageMetadata) else None
            if meta:
                if meta.tarball_size:
                    self.code_size = meta.tarball_size * 3
                    return self.code_size
                if meta.src_path:
                    paths_to_scan.append(meta.src_path)
                paths_to_scan.extend(meta.src_files)
            else:
                if 'tarball_size' in self.meta_data:
                    self.code_size = self.meta_data['tarball_size'] * 3
                    return self.code_size
                if 'src_path' in self.meta_data and self.meta_data['src_path']:
                    paths_to_scan.append(self.meta_data['src_path'])
                if 'src_files' in self.meta_data:
                    paths_to_scan.extend(self.meta_data['src_files'])

        # Calculate size from paths
        for path in paths_to_scan:
            path_obj = Path(path)
            if not path_obj.exists():
                continue

            if path_obj.is_file():
                # Check if it's a tarball
                if path.endswith(('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz')):
                    try:
                        with tarfile.open(path) as tar:
                            total_size += sum(m.size for m in tar.getmembers() if m.isfile())
                    except Exception:
                        # Fallback: estimate as 3x compressed size
                        total_size += path_obj.stat().st_size * 3
                else:
                    total_size += path_obj.stat().st_size
            elif path_obj.is_dir():
                for root, _, files in os.walk(path_obj):
                    for name in files:
                        file_path = Path(root) / name
                        try:
                            total_size += file_path.stat().st_size
                        except Exception:
                            pass

        self.code_size = total_size
        return total_size

    def calculate_compile_complexity(self, source_paths=None):
        """Calculate estimated compile time in minutes.

        Args:
            source_paths: List of additional source paths not otherwise available from the meta_data

        Returns:
            Estimated compile time in minutes
        """
        # If previously calculated, just return that
        if self.compile_complexity is not None:
            return self.compile_complexity

        # Check if explicitly provided in metadata
        if self.meta_data and 'compile_time' in self.meta_data:
            self.compile_complexity = self.meta_data['compile_time']
            return self.compile_complexity

        # Estimate from code size if available
        if self.code_size is None:
            self.calculate_code_size(source_paths=source_paths)

        # Crude estimation: 1 minute per 10MB of source code
        # Adjust based on typical compile times
        estimated_minutes = max(1, self.code_size // (10 * 1024 * 1024))

        self.compile_complexity = estimated_minutes
        return estimated_minutes

