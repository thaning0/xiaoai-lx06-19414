"""Bounded static ARM ELF closure audit using host readelf; never target ldd.

Models object RPATH/RUNPATH ($ORIGIN) then /lib and /usr/lib, no environment,
ld.so.cache, working-directory search (empty path entries), inherited RPATH, dlopen, relocations or runtime symbol interposition.
Checks required GNU version labels against the selected provider, not merely any
library with a matching name. This is not a full dynamic linker simulation.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess

try:
    from . import lx06_offline_build as build
except ImportError:
    import lx06_offline_build as build


def elf_info(path):
    with path.open('rb') as stream:
        header = stream.read(52)
    if header[:4] != b'\x7fELF':
        return None
    if len(header) >= 20 and struct.unpack_from('<H', header, 16)[0] == 1:
        # Original AArch64 kernel modules are relocatables, not ARM user ELFs.
        return None
    abi = (header[4], header[5], struct.unpack_from('<H', header, 18)[0])
    if len(header) < 52 or abi not in ((1, 1, 40), (2, 1, 183)):
        raise ValueError('not little-endian ARM/AArch64 ELF: '+str(path))
    flags = struct.unpack_from('<I', header, 36 if abi[0] == 1 else 48)[0]
    result = subprocess.run(['/usr/bin/readelf', '-W', '-l', '-d', '-V', str(path)],
                            capture_output=True, text=True, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    text = result.stdout
    needed = re.findall(r'\(NEEDED\).*\[([^]]+)\]', text)
    paths = re.findall(r'\((?:RUNPATH|RPATH)\).*\[([^]]*)\]', text)
    if len(paths) > 1:
        raise ValueError('multiple search path tags unsupported: '+str(path))
    required = {}; provided = set(); section = ''; library = None
    for line in text.splitlines():
        if line.startswith('Version needs section'):
            section = 'needs'
        elif line.startswith('Version definition section'):
            section = 'definitions'
        elif line.startswith('Version symbols section'):
            section = ''
        if section == 'needs':
            match = re.search(r'File: (\S+)', line)
            if match:
                library = match[1]; required[library] = []
            match = re.search(r'Name: (\S+)', line)
            if match and library:
                required[library].append(match[1])
        elif section == 'definitions':
            match = re.search(r'Name: (\S+)', line)
            if match:
                provided.add(match[1])
    interp = re.search(r'Requesting program interpreter: ([^]]+)', text)
    return {'needed': needed, 'search_path': paths[0].split(':') if paths else [],
            'required_versions': required, 'provided_versions': sorted(provided),
            'interpreter': interp[1] if interp else None, 'arm_flags': flags, 'abi': list(abi)}


def audit(root):
    root = Path(root).absolute(); entries = build.inventory(root)
    objects = {}
    for name, entry in entries.items():
        if entry['type'] == 'file':
            info = elf_info(root/name)
            if info is not None:
                objects[name] = info
    failures = []; details = {}
    for name, info in objects.items():
        resolved = {}; search = []
        for directory in info['search_path']:
            if not directory:
                continue
            directory = directory.replace('${ORIGIN}', '/'+str(PurePosixPath(name).parent)).replace('$ORIGIN', '/'+str(PurePosixPath(name).parent))
            if not directory.startswith('/') or '$' in directory:
                raise ValueError('unsupported ELF search path: '+name+': '+directory)
            search.append(directory)
        search += ['/lib', '/usr/lib']
        requests = list(dict.fromkeys(info['needed'] + list(info['required_versions'])))
        if info['interpreter']:
            requests.append(info['interpreter'])
        for library in requests:
            candidates = [library] if library.startswith('/') else [p+'/'+library for p in search]
            provider = None
            for candidate in candidates:
                try:
                    path = build.virtual_file(root, candidate)
                except build.PrerequisiteError:
                    continue
                provider = path.relative_to(root).as_posix()
                break
            if provider is None:
                failures.append({'object': name, 'library': library, 'error': 'missing'})
                continue
            supplied = objects.get(provider)
            if supplied is None:
                failures.append({'object': name, 'library': library, 'provider': provider, 'error': 'not ELF'})
                continue
            if info['abi'] != supplied['abi']:
                failures.append({'object': name, 'library': library, 'error': 'ELF ABI mismatch'})
            # Hard/soft float ABI must agree when both ELF objects declare it.
            a = info['arm_flags'] & 0x600; b = supplied['arm_flags'] & 0x600
            if a and b and a != b:
                failures.append({'object': name, 'library': library, 'error': 'ARM float ABI mismatch'})
            missing = sorted(set(info['required_versions'].get(library, [])) - set(supplied['provided_versions']))
            if missing:
                failures.append({'object': name, 'library': library, 'provider': provider,
                                 'error': 'missing version definitions', 'versions': missing})
            resolved[library] = provider
        details[name] = dict(info, resolved=resolved)
    return {'status': 'passed' if not failures else 'failed', 'elf_files': len(objects),
            'failures': failures, 'details': details,
            'limitations': __doc__.split('\n\n', 1)[1].strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rootfs'); parser.add_argument('--output', required=True)
    args = parser.parse_args(); result = audit(args.rootfs)
    with Path(args.output).open('x') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    print(json.dumps({k: result[k] for k in ('status', 'elf_files', 'failures')}))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
