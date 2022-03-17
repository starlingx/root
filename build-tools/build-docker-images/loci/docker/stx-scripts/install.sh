#!/bin/bash

set -ex

LOCI_DIR="/opt/loci"

# configure apt/yum repos
"$LOCI_DIR/stx-scripts/setup-package-repos.sh"

# run Loci installer
"$LOCI_DIR/scripts/install.sh" "$@"

# delete wheel tarball etc
"$LOCI_DIR/stx-scripts/cleanup.sh"
