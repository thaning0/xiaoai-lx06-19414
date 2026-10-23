"""No firmware blobs in the repository. Opt-in real-fixture tests use external files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import lx06_compat as compat
from tools import lx06_profile as profile
from tools import lx06_offline_build as build

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO / '.lx06-tmp'
SCRATCH.mkdir(exist_ok=True)


class OfflineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_actual_metadata_section_and_duplicate_rejected(self):
        version = self.base/'version'
        version.write_text("config core 'version'\noption HARDWARE 'LX06'\noption ROM '1.94.14'\n")
        self.assertEqual(compat.metadata(version)['ROM'], '1.94.14')
        version.write_text("config version 'version'\noption ROM '1.94.14'\n")
        with self.assertRaises(ValueError):
            compat.metadata(version)
        version.write_text("config core 'version'\nconfig core 'version'\n")
        with self.assertRaises(ValueError):
            compat.metadata(version)

    def test_missing_overlay_does_not_create_output_or_run_host_build(self):
        output = self.base/'output'
        with patch.object(build.subprocess, 'run') as run:
            with self.assertRaises(build.PrerequisiteError):
                build.build(self.base/'absent-image', self.base/'absent-overlay', output, 1)
        run.assert_not_called()
        self.assertFalse(output.exists())

    def test_empty_and_non_arm_overlays_refused_without_execution(self):
        overlay = self.base/'overlay'
        overlay.mkdir()
        with self.assertRaisesRegex(build.PrerequisiteError, 'empty'):
            build.overlay_check(overlay)
        (overlay/'usr/bin').mkdir(parents=True)
        binary = overlay/'usr/bin/bluealsa'
        marker = self.base/'EXECUTED'
        binary.write_text('#!/bin/sh\ntouch '+str(marker)+'\n')
        binary.chmod(0o755)
        with self.assertRaisesRegex(build.PrerequisiteError, 'ARM ELF'):
            build.overlay_check(overlay)
        self.assertFalse(marker.exists())

    def test_unsafe_unified_headers_rejected(self):
        patchfile = self.base/'bad.patch'
        for header in ('--- /etc/passwd\n+++ /etc/passwd',
                       '--- a/../../outside\n+++ b/../../outside',
                       '--- a/path with spaces\n+++ b/path with spaces',
                       'diff --git a/safe b/../outside\n--- a/safe\n+++ b/safe'):
            patchfile.write_text(header+'\n@@ -1 +1 @@\n-old\n+new\n')
            with self.subTest(header=header), self.assertRaises(ValueError):
                compat.validate_patch(patchfile, self.base)

    def test_overlay_symlinks_resolve_only_in_firmware_namespace(self):
        overlay = self.base/'overlay'
        (overlay/'usr/bin').mkdir(parents=True)
        (overlay/'usr/bin/real').write_bytes(b'data')
        (overlay/'usr/bin/link').symlink_to('/usr/bin/real')
        self.assertEqual(build.virtual_file(overlay, 'usr/bin/link'), overlay/'usr/bin/real')
        (overlay/'usr/bin/link').unlink()
        (overlay/'usr/bin/link').symlink_to('../../../outside')
        with self.assertRaises(ValueError):
            build.virtual_file(overlay, 'usr/bin/link')

    def test_overlay_cannot_replace_hardware_or_traverse_destination_links(self):
        stock = self.base/'stock'
        stock.mkdir()
        (stock/'etc').symlink_to('/etc')
        for name in ('etc/passwd', 'lib/modules/new.ko', 'lib/firmware/calibration',
                     'etc/init.d/boot', 'usr/sbin/mlanutl', 'dev/console'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.validate_overlay_destinations(stock, {name: {'type': 'file'}})

    def test_strict_image_size_limit_before_any_tool_invocation(self):
        image = self.base/'oversize'
        with image.open('wb') as stream:
            stream.truncate(build.LIMIT)
        with patch.object(build.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'strict <'):
                build.verify_image(image)
        run.assert_not_called()

    @unittest.skipUnless(shutil.which('mksquashfs') and shutil.which('unsquashfs'), 'SquashFS tools required')
    def test_real_squashfs_format_and_console_pseudo_inode(self):
        tree = self.base/'tree'
        (tree/'dev').mkdir(parents=True)
        (tree/'text').write_text('inert test data\n')
        image = self.base/'test.squashfs'
        build.pack_image(tree, image, 1770618789,
                         dict(os.environ, SOURCE_DATE_EPOCH='1770618789'))
        result = build.verify_image(image)
        self.assertEqual(result['console'], 'c 0600 0:0 5:1')
        self.assertEqual(result['compression'], 'xz')
        self.assertEqual(result['block_size'], 131072)
        self.assertEqual(result['superblock_time'], 1770618789)

    @unittest.skipUnless(shutil.which('bwrap') and shutil.which('fakeroot'), 'confinement tools required')
    def test_sandbox_and_fakeroot_do_not_touch_external_symlink_referent(self):
        work = self.base/'work'
        (work/'repo').mkdir(parents=True)
        external = self.base/'outside'
        external.write_text('must survive')
        original_uid = external.stat().st_uid
        (work/'link').symlink_to(external)
        command = build.sandbox_command(work, ['/usr/bin/fakeroot', '-u', '--', '/bin/sh', '-c',
                   'test ! -e /work/link && chown 0:0 /work/repo && test "$(stat -c %u /work/repo)" = 0'])
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(external.read_text(), 'must survive')
        self.assertEqual(external.stat().st_uid, original_uid)
        self.assertEqual((work/'repo').stat().st_uid, os.getuid())

    def test_makefile_patch_failure_propagates_without_success_marker(self):
        (self.base/'scripts').mkdir()
        (self.base/'scripts/00_fail.sh').write_text('#!/bin/sh\nexit 17\n')
        (self.base/'scripts/01_never.sh').write_text('#!/bin/sh\ntouch SHOULD_NOT_EXIST\n')
        tree = self.base/'tree'
        tree.mkdir()
        result = subprocess.run(['make', '--no-print-directory', '-f', str(REPO/'Makefile'),
                                 'patch', 'MODEL=lx06', 'BUILD_DIR='+str(tree)],
                                cwd=self.base, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exit code 17', result.stdout)
        self.assertFalse((tree/'patched').exists())
        self.assertFalse((self.base/'SHOULD_NOT_EXIST').exists())

    def test_makefile_size_failure_propagates(self):
        # Exercise the actual make recipe with an inert image producer.
        bindir = self.base/'bin'
        bindir.mkdir()
        fake = bindir/'mksquashfs'
        fake.write_text('#!/bin/sh\ntruncate -s 41943040 "$2"\n')
        fake.chmod(0o755)
        destination = self.base/'dest'
        destination.mkdir()
        result = subprocess.run(['make', '--no-print-directory', 'build_squashfs', 'MODEL=lx06',
                                 'BUILD_DIR='+str(self.base/'tree'), 'DESTDIR='+str(destination),
                                 'IMAGE_NAME=image'], cwd=REPO,
                                env=dict(os.environ, PATH=str(bindir)+':/usr/bin:/bin'),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('strict size limit', result.stdout)


@unittest.skipUnless(os.environ.get('LX06_STOCK_ROOTFS'), 'set LX06_STOCK_ROOTFS to verified external extraction')
class RealProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stock = Path(os.environ['LX06_STOCK_ROOTFS']).absolute()
        cls.manifest = json.loads((profile.PROFILE/'manifest.json').read_text())
        profile.preflight(cls.stock)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'root'
        self.root.mkdir()
        for name in list(self.manifest['targets']) + ['usr/share/mico/version']:
            dest = self.root/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.stock/name, dest)
        # Legitimate absolute links must remain inert even when absent on host.
        (self.root/'var').symlink_to('/tmp')
        (self.root/'root').mkdir()
        (self.root/'root/.ssh').symlink_to('/data/ssh')

    def snapshot(self):
        return build.inventory(self.root)

    def test_exact_apply_outputs_invariants_and_hardware_preservation(self):
        result = profile.apply(self.root)
        self.assertEqual(result['status'], 'profile-validated')
        self.assertEqual(profile.invariants(self.root), 'passed')
        for name, hashes in self.manifest['targets'].items():
            self.assertEqual(profile.digest(self.root/name), hashes['result_sha256'])
        # Compare untouched board-specific function bodies, independently of hashes.
        def function(text, name):
            import re
            return re.search(r'(?ms)^(?:function )?'+name+r'\(\)\s*\n?\{\n.*?^\}', text).group()
        for path, names in {
            'etc/init.d/wireless': ['firmware_prepare_sta', 'firmware_prepare_ap', 'firmware_stop'],
            'etc/init.d/bluetooth': ['marvell_setup'],
            'etc/init.d/boot': ['rtl8821_cfg', 'wifi_bt_driver_prepare_lx06', 'binfo_create_lx06'],
            'etc/init.d/bluetoothd': ['start_marvell', 'start_marvell_8987'],
        }.items():
            for name in names:
                self.assertEqual(function((self.root/path).read_text(), name),
                                 function((self.stock/path).read_text(), name))
        self.assertTrue((self.root/'var').is_symlink())
        self.assertEqual(os.readlink(self.root/'var'), '/tmp')

    def test_bounded_migration_fragment_preserves_credentials_and_fails_closed(self):
        profile.apply(self.root)
        import re
        text = (self.root/'etc/init.d/wireless').read_text()
        fragment = re.search(r'(?ms)^migrate_wireless_conf\(\)\n\{\n.*?^\}', text).group()
        # Execute only this isolated copy/file fragment with fake paths, never
        # the firmware script, its imports, hardware setup or station startup.
        wifi = self.root/'fixture-wifi'; wifi.mkdir()
        legacy = wifi/'wpa.conf'; target = wifi/'wpa_supplicant.conf'
        fragment = fragment.replace('/data/wifi/wpa.conf', str(legacy))
        script = 'WIRELESS_CONF="'+str(target)+'"\nwireless_log() { :; }\n'+fragment+'\nmigrate_wireless_conf\n'
        def run():
            return subprocess.run(['/bin/sh', '-c', script], capture_output=True).returncode
        self.assertNotEqual(run(), 0)
        self.assertFalse(target.exists())
        legacy.write_text('network={ssid="paired"}\n')
        self.assertEqual(run(), 0)
        self.assertEqual(target.read_bytes(), legacy.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        target.write_text('custom config\n')
        self.assertEqual(run(), 0); self.assertEqual(target.read_text(), 'custom config\n')
        target.write_text('')
        self.assertNotEqual(run(), 0); self.assertEqual(target.read_bytes(), b'')
        target.unlink(); target.symlink_to(wifi/'absent')
        self.assertNotEqual(run(), 0); self.assertTrue(target.is_symlink())
        target.unlink(); legacy.write_text('')
        self.assertNotEqual(run(), 0); self.assertFalse(target.exists())
        # An unavailable destination must propagate a copy/link failure.
        legacy.write_text('nonempty')
        bad = script.replace(str(target), str(wifi/'absent-dir'/'config'))
        self.assertNotEqual(subprocess.run(['/bin/sh', '-c', bad], capture_output=True).returncode, 0)

    def test_wrong_version_and_changed_preimage_before_any_mutation(self):
        metadata = self.root/'usr/share/mico/version'
        metadata.write_text(metadata.read_text().replace("'1.94.14'", "'1.94.13'"))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'metadata'):
            profile.apply(self.root)
        self.assertEqual(self.snapshot(), before)
        shutil.copy2(self.stock/'usr/share/mico/version', metadata)
        target = self.root/'etc/init.d/bluetooth'
        target.write_text(target.read_text()+'\n# changed\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'preimage'):
            profile.apply(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_already_patched_refused_without_mutation(self):
        profile.apply(self.root)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'already patched'):
            profile.apply(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_selected_profile_patch_failure_never_falls_back(self):
        target = self.root/'etc/init.d/wireless'
        target.write_text(target.read_text()+'# modified\n')
        before = self.snapshot()
        result = subprocess.run(['/bin/sh', 'scripts/10_apply_patches.sh'], cwd=REPO,
                                env=dict(os.environ, MODEL='lx06', ROOTFS=str(self.root)),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('preimage', result.stdout)
        self.assertNotIn('[*] Applying patches', result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_patch_and_metadata_symlink_ancestors_rejected(self):
        for name in ('etc/init.d/bluetooth', 'usr/share/mico/version'):
            target = self.root/name
            target.unlink()
            target.symlink_to(self.stock/name)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                profile.preflight(self.root)
            target.unlink()
            shutil.copy2(self.stock/name, target)
        directory = self.root/'etc/init.d'
        directory.rename(self.root/'saved-init')
        directory.symlink_to(self.root/'saved-init', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            profile.preflight(self.root)


if __name__ == '__main__':
    unittest.main()
