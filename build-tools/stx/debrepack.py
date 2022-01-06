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
# Copyright (C) 2021 WindRiver Corporation

# import apt
import apt_pkg
import debian.deb822
from debian.debian_support import BaseVersion
import git
import hashlib
import logging
import os
import progressbar
import re
import shutil
import subprocess
import sys
# import urllib.request
import yaml


RELEASENOTES = " ".join([os.environ.get('PROJECT'), os.environ.get('MY_RELEASE'), "distribution"])
DIST = os.environ.get('STX_DIST')


class DownloadProgress():
    def __init__(self):
        self.pbar = None

    def __call__(self, block_num, block_size, total_size):

        if total_size < 0:
            return

        if not self.pbar:
            self.pbar = progressbar.ProgressBar(maxval=total_size)
            self.pbar.start()

        downloaded = block_num * block_size
        if downloaded < total_size:
            self.pbar.update(downloaded)
        else:
            self.pbar.finish()


def get_str_md5(text):

    md5obj = hashlib.md5()
    md5obj.update(text.encode())
    _hash = md5obj.hexdigest()
    return str(_hash)


def tar_cmd(tarball_name, logger):

    targz = re.match(r'.*.(tar\.gz|tar\.bz2|tar\.xz|tgz)$', tarball_name)
    if targz is None:
        logger.error('Not supported tarball type, the supported types are: tar.gz|tar.bz2|tar.xz|tgz')
        raise ValueError(f'{tarball_name} type is not supported')

    # Refer to untar.py of debmake python module
    if targz == 'tar.bz2':
        cmd = 'tar --bzip2 -xf %s '
        cmdx = 'tar --bzip2 -tf %s '
        cmdc = 'tar --bzip2 -cf %s %s '
    elif targz == 'tar.xz':
        cmd = 'tar --xz -xf %s '
        cmdx = 'tar --xz -tf %s '
        cmdc = 'tar --xz -cf %s %s '
    else:
        cmd = 'tar -xzf %s '
        cmdx = 'tar -tzf %s '
        cmdc = 'tar -czf %s %s '

    return cmd, cmdx, cmdc


def get_topdir(tarball_file, logger):

    if not os.path.exists(tarball_file):
        logger.error('Not such file %s', tarball_file)
        raise IOError

    tarball_name = os.path.basename(tarball_file)
    _, cmdx, _ = tar_cmd(tarball_name, logger)
    cmdx = cmdx + '| awk -F "/" \'{print $%s}\' | sort | uniq'
    topdir = run_shell_cmd(cmdx % (tarball_file, "1"), logger)
    subdir = run_shell_cmd(cmdx % (tarball_file, "2"), logger)

    # The tar ball has top directory
    if len(topdir.split('\n')) == 1 and subdir != '':
        return topdir.split('\n')[0]
    # Return None if no top directory
    else:
        return None


def md5_checksum(dl_file, md5sum, logger):

    if not os.path.exists(dl_file):
        return False

    md5 = run_shell_cmd('md5sum %s |cut -d" " -f1' % dl_file, logger)
    if md5 != md5sum:
        logger.debug(f"The {md5} not match the expected {md5sum}")
        return False
    return True


def run_shell_cmd(cmd, logger):

    logger.info(f'[ Run - "{cmd}" ]')
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, shell=True)
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


def download(url, savepath, logger):

    logger.info(f"Download {url} to {savepath}")
    download_cmd = "wget -t 5 --wait=15 %s -O %s"
    run_shell_cmd(download_cmd % (url, savepath), logger)
    # urllib.request.urlretrieve(url, savepath, DownloadProgress())

    return True


def is_git_repo(path):
    try:
        _ = git.Repo(path).git_dir
        return True
    except git.exc.InvalidGitRepositoryError:
        return False


class Parser():
    level_relations = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        'crit': logging.CRITICAL
    }

    def __init__(self, basedir, output, loglevel='info', srcrepo=None):

        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(self.level_relations.get(loglevel))
        formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        self.logger.addHandler(console)

        if not os.path.isdir(basedir):
            self.logger.error("%s: No such file or directory", basedir)
            raise Exception(f"{basedir}: No such file or directory")
        self.basedir = os.path.abspath(basedir)

        if not os.path.isdir(output):
            self.logger.error("%s: No such file or directory", output)
            raise Exception(f"{output}: No such file or directory")
        self.output = os.path.abspath(output)

        self.srcrepo = srcrepo
        self.meta_data = dict()
        self.versions = dict()
        self.pkginfo = dict()

    def setup(self, pkgpath):

        if not os.path.isdir(pkgpath):
            self.logger.error("%s: No such file or directory", pkgpath)
            raise Exception(f"{pkgpath}: No such file or directory")

        self.pkginfo["pkgpath"] = os.path.abspath(pkgpath)
        self.pkginfo["pkgname"] = os.path.basename(pkgpath)
        self.pkginfo["packdir"] = os.path.join(self.basedir, self.pkginfo["pkgname"])
        if os.path.exists(self.pkginfo["packdir"]):
            shutil.rmtree(self.pkginfo["packdir"])
        os.mkdir(self.pkginfo["packdir"])

        logfile = os.path.join(self.pkginfo["packdir"], self.pkginfo["pkgname"] + ".log")
        if os.path.exists(logfile):
            os.remove(logfile)
        logfile_handler = logging.FileHandler(logfile, 'w')
        formatter = logging.Formatter('%(levelname)s - %(message)s')
        logfile_handler.setFormatter(formatter)
        self.logger.addHandler(logfile_handler)

        self.pkginfo["debfolder"] = os.path.join(self.pkginfo["pkgpath"], "debian")
        if not os.path.isdir(self.pkginfo["debfolder"]):
            self.logger.error("No debian folder")
            raise Exception("No debian folder")

        meta_data = os.path.join(self.pkginfo["debfolder"], "meta_data.yaml")
        if not os.path.exists(meta_data):
            self.logger.error("Not find meta_data.yaml")
            raise Exception("Not find meta_data.yaml")
        with open(meta_data) as f:
            self.meta_data = yaml.full_load(f)

        if "debver" not in self.meta_data:
            self.logger.error("No debver defined in meta_data.yaml")
            raise Exception("No debver defined in meta_data.yaml")

        if "debname" in self.meta_data:
            self.pkginfo["debname"] = self.meta_data["debname"]
        else:
            self.pkginfo["debname"] = self.pkginfo["pkgname"]

        if "src_path" in self.meta_data and self.meta_data["src_path"] is not None:
            src_dirname = self.meta_data["src_path"]
            src_path = os.path.expandvars(src_dirname)
            if not os.path.isabs(src_path):
                src_path = os.path.abspath(os.path.join(self.pkginfo["pkgpath"], src_dirname))
                if not os.path.exists(src_path):
                    self.logger.error("%s: No such directory", src_path)
                    raise ValueError(f"{src_path}: No such directory")
            self.meta_data["src_path"] = src_path

        if "src_files" in self.meta_data:
            src_files = self.meta_data['src_files']
            self.meta_data['src_files'] = []
            for src_file in src_files:
                src_path = os.path.expandvars(src_file)
                if not os.path.exists(src_path):
                    src_path = os.path.join(self.pkginfo["pkgpath"], src_file)
                if not os.path.exists(src_path):
                    self.logger.error("No such file %s", src_path)
                    raise IOError
                self.meta_data['src_files'].append(src_path)

        self.versions["full_version"] = str(self.meta_data["debver"])
        self.versions["upstream_version"] = BaseVersion(self.versions["full_version"]).upstream_version
        self.versions["debian_revision"] = BaseVersion(self.versions["full_version"]).debian_revision
        self.versions["epoch"] = BaseVersion(self.versions["full_version"]).epoch

        self.logger.info("=== Package Name: %s", self.pkginfo["pkgname"])
        self.logger.info("=== Debian Package Name: %s", self.pkginfo["debname"])
        self.logger.info("=== Package Version: %s", self.versions["full_version"])
        self.logger.info("=== Package Path: %s", self.pkginfo["pkgpath"])

        srcdir = self.pkginfo["debname"] + "-" + self.versions["upstream_version"]
        self.pkginfo["srcdir"] = os.path.join(self.pkginfo["packdir"], srcdir)
        if os.path.exists(self.pkginfo["srcdir"]):
            shutil.rmtree(self.pkginfo["srcdir"])

    def set_revision(self):

        revision = 0
        dist = ""
        if "revision" not in self.meta_data:
            return dist

        revision_data = self.meta_data["revision"]
        if "dist" in revision_data:
            if revision_data["dist"] is not None:
                dist = os.path.expandvars(revision_data["dist"])

        git_rev_list = "cd %s;git rev-list --count HEAD ."
        git_rev_list_from = "cd %s;git rev-list --count %s..HEAD ."
        git_status = "cd %s;git status --porcelain . | wc -l"

        if "PKG_GITREVCOUNT" in revision_data:
            if "PKG_BASE_SRCREV" in revision_data:
                revision = int(run_shell_cmd(git_rev_list_from % (self.pkginfo["debfolder"], revision_data["PKG_BASE_SRCREV"]), self.logger))
            else:
                revision = int(run_shell_cmd(git_rev_list % self.pkginfo["debfolder"], self.logger))
            revision += int(run_shell_cmd(git_status % self.pkginfo["debfolder"], self.logger))

        if "src_path" not in self.meta_data:
            return dist + "." + str(revision)

        src_path = self.meta_data["src_path"]
        if src_path is None:
            return dist + "." + str(revision)

        if "SRC_GITREVCOUNT" in revision_data:
            if "SRC_BASE_SRCREV" not in revision_data:
                self.logger.error("SRC_BASE_SRCREV must be set")
                raise ValueError("SRC_BASE_SRCREV must be set")
            revision += int(run_shell_cmd(git_rev_list_from % (src_path, revision_data["SRC_BASE_SRCREV"]), self.logger))
            revision += int(run_shell_cmd(git_status % src_path, self.logger))

        return dist + "." + str(revision)

    def checksum(self, pkgpath):

        self.setup(pkgpath)
        if not os.path.isdir(pkgpath):
            self.logger.error("%s: No such file or directory", pkgpath)
            raise Exception(f"{pkgpath}: No such file or directory")

        debfolder = os.path.join(pkgpath, "debian")
        if not os.path.isdir(debfolder):
            self.logger.error("%s: no such directory", debfolder)
            raise Exception(f"{debfolder}: no such directory")

        content = ""
        for root, _, files in os.walk(debfolder):
            for name in files:
                f = open(os.path.join(root, name), 'r', encoding="ISO-8859-1")
                content += f.read()

        if "src_path" in self.meta_data and self.meta_data["src_path"] is not None:
            for root, _, files in os.walk(self.meta_data["src_path"]):
                for name in files:
                    try:
                        f = open(os.path.join(root, name), 'r', encoding="ISO-8859-1")
                        content += f.read()
                    except IOError:
                        self.logger.error("Can't open %s" % name)
                        raise IOError

        if "src_files" in self.meta_data:
            for src_file in self.meta_data['src_files']:
                if os.path.isdir(src_file):
                    for root, _, files in os.walk(src_file):
                        for name in files:
                            try:
                                f = open(os.path.join(root, name), 'r', encoding="ISO-8859-1")
                                content += f.read()
                            except IOError:
                                self.logger.error("Can't open %s" % name)
                                raise IOError
                else:
                    try:
                        f = open(src_file, 'r', encoding="ISO-8859-1")
                        content += f.read()
                    except IOError:
                        self.logger.error("Can't open %s" % src_file)
                        raise IOError

        return get_str_md5(content)

    def set_deb_format(self):

        deb_format = run_shell_cmd('dpkg-source --print-format %s' % self.pkginfo["srcdir"], self.logger)
        if re.match("1.0", deb_format):
            return "1.0", None

        format_ver, format_type = deb_format.split(" ")
        format_ver = format_ver.strip()
        format_type = format_type.strip("()")

        return format_ver, format_type

    def update_deb_folder(self):

        metadata = os.path.join(self.pkginfo["debfolder"], "deb_folder")
        if not os.path.isdir(metadata):
            return True

        deb_folder = os.path.join(self.pkginfo["srcdir"], "debian")
        if not os.path.exists(deb_folder):
            os.mkdir(deb_folder)

        self.logger.info("Overwrite the debian folder by %s", metadata)
        run_shell_cmd('cp -r %s/* %s' % (metadata, deb_folder), self.logger)

        series = os.path.join(metadata, "patches/series")
        if not os.path.isfile(series):
            return True

        format_ver, format_type = self.set_deb_format()
        if format_type == "quilt" and format_ver == "3.0":
            return True

        f = open(series)
        patches = f.readlines()
        patches_src = os.path.dirname(series)
        f.close()

        pwd = os.getcwd()
        os.chdir(self.pkginfo["srcdir"])
        for patch in patches:
            patch_file = patch.strip()
            # Skip comment lines and blank lines
            if patch_file.startswith('#') or patch_file == "":
                continue
            self.logger.info("Apply src patch: %s", patch_file)
            patch = os.path.join(patches_src, patch_file)
            run_shell_cmd('patch -p1 < %s' % patch, self.logger)
        os.chdir(pwd)

        return True

    def copy_custom_files(self):

        if "src_files" in self.meta_data:
            for src_file in self.meta_data['src_files']:
                run_shell_cmd('cp -rL %s %s' % (src_file, self.pkginfo["srcdir"]),
                              self.logger)

        if "dl_files" in self.meta_data:
            pwd = os.getcwd()
            os.chdir(self.pkginfo["packdir"])
            for dl_file in self.meta_data['dl_files']:
                dir_name = self.meta_data['dl_files'][dl_file]['topdir']
                dl_path = os.path.join(self.pkginfo["packdir"], dl_file)
                if not os.path.exists(dl_path):
                    self.logger.error("No such file %s in local mirror", dl_file)
                    raise IOError
                if dir_name is not None:
                    cmd, _, cmdc = tar_cmd(dl_path, self.logger)
                    # The tar ball has top directory
                    if get_topdir(dl_path, self.logger) is not None:
                        # Remove the top diretory
                        cmd += '--strip-components 1 -C %s'
                    # The tar ball is extracted under $PWD by default
                    else:
                        cmd += '-C %s'
                    run_shell_cmd("mkdir -p %s" % dir_name, self.logger)
                    run_shell_cmd(cmd % (dl_path, dir_name), self.logger)
                    run_shell_cmd(cmdc % (dl_path, dir_name), self.logger)

                run_shell_cmd('cp -rL %s %s' % (dl_path, self.pkginfo["srcdir"]),
                              self.logger)
                os.chdir(pwd)

        files = os.path.join(self.pkginfo["debfolder"], "files")
        if not os.path.isdir(files) or not os.path.exists(files):
            return True

        for root, _, files in os.walk(files):
            for name in files:
                os.path.join(root, name)
                run_shell_cmd('cp -rL %s %s' % (os.path.join(root, name), self.pkginfo["srcdir"]), self.logger)

        return True

    def apply_src_patches(self):

        format_ver, format_type = self.set_deb_format()
        series = os.path.join(self.pkginfo["debfolder"], "patches/series")
        if not os.path.isfile(series):
            return True

        f = open(series)
        patches = f.readlines()
        patches_src = os.path.dirname(series)
        f.close()

        patches_folder = os.path.join(self.pkginfo["srcdir"], "debian/patches")
        series_file = os.path.join(self.pkginfo["srcdir"], "debian/patches/series")
        if not os.path.isdir(patches_folder):
            os.mkdir(patches_folder)
            os.mknod(series_file)

        pwd = os.getcwd()
        os.chdir(self.pkginfo["srcdir"])
        for patch in patches:
            patch_file = patch.strip()
            # Skip comment lines and blank lines
            if patch_file.startswith('#') or patch_file == "":
                continue
            self.logger.info("Apply src patch: %s", patch_file)
            patch = os.path.join(patches_src, patch_file)
            if format_ver == "1.0":
                run_shell_cmd('patch -p1 < %s' % patch, self.logger)
            else:
                if format_type == "quilt":
                    run_shell_cmd('cp -r %s %s' % (patch, patches_folder), self.logger)
                    with open(series_file, 'a') as f:
                        f.write(patch_file + "\n")
                    f.close()
                elif format_type == "native":
                    run_shell_cmd('patch -p1 < %s' % patch, self.logger)
                else:
                    self.logger.error('Invalid deb format: %s %s', format_ver, format_type)
                    raise Exception(f'[ Invalid deb format: {format_ver} {format_type} ]')

        os.chdir(pwd)
        return True

    def apply_deb_patches(self):

        series = os.path.join(self.pkginfo["debfolder"], "deb_patches/series")
        if not os.path.isfile(series):
            return True
        f = open(series)
        patches = f.readlines()
        patches_src = os.path.dirname(series)

        pwd = os.getcwd()
        os.chdir(self.pkginfo["srcdir"])
        for patch in patches:
            patch_file = patch.strip()
            # Skip comment lines and blank lines
            if patch_file.startswith('#') or patch_file == "":
                continue
            self.logger.info("Apply deb patch: %s", patch_file)
            patch = os.path.join(patches_src, patch_file)
            run_shell_cmd("patch -p1 < %s" % patch, self.logger)
        os.chdir(pwd)

        return True

    def extract_tarball(self):

        tarball_name = self.meta_data["dl_path"]["name"]
        tarball_file = os.path.join(self.pkginfo["packdir"], tarball_name)

        cmd, _, _ = tar_cmd(tarball_name, self.logger)
        # The tar ball has top directory
        if get_topdir(tarball_file, self.logger) is not None:
            # Remove the top diretory
            cmd += '--strip-components 1 -C %s'
        # The tar ball is extracted under $PWD by default
        else:
            cmd += '-C %s'

        os.mkdir(self.pkginfo["srcdir"])
        run_shell_cmd(cmd % (tarball_file, self.pkginfo["srcdir"]), self.logger)
        self.copy_custom_files()
        self.create_orig_tarball()
        self.update_deb_folder()
        self.apply_deb_patches()

        return True

    def upload_deb_package(self):

        self.logger.info("Uploading the dsc files of %s to local repo %s", self.pkginfo["debname"], self.srcrepo)
        # strip epoch
        ver = self.versions["full_version"].split(":")[-1]
        dsc_file = os.path.join(self.pkginfo["packdir"], self.pkginfo["debname"] + "_" + ver + ".dsc")

        cmd = "repo_manage.py upload_pkg -p %s  -r %s"
        run_shell_cmd(cmd % (dsc_file, self.srcrepo), self.logger)

        return True

    def create_orig_tarball(self):

        if not os.path.exists(self.pkginfo["srcdir"]):
            self.logger.error("%s: no such directory", self.pkginfo["srcdir"])
            raise ValueError(f'{self.pkginfo["srcdir"]}: no such directory')

        if is_git_repo(self.pkginfo["srcdir"]):
            debian_folder = os.path.join(self.pkginfo["srcdir"], "debian")
            if os.path.exists(debian_folder):
                self.logger.info("Generate orig tarballs from git repositry %s", self.pkginfo["srcdir"])
                run_shell_cmd('cd %s; gbp export-orig --upstream-tree=HEAD' % self.pkginfo["srcdir"], self.logger)
                return
            # remove .git directory
            run_shell_cmd('rm -rf %s' % os.path.join(self.pkginfo["srcdir"], ".git"), self.logger)

        srcname = os.path.basename(self.pkginfo["srcdir"])
        origtargz = self.pkginfo["debname"] + '_' + self.versions["upstream_version"] + '.orig.tar.gz'
        run_shell_cmd('cd %s; tar czvf %s %s' % (self.pkginfo["packdir"], origtargz, srcname), self.logger)

    def create_src_package(self):

        src_path = self.meta_data["src_path"]
        if src_path is None:
            os.mkdir(self.pkginfo["srcdir"])
        else:
            # cp the .git folder, the git meta files in .git are symbol link, so need -L
            run_shell_cmd('cp -rL %s %s' % (src_path, self.pkginfo["srcdir"]), self.logger)

        self.copy_custom_files()
        self.create_orig_tarball()
        self.update_deb_folder()

        return True

    def run_dl_hook(self):

        dl_hook = self.meta_data["dl_hook"]
        if not os.path.isabs(dl_hook):
            dl_hook = os.path.join(self.pkginfo["debfolder"], dl_hook)
        if not os.path.exists(dl_hook):
            self.logger.error("%s doesn't exist", dl_hook)
            raise ValueError(f"{dl_hook} doesn't exist")
        run_shell_cmd('cp -r %s %s' % (dl_hook, self.pkginfo["packdir"]), self.logger)

        pwd = os.getcwd()
        os.chdir(self.pkginfo["packdir"])
        if not os.access("dl_hook", os.X_OK):
            self.logger.error("dl_hook can't execute")
            raise ValueError("dl_hook can't execute")
        run_shell_cmd('./dl_hook %s' % os.path.basename(self.pkginfo["srcdir"]), self.logger)
        origtar = self.pkginfo["pkgname"] + '_' + self.versions["upstream_version"]
        origtargz = origtar + '.orig.tar.gz'
        origtarxz = origtar + '.orig.tar.xz'
        if not os.path.exists(origtargz) and not os.path.exists(origtarxz):
            self.create_orig_tarball()
        os.chdir(pwd)
        self.update_deb_folder()
        self.apply_deb_patches()

    def download(self, pkgpath, mirror):

        self.setup(pkgpath)
        if not os.path.exists(mirror):
            self.logger.error("No such %s directory", mirror)
            raise ValueError(f"No such {mirror} directory")

        saveto = os.path.join(mirror, self.pkginfo["pkgname"])
        if not os.path.exists(saveto):
            os.mkdir(saveto)

        pwd = os.getcwd()
        os.chdir(saveto)
        if "dl_path" in self.meta_data:
            dl_file = self.meta_data["dl_path"]["name"]
            url = self.meta_data["dl_path"]["url"]
            md5sum = self.meta_data["dl_path"]["md5sum"]
            if not md5_checksum(dl_file, md5sum, self.logger):
                download(url, dl_file, self.logger)
        elif "archive" in self.meta_data:
            url = self.meta_data["archive"]
            ver = self.versions["full_version"].split(":")[-1]
            dsc_filename = self.pkginfo["debname"] + "_" + ver + ".dsc"
            dsc_file = os.path.join(url, dsc_filename)
            run_shell_cmd("dget -d %s" % dsc_file, self.logger)
        elif "src_path" not in self.meta_data and "dl_hook" not in self.meta_data:
            fullname = self.pkginfo["debname"] + "=" + self.versions["full_version"]
            supported_versions = list()

            apt_pkg.init()
            sources = apt_pkg.SourceRecords()
            source_lookup = sources.lookup(self.pkginfo["debname"])
            while source_lookup and self.versions["full_version"] != sources.version:
                supported_versions.append(sources.version)
                source_lookup = sources.lookup(self.pkginfo["debname"])

            if not source_lookup:
                self.logger.error("No source for %s", fullname)
                self.logger.info("The supported versions are %s", supported_versions)
                raise ValueError(f"No source for {fullname}")

            self.logger.info("Fetch %s to %s", fullname, self.pkginfo["packdir"])
            run_shell_cmd("apt-get source -d %s" % fullname, self.logger)
            if self.srcrepo is not None:
                self.upload_deb_package()

        if "dl_files" in self.meta_data:
            for dl_file in self.meta_data['dl_files']:
                url = self.meta_data['dl_files'][dl_file]['url']
                md5sum = self.meta_data['dl_files'][dl_file]['md5sum']
                if not md5_checksum(dl_file, md5sum, self.logger):
                    download(url, dl_file, self.logger)
        os.chdir(pwd)

    def package(self, pkgpath, mirror):

        self.setup(pkgpath)
        if not os.path.exists(mirror):
            self.logger.error("No such %s directory", mirror)
            raise ValueError(f"No such {mirror} directory")

        sources = os.path.join(mirror, self.pkginfo["pkgname"])
        if os.path.exists(sources):
            run_shell_cmd('cp -r %s %s' % (sources, self.basedir), self.logger)

        if "dl_hook" in self.meta_data:
            self.run_dl_hook()
        elif "dl_path" in self.meta_data:
            self.extract_tarball()
        elif "src_path" in self.meta_data:
            self.create_src_package()
        else:
            ver = self.versions["full_version"].split(":")[-1]
            dsc_filename = self.pkginfo["debname"] + "_" + ver + ".dsc"
            dsc_file = os.path.join(self.pkginfo["packdir"], dsc_filename)
            if not os.path.exists(dsc_file):
                self.logger.error("No dsc file \"%s\" was found in local mirror." % dsc_filename)
                raise IOError
            run_shell_cmd("cd %s;dpkg-source -x %s" % (self.pkginfo["packdir"], dsc_filename), self.logger)
            self.apply_deb_patches()
        self.apply_src_patches()

        self.logger.info("Repackge the package %s", self.pkginfo["srcdir"])

        changelog = os.path.join(self.pkginfo["srcdir"], 'debian/changelog')
        src = run_shell_cmd('dpkg-parsechangelog -l %s --show-field source' % changelog, self.logger)
        ver = run_shell_cmd('dpkg-parsechangelog -l %s --show-field version' % changelog, self.logger)
        ver += self.set_revision()
        run_shell_cmd('cd %s; dch -p -D stable -v %s %s' % (self.pkginfo["srcdir"], ver, RELEASENOTES), self.logger)
        # strip epoch
        ver = ver.split(":")[-1]

        # Skip building(-S) and skip checking dependence(-d)
        run_shell_cmd('cd %s; dpkg-buildpackage -nc -us -uc -S -d' % self.pkginfo["srcdir"], self.logger)

        dsc_file = src + "_" + ver + ".dsc"
        with open(os.path.join(self.pkginfo["packdir"], dsc_file)) as f:
            c = debian.deb822.Dsc(f)

        files = list()
        files.append(dsc_file)
        for f in c['Files']:
            files.append(f['name'])

        for f in files:
            source = os.path.join(self.pkginfo["packdir"], f)
            run_shell_cmd('cp -Lr %s %s' % (source, self.output), self.logger)

        return files
