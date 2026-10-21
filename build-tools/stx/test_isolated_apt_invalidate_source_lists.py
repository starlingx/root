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

"""Regression test for IsolatedApt.invalidate_source_lists().

Background: downloader-2.0's binary phase runs binary_apt.update() early,
caching Release/Packages for deb-local-binary as it existed BEFORE this
run's uploads. It then uploads packages and republishes deb-local-binary
via deploy_local() with new content, and finally detach()es binary_apt so
the dependency-analysis phase can reuse its already-fetched upstream
indices (avoiding a slow re-fetch of large Debian snapshot indices).

Without invalidating deb-local-binary's cached lists first, dep-analysis's
reused apt environment carries stale Release/Packages metadata for the
repo that was just republished with different content, which can surface
as a spurious "File has unexpected size... Mirror sync in progress?"
error when apt validates a freshly-fetched file against the stale cached
Release.

This test verifies that invalidate_source_lists(url_substring) removes
only the matching cached list files (leaving unrelated cached sources,
e.g. the upstream Debian snapshot indices, untouched).
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from isolated_apt import IsolatedApt  # noqa: E402


class TestInvalidateSourceLists(unittest.TestCase):

    def setUp(self):
        self.apt = IsolatedApt()
        self.lists_dir = self.apt.var_lib / "lists"

    def tearDown(self):
        self.apt.cleanup()

    def _touch(self, name):
        path = self.lists_dir / name
        path.write_text("stub")
        return path

    def test_removes_only_matching_files(self):
        local_release = self._touch(
            "stx-dev-master-1-stx-repomgr:80_deb-local-binary_dists_trixie_Release")
        local_packages = self._touch(
            "stx-dev-master-1-stx-repomgr:80_deb-local-binary_dists_trixie_main_"
            "binary-amd64_Packages")
        snapshot_release = self._touch(
            "snapshot.debian.org_archive_debian_20260925T082640Z_dists_trixie_"
            "Release")
        snapshot_packages = self._touch(
            "snapshot.debian.org_archive_debian_20260925T082640Z_dists_trixie_"
            "main_binary-amd64_Packages")

        removed = self.apt.invalidate_source_lists('deb-local-binary')

        self.assertIn(local_release.name, removed)
        self.assertIn(local_packages.name, removed)
        self.assertFalse(local_release.exists())
        self.assertFalse(local_packages.exists())

        # Unrelated (upstream snapshot) cached indices must survive untouched.
        self.assertTrue(snapshot_release.exists())
        self.assertTrue(snapshot_packages.exists())

    def test_no_matching_files_is_a_noop(self):
        snapshot_release = self._touch(
            "snapshot.debian.org_archive_debian_20260925T082640Z_dists_trixie_"
            "Release")

        removed = self.apt.invalidate_source_lists('deb-local-binary')

        self.assertEqual(removed, [])
        self.assertTrue(snapshot_release.exists())

    def test_partial_dir_is_never_touched(self):
        partial_dir = self.lists_dir / "partial"
        self.assertTrue(partial_dir.is_dir())
        marker = partial_dir / "deb-local-binary_dists_trixie_Release"
        marker.write_text("in-flight download")

        self.apt.invalidate_source_lists('deb-local-binary')

        # 'partial' itself must never be matched/removed as a top-level
        # entry, even though its name could coincidentally contain the
        # substring in some other setup; here we just assert the directory
        # and its in-progress file are left alone.
        self.assertTrue(partial_dir.is_dir())
        self.assertTrue(marker.exists())

    def test_reuse_after_invalidation_forces_clean_state(self):
        """Simulates the detach()-then-reuse flow: after invalidation, a new
        IsolatedApt constructed against the same base_dir must not see the
        stale list files for the invalidated source."""
        self._touch(
            "stx-dev-master-1-stx-repomgr:80_deb-local-binary_dists_trixie_Release")
        self.apt.invalidate_source_lists('deb-local-binary')
        basedir = self.apt.detach()

        reused = IsolatedApt(base_dir=str(basedir))
        try:
            remaining = [
                p.name for p in (reused.var_lib / "lists").iterdir()
                if p.name != "partial"
            ]
            self.assertEqual(remaining, [])
        finally:
            reused.cleanup()


if __name__ == '__main__':
    unittest.main()
