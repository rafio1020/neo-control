#!/usr/bin/python3
"""Read-only install checks and explicit dependency/boot-image setup commands.

SPDX-License-Identifier: GPL-3.0-or-later
"""
import argparse
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys


def distro_family(info):
    ids = {info.get('ID', ''), *info.get('ID_LIKE', '').split()}
    if ids & {'cachyos', 'arch'}:
        return 'arch'
    if 'fedora' in ids:
        return 'fedora'
    return 'other'


def dependency_command(info, kernel, pkgbase=None):
    family = distro_family(info)
    if family == 'arch':
        if not pkgbase or not re.fullmatch(r'linux[a-z0-9.+_-]*', pkgbase):
            raise ValueError('Cannot identify the running kernel package. Install its matching headers using CachyOS Kernel Manager, reboot into that kernel, then retry.')
        # Full upgrade avoids unsupported partial upgrades on Arch-based systems.
        return ['pacman', '-Syu', '--needed', 'base-devel', 'dkms', 'python',
                'python-gobject', 'python-cairo', 'gtk4', 'polkit',
                'desktop-file-utils', 'clang', 'llvm', 'lld', f'{pkgbase}-headers']
    if family == 'fedora':
        return ['dnf', 'install', 'gcc', 'make', 'dkms', f'kernel-devel-{kernel}',
                'python3-gobject', 'python3-cairo', 'gtk4', 'polkit',
                'desktop-file-utils', 'clang', 'llvm', 'lld']
    raise ValueError('Automatic dependencies are supported on CachyOS/Arch and Fedora. Install Python 3, GTK4/PyGObject/Cairo, DKMS, matching kernel headers, a C toolchain and polkit using your distribution tools.')


def initramfs_command(info, which=shutil.which):
    if distro_family(info) == 'arch':
        # Keep the same boot-manager priority as CachyOS update-initramfs.
        for name, args in [('limine-mkinitcpio', []), ('mkinitcpio', ['--allpresets']),
                           ('limine-dracut', []), ('dracut', ['--regenerate-all', '--force'])]:
            if which(name):
                return [which(name), *args]
        booster = '/usr/lib/booster/regenerate_images'
        if which(booster):
            return [booster]
    elif distro_family(info) == 'fedora' and which('dracut'):
        return [which('dracut'), '--regenerate-all', '--force']
    raise ValueError('No supported boot-image generator found. Supported installer targets: CachyOS/Arch (Limine, mkinitcpio, dracut or booster) and Fedora (dracut).')


def header_problem(build, kernel):
    release = build / 'include/config/kernel.release'
    try:
        actual = release.read_text().strip()
        if actual != kernel:
            return f'Headers are for {actual}, but the running kernel is {kernel}. Reboot into the updated kernel and retry.'
        if not (build / 'Makefile').is_file() or not (build / 'include/config/auto.conf').is_file():
            return 'Incomplete kernel headers: reinstall the matching headers package.'
    except OSError:
        return f'Matching headers are missing for {kernel}. Run ./install.sh --install-deps; if the kernel was upgraded, reboot and rerun the installer.'
    return None


def check():
    errors = []
    model = Path('/sys/class/dmi/id/product_name')
    vendor = Path('/sys/class/dmi/id/sys_vendor')
    if not model.exists() or model.read_text().strip() != 'Predator PHN16-71' or vendor.read_text().strip() != 'Acer':
        errors.append('This driver supports only Acer Predator PHN16-71.')
    if not Path('/run/systemd/system').is_dir():
        errors.append('This installer requires a running systemd system.')
    kernel = platform.release()
    build = Path('/lib/modules') / kernel / 'build'
    problem = header_problem(build, kernel)
    if problem:
        errors.append(problem)
    else:
        config = (build / 'include/config/auto.conf').read_text()
        compiler = ['clang', 'ld.lld', 'llvm-ar'] if 'CONFIG_CC_IS_CLANG=y' in config.splitlines() else ['gcc']
        for tool in compiler:
            if not shutil.which(tool):
                errors.append(f'Missing kernel build tool: {tool}. Run ./install.sh --install-deps.')
    for tool in ('dkms', 'make', 'modprobe', 'systemctl', 'groupadd', 'usermod'):
        if not shutil.which(tool):
            errors.append(f'Missing command: {tool}. Run ./install.sh --install-deps.')
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_foreign('cairo')
        from gi.repository import Gtk  # noqa: F401
        import cairo  # noqa: F401
    except (ImportError, ValueError) as error:
        errors.append(f'GTK4/PyGObject/Cairo unavailable: {error}. Run ./install.sh --install-deps.')
    try:
        initramfs_command(platform.freedesktop_os_release())
    except ValueError as error:
        errors.append(str(error))
    conflicts = [name for name in ('acer_wmi', 'facer', 'linuwu_sense') if (Path('/sys/module') / name).exists()]
    if conflicts:
        print('NOTE: Active Acer driver(s): ' + ', '.join(conflicts) + '. Installation will prepare the next boot; reboot afterward.')
    if not shutil.which('nvidia-smi'):
        print('NOTE: NVIDIA telemetry is unavailable. Fans/RGB do not require nvidia-smi.')
    lockdown = Path('/sys/kernel/security/lockdown')
    try:
        if lockdown.exists() and '[none]' not in lockdown.read_text():
            print('NOTE: Kernel lockdown is active. DKMS module signing/key enrollment may be needed; see README.md.')
    except PermissionError:
        print('NOTE: Kernel lockdown state is not readable. Module signing is not configured by this installer.')
    for error in errors:
        print('ERROR: ' + error, file=sys.stderr)
    if not errors:
        print(f'Preflight passed for {kernel}.')
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--dependency-command', action='store_true')
    action.add_argument('--install-deps', action='store_true')
    action.add_argument('--refresh-initramfs', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            return check()
        info = platform.freedesktop_os_release()
        if args.refresh_initramfs:
            command = initramfs_command(info)
        else:
            kernel = platform.release()
            pkgbase_path = Path('/lib/modules') / kernel / 'pkgbase'
            pkgbase = pkgbase_path.read_text().strip() if pkgbase_path.exists() else None
            command = dependency_command(info, kernel, pkgbase)
        print(shlex.join(command), flush=True)
        if args.dependency_command:
            return 0
        if os.geteuid() != 0:
            raise ValueError('Run this operation through install.sh or sudo.')
        subprocess.run(command, check=True)
        if args.install_deps:
            print('Dependencies installed. If your kernel was upgraded, reboot before running ./install.sh again.')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Setup failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
