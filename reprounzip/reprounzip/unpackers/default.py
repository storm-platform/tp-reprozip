# Copyright (C) 2014 New York University
# This file is part of ReproZip which is released under the Revised BSD License
# See file LICENSE for full license details.

"""Default unpackers for reprounzip.

This file contains the default plugins that come with reprounzip:
- ``directory`` puts all the files in a simple directory. This is simple but
  can be unreliable.
- ``chroot`` creates a chroot environment. This is more reliable as you get a
  harder isolation from the host system.
- ``installpkgs`` installs on your distribution the packages that were used by
  the experiment on the original machine. This is useful if some of them were
  not packed and you do not have them installed.
"""

from __future__ import unicode_literals

import platform
from rpaths import PosixPath, DefaultAbstractPath, Path
import subprocess
import sys
import tarfile

from reprounzip.unpackers.common import THIS_DISTRIBUTION, COMPAT_OK, \
    COMPAT_NO, load_config, select_installer, shell_escape, busybox_url, \
    join_root, PKG_NOT_INSTALLED
from reprounzip.utils import unicode_, download_file


def installpkgs(args):
    """Installs the necessary packages on the current machine.
    """
    if not THIS_DISTRIBUTION:
        sys.stderr.write("Error: Not running on Linux\n")
        sys.exit(1)

    pack = args.pack[0]
    missing = args.missing

    # Loads config
    runs, packages, other_files = load_config(pack)

    installer = select_installer(pack, runs)

    if args.summary:
        # Print out a list of packages with their status
        if missing:
            print("Packages not present in pack:")
            packages = [pkg for pkg in packages if not pkg.packfiles]
        else:
            print("All packages:")
        pkgs = installer.get_packages_info(packages)
        for pkg in packages:
            print("    %s (required version: %s, status: %s)" % (
                  pkg.name, pkg.version, pkgs[pkg.name][1]))
    else:
        if missing:
            # With --missing, ignore packages whose files were packed
            packages = [pkg for pkg in packages if not pkg.packfiles]

        # Installs packages
        r, pkgs = installer.install(packages, assume_yes=args.assume_yes)
        for pkg in packages:
            req = pkg.version
            real = pkgs[pkg.name][1]
            if real == PKG_NOT_INSTALLED:
                sys.stderr.write("Warning: package %s was not installed\n" %
                                 pkg.name)
            else:
                sys.stderr.write("Warning: version %s of %s was installed, "
                                 "instead or %s\n" % (real, pkg.name, req))
        if r != 0:
            sys.exit(r)


def create_directory(args):
    """Unpacks the experiment in a folder.

    Only the files that are not part of a package are copied (unless they are
    missing from the system and were packed).
    """
    pack = Path(args.pack[0])
    target = Path(args.target[0])
    if target.exists():
        sys.stderr.write("Error: Target directory exists\n")
        sys.exit(1)

    if DefaultAbstractPath is not PosixPath:
        sys.stderr.write("Error: Not unpacking on POSIX system\n")
        sys.exit(1)

    # Loads config
    runs, packages, other_files = load_config(pack)

    target.mkdir()
    root = (target / 'root').absolute()
    root.mkdir()

    # Unpacks files
    tar = tarfile.open(str(pack), 'r:*')
    if any('..' in m.name or m.name.startswith('/') for m in tar.getmembers()):
        sys.stderr.write("Error: Tar archive contains invalid pathnames\n")
        sys.exit(1)
    members = [m for m in tar.getmembers() if m.name.startswith('DATA/')]
    for m in members:
        m.name = m.name[5:]
    # Makes symlink targets relative
    for m in members:
        if not m.issym():
            continue
        linkname = PosixPath(m.linkname)
        if linkname.is_absolute:
            m.linkname = join_root(root, PosixPath(m.linkname)).path
    tar.extractall(str(root), members)
    tar.close()

    # Gets library paths
    lib_dirs = []
    p = subprocess.Popen(['/sbin/ldconfig', '-v', '-N'],
                         stdout=subprocess.PIPE)
    try:
        for l in p.stdout:
            if len(l) < 3 or l[0] in (b' ', b'\t'):
                continue
            if l.endswith(b':\n'):
                lib_dirs.append(Path(l[:-2]))
    finally:
        p.wait()

    # Writes start script
    with (target / 'script.sh').open('w', encoding='utf-8') as fp:
        fp.write('#!/bin/sh\n\n')
        fp.write("export LD_LIBRARY_PATH=%s\n\n" % ':'.join(
                shell_escape(unicode_(join_root(root, d)))
                for d in lib_dirs))
        for run in runs:
            cmd = 'cd %s && ' % shell_escape(
                    unicode_(join_root(root,
                                       Path(run['workingdir']))))
            cmd += ' '.join('%s=%s' % (k, shell_escape(v))
                            for k, v in run['environ'].items())
            cmd += ' '
            path = [PosixPath(d)
                    for d in run['environ'].get('PATH', '').split(':')]
            path = ':'.join(unicode_(join_root(root, d)) if d.root == '/'
                            else unicode_(d)
                            for d in path)
            cmd += 'PATH=%s ' % shell_escape(path)
            # FIXME : Use exec -a or something if binary != argv[0]
            cmd += ' '.join(
                    shell_escape(a)
                    for a in run['argv'])
            fp.write('%s\n' % cmd)

    print("Experiment set up, run %s to start" % (target / 'script.sh'))


def create_chroot(args):
    """Unpacks the experiment in a folder so it can be run with chroot.

    All the files in the pack are unpacked; system files are copied only if
    they were not packed, and busybox is installed if /bin/sh wasn't packed.
    """
    pack = Path(args.pack[0])
    target = Path(args.target[0])
    if target.exists():
        sys.stderr.write("Error: Target directory exists\n")
        sys.exit(1)

    if DefaultAbstractPath is not PosixPath:
        sys.stderr.write("Error: Not unpacking on POSIX system\n")
        sys.exit(1)

    # Loads config
    runs, packages, other_files = load_config(pack)

    target.mkdir()
    root = (target / 'root').absolute()
    root.mkdir()

    # Checks that everything was packed
    packages_not_packed = [pkg for pkg in packages if not pkg.packfiles]
    if packages_not_packed:
        sys.stderr.write("Error: According to configuration, some files were "
                         "left out because they belong to the following "
                         "packages:\n")
        sys.stderr.write(''.join('    %s\n' % pkg
                                 for pkg in packages_not_packed))
        sys.stderr.write("Will copy files from HOST SYSTEM\n")
        for pkg in packages_not_packed:
            for f in pkg.files:
                f = Path(f.path)
                if not f.exists():
                    sys.stderr.write(
                            "Missing file %s (from package %s) on host, "
                            "experiment will probably miss it\n" % (
                                f, pkg.name))
                dest = join_root(root, f)
                dest.parent.mkdir(parents=True)
                if f.is_link():
                    dest.symlink(f.read_link())
                else:
                    f.copy(dest)

    # Unpacks files
    tar = tarfile.open(str(pack), 'r:*')
    if any('..' in m.name or m.name.startswith('/') for m in tar.getmembers()):
        sys.stderr.write("Error: Tar archive contains invalid pathnames\n")
        sys.exit(1)
    members = [m for m in tar.getmembers() if m.name.startswith('DATA/')]
    for m in members:
        m.name = m.name[5:]
    tar.extractall(str(root), members)
    tar.close()

    # Sets up /bin/sh and /usr/bin/env, downloading busybox if necessary
    sh_path = join_root(root, Path('/bin/sh'))
    env_path = join_root(root, Path('/usr/bin/env'))
    if not sh_path.lexists() or not env_path.lexists():
        busybox_path = join_root(root, Path('/bin/busybox'))
        busybox_path.parent.mkdir(parents=True)
        download_file(busybox_url(runs[0]['architecture']),
                      busybox_path)
        busybox_path.chmod(0o755)
        if not sh_path.lexists():
            sh_path.parent.mkdir(parents=True)
            sh_path.symlink('/bin/busybox')
        if not env_path.lexists():
            env_path.parent.mkdir(parents=True)
            env_path.symlink('/bin/busybox')

    # Writes start script
    with (target / 'script.sh').open('w', encoding='utf-8') as fp:
        fp.write('#!/bin/sh\n\n')
        for run in runs:
            cmd = 'cd %s && ' % shell_escape(run['workingdir'])
            cmd += '/usr/bin/env -i '
            cmd += ' '.join('%s=%s' % (k, shell_escape(v))
                            for k, v in run['environ'].items())
            cmd += ' '
            # FIXME : Use exec -a or something if binary != argv[0]
            cmd += ' '.join(
                    shell_escape(a)
                    for a in [run['binary']] + run['argv'][1:])
            userspec = '%s:%s' % (run.get('uid', 1000), run.get('gid', 1000))
            fp.write('chroot --userspec=%s %s /bin/sh -c %s\n' % (
                     userspec,
                     shell_escape(unicode_(root)),
                     shell_escape(cmd)))

    print("Experiment set up, run %s to start" % (target / 'script.sh'))


def test_same_pkgmngr(pack, config, **kwargs):
    """Compatibility test: platform is Linux and uses same package manager.
    """
    runs, packages, other_files = config

    orig_distribution = runs[0]['distribution'][0].lower()
    if not THIS_DISTRIBUTION:
        return COMPAT_NO, "This machine is not running Linux"
    elif THIS_DISTRIBUTION == orig_distribution:
        return COMPAT_OK
    else:
        return COMPAT_NO, "Different distributions. Then: %s, now: %s" % (
                orig_distribution, THIS_DISTRIBUTION)


def test_linux_same_arch(pack, config, **kwargs):
    """Compatibility test: this platform is Linux and arch is compatible.
    """
    runs, packages, other_files = config

    orig_architecture = runs[0]['architecture']
    current_architecture = platform.machine().lower()
    if platform.system().lower() != 'linux':
        return COMPAT_NO, "This machine is not running Linux"
    elif (orig_architecture == current_architecture or
            (orig_architecture == 'i386' and current_architecture == 'amd64')):
        return COMPAT_OK
    else:
        return COMPAT_NO, "Different architectures. Then: %s, now: %s" % (
                orig_architecture, current_architecture)


def setup_installpkgs(parser):
    """"Installs the required packages on this system
    """
    parser.add_argument('pack', nargs=1, help="Pack to process")
    parser.add_argument(
            '-y', '--assume-yes',
            help="Assumes yes for package manager's questions (if supported)")
    parser.add_argument(
            '--missing', action='store_true',
            help="Only install packages that weren't packed")
    parser.add_argument(
            '--summary', action='store_true',
            help="Don't install, print which packages are installed or not")
    parser.set_defaults(func=installpkgs)

    return {'test_compatibility': test_same_pkgmngr}


def setup_directory(parser):
    """Unpacks the files in a directory
    """
    parser.add_argument('pack', nargs=1, help="Pack to extract")
    parser.add_argument('target', nargs=1, help="Directory to create")
    parser.set_defaults(func=create_directory)

    return {'test_compatibility': test_linux_same_arch}


def setup_chroot(parser):
    """Unpacks the files so the experiment can be run with chroot
    """
    parser.add_argument('pack', nargs=1, help="Pack to extract")
    parser.add_argument('target', nargs=1, help="Directory to create")
    parser.set_defaults(func=create_chroot)

    return {'test_compatibility': test_linux_same_arch}
