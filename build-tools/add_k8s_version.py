#!/usr/bin/env python3
#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# add_k8s_version.py: Automates adding a new Kubernetes version to the
#                     project by generating the necessary changes across
#                     multiple repositories.
#
#                     The script performs the following:
#
#                     1) Creates the kubernetes debian package directory
#                        in integ, based on the reference version
#                     2) Ports patches from the reference version,
#                        validating each patch applies cleanly
#                     3) Registers the new version in package listing
#                        and iso image include files
#                     4) Adds or updates golang dependency entries in
#                        stx-tools base download lists if not present in
#                        the compile
#                     5) Creates ansible-playbooks version-specific
#                        entries for bootstrap, load-images, and
#                        snapshot-controller roles
#                     6) Adds k8s container images to WRCP prebuilt-images
#                        lists in titanium-tools (requires Docker)
#
#                     The latest existing K8s version is auto-detected as
#                     reference. The required Go version and source tarball
#                     SHA256 are fetched from GitHub.
#
# Prerequisites:
#
#   apt install python3-packaging
#   source <project-env-file>
#
# Usage:
#
#   add_k8s_version.py <k8s_version> [--reference-k8s <version>]
#
#     k8s_version:     Kubernetes version to add (e.g., 1.36.1 or v1.36.1)
#     --reference-k8s: (Optional) Existing K8s version to use as reference
#                      base. If not specified, the default version is used
#                      when adding a newer version, or the highest existing
#                      version below the new one when adding an older version.
#                      Use this when adding multiple versions sequentially to
#                      base the new version on a previously added one.
#
# Sample usage:
#   ./add_k8s_version.py 1.36.1
#   ./add_k8s_version.py 1.37.0 --reference-k8s 1.36.1
#

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import Version, InvalidVersion


def eprint(*args, **kwargs):
    """Print to stderr for consistent output with sys.exit."""
    print(*args, file=sys.stderr, **kwargs)


def deb_ver_is_older(ver_a, ver_b):
    """Return True if ver_a is older than ver_b using dpkg --compare-versions.

    This handles all Debian version string quirks including epochs,
    tilde sorting, and non-numeric parts (e.g., 1.22.12-3~bpo12+1).
    """
    result = subprocess.run(
        ["dpkg", "--compare-versions", ver_a, "lt", ver_b],
        capture_output=True
    )
    return result.returncode == 0


ANSIBLE_SYMLINK_DIRS = [
    "bootstrap/prepare-env/vars",
    "common/bringup-kubemaster/templates",
    "common/load-images-information/vars",
    "k8s-storage-backends/snapshot-controller/templates",
    "k8s-storage-backends/snapshot-controller/files",
]


def setup_environment():
    """Validate environment variables and set up global paths."""
    global PROJECT_ROOT, INTEG_DIR, COMPILE_DIR, STX_TOOLS_DIR, K8S_PKG_DIR
    global ANSIBLE_DIR, VARS_MAIN_YML, EXECUTED_BY
    global BASE_LST_TRIXIE, BASE_LST_BULLSEYE
    global TITANIUM_TOOLS_DIR, PREBUILT_IMAGES_TRIXIE, PREBUILT_IMAGES_BULLSEYE

    if "REPO_ROOT" not in os.environ:
        sys.exit("ERROR: REPO_ROOT environment variable is not set.\n"
                 "  export REPO_ROOT=\"/path/to/your/repo\"")

    PROJECT_ROOT = Path(os.environ["REPO_ROOT"])
    if not (PROJECT_ROOT / "cgcs-root").is_dir():
        sys.exit(f"ERROR: Invalid REPO_ROOT: {PROJECT_ROOT}\n"
                 f"  Directory 'cgcs-root' not found under REPO_ROOT.")
    INTEG_DIR = PROJECT_ROOT / "cgcs-root/stx/integ"
    COMPILE_DIR = PROJECT_ROOT / "cgcs-root/stx/compile"
    STX_TOOLS_DIR = PROJECT_ROOT / "stx-tools"
    K8S_PKG_DIR = INTEG_DIR / "kubernetes"
    ANSIBLE_DIR = PROJECT_ROOT / "cgcs-root/stx/ansible-playbooks/playbookconfig/src/playbooks/roles"

    VARS_MAIN_YML = ANSIBLE_DIR / "bootstrap/validate-config/vars/main.yml"

    if not os.environ.get("USER_NAME") or not os.environ.get("USER_EMAIL"):
        sys.exit("ERROR: Please set USER_NAME and USER_EMAIL environment variables.\n"
                 "  export USER_NAME=\"Your Full Name\"\n"
                 "  export USER_EMAIL=\"your.email@domain.com\"")
    EXECUTED_BY = f"{os.environ['USER_NAME']} {os.environ['USER_EMAIL']}"

    BASE_LST_TRIXIE = STX_TOOLS_DIR / "debian-mirror-tools/config/debian/trixie/common/base-trixie.lst"
    BASE_LST_BULLSEYE = STX_TOOLS_DIR / "debian-mirror-tools/config/debian/bullseye/common/base-bullseye.lst"

    TITANIUM_TOOLS_DIR = PROJECT_ROOT / "cgcs-root/wrs/titanium-tools"
    PREBUILT_IMAGES_TRIXIE = TITANIUM_TOOLS_DIR / "docker-images/WRCP-prebuilt-images-trixie.lst"
    PREBUILT_IMAGES_BULLSEYE = TITANIUM_TOOLS_DIR / "docker-images/WRCP-prebuilt-images-bullseye.lst"


def fetch_go_version(k8s_ver):
    """Fetch Go version from Kubernetes GitHub .go-version file."""
    url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{k8s_ver}/.go-version"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode().strip()
    except Exception as e:
        sys.exit(f"ERROR: Could not fetch .go-version for v{k8s_ver}: {e}")


def fetch_tar_sha256(k8s_ver):
    """Download K8s source tarball and compute SHA256."""
    url = f"https://github.com/kubernetes/kubernetes/archive/refs/tags/v{k8s_ver}.tar.gz"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as resp:
            sha256 = hashlib.sha256()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                sha256.update(chunk)
            return sha256.hexdigest()
    except Exception as e:
        eprint(f"  WARNING: Could not download tarball to compute SHA256: {e}")
        return "PLACEHOLDER_SHA256_UPDATE_ME"


def get_default_k8s_version():
    """Fetch the default (fresh install) k8s version from vars/main.yml."""
    if not VARS_MAIN_YML.exists():
        sys.exit(f"ERROR: Required file not found: {VARS_MAIN_YML}\n"
                 f"  This file is needed to determine the default Kubernetes version.")
    content = VARS_MAIN_YML.read_text()
    m = re.search(r"^fresh_install_k8s_version:\s*(\S+)", content, re.MULTILINE)
    if not m:
        sys.exit(f"ERROR: 'fresh_install_k8s_version' not found in {VARS_MAIN_YML}")
    return m.group(1)


def get_integ_base_srcrev():
    """Get the latest commit hash from the integ repo to use as BASE_SRCREV."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(INTEG_DIR),
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception as e:
        eprint(f"  WARNING: Could not get BASE_SRCREV from integ repo: {e}")
        return "PLACEHOLDER_UPDATE_ME"


def find_latest_k8s_version(new_ver):
    """Find the latest existing kubernetes-X.Y.Z directory that is less than new_ver."""
    new_ver_tuple = list(map(int, str(new_ver).split(".")))
    versions = []
    for d in K8S_PKG_DIR.iterdir():
        m = re.match(r"kubernetes-(\d+\.\d+\.\d+)$", d.name)
        if m and d.is_dir():
            v = m.group(1)
            v_tuple = list(map(int, v.split(".")))
            if v_tuple < new_ver_tuple:
                versions.append(v)
    if not versions:
        sys.exit(f"ERROR: No kubernetes-* packages found in {K8S_PKG_DIR} with version < {new_ver}")
    versions.sort(key=lambda v: list(map(int, v.split("."))))
    return versions[-1]


def go_major_minor(go_ver):
    """Extract major.minor from full go version (e.g., 1.25.7 -> 1.25)."""
    return ".".join(go_ver.split(".")[:2])


def insert_after_last_match(filepath, pattern, new_text):
    """Insert new_text after the last line matching pattern."""
    lines = filepath.read_text().splitlines(keepends=True)
    last_idx = None
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            last_idx = i
    if last_idx is not None:
        # Ensure new_text ends with newline
        if not new_text.endswith("\n"):
            new_text += "\n"
        lines.insert(last_idx + 1, new_text)
        filepath.write_text("".join(lines))


def create_k8s_package(new_ver, ref_ver, go_ver, ref_go_ver, sha256):
    """Create the kubernetes debian package from reference."""
    src = K8S_PKG_DIR / f"kubernetes-{ref_ver}"
    dst = K8S_PKG_DIR / f"kubernetes-{new_ver}"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('patches'))

    deb_dir = dst / "debian/all/deb_folder"
    golang_pkg = go_major_minor(go_ver)
    ref_golang_pkg = go_major_minor(ref_go_ver)
    timestamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    # meta_data.yaml
    base_srcrev = get_integ_base_srcrev()
    (dst / "debian/all/meta_data.yaml").write_text(
        f"---\n"
        f"debver: {new_ver}\n"
        f"dl_path:\n"
        f"  name: kubernetes-{new_ver}.tar.gz\n"
        f"  url: https://github.com/kubernetes/kubernetes/archive/refs/tags/v{new_ver}.tar.gz\n"
        f"  sha256sum: {sha256}\n"
        f"revision:\n"
        f"  dist: ${{STX_DIST}}\n"
        f"  GITREVCOUNT:\n"
        f"    BASE_SRCREV: {base_srcrev}\n"
        f"    SRC_DIR: ${{MY_REPO}}/stx/integ/kubernetes/kubernetes-{new_ver}\n"
    )

    # changelog
    ref_changelog = (src / "debian/all/deb_folder/changelog").read_text()
    (deb_dir / "changelog").write_text(
        f"kubernetes-{new_ver} ({new_ver}-1) unstable; urgency=medium\n\n"
        f"  * Updated to support building {new_ver}\n\n"
        f" -- {EXECUTED_BY}  {timestamp}\n\n"
        f"{ref_changelog}"
    )

    # control
    control = (deb_dir / "control").read_text()
    control = control.replace(f"kubernetes-{ref_ver}", f"kubernetes-{new_ver}")
    control = control.replace(f"golang-{ref_golang_pkg}", f"golang-{golang_pkg}")
    (deb_dir / "control").write_text(control)

    # rules
    rules = (deb_dir / "rules").read_text()
    rules = rules.replace(f"kube_version := {ref_ver}", f"kube_version := {new_ver}")
    rules = rules.replace(f"go_version := {ref_go_ver}", f"go_version := {go_ver}")
    rules = rules.replace(f"/usr/lib/go-{ref_golang_pkg}/bin", f"/usr/lib/go-{golang_pkg}/bin")
    (deb_dir / "rules").write_text(rules)

    # Rename and update versioned files
    for f in list(deb_dir.glob(f"kubernetes-{ref_ver}*")):
        new_name = f.name.replace(str(ref_ver), str(new_ver))
        new_path = f.parent / new_name
        f.rename(new_path)
        content = new_path.read_text()
        new_path.write_text(content.replace(str(ref_ver), str(new_ver)))

    eprint(f"  Created: {dst}")


def port_patches(new_ver, ref_ver):
    """Port patches from the reference version to the new version.

    Downloads the new k8s source tarball, extracts it, applies each patch
    from the reference version to verify it applies cleanly, then copies
    the validated patches to the new version's patches directory.
    """
    ref_patches_dir = K8S_PKG_DIR / f"kubernetes-{ref_ver}/debian/all/deb_folder/patches"
    dst_patches_dir = K8S_PKG_DIR / f"kubernetes-{new_ver}/debian/all/deb_folder/patches"

    if not ref_patches_dir.exists():
        eprint("  No patches in reference version, skipping")
        return

    series_file = ref_patches_dir / "series"
    if not series_file.exists():
        eprint("  No series file in reference version, skipping")
        return

    patch_names = [line.strip() for line in series_file.read_text().splitlines() if line.strip() and not line.startswith('#')]
    if not patch_names:
        eprint("  No patches listed in series file, skipping")
        return

    # Download and extract new k8s source
    tarball_url = f"https://github.com/kubernetes/kubernetes/archive/refs/tags/v{new_ver}.tar.gz"
    eprint(f"  Downloading k8s v{new_ver} source to verify patches...")

    tmpdir = tempfile.mkdtemp()
    try:
        tarball_path = Path(tmpdir) / f"kubernetes-{new_ver}.tar.gz"

        try:
            req = urllib.request.Request(tarball_url)
            with urllib.request.urlopen(req, timeout=120) as resp:
                tarball_path.write_bytes(resp.read())
        except Exception as e:
            eprint(f"  WARNING: Could not download k8s source tarball: {e}")
            eprint(f"  Copying patches from reference without verification")
            shutil.copytree(ref_patches_dir, dst_patches_dir)
            return

        # Extract tarball
        with tarfile.open(tarball_path, 'r:gz') as tar:
            try:
                tar.extractall(path=tmpdir, filter='data')
            except TypeError:
                tar.extractall(path=tmpdir)

        # The extracted directory is typically kubernetes-{new_ver}
        extracted_dir = Path(tmpdir) / f"kubernetes-{new_ver}"
        if not extracted_dir.exists():
            # Sometimes github tarballs have different directory names
            dirs = [d for d in Path(tmpdir).iterdir() if d.is_dir() and d.name.startswith("kubernetes")]
            if dirs:
                extracted_dir = dirs[0]
            else:
                eprint(f"  WARNING: Could not find extracted source directory")
                shutil.copytree(ref_patches_dir, dst_patches_dir)
                return

        # Try applying each patch
        dst_patches_dir.mkdir(parents=True, exist_ok=True)
        applied_patches = []
        failed_patches = []

        for patch_name in patch_names:
            patch_file = ref_patches_dir / patch_name
            if not patch_file.exists():
                eprint(f"  WARNING: Patch {patch_name} not found in reference, skipping")
                failed_patches.append((patch_name, "Patch file not found"))
                continue

            # Test if patch applies cleanly using patch --dry-run
            result = subprocess.run(
                ["patch", "-p1", "--dry-run", "-i", str(patch_file)],
                cwd=str(extracted_dir),
                capture_output=True, text=True
            )

            if result.returncode == 0:
                # Patch applies cleanly - copy it
                shutil.copy2(patch_file, dst_patches_dir / patch_name)
                applied_patches.append(patch_name)
                # Actually apply it so subsequent patches have correct context
                subprocess.run(
                    ["patch", "-p1", "-i", str(patch_file)],
                    cwd=str(extracted_dir),
                    capture_output=True
                )
            else:
                failed_patches.append((patch_name, result.stdout.strip() or result.stderr.strip()))

        # Write series file
        (dst_patches_dir / "series").write_text(
            "\n".join(applied_patches) + "\n"
        )

        # Print successful patches block
        if applied_patches:
            eprint(f"\n  Successfully applied patches ({len(applied_patches)}):")
            for patch_name in applied_patches:
                eprint(f"    [OK] {patch_name}")

        # Print conflict patches block
        if failed_patches:
            eprint(f"\n  Conflicting patches ({len(failed_patches)}) - require developer intervention:")
            for patch_name, error_detail in failed_patches:
                eprint(f"    [CONFLICT] {patch_name}")
                eprint(f"               {error_detail}")

        eprint(f"\n  Summary: {len(applied_patches)} included, {len(failed_patches)} skipped")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def register_in_listings(new_ver):
    """Add entries to all package listing and iso image files."""

    # integ/debian_pkg_dirs
    f = INTEG_DIR / "debian_pkg_dirs"
    if f"kubernetes/kubernetes-{new_ver}" not in f.read_text():
        insert_after_last_match(f, r"^kubernetes/kubernetes-\d", f"kubernetes/kubernetes-{new_ver}\n")
        eprint(f"  Updated: integ/debian_pkg_dirs")

    # integ/debian_trixie_pkg_dirs_std
    f = INTEG_DIR / "debian_trixie_pkg_dirs_std"
    if f"kubernetes/kubernetes-{new_ver}" not in f.read_text():
        insert_after_last_match(f, r"^kubernetes/kubernetes-\d", f"kubernetes/kubernetes-{new_ver}\n")
        eprint(f"  Updated: integ/debian_trixie_pkg_dirs_std")

    # integ iso_image files
    iso_entry = (
        f"#kubernetes-{new_ver}\n"
        f"kubernetes-{new_ver}-client\n"
        f"kubernetes-{new_ver}-kubeadm\n"
        f"kubernetes-{new_ver}-node\n"
    )
    for name in ("debian_iso_image.inc", "debian_trixie_iso_image_std.inc"):
        f = INTEG_DIR / name
        if f"kubernetes-{new_ver}-client" not in f.read_text():
            insert_after_last_match(f, r"^kubernetes-.*-node$", iso_entry)
            eprint(f"  Updated: integ/{name}")


def add_golang_to_base_lists(go_ver, ref_go_ver, new_k8s_ver):
    """Handle golang entries in stx-tools base download lists.

    - Same minor as reference: update existing entries to new version.
    - Different minor: add new entries and alert user to verify.
    """
    golang_pkg = go_major_minor(go_ver)
    ref_golang_pkg = go_major_minor(ref_go_ver)

    # Check if golang is provided via the compile repo (older approach)
    compile_golang = check_compile_repo(golang_pkg)

    # Find all K8s versions using this golang minor
    k8s_using_this_golang = find_k8s_versions_using_golang(golang_pkg, new_k8s_ver)

    # If already in compile repo, no base-list changes needed
    if compile_golang:
        eprint(f"  golang-{golang_pkg} provided via compile repo: {compile_golang.relative_to(PROJECT_ROOT)}")
        eprint(f"  K8s versions using golang-{golang_pkg}: {', '.join(k8s_using_this_golang)}")
        return False

    if golang_pkg == ref_golang_pkg:
        # Same minor - replace with newer patch if available
        updated = update_golang_entries(golang_pkg, go_ver, ref_go_ver)
        if updated:
            other_k8s = [v for v in k8s_using_this_golang if v != str(new_k8s_ver)]
            if other_k8s:
                eprint("")
                eprint(f"  *** NOTE: Replaced golang-{golang_pkg} to support kubernetes-{new_k8s_ver}. ***")
                eprint(f"  *** This golang is also used by: {', '.join(other_k8s)} ***")
                eprint(f"  *** Verify these K8s versions by building the packages and deploying. ***")
        elif k8s_using_this_golang:
            eprint(f"  K8s versions using golang-{golang_pkg}: {', '.join(k8s_using_this_golang)}")
    else:
        # Different minor - add new entries if not already present
        added, in_compile = add_new_golang_entries(golang_pkg, go_ver)
        if added:
            eprint("")
            eprint(f"  *** ALERT: New golang minor version golang-{golang_pkg} added! ***")
            eprint(f"  *** This golang will be used by K8s versions: {', '.join(k8s_using_this_golang)} ***")
            eprint(f"  *** Please verify all packages build correctly with golang-{golang_pkg}. ***")
            if in_compile:
                return True  # signal that golang was added to compile
        else:
            # golang already present - check if patch version needs updating
            updated = update_golang_entries(golang_pkg, go_ver, "")
            if updated:
                other_k8s = [v for v in k8s_using_this_golang if v != str(new_k8s_ver)]
                if other_k8s:
                    eprint("")
                    eprint(f"  *** NOTE: Updated golang-{golang_pkg} to support kubernetes-{new_k8s_ver}. ***")
                    eprint(f"  *** This golang is also used by: {', '.join(other_k8s)} ***")
                    eprint(f"  *** Verify these K8s versions by building the packages and deploying. ***")
                else:
                    eprint(f"  Updated golang-{golang_pkg} patch version for kubernetes-{new_k8s_ver}")
            else:
                eprint(f"  golang-{golang_pkg} already present (used by: {', '.join(k8s_using_this_golang)})")
    return False

def check_compile_repo(golang_pkg):
    """Check if golang is provided via the compile repo. Returns Path or None."""
    langs_dir = COMPILE_DIR / "languages"
    if not langs_dir.exists():
        return None
    for d in langs_dir.iterdir():
        if d.is_dir() and d.name.startswith(f"golang-{golang_pkg}"):
            return d
    return None


def find_k8s_versions_using_golang(golang_pkg, new_k8s_ver):
    """Find all K8s versions (including the new one) that Build-Depend on this golang package."""
    versions = [str(new_k8s_ver)]
    for d in sorted(K8S_PKG_DIR.iterdir()):
        m = re.match(r"kubernetes-(\d+\.\d+\.\d+)$", d.name)
        if not m or m.group(1) == str(new_k8s_ver):
            continue
        control = d / "debian/all/deb_folder/control"
        if control.exists() and f"golang-{golang_pkg}" in control.read_text():
            versions.append(m.group(1))
    versions.sort(key=lambda v: list(map(int, v.split("."))))
    return versions


def update_golang_entries(golang_pkg, go_ver, ref_go_ver):
    """Update existing golang entries when patch version changes (same minor)."""
    if go_ver == ref_go_ver:
        for lst_file in (BASE_LST_TRIXIE, BASE_LST_BULLSEYE):
            if lst_file.exists() and f"golang-{golang_pkg} " in lst_file.read_text():
                eprint(f"  golang-{golang_pkg} in {lst_file.name} (same version, no change needed)")
        return False

    # Different patch version - update the entries in base lists
    updated = False
    for lst_file in (BASE_LST_TRIXIE, BASE_LST_BULLSEYE):
        if not lst_file.exists():
            continue
        content = lst_file.read_text()
        if f"golang-{golang_pkg} " not in content:
            continue

        # Extract current debian package version from the list
        m = re.search(rf"^golang-{re.escape(golang_pkg)}-go\s+(\S+)", content, re.MULTILINE)
        if not m:
            continue
        old_deb_ver = m.group(1)

        # Check if the current version already satisfies the required Go version
        # e.g., if list has 1.25.9-1 and we need 1.25.9, no update needed
        if go_ver in old_deb_ver:
            eprint(f"  golang-{golang_pkg} in {lst_file.name} already at {old_deb_ver} (satisfies Go {go_ver}), no update needed")
            continue

        # Lookup the new version from snapshot
        snapshot_base, new_deb_ver = lookup_golang_from_snapshot(golang_pkg, go_ver)
        if not snapshot_base:
            eprint(f"  golang-{golang_pkg} in {lst_file.name}:"
                  f" could not verify from snapshot.debian.org,"
                  f" keeping current ({old_deb_ver})")
            continue
        if new_deb_ver == old_deb_ver:
            eprint(f"  golang-{golang_pkg} in {lst_file.name} already at latest ({old_deb_ver})")
            continue

        # Do not downgrade
        if deb_ver_is_older(new_deb_ver, old_deb_ver):
            eprint(f"  golang-{golang_pkg} in {lst_file.name}: snapshot version ({new_deb_ver}) is older than current ({old_deb_ver}), skipping")
            continue

        # Replace old entries with new ones
        lines = content.splitlines(keepends=True)
        new_lines = []
        for line in lines:
            if re.match(rf"golang-{re.escape(golang_pkg)}(\s|-)", line):
                pkg_suffix = re.match(rf"(golang-{re.escape(golang_pkg)}(?:-\w+)?)\s", line)
                if pkg_suffix:
                    pkg_name = pkg_suffix.group(1)
                    arch = "amd64" if pkg_name.endswith("-go") else "all"
                    url_ver = urllib.parse.quote(new_deb_ver, safe='~-')
                    new_lines.append(
                        f"{pkg_name}  {new_deb_ver}  {snapshot_base}/{pkg_name}_{url_ver}_{arch}.deb\n"
                    )
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        lst_file.write_text("".join(new_lines))
        eprint(f"  Replaced golang-{golang_pkg} ({old_deb_ver} -> {new_deb_ver}) in {lst_file.name}")
        updated = True
    return updated


def add_new_golang_entries(golang_pkg, go_ver):
    """Add new golang entries when a new minor version is required. Returns (added, in_compile)."""
    added = False

    # First check if it's available on snapshot.debian.org
    snapshot_base, deb_ver = lookup_golang_from_snapshot(golang_pkg)

    if snapshot_base:
        # Add to base-*.lst files
        for lst_file in (BASE_LST_TRIXIE, BASE_LST_BULLSEYE):
            if not lst_file.exists():
                continue
            content = lst_file.read_text()
            if f"golang-{golang_pkg} " in content:
                continue

            url_ver = urllib.parse.quote(deb_ver, safe='~-')
            entries = (
                f"golang-{golang_pkg}  {deb_ver}  {snapshot_base}/golang-{golang_pkg}_{url_ver}_all.deb\n"
                f"golang-{golang_pkg}-doc  {deb_ver}  {snapshot_base}/golang-{golang_pkg}-doc_{url_ver}_all.deb\n"
                f"golang-{golang_pkg}-go  {deb_ver}  {snapshot_base}/golang-{golang_pkg}-go_{url_ver}_amd64.deb\n"
                f"golang-{golang_pkg}-src  {deb_ver}  {snapshot_base}/golang-{golang_pkg}-src_{url_ver}_all.deb\n"
            )

            insert_after_last_match(lst_file, r"^golang-\d+\.\d+-src", entries)
            eprint(f"  Added golang-{golang_pkg} to {lst_file.relative_to(PROJECT_ROOT)}")
            added = True
        return added, False

    # Not available in Debian - add to compile repo as fallback
    added = add_golang_to_compile_repo(golang_pkg, go_ver)
    return added, added


def add_golang_to_compile_repo(golang_pkg, go_ver):
    """Add golang package to the compile repo when not available in Debian mirrors."""
    golang_dir = COMPILE_DIR / f"languages/golang-{go_ver}"

    if golang_dir.exists():
        eprint(f"  golang-{go_ver} already exists in compile repo")
        return False

    # Try to find the source package archive URL from snapshot
    archive_url = lookup_golang_source_archive(golang_pkg)

    if not archive_url:
        eprint(f"  ERROR: Could not find golang-{golang_pkg} on snapshot.debian.org")
        eprint(f"         Neither binary nor source packages available.")
        eprint(f"         Manually add golang-{golang_pkg} to base-*.lst or compile repo.")
        return False

    # Create compile repo entry for trixie
    trixie_dir = golang_dir / "debian/trixie"
    trixie_dir.mkdir(parents=True)
    (trixie_dir / "meta_data.yaml").write_text(
        f"debver: {go_ver}-1\n"
        f"debname: golang-{golang_pkg}\n"
        f"archive: {archive_url}\n"
        f"revision:\n"
        f"  dist: $STX_DIST\n"
        f"  PKG_GITREVCOUNT:\n"
    )

    # Create compile repo entry for bullseye
    bullseye_dir = golang_dir / "debian/bullseye"
    bullseye_dir.mkdir(parents=True)
    (bullseye_dir / "meta_data.yaml").write_text(
        f"debver: {go_ver}-1\n"
        f"debname: golang-{golang_pkg}\n"
        f"archive: {archive_url}\n"
        f"revision:\n"
        f"  dist: $STX_DIST\n"
        f"  PKG_GITREVCOUNT:\n"
    )

    # Register in compile debian_pkg_dirs
    pkg_dirs = COMPILE_DIR / "debian_pkg_dirs"
    if pkg_dirs.exists():
        content = pkg_dirs.read_text()
        if f"languages/golang-{go_ver}" not in content:
            insert_after_last_match(pkg_dirs, r"^languages/golang-", f"languages/golang-{go_ver}\n")

    # Register in compile debian_trixie_pkg_dirs_std
    trixie_pkg_dirs = COMPILE_DIR / "debian_trixie_pkg_dirs_std"
    if trixie_pkg_dirs.exists():
        content = trixie_pkg_dirs.read_text()
        if f"languages/golang-{go_ver}" not in content:
            insert_after_last_match(trixie_pkg_dirs, r"^languages/golang-", f"languages/golang-{go_ver}\n")

    eprint(f"  Added golang-{go_ver} to compile repo: {golang_dir.relative_to(PROJECT_ROOT)}")
    eprint("")
    eprint(f"  *** WARNING: golang-{golang_pkg} not available in Debian mirrors. ***")
    eprint(f"  *** Added to compile repo instead. Please verify: ***")
    eprint(f"  ***   1. Build the golang-{golang_pkg} package first ***")
    eprint(f"  ***   2. Then build kubernetes with this golang ***")
    eprint(f"  ***   3. Deploy and verify the system works ***")
    return True


def lookup_golang_source_archive(golang_pkg):
    """Look up golang source package archive URL from snapshot.debian.org."""
    url = f"https://snapshot.debian.org/mr/package/golang-{golang_pkg}/"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            versions = data.get("result", [])
            if not versions:
                return None
            # API returns versions in reverse chronological order (newest first)
            latest = versions[0]["version"]
            files_url = f"https://snapshot.debian.org/mr/package/golang-{golang_pkg}/{latest}/srcfiles?fileinfo=1"
            with urllib.request.urlopen(files_url, timeout=10) as fr:
                fdata = json.loads(fr.read())
                for _, infos in fdata.get("fileinfo", {}).items():
                    for info in infos:
                        if info.get("archive_name") == "debian":
                            first_seen = info["first_seen"]
                            return f"https://snapshot.debian.org/archive/debian/{first_seen}/pool/main/g/golang-{golang_pkg}/"
    except Exception:
        pass
    return None


def lookup_golang_from_snapshot(golang_pkg, go_ver=None):
    """Look up golang package on snapshot.debian.org. Returns (base_url, deb_version) or (None, None).
    If go_ver is specified, finds the package version matching that Go version.
    """
    url = f"https://snapshot.debian.org/mr/package/golang-{golang_pkg}/"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            versions = data.get("result", [])
            if not versions:
                return None, None

            # If a specific Go version is required, find the matching debian package
            if go_ver:
                target = None
                for v in versions:
                    if v["version"].startswith(go_ver):
                        target = v["version"]
                        break
                if not target:
                    return None, None
            else:
                # Pick the newest (first in list, API returns reverse chronological)
                target = versions[0]["version"]

            # Get file info to find the snapshot timestamp
            files_url = f"https://snapshot.debian.org/mr/package/golang-{golang_pkg}/{target}/srcfiles?fileinfo=1"
            with urllib.request.urlopen(files_url, timeout=10) as fr:
                fdata = json.loads(fr.read())
                for _, infos in fdata.get("fileinfo", {}).items():
                    for info in infos:
                        if info.get("archive_name") == "debian":
                            first_seen = info["first_seen"]
                            base = f"https://snapshot.debian.org/archive/debian/{first_seen}/pool/main/g/golang-{golang_pkg}"
                            return base, target
    except Exception:
        pass
    return None, None


def fetch_snapshot_controller_version(k8s_ver):
    """Fetch the snapshot-controller image version from K8s GitHub repo."""
    url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{k8s_ver}/cluster/addons/volumesnapshots/volume-snapshot-controller/volume-snapshot-controller-deployment.yaml"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            m = re.search(r"snapshot-controller:(v[\d.]+)", resp.read().decode())
            if m:
                return m.group(1)
    except Exception as e:
        eprint(f"  WARNING: Could not fetch snapshot-controller version for v{k8s_ver}: {e}")
    return None


def get_ref_snapshot_controller_version(ref_ver):
    """Get the snapshot-controller version from the reference version's system-images.yml."""
    vars_dir = ANSIBLE_DIR / "common/load-images-information/vars"
    ref_path = vars_dir / f"k8s-v{ref_ver}"
    if ref_path.is_symlink():
        ref_path = vars_dir / os.readlink(ref_path)
    images_file = ref_path / "system-images.yml"
    if images_file.exists():
        m = re.search(r"snapshot-controller:(v[\d.]+)", images_file.read_text())
        if m:
            return m.group(1)
    return None


def check_crds_changed(new_ver, ref_ver):
    """Check if upstream CRD content differs from reference version's CRDs."""
    files_dir = ANSIBLE_DIR / "k8s-storage-backends/snapshot-controller/files"
    ref_path = files_dir / f"k8s-v{ref_ver}"
    # Resolve symlink chain to find the actual CRD directory
    while ref_path.is_symlink():
        ref_path = files_dir / os.readlink(ref_path)
    ref_crd_dir = ref_path / "crd"
    if not ref_crd_dir.exists():
        return True  # No existing CRDs, must create them

    # Compare one CRD file from upstream with local reference
    crd_file = "snapshot.storage.k8s.io_volumesnapshotclasses.yaml"
    url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{new_ver}/cluster/addons/volumesnapshots/crd/{crd_file}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            upstream_content = resp.read().decode()
        local_content = (ref_crd_dir / crd_file).read_text()
        return upstream_content.strip() != local_content.strip()
    except Exception:
        return True  # Assume changed if we can't verify


def download_snapshot_crds(files_dir, new_ver):
    """Download CRD files from K8s GitHub for the new version."""
    crd_files = [
        "snapshot.storage.k8s.io_volumesnapshotclasses.yaml",
        "snapshot.storage.k8s.io_volumesnapshotcontents.yaml",
        "snapshot.storage.k8s.io_volumesnapshots.yaml",
    ]
    crd_dir = files_dir / f"k8s-v{new_ver}" / "crd"
    crd_dir.mkdir(parents=True)
    base_url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{new_ver}/cluster/addons/volumesnapshots/crd"
    for crd_file in crd_files:
        url = f"{base_url}/{crd_file}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                (crd_dir / crd_file).write_text(resp.read().decode())
        except Exception as e:
            eprint(f"  WARNING: Could not download {crd_file}: {e}")
    eprint(f"  Created: snapshot-controller/files/k8s-v{new_ver}/crd/ (downloaded from upstream)")


def download_snapshot_templates(templates_dir, new_ver):
    """Download and customize snapshot-controller templates from K8s GitHub."""
    tmpl_dir = templates_dir / f"k8s-v{new_ver}" / "volume-snapshot-controller"
    tmpl_dir.mkdir(parents=True)
    base_url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{new_ver}/cluster/addons/volumesnapshots/volume-snapshot-controller"

    # Download and customize rbac file - add imagePullSecrets to ServiceAccount
    try:
        url = f"{base_url}/rbac-volume-snapshot-controller.yaml"
        with urllib.request.urlopen(url, timeout=30) as resp:
            rbac_content = resp.read().decode()
        # Add imagePullSecrets after ServiceAccount metadata
        rbac_content = rbac_content.replace(
            "  name: volume-snapshot-controller\n  namespace: kube-system\n  labels:\n    kubernetes.io/cluster-service: \"true\"\n    addonmanager.kubernetes.io/mode: Reconcile\n---",
            "  name: volume-snapshot-controller\n  namespace: kube-system\n  labels:\n    kubernetes.io/cluster-service: \"true\"\n    addonmanager.kubernetes.io/mode: Reconcile\nimagePullSecrets:\n  - name: registry-local-secret\n---"
        )
        (tmpl_dir / "rbac-snapshot-controller.yaml.j2").write_text(rbac_content)
    except Exception as e:
        eprint(f"  WARNING: Could not download rbac file: {e}")

    # Download and customize deployment file - use Jinja2 image variable, add tolerations
    try:
        url = f"{base_url}/volume-snapshot-controller-deployment.yaml"
        with urllib.request.urlopen(url, timeout=30) as resp:
            deploy_content = resp.read().decode()
        # Replace the image reference with Jinja2 template variable
        deploy_content = re.sub(
            r"image: registry\.k8s\.io/sig-storage/snapshot-controller:v[\d.]+",
            'image: "{{ local_registry }}/{{ snapshot_controller_img }}"',
            deploy_content
        )
        # Add tolerations if not present
        if "tolerations:" not in deploy_content:
            deploy_content = deploy_content.replace(
                "      serviceAccount: volume-snapshot-controller",
                "      tolerations:\n"
                "      - key: \"node-role.kubernetes.io/master\"\n"
                "        operator: \"Exists\"\n"
                "        effect: \"NoSchedule\"\n"
                "      - key: \"node-role.kubernetes.io/control-plane\"\n"
                "        operator: \"Exists\"\n"
                "        effect: \"NoSchedule\"\n"
                "      serviceAccount: volume-snapshot-controller"
            )
        (tmpl_dir / "volume-snapshot-controller-deployment.yaml.j2").write_text(deploy_content)
    except Exception as e:
        eprint(f"  WARNING: Could not download deployment file: {e}")

    eprint(f"  Created: snapshot-controller/templates/k8s-v{new_ver}/ (downloaded and customized from upstream)")


def create_ansible_changes(new_ver, ref_ver):
    """Create ansible-playbooks entries for the new k8s version.

    Fetches snapshot-controller version from K8s GitHub.
    If changed: creates new system-images.yml with updated version.
    If same: creates symlinks to reference.
    """
    new_snapshot_ver = fetch_snapshot_controller_version(new_ver)
    ref_snapshot_ver = get_ref_snapshot_controller_version(ref_ver)

    need_new_images = (new_snapshot_ver and ref_snapshot_ver and
                       new_snapshot_ver != ref_snapshot_ver)

    if need_new_images:
        eprint(f"  snapshot-controller changed: {ref_snapshot_ver} -> {new_snapshot_ver}")
        # Check if CRDs actually changed by comparing upstream content with reference
        need_new_crds = check_crds_changed(new_ver, ref_ver)
        if need_new_crds:
            eprint(f"  CRDs changed upstream, downloading new CRDs and templates")
    else:
        eprint(f"  snapshot-controller unchanged ({ref_snapshot_ver}), using symlinks")
        need_new_crds = False

    for rel_dir in ANSIBLE_SYMLINK_DIRS:
        target_dir = ANSIBLE_DIR / rel_dir
        if not target_dir.exists():
            eprint(f"  WARNING: {rel_dir} not found, skipping")
            continue
        new_entry = target_dir / f"k8s-v{new_ver}"
        if new_entry.exists():
            eprint(f"  Already exists: {rel_dir}/k8s-v{new_ver}")
            continue

        # For load-images-information/vars: create real dir if snapshot-controller changed
        if need_new_images and rel_dir == "common/load-images-information/vars":
            ref_path = target_dir / f"k8s-v{ref_ver}"
            if ref_path.is_symlink():
                ref_path = target_dir / os.readlink(ref_path)
            ref_images = (ref_path / "system-images.yml").read_text()
            new_images = re.sub(r"(snapshot-controller:)v[\d.]+", rf"\g<1>{new_snapshot_ver}", ref_images)
            new_entry.mkdir()
            (new_entry / "system-images.yml").write_text(new_images)
            eprint(f"  Created: {rel_dir}/k8s-v{new_ver}/system-images.yml (snapshot-controller:{new_snapshot_ver})")
            continue

        # For snapshot-controller files/templates: download from upstream if major version changed
        if need_new_crds and rel_dir == "k8s-storage-backends/snapshot-controller/files":
            download_snapshot_crds(target_dir, new_ver)
            continue
        if need_new_crds and rel_dir == "k8s-storage-backends/snapshot-controller/templates":
            download_snapshot_templates(target_dir, new_ver)
            continue

        # Otherwise create symlink to latest existing entry's target
        existing = []
        for entry in target_dir.iterdir():
            m = re.match(r"k8s-v(\d+\.\d+\.\d+)$", entry.name)
            if m:
                existing.append((list(map(int, m.group(1).split("."))), entry))
        if not existing:
            eprint(f"  WARNING: No existing k8s entries in {rel_dir}, skipping")
            continue
        existing.sort(key=lambda x: x[0])
        latest_entry = existing[-1][1]
        symlink_target = os.readlink(latest_entry) if latest_entry.is_symlink() else latest_entry.name
        os.symlink(symlink_target, new_entry)
        eprint(f"  Created: {rel_dir}/k8s-v{new_ver} -> {symlink_target}")


def fetch_k8s_images(k8s_ver):
    """Fetch k8s image list with IDs and digests using docker.

    Determines the full set of images (control plane + etcd + coredns + pause)
    from K8s source, pulls each, and inspects to get image ID and digest.

    Returns list of tuples: (full_image_ref, short_id, digest)
    """
    # Fetch component versions from K8s constants.go
    url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{k8s_ver}/cmd/kubeadm/app/constants/constants.go"
    etcd_ver = None
    coredns_ver = None
    pause_ver = None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            content = resp.read().decode()
            m = re.search(r'DefaultEtcdVersion\s*=\s*"([^"]+)"', content)
            if m:
                etcd_ver = m.group(1)
            m = re.search(r'CoreDNSVersion\s*=\s*"([^"]+)"', content)
            if m:
                coredns_ver = m.group(1)
            m = re.search(r'PauseVersion\s*=\s*"([^"]+)"', content)
            if m:
                pause_ver = m.group(1)
    except Exception as e:
        eprint(f"  WARNING: Could not fetch constants.go: {e}")

    images = [
        f"registry.k8s.io/kube-apiserver:v{k8s_ver}",
        f"registry.k8s.io/kube-controller-manager:v{k8s_ver}",
        f"registry.k8s.io/kube-proxy:v{k8s_ver}",
        f"registry.k8s.io/kube-scheduler:v{k8s_ver}",
    ]
    if etcd_ver:
        images.append(f"registry.k8s.io/etcd:{etcd_ver}")
    if coredns_ver:
        images.append(f"registry.k8s.io/coredns/coredns:{coredns_ver}")
    if pause_ver:
        images.append(f"registry.k8s.io/pause:{pause_ver}")

    results = []
    for image in images:
        eprint(f"  Pulling {image}...")
        pull = subprocess.run(["docker", "pull", image],
                             capture_output=True, text=True)
        if pull.returncode != 0:
            eprint(f"  WARNING: Failed to pull {image}: {pull.stderr.strip()}")
            results.append((image, "PLACEHOLDER_ID", "sha256:PLACEHOLDER_DIGEST"))
            continue

        # Get image ID (first 12 hex chars)
        inspect_id = subprocess.run(
            ["docker", "inspect", "--format={{.Id}}", image],
            capture_output=True, text=True)
        full_id = inspect_id.stdout.strip().replace("sha256:", "")
        short_id = full_id[:12]

        # Get repo digest
        inspect_digest = subprocess.run(
            ["docker", "inspect", "--format={{index .RepoDigests 0}}", image],
            capture_output=True, text=True)
        repo_digest = inspect_digest.stdout.strip()
        digest = repo_digest.split("@")[-1] if "@" in repo_digest else "sha256:UNKNOWN"

        results.append((image, short_id, digest))

    return results


def add_images_to_prebuilt_lists(images):
    """Insert k8s images into WRCP-prebuilt-images-trixie.lst and bullseye.lst.

    Images are inserted in sorted order next to existing entries of the same component.
    Skips images that already exist in the file.
    Format: <image>,<12-char-id>,<sha256:digest>,bootstrap
    """
    for lst_file in (PREBUILT_IMAGES_TRIXIE, PREBUILT_IMAGES_BULLSEYE):
        if not lst_file.exists():
            eprint(f"  SKIP: {lst_file.name} not found")
            continue

        lines = lst_file.read_text().splitlines(keepends=True)
        content = "".join(lines)
        added = 0

        for image_ref, short_id, digest in images:
            # Skip if already present
            image_tag = image_ref.split(",")[0] if "," in image_ref else image_ref
            if image_tag in content:
                continue

            new_line = f"{image_ref},{short_id},{digest},bootstrap\n"

            # Extract component prefix (e.g. "registry.k8s.io/kube-apiserver"
            # or "registry.k8s.io/coredns/coredns" or "registry.k8s.io/etcd")
            component = image_ref.rsplit(":", 1)[0]

            last_idx = None
            for i, line in enumerate(lines):
                if line.startswith(component + ":"):
                    last_idx = i
            if last_idx is not None:
                lines.insert(last_idx + 1, new_line)
            else:
                # New component not in file yet - insert after last registry.k8s.io entry
                last_k8s_idx = None
                for i, line in enumerate(lines):
                    if line.startswith("registry.k8s.io/"):
                        last_k8s_idx = i
                if last_k8s_idx is not None:
                    lines.insert(last_k8s_idx + 1, new_line)
                else:
                    lines.append(new_line)
            content = "".join(lines)
            added += 1

        lst_file.write_text("".join(lines))
        if added:
            eprint(f"  Updated: {lst_file.name} ({added} images added)")
        else:
            eprint(f"  {lst_file.name}: all images already present")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Automate adding a new Kubernetes version to the project.",
        epilog=(
            "Examples:\n"
            "  %(prog)s 1.37.1\n"
            "  %(prog)s 1.37.1 --reference-k8s 1.36.1\n"
            "\n"
            "By default, the script auto-detects the reference version:\n"
            "  - If new version > default version: uses the default version\n"
            "  - If new version < default version: uses the highest existing version < new\n"
            "\n"
            "Use --reference-k8s to explicitly specify which existing version to\n"
            "use as the reference base. This is useful when adding multiple versions\n"
            "sequentially (e.g., adding 1.37.0 based on previously added 1.36.1\n"
            "rather than the default 1.35.2).\n"
            "\n"
            "Docker must be running to generate prebuilt-images entries.\n"
            "If Docker is unavailable, the script will skip that step."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "k8s_version",
        help="Kubernetes version to add (e.g., 1.36.1 or v1.36.1)",
    )
    parser.add_argument(
        "--reference-k8s",
        metavar="VERSION",
        help=(
            "Existing Kubernetes version to use as reference base "
            "(e.g., 1.36.1). If not specified, the script auto-detects "
            "the reference version."
        ),
    )
    return parser.parse_args()


def k8s_version(dirname: str) -> str | None:
    """Extract kubernetes version from directory name.

    Returns the version string if dirname matches 'kubernetes-X.Y.Z',
    otherwise returns None.
    """
    if not dirname.startswith("kubernetes-"):
        return None
    version = dirname.removeprefix("kubernetes-")
    return version if re.fullmatch(r"\d+\.\d+\.\d+", version) else None


def main():
    args = parse_args()

    # Validate environment and set up global paths
    setup_environment()

    new_ver = args.k8s_version.lstrip("v")

    # Validate version format
    try:
        new_ver = Version(new_ver)
    except InvalidVersion:
        sys.exit(
            f"ERROR: Invalid version format '{args.k8s_version}'. "
            "Expected X.Y.Z (e.g., 1.37.1)"
        )

    if len(new_ver.release) != 3 or new_ver != Version(".".join(map(str, new_ver.release))):
        sys.exit(
            f"ERROR: Invalid version format '{args.k8s_version}'. "
            "Expected X.Y.Z (e.g., 1.37.1)"
        )

    # Validate version exists on GitHub
    try:
        url = f"https://github.com/kubernetes/kubernetes/releases/tag/v{new_ver}"
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        sys.exit(f"ERROR: Kubernetes v{new_ver} does not exist on GitHub.\n"
                 f"  Check https://github.com/kubernetes/kubernetes/releases for valid versions.")

    # Pre-check: already exists?
    if (K8S_PKG_DIR / f"kubernetes-{new_ver}").exists():
        eprint(f"kubernetes-{new_ver} is already present in the repo.")
        sys.exit(0)

    # Determine reference version (validate before network calls)
    if args.reference_k8s:
        # User explicitly specified a reference version
        ref_ver = args.reference_k8s.lstrip("v")
        if not re.match(r"^\d+\.\d+\.\d+$", ref_ver):
            sys.exit(f"ERROR: Invalid --reference-k8s format '{ref_ver}'. Expected X.Y.Z (e.g., 1.36.1)")
        ref_pkg_dir = K8S_PKG_DIR / f"kubernetes-{ref_ver}"
        if not ref_pkg_dir.exists():
            # List available kubernetes versions, excluding non-version dirs like kubernetes-unversioned
            available = sorted(
                (
                    version
                    for d in K8S_PKG_DIR.iterdir()
                    if d.is_dir()
                    and (version := k8s_version(d.name)) is not None
                ),
                key=Version,
            )
            sys.exit(f"ERROR: Reference version package kubernetes-{ref_ver} not found in {K8S_PKG_DIR}\n"
                     f"  Available versions: {', '.join(available)}")
        ref_rules_file = ref_pkg_dir / "debian/all/deb_folder/rules"
        if not ref_rules_file.exists():
            sys.exit(f"ERROR: Reference version kubernetes-{ref_ver} is incomplete (missing debian/all/deb_folder/rules).\n"
                     f"  Please specify a fully built reference version.")
        ref_ver = Version(ref_ver)
        if ref_ver >= new_ver:
            sys.exit(f"ERROR: --reference-k8s version ({ref_ver}) must be older than "
                     f"the new version ({new_ver}).")
        # Validate reference is from the immediate previous minor version
        if new_ver.release[0] != ref_ver.release[0]:
            sys.exit(f"ERROR: --reference-k8s version ({ref_ver}) has a different major version "
                     f"than the new version ({new_ver}).\n"
                     f"  Cross-major-version references are not supported.")
        if new_ver.release[1] - ref_ver.release[1] > 1:
            sys.exit(f"ERROR: --reference-k8s version ({ref_ver}) is not from the immediate "
                     f"previous minor release.\n"
                     f"  When adding kubernetes {new_ver} (minor {new_ver.release[1]}), the reference "
                     f"must be from minor {new_ver.release[1] - 1} (e.g., 1.{new_ver.release[1] - 1}.x).\n"
                     f"  Please use a 1.{new_ver.release[1] - 1}.x version as the reference.")
        ref_source = "user-specified"
    else:
        # Auto-detect reference version:
        # If new version > default version, use default as reference (known-good baseline)
        # If new version < default version, use the highest existing version < new_ver
        default_ver = get_default_k8s_version()

        if new_ver > Version(default_ver):
            if not (K8S_PKG_DIR / f"kubernetes-{default_ver}").exists():
                sys.exit(f"ERROR: Default version package kubernetes-{default_ver} not found in {K8S_PKG_DIR}")
            ref_ver = default_ver
        else:
            ref_ver = find_latest_k8s_version(new_ver)

        # Validate auto-detected reference is from the immediate previous minor version
        ref_ver = Version(ref_ver)
        if new_ver.release[0] != ref_ver.release[0]:
            sys.exit(f"ERROR: Auto-detected reference version ({ref_ver}) has a different major "
                     f"version than the new version ({new_ver}).\n"
                     f"  Cross-major-version references are not supported.")
        if new_ver.release[1] - ref_ver.release[1] > 1:
            sys.exit(f"ERROR: Auto-detected reference version ({ref_ver}) is not from the "
                     f"immediate previous minor release.\n"
                     f"  When adding kubernetes {new_ver} (minor {new_ver.release[1]}), the reference "
                     f"must be from minor {new_ver.release[1] - 1} (e.g., 1.{new_ver.release[1] - 1}.x).\n"
                     f"  Please add kubernetes 1.{new_ver.release[1] - 1}.x first, or if already in the "
                     f"load, provide a 1.{new_ver.release[1] - 1}.x reference with --reference-k8s.")
        ref_source = "auto-detected"

    # Fetch Go version dynamically
    eprint(f"Fetching Go version for Kubernetes v{new_ver}...")
    go_ver = fetch_go_version(new_ver)

    # Fetch SHA256 of the source tarball
    eprint(f"Fetching SHA256 for v{new_ver} tarball...")
    sha256 = fetch_tar_sha256(new_ver)

    ref_go_ver = ""
    rules_file = K8S_PKG_DIR / f"kubernetes-{ref_ver}/debian/all/deb_folder/rules"
    for line in rules_file.read_text().splitlines():
        if line.startswith("go_version :="):
            ref_go_ver = line.split(":=")[1].strip()
            break

    eprint("=" * 50)
    eprint(f" Adding Kubernetes {new_ver}")
    eprint(f" Reference: kubernetes-{ref_ver} ({ref_source})")
    eprint(f" Go: {go_ver} (ref: {ref_go_ver})")
    eprint(f" Golang pkg: golang-{go_major_minor(go_ver)}")
    eprint("=" * 50)

    eprint("\n[1/6] Creating kubernetes package...")
    create_k8s_package(new_ver, ref_ver, go_ver, ref_go_ver, sha256)

    eprint("\n[2/6] Porting patches from reference version...")
    port_patches(new_ver, ref_ver)

    eprint("\n[3/6] Registering in package listing files...")
    register_in_listings(new_ver)

    eprint("\n[4/6] Adding golang to stx-tools base download lists (if needed)...")
    golang_in_compile = add_golang_to_base_lists(go_ver, ref_go_ver, new_ver)

    eprint("\n[5/6] Creating ansible-playbooks changes...")
    create_ansible_changes(new_ver, ref_ver)

    eprint("\n[6/6] Adding k8s images to prebuilt-images lists...")
    lst_files_present = PREBUILT_IMAGES_TRIXIE.exists() or PREBUILT_IMAGES_BULLSEYE.exists()
    docker_available = False
    if not lst_files_present:
        eprint("  SKIPPED: prebuilt-images list files not found."
              " Please add the required container image entries manually.")
    else:
        docker_available = subprocess.run(
            ["docker", "info"], capture_output=True).returncode == 0
        if not docker_available:
            eprint("  WARNING: Docker is not available/running.")
            eprint("  Cannot generate prebuilt-images entries without Docker.")
            eprint("  Please add the required container image entries manually.")
        else:
            k8s_images = fetch_k8s_images(new_ver)
            add_images_to_prebuilt_lists(k8s_images)

    eprint(f"\n{'=' * 50}")
    eprint(f" DONE! Kubernetes {new_ver} added.")
    eprint(f"{'=' * 50}")
    repos_modified = ["stx-tools", "integ", "ansible-playbooks"]
    if lst_files_present and docker_available:
        repos_modified.append("titanium-tools")
    eprint(f"\nThis script has added the new k8s version {new_ver} changes in:")
    eprint(f"  {', '.join(repos_modified)}")
    if not lst_files_present or not docker_available:
        eprint(f"\n  NOTE: prebuilt-images list entries were not added."
              f" Please add the required container image entries manually.")
    eprint(f"\nIt is the developer's responsibility to build and verify the changes.")
    eprint(f"In case of build or deployment failure, do the corresponding fixes before raising the review.")
    eprint(f"\nNext steps:")
    step = 1
    if golang_in_compile:
        eprint(f"  {step}. Build golang-{go_major_minor(go_ver)} package first (added to compile repo)")
        eprint(f"     build-pkgs -c -p golang-{go_major_minor(go_ver)}")
        step += 1
    eprint(f"  {step}. Build and verify the kubernetes-{new_ver} package")
    eprint(f"     build-pkgs -c -p kubernetes-{new_ver}")
    step += 1
    eprint(f"  {step}. Deploy and verify:")
    eprint(f"     - Install ISO with k8s {new_ver} on AIO-SX")
    eprint(f"     - Install ISO with k8s {new_ver} on AIO-DX")
    step += 1
    eprint(f"  {step}. K8s upgrade verification:")
    eprint(f"     - Upgrade from previous k8s version to {new_ver}")
    eprint(f"     - Verify all pods are running after upgrade")


if __name__ == "__main__":
    main()
