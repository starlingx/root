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

from debian_package import DebianBinaryPackage, DebianSourcePackage
import apt_pkg

apt_pkg.init_system()


class DebianPackageSet:
    def __init__(self, apt_cache=None):
        self._apt_cache = apt_cache
        self.binary_packages_upstream = {}
        self.binary_packages_from_source = {}
        self.binary_packages_unified = {}
        self.source_packages = {}
        self.eclipsed_packages_upstream = {}
        self.eclipsed_packages_from_source = {}
        self.inconsistent_packages = {}
        self.provides = {}

    @staticmethod
    def _clean_dep_name(dep):
        """Extract package name from dependency string (removes version constraints)."""
        return dep.split()[0].split('(')[0].split('|')[0].strip()

    @staticmethod
    def _parse_dependency(dep_str):
        """Parse dependency string into (package_name, operator, version) tuple.
        Returns (name, None, None) if no version constraint."""
        import re
        dep_str = dep_str.strip()

        # Handle alternatives - take first option
        if '|' in dep_str:
            dep_str = dep_str.split('|')[0].strip()

        # Match pattern: package-name (operator version)
        match = re.match(r'^([a-zA-Z0-9][a-zA-Z0-9+.-]*)\s*(?:\(([<>=]+)\s*([^\)]+)\))?', dep_str)
        if match:
            pkg_name = match.group(1)
            operator = match.group(2)
            version = match.group(3)
            return (pkg_name, operator, version)

        return (dep_str.split()[0], None, None)

    @staticmethod
    def _check_version_constraint(actual_version, operator, required_version):
        """Check if actual_version satisfies the constraint operator required_version."""
        if operator is None or required_version is None:
            return True

        cmp = apt_pkg.version_compare(actual_version, required_version)

        if operator == '>=':
            return cmp >= 0
        elif operator == '<=':
            return cmp <= 0
        elif operator == '>>':
            return cmp > 0
        elif operator == '<<':
            return cmp < 0
        elif operator == '=':
            return cmp == 0

        return True

    @staticmethod
    def _compare_versions(ver1, ver2):
        """Compare two Debian version strings. Returns 1 if ver1 > ver2, -1 if ver1 < ver2, 0 if equal."""
        return apt_pkg.version_compare(ver1, ver2)

    @staticmethod
    def _compare_packages(pkg1, pkg2):
        """Compare two packages for consistency. Returns dict of differences or None if consistent.
        pkg1 is upstream (built), pkg2 is from source."""
        diffs = {}

        # Check dependencies - ignore if source has unresolved variables
        def has_unresolved_vars(deps):
            """Check if dependency list contains unresolved Debian variables."""
            return any('${' in dep for dep in deps)

        if not has_unresolved_vars(pkg2.depends):
            if set(pkg1.depends) != set(pkg2.depends):
                diffs['depends'] = {'upstream': pkg1.depends, 'source': pkg2.depends}

        if not has_unresolved_vars(pkg2.pre_depends):
            if set(pkg1.pre_depends) != set(pkg2.pre_depends):
                diffs['pre_depends'] = {'upstream': pkg1.pre_depends, 'source': pkg2.pre_depends}

        if not has_unresolved_vars(pkg2.provides):
            # Filter out empty strings from provides lists
            provides1 = [p for p in pkg1.provides if p.strip()]
            provides2 = [p for p in pkg2.provides if p.strip()]
            if set(provides1) != set(provides2):
                diffs['provides'] = {'upstream': pkg1.provides, 'source': pkg2.provides}

        # Check architecture - ignore if source has wildcard or if upstream arch is in source arch list
        source_arch_wildcards = ['any', 'all', 'linux-any', 'kfreebsd-any', 'hurd-any']

        # Source architecture can be a space-separated list
        source_archs = pkg2.architecture.split() if pkg2.architecture else []

        # Check if it's a wildcard or if upstream is in the list
        is_wildcard = any(arch in source_arch_wildcards for arch in source_archs)
        upstream_in_list = pkg1.architecture in source_archs

        # Also check for any-<arch> patterns that match upstream
        any_pattern_match = any(arch == f'any-{pkg1.architecture}' for arch in source_archs)

        if not (is_wildcard or upstream_in_list or any_pattern_match):
            if pkg1.architecture != pkg2.architecture:
                diffs['architecture'] = {'upstream': pkg1.architecture, 'source': pkg2.architecture}

        return diffs if diffs else None

    def load_from_apt_cache(self, package_names=None):
        """Load packages from the isolated apt cache.

        Args:
            package_names: Optional set of package names to load. If None, loads all.
        """
        if not self._apt_cache:
            raise ValueError("No apt cache provided")

        if package_names:
            # Direct lookup is O(n) on the filter set, not O(all packages)
            pkgs = []
            for name in package_names:
                if name in self._apt_cache.cache:
                    pkgs.append(self._apt_cache.cache[name])
        else:
            pkgs = self._apt_cache.cache

        for pkg in pkgs:
            if pkg.candidate:
                bin_pkg = DebianBinaryPackage(apt_cache=self._apt_cache)
                bin_pkg._parse_from_apt_package(pkg)
                bin_pkg.origin = 'apt'
                self.binary_packages_upstream[pkg.name] = bin_pkg

                # Track provides
                if pkg.candidate.provides:
                    for provided in pkg.candidate.provides:
                        provided_name = provided if isinstance(provided, str) else provided.name
                        if provided_name not in self.provides:
                            self.provides[provided_name] = []
                        self.provides[provided_name].append(pkg.name)

                src_name = pkg.candidate.source_name
                if src_name not in self.source_packages:
                    src_pkg = DebianSourcePackage(apt_cache=self._apt_cache)
                    src_pkg.name = src_name
                    src_pkg.version = pkg.candidate.source_version
                    self.source_packages[src_name] = src_pkg

    def add_source_package_binaries(self, source_package):
        """Add binary packages from a source package to binary_packages_from_source."""
        for bin_pkg in source_package.binary_packages:
            self.binary_packages_from_source[bin_pkg.name] = bin_pkg

    def add_all_source_package_binaries(self):
        """Add binary packages from all source packages to binary_packages_from_source."""
        for src_pkg in self.source_packages.values():
            self.add_source_package_binaries(src_pkg)

    def load_source_from_starlingx_workspace(self, workspace_path, parallel=8):
        """Load source packages from StarlingX workspace by finding .dsc files."""
        from pathlib import Path
        import concurrent.futures

        workspace = Path(workspace_path)
        if not workspace.exists():
            raise ValueError(f"Workspace path does not exist: {workspace_path}")

        # Find all .dsc files (only one level deep - package_name/*.dsc).
        # Workspace may contain stale directories or multiple .dsc versions,
        # so we keep only the newest .dsc per package and then filter to
        # packages that match the current source tree (self.source_packages).
        all_dsc_files = list(workspace.glob("*/*.dsc"))

        # Group by parent directory (package name), keep newest per package
        newest_per_pkg = {}
        for dsc_file in all_dsc_files:
            pkg_dir_name = dsc_file.parent.name
            if pkg_dir_name not in newest_per_pkg or dsc_file.stat().st_mtime > newest_per_pkg[pkg_dir_name].stat().st_mtime:
                newest_per_pkg[pkg_dir_name] = dsc_file

        # If source_packages already loaded (from source tree), filter to only known packages
        if self.source_packages:
            dsc_files = [dsc for name, dsc in newest_per_pkg.items()
                         if name in self.source_packages]
        else:
            dsc_files = list(newest_per_pkg.values())

        failed_packages = []

        def _parse_dsc(dsc_file):
            """Parse a single .dsc file and estimate complexity from source size."""
            try:
                with open(dsc_file, 'r') as f:
                    first_lines = ''.join([f.readline() for _ in range(5)])
                    if 'Format:' not in first_lines and 'Source:' not in first_lines:
                        return None

                src_pkg = DebianSourcePackage()
                src_pkg.extract_and_read_from_dsc(str(dsc_file), skip_extract=True)
                src_pkg.status = 'repacked'

                # Estimate compile complexity from source directory size
                # TODO
                src_pkg.calculate_compile_complexity(source_paths=None)

                return src_pkg
            except Exception as e:
                return {'file': str(dsc_file), 'error': str(e)}

        # Parse .dsc files in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
            results = executor.map(_parse_dsc, dsc_files)

        for result in results:
            if result is None:
                continue
            elif isinstance(result, dict):
                failed_packages.append(result)
            else:
                src_pkg = result
                if src_pkg.name not in self.source_packages:
                    self.source_packages[src_pkg.name] = src_pkg
                else:
                    existing = self.source_packages[src_pkg.name]
                    if src_pkg.build_depends and not existing.build_depends:
                        self.source_packages[src_pkg.name] = src_pkg
                    elif self._compare_versions(src_pkg.version, existing.version) > 0:
                        self.source_packages[src_pkg.name] = src_pkg

                self.add_source_package_binaries(src_pkg)

        return failed_packages

    def load_source_from_starlingx_sources(self, source_root, os_id, os_codename, build_type='std', layers=None):
        """Load source packages from StarlingX source directories using discovery.py.

        Args:
            source_root: Root directory of StarlingX source tree
            os_id: OS identifier (e.g., 'debian')
            os_codename: OS codename (e.g., 'trixie')
            build_type: Build type (e.g., 'std', 'rt')
            layers: List of layer names to filter by (None = all layers)
        """
        from pathlib import Path
        import sys
        import os

        # Import discovery module
        sys.path.insert(0, str(Path(__file__).parent))

        # Change to source_root and set environment for discovery to work
        old_cwd = os.getcwd()
        old_pwd = os.environ.get('PWD')
        old_my_repo = os.environ.get('MY_REPO_ROOT_DIR')
        try:
            os.chdir(source_root)
            os.environ['PWD'] = source_root
            os.environ['MY_REPO_ROOT_DIR'] = source_root

            from discovery import package_dir_list, get_all_layers

            # Build package-to-layer mapping by querying each layer
            pkg_to_layer = {}
            all_layers = get_all_layers(distro=os_id, codename=os_codename)
            for layer in all_layers:
                if layers and layer not in layers:
                    continue
                pkgs = package_dir_list(distro=os_id, codename=os_codename, layer=layer, build_type=build_type)
                for pkg in pkgs:
                    pkg_to_layer[pkg] = layer

            # Get all package directories
            pkg_dirs = list(pkg_to_layer.keys())
        finally:
            os.chdir(old_cwd)
            if old_pwd:
                os.environ['PWD'] = old_pwd
            if old_my_repo:
                os.environ['MY_REPO_ROOT_DIR'] = old_my_repo
            elif 'MY_REPO_ROOT_DIR' in os.environ:
                del os.environ['MY_REPO_ROOT_DIR']

        failed_packages = []
        for pkg_dir in pkg_dirs:
            try:
                # Extract package name from directory path
                pkg_name = Path(pkg_dir).name

                # Get git relative path (everything after source_root)
                full_path = Path(pkg_dir).resolve()
                source_root_path = Path(source_root).resolve()

                if not str(full_path).startswith(str(source_root_path)):
                    continue

                git_relative = str(full_path.relative_to(source_root_path).parent)

                # Get layer from mapping
                layer = pkg_to_layer.get(pkg_dir)

                src_pkg = DebianSourcePackage()
                src_pkg.read_from_starlingx_source(
                    source_root,
                    git_relative,
                    pkg_name,
                    os_id,
                    os_codename
                )
                src_pkg.status = 'unbuilt'  # Source from source tree is unbuilt

                # Set StarlingX metadata
                src_pkg.set_layer(layer)
                src_pkg.set_build_type(build_type)
                src_pkg.set_package_dir(pkg_dir)

                # Add to source_packages
                if src_pkg.name not in self.source_packages:
                    self.source_packages[src_pkg.name] = src_pkg
                else:
                    existing = self.source_packages[src_pkg.name]
                    if self._compare_versions(src_pkg.version, existing.version) > 0:
                        self.source_packages[src_pkg.name] = src_pkg

                # Add binary packages to binary_packages_from_source
                self.add_source_package_binaries(src_pkg)
            except Exception as e:
                failed_packages.append({'dir': pkg_dir, 'error': str(e)})
                continue

        return failed_packages

    def create_unified_packages(self):
        """Create unified package set, choosing highest version on collision.
        Returns True if merge was successful, False if inconsistencies found."""
        self.binary_packages_unified = {}
        self.eclipsed_packages_upstream = {}
        self.eclipsed_packages_from_source = {}
        self.inconsistent_packages = {}
        has_inconsistencies = False

        # Start with upstream packages
        for name, pkg in self.binary_packages_upstream.items():
            self.binary_packages_unified[name] = pkg

        # Merge in source packages, handling collisions
        for name, src_pkg in self.binary_packages_from_source.items():
            if name in self.binary_packages_unified:
                upstream_pkg = self.binary_packages_unified[name]
                version_cmp = self._compare_versions(src_pkg.version, upstream_pkg.version)

                if version_cmp > 0:
                    # Source version is higher
                    self.binary_packages_unified[name] = src_pkg
                    self.eclipsed_packages_upstream[name] = upstream_pkg
                elif version_cmp < 0:
                    # Upstream version is higher
                    self.eclipsed_packages_from_source[name] = src_pkg
                else:
                    # Versions are equal - check consistency
                    diffs = self._compare_packages(upstream_pkg, src_pkg)
                    if diffs:
                        # Create detailed difference report
                        detailed_diffs = {}
                        for field, values in diffs.items():
                            # Filter out empty strings
                            upstream_list = values['upstream'] if isinstance(values['upstream'], list) else [values['upstream']]
                            source_list = values['source'] if isinstance(values['source'], list) else [values['source']]

                            upstream_list = [v for v in upstream_list if str(v).strip()]
                            source_list = [v for v in source_list if str(v).strip()]

                            upstream_set = set(upstream_list)
                            source_set = set(source_list)

                            detailed_diffs[field] = {
                                'upstream': upstream_list,
                                'source': source_list,
                                'only_in_upstream': list(upstream_set - source_set),
                                'only_in_source': list(source_set - upstream_set),
                                'in_both': list(upstream_set & source_set)
                            }

                        self.inconsistent_packages[name] = {
                            'upstream': upstream_pkg,
                            'source': src_pkg,
                            'differences': detailed_diffs
                        }
                        has_inconsistencies = True
                    # Always use upstream (built) version in unified when versions are equal
                    self.binary_packages_unified[name] = upstream_pkg
            else:
                self.binary_packages_unified[name] = src_pkg

        return not has_inconsistencies

    def get_inconsistency_report(self, package_name=None):
        """Get report on inconsistent packages. If package_name provided, return details for that package."""
        if package_name:
            return self.inconsistent_packages.get(package_name)
        return self.inconsistent_packages

    def get_binary_package(self, name, source='unified'):
        """Get binary package from specified source: 'unified', 'upstream', or 'source'."""
        if source == 'unified':
            return self.binary_packages_unified.get(name) or self.binary_packages_upstream.get(name)
        elif source == 'upstream':
            return self.binary_packages_upstream.get(name)
        elif source == 'source':
            return self.binary_packages_from_source.get(name)
        return None

    def _get_package_dict(self, source):
        """Get the appropriate package dictionary based on source."""
        if source == 'unified':
            return self.binary_packages_unified if self.binary_packages_unified else self.binary_packages_upstream
        elif source == 'upstream':
            return self.binary_packages_upstream
        elif source == 'source':
            return self.binary_packages_from_source
        return self.binary_packages_upstream

    def get_source_package(self, name):
        return self.source_packages.get(name)

    def filter_by_layer(self, layers):
        """Create a filtered set containing only packages from specified layers.
        Returns a new DebianPackageSet that shares the same package objects."""
        if not isinstance(layers, list):
            layers = [layers]

        filtered = DebianPackageSet(apt_cache=self._apt_cache)

        # Share the same package objects
        for name, src_pkg in self.source_packages.items():
            if src_pkg.get_layer() in layers:
                filtered.source_packages[name] = src_pkg

        # Filter binary packages
        for name, bin_pkg in self.binary_packages_upstream.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_upstream[name] = bin_pkg

        for name, bin_pkg in self.binary_packages_from_source.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_from_source[name] = bin_pkg

        # Share provides dict
        filtered.provides = self.provides

        return filtered

    def filter_by_build_type(self, build_types):
        """Create a filtered set containing only packages from specified build types.
        Returns a new DebianPackageSet that shares the same package objects."""
        if not isinstance(build_types, list):
            build_types = [build_types]

        filtered = DebianPackageSet(apt_cache=self._apt_cache)

        # Share the same package objects
        for name, src_pkg in self.source_packages.items():
            if src_pkg.get_build_type() in build_types:
                filtered.source_packages[name] = src_pkg

        # Filter binary packages
        for name, bin_pkg in self.binary_packages_upstream.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_upstream[name] = bin_pkg

        for name, bin_pkg in self.binary_packages_from_source.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_from_source[name] = bin_pkg

        # Share provides dict
        filtered.provides = self.provides

        return filtered

    def filter_by_names(self, package_names):
        """Create a filtered set containing only specified packages.
        Matches by source package name OR package directory name.
        Returns a new DebianPackageSet that shares the same package objects."""
        if not isinstance(package_names, list):
            package_names = [package_names]

        filtered = DebianPackageSet(apt_cache=self._apt_cache)

        # Share the same package objects
        for name in package_names:
            # Try exact source package name match first
            if name in self.source_packages:
                filtered.source_packages[name] = self.source_packages[name]
            else:
                # Try matching by package directory basename
                for src_name, src_pkg in self.source_packages.items():
                    pkg_dir = src_pkg.get_package_dir()
                    if pkg_dir:
                        import os
                        if os.path.basename(pkg_dir) == name:
                            filtered.source_packages[src_name] = src_pkg
                            break

        # Filter binary packages
        for name, bin_pkg in self.binary_packages_upstream.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_upstream[name] = bin_pkg

        for name, bin_pkg in self.binary_packages_from_source.items():
            if bin_pkg.source_package in filtered.source_packages:
                filtered.binary_packages_from_source[name] = bin_pkg

        # Share provides dict
        filtered.provides = self.provides

        return filtered

    def get_dependencies(self, package_name, recursive=False, source='upstream'):
        """Return list of dependency package names."""
        pkg = self.get_binary_package(package_name, source)
        if not pkg:
            return []

        all_deps = [self._clean_dep_name(d) for d in pkg.depends + pkg.pre_depends]

        if not recursive:
            return all_deps

        deps = set()
        visited = set()
        pkg_dict = self._get_package_dict(source)

        def collect(name):
            if name in visited or name not in pkg_dict:
                return
            visited.add(name)
            p = pkg_dict[name]
            for dep in p.depends + p.pre_depends:
                clean_name = self._clean_dep_name(dep)
                deps.add(clean_name)
                collect(clean_name)

        collect(package_name)
        return list(deps)

    def verify_dependencies(self, package_name, recursive=False, source='upstream'):
        """Return True if all dependencies are present in the package set and satisfy version constraints."""
        pkg = self.get_binary_package(package_name, source)
        if not pkg:
            return False

        pkg_dict = self._get_package_dict(source)
        all_deps = pkg.depends + pkg.pre_depends

        for dep_str in all_deps:
            pkg_name, operator, required_version = self._parse_dependency(dep_str)

            # Check if package exists
            if pkg_name not in pkg_dict and pkg_name not in self.provides:
                return False

            # Check version constraint if present
            if operator and required_version:
                if pkg_name in pkg_dict:
                    actual_version = pkg_dict[pkg_name].version
                    if not self._check_version_constraint(actual_version, operator, required_version):
                        return False
                elif pkg_name in self.provides:
                    # Check version of provider
                    provider_name = self.provides[pkg_name][0]
                    if provider_name in pkg_dict:
                        actual_version = pkg_dict[provider_name].version
                        if not self._check_version_constraint(actual_version, operator, required_version):
                            return False

            # Recursively check this dependency's dependencies
            if recursive:
                if not self.verify_dependencies(pkg_name, recursive=True, source=source):
                    return False

        return True

    def get_missing_dependencies(self, package_name, recursive=False, source='upstream'):
        """Return list of missing or unsatisfied dependency package names with reasons."""
        pkg = self.get_binary_package(package_name, source)
        if not pkg:
            return []

        pkg_dict = self._get_package_dict(source)
        missing = []
        visited = set()

        def check_deps(pkg_name, is_recursive=False):
            if pkg_name in visited:
                return
            visited.add(pkg_name)

            p = self.get_binary_package(pkg_name, source)
            if not p:
                return

            all_deps = p.depends + p.pre_depends

            for dep_str in all_deps:
                dep_pkg_name, operator, required_version = self._parse_dependency(dep_str)

                # Check if package exists
                if dep_pkg_name not in pkg_dict and dep_pkg_name not in self.provides:
                    missing.append(dep_str)
                    continue

                # Check version constraint if present
                if operator and required_version:
                    if dep_pkg_name in pkg_dict:
                        actual_version = pkg_dict[dep_pkg_name].version
                        if not self._check_version_constraint(actual_version, operator, required_version):
                            missing.append(f"{dep_str} (found {actual_version})")
                    elif dep_pkg_name in self.provides:
                        provider_name = self.provides[dep_pkg_name][0]
                        if provider_name in pkg_dict:
                            actual_version = pkg_dict[provider_name].version
                            if not self._check_version_constraint(actual_version, operator, required_version):
                                missing.append(f"{dep_str} (provider {provider_name} has {actual_version})")

                # Recurse if needed
                if is_recursive:
                    check_deps(dep_pkg_name, True)

        check_deps(package_name, recursive)
        return missing

    def get_resolved_dependencies(self, package_name, recursive=False, source='upstream'):
        """Return list of DebianBinaryPackage objects for resolved dependencies."""
        deps = self.get_dependencies(package_name, recursive, source)
        pkg_dict = self._get_package_dict(source)
        resolved = []
        for dep in deps:
            if dep in pkg_dict:
                resolved.append(pkg_dict[dep])
            elif dep in self.provides:
                # Check if any provider is already in resolved set (prefer installed)
                # Otherwise use the provider with highest priority from apt cache
                providers = self.provides[dep]
                best_provider = None

                if self._apt_cache:
                    # Use apt's resolution
                    for provider_name in providers:
                        pkg = self._apt_cache.show(provider_name)
                        if pkg and pkg.candidate:
                            if best_provider is None or (pkg.candidate.priority > best_provider[1].candidate.priority):
                                best_provider = (provider_name, pkg)
                    if best_provider and best_provider[0] in pkg_dict:
                        resolved.append(pkg_dict[best_provider[0]])
                else:
                    # Fallback: use first provider
                    if providers[0] in pkg_dict:
                        resolved.append(pkg_dict[providers[0]])
        return resolved

