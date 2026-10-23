"""Fail-closed, read-only LX06 USB boot_part predicate probe."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform

try:
    from tools import lx06_usb_read as reader
except ModuleNotFoundError:  # Direct execution from the repository's tools directory.
    import lx06_usb_read as reader


TIMEOUT = reader.TIMEOUT
NO_DEVICE, GUARD, IDENTIFY, TRANSFER, INPUT = (
    reader.NO_DEVICE, reader.GUARD, reader.IDENTIFY, reader.TRANSFER, reader.INPUT)
Failure = reader.Failure

COMMANDS = (
    ('calibration_true_1', 'test boot0 = boot0'),
    ('calibration_false_1', 'test boot0 = boot1'),
    ('boot_part_boot0_1', 'test ${boot_part} = boot0'),
    ('boot_part_boot1_1', 'test ${boot_part} = boot1'),
    ('boot_part_boot0_2', 'test ${boot_part} = boot0'),
    ('boot_part_boot1_2', 'test ${boot_part} = boot1'),
    ('calibration_true_2', 'test boot0 = boot0'),
    ('calibration_false_2', 'test boot0 = boot1'),
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _reserve_output(output):
    if output is None:
        raise Failure(INPUT, 'new output directory required')
    out = Path(output)
    if any(path.is_symlink() for path in (out, *out.absolute().parents)):
        raise Failure(INPUT, 'symlink output path')
    try:
        out.mkdir(exist_ok=False)
    except OSError as exc:
        raise Failure(INPUT, f'cannot reserve new output directory: {exc}') from exc
    return out


def _write_json_exclusive(path, value):
    created = False
    try:
        payload = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode('utf-8')
        with open(path, 'xb') as stream:
            created = True
            if stream.write(payload) != len(payload):
                raise OSError('short JSON write')
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _windows_evidence(evidence):
    try:
        devices = evidence['devices']
        item = devices[0]
        valid = (evidence['checked'] is True and
                 evidence['query'] == 'Get-PnpDevice -PresentOnly' and
                 isinstance(devices, list) and len(devices) == 1 and
                 bool(item['InstanceId']) and item['Status'] == 'OK' and
                 str(item['Service']).lower() == 'winusb')
    except (KeyError, IndexError, TypeError):
        valid = False
    if not valid:
        raise Failure(GUARD, 'fresh Windows guard must report exactly one Status OK WinUSB device')


def _payload(command):
    try:
        encoded = command.encode('ascii')
    except UnicodeEncodeError as exc:  # Constants are checked again at use.
        raise Failure(INPUT, 'non-ASCII fixed command') from exc
    if len(encoded) >= 64:
        raise Failure(INPUT, 'fixed command must be shorter than 64 bytes')
    return encoded.ljust(64, b'\0')


def _status(raw):
    if raw == b'success' + bytes(505):
        return 'success'
    if raw == b'failed:' + bytes(505):
        return 'failed:'
    raise Failure(TRANSFER, 'status packet is not an exact NUL-padded success or failed: token')


def _one_hot(pair):
    if pair == ['success', 'failed:']:
        return 'boot0'
    if pair == ['failed:', 'success']:
        return 'boot1'
    return None


def operate(*, output, dll=None, system=None, guard=reader.windows_guard,
            load_backend=reader.backend):
    """Run the fixed predicate sequence. Inject guard/backend for offline tests."""
    out = _reserve_output(output)
    event_stream = None
    devices = []
    util = None
    evidence = None
    identify = b''
    raw = b''
    statuses = []
    attempted = 0
    observation = None
    disposal_errors = []
    stage = 'event-log'
    primary = None

    try:
        event_stream = open(out / 'events.jsonl', 'xb')
        host_system = system or platform.system()
        stage = 'driver-guard'
        if host_system == 'Windows':
            evidence = guard()
            _windows_evidence(evidence)
        else:
            evidence = {'checked': False,
                        'reason': 'non-Windows; no Windows PnP Service/Status comparison'}

        stage = 'backend'
        core, util, backend = load_backend(dll)
        stage = 'enumeration'
        for device in core.find(find_all=True, idVendor=0x1b8e,
                                idProduct=0xc003, backend=backend):
            devices.append(device)
        if not devices:
            raise Failure(NO_DEVICE, 'no matching USB enumeration')
        if len(devices) != 1:
            raise Failure(INPUT, 'require exactly one USB device')
        device = devices[0]

        stage = 'identify'
        identify = bytes(device.ctrl_transfer(0xc0, 0x20, 0, 0, 4,
                                              timeout=TIMEOUT))
        if identify != bytes.fromhex('00070010'):
            raise Failure(IDENTIFY, 'unexpected identify response')
        stage = 'claim'
        device.get_active_configuration()
        util.claim_interface(device, 0)

        for sequence, (label, command) in enumerate(COMMANDS, 1):
            stage = f'command-{sequence}'
            payload = _payload(command)
            raw = b''
            event = {'sequence': sequence, 'label': label, 'command': command,
                     'command_payload_hex': payload.hex(),
                     'started_utc': _utcnow(), 'response_hex': '',
                     'status': None, 'error': None}
            command_error = None
            try:
                sent = device.ctrl_transfer(0x40, 0x34, 0, 2, payload,
                                            timeout=TIMEOUT)
                event['out_length'] = sent
                if sent != 64:
                    raise Failure(TRANSFER, 'short command transfer')
                raw = bytes(device.read(0x81, 512, timeout=TIMEOUT))
                event['response_hex'] = raw.hex()
                if len(raw) != 512:
                    raise Failure(TRANSFER, 'short status packet')
                event['status'] = _status(raw)
            except (Exception, KeyboardInterrupt) as exc:
                command_error = exc
                event['error'] = str(exc) or type(exc).__name__
            event['completed_utc'] = _utcnow()
            line = (json.dumps(event, sort_keys=True) + '\n').encode('utf-8')
            try:
                if event_stream.write(line) != len(line):
                    raise OSError('short write')
                event_stream.flush()
                os.fsync(event_stream.fileno())
            except OSError as exc:
                stage = 'event-log'
                raise Failure(INPUT, f'event-log persistence failed: {exc}') from exc
            attempted = sequence
            if command_error is not None:
                if isinstance(command_error, Failure):
                    raise command_error
                raise Failure(TRANSFER, str(command_error) or type(command_error).__name__) from command_error
            statuses.append(event['status'])

            if sequence == 1 and statuses[0] != 'success':
                raise Failure(TRANSFER, 'initial predicate calibration failed')
            if sequence == 2 and statuses != ['success', 'failed:']:
                raise Failure(TRANSFER, 'initial predicate calibration failed')
            if sequence == 4:
                observation = _one_hot(statuses[2:4])
                if observation is None:
                    observation = 'unknown'
                    raise Failure(TRANSFER, 'boot_part predicates are not one-hot')
            if sequence == 5 and statuses[4] != statuses[2]:
                observation = 'unknown'
                raise Failure(TRANSFER, 'boot_part predicates changed')
            if sequence == 6:
                repeated = _one_hot(statuses[4:6])
                if repeated is None or repeated != observation:
                    observation = 'unknown'
                    raise Failure(TRANSFER, 'boot_part predicates changed or are not one-hot')
            if sequence == 7 and statuses[6] != statuses[0]:
                raise Failure(TRANSFER, 'final predicate calibration failed')
            if sequence == 8 and statuses[6:8] != statuses[0:2]:
                raise Failure(TRANSFER, 'final predicate calibration failed')
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, Failure):
            primary = exc
        else:
            code = (IDENTIFY if stage == 'identify' else TRANSFER if
                    stage == 'claim' or stage.startswith('command-') else INPUT)
            primary = Failure(code, str(exc) or type(exc).__name__)

    if util is not None:
        for device in devices:
            try:
                util.dispose_resources(device)
            except (Exception, KeyboardInterrupt) as exc:
                disposal_errors.append(str(exc) or type(exc).__name__)
    if event_stream is not None:
        try:
            event_stream.close()
        except (Exception, KeyboardInterrupt) as exc:
            if primary is None:
                primary = Failure(INPUT, f'event-log close failed: {exc}')
    if disposal_errors and primary is None:
        stage = 'dispose'
        primary = Failure(TRANSFER, 'USB resource disposal failed')

    if primary is not None:
        failure = {'status': 'failed', 'stage': stage, 'error': str(primary),
                   'exit_code': primary.code, 'timestamp_utc': _utcnow(),
                   'commands_attempted': attempted,
                   'commands_completed': len(statuses),
                   'observation': {'name': 'boot_part',
                                   'value': observation or 'unknown'},
                   'identify_hex': identify.hex(), 'last_response_hex': raw.hex(),
                   'winusb_guard': evidence, 'disposal_errors': disposal_errors}
        try:
            _write_json_exclusive(out / 'failure.json', failure)
        except Exception:
            pass
        raise primary

    manifest = {
        'status': 'validated', 'vid': '1b8e', 'pid': 'c003',
        'identify': identify.hex(), 'backend': 'libusb1',
        'timestamp_utc': _utcnow(), 'commands_executed': len(statuses),
        'event_log': 'events.jsonl', 'winusb_guard': evidence,
        'observation': {
            'name': 'boot_part', 'value': observation,
            'scope': 'environment value only; not proof of active Linux root or rollback'},
        'provenance': 'calibrated read-only USB test string-equality predicates',
    }
    try:
        _write_json_exclusive(out / 'manifest.json', manifest)
    except Exception as exc:
        failure = {'status': 'failed', 'stage': 'manifest', 'error': str(exc),
                   'exit_code': INPUT, 'timestamp_utc': _utcnow(),
                   'commands_completed': len(statuses), 'identify_hex': identify.hex(),
                   'last_response_hex': raw.hex(), 'winusb_guard': evidence,
                   'disposal_errors': disposal_errors,
                   'observation': {'name': 'boot_part', 'value': observation}}
        try:
            _write_json_exclusive(out / 'failure.json', failure)
        except Exception:
            pass
        raise Failure(INPUT, f'manifest publication failed: {exc}') from exc
    return manifest


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(INPUT, json.dumps({'error': message, 'exit_code': INPUT}) + '\n')


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('--output', required=True, help='new exclusive evidence directory')
    parser.add_argument('--dll', help='explicit libusb-1.0.dll path')
    args = vars(parser.parse_args())
    try:
        print(json.dumps(operate(**args), indent=2))
        return 0
    except Failure as exc:
        print(json.dumps({'error': str(exc), 'exit_code': exc.code}))
        return exc.code


if __name__ == '__main__':
    raise SystemExit(main())
