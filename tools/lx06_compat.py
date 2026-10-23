"""Offline rootfs review; never executes code from the unpacked tree."""
import argparse
import json
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]


def inside(path, root):
    return path.resolve().is_relative_to(root.resolve())


def confined_file(root, relative, *, missing_ok=False):
    """Require lexical confinement and real directories/files, never symlinks."""
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts or str(p) != relative:
        raise ValueError(f'unsafe path: {relative}')
    target = root
    for index, part in enumerate(p.parts):
        target = target / part
        if target.is_symlink():
            raise ValueError(f'symlink in target path: {relative}')
        if not target.exists() and missing_ok:
            continue
        if index < len(p.parts) - 1:
            if not target.is_dir():
                raise ValueError(f'non-directory ancestor: {relative}')
        elif not target.is_file():
            raise ValueError(f'missing/non-regular target: {relative}')
    return target


def inspect_tree(root):
    # Firmware absolute symlinks are inert data. Never resolve their referents.
    import stat
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                continue
            if (path.relative_to(root).as_posix() == 'dev/console' and
                    stat.S_ISCHR(mode) and os.major(path.lstat().st_rdev) == 5 and
                    os.minor(path.lstat().st_rdev) == 1):
                continue
            raise ValueError(f'unsupported special file: {path.relative_to(root)}')


def metadata(path):
    sections = []
    current = None
    for line in path.read_text(encoding='utf-8').splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0] == 'config':
            current = {} if words[1:] == ['core', 'version'] else None
            if current is not None:
                sections.append(current)
        elif words[0] == 'option' and len(words) == 3 and current is not None:
            if words[1] in current:
                raise ValueError('duplicate metadata option')
            current[words[1]] = words[2]
        elif words[0] not in ('option', 'list'):
            raise ValueError('invalid UCI text')
    if len(sections) != 1:
        raise ValueError("missing/duplicate config core 'version'")
    return sections[0]


def validate_patch(patch, tree):
    # GNU patch also recognizes Index: and diff command headers; reject unsafe
    # paths there rather than relying on patch's own filename heuristics.
    targets = []
    for line in patch.read_text().splitlines():
        if line.startswith('*** '):
            raise ValueError('only unified patches are accepted')
        if line.startswith(('--- ', '+++ ')):
            raw = line[4:]
            value = raw.split('\t', 1)[0]
            if '\t' not in raw and ' ' in raw:
                value, suffix = raw.split(' ', 1)
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?(?: [+-]\d{4})?', suffix):
                    raise ValueError('ambiguous/unsafe unified patch filename')
            targets.append(value)
        elif line.startswith('Index: '):
            targets.append(line[7:].strip())
        elif line.startswith('diff '):
            words = shlex.split(line)
            targets.extend(words[-2:])
    if not targets:
        raise ValueError(f'no unified patch targets: {patch.name}')
    for value in targets:
        if value == '/dev/null':
            continue
        p = PurePosixPath(value)
        if p.is_absolute() or '..' in p.parts or len(p.parts) < 2 or any(c in value for c in ('\\', '"', "'")):
            raise ValueError(f'unsafe patch target: {value}')
        confined_file(tree, '/'.join(p.parts[1:]), missing_ok=True)


def check(rootfs, expected='1.94.14', patches=None, run=subprocess.run, patch_executable=None):
    report = {'status': 'rejected', 'rootfs': str(Path(rootfs).absolute()),
              'expected_rom': expected, 'errors': [], 'missing': [], 'patches': [],
              'limitations': ['Independent dry runs do not capture application order dependencies.',
                              'Alternative version hunks mean all-hunks-success is not a compatibility criterion.',
                              'Trailing true in existing scripts is not proof of compatibility.']}
    try:
        supplied = Path(rootfs).absolute()
        # Disallow root argument symlinks/ancestor aliases, whose boundary is ambiguous.
        if any(p.is_symlink() for p in (supplied, *supplied.parents)):
            raise ValueError('rootfs directory path contains symlink')
        root = supplied.resolve()
        if not root.is_dir():
            report['missing'].append('rootfs directory')
            raise ValueError('missing rootfs directory')
        inspect_tree(root)
        version = root / 'usr/share/mico/version'
        if version.exists() or version.is_symlink():
            confined_file(root, 'usr/share/mico/version')
        if not inside(version, root):
            raise ValueError('metadata escapes rootfs')
        if not version.is_file():
            report['missing'].append('usr/share/mico/version')
            raise ValueError('missing metadata')
        info = metadata(version)
        report['metadata'] = info
        report['missing'].extend(key for key in ('HARDWARE', 'ROM') if key not in info)
        for key, value in [('HARDWARE', 'LX06'), ('ROM', expected)]:
            if info.get(key) != value:
                raise ValueError(f'{key} must equal {value}')
        executable = patch_executable or shutil.which('patch', path='/usr/bin:/bin')
        if not executable:
            raise ValueError('host GNU patch unavailable')
        executable = Path(executable).resolve(strict=True)
        if not executable.is_absolute() or inside(executable, root):
            raise ValueError('patch executable must be outside rootfs')
        tempbase = REPO / '.lx06-tmp'
        if inside(tempbase, root):
            raise ValueError('rootfs contains repository scratch directory')
        if tempbase.is_symlink():
            raise ValueError('temporary directory must not be a symlink')
        tempbase.mkdir(exist_ok=True)
        env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(tempbase),
               'TMPDIR': str(tempbase)}
        version_result = run([str(executable), '--version'], capture_output=True, text=True,
                             env=env, timeout=30, cwd=str(REPO))
        report['host_patch'] = {'executable': str(executable),
                                'stdout': version_result.stdout, 'stderr': version_result.stderr,
                                'exit_code': version_result.returncode}
        if version_result.returncode or 'GNU patch' not in version_result.stdout:
            raise ValueError('host GNU patch required')
        patch_dir = Path(patches) if patches else REPO / 'patches'
        selected = sorted(patch_dir.glob('*.patch')) + sorted((patch_dir / 'lx06').glob('*.patch'))
        # Explicit repository-local temporary directory, including when called as library.
        with tempfile.TemporaryDirectory(dir=tempbase) as temporary:
            tree = Path(temporary) / 'rootfs'
            shutil.copytree(root, tree, symlinks=True)
            inspect_tree(tree)
            for patch in selected:
                validate_patch(patch, tree)
                record = {'patch': str(patch), 'stdout': '', 'stderr': '', 'exit_code': None}
                report['patches'].append(record)
                try:
                    result = run([str(executable), '-p1', '--batch', '--forward', '--dry-run',
                                  '-i', str(patch.resolve())], cwd=str(tree), env=env,
                                 capture_output=True, text=True, timeout=60)
                    record.update(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
                    if result.returncode not in (0, 1):
                        report['errors'].append(f'host patch failed: {patch} (exit {result.returncode})')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    def output(value):
                        return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
                    record.update(stdout=output(getattr(exc, 'stdout', None)),
                                  stderr=output(getattr(exc, 'stderr', None)), error=str(exc))
                    report['errors'].append(str(exc))
        if not report['errors']:
            report['status'] = 'needs-review'
    except Exception as exc:
        report['errors'].append(str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rootfs')
    parser.add_argument('--profile', action='store_true', help='validate exact profile on a temporary copy')
    parser.add_argument('--expected-rom', default='1.94.14')
    parser.add_argument('--report', help='new report file; existing files refused')
    args = parser.parse_args()
    if args.profile:
        from lx06_profile import check_profile
        report = check_profile(args.rootfs)
    else:
        report = check(args.rootfs, args.expected_rom)
    rendered = json.dumps(report, indent=2)
    if args.report:
        try:
            path = Path(args.report).absolute()
            if inside(path, Path(args.rootfs)):
                raise ValueError('report destination is inside input tree')
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError('symlink report destination')
            with path.open('x') as stream:
                stream.write(rendered + '\n')
        except Exception as exc:
            report['status'] = 'rejected'
            report['errors'].append(str(exc))
            rendered = json.dumps(report, indent=2)
    print(rendered)
    return 1 if report['status'] == 'rejected' else 0


if __name__ == '__main__':
    raise SystemExit(main())
