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

"""Regression test for __publish_snap's multi-component switch bug.

Background: deploy_local(name) with suffix=None (used by downloader-2.0 to
publish deb-local-binary) routes through __publish_snap, which atomically
re-points an EXISTING publish to a new snapshot via aptly's
PUT /api/publish/:prefix/:distribution (snapshots=[{Name: ...}, ...]).

Per Aptly's own API docs (https://www.aptly.info/doc/api/publish/,
"Update Published Local Repo/Switch Published Snapshot"): "for multiple
component published repository, `Component` would be guessed from source
if not set" -- but that guess resolves to the DEFAULT/UNNAMED component,
never a named one like 'main', for a multi-component publish. Previously,
__publish_snap always called publish.update(snapshots=[{'name': snap}])
with NO Component key, so every switch only ever advanced the unnamed
component -- any OTHER named component (e.g. 'main') on the same publish
was permanently frozen at whatever snapshot was active when it was first
created, and never updated again by any subsequent call.

Observed live on StarlingX deb-local-binary (confirmed via `aptly publish
list` against the real server data directory): the publish had TWO
sources -- {: [snap-ADVANCING]} (unnamed, correctly tracked every run) and
{main: [snap-FROZEN]} (never advanced past the one snapshot active when it
was created) -- while the Release file's Components: field (which is what
apt/debootstrap actually reads) said 'main'. Every consumer therefore
read permanently stale content from the frozen 'main' component no matter
how many times downloader-2.0 successfully ran deploy_local() and reported
success -- causing sbuild-createchroot's debootstrap to fail with
"E: Couldn't download .../main/binary-amd64/Packages" / "was corrupt" /
"File has unexpected size", intermittently, depending on how much the
unnamed component's (irrelevant, unread) content had drifted from main's
frozen snapshot.

The fix: __publish_snap now reads the EXISTING publish's real component
list (from the live publish.list() response, not guessed) and builds one
Snapshots entry per existing component, all pointed at the new snapshot --
so a switch call advances every component a consumer might actually read,
not just the default/unnamed one.
"""
import logging
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import aptly_deb_usage  # noqa: E402

if aptly_deb_usage.DEBIAN_DISTRIBUTION is None:
    aptly_deb_usage.DEBIAN_DISTRIBUTION = 'trixie'


class _FakePublish:
    def __init__(self, prefix, distribution, sources):
        self.prefix = prefix
        self.distribution = distribution
        self.sources = sources


class _FakePublishApi:
    def __init__(self, outer):
        self._outer = outer

    def list(self):
        return list(self._outer.existing_publishes)

    def update(self, prefix, distribution, snapshots=None, skip_contents=True,
               skip_cleanup=True, force_overwrite=True, **sign_kwargs):
        self._outer.update_calls.append(
            {'prefix': prefix, 'distribution': distribution,
             'snapshots': snapshots})
        return 'fake-task'

    def publish(self, source_kind='local', sources=None, prefix=None,
                distribution=None, architectures=None, label=None,
                origin=None, skip_contents=True, **sign_kwargs):
        self._outer.create_calls.append(
            {'prefix': prefix, 'sources': sources})
        return 'fake-task'

    def drop(self, prefix, distribution, force_delete=True):
        self._outer.drop_calls.append(prefix)
        return 'fake-task'


class _FakeMirrorsApi:
    def list(self):
        return []


class _FakeAptlyClient:
    def __init__(self, outer):
        self.publish = _FakePublishApi(outer)
        self.mirrors = _FakeMirrorsApi()


def _make_shim(existing_publishes):
    class _Shim:
        logger = logging.getLogger('test_publish_snap_multi_component')
        _Deb_aptly__publish_snap = aptly_deb_usage.Deb_aptly._Deb_aptly__publish_snap
        _Deb_aptly__wait_for_task = aptly_deb_usage.Deb_aptly._Deb_aptly__wait_for_task

        def __init__(self):
            self.existing_publishes = list(existing_publishes)
            self.update_calls = []
            self.create_calls = []
            self.drop_calls = []
            self.aptly = _FakeAptlyClient(self)
            self.origin = 'starlingx'

        def _Deb_aptly__wait_for_task(self, task, count=10, quiet_fail=False):
            return 'SUCCEEDED'

    return _Shim()


class PublishSnapMultiComponentTests(unittest.TestCase):

    def test_switch_advances_every_existing_component(self):
        """Real StarlingX bug: a publish with a named ('main') AND an
        unnamed component. The switch call must advance BOTH to the new
        snapshot, not just the unnamed one."""
        existing = [_FakePublish(
            'deb-local-binary', 'trixie',
            sources=[{'Name': 'deb-local-binary-OLD-UNNAMED'},
                     {'Component': 'main', 'Name': 'deb-local-binary-OLD-MAIN'}])]
        shim = _make_shim(existing)

        shim._Deb_aptly__publish_snap('deb-local-binary', snap_name='deb-local-binary-NEW')

        self.assertEqual(len(shim.update_calls), 1)
        snapshots = shim.update_calls[0]['snapshots']
        # Every entry must target the SAME new snapshot.
        names = {s.get('Name') for s in snapshots}
        self.assertEqual(names, {'deb-local-binary-NEW'},
                         "all components must be switched to the same new "
                         "snapshot in one atomic call")
        # The named component ('main') must be explicitly present and
        # explicitly targeted -- this is the exact fix: previously no
        # Component key was ever sent, so 'main' was never advanced.
        components = {s.get('Component') for s in snapshots}
        self.assertIn('main', components,
                      "the existing named component 'main' must be "
                      "explicitly included in the switch, or it is "
                      "silently orphaned at its old snapshot forever")
        # The unnamed component must also still be present (no Component
        # key, or an empty one) -- the fix must not drop it either.
        self.assertTrue(
            any(s.get('Component') in (None, '') for s in snapshots),
            "the pre-existing unnamed component must still be included")

    def test_switch_single_component_publish_still_works(self):
        """The common, single-component case (e.g. deb-local-build) must
        continue to work exactly as before -- one Snapshots entry, still
        correctly targeting the new snapshot."""
        existing = [_FakePublish(
            'deb-local-build', 'trixie',
            sources=[{'Name': 'deb-local-build-OLD'}])]
        shim = _make_shim(existing)

        shim._Deb_aptly__publish_snap('deb-local-build', snap_name='deb-local-build-NEW')

        self.assertEqual(len(shim.update_calls), 1)
        snapshots = shim.update_calls[0]['snapshots']
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].get('Name'), 'deb-local-build-NEW')

    def test_no_drop_recreate_when_switch_succeeds(self):
        """The atomic switch must not fall through to a destructive
        drop+recreate when it succeeds (regression guard for the whole
        point of __publish_snap existing)."""
        existing = [_FakePublish(
            'deb-local-binary', 'trixie',
            sources=[{'Name': 'deb-local-binary-OLD-UNNAMED'},
                     {'Component': 'main', 'Name': 'deb-local-binary-OLD-MAIN'}])]
        shim = _make_shim(existing)

        shim._Deb_aptly__publish_snap('deb-local-binary', snap_name='deb-local-binary-NEW')

        self.assertEqual(shim.drop_calls, [])
        self.assertEqual(shim.create_calls, [])


if __name__ == '__main__':
    unittest.main()
