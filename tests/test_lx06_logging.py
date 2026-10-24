"""Repository shell only; native BusyBox runs in private mount/IPC namespaces."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import lx06_offline_build as build
from tools import lx06_profile as profile
from tools import lx06_release_overlay as release
from tools import lx06_diagnostic_build as diagnostic


FILES = ('etc/init.d/syslog-ng', 'usr/bin/coredump.sh')


def logging_fixture(root):
    """The full-context logging hunks contain complete, hash-pinned preimages."""
    manifest = json.loads((profile.PROFILE/'manifest.json').read_text())
    patch = (profile.PROFILE/'default.patch').read_text()
    for name in FILES:
        section = patch.split('+++ b/'+name+'\n', 1)[1].split('--- a/', 1)[0]
        lines = section.splitlines(keepends=True)
        old = ''.join(line[1:] for line in lines if line.startswith((' ', '-')))
        new = ''.join(line[1:] for line in lines if line.startswith((' ', '+')))
        hashes = manifest['targets'][name]
        assert hashlib.sha256(old.encode()).hexdigest() == hashes['sha256']
        assert hashlib.sha256(new.encode()).hexdigest() == hashes['result_sha256']
        dest = root/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(old)
        dest.chmod(0o755)
        result = subprocess.run(['patch', '-p1', '--batch', '--fuzz=0'], cwd=root,
                                input='--- a/'+name+'\n+++ b/'+name+'\n'+section,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert dest.read_text() == new
    profile.logging_invariants(root)


def registrations(root):
    # Source ONLY the repository's patched logging init, with inert procd stubs.
    script = '''
procd_open_instance() { printf 'open %s\n' "$1"; }
procd_set_param() { printf 'param %s\n' "$*"; }
procd_close_instance() { echo close; }
. "$1"
printf 'settings %s %s\n' "$START" "$USE_PROCD"
start_service
start_service
'''
    result = subprocess.run(['/bin/sh', '-c', script, 'fixture', str(root/FILES[0])],
                            check=True, capture_output=True, text=True, cwd=root)
    return result.stdout.splitlines()


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        logging_fixture(self.root)

    def test_startup_fixed_instances_foreground_and_idempotent_without_data(self):
        lines = registrations(self.root)
        self.assertEqual(lines[0], 'settings 50 1')
        instance = [
            'open syslog', 'param command /bin/busybox syslogd -n -C64 -D -f /dev/null',
            'param respawn 3600 5 5', 'close',
            'open kernel', 'param command /bin/busybox klogd -n',
            'param respawn 3600 5 5', 'close']
        self.assertEqual(lines[1:], instance * 2)
        self.assertFalse((self.root/'data').exists())
        self.assertNotIn('service_start', (self.root/FILES[0]).read_text())

    def test_post_copy_invariants_reject_policy_or_mode_regression(self):
        for name in FILES:
            path = self.root/name
            original = path.read_bytes()
            path.write_bytes(original + b'\n# overlay overwrite\n')
            with self.assertRaisesRegex(ValueError, 'bounded logging changed'):
                profile.logging_invariants(self.root)
            path.write_bytes(original)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'not executable'):
                profile.logging_invariants(self.root)
            path.chmod(0o755)

    def test_startup_link_must_survive_overlay(self):
        rc = self.root/'etc/rc.d'; rc.mkdir(parents=True)
        link = rc/'S50syslog-ng'
        for target in (None, '../init.d/wrong-logger'):
            if target:
                link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'startup link changed'):
                profile.logging_invariants(self.root, startup=True)
            link.unlink(missing_ok=True)
        link.symlink_to('../init.d/syslog-ng')
        profile.logging_invariants(self.root, startup=True)

    def test_busybox_version_and_applets_required_before_build(self):
        # Supply inert ELF headers, never executable target code.
        for name in build.REQUIRED:
            path = self.root/'packages'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            header = bytearray(20); header[:6] = b'\x7fELF\x01\x01'; header[18:20] = b'\x28\x00'
            path.write_bytes(header + b'4.3.0\0' + b'5.71\0')
            path.chmod(0o755)
        busybox = self.root/'packages/bin/busybox'
        header = busybox.read_bytes()
        tokens = [b'BusyBox v1.36.1', b'syslogd\0', b'klogd\0', b'logread\0', b'logger\0']
        for missing in tokens:
            busybox.write_bytes(header + b''.join(t for t in tokens if t != missing))
            with self.assertRaisesRegex(build.PrerequisiteError, 'BusyBox 1.36.1'):
                build.overlay_check(self.root/'packages')
        busybox.write_bytes(header + b''.join(tokens))
        build.overlay_check(self.root/'packages')

    def test_import_copy_snapshot_and_diagnostic_source_binding(self):
        for name in FILES:
            self.assertEqual(release.excluded(name), 'exact 1.94.14 profile target')
            with self.assertRaisesRegex(ValueError, 'protected original/profile'):
                build.validate_overlay_destinations(self.root, {name: {'type': 'file'}})
        overlay = self.root/'overlay'; overlay.mkdir()
        (overlay/'inert').write_text('fixture')
        work = self.root/'work'; work.mkdir()
        snapshot = build.prepare_snapshot(work, overlay)
        inputs = build.inventory(snapshot)
        report = {'status': 'offline-built-hardware-unverified',
                  'source_sha256': build.SOURCE_SHA256, 'source_date_epoch': diagnostic.EPOCH,
                  'profile': 'lx06/1.94.14', 'image': {'size': 4096, 'sha256': 'a'*64},
                  'tree_manifests': ['work/original-tree.json', 'work/patched-tree.json'],
                  'build_inputs': inputs, 'overlay_entries': build.inventory(overlay),
                  'dependency_audit': {'failures': []}}
        diagnostic.validate_report(report)
        for name in ('profiles/lx06/1.94.14/default.patch',
                     'profiles/lx06/1.94.14/manifest.json', 'tools/lx06_profile.py'):
            original = inputs[name]['sha256']
            inputs[name]['sha256'] = '0'*64
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                diagnostic.validate_report(report)
            inputs[name]['sha256'] = original

    @unittest.skipUnless(shutil.which('bwrap') and
                         (os.environ.get('LX06_HOST_BUSYBOX') or shutil.which('busybox')),
                         'native BusyBox and bubblewrap required for private ring test')
    def test_real_ring_wrap_crash_hook_and_restart_in_private_namespace(self):
        busybox = Path(os.environ.get('LX06_HOST_BUSYBOX') or shutil.which('busybox')).resolve()
        work = self.root/'namespace'; work.mkdir()
        (work/'repo').mkdir()
        shutil.copy2(busybox, work/'busybox')
        shutil.copy2(self.root/FILES[1], work/'coredump.sh')
        commands = [line.removeprefix('param command ').split()
                    for line in registrations(self.root) if line.startswith('param command ')][:2]
        self.assertEqual(commands[1], ['/bin/busybox', 'klogd', '-n'])
        commands[0][0] = '/work/busybox'
        (work/'command.json').write_text(json.dumps(commands[0]))
        shutil.copy2(Path(__file__).with_name('logging_ring_worker.py'), work/'worker.py')
        # The only runtime rewrite selects the host-native binary, never target ARM.
        hook = (work/'coredump.sh').read_text().replace('/bin/busybox', '/work/busybox')
        (work/'coredump.sh').write_text(hook)
        command = build.sandbox_command(work, ['/usr/bin/python3', '/work/worker.py'])
        # A deliberately unsafe default must be ignored by the actual -f /dev/null.
        (work/'host-etc/syslog.conf').write_text('*.* /work/forbidden-file\n')
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ring/crash/restart passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
