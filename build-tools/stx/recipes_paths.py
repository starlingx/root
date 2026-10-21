"""
Copyright (c) 2026 Wind River Systems, Inc.

SPDX-License-Identifier: Apache-2.0

Single authority for the on-disk layout of repacked source recipe artifacts
(.dsc files, tarballs, and the .repack_manifest.json describing them).

Every module in the 2.0 build pipeline that needs to locate a repacked
package's recipe artifacts — debrepack_2_0 (the writer), debian_dsc.DscManager
(index/sidecar/stamp/manifest store), downloader-2.0 and build-pkgs-2.0 — MUST
compose on the helpers here rather than open-coding os.path.join(..., 'recipes',
...) with its own copy of the layout. Independently-maintained copies of this
layout are exactly what let std and rt repacks of @KERNEL_TYPE@-templated
driver packages (i40e/iavf/ice/bnxt-en + -rt) disagree on the canonical .dsc
location, so the std binaries were silently never produced and build-image
failed with "no installation candidate".

This module is intentionally dependency-free (standard library only) so that
every consumer — including lightweight modules and their unit tests — can
import it without pulling in heavier build-only dependencies.

Canonical layout (flat recipes root -> per build_type -> per package):

    <recipes_root>/<build_type>/<pkg_name>/<name>_<ver>.dsc
    <recipes_root>/<build_type>/<pkg_name>/.repack_manifest.json

Per-build_type nesting is essential: Debian source package filenames carry no
build_type marker, so for a @KERNEL_TYPE@-templated package the std and rt
repacks of the SAME source checksum produce .dsc/tarball files with IDENTICAL
names. Separate physical paths per build_type keep them from overwriting each
other.
"""

import os

# The subdirectory under build_root that holds all recipe artifacts. Named
# here once so no consumer hard-codes the literal 'recipes' string.
RECIPES_SUBDIR = 'recipes'


def recipes_root(build_root):
    """Flat recipes root for a build tree: <build_root>/recipes."""
    return os.path.join(build_root, RECIPES_SUBDIR)


def recipes_dir(recipes_root_dir, build_type):
    """Per-build_type recipes dir: <recipes_root>/<build_type>.

    Takes the FLAT recipes root (as returned by recipes_root()) and adds
    exactly one build_type layer. Pass the flat root, never an
    already-scoped dir — passing an already-build_type-scoped directory is
    what produced the recipes/<btype>/<btype> double-nesting bug.
    """
    return os.path.join(recipes_root_dir, build_type)


def pkg_recipes_dir(recipes_root_dir, build_type, pkg_name):
    """Canonical per-package recipe-artifact dir for a build_type:
    <recipes_root>/<build_type>/<pkg_name>.

    This is the one true home of a repacked package's .dsc, tarballs, and
    .repack_manifest.json. Composes on recipes_dir().
    """
    return os.path.join(recipes_dir(recipes_root_dir, build_type), pkg_name)
