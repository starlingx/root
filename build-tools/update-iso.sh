#!/bin/bash
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# Utility for updating an ISO
#
# This utility supports the following:
# 1. Provide a custom kickstart post addon, allowing the user
# to add some custom configuration, such as custom network
# interface config
# 2. Add or modify installation boot parameters, such as changing
# the default boot_device and rootfs_device disks
#

function usage {
    cat <<ENDUSAGE
Usage:
   $(basename $0) -i <input bootimage.iso> -o <output bootimage.iso>
                   [ -a <ks-addon.cfg> ] [ -p param=value ]
        -i <file>: Specify input ISO file
        -o <file>: Specify output ISO file
        -a <file>: Specify ks-addon.cfg file
        -p <p=v>:  Specify boot parameter
                   Examples:
                   -p rootfs_device=nvme0n1 -p boot_device=nvme0n1

                   -p rootfs_device=/dev/disk/by-path/pci-0000:00:0d.0-ata-1.0
                   -p boot_device=/dev/disk/by-path/pci-0000:00:0d.0-ata-1.0

Example ks-addon.cfg, to define a VLAN on initial OAM interface setup:
#### start ks-addon.cfg
OAM_DEV=enp0s3
OAM_VLAN=1234

    cat << EOF > /etc/sysconfig/network-scripts/ifcfg-\$OAM_DEV
DEVICE=\$OAM_DEV
BOOTPROTO=none
ONBOOT=yes
LINKDELAY=20
EOF

    cat << EOF > /etc/sysconfig/network-scripts/ifcfg-\$OAM_DEV.\$OAM_VLAN
DEVICE=\$OAM_DEV.\$OAM_VLAN
BOOTPROTO=dhcp
ONBOOT=yes
VLAN=yes
LINKDELAY=20
EOF
#### end ks-addon.cfg

ENDUSAGE
}

function check_requirements {
    local -a required_utils=(
        rsync
        guestmount
        guestunmount
        mkisofs
        isohybrid
        implantisomd5
    )
    local -i missing=0

    for req in ${required_utils[@]}; do
        which ${req} >&/dev/null
        if [ $? -ne 0 ]; then
            echo "Unable to find required utility: ${req}" >&2
            let -i missing++
        fi
    done

    if [ ${missing} -gt 0 ]; then
        echo "One or more required utilities are missing. Aborting..." >&2
        exit 1
    fi
}

function update_parameter {
    local isodir=$1
    local param=$2
    local value=$3

    for f in ${isodir}/isolinux.cfg ${isodir}/syslinux.cfg; do
        grep -q "^[[:space:]]*append\>.*[[:space:]]${param}=" ${f}
        if [ $? -eq 0 ]; then
            # Parameter already exists. Update the value
            sed -i -e "s#^\([[:space:]]*append\>.*${param}\)=[^[:space:]]*#\1=${value}#" ${f}
            if [ $? -ne 0 ]; then
                echo "Failed to update parameter ($param)"
                exit 1
            fi
        else
            # Parameter doesn't exist. Add it to the cmdline
            sed -i -e "s|^\([[:space:]]*append\>.*\)|\1 ${param}=${value}|" ${f}
            if [ $? -ne 0 ]; then
                echo "Failed to add parameter ($param)"
                exit 1
            fi
        fi
    done
}

declare INPUT_ISO=
declare OUTPUT_ISO=
declare ORIG_PWD=$PWD
declare ADDON=
declare -a PARAMS

while getopts "hi:o:a:p:" opt; do
    case $opt in
        i)
            INPUT_ISO=$OPTARG
            ;;
        o)
            OUTPUT_ISO=$OPTARG
            ;;
        a)
            ADDON=$OPTARG
            ;;
        p)
            PARAMS+=(${OPTARG})
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

check_requirements

if [ -z "$INPUT_ISO" -o -z "$OUTPUT_ISO" ]; then
    usage
    exit 1
fi

if [ ! -f ${INPUT_ISO} ]; then
    echo "Input file does not exist: ${INPUT_ISO}"
    exit 1
fi

if [ -f ${OUTPUT_ISO} ]; then
    echo "Output file already exists: ${OUTPUT_ISO}"
    exit 1
fi

shift $((OPTIND-1))

declare MNTDIR=
declare BUILDDIR=
declare WORKDIR=

function cleanup {
    if [ -n "$MNTDIR" -a -d "$MNTDIR" ]; then
        guestunmount $MNTDIR
        \rmdir $MNTDIR
    fi

    if [ -n "$BUILDDIR" -a -d "$BUILDDIR" ]; then
        \rm -rf $BUILDDIR
    fi

    if [ -n "$WORKDIR" -a -d "$WORKDIR" ]; then
        \rm -rf $WORKDIR
    fi
}

trap cleanup EXIT

MNTDIR=$(mktemp -d -p $PWD updateiso_mnt_XXXXXX)
if [ -z "${MNTDIR}" -o ! -d ${MNTDIR} ]; then
    echo "Failed to create mntdir. Aborting..."
    exit $rc
fi

BUILDDIR=$(mktemp -d -p $PWD updateiso_build_XXXXXX)
if [ -z "${BUILDDIR}" -o ! -d ${BUILDDIR} ]; then
    echo "Failed to create builddir. Aborting..."
    exit $rc
fi

# Mount the ISO
guestmount -a ${INPUT_ISO} -m /dev/sda1 --ro ${MNTDIR}
rc=$?
if [ $rc -ne 0 ]; then
    echo "Call to guestmount failed with rc=$rc. Aborting..."
    exit $rc
fi

rsync -a ${MNTDIR}/ ${BUILDDIR}/
rc=$?
if [ $rc -ne 0 ]; then
    echo "Call to rsync ISO content. Aborting..."
    exit $rc
fi

guestunmount ${MNTDIR}
\rmdir ${MNTDIR}

if [ ${#PARAMS[@]} -gt 0 ]; then
    for p in ${PARAMS[@]}; do
        param=${p%%=*} # Strip from the first '=' on
        value=${p#*=}  # Strip to the first '='

        update_parameter ${BUILDDIR} "${param}" "${value}"
    done
fi

if [ -n "${ADDON}" ]; then
    \rm -f ${BUILDDIR}/ks-addon.cfg
    \cp ${ADDON} ${BUILDDIR}/ks-addon.cfg
    if [ $? -ne 0 ]; then
        echo "Error: Failed to copy ${ADDON}"
        exit 1
    fi
fi

# Rebuild the ISO
mkisofs -o ${OUTPUT_ISO} \
    -R -D -A 'oe_iso_boot' -V 'oe_iso_boot' \
    -quiet \
    -b isolinux.bin -c boot.cat -no-emul-boot \
    -boot-load-size 4 -boot-info-table \
    -eltorito-alt-boot \
    -e images/efiboot.img \
    -no-emul-boot \
    ${BUILDDIR}

isohybrid --uefi ${OUTPUT_ISO}
implantisomd5 ${OUTPUT_ISO}

echo "Updated ISO: ${OUTPUT_ISO}"

