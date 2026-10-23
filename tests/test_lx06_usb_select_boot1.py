import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

from tools import lx06_usb_bank_probe as bank
from tools import lx06_usb_select_boot1 as select
from tests.test_lx06_usb_bank_probe import Device, SUCCESS, FAILED


BOOT0 = [SUCCESS, FAILED, SUCCESS, FAILED,
         SUCCESS, FAILED, SUCCESS, FAILED]
BOOT1 = [SUCCESS, FAILED, FAILED, SUCCESS,
         FAILED, SUCCESS, SUCCESS, FAILED]
GUARD = {'checked': True, 'query': 'Get-PnpDevice -PresentOnly',
         'devices': [{'InstanceId': 'USB\\VID_1B8E&PID_C003\\mock',
                      'Service': 'WinUSB', 'Status': 'OK'}]}


class CommandDevice(Device):
    def __init__(self, reply=SUCCESS, *, short=False, out_error=None,
                 root=None, name=None):
        super().__init__([reply])
        self.short = short
        self.out_error = out_error
        self.root = root
        self.name = name

    def ctrl_transfer(self, *args, timeout):
        if args[1] == 0x34:
            events = [json.loads(line) for line in
                      (self.root/'events.jsonl').read_text().splitlines()]
            self.assert_intent(events, args[4])
            if self.out_error:
                raise self.out_error
            if self.short:
                self.short_out_at = 1
        return super().ctrl_transfer(*args, timeout=timeout)

    def assert_intent(self, events, payload):
        assert events[-1]['kind'] == 'command-intent'
        assert events[-1]['command'] == self.name
        assert bytes.fromhex(events[-1]['payload_hex']) == payload


class SelectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = []
        self.disposed = []
        self.calls = []
        self.guard_calls = 0
        self.dispose_error_at = None

    def probe_device(self, replies):
        return Device(replies)

    def command_device(self, name, **kwargs):
        return CommandDevice(root=self.root/'out', name=name, **kwargs)

    def guard(self):
        self.guard_calls += 1
        return GUARD

    def loader(self, dll):
        self.assertEqual(dll, 'mock-libusb-1.0.dll')
        device = self.queue.pop(0)
        self.calls.append(device)
        def find(**kwargs):
            self.assertEqual(kwargs, dict(find_all=True, idVendor=0x1b8e,
                                          idProduct=0xc003, backend='mock'))
            return iter([device])
        def claim(candidate, interface):
            self.assertIs(candidate, device)
            self.assertEqual(interface, 0)
            device.events.append('claim')
        def dispose(candidate):
            self.assertIs(candidate, device)
            self.disposed.append(candidate)
            if self.dispose_error_at == len(self.disposed):
                raise OSError('mock cleanup failed')
        return (types.SimpleNamespace(find=find),
                types.SimpleNamespace(claim_interface=claim,
                                      dispose_resources=dispose), 'mock')

    def run_selector(self):
        return select.operate(output=self.root/'out', dll='mock-libusb-1.0.dll',
                              system='Windows', guard=self.guard,
                              load_backend=self.loader)

    def failure(self):
        with self.assertRaises(select.Failure):
            self.run_selector()
        out = self.root/'out'
        self.assertFalse((out/'manifest.json').exists())
        return json.loads((out/'failure.json').read_text())

    def test_exact_sequence_and_success_scope(self):
        pre = self.probe_device(BOOT0)
        setenv = self.command_device(select.COMMANDS[0])
        middle = self.probe_device(BOOT1)
        saveenv = self.command_device(select.COMMANDS[1])
        final = self.probe_device(BOOT1)
        self.queue = [pre, setenv, middle, saveenv, final]
        result = self.run_selector()
        self.assertEqual(self.calls, [pre, setenv, middle, saveenv, final])
        self.assertEqual(self.disposed, self.calls)
        self.assertEqual(setenv.commands, [bank._payload(select.COMMANDS[0])])
        self.assertEqual(saveenv.commands, [bank._payload(select.COMMANDS[1])])
        self.assertEqual(self.guard_calls, 5)
        self.assertEqual(result['observation']['value'], 'boot1')
        self.assertIn('not a boot result', result['observation']['scope'])
        self.assertIn('whole environment', result['scope'])
        self.assertEqual(result, json.loads((self.root/'out/manifest.json').read_text()))
        events = [json.loads(line) for line in
                  (self.root/'out/events.jsonl').read_text().splitlines()]
        self.assertEqual([event['kind'] for event in events],
                         ['command-intent', 'command-result'] * 2)
        for event in (events[1], events[3]):
            self.assertEqual(event['out_length'], 64)
            self.assertEqual(event['response_hex'], SUCCESS.hex())
            self.assertEqual(event['status'], 'success')
            self.assertEqual(event['cleanup_errors'], [])
        self.assertFalse(any('reset' in cmd.decode('ascii') for dev in self.calls
                             for cmd in dev.commands))

    def test_preflight_must_be_boot0(self):
        self.queue = [self.probe_device(BOOT1)]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'preflight-bank')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(failure['setenv_may_have_changed_ram'])

    def test_bad_windows_guard_or_identify_prevents_mutation(self):
        bad = dict(GUARD, devices=[dict(GUARD['devices'][0], Status='Error')])
        with self.assertRaises(select.Failure) as caught:
            select.operate(output=self.root/'bad-guard', dll='mock',
                           system='Windows', guard=lambda: bad,
                           load_backend=lambda dll: self.fail('backend called'))
        self.assertEqual(caught.exception.code, select.GUARD)
        self.assertEqual(self.calls, [])
        wrong = Device([], identify=b'bad!')
        self.queue = [wrong]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'preflight-bank')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(failure['setenv_may_have_changed_ram'])

    def test_setenv_short_out_or_bad_status_stops(self):
        for name, kwargs in [('short', {'short': True}),
                             ('failed', {'reply': FAILED}),
                             ('malformed', {'reply': b'success'}),
                             ('timeout', {'out_error': TimeoutError('uncertain OUT')})]:
            with self.subTest(name=name):
                self.setUp()
                cmd = self.command_device(select.COMMANDS[0], **kwargs)
                self.queue = [self.probe_device(BOOT0), cmd]
                failure = self.failure()
                self.assertEqual(failure['stage'], 'setenv')
                self.assertTrue(failure['setenv_may_have_changed_ram'])
                self.assertFalse(failure['saveenv_may_have_persisted_environment'])
                self.assertEqual(len(self.calls), 2)
                events = [json.loads(line) for line in
                          (self.root/'out/events.jsonl').read_text().splitlines()]
                self.assertEqual(events[-1]['response_hex'],
                                 '' if name in ('short', 'timeout') else kwargs['reply'].hex())
                self.assertIsNotNone(events[-1]['exception'])

    def test_failed_intermediate_probe_prevents_saveenv(self):
        self.queue = [self.probe_device(BOOT0),
                      self.command_device(select.COMMANDS[0]),
                      self.probe_device(BOOT0)]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'after-setenv-bank')
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(failure['saveenv_may_have_persisted_environment'])

    def test_saveenv_ambiguity_has_no_further_attempts(self):
        save = self.command_device(select.COMMANDS[1], reply=FAILED)
        self.queue = [self.probe_device(BOOT0),
                      self.command_device(select.COMMANDS[0]),
                      self.probe_device(BOOT1), save]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'saveenv')
        self.assertTrue(failure['saveenv_may_have_persisted_environment'])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(save.commands), 1)

    def test_cleanup_failure_stops_before_next_step(self):
        self.dispose_error_at = 2
        self.queue = [self.probe_device(BOOT0),
                      self.command_device(select.COMMANDS[0])]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'setenv')
        self.assertEqual(len(self.calls), 2)
        events = [json.loads(line) for line in
                  (self.root/'out/events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['cleanup_errors'], ['mock cleanup failed'])

    def test_final_probe_failure_cannot_claim_success(self):
        self.queue = [self.probe_device(BOOT0),
                      self.command_device(select.COMMANDS[0]),
                      self.probe_device(BOOT1),
                      self.command_device(select.COMMANDS[1]),
                      self.probe_device(BOOT0)]
        failure = self.failure()
        self.assertEqual(failure['stage'], 'after-saveenv-bank')
        self.assertTrue(failure['saveenv_may_have_persisted_environment'])
        self.assertEqual(len(self.calls), 5)

    def test_existing_output_and_nonwindows_host_stop_before_usb(self):
        out = self.root/'out'
        out.mkdir()
        (out/'keep').write_text('keep')
        with self.assertRaises(select.Failure):
            self.run_selector()
        self.assertEqual((out/'keep').read_text(), 'keep')
        self.assertEqual(self.calls, [])
        with self.assertRaises(select.Failure) as caught:
            select.operate(output=self.root/'linux', dll='mock', system='Linux',
                           guard=self.guard, load_backend=self.loader)
        self.assertEqual(caught.exception.code, select.GUARD)
        self.assertEqual(self.calls, [])

    def test_import_does_not_load_usb(self):
        code = ("import sys; from tools import lx06_usb_select_boot1; "
                "assert not any(x == 'usb' or x.startswith('usb.') for x in sys.modules)")
        subprocess.run([sys.executable, '-B', '-c', code], check=True,
                       cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    unittest.main()
