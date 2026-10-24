"""Native syslogd test worker. Never read the host kernel log or start klogd."""
import json
from pathlib import Path
import socket
import subprocess
import time


def wait_for(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (FileNotFoundError, ConnectionRefusedError, subprocess.CalledProcessError):
            pass  # A socket pathname can survive a previous daemon's exit.
        time.sleep(0.01)
    raise AssertionError('private syslogd did not become ready/drain messages')


def logs():
    return subprocess.run(['/work/busybox', 'logread'], check=True,
                          capture_output=True, text=True).stdout


def ring_size():
    rows = Path('/proc/sysvipc/shm').read_text().splitlines()
    columns = rows[0].split()
    sizes = [int(row.split()[columns.index('size')]) for row in rows[1:]
             if int(row.split()[columns.index('key')]) == 0x414e4547]
    assert sizes == [65536], sizes


def main():
    assert Path.cwd() == Path('/repo') and not Path('/data').exists()
    assert not Path('/dev/log').exists()
    # Even a hook repeatedly invoked with core input must not touch this log.
    Path('/tmp/log').mkdir()
    sentinel = Path('/tmp/log/messages')
    sentinel.write_text('preserve existing evidence\n')
    initial = sentinel.read_bytes()
    command = json.loads(Path('/work/command.json').read_text())
    for attempt in range(2):
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: Path('/dev/log').is_socket() and proc.poll() is None)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.settimeout(2)
            def connected():
                sock.connect('/dev/log')
                return True
            wait_for(connected)
            try:
                sock.send(b'<13>fixture: oldest-marker')
                wait_for(lambda: 'oldest-marker' in logs())
                ring_size()
                # Several MiB through a 64 KiB ring: old content must be evicted.
                for number in range(20000):
                    sock.send(f'<14>fixture: {number:05d} '.encode() + b'x'*220)
                sock.send(b'<14>fixture: drained-marker')
                wait_for(lambda: 'drained-marker' in logs())
                ring_size()
                assert 'oldest-marker' not in logs()
                assert len(logs().encode()) <= 65536
                for _ in range(4):
                    sock.send(b'<11>fixture: identical-message')
                sock.send(b'<14>fixture: duplicates-drained')
                wait_for(lambda: 'duplicates-drained' in logs())
                assert logs().count('identical-message') == 1
                # Exercise the hook repeatedly, including a large pipe payload.
                for _ in range(16):
                    subprocess.run(['/bin/sh', '/work/coredump.sh', '123', '6', 'fixture'],
                                   input=b'core' * 524288, timeout=3, check=True)
                # Synthetic kernel-facility input; actual klogd would read host kernel state.
                sock.send(b'<0>kernel: kernel-marker')
                wait_for(lambda: 'kernel-marker' in logs())
                evidence = logs()
                assert 'process=fixture pid=123 signal=6; core capture disabled' in evidence
                assert 'kernel-marker' in evidence
                assert sentinel.read_bytes() == initial
                assert not list(Path('/tmp').glob('core.*'))
                assert not Path('/work/forbidden-file').exists()
                ring_size()
            finally:
                sock.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)
        assert proc.returncode == -15, proc.returncode
    print('ring/crash/restart passed')


if __name__ == '__main__':
    main()
