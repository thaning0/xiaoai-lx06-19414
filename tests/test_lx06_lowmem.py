"""LX06 package-init policy; no target programs, hardware or services run."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import lx06_profile as profile
from tools import lx06_offline_build as build
from tools import lx06_diagnostic_build as diagnostic
from tools import lx06_release_overlay as release


class LowMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'root'
        self.init = self.root/'etc/init.d/squeezelite'
        self.init.parent.mkdir(parents=True)
        shutil.copy2(build.REPO/'packages/squeezelite/config/squeezelite.init', self.init)
        version = self.root/'usr/share/mico/version'
        version.parent.mkdir(parents=True)
        version.write_text("config core 'version'\noption HARDWARE 'LX06'\noption ROM '1.94.14'\n")

    def apply(self, model='lx06'):
        return subprocess.run(['/bin/sh', 'scripts/97_lx06_lowmem.sh'], cwd=build.REPO,
                              env=dict(os.environ, ROOTFS=str(self.root), MODEL=model),
                              capture_output=True, text=True)

    def test_boot_opt_in_and_manual_start_dispatch(self):
        original = self.init.read_text()
        self.assertEqual(self.apply().returncode, 0)
        self.assertTrue(self.init.read_text().startswith(original))
        marker = self.root/'data/squeezelite-autostart'
        marker.parent.mkdir()
        # rc.common provides boot before sourcing the service, then installs the
        # procd start wrapper. The unchanged start_service remains available.
        script = 'boot() { start "$@"; };\n' + self.init.read_text().replace(
            '/data/squeezelite-autostart', str(marker)) + '''
make_cmdline() { cmdline="fixture"; }
procd_open_instance() { echo open; }
procd_set_param() { printf '%s\n' "$*"; }
procd_close_instance() { echo close; }
start() { start_service "$@"; }
"$1"
'''
        def invoke(action):
            return subprocess.run(['/bin/sh', '-c', script, 'fixture', action],
                                  capture_output=True, text=True, check=True).stdout
        self.assertEqual(invoke('boot'), '')
        manual = invoke('start')
        self.assertIn('command /usr/bin/squeezelite fixture', manual)
        self.assertIn('respawn 3600 5 0', manual)  # Existing manual-run behavior retained.
        marker.touch()
        self.assertEqual(invoke('boot'), manual)
        marker.unlink()
        marker.mkdir()  # A directory is not an opt-in file.
        self.assertEqual(invoke('boot'), '')

    def test_exact_copy_guard_profile_scope_and_final_startup_invariants(self):
        original = self.init.read_bytes()
        self.assertEqual(self.apply('lx01').returncode, 0)
        self.assertEqual(self.init.read_bytes(), original)
        version = self.root/'usr/share/mico/version'
        version.write_text(version.read_text().replace('1.94.14', '1.74.10'))
        self.assertEqual(self.apply().returncode, 0)
        self.assertEqual(self.init.read_bytes(), original)
        version.write_text(version.read_text().replace('1.74.10', '1.94.14'))
        self.init.write_bytes(original + b'\n# unknown release init\n')
        before = build.inventory(self.root)
        self.assertNotEqual(self.apply().returncode, 0)
        self.assertEqual(build.inventory(self.root), before)
        self.init.write_bytes(original)
        self.assertEqual(self.apply().returncode, 0)
        before = build.inventory(self.root)
        self.assertNotEqual(self.apply().returncode, 0)  # Build is single-application.
        self.assertEqual(build.inventory(self.root), before)
        rc = self.root/'etc/rc.d'; rc.mkdir()
        link = rc/'S99squeezelite'; link.symlink_to('../init.d/squeezelite')
        program = self.root/'usr/bin/squeezelite'; program.parent.mkdir()
        program.write_bytes(b'inert'); program.chmod(0o755)
        profile.squeezelite_invariants(self.root, startup=True)
        link.unlink()
        with self.assertRaisesRegex(ValueError, 'startup link'):
            profile.squeezelite_invariants(self.root, startup=True)
        # A later copy of the unconditional release init must be rejected.
        self.init.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'opt-in policy'):
            profile.squeezelite_invariants(self.root)

    def test_late_package_application_and_replay_snapshot_binding(self):
        self.assertIsNone(release.excluded('etc/init.d/squeezelite'))
        scripts = sorted(p.name for p in (build.REPO/'scripts').glob('??_*.sh'))
        self.assertLess(scripts.index('92_copy_build_packages.sh'), scripts.index('97_lx06_lowmem.sh'))
        self.assertLess(scripts.index('93_include.sh'), scripts.index('97_lx06_lowmem.sh'))
        self.assertLess(scripts.index('97_lx06_lowmem.sh'), scripts.index('98_buildinfo.sh'))
        overlay = self.root/'overlay'; overlay.mkdir()
        (overlay/'fixture').write_text('inert')
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
        for name in ('scripts/97_lx06_lowmem.sh', 'profiles/lx06/1.94.14/squeezelite.patch',
                     'profiles/lx06/1.94.14/manifest.json'):
            old = inputs[name]['sha256']
            inputs[name]['sha256'] = '0'*64
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                diagnostic.validate_report(report)
            inputs[name]['sha256'] = old


if __name__ == '__main__':
    unittest.main()
