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

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import apt
import apt.progress.base
import apt_pkg as apt_pkg_module


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
        # AllowUnauthenticated/AllowInsecureRepositories are required because
        # local aptly repos and snapshot mirrors don't have GPG signatures.
        # This does not affect dependency resolution.
        apt_pkg_module.init_config()
        apt_pkg_module.config.set("Dir", str(self.base_dir))
        apt_pkg_module.config.set("APT::Get::AllowUnauthenticated", "true")
        apt_pkg_module.config.set("Acquire::AllowInsecureRepositories", "true")
        apt_pkg_module.config.set("Acquire::Retries", "3")
        apt_pkg_module.config.set("Acquire::http::Timeout", "30")
        apt_pkg_module.config.set("Acquire::https::Timeout", "30")
        apt_pkg_module.config.set("Acquire::ftp::Timeout", "30")
        apt_pkg_module.init_system()

        self.cache = apt.Cache(rootdir=str(self.base_dir))

    def _setup(self):
        """Initialize directory structure."""
        (self.etc_apt / "sources.list.d").mkdir(parents=True, exist_ok=True)
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

    def update(self):
        """Update APT cache."""
        cmd = [
            "apt-get", "update",
            f"-o=Dir={self.base_dir}",
            f"-o=Dir::State={self.var_lib}",
            f"-o=Dir::Cache={self.var_cache}",
            f"-o=Dir::Etc::SourceList={self.sources_list}",
            f"-o=Dir::Etc::SourceParts={self.etc_apt / 'sources.list.d'}",
            "-o=APT::Get::AllowUnauthenticated=true",
            "-o=Acquire::AllowInsecureRepositories=true"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        # Allow warnings but fail on actual errors that prevent cache from working
        if result.returncode != 0 and "E: Failed to fetch" in result.stderr:
            raise RuntimeError(f"Update failed: {result.stderr}")

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
        """Get dependencies of a package. Returns dict with package names and their info."""
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

        # Recursive: use mark_install to get full dependency tree
        self.cache.clear()
        try:
            pkg.mark_install()
        except Exception:
            # If mark_install fails, fall back to manual recursion
            return self._manual_recursive_deps(package_name)

        deps = {}
        for p in self.cache.get_changes():
            if p.name != package_name and p.marked_install:
                deps[p.name] = p.candidate.version

        # Reset cache state
        self.cache.clear()
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
        self.cache.close()
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir)

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

