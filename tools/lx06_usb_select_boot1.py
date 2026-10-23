"""PUBLIC CLI DISABLED. See docs/flashing-research.md.

One-way LX06 live boot0 to boot1 selection for a later manual cold boot.

The only mutating USB commands are fixed setenv and saveenv strings. Import is inert.
"""
import argparse
import json
import os
import platform

try:
    from tools import lx06_usb_bank_probe as bank
except ModuleNotFoundError:  # Direct execution from the tools directory.
    import lx06_usb_bank_probe as bank


Failure = bank.Failure
INPUT, GUARD, IDENTIFY, NO_DEVICE, TRANSFER = (
    bank.INPUT, bank.GUARD, bank.IDENTIFY, bank.NO_DEVICE, bank.TRANSFER)
COMMANDS = ('setenv boot_part boot1', 'saveenv')
SUCCESS = b'success' + bytes(505)


def _event(stream, record):
    payload = (json.dumps(record, sort_keys=True) + '\n').encode('utf-8')
    try:
        if stream.write(payload) != len(payload):
            raise OSError('short event write')
        stream.flush()
        os.fsync(stream.fileno())
    except (Exception, KeyboardInterrupt) as exc:
        raise Failure(INPUT, f'event-log persistence failed: {exc}') from exc


def _probe(output, expected, *, dll, guard, load_backend, probe):
    result = probe(output=output, dll=dll, system='Windows', guard=guard,
                   load_backend=load_backend)
    if (result.get('status') != 'validated' or
            result.get('commands_executed') != len(bank.COMMANDS) or
            result.get('observation', {}).get('name') != 'boot_part' or
            result['observation'].get('value') != expected):
        raise Failure(TRANSFER, f'fresh calibrated probe did not establish {expected}')
    return result


def _command(command, *, dll, guard, load_backend, stream, state):
    """Open one handle, send one fixed command, dispose before returning."""
    if command not in COMMANDS:
        raise Failure(INPUT, 'command is not in the fixed selector sequence')
    devices = []
    util = None
    evidence = None
    identified = b''
    stage = 'driver-guard'
    intent = False
    error = None
    event = {'kind': 'command-result', 'command': command,
             'started_utc': bank._utcnow(), 'out_length': None,
             'response_hex': '', 'status': None, 'exception': None,
             'cleanup_errors': []}
    try:
        evidence = guard()
        bank._windows_evidence(evidence)
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
        identified = bytes(device.ctrl_transfer(0xc0, 0x20, 0, 0, 4,
                                                timeout=bank.TIMEOUT))
        if identified != bytes.fromhex('00070010'):
            raise Failure(IDENTIFY, 'unexpected identify response')
        stage = 'claim'
        device.get_active_configuration()
        util.claim_interface(device, 0)
        payload = bank._payload(command)
        # This durable intent is the final prerequisite for the USB mutation.
        stage = 'intent'
        _event(stream, {'kind': 'command-intent', 'command': command,
                        'payload_hex': payload.hex(), 'timestamp_utc': bank._utcnow(),
                        'identify_hex': identified.hex(), 'winusb_guard': evidence})
        intent = True
        if command == COMMANDS[0]:
            state['setenv_may_have_changed_ram'] = True
        else:
            state['saveenv_may_have_persisted_environment'] = True
        stage = 'command-out'
        event['out_length'] = device.ctrl_transfer(0x40, 0x34, 0, 2, payload,
                                                  timeout=bank.TIMEOUT)
        if event['out_length'] != 64:
            raise Failure(TRANSFER, 'short command OUT transfer')
        stage = 'command-status'
        raw = bytes(device.read(0x81, 512, timeout=bank.TIMEOUT))
        event['response_hex'] = raw.hex()
        if raw != SUCCESS:
            raise Failure(TRANSFER, 'command status is not exact 512-byte NUL-padded success')
        event['status'] = 'success'
    except (Exception, KeyboardInterrupt) as exc:
        event['exception'] = str(exc) or type(exc).__name__
        if isinstance(exc, Failure):
            error = exc
        else:
            code = IDENTIFY if stage == 'identify' else TRANSFER if stage in (
                'claim', 'command-out', 'command-status') else INPUT
            error = Failure(code, event['exception'])
    finally:
        if util is not None:
            for device in devices:
                try:
                    util.dispose_resources(device)
                except (Exception, KeyboardInterrupt) as exc:
                    event['cleanup_errors'].append(str(exc) or type(exc).__name__)
        event['completed_utc'] = bank._utcnow()
        event['intent_persisted'] = intent
        event['identify_hex'] = identified.hex()
        event['winusb_guard'] = evidence
        _event(stream, event)
    if event['cleanup_errors']:
        raise Failure(TRANSFER, 'USB resource disposal failed: ' +
                      '; '.join(event['cleanup_errors']))
    if error is not None:
        raise error
    return event


def operate(*, output, dll=None, system=None, guard=bank.reader.windows_guard,
            load_backend=bank.reader.backend, probe=bank.operate):
    """Select boot1 once; injected dependencies allow hardware-free mock tests."""
    out = bank._reserve_output(output)
    state = {'setenv_may_have_changed_ram': False,
             'saveenv_may_have_persisted_environment': False}
    stage = 'event-log'
    stream = None
    primary = None
    close_error = None
    results = []
    try:
        stream = open(out / 'events.jsonl', 'xb')
        stage = 'host'
        if (system or platform.system()) != 'Windows':
            raise Failure(GUARD, 'native Windows required')
        stage = 'preflight-bank'
        results.append(_probe(out / 'preflight-bank', 'boot0', dll=dll,
                              guard=guard, load_backend=load_backend, probe=probe))
        stage = 'setenv'
        _command(COMMANDS[0], dll=dll, guard=guard, load_backend=load_backend,
                 stream=stream, state=state)
        stage = 'after-setenv-bank'
        results.append(_probe(out / 'after-setenv-bank', 'boot1', dll=dll,
                              guard=guard, load_backend=load_backend, probe=probe))
        stage = 'saveenv'
        _command(COMMANDS[1], dll=dll, guard=guard, load_backend=load_backend,
                 stream=stream, state=state)
        stage = 'after-saveenv-bank'
        results.append(_probe(out / 'after-saveenv-bank', 'boot1', dll=dll,
                              guard=guard, load_backend=load_backend, probe=probe))
    except (Exception, KeyboardInterrupt) as exc:
        primary = exc if isinstance(exc, Failure) else Failure(
            INPUT, str(exc) or type(exc).__name__)
    if stream is not None:
        try:
            stream.close()
        except (Exception, KeyboardInterrupt) as exc:
            close_error = str(exc) or type(exc).__name__
    if close_error and primary is None:
        stage = 'event-log-close'
        primary = Failure(INPUT, f'event-log close failed: {close_error}')
    if primary is not None:
        failure = {'status': 'failed', 'stage': stage, 'error': str(primary),
                   'exit_code': primary.code, 'timestamp_utc': bank._utcnow(),
                   'completed_probes': len(results), 'event_log': 'events.jsonl',
                   'event_log_close_error': close_error, **state,
                   'scope': 'saveenv may save all existing RAM environment changes; '
                            'no independent stored-environment readback or boot verification'}
        try:
            bank._write_json_exclusive(out / 'failure.json', failure)
        except Exception:
            pass
        raise primary
    manifest = {
        'status': 'validated', 'timestamp_utc': bank._utcnow(),
        'commands': list(COMMANDS), 'event_log': 'events.jsonl',
        'probes': ['preflight-bank', 'after-setenv-bank', 'after-saveenv-bank'],
        'observation': {'name': 'boot_part', 'value': 'boot1',
                        'scope': 'live environment value after saveenv acknowledgment; '
                                 'not a boot result or independent persistence readback'},
        'saveenv_acknowledged': True,
        'scope': 'saveenv saves the whole environment, including other existing RAM changes',
    }
    try:
        bank._write_json_exclusive(out / 'manifest.json', manifest)
    except (Exception, KeyboardInterrupt) as exc:
        failure = {'status': 'failed', 'stage': 'manifest', 'error': str(exc),
                   'exit_code': INPUT, 'timestamp_utc': bank._utcnow(), **state}
        try:
            bank._write_json_exclusive(out / 'failure.json', failure)
        except Exception:
            pass
        raise Failure(INPUT, f'manifest publication failed: {exc}') from exc
    return manifest


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(INPUT, json.dumps({'error': message, 'exit_code': INPUT}) + '\n')


def main():
    # Historical single-device operations remain importable for protocol tests.
    # Never expose the old image pins as a public flashing workflow.
    print(json.dumps({'error': 'Public CLI disabled: historical single-device reference only. '
                      'See docs/flashing-research.md; do not flash the old CANDIDATE '
                      'or private DIAGNOSTIC images.', 'exit_code': INPUT}))
    return INPUT


if __name__ == '__main__':
    raise SystemExit(main())
