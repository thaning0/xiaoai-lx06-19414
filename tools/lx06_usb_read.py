"""Bounded experimental LX06 reader. Importing this module never imports USB."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess

TIMEOUT = 5000
WINDOW = 65536
# data: observed on this one device via SSH /proc/mtd (mtd6, 0x01400000,
# erase 0x20000, UBIFS); a logical extent, not a general model guarantee.
CAPS = dict(boot0=6*1024*1024, boot1=6*1024*1024,
            system0=40*1024*1024, system1=40*1024*1024, data=20*1024*1024)
# Exit codes: no device, guard, identify, transfer, input/backend/files.
NO_DEVICE, GUARD, IDENTIFY, TRANSFER, INPUT = 2, 3, 4, 5, 6


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def windows_guard(run=subprocess.run):
    script = r"""$ErrorActionPreference = 'Stop'
$records = @(Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -match '^USB\\VID_1B8E&PID_C003(?:&|\\)'
} | ForEach-Object {
    $service = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction Stop
    [pscustomobject]@{InstanceId=$_.InstanceId; Service=$service.Data; Status=$_.Status}
})
ConvertTo-Json -InputObject $records -Compress
"""
    try:
        result = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                     capture_output=True, text=True, timeout=20, check=True)
        records = json.loads(result.stdout)
        if records == []:
            raise Failure(NO_DEVICE, 'no present matching PnP device')
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError('require exactly one present matching PnP device')
        item = records[0]
        if not item.get('InstanceId') or not item.get('Status') or str(item.get('Service', '')).lower() != 'winusb':
            raise ValueError('actual device Service must be WinUSB')
        return {'checked': True, 'query': 'Get-PnpDevice -PresentOnly', 'devices': records}
    except Failure:
        raise
    except Exception as exc:
        raise Failure(GUARD, f'WinUSB guard unavailable or mismatched: {exc}') from exc


def backend(dll=None):
    if dll:
        path = Path(dll)
        if not path.is_file() or 'libusb0' in path.name.lower() or path.name.lower() != 'libusb-1.0.dll' or 'libusb0' in path.resolve().name.lower():
            raise Failure(INPUT, 'DLL must be an existing libusb-1.0.dll, never libusb0')
    import usb.core
    import usb.util
    import usb.backend.libusb1
    be = usb.backend.libusb1.get_backend(find_library=(lambda _: str(Path(dll).resolve())) if dll else None)
    if be is None:
        raise Failure(INPUT, 'libusb1 backend unavailable; no fallback')
    return usb.core, usb.util, be


def validate(part, length, reference, reference_sha256):
    if part not in CAPS or not isinstance(length, int) or length <= 0 or length % WINDOW or length > CAPS[part]:
        raise Failure(INPUT, 'invalid partition/length: positive 64KiB alignment and safety ceiling required')
    if bool(reference) != bool(reference_sha256):
        raise Failure(INPUT, 'reference requires SHA256 and vice versa')
    if reference:
        data = Path(reference).read_bytes()
        if len(data) != length or hashlib.sha256(data).hexdigest().lower() != reference_sha256.lower():
            raise Failure(INPUT, 'reference length or SHA256 mismatch')
        return data
    return None


def operate(action, *, part=None, length=None, output=None, reference=None,
            reference_sha256=None, dll=None, system=None, guard=windows_guard,
            load_backend=backend):
    devices = []
    util = None
    partial = None
    owned_directory = False
    owned_image = False
    owned_manifest = False
    stage, offset, raw = 'setup', 0, b''
    try:
        if action not in ('identify', 'read'):
            raise Failure(INPUT, 'unknown action')
        golden = validate(part, length, reference, reference_sha256) if action == 'read' else None
        # Reserve a new directory exclusively, before any device access.
        if action == 'read':
            if output is None:
                raise Failure(INPUT, 'new output directory required')
            out = Path(output)
            # Refuse ancestor symlinks as well as existing destinations.
            if any(p.is_symlink() for p in (out, *out.absolute().parents)):
                raise Failure(INPUT, 'symlink output path')
            out.mkdir(exist_ok=False)
            owned_directory = True
            partial = open(out / f'{part}.partial', 'xb')
        stage = 'driver-guard'
        evidence = guard() if (system or platform.system()) == 'Windows' else {
            'checked': False, 'reason': 'non-Windows; no Windows PnP Service comparison'}
        stage = 'backend'
        core, util, be = load_backend(dll)
        stage = 'enumeration'
        # Append during iteration so even partial enumeration failures are disposed.
        for dev in core.find(find_all=True, idVendor=0x1b8e, idProduct=0xc003, backend=be):
            devices.append(dev)
        if not devices:
            raise Failure(NO_DEVICE, 'no matching USB enumeration')
        if len(devices) != 1:
            raise Failure(INPUT, 'require exactly one USB device')
        dev = devices[0]
        stage = 'identify'
        raw = bytes(dev.ctrl_transfer(0xc0, 0x20, 0, 0, 4, timeout=TIMEOUT))
        if raw != bytes.fromhex('00070010'):
            raise Failure(IDENTIFY, 'unexpected identify response')
        if action == 'identify':
            return {'identify': raw.hex(), 'winusb_guard': evidence}
        stage, raw = 'claim', b''
        dev.get_active_configuration()
        util.claim_interface(dev, 0)
        stage = 'upload'
        command = f'upload store {part} normal {length:#x}'.encode('ascii').ljust(64, b'\0')
        if dev.ctrl_transfer(0x40, 0x34, 0, 2, command, timeout=TIMEOUT) != 64:
            raise Failure(TRANSFER, 'short command transfer')
        stage = 'status'
        raw = bytes(dev.read(0x81, 512, timeout=TIMEOUT))
        if len(raw) != 512 or raw.split(b'\0', 1)[0].strip(b' \t\r\n') != b'success':
            raise Failure(TRANSFER, 'status is not exact success token')
        digest = hashlib.sha256()
        for window in range(0, length, WINDOW):
            stage, offset, raw = 'header', window, b''
            raw = bytes(dev.ctrl_transfer(0xc0, 0x33, 4096, 16, 16, timeout=TIMEOUT))
            if len(raw) != 16 or int.from_bytes(raw[4:8], 'little') != WINDOW:
                raise Failure(TRANSFER, 'invalid window header')
            for packet in range(128):
                stage, offset, raw = 'packet', window + packet*512, b''
                raw = bytes(dev.read(0x81, 512, timeout=TIMEOUT))
                if len(raw) != 512:
                    raise Failure(TRANSFER, 'short packet')
                if golden is not None and raw != golden[offset:offset+512]:
                    raise Failure(TRANSFER, 'reference mismatch')
                try:
                    if partial.write(raw) != 512:
                        partial.seek(offset)
                        partial.truncate()
                        raise OSError('short output write')
                except OSError as exc:
                    raise Failure(INPUT, f'output write failed: {exc}') from exc
                digest.update(raw)
        stage, offset, raw = 'finalize', length, b''
        partial.flush()
        os.fsync(partial.fileno())
        partial.close()
        partial = None
        manifest = {'vid': '1b8e', 'pid': 'c003', 'identify': '00070010',
                    'backend': 'libusb1', 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                    'partition': part, 'length': length, 'sha256': digest.hexdigest(),
                    'provenance': 'experimental bounded USB upload store read',
                    'reference_sha256': reference_sha256, 'winusb_guard': evidence,
                    'scope': 'requested bytes only; does not establish full physical partition size'}
        # Hard-link publication refuses any existing file or symlink.
        os.link(out / f'{part}.partial', out / f'{part}.img')
        owned_image = True
        with open(out / 'manifest.json', 'x') as f:
            owned_manifest = True
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        (out / f'{part}.partial').unlink()
        return manifest
    except (Exception, KeyboardInterrupt) as exc:
        # Only remove publications this run successfully created. The partial
        # remains available even when failure happened after closing its stream.
        for owned, name in ((owned_image, f'{part}.img'), (owned_manifest, 'manifest.json')):
            if owned:
                try:
                    (out / name).unlink()
                except OSError:
                    pass
        code = exc.code if isinstance(exc, Failure) else (IDENTIFY if stage == 'identify' else TRANSFER if stage in ('claim', 'upload', 'status', 'header', 'packet') else INPUT)
        if partial is not None:
            try:
                partial.close()
            except OSError:
                pass
        if owned_directory:
            for name, data in (
                ('failure.bin', raw),
                ('failure.json', json.dumps({'stage': stage, 'absolute_partition_offset': offset,
                                            'actual_length': len(raw), 'error': str(exc)}, indent=2).encode()),
            ):
                try:
                    with open(out / name, 'xb') as f:
                        f.write(data)
                except OSError:
                    pass
        raise Failure(code, str(exc)) from exc
    finally:
        if util is not None:
            for dev in devices:
                try:
                    util.dispose_resources(dev)
                except Exception:
                    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(INPUT, json.dumps({'error': message, 'exit_code': INPUT}) + '\n')


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('--dll', help='explicit libusb-1.0.dll path')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('identify')
    read = commands.add_parser('read')
    read.add_argument('part', choices=CAPS)
    read.add_argument('--length', required=True, type=lambda s: int(s, 0))
    read.add_argument('--output', required=True, help='new directory')
    read.add_argument('--reference')
    read.add_argument('--reference-sha256')
    args = vars(parser.parse_args())
    try:
        print(json.dumps(operate(**args), indent=2))
        return 0
    except Failure as exc:
        print(json.dumps({'error': str(exc), 'exit_code': exc.code}))
        return exc.code


if __name__ == '__main__':
    raise SystemExit(main())
