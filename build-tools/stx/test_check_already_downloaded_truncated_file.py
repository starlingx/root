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
# Copyright (C) 2026 Wind River Systems, Inc.

"""Regression test for downloader-2.0's _check_already_downloaded() trusting
a truncated/corrupt file in the mirror directory as a valid cache hit.

Symptom: StarlingX openssl 3.5.7-1~deb13u2 repack failed with:

    Command 'tar -xzf .../openssl-debian-openssl-3.5.7-1_deb13u2.tar.gz
    --strip-components 1 -C ...' returned non-zero exit status 2.

Root cause: openssl's dl_path source is fetched from Salsa's dynamic
git-archive tarball endpoint (https://salsa.debian.org/.../-/archive/
.../....tar.gz), which has no Content-Length header and generates a large
(~192MB) archive on demand. Live reproduction showed this endpoint
reliably truncates the transfer at a different, growing byte count on
every attempt, while curl exits 0 (the connection closes cleanly, it just
closes early -- no HTTP error, never stalls below the --speed-limit
threshold, so --fail/--retry never trigger).

_check_already_downloaded()'s "Step 2" cache-status check (downloader-2.0)
only verified file *existence* (`.exists()`) for the package's required
download(s) before declaring the mirror cache status 'downloaded' -- never
content integrity. A previous run's truncated download left a corrupt but
correctly-NAMED tarball sitting in the mirror dir. Because the file
existed, download_source() was skipped entirely on the next run (bypassing
debrepack_2_0.py's checksum-verification gate, which only runs *during*
an actual download), and extract_tarball() later ran `tar -xzf` directly
against the truncated file -- failing with exit status 2 far from the
real cause.

downloader-2.0 cannot be imported directly in this environment (it has a
hyphenated filename with no .py extension, and transitively imports the
'progressbar' package, which is not installed outside the real build
venv -- consistent with every other test in this suite that touches
downloader-2.0 logic avoiding a direct import and instead testing the
underlying mechanism). This test exercises the fixed verification logic
itself -- extracted verbatim as a standalone helper mirroring the fix
applied inside _check_already_downloaded()'s "Step 2" block -- against a
real PackageMetadata-shaped meta_data.yaml fixture and real on-disk files.
"""
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from package_metadata import PackageMetadata


def verify_required_downloads_intact(meta, mirror_pkg_dir, required):
    """Standalone copy of the integrity-verification logic added to
    _check_already_downloaded()'s "Step 2" block in downloader-2.0.

    Returns True if every file in `required` exists under `mirror_pkg_dir`
    AND passes checksum verification (or, when no checksum is known for a
    file, is at least non-empty). Returns False otherwise, meaning the
    cache hit must not be trusted and a re-download is required.
    """
    missing = [name for name in required
               if not (mirror_pkg_dir / name).exists()]
    if missing:
        return False

    try:
        checksum_by_name = {item.name: item.checksum
                             for item in meta.downloads() if item.checksum} if meta else {}
    except Exception:
        checksum_by_name = {}

    for name in required:
        file_path = mirror_pkg_dir / name
        check = checksum_by_name.get(name)
        if check:
            algo, expected = check
            hasher = hashlib.sha256() if algo == 'sha256' else hashlib.md5()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    hasher.update(chunk)
            if hasher.hexdigest() != expected:
                return False
        elif file_path.stat().st_size == 0:
            return False

    return True


class CheckAlreadyDownloadedTruncatedFileTests(unittest.TestCase):

    REAL_CONTENT = b'this is the complete, correct tarball content\n' * 100

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='check_already_dl_test_'))

        # Package source tree: <pkg_dir>/debian/trixie/meta_data.yaml
        self.pkg_dir = self.tmpdir / 'src' / 'openssl'
        meta_dir = self.pkg_dir / 'debian' / 'trixie'
        meta_dir.mkdir(parents=True)

        self.real_sha256 = hashlib.sha256(self.REAL_CONTENT).hexdigest()
        self.dl_name = 'openssl-debian-openssl-3.5.7-1_deb13u2.tar.gz'

        self.meta_yaml = meta_dir / 'meta_data.yaml'
        self.meta_yaml.write_text(
            "debver: 3.5.7-1~deb13u2\n"
            "debname: openssl\n"
            "dl_path:\n"
            f"  name: {self.dl_name}\n"
            "  url: https://example.invalid/openssl.tar.gz\n"
            f"  sha256sum: {self.real_sha256}\n"
        )

        self.mirror_pkg_dir = self.tmpdir / 'mirror' / 'openssl'
        self.mirror_pkg_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _load_meta(self):
        return PackageMetadata.load(str(self.pkg_dir), 'debian', 'trixie')

    def _write_mirror_file(self, content):
        (self.mirror_pkg_dir / self.dl_name).write_bytes(content)

    def test_truncated_file_is_not_trusted_as_downloaded(self):
        """A truncated file under the correct name must NOT satisfy the
        download cache check -- this is the exact bug: existence alone was
        treated as proof of a successful download."""
        truncated = self.REAL_CONTENT[: len(self.REAL_CONTENT) // 4]
        self.assertNotEqual(len(truncated), len(self.REAL_CONTENT))
        self._write_mirror_file(truncated)

        meta = self._load_meta()
        required = [item.name for item in meta.downloads()]
        intact = verify_required_downloads_intact(
            meta, self.mirror_pkg_dir, required)

        self.assertFalse(
            intact,
            "A truncated download must force a re-download, not be "
            "accepted as a valid cache hit")

    def test_complete_correct_file_is_trusted_as_downloaded(self):
        """Sanity check: the fix must not regress the normal, intact-file
        case -- a file matching its pinned checksum should still short-
        circuit re-download."""
        self._write_mirror_file(self.REAL_CONTENT)

        meta = self._load_meta()
        required = [item.name for item in meta.downloads()]
        intact = verify_required_downloads_intact(
            meta, self.mirror_pkg_dir, required)

        self.assertTrue(intact)

    def test_zero_byte_file_without_checksum_is_not_trusted(self):
        """When metadata has no checksum at all, fall back to the weakest
        integrity floor: a zero-byte file must still not be trusted."""
        self.meta_yaml.write_text(
            "debver: 3.5.7-1~deb13u2\n"
            "debname: openssl\n"
            "dl_path:\n"
            f"  name: {self.dl_name}\n"
            "  url: https://example.invalid/openssl.tar.gz\n"
        )
        self._write_mirror_file(b'')

        meta = self._load_meta()
        required = [item.name for item in meta.downloads()]
        intact = verify_required_downloads_intact(
            meta, self.mirror_pkg_dir, required)

        self.assertFalse(intact)

    def test_missing_file_is_not_trusted(self):
        """Baseline (pre-existing behavior): a required file that doesn't
        exist at all must never be treated as a cache hit."""
        meta = self._load_meta()
        required = [item.name for item in meta.downloads()]
        intact = verify_required_downloads_intact(
            meta, self.mirror_pkg_dir, required)

        self.assertFalse(intact)


if __name__ == '__main__':
    unittest.main()
