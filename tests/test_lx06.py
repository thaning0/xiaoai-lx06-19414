import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tools import lx06_usb_read as usb
from tools import lx06_compat as compat

BASE = Path(__file__).resolve().parents[1] / '.lx06-tmp'
BASE.mkdir(exist_ok=True)


class Device:
    def __init__(self, fail=None):
        self.events = []
        self.packets = 0
        self.fail = fail
        self.status = True

    def ctrl_transfer(self, *args, timeout):
        assert 0 < timeout < 60000
        self.events.append(args)
        if args[1] == 0x20:
            return b'bad!' if self.fail == 'identify' else bytes.fromhex('00070010')
        if args[1] == 0x34:
            assert self.events[-2] == 'claim'
            assert args[:4] == (0x40, 0x34, 0, 2)
            assert len(args[4]) == 64
            return 63 if self.fail == 'command' else 64
        assert args == (0xc0, 0x33, 4096, 16, 16)
        assert self.packets % 128 == 0
        if self.fail == 'header':
            return b'bad'
        return bytes(4) + (512 if self.fail == 'header-size' else 65536).to_bytes(4, 'little') + bytes(8)

    def read(self, endpoint, length, timeout):
        assert (endpoint, length) == (0x81, 512)
        assert 0 < timeout < 60000
        self.events.append('read')
        if self.status:
            self.status = False
            return (b'successful' if self.fail == 'status' else b'success').ljust(512, b'\0')
        self.packets += 1
        if self.packets == 130:
            if self.fail == 'exception':
                raise OSError('fake USB fault')
            if self.fail == 'short':
                return b'x'
            if self.fail == 'mismatch':
                return b'x'*512
        return b'a'*512

    def get_active_configuration(self):
        self.events.append('configuration')

    def __getattr__(self, name):
        if name in ('write', 'reset', 'clear_halt', 'set_configuration', 'detach_kernel_driver'):
            raise AssertionError('forbidden operation ' + name)
        raise AttributeError(name)


class USBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dev = Device()
        self.disposed = []
        self.loads = 0

    def loader(self, dll):
        self.loads += 1
        def find(**kwargs):
            self.assertEqual(kwargs, dict(find_all=True, idVendor=0x1b8e, idProduct=0xc003, backend='libusb1'))
            return iter([self.dev])
        util = types.SimpleNamespace(claim_interface=lambda d, i: d.events.append('claim'),
                                     dispose_resources=self.disposed.append)
        return types.SimpleNamespace(find=find), util, 'libusb1'

    def read(self, **kwargs):
        return usb.operate('read', part='boot0', length=131072, output=self.root/'out',
                           load_backend=self.loader, system='Linux', **kwargs)

    def test_two_windows_exact_setup(self):
        result = self.read()
        self.assertEqual(self.dev.packets, 256)
        self.assertEqual(self.dev.events[0], (0xc0, 0x20, 0, 0, 4))
        self.assertEqual(self.dev.events[1:3], ['configuration', 'claim'])
        command = next(e for e in self.dev.events if isinstance(e, tuple) and e[1] == 0x34)
        self.assertEqual(command[4], b'upload store boot0 normal 0x20000'.ljust(64, b'\0'))
        headers = [i for i, e in enumerate(self.dev.events) if isinstance(e, tuple) and e[1] == 0x33]
        self.assertEqual(headers[1]-headers[0], 129)
        self.assertEqual((self.root/'out/boot0.img').read_bytes(), b'a'*131072)
        self.assertEqual([result[k] for k in ('vid', 'pid', 'identify', 'backend')],
                         ['1b8e', 'c003', '00070010', 'libusb1'])
        self.assertTrue(result['timestamp_utc'].endswith('+00:00'))
        self.assertEqual(result['sha256'], hashlib.sha256(b'a'*131072).hexdigest())
        self.assertEqual(self.disposed, [self.dev])

    def test_failures_stop_and_preserve_prefix(self):
        reference = self.root/'ref'
        reference.write_bytes(b'a'*131072)
        for failure in ('short', 'exception', 'mismatch', 'header', 'header-size', 'status', 'command'):
            with self.subTest(failure=failure):
                self.dev = Device(failure)
                out = self.root/failure
                with self.assertRaises(usb.Failure) as caught:
                    usb.operate('read', part='boot0', length=131072, output=out,
                                reference=reference, reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                                load_backend=self.loader, system='Linux')
                self.assertEqual(caught.exception.code, usb.TRANSFER)
                self.assertFalse((out/'boot0.img').exists())
                diagnostic = json.loads((out/'failure.json').read_text())
                if failure in ('short', 'exception', 'mismatch'):
                    self.assertEqual(self.dev.packets, 130)
                    self.assertEqual(diagnostic['absolute_partition_offset'], 129*512)
                    self.assertEqual((out/'boot0.partial').stat().st_size, 129*512)
                    expected = b'x'*512 if failure == 'mismatch' else b'x' if failure == 'short' else b''
                    self.assertEqual((out/'failure.bin').read_bytes(), expected)
                else:
                    self.assertEqual(self.dev.packets, 0)
                self.assertIn(self.dev, self.disposed)

    def test_finalization_failures_preserve_complete_partial(self):
        real_fsync = os.fsync
        for failure in ('link', 'data-fsync', 'manifest-fsync', 'manifest-open'):
            with self.subTest(failure=failure):
                self.dev = Device()
                out = self.root/failure
                calls = 0
                def fsync(fd):
                    nonlocal calls
                    calls += 1
                    if (failure == 'data-fsync' and calls == 1 or
                            failure == 'manifest-fsync' and calls == 2):
                        raise OSError('fake fsync failure')
                    real_fsync(fd)
                real_open = open
                def opening(path, mode, *args, **kwargs):
                    if failure == 'manifest-open' and Path(path).name == 'manifest.json':
                        raise OSError('fake manifest failure')
                    return real_open(path, mode, *args, **kwargs)
                def link(src, dst):
                    if failure == 'link':
                        raise OSError('fake publication failure')
                    os_link(src, dst)
                os_link = os.link
                with patch.object(usb.os, 'fsync', fsync), patch.object(usb.os, 'link', link), patch('builtins.open', opening):
                    with self.assertRaises(usb.Failure) as caught:
                        usb.operate('read', part='boot0', length=131072, output=out,
                                    system='Linux', load_backend=self.loader)
                self.assertEqual(caught.exception.code, usb.INPUT)
                self.assertFalse((out/'boot0.img').exists())
                self.assertFalse((out/'manifest.json').exists())
                self.assertEqual((out/'boot0.partial').read_bytes(), b'a'*131072)
                diagnostic = json.loads((out/'failure.json').read_text())
                self.assertEqual(diagnostic['stage'], 'finalize')
                self.assertEqual(diagnostic['absolute_partition_offset'], 131072)
                self.assertIn(self.dev, self.disposed)

    def test_existing_manifest_not_removed_on_failure(self):
        real_link = os.link
        out = self.root/'out'
        def link(src, dst):
            real_link(src, dst)
            (out/'manifest.json').write_text('preexisting competitor')
        with patch.object(usb.os, 'link', link):
            with self.assertRaises(usb.Failure):
                self.read()
        self.assertEqual((out/'manifest.json').read_text(), 'preexisting competitor')
        self.assertFalse((out/'boot0.img').exists())
        self.assertEqual((out/'boot0.partial').stat().st_size, 131072)
        self.assertEqual(json.loads((out/'failure.json').read_text())['stage'], 'finalize')
        self.assertEqual(self.disposed, [self.dev])

    def test_short_file_write_excludes_packet(self):
        real_open = open
        class ShortWriter:
            def __init__(self, stream):
                self.stream = stream
                self.calls = 0
            def write(self, data):
                self.calls += 1
                return self.stream.write(data[:7] if self.calls == 2 else data)
            def __getattr__(self, name):
                return getattr(self.stream, name)
        def opening(path, mode, *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            return ShortWriter(stream) if str(path).endswith('.partial') else stream
        with patch('builtins.open', opening):
            with self.assertRaises(usb.Failure) as caught:
                self.read()
        out = self.root/'out'
        self.assertEqual(caught.exception.code, usb.INPUT)
        self.assertEqual((out/'boot0.partial').read_bytes(), b'a'*512)
        self.assertEqual((out/'failure.bin').read_bytes(), b'a'*512)
        self.assertFalse((out/'boot0.img').exists())
        self.assertEqual(self.disposed, [self.dev])

    def test_diagnostics_failure_preserves_original_error(self):
        self.dev = Device('identify')
        real_open = open
        def opening(path, mode, *args, **kwargs):
            if Path(path).name.startswith('failure.'):
                raise OSError('diagnostics unavailable')
            return real_open(path, mode, *args, **kwargs)
        with patch('builtins.open', opening):
            with self.assertRaises(usb.Failure) as caught:
                self.read()
        self.assertEqual(caught.exception.code, usb.IDENTIFY)
        self.assertIn('unexpected identify', str(caught.exception))
        self.assertEqual(self.disposed, [self.dev])

    def test_reference_invalid_before_guard_or_backend(self):
        reference = self.root/'ref'
        reference.write_bytes(b'a'*131072)
        with self.assertRaises(usb.Failure) as caught:
            usb.operate('read', part='boot0', length=131072, output=self.root/'out',
                        reference=reference, reference_sha256='0'*64, system='Windows',
                        guard=lambda: self.fail('guard called'), load_backend=lambda _: self.fail('backend called'))
        self.assertEqual(caught.exception.code, usb.INPUT)

    def test_guard_failures_before_backend(self):
        for records in ([], None, {'InstanceId': 'x', 'Service': 'WinUSB', 'Status': 'OK'}, [42], [{ 'InstanceId': 'x', 'Service': 'WinUSB'}]*2,
                        [{'InstanceId': 'x', 'Service': 'libusb0'}],
                        [{'InstanceId': 'x'}]):
            def runner(*a, **k):
                self.assertIn('Get-PnpDevice -PresentOnly', a[0][-1])
                self.assertIn('DEVPKEY_Device_Service', a[0][-1])
                self.assertIn('ConvertTo-Json -InputObject $records', a[0][-1])
                self.assertIn('Status=$_.Status', a[0][-1])
                return types.SimpleNamespace(stdout=json.dumps(records))
            with self.assertRaises(usb.Failure) as caught:
                usb.operate('identify', system='Windows', guard=lambda: usb.windows_guard(runner),
                            load_backend=lambda _: self.fail('backend called'))
            self.assertEqual(caught.exception.code, usb.NO_DEVICE if records == [] else usb.GUARD)
        with self.assertRaises(usb.Failure) as caught:
            usb.operate('identify', system='Windows',
                        guard=lambda: usb.windows_guard(lambda *a, **k: (_ for _ in ()).throw(OSError('unavailable'))),
                        load_backend=lambda _: self.fail('backend called'))
        self.assertEqual(caught.exception.code, usb.GUARD)
        evidence = usb.windows_guard(lambda *a, **k: types.SimpleNamespace(
            stdout='[{"InstanceId":"x","Service":"winUSB","Status":"OK"}]'))
        self.assertTrue(evidence['checked'])
        self.assertEqual(evidence['devices'][0]['Status'], 'OK')
        self.assertEqual(evidence['query'], 'Get-PnpDevice -PresentOnly')
        for stdout in ('', 'bad json'):
            with self.assertRaises(usb.Failure) as caught:
                usb.operate('identify', system='Windows',
                            guard=lambda: usb.windows_guard(lambda *a, **k: types.SimpleNamespace(stdout=stdout)),
                            load_backend=lambda _: self.fail('backend called'))
            self.assertEqual(caught.exception.code, usb.GUARD)

    def test_identify_and_disposal_and_no_enumeration(self):
        self.assertEqual(usb.operate('identify', load_backend=self.loader, system='Linux')['identify'], '00070010')
        self.dev = Device('identify')
        with self.assertRaises(usb.Failure) as caught:
            usb.operate('identify', load_backend=self.loader, system='Linux')
        self.assertEqual(caught.exception.code, usb.IDENTIFY)
        self.assertIn(self.dev, self.disposed)
        with self.assertRaises(usb.Failure) as caught:
            usb.operate('identify', system='Linux', load_backend=lambda _: (types.SimpleNamespace(find=lambda **k: []), types.SimpleNamespace(), 'be'))
        self.assertEqual(caught.exception.code, usb.NO_DEVICE)

    def test_partial_enumeration_disposes_and_duplicate_rejected(self):
        def broken():
            yield self.dev
            raise OSError('enumeration failed')
        core, util, be = self.loader(None)
        core.find = lambda **k: broken()
        with self.assertRaises(usb.Failure):
            usb.operate('identify', system='Linux', load_backend=lambda _: (core, util, be))
        self.assertEqual(self.disposed, [self.dev])
        other = Device()
        core.find = lambda **k: [self.dev, other]
        with self.assertRaises(usb.Failure):
            usb.operate('identify', system='Linux', load_backend=lambda _: (core, util, be))
        self.assertIn(other, self.disposed)
        self.assertFalse(other.events)

    def test_cli_invalid_input_code(self):
        result = subprocess.run([sys.executable, 'tools/lx06_usb_read.py', 'read',
                                 'boot0', '--length', 'not-a-number', '--output', 'unused'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, usb.INPUT)
        self.assertEqual(json.loads(result.stderr)['exit_code'], usb.INPUT)

    def test_interrupt_retains_prefix_and_disposes(self):
        original_read = self.dev.read
        def interrupted_read(*args, **kwargs):
            if self.dev.packets == 1:
                raise KeyboardInterrupt()
            return original_read(*args, **kwargs)
        self.dev.read = interrupted_read
        with self.assertRaises(usb.Failure) as caught:
            self.read()
        self.assertEqual(caught.exception.code, usb.TRANSFER)
        out = self.root/'out'
        self.assertEqual((out/'boot0.partial').stat().st_size, 512)
        self.assertFalse((out/'boot0.img').exists())
        self.assertEqual(json.loads((out/'failure.json').read_text())['absolute_partition_offset'], 512)
        self.assertEqual(self.disposed, [self.dev])

    def test_import_no_usb(self):
        code = "import sys; from tools import lx06_usb_read; assert not any(x == 'usb' or x.startswith('usb.') for x in sys.modules)"
        subprocess.run([sys.executable, '-c', code], check=True)

    def test_backend_only_libusb1(self):
        calls = []
        package = types.ModuleType('usb')
        package.__path__ = []
        package.core = types.ModuleType('usb.core')
        package.util = types.ModuleType('usb.util')
        package.backend = types.ModuleType('usb.backend')
        package.backend.__path__ = []
        package.backend.libusb1 = types.ModuleType('usb.backend.libusb1')
        package.backend.libusb1.get_backend = lambda **k: calls.append(k) or 'be'
        modules = {m.__name__: m for m in (package, package.core, package.util, package.backend, package.backend.libusb1)}
        dll = self.root/'libusb-1.0.dll'
        dll.write_bytes(b'fake')
        with patch.dict(sys.modules, modules):
            self.assertEqual(usb.backend(str(dll))[2], 'be')
            self.assertEqual(calls[0]['find_library']('anything'), str(dll.resolve()))
        wrong = self.root/'libusb0.dll'
        wrong.write_bytes(b'fake')
        with self.assertRaises(usb.Failure):
            usb.backend(str(wrong))

    def test_output_and_bounds_rejected(self):
        for length in (0, -65536, 512, 6*1024*1024+65536):
            with self.assertRaises(usb.Failure):
                usb.operate('read', part='boot0', length=length, output=self.root/'out',
                            load_backend=lambda _: self.fail('backend'))
        out = self.root/'out'
        out.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(usb.Failure):
            self.read()
        self.assertEqual(self.loads, 0)


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'root'
        self.version = self.root/'usr/share/mico/version'
        self.version.parent.mkdir(parents=True)
        self.version.write_text("config core 'version'\n option HARDWARE 'LX06'\n option ROM '1.94.14'\n")
        self.patches = self.base/'patches'
        (self.patches/'lx06').mkdir(parents=True)

    def test_metadata_rejections(self):
        for text in ('', "config core 'version'\noption HARDWARE 'LX05'\noption ROM '1.94.14'", "config core 'version'\noption HARDWARE 'LX06'\noption ROM '1.0'"):
            self.version.write_text(text)
            result = compat.check(self.root, patches=self.patches)
            self.assertEqual(result['status'], 'rejected')
            self.assertTrue(result['errors'])
        self.version.unlink()
        self.assertTrue(compat.check(self.root)['missing'])
        self.assertTrue(compat.check(self.base/'absent')['missing'])

    def test_missing_keys_and_expected_override(self):
        self.version.write_text("config core 'version'\n")
        self.assertEqual(compat.check(self.root)['missing'], ['HARDWARE', 'ROM'])
        self.version.write_text("config core 'version'\noption HARDWARE 'LX06'\noption ROM '2.0'\n")
        result = compat.check(self.root, expected='2.0', patches=self.patches)
        self.assertEqual(result['status'], 'needs-review')
        self.assertEqual(result['expected_rom'], '2.0')
        self.assertEqual(result['rootfs'], str(self.root))

    def test_scratch_containment_before_copy(self):
        # Simulate a repository inside the rootfs; rootfs inside scratch is
        # already exercised by every ordinary fixture in this suite.
        with patch.object(compat, 'REPO', self.root), patch.object(compat.shutil, 'copytree') as copy:
            result = compat.check(self.root, patches=self.patches)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('scratch', result['errors'][0])
        copy.assert_not_called()

    def test_direct_metadata_symlink_escape(self):
        external = self.base/'external-version'
        external.write_text(self.version.read_text())
        self.version.unlink()
        self.version.symlink_to(external)
        self.assertEqual(compat.check(self.root)['status'], 'rejected')

    def test_report_inside_input_refused(self):
        destination = self.root/'report.json'
        result = subprocess.run([sys.executable, 'tools/lx06_compat.py', str(self.root),
                                 '--report', str(destination)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(destination.exists())
        self.assertIn('inside input tree', json.loads(result.stdout)['errors'][-1])

    def test_host_patch_exit_two_and_exception_retained(self):
        for name in ('01', '02', '03'):
            (self.patches/(name+'.patch')).write_text('--- a/target\n+++ b/target\n@@ -1 +1 @@\n-a\n+b\n')
        def run(args, **kwargs):
            if args[-1] == '--version':
                return types.SimpleNamespace(returncode=0, stdout='GNU patch', stderr='')
            if args[-1].endswith('02.patch'):
                raise subprocess.TimeoutExpired(args, 60, output=b'partial stdout', stderr=b'partial stderr')
            return types.SimpleNamespace(returncode=2, stdout='malformed', stderr='host error')
        result = compat.check(self.root, patches=self.patches, run=run)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual([p['exit_code'] for p in result['patches']], [2, None, 2])
        self.assertEqual(result['patches'][1]['stdout'], 'partial stdout')
        self.assertEqual(result['patches'][1]['stderr'], 'partial stderr')
        self.assertIn('error', result['patches'][1])
        self.assertEqual(result['patches'][0]['stderr'], 'host error')

    def test_symlink_escapes(self):
        external = self.base/'external'
        external.mkdir()
        (self.root/'escape').symlink_to(external, target_is_directory=True)
        self.assertEqual(compat.check(self.root, patches=self.patches)['status'], 'needs-review')
        (self.patches/'unsafe.patch').write_text('--- a/escape/target\n+++ b/escape/target\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertEqual(compat.check(self.root, patches=self.patches)['status'], 'rejected')
        (self.patches/'unsafe.patch').unlink()
        (self.root/'escape').unlink()
        self.version.unlink()
        self.version.parent.rmdir()
        (self.root/'usr/share/mico').symlink_to(external, target_is_directory=True)
        self.assertEqual(compat.check(self.root)['status'], 'rejected')
        alias = self.base/'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(compat.check(alias)['status'], 'rejected')

    def test_real_dryruns_outputs_originals_and_no_execution(self):
        target = self.root/'target'
        target.write_text('old\n')
        marker = self.base/'EXECUTED'
        binary = self.root/'bin/patch'
        binary.parent.mkdir()
        binary.write_text(f'#!/bin/sh\ntouch {marker}\n')
        binary.chmod(0o755)
        (self.patches/'01.patch').write_text('--- a/target\n+++ b/target\n@@ -1 +1 @@\n-old\n+new\n')
        (self.patches/'lx06/02.patch').write_text('--- a/target\n+++ b/target\n@@ -1 +1 @@\n-impossible\n+other\n')
        result = compat.check(self.root, patches=self.patches)
        self.assertEqual(result['status'], 'needs-review')
        self.assertEqual([p['exit_code'] for p in result['patches']], [0, 1])
        self.assertTrue(all('stdout' in p and 'stderr' in p for p in result['patches']))
        self.assertEqual(target.read_text(), 'old\n')
        self.assertFalse(marker.exists())
        self.assertFalse(list(self.root.rglob('*.rej')))

    def test_repository_patches_all_reported_in_sorted_groups(self):
        result = compat.check(self.root)
        selected = sorted((compat.REPO/'patches').glob('*.patch')) + sorted((compat.REPO/'patches/lx06').glob('*.patch'))
        self.assertEqual(result['status'], 'needs-review', result['errors'])
        self.assertFalse(any(p['exit_code'] == 2 for p in result['patches']))
        self.assertEqual([p['patch'] for p in result['patches']], [str(p) for p in selected])
        self.assertTrue(any(p['exit_code'] for p in result['patches']))
        self.assertTrue(all(isinstance(p['stdout'], str) and isinstance(p['stderr'], str) for p in result['patches']))

    def test_unsafe_patch_and_dependency_failure(self):
        (self.patches/'bad.patch').write_text('--- a/../../outside\n+++ b/../../outside\n@@ -1 +1 @@\n-a\n+b\n')
        self.assertEqual(compat.check(self.root, patches=self.patches)['status'], 'rejected')
        self.assertEqual(compat.check(self.root, patch_executable='/nonexistent/patch')['status'], 'rejected')
        result = compat.check(self.root, run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout='', stderr='host failure'))
        self.assertEqual(result['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
