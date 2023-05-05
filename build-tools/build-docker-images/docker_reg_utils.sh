# bash

#
# Copyright (c) 2018-2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#


#
# Usage: docker_reg_tag_exists [OPTIONS...] REGISTRY/IMAGE:TAG
#
# Check whether the specified image exists in a remote registry.
#
#   --max-attempts MAX_ATTEMPTS  try to access the tag at most this many times
#                                upon detecting transient errors.
#                                Default: 3.
#
#   --backoff-delay SECONDS      sleep this many seconds between retries
#                                By default we sleep 5 seconds on the first retry,
#                                then increment the sleep time by 5 on subsequent
#                                retries.
#
#   --request-timeout SECONDS    timeout for the REST API request
#                                Default: 10.
#
# Returns:
#   0 (true)    - if image/tag exists
#   1 (false)   - if image/tag doesn't exist, or we have no permissions to access it.
#
# Exits with status other than 0 or 1 if we can't establish a connection
# with the registry.
#

declare _DRU_REGCTL_FOUND=
declare -A _DRU_STATUS=(
    [found]=0
    [not_found]=1
    [err_unknown]=2
    [err_invalid_ref]=3
    [err_auth]=4
    [err_dns]=5
    [err_bad_gateway]=6
    [err_connrefused]=7
    [err_no_route]=8
    [err_tls]=9
    [err_rate_limit]=10
    [err_timeout]=124
    [err_interrupt]=130
)
function docker_reg_tag_exists {
    local image
    local max_attempts=3
    local backoff_delay=5
    local backoff_delay_increment=5
    local req_timeout=10
    local error_code=${_DRU_STATUS[err_unknown]}
    local usage="\
Usage: ${FUNCNAME[0]} OPTIONS REGISTRY/IMAGE:TAG
    --max-attempts MAX_ATTEMPTS
    --backoff-delay SECONDS
    --request-timeout SECONDS
"

    # process command line
    local opts
    if ! opts=$(getopt -l max-attempts:,backoff-delay:,request-timeout: -- \
                        ${FUNCNAME[0]} "$@") ; then
        echo "$usage" >&2
        exit ${_DRU_STATUS[err_unknown]}
    fi
    eval set -- "${opts}"
    while [[ "$#" -gt 0 ]] ; do
        case "$1" in
            --max-attempts)
                max_attempts="$2"
                shift 2
                ;;
            --backoff-delay)
                backoff_delay="$2"
                backoff_delay_increment=0
                shift 2
                ;;
            --request-timeout)
                req_timeout="$2"
                shift 2
                ;;
            --)
                shift
                break
                ;;
            *)
                echo "$usage" >&2
                exit ${_DRU_STATUS[err_unknown]}
                ;;
        esac
    done
    if [[ "$#" -ne 1 ]] ; then
        echo "$usage" >&2
        exit ${_DRU_STATUS[err_unknown]}
    fi
    image="$1"

    # make sure regctl exists
    if [[ ! "$_DRU_REGCTL_FOUND" ]] ; then
        if ! regctl --help >/dev/null ; then
            echo >&2
            echo "The regctl command was not found in your \$PATH" >&2
            echo "Please install it from here:" >&2
            echo "     https://github.com/regclient/regclient/releases" >&2
            echo >&2
            exit ${_DRU_STATUS[err_unknown]}
        fi
        _DRU_REGCTL_FOUND=1
    fi

    local attempt=1
    local error_msg
    while true ; do

        local regctl=(timeout --foreground "${req_timeout}s" \
                            regctl -v debug manifest get "$image")
        local stderr status
        stderr="$("${regctl[@]}" 2>&1 1>/dev/null)"
        exit_status="$?"
        if [[ $exit_status -eq 0 ]] ; then
            return ${_DRU_STATUS[found]}
        fi

        local retry=0
        # interrupt
        if [[ $exit_status -eq 130 ]] ; then
            error_code=${_DRU_STATUS[err_interrupt]}
            error_msg=
            retry=0
        # invalid "registry/image:tag" format
        elif echo "$stderr" | grep -qi "invalid reference" ; then
            error_code=${_DRU_STATUS[err_invalid_ref]}
            error_msg="invalid image reference format"
            retry=0
        # amazon returns this when the auto-generated username/password in
        # ~/.docker/config.json is valid, but expired recently
        elif echo "$stderr" | grep -qi 'authorization token has expired' ; then
            error_code=${_DRU_STATUS[err_auth]}
            error_msg="authorization token has expired"
            retry=0
        # HTTP proxy error
        elif echo "$stderr" | grep -qi "bad gateway" ; then
            error_code=${_DRU_STATUS[err_bad_gateway]}
            error_msg="registry server returned <bad gateway>"
            retry=1
        # registry host name unresolvable
        elif echo "$stderr" | grep -qi 'lookup .* no such host' ; then
            error_msg="DNS lookup error"
            error_code=${_DRU_STATUS[err_dns]}
            retry=1
        # TCP connection refused
        elif echo "$stderr" | grep -qi 'connection refused' ; then
            error_msg="connection refused"
            error_code=${_DRU_STATUS[err_connrefused]}
            retry=1
        # IP routing error
        elif echo "$stderr" | grep -qi 'no route to host' ; then
            error_msg="no route to host"
            error_code=${_DRU_STATUS[err_no_route]}
            retry=1
        # SSL: untrusted signer
        elif echo "$stderr" | grep -qi \
                'certificate signed by unknown authority' ; then
            error_msg="invalid SSL certificate"
            error_code=${_DRU_STATUS[err_tls]}
            retry=0
        # SSL: expired cert
        elif echo "$stderr" | grep -qi 'certificate has expired' ; then
            error_msg="expired SSL certificate"
            error_code=${_DRU_STATUS[err_tls]}
            retry=0
        # docker hub rate limit
        elif echo "$stderr" | grep -qi 'rate limit exceeded' ; then
            error_msg="request rate limit exceeded"
            error_code=${_DRU_STATUS[err_rate_limit]}
            retry=1
        # the timeout command returns 124 if we timed out
        elif [[ $exit_status -eq 124 ]] ; then
            error_msg='operation timed out'
            error_code=${_DRU_STATUS[err_timeout]}
            retry=1
        # Some other error, such as http "404 Not Found" or "403 Forbidden".
        # There's no way to distinguish non-existent namespaces from insufficient
        # permissions (both return "permission denied"-type errors).
        # These errors likely mean "docker push" would fail as well.
        # Return false in all of these cases.
        else
            return ${_DRU_STATUS[not_found]}
        fi

        # retry on intermittent errors
        if [[ $retry -eq 1 && $attempt -lt $max_attempts ]] ; then
            let ++attempt
            echo "$image: connection error," \
                        "sleeping $backoff_delay second(s)" >&2
            sleep $backoff_delay || exit ${_DRU_STATUS[err_unknown]}
            let backoff_delay+=backoff_delay_increment
            echo "$image: retrying, attempt $attempt/$max_attempts" >&2
            continue
        fi

        echo "error: command failed: ${regctl[@]}" >&2
        echo "$stderr" | sed -r 's/^/    /' >&2
        break
    done

    if [[ "$error_msg" ]] ; then
        echo "error: $image: $error_msg" >&2
    fi
    exit $error_code
}
