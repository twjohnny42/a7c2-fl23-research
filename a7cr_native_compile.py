"""Offline AArch64 native compiler backend; no USB."""
from pathlib import Path
import os,sys,subprocess

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
