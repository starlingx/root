#!/usr/bin/python3

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright (C) 2021-2022 Wind River Systems,Inc

# import apt_pkg
# import argparse
# import debrepack
# import discovery
# import fnmatch
# import glob
# import logging
import os
# import pathlib
# import repo_manage
import shutil
# import signal
import subprocess
# import sys
# import utils


apt_rootdir = '/usr/local/apt-chroot'

apt_conf_content = f"""\
Dir "/";
Dir::State "/var/lib/apt";
Dir::State::Lists "/var/lib/apt/lists";
Dir::Cache "/var/cache/apt";
Dir::Cache::Archives "/var/cache/apt/archives";
Dir::Etc "/etc/apt";
Dir::Etc::Trusted "/etc/apt/trusted.gpg";
Dir::Etc::TrustedParts "/etc/apt/trusted.gpg.d";
Acquire::Check-Valid-Until "false";
"""

apt_conf_chroot_content = f"""\
Dir "{apt_rootdir}";
Dir::State "{apt_rootdir}/var/lib/apt";
Dir::State::Lists "{apt_rootdir}/var/lib/apt/lists";
Dir::Cache "{apt_rootdir}/var/cache/apt";
Dir::Cache::Archives "{apt_rootdir}/var/cache/apt/archives";
Dir::Etc "{apt_rootdir}/etc/apt";
Dir::Etc::Trusted "{apt_rootdir}/etc/apt/trusted.gpg";
Dir::Etc::TrustedParts "{apt_rootdir}/etc/apt/trusted.gpg.d";
Acquire::Check-Valid-Until "false";
"""

def run_in_chroot(cmd):
    """Run a shell command inside the chroot"""
    full_cmd = ["sudo", "chroot", apt_rootdir] + cmd
    print(f"Running inside chroot: {' '.join(cmd)}")
    subprocess.run(full_cmd, check=True)

def run_in_chroot_capture(cmd):
    """Run command in chroot and capture output"""
    full_cmd = ["sudo", "chroot", apt_rootdir] + cmd
    result = subprocess.run(full_cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()

def kill_gpg_agent_in_chroot():
    try:
        run_in_chroot(["pkill", "-u", "root", "gpg-agent"])
        run_in_chroot(["pkill", "-u", "root", "dirmngr"])
        print("gpg-agent inside chroot killed.")
    except subprocess.CalledProcessError:
        print("No gpg-agent process found inside chroot (or already exited).")

def extract_and_split_keys_in_chroot(combined_keyring, output_dir):
    # Temporary keyring inside chroot
    temp_keyring = "/tmp/temp.gpg"

    # Import combined keyring into temp keyring
    run_in_chroot([
        "gpg", "--no-default-keyring",
        "--keyring", temp_keyring,
        "--import", combined_keyring
    ])

    # List keys from temp keyring
    output = run_in_chroot_capture([
        "gpg", "--no-default-keyring",
        "--keyring", temp_keyring,
        "--list-keys", "--with-colons"
    ])

    # Extract key fingerprints from 'pub' lines
    key_ids = [line.split(":")[4] for line in output.splitlines() if line.startswith("pub")]

    for keyid in key_ids:
        keyfile = os.path.join(output_dir, f"{keyid}.gpg")
        if os.path.isfile(f"{apt_rootdir}{keyfile}"):
            continue
        print(f"Exporting key {keyid} to {keyfile} inside chroot...")
        # Export key and write to file inside chroot
        with open(f"{apt_rootdir}{keyfile}", "wb") as f:
            export_proc = subprocess.run(
                ["sudo", "chroot", apt_rootdir,
                 "gpg", "--no-default-keyring",
                 "--keyring", temp_keyring,
                 "--export", keyid],
                check=True, stdout=subprocess.PIPE
            )
            f.write(export_proc.stdout)
        os.chmod(f"{apt_rootdir}{keyfile}", 0o644)

    # Clean up temp keyring file inside chroot
    run_in_chroot(["rm", "-f", temp_keyring])
    kill_gpg_agent_in_chroot()
    print("Key export inside chroot complete.")

def combine_keys_in_chroot():
    # Temporary keyring inside chroot
    temp_keyring = "/tmp/temp.gpg"
    combined_keyring = "/etc/apt/trusted.gpg"
    partial_keyring_dir = "/etc/apt/trusted.gpg.d"

    # Clean up temp keyring file inside chroot
    run_in_chroot(["rm", "-f", temp_keyring])

    # Import combined keyring into temp keyring
    run_in_chroot([
        "bash", "-c",
        "for f in /etc/apt/trusted.gpg.d/*.gpg; do " +
        "gpg --no-default-keyring --keyring $f --export >> " + temp_keyring + "; " +
        "done"
    ])

    # Copy temp keyring into legacy location for apt unified key
    run_in_chroot(["mv", "-f", temp_keyring, combined_keyring])

    # Set ownership of combined keyring
    run_in_chroot(["chown", "root:root", "-R", combined_keyring, partial_keyring_dir])

    # make sure there is no gpg_agent process left running in chroot that might prevent umount
    kill_gpg_agent_in_chroot()
    print("Key preperation inside chroot complete.")

def create_apt_chroot():
    # Step 0: clean previous chroot
    if os.path.exists(apt_rootdir):
        for path in [ "/proc", ]:
            mount_path = os.path.join(apt_rootdir, path.lstrip("/"))
            if not os.path.ismount(mount_path):
               subprocess.run([
                  "sudo", "mount", "-o", "bind", path, os.path.join(apt_rootdir, path.lstrip("/"))
               ], check=True)

        kill_gpg_agent_in_chroot()

        for path in [
            "/proc",
            "/sys",
            "/dev/pts",
            "/dev",
            "/etc/resolv.conf"
            ]:
            mount_path = os.path.join(apt_rootdir, path.lstrip("/"))
            if os.path.ismount(mount_path):
                subprocess.run([
                    "sudo", "umount", mount_path
                ], check=True)

        for path in [
            "var/lib/apt/lists/partial",
            "var/cache/apt/archives/partial"
            ]:
            path = os.path.join(apt_rootdir, path.lstrip("/"))
            if os.path.exists(path):
                subprocess.run([
                    "sudo", "chown", str(os.getuid()), path
                ], check=True)

    # Step 1: If missing apt_rootdir (running on an old container) then run debootstrap
    if not os.path.exists(apt_rootdir):
        os.makedirs(apt_rootdir)
        subprocess.run([
            "fakeroot", "debootstrap", "--variant=minbase", "--include=ca-certificates,debian-archive-keyring,gnupg,procps", "--foreign", DIST_CODENAME, apt_rootdir, "http://deb.debian.org/debian"
        ], check=True)

    # Step 2: Copy current system's APT sources into chroot
    etc_apt = "/etc/apt"
    key_src_dir = "/usr/share/keyrings"
    key_dst_dir = "/etc/apt/trusted.gpg.d"
    chroot_etc_apt = os.path.join(apt_rootdir, etc_apt.lstrip('/'))

    for path in [
        "var/lib/apt/lists/partial",
        "var/cache/apt/archives/partial",
        "etc/apt"
        ]:
        subprocess.run([
            "sudo", "mkdir", "-p", os.path.join(apt_rootdir, path.lstrip("/"))
        ], check=True)

    for path in [
        etc_apt,
        key_dst_dir
        ]:
        subprocess.run([
            "sudo", "chown", str(os.getuid()), os.path.join(apt_rootdir, path.lstrip("/"))
        ], check=True)

    # Copy /etc/apt/sources.list
    if os.path.exists("/etc/apt/sources.list"):
        shutil.copy("/etc/apt/sources.list", chroot_etc_apt)

    # Copy /etc/apt/sources.list.d/
    src_list_d = "/etc/apt/sources.list.d"
    dest_list_d = os.path.join(chroot_etc_apt, "sources.list.d")
    if os.path.exists(src_list_d):
        shutil.copytree(src_list_d, dest_list_d, dirs_exist_ok=True)

    # Create apt.conf for the non-standard location
    apt_config = os.path.join(chroot_etc_apt, 'apt.conf')
    apt_config_chroot = os.path.join(chroot_etc_apt, 'apt_chroot.conf')
    apt_config_d = os.path.join(etc_apt, 'apt.conf.d')
    apt_source_d = os.path.join(etc_apt, 'sources.list.d')

    with open(apt_config, "w") as f:
        f.write(apt_conf_content)
    with open(apt_config_chroot, "w") as f:
        f.write(apt_conf_chroot_content)

    for path in [
        "/proc",
        "/sys",
        "/dev",
        "/etc/resolv.conf"
        ]:
        subprocess.run([
           "sudo", "mount", "-o", "bind", path, os.path.join(apt_rootdir, path.lstrip("/"))
        ], check=True)

    subprocess.run([
        "sudo", "mount", "-t", "devpts", "devpts", os.path.join(apt_rootdir, "dev/pts")
    ], check=True)

    run_in_chroot(["rm", "-rf", apt_config_d])
    run_in_chroot(["rm", "-rf", apt_source_d])

    missing_keys = [ "debian-archive-keyring.gpg", "debian-archive-removed-keys.gpg" ]
    for missing_key in missing_keys:
        key_src = os.path.join(apt_rootdir, key_src_dir.lstrip('/'), missing_key)
        key_dst = os.path.join(apt_rootdir, key_dst_dir.lstrip('/'), missing_key)
        if not os.path.isfile(key_dst) and os.path.isfile(key_src):
            shutil.copy2(key_src, key_dst)

        extract_and_split_keys_in_chroot(os.path.join('/', key_dst_dir, missing_key),
                                         os.path.join('/', key_dst_dir))
    combine_keys_in_chroot()

    os.environ['APT_CONFIG'] = apt_config
    os.environ["DIR"] = apt_rootdir
    os.environ["DIR::State"] = os.path.join(apt_rootdir, "var/lib/apt")
    os.environ["DIR::Cache"] = os.path.join(apt_rootdir, "var/cache/apt")
    os.environ["DIR::Etc"] = os.path.join(apt_rootdir, "etc/apt")

    print("Chroot created and APT sources synced.")

def apt_update_inside_chroot():
    subprocess.run([
        "sudo", "chroot", apt_rootdir, "apt-get", "update"
    ])


