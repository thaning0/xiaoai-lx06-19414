"""Exact original LX06 profile. Only host patch/bash are invoked; no firmware code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

try:
    from . import lx06_compat as compat
except ImportError:
    import lx06_compat as compat

PROFILE = compat.REPO / 'profiles/lx06/1.94.14'


def logging_invariants(root, *, startup=False):
    """Check after package copies too: an overlay must not undo the log policy."""
    manifest = json.loads((PROFILE / 'manifest.json').read_text())
    for name in ('etc/init.d/syslog-ng', 'usr/bin/coredump.sh'):
        path = compat.confined_file(root, name)
        if digest(path) != manifest['targets'][name]['result_sha256']:
            raise ValueError('profile invariant: bounded logging changed: ' + name)
        if not path.stat().st_mode & 0o111:
            raise ValueError('profile invariant: logging script not executable: ' + name)
    if startup:
        link = root/'etc/rc.d/S50syslog-ng'
        if not link.is_symlink() or os.readlink(link) != '../init.d/syslog-ng':
            raise ValueError('profile invariant: bounded logging startup link changed')
    return 'passed'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def squeezelite_invariants(root, *, startup=False):
    policy = json.loads((PROFILE/'manifest.json').read_text())['squeezelite_policy']
    path = compat.confined_file(root, 'etc/init.d/squeezelite')
    if digest(path) != policy['result_sha256'] or not path.stat().st_mode & 0o111:
        raise ValueError('profile invariant: Squeezelite opt-in policy changed')
    if startup:
        link = root/'etc/rc.d/S99squeezelite'
        if not link.is_symlink() or os.readlink(link) != '../init.d/squeezelite':
            raise ValueError('profile invariant: Squeezelite startup link changed')
        program = compat.confined_file(root, 'usr/bin/squeezelite')
        if not program.stat().st_mode & 0o111:
            raise ValueError('profile invariant: Squeezelite program not executable')
    return 'passed'


def apply_lowmem(rootfs):
    """The release supplies this init, so patch AFTER package/include copies."""
    root = root_path(rootfs)
    if not selected(root):
        raise ValueError('Squeezelite policy requires LX06 1.94.14')
    policy = json.loads((PROFILE/'manifest.json').read_text())['squeezelite_policy']
    patch = PROFILE/'squeezelite.patch'
    name = 'etc/init.d/squeezelite'
    source = compat.confined_file(root, name)
    if digest(patch) != policy['patch_sha256'] or digest(source) != policy['sha256']:
        raise ValueError('Squeezelite policy patch/preimage mismatch')
    compat.validate_patch(patch, root)
    names = {line[6:].split('\t')[0] for line in patch.read_text().splitlines()
             if line.startswith(('--- a/', '+++ b/'))}
    if names != {name}:
        raise ValueError('Squeezelite policy patch target mismatch')
    with tempfile.TemporaryDirectory(prefix='.lx06-lowmem-', dir=root.parent) as temporary:
        stage = Path(temporary)
        target = stage/name
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
        subprocess.run(['/usr/bin/patch', '-p1', '--batch', '--forward', '--fuzz=0',
                        '--no-backup-if-mismatch', '-r', '-', '-i', str(patch)],
                       cwd=stage, capture_output=True, text=True, check=True)
        subprocess.run(['/bin/bash', '-n', str(target)], capture_output=True, check=True)
        squeezelite_invariants(stage)
        os.replace(target, source)
    return {'status': 'squeezelite-opt-in-validated'}


def root_path(value):
    root = Path(value).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)) or not root.is_dir():
        raise ValueError('rootfs must be a real directory without symlink ancestors')
    return root


def selected(root):
    info = compat.metadata(compat.confined_file(root, 'usr/share/mico/version'))
    return info.get('HARDWARE') == 'LX06' and info.get('ROM') == '1.94.14'


def preflight(root, profile=PROFILE):
    root = root_path(root)
    manifest = json.loads((profile / 'manifest.json').read_text())
    info = compat.metadata(compat.confined_file(root, 'usr/share/mico/version'))
    if any(info.get(k) != v for k, v in manifest['metadata'].items()):
        raise ValueError('profile metadata mismatch; requires original LX06 1.94.14')
    if digest(compat.confined_file(root, 'usr/share/mico/version')) != manifest['metadata_sha256']:
        raise ValueError('metadata preimage changed')
    patch = profile / 'default.patch'
    if digest(patch) != manifest['patch_sha256']:
        raise ValueError('profile patch hash mismatch')
    compat.validate_patch(patch, root)
    # Require manifest coverage of every patch filename, not just hashes it lists.
    names = {line[6:].split('\t')[0] for line in patch.read_text().splitlines()
             if line.startswith(('--- a/', '+++ b/'))}
    if names != {p for p, h in manifest['targets'].items() if not h.get('unchanged')}:
        raise ValueError('patch/manifest target mismatch')
    for name, hashes in manifest['targets'].items():
        if digest(compat.confined_file(root, name)) != hashes['sha256']:
            raise ValueError(f'preimage changed or already patched: {name}')
    compat.inspect_tree(root)
    return root, manifest


def invariants(root):
    def read(name):
        return compat.confined_file(root, name).read_text()
    def require(condition, message):
        if not condition:
            raise ValueError('profile invariant: ' + message)
    ssh = read('etc/init.d/dropbear')
    require('ssh_en' not in ssh and 'micocfg_work_mode' not in ssh, 'SSH gate remains')
    bt = read('etc/init.d/bluetooth')
    require('00:00:00:00:00:00 -vv -D "$ALSA_MIXER" --profile-a2dp' in bt,
            'bluealsa-aplay arguments')
    require('bluez_mibt' not in bt and 'bluetooth_ble' not in bt, 'proprietary Bluetooth invocation')
    require('marvell_setup\n\tbluez_start' in bt, 'Bluetooth hardware setup')
    wireless = read('etc/init.d/wireless')
    require('WIRELESS_CONF="/data/wifi/wpa_supplicant.conf"' in wireless, 'Wi-Fi config path')
    require('    migrate_wireless_conf || return 1' in wireless and
            'ln "$temporary" "$WIRELESS_CONF"' in wireless and
            'force remove $WIRELESS_CONF' not in wireless, 'Wi-Fi config migration/preservation')
    require('command /usr/sbin/wpa_supplicant' in wireless and
            'command /usr/sbin/wpa_cli -i "$STA_IF" -a/bin/wpa_action.sh' in wireless,
            'Wi-Fi connection/event initialization')
    import re
    require(not re.search(r'^\s*/etc/init.d/(?:pns|voip|mico_aivs_lab|cmcc_ims|cmcc_andlink|idmruntime|bluetooth_ble|messagingagent|mediaplayer|alarm|miplay)\b', wireless, re.M),
            'proprietary restart hook')
    action = read('bin/wpa_action.sh')
    require('/etc/init.d/dhcpc start' in action and '/etc/init.d/odhcp6c start' in action and
            'ntpd -q -p pool.ntp.org' in action, 'DHCP/NTP event hook')
    boot = read('etc/init.d/boot')
    require('mount --bind /data/console/shadow /etc/shadow' in boot, 'persistent shadow')
    require('[ -f "/data/mico/${cfg##*/}" ] || cp' in boot, 'preserve custom L06A config')
    require('echo 1 > /sys/devices/i2c-2/2-002c/reg_reconfig' in boot and
            'wifi_bt_driver_prepare_lx06' in boot and 'ubiattach -p /dev/mtd6' in boot,
            'original hardware initialization')
    require('    auen\n' in read('etc/init.d/alsa'), 'amplifier enable')
    for name in ('etc/asound.conf', 'etc/asound.conf.dts'):
        text = read(name)
        require('dtsladspa' not in text and 'slave.pcm dtsaudio' not in text and
                'pcm "hw:0,2"' in text, 'ALSA hardware path without DTS dependency')
    logging_invariants(root)
    return 'passed'


def apply(rootfs, profile=PROFILE):
    root, manifest = preflight(rootfs, profile)
    # Validate all preimages before staging, and all outputs before any mutation.
    with tempfile.TemporaryDirectory(prefix='.lx06-profile-', dir=root.parent) as temporary:
        stage = Path(temporary)
        for name in manifest['targets']:
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, dest)
        env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
        result = subprocess.run(['/usr/bin/patch', '-p1', '--batch', '--forward', '--fuzz=0',
                                 '--no-backup-if-mismatch', '-r', '-', '-i', str((profile/'default.patch').resolve())],
                                cwd=stage, env=env, capture_output=True, text=True, check=True)
        for name, hashes in manifest['targets'].items():
            if digest(stage / name) != hashes['result_sha256']:
                raise ValueError(f'postimage mismatch: {name}')
            if name.startswith(('etc/init.d/', 'usr/sbin/')) or name in ('bin/wpa_action.sh', 'usr/bin/coredump.sh'):
                subprocess.run(['/bin/bash', '-n', str(stage / name)], env=env, check=True,
                               capture_output=True, text=True)
        invariants(stage)
        for name in manifest['targets']:
            os.replace(stage / name, root / name)
    return {'profile': manifest['profile'], 'status': 'profile-validated',
            'manifest_sha256': digest(profile/'manifest.json'),
            'patch_sha256': manifest['patch_sha256'], 'metadata': manifest['metadata'],
            'limitations': ['Static/offline validation only; hardware unverified.'],
            'targets': manifest['targets'], 'invariants': 'passed', 'patch_stdout': result.stdout}


def check_profile(rootfs):
    report = {'status': 'rejected', 'profile': 'lx06/1.94.14',
              'rootfs': str(Path(rootfs).absolute()), 'errors': []}
    try:
        root, _ = preflight(rootfs)
        with tempfile.TemporaryDirectory(prefix='.lx06-check-', dir=root.parent) as temporary:
            tree = Path(temporary) / 'rootfs'
            # No dereferencing of inert firmware symlinks.
            shutil.copytree(root, tree, symlinks=True, ignore=lambda d, n: ['console'] if Path(d) == root/'dev' else [])
            report.update(apply(tree))
    except Exception as exc:
        report['errors'].append(str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['select', 'check', 'apply', 'invariants', 'lowmem'])
    parser.add_argument('rootfs')
    args = parser.parse_args()
    try:
        if args.action == 'select':
            return 0 if selected(root_path(args.rootfs)) else 3
        if args.action == 'check':
            report = check_profile(args.rootfs)
        elif args.action == 'invariants':
            report = {'status': invariants(root_path(args.rootfs))}
        elif args.action == 'lowmem':
            report = apply_lowmem(args.rootfs)
        else:
            report = apply(args.rootfs)
    except Exception as exc:
        report = {'status': 'rejected', 'errors': [str(exc)]}
    print(json.dumps(report, indent=2))
    return 1 if report['status'] == 'rejected' else 0


if __name__ == '__main__':
    raise SystemExit(main())
