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
# Copyright (C) 2026 WindRiver Corporation

"""
StarlingX package metadata abstraction.

Provides structured access to meta_data.yaml fields and derived properties
for download, checksumming, and revision calculation.
"""

import os
from pathlib import Path

import yaml


class DownloadItem:
    """A single file to be downloaded."""

    __slots__ = ('name', 'url', 'sha256', 'md5', 'topdir')

    def __init__(self, name, url, sha256=None, md5=None, topdir=None):
        self.name = name
        self.url = url
        self.sha256 = sha256
        self.md5 = md5
        self.topdir = topdir

    @property
    def checksum(self):
        """Preferred checksum as (algorithm, value) tuple."""
        if self.sha256:
            return ('sha256', self.sha256)
        if self.md5:
            return ('md5', self.md5)
        return None


class PackageMetadata:
    """Structured access to a StarlingX package's meta_data.yaml.

    Usage:
        meta = PackageMetadata.load(pkg_dir, os_id, os_codename)
        print(meta.name, meta.version)
        for item in meta.downloads():
            download(item.url, item.name)
    """

    def __init__(self, raw, meta_file, pkg_dir, os_id='debian'):
        self._raw = raw
        self._meta_file = Path(meta_file)
        self._pkg_dir = Path(pkg_dir)
        self._os_id = os_id

    # --- Loading ---

    @classmethod
    def load(cls, pkg_dir, os_id='debian', os_codename='bullseye'):
        """Load meta_data.yaml from the package's relocated os-specific folder."""
        pkg_dir = Path(pkg_dir)
        candidates = [
            pkg_dir / os_id / os_codename / 'meta_data.yaml',
            pkg_dir / os_id / 'all' / 'meta_data.yaml',
            pkg_dir / os_id / 'meta_data.yaml',
        ]
        for path in candidates:
            if path.is_file():
                with open(path) as f:
                    raw = yaml.safe_load(f) or {}
                return cls(raw, path, pkg_dir, os_id)
        raise FileNotFoundError(
            f"No meta_data.yaml found in {pkg_dir}/{os_id}/ "
            f"(tried: {os_codename}/, all/, .)")

    @classmethod
    def from_dict(cls, raw, meta_file='', pkg_dir=''):
        """Create from an already-loaded dict (for testing or compatibility)."""
        return cls(raw, meta_file, pkg_dir)

    # --- Core Properties ---

    @property
    def name(self):
        """Debian source package name (debname or directory name)."""
        return self._raw.get('debname') or self._pkg_dir.name

    @property
    def version(self):
        """Full upstream version string (debver), with trailing '@' stripped."""
        v = self._raw.get('debver', '')
        if not v:
            raise ValueError(f"No debver in {self._meta_file}")
        return v.rstrip('@')

    @property
    def tarball_size(self):
        """Estimated tarball size in bytes, or None."""
        return self._raw.get('tarball_size')

    @property
    def compile_time(self):
        """Explicit compile time estimate in minutes, or None."""
        return self._raw.get('compile_time')

    @property
    def serial(self):
        """Whether this package requires serial (non-parallel) build."""
        return bool(self._raw.get('serial'))

    @property
    def meta_file(self):
        """Path to the meta_data.yaml file."""
        return self._meta_file

    @property
    def recipes_dir(self):
        """The directory containing meta_data.yaml."""
        return self._meta_file.parent

    # --- Package Type ---

    @property
    def package_type(self):
        """Package type: 'dl_hook', 'dl_path', 'src_path', or 'archive'."""
        if 'dl_hook' in self._raw:
            return 'dl_hook'
        if 'dl_path' in self._raw:
            return 'dl_path'
        if 'src_path' in self._raw:
            return 'src_path'
        return 'archive'

    # --- Download ---

    @property
    def dl_hook(self):
        """Custom download hook script path, or None."""
        return self._raw.get('dl_hook')

    @property
    def archive_url(self):
        """Base archive URL for dget/apt-get source, or None."""
        return self._raw.get('archive')

    @property
    def dsc_sha256(self):
        """SHA-256 checksum for .dsc file verification, or None."""
        v = self._raw.get('dsc_sha256')
        return v if v else None

    def downloads(self):
        """List of DownloadItem objects for all files to fetch.

        Includes dl_path and dl_files entries. Empty for src_path/archive types.
        """
        items = []
        if self.primary_download:
            items.append(self.primary_download)
        items.extend(self.extra_downloads)
        return items

    @property
    def primary_download(self):
        """The primary DownloadItem (dl_path), or None."""
        dl_path = self._raw.get('dl_path')
        if not dl_path:
            return None
        return DownloadItem(
            name=dl_path['name'],
            url=dl_path['url'],
            sha256=dl_path.get('sha256sum'),
            md5=dl_path.get('md5sum'),
        )

    @property
    def extra_downloads(self):
        """List of DownloadItem for dl_files entries."""
        items = []
        for name, info in self._raw.get('dl_files', {}).items():
            items.append(DownloadItem(
                name=name,
                url=info['url'],
                sha256=info.get('sha256sum'),
                md5=info.get('md5sum'),
                topdir=info.get('topdir'),
            ))
        return items

    # --- Source Paths ---

    @property
    def src_path(self):
        """Resolved absolute path to upstream source directory, or None."""
        raw = self._raw.get('src_path')
        if not raw:
            return None
        expanded = os.path.expandvars(raw)
        if not os.path.isabs(expanded):
            expanded = str(self._pkg_dir / expanded)
        return expanded

    @property
    def src_files(self):
        """Resolved absolute paths for additional source files/dirs."""
        raw_list = self._raw.get('src_files', [])
        result = []
        for entry in raw_list:
            expanded = os.path.expandvars(entry)
            if not os.path.isabs(expanded):
                expanded = str(self._pkg_dir / expanded)
            result.append(expanded)
        return result

    # --- Revision ---

    @property
    def revision(self):
        """Revision sub-dictionary, or empty dict."""
        return self._raw.get('revision') or {}

    @property
    def dist(self):
        """Distribution string for revision (expanded), or empty string."""
        raw = self.revision.get('dist', '')
        return os.path.expandvars(raw)

    @property
    def stx_patch(self):
        """Manual revision offset (int), or 0."""
        v = self.revision.get('stx_patch', 0)
        return v if isinstance(v, int) else 0

    @property
    def pkg_gitrevcount(self):
        """Whether PKG_GITREVCOUNT is enabled."""
        return bool(self.revision.get('PKG_GITREVCOUNT'))

    @property
    def pkg_base_srcrev(self):
        """Base commit SHA for PKG_GITREVCOUNT, or None."""
        return self.revision.get('PKG_BASE_SRCREV')

    @property
    def src_gitrevcount(self):
        """SRC_GITREVCOUNT config dict, or None."""
        return self.revision.get('SRC_GITREVCOUNT')

    @property
    def files_gitrevcount(self):
        """List of base SHAs for FILES_GITREVCOUNT, or None."""
        return self.revision.get('FILES_GITREVCOUNT')

    @property
    def gitrevcount(self):
        """GITREVCOUNT config dict, or None."""
        return self.revision.get('GITREVCOUNT')

    # --- Rebuild Triggers ---

    @property
    def rebuild_triggers(self):
        """List of build-dep package names whose version affects this package's checksum.

        When present, the resolved versions of these packages are included in
        the checksum calculation, causing a rebuild when a listed dependency
        changes even if this package's source is unchanged.
        """
        return self._raw.get('rebuild_triggers', [])

    # --- Checksumming ---

    def checksum_inputs(self):
        """Paths and files that should be hashed to detect package changes.

        Returns a list of dicts with:
            'type': 'file' or 'dir'
            'path': absolute path string

        Covers: meta_data.yaml itself, deb_patches/, patches/, deb_folder/,
        src_path, and src_files.
        """
        inputs = []
        # The metadata file itself (version/URL changes)
        inputs.append({'type': 'file', 'path': str(self._meta_file)})
        # Patch and debian overlay directories
        deb_dir = self._meta_file.parent
        for subdir in ('deb_patches', 'patches', 'deb_folder'):
            d = deb_dir / subdir
            if d.is_dir():
                inputs.append({'type': 'dir', 'path': str(d)})
        # Source paths
        if self.src_path and os.path.exists(self.src_path):
            inputs.append({'type': 'dir', 'path': self.src_path})
        for sf in self.src_files:
            if os.path.exists(sf):
                t = 'dir' if os.path.isdir(sf) else 'file'
                inputs.append({'type': t, 'path': sf})
        return inputs

    def checksum_file_list(self):
        """Expanded list of all files that contribute to the package checksum.

        Walks the debian directory, src_path, and src_files to produce a flat
        list of absolute file paths. Excludes .git/.tox dirs and .pyc/.pyo files.
        """
        files = []
        # All files under the os-specific directory (e.g. debian/, centos/)
        os_dir = self._pkg_dir / self._os_id
        if os_dir.is_dir():
            for root, dirs, names in os.walk(os_dir):
                dirs[:] = [d for d in dirs if d not in ('.git', '.tox')]
                for name in names:
                    if not name.endswith(('.pyc', '.pyo')):
                        files.append(os.path.abspath(os.path.join(root, name)))
        # src_path
        if self.src_path and os.path.isdir(self.src_path):
            for root, dirs, names in os.walk(self.src_path):
                dirs[:] = [d for d in dirs if d not in ('.git', '.tox')]
                for name in names:
                    if not name.endswith(('.pyc', '.pyo')):
                        files.append(os.path.join(root, name))
        # src_files
        for sf in self.src_files:
            if os.path.isdir(sf):
                for root, dirs, names in os.walk(sf):
                    dirs[:] = [d for d in dirs if d not in ('.git', '.tox')]
                    for name in names:
                        if not name.endswith(('.pyc', '.pyo')):
                            files.append(os.path.join(root, name))
            elif os.path.isfile(sf):
                files.append(sf)
        return files

    # --- Raw Access ---

    def validate_checksums(self):
        """Return list of download files missing checksums."""
        missing = []
        for item in self.downloads():
            if not item.checksum:
                missing.append(item.name)
        return missing

    def get(self, key, default=None):
        """Direct access to raw meta_data fields."""
        return self._raw.get(key, default)

    def __contains__(self, key):
        return key in self._raw

    def __repr__(self):
        return f"PackageMetadata({self.name} {self.version})"
