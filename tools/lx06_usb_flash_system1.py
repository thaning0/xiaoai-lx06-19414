"""PUBLIC CLI DISABLED. See docs/flashing-research.md.

Historical single-device LX06 system1 writer. Import is inert; native Windows only.

No reboot, retry, automatic restore, arbitrary command or partition interface.
The read-only helpers retain their original safety ceilings and wire protocols.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import time

try:
    from tools import lx06_usb_read as reader
    from tools import lx06_usb_bank_probe as bank
except ModuleNotFoundError:
    import lx06_usb_read as reader
    import lx06_usb_bank_probe as bank


Failure = reader.Failure
INPUT, TRANSFER = reader.INPUT, reader.TRANSFER
CHUNK = 65536
ALIGNMENT = 16384
# Known-broken old base; never a public flash target.
CANDIDATE = (38158336, '116fc84bcdb8880df9b88362db09d33bec3723dcca3b4b9fc41cae544c736672')
# Historical private-Wi-Fi v2, not distributed.
DIAGNOSTIC = (38158336, 'a8f50d5f70f42c1cb2fc3b1d45bc1b3560967108bd804cc19b78f9a9ee5126aa')
# Historical pre-write state guard, retained without any bypass.
DIAGNOSTIC_CURRENT = '8bf1d71905600a3a89fa438f76dff64eac7561008c66e1788113f569ccb818f9'
BACKUPS = {
    'boot0': (6291456, '90c5c64779f1adce42c62080e37d342b8f05984a3aa43c89fc6c8fd046bc901c'),
    'boot1': (6291456, '6cc9cc8f72a069fe17bd2a18f30c52f18440d9cc09d6f2b2bb3cc03498eb0fa8'),
    'system0': (41943040, 'fa1f338dcc0dac7b2e0cf84b9ceaf63a7a964889306b31d833c8806975756973'),
    'system1': (41943040, 'b5c76c19c5b101091142baff84d2492ee8eda4e514b6b4f88a9d86655d1a97df'),
}
SCOPE = ('Nominal logical bytes only; no physical NAND/OOB coverage claim. '
         'Named system1 erase can extend beyond payload. ACK is not NAND commit. '
         'No reboot or environment/data command; boot verification remains external.')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def addsum32(data):
    """Unsigned sum of little-endian words, zero-padding only a partial word."""
    padded = data + bytes((-len(data)) % 4)
    return sum(word[0] for word in struct.iter_unpack('<I', padded)) & 0xffffffff


def media_header(chunk, index):
    return struct.pack('<IIIIHH', 0, len(chunk), 16 * index,
                       addsum32(chunk), 0xef, 512).ljust(32, b'\0')


class Journal:
    def __init__(self, out, mode):
        self.out = out
        self.state = dict(mode=mode, target='system1', stage='input-validation',
                          offset=0, bytes_sent=0, bytes_acked=0,
                          bytes_send_attempted=0, chunk_length=0,
                          write_started=False, may_have_modified_flash=False,
                          last_response_hex='', disposal_errors=[])
        self.stream = open(out / 'events.jsonl', 'xb')

    def event(self, kind, **fields):
        value = dict(self.state, event=kind, timestamp_utc=bank._utcnow(), **fields)
        payload = (json.dumps(value, sort_keys=True) + '\n').encode('utf-8')
        if self.stream.write(payload) != len(payload):
            raise OSError('short event-log write')
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def stage(self, name):
        self.state.update(stage=name, last_response_hex='')
        self.event('stage')

    def close(self):
        if not self.stream.closed:
            self.stream.close()


def _pinned(path, pin):
    # Immutable bytes are also the exact snapshot later streamed on USB.
    with open(path, 'rb') as stream:
        data = stream.read(pin[0] + 1)
    if (len(data), sha256(data)) != pin:
        raise Failure(INPUT, f'pinned size/SHA256 mismatch: {path}')
    return data


def _strict_guard(guard):
    def checked():
        evidence = guard()
        bank._windows_evidence(evidence)
        return evidence
    return checked


def _tracked_backend(loader, journal):
    """Surface even reader cleanup errors, which its legacy finally suppresses."""
    def load(dll):
        core, util, backend = loader(dll)

        class Util:
            def __getattr__(self, name):
                return getattr(util, name)

            def dispose_resources(self, device):
                try:
                    util.dispose_resources(device)
                except (Exception, KeyboardInterrupt) as exc:
                    journal.state['disposal_errors'].append(str(exc) or type(exc).__name__)
                    raise

        return core, Util(), backend
    return load


def _clean(journal):
    if journal.state['disposal_errors']:
        raise Failure(TRANSFER, 'USB resource disposal failed')


def _helper_failure(journal, directory):
    """Copy the helper's exact failing stage/offset/raw evidence into our record."""
    try:
        path = directory / 'failure.json'
        if path.is_file():
            detail = json.loads(path.read_text(encoding='utf-8'))
            journal.state['helper_failure'] = detail
            if 'absolute_partition_offset' in detail:
                journal.state['offset'] = detail['absolute_partition_offset']
        raw_path = directory / 'failure.bin'
        if raw_path.is_file():
            journal.state['last_response_hex'] = raw_path.read_bytes().hex()
        elif path.is_file():
            journal.state['last_response_hex'] = journal.state['helper_failure'].get('last_response_hex', '')
    except (Exception, KeyboardInterrupt) as exc:
        # A damaged diagnostic must not replace the original helper failure.
        journal.state['helper_evidence_error'] = str(exc) or type(exc).__name__


def _endpoints(configuration):
    if configuration is None:
        return {'available': False}
    # PyUSB descriptors expose interface number and alternate setting through
    # iteration. Refuse ambiguous alternate settings instead of choosing one.
    interfaces = [i for i in configuration if i.bInterfaceNumber == 0]
    if len(interfaces) != 1:
        raise Failure(TRANSFER, 'require one active interface 0 descriptor')
    endpoints = {e.bEndpointAddress: e.bmAttributes & 3 for e in interfaces[0]}
    if endpoints.get(0x02) != 2 or endpoints.get(0x81) != 2:
        raise Failure(TRANSFER, 'active interface requires bulk OUT 0x02 and IN 0x81')
    return {'available': True, 'alternate_setting': interfaces[0].bAlternateSetting,
            'bulk_out': '02', 'bulk_in': '81'}


def _timeout(deadline, limit_ms, now):
    remaining = deadline - now()
    if remaining <= 0:
        raise Failure(TRANSFER, 'absolute 120-second deadline exceeded')
    return min(limit_ms, max(1, math.ceil(remaining * 1000)))


def _reply(device, journal, *, media, deadline, now, pause):
    for poll in range(240):
        raw = bytes(device.read(0x81, 512,
                                timeout=_timeout(deadline, 15000, now)))
        journal.state['last_response_hex'] = raw.hex()
        journal.event('media-ack' if media else 'command-status', poll=poll)
        if len(raw) != 512:
            raise Failure(TRANSFER, 'short ACK/status: require 512 bytes')
        _timeout(deadline, 15000, now)
        if media and raw.startswith(b'OK!!'):
            return
        if not media and raw.split(b'\0', 1)[0].strip(b' \t\r\n') == b'success':
            return
        busy = b'Continue:32' if media else b'Continue:34'
        if not raw.startswith(busy):
            raise Failure(TRANSFER, 'rejected ACK/status (see raw response)')
        # Busy continuation only reads; it never resends a header or payload.
        pause(min(0.5, max(0, deadline - now())))
    raise Failure(TRANSFER, 'busy poll limit exceeded')


def _command(device, payload, journal, deadline, now):
    journal.state['last_response_hex'] = ''
    sent = device.ctrl_transfer(0x40, 0x34, 0, 2, payload,
                               timeout=_timeout(deadline, 5000, now))
    journal.event('command-out', payload_hex=payload.hex(), out_length=sent)
    if sent != 64:
        raise Failure(TRANSFER, 'short 64-byte command OUT')


def _transfer(data, journal, *, dll, guard, load_backend,
              now=time.monotonic, pause=time.sleep):
    devices, util = [], None
    primary = None
    try:
        journal.stage('writer-driver-guard')
        journal.event('winusb-guard', evidence=guard())
        journal.stage('writer-backend')
        core, util, backend = load_backend(dll)
        journal.stage('writer-enumeration')
        for device in core.find(find_all=True, idVendor=0x1b8e,
                                idProduct=0xc003, backend=backend):
            devices.append(device)
        if len(devices) != 1:
            raise Failure(reader.NO_DEVICE if not devices else INPUT,
                          'require exactly one fresh matching USB device')
        device = devices[0]
        journal.stage('writer-identify')
        raw = bytes(device.ctrl_transfer(0xc0, 0x20, 0, 0, 4, timeout=5000))
        journal.state['last_response_hex'] = raw.hex()
        journal.event('identify')
        if raw != bytes.fromhex('00070010'):
            raise Failure(reader.IDENTIFY, 'unexpected identify response')
        journal.stage('writer-claim')
        configuration = device.get_active_configuration()
        util.claim_interface(device, 0)
        journal.event('endpoints', evidence=_endpoints(configuration))

        journal.stage('download-system1')
        journal.state.update(write_started=True, may_have_modified_flash=True)
        journal.event('write-intent')  # Durable BEFORE even attempting download.
        deadline = now() + 120
        payload = f'download store system1 normal {len(data):#x}'.encode('ascii').ljust(64, b'\0')
        _command(device, payload, journal, deadline, now)
        _reply(device, journal, media=False, deadline=deadline, now=now, pause=pause)

        for index, offset in enumerate(range(0, len(data), CHUNK)):
            chunk = data[offset:offset + CHUNK]
            journal.state.update(offset=offset, chunk_length=len(chunk))
            journal.stage('write-media-header')
            deadline = now() + 120
            header = media_header(chunk, index)
            sent = device.ctrl_transfer(0x40, 0x32, 1, 0xffff, header,
                                       timeout=_timeout(deadline, 5000, now))
            journal.event('media-header-out', payload_hex=header.hex(), out_length=sent)
            if sent != 32:
                raise Failure(TRANSFER, 'short 32-byte WRITE_MEDIA control OUT')
            journal.stage('write-media-payload')
            journal.state['bytes_send_attempted'] += len(chunk)
            journal.event('bulk-out-intent')
            sent = device.write(0x02, chunk, timeout=_timeout(deadline, 10000, now))
            if isinstance(sent, int) and 0 <= sent <= len(chunk):
                journal.state['bytes_sent'] += sent
            journal.event('bulk-out', out_length=sent)
            if sent != len(chunk):
                raise Failure(TRANSFER, 'short bulk OUT')
            journal.stage('write-media-ack')
            _reply(device, journal, media=True, deadline=deadline, now=now, pause=pause)
            journal.state['bytes_acked'] += len(chunk)
            journal.event('chunk-complete')
            if journal.state['bytes_acked'] % (1024 * 1024) == 0 or offset + len(chunk) == len(data):
                print(f"system1: {journal.state['bytes_acked']}/{len(data)} bytes ACKed", flush=True)

        journal.stage('download-get-status')
        deadline = now() + 120
        _command(device, b'download get_status'.ljust(64, b'\0'), journal, deadline, now)
        _reply(device, journal, media=False, deadline=deadline, now=now, pause=pause)
    except (Exception, KeyboardInterrupt) as exc:
        primary = exc
    finally:
        if util is not None:
            for device in devices:
                try:
                    util.dispose_resources(device)
                except (Exception, KeyboardInterrupt) as exc:
                    if primary is None:
                        journal.state['stage'] = 'writer-dispose'
                        primary = Failure(TRANSFER, f'USB disposal failed: {exc}')
    if primary is not None:
        raise primary
    _clean(journal)


def operate(mode, *, image=None, backups, output, dll=None, system=None,
            guard=reader.windows_guard, load_backend=reader.backend):
    """All production guards run here; injectable USB backends are for mocks."""
    out = bank._reserve_output(output)
    journal = None
    try:
        journal = Journal(out, mode)
        if mode not in ('flash', 'diagnostic', 'restore'):
            raise Failure(INPUT, 'mode must be flash, diagnostic or restore')
        if (system or platform.system()) != 'Windows':
            raise Failure(INPUT, 'native Windows required')
        if not dll:
            raise Failure(INPUT, '--dll required on Windows')
        dll_path = Path(dll)
        if (not dll_path.is_file() or dll_path.name.lower() != 'libusb-1.0.dll'
                or 'libusb0' in dll_path.resolve().name.lower()):
            raise Failure(INPUT, 'existing libusb-1.0.dll required')
        snapshots = {}
        input_hashes = {}
        for part, pin in BACKUPS.items():
            journal.stage(f'validate-backup-{part}')
            snapshots[part] = _pinned(Path(backups) / f'{part}.img', pin)
            input_hashes[f'{part}.img'] = {'length': pin[0], 'sha256': pin[1]}
        journal.stage('validate-image')
        if mode in ('flash', 'diagnostic'):
            if image is None:
                raise Failure(INPUT, '--image required for flash/diagnostic')
            data = _pinned(image, CANDIDATE if mode == 'flash' else DIAGNOSTIC)
        else:
            if image is not None:
                raise Failure(INPUT, 'restore accepts only pinned system1.img; no --image')
            data = snapshots['system1']
        if len(data) % ALIGNMENT or len(data) > BACKUPS['system1'][0]:
            raise Failure(INPUT, 'image must fit nominal system1 and align to 16 KiB')
        input_hashes['payload'] = {'length': len(data), 'sha256': sha256(data)}
        journal.stage('plan')
        bank._write_json_exclusive(out / 'plan.json', {
            'mode': mode, 'target': 'system1', 'inputs': input_hashes,
            'command': f'download store system1 normal {len(data):#x}',
            'scope': SCOPE, 'no_automatic_restore': True,
            'preserved': ['boot0', 'boot1', 'system0', 'environment', 'data']})
        # Retain only immutable payload; unchanged helpers validate their own
        # references again immediately before each USB read.
        del snapshots
        strict_guard = _strict_guard(guard)
        tracked = _tracked_backend(load_backend, journal)
        helper_args = dict(dll=str(dll_path), system='Windows',
                           guard=strict_guard, load_backend=tracked)

        def probe(name):
            journal.state.update(offset=0, chunk_length=0)
            journal.stage(name)
            try:
                result = bank.operate(output=out / name, **helper_args)
            except (Exception, KeyboardInterrupt):
                _helper_failure(journal, out / name)
                raise
            _clean(journal)
            if (result.get('status') != 'validated'
                    or result.get('commands_executed') != 8
                    or result.get('observation', {}).get('value') != 'boot1'):
                raise Failure(INPUT, 'require fresh validated boot_part=boot1')
            journal.event('bank-observation', evidence=result)
            return result

        def read(part, name, reference=False):
            journal.state.update(offset=0, chunk_length=0)
            journal.stage(name)
            pin = BACKUPS[part]
            refs = dict(reference=Path(backups) / f'{part}.img',
                        reference_sha256=pin[1]) if reference else {}
            try:
                result = reader.operate('read', part=part, length=pin[0],
                                        output=out / name, **refs, **helper_args)
            except (Exception, KeyboardInterrupt):
                _helper_failure(journal, out / name)
                raise
            _clean(journal)
            observed = (out / name / f'{part}.img').read_bytes()
            digest = sha256(observed)
            if (len(observed) != pin[0] or result.get('length') != pin[0]
                    or result.get('sha256') != digest):
                raise Failure(TRANSFER, 'read length or manifest/hash mismatch')
            journal.event('read-complete', partition=part, length=len(observed), sha256=digest)
            return observed, digest

        before_bank = probe('preflight-bank')
        current, current_hash = read('system1', 'preflight-system1')
        already_matches = mode in ('flash', 'diagnostic') and current[:len(data)] == data
        if mode == 'flash' and not already_matches and current_hash != BACKUPS['system1'][1]:
            raise Failure(INPUT, 'preflight system1 is neither original nor exact candidate prefix')
        if mode == 'diagnostic' and not already_matches and current_hash != DIAGNOSTIC_CURRENT:
            raise Failure(INPUT, 'preflight system1 is neither pinned prior full readback nor exact diagnostic prefix')
        del current
        journal.event('preflight-accepted', already_matches=already_matches,
                      system1_sha256=current_hash)
        if not already_matches:
            _transfer(data, journal, dll=str(dll_path), guard=strict_guard,
                      load_backend=tracked)
        readback, readback_hash = read('system1', 'readback-system1')
        journal.stage('verify-system1')
        prefix_hash = sha256(readback[:len(data)])
        if readback[:len(data)] != data or prefix_hash != sha256(data):
            journal.state['offset'] = next((i for i, (actual, expected) in
                enumerate(zip(readback, data)) if actual != expected), 0)
            raise Failure(TRANSFER, 'system1 readback bytes/prefix SHA256 mismatch')
        if mode == 'restore' and readback_hash != BACKUPS['system1'][1]:
            raise Failure(TRANSFER, 'restored full system1 SHA256 mismatch')
        tail = readback[len(data):]
        tail_record = dict(length=len(tail), sha256=sha256(tail),
                           all_ff=all(value == 255 for value in tail), required_all_ff=False)
        del readback, tail
        unchanged = {}
        for part in ('boot0', 'boot1', 'system0'):
            observed, digest = read(part, f'unchanged-{part}', reference=True)
            if digest != BACKUPS[part][1]:
                raise Failure(TRANSFER, f'{part} changed')
            unchanged[part] = dict(length=len(observed), sha256=digest, unchanged=True)
            del observed
        after_bank = probe('postflight-bank')
        journal.stage('publish-success')
        manifest = dict(journal.state, status='success', timestamp_utc=bank._utcnow(),
                        input_hash=sha256(data), input_length=len(data),
                        inputs=input_hashes, readback_hash=readback_hash,
                        readback_prefix_hash=prefix_hash, tail=tail_record,
                        unchanged_checks=unchanged, already_matching_candidate=already_matches,
                        bank_observation={'before': before_bank, 'after': after_bank}, scope=SCOPE)
        journal.event('verification-complete', result=manifest)
        journal.close()  # Even a log close failure prevents success publication.
        bank._write_json_exclusive(out / 'manifest.json', manifest)
        return manifest
    except (Exception, KeyboardInterrupt) as exc:
        state = dict(journal.state) if journal else dict(
            mode=mode, target='system1', stage='event-log', offset=0,
            bytes_sent=0, bytes_acked=0, bytes_send_attempted=0, chunk_length=0,
            may_have_modified_flash=False, write_started=False,
            last_response_hex='', disposal_errors=[])
        code = exc.code if isinstance(exc, Failure) else (
            TRANSFER if state['stage'].startswith(('writer-', 'write-media-', 'download-')) else INPUT)
        failure = dict(state, status='failed', error=str(exc) or type(exc).__name__,
                       exit_code=code,
                       timestamp_utc=bank._utcnow())
        persistence_errors = []
        if journal:
            try:
                if not journal.stream.closed:
                    journal.event('failure', error=failure['error'])
            except (Exception, KeyboardInterrupt) as log_exc:
                persistence_errors.append(f'event log: {log_exc}')
            try:
                journal.close()
            except (Exception, KeyboardInterrupt) as close_exc:
                persistence_errors.append(f'event log close: {close_exc}')
        failure['persistence_errors'] = persistence_errors
        try:
            bank._write_json_exclusive(out / 'failure.json', failure)
        except (Exception, KeyboardInterrupt) as failure_exc:
            persistence_errors.append(f'failure.json: {failure_exc}')
        message = failure['error']
        if persistence_errors:
            message += '; evidence persistence failed: ' + '; '.join(persistence_errors)
        raise Failure(failure['exit_code'], message) from exc


def main():
    # Historical single-device operations remain importable for protocol tests.
    # Never expose the old image pins as a public flashing workflow.
    print(json.dumps({'error': 'Public CLI disabled: historical single-device reference only. '
                      'See docs/flashing-research.md; do not flash the old CANDIDATE '
                      'or private DIAGNOSTIC images.', 'exit_code': INPUT}))
    return INPUT


if __name__ == '__main__':
    raise SystemExit(main())
