#
# Copyright (c) 2023 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import hashlib
import logging
import os
import subprocess
import sys

import signing.patch_verify as patch_verify
import utils

from Cryptodome.Signature import PKCS1_PSS
from Cryptodome.Hash import SHA256


logger = logging.getLogger('signing')
utils.set_logger(logger)

# To save memory, read and hash 1M of files at a time
default_blocksize = 1 * 1024 * 1024

# When we sign patches, look for private keys in the following paths
#
# The (currently hardcoded) path on the signing server will be replaced
# by the capability to specify filename from calling function.
private_key_files = {
    patch_verify.cert_type_formal_str: '/signing/keys/formal-private-key.pem',
    patch_verify.cert_type_dev_str: os.path.expandvars(
        '$MY_REPO/build-tools/signing/dev-private-key.pem')
}

# Default path to the script that generates the upload path
GET_UPLOAD_PATH = "/opt/signing/sign.sh"

# Default path to the script that signs the patch
REQUEST_SIGN = "/opt/signing/sign_patch.sh"

# Expected patch contents
METADATA_FILE = "metadata.tar"
SOFTWARE_FILE = "software.tar"
EXTRA_FILE = "extra.tar"
PATCH_CONTENTS = [METADATA_FILE, SOFTWARE_FILE, EXTRA_FILE]
MANDATORY_PATCH_CONTENTS = [METADATA_FILE, SOFTWARE_FILE]

# Patch signature files
DETACHED_SIGNATURE_FILENAME = "signature.v2"
MD5SUM_SIGNATURE_FILENAME = "signature"

MD5_HASH_BLOCK_SIZE = 8192


def get_md5(path):
    '''
    Utility function for generating the md5sum of a file
    :param path: Path to file
    '''
    md5 = hashlib.md5()
    block_size = MD5_HASH_BLOCK_SIZE
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(block_size), b''):
            md5.update(chunk)
    return int(md5.hexdigest(), 16)


def get_patch_content_filelist(path):
    """
    Check directory defined by :path: for PATCH_CONTENTS
    Return list of abspaths of valid patch contents in :path:
    Throw exception if mandatory content is missing
    """

    # Check which valid patch content files are present in path
    candidate_files = [os.path.join(path, item) for item in PATCH_CONTENTS]
    confirmed_files = [item for item in candidate_files if os.path.isfile(item)]

    item_names = set(os.path.basename(item) for item in confirmed_files)

    # Check if any mandatory patch content is missing
    missing_content = set(MANDATORY_PATCH_CONTENTS) - item_names

    if missing_content:
        raise Exception(f"Missing mandatory patch contents: {missing_content}")

    return confirmed_files


def generate_md5_signature(path):
    """
    Generate an md5 checksum signature file by
    combining the md5 checksum of several files using the XOR operation.

    * :path: is the directory where the target files are located
    * The signature file is also created in :path:
    * Files not in PATCH_CONTENTS are ignored for signature calculation
    """

    filelist = get_patch_content_filelist(path)

    item_names = set(os.path.basename(item) for item in filelist)

    logger.debug(f"Generating md5 signature file for patch contents: {item_names}")

    sig = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
    for item in filelist:
        sig ^= get_md5(item)

    sig_path = os.path.join(path, MD5SUM_SIGNATURE_FILENAME)

    with open(sig_path, "w") as sig_file:
        sig_file.write("%x" % sig)


def sign_files(filenames, signature_file, private_key=None, cert_type=None):
    """
    Utility function for signing data in files.
    :param filenames: A list of files containing the data to be signed
    :param signature_file: The name of the file to which the signature will be
                           stored
    :param private_key: If specified, sign with this private key.  Otherwise,
                        the files in private_key_files will be searched for
                        and used, if found.
    :param cert_type: If specified, and private_key is not specified, sign
                      with a key of the specified type.  e.g. 'dev' or 'formal'
    """

    # Hash the data across all files
    blocksize = default_blocksize
    data_hash = SHA256.new()
    for filename in filenames:
        with open(filename, 'rb') as infile:
            data = infile.read(blocksize)
            while len(data) > 0:
                data_hash.update(data)
                data = infile.read(blocksize)

    # Find a private key to use, if not already provided
    need_resign_with_formal = False
    if private_key is None:
        if cert_type is not None:
            # A Specific key is asked for
            assert (cert_type in list(private_key_files)
                    ), "cert_type=%s is not a known cert type" % cert_type
            dict_key = cert_type
            filename = private_key_files[dict_key]
            logger.info(f'Cert type "{cert_type}": Checking to see if {filename} exists')
            if not os.path.exists(filename) and dict_key == patch_verify.cert_type_formal_str:
                # The formal key is asked for, but is not locally available,
                # substitute the dev key, and we will try to resign with the formal later.
                dict_key = patch_verify.cert_type_dev_str
                filename = private_key_files[dict_key]
                need_resign_with_formal = True
                logger.warn('Formal key not found, using development keys')

            if os.path.exists(filename):
                # print 'Getting private key from ' + filename + '\n'
                private_key = patch_verify.read_RSA_key(
                    open(filename, 'rb').read())
        else:
            # Search for available keys
            for dict_key in private_key_files.keys():
                filename = private_key_files[dict_key]
                # print 'Search for available keys: Checking to see if ' + filename + ' exists\n'
                if os.path.exists(filename):
                    # print 'Getting private key from ' + filename + '\n'
                    private_key = patch_verify.read_RSA_key(
                        open(filename, 'rb').read())

    assert (private_key is not None), "Could not find signing key"

    # Encrypt the hash (sign the data) with the key we find
    signer = PKCS1_PSS.new(private_key)
    signature = signer.sign(data_hash)

    # Save it
    with open(signature_file, 'wb') as outfile:
        outfile.write(signature)

    return need_resign_with_formal


def generate_detached_signature(path):
    """
    Generate 'detached' signature file.
    This signature determines if the patch generated is a formal or designer patch.

    * :path: is the directory where the target files are located
    * The signature file is also created in :path:
    * Files not in PATCH_CONTENTS are ignored for signature calculation
    """

    filelist = get_patch_content_filelist(path)
    sig_path = os.path.join(path, DETACHED_SIGNATURE_FILENAME)

    # this comes from patch_functions write_patch
    # Generate the detached signature
    #
    # Note: if cert_type requests a formal signature, but the signing key
    #    is not found, we'll instead sign with the "dev" key and
    #    need_resign_with_formal is set to True.
    need_resign_with_formal = sign_files(
        filelist,
        sig_path,
        cert_type=None)

    # logger.info(f"Formal signing status: {need_resign_with_formal}")


def sign_patch_remotely(patch_file, signing_server, signing_user):
    """
    Send the patch file to be signed remotely by a signing server
    :patch_file: is the full path to the patch file
    """

    logger.info("Starting remote signing for: %s", patch_file)

    try:
        conn_string = f"{signing_user}@{signing_server}"
        patch_basename = os.path.basename(patch_file)

        # First we get the upload path from the signing server, it should return something
        # similar to: "Upload: /tmp/sign_upload.5jR11pS0"
        call_path = subprocess.check_output([
            "ssh",
            "-o StrictHostKeyChecking=no",
            conn_string,
            f"sudo {GET_UPLOAD_PATH} -r"]).decode(sys.stdout.encoding).strip()
        upload_path = call_path.split()[1]
        logger.debug("Upload path receive from signing server: %s", upload_path)

        # We send the patch to the signing server
        logger.debug("Sending patch to signing server...")
        subprocess.check_output([
            "scp",
            "-q",
            patch_file,
            f"{conn_string}:{upload_path}"])

        # Request the signing server to sign the patch, it should return the full path
        # of the patch inside the signing server
        logger.debug("Signing patch...")
        signed_patch_path = subprocess.check_output([
            "ssh",
            conn_string,
            f"sudo {REQUEST_SIGN}",
            f"{upload_path}/{patch_basename}",
            "usm"]).decode(sys.stdout.encoding).strip()
        logger.debug("Signing successful, path returned: %s", signed_patch_path)

        logger.debug("Downloading signed patch...")
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
