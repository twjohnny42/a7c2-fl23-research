"""Build the preparation GUI; this does not package Sony firmware."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import importlib.util
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import a7cr_research as research


def make_reference_pack():
    references = ROOT / 'build/a7cr/references'
    research.bootstrap(references)
    pack = ROOT / 'build/a7cr/reference-pack'
    pack.mkdir(parents=True, exist_ok=True)
    for relative in research.REFERENCES:
        destination = pack / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((references / relative).read_bytes())
    shutil.copytree(references / 'notices', pack / 'notices', dirs_exist_ok=True)
    parser = references / 'research/repos/fwtool-cxd90057'
    rows = {}
    for name in subprocess.check_output(['git', '-C', str(parser), 'ls-files'], text=True).splitlines():
        raw = subprocess.check_output(['git', '-C', str(parser), 'show', research.FWTOOL_REV + ':' + name])
        path = pack / 'research/repos/fwtool-cxd90057' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        rows[name] = hashlib.sha256(raw).hexdigest()
    manifest = {'schema': 'a7cr-bundled-parser/1', 'revision': research.FWTOOL_REV, 'files': rows}
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    if hashlib.sha256(raw).hexdigest() != research.PARSER_MANIFEST_SHA256:
        raise ValueError('Parser pack differs from reviewed source')
    (pack / 'parser-manifest.json').write_bytes(raw)
    research.verify_references(pack)
    return pack


def make_worker_runtime():
    archive = ROOT / 'build/a7cr/python-embed.zip'
    if not archive.exists():
        with urllib.request.urlopen('https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip', timeout=60) as response:
            with archive.open('xb') as output:
                shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3':
        raise ValueError('Official embedded Python archive differs')
    worker = ROOT / 'build/a7cr/worker-runtime'
    worker.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if not (worker / item.filename).resolve().is_relative_to(worker.resolve()):
                raise ValueError('Archive path escapes worker')
            package.extract(item, worker)
    (worker / 'python312._pth').write_text('python312.zip\n.\npackages\n', encoding='utf-8')
    for name in ('Cryptodome', 'yaml', 'elftools', 'unicorn'):
        source = Path(importlib.util.find_spec(name).origin).parent
        shutil.copytree(source, worker / 'packages' / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    import ziglang
    zig = Path(ziglang.__file__).resolve().parent
    destination = worker / 'packages/ziglang'
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('zig.exe', '__init__.py', '__main__.py', 'LICENSE'):
        shutil.copyfile(zig / name, destination / name)
    shutil.copyfile(ROOT / 'a7cr_research.py', worker / 'a7cr_research.py')
    shutil.copyfile(ROOT / 'dev/a7cr_worker.py', worker / 'worker.py')
    return worker


if __name__ == '__main__':
    pack = make_reference_pack()
    worker = make_worker_runtime()
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
               '--name', 'A7CR-Preparation', '--distpath', str(ROOT / 'dist/a7cr'),
               '--workpath', str(ROOT / 'build/a7cr/pyinstaller'), '--specpath', str(ROOT / 'build/a7cr'),
               '--add-data', str(pack) + ':reference-pack', '--add-data', str(ROOT / 'LICENSE') + ':.',
               '--add-data', str(worker) + ':worker',
               str(ROOT / 'a7cr_easy_gui.py')]
    subprocess.run(command, cwd=ROOT, check=True)
