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

"""Unit tests for the recipes_paths authority.

recipes_paths is the single, dependency-free source of truth for the on-disk
layout of repacked recipe artifacts (.dsc, tarballs, .repack_manifest.json),
which debrepack_2_0 (writer), debian_dsc.DscManager, downloader-2.0 and
build-pkgs-2.0 all compose on. These tests pin the canonical layout:

    <build_root>/recipes/<build_type>/<pkg_name>/

guard the composition contract (each helper builds on the one below it), and
assert the std/rt distinctness that is the whole reason per-build_type nesting
exists.
"""
import os
import unittest

import recipes_paths


class RecipesPathAuthorityTests(unittest.TestCase):

    def test_recipes_root_appends_recipes_to_build_root(self):
        self.assertEqual(recipes_paths.recipes_root('/build'),
                         os.path.join('/build', 'recipes'))

    def test_recipes_root_uses_named_subdir_constant(self):
        self.assertEqual(
            recipes_paths.recipes_root('/build'),
            os.path.join('/build', recipes_paths.RECIPES_SUBDIR))

    def test_recipes_dir_adds_exactly_one_build_type_layer(self):
        root = recipes_paths.recipes_root('/build')
        self.assertEqual(recipes_paths.recipes_dir(root, 'rt'),
                         os.path.join('/build', 'recipes', 'rt'))
        self.assertEqual(recipes_paths.recipes_dir(root, 'std'),
                         os.path.join('/build', 'recipes', 'std'))

    def test_pkg_recipes_dir_is_canonical_three_layer_path(self):
        root = recipes_paths.recipes_root('/build')
        self.assertEqual(
            recipes_paths.pkg_recipes_dir(root, 'rt', 'i40e'),
            os.path.join('/build', 'recipes', 'rt', 'i40e'))

    def test_std_and_rt_paths_are_distinct_for_same_package(self):
        """A @KERNEL_TYPE@-templated driver's std and rt artifacts must land
        on physically distinct paths so neither overwrites the other."""
        root = recipes_paths.recipes_root('/build')
        std = recipes_paths.pkg_recipes_dir(root, 'std', 'iavf')
        rt = recipes_paths.pkg_recipes_dir(root, 'rt', 'iavf')
        self.assertNotEqual(std, rt)

    def test_composition_pkg_dir_builds_on_recipes_dir(self):
        """pkg_recipes_dir must compose on recipes_dir (single authority per
        layer), not re-derive the <build_type> layer independently."""
        root = recipes_paths.recipes_root('/build')
        self.assertTrue(
            recipes_paths.pkg_recipes_dir(root, 'rt', 'ice').startswith(
                recipes_paths.recipes_dir(root, 'rt') + os.sep))


class DebrepackReexportTests(unittest.TestCase):
    """debrepack_2_0 re-exports the authority; both names must resolve to the
    same single definition. Skipped if debrepack_2_0's heavy optional deps are
    unavailable in this environment."""

    def test_debrepack_reexports_same_functions(self):
        try:
            import debrepack_2_0
        except Exception as e:  # pragma: no cover - env-dependent
            self.skipTest("debrepack_2_0 not importable here: %s" % e)
        self.assertIs(debrepack_2_0.recipes_root, recipes_paths.recipes_root)
        self.assertIs(debrepack_2_0.recipes_dir, recipes_paths.recipes_dir)
        self.assertIs(debrepack_2_0.pkg_recipes_dir,
                      recipes_paths.pkg_recipes_dir)


if __name__ == '__main__':
    unittest.main()
