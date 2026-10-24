"""Build only root.squashfs from pinned LX06 stock plus a REQUIRED ARMv7 overlay.

Host tools run in a private bubblewrap namespace, with only task scratch writable.
Firmware files are data: never chroot, source firmware, or execute target binaries.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys

try:
    from . import lx06_profile as profile
    from . import lx06_compat as compat
except ImportError:
    import lx06_profile as profile
    import lx06_compat as compat

REPO = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = 'bdcac4449491bfaa9b3960ee006fd6e4932a9dae4473736fc808564d5eab7439'
LIMIT = 41943040
REQUIRED = ('usr/bin/bluealsa', 'usr/bin/bluealsa-aplay', 'usr/bin/bluetoothd',
            'usr/bin/bluetoothctl', 'usr/bin/dbus-daemon', 'usr/bin/aplay',
            'usr/bin/ogg123', 'usr/bin/mpg123', 'usr/bin/mpd', 'usr/bin/snapclient',
            'usr/bin/shairport-sync', 'usr/bin/upmpdcli', 'usr/bin/squeezelite', 'bin/busybox')
PROTECTED = ('lib/modules', 'lib/firmware', 'etc/wifi', 'etc/bt', 'etc/sn_head_list',
             'usr/share/mico.l06a',
             'usr/sbin/mlanutl', 'usr/sbin/rtwpriv', 'usr/sbin/rtlbtmp')


class PrerequisiteError(ValueError):
    pass


def safe_path(value):
    path = Path(value).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink input/output path')
    return path


def inventory(root):
    """Hash leaves and record symlinks as strings; never traverse a symlink."""
    entries = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            rel = path.relative_to(root).as_posix()
            record = {'mode': stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                record.update(type='symlink', target=os.readlink(path))
            elif stat.S_ISREG(mode):
                record.update(type='file', size=path.stat().st_size, sha256=profile.digest(path))
            elif stat.S_ISDIR(mode):
                record.update(type='directory')
            else:
                raise ValueError(f'unsupported special file: {rel}')
            entries[rel] = record
    return dict(sorted(entries.items()))


def virtual_file(root, relative):
    """Resolve firmware-style absolute/relative links inside the overlay only."""
    pending = list(PurePosixPath(relative).parts)
    parts = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.', '/'):
            continue
        if part == '..':
            if not parts:
                raise ValueError('overlay symlink escapes its root')
            parts.pop()
            continue
        path = root.joinpath(*parts, part)
        if path.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('overlay symlink loop')
            target = PurePosixPath(os.readlink(path))
            if target.is_absolute():
                parts = []
            pending = list(target.parts) + pending
        else:
            if not path.exists():
                raise PrerequisiteError(f'missing required staged package: {relative}')
            parts.append(part)
    return compat.confined_file(root, '/'.join(parts))


def overlay_check(value):
    root = safe_path(value)
    if not root.is_dir():
        raise PrerequisiteError('required staged package overlay missing: build-packages/s2t/armv7')
    entries = inventory(root)
    if not entries:
        raise PrerequisiteError('required staged package overlay is empty')
    for name in REQUIRED:
        path = virtual_file(root, name)
        with path.open('rb') as stream:
            header = stream.read(20)
        if header[:6] != b'\x7fELF\x01\x01' or len(header) != 20 or int.from_bytes(header[18:20], 'little') != 40:
            raise PrerequisiteError(f'required package is not a little-endian ARM ELF: {name}')
        if not path.stat().st_mode & 0o111:
            raise PrerequisiteError(f'required package is not executable: {name}')
    # The profile uses the staged 4.3.0/5.71 interfaces, never stock 1.2.0/5.37.
    for name, version in (('usr/bin/bluealsa', b'4.3.0\0'), ('usr/bin/bluetoothd', b'5.71\0')):
        if version not in virtual_file(root, name).read_bytes():
            raise PrerequisiteError(f'staged {name} must match repository package version {version[:-1].decode()}')
    busybox = virtual_file(root, 'bin/busybox').read_bytes()
    if any(token not in busybox for token in
           (b'BusyBox v1.36.1', b'syslogd\0', b'klogd\0', b'logread\0', b'logger\0')):
        raise PrerequisiteError('BusyBox 1.36.1 with syslogd/klogd/logread/logger required')
    return root, entries


def protected(name):
    return any(name == p or name.startswith(p + '/') for p in PROTECTED)


def validate_overlay_destinations(stock, overlay_entries):
    exact = json.loads((profile.PROFILE/'manifest.json').read_text())['targets']
    forbidden = set(exact) | {'usr/share/mico/version', 'usr/share/mico/system.cfg', 'dev/console'}
    for name, entry in overlay_entries.items():
        if (protected(name) or name in forbidden or name == 'usr/share/mico' or
                name.startswith('usr/share/mico/')):
            raise ValueError(f'overlay would replace protected original/profile file: {name}')
        # An overlay may not turn a stock directory into a link, or write through
        # a firmware link on the destination. Links at leaf paths remain data.
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            parent = stock.joinpath(*parts[:index])
            if parent.is_symlink():
                raise ValueError(f'overlay destination traverses stock symlink: {name}')
        destination = stock / name
        if entry['type'] != 'directory' and destination.is_dir() and not destination.is_symlink():
            raise ValueError(f'overlay replaces stock directory: {name}')


def verify_image(path):
    size = path.stat().st_size
    if size >= LIMIT:
        raise ValueError(f'image size {size} violates strict < {LIMIT} limit')
    with path.open('rb') as stream:
        header = stream.read(96)
    if len(header) != 96:
        raise ValueError('truncated SquashFS superblock')
    values = struct.unpack('<5I6H8Q', header)
    if (values[0], values[3], values[5], values[9], values[10]) != (0x73717368, 131072, 4, 4, 0):
        raise ValueError('requires SquashFS 4.0, xz, block size 131072')
    if not 96 <= values[12] <= size:
        raise ValueError('invalid SquashFS bytes_used')
    result = subprocess.run(['/usr/bin/unsquashfs', '-lln', str(path), 'dev/console'],
                            capture_output=True, text=True, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if not re.search(r'^crw-------\s+0/0\s+5,\s+1\s+.* /?[^\n]*dev/console$', result.stdout, re.M):
        raise ValueError('missing/wrong dev/console c 0600 0:0 5:1')
    return {'size': size, 'sha256': profile.digest(path), 'format': 'SquashFS 4.0',
            'compression': 'xz', 'block_size': 131072, 'strict_limit': LIMIT,
            'console': 'c 0600 0:0 5:1', 'superblock_time': values[2]}


def sandbox_command(work, command):
    # Minimal synthetic account database supports host chown root:root without
    # exposing the host's /etc. Debian command aliases need their alternatives.
    host_etc = work/'host-etc'
    host_etc.mkdir(exist_ok=True)
    (host_etc/'passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (host_etc/'group').write_text('root:x:0:\n')
    (host_etc/'alternatives').mkdir(exist_ok=True)
    for name in ('awk', 'fakeroot'):
        alias = host_etc/'alternatives'/name
        if not alias.is_symlink():
            alias.symlink_to(Path('/usr/bin/'+name).resolve())
    args = ['/usr/bin/bwrap', '--unshare-all', '--uid', '0', '--gid', '0',
            '--die-with-parent', '--new-session',
            '--clearenv', '--ro-bind', '/usr', '/usr']
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        path = Path('/') / name
        if path.is_symlink():
            args += ['--symlink', os.readlink(path), '/' + name]
        elif path.exists():
            args += ['--ro-bind', str(path), str(path)]
    args += ['--ro-bind', str(host_etc), '/etc', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--bind', str(work), '/work', '--ro-bind', str(work/'repo'), '/repo',
             '--chdir', '/repo', '--setenv', 'PATH', '/usr/bin:/bin',
             '--setenv', 'HOME', '/tmp', '--setenv', 'LC_ALL', 'C',
             '--setenv', 'TZ', 'UTC', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
             '--setenv', 'TMPDIR', '/tmp', '--']
    return args + command


def prepare_snapshot(work, overlay):
    snapshot = work/'repo'
    snapshot.mkdir()
    for name in ('scripts', 'patches', 'profiles', 'bin', 'api'):
        shutil.copytree(REPO/name, snapshot/name, symlinks=True)
    (snapshot/'tools').mkdir()
    (snapshot/'tools').chmod(0o755)
    for name in ('lx06_profile.py', 'lx06_compat.py', 'lx06_offline_build.py', 'lx06_elf_audit.py'):
        shutil.copy2(REPO/'tools'/name, snapshot/'tools'/name)
    # No optional custom include/sound layer: this entry builds the default only.
    shutil.copytree(overlay, snapshot/'build-packages/s2t/armv7', symlinks=True)
    for name in ('build-packages', 'build-packages/s2t'):
        (snapshot/name).chmod(0o755)
    return snapshot


def clean_env():
    # An allowlist also drops LD_PRELOAD, FAKEROOTKEY and future fakeroot flags.
    return {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
            'PYTHONDONTWRITEBYTECODE': '1'}


def physical_inventory(root):
    result = subprocess.run(['/usr/bin/python3', str(Path(__file__).resolve()),
                             '--physical-inventory', str(root)], env=clean_env(),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def compare_trees(expected, actual):
    changed = sorted(n for n in expected.keys() | actual.keys()
                     if expected.get(n) != actual.get(n))
    if changed:
        raise ValueError('packed physical metadata/content mismatch: ' + repr(changed[:20]))


def pack_image(root, destination, epoch, env):
    # Exec a fresh interpreter: deleting LD_PRELOAD cannot unload this process's
    # already-loaded fakeroot stat interceptor. No target program is executed.
    subprocess.run(['/usr/bin/python3', str(Path(__file__).resolve()), '--pack-worker',
                    str(root), str(destination), str(epoch)], env=clean_env(), check=True)


def pack_worker(root, destination, epoch):
    expected = inventory(root)
    expected['.'] = {'type': 'directory', 'mode': stat.S_IMODE(root.stat().st_mode)}
    result = subprocess.run(['/usr/bin/mksquashfs', str(root), str(destination),
                    '-comp', 'xz', '-b', '131072', '-noappend', '-all-root',
                    '-always-use-fragments', '-no-progress', '-processors', '1',
                    '-mkfs-time', str(epoch), '-all-time', str(epoch),
                    '-p', 'dev/console c 600 0 0 5 1'], env=clean_env(),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        result.check_returncode()
    verify_image(destination)
    listing = subprocess.run(['/usr/bin/unsquashfs', '-lln', str(destination)],
                             env=clean_env(), capture_output=True, text=True, check=True)
    owners = re.findall(r'^[bcdlps-][rwxStTs-]{9}\s+(\d+/\d+)\s+', listing.stdout, re.M)
    if len(owners) != len(expected) + 1 or any(owner != '0/0' for owner in owners):
        raise ValueError('packed ownership/entry count mismatch')
    import tempfile
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='pack-audit-') as tmp:
        extracted = Path(tmp)/'root'
        subprocess.run(['/usr/bin/unsquashfs', '-no-progress', '-strict-errors', '-excludes',
                        '-d', str(extracted), str(destination), 'dev/console'],
                       env=clean_env(), check=True, stdout=subprocess.DEVNULL)
        actual = inventory(extracted)
        actual['.'] = {'type': 'directory', 'mode': stat.S_IMODE(extracted.stat().st_mode)}
        compare_trees(expected, actual)
    destination.with_suffix('.tree-audit.json').write_text(json.dumps({
        'status': 'passed', 'entries': len(expected), 'all_owners': '0:0',
        'console': 'c 0600 0:0 5:1', 'tree': expected}, indent=2)+'\n')


def inside_worker(epoch):
    """Only called inside bwrap. Trusted host tools; no shell/rootfs execution."""
    # Freeze creation modes independently of the invoking shell/private umask.
    os.umask(0o022)
    root = Path('/work/rootfs')
    env = dict(os.environ, ROOTFS=str(root), MODEL='lx06', LX06_OFFLINE='1',
               SOURCE_DATE_EPOCH=str(epoch))
    subprocess.run(['/usr/bin/unsquashfs', '-no-progress', '-strict-errors', '-excludes',
                    '-d', str(root), '/work/original.squashfs', 'dev/console'], check=True)
    validate_overlay_destinations(root, inventory(Path('/repo/build-packages/s2t/armv7')))
    original = inventory(root)
    (Path('/work')/'original-tree.json').write_text(json.dumps(original, indent=2)+'\n')
    for script in sorted(Path('/repo/scripts').glob('??_*.sh')):
        subprocess.run(['/bin/bash', '-n', str(script)], check=True, env=env)
        print(f'Applying {script.name}', flush=True)
        # Execute the repository's host patch scripts, NEVER firmware scripts.
        subprocess.run(['/bin/sh', str(script)], check=True, env=env)
    profile.invariants(root)
    profile.logging_invariants(root, startup=True)
    profile.squeezelite_invariants(root, startup=True)
    overlay_check(root) # Required staged programs must survive the standard cleanup.
    import lx06_elf_audit
    dependencies = lx06_elf_audit.audit(root)
    Path('/work/dependency-audit.json').write_text(json.dumps(dependencies, indent=2)+'\n')
    if dependencies['failures']:
        raise ValueError('ELF closure/version audit failed: '+json.dumps(dependencies['failures']))
    after = physical_inventory(root)
    for name, entry in original.items():
        if protected(name) and after.get(name) != entry:
            raise ValueError(f'original hardware/calibration changed: {name}')
    # Syntax-check as text only. Bash handles firmware's ash extensions.
    for directory in ('etc/init.d',):
        for path in sorted((root/directory).iterdir()):
            if not path.is_symlink() and path.is_file():
                subprocess.run(['/bin/bash', '-n', str(path)], check=True, env=env)
    for name in ('usr/sbin/boot_function.sh', 'bin/wpa_action.sh'):
        subprocess.run(['/bin/bash', '-n', str(root/name)], check=True, env=env)
    # Ensure new removed-service variants are not left enabled.
    for name in ('miio', 'miio_agent', 'miio_client', 'bluetooth_ble', 'miplay',
                 'pns', 'mediaplayer', 'messagingagent', 'mico_aivs_lab', 'cmcc_ims'):
        if list((root/'etc/rc.d').glob('S??'+name)):
            raise ValueError(f'replaced service still enabled: {name}')
    final = {'profile': 'lx06/1.94.14', 'choice': 'default-open-audio-replacement',
             'invariants': 'passed', 'preserved_hardware_entries': sum(protected(n) for n in original),
             'tree': after, 'offline_ca_policy': 'stock store retained unless supplied by staged overlay'}
    Path('/work/patched-tree.json').write_text(json.dumps(final, indent=2)+'\n')
    pack_image(root, Path('/work/candidate.squashfs'), epoch, env)
    verify_image(Path('/work/candidate.squashfs'))
    return 0


def build(source, overlay, output, epoch):
    # Missing packages are a prerequisite failure, separate from profile checks.
    overlay, package_entries = overlay_check(overlay)
    source = safe_path(source)
    if not source.is_file() or source.stat().st_size != 37501023 or profile.digest(source) != SOURCE_SHA256:
        raise ValueError('input must be the verified original 1.94.14 SquashFS (exact size/SHA256)')
    output = safe_path(output)
    if not output.parent.is_dir() or output.exists():
        raise ValueError('output must be a NEW directory under an existing real parent')
    if source.is_relative_to(output) or overlay.is_relative_to(output) or output.is_relative_to(overlay):
        raise ValueError('input/output overlap')
    for name in ('bwrap', 'fakeroot', 'unsquashfs', 'mksquashfs', 'patch', 'rsync', 'bash', 'python3', 'readelf'):
        if not shutil.which(name, path='/usr/bin:/bin'):
            raise PrerequisiteError(f'missing host tool: {name}')
    output.mkdir()
    work = output/'work'
    work.mkdir()
    manifest = {'status': 'failed', 'source_sha256': SOURCE_SHA256,
                'overlay_path': str(overlay), 'overlay_entries': package_entries,
                'source_date_epoch': epoch, 'profile': 'lx06/1.94.14',
                'limits': ['No hardware validation', 'No cryptographic OTA signature verification'],
                'package_interfaces': {'stock_bluealsa': '1.2.0', 'staged_bluealsa': '4.3.0',
                                       'stock_bluez': '5.37', 'staged_bluez': '5.71'}}
    try:
        snapshot = prepare_snapshot(work, overlay)
        # Freeze all inputs; later changes to the staged overlay cannot alter this build.
        if inventory(snapshot/'build-packages/s2t/armv7') != package_entries:
            raise ValueError('overlay changed while snapshotting')
        shutil.copyfile(source, work/'original.squashfs')
        if profile.digest(work/'original.squashfs') != SOURCE_SHA256:
            raise ValueError('source changed while snapshotting')
        provenance = overlay.with_name(overlay.name+'.source.json')
        if provenance.exists():
            report = json.loads(provenance.read_text())
            if report.get('overlay_inventory') != package_entries:
                raise ValueError('overlay provenance inventory mismatch')
            manifest['overlay_provenance'] = report
            manifest['overlay_provenance_sha256'] = profile.digest(provenance)
        manifest['build_inputs'] = inventory(snapshot)
        command = sandbox_command(work, ['/usr/bin/fakeroot', '-u', '--', '/usr/bin/python3',
                    '/repo/tools/lx06_offline_build.py', '--inside-worker', str(epoch)])
        with (output/'build.log').open('x') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise ValueError(f'confined build failed ({result.returncode}); see build.log')
        manifest['dependency_audit'] = json.loads((work/'dependency-audit.json').read_text())
        manifest['image'] = verify_image(work/'candidate.squashfs')
        manifest['tools'] = {}
        for name, flag in (('mksquashfs', '-version'), ('unsquashfs', '-version'),
                           ('patch', '--version'), ('bwrap', '--version'), ('fakeroot', '-v')):
            tool = subprocess.run(['/usr/bin/'+name, flag], capture_output=True, text=True,
                                  env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            manifest['tools'][name] = (tool.stdout + tool.stderr).splitlines()[0]
        os.link(work/'candidate.squashfs', output/'root.squashfs')
        (work/'candidate.squashfs').unlink()
        manifest['tree_manifests'] = ['work/original-tree.json', 'work/patched-tree.json']
        manifest['status'] = 'offline-built-hardware-unverified'
    except Exception as exc:
        manifest['error'] = str(exc)
        raise
    finally:
        (output/'build-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--physical-inventory':
        print(json.dumps(inventory(Path(sys.argv[2]))))
        return 0
    if len(sys.argv) == 5 and sys.argv[1] == '--pack-worker':
        pack_worker(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
        return 0
    # Private worker flag is not an alternative host build path.
    if len(sys.argv) == 3 and sys.argv[1] == '--inside-worker':
        if Path.cwd() != Path('/repo') or not Path('/work/original.squashfs').is_file():
            raise SystemExit('worker requires prepared private namespace')
        return inside_worker(int(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--squashfs', required=True)
    parser.add_argument('--packages', required=True, help='required staged build-packages/s2t/armv7')
    parser.add_argument('--output', required=True, help='new task-owned directory')
    parser.add_argument('--source-date-epoch', type=int, default=1770618789)
    args = parser.parse_args()
    try:
        if not 0 <= args.source_date_epoch <= 0xffffffff:
            raise ValueError('epoch outside SquashFS timestamp range')
        result = build(args.squashfs, args.packages, args.output, args.source_date_epoch)
        print(json.dumps({'status': result['status'], 'image': result['image']}))
        return 0
    except PrerequisiteError as exc:
        print(json.dumps({'status': 'prerequisite-missing', 'error': str(exc)}))
        return 2
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
