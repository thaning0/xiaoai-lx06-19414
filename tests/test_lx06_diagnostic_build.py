"""Focused permission-packing and diagnostic guard regressions; no target execution."""
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import copy

from tools import lx06_diagnostic_build as diag
from tools import lx06_offline_build as common


class DiagnosticRepairTests(unittest.TestCase):
    def test_exact_mode_overlay_and_unexpected_mode_rejected(self):
        self.assertEqual(len(diag.PERMISSION_REPAIRS), 11)
        self.assertEqual({n for n, (old, new) in diag.PERMISSION_REPAIRS.items()
                          if new & 0o111 and not old & 0o111}, {
            'bin/wpa_action.sh', 'etc/init.d/bluetooth', 'etc/init.d/boot',
            'etc/init.d/dropbear', 'usr/sbin/boot_function.sh'})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, (old, _) in diag.PERMISSION_REPAIRS.items():
                p = root/name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'unchanged')
                p.chmod(old)
            first = root/next(iter(diag.PERMISSION_REPAIRS))
            first.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'unexpected base permission'):
                diag.repair_permissions(root)
            first.chmod(diag.PERMISSION_REPAIRS[first.relative_to(root).as_posix()][0])
            diag.repair_permissions(root)
            for name, (_, intended) in diag.PERMISSION_REPAIRS.items():
                self.assertEqual((root/name).stat().st_mode & 0o7777, intended)
                self.assertEqual((root/name).read_bytes(), b'unchanged')
            # A current offline build may already have every intended mode.
            diag.repair_permissions(root)

    def test_tree_comparison_rejects_lost_execute_bits(self):
        with self.assertRaisesRegex(ValueError, 'metadata/content mismatch'):
            common.compare_trees({'boot': {'mode': 0o755}}, {'boot': {'mode': 0o664}})

    def test_seed_symlinks_rejected_without_writes(self):
        helper = (common.REPO/'diagnostic/lx06-diagnostic.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'data'
            (data/'lx06-diagnostic').mkdir(parents=True)
            (data/'status').mkdir()
            target = root/'sentinel'
            target.write_text('preserve')
            # Host shell executes only the repository helper with fixture paths.
            helper = helper.replace('/data', str(data))
            helper = helper.replace("grep -q ' " + str(data) + " ubifs ' /proc/mounts || return 1", ':')
            for leaf in (data/'lx06-diagnostic/wifi-seeded-v1', data/'status/config_done'):
                for dest in (target, root/'dangling'):
                    leaf.symlink_to(dest)
                    result = subprocess.run(['/bin/sh', '-c', helper + '\nlx06_diag_seed_wifi'],
                                            capture_output=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(target.read_text(), 'preserve')
                    self.assertFalse((root/'dangling').exists())
                    self.assertFalse((data/'wifi/wpa_supplicant.conf').exists())
                    leaf.unlink()

    @unittest.skipUnless(shutil.which('fakeroot') and shutil.which('mksquashfs'), 'host packing tools')
    def test_pack_ignores_stale_fakeroot_modes_and_audits_every_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'root'
            (root/'dev').mkdir(parents=True)
            boot = root/'boot'
            boot.write_text('fixture')
            boot.chmod(0o664)
            state = Path(tmp)/'fakeroot.state'
            subprocess.run(['fakeroot', '-s', str(state), '--', 'chmod', '664', str(boot)],
                           check=True, stdout=subprocess.DEVNULL)
            boot.chmod(0o755)
            image = Path(tmp)/'image.squashfs'
            code = ('from pathlib import Path; from tools import lx06_offline_build as b; '
                    f'r=Path({str(root)!r}); '
                    'assert b.inventory(r)["boot"]["mode"] == 0o664; '
                    'assert b.physical_inventory(r)["boot"]["mode"] == 0o755; '
                    f'b.pack_image(r,Path({str(image)!r}),1770618789,dict(__import__("os").environ))')
            subprocess.run(['fakeroot', '-i', str(state), '--', 'python3', '-c', code],
                           check=True, cwd=common.REPO, stdout=subprocess.DEVNULL)
            audit = json.loads(image.with_suffix('.tree-audit.json').read_text())
            self.assertEqual(audit['tree']['boot']['mode'], 0o755)
            self.assertEqual(audit['all_owners'], '0:0')
            self.assertEqual(audit['console'], 'c 0600 0:0 5:1')


class BaseReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=common.REPO/'.lx06-tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        overlay = self.root/'overlay'
        overlay.mkdir()
        (overlay/'fixture').write_text('inert')
        work = self.root/'work'
        work.mkdir()
        snapshot = common.prepare_snapshot(work, overlay)
        self.report = {'status': 'offline-built-hardware-unverified',
            'source_sha256': common.SOURCE_SHA256, 'source_date_epoch': diag.EPOCH,
            'profile': 'lx06/1.94.14',
            'tree_manifests': ['work/original-tree.json', 'work/patched-tree.json'],
            'image': {'size': 4096, 'sha256': 'a'*64},
            'build_inputs': common.inventory(snapshot),
            'overlay_entries': common.inventory(overlay),
            'dependency_audit': {'failures': []}}

    def test_current_complete_report_accepted(self):
        self.assertIs(diag.validate_report(self.report), self.report)

    def test_status_source_profile_epoch_and_missing_audit_rejected(self):
        for key, value in (('status', 'failed'), ('source_sha256', '0'*64),
                           ('profile', 'lx06/1.74.10'), ('source_date_epoch', 0),
                           ('tree_manifests', []), ('image', {}),
                           ('overlay_entries', {}), ('dependency_audit', {})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                diag.validate_report(dict(self.report, **{key: value}))

    def test_current_source_or_profile_snapshot_tamper_rejected(self):
        for name in ('tools/lx06_offline_build.py', 'profiles/lx06/1.94.14/manifest.json',
                     'api/static/app.js', 'scripts/97_local_api.sh'):
            report = copy.deepcopy(self.report)
            report['build_inputs'][name]['sha256'] = '0'*64
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'snapshot'):
                diag.validate_report(report)

    def test_hash_only_or_historical_base_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, 'base-report required'):
            diag.build(self.root/'absent', self.root/'wifi', self.root/'out')
        self.assertFalse((self.root/'out').exists())

    def test_image_hash_or_size_mismatch_rejected_before_output(self):
        path = self.root/'build-manifest.json'
        path.write_text(json.dumps(self.report))
        for key, value in (('size', 4097), ('sha256', 'b'*64)):
            with self.subTest(key=key), patch.object(common, 'verify_image',
                    return_value=dict(self.report['image'], **{key: value})):
                with self.assertRaisesRegex(ValueError, 'differs from report'):
                    diag.build(self.root/'image', self.root/'wifi', self.root/'out', path)
            self.assertFalse((self.root/'out').exists())

    def test_original_stock_tamper_rejected_before_output(self):
        path = self.root/'build-manifest.json'
        path.write_text(json.dumps(self.report))
        (self.root/'work/original.squashfs').write_bytes(b'not stock')
        with patch.object(common, 'verify_image', return_value=self.report['image']):
            with self.assertRaisesRegex(ValueError, 'original source size/hash'):
                diag.build(self.root/'image', self.root/'wifi', self.root/'out', path)
        self.assertFalse((self.root/'out').exists())

    @unittest.skipUnless(shutil.which('mksquashfs') and shutil.which('unsquashfs'), 'SquashFS tools')
    def test_real_full_extract_accepts_correct_tree_rejects_mode_content_link_and_owner(self):
        root = self.root/'tree'
        (root/'dev').mkdir(parents=True)
        (root/'file').write_bytes(b'fixture')
        (root/'file').chmod(0o755)
        (root/'link').symlink_to('file')
        image = self.root/'image.squashfs'
        common.pack_image(root, image, diag.EPOCH, {})
        tree = json.loads(image.with_suffix('.tree-audit.json').read_text())['tree']
        diag.verify_base_tree(image, self.root/'valid', tree)
        for kind, field, value in (('mode', 'mode', 0o600), ('content', 'sha256', '0'*64)):
            bad = copy.deepcopy(tree)
            bad['file'][field] = value
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'metadata/content'):
                diag.verify_base_tree(image, self.root/kind, bad)
        bad = copy.deepcopy(tree)
        bad['link']['target'] = 'elsewhere'
        with self.assertRaisesRegex(ValueError, 'metadata/content'):
            diag.verify_base_tree(image, self.root/'bad-link', bad)
        # Change only one image owner: data and modes still match.
        owner_image = self.root/'owner.squashfs'
        subprocess.run(['mksquashfs', str(root), str(owner_image), '-noappend',
                        '-comp', 'xz', '-b', '131072', '-processors', '1',
                        '-p', 'file m 755 1 1',
                        '-p', 'dev/console c 600 0 0 5 1'], check=True,
                       capture_output=True)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            diag.verify_base_tree(owner_image, self.root/'bad-owner', tree)


class PublicCLITests(unittest.TestCase):
    def test_mutating_clis_cannot_reach_operations_or_usb(self):
        from tools import lx06_usb_flash_system1 as flash
        from tools import lx06_usb_select_boot1 as select
        for module in (flash, select):
            with self.subTest(module=module.__name__), patch.object(module, 'operate') as operation:
                with patch('builtins.print') as printed:
                    self.assertEqual(module.main(), module.INPUT)
                operation.assert_not_called()
                self.assertIn('docs/flashing-research.md', printed.call_args.args[0])
