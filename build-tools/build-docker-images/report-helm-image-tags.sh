#!/bin/bash
#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# Report all docker image tags referenced in built helm chart overrides
# and packaged helm charts.
#
# Usage:
#   report-helm-image-tags.sh -d <helm-output-dir>
#   report-helm-image-tags.sh -p <deb-file> [<deb-file> ...]
#   report-helm-image-tags.sh -i <iso-file>
#

usage() {
    cat >&2 <<EOF
Usage: $(basename $0) [OPTIONS]

Report docker image tags from helm chart overrides and packaged charts.

Scans both:
  - usr/lib/fluxcd/*-static-overrides.yaml  (deployment overrides)
  - usr/lib/helm/*.tgz values.yaml          (chart defaults)

Input sources (one required):
  -d, --helm-dir <dir>       Helm build output directory
  -p, --pkg <deb> [<deb>...] One or more .deb files containing helm charts
  -i, --iso <iso>            ISO file (will mount and scan for helm .deb packages)

Other options:
  -h, --help                 Show this help message

Examples:
  $(basename $0) -d /localdisk/loadbuild/\$USER/\$PROJECT/std/build-helm/stx
  $(basename $0) -p wr-analytics-helm_26.09-0.stx.13_all.deb elastic-helm-charts_1.0-0.stx.15_all.deb
  $(basename $0) -i bootimage.iso

EOF
    local found=0
    for d in /localdisk/loadbuild/${USER}/*/std/build-helm/stx; do
        if [ -d "${d}/usr/lib/fluxcd" ] || [ -d "${d}/usr/lib/helm" ]; then
            if [ "${found}" -eq 0 ]; then
                echo "Detected helm build outputs:" >&2
            fi
            echo "  $d" >&2
            found=1
        fi
    done
    exit 1
}

# -----------------------------------------------------------------------
# extract_image_refs <yaml-file> <chart-name>
#   Parse a yaml file for image references, emit: repo\ttag\tchart
# -----------------------------------------------------------------------
extract_image_refs() {
    local f="$1" chart="$2"
    awk -v chart="${chart}" '
    {
        sub(/#.*/, "")
        sub(/^[[:space:]]+/, "")
        gsub(/"/, "")
    }

    # inline image:tag on any key (e.g. "image: repo/img:tag" or "ks_user: repo/img:tag")
    # Match: <key>: <value-with-slash-and-colon>
    # Require "/" to distinguish from non-image values
    /^[a-zA-Z_][a-zA-Z0-9_]*: .+\/.+:.+/ {
        val = $2
        # skip URLs (http:// https://)
        if (val ~ /^https?:/) { next }
        n = split(val, parts, ":")
        if (n >= 2) {
            tag = parts[n]
            repo = parts[1]
            for (i = 2; i < n; i++) repo = repo ":" parts[i]
            # skip if tag looks like a path (contains /)
            if (tag !~ /\//) {
                print repo "\t" tag "\t" chart
            }
        }
        pending_key = ""
        next
    }

    /^image: .+\/.+/ {
        pending_key = "image"
        pending_val = $2
        next
    }
    /^image: [a-z]/ {
        pending_key = "image"
        pending_val = $2
        next
    }
    /^imageTag: / {
        if (pending_key == "image") {
            print pending_val "\t" $2 "\t" chart
        }
        pending_key = ""
        next
    }
    /^repository: / {
        pending_key = "repository"
        pending_val = $2
        next
    }
    /^tag: / && pending_key == "repository" {
        print pending_val "\t" $2 "\t" chart
        pending_key = ""
        next
    }
    ' "$f"
}

# -----------------------------------------------------------------------
# process_fluxcd_dir <dir>
#   Scan for *-static-overrides.yaml and extract image refs.
#   Appends " (override)" to chart name.
# -----------------------------------------------------------------------
process_fluxcd_dir() {
    local dir="$1"
    find "${dir}" -name '*-static-overrides.yaml' -print0 2>/dev/null | sort -z | \
    while IFS= read -r -d '' f; do
        local chart
        chart="$(basename "$(dirname "$f")")"
        extract_image_refs "$f" "${chart} (override)"
    done
}

# -----------------------------------------------------------------------
# process_helm_tgz_dir <dir>
#   Extract values.yaml from each .tgz chart and parse image refs.
# -----------------------------------------------------------------------
process_helm_tgz_dir() {
    local dir="$1"
    local tgz chart
    for tgz in "${dir}"/*.tgz; do
        [ -f "${tgz}" ] || continue
        chart=$(basename "${tgz}" | sed 's/-[0-9].*//') # strip version
        tar -xzf "${tgz}" -O --wildcards '*/values.yaml' 2>/dev/null | \
            awk -v chart="${chart}" '
            {
                sub(/#.*/, "")
                sub(/^[[:space:]]+/, "")
                gsub(/"/, "")
            }
            /^image: .+\/.+:.+/ {
                val = $2
                n = split(val, parts, ":")
                if (n >= 2) {
                    tag = parts[n]; repo = parts[1]
                    for (i = 2; i < n; i++) repo = repo ":" parts[i]
                    print repo "\t" tag "\t" chart " (chart-default)"
                }
                pending_key = ""; next
            }
            /^image: .+\/.+/ { pending_key = "image"; pending_val = $2; next }
            /^image: [a-z]/  { pending_key = "image"; pending_val = $2; next }
            /^imageTag: /    {
                tag = $2
                if (pending_key == "image") {
                    if (tag == "") tag = "<no-tag>"
                    print pending_val "\t" tag "\t" chart " (chart-default)"
                }
                pending_key = ""; next
            }
            /^repository: /  { pending_key = "repository"; pending_val = $2; next }
            /^tag: / && pending_key == "repository" {
                tag = $2
                if (tag == "") tag = "<no-tag>"
                print pending_val "\t" tag "\t" chart " (chart-default)"
                pending_key = ""; next
            }
            '
    done
}

# -----------------------------------------------------------------------
# process_helm_dir <base-dir>
#   Scan both usr/lib/fluxcd/ and usr/lib/helm/ under base-dir.
# -----------------------------------------------------------------------
process_helm_dir() {
    local base="$1"
    [ -d "${base}/usr/lib/fluxcd" ] && process_fluxcd_dir "${base}/usr/lib/fluxcd"
    [ -d "${base}/usr/lib/helm" ]   && process_helm_tgz_dir "${base}/usr/lib/helm"
}

# -----------------------------------------------------------------------
# process_deb <deb-file>
#   Extract fluxcd overrides and helm tgz charts from a .deb.
# -----------------------------------------------------------------------
process_deb() {
    local deb="$1"
    local tmpdir
    tmpdir=$(mktemp -d)

    # Extract both fluxcd overrides and helm tgz charts in one pass
    dpkg-deb --fsys-tarfile "${deb}" | \
        tar -C "${tmpdir}" -xf - \
            --wildcards '*/fluxcd/*-static-overrides.yaml' '*/helm/*.tgz' 2>/dev/null

    local found=0
    if [ -d "${tmpdir}/usr/lib/fluxcd" ]; then
        process_fluxcd_dir "${tmpdir}/usr/lib/fluxcd"
        found=1
    fi
    if [ -d "${tmpdir}/usr/lib/helm" ]; then
        process_helm_tgz_dir "${tmpdir}/usr/lib/helm"
        found=1
    fi

    if [ "${found}" -eq 0 ]; then
        echo "Warning: no helm content found in $(basename ${deb})" >&2
    fi

    rm -rf "${tmpdir}"
}

# -----------------------------------------------------------------------
# process_iso <iso-file>
#   Mount ISO, find helm .deb packages, extract and parse.
# -----------------------------------------------------------------------
process_iso() {
    local iso="$1"
    local mntdir
    mntdir=$(mktemp -d)
    USED_SUDO=""

    if ! mount -o loop,ro "${iso}" "${mntdir}" 2>/dev/null; then
        if ! sudo mount -o loop,ro "${iso}" "${mntdir}" 2>/dev/null; then
            echo "Error: failed to mount ${iso} (try running with sudo)" >&2
            rmdir "${mntdir}"
            return 1
        fi
        USED_SUDO=1
    fi

    echo "Mounted ${iso} at ${mntdir}" >&2

    local found_content=0

    # 1. Direct .deb files on the ISO
    while IFS= read -r -d '' deb; do
        # Check if deb has any helm content
        if dpkg-deb --fsys-tarfile "${deb}" 2>/dev/null | \
           tar -tf - --wildcards '*/fluxcd/*-static-overrides.yaml' '*/helm/*.tgz' >/dev/null 2>&1; then
            echo "  Found helm content in: $(basename ${deb})" >&2
            process_deb "${deb}"
            found_content=1
        fi
    done < <(find "${mntdir}" -name '*.deb' -print0 2>/dev/null)

    # 2. Check ostree repo
    if [ -d "${mntdir}/ostree_repo" ]; then
        local ref
        ref=$(ostree --repo="${mntdir}/ostree_repo" refs 2>/dev/null | head -1)
        if [ -n "${ref}" ]; then
            # 2a. App tarballs at /usr/local/share/applications/helm/*.tgz
            #     Each contains fluxcd-manifests/ and charts/ directories.
            local app_tgz_list
            app_tgz_list=$(ostree --repo="${mntdir}/ostree_repo" ls -R starlingx \
                /usr/local/share/applications/helm 2>/dev/null | \
                awk '/\.tgz$/ {print $NF}')
            if [ -n "${app_tgz_list}" ]; then
                echo "  Found application helm tarballs in ostree" >&2
                local app_tmpdir
                app_tmpdir=$(mktemp -d)
                while IFS= read -r app_path; do
                    local app_name
                    app_name=$(basename "${app_path}" .tgz)
                    echo "    Extracting: ${app_name}" >&2
                    local app_extract="${app_tmpdir}/${app_name}"
                    mkdir -p "${app_extract}"
                    ostree --repo="${mntdir}/ostree_repo" cat starlingx "${app_path}" 2>/dev/null | \
                        tar -xzf - -C "${app_extract}" 2>/dev/null
                    # Process fluxcd-manifests inside the app tarball
                    if [ -d "${app_extract}/fluxcd-manifests" ]; then
                        process_fluxcd_dir "${app_extract}/fluxcd-manifests"
                        found_content=1
                    fi
                    # Process charts inside the app tarball
                    if [ -d "${app_extract}/charts" ]; then
                        process_helm_tgz_dir "${app_extract}/charts"
                        found_content=1
                    fi
                done <<< "${app_tgz_list}"
                rm -rf "${app_tmpdir}"
            fi

            # 2b. Direct fluxcd/helm content in ostree rootfs
            if [ "${found_content}" -eq 0 ]; then
                echo "  Checking ostree rootfs for helm content..." >&2
                local ostree_tmpdir
                ostree_tmpdir=$(mktemp -d)
                ostree --repo="${mntdir}/ostree_repo" checkout --union "${ref}" "${ostree_tmpdir}" 2>/dev/null
                if [ -d "${ostree_tmpdir}/usr/lib/fluxcd" ] || [ -d "${ostree_tmpdir}/usr/lib/helm" ]; then
                    process_helm_dir "${ostree_tmpdir}"
                    found_content=1
                fi
                rm -rf "${ostree_tmpdir}"
            fi
        fi
    fi

    if [ "${found_content}" -eq 0 ]; then
        echo "Warning: no helm chart content found in ISO" >&2
    fi

    if [ -n "${USED_SUDO}" ]; then
        sudo umount "${mntdir}"
    else
        umount "${mntdir}" 2>/dev/null || sudo umount "${mntdir}"
    fi
    rmdir "${mntdir}"
}

# -----------------------------------------------------------------------
# format_report <source-label>
# -----------------------------------------------------------------------
format_report() {
    local source="$1"

    aggregate() {
        local file="$1"
        [ -s "$file" ] || return
        awk -F'\t' '
        {
            key = $1 "\t" $2
            if (!(key in charts)) {
                charts[key] = $3
                order[++n] = key
            } else if (index(charts[key], $3) == 0) {
                charts[key] = charts[key] ", " $3
            }
        }
        END {
            for (i = 1; i <= n; i++) {
                split(order[i], kv, "\t")
                print kv[1] "\t" kv[2] "\t" charts[order[i]]
            }
        }' "$file"
    }

    strip_registry() {
        echo "$1" | sed 's|.*/wind-river/|wind-river/|'
    }

    echo ""
    echo "=== Docker Image Tag Report ==="
    echo "Source: ${source}"
    echo ""

    if [ -s "${WR_FILE}" ]; then
        echo "WR/Custom Images (registry: admin-2.cumulus.wrs.com:30093)"
        echo ""
        printf "  %-40s %-50s %s\n" "IMAGE" "TAG" "CHARTS"
        printf "  %-40s %-50s %s\n" "-----" "---" "------"
        aggregate "${WR_FILE}" | while IFS=$'\t' read -r img tag charts; do
            printf "  %-40s %-50s %s\n" "$(strip_registry "${img}")" "${tag}" "${charts}"
        done
        echo ""
    fi

    if [ -s "${UP_FILE}" ]; then
        echo "Upstream/Third-Party Images"
        echo ""
        printf "  %-60s %-20s %s\n" "IMAGE" "TAG" "CHARTS"
        printf "  %-60s %-20s %s\n" "-----" "---" "------"
        aggregate "${UP_FILE}" | while IFS=$'\t' read -r img tag charts; do
            printf "  %-60s %-20s %s\n" "${img}" "${tag}" "${charts}"
        done
        echo ""
    fi
}

# -----------------------------------------------------------------------
# classify - split stdin into WR_FILE and UP_FILE
# -----------------------------------------------------------------------
classify() {
    while IFS=$'\t' read -r repo tag chart; do
        if [[ "${repo}" == *"wind-river"* ]] || [[ "${repo}" == *"cumulus"* ]]; then
            printf '%s\t%s\t%s\n' "${repo}" "${tag}" "${chart}" >> "${WR_FILE}"
        else
            printf '%s\t%s\t%s\n' "${repo}" "${tag}" "${chart}" >> "${UP_FILE}"
        fi
    done
}

# =======================================================================
# Main
# =======================================================================

MODE=""
HELM_DIR=""
DEB_FILES=()
ISO_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--helm-dir)
            [ -z "$2" ] && usage
            MODE="dir"
            HELM_DIR="$2"
            shift 2
            ;;
        -p|--pkg)
            MODE="pkg"
            shift
            while [ $# -gt 0 ] && [[ "$1" != -* ]]; do
                DEB_FILES+=("$1")
                shift
            done
            [ ${#DEB_FILES[@]} -eq 0 ] && usage
            ;;
        -i|--iso)
            [ -z "$2" ] && usage
            MODE="iso"
            ISO_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

[ -z "${MODE}" ] && usage

WR_FILE=$(mktemp)
UP_FILE=$(mktemp)
trap "rm -f ${WR_FILE} ${UP_FILE}" EXIT

case "${MODE}" in
    dir)
        if [ ! -d "${HELM_DIR}/usr/lib/fluxcd" ] && [ ! -d "${HELM_DIR}/usr/lib/helm" ]; then
            echo "Error: no usr/lib/fluxcd/ or usr/lib/helm/ found under ${HELM_DIR}" >&2
            echo "" >&2
            if [ -d "${HELM_DIR}" ]; then
                echo "Directory exists but contains no helm content:" >&2
                ls "${HELM_DIR}" >&2
            else
                echo "Directory does not exist: ${HELM_DIR}" >&2
            fi
            echo "" >&2
            echo "Likely paths:" >&2
            _found=0
            for d in /localdisk/loadbuild/${USER}/*/std/build-helm/stx; do
                if [ -d "${d}/usr/lib/fluxcd" ] || [ -d "${d}/usr/lib/helm" ]; then
                    echo "  $d" >&2
                    _found=1
                fi
            done
            if [ "${_found}" -eq 0 ]; then
                echo "  /localdisk/loadbuild/\$USER/\$PROJECT/std/build-helm/stx" >&2
            fi
            exit 1
        fi
        process_helm_dir "${HELM_DIR}" | sort -u | classify
        format_report "${HELM_DIR}"
        ;;
    pkg)
        SOURCE=""
        for deb in "${DEB_FILES[@]}"; do
            if [ ! -f "${deb}" ]; then
                echo "Error: ${deb} not found" >&2
                exit 1
            fi
            SOURCE="${SOURCE:+${SOURCE}, }$(basename ${deb})"
        done
        for deb in "${DEB_FILES[@]}"; do
            process_deb "${deb}"
        done | sort -u | classify
        format_report "${SOURCE}"
        ;;
    iso)
        if [ ! -f "${ISO_FILE}" ]; then
            echo "Error: ${ISO_FILE} not found" >&2
            exit 1
        fi
        process_iso "${ISO_FILE}" | sort -u | classify
        format_report "$(basename ${ISO_FILE})"
        ;;
esac
