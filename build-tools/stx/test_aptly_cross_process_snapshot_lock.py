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

"""Regression test for the cross-process aptly snapshot/publish-swap lock.

Background: create_named_snapshot_from_repo() and merge_repos() both do a
drop-publish / rename / recreate-snapshot sequence against a shared aptly
repo. These are called from SEPARATE OS processes/containers with no other
synchronization: build-pkgs-2.0 (once, after all layer/build_type threads
join) and build-image, invoked independently by the 'ISO' and 'remote-cli'
Jenkins stages -- which the monolithic pipeline runs in PARALLEL and which
both snapshot/merge the exact same repo names (REPO_BUILD, deb-merge-all).//
A threading.Lock cannot protect against this: it only ever serializes
callers within the SAME process. Deb_aptly._cross_process_snapshot_lock uses
flock() on a file under a shared filesystem path instead, which is visible
to every process/container that mounts it.

This test proves the lock actually serializes across process boundaries
(via multiprocessing, real separate OS processes -- not threads) and that
it is keyed per snap_name (two different names do not contend with each
other).
"""
import multiprocessing
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import aptly_deb_usage  # noqa: E402


def _acquire_hold_record(lock_dir, snap_name, hold_seconds, start_times, end_times, idx):
    """Helper run in a child process: construct a bare Deb_aptly-like shim
    (we only need _cross_process_snapshot_lock, which depends on nothing but
    self.logger and the module-level lock dir) and hold the lock for
    hold_seconds, recording wall-clock enter/exit times so the parent test
    can assert the two children's held intervals never overlapped.
    """
    import logging
    aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = lock_dir

    class _Shim:
        logger = logging.getLogger('test_lock_child_%d' % idx)
        _cross_process_snapshot_lock = aptly_deb_usage.Deb_aptly._cross_process_snapshot_lock

    shim = _Shim()
    with shim._cross_process_snapshot_lock(snap_name, timeout=10):
        start_times[idx] = time.time()
        time.sleep(hold_seconds)
        end_times[idx] = time.time()


class CrossProcessSnapshotLockTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='aptly_lock_test_')
        self._orig_lock_dir = aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR

    def tearDown(self):
        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._orig_lock_dir

    def test_two_processes_same_name_serialize(self):
        """Two separate OS processes locking the SAME snap_name must never
        both be inside the critical section at the same time."""
        manager = multiprocessing.Manager()
        start_times = manager.dict()
        end_times = manager.dict()

        p0 = multiprocessing.Process(
            target=_acquire_hold_record,
            args=(self._tmpdir, 'deb-local-build', 0.5, start_times, end_times, 0))
        p1 = multiprocessing.Process(
            target=_acquire_hold_record,
            args=(self._tmpdir, 'deb-local-build', 0.5, start_times, end_times, 1))

        p0.start()
        time.sleep(0.1)  # ensure p0 gets there first
        p1.start()
        p0.join(timeout=10)
        p1.join(timeout=10)

        self.assertEqual(p0.exitcode, 0)
        self.assertEqual(p1.exitcode, 0)
        self.assertIn(0, start_times)
        self.assertIn(1, start_times)

        # Non-overlap: one interval must end before (or exactly as) the
        # other starts. If the lock failed to serialize them, both
        # intervals would overlap by ~0.5s instead.
        overlap = min(end_times[0], end_times[1]) - max(start_times[0], start_times[1])
        self.assertLessEqual(overlap, 0,
            "both processes were inside the critical section at the same "
            "time (overlap=%.3fs) -- the cross-process lock did not "
            "serialize them" % overlap)

    def test_two_processes_different_names_do_not_contend(self):
        """Locking DIFFERENT snap_names must not block each other -- the
        lock is keyed per-name, not a single global mutex over all aptly
        operations (which would needlessly serialize unrelated repos)."""
        manager = multiprocessing.Manager()
        start_times = manager.dict()
        end_times = manager.dict()

        p0 = multiprocessing.Process(
            target=_acquire_hold_record,
            args=(self._tmpdir, 'deb-local-build', 0.5, start_times, end_times, 0))
        p1 = multiprocessing.Process(
            target=_acquire_hold_record,
            args=(self._tmpdir, 'deb-merge-all', 0.5, start_times, end_times, 1))

        t0 = time.time()
        p0.start()
        p1.start()
        p0.join(timeout=10)
        p1.join(timeout=10)
        total = time.time() - t0

        self.assertEqual(p0.exitcode, 0)
        self.assertEqual(p1.exitcode, 0)
        # If these contended on one global lock, total would be >= ~1.0s
        # (serialized). Running concurrently, it should finish well under
        # that (close to the 0.5s hold time plus process-start overhead).
        self.assertLess(total, 0.9,
            "different snap_names contended with each other (total=%.3fs) "
            "-- the lock should be keyed per-name" % total)

    def test_lock_file_created_under_configured_dir(self):
        """Sanity check: the lock file actually lands under the configured
        lock directory (not some hardcoded path), so pointing it at a test
        tmpdir (as every other test here does) actually takes effect."""
        import logging

        class _Shim:
            logger = logging.getLogger('test_lock_sanity')
            _cross_process_snapshot_lock = aptly_deb_usage.Deb_aptly._cross_process_snapshot_lock

        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._tmpdir
        shim = _Shim()
        with shim._cross_process_snapshot_lock('some-repo', timeout=5):
            pass
        expected = os.path.join(self._tmpdir, 'snapshot-some-repo.lock')
        self.assertTrue(os.path.isfile(expected),
                        "expected lock file %s to exist" % expected)


class _FakePublish:
    def __init__(self, prefix, distribution):
        self.prefix = prefix
        self.distribution = distribution


def _make_ensure_deployed_shim(published_names, deploy_calls, deploy_raises=False):
    """Minimal Deb_aptly-like object exercising the REAL ensure_deployed
    method (bound unmodified from the class) against fakes for everything
    it touches: is_published_complete, self.aptly.publish.list, and
    deploy_local. published_names is a (shared, e.g. multiprocessing.manager
    list/set-backed) mutable collection simulating aptly's actual publish
    state -- is_published_complete() consults it; a successful deploy_local
    call adds the name to it, matching real aptly's behaviour of making the
    publish visible to subsequent callers.
    """
    import logging

    class _FakeAptlyClient:
        def __init__(self, published_names):
            self._published_names = published_names

        class _Publish:
            def __init__(self, outer):
                self._outer = outer

            def list(self):
                return [_FakePublish(n, 'trixie') for n in self._outer._published_names]

        @property
        def publish(self):
            return self._Publish(self)

    class _Shim:
        logger = logging.getLogger('test_ensure_deployed')
        _cross_process_snapshot_lock = aptly_deb_usage.Deb_aptly._cross_process_snapshot_lock
        ensure_deployed = aptly_deb_usage.Deb_aptly.ensure_deployed

        def __init__(self):
            self.aptly = _FakeAptlyClient(published_names)

        def is_published_complete(self, name):
            return name in published_names

        def deploy_local(self, name, suffix=None, sign=False):
            deploy_calls.append((name, sign))
            if deploy_raises:
                raise RuntimeError('simulated aptly publish.create conflict')
            published_names.append(name)
            return name + ' trixie'

    return _Shim()


def _ensure_deployed_worker(lock_dir, name, published_names, deploy_calls,
                            deploy_delay, barrier, result_queue):
    """Child process: wait at the barrier so both workers reach
    ensure_deployed() at (as close to) the same instant as possible, then
    call it. Returns the result via result_queue."""
    aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = lock_dir
    shim = _make_ensure_deployed_shim(published_names, deploy_calls)
    # Inject an artificial delay inside deploy_local so a racing second
    # caller (if the lock did NOT serialize them) would call deploy_local
    # too, before the first one finishes -- making the race observable.
    orig_deploy_local = shim.deploy_local

    def _slow_deploy_local(n, suffix=None, sign=False):
        time.sleep(deploy_delay)
        return orig_deploy_local(n, suffix=suffix, sign=sign)
    shim.deploy_local = _slow_deploy_local

    barrier.wait()
    result = shim.ensure_deployed(name)
    result_queue.put(result)


class EnsureDeployedCrossProcessLockTests(unittest.TestCase):
    """Regression test for StarlingX monolithic #1219: build-wheels #779 and
    build-iso #793 both called ensure_deployed('deb-local-build') from
    separate processes at nearly the same time. Each independently observed
    is_published_complete() == False (TOCTOU), then both called
    deploy_local() -> aptly publish.create, and the second one failed with
    "prefix/distribution already used by another published repo:
    deb-local-build/trixie". ensure_deployed() must hold
    _cross_process_snapshot_lock across BOTH the check and the deploy call
    (like merge_repos already does) so a second caller that acquires the
    lock after the first finishes sees the NOW-complete publish and takes
    the no-op fast path instead of racing deploy_local.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='aptly_ensure_deployed_test_')
        self._orig_lock_dir = aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR

    def tearDown(self):
        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._orig_lock_dir

    def test_single_caller_deploys_when_absent(self):
        published = []
        calls = []
        shim = _make_ensure_deployed_shim(published, calls)
        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._tmpdir

        result = shim.ensure_deployed('deb-local-build')
        self.assertEqual(result, 'deb-local-build trixie')
        self.assertEqual(calls, [('deb-local-build', True)])

    def test_single_caller_deploy_is_signed(self):
        """Regression for StarlingX monolithic #1221: build-iso #795's
        ensure_deployed('deb-local-build') dropped and recreated the
        publish via the quick/local path with sign_skip=True, silently
        reverting a repo that publish-packages #802 had already signed
        minutes earlier. The subsequent chroot `apt update` then failed
        with "repository ... is not signed". Any (re)deploy performed by
        ensure_deployed must request a REAL signature, not sign_skip."""
        published = []
        calls = []
        shim = _make_ensure_deployed_shim(published, calls)
        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._tmpdir

        shim.ensure_deployed('deb-local-build')
        self.assertEqual(calls, [('deb-local-build', True)],
                         "ensure_deployed's deploy_local call must pass "
                         "sign=True so a (re)publish is never left "
                         "unsigned")

    def test_single_caller_noop_when_already_published(self):
        published = ['deb-local-build']
        calls = []
        shim = _make_ensure_deployed_shim(published, calls)
        aptly_deb_usage._APTLY_SNAPSHOT_LOCK_DIR = self._tmpdir

        result = shim.ensure_deployed('deb-local-build')
        self.assertEqual(result, 'deb-local-build trixie')
        self.assertEqual(calls, [],
                         "already-published repo must take the no-op path "
                         "and never call deploy_local")

    def test_two_processes_racing_only_one_calls_deploy_local(self):
        """The exact #1219 scenario: two separate OS processes both call
        ensure_deployed('deb-local-build') for a repo that is NOT yet
        published, at nearly the same instant. Exactly ONE must actually
        call deploy_local(); the other, serialized behind the lock, must
        see the now-complete publish and take the no-op path."""
        manager = multiprocessing.Manager()
        published_names = manager.list()
        deploy_calls = manager.list()
        barrier = manager.Barrier(2)
        result_queue = manager.Queue()

        p0 = multiprocessing.Process(
            target=_ensure_deployed_worker,
            args=(self._tmpdir, 'deb-local-build', published_names,
                  deploy_calls, 0.3, barrier, result_queue))
        p1 = multiprocessing.Process(
            target=_ensure_deployed_worker,
            args=(self._tmpdir, 'deb-local-build', published_names,
                  deploy_calls, 0.3, barrier, result_queue))

        p0.start()
        p1.start()
        p0.join(timeout=10)
        p1.join(timeout=10)

        self.assertEqual(p0.exitcode, 0)
        self.assertEqual(p1.exitcode, 0)

        results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
        self.assertEqual(len(deploy_calls), 1,
                         "exactly one caller must actually invoke "
                         "deploy_local -- the lock must serialize the "
                         "second caller behind the first, which then sees "
                         "the publish as already complete (got deploy_calls=%s)"
                         % list(deploy_calls))
        self.assertEqual(list(deploy_calls), [('deb-local-build', True)],
                         "the single real deploy must still be signed")
        self.assertEqual(list(published_names), ['deb-local-build'])
        for r in results:
            self.assertEqual(r, 'deb-local-build trixie',
                             "both callers must report success -- neither "
                             "should see the 'already published by another "
                             "process' aptly error")


if __name__ == '__main__':
    unittest.main()
