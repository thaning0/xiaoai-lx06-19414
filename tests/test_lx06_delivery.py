"""Host-only regression checks for release import and static dependency decisions."""
import json
import hashlib
import sys
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from tools import lx06_release_overlay as release
from tools import lx06_elf_audit as elf
from tools import lx06_offline_build as build
from tools import lx06_usb_read as usb
from tests import test_lx06 as reader_fixtures


class DeliveryTests(unittest.TestCase):
    def test_import_pin_failure_before_extraction_or_output(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.lx06-tmp') as tmp:
            base = Path(tmp); source = base/'bad'; source.write_bytes(b'bad')
            with patch.object(release.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    release.import_overlay(source, source, base/'output')
            run.assert_not_called(); self.assertFalse((base/'output').exists())

    def test_release_excludes_dangling_repository_link_and_vendor_targets(self):
        self.assertEqual(release.excluded('bin/bc'), 'repository bin script')
        for name in ('etc/init.d/wireless', 'lib/modules/4.9.61/foo.ko', 'usr/share/mico/version',
                     'etc/passwd', 'usr/share/sound/bootup.opus'):
            self.assertIsNotNone(release.excluded(name))

    def test_version_parser_separates_needs_from_definitions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.lx06-tmp') as tmp:
            path = Path(tmp)/'elf'; header = bytearray(52); header[:6] = b'\x7fELF\x01\x01'
            struct.pack_into('<HH', header, 16, 3, 40); path.write_bytes(header)
            output = ''' (NEEDED) Shared library: [libtest.so]
 (RUNPATH) Library runpath: [$ORIGIN/../lib]
Version definition section '.gnu.version_d' contains 1 entry:
  0x00: Rev: 1 Flags: none Index: 2 Cnt: 1 Name: OWN_1
Version needs section '.gnu.version_r' contains 1 entry:
  0000: Version: 1 File: libtest.so Cnt: 1
  0x10: Name: TEST_2 Flags: none Version: 2
'''
            with patch.object(elf.subprocess, 'run') as run:
                run.return_value.stdout = output
                info = elf.elf_info(path)
            self.assertEqual(info['provided_versions'], ['OWN_1'])
            self.assertEqual(info['required_versions'], {'libtest.so': ['TEST_2']})
            self.assertEqual(info['search_path'], ['$ORIGIN/../lib'])

    def test_selected_rpath_provider_must_supply_required_version(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.lx06-tmp') as tmp:
            root = Path(tmp)
            for name in ('usr/bin/app', 'usr/private/real.so', 'lib/libtest.so'):
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'inert')
            (root/'usr/private/libtest.so').symlink_to('/usr/private/real.so')
            def info(path):
                result = {'needed': [], 'search_path': [], 'required_versions': {},
                          'provided_versions': [], 'interpreter': None, 'arm_flags': 0x5000400, 'abi': [1, 1, 40]}
                if path.name == 'app':
                    result.update(needed=['libtest.so'], search_path=['$ORIGIN/../private'],
                                  required_versions={'libtest.so': ['TEST_2']})
                elif path.parent.name == 'lib':
                    result['provided_versions'] = ['TEST_2']
                return result
            with patch.object(elf, 'elf_info', side_effect=info):
                result = elf.audit(root)
            self.assertEqual(result['failures'][0]['error'], 'missing version definitions')
            self.assertEqual(result['failures'][0]['provider'], 'usr/private/real.so')
            self.assertEqual(result['failures'][0]['versions'], ['TEST_2'])


class DataReaderTests(unittest.TestCase):
    setUp = reader_fixtures.USBTests.setUp
    loader = reader_fixtures.USBTests.loader

    def test_data_cap_accepted_before_backend(self):
        # Stop at a mocked backend after validation, without a 20 MiB transfer.
        with patch.object(usb, 'backend') as unused:
            unused.side_effect = usb.Failure(usb.INPUT, 'mock backend reached')
            with self.assertRaisesRegex(usb.Failure, 'mock backend reached'):
                usb.operate('read', part='data', length=0x1400000,
                            output=self.root/'out', system='Linux', load_backend=unused)
            unused.assert_called_once_with(None)
        self.assertTrue((self.root/'out/data.partial').exists())

    def test_invalid_data_requests_before_guard_or_backend(self):
        for part, length in (('data', 0x1410000), ('data', 0x13fffff),
                             ('data', 0), ('data', -65536), ('data0', 65536)):
            with self.subTest(part=part, length=length):
                with patch.object(usb, 'windows_guard') as guard, patch.object(usb, 'backend') as backend:
                    with self.assertRaises(usb.Failure) as caught:
                        usb.operate('read', part=part, length=length, output=self.root/'out',
                                    system='Windows', guard=guard, load_backend=backend)
                    self.assertEqual(caught.exception.code, usb.INPUT)
                    guard.assert_not_called()
                    backend.assert_not_called()
                self.assertFalse((self.root/'out').exists())

    def test_data_cli_and_one_window_reference(self):
        reference = self.root/'reference'
        reference.write_bytes(b'a'*65536)
        digest = hashlib.sha256(reference.read_bytes()).hexdigest()
        out = self.root/'out'
        argv = ['lx06_usb_read.py', 'read', 'data', '--length', '0x10000',
                '--output', str(out), '--reference', str(reference), '--reference-sha256', digest]
        operate = usb.operate
        def mocked_operate(**kwargs):
            return operate(**kwargs, system='Linux', load_backend=self.loader)
        with patch.object(sys, 'argv', argv), patch.object(usb, 'operate', side_effect=mocked_operate), patch('builtins.print'):
            self.assertEqual(usb.main(), 0)
        command = next(e for e in self.dev.events if isinstance(e, tuple) and e[1] == 0x34)
        self.assertEqual(command, (0x40, 0x34, 0, 2,
                                  b'upload store data normal 0x10000'.ljust(64, b'\0')))
        self.assertEqual(self.dev.packets, 128)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['data.img', 'manifest.json'])
        self.assertEqual((out/'data.img').read_bytes(), reference.read_bytes())
        manifest = json.loads((out/'manifest.json').read_text())
        self.assertEqual((manifest['partition'], manifest['length'], manifest['sha256'],
                          manifest['reference_sha256']), ('data', 65536, digest, digest))
        self.assertEqual(self.disposed, [self.dev])

    def test_data_short_packet_preserves_evidence(self):
        original_read = self.dev.read
        def short_read(*args, **kwargs):
            if not self.dev.status and self.dev.packets == 1:
                return b'x'
            return original_read(*args, **kwargs)
        self.dev.read = short_read
        out = self.root/'out'
        with self.assertRaises(usb.Failure) as caught:
            usb.operate('read', part='data', length=65536, output=out,
                        system='Linux', load_backend=self.loader)
        self.assertEqual(caught.exception.code, usb.TRANSFER)
        self.assertEqual((out/'data.partial').read_bytes(), b'a'*512)
        self.assertEqual((out/'failure.bin').read_bytes(), b'x')
        evidence = json.loads((out/'failure.json').read_text())
        self.assertEqual((evidence['stage'], evidence['absolute_partition_offset']), ('packet', 512))
        self.assertFalse((out/'data.img').exists())
        self.assertFalse((out/'manifest.json').exists())
        self.assertEqual(self.disposed, [self.dev])


if __name__ == '__main__':
    unittest.main()
