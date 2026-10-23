import contextlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tools import lx06_usb_flash_system1 as flash


SUCCESS = b'success'.ljust(512, b'\0')
OK = b'OK!!' + b'stale shared buffer'.ljust(508, b'!')
BUSY32 = b'Continue:32'.ljust(512, b'\0')
BUSY34 = b'Continue:34'.ljust(512, b'\0')


def evidence(status='OK'):
    return dict(checked=True, query='Get-PnpDevice -PresentOnly',
                devices=[dict(InstanceId='USB\\VID_1B8E&PID_C003\\fake',
                              Service='WinUSB', Status=status)])


class Interface(list):
    bInterfaceNumber = 0
    bAlternateSetting = 0


class Device:
    def __init__(self, replies=None, *, short_command=None, short_header=None,
                 bulk_result=None, identify=bytes.fromhex('00070010'),
                 bad_endpoints=False):
        self.replies = list(replies if replies is not None else [SUCCESS, OK, OK, SUCCESS])
        self.commands, self.headers, self.chunks, self.calls = [], [], [], []
        self.short_command = short_command
        self.short_header = short_header
        self.bulk_result = bulk_result
        self.identify = identify
        self.bad_endpoints = bad_endpoints

    def ctrl_transfer(self, *args, timeout):
        self.calls.append(('control', args[:4], timeout))
        assert 0 < timeout <= 5000
        if args[1] == 0x20:
            assert args == (0xc0, 0x20, 0, 0, 4)
            return self.identify
        if args[1] == 0x34:
            assert args[:4] == (0x40, 0x34, 0, 2)
            assert len(args[4]) == 64
            self.commands.append(args[4])
            return 63 if len(self.commands) == self.short_command else 64
        assert args[:4] == (0x40, 0x32, 1, 0xffff)
        assert len(args[4]) == 32
        self.headers.append(args[4])
        return 31 if len(self.headers) == self.short_header else 32

    def write(self, endpoint, data, timeout):
        assert endpoint == 0x02 and 0 < timeout <= 10000
        self.calls.append(('write', len(data), timeout))
        self.chunks.append(bytes(data))
        if isinstance(self.bulk_result, BaseException):
            raise self.bulk_result
        return len(data) if self.bulk_result is None else self.bulk_result

    def read(self, endpoint, length, timeout):
        assert (endpoint, length) == (0x81, 512) and 0 < timeout <= 15000
        self.calls.append(('read', length, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def get_active_configuration(self):
        return [Interface([types.SimpleNamespace(bEndpointAddress=0x02, bmAttributes=2),
                           types.SimpleNamespace(bEndpointAddress=0x83 if self.bad_endpoints else 0x81,
                                                 bmAttributes=2)])]

    def __getattr__(self, name):
        if name in ('set_configuration', 'reset', 'clear_halt', 'detach_kernel_driver'):
            raise AssertionError('forbidden USB operation: ' + name)
        raise AttributeError(name)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        self.originals = {'boot0': b'0' * 65536, 'boot1': b'1' * 65536,
                          'system0': b'2' * 131072, 'system1': b'3' * 131072}
        pins = {part: (len(data), flash.sha256(data)) for part, data in self.originals.items()}
        self.candidate = bytes(range(256)) * 320  # 64 KiB + final 16 KiB.
        self.image = self.root / 'root.squashfs'
        self.image.write_bytes(self.candidate)
        for part, data in self.originals.items():
            (self.backups / f'{part}.img').write_bytes(data)
        self.dll = self.root / 'libusb-1.0.dll'
        self.dll.write_bytes(b'inert mock DLL, never loaded')
        self.addCleanup(patch.stopall)
        patch.object(flash, 'BACKUPS', pins).start()
        patch.object(flash, 'CANDIDATE', (len(self.candidate), flash.sha256(self.candidate))).start()
        self.bank_value = 'boot1'
        self.bank_status = 'validated'
        self.current = self.originals['system1']
        self.readback = self.candidate + b'non-FF-tail!' * 4000
        self.readback = self.readback[:131072].ljust(131072, b't')
        self.read_changes = {}
        self.helpers = []
        self.disposed, self.claimed = [], []
        self.device = Device()
        self.backend_calls = 0
        self.guard_calls = 0
        self.dispose_error = False
        self.enumeration = None
        self.probe_hook = None
        patch.object(flash.bank, 'operate', side_effect=self.fake_probe).start()
        patch.object(flash.reader, 'operate', side_effect=self.fake_read).start()

    def guard(self):
        self.guard_calls += 1
        return evidence()

    def loader(self, dll):
        self.backend_calls += 1
        self.assertEqual(dll, str(self.dll))

        def find(**kwargs):
            self.assertEqual(kwargs, dict(find_all=True, idVendor=0x1b8e,
                                          idProduct=0xc003, backend='fake'))
            return self.enumeration() if self.enumeration else iter([self.device])

        def dispose(device):
            self.disposed.append(device)
            if self.dispose_error:
                raise OSError('mock cleanup failure')

        return (types.SimpleNamespace(find=find),
                types.SimpleNamespace(claim_interface=lambda d, n: self.claimed.append((d, n)),
                                      dispose_resources=dispose), 'fake')

    def fake_probe(self, *, output, guard, **kwargs):
        self.helpers.append(output.name)
        guard()
        self.assertTrue((output.parent / 'plan.json').exists())
        if self.probe_hook:
            self.probe_hook()
        return dict(status=self.bank_status, commands_executed=8,
                    observation={'value': self.bank_value})

    def fake_read(self, action, *, part, length, output, guard, **kwargs):
        self.assertEqual(action, 'read')
        self.helpers.append(output.name)
        guard()
        self.assertEqual(length, flash.BACKUPS[part][0])
        if output.name.startswith('unchanged-'):
            self.assertEqual(kwargs['reference'], self.backups / f'{part}.img')
            self.assertEqual(kwargs['reference_sha256'], flash.BACKUPS[part][1])
        if output.name == 'preflight-system1':
            self.assertFalse(self.device.commands)
            data = self.current
        elif output.name == 'readback-system1':
            data = self.readback
        else:
            data = self.originals[part]
        data = self.read_changes.get(output.name, data)
        output.mkdir()
        (output / f'{part}.img').write_bytes(data)
        return dict(length=len(data), sha256=flash.sha256(data))

    def run_writer(self, name='out', mode='flash', **kwargs):
        params = dict(image=self.image if mode in ('flash', 'diagnostic') else None,
                      backups=self.backups, output=self.root / name,
                      dll=self.dll, system='Windows', guard=self.guard,
                      load_backend=self.loader)
        params.update(kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            return flash.operate(mode, **params)

    def expect_failure(self, name='out', **kwargs):
        with self.assertRaises(flash.Failure):
            self.run_writer(name, **kwargs)
        self.assertFalse((self.root / name / 'manifest.json').exists())
        return json.loads((self.root / name / 'failure.json').read_text())

    def test_checksum_vectors_and_final_chunk(self):
        for data, expected in ((b'', 0), (b'\x01', 1), (b'\x01\x02', 513),
                               (b'\x01\x02\x03', 0x030201),
                               (b'\x01\x02\x03\x04', 0x04030201),
                               (b'\xff' * 8, 0xfffffffe),
                               (b'\xff' * 16384, (-4096) & 0xffffffff)):
            with self.subTest(length=len(data)):
                self.assertEqual(flash.addsum32(data), expected)
        actual = flash.addsum32(self.candidate[-16384:])
        expected = sum(int.from_bytes(self.candidate[i:i+4], 'little')
                       for i in range(len(self.candidate)-16384, len(self.candidate), 4)) & 0xffffffff
        self.assertEqual(actual, expected)

    def test_success_wire_and_unchanged_guards(self):
        result = self.run_writer()
        self.assertEqual(self.device.commands, [
            b'download store system1 normal 0x14000'.ljust(64, b'\0'),
            b'download get_status'.ljust(64, b'\0')])
        self.assertEqual([len(c) for c in self.device.chunks], [65536, 16384])
        self.assertEqual(b''.join(self.device.chunks), self.candidate)
        for index, (chunk, header) in enumerate(zip(self.device.chunks, self.device.headers)):
            self.assertEqual(struct.unpack('<IIIIHH', header[:20]),
                             (0, len(chunk), index*16, flash.addsum32(chunk), 0xef, 512))
            self.assertEqual(header[20:], bytes(12))
        self.assertEqual(self.helpers, ['preflight-bank', 'preflight-system1',
                                       'readback-system1', 'unchanged-boot0',
                                       'unchanged-boot1', 'unchanged-system0', 'postflight-bank'])
        self.assertEqual(result['bytes_sent'], len(self.candidate))
        self.assertEqual(result['bytes_acked'], len(self.candidate))
        self.assertTrue(result['may_have_modified_flash'])
        self.assertFalse(result['tail']['all_ff'])
        self.assertEqual(set(result['unchanged_checks']), {'boot0', 'boot1', 'system0'})
        self.assertEqual(self.disposed, [self.device])
        self.assertEqual(self.claimed, [(self.device, 0)])
        manifest = json.loads((self.root / 'out' / 'manifest.json').read_text())
        self.assertEqual(manifest, result)
        events = [json.loads(line) for line in (self.root / 'out' / 'events.jsonl').read_text().splitlines()]
        done = [e for e in events if e['event'] == 'chunk-complete']
        self.assertEqual([e['bytes_acked'] for e in done], [65536, 81920])
        self.assertEqual(done[0]['last_response_hex'], OK.hex())

    def test_diagnostic_accepts_only_pinned_prior_full_readback(self):
        with patch.object(flash, 'DIAGNOSTIC', (len(self.candidate), flash.sha256(self.candidate))), \
             patch.object(flash, 'DIAGNOSTIC_CURRENT', flash.sha256(self.current)):
            self.run_writer(mode='diagnostic')
            self.assertEqual(len(self.device.chunks), 2)

    def test_diagnostic_rejects_unknown_prior_image_before_write(self):
        with patch.object(flash, 'DIAGNOSTIC', (len(self.candidate), flash.sha256(self.candidate))):
            failure = self.expect_failure(mode='diagnostic')
        self.assertEqual(failure['stage'], 'preflight-system1')
        self.assertFalse(failure['write_started'])
        self.assertFalse(self.device.commands)

    def test_diagnostic_rejects_wrong_payload_pin_before_usb(self):
        failure = self.expect_failure(mode='diagnostic')
        self.assertEqual(failure['stage'], 'validate-image')
        self.assertEqual(self.helpers, [])
        self.assertFalse(failure['write_started'])

    def test_diagnostic_exact_candidate_is_idempotent(self):
        self.current = self.readback
        with patch.object(flash, 'DIAGNOSTIC', (len(self.candidate), flash.sha256(self.candidate))):
            result = self.run_writer(mode='diagnostic')
        self.assertTrue(result['already_matching_candidate'])
        self.assertFalse(result['write_started'])
        self.assertFalse(self.device.commands)

    def test_busy_initial_chunk_and_final_status_only_poll(self):
        self.device = Device([BUSY34, SUCCESS, BUSY32, OK, OK, BUSY34, SUCCESS])
        original = flash._transfer
        with patch.object(flash, '_transfer', side_effect=lambda *a, **k: original(*a, **k, pause=lambda _: None)):
            self.run_writer()
        self.assertEqual(len(self.device.headers), 2)
        self.assertEqual(len(self.device.chunks), 2)
        self.assertEqual(len(self.device.commands), 2)

    def test_transport_errors_never_resend_or_finalize(self):
        cases = [
            ('short-command', Device(short_command=1), 0, 0, 0),
            ('short-header', Device(short_header=1), 1, 0, 0),
            ('short-bulk', Device(bulk_result=13), 1, 1, 13),
            ('bulk-timeout', Device(bulk_result=TimeoutError('bulk timeout')), 1, 1, 0),
            ('short-ack', Device([SUCCESS, b'OK!!']), 1, 1, 65536),
            ('media-error', Device([SUCCESS, b'media error 4'.ljust(512, b'\0')]), 1, 1, 65536),
            ('ack-timeout', Device([SUCCESS, TimeoutError('ACK timeout')]), 1, 1, 65536),
            ('wrong-busy', Device([SUCCESS, BUSY34]), 1, 1, 65536),
            ('short-status', Device([b'success']), 0, 0, 0),
            ('failed-status', Device([b'failed:'.ljust(512, b'\0')]), 0, 0, 0),
        ]
        for name, device, headers, chunks, sent in cases:
            with self.subTest(name=name):
                self.device = device
                failure = self.expect_failure(name)
                self.assertEqual(len(device.headers), headers)
                self.assertEqual(len(device.chunks), chunks)
                self.assertEqual(len(device.commands), 1)
                self.assertEqual(failure['bytes_sent'], sent)
                self.assertEqual(failure['bytes_acked'], 0)
                self.assertTrue(failure['may_have_modified_flash'])
                self.assertNotIn('readback-system1', self.helpers)

    def test_final_status_rejection_prevents_readback(self):
        self.device = Device([SUCCESS, OK, OK, b'failed:'.ljust(512, b'\0')])
        failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'download-get-status')
        self.assertEqual(failure['bytes_acked'], len(self.candidate))
        self.assertNotIn('readback-system1', self.helpers)

    def test_short_final_command_is_failure_after_all_data(self):
        self.device = Device(short_command=2)
        failure = self.expect_failure()
        self.assertEqual(failure['bytes_acked'], len(self.candidate))
        self.assertEqual(failure['stage'], 'download-get-status')
        self.assertNotIn('readback-system1', self.helpers)

    def test_second_chunk_failure_preserves_offset_and_counts(self):
        self.device = Device([SUCCESS, OK, b'media error'.ljust(512, b'\0')])
        failure = self.expect_failure()
        self.assertEqual(failure['offset'], 65536)
        self.assertEqual(failure['bytes_sent'], 81920)
        self.assertEqual(failure['bytes_acked'], 65536)
        self.assertEqual(bytes.fromhex(failure['last_response_hex'])[:11], b'media error')

    def test_bad_image_and_each_backup_are_rejected_before_helpers(self):
        for name in ('image', *self.originals):
            with self.subTest(name=name):
                path = self.image if name == 'image' else self.backups / f'{name}.img'
                data = path.read_bytes()
                path.write_bytes(b'!' + data[1:])
                failure = self.expect_failure(name)
                self.assertFalse(failure['may_have_modified_flash'])
                self.assertEqual(self.backend_calls, 0)
                self.assertEqual(self.helpers, [])
                path.write_bytes(data)

    def test_alignment_is_required_before_helpers(self):
        self.image.write_bytes(self.candidate[:-1])
        with patch.object(flash, 'CANDIDATE', (81919, flash.sha256(self.candidate[:-1]))):
            self.expect_failure()
        self.assertEqual(self.helpers, [])

    def test_wrong_or_unvalidated_bank_and_preflight_mismatch_do_not_write(self):
        for name, bank_value, status in [('boot0', 'boot0', 'validated'),
                                         ('unknown', 'unknown', 'validated'),
                                         ('unvalidated', 'boot1', 'failed')]:
            self.bank_value, self.bank_status = bank_value, status
            self.expect_failure(name)
        self.bank_value, self.bank_status = 'boot1', 'validated'
        self.current = b'?' * 131072
        failure = self.expect_failure('preflight-mismatch')
        self.assertFalse(failure['may_have_modified_flash'])
        self.assertEqual(self.backend_calls, 0)
        self.assertEqual(self.device.commands, [])

    def test_wrong_guard_identify_and_endpoints_do_not_write(self):
        self.expect_failure('guard', guard=lambda: evidence('Error'))
        self.device = Device(identify=b'bad!')
        self.expect_failure('identify')
        self.device = Device(bad_endpoints=True)
        self.expect_failure('endpoints')
        self.assertEqual(self.device.commands, [])

    def test_readback_and_unchanged_and_final_bank_fail_never_publish(self):
        cases = ['readback-system1', 'unchanged-boot0', 'unchanged-boot1', 'unchanged-system0', 'postflight-bank']
        for name in cases:
            with self.subTest(name=name):
                self.device = Device()
                self.read_changes = {} if name == 'postflight-bank' else {
                    name: b'!' * flash.BACKUPS['system1' if name == 'readback-system1' else name[10:]][0]}
                if name == 'postflight-bank':
                    def hook():
                        if self.helpers[-1] == 'postflight-bank':
                            self.bank_value = 'boot0'
                    self.probe_hook = hook
                self.expect_failure(name)

    def test_already_matching_candidate_skips_writer_and_still_verifies(self):
        self.current = self.readback
        result = self.run_writer()
        self.assertTrue(result['already_matching_candidate'])
        self.assertFalse(result['write_started'])
        self.assertFalse(result['may_have_modified_flash'])
        self.assertEqual(result['bytes_acked'], 0)
        self.assertEqual(self.backend_calls, 0)
        self.assertEqual(len(self.helpers), 7)

    def test_restore_pinned_full_backup_from_arbitrary_current(self):
        self.current = b'?' * 131072
        self.readback = self.originals['system1']
        result = self.run_writer(mode='restore')
        self.assertEqual(b''.join(self.device.chunks), self.originals['system1'])
        self.assertEqual(result['input_hash'], flash.BACKUPS['system1'][1])
        self.assertEqual(result['readback_hash'], flash.BACKUPS['system1'][1])
        self.assertEqual(self.device.commands[0], b'download store system1 normal 0x20000'.ljust(64, b'\0'))
        self.expect_failure('restore-image', mode='restore', image=self.image)
        self.device = Device()
        self.readback = self.originals['system1'][:-1] + b'!'
        self.expect_failure('restore-readback', mode='restore')

    def test_image_snapshot_is_used_after_path_changes(self):
        self.probe_hook = lambda: self.image.write_bytes(b'?' * len(self.candidate))
        self.run_writer()
        self.assertEqual(b''.join(self.device.chunks), self.candidate)

    def test_disposal_failure_prevents_success_or_readback(self):
        self.dispose_error = True
        failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'writer-dispose')
        self.assertEqual(failure['disposal_errors'], ['mock cleanup failure'])
        self.assertNotIn('readback-system1', self.helpers)

    def test_swallowed_reader_disposal_failure_is_detected(self):
        original_read = self.fake_read

        def with_swallowed_dispose(*args, **kwargs):
            result = original_read(*args, **kwargs)
            _, util, _ = kwargs['load_backend'](kwargs['dll'])
            try:
                util.dispose_resources(self.device)
            except OSError:
                pass  # Emulate existing reader's finally.
            return result

        self.dispose_error = True
        with patch.object(flash.reader, 'operate', side_effect=with_swallowed_dispose):
            failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'preflight-system1')
        self.assertEqual(failure['disposal_errors'], ['mock cleanup failure'])
        self.assertFalse(failure['may_have_modified_flash'])
        self.assertFalse(self.device.commands)

    def test_helper_failure_raw_reply_and_offset_are_retained(self):
        raw = b'bad read packet'

        def failed_read(*args, output, **kwargs):
            output.mkdir()
            (output / 'failure.bin').write_bytes(raw)
            (output / 'failure.json').write_text(json.dumps({
                'stage': 'packet', 'absolute_partition_offset': 512,
                'actual_length': len(raw), 'error': 'short packet'}))
            raise flash.Failure(flash.TRANSFER, 'short packet')

        with patch.object(flash.reader, 'operate', side_effect=failed_read):
            failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'preflight-system1')
        self.assertEqual(failure['helper_failure']['stage'], 'packet')
        self.assertEqual(failure['last_response_hex'], raw.hex())
        self.assertEqual(failure['offset'], 512)
        self.assertFalse(self.device.commands)

    def test_plan_persistence_failure_blocks_all_usb(self):
        write_json = flash.bank._write_json_exclusive

        def fail_plan(path, value):
            if path.name == 'plan.json':
                raise OSError('mock plan persistence failure')
            return write_json(path, value)

        with patch.object(flash.bank, '_write_json_exclusive', side_effect=fail_plan):
            failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'plan')
        self.assertFalse(self.helpers)
        self.assertEqual(self.backend_calls, 0)

    def test_event_persistence_failure_stops_further_data(self):
        event = flash.Journal.event

        def fail_chunk(journal, kind, **fields):
            if kind == 'chunk-complete':
                raise OSError('mock chunk log failure')
            return event(journal, kind, **fields)

        with patch.object(flash.Journal, 'event', new=fail_chunk):
            failure = self.expect_failure()
        self.assertEqual(len(self.device.chunks), 1)
        self.assertEqual(len(self.device.commands), 1)
        self.assertEqual(failure['bytes_acked'], 65536)

    def test_log_close_failure_prevents_manifest_publication(self):
        close = flash.Journal.close

        def broken_close(journal):
            close(journal)
            raise OSError('mock event-log close failure')

        with patch.object(flash.Journal, 'close', new=broken_close):
            failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'publish-success')

    def test_manifest_persistence_failure_preserves_failure_record(self):
        write_json = flash.bank._write_json_exclusive

        def fail_manifest(path, value):
            if path.name == 'manifest.json':
                raise OSError('mock manifest persistence failure')
            return write_json(path, value)

        with patch.object(flash.bank, '_write_json_exclusive', side_effect=fail_manifest):
            failure = self.expect_failure()
        self.assertEqual(failure['stage'], 'publish-success')
        self.assertTrue(failure['may_have_modified_flash'])

    def test_partial_enumeration_and_duplicate_handles_are_all_disposed(self):
        def broken():
            yield self.device
            raise OSError('enumeration interrupted')
        self.enumeration = broken
        self.expect_failure('partial-enumeration')
        self.assertEqual(self.disposed, [self.device])
        other = Device()
        self.enumeration = lambda: iter([self.device, other])
        self.expect_failure('duplicates')
        self.assertEqual(self.disposed, [self.device, self.device, other])
        self.assertFalse(self.device.commands)

    def test_output_must_be_new_and_have_no_symlink_ancestors(self):
        out = self.root / 'exists'
        out.mkdir()
        (out / 'keep').write_bytes(b'keep')
        with self.assertRaises(flash.Failure):
            self.run_writer('exists')
        self.assertEqual((out / 'keep').read_bytes(), b'keep')
        link = self.root / 'link'
        try:
            link.symlink_to(self.backups, target_is_directory=True)
        except OSError:
            return  # Windows without symlink privilege: exclusive-path check above still runs.
        with self.assertRaises(flash.Failure):
            self.run_writer(output=link / 'new')
        self.assertFalse((self.backups / 'new').exists())
        self.assertFalse(self.helpers)

    def test_native_windows_and_dll_are_mandatory(self):
        self.expect_failure('linux', system='Linux')
        self.expect_failure('no-dll', dll=None)
        self.expect_failure('no-image', image=None)
        self.assertEqual(self.helpers, [])

    def test_deadline_is_absolute_even_after_busy(self):
        journal = flash.Journal(self.root, 'flash')
        self.addCleanup(journal.close)
        device = Device([BUSY32, OK])
        ticks = iter([0, 0, 0, 121])
        with self.assertRaises(flash.Failure):
            flash._reply(device, journal, media=True, deadline=120,
                         now=lambda: next(ticks), pause=lambda _: None)
        self.assertEqual(len(device.replies), 1)

    def test_busy_poll_count_is_bounded_without_resending(self):
        journal = flash.Journal(self.root, 'flash')
        self.addCleanup(journal.close)
        device = Device([BUSY32] * 240)
        with self.assertRaises(flash.Failure):
            flash._reply(device, journal, media=True, deadline=120,
                         now=lambda: 0, pause=lambda _: None)
        self.assertEqual(len(device.replies), 0)
        self.assertFalse(device.chunks)
        self.assertFalse(device.headers)

    def test_import_is_inert_and_pins_are_historical_reference_values(self):
        script = """import importlib.abc, sys
class NoUSB(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'usb' or fullname.startswith('usb.'):
            raise AssertionError('USB imported')
sys.meta_path.insert(0, NoUSB())
from tools import lx06_usb_flash_system1 as f
assert not any(n == 'usb' or n.startswith('usb.') for n in sys.modules)
assert f.CANDIDATE == (38158336, '116fc84bcdb8880df9b88362db09d33bec3723dcca3b4b9fc41cae544c736672')
assert f.BACKUPS['system1'] == (41943040, 'b5c76c19c5b101091142baff84d2492ee8eda4e514b6b4f88a9d86655d1a97df')
"""
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
