#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
#  A place to collect potentially reusable python functions
#

def splitRpmFilename(filename):
    """
    Split an rpm filename into components:
    package name, version, release, epoch, architecture
    """

    if filename[-4:] == '.rpm':
        filename = filename[:-4]

    idx = filename.rfind('.')
    arch = filename[idx+1:]
    filename = filename[:idx]

    idx = filename.rfind('-')
    rel = filename[idx+1:]
    filename = filename[:idx]

    idx = filename.rfind('-')
    ver = filename[idx+1:]
    filename = filename[:idx]

    idx = filename.find(':')
    if idx == -1:
        epoch = ''
        name = filename
    else:
        epoch = filename[:idx]
        name = filename[idx+1:]

    return name, ver, rel, epoch, arch

