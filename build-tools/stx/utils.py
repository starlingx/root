#
# Copyright (c) 2021-2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
import os
import pathlib
import subprocess
import urllib.parse
import urllib.request


OS_MIRROR_URL = os.environ.get('OS_MIRROR_URL')
OS_MIRROR_DL_PATH = os.environ.get('OS_MIRROR_DL_PATH', 'debian/')
if OS_MIRROR_URL:
    OS_MIRROR_BASE = os.path.join(OS_MIRROR_URL, OS_MIRROR_DL_PATH)


log_levels = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'crit': logging.CRITICAL
}


def set_logger(logger, log_level='debug'):
    logger.setLevel(log_levels[log_level])

    class ColorFormatter(logging.Formatter):
        FORMAT = ("%(asctime)s - $BOLD%(name)-s$RESET - %(levelname)s: %(message)s")

        BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = list(range(8))

        RESET_SEQ = "\033[0m"
        COLOR_SEQ = "\033[1;%dm"
        BOLD_SEQ = "\033[1m"

        COLORS = {
            'WARNING': YELLOW,
            'INFO': GREEN,
            'DEBUG': BLUE,
            'ERROR': RED
        }

        def formatter_msg(self, msg, use_color=True):
            if use_color:
                msg = msg.replace("$RESET", self.RESET_SEQ)
                msg = msg.replace("$BOLD", self.BOLD_SEQ)
            else:
                msg = msg.replace("$RESET", "").replace("$BOLD", "")
            return msg

        def __init__(self, use_color=True):
            msg = self.formatter_msg(self.FORMAT, use_color)
            logging.Formatter.__init__(self, msg)
            self.use_color = use_color

        def format(self, record):
            lname = record.levelname
            if self.use_color and lname in self.COLORS:
                fcolor = 30 + self.COLORS[lname]
                lncolor = self.COLOR_SEQ % fcolor + lname + self.RESET_SEQ
                record.levelname = lncolor
            return logging.Formatter.format(self, record)

    # create log and console handler and set level
    fh = logging.FileHandler('/localdisk/builder.log')
    fh.setLevel(log_levels[log_level])
    fh.setFormatter(ColorFormatter(use_color=False))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(log_levels[log_level])
    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)

    logger.propagate = 0


# Read file 'lst_file', sprip out blank lines and lines starting with '#'.
# Return the remaining lines as a list.  Optionally subject the lines
# to additional processing via the entry_handler prior to inclusion in
# the list
def bc_safe_fetch(lst_file, entry_handler=None, entry_handler_arg=None):
    entries = []
    try:
        with open(lst_file, 'r') as flist:
            lines = list(line for line in (p.strip() for p in flist) if line)
    except IOError as e:
        raise e
    except Exception as e:
        raise e
    else:
        for entry in lines:
            entry = entry.strip()
            if entry.startswith('#'):
                continue
            if entry == "":
                continue
            if entry_handler:
                if entry_handler_arg:
                    entries.extend(entry_handler(entry, entry_handler_arg))
                else:
                    entries.extend(entry_handler(entry))
            else:
                entries.append(entry)
    return entries


def limited_walk(dir, max_depth=1):
    dir = dir.rstrip(os.path.sep)
    assert os.path.isdir(dir)
    num_sep_dir = dir.count(os.path.sep)
    for root, dirs, files in os.walk(dir):
        yield root, dirs, files
        num_sep_root = root.count(os.path.sep)
        if num_sep_dir + max_depth <= num_sep_root:
            del dirs[:]


def run_shell_cmd_full(cmd, logger, error_level=logging.ERROR):
    if type(cmd) is str:
        shell = True
    elif type(cmd) in (tuple, list):
        shell = False
    else:
        raise Exception("Unrecognized 'cmd' type '%s'. Must be one of [str, list, tuple]." % (type(cmd)))

    logger.info(f'[ Run - "{cmd}" ]')
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, shell=shell)
    except Exception as e:
        msg = f'[ Failed to execute command: "{cmd}" Exception: "{e}" ]'
        logger.log(error_level, msg)
        # Suppress the original exception when raising our own exception.
        # Syntax is acquired from: https://peps.python.org/pep-0409/#proposal
        raise Exception(msg) from None

    outs, errs = process.communicate()

    for log in outs.strip().split("\n"):
        log = log.strip()
        if log:
            logger.debug("stdout: %s", log)

    for log in errs.strip().split("\n"):
        log = log.strip()
        if log:
            logger.debug("stderr: %s", log)

    if process.returncode != 0:
        msg = f'[ Command failed with a non-zero return code: "{cmd}" return code: {process.returncode} ]'
        logger.log(error_level, msg)
        raise subprocess.CalledProcessError(
                returncode=process.returncode,
                cmd=cmd,
                output=outs,
                stderr=errs
        )

    return outs.strip(),errs.strip()


def run_shell_cmd(cmd, logger, error_level=logging.ERROR):
    return run_shell_cmd_full(cmd, logger, error_level)[0]


def url_to_os_mirror(url):

    url_change = urllib.parse.urlparse(url)
    url_path = pathlib.Path(url_change.path)
    if url_change.netloc != '':
        path = pathlib.Path(url_change.netloc, url_path.relative_to("/"))
    else:
        path = url_path

    # FIXME: the ":" in a path is converted to "%25", after
    # uploading to OS_MIRROR, the "%25" in the path is converted
    # to "%2525".
    return os.path.join(OS_MIRROR_BASE, path).replace("%25", "%2525")


def get_download_url(url, strategy):

    alt_rt_url = None
    os_mirror_url = url_to_os_mirror(url)
    if strategy == "stx_mirror":
        rt_url = os_mirror_url
    elif strategy == "upstream":
        rt_url = url
    elif strategy == "stx_mirror_first":
        try:
            urllib.request.urlopen(os_mirror_url)
            rt_url = os_mirror_url
            alt_rt_url = url
        except:
            rt_url = url
    elif strategy == "upstream_first":
        try:
            urllib.request.urlopen(url)
            rt_url = url
            alt_rt_url = os_mirror_url
        except:
            rt_url = os_mirror_url
    else:
        raise Exception(f'Invalid value "{strategy}" of STX_MIRROR_STRATEGY')

    return (rt_url, alt_rt_url)


def deb_file_name_to_dict(deb_file):
    ver_array = []
    arch = None
    pkg_epoch = None
    pkg_ver = None
    deb_array = deb_file.split("_")
    pkg_name = deb_array[0]
    if len(deb_array) >= 3:
        arch = deb_array[2].split(".")[0]
    if len(deb_array) >= 2:
        ver_array = deb_array[1].split(":")
    if len(ver_array) >= 2:
        pkg_ver = ver_array[-1]
        pkg_epoch = ver_array[0]
    elif len(ver_array) == 1:
        pkg_ver = ver_array[0]
        pkg_epoch = None
    pkg_dict = {'name':pkg_name, 'ver':pkg_ver, 'epoch':pkg_epoch, 'arch':arch, 'url':None}
    return pkg_dict


def deb_url_name_to_dict(deb_url):
    deb_file = os.path.basename(dub_url)
    pkg_dict = deb_file_name_to_dict(deb_file)
    pkg_dict['url'] = deb_url
    return pkg_dict


def get_env_variable(var: str) -> str:
    """Get env variable. Raise error if not defined."""

    value = os.environ.get(var)
    if value == None:
        raise ValueError(f"Env variable not defined: {var}")

    return value
