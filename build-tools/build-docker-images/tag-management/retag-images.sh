#!/bin/bash
#
# Copyright (c) 2020 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# This utility retags images, as configured by yaml files, formatted as:
#
# images:
#  - name: docker.io/starlingx/k8s-cni-sriov
#    src_build_tag: master-centos-stable-20191203T153530Z.0
#    src_ref: https://opendev.org/starlingx/integ/commit/dac417bd31ed36d455e94db4aabe5916367654d4
#    tag: stx.3.0-v2.2
#  - name: docker.io/starlingx/k8s-plugins-sriov-network-device
#    src_build_tag: master-centos-stable-20191203T153530Z.0
#    src_ref: https://opendev.org/starlingx/integ/commit/dac417bd31ed36d455e94db4aabe5916367654d4
#    tag: stx.3.0-v3.1
#

declare RUNCMD=
declare -a REPUSH

function usage {
    cat >&2 <<EOF
Usage:
$(basename $0)

Options:
    --dryrun:         Print commands that would be run
    --repush <image>: Repush a specific image, even if the tag already exists

EOF
}

function is_in {
    local search=$1
    shift

    for v in $*; do
        if [ "${search}" = "${v}" ]; then
            return 0
        fi
    done
    return 1
}

function parse_image_yaml {
python -c '
import sys
import yaml

for fname in sys.argv[1:]:
    with open(fname) as f:
        imgs = yaml.load_all(f, Loader=yaml.FullLoader)
        for entry in imgs:
            for img in entry.get("images"):
                print ("%s|%s|%s|%s" % (
                    img.get("name"),
                    img.get("tag"),
                    img.get("src_build_tag"),
                    img.get("src_ref")))
' ${@}
}

function get_tags_from_docker_hub {
    local url=$1

    curl -k -sSL -X GET ${url} | python -c '
import sys, json
y=json.loads(sys.stdin.read())
if y and y.get("next"):
    print("next=%s" % y.get("next"))
if y and y.get("results"):
    for res in y.get("results"):
        if res.get("name"):
            print("tag=%s" % res.get("name"))
' | while IFS='=' read key value; do
        if [ "${key}" = "next" ]; then
            get_tags_from_docker_hub ${value}
        else
            echo "${key}=${value}"
        fi
    done
}

function retag_and_push_image {
    local name=$1
    local src_tag=$2
    local new_tag=$3

    # Break down the name into components
    local docker_registry=${name%%/*}
    local image=${name/${docker_registry}\/}
    local repository_name=${image%/*}
    local label=${image/*\/}

    if is_in $(basename $label) ${REPUSH[@]}; then
        echo "Skipping existence check for ${name}"
    else
        if [ "${docker_registry}" = "docker.io" ]; then
            get_tags_from_docker_hub https://registry.hub.docker.com/v2/repositories/${image}/tags \
                | grep -q "^tag=${new_tag}$"
            if [ $? -eq 0 ]; then
                # Already exists
                echo "Image tag exists: ${name}:${new_tag}"
                return 0
            fi
        else
            curl -k -sSL -X GET https://${docker_registry}/v2/${image}/tags/list \
                | python -c '
import sys, yaml, json, re
y=yaml.load(sys.stdin.read(), Loader=yaml.FullLoader)
RC=1
if y and sys.argv[1] in [img for img in y.get("tags")]:
    RC=0
sys.exit(RC)
' ${new_tag}
            if [ $? -eq 0 ]; then
                # Already exists
                echo "Image tag exists: ${name}:${new_tag}"
                return 0
            fi
        fi
    fi

    ${RUNCMD} docker image pull ${name}:${src_tag}
    if [ $? -ne 0 ]; then
        echo "Failed to pull ${name}:${src_tag}" >&2
        return 1
    fi

    ${RUNCMD} docker tag ${name}:${src_tag} ${name}:${new_tag}
    if [ $? -ne 0 ]; then
        echo "Failed to retag ${name}:${src_tag} as ${name}:${new_tag}" >&2
        return 1
    fi

    ${RUNCMD} docker push ${name}:${new_tag}
    if [ $? -ne 0 ]; then
        echo "Failed to push ${name}:${new_tag}" >&2
        return 1
    fi

    echo "Pushed: ${name}:${new_tag}"

    ${RUNCMD} docker image rm ${name}:${src_tag} ${name}:${new_tag}
    if [ $? -ne 0 ]; then
        echo "Failed to rm images ${name}:${src_tag} ${name}:${new_tag}" >&2
    fi

    return 0
}

OPTS=$(getopt -o h,c -l help,repush:,dryrun -- "$@")
if [ $? -ne 0 ]; then
    usage
    exit 1
fi

eval set -- "${OPTS}"

while true; do
    case $1 in
        --)
            # End of getopt arguments
            shift
            break
            ;;
        --repush)
            # Read comma-separated values into array
            REPUSH+=(${2//,/ })
            shift 2
            ;;
        --dryrun)
            RUNCMD=echo
            shift
            ;;
        -h | --help )
            usage
            exit 1
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

parse_image_yaml ${@} | while IFS='|' read name new_tag src_tag src_ref; do
    retag_and_push_image ${name} ${src_tag} ${new_tag}
done

