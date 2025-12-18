#!/usr/bin/env python3

# Copyright (C) 2024 Wind River Systems,Inc
#
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
# Script for generating a pre-patched ISO matching the USM framework
# (stx 10 or later)
#
# A system installed with a pre-patched ISO will behave the same as a system
# installed with the base ISO with the patches applied.
#
# Requirements:
# - Should be run from inside the LAT container, part of the STX build env,
#   as this requires several env variables and dependencies readily available
#   in it.
# - tools repo present in MY_REPO_ROOT_DIR, as 'base-bullseye.yaml' is used;
#

# This enables usage of "A|B" type hints, which are not available in py39 yet
# This can be removed if LAT is upgraded to py310 or above.
from __future__ import annotations

from typing import Any

import argparse
import glob
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import yaml


# === Logging === #

logger = logging.getLogger(__file__)

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s - %(levelname)-7s]  %(message)s',
                    datefmt='%H:%M:%S')


# === Get env variables === #

# Note all these variables are automatically set inside a LAT container
REQUIRED_ENV_VARIABLES = [
    "HTTP_SERVER_IP",
    "MY_REPO_ROOT_DIR",
    "MYUNAME",
    "PROJECT",
]

HTTP_SERVER_IP = os.environ.get("HTTP_CONTAINER_IP")
MY_REPO_ROOT_DIR = os.environ.get("MY_REPO_ROOT_DIR", default="")
MYUNAME = os.environ.get("MYUNAME")
PROJECT = os.environ.get("PROJECT")


# === Parameters === #

BASE_BULLSEYE_YAML_PATH = os.path.join(
    MY_REPO_ROOT_DIR, "stx-tools", "debian-mirror-tools", "config", "debian",
    "common", "base-bullseye.yaml")

# This is used to help validate content packed into the output ISO
EXPECTED_ISO_CONTENTS = {
    "EFI",
    "bzImage",
    "bzImage.sig",
    "bzImage-rt",
    "bzImage-rt.sig",
    "bzImage-std",
    "bzImage-std.sig",
    "efi.img",
    "images",
    "initrd",
    "initrd.sig",
    "isolinux",
    "kickstart",
    "ostree_repo",
    "patches",
    "pxeboot",
    "upgrades",
}

GPG_HOME = "/tmp/.lat_gnupg_root"
HTTP_FULL_ADDR = f"http://{HTTP_SERVER_IP}:8088"
LAT_SDK_SYSROOT = "/opt/LAT/SDK/sysroots/x86_64-wrlinuxsdk-linux"
PATCHES_FEED_PATH = f"/localdisk/loadbuild/{MYUNAME}/{PROJECT}/patches_feed"

# Some command outputs are very long (e.g.: ostree history).
# Max length before replacing them with "<omitted>" in the log.
MAX_LOG_LENGTH = 2500


# === Exceptions === #

class YamlParsingException(Exception):
    """
    Exception class for errors when trying to get a value from a yaml file.
    """

    def __init__(self, msg, key, file) -> None:
        final_msg = msg
        final_msg += f" Key: '{key}' ;"
        final_msg += f" Yaml File: '{file}'."

        super().__init__(final_msg)


# === Functions === #

def log_message(label: str, text: str) -> str:
    """Organize log message to make it easier to read

    If the message fits a single line, display it as so.
    If the message has multiple lines, better start on the next line in the log
    so that all message lines are aligned.
    """
    if ("\n" in text.strip()):
        return f"{label}\n\"{text}\""

    return f"{label}\"{text}\""


def run_command(cmd: list[str], ignore_errors: bool = False,
                verbose: bool = True) -> str:
    """Execute bash command

    :param cmd: Args which compose the command to execute
    :param ignore_errors: Whether to ignore non-zero return codes or not
    :param verbose: Whether to log the execution results

    :returns: Command execution stdout
    """

    logger.info(f"Running command: {cmd}")

    # Note that the wildcard character (asterisk "*") needs to be put in quotes
    # when used in the CLI to be passed as input to commands. This is to
    # prevent shell from using the wildcard itself instead of forwarding it.
    # But in Python there is no need for quotes. In fact, they shouldn't be
    # used, as asterisk doesn't have any special meaning to it.

    result = subprocess.run(args=cmd, capture_output=True, text=True)

    if result.returncode == 127:
        raise Exception("Command not available. Please install it.")

    if result.returncode != 0:
        logger.error(log_message(" - stdout: ", result.stdout))
        logger.error(log_message(" - stderr: ", result.stderr))
        logger.error(f"RC: {result.returncode}")
        if not ignore_errors:
            raise Exception("Command failed!")
        else:
            logger.warning("Command resulted in non-zero return code!")

    if verbose:
        if len(result.stdout) > MAX_LOG_LENGTH:
            output = "<ommitted_large_output>"
        else:
            output = result.stdout

        logger.debug(log_message(" - stdout: ", output))
        logger.debug(log_message(" - stderr: ", result.stderr))

    return result.stdout


def get_iso_label_from_isolinux_cfg(isolinux_cfg_path: str) -> str:
    """Get ISO label from isolinux.cfg file.

    Open isolunux.cfg file, do a regex search for 'instiso=<value>'
    (where <value> is composed by any ammount of chars, numbers and dashes)
    and return the first match for <value> (Note there may be several matches
    for instiso, but they all seem to have the same value).

    :param isolinux_cfg_path: Full path name to isolinux.cfg file

    :returns: First match for the instiso label value
    """

    logger.info("Getting ISO label...")
    logger.info(f" - isolinux.cfg path: {isolinux_cfg_path}")

    try:
        with open(isolinux_cfg_path, mode="r", encoding="utf-8") as file:
            content = file.read()
    except Exception:
        logger.error("Could not read contents from isolinux.cfg")
        raise

    # Match "instiso=" and a value composed of chars, numbers and dashes
    try:
        first_key_value_match = re.findall(pattern=r"instiso=[\w\d-]+",
                                           string=content)[0]
    except IndexError:
        # Regex search returned no matches. Label not in file.
        raise Exception("Could not find ISO label in isolinux.cfg")

    # Remove the "instiso=" part
    iso_label = first_key_value_match[8:]

    logger.info(f"ISO label: {iso_label}")

    return iso_label


def create_iso(iso_contents_dir: str, iso_label: str, output_iso_path: str,
               expected_iso_contents: set[str] = EXPECTED_ISO_CONTENTS
               ) -> None:
    """Create a new ISO or overwrite existing ISO

    Check if item names in :iso_contents_dir: matches :expected_iso_contents:,
    use 'mkisofs' with specific parameters to create an ISO,
    use 'isohybrid --uefi' to make ISO "EFI bootable" and, lastly,
    use 'implantisomd5' to implant md5sum into the ISO.

    :param iso_contents_dir:
        Path to dir containing files to include in the ISO
    :param iso_label: Value to use as Volume ID
    :param output_iso_path: Path for the output ISO
    :param expected_iso_contents:
        A set with the expected contents from 'ls :iso_contents_dir:'
    """

    logger.info("Generating output ISO...")
    logger.info(f" - ISO label: {iso_label}")

    # Logging ISO contents
    cmd = ["ls", "-l", iso_contents_dir]
    iso_contents_list = run_command(cmd, verbose=False)
    logger.debug(f" - ISO contents to include: {iso_contents_list}")

    # Checking if contents match what is expected
    iso_contents = set(os.listdir(iso_contents_dir))
    if iso_contents != expected_iso_contents:
        logger.warning("Output ISO contents are different than expected")

    # Create the output ISO
    # The parameters are so that the iso is created with eltorito header and
    # with ISO 9660 format. Some parameters are hard-coded,
    # as there is no need to customize them.
    cmd = ["mkisofs",
           "-o", output_iso_path,
           "-A", iso_label,
           "-V", iso_label,
           "-U", "-J",
           "-joliet-long",
           "-r",
           "-iso-level", "2",
           "-b", "isolinux/isolinux.bin",
           "-c", "isolinux/boot.cat",
           "-no-emul-boot",
           "-boot-load-size", "4",
           "-boot-info-table",
           "-eltorito-alt-boot",
           "-eltorito-platform", "0xEF",
           "-eltorito-boot", "efi.img",
           "-no-emul-boot",
           iso_contents_dir]

    run_command(cmd)

    logger.info("Making output ISO EFI bootable...")
    cmd = ["isohybrid", "--uefi", output_iso_path]
    run_command(cmd)

    logger.info("Implanting new checksum (required for ISO9660 image)...")
    cmd = ["implantisomd5", output_iso_path]
    run_command(cmd)

    logger.info(f"Output ISO: {output_iso_path}")


def mount_iso(iso: str, mountpoint: str) -> None:
    """Mount an ISO on a directory

    :param iso_path: ISO path
    :param mount_path: Path to a directory in which to mount the ISO
    """

    logger.info("Mounting ISO...")
    logger.info(f" - ISO: {iso}")
    logger.info(f" - Mountpoint: {mountpoint}")

    # Mount the ISO
    cmd = ["mount", "-o", "loop", iso, mountpoint]
    run_command(cmd)


def unmount_iso(mountpoint: str) -> None:
    """Unmount ISO from mountpoint

    :param mountpoint: Path to directory where an ISO is mounted
    """

    logger.info("Un-mounting ISO...")
    logger.info(f" - Mountpoint: {mountpoint}")

    cmd = ["umount", "-l", mountpoint]
    run_command(cmd)


def get_value_from_yaml(concatenated_key: str,
                        yaml_path: str = BASE_BULLSEYE_YAML_PATH) -> Any:
    """Get value associated to a composed key from a yaml file.

    :param concatenated_key: Key for searching a value. For selecting
        values at higher depths, concatenate keys with a '.' between each key.

    :returns: Value for the key
    """

    with open(yaml_path, mode="r") as stream:
        data = yaml.safe_load(stream)

    keys = concatenated_key.split(".")
    for key in keys:
        try:
            data = data.get(key)

        except AttributeError:
            error_msg = "Invalid key: Tried treating final value as a dict."
            raise YamlParsingException(error_msg, keys, yaml_path)

        if data is None:
            error_msg = "Invalid key: Dict doesn't have a value for key used."
            raise YamlParsingException(error_msg, keys, yaml_path)

    return data


def setup_gpg_client(gpg_home: str = GPG_HOME,
                     lat_sdk_sysroot: str = LAT_SDK_SYSROOT) -> None:
    """Setup GPG client configs

    - Create and setup GPG config folder (GPG_HOME) if it doesn't exist
    - Set GNUPGHOME env variable

    These actions are usually performed automatically by the LAT SDK.

    :param gpg_home: GPG home config directory path
    :param lat_sdk_sysroot: LAT SDK sysroot directory path
    """

    logger.info("Setting up GPG configs...")
    logger.info(f" - GPG home folder: {gpg_home}")

    ostree_gpg_id = get_value_from_yaml("gpg.ostree.gpgid")
    ostree_gpg_key = get_value_from_yaml("gpg.ostree.gpgkey")
    ostree_gpg_pass = get_value_from_yaml("gpg.ostree.gpg_password")

    if os.path.exists(gpg_home):
        logger.info("GPG home already exists.")

    else:
        logger.info("GPG home dir doesn't exist, creating...")

        os.makedirs(gpg_home)

        os.chmod(gpg_home, 0o700)

        os.environ["OECORE_NATIVE_SYSROOT"] = lat_sdk_sysroot

        with open(f"{gpg_home}/gpg-agent.conf", mode="w") as file:
            file.write("allow-loopback-pinentry")

        cmd = ["gpg-connect-agent", "--homedir", gpg_home, "reloadagent",
               "/bye"]
        run_command(cmd)

        cmd = ["gpg", "--homedir", gpg_home, "--import", ostree_gpg_key]
        run_command(cmd)

        cmd = ["gpg", "--homedir", gpg_home, "--list-keys", ostree_gpg_id]
        run_command(cmd)

    cmd = ["gpg", "--homedir", gpg_home, "-o", "/dev/null",
           "-u", f'"{ostree_gpg_id}"', "--pinentry", "loopback",
           "--passphrase", ostree_gpg_pass, "-s", "/dev/null"]
    run_command(cmd)

    os.environ["GNUPGHOME"] = gpg_home


def add_tag_xml(parent: ET.Element, name: str, text: str) -> None:
    """Add tag with text to a parent tag

    Create an XML tag inside another tag with a text inside it.

    :param parent: XML parent tag
    :param name: Name of the tag
    :param text: Text value inside the tag
    """

    tag = ET.SubElement(parent, name)
    tag.text = text


# TODO: Some hardcoded values in this function. These should be put in
#       a constants file.
def update_metadata_info(metadata_xml_path: str, iso_path: str) -> None:
    """Update ISO's metadata

    Update the metadata XML with the ostree commit ID and checksum,
    along with some other adjustments for compatibility with the USM
    patching system.

    :param metadata_xml_path: Metadata XML file path
    :param iso_path: ISO Path
    """

    logger.info("Getting ostree info to add to metadata XML...")

    cmd = f"ostree --repo={iso_path}/ostree_repo rev-parse starlingx"
    commit_id = run_command(cmd.split()).strip()

    repo_history = get_ostree_history(f"{iso_path}/ostree_repo")

    logger.debug("Ostree repo history:\n{repo_history}")

    checksum = re.findall(pattern=r"^ContentChecksum:\s*([\w\d]+)",
                          string=repo_history, flags=re.MULTILINE)[0]

    logger.info("Preparing metadata XML changes...")

    # Load metadata XML
    tree = ET.parse(metadata_xml_path)
    root = tree.getroot()

    element_contents = ET.SubElement(root, "contents")
    element_ostree = ET.SubElement(element_contents, "ostree")
    element_base = ET.SubElement(element_ostree, "base")
    element_commit1 = ET.SubElement(element_ostree, "commit1")

    logger.info("Set: prepatched_iso = Y")
    add_tag_xml(root, "prepatched_iso", "Y")

    logger.info("Set: ostree.number_of_commits = 1")
    add_tag_xml(element_ostree, "number_of_commits", "1")

    logger.info("Set: ostree.base.commit = \"\"")
    add_tag_xml(element_base, "commit", "")

    logger.info("Set: ostree.base.checksum = \"\"")
    add_tag_xml(element_base, "checksum", "")

    logger.info(f"Set: ostree.commit1.commit = '{commit_id}'")
    add_tag_xml(element_commit1, "commit", commit_id)

    logger.info(f"Set: ostree.commit1.checksum = '{checksum}'")
    add_tag_xml(element_commit1, "checksum", checksum)

    # A pre-patched ISO is always Reboot Required
    logger.info("Set: reboot_required = Y")
    element_reboot_required = root.find('reboot_required')
    if element_reboot_required is not None:
        element_reboot_required.text = 'Y'
    else:
        msg = "Patch metadata does not contain 'reboot_required' field"
        raise Exception(msg)

    logger.info("Remove: requires")
    requires = root.find("requires")
    if requires is not None:
        requires.clear()

    logger.info("Saving metadata XML changes...")
    tree.write(metadata_xml_path)


def get_ostree_history(ostree_repo: str, filtered: bool = True) -> str:
    """Get ostree repo history

    Take an ostree repo path and return the ostree history.
    Has an option to filter out the commit messages.

    :param ostree_repo: ostree repo path
    :param filtered: Whether to filter out commit messages

    :returns: ostree repo history
    """

    if not os.path.isdir(ostree_repo):
        raise Exception(f"Ostree repo directory does not exist: {ostree_repo}")

    cmd = f"ostree --repo={ostree_repo} log starlingx"
    repo_history = run_command(cmd.split())

    if not filtered:
        return repo_history

    # Strings that identify relevant info in the ostree repo history
    keywords = ["commit ", "Parent", "Checksum", "Date", "History"]

    filtered_history = []
    for line in repo_history.splitlines():
        if any(keyword in line for keyword in keywords):
            filtered_history.append(line)

    return "\n".join(filtered_history)


def remove_ostree_remotes(ostree_repo: str) -> None:
    """
    Remove all references to remote ostree repos from the target ostree repo

    :param ostree_repo: Path to ostree repo
    """

    logger.info("Cleaning remotes from ostree repo...")

    if not os.path.isdir(ostree_repo):
        raise Exception(f"Ostree repo directory does not exist: {ostree_repo}")

    cmd = ["ostree", f"--repo={ostree_repo}", "remote", "list"]
    remote_list = run_command(cmd).split()
    logger.debug(f"Remotes: {remote_list}")

    for remote in remote_list:
        cmd = ["ostree", f"--repo={ostree_repo}", "remote", "delete", remote]
        run_command(cmd)

    with open(f"{ostree_repo}/config", mode="r", encoding="utf-8") as file:
        ostree_config = file.read()

    logger.debug(log_message("Clean ostree config:", ostree_config))


# TODO (lfagunde): This function, along with all ostree repo manipulations
# across this script, can be implemented as a separate file for "ostree utils".
# Define a class with the repo path as it's defining property and several
# methods to operate on it.
def clean_ostree(ostree_repo: str) -> None:
    """
    Delete all commits in the ostree repo except for the latest one.

    :param ostree_repo: Path to the ostree repository
    """

    logger.info("Cleaning old commits from ostree repo...")
    logger.info(f"ostree repo: {ostree_repo}")

    if not os.path.isdir(ostree_repo):
        raise Exception(f"Ostree repo directory does not exist: {ostree_repo}")

    repo_history = get_ostree_history(ostree_repo)

    logger.debug("Ostree repo history before cleaning old commits:\n"
                 f"{repo_history}")

    commits = re.findall(pattern=r"^commit\s*([\w\d-]+)", string=repo_history,
                         flags=re.MULTILINE)

    # Delete each commit except the latest one
    for commit in commits[1:]:
        cmd = f"ostree --repo={ostree_repo} prune --delete-commit={commit}"
        run_command(cmd.split())

    cmd = ["ostree", "summary", "--update", f"--repo={ostree_repo}"]
    run_command(cmd)

    repo_history = get_ostree_history(ostree_repo)

    logger.debug("Ostree repo history after cleaning old commits:\n"
                 f"{repo_history}")


def copy_iso_contents_exclude_selected(
        iso_path: str, target_dir: str,
        exclude_list: list[str] | None = None,
        verbose: bool = False) -> None:

    """Copy ISO contents to target dir EXCEPT some selected content

    To copy only specific contents, check copy_specific_iso_contents()

    :param iso_path: Path to ISO file
    :param target_dir: Directory where to copy the ISO contents
    :param exclude_list: List with names of files and directories to exclude.
        Must contain only their names. E.g: 'patches', 'efi.img', 'upgrades'
    :param verbose: Whether or not to show the list of transferred items
    """

    logger.info("Copying all ISO contents except selected...")
    logger.info(f" - ISO: {iso_path}")
    logger.info(f" - Target dir: {target_dir}")

    if not exclude_list:
        exclude_list = []

    logger.info(f" - Excluded contents: {exclude_list}")

    # Create tempdir for mounting
    mount_tempdir = tempfile.mkdtemp(prefix='mount_tempdir_')

    mount_iso(iso_path, mount_tempdir)

    # The slashes at the end of dir names are necessary for rsync
    cmd = ["rsync", "-a"]

    if verbose:
        cmd += ["-v"]

    for item in exclude_list:
        path = os.path.join(mount_tempdir, item)
        if os.path.isfile(path):
            cmd += ["--exclude", item]
        elif os.path.isdir(path):
            # Syntax to account for all internal dir content
            cmd += ["--exclude", f"{item}/***"]
        else:
            raise Exception(f"Item in exclude list not in source ISO: {item}")

    cmd += [f"{mount_tempdir}/", f"{target_dir}/"]

    run_command(cmd)

    # Calculate if copy was successful
    iso_contents = set(os.listdir(mount_tempdir))
    target_dir_contents = set(os.listdir(target_dir))
    missing_content = iso_contents - target_dir_contents - set(exclude_list)

    # Remove mountpoint
    unmount_iso(mount_tempdir)
    os.rmdir(mount_tempdir)

    # Report errors if any
    if missing_content:
        raise Exception(f"Failed to copy ISO contents: {missing_content}")


def copy_iso_contents_include_selected(iso_path: str, target_dir: str,
                                       include_list: list[str] | None,
                                       verbose: bool = False) -> None:

    """Copy ONLY selected ISO contents to target dir

    :param iso_path: Path to ISO file
    :param target_dir: Path to directory where to copy the ISO contents
    :param include_list: List with names of files and directories to copy. Must
        contain only their names. E.g: 'patches', 'efi.img', 'upgrades'
    :param verbose: Whether or not to show the list of transfered items
    """

    logger.info("Copying specific ISO contents...")
    logger.info(f" - ISO: {iso_path}")
    logger.info(f" - Target dir: {target_dir}")

    if not include_list:
        include_list = []

    logger.info(f" - Contents to include: {include_list}")

    # Create tempdir for mounting
    mount_tempdir = tempfile.mkdtemp(prefix='mount_tempdir_')

    mount_iso(iso_path, mount_tempdir)

    cmd = ["rsync", "-a"]

    if verbose:
        cmd += ["-v"]

    for item in include_list:
        path = os.path.join(mount_tempdir, item)
        if os.path.isfile(path):
            cmd += ["--include", item]
        elif os.path.isdir(path):
            # Syntax to include all internal dir content
            cmd += ["--include", f"{item}/***"]
        else:
            raise Exception(f"Invalid content to copy: {item}")

    cmd += ["--exclude", "*", f"{mount_tempdir}/", f"{target_dir}/"]

    run_command(cmd)

    # Calculate if copy was successful
    target_dir_contents = set(os.listdir(target_dir))
    missing_content = set(include_list) - target_dir_contents

    # Remove mountpoint
    unmount_iso(mount_tempdir)
    os.rmdir(mount_tempdir)

    # Report errors if any
    if missing_content:
        raise Exception(f"Failed to copy ISO contents: {missing_content}")


# === Main === #

def main():

    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Create an ISO with patches already applied. "
                    "Requires some env variables: "
                    f"{REQUIRED_ENV_VARIABLES}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '-i', '--base-iso',
        help="Full path to main ISO file. All content used to make the output "
             "ISO will be pulled from here, unless specified otherwise",
        type=str,
        required=True)

    parser.add_argument(
        '-si', '--secondary-iso',
        help="Optional. Full path to a secondary ISO file. Some ISO content "
             "can be pulled from here to replace equivalent from base input "
             "ISO, if necessary",
        type=str)

    parser.add_argument(
        '-sc', '--secondary-content',
        help="Optional. Name of a file or directory to pull from the secondary"
             " ISO instead of the main ISO. Can be used multiple times",
        action='append',
        type=str)

    parser.add_argument(
        '-p', '--patch',
        help="Full path to a patch file to apply. Can be used multiple times",
        type=str,
        action='append',
        required=True)

    parser.add_argument(
        '-o', '--output',
        help="Full path to use for the output pre-patched ISO",
        type=str,
        required=True)

    parser.add_argument(
        '-v', '--verbose',
        help="Enable debug logs",
        action='store_true')

    parser.add_argument(
        '-g', '--sign-gpg',
        help="When adding a new ostree commit corresponding to each patch, "
             "sign it using the default GPG_HOME from the LAT container.",
        action='store_true')

    parser.add_argument(
        '-b', '--base-ostree-repo',
        help="Optional. Full path to an ostree repo to use as base to apply "
             "the patches instead of one from the input ISOs",
        type=str)

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    logger.debug("=> Args provided in command line:")
    for key, value in args._get_kwargs():
        logger.debug(f"- {key} = {value}")

    # Create temporary directories
    # Tempdir for setting up ISO contents
    build_tempdir = tempfile.mkdtemp(prefix='build_tempdir_')
    # Tempdir for patches' metadata and debs
    patch_tempdir = tempfile.mkdtemp(prefix='patch_tempdir_')

    # Change permissions on build dir so we can update the files
    os.chmod(build_tempdir, 0o777)

    try:
        # Check if any required env variables are missing
        missing_env_variables = [var_name
                                 for var_name in REQUIRED_ENV_VARIABLES
                                 if not globals()[var_name]]

        if missing_env_variables:
            raise Exception("Env variables are missing: "
                            f"{missing_env_variables}. Consider executing "
                            "script from inside LAT container.")

        # Validate parsed arguments

        if not os.path.isfile(args.base_iso):
            raise Exception(f"Input ISO doesn't exist: {args.base_iso}")

        if args.secondary_iso and not os.path.isfile(args.secondary_iso):
            raise Exception("Secondary ISO doesn't exist: "
                            f"{args.secondary_iso}")

        if args.secondary_content:
            if not args.secondary_iso:
                raise Exception("Can't define secondary content without a "
                                "secondary input ISO to take it from")
            if any(["/" in item for item in args.secondary_content]):
                raise Exception("Secondary content must not be a path or "
                                "contain '/', only content names: "
                                f"{args.secondary_content}")

        if not all([os.path.isfile(patch) for patch in args.patch]):
            raise Exception("One or more patch files provided do not exist")

        if os.path.exists(args.output):
            raise Exception(f"Output filepath already exists: {args.output}")

        if args.base_ostree_repo and not os.path.isdir(args.base_ostree_repo):
            raise Exception("Base ostree repo doesn't exist: "
                            f"{args.base_ostree_repo}")

        # Re-assign args to local variables
        base_input_iso = args.base_iso
        secondary_input_iso = args.secondary_iso
        secondary_content = args.secondary_content
        output_iso = args.output
        patches = args.patch
        sign_gpg = args.sign_gpg
        base_ostree_repo = args.base_ostree_repo

        # Assign default values
        if not secondary_content:
            secondary_content = []

        logger.info("=> Starting execution")

        # Copy content from base input ISO to build dir
        # except the ostree_repo and whatever content was selected to be pulled
        # from the secondary ISO
        logger.info("=> Taking content from base input ISO...")
        exclude_list = secondary_content + ['ostree_repo']
        copy_iso_contents_exclude_selected(iso_path=base_input_iso,
                                           target_dir=build_tempdir,
                                           exclude_list=exclude_list,
                                           verbose=True)

        # Copy content from secondary input ISO to build dir
        if secondary_input_iso and secondary_content:
            logger.info("=> Taking content from secondary input ISO...")
            copy_iso_contents_include_selected(iso_path=secondary_input_iso,
                                               target_dir=build_tempdir,
                                               include_list=secondary_content,
                                               verbose=True)

        # Copy ostree_repo to build dir
        logger.info("=> Copying base ostree repository from inputs...")
        if base_ostree_repo:
            # A custom ostree_repo was provided to serve as base
            cmd = ["rsync", "-a", f'{base_ostree_repo}/',
                   f"{build_tempdir}/ostree_repo"]
            run_command(cmd)

        else:
            # As fallback, use ostree_repo from main Input ISO
            copy_iso_contents_include_selected(iso_path=base_input_iso,
                                               target_dir=build_tempdir,
                                               include_list=['ostree_repo'])

        # We initiate a reprepo feed in loadbuild because we need to access it
        # through a http service
        # TODO: apt-ostree outputs are going directly to the console,
        # instead of being returned to the caller via stdio
        logger.info(f'=> Setting up package feed in {PATCHES_FEED_PATH}...')
        cmd = ["apt-ostree", "repo", "init", "--feed", PATCHES_FEED_PATH,
               "--release", "bullseye", "--origin", "updates"]
        run_command(cmd)

        logger.info('=> Unpacking patches...')
        latest_patch_number = 0
        # For each patch, extract the metadata.xml and the deb files
        # and save the sw_version and package names to be used on apt-ostree
        patches_data = []
        for patch in patches:
            with tempfile.TemporaryDirectory() as extract_folder:
                with tarfile.open(patch) as f:

                    # We extract the metadata.xml from the metadata.tar
                    f.extract('metadata.tar', f"{extract_folder}/")
                    metadata_tar = tarfile.open(f"{extract_folder}/metadata.tar")
                    metadata_tar.extract('metadata.xml', f"{extract_folder}/")

                    # Get sw_version value and save metadata.xml using sw_version as suffix
                    xml_root = ET.parse(f"{extract_folder}/metadata.xml").getroot()
                    sw_version = xml_root.find('sw_version').text
                    component = xml_root.find('component').text
                    os.makedirs(f"{patch_tempdir}/{sw_version}/metadata")
                    metadata_path = (f"{patch_tempdir}/{sw_version}/metadata/{component}-{sw_version}"
                        "-metadata.xml")
                    shutil.copy(f"{extract_folder}/metadata.xml", metadata_path)

                    # From inside software.tar we extract every .deb file
                    f.extract('software.tar', f"{extract_folder}/")
                    software_tar = tarfile.open(f"{extract_folder}/software.tar")
                    software_tar.extractall(f"{patch_tempdir}/{sw_version}/debs/")
                    # Packages names need to include version and revision
                    # e.g.: logmgmt_1.0-1.stx.10
                    packages = []
                    for i in xml_root.find('packages').findall('deb'):
                        packages.append(i.text.split("_")[0])

                    # Patches can contain precheck scripts, we need to verify if
                    # they exist and, if so, move them to the pre-patched iso.
                    precheck = False
                    path_precheck = ''
                    path_upgrade_utils = ''
                    if "deploy-precheck" in f.getnames() and "upgrade_utils.py" in f.getnames():
                        precheck = True
                        f.extract('deploy-precheck', f"{extract_folder}/")
                        f.extract('upgrade_utils.py', f"{extract_folder}/")
                        precheck_folder = f"{patch_tempdir}/{sw_version}/precheck"
                        os.makedirs(f"{precheck_folder}")
                        path_precheck = f"{precheck_folder}/deploy-precheck"
                        path_upgrade_utils = f"{precheck_folder}/upgrade_utils.py"
                        shutil.copy(f"{extract_folder}/deploy-precheck", path_precheck)
                        shutil.copy(f"{extract_folder}/upgrade_utils.py", path_upgrade_utils)

                    # Now we save the information we extract for later use
                    patches_data.append({
                        "sw_version": sw_version,
                        "path": f"{patch_tempdir}/{sw_version}",
                        "packages": packages,
                        "metadata": metadata_path,
                        "precheck": precheck,
                        "path_precheck": path_precheck,
                        "path_upgrade_utils": path_upgrade_utils
                        })

                    # Save the biggest version from the patches we have
                    patch_num = int(sw_version.split(".")[-1])

                    latest_patch_number = max(patch_num, latest_patch_number)

                    logger.info(f'Patch {sw_version} unpacked sucessfully.')

        # Here we setup our gpg client if needed
        if sign_gpg:
            setup_gpg_client()

        # We delete the patches folder from the base iso and recreate it
        # so we may populate with the metadatas from the patches we are using
        shutil.rmtree(f"{build_tempdir}/patches", ignore_errors=True)
        os.mkdir(f"{build_tempdir}/patches")

        # We clean all the metadatas inside upgrades folder
        for file in glob.glob(f"{build_tempdir}/upgrades/*-metadata.xml"):
            os.remove(file)

        # Now we need to populate reprepo feed with every deb from every patch
        # after that we install it on the ostree repository
        logger.info('Populate ostree repository with .deb files...')
        patches_data = sorted(patches_data, key=lambda x: x['sw_version'])
        for patch in patches_data:
            # Scan /debs/ folder and load each patch onto the reprepo feed

            debs_dir = os.path.join(patch["path"], "debs/")
            if not os.path.isdir(debs_dir):
                msg = f"Patch '{patch['sw_version']}' does not contain any deb pkgs. " \
                      "Skipping creation of corresponding ostree commit."
                logger.warning(msg)
                #TODO: Re-evaluate GPG signing for empty patches.

            else:
                # Populate apt repo
                debs = os.listdir(debs_dir)
                for deb in debs:
                    cmd = ["apt-ostree", "repo", "add", "--feed", PATCHES_FEED_PATH,
                        "--release", "bullseye", "--component", patch['sw_version'],
                        os.path.join(f"{patch['path']}/debs/", deb)]
                    logger.debug('Running command: %s', cmd)
                    subprocess.check_call(cmd, shell=False)

                # Now with every deb loaded we commit it in the ostree repository
                # apt-ostree requires an http connection to access the host files
                # so we give the full http path using the ip
                full_feed_path = f'\"{HTTP_FULL_ADDR}{PATCHES_FEED_PATH} bullseye\"'
                cmd = ["apt-ostree", "compose", "install", "--repo", f"{build_tempdir}/ostree_repo"]
                # If we have ostree setup we will use the gpg key
                if sign_gpg:
                    gpg_key = get_value_from_yaml("gpg.ostree.gpgid")
                    cmd += ["--gpg-key", gpg_key]
                pkgs = " ".join(patch["packages"])
                cmd += ["--branch", "starlingx", "--feed", full_feed_path, "--component",
                    patch['sw_version'], pkgs]

                logger.debug('Running command: %s', cmd)
                subprocess.check_call(cmd, shell=False)

            # Check if patch has precheck scripts, if yes move then to the upgrades folder
            if patch["precheck"]:
                shutil.copy(patch["path_precheck"], f"{build_tempdir}/upgrades")
                shutil.copy(patch["path_upgrade_utils"], f"{build_tempdir}/upgrades")

            # Copy only the patch metadata with the biggest patch version to ISO
            patch_num = int(patch["sw_version"].split(".")[-1])
            if latest_patch_number == patch_num:
                # Metadata inside upgrades requires ostree information
                update_metadata_info(patch["metadata"], build_tempdir)
                shutil.copy(patch["metadata"], f"{build_tempdir}/patches")
                shutil.copy(patch["metadata"], f"{build_tempdir}/upgrades")

        # Update ostree summary
        cmd = ["ostree", "summary", "--update", f"--repo={build_tempdir}/ostree_repo"]
        logger.debug('Running command: %s', cmd)
        subprocess.check_call(cmd, shell=False)

        # Keep only the latest commit in ostree_repo to save storage space
        clean_ostree(f"{build_tempdir}/ostree_repo")

        # Remove all references to remote ostree repos used during build
        remove_ostree_remotes(f"{build_tempdir}/ostree_repo")

        # Now we get the label and re create the ISO with the new ostree
        logger.info('Creating new .iso file...')
        instlabel = get_iso_label_from_isolinux_cfg(f"{build_tempdir}/isolinux/isolinux.cfg")
        create_iso(build_tempdir, instlabel, output_iso)

        # Allow to edit and read the newly created iso
        os.chmod(output_iso, 0o664)
        logger.info("Pre-patched ISO created sucessfully: %s", output_iso)

    except Exception as e:
        logger.error("[EXECUTION FAILED]")
        logger.exception(f"Summary: {e}")

    # Clean up temporary folders
    shutil.rmtree(build_tempdir, ignore_errors=True)
    shutil.rmtree(patch_tempdir, ignore_errors=True)

    # Clean reprepro feed
    if os.path.exists(PATCHES_FEED_PATH):
        shutil.rmtree(PATCHES_FEED_PATH, ignore_errors=True)


if __name__ == "__main__":
    main()
