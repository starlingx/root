#!/usr/bin/env python3
#
# Copyright (c) 2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
'''
Builds a Debian patch
'''

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

import click
import fetch_debs
import metadata
from signing.patch_signing import sign_files

sys.path.append('..')
import utils

logger = logging.getLogger('patch_builder')
utils.set_logger(logger)

# Patch signature files
detached_signature_file = "signature.v2"
mdsum_signature_file = "signature"

# Patch output directory
BUILD_ROOT = os.environ.get('MY_BUILD_PKG_DIR')
PATCH_OUTPUT = os.path.join(BUILD_ROOT, "patch_output")

# Default names for every script type
PATCH_SCRIPTS = {
   "PRE_INSTALL": "pre-install.sh",
   "POST_INSTALL": "post-install.sh",
   "DEPLOY_PRECHECK": "deploy-precheck.sh"
}

class PatchBuilder(object):
    def __init__(self, patch_recipe_file, file_name=None):
        self.metadata = metadata.PatchMetadata(patch_recipe_file)
        self.metadata.parse_input_xml_data()
        self.fetch_debs = fetch_debs.FetchDebs()
        self.fetch_debs.need_dl_stx_pkgs = self.metadata.stx_packages
        self.fetch_debs.need_dl_binary_pkgs = self.metadata.binary_packages
        self.patch_name = f'{self.metadata.patch_id}.patch' if file_name == None else file_name

    def get_md5(self, path):
        '''
        Utility function for generating the md5sum of a file
        :param path: Path to file
        '''
        md5 = hashlib.md5()
        block_size = 8192
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(block_size), b''):
                md5.update(chunk)
        return int(md5.hexdigest(), 16)

    def build_patch(self):
        logger.info(f"Generating patch {self.patch_name}")
        # Fetch debs from metadata and
        # Create software.tar, metadata.tar and signatures
        # Create a temporary working directory
        logger.debug("Fetching debs...")
        self.fetch_debs.fetch_stx_packages()
        self.fetch_debs.fetch_external_binaries()
        # verify if dir is not empty
        dl_dir = os.path.join(self.fetch_debs.output_dir, "downloads", "binary")
        if not os.listdir(dl_dir):
            logger.error("No debs fetched")
            return False
        logger.info("################ PATCH BUILD ################")
        logger.info("Download completed, building our patch")
        tmpdir = tempfile.mkdtemp(prefix="patch_")
        os.chdir(tmpdir)
        tar = tarfile.open("software.tar", "w")
        # copy all files from dl_dir into the tar
        for file in os.listdir(dl_dir):
            logger.info(f"Saving file {file}")
            tar.add(os.path.join(dl_dir, file), arcname=file)
            # append deb name into metadata
            self.metadata.debs.append(file)
        tar.close()

        pre_install = self.metadata.pre_install
        post_install = self.metadata.post_install
        deploy_precheck = self.metadata.deploy_precheck
        # pre/post install scripts
        if pre_install:
            logger.debug(f"Copying pre-install script: {pre_install}")
            self.copy_script("PRE_INSTALL", pre_install)

        if post_install:
            logger.debug(f"Copying post-install script: {post_install}")
            self.copy_script("POST_INSTALL", post_install)

        if deploy_precheck:
            logger.debug(f"Copying deploy pre-check script: {deploy_precheck}")
            self.copy_script("DEPLOY_PRECHECK", deploy_precheck)

        if not pre_install and not post_install and self.metadata.reboot_required == 'N':
            logger.warn("In service patch without restart scripts provided")

        # Generate metadata.xml
        logger.debug("Generating metadata file")
        self.metadata.generate_patch_metadata("metadata.xml")
        tar = tarfile.open("metadata.tar", "w")
        tar.add("metadata.xml")
        tar.close()
        os.remove("metadata.xml")

        # Pack .patch file
        self.__sign_and_pack(self.patch_name)

    def copy_script(self, script_type, install_script):
        if not os.path.isfile(install_script):
            erro_msg = f"Install script {install_script} not found"
            logger.error(erro_msg)
            raise FileNotFoundError(erro_msg)

        # We check the type to correctly rename the file to a expected value
        script_name = PATCH_SCRIPTS.get(script_type, None)

        if script_name:
            logger.info(f"Renaming {install_script} to {script_name}")
            shutil.copy(install_script, f"./{script_name}")
        else:
            raise ValueError(f"Script type provided is not valid one: {script_type}")

    def __sign_and_pack(self, patch_file):
        """
        Generates the patch signatures and pack the .patch file
        :param patch_file .patch file full path
        """
        filelist = ["metadata.tar", "software.tar"]

        if self.metadata.pre_install:
            filelist.append(PATCH_SCRIPTS["PRE_INSTALL"])

        if self.metadata.post_install:
            filelist.append(PATCH_SCRIPTS["POST_INSTALL"])

        # Generate the local signature file
        logger.debug(f"Generating signature for patch files {filelist}")
        sig = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        for f in filelist:
            sig ^= self.get_md5(f)

        sigfile = open(mdsum_signature_file, "w")
        sigfile.write("%x" % sig)
        sigfile.close()

        # this comes from patch_functions write_patch
        # Generate the detached signature
        #
        # Note: if cert_type requests a formal signature, but the signing key
        #    is not found, we'll instead sign with the "dev" key and
        #    need_resign_with_formal is set to True.
        need_resign_with_formal = sign_files(
            filelist,
            detached_signature_file,
            cert_type=None)

        logger.info(f"Formal signing status: {need_resign_with_formal}")

        # Save files into .patch
        files = [f for f in os.listdir('.') if os.path.isfile(f)]

        if not os.path.exists(PATCH_OUTPUT):
            os.makedirs(PATCH_OUTPUT)
        patch_full_path = os.path.join(PATCH_OUTPUT, patch_file)
        tar = tarfile.open(patch_full_path, "w:gz")
        for file in files:
            logger.info(f"Saving file {file}")
            tar.add(file)
        tar.close()
        logger.info(f"Patch file created {patch_full_path}")

    def __sign_official_patches(self, patch_file):
        """
        Sign formal patch
        Called internally once a patch is created and formal flag is set to true
        :param patch_file full path to the patch file
        """
        logger.info("Signing patch %s", patch_file)
        try:
            subprocess.check_call(["sign_patch_formal.sh", patch_file])
        except subprocess.CalledProcessError as e:
            logger.exception("Failed to sign official patch. Call to sign_patch_formal.sh process returned non-zero exit status %i", e.returncode)
        except FileNotFoundError:
            logger.exception("sign_patch_formal.sh not found, make sure $STX_BUILD_HOME/repo/cgcs-root/build-tools is in the $PATH")


@click.command()
@click.option('--recipe', help='Patch recipe input XML file, examples are available under EXAMLES directory',
               required=True)
@click.option('--name', help='Allow user to define name of the patch file. e.g.: test-sample-rr.patch. \
              Name will default to patch_id if not defined',
               required=False)
def build(recipe, name=None):
    patch_builder = PatchBuilder(recipe, name)
    patch_builder.build_patch()

if __name__ == '__main__':
    build()