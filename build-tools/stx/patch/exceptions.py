#
# Copyright (c) 2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Defining patch building exceptions
'''

class FetchDebsError(Exception):
    "Exception for failures when getting debs from aptly"

    def __init__(self, message="") -> None:
        self.message = "Error when fetching debs. " + message
        super().__init__(self.message)

