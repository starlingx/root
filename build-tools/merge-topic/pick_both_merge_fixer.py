#!/usr/bin/python3

#
# Copyright (c) 2021,2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import pprint
import subprocess

CHERRY_PICKING = 'You are currently cherry-picking commit'
BOTH_ADDED = 'both added:'
BOTH_MODIFIED = 'both modified:'

SUCCESS = 0
FAILURE = 1


def run_cmd(cmd, env=None, shell=False, halt_on_exception=False):
    try:
        print('Running {} with env {}:\n'.format(cmd, env))
        oldenv = os.environ.copy()
        # Merge any caller-supplied overrides onto the current environment.
        # env may be None (the common case), so guard against it — {**None}
        # raises "TypeError: 'NoneType' object is not a mapping".
        env = { **oldenv, **(env or {}) }
        output = subprocess.check_output(
            cmd
            , env=env
            , errors="strict"
            , shell=shell).strip()
        found_exception = False
    except Exception as e:
        found_exception = True
        pprint.pprint(e)
    finally:
        if found_exception and halt_on_exception:
            exit(1)
        if not found_exception:
            return SUCCESS, output
    return FAILURE, None


print('CWD {}'.format(os.getcwd()))

rc, out = run_cmd(['git', 'status'])
if rc != SUCCESS:
    exit(rc)

if CHERRY_PICKING in out:
    print('Detected cherry-picking {}'.format(os.getcwd()))
    status_out = out
    resolved_any = False
    for status_line in status_out.splitlines():
        if BOTH_ADDED in status_line or BOTH_MODIFIED in status_line:
            # Get file
            conflict_file = status_line.split(':')[1].strip()
            print('Identified conflict file {}'.format(conflict_file))

            with open(conflict_file, "r+") as f:
                # Need to buffer, so file pointer is reset
                buffer = f.readlines()
                f.seek(0)
                for code_line in buffer:
                    # TODO better matching
                    if not code_line.startswith('<<<<<<< ') and \
                            not (code_line.startswith('=======')) and \
                            not code_line.startswith('>>>>>>> '):
                        f.write(code_line)
                    else:
                        print('Dropping :{}'.format(code_line))

                f.truncate()

            # Git add file
            rc, _ = run_cmd(['git', 'add', conflict_file])
            if rc != SUCCESS:
                exit(rc)
            resolved_any = True

    # Git cherry-pick --continue
    #
    # This MUST run exactly once, AFTER every conflicted file has been
    # staged -- not once per file inside the loop. `--continue` completes
    # the whole cherry-pick and creates the commit; calling it again on a
    # second loop iteration finds no cherry-pick in progress and fails
    # (its error goes to stderr, which run_cmd does not capture, so the
    # failure looked silent). When more than one file conflicts (e.g. a
    # rerere-resolved base-bullseye.lst alongside another file) the old
    # per-file placement caused exactly that silent failure.
    #
    # `--continue` also wants to open an editor on the reused commit
    # message. GIT_EDITOR=true is not sufficient on its own (the
    # sequencer's commit step still launched the editor), so --no-edit is
    # used to keep the existing message and never open an editor.
    if resolved_any:
        rc, out = run_cmd(['git', 'cherry-pick', '--continue', '--no-edit'],
                          env={'GIT_EDITOR': 'true'})
        if rc != SUCCESS:
            exit(rc)

