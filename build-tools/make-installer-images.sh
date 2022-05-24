#!/bin/bash -e
## this script is called by "update-pxe-network-installer" and run in "sudo"
## created by Yong Hu (yong.hu@intel.com), 05/24/2018

function clean_rootfs {
    rootfs_dir=$1
    echo "--> remove old files in original rootfs"
    conf="$(ls ${rootfs_dir}/etc/ld.so.conf.d/kernel-*.conf)"
    echo "conf basename = $(basename $conf)"
    old_version="tbd"
    if [ -f $conf ]; then
        old_version="$(echo $(basename $conf) | rev | cut -d'.' -f2- | rev | cut -d'-' -f2-)"
    fi
    echo "old version is $old_version"
    # remove old files in original initrd.img
    # do this in chroot to avoid accidentialy wrong operations on host root
chroot $rootfs_dir /bin/bash -x <<EOF
    rm -rf ./boot/ ./etc/modules-load.d/
    if [ -n $old_version ] &&  [ -f ./etc/ld.so.conf.d/kernel-${old_version}.conf ]; then
        rm -rf ./etc/ld.so.conf.d/kernel-${old_version}.conf
        rm -rf ./lib/modules/${old_version}
    fi
    if [ -d ./usr/lib64/python2.7/site-packages/pyanaconda/ ];then
            rm -rf usr/lib64/python2.7/site-packages/pyanaconda/
        fi
        if [ -d ./usr/lib64/python2.7/site-packages/rpm/ ];then
            rm -rf usr/lib64/python2.7/site-packages/rpm/
        fi
        #find old .pyo files and delete them
        all_pyo="`find ./usr/lib64/python2.7/site-packages/pyanaconda/ usr/lib64/python2.7/site-packages/rpm/ -name *.pyo`"
        if [ -n $all ]; then
            for pyo in $all_pyo;do
                rm -f $pyo
            done
        fi
        exit
EOF
    #back to previous folder
}


echo "This script makes new initrd.img, vmlinuz and squashfs.img."
echo "NOTE: it has to be executed with *root*!"

if [ $# -lt 1 ];then
    echo "$0 <work_dir>"
    exit -1;
fi

work_dir=$1
output_dir=$work_dir/output
if [ ! -d $output_dir ]; then
    mkdir -p $output_dir;
fi

timestamp=$(date +%F_%H%M)

echo "---------------- start to make new initrd.img and vmlinuz -------------"
ORIG_INITRD=$work_dir/orig/initrd.img
if [ ! -f $ORIG_INITRD ];then
    echo "ERROR: $ORIG_INITRD does NOT exist!"
    exit -1
fi

kernel_rpms_dir=$work_dir/kernel-rpms
if [ ! -d $kernel_rpms_dir ];then
    echo "ERROR: $kernel_rpms_dir does NOT exist!"
    exit -1
fi

firmware_rpms_dir=${work_dir}/firmware-rpms
if [ ! -d ${firmware_rpms_dir} ];then
    echo "ERROR: ${firmware_rpms_dir} does NOT exist!"
    exit -1
fi
firmware_list_file=${work_dir}/firmware-list


initrd_root=$work_dir/initrd.work
if [ -d $initrd_root ];then
    rm -rf $initrd_root
fi
mkdir -p $initrd_root

cd $initrd_root
# uncompress initrd.img
echo "--> uncompress original initrd.img"
/usr/bin/xzcat $ORIG_INITRD | cpio -i

echo "--> clean up $initrd_root"
clean_rootfs $initrd_root

echo "--> extract files from new kernel and its modular rpms to initrd root"
for kf in ${kernel_rpms_dir}/std/*.rpm ; do rpm2cpio $kf | cpio -idu; done

echo "--> extract files from new firmware rpms to initrd root"
if [ -f ${firmware_list_file} ]; then
    echo "--> extract files from new firmware rpm to initrd root"
    firmware_list=`cat ${firmware_list_file}`
    for fw in ${firmware_rpms_dir}/std/*.rpm ; do rpm2cpio ${fw} | cpio -iduv ${firmware_list}; done
fi

# by now new kernel and its modules exist!
# find new kernel in /boot/vmlinuz-* or /lib/modules/*/vmlinuz
echo "--> get new kernel image: vmlinuz"
new_kernel="$(ls ./boot/vmlinuz-* 2>/dev/null || ls ./lib/modules/*/vmlinuz 2>/dev/null || true)"
echo "New kernel: \"${new_kernel}\""
if [ -f "${new_kernel}" ];then
    # copy out the new kernel
    if [ -f $output_dir/new-vmlinuz ]; then
        mv -f $output_dir/new-vmlinuz $output_dir/vmlinuz-backup-$timestamp
    fi
    cp -f $new_kernel $output_dir/new-vmlinuz

    if echo "${new_kernel}" | grep -q '^\./boot/vmlinuz'; then
        kernel_name=$(basename $new_kernel)
        new_ver=$(echo $kernel_name | cut -d'-' -f2-)
        system_map="boot/System.map-${new_ver}"
    elif echo "${new_kernel}" | grep -q '^\./lib/modules/'; then
        new_ver="$(echo "${new_kernel}" | sed 's#^\./lib/modules/\([^/]\+\)/.*$#\1#')"
        system_map="lib/modules/${new_ver}/System.map"
    else
        echo "Unrecognized new kernel path: ${new_kernel}"
        exit -1
    fi

    if [ -z "${new_ver}" ]; then
        echo "Could not determine new kernel version"
        exit -1
    fi

    echo "New kernel version: ${new_ver}"

    if ! [ -f "${system_map}" ]; then
        echo "Could not find System.map file at: ${system_map}"
        exit -1
    fi
else
    echo "ERROR: new kernel is NOT found!"
    exit -1
fi

echo "-->check module dependencies in new initrd.img in chroot context"
chroot $initrd_root /bin/bash -x <<EOF
/usr/sbin/depmod -aeF "/${system_map}" "$new_ver"
if [ $? == 0 ]; then echo "module dependencies are satisfied!" ; fi
## Remove the biosdevname package!
rm -f ./usr/lib/udev/rules.d/71-biosdevname.rules ./usr/sbin/biosdevname
exit
EOF

echo "-->patch usr/lib/net-lib.sh with IPv6 improvements from newer dracut"
patch usr/lib/net-lib.sh <<EOF
--- ../initrd.orig/usr/lib/net-lib.sh   2020-08-18 19:37:17.063163840 -0400
+++ usr/lib/net-lib.sh  2020-08-19 09:47:15.237089800 -0400
@@ -645,7 +645,8 @@
     timeout=\$((\$timeout*10))

     while [ \$cnt -lt \$timeout ]; do
-        [ -z "\$(ip -6 addr show dev "\$1" scope link tentative)" ] \\
+        [ -n "\$(ip -6 addr show dev "\$1" scope link)" ] \\
+            && [ -z "\$(ip -6 addr show dev "\$1" scope link tentative)" ] \\
             && return 0
         [ -n "\$(ip -6 addr show dev "\$1" scope link dadfailed)" ] \\
             && return 1
@@ -662,7 +663,9 @@
     timeout=\$((\$timeout*10))

     while [ \$cnt -lt \$timeout ]; do
-        [ -z "\$(ip -6 addr show dev "\$1" tentative)" ] \\
+        [ -n "\$(ip -6 addr show dev "\$1")" ] \\
+            && [ -z "\$(ip -6 addr show dev "\$1" tentative)" ] \\
+            && [ -n "\$(ip -6 route list proto ra dev "\$1" | grep ^default)" ] \\
             && return 0
         [ -n "\$(ip -6 addr show dev "\$1" dadfailed)" ] \\
             && return 1
@@ -679,8 +682,9 @@
     timeout=\$((\$timeout*10))

     while [ \$cnt -lt \$timeout ]; do
-        [ -z "\$(ip -6 addr show dev "\$1" tentative)" ] \\
-            && [ -n "\$(ip -6 route list proto ra dev "\$1")" ] \\
+        [ -n "\$(ip -6 addr show dev "\$1")" ] \\
+            && [ -z "\$(ip -6 addr show dev "\$1" tentative)" ] \\
+            && [ -n "\$(ip -6 route list proto ra dev "\$1" | grep ^default)" ] \\
             && return 0
         sleep 0.1
         cnt=\$((\$cnt+1))
EOF

echo "-->patch usr/lib/dracut/hooks/pre-trigger/03-lldpad.sh with rd.fcoe disabling support"
patch usr/lib/dracut/hooks/pre-trigger/03-lldpad.sh <<EOF
--- ../initrd.orig/usr/lib/dracut/hooks/pre-trigger/03-lldpad.sh	2021-05-12 16:32:44.007007124 -0400
+++ usr/lib/dracut/hooks/pre-trigger/03-lldpad.sh	2021-05-12 16:35:31.321509139 -0400
@@ -1,5 +1,10 @@
 #!/bin/bash
 
+if ! getargbool 0 rd.fcoe -d -n rd.nofcoe; then
+    info "rd.fcoe=0: skipping lldpad activation"
+    return 0
+fi
+
 # Note lldpad will stay running after switchroot, the system initscripts
 # are to kill it and start a new lldpad to take over. Data is transfered
 # between the 2 using a shm segment
EOF

echo "-->patch usr/lib/dracut/hooks/cmdline/99-parse-fcoe.sh with rd.fcoe disabling support"
patch usr/lib/dracut/hooks/cmdline/99-parse-fcoe.sh <<EOF
--- ../initrd.orig/usr/lib/dracut/hooks/cmdline/99-parse-fcoe.sh	2021-05-12 16:32:44.008007121 -0400
+++ usr/lib/dracut/hooks/cmdline/99-parse-fcoe.sh	2021-05-12 16:36:56.874254504 -0400
@@ -20,6 +20,10 @@
 # If it's not set we don't continue
 [ -z "$fcoe" ] && return
 
+if ! getargbool 0 rd.fcoe -d -n rd.nofcoe; then
+    info "rd.fcoe=0: skipping fcoe"
+    return 0
+fi
 
 # BRCM: Later, should check whether bnx2x is loaded first before loading bnx2fc so do not load bnx2fc when there are no Broadcom adapters
 [ -e /sys/bus/fcoe/ctlr_create ] || modprobe -b -a fcoe || die "FCoE requested but kernel/initrd does not support FCoE"
EOF

echo "--> Rebuild the initrd"
if [ -f $output_dir/new-initrd.img ]; then
    mv -f $output_dir/new-initrd.img $output_dir/initrd.img-backup-$timestamp
fi
find . | cpio -o -H newc | xz --check=crc32 --x86 --lzma2=dict=512KiB > $output_dir/new-initrd.img
if [ $? != 0 ];then
    echo "ERROR: failed to create new initrd.img"
    exit -1
fi

cd $work_dir

if [ -f $output_dir/new-initrd.img ];then
    ls -l $output_dir/new-initrd.img
else
    echo "ERROR: new-initrd.img is not generated!"
    exit -1
fi

if [ -f $output_dir/new-vmlinuz ];then
    ls -l $output_dir/new-vmlinuz
else
    echo "ERROR: new-vmlinuz is not generated!"
    exit -1
fi

echo "---------------- start to make new squashfs.img -------------"
ORIG_SQUASHFS=$work_dir/orig/squashfs.img
if [ ! -f $ORIG_SQUASHFS ];then
    echo "ERROR: $ORIG_SQUASHFS does NOT exist!"
    exit -1
fi

rootfs_rpms_dir=$work_dir/rootfs-rpms
if [ ! -d $rootfs_rpms_dir ];then
    echo "ERROR: $rootfs_rpms_dir does NOT exist!"
    exit -1
fi

# make squashfs.mnt and ready and umounted
if [ ! -d $work_dir/squashfs.mnt ];then
    mkdir -p $work_dir/squashfs.mnt
else
    # in case it was mounted previously
    mnt_path=$(mount | grep "squashfs.mnt" | cut -d' ' -f3-3)
    if [ x"$mnt_path" != "x" ] &&  [ "$(basename $mnt_path)" == "squashfs.mnt" ];then
        umount $work_dir/squashfs.mnt
    fi
fi

# make squashfs.work ready and umounted
squashfs_root="$work_dir/squashfs.work"
# Now mount the rootfs.img file:
if [ ! -d $squashfs_root ];then
    mkdir -p $squashfs_root
else
    # in case it was mounted previously
    mnt_path=$(mount | grep "$(basename $squashfs_root)" | cut -d' ' -f3-3)
    if [ x"$mnt_path" != "x" ] &&  [ "$(basename $mnt_path)" == "$(basename $squashfs_root)" ];then
        umount $squashfs_root
    fi
fi

echo $ORIG_SQUASHFS
mount -o loop -t squashfs $ORIG_SQUASHFS $work_dir/squashfs.mnt

if [ ! -d ./LiveOS ]; then
    mkdir -p ./LiveOS
fi

echo "--> copy rootfs.img from original squashfs.img to LiveOS folder"
cp -f ./squashfs.mnt/LiveOS/rootfs.img ./LiveOS/.

echo "--> done to copy rootfs.img, umount squashfs.mnt"
umount ./squashfs.mnt

echo "--> mount rootfs.img into $squashfs_root"
mount -o loop LiveOS/rootfs.img $squashfs_root

echo "--> clean up ./squashfs-rootfs from original squashfs.img in chroot context"
clean_rootfs $squashfs_root

cd $squashfs_root
echo "--> extract files from rootfs-rpms to squashfs root"
for ff in $rootfs_rpms_dir/*.rpm ; do rpm2cpio $ff | cpio -idu; done

echo "--> extract files from kernel and its modular rpms to squashfs root"
for kf in ${kernel_rpms_dir}/std/*.rpm ; do rpm2cpio $kf | cpio -idu; done

echo "-->check module dependencies in new squashfs.img in chroot context"
#we are using the same new  kernel-xxx.rpm, so the $new_ver is the same
chroot $squashfs_root /bin/bash -x <<EOF
/usr/sbin/depmod -aeF "/${system_map}" "$new_ver"
if [ $? == 0 ]; then echo "module dependencies are satisfied!" ; fi
## Remove the biosdevname package!
rm -f ./usr/lib/udev/rules.d/71-biosdevname.rules ./usr/sbin/biosdevname
exit
EOF

# come back to the original work dir
cd $work_dir

echo "--> unmount $squashfs_root"
umount $squashfs_root
#rename the old version
if [ -f $output_dir/new-squashfs.img ]; then
    mv -f $output_dir/new-squashfs.img $output_dir/squashfs.img-backup-$timestamp
fi

echo "--> make the new squashfs image"
mksquashfs LiveOS $output_dir/new-squashfs.img -keep-as-directory -comp xz -b 1M
if [ $? == 0 ];then
    ls -l $output_dir/new-squashfs.img
else
    echo "ERROR: failed to make a new squashfs.img"
    exit -1
fi

echo "--> done successfully!"
