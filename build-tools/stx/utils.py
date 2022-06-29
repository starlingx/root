# Copyright (c) 2021 Wind River Systems, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright (C) 2021 Wind River Systems,Inc

import logging
import os
import subprocess

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
        logger.error(str(e))
    except Exception as e:
        logger.error(str(e))
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


def run_shell_cmd(cmd, logger):
    if type(cmd) is str:
        shell = True
        cmd_str = cmd
    elif type(cmd) in (tuple, list):
        shell = False
        cmd_str = " ".join(cmd)
    else:
        raise Exception("Unrecognized 'cmd' type '%s'. Must be one of [str, list, tuple]." % (type(cmd)))

    logger.info(f'[ Run - "{cmd_str}" ]')
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, shell=shell)
        # process.wait()
        outs, errs = process.communicate()
    except Exception:
        process.kill()
        outs, errs = process.communicate()
        logger.error(f'[ Failed - "{cmd}" ]')
        raise Exception(f'[ Failed - "{cmd}" ]')

    for log in outs.strip().split("\n"):
        if log != "":
            logger.debug(log.strip())

    if process.returncode != 0:
        for log in errs.strip().split("\n"):
            logger.error(log)
        logger.error(f'[ Failed - "{cmd}" ]')
        raise Exception(f'[ Failed - "{cmd}" ]')

    return outs.strip()
