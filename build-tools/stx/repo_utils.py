# Copyright (c) 2021 Wind River Systems, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fnmatch
import os

# repo_root [<dir>]:
#      Return the root directory of a repo.
#      Note: symlinks are fully expanded.
#

def repo_root (dir=os.environ['PWD']):
    if dir is None:
        return None
    if not os.path.isdir(dir):
        # Parhaps a file, try the parent directory of the file.
        dir = os.path.dirname(dir)
    if not os.path.isdir(dir):
        return None
    while dir != "/":
        if os.path.isdir(os.path.join(dir, ".repo")):
            return os.path.normpath(dir)
        dir = os.path.dirname(dir)
    return None

