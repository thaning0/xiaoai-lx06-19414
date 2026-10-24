"""Build a diagnostic overlay from a fully replay-verified current offline build. Firmware files are data only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

try:
    from . import lx06_offline_build as common
except ImportError:
    import lx06_offline_build as common

# Historical broken base pin, reference only; never accepted without a current report.
BASE_PIN = (38158336, '116fc84bcdb8880df9b88362db09d33bec3723dcca3b4b9fc41cae544c736672')
EPOCH = 1770618789
CHANGED = {'etc/init.d/boot', 'etc/init.d/adbd', 'etc/init.d/done', 'etc/init.d/wireless',
           'usr/sbin/lx06-diagnostic.sh', 'etc/lx06-diagnostic',
           'etc/lx06-diagnostic/wpa_supplicant.conf', 'etc/lx06-diagnostic/version'}
PERMISSION_REPAIRS = {
    'bin/wpa_action.sh': (0o664, 0o755),
    'etc/inittab': (0o755, 0o664),
    'etc/asound.conf.dts': (0o755, 0o664),
    'etc/asound.conf': (0o755, 0o644),
    'etc/pam.d/common-auth': (0o755, 0o664),
    'etc/init.d/adbd': (0o775, 0o755),
    'etc/init.d/bluetooth': (0o664, 0o755),
    'etc/init.d/done': (0o755, 0o775),
    'etc/init.d/boot': (0o664, 0o755),
    'etc/init.d/dropbear': (0o644, 0o755),
    'usr/sbin/boot_function.sh': (0o644, 0o755),
}
CONTENT_CHANGED = CHANGED.copy()
CHANGED |= set(PERMISSION_REPAIRS)


def repair_permissions(root):
    # Validate the entire closed map before changing anything. Never blanket chmod.
    for name, (old, new) in PERMISSION_REPAIRS.items():
        mode = (root/name).lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) not in (old, new):
            raise ValueError("unexpected base permission: " + name)
    for name, (old, new) in PERMISSION_REPAIRS.items():
        (root/name).chmod(new)


SECRET = 'etc/lx06-diagnostic/wpa_supplicant.conf'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError('unexpected pinned base script: ' + path.name)
    path.write_text(text.replace(old, new))


def modify(root, wifi, helper):
    boot = root/'etc/init.d/boot'
    replace_once(boot, '. /usr/sbin/boot_function.sh\n',
                 '. /usr/sbin/boot_function.sh\n. /usr/sbin/lx06-diagnostic.sh\n')
    replace_once(boot, '    openwrt_env_prepare\n',
                 '    lx06_diag_event init-entry\n    openwrt_env_prepare\n')
    replace_once(boot, '    /sbin/kmodloader\n',
                 '    /sbin/kmodloader\n'
                 '    if /etc/init.d/adbd start; then lx06_diag_event adb-started; '
                 'else lx06_diag_event adb-failed; fi\n')
    old = '''    ubiattach -p /dev/mtd6
    mount -t ubifs /dev/ubi0_0 /data
    if [ $? != 0 ]; then
        echo Mount data partition is failed, so format it ...
        ubidetach -p /dev/mtd6
        flash_erase /dev/mtd6 0 0
        ubiattach -p /dev/mtd6
        ubimkvol -N data -m /dev/ubi0
        mount -t ubifs /dev/ubi0_0 /data
    fi
'''
    new = '''    ubiattach -p /dev/mtd6
    if ! mount -t ubifs /dev/ubi0_0 /data || ! grep -q ' /data ubifs ' /proc/mounts; then
        lx06_diag_event data-mount-failed
        lx06_diag_event reformat-skipped
        lx06_diag_snapshot
        return 1
    fi
    lx06_diag_event data-mounted
    lx06_diag_event reformat-skipped
    lx06_diag_seed_wifi || lx06_diag_event wifi-seed-failed
    lx06_diag_snapshot
    lx06_diag_persist
'''
    replace_once(boot, old, new)
    replace_once(boot, '    miio_fix_registed_file\n',
                 '    miio_fix_registed_file\n    lx06_diag_snapshot\n    lx06_diag_persist\n')

    adbd = root/'etc/init.d/adbd'
    replace_once(adbd, '\tsn=`/sbin/uci -q get /data/etc/binfo.binfo.sn`',
                 '\tsn=\n\tgrep -q " /data ubifs " /proc/mounts && '
                 'sn=`/sbin/uci -q get /data/etc/binfo.binfo.sn`')
    replace_once(adbd, '''\tif [ ! -f "/data/adb_enable" ]; then return 0 ; fi
\tstart_usbgadget &''', '''\tstart_usbgadget''')
    done = root/'etc/init.d/done'
    replace_once(done, 'START=95\n', 'START=95\n. /usr/sbin/lx06-diagnostic.sh\n')
    replace_once(done, '\t/etc/init.d/wireless boot_done\n',
                 '\t/etc/init.d/wireless boot_done\n'
                 '\tlx06_diag_event late-init-done\n\tlx06_diag_snapshot\n\tlx06_diag_persist\n')
    wireless = root/'etc/init.d/wireless'
    replace_once(wireless, '    file2log $WIRELESS_CONF\n',
                 '    : # Private Wi-Fi configuration must never be logged.\n')
    replace_once(wireless, ' -s -dd $extra_cmd\n', ' -s $extra_cmd\n')
    target = root/'usr/sbin/lx06-diagnostic.sh'
    shutil.copyfile(helper, target)
    target.chmod(0o755)
    private = root/'etc/lx06-diagnostic'
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private/'wpa_supplicant.conf'
    with wifi.open('rb') as source, target.open('xb') as dest:
        shutil.copyfileobj(source, dest)
    target.chmod(0o600)
    (private/'version').write_text('diagnostic-v2\n')
    (private/'version').chmod(0o600)
    for script in (boot, adbd, done, wireless, root/'usr/sbin/lx06-diagnostic.sh'):
        subprocess.run(['/bin/bash', '-n', str(script)], check=True)


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory)/name
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                result[rel] = ['link', os.readlink(path)]
            elif stat.S_ISDIR(mode):
                result[rel] = ['dir', stat.S_IMODE(mode)]
            elif stat.S_ISREG(mode):
                result[rel] = ['file', stat.S_IMODE(mode), path.stat().st_size, digest(path)]
            else:
                raise ValueError('unexpected special file: ' + rel)
    return result


def validate_report(report):
    """Reports are consistency evidence, never a caller-selected image hash pin."""
    if (report.get('status') != 'offline-built-hardware-unverified' or
            report.get('source_sha256') != common.SOURCE_SHA256 or
            report.get('profile') != 'lx06/1.94.14' or
            report.get('source_date_epoch') != EPOCH or
            report.get('tree_manifests') != ['work/original-tree.json', 'work/patched-tree.json']):
        raise ValueError('base report status/source/profile/epoch mismatch')
    image = report.get('image', {})
    if (not isinstance(image.get('size'), int) or not 96 <= image['size'] < common.LIMIT or
            not isinstance(image.get('sha256'), str) or len(image['sha256']) != 64):
        raise ValueError('base report image size/hash missing')
    inputs = report.get('build_inputs', {})
    # Bind every trusted source/profile file to THIS checkout, including modes.
    expected = {}
    for directory in ('scripts', 'patches', 'profiles', 'bin', 'api'):
        root = common.REPO/directory
        expected[directory] = {'type': 'directory', 'mode': stat.S_IMODE(root.stat().st_mode)}
        expected.update({directory+'/'+n: v for n, v in common.inventory(root).items()})
    expected['tools'] = {'type': 'directory', 'mode': 0o755}
    for name in ('lx06_profile.py', 'lx06_compat.py', 'lx06_offline_build.py', 'lx06_elf_audit.py'):
        path = common.REPO/'tools'/name
        expected['tools/'+name] = {'type': 'file', 'mode': stat.S_IMODE(path.stat().st_mode),
                                  'size': path.stat().st_size, 'sha256': digest(path)}
    actual = {n: v for n, v in inputs.items() if not n.startswith('build-packages')}
    if actual != expected:
        raise ValueError('base report source/profile snapshot differs from current checkout')
    if not isinstance(report.get('overlay_entries'), dict) or not report['overlay_entries']:
        raise ValueError('base report overlay inventory missing')
    if report.get('dependency_audit', {}).get('failures') != []:
        raise ValueError('base report dependency audit missing or failed')
    return report


def verify_base_tree(image, root, expected):
    """Fresh full extraction plus modes/content/links, all owners and console."""
    common.verify_image(image)
    subprocess.run(['/usr/bin/unsquashfs', '-no-progress', '-strict-errors',
                    '-excludes', '-d', str(root), str(image), 'dev/console'],
                   check=True, env=common.clean_env(), umask=0, stdout=subprocess.DEVNULL)
    actual = common.inventory(root)
    actual['.'] = {'type': 'directory', 'mode': stat.S_IMODE(root.stat().st_mode)}
    common.compare_trees(expected, actual)
    listing = subprocess.run(['/usr/bin/unsquashfs', '-lln', str(image)],
                             check=True, env=common.clean_env(), capture_output=True, text=True)
    import re
    owners = re.findall(r'^[bcdlps-][rwxStTs-]{9}\s+(\d+/\d+)\s+', listing.stdout, re.M)
    if len(owners) != len(expected) + 1 or any(n != '0/0' for n in owners):
        raise ValueError('base ownership/entry count mismatch')


def worker():
    work = Path('/work')
    report = json.loads((work/'base-report.json').read_text())
    # Replay current trusted host patch scripts against the exact pinned stock.
    # No scripts from an untrusted report snapshot and no target ARM code run.
    common.inside_worker(EPOCH)
    replay = common.verify_image(work/'candidate.squashfs')
    if replay != report['image'] or common.verify_image(work/'base.squashfs') != replay:
        raise ValueError('base image/report does not match current offline build replay')
    for name in ('original-tree.json', 'patched-tree.json', 'candidate.tree-audit.json'):
        if json.loads((work/name).read_text()) != json.loads((work/('base-'+name)).read_text()):
            raise ValueError('base report full tree audit mismatch: '+name)
    if json.loads((work/'dependency-audit.json').read_text()) != report['dependency_audit']:
        raise ValueError('base report dependency audit differs from replay')
    audit = json.loads((work/'candidate.tree-audit.json').read_text())
    if audit.get('status') != 'passed' or audit.get('all_owners') != '0:0':
        raise ValueError('base tree audit failed')
    base = work/'base'
    root = work/'root'
    verify_base_tree(work/'base.squashfs', base, audit['tree'])
    shutil.copytree(base, root, symlinks=True)
    before = inventory(base)
    required = CONTENT_CHANGED | {
        n for n, (_, new) in PERMISSION_REPAIRS.items() if before[n][1] != new}
    repair_permissions(root)
    modify(root, work/'wifi.conf', Path('/repo/diagnostic/lx06-diagnostic.sh'))
    common.profile.logging_invariants(root, startup=True)
    common.profile.squeezelite_invariants(root, startup=True)
    after = inventory(root)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if changed != required:
        raise ValueError('unexpected changed-file inventory: ' + repr(sorted(changed ^ required)))
    common.pack_image(root, work/'packed.squashfs', EPOCH, dict(os.environ))
    verified = work/'verified'
    subprocess.run(['/usr/bin/unsquashfs', '-no-progress', '-strict-errors',
                    '-excludes', '-d', str(verified), str(work/'packed.squashfs'), 'dev/console'],
                   check=True, umask=0, stdout=subprocess.DEVNULL)
    again = inventory(verified)
    extracted_change = {name for name in before.keys() | again.keys() if before.get(name) != again.get(name)}
    if extracted_change != required:
        raise ValueError('re-extracted changed-file inventory mismatch: '+repr(sorted(extracted_change ^ required)))
    if after != again:
        raise ValueError('packed tree differs from input tree')
    common.profile.logging_invariants(verified, startup=True)
    common.profile.squeezelite_invariants(verified, startup=True)
    summary = []
    for name in sorted(changed):
        record = {'path': name, 'kind': after[name][0], 'mode': oct(after[name][1]) if after[name][0] != 'link' else None}
        if name != SECRET and after[name][0] == 'file':
            record['sha256'] = after[name][3]
        summary.append(record)
    (work/'inventory.json').write_text(json.dumps(summary, indent=2)+'\n')
    repairs = [{'path': n, 'legacy_mode': oct(old), 'input_mode': oct(before[n][1]),
                'new_mode': oct(new), 'changed': before[n][1] != new}
               for n, (old, new) in sorted(PERMISSION_REPAIRS.items())]
    (work/'permission-repairs.json').write_text(json.dumps(repairs, indent=2)+'\n')


def build(base, wifi, output, base_report=None):
    base, wifi, output = map(common.safe_path, (base, wifi, output))
    if base_report is None:
        raise ValueError('current complete offline --base-report required; historical BASE_PIN is not a supported base')
    report_path = common.safe_path(base_report)
    report = validate_report(json.loads(report_path.read_text()))
    if common.verify_image(base) != report['image']:
        raise ValueError('base image size/hash/format differs from report')
    artifact = report_path.parent/'work'
    source = common.safe_path(artifact/'original.squashfs')
    if source.stat().st_size != 37501023 or digest(source) != common.SOURCE_SHA256:
        raise ValueError('base original source size/hash mismatch')
    overlay, entries = common.overlay_check(artifact/'repo/build-packages/s2t/armv7')
    if entries != report['overlay_entries']:
        raise ValueError('base frozen overlay differs from report')
    artifacts = {n: common.safe_path(artifact/n) for n in (
        'original-tree.json', 'patched-tree.json', 'candidate.tree-audit.json')}
    if any(not p.is_file() for p in artifacts.values()):
        raise ValueError('base complete tree audit artifacts required')
    if any(output == p or output.is_relative_to(p) or p.is_relative_to(output)
           for p in (base, wifi, report_path.parent, overlay)):
        raise ValueError('input/output overlap')
    if not wifi.is_file() or wifi.stat().st_size == 0 or wifi.stat().st_size > 16384 or wifi.stat().st_mode & 0o077:
        raise ValueError('private Wi-Fi input missing, oversized, or not mode 0600')
    if output.exists() or not output.parent.is_dir():
        raise ValueError('output must be a new directory')
    old_umask = os.umask(0o077)
    try:
        output.mkdir(mode=0o700)
        work = output/'work'
        work.mkdir(mode=0o700)
        snapshot = common.prepare_snapshot(work, overlay)
        if common.inventory(snapshot) != report['build_inputs']:
            raise ValueError('base frozen build inputs differ from report/current checkout')
        (snapshot/'diagnostic').mkdir()
        shutil.copyfile(Path(__file__).resolve(), snapshot/'tools/lx06_diagnostic_build.py')
        shutil.copyfile(common.REPO/'diagnostic/lx06-diagnostic.sh', snapshot/'diagnostic/lx06-diagnostic.sh')
        shutil.copyfile(source, work/'original.squashfs')
        if digest(work/'original.squashfs') != common.SOURCE_SHA256:
            raise ValueError('base source changed while snapshotting')
        for name, path in artifacts.items():
            shutil.copyfile(path, work/('base-'+name))
        (work/'base-report.json').write_text(json.dumps(report))
        shutil.copyfile(base, work/'base.squashfs')
        if common.verify_image(work/'base.squashfs') != report['image']:
            raise ValueError('base changed while snapshotting')
        shutil.copyfile(wifi, work/'wifi.conf')
        (work/'wifi.conf').chmod(0o600)
        command = common.sandbox_command(work, ['/usr/bin/python3',
                   '/repo/tools/lx06_diagnostic_build.py', '--worker'])
        with (output/'build.log').open('xb') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise ValueError('confined diagnostic build failed; see private build.log')
        os.link(work/'packed.squashfs', output/'root.squashfs')
        info = common.verify_image(output/'root.squashfs')
        if info['superblock_time'] != EPOCH:
            raise ValueError('unexpected image timestamp')
        payload = output/'root.squashfs'
        with payload.open('ab') as stream:
            stream.write(b'\0' * (-payload.stat().st_size % 16384))
        if payload.stat().st_size >= common.LIMIT:
            raise ValueError('aligned image reaches nominal 40 MiB limit')
        info = common.verify_image(payload)
        manifest = {'status': 'offline-built-hardware-unverified', 'image': {'size': payload.stat().st_size, 'sha256': digest(payload)},
                    'base': report['image'], 'base_validation': 'current-source-replay-and-full-extraction',
                    'base_report_sha256': digest(report_path), 'format': info,
                    'permission_repairs': json.loads((work/'permission-repairs.json').read_text()),
                    'packed_tree_audit': 'work/packed.tree-audit.json',
                    'changed_files': json.loads((work/'inventory.json').read_text())}
        (output/'build-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        # Private input and extracted credential remain inside the owner-only output.
        return manifest
    finally:
        os.umask(old_umask)


if __name__ == '__main__':
    if os.sys.argv[1:] == ['--worker']:
        if Path.cwd() != Path('/repo') or not Path('/work/base.squashfs').is_file():
            raise SystemExit('worker requires private namespace')
        worker()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--base-image', required=True)
        parser.add_argument('--base-report', required=True, help='complete current offline build-manifest.json with sibling work artifacts')
        parser.add_argument('--private-wifi-file', required=True)
        parser.add_argument('--output', required=True)
        args = parser.parse_args()
        result = build(args.base_image, args.private_wifi_file, args.output, args.base_report)
        print(json.dumps({'status': result['status'], 'image': result['image']}))
