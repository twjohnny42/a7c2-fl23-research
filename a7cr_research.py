"""Offline-only A7CR 2.01 firmware research. Never opens USB or installs firmware."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

TOOLS_REV = '48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f'
NOTES_REV = '43ba74441c8f10459ade41e84a379ad8005e6ef4'
A7C2_REV = 'b51688deecb56cd34fdd1f2948915cd6ea4e6736'
FWTOOL_REV = '3955d7890fc38daf942bb52827599bfe35de4a14'
# Hashes normalize Git's Windows CRLF conversion, not executable semantics.
REFERENCES = {
 'offline/inspect_sources.py': ('injectiver/a7cr-creative-look-tools', TOOLS_REV, 'offline/inspect_sources.py', 'c197d7c40ae1e69c39f21464a6f05091e2389371e6eba7b7b7f2ba5b096d3869'),
 'offline/extract_components.py': ('injectiver/a7cr-creative-look-tools', TOOLS_REV, 'offline/extract_components.py', '4df28c9ee35202c0568f2a0076cbb0c0eb5d914e14b50a5a9afaa4bbcbad403f'),
 'offline/build_fl23_candidate.py': ('injectiver/a7cr-creative-look-tools', TOOLS_REV, 'offline/build_fl23_candidate.py', '6c3c66415eb9e3987d1afa1a9239772c56c7d31dc516401b36ec5f4fc2c771d5'),
 'research/repos/a7cr-fl23-research/README.md': ('lzh2016zzz/a7cr-fl23-research', NOTES_REV, 'README.md', '557f987125b189ebdebd167683cd197965eedc428f22f50dbadb207a576fa4d7'),
 'research/repos/a7c2-fl23-research/src/fl23/assets/patches.json': ('QAQLeonard/a7c2-fl23-research', A7C2_REV, 'src/fl23/assets/patches.json', 'c01614a926d6ac48d6563e569a1cfc1a2c35bcb2e45ef9429227c7d14cea6ed0'),
}
MODELS = {'ILCE-7CR': ('2.01', '0x20030013'), 'ILCE-1M2': ('4.00', '0x40030023')}


def text_digest(raw):
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def verify_references(root):
    for relative, (_, _, _, digest) in REFERENCES.items():
        if text_digest((root / relative).read_bytes()) != digest:
            raise ValueError('Reference source changed: ' + relative)
    fwtool = root / 'research/repos/fwtool-cxd90057'
    actual = subprocess.check_output(['git', '-C', str(fwtool), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != FWTOOL_REV:
        raise ValueError('Firmware parser revision differs')
    subprocess.run(['git', '-C', str(fwtool), 'diff', '--exit-code', 'HEAD', '--'], check=True,
                   stdout=subprocess.DEVNULL)
    if subprocess.check_output(['git', '-C', str(fwtool), 'ls-files', '--others', '--exclude-standard']).strip():
        raise ValueError('Untracked files in firmware parser checkout')


def bootstrap(root):
    for relative, (repo, revision, path, expected) in REFERENCES.items():
        target = root / relative
        if target.exists():
            if text_digest(target.read_bytes()) != expected:
                raise ValueError('Existing reference preserved: ' + relative)
            continue
        url = f'https://raw.githubusercontent.com/{repo}/{revision}/{path}'
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read()
        if text_digest(raw) != expected:
            raise ValueError('Downloaded reference differs: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(raw)
    fwtool = root / 'research/repos/fwtool-cxd90057'
    if not fwtool.exists():
        subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout',
                        'https://github.com/DavidBuchanan314/fwtool.py.git', str(fwtool)], check=True)
        subprocess.run(['git', '-C', str(fwtool), 'checkout', '--detach', FWTOOL_REV], check=True)
    # Retain the source notices alongside references, never relicense them.
    for repo, revision, filename, targetname in [
        ('injectiver/a7cr-creative-look-tools', TOOLS_REV, 'LICENSE', 'injectiver-LICENSE'),
        ('injectiver/a7cr-creative-look-tools', TOOLS_REV, 'NOTICE.md', 'injectiver-NOTICE.md'),
        ('QAQLeonard/a7c2-fl23-research', A7C2_REV, 'LICENSE', 'legacy-a7c2-LICENSE')]:
        target = root / 'notices' / targetname
        if not target.exists():
            with urllib.request.urlopen(f'https://raw.githubusercontent.com/{repo}/{revision}/{filename}', timeout=60) as response:
                raw = response.read()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(raw)
    verify_references(root)
    print('Pinned offline references ready; no camera accessed.')


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_model_record(record, model):
    version, model_id = MODELS[model]
    if (record.get('model') != model or record.get('fdat_model') != model_id
            or record.get('version') != version or record.get('fdat_version') != version
            or record.get('outer_crc_valid') is not True
            or record.get('fdat_header_crc_valid') is not True):
        raise ValueError('Firmware model/version/container checks differ: ' + model)


def portable_assembler(command, **kwargs):
    # Translate the one hard-coded Apple clang invocation. Other commands fail closed.
    if command[0] != '/Library/Developer/CommandLineTools/usr/bin/clang' or command[1:3] != ['-target', 'armv7-none-eabi']:
        raise ValueError('Unexpected builder subprocess')
    if len(command) != 8 or command[3:5] != ['-mthumb', '-c'] or command[6] != '-o':
        raise ValueError('Unexpected assembler arguments')
    translated = [sys.executable, '-m', 'ziglang', 'clang', '-cc1as',
                  '-triple', 'thumbv7-none-eabi', '-target-cpu', 'cortex-a9',
                  '-filetype', 'obj', '-o', command[7], command[5]]
    temp = Path(os.environ['ZIG_LOCAL_CACHE_DIR']) / 'temp'
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    return subprocess.run(translated, env=env, **kwargs)


def run(command, root, workspace):
    verify_references(root)
    if not workspace.is_dir():
        raise ValueError('Create a private workspace with the official inputs first')
    inspector = load_module('inspect_sources', root / 'offline/inspect_sources.py')
    inspector.ROOT = workspace
    if command == 'inspect':
        for source in inspector.SOURCES:
            record = inspector.inspect(source)
            validate_model_record(record, source['model'])
        return
    for model, (version, _) in MODELS.items():
        path = workspace / 'private/firmware' / f'{model}_{version}' / 'inspection.private.json'
        validate_model_record(json.loads(path.read_text(encoding='utf-8')), model)
    if command == 'extract':
        extractor = load_module('extract_components', root / 'offline/extract_components.py')
        for source in inspector.SOURCES:
            extractor.extract(source)
        return
    builder = load_module('build_fl23_candidate', root / 'offline/build_fl23_candidate.py')
    builder.TARGET = workspace / 'private/components/ILCE-7CR_2.01'
    builder.DONOR = workspace / 'private/components/ILCE-1M2_4.00'
    builder.OUT = workspace / 'private/offline/fl23-candidate'
    if builder.OUT.exists():
        raise ValueError('Existing candidate preserved; use a fresh workspace for another build')
    builder.NOTES = root / 'research/repos/a7cr-fl23-research/README.md'
    builder.UPSTREAM = root / 'research/repos/a7c2-fl23-research/src/fl23/assets/patches.json'
    builder.subprocess = SimpleNamespace(run=portable_assembler)
    cache = workspace / 'private/compiler-cache'
    os.environ['ZIG_GLOBAL_CACHE_DIR'] = str(cache / 'global')
    os.environ['ZIG_LOCAL_CACHE_DIR'] = str(cache / 'local')
    builder.main()
    audit = json.loads((builder.OUT / 'audit.private.json').read_text(encoding='utf-8'))
    report = {
        'schema': 'a7cr-windows-offline-research/1', 'model': 'ILCE-7CR', 'firmware': '2.01',
        'device_access': False, 'installable_package': False, 'physical_validation_performed': False,
        'cpu_checks': audit['cpu_checks'],
        'programs': {kind: {key: value for key, value in data.items()
                           if key in ('bytes', 'actual_changed_bytes', 'inverse_roundtrip_exact')}
                     for kind, data in audit['programs'].items()},
        'remaining': ['Windows service transport and driver validation',
                      'Fresh same-body backups and native parameter merge',
                      'Native preflight, write/readback and rollback integration',
                      'Physical boot, preview, new photos/playback and restoration acceptance'],
    }
    inspector.save_json(workspace / 'public-summary.json', report)


def private_location(path):
    path = path.resolve()
    repo = Path(__file__).resolve().parent
    if path == repo or (path.is_relative_to(repo) and not any(
            path.is_relative_to(repo / name) for name in ('build', 'private'))):
        raise ValueError('Keep firmware and references outside tracked source, or under build/private')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('bootstrap', 'inspect', 'extract', 'build'))
    parser.add_argument('--references', required=True, type=Path)
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args(argv)
    root = private_location(args.references)
    if args.command == 'bootstrap':
        bootstrap(root)
    else:
        if args.workspace is None:
            parser.error('--workspace is required for offline firmware analysis')
        run(args.command, root, private_location(args.workspace))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print('Stopped; no automatic retry: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
