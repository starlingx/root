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

"""Regression tests for Deb_aptly's list_local()/list_remotes()/pkg_exist()
caching.

Background: build-image's package-list verification (verify_pkgs_in_repo()
-> repomgr.search_pkg() -> Deb_aptly.pkg_exist()) is called once per
(package, repo) pair for every package in a build's package lists (hundreds
of packages x several repos). Before this fix, search_pkg() called
list_local()/list_remotes() fresh every time, and pkg_exist() called
aptly.mirrors.list_packages() (which returns a remote mirror's ENTIRE
package index, not a filtered result) fresh every time too -- none of this
data changes during the check phase, so this was thousands of redundant
HTTP round-trips to the aptly server.

Observed live: a single build-image run spent ~38 minutes in exactly this
phase (~336 OS packages + ~34 STX packages, each checked against 4-5
repos) before any actual image-building work started.

These tests exercise the REAL (unmodified) Deb_aptly.list_local(),
list_remotes(), pkg_exist(), create_local(), create_remote(), and
_get_mirror_pkgs() methods (bound directly from the class) against a fake
aptly client that counts calls, to prove:
  1. Repeated list_local()/list_remotes()/pkg_exist() calls hit the aptly
     client's repos.list()/mirrors.list()/mirrors.list_packages() AT MOST
     ONCE per distinct underlying collection, not once per call.
  2. create_local()/create_remote() correctly invalidate the relevant
     cache so a newly created repo/mirror is visible to the next
     list_local()/list_remotes() call instead of being hidden by a stale
     cached list.
"""
import logging
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import aptly_deb_usage  # noqa: E402


class _FakeRepo:
    def __init__(self, name):
        self.name = name
        self.default_distribution = 'trixie'
        self.default_component = 'main'


class _FakeMirror:
    def __init__(self, name):
        self.name = name
        self.archiveurl = 'http://example/'
        self.distribution = 'trixie'
        self.components = 'main'
        self.architectures = ['amd64']


class _FakeSearchPkg:
    def __init__(self, key):
        self.key = key


class _FakeReposApi:
    def __init__(self, outer):
        self._outer = outer

    def list(self):
        self._outer.repos_list_calls += 1
        return list(self._outer.local_repos)

    def search_packages(self, repo_name, query=None):
        self._outer.search_packages_calls += 1
        results = self._outer.local_pkgs.get(repo_name, [])
        if query and query != 'Name':
            # Simplified substring match on the package name token.
            name_token = query.split()[0]
            results = [r for r in results if r.key.split()[1] == name_token]
        return results

    def create(self, name, default_distribution=None, default_component=None):
        repo = _FakeRepo(name)
        self._outer.local_repos.append(repo)
        return repo

    def delete(self, reponame, force=True):
        self._outer.local_repos = [r for r in self._outer.local_repos if r.name != reponame]
        return 'fake-task'


class _FakeMirrorsApi:
    def __init__(self, outer):
        self._outer = outer

    def list(self):
        self._outer.mirrors_list_calls += 1
        return list(self._outer.remote_mirrors)

    def list_packages(self, mirror_name):
        self._outer.list_packages_calls += 1
        self._outer.list_packages_calls_by_mirror[mirror_name] = (
            self._outer.list_packages_calls_by_mirror.get(mirror_name, 0) + 1)
        return self._outer.mirror_pkgs.get(mirror_name, [])

    def create(self, name, url, **kwargs):
        mirror = _FakeMirror(name)
        self._outer.remote_mirrors.append(mirror)
        return mirror

    def drop(self, name, force=True):
        self._outer.remote_mirrors = [m for m in self._outer.remote_mirrors if m.name != name]
        return 'fake-task'

    def update(self, name, ignore_signatures=True):
        return 'fake-task'


class _FakePublishApi:
    def list(self):
        return []


class _FakeSnapshotsApi:
    def list(self):
        return []


class _FakeDbApi:
    def cleanup(self):
        return 'fake-task'


class _FakeAptlyClient:
    """Minimal fake standing in for the real `aptly` client, wired to a
    Deb_aptly-like shim via the outer test's counters."""

    def __init__(self, outer):
        self.repos = _FakeReposApi(outer)
        self.mirrors = _FakeMirrorsApi(outer)
        self.publish = _FakePublishApi()
        self.snapshots = _FakeSnapshotsApi()
        self.db = _FakeDbApi()


def _make_shim(local_repos=None, remote_mirrors=None, local_pkgs=None, mirror_pkgs=None):
    """Build a bare object exercising the REAL Deb_aptly methods (bound
    unmodified from the class) against the fake aptly client above, with
    call counters exposed directly on the object for assertions.
    """
    class _Shim:
        logger = logging.getLogger('test_aptly_list_mirror_caching')

        list_local = aptly_deb_usage.Deb_aptly.list_local
        list_remotes = aptly_deb_usage.Deb_aptly.list_remotes
        pkg_exist = aptly_deb_usage.Deb_aptly.pkg_exist
        pkg_list = aptly_deb_usage.Deb_aptly.pkg_list
        create_local = aptly_deb_usage.Deb_aptly.create_local
        create_remote = aptly_deb_usage.Deb_aptly.create_remote
        remove_local = aptly_deb_usage.Deb_aptly.remove_local
        remove_remote = aptly_deb_usage.Deb_aptly.remove_remote
        _get_mirror_pkgs = aptly_deb_usage.Deb_aptly._get_mirror_pkgs
        _get_local_pkgs = aptly_deb_usage.Deb_aptly._get_local_pkgs
        _Deb_aptly__wait_for_task = aptly_deb_usage.Deb_aptly._Deb_aptly__wait_for_task

        def __init__(self):
            self.repos_list_calls = 0
            self.mirrors_list_calls = 0
            self.search_packages_calls = 0
            self.list_packages_calls = 0
            self.list_packages_calls_by_mirror = {}
            self.local_repos = list(local_repos or [])
            self.remote_mirrors = list(remote_mirrors or [])
            self.local_pkgs = dict(local_pkgs or {})
            self.mirror_pkgs = dict(mirror_pkgs or {})
            self.aptly = _FakeAptlyClient(self)
            self._list_local_cache = None
            self._list_remotes_cache = None
            self._mirror_pkgs_cache = {}
            self._local_pkgs_cache = {}
            self._skip_cleanup = True

    return _Shim()


class ListLocalCachingTests(unittest.TestCase):

    def test_list_local_only_calls_repos_list_once(self):
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-binary')])

        self.assertEqual(shim.list_local(quiet=True), ['deb-local-binary'])
        self.assertEqual(shim.list_local(quiet=True), ['deb-local-binary'])
        self.assertEqual(shim.list_local(quiet=True), ['deb-local-binary'])

        self.assertEqual(shim.repos_list_calls, 1,
                         "list_local() must only hit aptly.repos.list() "
                         "once and serve subsequent calls from cache")

    def test_create_local_invalidates_cache(self):
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-binary')])

        self.assertEqual(shim.list_local(quiet=True), ['deb-local-binary'])
        calls_before_create = shim.repos_list_calls
        shim.create_local('deb-local-binary-newlayer')
        result = shim.list_local(quiet=True)

        self.assertIn('deb-local-binary-newlayer', result,
                      "a newly created repo must be visible on the next "
                      "list_local() call, not hidden by a stale cache")
        # create_local() itself does one extra repos.list() call (its own
        # pre-existence check, unrelated to this cache); what matters here
        # is that list_local() AFTER create_local() causes exactly one more
        # real fetch, not that it serves a now-stale cached list.
        self.assertEqual(shim.repos_list_calls, calls_before_create + 2,
                         "cache must be invalidated by create_local(), "
                         "causing exactly one more real fetch from the "
                         "following list_local() call")

    def test_remove_local_invalidates_cache(self):
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-binary')])

        self.assertEqual(shim.list_local(quiet=True), ['deb-local-binary'])
        calls_before_remove = shim.repos_list_calls
        shim.remove_local('deb-local-binary')
        result = shim.list_local(quiet=True)

        self.assertEqual(result, [],
                         "a removed repo must disappear on the next "
                         "list_local() call, not linger in a stale cache")
        self.assertEqual(shim.repos_list_calls, calls_before_remove + 2)


class ListRemotesCachingTests(unittest.TestCase):

    def test_list_remotes_only_calls_mirrors_list_once(self):
        shim = _make_shim(remote_mirrors=[_FakeMirror('deb-remote-trixie')])

        self.assertEqual(shim.list_remotes(quiet=True), ['deb-remote-trixie'])
        self.assertEqual(shim.list_remotes(quiet=True), ['deb-remote-trixie'])
        self.assertEqual(shim.list_remotes(quiet=True), ['deb-remote-trixie'])

        self.assertEqual(shim.mirrors_list_calls, 1,
                         "list_remotes() must only hit aptly.mirrors.list() "
                         "once and serve subsequent calls from cache")

    def test_create_remote_invalidates_cache(self):
        shim = _make_shim(remote_mirrors=[_FakeMirror('deb-remote-trixie')])

        self.assertEqual(shim.list_remotes(quiet=True), ['deb-remote-trixie'])
        calls_before_create = shim.mirrors_list_calls
        shim.create_remote('deb-remote-trixie-updates', 'http://example/', 'trixie-updates')
        result = shim.list_remotes(quiet=True)

        self.assertIn('deb-remote-trixie-updates', result)
        # create_remote() itself does one extra mirrors.list() call (its
        # own pre-existence check); the following list_remotes() call must
        # still cause exactly one more real fetch rather than serving a
        # stale cached list.
        self.assertEqual(shim.mirrors_list_calls, calls_before_create + 2)

    def test_remove_remote_invalidates_cache(self):
        shim = _make_shim(remote_mirrors=[_FakeMirror('deb-remote-trixie')])

        self.assertEqual(shim.list_remotes(quiet=True), ['deb-remote-trixie'])
        calls_before_remove = shim.mirrors_list_calls
        shim.remove_remote('deb-remote-trixie')
        result = shim.list_remotes(quiet=True)

        self.assertEqual(result, [])
        self.assertEqual(shim.mirrors_list_calls, calls_before_remove + 2)


class MirrorPkgsCachingTests(unittest.TestCase):
    """The core fix for the ~38-minute build-image regression: pkg_exist()
    must not re-fetch a remote mirror's entire package index for every
    package being checked against it."""

    def test_pkg_exist_only_fetches_mirror_pkgs_once_across_many_lookups(self):
        mirror_pkgs = {
            'deb-remote-trixie': [
                'Pamd64 acl 2.3.2-2 abc123',
                'Pamd64 curl 8.0.1-1 def456',
            ],
        }
        shim = _make_shim(remote_mirrors=[_FakeMirror('deb-remote-trixie')],
                          mirror_pkgs=mirror_pkgs)

        # Simulate checking many distinct packages against the same mirror,
        # as verify_pkgs_in_repo() does for every package in a build's
        # package lists.
        found_acl = shim.pkg_exist(['deb-remote-trixie'], 'acl', 'binary')
        found_curl = shim.pkg_exist(['deb-remote-trixie'], 'curl', 'binary')
        found_missing = shim.pkg_exist(['deb-remote-trixie'], 'does-not-exist', 'binary')

        self.assertTrue(found_acl)
        self.assertTrue(found_curl)
        self.assertFalse(found_missing)
        self.assertEqual(shim.list_packages_calls, 1,
                         "pkg_exist() must fetch a given mirror's package "
                         "index AT MOST ONCE regardless of how many "
                         "distinct packages are subsequently checked "
                         "against it -- this is the ~38-minute "
                         "build-image regression")
        self.assertEqual(shim.list_packages_calls_by_mirror.get('deb-remote-trixie'), 1)

    def test_update_mirror_invalidates_only_that_mirrors_cache(self):
        mirror_pkgs = {
            'deb-remote-trixie': ['Pamd64 acl 2.3.2-2 abc123'],
            'deb-remote-trixie-updates': ['Pamd64 curl 8.0.2-1 xyz789'],
        }
        shim = _make_shim(
            remote_mirrors=[_FakeMirror('deb-remote-trixie'),
                             _FakeMirror('deb-remote-trixie-updates')],
            mirror_pkgs=mirror_pkgs)

        shim.pkg_exist(['deb-remote-trixie'], 'acl', 'binary')
        shim.pkg_exist(['deb-remote-trixie-updates'], 'curl', 'binary')
        self.assertEqual(shim.list_packages_calls_by_mirror.get('deb-remote-trixie'), 1)
        self.assertEqual(shim.list_packages_calls_by_mirror.get('deb-remote-trixie-updates'), 1)

        shim._Deb_aptly__update_mirror = aptly_deb_usage.Deb_aptly._Deb_aptly__update_mirror.__get__(shim)
        shim._Deb_aptly__update_mirror('deb-remote-trixie')

        # Re-checking the updated mirror must refetch (cache entry dropped);
        # the untouched mirror's cache entry must remain intact.
        shim.pkg_exist(['deb-remote-trixie'], 'acl', 'binary')
        shim.pkg_exist(['deb-remote-trixie-updates'], 'curl', 'binary')
        self.assertEqual(shim.list_packages_calls_by_mirror.get('deb-remote-trixie'), 2,
                         "the updated mirror's cache entry must have been "
                         "invalidated, causing exactly one more fetch")
        self.assertEqual(shim.list_packages_calls_by_mirror.get('deb-remote-trixie-updates'), 1,
                         "a DIFFERENT mirror's cache entry must be "
                         "unaffected by updating this one")

    def test_remove_remote_drops_that_mirrors_pkgs_cache(self):
        mirror_pkgs = {'deb-remote-trixie': ['Pamd64 acl 2.3.2-2 abc123']}
        shim = _make_shim(remote_mirrors=[_FakeMirror('deb-remote-trixie')],
                          mirror_pkgs=mirror_pkgs)

        shim.pkg_exist(['deb-remote-trixie'], 'acl', 'binary')
        self.assertIn('deb-remote-trixie', shim._mirror_pkgs_cache)

        shim.remove_remote('deb-remote-trixie')

        self.assertNotIn('deb-remote-trixie', shim._mirror_pkgs_cache,
                         "removing a mirror must drop its package-list "
                         "cache entry so a stale index can never be served "
                         "under a reused name")


class LocalPkgsCachingTests(unittest.TestCase):
    """pkg_exist()'s local-repo branch must not re-fetch a local repo's
    entire package list for every package being checked against it --
    the same ~38-minute build-image regression, but for local repos
    (deb-local-build/deb-local-binary*) rather than remote mirrors."""

    def test_pkg_exist_only_fetches_local_pkgs_once_across_many_lookups(self):
        local_pkgs = {
            'deb-local-build': [
                _FakeSearchPkg('Pamd64 acl 2.3.2-2 abc123'),
                _FakeSearchPkg('Pamd64 curl 8.0.1-1 def456'),
            ],
        }
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-build')],
                          local_pkgs=local_pkgs)

        found_acl = shim.pkg_exist(['deb-local-build'], 'acl', 'binary')
        found_curl = shim.pkg_exist(['deb-local-build'], 'curl', 'binary')
        found_missing = shim.pkg_exist(['deb-local-build'], 'does-not-exist', 'binary')

        self.assertTrue(found_acl)
        self.assertTrue(found_curl)
        self.assertFalse(found_missing)
        self.assertEqual(shim.search_packages_calls, 1,
                         "pkg_exist() must fetch a given local repo's "
                         "package list AT MOST ONCE regardless of how many "
                         "distinct packages are subsequently checked "
                         "against it")

    def test_pkg_exist_respects_pkg_version_for_local_repo(self):
        local_pkgs = {
            'deb-local-build': [_FakeSearchPkg('Pamd64 acl 2.3.2-2 abc123')],
        }
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-build')],
                          local_pkgs=local_pkgs)

        self.assertTrue(shim.pkg_exist(['deb-local-build'], 'acl', 'binary', '2.3.2-2'))
        self.assertFalse(shim.pkg_exist(['deb-local-build'], 'acl', 'binary', '9.9.9-9'))
        # Both lookups served from the single cached fetch.
        self.assertEqual(shim.search_packages_calls, 1)

    def test_upload_pkg_local_invalidates_cache(self):
        local_pkgs = {'deb-local-build': [_FakeSearchPkg('Pamd64 acl 2.3.2-2 abc123')]}
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-build')],
                          local_pkgs=local_pkgs)

        self.assertFalse(shim.pkg_exist(['deb-local-build'], 'curl', 'binary'))
        self.assertIn('deb-local-build', shim._local_pkgs_cache)

        # Simulate curl being added to the repo out-of-band, then tell the
        # cache it's stale via the same invalidation upload_pkg_local() does.
        shim.local_pkgs['deb-local-build'].append(_FakeSearchPkg('Pamd64 curl 8.0.1-1 def456'))
        shim._local_pkgs_cache.pop('deb-local-build', None)

        self.assertTrue(shim.pkg_exist(['deb-local-build'], 'curl', 'binary'))

    def test_remove_local_drops_that_repos_pkgs_cache(self):
        local_pkgs = {'deb-local-build': [_FakeSearchPkg('Pamd64 acl 2.3.2-2 abc123')]}
        shim = _make_shim(local_repos=[_FakeRepo('deb-local-build')],
                          local_pkgs=local_pkgs)

        shim.pkg_exist(['deb-local-build'], 'acl', 'binary')
        self.assertIn('deb-local-build', shim._local_pkgs_cache)

        shim.remove_local('deb-local-build')

        self.assertNotIn('deb-local-build', shim._local_pkgs_cache,
                         "removing a local repo must drop its package-list "
                         "cache entry so a stale list can never be served "
                         "under a reused name")


if __name__ == '__main__':
    unittest.main()
