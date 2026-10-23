import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

from tools import lx06_usb_bank_probe as probe


SUCCESS = b'success'.ljust(512, b'\0')
FAILED = b'failed:'.ljust(512, b'\0')


class Device:
    def __init__(self, replies=(), *, identify=bytes.fromhex('00070010'),
                 short_out_at=None):
        self.replies = list(replies)
        self.identify = identify
        self.short_out_at = short_out_at
        self.events = []
        self.commands = []
        self.reads = 0

    def ctrl_transfer(self, *args, timeout):
        assert timeout == 5000
        if args[1] == 0x20:
            self.events.append('identify')
            assert args == (0xc0, 0x20, 0, 0, 4)
            return self.identify
        assert args[:4] == (0x40, 0x34, 0, 2)
        assert len(args[4]) == 64 and args[4].endswith(b'\0')
        self.events.append('command')
        self.commands.append(args[4])
        return 63 if len(self.commands) == self.short_out_at else 64

    def read(self, endpoint, length, timeout):
        assert (endpoint, length, timeout) == (0x81, 512, 5000)
        self.events.append('read')
        reply = self.replies[self.reads]
        self.reads += 1
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def get_active_configuration(self):
        self.events.append('configuration')

    def __getattr__(self, name):
        if name in ('write', 'reset', 'clear_halt', 'set_configuration',
                    'detach_kernel_driver'):
            raise AssertionError('forbidden operation ' + name)
        raise AttributeError(name)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.disposed = []
        self.backend_calls = 0

    def loader_for(self, found, *, dispose=None):
        def load(dll):
            self.backend_calls += 1
            self.assertIsNone(dll)
            def find(**kwargs):
                self.assertEqual(kwargs, dict(find_all=True, idVendor=0x1b8e,
                                              idProduct=0xc003, backend='libusb1'))
                return found() if callable(found) else iter(found)
            def claim(device, interface):
                self.assertEqual(interface, 0)
                device.events.append('claim')
            util = types.SimpleNamespace(
                claim_interface=claim,
                dispose_resources=dispose or self.disposed.append)
            return types.SimpleNamespace(find=find), util, 'libusb1'
        return load

    def run_probe(self, device, name='out', **kwargs):
        return probe.operate(output=self.root/name, system='Linux',
                             load_backend=self.loader_for([device]), **kwargs)

    @staticmethod
    def events(path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_both_bank_values_have_exact_fixed_sequence_and_evidence(self):
        cases = {
            'boot0': [SUCCESS, FAILED, SUCCESS, FAILED,
                      SUCCESS, FAILED, SUCCESS, FAILED],
            'boot1': [SUCCESS, FAILED, FAILED, SUCCESS,
                      FAILED, SUCCESS, SUCCESS, FAILED],
        }
        for bank, replies in cases.items():
            with self.subTest(bank=bank):
                device = Device(replies)
                result = self.run_probe(device, bank)
                self.assertEqual(result['observation']['value'], bank)
                self.assertIn('not proof of active Linux root',
                              result['observation']['scope'])
                self.assertEqual(device.events[:3],
                                 ['identify', 'configuration', 'claim'])
                expected = [command.encode('ascii').ljust(64, b'\0')
                            for _, command in probe.COMMANDS]
                self.assertEqual(device.commands, expected)
                logged = self.events(self.root/bank/'events.jsonl')
                self.assertEqual(len(logged), 8)
                self.assertEqual([event['response_hex'] for event in logged],
                                 [reply.hex() for reply in replies])
                self.assertTrue(all(event['started_utc'].endswith('+00:00') and
                                    event['completed_utc'].endswith('+00:00')
                                    for event in logged))
                manifest = json.loads((self.root/bank/'manifest.json').read_text())
                self.assertEqual(manifest, result)
                self.assertIn(device, self.disposed)

    def assert_probe_failure(self, replies, expected_commands, name, **device_args):
        device = Device(replies, **device_args)
        with self.assertRaises(probe.Failure) as caught:
            self.run_probe(device, name)
        self.assertEqual(caught.exception.code, probe.TRANSFER)
        out = self.root/name
        self.assertFalse((out/'manifest.json').exists())
        self.assertEqual(len(device.commands), expected_commands)
        self.assertIn(device, self.disposed)
        return device, json.loads((out/'failure.json').read_text()), self.events(out/'events.jsonl')

    def test_missing_or_unexpected_value_is_unknown(self):
        _, failure, _ = self.assert_probe_failure(
            [SUCCESS, FAILED, FAILED, FAILED], 4, 'unknown')
        self.assertEqual(failure['observation']['value'], 'unknown')
        self.assertIn('not one-hot', failure['error'])

    def test_changing_bank_and_both_true_stop_immediately(self):
        _, changing, _ = self.assert_probe_failure(
            [SUCCESS, FAILED, SUCCESS, FAILED, FAILED], 5, 'changing')
        self.assertEqual(changing['observation']['value'], 'unknown')
        _, both, _ = self.assert_probe_failure(
            [SUCCESS, FAILED, SUCCESS, SUCCESS], 4, 'both-true')
        self.assertEqual(both['commands_completed'], 4)

    def test_unsupported_test_and_always_success_fail_calibration(self):
        for name, replies, count in (('unsupported', [FAILED], 1),
                                     ('always-success', [SUCCESS, SUCCESS], 2)):
            with self.subTest(name=name):
                _, failure, _ = self.assert_probe_failure(replies, count, name)
                self.assertIn('calibration', failure['error'])

    def test_final_calibration_must_match(self):
        replies = [SUCCESS, FAILED, SUCCESS, FAILED,
                   SUCCESS, FAILED, SUCCESS, SUCCESS]
        _, failure, _ = self.assert_probe_failure(replies, 8, 'final-calibration')
        self.assertIn('final predicate calibration', failure['error'])

    def test_partial_and_malformed_status_retain_exact_raw_bytes(self):
        malformed = b'success' + bytes(504) + b'x'
        for name, reply in (('partial', b'success'), ('malformed', malformed)):
            with self.subTest(name=name):
                _, failure, events = self.assert_probe_failure([reply], 1, name)
                self.assertEqual(failure['last_response_hex'], reply.hex())
                self.assertEqual(events[0]['response_hex'], reply.hex())
                self.assertIsNotNone(events[0]['error'])

    def test_timeout_midcommand_and_short_out_issue_no_extra_usb(self):
        device, failure, events = self.assert_probe_failure(
            [SUCCESS, FAILED, TimeoutError('mock timeout')], 3, 'timeout')
        self.assertEqual(device.reads, 3)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]['response_hex'], '')
        self.assertIn('mock timeout', failure['error'])

        device, _, events = self.assert_probe_failure(
            [SUCCESS, FAILED], 3, 'short-out', short_out_at=3)
        self.assertEqual(device.reads, 2)
        self.assertEqual(events[-1]['out_length'], 63)
        self.assertEqual(events[-1]['response_hex'], '')

    def test_identify_enumeration_and_duplicate_fail_closed(self):
        wrong = Device([], identify=b'bad!')
        with self.assertRaises(probe.Failure) as caught:
            self.run_probe(wrong, 'wrong-identify')
        self.assertEqual(caught.exception.code, probe.IDENTIFY)
        self.assertEqual(wrong.events, ['identify'])
        self.assertIn(wrong, self.disposed)

        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=self.root/'none', system='Linux',
                          load_backend=self.loader_for([]))
        self.assertEqual(caught.exception.code, probe.NO_DEVICE)

        first, second = Device([]), Device([])
        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=self.root/'duplicate', system='Linux',
                          load_backend=self.loader_for([first, second]))
        self.assertEqual(caught.exception.code, probe.INPUT)
        self.assertEqual(first.events + second.events, [])
        self.assertIn(first, self.disposed)
        self.assertIn(second, self.disposed)

    def test_partial_enumeration_disposes_yielded_handle(self):
        device = Device([])
        def broken():
            yield device
            raise OSError('mock enumeration failure')
        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=self.root/'partial-enumeration', system='Linux',
                          load_backend=self.loader_for(broken))
        self.assertEqual(caught.exception.code, probe.INPUT)
        self.assertIn(device, self.disposed)

    def test_windows_status_must_be_fresh_ok_before_backend(self):
        evidence = {'checked': True, 'query': 'Get-PnpDevice -PresentOnly',
                    'devices': [{'InstanceId': 'USB\\VID_1B8E&PID_C003\\x',
                                 'Service': 'WinUSB', 'Status': 'Error'}]}
        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=self.root/'driver', system='Windows',
                          guard=lambda: evidence,
                          load_backend=lambda dll: self.fail('backend called'))
        self.assertEqual(caught.exception.code, probe.GUARD)
        self.assertEqual(self.backend_calls, 0)

    def test_existing_output_is_untouched_before_guard_or_backend(self):
        out = self.root/'existing'
        out.mkdir()
        marker = out/'keep'
        marker.write_text('unchanged')
        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=out, system='Windows',
                          guard=lambda: self.fail('guard called'),
                          load_backend=lambda dll: self.fail('backend called'))
        self.assertEqual(caught.exception.code, probe.INPUT)
        self.assertEqual(marker.read_text(), 'unchanged')
        self.assertEqual(list(out.iterdir()), [marker])

    def test_disposal_error_is_recorded_and_cannot_claim_success(self):
        replies = [SUCCESS, FAILED, SUCCESS, FAILED,
                   SUCCESS, FAILED, SUCCESS, FAILED]
        device = Device(replies)
        def bad_dispose(candidate):
            self.assertIs(candidate, device)
            raise OSError('mock disposal failure')
        with self.assertRaises(probe.Failure) as caught:
            probe.operate(output=self.root/'dispose', system='Linux',
                          load_backend=self.loader_for([device], dispose=bad_dispose))
        self.assertEqual(caught.exception.code, probe.TRANSFER)
        self.assertFalse((self.root/'dispose/manifest.json').exists())
        failure = json.loads((self.root/'dispose/failure.json').read_text())
        self.assertEqual(failure['stage'], 'dispose')
        self.assertEqual(failure['disposal_errors'], ['mock disposal failure'])

    def test_import_is_usb_inert(self):
        code = ("import sys; from tools import lx06_usb_bank_probe; "
                "assert not any(x == 'usb' or x.startswith('usb.') for x in sys.modules)")
        subprocess.run([sys.executable, '-B', '-c', code], check=True,
                       cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    unittest.main()
