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
# Copyright (C) 2021-2022  WindRiver Corporation
#
# Requires aptly-api-client:
# https://github.com/masselstine/aptly-api-client
#
# Old document of relate RESTAPI:
# https://www.aptly.info/doc/api/
#
# Realization of its real RESTAPI(go)
# https://github.com/molior-dbs/aptly
from aptly_api import Client
from aptly_api.parts.tasks import Task
from aptly_api.parts.packages import Package
from debian import debian_support
import contextlib
import fcntl
import os
import time
import uuid
from typing import Optional, NamedTuple

# Cross-PROCESS lock directory for aptly snapshot/publish-swap operations.
#
# create_named_snapshot_from_repo() is called from multiple SEPARATE
# processes/containers that all share this filesystem but have no other
# synchronization between them: build-pkgs-2.0 (once per layer/build_type
# thread -- now deferred to a single post-join call, see build_layers) and
# build-image, invoked independently by the 'ISO' and 'remote-cli' Jenkins
# stages, which the monolithic pipeline runs in PARALLEL (stage('X1') {
# parallel { stage('ISO') {...} stage('remote-cli') {...} } }). Both ISO's
# and remote-cli's build-image calls snapshot the SAME repo names
# (REPO_BUILD and each deb-local-binary-*), so two build-image processes can
# race the exact same drop-publish/rename/recreate sequence against each
# other with no in-process lock (self._repo_lock, a threading.Lock) able to
# see across that process boundary. An in-process lock also cannot prevent
# process A's "is a drop even needed" check from running concurrently with
# process B's in-flight drop -- the check-then-act sequence must be inside
# the SAME critical section as the act itself, which is why the lock in
# create_named_snapshot_from_repo wraps the list() calls too, not just the
# delete/create calls.
#
# /localdisk is the one filesystem every one of these processes/containers
# mounts in common (confirmed: build-pkgs-2.0 and build-image both resolve
# their working roots under it), so a flock()'d file there is visible to
# all of them regardless of which pod/container issued the call -- unlike
# threading.Lock, which only ever protects callers inside the SAME process.
_APTLY_SNAPSHOT_LOCK_DIR = '/localdisk/.aptly_locks'

PREFIX_LOCAL = 'deb-local-'
PREFIX_REMOTE = 'deb-remote-'
PREFIX_MERGE = 'deb-merge-'
SIGN_KEY = '8C58D092AD39022571D1F57AFA689A0116E3E718'
SIGN_PASSWD = 'starlingx'
DEFAULT_TIMEOUT_COUNT = 1
STX_DIST = os.environ.get('STX_DIST')
DEBIAN_DISTRIBUTION = os.environ.get('DEBIAN_DISTRIBUTION')

# Class used to manage aptly data base, it can:
#     create_remote: Create a repository link to a remote mirror
#     deploy_remote: Sync and deploy a remote mirror
#     list_remotes:  List all remote repositories
#     remove_remote: Delete a remote repository
#     create_local: Create a local repository
#     upload_pkg_local: Upload a deb package into a local repository
#     delete_pkg_local: Remove a deb package from a local repository
#     pkg_exist: Search a package in a set of repos
#     copy_pkgs: Copy packages from one repo to another
#     deploy_local: Deploy a local repository
#     list_local: List all local repositories
#     remove_local: Delete a local repository
#     clean_all: Clean all meta data including repo, public, distribution, package, task


class Deb_aptly():
    def __init__(self, url, origin, logger):
        '''The basic interface to manage aptly database. '''
        self.logger = logger
        self.url = url
        self.aptly = Client(self.url, timeout=300)
        self.logger.info('Aptly connected, version: %s', self.aptly.misc.version())
        self._skip_cleanup = False
        self._db_health_check()
        if origin:
            self.origin = origin.strip() or None
        else:
            self.origin = None
        # Caches for list_local()/list_remotes() and per-mirror package
        # indexes. These never change spontaneously -- only this class's own
        # create/remove/update methods add, drop, or resync a repo/mirror --
        # so caching them for the lifetime of this object (invalidated at
        # exactly those call sites) is safe and removes a massive amount of
        # redundant aptly HTTP traffic. Observed live: build-image's package
        # list verification (pkg_exist()/search_pkg(), via
        # verify_pkgs_in_repo()) was re-fetching the full local-repo and
        # mirror name lists, AND re-fetching each remote mirror's entire
        # package index (list_packages(), which returns the whole mirror's
        # Packages file -- potentially tens of thousands of entries) from
        # scratch for every single (package, repo) pair being checked. For
        # ~370 packages across ~4-5 repos that's ~1,500+ redundant full
        # list/list_packages HTTP round-trips, measured taking ~38 minutes
        # wall-clock before the actual image build even starts.
        self._list_local_cache = None
        self._list_remotes_cache = None
        self._mirror_pkgs_cache = {}
        # Local-repo package-key cache, keyed by repo name -- mirrors
        # _mirror_pkgs_cache but for aptly.repos.search_packages() instead of
        # aptly.mirrors.list_packages(). Without this, pkg_exist()'s local-repo
        # branch issued one live search_packages() HTTP call per (package,
        # local repo) pair checked -- observed live via build-image's package
        # verification loop: ~150-350ms per call, repeated for every package
        # against deb-local-build/deb-local-binary/etc, well after the mirror
        # side of this same cache was already in place. A local repo's content
        # only changes via this class's own upload/delete/copy methods (which
        # invalidate the matching entry below) or clean_all()/remove_local()
        # (which drop it entirely), so caching for the object's lifetime
        # between those points is safe.
        self._local_pkgs_cache = {}
        # Parallel cache of DETAILED package lists (includes SHA256 in
        # .fields), used by the upload pre-filter's tier-2 hash verification.
        # Invalidated at the same points as _local_pkgs_cache.
        self._local_pkgs_detailed_cache = {}

    @contextlib.contextmanager
    def _cross_process_snapshot_lock(self, snap_name, timeout=300):
        '''Cross-process (and cross-container) mutex, keyed by snap_name, held
        for the ENTIRE duration of a snapshot-swap operation -- both the
        "is this needed" check (snapshots.list/publish.list) and the
        drop/rename/create itself. See _APTLY_SNAPSHOT_LOCK_DIR's module-level
        docstring for why this must be filesystem-based rather than a
        threading.Lock.

        Blocks (with a bounded timeout) rather than failing fast: the other
        side is expected to finish its own swap in well under a minute (these
        are small metadata operations, not package uploads), so waiting here
        is the right behaviour -- the whole point is that both callers must
        serialize, not that the loser should give up.
        '''
        os.makedirs(_APTLY_SNAPSHOT_LOCK_DIR, exist_ok=True)
        lock_path = os.path.join(_APTLY_SNAPSHOT_LOCK_DIR, 'snapshot-%s.lock' % snap_name)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            deadline = time.time() + timeout
            acquired = False
            while time.time() < deadline:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(1)
            if not acquired:
                # Fall through and take the lock blockingly rather than give
                # up entirely -- better to wait past the soft timeout (just
                # logging loudly) than to let two processes run the swap
                # concurrently, which is the exact bug this lock exists to
                # prevent.
                self.logger.warning(
                    '_cross_process_snapshot_lock: %ds and still waiting for '
                    'lock on %s (another process is taking unusually long) '
                    '-- continuing to wait', timeout, snap_name)
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


    def set_skip_cleanup(self, skip):
        '''Control whether deploy_local skips db.cleanup after each publish.
        Call db_cleanup() manually once after a batch of operations.'''
        self._skip_cleanup = skip

    def db_cleanup(self):
        '''Run db.cleanup explicitly (e.g. once after startup batch).'''
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)

    def _db_health_check(self):
        '''Verify the aptly database is usable and clear any stale tasks.

        If the DB is corrupt (e.g. Go time format mismatch after an
        aptly/container upgrade), detect it early and provide a clear
        error message with recovery instructions.

        Also clears any running/pending tasks left over from a previous
        interrupted session. Aptly processes tasks sequentially, so a
        stale task blocks all subsequent API calls indefinitely.
        '''
        try:
            self.aptly.repos.list()
        except Exception as e:
            err_str = str(e)
            if 'UnmarshalBinary' in err_str or ('500' in err_str and 'Internal Server Error' in err_str):
                recovery_msg = (
                    'Aptly database is corrupt: %s\n'
                    'This typically happens when reusing a workspace created '
                    'by a different version of the build tools.\n'
                    'To fix, run the following commands outside of "stx shell":\n'
                    "  stx shell --container repomgr -c 'rm -rf /var/aptly/db'\n"
                    '  stx control stop --wait\n'
                    '  stx control start --wait\n' % e)
                self.logger.error(recovery_msg)
                raise Exception(recovery_msg) from e
            raise

        # Clear stale tasks from a previous interrupted session
        try:
            tasks = self.aptly.tasks.list()
            if tasks:
                running = [t for t in tasks if t.state in ('RUNNING', 'IDLE')]
                if running:
                    self.logger.warning("Clearing %d stale aptly task(s) from previous session", len(running))
                    self.aptly.tasks.clear()
        except Exception as e:
            self.logger.debug("Could not check/clear aptly tasks: %s", e)

        # Clear orphan upload directories from a previous interrupted session
        try:
            stale_dirs = self.aptly.files.list()
            if stale_dirs:
                self.logger.warning("Clearing %d stale upload dir(s) from previous session", len(stale_dirs))
                for d in stale_dirs:
                    self.aptly.files.delete(d)
        except Exception as e:
            self.logger.debug("Could not clear stale upload dirs: %s", e)

    # Create a remote mirror(make sure the name has specified prefix)
    # Input
    #       name: the name of the remote repo : PREFIX_REMOTE-xxx
    #       url: the base url of the remote mirror: http://nginx.org/packages/debian
    #       distributions: the distribution: buster
    #       components: components=['nginx']
    #       architectures: architectures=['i386', 'arm64']
    #       with_sources: with_sources=True
    # Output: None or Class 'aptly_api.parts.mirrors.Mirror'
    def create_remote(self, name, url, distribution, **kwargs):
        '''Base on a Debian repository, construct a repo as its mirror'''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.error('%s is not started with %s, Failed.', name, PREFIX_REMOTE)
            raise ValueError('remote repository create failed: prefix error')

        remote_list = self.aptly.mirrors.list()
        for remote in remote_list:
            if remote.name == name:
                self.logger.warning('mirror %s already exists.', remote.name)
                return None
        extra_param = {}
        extra_param['distribution'] = distribution
        extra_param['ignore_signatures'] = True
        for key, value in kwargs.items():
            if key == 'components':
                extra_param['components'] = value
            if key == 'with_sources':
                extra_param['with_sources'] = True
            if key == 'architectures':
                extra_param['architectures'] = value
            # Not find good/small repository with udebs, not verified
            # if key == 'with_udebs':
            #    extra_param['with_udebs'] = True
        remote = self.aptly.mirrors.create(name, url, **extra_param)
        # Invalidate the remote list cache: a new mirror now exists.
        self._list_remotes_cache = None
        return remote

    # update a mirror called "name". Mirror exist.
    # Return False if failed
    def __update_mirror(self, name):
        '''Sync the mirror, may take minutes, depends on the size of the mirror ans the network. '''
        mirror_list = self.aptly.mirrors.list()
        # Add variable mirror_find just to avoid W0631
        mirror_find = False
        for mirror in mirror_list:
            if mirror.name == name:
                mirror_find = True
                break
        if not mirror_find:
            self.logger.warning('Publish failed for mirror %s not find', name)
            return False
        # Please do NOT add any parameters here beside "ignore_signatures=True", that may
        # overwrite previous settings and get strange results.
        task = self.aptly.mirrors.update(name=name, ignore_signatures=True)
        task_state = self.__wait_for_task(task, 15)
        # The mirror's content on the server just changed (or was confirmed
        # unchanged, but we can't tell which from here) -- drop any cached
        # package-list for it so the next pkg_exist()/pkg_list() call
        # refetches instead of serving a stale index.
        self._mirror_pkgs_cache.pop(name, None)
        if task_state == 'SUCCEEDED':
            return True
        else:
            self.logger.warning('Mirror %s update failed: %s', name, task_state)
            return False

    # Create a snapshot based on several others
    # name : string, the name of new build snapshot
    # source_snapshots: list of snapshots to be merge, order matters, snapshot at front of
    #                   list has higher priority than snapshot later in the list.
    # For each package, only the one with higher version can be selected:
    # Return False on failure
    def __merge_snapshot(self, name, source_snapshots):
        '''Merge several snapshots into one, prepare for later deploy.'''
        if not name.startswith(PREFIX_MERGE):
            self.logger.error('%s did not start with %s, Failed.' % (name, PREFIX_MERGE))
            return False
        package_refs = []
        # package_uniq_dict[pkgname_arch] = [package.key, snapshot]
        package_uniq_dict = dict()
        source_snapshots = [x.strip() for x in source_snapshots if x.strip() != '']
        # remove duplicates (keep order)
        source_snapshots = list(dict.fromkeys(source_snapshots))
        snap_list = self.aptly.snapshots.list()
        for snapshot in source_snapshots:
            snap_exist = False
            for snap in snap_list:
                if snap.name == snapshot:
                    snap_exist = True
                    package_list = self.aptly.snapshots.list_packages(snap.name, with_deps=False, detailed=False)
                    # Debug only
                    # package_list.sort()
                    # self.logger.debug('%s packages in repo %s' % (len(package_list), snapshot))
                    for package in package_list:
                        key_list = package.key.split()
                        # 0: pkg_arch  1: pkg_name  2: pkg_version 3: pkg_key of aptly
                        pkgname_arch = '_'.join([key_list[1], key_list[0]])
                        # Source packages are useless for LAT, ignore them.
                        if "Psource" == key_list[0]:
                            continue
                        # Check and drop duplicate packages
                        if pkgname_arch in package_uniq_dict.keys():
                            need_replace = False
                            orig_version = package_uniq_dict[pkgname_arch][0].split()[2]
                            if STX_DIST in orig_version and STX_DIST not in key_list[2]:
                                self.logger.warn('STX package %s %s has been eclipsed by upstream version %s' %
                                                 (pkgname_arch, orig_version, key_list[2]))
                            if debian_support.version_compare(key_list[2], orig_version) > 0:
                                self.logger.warn('Drop duplicate package: %s.' %
                                                 ' of '.join(package_uniq_dict[pkgname_arch]))
                                package_refs.remove(package_uniq_dict[pkgname_arch][0])
                                package_refs.append(package.key)
                                package_uniq_dict[pkgname_arch] = [package.key, snapshot]
                            else:
                                self.logger.warn('Drop duplicate package: %s of %s.' % (package.key, snapshot))
                            continue
                        package_uniq_dict[pkgname_arch] = [package.key, snapshot]
                        package_refs.append(package.key)
                    break
            if not snap_exist:
                self.logger.error('snapshot %s does not exist, merge failed.' % snapshot)
                return False

        # Remove a same name publish if exists
        # For exist snapshot called NAME, we will:
        # 1, rename it to backup-NAME
        # 2, Create a new snapshot: NAME
        # 3, delete snapshot backup-name
        backup_name = None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop publication failed %s : %s' % (name, task_state))
                    return False
        # Remove the backup snapshot if it exists
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == 'backup-' + name:
                backup_name = 'backup-' + name
                task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s' % (backup_name, task_state))
                    return False
        # Rename the snapshot if it exists
        for snap in snap_list:
            if snap.name == name:
                backup_name = 'backup-' + name
                self.__wait_for_task(self.aptly.snapshots.update(name, backup_name))

        # crate a snapshot with package_refs. Duplicate package_refs is harmless.
        # Note: The key is "package_refs" instead of "source_snapshots", for function
        #       "create_from_packages", parameter "source_snapshots" almost has no means.
        task = None
        task = self.aptly.snapshots.create_from_packages(name, source_snapshots=source_snapshots, package_refs=package_refs)
        task_state = self.__wait_for_task(task)
        if task_state != 'SUCCEEDED':
            if backup_name:
                self.__wait_for_task(self.aptly.snapshots.update(backup_name, name))
            self.logger.warning('merge_snapshot: Snapshot for %s creation failed: %s. ' % (name, task_state))
            return False
        # Remove the backup snapshot if it is created above
        if backup_name:
            task = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop snapshot failed %s : %s' % (backup_name, task_state))
        return True

    # Create a snapshot based on "name" with same name
    # Create (or replace) a snapshot whose name is EXACTLY snap_name, taken
    # from the local repo repo_name.
    #
    # Unlike __create_snapshot (which mints a unique, versioned snapshot name
    # for atomic publish switching), this deliberately produces a snapshot with
    # a stable, caller-chosen name. It exists so that a consumer which merges by
    # snapshot name -- e.g. build-image's merge(deb-merge-all, [deb-local-build,
    # deb-local-binary-*]) via __merge_snapshot -- can find an exact-named
    # snapshot for a repo that is otherwise published incrementally (repo-based,
    # no snapshot). Returns True on success, False on failure.
    def create_named_snapshot_from_repo(self, snap_name, repo_name):
        '''Create/replace an exact-named snapshot from a local repo.

        Cross-process-locked wrapper around _create_named_snapshot_from_repo_impl
        -- see _APTLY_SNAPSHOT_LOCK_DIR's module docstring and
        _cross_process_snapshot_lock's docstring for why: this is called from
        separate build-pkgs-2.0 and build-image processes/containers (ISO and
        remote-cli run in parallel Jenkins stages, both via build-image, both
        snapshotting the same repo names) with no synchronization between them
        otherwise. The lock wraps BOTH the "is a drop/recreate needed" check
        and the drop/rename/create itself -- holding it only around the
        mutating calls would still let two processes both observe "not
        currently stale" and both proceed, or let one process's check run
        while another's drop is in flight.
        '''
        with self._cross_process_snapshot_lock(snap_name):
            return self._create_named_snapshot_from_repo_impl(snap_name, repo_name)

    def _create_named_snapshot_from_repo_impl(self, snap_name, repo_name):
        '''Create/replace an exact-named snapshot from a local repo.

        If snap_name is currently published (e.g. by a prior build-image run
        that merged by this exact name), a plain delete fails with "unable to
        drop: snapshot is published" and the snapshot is left stale forever —
        this function would then silently keep returning False on every
        subsequent run while callers (build-image) ignore that return value,
        so the merge step downstream picks up the OLD snapshot content next
        to freshly-rebuilt sibling repos. Observed: deb-local-binary-compiler/
        -distro/-flock stuck on a pre-rebuild snapshot while deb-local-build
        and deb-local-binary refreshed, so deb-merge-all (the apt feed used
        to build images) simultaneously advertised gpgconf +b3 (from the
        stale snapshot) and +b4 (from the fresh one) -- apt then refused to
        install dirmngr/gpgsm, which pin an exact gpgconf version, failing
        the initramfs image build.

        Fixed the same way merge_snapshot() already handles this: drop the
        publish first (aptly then allows the delete), or if there is no
        publish but the delete still fails for some other transient reason,
        fall back to the rename-out-of-the-way dance (snapshots.update to a
        backup name) so a fresh snapshot can be created under snap_name
        regardless.

        IMPORTANT when snap_name == repo_name (e.g. build-pkgs-2.0 snapshotting
        REPO_BUILD under its own name for the downstream ISO/image merge): the
        publish being dropped above is the REPO's own live, incremental
        publish -- the one every concurrently-running sbuild chroot reads via
        visible_repos. Multiple layer/build_type threads call this at their
        own completion, so one thread finishing early drops a publish every
        OTHER still-running thread's chroots depend on, and nothing
        re-published it afterward: dropped at 19:05:13 on StarlingX
        build-packages #889, after which every chroot's "apt-get update"
        against deb-local-build 404'd for the rest of the run (not a transient
        race -- genuinely gone), exhausting the apt-retry cap for ~90 packages
        before this was traced back here. Always re-publish repo_name
        afterward (quick/incremental publish, cheap) so this function never
        leaves it unpublished for longer than the snapshot operation itself
        takes, regardless of whether repo_name is also being read live by
        other threads.

        Only ever call this via create_named_snapshot_from_repo (the lock
        wrapper above) -- calling this directly would reopen the exact
        cross-process race this split was introduced to close.
        '''
        try:
            snap_list = self.aptly.snapshots.list()
        except Exception as e:
            self.logger.warning('create_named_snapshot_from_repo: list failed: %s', e)
            snap_list = []
        snap_exists = any(snap.name == snap_name for snap in snap_list)
        repo_publish_dropped = False
        if snap_exists:
            # Drop any publish currently serving this exact snapshot name --
            # aptly refuses to delete a published snapshot otherwise.
            try:
                publish_list = self.aptly.publish.list()
            except Exception as e:
                self.logger.warning('create_named_snapshot_from_repo: publish '
                                    'list failed: %s', e)
                publish_list = []
            for publish in publish_list:
                if publish.prefix == snap_name:
                    task = self.aptly.publish.drop(prefix=snap_name,
                                                    distribution=publish.distribution,
                                                    force_delete=True)
                    st = self.__wait_for_task(task, 3, quiet_fail=True)
                    if st != 'SUCCEEDED':
                        self.logger.warning(
                            'create_named_snapshot_from_repo: could not drop '
                            'publish for %s: %s', snap_name, st)
                    else:
                        repo_publish_dropped = True

            task = self.aptly.snapshots.delete(snapshotname=snap_name, force=True)
            st = self.__wait_for_task(task, 3, quiet_fail=True)
            if st != 'SUCCEEDED':
                # Fallback: rename the stale snapshot out of the way (as
                # merge_snapshot does) instead of giving up -- a fresh
                # snapshot can still be created under snap_name even though
                # the old one couldn't be dropped directly.
                backup_name = 'backup-%s-%d' % (snap_name, int(time.time()))
                rst = self.__wait_for_task(
                    self.aptly.snapshots.update(snap_name, backup_name), 3,
                    quiet_fail=True)
                if rst != 'SUCCEEDED':
                    self.logger.warning(
                        'create_named_snapshot_from_repo: could not drop OR '
                        'rename existing snapshot %s: delete=%s rename=%s -- '
                        'giving up, snapshot %s remains stale', snap_name, st,
                        rst, snap_name)
                    if repo_publish_dropped:
                        self._republish_quick_best_effort(repo_name)
                    return False
                self.logger.info(
                    'create_named_snapshot_from_repo: renamed stale snapshot '
                    '%s to %s so a fresh one can be created', snap_name,
                    backup_name)
                dtask = self.aptly.snapshots.delete(snapshotname=backup_name, force=True)
                dst = self.__wait_for_task(dtask, 3, quiet_fail=True)
                if dst != 'SUCCEEDED':
                    self.logger.debug(
                        'create_named_snapshot_from_repo: could not delete '
                        'backup snapshot %s (harmless, will be orphaned): %s',
                        backup_name, dst)

        # aptly-api-client signature is create_from_repo(SOURCE_REPO, SNAPSHOT).
        try:
            task = self.aptly.snapshots.create_from_repo(repo_name, snap_name)
        except Exception as e:
            self.logger.warning(
                'create_named_snapshot_from_repo: create_from_repo(%s, %s) '
                'raised %s', repo_name, snap_name, e)
            if repo_publish_dropped:
                self._republish_quick_best_effort(repo_name)
            return False
        st = self.__wait_for_task(task, 5)
        if st != 'SUCCEEDED':
            self.logger.warning(
                'create_named_snapshot_from_repo: snapshot %s creation failed: %s',
                snap_name, st)
            if repo_publish_dropped:
                self._republish_quick_best_effort(repo_name)
            return False
        self.logger.info('Created snapshot %s from repo %s', snap_name, repo_name)
        if repo_publish_dropped:
            self._republish_quick_best_effort(repo_name)
        return True

    def _republish_quick_best_effort(self, repo_name):
        '''Re-publish repo_name (quick/incremental) after this module dropped
        its publish to free up a same-named snapshot for deletion. Best-effort:
        logs and swallows failure rather than raising, since this runs on
        every exit path (including existing-failure paths) purely to restore
        chroot-visibility -- it must never mask the caller's original error.

        Reuses __quick_publish_repo (the same incremental-publish path
        publish_repo()/deploy_local(..., suffix='') already uses elsewhere in
        this class) rather than calling the aptly API directly, so this gets
        the same first-time-create-vs-incremental-update handling for free.

        sign=True: this re-publish is restoring a repo that may have been
        signed moments ago (e.g. by build-packages's one-time sign_publish
        pass, or an earlier call to ensure_deployed/deploy_local with
        sign=True) -- an unsigned re-publish here would silently revert it,
        and every consumer reading it afterward (apt in a chroot, another
        build-image call) would hard-fail with "repository ... is not
        signed". Observed live: build-image's own snapshot_repo() call --
        made for every one of REPO_BUILD/deb-local-binary* right after their
        respective ensure_deployed() calls already confirmed them signed and
        complete -- immediately dropped and recreated each publish via this
        path with the old sign_skip=True, reverting ALL of them to unsigned
        and failing the chroot's own apt update ("not signed" for all 5
        repos) a few seconds later in the same build-image run. Unlike the
        hundreds of per-package incremental updates during active package
        building (which intentionally stay fast/unsigned via
        __quick_publish_repo's own default), every call site of this method
        is this one snapshot-swap path, never the hot per-package loop, so
        there is no fast-path case to preserve here -- always sign.
        '''
        try:
            result = self.__quick_publish_repo(repo_name, '', sign=True)
            if not result:
                self.logger.warning(
                    'create_named_snapshot_from_repo: re-publish of %s after '
                    'dropping its publish FAILED -- %s is now unpublished '
                    'until the next normal publish_repo() call; any chroot '
                    'reading it in the meantime will 404', repo_name, repo_name)
        except Exception as e:
            self.logger.warning(
                'create_named_snapshot_from_repo: re-publish of %s after '
                'dropping its publish raised %s -- %s is now unpublished '
                'until the next normal publish_repo() call', repo_name, e,
                repo_name)

    # Create a snapshot based on "name" with same name
    # local: True ==> local_repo False ==> remote_mirror
    # Return False if failed
    def __create_snapshot(self, name, local):
        '''Create a fresh snapshot for a local/remote repo, prepared for an
        atomic publish switch.

        Returns the NEW snapshot's name on success (a unique, versioned name),
        or None on failure. The caller passes this name to __publish_snap, which
        switches the live publish to it in place — so the currently-published
        snapshot is left untouched and keeps serving apt clients until the
        switch happens (no 404 window). The previous snapshot is cleaned up
        AFTER the switch by __cleanup_old_snapshots().

        Previously this dropped the publish up front so it could rename the
        old snapshot to backup-NAME; that drop is exactly what caused the
        multi-minute 404 window (StarlingX build 1155 wheels failure). We no
        longer touch the publish here.
        '''
        # Unique, time-ordered snapshot name so the new snapshot can coexist
        # with the currently-published one during the atomic switch.
        new_snap = '%s-%d-%s' % (name, int(time.time()), uuid.uuid4().hex[:8])

        # Defensive: if a snapshot with the target name somehow exists, drop it.
        snap_list = self.aptly.snapshots.list()
        if any(snap.name == new_snap for snap in snap_list):
            self.__wait_for_task(self.aptly.snapshots.delete(new_snap, force=True), 3)

        # Create the new snapshot (publish still live on the old snapshot).
        # NOTE: the aptly-api-client signature is (reponame/mirrorname first,
        # snapshotname second) — i.e. create_from_repo(SOURCE, SNAPSHOT). The
        # original code passed (name, name) so the order was masked; with a
        # distinct snapshot name the source MUST come first or aptly 404s
        # ("local repo with name <snapshot> not found").
        if local:
            task = self.aptly.snapshots.create_from_repo(name, new_snap)
        else:
            task = self.aptly.snapshots.create_from_mirror(name, new_snap)
        task_state = self.__wait_for_task(task, 5)
        if task_state != 'SUCCEEDED':
            self.logger.warning('create_snapshot: Snapshot %s creation failed: %s.'
                                % (new_snap, task_state))
            # Best-effort cleanup of a partial snapshot.
            try:
                self.__wait_for_task(self.aptly.snapshots.delete(new_snap, force=True), 3)
            except Exception:
                pass
            return None
        return new_snap

    def __cleanup_old_snapshots(self, name, keep_snap):
        '''Delete stale snapshots for a repo, keeping only the one now published.

        Removes the legacy '<name>' and 'backup-<name>' snapshots plus any
        older versioned '<name>-<ts>-<hex>' snapshots, except keep_snap (the
        one the publish now points at). Safe to call after the publish switch:
        aptly refuses to delete a snapshot still referenced by a publish, so
        even a mismatch cannot break the live repo — such deletes just warn.
        '''
        try:
            snap_list = self.aptly.snapshots.list()
        except Exception as e:
            self.logger.debug('cleanup snapshots: list failed for %s: %s', name, e)
            return
        prefix = name + '-'
        for snap in snap_list:
            sn = snap.name
            if sn == keep_snap:
                continue
            if sn == name or sn == 'backup-' + name or sn.startswith(prefix):
                try:
                    task = self.aptly.snapshots.delete(snapshotname=sn, force=True)
                    st = self.__wait_for_task(task, 3, quiet_fail=True)
                    if st != 'SUCCEEDED':
                        self.logger.debug('cleanup: could not delete snapshot %s: %s (in use?)', sn, st)
                except Exception as e:
                    self.logger.debug('cleanup: delete snapshot %s raised %s (ignored)', sn, e)

    # Wait for an aptly task up to a maximum of "count" minutes.
    # By default, wait for DEFAULT_TIMEOUT_COUNT minute(s).
    # Return: SUCCEEDED, FAILED, TIMEOUTED, EINVAL
    def __wait_for_task(self, task, count=DEFAULT_TIMEOUT_COUNT, quiet_fail=False):
        '''Wait for an aptly task up to 'count' minutes.

        Polls with exponential backoff: starts at 0.5s, doubles up to
        a 10s cap.  This detects fast completions immediately instead of
        blocking 60s per iteration.  On timeout or failure, logs
        diagnostic info including task state and output.

        If quiet_fail is True, failures are logged at DEBUG level instead
        of ERROR (useful when failure is expected and handled by caller).
        '''
        # Some functions return object that are not of type 'Task' when the job completed successfully
        if not isinstance(task, Task):
            return 'SUCCEEDED'
        # Perhaps the job completed immediately, without spawning a task
        if task.state == 'SUCCEEDED' or task.state == 'FAILED':
            return task.state
        if count not in range(1, 30):
            self.logger.error('Requested wait of %d minutes exceeds 30 minute maximum.', count)
            return 'EINVAL'
        timeout_factor = os.environ.get('REPOMGR_REQ_TIMEOUT_FACTOR')
        if timeout_factor and timeout_factor.isdigit() and int(timeout_factor) != 0:
            count *= int(timeout_factor)
        start_time = time.time()
        deadline = start_time + count * 60
        poll_interval = 0.5
        max_poll_interval = 10.0
        logged_minute = 0
        self.logger.debug('Aptly task %d(%s) queued, waiting up to %d min',
                          task.id, task.name, count)
        while True:
            time.sleep(poll_interval)
            try:
                current = self.aptly.tasks.show(task.id)
            except Exception as e:
                # Network error — keep trying until deadline
                if time.time() >= deadline:
                    self.logger.error('Aptly task %d: lost contact: %s', task.id, e)
                    break
                poll_interval = min(poll_interval * 2, max_poll_interval)
                continue

            if current.state == 'SUCCEEDED':
                elapsed = time.time() - start_time
                self.logger.info('Aptly task %d(%s) completed in %.1fs',
                                 task.id, task.name, elapsed)
                return 'SUCCEEDED'
            if current.state == 'FAILED':
                elapsed = time.time() - start_time
                log_fn = self.logger.debug if quiet_fail else self.logger.error
                try:
                    output = self.aptly.tasks.output_show(task.id)
                    log_fn('Aptly task %d(%s) FAILED after %.1fs: %s',
                           task.id, task.name, elapsed,
                           output[-500:] if output else '(no output)')
                except Exception:
                    log_fn('Aptly task %d(%s) FAILED after %.1fs (no output available)',
                           task.id, task.name, elapsed)
                return 'FAILED'

            # Still running — check deadline
            elapsed = time.time() - start_time
            elapsed_min = int(elapsed / 60)
            if elapsed_min > logged_minute:
                logged_minute = elapsed_min
                remaining = int((deadline - time.time()) / 60)
                self.logger.info('Aptly task %d(%s) still running after %d min '
                                 '(timeout in %d min)',
                                 task.id, task.name, elapsed_min, remaining)

            if time.time() >= deadline:
                break

            # Exponential backoff capped at max_poll_interval
            poll_interval = min(poll_interval * 2, max_poll_interval)

        # Timed out — gather diagnostics
        elapsed = time.time() - start_time
        self.logger.error('Aptly task %d(%s) timed out after %.1fs.',
                          task.id, task.name, elapsed)
        try:
            current = self.aptly.tasks.show(task.id)
            output = self.aptly.tasks.output_show(task.id)
            self.logger.error('  Task state: %s', current.state)
            self.logger.error('  Task output (last 500 chars): %s',
                              output[-500:] if output else '(empty)')
            all_tasks = self.aptly.tasks.list()
            # Aptly processes tasks strictly sequentially — a single stuck
            # task (RUNNING *or* IDLE/queued-but-never-started) blocks every
            # subsequent API call indefinitely. IDLE was previously omitted
            # here, so this diagnostic never fired for the exact state we
            # actually observed in practice (a whole run's worth of delete/
            # add/snapshot tasks all timing out at state IDLE back-to-back).
            blocking = [t for t in all_tasks if t.state in ('RUNNING', 'IDLE')]
            if len(blocking) > 1:
                self.logger.error('  %d tasks currently RUNNING/IDLE (possible queue contention):',
                                  len(blocking))
                for t in blocking[:5]:
                    self.logger.error('    task %d: %s (%s)', t.id, t.name, t.state)
            if current.state == 'IDLE':
                # A task still IDLE after our full wait means aptly's
                # sequential task queue is stuck behind it (or it never got
                # dispatched at all) — it will not recover on its own, and
                # every task after it in this run would time out identically.
                # Clear the queue so subsequent operations get a fresh start,
                # same recovery already done once at connection time in
                # _db_health_check(); here it also runs mid-run, right when
                # the stall is actually detected.
                self.logger.warning(
                    '  Task %d still IDLE — aptly task queue appears stuck. '
                    'Clearing all aptly tasks so subsequent operations are '
                    'not blocked behind it.', task.id)
                try:
                    self.aptly.tasks.clear()
                except Exception as clear_e:
                    self.logger.error('  Failed to clear stuck aptly tasks: %s', clear_e)
        except Exception as diag_e:
            self.logger.error('  (failed to gather diagnostics: %s)', diag_e)
        self.logger.error('Set REPOMGR_REQ_TIMEOUT_FACTOR=N to multiply timeout by N.')
        return 'TIMEOUTED'

    # Publish a local repository directly, without snapshot or signature
    # If an old publish exists, drop it firstly and then create a new one.
    # Do not use publish.update just for safety.
    # (repo)repo_name ==> (publish)repo_name-suffix
    def __quick_publish_repo(self, repo_name, suffix, sign=False):
        '''Publish a local repository directly, without snapshot.

        On first call, creates a new publish.  On subsequent calls,
        uses publish.update (PUT) which is incremental — no drop/recreate
        cycle.  Also skips Contents index generation which is expensive
        and unnecessary for build-dep resolution.

        If suffix is None or empty, the publish prefix is the repo name itself.

        sign=False (default) matches every pre-existing caller of this
        method: the hundreds of per-package incremental updates during an
        active build stay fast/unsigned (sign_skip=True), relying on the
        later, separate, one-time sign_publish() pass to produce the real
        signature once, after all content is final.

        sign=True is for ensure_deployed()'s call path ONLY: ensure_deployed
        is the shared "is this repo healthy and ready for a consumer to
        read" gate used by build-iso/build-wheels/build-docker-images, which
        can run concurrently with (and AFTER) the one-time sign_publish pass.
        If ensure_deployed's own deploy_local() call ever has to actually
        create/recreate the publish (not just take the no-op fast path),
        doing so with sign_skip=True would silently revert an already-signed
        repo back to unsigned -- apt then hard-fails with "repository ...
        is not signed" for every consumer that reads it afterward.
        Observed live: StarlingX monolithic #1221's build-iso #795 dropped
        and recreated deb-local-build's publish via ensure_deployed/
        merge_local_repos ~2 minutes after publish-packages had already
        signed it, reverting it to unsigned and failing the chroot's own
        apt update with exactly that error. Signing here costs one extra
        GPG pass only on the (rare) path where ensure_deployed must
        actually (re)create the publish, not on the common no-op path.
        '''
        if suffix:
            publish_name = '-'.join([repo_name, suffix])
        else:
            publish_name = repo_name
        t0 = time.time()
        sign_kwargs = ({'sign_gpgkey': SIGN_KEY, 'sign_passphrase': SIGN_PASSWD}
                       if sign else {'sign_skip': True})

        # Check if this publish already exists and is functional
        publish_list = self.aptly.publish.list()
        existing = None
        for publish in publish_list:
            if publish.prefix == publish_name:
                existing = publish
                break

        if existing:
            # Verify the publish is actually valid by checking the source repo exists
            repo_list = self.aptly.repos.list()
            repo_exists = any(r.name == repo_name for r in repo_list)
            if not repo_exists:
                # Stale publish entry — drop it silently
                self.logger.debug('Dropping stale publish %s (source repo gone)', publish_name)
                try:
                    drop_task = self.aptly.publish.drop(
                        prefix=publish_name,
                        distribution=existing.distribution,
                        force_delete=True)
                    self.__wait_for_task(drop_task)
                except Exception:
                    pass
                existing = None

        if existing:
            # Incremental update — regenerate Packages/Release from current
            # repo contents without drop/recreate overhead.
            self.logger.info('publish.update %s (incremental%s)',
                             publish_name, ', signed' if sign else '')
            try:
                task = self.aptly.publish.update(
                    prefix=publish_name,
                    distribution=existing.distribution,
                    skip_contents=True,
                    skip_cleanup=True,
                    force_overwrite=True,
                    **sign_kwargs,
                )
                task_state = self.__wait_for_task(task, 10, quiet_fail=True)
            except Exception as e:
                self.logger.info('publish.update for %s raised %s — falling back to create',
                                    publish_name, e)
                task_state = 'FAILED'
            if task_state != 'SUCCEEDED':
                if task_state != 'FAILED':
                    self.logger.info('publish.update for %s failed — falling back to create',
                                        publish_name)
                # Update failed (stale publish entry?) — drop and recreate
                try:
                    drop_task = self.aptly.publish.drop(
                        prefix=publish_name,
                        distribution=existing.distribution,
                        force_delete=True)
                    self.__wait_for_task(drop_task)
                except Exception:
                    pass
                existing = None  # Fall through to create path

        if not existing:
            # First time — create the publish
            self.logger.info('publish.create %s (first time%s)',
                             publish_name, ', signed' if sign else '')
            task = self.aptly.publish.publish(
                source_kind='local',
                sources=[{'Name': repo_name}],
                architectures=['amd64', 'source'],
                prefix=publish_name,
                distribution=None,
                origin=self.origin,
                **sign_kwargs,
            )
            task_state = self.__wait_for_task(task, 10)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Quick publish create for %s failed: %s (%.1fs)',
                                    publish_name, task_state, time.time() - t0)
                return None

        self.logger.info('Quick publish %s completed in %.1fs', publish_name, time.time() - t0)
        return publish_name + ' ' + DEBIAN_DISTRIBUTION

    # Publish a snap called "snap_name" at prefix "name", DEBIAN_DISTRIBUTION as the distribution
    # Return None or prefix/distribution
    def __publish_snap(self, name, snap_name=None, distribution=DEBIAN_DISTRIBUTION):
        '''Deploy a snapshot at publish prefix `name`.

        `snap_name` is the underlying snapshot to publish/switch to. It may
        differ from `name` (the publish prefix) so a freshly-created snapshot
        can be switched in atomically while the old one still serves clients.
        If omitted, it defaults to `name` (legacy same-name behaviour).

        If a publish already exists at this prefix, the published distribution
        is switched ATOMICALLY to the new snapshot (aptly `publish switch`)
        instead of being dropped and re-created. The old drop+publish sequence
        left the repo's dists/.../Packages returning 404 for the entire
        (multi-minute) re-publish window; any consumer doing `apt-get update`
        during that window failed (StarlingX build 1155: the wheelbuilder image
        build hit a 404 on deb-local-build while it was mid-republish and failed
        the whole run). `publish switch` swaps the snapshot pointer in place, so
        the repo is always served by either the old or the new snapshot and
        never disappears.
        '''
        if snap_name is None:
            snap_name = name
        # Detect an existing publish at this prefix (switch target).
        existing_publish = None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                existing_publish = publish
                break

        # is_remote: True => remote repo; False => local repo
        is_remote = False
        mirror = None
        mirror_list = self.aptly.mirrors.list()
        for mirror in mirror_list:
            if mirror.name == name:
                is_remote = True
                break

        # crate a publish
        extra_param = {}
        if is_remote:
            # it is a remote repo: info storied in "mirror"
            # Add 'source' to publish source packages, if no source packages, that is also harmless.
            extra_param['architectures'] = mirror.architectures.append('source')
            extra_param['distribution'] = mirror.distribution
            extra_param['origin'] = None
        else:
            # Only support binary_amd64 and source packages
            extra_param['architectures'] = ['amd64', 'source']
            extra_param['distribution'] = distribution
            extra_param['origin'] = self.origin

        extra_param['source_kind'] = 'snapshot'
        extra_param['sources'] = [{'Name': snap_name}]
        extra_param['sign_skip'] = True
        extra_param['prefix'] = name

        if existing_publish is not None:
            # Atomic in-place re-point to the new snapshot — no 404 window.
            #
            # NOTE: the atomic-switch primitive in THIS aptly-api-client is
            # publish.update(..., snapshots=[{'name': snap}]) — a PUT to
            # api/publish/{prefix}/{dist} that re-points the publication to new
            # snapshot sources in place. There is NO publish.switch method on
            # PublishAPISection (list/publish/update/drop only); calling
            # .switch() raised AttributeError on every invocation and silently
            # fell through to the destructive drop+create path this design was
            # meant to avoid (StarlingX build 1160 build-iso #775: every
            # 'publish.switch ... (atomic, no drop)' was immediately followed by
            # "'PublishAPISection' object has no attribute 'switch' — falling
            # back to drop+create").
            #
            # COMPONENT must be specified explicitly for a MULTI-component
            # publish. The real Aptly server API (PUT /api/publish/:prefix/
            # :distribution) only infers/defaults a Snapshots entry's
            # Component for a SINGLE-component repo; for a multi-component
            # one, an entry with no Component silently targets a DIFFERENT
            # (default/unnamed) component than any named one the publish
            # already has (e.g. 'main') -- that named component is then never
            # updated again by any future switch call, permanently serving
            # stale content, while apt/debootstrap (which reads the Release
            # file's Components: field) keeps reading the orphaned, frozen
            # component. Observed live on deb-local-binary: aptly publish list
            # showed TWO sources under one publish -- {: [snap-OLD]} (the
            # unnamed component, correctly advancing every run) and
            # {main: [snap-FROZEN]} (never advancing past whatever snapshot
            # was active the one time it was created) -- causing
            # sbuild-createchroot's debootstrap to fail fetching
            # dists/trixie/main/binary-amd64/Packages ("was corrupt" / "File
            # has unexpected size") against the always-stale main component,
            # even though downloader-2.0's own deploy_local() reported
            # success every single run.
            #
            # Build one Snapshots entry per EXISTING component on this
            # publish (from the live publish.list() response, not guessed),
            # all repointed to the same new snapshot -- so every component a
            # consumer might read (named or unnamed) advances together.
            existing_components = sorted({
                src.get('Component', '') for src in (existing_publish.sources or [])
            }) or ['']
            switch_sources = [
                {'Component': comp, 'Name': snap_name} if comp else {'Name': snap_name}
                for comp in existing_components
            ]
            self.logger.info('publish.update %s -> snapshot %s (atomic re-point, no drop, components=%s)',
                             name, snap_name, existing_components)
            try:
                task = self.aptly.publish.update(
                    prefix=name,
                    distribution=existing_publish.distribution,
                    snapshots=switch_sources,
                    sign_skip=True,
                    skip_contents=True,
                    skip_cleanup=True,
                    force_overwrite=True,
                )
                task_state = self.__wait_for_task(task, 10, quiet_fail=True)
            except Exception as e:
                self.logger.info('publish.update (switch) for %s raised %s — falling back to drop+create',
                                 name, e)
                task_state = 'FAILED'
            if task_state == 'SUCCEEDED':
                for publish in self.aptly.publish.list():
                    if publish.prefix == name:
                        return publish.prefix + ' ' + publish.distribution
                return name + ' ' + extra_param['distribution']
            # Switch failed (stale/mismatched publish?) — drop then create below.
            self.logger.warning('publish.update (switch) for %s failed (%s) — dropping and recreating',
                                name, task_state)
            try:
                drop_task = self.aptly.publish.drop(
                    prefix=name, distribution=existing_publish.distribution, force_delete=True)
                self.__wait_for_task(drop_task)
            except Exception:
                pass

        # No existing publish (first deploy) or switch fell through: create it.
        task = self.aptly.publish.publish(source_kind='snapshot', sources=extra_param['sources'],
                                          architectures=extra_param['architectures'], prefix=extra_param['prefix'],
                                          distribution=extra_param['distribution'],
                                          sign_gpgkey=SIGN_KEY, sign_passphrase=SIGN_PASSWD,
                                          origin=extra_param['origin'])
        task_state = self.__wait_for_task(task, 10, quiet_fail=True)
        if task_state != 'SUCCEEDED':
            # IDEMPOTENCY / CONCURRENCY RECOVERY.
            # A create can fail with "prefix/distribution already used by
            # another published repo" when a CONCURRENT deployer (another X1
            # branch — e.g. build-iso vs build-wheels both calling
            # ensure_deployed) published this same prefix in the window between
            # our publish.list() scan (which saw no publish, because the peer's
            # publish.switch was mid-flight and the prefix was transiently
            # absent) and this create. In that case a publish now DOES exist;
            # the correct action is to switch it to our snapshot, not to fail.
            # This turned a benign race into a hard 'ensure_deployed failed ->
            # aborting' (StarlingX build 1159, build-wheels #760, task 1645).
            publish_now = None
            try:
                for publish in self.aptly.publish.list():
                    if publish.prefix == name:
                        publish_now = publish
                        break
            except Exception as e:
                self.logger.warning('Publication %s failed (%s) and re-list errored: %s',
                                    name, task_state, e)
                return None
            if publish_now is not None:
                self.logger.info('Publication %s: prefix already published by a peer — '
                                 'switching it to our snapshot %s (idempotent recovery)',
                                 name, snap_name)
                try:
                    sw_task = self.aptly.publish.update(
                        prefix=name,
                        distribution=publish_now.distribution,
                        snapshots=[{'name': snap_name}],
                        sign_skip=True,
                        skip_contents=True,
                        skip_cleanup=True,
                        force_overwrite=True,
                    )
                    sw_state = self.__wait_for_task(sw_task, 10, quiet_fail=True)
                except Exception as e:
                    self.logger.info('recovery publish.update (switch) for %s raised %s', name, e)
                    sw_state = 'FAILED'
                # Whether or not our switch won, a healthy publish now exists at
                # this prefix (the peer's or ours). Treat the prefix as deployed.
                for publish in self.aptly.publish.list():
                    if publish.prefix == name:
                        if sw_state != 'SUCCEEDED':
                            self.logger.info('publish %s already present via peer; '
                                             'accepting existing publication', name)
                        return publish.prefix + ' ' + publish.distribution
                return name + ' ' + extra_param['distribution']
            self.logger.warning('Publication %s failed: %s' % (name, task_state))
            return None
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                repo_str = publish.prefix + ' ' + publish.distribution
                return repo_str
        return None

    # sync a remote mirror and deploy it
    # Input: the name of the remote
    # Output: bool
    def deploy_remote(self, name):
        '''Deploy a mirror, it will sync/update, snapshot and publish at last.
        It may take minutes, depends on the size of the mirror and the bandwidth,
        '''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.warning('%s has no %s prefix, not a remote repository.', name, PREFIX_REMOTE)
            return None

        remote_list = self.aptly.mirrors.list()
        remote = None
        for remote in remote_list:
            if remote.name == name:
                break

        if not remote:
            self.logger.warning('mirror %s not find, please create it firstly.', name)
            return None

        if self.__update_mirror(name):
            new_snap = self.__create_snapshot(name, False)
            if new_snap:
                ret = self.__publish_snap(name, snap_name=new_snap)
                if ret:
                    self.__cleanup_old_snapshots(name, new_snap)
                return ret
        return None

    # info all remote repositories through logger
    def list_remotes(self, quiet=False):
        '''List all remote repositories/mirrors.'''
        r_list = []
        if self._list_remotes_cache is not None:
            remote_list = self._list_remotes_cache
        else:
            try:
                remote_list = self.aptly.mirrors.list()
            except Exception as e:
                if not str(e).startswith('404 Not Found'):
                    self.logger.error('Error: %s' % e)
                remote_list = []
            self._list_remotes_cache = remote_list
        if not len(remote_list):
            if not quiet:
                self.logger.info('No remote repo')
            return r_list
        if not quiet:
            self.logger.info('%d remotes:', len(remote_list))
        for remote in remote_list:
            r_list.append(remote.name)
            if not quiet:
                if 'archive_root' in remote:
                    # Old api
                    self.logger.info('%s : %s : %s', remote.name,
                                     remote.archive_root, remote.distribution)
                else:
                    self.logger.info('%s : %s : %s : %s : %s', remote.name,
                                     remote.archiveurl, remote.distribution,
                                     remote.components, remote.architectures)
        return r_list

    # find and remove a remote
    # Input: the name of the remote
    # Output: Bool
    def remove_remote(self, name):
        '''Delete a remote repository/mirror and all related publish and snapshot.'''
        if not name.startswith(PREFIX_REMOTE):
            self.logger.warning('%s is not a correct remote name', name)
            return False

        # find and remove related publish
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            if publish.prefix == name:
                task = self.aptly.publish.drop(prefix=name, distribution=publish.distribution, force_delete=True)
                self.__wait_for_task(task)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, task_state)

        # find and remove the remote(mirror)
        remote_list = self.aptly.mirrors.list()
        for remote in remote_list:
            if remote.name == name:
                task = self.aptly.mirrors.drop(name=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop mirror failed %s : %s', name, task_state)

        # Delete orphan files, wait up to 5 minutes for the cleanup to complete
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)

        # Invalidate caches: the set of remotes just changed, and this
        # mirror's package-list cache entry (if any) is now stale/gone.
        self._list_remotes_cache = None
        self._mirror_pkgs_cache.pop(name, None)

        return True

    # info all local repositories through logger
    def list_local(self, quiet=False):
        '''List all local repository.'''
        local_list = []
        if self._list_local_cache is not None:
            repo_list = self._list_local_cache
        else:
            repo_list = self.aptly.repos.list()
            self._list_local_cache = repo_list
        if not len(repo_list):
            self.logger.info('No local repo')
            return local_list
        if not quiet:
            self.logger.info('%d local repos:', len(repo_list))
        for repo in repo_list:
            # rpo.name, repo.url, repo.distributions, repo.components
            local_list.append(repo.name)
            if not quiet:
                self.logger.info('%s : %s : %s', repo.name, repo.default_distribution, repo.default_component)
        return local_list

    # Create a local repository
    # Input:the name of the repo
    # Output: None or repo
    def create_local(self, local_name):
        '''Create an empty local repository.'''
        if not local_name.startswith(PREFIX_LOCAL):
            self.logger.error('%s  is not started with %s, Failed.', local_name, PREFIX_LOCAL)
            raise ValueError('local repository create failed: prefix error.')

        repo_list = self.aptly.repos.list()
        for repo in repo_list:
            if local_name == repo.name:
                self.logger.warning('%s exists, please choose another name', local_name)
                return None

        # Static settings: DEBIAN_DISTRIBUTION main
        repo = self.aptly.repos.create(local_name, default_distribution=DEBIAN_DISTRIBUTION, default_component='main')
        # Invalidate the local-repo list cache: the set of local repos just
        # changed, so a stale cached list would hide this new repo from
        # list_local()/search_pkg() callers for the rest of this process.
        self._list_local_cache = None
        return repo

    # Upload a bundle of Debian package files into a local repository.
    # For source package, all its package files need to be uploaded in one
    # function call, or, uploaded files will not be inserted into repository
    @staticmethod
    def _parse_deb_filename(path):
        '''Parse (name, version, arch) from a Debian .deb filename.

        Debian binary package files are named ``name_version_arch.deb``.
        The version may itself contain underscores? No -- Debian versions
        never contain '_', so splitting on the last two underscores before
        the '.deb' suffix is unambiguous. Returns None for anything that
        does not look like a .deb (e.g. source .dsc/.tar), so such files are
        never filtered and always fall through to upload.
        '''
        base = os.path.basename(path)
        if not base.endswith('.deb'):
            return None
        stem = base[:-len('.deb')]
        parts = stem.split('_')
        if len(parts) < 3:
            return None
        arch = parts[-1]
        version = parts[-2]
        name = '_'.join(parts[:-2])
        if not name or not version or not arch:
            return None
        # Normalize away any epoch so the key matches aptly's epoch-stripped
        # tier-1/tier-2 index. Debian filenames normally omit the epoch, but
        # some tooling keeps it (as '1:' or the url-escaped '1%3a'); the
        # caller already de-escapes '%3a' -> ':' before this runs.
        if ':' in version:
            version = version.split(':', 1)[1]
        return (name, version, arch)

    def _filter_already_present(self, files_to_upload, repo_name):
        '''Drop package files already present in repo_name with identical content.

        Two-tier check, centralized so every upload path benefits:
          1. name+version+arch match against the repo's current package list
             (cheap: uses the cached _get_local_pkgs list, aptly key is
             'Parch name version'). No match -> new/changed -> keep (upload).
          2. For tier-1 matches only, verify by content hash: compare the
             local .deb's SHA256 against the repo package's SHA256 (from a
             detailed search). Equal -> truly identical -> skip. Differ ->
             same version but different content -> keep (upload) so the repo
             is corrected.

        Files that cannot be classified (non-.deb, parse failure, missing
        hash, hashing error) are conservatively KEPT -- this filter must
        never cause a needed package to be skipped.

        Returns the filtered list to actually upload.
        '''
        try:
            present = self._get_local_pkgs(repo_name)
        except Exception as e:
            self.logger.debug('_filter_already_present: could not list %s: %s '
                              '(uploading all)', repo_name, e)
            return files_to_upload

        # tier-1 index: {(name, version_no_epoch, arch): Package} for binary
        # packages. Debian .deb filenames never carry the version epoch
        # (e.g. '1:4.10.0-2' -> file '..._4.10.0-2_...deb'), whereas aptly
        # keys DO ('Pall name 1:4.10.0-2'). Strip the epoch from the aptly
        # version so the filename-derived key can match; tier-2 SHA256 still
        # guarantees content identity before anything is skipped.
        def _strip_epoch(ver):
            return ver.split(':', 1)[1] if ':' in ver else ver
        by_nva = {}
        for pkg in present:
            key = pkg.key if isinstance(pkg, Package) else pkg
            kp = key.split()
            if len(kp) < 3:
                continue
            arch = kp[0][1:]          # 'Pamd64' -> 'amd64'; 'Psource' -> 'source'
            if arch == 'source':
                continue
            by_nva[(kp[1], _strip_epoch(kp[2]), arch)] = pkg

        if not by_nva:
            return files_to_upload   # empty repo -> upload everything

        tier1_matches = []
        keep = []
        for f in files_to_upload:
            nva = self._parse_deb_filename(f)
            if nva is None or nva not in by_nva:
                keep.append(f)                 # new/changed or non-.deb
            else:
                tier1_matches.append((f, nva))

        if not tier1_matches:
            return keep

        # tier-2: resolve SHA256 for the matched repo packages via a single
        # detailed search (cached separately so we don't disturb the plain
        # list cache that pkg_exist()/pkg_list() rely on).
        detailed_by_nva = {}
        try:
            if not hasattr(self, '_local_pkgs_detailed_cache'):
                self._local_pkgs_detailed_cache = {}
            if repo_name not in self._local_pkgs_detailed_cache:
                self._local_pkgs_detailed_cache[repo_name] = \
                    self.aptly.repos.search_packages(repo_name, query='Name',
                                                     detailed=True)
            for pkg in self._local_pkgs_detailed_cache[repo_name]:
                key = pkg.key if isinstance(pkg, Package) else pkg
                kp = key.split()
                if len(kp) < 3 or kp[0] == 'Psource':
                    continue
                fields = getattr(pkg, 'fields', None) or {}
                sha = fields.get('SHA256') or fields.get('sha256')
                if sha:
                    # epoch-stripped version to match the filename-derived nva
                    ver = kp[2].split(':', 1)[1] if ':' in kp[2] else kp[2]
                    detailed_by_nva[(kp[1], ver, kp[0][1:])] = sha.lower()
        except Exception as e:
            # Detailed query failed -> cannot verify by hash; be safe and
            # upload all tier-1 matches too (correctness over speed).
            self.logger.debug('_filter_already_present: detailed search %s '
                              'failed: %s (uploading matched files)', repo_name, e)
            return keep + [f for f, _ in tier1_matches]

        skipped = 0
        for f, nva in tier1_matches:
            repo_sha = detailed_by_nva.get(nva)
            local_sha = self._sha256_of(f)
            if repo_sha and local_sha and repo_sha == local_sha:
                skipped += 1               # identical -> skip
            else:
                keep.append(f)             # differ/unknown -> upload
        if skipped:
            self.logger.info('Populating repo %s: skipping %d of %d file(s) '
                             'already present with identical content; '
                             'uploading %d.', repo_name, skipped,
                             len(files_to_upload), len(keep))
        return keep

    @staticmethod
    def _sha256_of(path):
        '''Return the lowercase hex SHA256 of a file, or None on error.'''
        try:
            import hashlib
            h = hashlib.sha256()
            with open(path, 'rb') as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b''):
                    h.update(block)
            return h.hexdigest().lower()
        except Exception:
            return None

    # but just deleted.
    # Input:
    #       pkg_files: the path-name of the package files. If the file name
    #                  contains "%3a", it will be replaced by ":".
    #       repo_name: the name of the local repository
    # Output: Bool
    def upload_pkg_local(self, pkg_files, repo_name):
        '''Upload a bundle of package files into a local repository.'''
        # sanity check: every package file is readable, local repository exists
        if not pkg_files:
            self.logger.warning('pkg_files should not be empty!')
            return False
        for pkg_file in set(pkg_files):
            if not os.access(pkg_file, os.R_OK):
                self.logger.warning('%s is NOT accessible to read.', pkg_file)
                return False
        if not repo_name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is NOT a well formed name.', repo_name)
            return False

        repo_list = self.aptly.repos.list()
        repo_found = False
        for repo in repo_list:
            if repo_name == repo.name:
                self.logger.debug('repo %s was found and can be used', repo_name)
                repo_found = True
                break

        if not repo_found:
            self.logger.warning('repo %s does not exist, please create it first.', repo_name)
            return False

        # Use a unique upload directory to avoid races when multiple
        # threads upload to the same repo concurrently.  Each caller
        # gets its own server-side folder that won't collide with others.
        upload_dir = '%s_%s' % (repo_name, uuid.uuid4().hex[:12])
        t0 = time.time()
        # Normalize filenames first: aptly chokes on '%3a' (apt sometimes
        # escapes ':' this way), so rename those on disk before uploading.
        files_to_upload = []
        for pkg_file in set(pkg_files):
            if pkg_file.find('%3a') >= 0:
                rename_file = pkg_file.replace('%3a', ':')
                try:
                    os.rename(pkg_file, rename_file)
                except Exception as e:
                    self.logger.error('Error: %s' % e)
                    self.logger.error('Package file %s rename error.' % pkg_file)
                    raise Exception('Package file %s rename error, upload failed.' % pkg_file)
                files_to_upload.append(rename_file)
            else:
                files_to_upload.append(pkg_file)

        # Centralized pre-filter: skip files already present in the repo with
        # identical content (name+version+arch match, then SHA256 verify).
        # Avoids re-uploading and re-processing packages aptly already holds,
        # which dominates wall-clock on incremental rebuilds. Opt-out via
        # REPOMGR_SKIP_PRESENT_FILTER=0 for debugging.
        _requested = len(files_to_upload)
        if os.environ.get('REPOMGR_SKIP_PRESENT_FILTER', '1') != '0':
            files_to_upload = self._filter_already_present(files_to_upload, repo_name)
        if not files_to_upload:
            self.logger.info('Populating repo %s: all %d package file(s) already '
                             'present with identical content; nothing to upload.',
                             repo_name, _requested)
            return True

        # aptly's files.upload() accepts multiple files in a single POST
        # (destination, *files).  Uploading one-at-a-time means one HTTP
        # round-trip per file, which dominates the wall-clock for large
        # batches (e.g. ~583s for 1476 files).  Upload in chunks instead so
        # each POST carries many files while keeping individual requests a
        # sane size.  Configurable via REPOMGR_UPLOAD_CHUNK (default 200).
        try:
            chunk_size = int(os.environ.get('REPOMGR_UPLOAD_CHUNK', '200'))
        except ValueError:
            chunk_size = 200
        if chunk_size < 1:
            chunk_size = 1
        for i in range(0, len(files_to_upload), chunk_size):
            chunk = files_to_upload[i:i + chunk_size]
            self.aptly.files.upload(upload_dir, *chunk)
        upload_elapsed = time.time() - t0

        # Add uploaded files into local repository.
        # Scale timeout: 1 min base + 1 min per 50 files (large batches take
        # longer, especially when other tasks run concurrently).  __wait_for_task
        # enforces a hard 1..29 minute range, so clamp here to stay valid — a
        # value of 30+ would be rejected outright as EINVAL and waste the upload.
        wait_minutes = max(2, 1 + len(pkg_files) // 50)
        wait_minutes = min(wait_minutes, 29)
        self.logger.info('Populating repo %s with %d package file(s) — this may take a '
                         'few minutes for large batches.', repo_name, len(pkg_files))
        task = self.aptly.repos.add_uploaded_file(repo_name, upload_dir, remove_processed_files=True)
        task_state = self.__wait_for_task(task, wait_minutes)
        total_elapsed = time.time() - t0
        if task_state != 'SUCCEEDED':
            self.logger.warning('add_upload_file failed %s : %s : %s (upload=%.1fs total=%.1fs)',
                                list(pkg_files)[0], repo_name, task_state,
                                upload_elapsed, total_elapsed)
            # Clean up the upload directory on failure
            try:
                self.aptly.files.delete(upload_dir)
            except Exception:
                pass
        else:
            self.logger.debug('upload_pkg_local %s -> %s: upload=%.1fs add=%.1fs total=%.1fs',
                              os.path.basename(list(pkg_files)[0]), repo_name,
                              upload_elapsed, total_elapsed - upload_elapsed, total_elapsed)
            # repo_name's package contents just changed -- drop its cached
            # package-key list (used by pkg_exist()/pkg_list()) so the next
            # lookup refetches instead of serving a stale pre-upload list.
            self._local_pkgs_cache.pop(repo_name, None)
            self._local_pkgs_detailed_cache.pop(repo_name, None)
        return True

    # Delete a Debian package from a local repository.
    # Input:
    #       local_repo: the name of the local repository
    #       pkg_name: the path-name of the deb file
    #       pkg_type: 'binary' or 'source'
    #       pkg_version: version of the deb file
    # Output: None
    def delete_pkg_local(self, local_repo, pkg_name, pkg_type, pkg_version=None):
        '''Delete a binary package from a local repository.'''
        # self.logger.debug('delete_pkg_local not supported yet.')
        if pkg_type not in {'binary', 'source'}:
            self.logger.error('package type must be one of either "binary" or "source"')
            return
        query = 'Name (= %s)' % pkg_name
        if pkg_version:
            query += ', Version (= %s)' % pkg_version
        # If we want more detailed info, add "detailed=True, with_deps=True" for search_packages.
        search_result = self.aptly.repos.search_packages(local_repo, query=query)
        self.logger.debug('delete_pkg_local found %d packages.' % len(search_result))
        for pkg in search_result:
            if (pkg_type == 'source' and pkg.key.split()[0] == 'Psource') or \
                (pkg_type != 'source' and pkg.key.split()[0] != 'Psource'):
                task = self.aptly.repos.delete_packages_by_key(local_repo, pkg.key)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Delete package failed %s : %s' % (pkg_name, task_state))
                else:
                    # local_repo's contents just changed -- drop the cached
                    # package-key list so the next pkg_exist()/pkg_list() call
                    # refetches instead of seeing an already-deleted package.
                    self._local_pkgs_cache.pop(local_repo, None)
                    self._local_pkgs_detailed_cache.pop(local_repo, None)

    def delete_pkgs_batch(self, local_repo, pkg_names, pkg_type='binary'):
        '''Delete multiple packages from a local repository in a single aptly operation.'''
        if not pkg_names:
            return True
        if pkg_type not in {'binary', 'source'}:
            self.logger.error('package type must be one of either "binary" or "source"')
            return False

        # Collect all matching package keys
        all_keys = []
        for pkg_name in pkg_names:
            query = 'Name (= %s)' % pkg_name
            try:
                search_result = self.aptly.repos.search_packages(local_repo, query=query)
            except Exception:
                continue
            for pkg in search_result:
                if (pkg_type == 'source' and pkg.key.split()[0] == 'Psource') or \
                    (pkg_type != 'source' and pkg.key.split()[0] != 'Psource'):
                    all_keys.append(pkg.key)

        if not all_keys:
            return True

        # Single batch delete
        task = self.aptly.repos.delete_packages_by_key(local_repo, *all_keys)
        # A batch delete from a large repo (thousands of packages) is I/O-heavy
        # on the aptly backend and can legitimately exceed the 1-minute
        # default, especially under concurrent load from other aptly
        # operations (publishes, other deletes) — observed repeatedly timing
        # out at ~60-68s in practice. Every other multi-package aptly
        # operation in this module already waits at least 5 minutes; align
        # this one with that pattern instead of the bare 1-minute default.
        task_state = self.__wait_for_task(task, 5)
        if task_state != 'SUCCEEDED':
            self.logger.warning('Batch delete failed (%d keys): %s' % (len(all_keys), task_state))
            return False
        self.logger.debug('delete_pkgs_batch: removed %d packages from %s', len(all_keys), local_repo)
        # local_repo's contents just changed -- see upload_pkg_local's comment.
        self._local_pkgs_cache.pop(local_repo, None)
        self._local_pkgs_detailed_cache.pop(local_repo, None)
        return True

    def _get_local_pkgs(self, repo_name):
        '''Return (and cache) the full package-key list for a local repo.

        Mirrors _get_mirror_pkgs() below, but for a local repo via
        aptly.repos.search_packages(repo_name, query='Name') (an unfiltered
        query matches every package). pkg_exist()'s local-repo branch
        previously called search_packages() with a per-package query on every
        invocation -- one live HTTP search per (package, local repo) pair,
        same redundant-traffic shape _get_mirror_pkgs() was already written to
        fix for remote mirrors. Invalidated by upload_pkg_local(),
        delete_pkg_local(), delete_pkgs_batch(), and copy_pkgs() (the only
        methods that mutate a local repo's contents), plus remove_local()/
        clean_all() (which drop it entirely).
        '''
        if repo_name not in self._local_pkgs_cache:
            self._local_pkgs_cache[repo_name] = self.aptly.repos.search_packages(repo_name, query='Name')
        return self._local_pkgs_cache[repo_name]

    def _get_mirror_pkgs(self, mirror_name):
        '''Return (and cache) the full package-key list for a remote mirror.

        aptly.mirrors.list_packages() returns the mirror's ENTIRE package
        index (every binary/source package it carries -- for a real Debian
        mirror that can be tens of thousands of entries) over HTTP, with no
        server-side filtering. Call sites like pkg_exist()/pkg_list() invoke
        this once per (package, repo) pair being checked; without caching,
        checking a few hundred packages against even one mirror means
        re-fetching that entire index a few hundred times. The mirror's
        content only changes via __update_mirror() (which invalidates this
        mirror's cache entry) or remove_remote()/clean_all() (which drop it
        entirely), so caching for the lifetime of this object between those
        points is safe.
        '''
        if mirror_name not in self._mirror_pkgs_cache:
            self._mirror_pkgs_cache[mirror_name] = self.aptly.mirrors.list_packages(mirror_name)
        return self._mirror_pkgs_cache[mirror_name]

    def pkg_list(self, repo_list):
        '''list packages available from any of the listed repos, local or remote.'''
        pkg_list=[]
        for repo_name in repo_list:
            if repo_name.startswith(PREFIX_LOCAL):
                pkgs_key = self._get_local_pkgs(repo_name)
            elif repo_name.startswith(PREFIX_REMOTE):
                pkgs_key = self._get_mirror_pkgs(repo_name)
            for key in pkgs_key:
                if isinstance(key, Package):
                    key = key.key
                pkg_name = key.split()[1]
                pkg_ver = key.split()[2]
                pkg_arch = key.split()[0][1:]
                if pkg_arch == 'source':
                    pkg_list.append("%s_%s.dsc" % (pkg_name, pkg_ver))
                else:
                    pkg_list.append("%s_%s_%s.deb" % (pkg_name, pkg_ver, pkg_arch))
        return pkg_list


    # Search a package in a set of repos, return True if find, or False
    # repolist: a list of repo names, including local repo and mirror
    # pkg_name: package name
    # architecture: Architecture of the package, now, only check 'source' or not
    # pkg_version:  the version of the package, None means version insensitive
    def pkg_exist(self, repo_list, pkg_name, architecture, pkg_version=None):
        '''Search a package in a bundle of repositories including local repo and remote one.'''
        for repo_name in repo_list:
            if repo_name.startswith(PREFIX_LOCAL):
                pkgs = self._get_local_pkgs(repo_name)
                for pkg in pkgs:
                    key_parts = pkg.key.split()
                    if key_parts[1] != pkg_name:
                        continue
                    if architecture != 'source' and key_parts[0] != 'Psource' and (not pkg_version or pkg_version == key_parts[2]):
                        self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                        return True
                    if architecture == 'source' and key_parts[0] == 'Psource' and (not pkg_version or pkg_version == key_parts[2]):
                        self.logger.debug('pkg_exist find package %s in %s.', pkg_name, repo_name)
                        return True
            elif repo_name.startswith(PREFIX_REMOTE):
                pkgs = self._get_mirror_pkgs(repo_name)
                for pkg in pkgs:
                    if isinstance(pkg, Package):
                        pkg = pkg.key
                    if pkg.split()[1] == pkg_name:
                        if architecture != 'source' and pkg.split()[0] != 'Psource' and (not pkg_version or pkg_version == pkg.split()[2]):
                            self.logger.debug('pkg_exist found package %s in %s.', pkg_name, repo_name)
                            return True
                        if architecture == 'source' and pkg.split()[0] == 'Psource' and (not pkg_version or pkg_version == pkg.split()[2]):
                            self.logger.debug('pkg_exist found package %s in %s.', pkg_name, repo_name)
                            return True
        return False

    # Copy a set of packages from one repository into another
    # source: the repository name that packages been copied from
    # dest: the repository name that packages been copied to
    # pkg_list: list of package name to be copied
    # pkg_type: binary or source. Default is binary
    # overwrite: True or False. Overwrite existing packages or not
    def copy_pkgs(self, source, dest, pkg_list, pkg_type='binary', overwrite=True):
        '''Copy package from one repository to another local repository'''
        dest_exist = False
        source_exist = False
        # package key list of destination and source repository
        dest_pkg_keys = list()
        src_pkg_keys = list()
        if source == dest:
            self.logger.error('%s and %s are the same repository.' % (source, dest))
            return False
        for repo in self.aptly.repos.list():
            if dest == repo.name:
                dest_exist = True
                pkgs = self.aptly.repos.search_packages(dest, query='Name')
                dest_pkg_keys = [pkg.key for pkg in pkgs]
            if source == repo.name:
                source_exist = True
                pkgs = self.aptly.repos.search_packages(source, query='Name')
                src_pkg_keys = [pkg.key for pkg in pkgs]
        if not dest_exist:
            self.logger.warning('Destination repository %s does not exist.', dest)
            return False
        if not source_exist:
            for repo in self.aptly.mirrors.list():
                if source == repo.name:
                    source_exist = True
                    src_pkg_keys = self._get_mirror_pkgs(source)
                    break
        if not source_exist:
            self.logger.warning('Source repository %s dose not exist.', source)
            return False
        del_keys = list()
        add_keys = list()
        for key in src_pkg_keys:
            if isinstance(key, Package):
                key = key.key
            package_name = key.split()[1]
            package_type = key.split()[0]
            if package_name not in pkg_list:
                continue
            if (pkg_type == 'source' and package_type != 'Psource') or (pkg_type == 'binary' and package_type == 'Psource'):
                continue
            # Find a package in source repository to be copied.
            pkg_list.remove(package_name)
            # Already exists in destination repository
            if key in dest_pkg_keys:
                continue
            pkg_in_dest = False
            for dest_key in dest_pkg_keys:
                # [0] package type/arch: Psource, Pamd64, Pall. [1] package name
                if package_type == dest_key.split()[0] and package_name == dest_key.split()[1]:
                    pkg_in_dest = True
                    if overwrite:
                        del_keys.append(dest_key)
                        add_keys.append(key)
                    break
            if not pkg_in_dest:
                add_keys.append(key)
            if not pkg_list:
                break

        # check to see if any packages not find in source repository
        if pkg_list:
            self.logger.warning('Copy package error, %s package %s not exist in %s' % (pkg_type, ' '.join(pkg_list), source))
            return False
        # Remove duplicate packages from destination repository
        if del_keys:
            task = self.aptly.repos.delete_packages_by_key(dest, *del_keys)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Delete packages failed: %s\n%s' % (task_state, '\n'.join(del_keys)))
                return False
        # Insert packages into destination repository
        if add_keys:
            task = self.aptly.repos.add_packages_by_key(dest, *add_keys)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Copy packages failed: %s\n%s' % (task_state, '\n'.join(add_keys)))
                return False
        if del_keys or add_keys:
            # dest's contents just changed -- see upload_pkg_local's comment.
            self._local_pkgs_cache.pop(dest, None)
            self._local_pkgs_detailed_cache.pop(dest, None)
        return True

    # Merge several repositories into a new one(just snapshot and publish)
    # name: the name of the new build snapshot/publish
    # source_snapshots: list, snapshots to be merged
    def merge_repos(self, name, source_snapshots):
        '''Merge several repositories into a new publish.

        Cross-process-locked (see _APTLY_SNAPSHOT_LOCK_DIR's module docstring):
        called via build-image's merge_local_repos() for REPO_ALL/deb-merge-all,
        and build-image itself runs from separate processes/containers in the
        'ISO' and 'remote-cli' Jenkins stages, which the monolithic pipeline
        runs in PARALLEL -- both merge the exact same deb-merge-all name.
        __merge_snapshot (drop/rename/recreate) and __publish_snap (the
        publish switch) are two sequential steps of one logical operation; the
        lock spans both so a concurrent caller can never observe or act on the
        intermediate state between them.
        '''
        if not name.startswith(PREFIX_MERGE):
            self.logger.warning('The name should started with %s.', PREFIX_MERGE)
            return None

        with self._cross_process_snapshot_lock(name):
            if self.__merge_snapshot(name, source_snapshots):
                ret = self.__publish_snap(name)
                return ret

    # deploy a local repository
    # Input
    #   name: the name of the local repository
    #   suffix: suffix of the publish name
    # Output: None or DebAptDistributionResponse
    def deploy_local(self, name, suffix=None, sign=False):
        '''Deploy a local repository.

        sign=False (default, unchanged behaviour) matches every pre-existing
        caller. sign=True is for ensure_deployed()'s call path -- see
        __quick_publish_repo's docstring for why.
        '''
        if not name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is NOT a well formed name.', name)
            return None

        repo_list = self.aptly.repos.list()
        repo_find = False
        for repo in repo_list:
            if name == repo.name:
                self.logger.debug('repo %s found, can be used', name)
                repo_find = True
                break
        if not repo_find:
            self.logger.debug('local repo %s not yet in list, proceeding anyway', name)
        if suffix is not None:
            return self.__quick_publish_repo(name, suffix, sign=sign)

        t0 = time.time()
        self.logger.info('deploy_local %s: snapshot + publish (full path)', name)
        new_snap = self.__create_snapshot(name, True)
        if new_snap:
            ret = self.__publish_snap(name, snap_name=new_snap)
            # After the atomic switch, remove the now-orphaned previous snapshot(s).
            if ret:
                self.__cleanup_old_snapshots(name, new_snap)
            # Delete orphan files, wait up to 5 minutes for the cleanup to complete
            if not self._skip_cleanup:
                task = self.aptly.db.cleanup()
                self.__wait_for_task(task, 5)
            self.logger.info('deploy_local %s completed in %.1fs', name, time.time() - t0)
            return ret
        self.logger.warning('deploy_local %s: snapshot creation failed (%.1fs)', name, time.time() - t0)
        return None

    def is_published_complete(self, name):
        '''Fast, HTTP-free check that a local repo has a healthy publication.

        Returns True only when ALL of the following hold:
          1. A publish exists at prefix `name`.
          2. Every snapshot source backing that publish actually exists.
          3. At least one backing snapshot is non-empty AND the published
             package count is not fewer than the source repo's package count
             (i.e. the publish is not a stale/partial snapshot missing packages).

        This detects the STRUCTURAL defects the aptly API can see — absent
        publish, dangling/missing source snapshot, empty or under-populated
        publication.  It deliberately does NOT probe the on-disk dists/ tree
        over HTTP; that mid-(re)publish filesystem gap is eliminated by the
        atomic `publish switch` path (__publish_snap), so it cannot occur.

        On any API error the check returns False (caller will (re)deploy),
        which is the safe direction.
        '''
        if not name.startswith(PREFIX_LOCAL):
            return False
        try:
            # 1. Publish exists?
            #    A CONCURRENT publish.switch (another X1 consumer deploying the
            #    same prefix) transiently removes the prefix from publish.list()
            #    for the duration of the switch (observed ~175s for a full
            #    deb-local-build switch). A single scan can therefore return a
            #    FALSE 'no publish', making the caller redundantly (and now
            #    racily) re-deploy — StarlingX build 1159 build-wheels #760.
            #    Ride out that window: re-scan a few times before concluding the
            #    publish is truly absent. A genuinely-absent publish simply costs
            #    a few extra seconds here before the caller deploys.
            publish = None
            _pub_retries = 5
            _pub_delay = 3
            for _attempt in range(_pub_retries):
                for p in self.aptly.publish.list():
                    if p.prefix == name:
                        publish = p
                        break
                if publish is not None:
                    break
                if _attempt < _pub_retries - 1:
                    self.logger.debug('is_published_complete(%s): no publish on scan %d/%d '
                                      '(a concurrent publish.switch may be in flight); retrying '
                                      'in %ds', name, _attempt + 1, _pub_retries, _pub_delay)
                    time.sleep(_pub_delay)
            if publish is None:
                self.logger.debug('is_published_complete(%s): no publish', name)
                return False

            # 2. Classify the publish's sources. Under the quick/in-place model
            #    (ensure_deployed / __quick_publish_repo) the publish is
            #    LOCAL-backed: its sources name the local REPO (e.g.
            #    'deb-local-build'), NOT a snapshot. Under the older snapshot
            #    model the sources name snapshot(s). Distinguish them so a
            #    local-backed publish is not falsely rejected for having "no
            #    backing snapshot" (build 1162 build-iso #776 logged
            #    'backing snapshot deb-local-build missing' for exactly this —
            #    the source was the local repo, not a snapshot).
            source_names = []
            sources = getattr(publish, 'sources', None) or []
            for src in sources:
                if isinstance(src, dict):
                    sn = src.get('Name') or src.get('name')
                else:
                    sn = getattr(src, 'name', None) or getattr(src, 'Name', None)
                if sn:
                    source_names.append(sn)

            existing_snaps = {s.name for s in self.aptly.snapshots.list()}
            repo_exists = any(r.name == name for r in self.aptly.repos.list())

            # Split sources into real snapshots vs the local-repo backing.
            snap_names = [sn for sn in source_names if sn in existing_snaps]
            local_backed = any(sn == name for sn in source_names) or (
                not snap_names and repo_exists)

            # Any source that is neither an existing snapshot nor the local repo
            # is a genuine dangling reference -> incomplete.
            for sn in source_names:
                if sn in existing_snaps:
                    continue
                if sn == name and repo_exists:
                    continue
                self.logger.debug('is_published_complete(%s): backing source %s missing '
                                  '(not an existing snapshot or local repo)', name, sn)
                return False

            # 3. Non-empty / not under-populated.
            if not repo_exists:
                # No source repo to compare against; accept a non-empty snapshot.
                for sn in snap_names:
                    pkgs = self.aptly.snapshots.list_packages(sn, with_deps=False, detailed=False)
                    if pkgs:
                        return True
                # Local-backed with no repo and no snapshot -> nothing to serve.
                if local_backed:
                    self.logger.debug('is_published_complete(%s): local-backed but source repo gone', name)
                    return False
                self.logger.debug('is_published_complete(%s): no source repo and empty snapshot', name)
                return False

            repo_pkgs = self.aptly.repos.search_packages(name)
            repo_count = len(repo_pkgs) if repo_pkgs is not None else 0

            if repo_count == 0:
                # Nothing expected in the repo — a publish (empty or not) is fine.
                return True

            if local_backed and not snap_names:
                # Local-backed publish serves the repo directly; a live publish
                # over a non-empty repo is complete (aptly regenerated the
                # indexes in place on the last publish.update). No snapshot to
                # count against.
                return True

            pub_count = 0
            for sn in snap_names:
                pkgs = self.aptly.snapshots.list_packages(sn, with_deps=False, detailed=False)
                pub_count += len(pkgs) if pkgs else 0

            if pub_count < repo_count:
                self.logger.debug('is_published_complete(%s): published %d < repo %d packages',
                                  name, pub_count, repo_count)
                return False
            return True
        except Exception as e:
            self.logger.debug('is_published_complete(%s): check error %s (treating as not-complete)',
                              name, e)
            return False

    def published_snapshot_name(self, name):
        '''Return the snapshot name currently backing the publish at `name`.

        downloader-2.0 keeps a repo like deb-local-binary published via the
        SNAPSHOT-backed path (deploy_local(name) -> __create_snapshot ->
        __publish_snap), and __create_snapshot now names snapshots uniquely
        ('<name>-<ts>-<hex>') rather than reusing the bare '<name>'. The old
        bare-name snapshot is additionally deleted by __cleanup_old_snapshots
        after the atomic switch, so there is NO snapshot literally named
        '<name>' for a consumer (build-image's merge_local_repos) to merge by.

        This resolves the publish prefix to the actual snapshot it points at,
        so the merge can reference the live, correct snapshot without
        build-image having to re-snapshot the repo itself (which would risk
        re-corrupting the publish via the incompatible local-backed path --
        the reason build-image deliberately skips snapshot_repo() for
        deb-local-binary; see build-image and
        test_build_image_binary_repo_publish.py).

        Returns the backing snapshot name (str) when the publish exists and is
        snapshot-backed, else None (no publish, local-backed publish, or any
        API error -- caller then falls back to the bare name).
        '''
        try:
            publish = None
            for p in self.aptly.publish.list():
                if p.prefix == name:
                    publish = p
                    break
            if publish is None:
                self.logger.debug('published_snapshot_name(%s): no publish', name)
                return None

            existing_snaps = {s.name for s in self.aptly.snapshots.list()}
            sources = getattr(publish, 'sources', None) or []
            for src in sources:
                if isinstance(src, dict):
                    sn = src.get('Name') or src.get('name')
                else:
                    sn = getattr(src, 'name', None) or getattr(src, 'Name', None)
                # Only accept a source that is an actual snapshot; a
                # local-backed publish names the repo itself, not a snapshot.
                if sn and sn in existing_snaps:
                    self.logger.debug('published_snapshot_name(%s): backing snapshot %s', name, sn)
                    return sn
            self.logger.debug('published_snapshot_name(%s): publish is not snapshot-backed', name)
            return None
        except Exception as e:
            self.logger.debug('published_snapshot_name(%s): lookup error %s', name, e)
            return None

    def sign_publish(self, name):
        '''Re-publish `name` IN PLACE with real GPG signing (one-time, final step).

        Every incremental publish during the build (__quick_publish_repo,
        __publish_snap's atomic switch) uses sign_skip=True so per-package
        publish/update calls stay fast (~0.5s) — signing on every one of the
        hundreds of updates in a build would be wasted work, since only the
        FINAL content matters to a consumer reading the repo after the build
        completes. This method does the one signature that actually matters:
        called once, after all builds/uploads are done, it re-publishes the
        current (already up to date) content with sign_gpgkey/sign_passphrase
        so InRelease/Release.gpg are generated fresh and match the final
        Release exactly.

        Without this, InRelease/Release.gpg are either entirely absent
        (repos that only ever went through sign_skip=True, e.g.
        deb-local-build — apt then hard-fails with "repository ... is not
        signed") or, worse, stale leftovers from a much older signed publish
        that no longer match the current Release content (any repo that had
        a signed create at some point in its history, then further
        sign_skip=True updates) — a latent invalid-signature landmine that
        just hasn't been hit yet.

        Skips local repos/publications that don't exist or are empty (no
        content, nothing to sign). Best-effort: failures are logged and
        do not raise, so one repo failing to sign does not abort the whole
        pass — apt will simply fail loudly and specifically on that repo,
        same as today, rather than the whole build's exit code flipping on a
        cosmetic finalization step.
        '''
        try:
            publish_list = self.aptly.publish.list()
        except Exception as e:
            self.logger.warning('sign_publish(%s): could not list publishes: %s', name, e)
            return False

        existing = None
        for publish in publish_list:
            if publish.prefix == name:
                existing = publish
                break
        if existing is None:
            self.logger.debug('sign_publish(%s): no publish exists, nothing to sign', name)
            return False

        t0 = time.time()
        try:
            task = self.aptly.publish.update(
                prefix=name,
                distribution=existing.distribution,
                sign_gpgkey=SIGN_KEY,
                sign_passphrase=SIGN_PASSWD,
                skip_contents=True,
                skip_cleanup=True,
                force_overwrite=True,
            )
            task_state = self.__wait_for_task(task, 5, quiet_fail=True)
        except Exception as e:
            self.logger.warning('sign_publish(%s): publish.update raised %s', name, e)
            return False

        if task_state != 'SUCCEEDED':
            self.logger.warning('sign_publish(%s): signing failed (%s, %.1fs)',
                                name, task_state, time.time() - t0)
            return False

        self.logger.info('sign_publish(%s): signed in %.1fs', name, time.time() - t0)
        return True

    def ensure_deployed(self, name):
        '''Idempotently guarantee `name` has a healthy publication.

        No-op (fast path) when the repo is already completely published; only
        performs a deploy when the publication is absent or structurally
        defective.  Never re-publishes a healthy repo — a destructive
        re-publish of an already-good repo was the root cause of the concurrent
        apt-get 404 (StarlingX build 1155).

        Shared entry point for every consumer of a local repo (ISO/build-image,
        wheels, docker-images) so they all use the SAME readiness definition.

        IMPORTANT — uses the QUICK (in-place, local-backed) publish path, NOT
        the snapshot+switch full path. These repos (deb-local-build*) are
        published from a LOCAL repo and kept current incrementally during
        build-packages via __quick_publish_repo (publish.update WITHOUT
        snapshots — the in-place local form). The snapshot+switch full path
        (deploy_local(name) with suffix=None) is architecturally wrong for
        them and caused a cascade of failures when consumers called it:
          - aptly rejects publish.update(snapshots=...) on a local-backed
            publish with "400 snapshots shouldn't be given when updating local
            repo" (build 1162 build-iso #776), and
          - the snapshot full path creates uniquely-named snapshots
            (deb-local-build-<ts>-<hex>), but the downstream merge_repos()
            expects a snapshot named exactly `deb-local-build`, so the merge
            then fails with "snapshot deb-local-build does not exist".
        The quick/local path re-publishes in place at the bare prefix `name`
        (suffix='' -> publish_name == name), which is exactly what consumers
        read, with no snapshot churn and no 404 window.

        Returns the publish descriptor string on success, or None on failure.
        '''
        if not name.startswith(PREFIX_LOCAL):
            self.logger.warning('ensure_deployed: %s is not a well-formed local repo name', name)
            return None
        # Cross-process-locked for the SAME reason merge_repos() is (see
        # _cross_process_snapshot_lock's docstring): build-iso and
        # build-wheels (and build-docker-images) each call ensure_deployed on
        # the SAME repo name (e.g. deb-local-build) from separate processes/
        # containers, and the monolithic pipeline runs those Jenkins stages
        # concurrently. Without this lock, two callers can both observe
        # is_published_complete() == False (TOCTOU), then both call
        # deploy_local(name, suffix='') -> publish.create, and the second
        # one's aptly call fails with "prefix/distribution already used by
        # another published repo" -- observed live: StarlingX monolithic
        # #1219's build-wheels #779 and build-iso #793 raced on
        # ensure_deployed('deb-local-build') and failed exactly this way.
        # The lock must span BOTH the check and the deploy, like
        # merge_repos's, so a second caller that acquires the lock after the
        # first finishes sees the NOW-complete publish and takes the no-op
        # fast path instead of racing the create.
        with self._cross_process_snapshot_lock(name):
            if self.is_published_complete(name):
                self.logger.info('ensure_deployed: %s already published and complete (no-op)', name)
                for p in self.aptly.publish.list():
                    if p.prefix == name:
                        return p.prefix + ' ' + p.distribution
                return name + ' ' + DEBIAN_DISTRIBUTION
            self.logger.info('ensure_deployed: %s absent or incomplete — deploying (quick/in-place)', name)
            # suffix='' => publish at the bare prefix `name` via the in-place
            # local publish.update path (NOT the snapshot+switch full path).
            # sign=True: this (re)create/update must produce a REAL
            # signature, not sign_skip=True's placeholder-free publish. This
            # path can run well after build-pkgs-2.0's one-time sign_publish
            # pass has already signed `name` -- e.g. a concurrent build-iso/
            # build-wheels/build-docker-images consumer whose own
            # merge_local_repos drops and recreates this repo's publish as
            # part of building deb-merge-all. An unsigned recreate here would
            # silently revert an already-signed repo, and every OTHER
            # consumer reading it afterward (including this same build-iso's
            # own chroot `apt update`) would then hard-fail with "repository
            # ... is not signed" -- observed live: StarlingX monolithic
            # #1221's build-iso #795 did exactly this to deb-local-build ~2
            # minutes after publish-packages had already signed it.
            return self.deploy_local(name, suffix='', sign=True)

    # remove a local repository
    # Input: the name of the local repository
    # Output: None
    def remove_local(self, name):
        '''Delete a local repository, including related publish and snapshot.'''
        if not name.startswith(PREFIX_LOCAL):
            self.logger.warning('%s is not a correct name', name)
            return None

        # find and remove related publish
        publish_list = self.aptly.publish.list()
        for publish in publish_list:
            # Remove all related publish including quick publish
            if publish.prefix.startswith(name + '-') or publish.prefix == name:
                try:
                    task = self.aptly.publish.drop(prefix=publish.prefix, distribution=DEBIAN_DISTRIBUTION, force_delete=True)
                    task_state = self.__wait_for_task(task)
                    if task_state != 'SUCCEEDED':
                        self.logger.warning('Drop publish failed %s : %s', name, task_state)
                except Exception as e:
                    self.logger.warning('Drop publish %s/%s: %s (ignored)',
                                        publish.prefix, DEBIAN_DISTRIBUTION, e)

        # find and remove related snapshot
        snap_list = self.aptly.snapshots.list()
        for snap in snap_list:
            if snap.name == name:
                task = self.aptly.snapshots.delete(snapshotname=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop snapshot failed %s : %s', name, task_state)

        # find and remove the remote(mirror)
        repo_list = self.aptly.repos.list()
        for repo in repo_list:
            if repo.name == name:
                task = self.aptly.repos.delete(reponame=name, force=True)
                task_state = self.__wait_for_task(task)
                if task_state != 'SUCCEEDED':
                    self.logger.warning('Drop repo failed %s : %s', name, task_state)

        # Invalidate caches: the repo itself (and whatever package list it
        # had) is gone, and the set of local repos just changed.
        self._list_local_cache = None
        self._local_pkgs_cache.pop(name, None)
        self._local_pkgs_detailed_cache.pop(name, None)

        # Delete orphan files, wait up to 5 minutes for the cleanup to complete
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)

        return None

    # clean all metadata including remote, repository, public, distribution, task and content
    # In theory, with this operation, there should be nothing left in aptly_deb
    # database. Please use it carefully.
    def clean_all(self):
        '''Clean all metadata including remote, repository, public, distribution, task and content.'''
        # clean publishes
        pub_list = self.aptly.publish.list()
        self.logger.info('%d publish', len(pub_list))
        for pub in pub_list:
            self.logger.info('Drop publish %s : %s', pub.prefix, pub.distribution)
            task = self.aptly.publish.drop(prefix=pub.prefix, distribution=pub.distribution, force_delete=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop publish failed %s : %s', pub.prefix, task_state)
        # clean snapshots
        snap_list = self.aptly.snapshots.list()
        self.logger.info('%d snapshot', len(snap_list))
        for snap in snap_list:
            self.logger.info('Drop snapshot %s', snap.name)
            task = self.aptly.snapshots.delete(snapshotname=snap.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop snapshot failed %s : %s', snap.name, task_state)

        # clean mirrors
        mirror_list = self.aptly.mirrors.list()
        self.logger.info('%d mirror', len(mirror_list))
        for mirror in mirror_list:
            task = self.aptly.mirrors.drop(name=mirror.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop mirror failed %s : %s', mirror.name, task_state)
        # clean local repos
        repo_list = self.aptly.repos.list()
        self.logger.info('%d repo', len(repo_list))
        for repo in repo_list:
            task = self.aptly.repos.delete(reponame=repo.name, force=True)
            task_state = self.__wait_for_task(task)
            if task_state != 'SUCCEEDED':
                self.logger.warning('Drop repo failed %s : %s', repo.name, task_state)
        # clean file folders
        file_list = self.aptly.files.list()
        self.logger.info('%d file folder', len(file_list))
        for file in file_list:
            self.aptly.files.delete(file)
        # clean tasks
        self.aptly.tasks.clear()
        # Delete orphan files, up to 5 minutes
        task = self.aptly.db.cleanup()
        self.__wait_for_task(task, 5)
        # Invalidate all caches: every local repo and remote mirror was just
        # dropped.
        self._list_local_cache = None
        self._list_remotes_cache = None
        self._mirror_pkgs_cache = {}
        self._local_pkgs_cache = {}
        self._local_pkgs_detailed_cache = {}
