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
# Copyright (C) 2024-2026 Wind River Systems, Inc.

#!/usr/bin/env python3
"""
Debian Package Dependency Analyzer

Analyzes build dependencies between source packages to:
1. Determine build order
2. Detect circular dependencies
3. Calculate build priorities
4. Resolve circular dependencies using downloaded binaries

Algorithm:
- Source packages build-depend on binary packages
- Binary packages have runtime dependencies on other binary packages
- We trace through runtime dependencies to find which source packages
  a source package transitively depends on
"""

import os
import apt_pkg
import logging
import re
from typing import Dict, Set, List, Tuple, Optional
from debian_package import DebianSourcePackage, DebianBinaryPackage
from debian_package_set import DebianPackageSet
from isolated_apt import IsolatedApt


class CircularDependency(Exception):
    """Raised when a circular dependency is detected."""
    def __init__(self, cycle: List[str]):
        self.cycle = cycle
        super().__init__(f"Circular dependency detected: {' -> '.join(cycle)}")


class DependencyAnalyzer:
    """Analyzes dependencies between Debian source packages."""

    def __init__(self, package_set: DebianPackageSet, logger=None):
        self.package_set = package_set
        self.logger = logger

        # Map: binary package name -> source package name
        self.binary_to_source: Dict[str, str] = {}

        # Map: source package name -> set of binary packages it produces
        self.source_produces: Dict[str, Set[str]] = {}

        # Map: source package name -> set of source packages it depends on
        self.source_depends_on: Dict[str, Set[str]] = {}

        # Map: source package name -> set of source packages that depend on it
        self.source_depended_by: Dict[str, Set[str]] = {}

        # Map: source package name -> priority (higher = build later)
        self.build_priority: Dict[str, int] = {}

        # Track circular dependency resolutions using downloaded binaries
        self.circular_resolutions: List[Dict] = []

        self._build_mappings()

    def _build_mappings(self):
        """Build the binary->source and source->binaries mappings."""
        # Use binary_packages_unified if available, otherwise fall back to
        # binary_packages_from_source (populated by load_source_from_starlingx_sources)
        bin_packages = self.package_set.binary_packages_unified
        if not bin_packages:
            bin_packages = self.package_set.binary_packages_from_source

        # Map all binary packages to their source packages
        for bin_name, bin_pkg in bin_packages.items():
            if bin_pkg.source_package:
                self.binary_to_source[bin_name] = bin_pkg.source_package

                if bin_pkg.source_package not in self.source_produces:
                    self.source_produces[bin_pkg.source_package] = set()
                self.source_produces[bin_pkg.source_package].add(bin_name)

    def _clean_dependency(self, dep_str: str) -> str:
        """Extract clean package name from dependency string.

        Examples:
            'libc6 (>= 2.31)' -> 'libc6'
            'gcc-10 | gcc' -> 'gcc-10'
            'pahole <!stage1>' -> 'pahole'
            'dwarves:native (>= 1.16)' -> 'dwarves'
            'linux@KERNEL_TYPE@-headers' -> None (unresolved template)
        """
        # Take first alternative if multiple options
        if '|' in dep_str:
            dep_str = dep_str.split('|')[0]

        # Remove build profiles <...>
        dep_str = re.sub(r'<[^>]*>', '', dep_str)

        # Remove version constraints (...)
        dep_str = re.sub(r'\([^)]*\)', '', dep_str)

        # Remove architecture qualifiers [...]
        dep_str = re.sub(r'\[[^\]]*\]', '', dep_str)

        # Remove architecture qualifiers :native, :any, etc.
        dep_str = re.sub(r':[a-z0-9]+', '', dep_str)

        return dep_str.strip()

    def expand_template_dep(self, dep_name: str, build_types: List[str]) -> List[str]:
        """Expand @KERNEL_TYPE@ template in a dependency name.

        Returns list of expanded names, or empty list if dep has unknown templates.
        """
        if '@KERNEL_TYPE@' in dep_name:
            expanded = []
            for bt in build_types:
                expanded.append(dep_name.replace('@KERNEL_TYPE@', '' if bt == 'std' else f'-{bt}'))
            return expanded
        if '@' in dep_name:
            return []  # Unknown template, skip
        return [dep_name]

    # The kernel source package dynamically generates versioned binary package
    # names at build time via debian/bin/gencontrol.py (e.g. linux-headers-6.6.0-1-amd64,
    # linux-kbuild-6.12, linux-rt-image-6.6.0-1-rt-amd64-unsigned). These names
    # embed the kernel ABI version and do NOT appear in the repacked .dsc Binary:
    # field because gencontrol.py requires build-deps (kernel-wedge) that aren't
    # available during repack. Other packages like meta-base depend on these
    # versioned names, causing false "missing dependency" reports.
    #
    # Example: meta-base Depends: linux-headers-6.6.0-1-amd64, linux-kbuild-6.12
    # but the linux .dsc only lists linux-image-stx-amd64, linux-headers-stx-amd64
    # (the static metapackage stanzas we patched into debian/control).
    #
    # Solution: if a dependency name matches this pattern and we have 'linux' or
    # 'linux-rt' as a source package we build, assume it will be satisfied after
    # the kernel is compiled.
    _KERNEL_VERSIONED_RE = re.compile(
        r'^linux(?:-rt)?-(?:headers|image|kbuild|modules|support|compiler-gcc-\d+)-\d')

    # Regex for static kernel meta-packages (e.g. linux-headers-stx-amd64,
    # linux-rt-keys, linux-rt-headers-stx-amd64).  These are produced by
    # the kernel build but don't appear in binary_to_source because the
    # kernel uses dl_hook instead of a static debian/control.
    _KERNEL_STATIC_RE = re.compile(
        r'^linux(-rt)?-(headers-stx-|keys|image-stx-|support-|perf$)')

    # Non-linux-prefixed binaries produced by the kernel build
    _KERNEL_EXTRA_BINARIES = frozenset([
        'bpftool', 'hyperv-daemons', 'libcpupower1', 'libcpupower-dev', 'usbip',
    ])

    def _infer_kernel_source(self, bin_name: str) -> Optional[str]:
        """Infer the source package for a kernel binary not in binary_to_source.

        Returns 'linux' or 'linux-rt' if the binary name matches kernel patterns
        and that source package exists in our set, otherwise None.
        """
        # Check versioned pattern (linux-headers-6.6.0-1-amd64)
        m = self._KERNEL_VERSIONED_RE.match(bin_name)
        if m:
            src = 'linux-rt' if '-rt-' in bin_name or bin_name.startswith('linux-rt') else 'linux'
            if src in self.package_set.source_packages:
                return src

        # Check static meta-package pattern (linux-headers-stx-amd64, linux-keys)
        m = self._KERNEL_STATIC_RE.match(bin_name)
        if m:
            src = 'linux-rt' if m.group(1) else 'linux'
            if src in self.package_set.source_packages:
                return src

        # Check non-linux-prefixed binaries produced by the kernel
        if bin_name in self._KERNEL_EXTRA_BINARIES:
            if 'linux' in self.package_set.source_packages:
                return 'linux'

        return None

    def is_dep_satisfied(self, dep_name: str, apt_cache=None, dep_str: str = None) -> bool:
        """Check if a dependency is satisfied by the unified package set.

        Checks:
        1. Direct presence in binary_packages_unified (with version check if constraint given)
        2. Virtual package with a provider in the unified set
        3. Kernel-versioned package whose source we build
        4. Available in apt cache at a satisfying version

        Args:
            dep_name: Clean package name (no version constraint)
            apt_cache: Optional apt cache for fallback checks
            dep_str: Optional full dependency string with version constraint
                     (e.g., 'libpq5 (= 13.23)')
        """
        # Parse version constraint if dep_str provided
        relation, req_version = self._parse_version_constraint(dep_str) if dep_str else (None, None)

        # Check binary_packages_unified
        bin_packages = self.package_set.binary_packages_unified
        if not bin_packages:
            bin_packages = self.package_set.binary_packages_from_source
        if dep_name in bin_packages:
            if relation and req_version:
                pkg = bin_packages[dep_name]
                if pkg.version and not self._version_satisfies(pkg.version, relation, req_version):
                    return False
            return True

        # Check virtual package providers
        if apt_cache and dep_name in apt_cache and apt_cache.is_virtual_package(dep_name):
            providers = apt_cache.get_providing_packages(dep_name)
            if any(p.name in bin_packages for p in providers):
                return True

        # Check kernel-versioned packages (e.g. linux-headers-6.6.0-1-amd64)
        # These are produced at build time by source packages we compile
        m = self._KERNEL_VERSIONED_RE.match(dep_name)
        if m and ('linux' in self.package_set.source_packages or
                  'linux-rt' in self.package_set.source_packages):
            return True

        # Check apt cache — package available from upstream at satisfying version
        if apt_cache and dep_name in apt_cache:
            pkg = apt_cache[dep_name]
            if pkg.candidate:
                if relation and req_version:
                    return self._version_satisfies(pkg.candidate.version, relation, req_version)
                return True

        return False

    @staticmethod
    def _parse_version_constraint(dep_str):
        """Extract version relation and version from a dep string.

        Examples:
            'libpq5 (= 13.23)' -> ('=', '13.23')
            'libc6 (>= 2.31)' -> ('>=', '2.31')
            'libfoo' -> (None, None)
        """
        if not dep_str:
            return None, None
        match = re.search(r'\(\s*([><=!]+)\s*([^)]+)\)', dep_str)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return None, None

    @staticmethod
    def _version_satisfies(have_version, relation, req_version):
        """Check if have_version satisfies the relation against req_version."""
        try:
            cmp = apt_pkg.version_compare(have_version, req_version)
        except Exception:
            return True  # Can't compare — assume satisfied
        if relation == '=':
            return cmp == 0
        elif relation == '>=':
            return cmp >= 0
        elif relation == '>>':
            return cmp > 0
        elif relation == '<=':
            return cmp <= 0
        elif relation == '<<':
            return cmp < 0
        elif relation == '!=':
            return cmp != 0
        return True

    def get_unsatisfied_build_deps(self, build_types: List[str] = None,
                                   apt_cache=None,
                                   check_snapshot_repos: bool = False
                                   ) -> Dict[str, List[str]]:
        """Find build-depends not satisfiable by the unified package set.

        Args:
            build_types: List of build types for @KERNEL_TYPE@ expansion
            apt_cache: apt.Cache for virtual package and snapshot checks
            check_snapshot_repos: If True, deps available in apt_cache are considered satisfied

        Returns:
            Dict mapping unsatisfied dep name -> list of source package names needing it
        """
        if not build_types:
            build_types = ['std', 'rt']

        missing = {}
        skipped_templates = []

        for src_name, src_pkg in self.package_set.source_packages.items():
            all_build_deps = src_pkg.build_depends + src_pkg.build_depends_indep
            for dep_str in all_build_deps:
                dep_name = self._clean_dependency(dep_str)
                if not dep_name:
                    continue

                # Expand templates
                expanded = self.expand_template_dep(dep_name, build_types)
                if not expanded:
                    skipped_templates.append((dep_name, src_name))
                    continue

                for edep in expanded:
                    if self.is_dep_satisfied(edep, apt_cache):
                        continue
                    # Optionally check snapshot repos
                    if check_snapshot_repos and apt_cache:
                        if edep in apt_cache or apt_cache.is_virtual_package(edep):
                            continue
                    missing.setdefault(edep, []).append(src_name)

        if skipped_templates and self.logger:
            for dep, src in skipped_templates:
                self.logger.info(f"  Skipping unsubstituted template: {dep} (needed by {src})")

        return missing

    def get_transitive_install_deps(self, packages: Set[str],
                                    apt_cache=None) -> Dict[str, str]:
        """Get all transitive runtime deps of a set of packages not in the unified set.

        Args:
            packages: Set of package names to resolve
            apt_cache: apt.Cache for dependency lookups

        Returns:
            Dict mapping transitive dep name -> direct parent dep it came from
        """
        if not apt_cache:
            return {}

        all_needed = set(packages)
        to_resolve = set(packages)
        resolved = {}  # dep -> parent

        while to_resolve:
            new_deps = set()
            for dep_name in to_resolve:
                if dep_name not in apt_cache:
                    continue
                pkg = apt_cache[dep_name]
                if not pkg.candidate:
                    continue
                for dep_list in pkg.candidate.dependencies:
                    for base_dep in dep_list:
                        rd = base_dep.name
                        if rd in all_needed:
                            continue
                        if rd in self.package_set.binary_packages_unified:
                            continue
                        if apt_cache.is_virtual_package(rd):
                            continue
                        all_needed.add(rd)
                        new_deps.add(rd)
                        resolved[rd] = dep_name
            to_resolve = new_deps

        return resolved

    def get_unsatisfied_runtime_deps(self, apt_cache=None) -> Dict[str, Dict[str, List[str]]]:
        """Check that all runtime deps of binary packages in the unified set are satisfiable.

        Transitively resolves: if package A needs B which needs C, reports both B and C.

        Returns:
            Dict mapping binary_pkg_name -> {'missing': [dep_names], 'source': source_pkg_name}
            Only packages with unsatisfied deps are included.

            Also sets self._missing_dep_details: dict mapping dep_name -> {
                'required': version constraint string (e.g. '(= 1.2.3)') or None,
                'available': version available in unified/apt or None,
                'reason': short explanation string
            }
        """
        # First pass: find direct missing deps of packages we have
        all_missing = {}  # dep_name -> set of packages needing it
        self._missing_dep_details = {}  # dep_name -> version/reason info

        bin_packages = self.package_set.binary_packages_unified
        if not bin_packages:
            bin_packages = self.package_set.binary_packages_from_source

        for bin_name, bin_pkg in self.package_set.binary_packages_unified.items():
            for dep_str in bin_pkg.depends + bin_pkg.pre_depends:
                if '${' in dep_str:
                    continue
                alternatives = [d.strip() for d in dep_str.split('|')]
                satisfied = False
                for alt in alternatives:
                    dep_name = self._clean_dependency(alt)
                    if not dep_name:
                        satisfied = True
                        break
                    if self.is_dep_satisfied(dep_name, apt_cache, dep_str=alt):
                        satisfied = True
                        break
                if not satisfied:
                    dep_name = self._clean_dependency(alternatives[0])
                    if dep_name:
                        # Only report if the package exists in apt (real
                        # downloadable package).  Virtual packages and ABI
                        # markers (e.g. perlapi-5.32.0, qtbase-abi-5-15-2)
                        # are not downloadable — they're provided by real
                        # packages that should already be in our set.
                        if not apt_cache or dep_name not in apt_cache:
                            continue
                        all_missing.setdefault(dep_name, set()).add(bin_name)
                        # Record version details if not already captured
                        if dep_name not in self._missing_dep_details:
                            relation, req_ver = self._parse_version_constraint(alternatives[0])
                            avail_ver = None
                            reason = 'not in unified set or apt'
                            if dep_name in bin_packages:
                                avail_ver = bin_packages[dep_name].version
                                reason = 'version mismatch (unified)'
                            elif apt_cache[dep_name].candidate:
                                avail_ver = apt_cache[dep_name].candidate.version
                                reason = 'version mismatch (apt)'
                            else:
                                reason = 'no candidate version'
                            self._missing_dep_details[dep_name] = {
                                'required': f"({relation} {req_ver})" if relation else None,
                                'available': avail_ver,
                                'reason': reason,
                            }

        # Transitive pass: chase deps of missing packages through apt cache
        if apt_cache:
            to_resolve = set(all_missing.keys())
            resolved = set(all_missing.keys())
            while to_resolve:
                new_missing = set()
                for dep_name in to_resolve:
                    if dep_name not in apt_cache:
                        continue
                    pkg = apt_cache[dep_name]
                    if not pkg.candidate:
                        continue
                    for dep_list in pkg.candidate.dependencies:
                        for base_dep in dep_list:
                            rd = base_dep.name
                            if rd in resolved:
                                continue
                            # Build dep_str with version for version-aware check
                            rd_str = None
                            if base_dep.relation:
                                rd_str = f"{rd} ({base_dep.relation} {base_dep.version})"
                            if self.is_dep_satisfied(rd, apt_cache, dep_str=rd_str):
                                continue
                            # Skip virtual/non-downloadable packages
                            if rd not in apt_cache:
                                continue
                            resolved.add(rd)
                            new_missing.add(rd)
                            all_missing.setdefault(rd, set()).add(dep_name)
                            # Record version details for transitive dep
                            if rd not in self._missing_dep_details:
                                avail_ver = None
                                reason = 'not in unified set or apt'
                                if rd in bin_packages:
                                    avail_ver = bin_packages[rd].version
                                    reason = 'version mismatch (unified)'
                                elif apt_cache[rd].candidate:
                                    avail_ver = apt_cache[rd].candidate.version
                                    reason = 'version mismatch (apt)'
                                else:
                                    reason = 'no candidate version'
                                req_constraint = f"({base_dep.relation} {base_dep.version})" if base_dep.relation else None
                                self._missing_dep_details[rd] = {
                                    'required': req_constraint,
                                    'available': avail_ver,
                                    'reason': reason,
                                }
                to_resolve = new_missing

        # Convert to per-package format
        unsatisfied = {}
        for bin_name, bin_pkg in self.package_set.binary_packages_unified.items():
            missing = []
            for dep_str in bin_pkg.depends + bin_pkg.pre_depends:
                if '${' in dep_str:
                    continue
                alternatives = [d.strip() for d in dep_str.split('|')]
                satisfied = False
                for alt in alternatives:
                    dep_name = self._clean_dependency(alt)
                    if not dep_name:
                        satisfied = True
                        break
                    if self.is_dep_satisfied(dep_name, apt_cache, dep_str=alt):
                        satisfied = True
                        break
                if not satisfied:
                    dep_name = self._clean_dependency(alternatives[0])
                    if dep_name and dep_name in all_missing:
                        missing.append(dep_name)
            if missing:
                unsatisfied[bin_name] = {
                    'missing': missing,
                    'source': bin_pkg.source_package or 'unknown'
                }

        # Add transitive deps (not direct deps of unified packages, but deps of deps)
        for dep_name, needed_by in all_missing.items():
            # If this dep isn't already reported as missing from a unified package,
            # report it as needed by whatever missing package requires it
            already_reported = False
            for info in unsatisfied.values():
                if dep_name in info['missing']:
                    already_reported = True
                    break
            if not already_reported:
                # Attribute to first needing package
                parent = sorted(needed_by)[0]
                unsatisfied.setdefault(f"(transitive via {parent})", {
                    'missing': [], 'source': 'transitive'
                })['missing'].append(dep_name)

        return unsatisfied

    def get_runtime_dependencies(self, binary_packages: Set[str],
                                 max_depth: int = 100) -> Set[str]:
        """Get all transitive runtime dependencies of a set of binary packages.

        Args:
            binary_packages: Set of binary package names
            max_depth: Maximum recursion depth to prevent infinite loops

        Returns:
            Set of all binary packages (including originals) in dependency tree
        """
        all_deps = set(binary_packages)
        to_check = set(binary_packages)
        depth = 0

        while to_check and depth < max_depth:
            depth += 1
            new_deps = set()

            for pkg_name in to_check:
                # Get the binary package
                bin_pkg = self.package_set.binary_packages_unified.get(pkg_name)
                if not bin_pkg:
                    bin_pkg = self.package_set.binary_packages_from_source.get(pkg_name)
                if not bin_pkg:
                    continue

                # Add all runtime dependencies
                for dep_str in bin_pkg.depends + bin_pkg.pre_depends:
                    dep_name = self._clean_dependency(dep_str)
                    if dep_name and dep_name not in all_deps:
                        new_deps.add(dep_name)

            # Only check newly discovered packages in next iteration
            to_check = new_deps - all_deps
            all_deps.update(new_deps)

        return all_deps

    def analyze_source_dependencies(self, build_types: List[str] = None, dsc_overrides: dict = None):
        """Analyze dependencies between source packages.

        For each source package:
        1. Get its Build-Depends (binary packages)
        2. Trace runtime dependencies of those packages
        3. Map back to source packages

        Args:
            build_types: List of build types for @KERNEL_TYPE@ expansion.
                         Defaults to ['std', 'rt'].
            dsc_overrides: Optional {pkg_name: dsc_path} mapping to read
                          Build-Depends from specific .dsc files rather than
                          src_pkg.build_depends. Used to ensure build-type-specific
                          dependencies are resolved correctly (e.g., rt .dsc has
                          linux-rt-headers-stx-amd64, std .dsc has linux-headers-stx-amd64).
        """
        if not build_types:
            build_types = ['std', 'rt']

        for src_name, src_pkg in self.package_set.source_packages.items():
            # Get all binary packages this source build-depends on
            build_deps = set()

            # Use dsc_overrides to get correct build-type-specific Build-Depends
            pkg_build_depends = src_pkg.build_depends
            pkg_build_depends_indep = src_pkg.build_depends_indep
            if dsc_overrides and src_name in dsc_overrides:
                dsc_path = dsc_overrides[src_name]
                try:
                    from debian import deb822
                    with open(dsc_path, 'r') as f:
                        dsc = deb822.Dsc(f)
                    if 'Build-Depends' in dsc:
                        pkg_build_depends = [d.strip() for d in str(dsc['Build-Depends']).split(',')]
                    if 'Build-Depends-Indep' in dsc:
                        pkg_build_depends_indep = [d.strip() for d in str(dsc['Build-Depends-Indep']).split(',')]
                except Exception as e:
                    if self.logger:
                        self.logger.debug("Could not read dsc override for %s: %s", src_name, e)

            for dep_str in pkg_build_depends + pkg_build_depends_indep:
                dep_name = self._clean_dependency(dep_str)
                if dep_name:
                    # Expand @KERNEL_TYPE@ templates
                    expanded = self.expand_template_dep(dep_name, build_types)
                    build_deps.update(expanded)

            # Get all transitive runtime dependencies
            all_deps = self.get_runtime_dependencies(build_deps)

            # Map binary packages back to source packages
            source_deps = set()
            # Check build_deps directly first (catches deps not in binary package set,
            # like kernel packages with dynamic control files)
            for bin_name in build_deps:
                src = self.binary_to_source.get(bin_name)
                if not src:
                    src = self._infer_kernel_source(bin_name)
                if src and src != src_name:
                    source_deps.add(src)
            # Then check transitive runtime deps
            for bin_name in all_deps:
                src = self.binary_to_source.get(bin_name)
                if not src:
                    src = self._infer_kernel_source(bin_name)
                if src and src != src_name:
                    source_deps.add(src)

            # Filter out false kernel cross-variant dependencies.
            #
            # Both 'linux' and 'linux-rt' produce identically-named binaries
            # (e.g. linux-compiler-gcc-10-x86) that share a single slot in
            # binary_packages_from_source (last loaded wins).  This causes
            # transitive runtime dep tracing to map those shared binaries to
            # the wrong kernel source.  For example, an rt package that
            # build-depends on linux-rt-headers-stx-amd64 traces through
            # linux-compiler-gcc-10-x86 and incorrectly picks up 'linux' (std)
            # because the std variant was loaded last.
            #
            # Fix: determine which kernel variant the direct build-deps
            # actually reference.  If the build-deps explicitly reference rt
            # kernel packages (linux-rt-headers-stx-*, linux-rt-keys, etc.)
            # then remove 'linux' from source_deps — the package clearly only
            # needs linux-rt.  Vice versa for std.
            if 'linux' in source_deps and 'linux-rt' in source_deps:
                # Check direct build-deps for kernel variant indicators
                has_rt_kernel_dep = any(
                    'linux-rt-' in dep or dep == 'linux-rt-keys'
                    for dep in build_deps
                )
                has_std_kernel_dep = any(
                    dep.startswith('linux-') and 'linux-rt' not in dep
                    and self._infer_kernel_source(dep) == 'linux'
                    for dep in build_deps
                )
                if has_rt_kernel_dep and not has_std_kernel_dep:
                    source_deps.discard('linux')
                elif has_std_kernel_dep and not has_rt_kernel_dep:
                    source_deps.discard('linux-rt')
                elif build_types == ['rt']:
                    # Fallback: if analyzing for rt only and we can't determine
                    # from build_deps (e.g., dsc_override failed to load), assume
                    # rt packages don't need std kernel
                    source_deps.discard('linux')
                elif build_types == ['std']:
                    # Same for std — don't need rt kernel
                    source_deps.discard('linux-rt')

            self.source_depends_on[src_name] = source_deps

        # Build reverse mapping (who depends on me)
        self.source_depended_by = {src: set() for src in self.source_depends_on.keys()}
        for src, deps in self.source_depends_on.items():
            for dep in deps:
                if dep in self.source_depended_by:
                    self.source_depended_by[dep].add(src)

    def detect_circular_dependencies(self) -> List[List[str]]:
        """Detect all circular dependencies.

        Returns:
            List of cycles, where each cycle is a list of source package names
        """
        cycles = []
        visited = set()

        def dfs(node: str, path: List[str]) -> bool:
            """Depth-first search to find cycles."""
            if node in path:
                # Found a cycle
                cycle_start = path.index(node)
                cycle = path[cycle_start:] + [node]
                cycles.append(cycle)
                return True

            if node in visited:
                return False

            visited.add(node)
            path.append(node)

            # Check all dependencies
            for dep in self.source_depends_on.get(node, set()):
                dfs(dep, path.copy())

            return False

        # Check each source package
        for src in self.source_depends_on.keys():
            if src not in visited:
                dfs(src, [])

        return cycles

    def calculate_build_priorities(self, try_resolve_circular: bool = True) -> Dict[str, int]:
        """Calculate build priority for each source package.

        Priority is based on estimated compile time and dependency depth:
        - Packages start with priority = compile_complexity (or 10 if not set)
        - Each package that depends on you adds its priority to yours
        - Higher priority = should build sooner (longer aggregate compile time)

        This prioritizes packages that contribute to the longest aggregate
        compile time, optimizing the critical path through the build.

        Args:
            try_resolve_circular: If True, attempt to resolve circular dependencies
                                 using downloaded binaries before raising exception

        Raises:
            CircularDependency: If circular dependencies cannot be resolved
        """
        # Initialize priorities based on compile complexity
        for src in self.source_depends_on.keys():
            src_pkg = self.package_set.source_packages.get(src)
            if src_pkg:
                # Trigger lazy calculation if not already done
                if src_pkg.compile_complexity is None:
                    src_pkg.calculate_compile_complexity()
                if src_pkg.compile_complexity:
                    self.build_priority[src] = src_pkg.compile_complexity
                else:
                    self.build_priority[src] = 10
            else:
                self.build_priority[src] = 10

        # Make working copies
        depends_on = {k: v.copy() for k, v in self.source_depends_on.items()}
        depended_by = {k: v.copy() for k, v in self.source_depended_by.items()}

        # Iteratively remove packages with no dependents
        while depends_on:
            # Find packages that nothing depends on (leaf nodes)
            leaves = [src for src, deps_by in depended_by.items()
                     if not deps_by and src in depends_on]

            if not leaves:
                # No leaves found = circular dependency
                if try_resolve_circular:
                    try_resolve_circular = False  # Only attempt once
                    # Try to resolve using downloaded binaries
                    unresolved = self.resolve_circular_dependencies()
                    if unresolved:
                        # Still have unresolved cycles
                        cycle = unresolved[0]
                        raise CircularDependency(cycle)
                    else:
                        # Break cycle edges: remove resolved packages from
                        # other packages' dependency sets, but keep the
                        # resolved package itself in the graph so it gets
                        # a build priority.
                        for resolution in self.circular_resolutions:
                            resolved_pkg = resolution['resolved_by']
                            # Remove resolved_pkg from other packages' depends_on
                            # (they no longer need to wait for it)
                            for src in list(depends_on.keys()):
                                if src != resolved_pkg:
                                    depends_on[src].discard(resolved_pkg)
                            # Clear its depended_by (nothing waits on it now)
                            if resolved_pkg in depended_by:
                                depended_by[resolved_pkg] = set()
                        # Continue the loop
                        continue
                else:
                    # Don't try to resolve, just raise exception
                    remaining = list(depends_on.keys())
                    def find_cycle(node, path):
                        if node in path:
                            return path[path.index(node):] + [node]
                        for dep in depends_on.get(node, set()):
                            if dep in depends_on:
                                cycle = find_cycle(dep, path + [node])
                                if cycle:
                                    return cycle
                        return None

                    cycle = find_cycle(remaining[0], [])
                    raise CircularDependency(cycle if cycle else remaining)

            # Process each leaf
            for leaf in leaves:
                # Add this leaf's priority to all packages it depends on
                for dep in depends_on[leaf]:
                    if dep in self.build_priority:
                        self.build_priority[dep] += self.build_priority[leaf]
                    # Remove leaf from dep's depended_by set
                    if dep in depended_by:
                        depended_by[dep].discard(leaf)

                # Remove leaf from working sets
                depends_on.pop(leaf)
                depended_by.pop(leaf)

        # Apply compile_priority_boost as a final override for packages that need
        # to build first regardless of dependency position (e.g. linux kernel)
        max_priority = max(self.build_priority.values()) if self.build_priority else 0
        for src in self.build_priority:
            src_pkg = self.package_set.source_packages.get(src)
            if src_pkg and src_pkg.compile_priority_boost:
                self.build_priority[src] = max(self.build_priority[src],
                                               max_priority + src_pkg.compile_priority_boost)

        return self.build_priority

    def get_build_order(self) -> List[str]:
        """Get recommended build order (highest priority first).

        Returns:
            List of source package names in build order
        """
        if not self.build_priority:
            self.calculate_build_priorities()

        # Sort by priority (descending) - highest priority builds first
        return sorted(self.build_priority.keys(),
                     key=lambda x: self.build_priority[x], reverse=True)

    def get_dependency_chain(self, source_pkg: str, target_pkg: str) -> Optional[List[str]]:
        """Find dependency chain from source_pkg to target_pkg.

        Args:
            source_pkg: Starting source package
            target_pkg: Target source package

        Returns:
            List of source packages forming the chain, or None if no path exists
        """
        if source_pkg not in self.source_depends_on:
            return None

        visited = set()

        def dfs(current: str, path: List[str]) -> Optional[List[str]]:
            if current == target_pkg:
                return path + [current]

            if current in visited:
                return None

            visited.add(current)

            for dep in self.source_depends_on.get(current, set()):
                result = dfs(dep, path + [current])
                if result:
                    return result

            return None

        return dfs(source_pkg, [])

    def _can_use_downloaded_binary(self, source_pkg: str) -> Tuple[bool, str]:
        """Check if a downloaded binary can substitute for a source package.

        Returns:
            Tuple of (can_use, reason)
        """

        # Check if we have both upstream and source versions
        src_pkg_obj = self.package_set.source_packages.get(source_pkg)
        if not src_pkg_obj:
            return False, "No source package found"

        # Check if any binary from this source is available upstream
        binaries = self.source_produces.get(source_pkg, set())
        if not binaries:
            return False, "No binary packages produced"

        for bin_name in binaries:
            upstream_bin = self.package_set.binary_packages_upstream.get(bin_name)
            source_bin = self.package_set.binary_packages_from_source.get(bin_name)

            if not upstream_bin or not source_bin:
                continue

            # Check if it's a -dev package
            if bin_name.endswith('-dev'):
                return False, f"Cannot use downloaded -dev package: {bin_name}"

            # Check version: upstream must be lower than source
            cmp = apt_pkg.version_compare(upstream_bin.version, source_bin.version)
            if cmp >= 0:
                return False, f"Upstream version {upstream_bin.version} >= source version {source_bin.version}"

        return True, "Downloaded binary available with lower version"

    def resolve_circular_dependencies(self) -> List[List[str]]:
        """Attempt to resolve circular dependencies using downloaded binaries.

        Returns:
            List of unresolved cycles
        """
        cycles = self.detect_circular_dependencies()
        unresolved = []

        for cycle in cycles:
            # Try to break the cycle by finding a package we can use from downloads
            resolved = False
            for pkg in cycle:
                can_use, reason = self._can_use_downloaded_binary(pkg)
                if can_use:
                    # Remove this package from dependency calculations
                    resolution = {
                        'cycle': cycle,
                        'resolved_by': pkg,
                        'method': 'downloaded_binary'
                    }
                    self.circular_resolutions.append(resolution)

                    if self.logger:
                        self.logger.info(f"Circular dependency resolved: {' -> '.join(cycle)}")
                        self.logger.info(f"  Using downloaded binary for: {pkg}")

                    resolved = True
                    break

            if not resolved:
                unresolved.append(cycle)

        return unresolved

    def suggest_download_packages(self, cycles: List[List[str]]) -> List[Dict]:
        """Suggest packages to download to break circular dependencies.

        Prefers low-level packages (fewer dependents).

        Returns:
            List of suggestions with package name, reason, and priority
        """
        suggestions = []

        for cycle in cycles:
            # Score each package in the cycle
            candidates = []
            for pkg in cycle:
                # Count how many packages depend on this one
                dependent_count = len(self.source_depended_by.get(pkg, set()))

                # Get compile complexity (lower is better for downloads)
                src_pkg = self.package_set.source_packages.get(pkg)
                complexity = src_pkg.compile_complexity if src_pkg and src_pkg.compile_complexity else 10

                # Check if any binaries are -dev packages
                binaries = self.source_produces.get(pkg, set())
                has_dev = any(b.endswith('-dev') for b in binaries)

                # Lower score = better candidate
                # Prefer: fewer dependents, lower complexity, no -dev packages
                score = dependent_count * 100 + complexity + (1000 if has_dev else 0)

                candidates.append({
                    'package': pkg,
                    'score': score,
                    'dependent_count': dependent_count,
                    'complexity': complexity,
                    'has_dev': has_dev,
                    'binaries': list(binaries)
                })

            # Sort by score and pick the best
            candidates.sort(key=lambda x: x['score'])
            best = candidates[0]

            suggestions.append({
                'cycle': cycle,
                'suggested_package': best['package'],
                'reason': f"Low-level package ({best['dependent_count']} dependents, complexity={best['complexity']})",
                'binaries': best['binaries'],
                'has_dev_warning': best['has_dev']
            })

        return suggestions

    def write_circular_dependency_report(self, output_path: str):
        """Write a report suggesting packages to download to break circular dependencies."""
        unresolved = self.resolve_circular_dependencies()

        with open(output_path, 'w') as f:
            f.write("# Circular Dependency Resolution Report\n\n")

            if self.circular_resolutions:
                f.write("## Resolved Circular Dependencies\n\n")
                f.write(f"Successfully resolved {len(self.circular_resolutions)} circular dependencies using downloaded binaries:\n\n")
                for res in self.circular_resolutions:
                    f.write(f"- Cycle: {' -> '.join(res['cycle'])}\n")
                    f.write(f"  Resolved by: {res['resolved_by']} (downloaded binary)\n\n")

            if unresolved:
                f.write(f"## Unresolved Circular Dependencies ({len(unresolved)})\n\n")
                suggestions = self.suggest_download_packages(unresolved)

                for i, suggestion in enumerate(suggestions, 1):
                    f.write(f"### Cycle {i}\n")
                    f.write(f"Packages: {' -> '.join(suggestion['cycle'])}\n\n")
                    f.write(f"**Suggested package to download:** `{suggestion['suggested_package']}`\n\n")
                    f.write(f"**Reason:** {suggestion['reason']}\n\n")
                    f.write(f"**Binary packages:**\n")
                    for bin_pkg in suggestion['binaries']:
                        f.write(f"- {bin_pkg}\n")
                    if suggestion['has_dev_warning']:
                        f.write(f"\n⚠️ **Warning:** This package produces -dev packages. Using downloaded -dev packages is currently not supported.\n")
                    f.write("\n")

                f.write("\n## Recommendation\n\n")
                f.write("Add the suggested packages to your binary download list to break these circular dependencies.\n")
                f.write("Prefer packages without -dev binaries when possible.\n")
            else:
                f.write("## All Circular Dependencies Resolved\n\n")
                f.write("No additional packages need to be downloaded.\n")

    def print_dependency_report(self):
        """Print a human-readable dependency report."""
        print("=" * 80)
        print("DEBIAN PACKAGE DEPENDENCY ANALYSIS")
        print("=" * 80)

        print(f"\nTotal source packages: {len(self.source_depends_on)}")
        print(f"Total binary packages: {len(self.binary_to_source)}")

        # Check for circular dependencies
        print("\n" + "-" * 80)
        print("CIRCULAR DEPENDENCY CHECK")
        print("-" * 80)
        try:
            cycles = self.detect_circular_dependencies()
            if cycles:
                print(f"⚠️  Found {len(cycles)} circular dependencies:")
                for i, cycle in enumerate(cycles, 1):
                    print(f"\n  Cycle {i}: {' -> '.join(cycle)}")
            else:
                print("✓ No circular dependencies detected")
        except Exception as e:
            print(f"✗ Error checking for cycles: {e}")

        # Calculate priorities
        print("\n" + "-" * 80)
        print("BUILD PRIORITIES")
        print("-" * 80)
        try:
            self.calculate_build_priorities()

            # Show top 10 highest priority (build last)
            top_priority = sorted(self.build_priority.items(),
                                 key=lambda x: x[1], reverse=True)[:10]
            print("\nTop 10 packages to build last (highest priority):")
            for src, prio in top_priority:
                deps_count = len(self.source_depends_on.get(src, set()))
                print(f"  {src:40} priority={prio:6} depends_on={deps_count}")

            # Show top 10 lowest priority (build first)
            bottom_priority = sorted(self.build_priority.items(),
                                    key=lambda x: x[1])[:10]
            print("\nTop 10 packages to build first (lowest priority):")
            for src, prio in bottom_priority:
                deps_count = len(self.source_depends_on.get(src, set()))
                print(f"  {src:40} priority={prio:6} depends_on={deps_count}")

        except CircularDependency as e:
            print(f"✗ Cannot calculate priorities: {e}")

        print("\n" + "=" * 80)


def main():
    """Example usage - analyze dependencies in a StarlingX build."""

    codename = "bullseye"
    stx_build_home = os.environ.get('STX_BUILD_HOME')

    print(f"Loading package data from StarlingX {codename} build...")

    # Create unified package set with both upstream and source packages
    with IsolatedApt() as apt:
        # Add local repositories for bullseye
        apt.add_source(f"deb file://{stx_build_home}/aptly/public/deb-local-build {codename} main")
        apt.add_source(f"deb file://{stx_build_homestx_build_home}/aptly/public/deb-local-binary {codename} main")
        apt.update()

        # Create package set
        pkg_set = DebianPackageSet(apt_cache=apt)

        # Load upstream binary packages from apt
        print("Loading upstream binary packages from apt...")
        pkg_set.load_from_apt_cache()
        print(f"  Loaded {len(pkg_set.binary_packages_upstream)} upstream binary packages")

        # Load source packages from workspace
        print("Loading source packages from workspace...")
        failed = pkg_set.load_source_from_starlingx_workspace(workspace)
        print(f"  Loaded {len(pkg_set.source_packages)} source packages")
        print(f"  Binary packages from source: {len(pkg_set.binary_packages_from_source)}")
        if failed:
            print(f"  Failed to parse: {len(failed)}")

        # Create unified view
        print("Creating unified package set...")
        pkg_set.create_unified_packages()
        print(f"  Unified binary packages: {len(pkg_set.binary_packages_unified)}")

        # DEBUG: Check what's actually in the source packages
        if 'linux' in pkg_set.source_packages:
            linux_obj = pkg_set.source_packages['linux']
            print(f"\nDEBUG: linux object type: {type(linux_obj)}")
            print(f"DEBUG: linux.build_depends type: {type(linux_obj.build_depends)}")
            print(f"DEBUG: linux.build_depends value: {linux_obj.build_depends[:3] if linux_obj.build_depends else 'empty'}")

        # Verify linux and libbpf have build dependencies
        if 'linux' in pkg_set.source_packages:
            linux_deps = pkg_set.source_packages['linux'].build_depends
            print(f"\nlinux has {len(linux_deps)} build dependencies")
            if any('pahole' in d for d in linux_deps):
                print("  ✓ pahole found in linux Build-Depends")

        if 'libbpf' in pkg_set.source_packages:
            libbpf_deps = pkg_set.source_packages['libbpf'].build_depends
            print(f"libbpf has {len(libbpf_deps)} build dependencies")
            if any('zlib' in d for d in libbpf_deps):
                print("  ✓ zlib1g-dev found in libbpf Build-Depends")

        # Create analyzer
        print("\n" + "=" * 80)
        print("ANALYZING DEPENDENCIES")
        print("=" * 80)
        analyzer = DependencyAnalyzer(pkg_set)
        analyzer.analyze_source_dependencies()

        # Print report
        analyzer.print_dependency_report()

        # Show the specific chains
        print("\n" + "=" * 80)
        print("CHECKING linux <-> libbpf CIRCULAR DEPENDENCY")
        print("=" * 80)

        if 'linux' in analyzer.source_depends_on and 'libbpf' in analyzer.source_depends_on:
            # Show what linux depends on
            linux_deps = analyzer.source_depends_on.get('linux', set())
            print(f"\nlinux depends on {len(linux_deps)} source packages")
            if 'libbpf' in linux_deps:
                print("  ✓ libbpf is in linux dependencies")

                # Show the chain
                chain = analyzer.get_dependency_chain('linux', 'libbpf')
                if chain:
                    print(f"  Chain: {' -> '.join(chain)}")
            else:
                print("  ✗ libbpf NOT in linux dependencies")

            # Show what libbpf depends on
            libbpf_deps = analyzer.source_depends_on.get('libbpf', set())
            print(f"\nlibbpf depends on {len(libbpf_deps)} source packages")
            if 'linux' in libbpf_deps:
                print("  ✓ linux is in libbpf dependencies")

                # Show the chain
                chain_rev = analyzer.get_dependency_chain('libbpf', 'linux')
                if chain_rev:
                    print(f"  Chain: {' -> '.join(chain_rev)}")
            else:
                print("  ✗ linux NOT in libbpf dependencies")

            # Final verdict
            if 'libbpf' in linux_deps and 'linux' in libbpf_deps:
                print("\n⚠️  CIRCULAR DEPENDENCY CONFIRMED!")
                print("This is why circular_dep.conf has an entry for linux and libbpf.")
            elif 'libbpf' in linux_deps or 'linux' in libbpf_deps:
                print("\n✓ One-way dependency found (no circular dependency)")
            else:
                print("\n✓ No dependency relationship found")
        else:
            if 'linux' not in analyzer.source_depends_on:
                print("✗ linux not found in source packages")
            if 'libbpf' not in analyzer.source_depends_on:
                print("✗ libbpf not found in source packages")


if __name__ == '__main__':
    main()
