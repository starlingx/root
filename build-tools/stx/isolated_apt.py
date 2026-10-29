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

#!/usr/bin/env python3
"""Isolated APT environment manager using python-apt.

This module provides a class for creating and managing temporary APT environments
that are isolated from the host system. It allows querying package information,
dependencies, and repository contents without affecting the system's APT state.

Key features:
- Creates temporary APT directory structure (etc/apt, var/lib/apt, var/cache/apt)
- Supports adding custom APT sources (local repos, URLs, file paths)
- Queries package information and dependencies
- Identifies missing dependencies
- Automatic cleanup of temporary environment

Usage:
    with IsolatedApt() as apt:
        apt.add_source("deb file:///path/to/repo dist main")
        apt.update()
        pkg = apt.show("package-name")
        deps = apt.get_dependencies("package-name")
"""

import os
import shutil
import subprocess
import tempfile
import logging
import threading
from pathlib import Path
from typing import Optional

import apt
import apt.progress.base
import apt_pkg as apt_pkg_module


# apt_pkg exposes a single PROCESS-GLOBAL configuration/system state
# (apt_pkg.config, init_config(), init_system()) and apt.Cache reads it.
# The python-apt C library is not thread-safe, so constructing two
# IsolatedApt instances from different threads at the same time would race
# on that shared global state. Serialize just the global-mutating section of
# __init__ with this module-level lock. In normal StarlingX use the parent
# builds one shared IsolatedApt before the download thread pool starts
# (see downloader-2.0), so this lock is uncontended and adds no measurable
# time; it only guards against concurrent construction by a future caller.
_APT_PKG_GLOBAL_LOCK = threading.Lock()


class IsolatedApt:
    """Manages a temporary APT environment isolated from the host."""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="apt_env_"))
        self.etc_apt = self.base_dir / "etc/apt"
        self.var_lib = self.base_dir / "var/lib/apt"
        self.var_lib_dpkg = self.base_dir / "var/lib/dpkg"
        self.var_cache = self.base_dir / "var/cache/apt"
        self.sources_list = self.etc_apt / "sources.list"
        self._setup()

        # Configure apt_pkg for isolated environment.
        # apt_pkg.config / init_config / init_system / apt.Cache all touch the
        # single process-global apt state, which is not thread-safe; hold the
        # module lock so two concurrent constructions can't interleave here.
        with _APT_PKG_GLOBAL_LOCK:
            # Set Dir BEFORE init_config so apt doesn't try to read host dirs
            # under the temp path (avoids "Unable to read apt.conf.d" warning).
            apt_pkg_module.config.set("Dir", str(self.base_dir))
            apt_pkg_module.init_config()
            apt_pkg_module.config.set("APT::Get::AllowUnauthenticated", "true")
            apt_pkg_module.config.set("Acquire::AllowInsecureRepositories", "true")
            apt_pkg_module.config.set("Acquire::Retries", "3")
            apt_pkg_module.config.set("Acquire::http::Timeout", "30")
            apt_pkg_module.config.set("Acquire::https::Timeout", "30")
            apt_pkg_module.config.set("Acquire::ftp::Timeout", "30")
            # Clear docker-clean hooks that silently delete downloaded .deb files
            # from apt cache (defined in /etc/apt/apt.conf.d/docker-clean).
            apt_pkg_module.config.clear("APT::Update::Post-Invoke")
            apt_pkg_module.config.clear("APT::Update::Post-Invoke-Success")
            apt_pkg_module.init_system()

            self.cache = apt.Cache(rootdir=str(self.base_dir))

    def _setup(self):
        """Initialize directory structure."""
        (self.etc_apt / "sources.list.d").mkdir(parents=True, exist_ok=True)
        (self.etc_apt / "apt.conf.d").mkdir(parents=True, exist_ok=True)
        (self.etc_apt / "trusted.gpg.d").mkdir(parents=True, exist_ok=True)
        (self.etc_apt / "preferences.d").mkdir(parents=True, exist_ok=True)
        (self.var_lib / "lists/partial").mkdir(parents=True, exist_ok=True)
        (self.var_cache / "archives/partial").mkdir(parents=True, exist_ok=True)
        self.var_lib_dpkg.mkdir(parents=True, exist_ok=True)
        self.sources_list.touch()
        (self.var_lib_dpkg / "status").touch()
        (self.var_lib_dpkg / "available").touch()

    def add_source(self, source: str):
        """Add APT source (deb line or file path/URL)."""
        if source.startswith(("http://", "https://", "file://")):
            source = f"deb {source}"
        elif Path(source).exists():
            source = f"deb file://{Path(source).resolve()}"

        with open(self.sources_list, "a") as f:
            f.write(f"{source}\n")

    def update(self, logger=None):
        """Update APT cache, streaming apt's progress to the log.

        Runs `apt-get update` against the configured sources and logs each line
        of apt's output as it arrives, so a slow-but-working update is visibly
        progressing rather than appearing hung. An overall timeout (seconds,
        via STX_APT_UPDATE_TIMEOUT, default 600) bounds the total time so a
        stalled mirror can't hang the downloader indefinitely; it is enforced
        even if apt emits no output (a watchdog kills the process at the
        deadline). apt's per-connection Acquire::*::Timeout (set in __init__)
        does not bound total time across many index files/retries.

        Pass ``logger`` to stream apt's output through the caller's configured
        logger (recommended — the default "isolated-apt" logger has no handler
        of its own, so its records rely on the root logger being configured).
        """
        cmd = [
            "apt-get", "update",
            f"-o=Dir={self.base_dir}",
            f"-o=Dir::State={self.var_lib}",
            f"-o=Dir::Cache={self.var_cache}",
            f"-o=Dir::Etc={self.etc_apt}",
            f"-o=Dir::Etc::SourceList={self.sources_list}",
            f"-o=Dir::Etc::SourceParts={self.etc_apt / 'sources.list.d'}",
            f"-o=Dir::Etc::TrustedParts={self.etc_apt / 'trusted.gpg.d'}",
            "-o=APT::Get::AllowUnauthenticated=true",
            "-o=Acquire::AllowInsecureRepositories=true",
            "-o=Acquire::AllowDowngradeToInsecureRepositories=true",
        ]

        try:
            timeout_s = int(os.environ.get("STX_APT_UPDATE_TIMEOUT", "600"))
        except ValueError:
            timeout_s = 600

        logger = logger or logging.getLogger("isolated-apt")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)

        # Watchdog: kill the process if it exceeds the deadline, so the reader
        # loop below can't block forever on a stalled fetch that emits nothing.
        timed_out = {"flag": False}

        def _watchdog():
            if not _done.wait(timeout_s):
                timed_out["flag"] = True
                try:
                    proc.kill()
                except Exception:
                    pass

        _done = threading.Event()
        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()

        captured = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    captured.append(line)
                    logger.info("apt-get update: %s", line)
        finally:
            proc.wait()
            _done.set()
            wd.join(timeout=1)

        output = "\n".join(captured)

        if timed_out["flag"]:
            raise RuntimeError(
                f"apt-get update timed out after {timeout_s}s "
                f"(set STX_APT_UPDATE_TIMEOUT to change). "
                f"A configured mirror may be slow/unreachable.\n"
                f"Sources: {self.sources_list}\n"
                f"Partial output:\n{output}")

        # Allow warnings but fail on real fetch errors that prevent cache from
        # working. Ignore 'rm: cannot remove' from system apt hooks
        # (docker-clean) that may leak through, and ignore 404s from repos that
        # don't exist yet.
        if proc.returncode != 0 and "E: Failed to fetch" in output:
            if "rm: cannot remove" in output:
                pass  # Harmless: system docker-clean hook tried to clean cache
            else:
                raise RuntimeError(f"Update failed: {output}")

        self.cache.open()

    def show(self, package_name: str):
        """Get package object."""
        return self.cache.get(package_name)

    def search(self, pattern: str):
        """Search for packages matching pattern."""
        for pkg in self.cache:
            if pattern.lower() in pkg.name.lower():
                yield pkg

    def get_dependencies(self, package_name: str, recursive: bool = True):
        """Get dependencies of a package. Returns dict with package names and their info.

        Both the recursive and non-recursive paths walk pkg.candidate.
        dependencies directly (first alternative of each dependency group)
        rather than invoking apt's full SAT-style installability resolver
        (cache.mark_install()/cache.get_changes()). The resolver walk was
        previously used for recursive=True, but each call reruns apt's
        dependency resolution against the ENTIRE loaded cache (tens of
        thousands of packages for a full Debian archive snapshot) and then
        clears it -- when this is called once per seed name from a caller
        with hundreds or thousands of seeds (e.g. build-pkgs-2.0's
        _load_mirror_metadata_for_build_order(), seeding from every in-tree
        source's Build-Depends plus every .lst-pinned name), the cumulative
        cost made mirror metadata loading take 10+ minutes with zero
        intermediate log output, easily mistaken for a hang. A plain
        name-level BFS over the already-parsed dependency metadata needs no
        further apt_pkg resolver calls and is the same flat "recursive
        Depends/Pre-Depends closure by name" result this caller actually
        needs (it is not asking "is this installable", just "what does this
        transitively Depend on").
        """
        pkg = self.show(package_name)
        if not pkg or not pkg.candidate:
            return {}

        if not recursive:
            deps = {}
            for dep_group in pkg.candidate.dependencies:
                for dep in dep_group:
                    dep_pkg = self.show(dep.name)
                    if dep_pkg and dep_pkg.candidate:
                        deps[dep.name] = dep_pkg.candidate.version
            return deps

        # Recursive: plain BFS over each visited package's own
        # pkg.candidate.dependencies (first alternative per group) -- no
        # apt_pkg resolver/cache mutation involved, so this is safe to call
        # repeatedly without clearing/restoring cache state.
        deps = {}
        visited = {package_name}
        frontier = [package_name]
        while frontier:
            next_frontier = []
            for name in frontier:
                cur = self.show(name)
                if not cur or not cur.candidate:
                    continue
                for dep_group in cur.candidate.dependencies:
                    for dep in dep_group:
                        if dep.name in visited:
                            continue
                        dep_pkg = self.show(dep.name)
                        if not dep_pkg or not dep_pkg.candidate:
                            continue
                        visited.add(dep.name)
                        deps[dep.name] = dep_pkg.candidate.version
                        next_frontier.append(dep.name)
            frontier = next_frontier
        return deps

    def _manual_recursive_deps(self, package_name: str, visited=None):
        """Manually walk dependency tree."""
        if visited is None:
            visited = set()

        if package_name in visited:
            return {}
        visited.add(package_name)

        pkg = self.show(package_name)
        if not pkg or not pkg.candidate:
            return {}

        deps = {}
        for dep_group in pkg.candidate.dependencies:
            for dep in dep_group:
                dep_pkg = self.show(dep.name)
                if dep_pkg and dep_pkg.candidate:
                    deps[dep.name] = dep_pkg.candidate.version
                    # Recurse
                    sub_deps = self._manual_recursive_deps(dep.name, visited)
                    deps.update(sub_deps)

        return deps

    def get_missing_dependencies(self, package_name: str):
        """Get dependencies that are declared but not available in the repository."""
        missing = {}
        visited = set()

        def is_satisfied(dep_name):
            """Check if dependency is satisfied by a real or virtual package."""
            # Check for real package
            if self.show(dep_name):
                return True
            # Check if any package provides this virtual package
            for pkg in self.cache:
                if pkg.candidate and pkg.candidate.provides:
                    for provided in pkg.candidate.provides:
                        provided_name = provided if isinstance(provided, str) else provided.name
                        if provided_name == dep_name:
                            return True
            return False

        def check_deps(pkg_name):
            if pkg_name in visited:
                return
            visited.add(pkg_name)

            pkg = self.show(pkg_name)
            if not pkg or not pkg.candidate:
                return

            for dep_group in pkg.candidate.dependencies:
                dep_type = dep_group.rawtype
                for dep in dep_group:
                    if not is_satisfied(dep.name):
                        if dep.name not in missing:
                            missing[dep.name] = {"required_by": [], "type": dep_type}
                        missing[dep.name]["required_by"].append(pkg_name)
                    else:
                        # Recurse into satisfied dependencies
                        check_deps(dep.name)

        check_deps(package_name)
        return missing

    def cleanup(self):
        """Remove temporary environment."""
        try:
            self.cache.close()
        except Exception:
            pass
        if getattr(self, '_detached', False):
            # base_dir ownership was handed off (e.g. to be reused by another
            # phase); don't delete it here.
            return
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir)

    def invalidate_source_lists(self, url_substring: str):
        """Remove cached index list files (Release/Packages/etc.) for sources
        matching url_substring, so a subsequent update() is forced to do a
        full re-fetch instead of trusting locally cached metadata.

        Use this before handing a detached apt environment off for reuse by
        another phase, when a source it already indexed (e.g. a local repo)
        is republished with new content in between: without invalidation,
        apt's cached Release/Packages for that source stays pinned to
        whatever was live at the time of the earlier update(), and the next
        update() can end up validating a freshly-downloaded Packages file
        against that stale cached Release (size/hash mismatch -> "File has
        unexpected size... Mirror sync in progress?").

        apt's list filenames are a URI-derived hash/encoding of the source
        URL (e.g. 'stx-dev-master-1-stx-repomgr:80_deb-local-binary_dists_...'),
        so matching is done by substring on the filename rather than trying
        to reconstruct apt's exact encoding.
        """
        lists_dir = self.var_lib / "lists"
        if not lists_dir.is_dir():
            return
        removed = []
        for entry in lists_dir.iterdir():
            if entry.name == "partial":
                continue
            if url_substring in entry.name:
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    removed.append(entry.name)
                except OSError:
                    pass
        return removed

    def detach(self):
        """Release the apt cache but KEEP base_dir on disk, handing ownership
        to the caller. After this, cleanup()/__del__ will not delete base_dir.

        Returns the base_dir path so another IsolatedApt can be constructed
        against it (reusing already-fetched index lists). The caller is then
        responsible for removing the directory when done.
        """
        try:
            self.cache.close()
        except Exception:
            pass
        self._detached = True
        return self.base_dir

    def __del__(self):
        """Auto-cleanup on deletion."""
        try:
            self.cleanup()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

