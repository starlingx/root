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
        if not os.path.exists(self.pkginfo["packdir"]):
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
            self.pkginfo["pkgname"] = self.meta_data["debname"]

        self.versions["full_version"] = str(self.meta_data["debver"])
        self.versions["upstream_version"] = BaseVersion(self.versions["full_version"]).upstream_version
        self.versions["debian_revision"] = BaseVersion(self.versions["full_version"]).debian_revision
        self.versions["epoch"] = BaseVersion(self.versions["full_version"]).epoch

        self.logger.info("=== Package Name: %s", self.pkginfo["pkgname"])
        self.logger.info("=== Package Version: %s", self.versions["full_version"])
        self.logger.info("=== Package Path: %s", self.pkginfo["pkgpath"])

        srcdir = self.pkginfo["pkgname"] + "-" + self.versions["upstream_version"]
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

        src_dirname = self.meta_data["src_path"]
        src_path = os.path.expandvars(src_dirname)
        if not os.path.isdir(src_path):
            src_path = os.path.abspath(os.path.join(self.pkginfo["pkgpath"], src_dirname))
            if not os.path.exists(src_path):
                self.logger.error("%s: No such directory", src_path)
                raise ValueError(f"{src_path}: No such directory")

        if "SRC_GITREVCOUNT" in revision_data:
            if "SRC_BASE_SRCREV" not in revision_data:
                self.logger.error("SRC_BASE_SRCREV must be set")
                raise ValueError("SRC_BASE_SRCREV must be set")
            revision += int(run_shell_cmd(git_rev_list_from % (src_path, revision_data["SRC_BASE_SRCREV"]), self.logger))
            revision += int(run_shell_cmd(git_status % src_path, self.logger))

        return dist + "." + str(revision)

    def checksum(self, pkgpath):

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

        self.logger.info("Overwrite the debian folder by %s", deb_folder)
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

        files = os.path.join(self.pkginfo["debfolder"], "files")
        if not os.path.isdir(files) or not os.path.exists(files):
            return True

        for root, _, files in os.walk(files):
            for name in files:
                os.path.join(root, name)
                run_shell_cmd('cp -r %s %s' % (os.path.join(root, name), self.pkginfo["srcdir"]), self.logger)

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

    def download_tarball(self):

        tarball_name = self.meta_data["dl_path"]["name"]
        tarball_url = self.meta_data["dl_path"]["url"]
        tarball_md5sum = self.meta_data["dl_path"]["md5sum"]

        tarball_file = os.path.join(self.pkginfo["packdir"], tarball_name)
        if os.path.exists(tarball_file):
            md5sum = run_shell_cmd('md5sum %s |cut -d" " -f1' % tarball_file, self.logger)
            if md5sum != tarball_md5sum:
                self.logger.info("The md5sum of existing %s is %s, but %s is expected, redownload", tarball_file, md5sum, tarball_md5sum)
                os.remove(tarball_file)

        if not os.path.exists(tarball_file):
            download(tarball_url, tarball_file, self.logger)
            md5sum = run_shell_cmd('md5sum %s |cut -d" " -f1' % tarball_file, self.logger)
            if md5sum != tarball_md5sum:
                self.logger.error("The md5sum of %s is %s, but %s is expected", tarball_file, md5sum, tarball_md5sum)
                raise ValueError(f"The md5sum of {tarball_file} is {md5sum}, but {tarball_md5sum} is expected")

        targz = re.match(r'.*.(tar\.gz|tar\.bz2|tar\.xz|tgz)$', tarball_name)
        if targz is None:
            self.logger.error('Not supported tarball type, the supported types are: tar.gz|tar.bz2|tar.xz|tgz')
            raise ValueError('Not supported tarball type, the supported types are: tar.gz|tar.bz2|tar.xz|tgz')

        targz = targz.group(1)
        if targz == "tgz":
            targz = "tar.gz"

        # Refer to untar.py of debmake python module
        if targz == 'tar.bz2':
            cmd = 'tar --bzip2 -xvf %s '
            cmdx = 'tar --bzip2 -tf %s '
        elif targz == 'tar.xz':
            cmd = 'tar --xz -xvf %s '
            cmdx = 'tar --xz -tf %s '
        else:
            cmd = 'tar -xvzf %s '
            cmdx = 'tar -tzf %s '

        cmdx = cmdx + '| awk -F "/" \'{print $1}\' | sort | uniq'
        topdir = run_shell_cmd(cmdx % tarball_file, self.logger)

        # The tar ball has top directory
        if len(topdir.split('\n')) == 1:
            # Remove the top diretory
            cmd += '--strip-components 1 -C %s'
        # The tar ball is extracted under $PWD by default
        else:
            cmd += '-C %s'

        os.mkdir(self.pkginfo["srcdir"])
        run_shell_cmd(cmd % (tarball_file, self.pkginfo["srcdir"]), self.logger)
        self.create_orig_tarball()
        self.update_deb_folder()

        return True

    def upload_deb_package(self):

        self.logger.info("Uploading the dsc files of %s to local repo %s", self.pkginfo["pkgname"], self.srcrepo)
        # strip epoch
        ver = self.versions["full_version"].split(":")[-1]
        dsc_file = os.path.join(self.pkginfo["packdir"], self.pkginfo["pkgname"] + "_" + ver + ".dsc")

        cmd = "repo_manage.py upload_pkg -p %s  -r %s"
        run_shell_cmd(cmd % (dsc_file, self.srcrepo), self.logger)

        return True

    def download_deb_archive(self):

        archive_url = self.meta_data["archive"]
        dsc_filename = self.pkginfo["pkgname"] + "_" + self.versions["full_version"] + ".dsc"
        dsc_file = os.path.join(archive_url, dsc_filename)
        local_dsc = os.path.join(self.pkginfo["packdir"], dsc_filename)
        download(dsc_file, local_dsc, self.logger)
        with open(local_dsc) as f:
            c = debian.deb822.Dsc(f)

        for f in c['Files']:
            local_f = os.path.join(self.pkginfo["packdir"], f['name'])
            remote_f = os.path.join(archive_url, f['name'])
            download(remote_f, local_f, self.logger)
        run_shell_cmd("cd %s;dpkg-source -x %s" % (self.pkginfo["packdir"], dsc_filename), self.logger)

        self.apply_deb_patches()

        return True

    def download_deb_package(self):

        fullname = self.pkginfo["pkgname"] + "=" + self.versions["full_version"]
        supported_versions = list()

        apt_pkg.init()
        sources = apt_pkg.SourceRecords()
        source_lookup = sources.lookup(self.pkginfo["pkgname"])
        while source_lookup and self.versions["full_version"] != sources.version:
            supported_versions.append(sources.version)
            source_lookup = sources.lookup(self.pkginfo["pkgname"])

        if not source_lookup:
            self.logger.error("No source for %s", fullname)
            self.logger.info("The supported versions are %s", supported_versions)
            raise ValueError(f"No source for {fullname}")
        self.logger.info("Found %s", fullname)

        self.logger.info("Fetch %s to %s", fullname, self.pkginfo["packdir"])
        # first_binary = sources.binaries[0]
        # cache = apt.cache.Cache()
        # package = cache[first_binary]
        # package.candidate.fetch_source(destdir=self.pkginfo["packdir"],unpack=True)
        run_shell_cmd("cd %s; apt-get source %s" % (self.pkginfo["packdir"], fullname), self.logger)

        self.logger.info("Deploy %s to %s", fullname, self.pkginfo["srcdir"])
        if self.srcrepo is not None:
            self.upload_deb_package()
        self.apply_deb_patches()

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
        origtargz = self.pkginfo["pkgname"] + '_' + self.versions["upstream_version"] + '.orig.tar.gz'
        run_shell_cmd('cd %s; tar czvf %s %s' % (self.pkginfo["packdir"], origtargz, srcname), self.logger)

    def create_src_package(self):

        src_dirname = self.meta_data["src_path"]
        src_path = os.path.expandvars(src_dirname)
        if not os.path.isdir(src_path):
            src_path = os.path.abspath(os.path.join(self.pkginfo["pkgpath"], src_dirname))
            if not os.path.exists(src_path):
                self.logger.error("%s: No such directory", src_path)
                raise ValueError(f"{src_path}: No such directory")

        # cp the .git folder, the git meta files in .git are symbol link, so need -L
        run_shell_cmd('cp -rL %s %s' % (src_path, self.pkginfo["srcdir"]), self.logger)
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

    def package(self, pkgpath):

        self.setup(pkgpath)
        if "dl_hook" in self.meta_data:
            self.run_dl_hook()
        elif "dl_path" in self.meta_data:
            self.download_tarball()
        elif "src_path" in self.meta_data:
            self.create_src_package()
        elif "archive" in self.meta_data:
            self.download_deb_archive()
        else:
            self.download_deb_package()

        self.apply_src_patches()
        self.copy_custom_files()

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
            target = os.path.join(self.output, f)
            source = os.path.join(self.pkginfo["packdir"], f)
            self.logger.info("Copy %s to %s", source, target)
            shutil.copy(source, self.output)

        return files
