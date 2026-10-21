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

"""Regression test for StarlingX build-image's unsigned-repo-after-snapshot
bug.

Background: build-image calls repo_manager.ensure_deployed(repo) followed
immediately by repo_manager.snapshot_repo(repo) for REPO_BUILD and every
deb-local-binary-* layer repo. snapshot_repo() -> create_named_snapshot_
from_repo() -> _create_named_snapshot_from_repo_impl() drops the repo's
live publish to free up the snapshot name, creates the snapshot, then
calls _republish_quick_best_effort() to restore chroot-visibility.

_republish_quick_best_effort() called __quick_publish_repo() with its
default sign=False (sign_skip=True), unconditionally. Since ensure_deployed()
immediately beforehand already confirmed each repo signed and complete
(a no-op fast path), snapshot_repo() calling this unconditionally-unsigned
re-publish right after would silently revert EVERY one of those repos back
to unsigned moments later -- apt in the chroot then hard-fails with
"repository ... is not signed" for all of them.

Observed live: a single interactive build-image run signed/no-op-confirmed
deb-local-build, deb-local-binary, deb-local-binary-compiler, -distro, and
-flock via ensure_deployed(), then snapshot_repo() immediately re-published
all 5 unsigned via this path, and the chroot's `apt update` failed with
"Error: The repository '...' is not signed" for all 5 repos a few seconds
later in the same run.

This test exercises the REAL (unmodified) _republish_quick_best_effort()
and __quick_publish_repo() methods, bound directly from the Deb_aptly
class, against a fake aptly client that records whether each publish call
was signed, to prove the re-publish is now always signed.
"""
import logging
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import aptly_deb_usage  # noqa: E402

# __quick_publish_repo()'s first-time-create return path concatenates
# DEBIAN_DISTRIBUTION (normally set from the DEBIAN_DISTRIBUTION env var at
# module import time by the real build tooling) onto the publish name.
# Ensure it's set so these tests exercise the real method's return path
# rather than failing on an unrelated None-concatenation TypeError.
if aptly_deb_usage.DEBIAN_DISTRIBUTION is None:
    aptly_deb_usage.DEBIAN_DISTRIBUTION = 'trixie'


class _FakePublish:
    def __init__(self, prefix, distribution):
        self.prefix = prefix
        self.distribution = distribution


class _FakeRepo:
    def __init__(self, name):
        self.name = name


class _FakePublishApi:
    def __init__(self, outer):
        self._outer = outer

    def list(self):
        return [_FakePublish(p, 'trixie') for p in self._outer.published_names]

    def update(self, prefix, distribution, skip_contents=True, skip_cleanup=True,
               force_overwrite=True, **sign_kwargs):
        self._outer.publish_calls.append((prefix, sign_kwargs))
        return 'fake-task'

    def publish(self, source_kind='local', sources=None, prefix=None,
                distribution=None, architectures=None, label=None,
                origin=None, skip_contents=True, **sign_kwargs):
        self._outer.publish_calls.append((prefix, sign_kwargs))
        self._outer.published_names.append(prefix)
        return 'fake-task'

    def drop(self, prefix, distribution, force_delete=True):
        return 'fake-task'


class _FakeReposApi:
    def __init__(self, outer):
        self._outer = outer

    def list(self):
        return [_FakeRepo(n) for n in self._outer.existing_repos]


class _FakeAptlyClient:
    def __init__(self, outer):
        self.publish = _FakePublishApi(outer)
        self.repos = _FakeReposApi(outer)


def _make_shim(published_names, existing_repos):
    class _Shim:
        logger = logging.getLogger('test_republish_quick_best_effort_signing')
        _Deb_aptly__quick_publish_repo = aptly_deb_usage.Deb_aptly._Deb_aptly__quick_publish_repo
        _Deb_aptly__wait_for_task = aptly_deb_usage.Deb_aptly._Deb_aptly__wait_for_task
        _republish_quick_best_effort = aptly_deb_usage.Deb_aptly._republish_quick_best_effort

        def __init__(self):
            self.publish_calls = []
            self.published_names = list(published_names)
            self.existing_repos = list(existing_repos)
            self.aptly = _FakeAptlyClient(self)
            self.origin = None

    return _Shim()


class RepublishQuickBestEffortSigningTests(unittest.TestCase):
    """Regression test for the all-5-repos-unsigned build-image failure."""

    def test_republish_after_publish_already_existed_is_signed(self):
        """The common case: snapshot_repo() dropped a publish that already
        existed (the repo was previously published, e.g. by ensure_deployed
        or an earlier build-packages signing pass), so the re-publish takes
        the incremental publish.update() path."""
        shim = _make_shim(published_names=['deb-local-build'],
                          existing_repos=['deb-local-build'])

        shim._republish_quick_best_effort('deb-local-build')

        self.assertEqual(len(shim.publish_calls), 1)
        prefix, sign_kwargs = shim.publish_calls[0]
        self.assertEqual(prefix, 'deb-local-build')
        self.assertNotIn('sign_skip', sign_kwargs,
                         "re-publish after a snapshot-induced drop must be "
                         "signed, not sign_skip=True -- an unsigned "
                         "re-publish here silently reverts a repo that "
                         "ensure_deployed/build-packages already signed, "
                         "and apt then fails with 'repository ... is not "
                         "signed' for every consumer")
        self.assertIn('sign_gpgkey', sign_kwargs)
        self.assertEqual(sign_kwargs.get('sign_gpgkey'), aptly_deb_usage.SIGN_KEY)
        self.assertEqual(sign_kwargs.get('sign_passphrase'), aptly_deb_usage.SIGN_PASSWD)

    def test_republish_first_time_create_is_signed(self):
        """The less common case: no publish currently exists under this
        name (e.g. it was fully dropped with nothing left to update), so
        the re-publish takes the first-time publish.publish() (create)
        path instead of publish.update()."""
        shim = _make_shim(published_names=[], existing_repos=['deb-local-binary'])

        shim._republish_quick_best_effort('deb-local-binary')

        self.assertEqual(len(shim.publish_calls), 1)
        prefix, sign_kwargs = shim.publish_calls[0]
        self.assertEqual(prefix, 'deb-local-binary')
        self.assertNotIn('sign_skip', sign_kwargs,
                         "the first-time-create re-publish path must also "
                         "be signed, matching the incremental-update path")
        self.assertEqual(sign_kwargs.get('sign_gpgkey'), aptly_deb_usage.SIGN_KEY)


if __name__ == '__main__':
    unittest.main()
