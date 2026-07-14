#!/usr/bin/env python3
#
# Copyright (c) 2026 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Wheel installer that works without pip.
Handles entry_points, .data directories, and path traversal protection.
"""
import configparser
import glob
import os
import re
import shutil
import sys
import zipfile


SCRIPT_TEMPLATE = """\
#!{interpreter}
import re
import sys
from {module} import {function}
if __name__ == "__main__":
    sys.argv[0] = re.sub(r"(-script\\.pyw|\\.exe)?$", "", sys.argv[0])
    sys.exit({callable}())
"""


def get_site_packages():
    import site
    for path in site.getsitepackages():
        if os.path.isdir(path) and os.access(path, os.W_OK):
            return path
    import sysconfig
    return sysconfig.get_path("purelib")


def get_scripts_dir(site_packages):
    bin_dir = os.path.join(os.path.dirname(site_packages), "..", "bin")
    bin_dir = os.path.realpath(bin_dir)
    if os.path.isdir(bin_dir):
        return bin_dir
    return os.path.join(sys.prefix, "bin")


def get_interpreter(scripts_dir):
    python_path = os.path.join(scripts_dir, "python3")
    if os.path.exists(python_path):
        return python_path
    python_path = os.path.join(scripts_dir, "python")
    if os.path.exists(python_path):
        return python_path
    return sys.executable


def safe_extract_member(archive, member, target_dir):
    target = os.path.realpath(target_dir)
    member_path = os.path.realpath(os.path.join(target, member))
    if not member_path.startswith(target + os.sep) and member_path != target:
        raise ValueError("Wheel contains unsafe path: {}".format(member))
    return member_path


def install_data_directory(archive, data_prefix, site_packages, scripts_dir):
    scheme_map = {
        "purelib": site_packages,
        "platlib": site_packages,
        "scripts": scripts_dir,
        "headers": os.path.join(sys.prefix, "include"),
        "data": sys.prefix,
    }

    for member in archive.namelist():
        if not member.startswith(data_prefix):
            continue

        relative = member[len(data_prefix):]
        if not relative:
            continue

        parts = relative.split("/", 1)
        scheme = parts[0]
        if len(parts) < 2 or not parts[1]:
            continue

        target_base = scheme_map.get(scheme)
        if target_base is None:
            continue

        dest_path = os.path.join(target_base, parts[1])
        dest_dir = os.path.dirname(dest_path)
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)

        if member.endswith("/"):
            os.makedirs(dest_path, exist_ok=True)
        else:
            with archive.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

            if scheme == "scripts":
                os.chmod(dest_path, 0o755)


def install_entry_points(dist_info_path, scripts_dir, interpreter):
    entry_points_file = os.path.join(dist_info_path, "entry_points.txt")
    if not os.path.exists(entry_points_file):
        return

    config = configparser.ConfigParser()
    config.read(entry_points_file)

    if not config.has_section("console_scripts"):
        return

    for name, value in config.items("console_scripts"):
        match = re.match(r"^(.+):(.+)$", value.strip())
        if not match:
            continue

        module = match.group(1)
        function = match.group(2)

        script_content = SCRIPT_TEMPLATE.format(
            interpreter=interpreter,
            module=module,
            function=function,
            callable=function,
        )

        script_path = os.path.join(scripts_dir, name)
        with open(script_path, "w") as f:
            f.write(script_content)
        os.chmod(script_path, 0o755)


def remove_old_package(target_dir, dist_name, scripts_dir):
    normalized = dist_name.replace("-", "_")
    pattern = os.path.join(target_dir, "{}-*.dist-info".format(normalized))

    for dist_info_dir in glob.glob(pattern):
        entry_points_file = os.path.join(dist_info_dir, "entry_points.txt")
        if os.path.exists(entry_points_file):
            config = configparser.ConfigParser()
            config.read(entry_points_file)
            if config.has_section("console_scripts"):
                for name, _ in config.items("console_scripts"):
                    script_path = os.path.join(scripts_dir, name)
                    if os.path.isfile(script_path):
                        os.remove(script_path)

        record_file = os.path.join(dist_info_dir, "RECORD")
        if os.path.exists(record_file):
            with open(record_file) as f:
                for line in f:
                    rel_path = line.split(",")[0].strip()
                    if not rel_path:
                        continue
                    full_path = os.path.join(target_dir, rel_path)
                    if os.path.isfile(full_path):
                        os.remove(full_path)

        shutil.rmtree(dist_info_dir)
        print("  Removed: {}".format(os.path.basename(dist_info_dir)))


def install_wheel(wheel_path, site_packages, scripts_dir, interpreter):
    filename = os.path.basename(wheel_path)
    parts = filename.split("-")
    if len(parts) < 3:
        raise ValueError("Invalid wheel filename: {}".format(filename))

    dist_name = parts[0]
    dist_version = parts[1]
    data_prefix = "{}-{}.data/".format(dist_name, dist_version)

    print("Installing {} -> {}".format(filename, site_packages))
    remove_old_package(site_packages, dist_name, scripts_dir)

    with zipfile.ZipFile(wheel_path, "r") as whl:
        for member in whl.namelist():
            safe_extract_member(whl, member, site_packages)

        regular_members = [
            m for m in whl.namelist() if not m.startswith(data_prefix)
        ]
        whl.extractall(site_packages, members=regular_members)

        install_data_directory(whl, data_prefix, site_packages, scripts_dir)

    dist_info_pattern = os.path.join(
        site_packages, "{}-*.dist-info".format(dist_name)
    )
    for dist_info_dir in glob.glob(dist_info_pattern):
        install_entry_points(dist_info_dir, scripts_dir, interpreter)

    print("  Done")


def collect_wheels(args):
    wheels = []
    for arg in args:
        if os.path.isfile(arg) and arg.endswith(".whl"):
            wheels.append(arg)
        elif os.path.isdir(arg):
            wheels.extend(sorted(glob.glob(os.path.join(arg, "*.whl"))))
    return wheels


def main():
    wheels = collect_wheels(sys.argv[1:])
    if not wheels:
        sys.stderr.write(
            "Usage: install_wheel.py <wheel_file.whl|directory> ...\n"
        )
        sys.exit(1)

    site_packages = get_site_packages()
    scripts_dir = get_scripts_dir(site_packages)
    interpreter = get_interpreter(scripts_dir)

    print("Target: {}".format(site_packages))
    print("Scripts: {}".format(scripts_dir))

    for whl in wheels:
        install_wheel(whl, site_packages, scripts_dir, interpreter)

    print("\nInstalled {} wheel(s).".format(len(wheels)))


if __name__ == "__main__":
    main()
