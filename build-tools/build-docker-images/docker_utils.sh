# bash

# Usage: MAX_ATTEMPTS=1 RETRY_DELAY=2 USE_DOCKER_CACHE=yes \
#          docker_build_with_retries DOCKER_ARGS...
function docker_build_with_retries {

    if [[ -z "$MAX_ATTEMPTS" ]] ; then
        echo "ERROR: docker_build_with_retries(): MAX_ATTEMPTS must be defined!" >&2
        return 1
    fi

    if [[ -z "$RETRY_DELAY" ]] ; then
        echo "ERROR: docker_build_with_retries(): RETRY_DELAY must be defined!" >&2
        return 1
    fi

    local max_attempts=$MAX_ATTEMPTS
    local delay=$RETRY_DELAY

    local -a cache_args
    if [[ "$USE_DOCKER_CACHE" != "yes" ]] ; then
        cache_args=("--no-cache")
    fi

    local -i attempt=0
    while true ; do
        let attempt++
        if [[ attempt -gt 1 ]] ; then
            cache_args=("--no-cache")
        fi
        echo "Running: docker build ${cache_args[@]} $@" >&2
        docker build "${cache_args[@]}" "$@"
        local rc=$?
        if [ $rc -eq 0 ]; then
            return 0
        fi

        echo "Command [docker build] failed [rc=$rc], attempt ${attempt} of ${max_attempts}." >&2
        if [ ${attempt} -lt ${max_attempts} ]; then
            echo "Waiting ${delay} seconds before retrying..." >&2
            sleep ${delay}
            continue
        else
            echo "Max command attempts reached. Aborting..." >&2
            return 1
        fi
    done

}
