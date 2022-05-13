#!/bin/bash
#
# Copyright (c) 2019-2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# Image and wheel build utility functions
#

#
# Echo to stderr
#    echo_stderr [any text you want]
#
echo_stderr ()
{
    echo "$@" >&2
}


#
# Function to call a command, with support for retries
#
#   with_retries [<options>] <retries> <cmd> [<cmd_args>...]
#
#   Options:
#       -d <secs> | --delay <secs>
#            Wait given number of seconds between retries
#       -t <secs> | --timeout <secs>
#            Each iteration of the command runs under a timeout
#       -k <secs> | --kill-timeout <secs>
#            Each iteration of the command is killed violently
#            if it doesn't exit voluntarily within the set time
#            after the initial timeout signal.
#
function with_retries {
    local delay=5
    local max_time=0
    local kill_time=0
    local to_cmd=""

    while [ $1 != "" ]; do
        case "$1" in
            -d | --delay)
                delay=$2
                shift 2
                ;;
            -t | --timeout)
                max_time=$2
                shift 2
                ;;
            -k | --kill-timeout)
                kill_time=$2
                shift 2
                ;;
            *)
                break
                ;;
        esac
    done

    local max_attempts=$1
    local cmd=$2
    shift 2

    if [ ${max_time} -gt 0 ]; then
        to_cmd="timeout "
        if [ ${kill_time} -gt 0 ]; then
            to_cmd+="--kill-after=${kill_time} "
        fi
        to_cmd+="${max_time} "
    fi

    # Pop the first two arguments off the list,
    # so we can pass additional args to the command safely

    local -i attempt=0
    local rc=0

    while :; do
        let attempt++

        echo_stderr "Running: ${cmd} $@"
        ${to_cmd} ${cmd} "$@"
        rc=$?
        if [ $rc -eq 0 ]; then
            return 0
        fi

        if [ $rc -eq 124 ]; then
            echo_stderr "Command (${cmd}) timed out, attempt ${attempt} of ${max_attempts}."
        elif [ $rc -eq 137 ]; then
            echo_stderr "Command (${cmd}) timed out and killed, attempt ${attempt} of ${max_attempts}."
        else
            echo_stderr "Command (${cmd}) failed, attempt ${attempt} of ${max_attempts}."
        fi

        if [ ${attempt} -lt ${max_attempts} ]; then
            echo_stderr "Waiting ${delay} seconds before retrying..."
            sleep ${delay}
            continue
        else
            echo_stderr "Max command attempts reached. Aborting..."
            return 1
        fi
    done
}

check_pipe_status() {
    local -a pipestatus=(${PIPESTATUS[*]})
    local -i i
    for ((i=0; i<${#pipestatus[*]}; ++i)) ; do
        [[ "${pipestatus[$i]}" -eq 0 ]] || return 1
    done
    return 0
}
