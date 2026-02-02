#!/usr/bin/env python3
#
# Copyright (c) 2026 Wind River Systems, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Tool for creating a modified version of a given STX patch.
# Some use cases:
# - Changing patch status from DEV (development) to REL (release)
# - Updating other metadata info (description, sw_version, etc)
# - Formalizing signatures of a manually edited patch file
#

# TODO: This script is a modified copy of "patch-builder", with a lot of overlap.
#       All USM patching code needs to be refactored to reduce code duplication.
# TODO: This script can be further developed to enable generating a patch from another
#       in more contexts. For example:
#       - Add/Remove patch scripts
#       - Add/Remove STX pkgs and third-party pkgs
#       - Add/Remove the extra tarball
#       - Modify any other patch characteristics


import click
import hashlib
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

import constants
import metadata
from signing.patch_signing import sign_files

sys.path.append('..')
import utils

# Patch signature files
DETACHED_SIGNATURE_FILENAME = "signature.v2"
MD5SUM_SIGNATURE_FILENAME = "signature"

# Default patch output directory
DEPLOY_DIR = "/localdisk/deploy"
DEFAULT_PATCH_OUTPUT_DIR = os.path.join(DEPLOY_DIR, "patch_output")


logger = logging.getLogger(__name__)
utils.set_logger(logger)


class PatchEditor(object):

    def __init__(self,
                 patch,
                 recipe,
                 release,
                 sign_remote=False):

        self.patch_path = patch
        self.patch_name = os.path.basename(patch)

        if not recipe:
            self.new_metadata = None
        else:
            self.new_metadata = metadata.PatchMetadata(recipe)
            self.new_metadata.parse_input_xml_data()

        self.set_release = release

        self.sign_remote = sign_remote
        self.signing_server = os.environ.get('SIGNING_SERVER')
        self.signing_user = os.environ.get('SIGNING_USER')

        self.validate_inputs()


    def validate_inputs(self):
        """Raise errors if any input is invalid"""

        # Formal signature requested without providing auths for signing server
        if self.sign_remote and \
           (self.signing_server == None or self.signing_user == None):
            msg = "Cannot sign patch without signing server info"
            logger.error(msg)
            raise Exception(msg)


    # TODO: This should be in the "signing" library
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


    def run(self):

        tmpdir = tempfile.mkdtemp(prefix="patch_")
        os.chdir(tmpdir)

        logger.debug("Extracting input patch...")
        with tarfile.open(self.patch_path, "r") as input_patch_file:
            input_patch_file.extractall()

        # TODO: These filenames (metadata.xml, metadata.tar) should be moved to the constants lib

        # Modify metadata
        if self.new_metadata or self.set_release:

            if self.new_metadata:
                # Generate new metadata XML from new recipe provided
                logger.debug("Updating metadata...")
                os.remove("metadata.tar")
                self.new_metadata.generate_patch_metadata("metadata.xml")

            else:
                # Extract metadata XML from input patch file
                with tarfile.open("metadata.tar", "r") as metadata_tar:
                    metadata_tar.extractall()
                os.remove("metadata.tar")

            if self.set_release:
                metadata.update_tag("metadata.xml", "status", "REL")

            # Create metadata tarball
            with tarfile.open("metadata.tar", "w") as new_metadata_tar:
                new_metadata_tar.add("metadata.xml")
            os.remove("metadata.xml")

        # Create new signatures and pack contents into a .patch file
        os.remove(MD5SUM_SIGNATURE_FILENAME)
        os.remove(DETACHED_SIGNATURE_FILENAME)

        self.__sign_and_pack(self.patch_name)


    # TODO: Generating the md5 signature and packing the files into a tarball
    #       should be two separate functions. We need to be able to generate
    #       generate the md5 sig function with just a list of files as input,
    #       because that is useful for validating the patch.
    # TODO: This function has some hidden requirements: It expects the curdir
    #       to be the temp build dir and expects *at least* the metadata and
    #       software tarballs to be there.
    def __sign_and_pack(self, patch_file):
        """
        Generates the patch signatures and pack the .patch file
        :param patch_file .patch file full path
        """
        filelist = ["metadata.tar", "software.tar"]

        if os.path.exists("extra.tar"):
            filelist.append("extra.tar")

        for script_name in constants.PATCH_SCRIPTS.values():
            if os.path.exists(script_name):
                filelist.append(script_name)

        # Generate the local signature file
        logger.debug(f"Generating signature for patch files: {filelist}")
        sig = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        for f in filelist:
            sig ^= self.get_md5(f)

        sigfile = open(MD5SUM_SIGNATURE_FILENAME, "w")
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
            DETACHED_SIGNATURE_FILENAME,
            cert_type=None)

        logger.info(f"Formal signing status: {need_resign_with_formal}")

        # Save files into .patch
        files = [f for f in os.listdir('.') if os.path.isfile(f)]

        if not os.path.exists(DEFAULT_PATCH_OUTPUT_DIR):
            os.makedirs(DEFAULT_PATCH_OUTPUT_DIR)
        patch_full_path = os.path.join(DEFAULT_PATCH_OUTPUT_DIR, patch_file)
        tar = tarfile.open(patch_full_path, "w:gz")
        for file in files:
            logger.info(f"Saving file {file}")
            tar.add(file)
        tar.close()
        logger.info(f"Patch file created {patch_full_path}")
        if self.sign_remote:
            self.__sign_patch_remotely(patch_full_path)


    def __sign_patch_remotely(self, patch_file):
        """
        Send the patch file to be signed remotely by a signing server

        :param patch_file full path to the patch file
        """
        logger.info("Starting remote signing for: %s", patch_file)

        if not self.signing_server:
            logger.error("SIGNING_SERVER variable not set, unable to continue.")
            sys.exit(1)
        if not self.signing_user:
            logger.error("SIGNING_USER variable not set, unable to continue.")
            sys.exit(1)
        try:
            conn_string = f"{self.signing_user}@{self.signing_server}"
            patch_basename = os.path.basename(patch_file)

            # First we get the upload path from the signing server, it should return something
            # similar to: "Upload: /tmp/sign_upload.5jR11pS0"
            call_path = subprocess.check_output([
                "ssh",
                "-o StrictHostKeyChecking=no",
                conn_string,
                f"sudo {constants.GET_UPLOAD_PATH} -r"]).decode(sys.stdout.encoding).strip()
            upload_path = call_path.split()[1]
            logger.info("Upload path receive from signing server: %s", upload_path)

            # We send the patch to the signing server
            logger.info("Sending patch to signing server...")
            subprocess.check_output([
                "scp",
                "-q",
                patch_file,
                f"{conn_string}:{upload_path}"])

            # Request the signing server to sign the patch, it should return the full path
            # of the patch inside the signing server
            logger.info("Signing patch...")
            signed_patch_path = subprocess.check_output([
                "ssh",
                conn_string,
                f"sudo {constants.REQUEST_SIGN}",
                f"{upload_path}/{patch_basename}",
                "usm"]).decode(sys.stdout.encoding).strip()
            logger.info("Signing successful, path returned: %s", signed_patch_path)

            logger.info("Downloading signed patch...")
            subprocess.check_output([
                "scp",
                "-q",
                f"{conn_string}:{signed_patch_path}",
                patch_file])
            logger.info("Patch successfully signed: %s", patch_file)
        except subprocess.CalledProcessError as e:
            logger.exception("Failure to sign patch: %s", e)
        except Exception as e:
            logger.exception("An unexpected error has occurred when signing the patch: %s", e)


@click.command()
@click.option('--patch',
              help='Path to patch file to use as reference',
              required=True)
@click.option('--recipe',
              help='When provided, it is used to replace the patch metadata. Note tt does no other changes (such as adding/removing pkgs/scripts)',
              required=False)
@click.option('--release',
              help='Set the patch status to REL (release)',
              is_flag=True,
              required=False)
@click.option('--remote-sign',
              help='Send patch to a signing server to receive another signature. Requires env vars: SIGNING_SERVER, SINGING_USER',
              is_flag=True,
              required=False)

def main(patch, recipe=None, release=False, remote_sign=False):
    """Tool for creating a modified version of a given STX patch.

    Some use cases:
    Changing patch status from DEV (development) to REL (release);
    Updating other metadata info (description, sw_version, etc);
    Formalizing signatures of a manually edited patch file;
    """

    patch_editor= PatchEditor(patch, recipe, release, sign_remote=remote_sign)
    patch_editor.run()


if __name__ == '__main__':
    main()
