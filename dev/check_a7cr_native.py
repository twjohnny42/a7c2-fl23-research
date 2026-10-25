"""Compile historical reference workers and emulate synthetic syscalls only.
No camera access, real mount, firmware data or host flash writes are involved.
These checks do not prove power-loss survival or physical camera recovery.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from a7cr_reader import save_json
from a7cr_research import private_location

SOURCES = {'build_native_workers.py': ['injectiver/a7cr-creative-look-tools', '48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f', 'offline/build_native_workers.py', '14df86763d3bec58640354458902c119d7f2f7c97b1132f31657825de5cb9b45'], 'check_native_workers.py': ['injectiver/a7cr-creative-look-tools', '48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f', 'offline/check_native_workers.py', '7c44add3194d21eb9b25a4f42514939d5b3d06f441e71d175d066001628436e2'], 'src/fl23/LICENSE': ['QAQLeonard/a7c2-fl23-research', 'b51688deecb56cd34fdd1f2948915cd6ea4e6736', 'src/fl23/LICENSE', 'c592870a8504689cbe03449c2ae582ba6517716c9e0cd2f880cea50c187d16e9'], 'src/fl23/native/installer.c': ['QAQLeonard/a7c2-fl23-research', 'b51688deecb56cd34fdd1f2948915cd6ea4e6736', 'src/fl23/native/installer.c', '62925a0461b896eb25ad6c7abbff908a7bfe27593526a9ace01cfb1f0770f945'], 'src/fl23/native/wbi.c': ['QAQLeonard/a7c2-fl23-research', 'b51688deecb56cd34fdd1f2948915cd6ea4e6736', 'src/fl23/native/wbi.c', 'f35ce9b4d22d86534664ebc3d0492e6e3bd0ee7234d58bdfad2488192e090585'], 'src/fl23/native/sha256.h': ['QAQLeonard/a7c2-fl23-research', 'b51688deecb56cd34fdd1f2948915cd6ea4e6736', 'src/fl23/native/sha256.h', '85dc96693857e6f4b9f9035e5c5b02b80a49ab44082f0c19dc46cd5c0400238e']}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def portable_compile(command, **kwargs):
    if command[:6] != [sys.executable, '-m', 'ziglang', 'cc', '-target', 'aarch64-linux-gnu']:
        raise ValueError('Unexpected native compiler request')
    destination = Path(command[command.index('-o') + 1])
    source = command[-1]
    obj = destination.with_suffix('.o')
    temporary = destination.parent / 'host-temp'
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(kwargs['env'], TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
    flags = [value for value in command[4:command.index('-o')]
             if value != '-shared' and not value.startswith('-Wl,')]
    subprocess.run([sys.executable, '-m', 'ziglang', 'clang', *flags,
                    '-c', source, '-o', str(obj)], env=env, check=True)
    return subprocess.run([sys.executable, '-m', 'ziglang', 'ld.lld', '-shared', '--no-undefined',
                           '-Bsymbolic', '-z', 'noexecstack', '--init=_init',
                           '-o', str(destination), str(obj)], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    workspace = private_location(args.workspace)
    sources = workspace / 'sources'
    for relative, (repo, revision, path, digest) in SOURCES.items():
        destination = sources / relative
        if not destination.exists():
            with urllib.request.urlopen(f'https://raw.githubusercontent.com/{repo}/{revision}/{path}', timeout=60) as response:
                raw = response.read().replace(b'\r\n', b'\n')
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Downloaded source differs')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        if hashlib.sha256(destination.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest:
            raise ValueError('Reference source changed; preserved')
    out = workspace / 'private/native-workers'
    if (out / 'simulation.private.json').exists():
        raise ValueError('Previous results preserved; use a new workspace')
    out.mkdir(parents=True, exist_ok=True)
    sys.modules['inspect_sources'] = SimpleNamespace(ROOT=workspace, save_json=save_json)
    compiler = load('build_native_workers', sources / 'build_native_workers.py')
    compiler.UPSTREAM = sources / 'src/fl23'
    compiler.subprocess = SimpleNamespace(run=portable_compile)
    save_json(out / 'workers.private.json', {'camera_preflight_passed': False,
              'installation_authorized': False, 'installable_package': False})
    checker = load('check_native_workers', sources / 'check_native_workers.py')
    checker.main()


if __name__ == '__main__':
    main()
