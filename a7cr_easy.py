"""Host-only one-button preparation. No USB, driver or camera write imports."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

VERSION = '0.2.0-preparation'
SOURCES = (
    ('ILCE-7CR_2.01', 'https://support.d-imaging.sony.co.jp/download/NEX/jY168ad238/BODYDATA.DAT?fm=cn',
     1140252120, '6f0e088e2aea121fcb6fc32bf57b225c68705939a284741d529d477f0ad90128'),
    ('ILCE-1M2_4.00', 'https://support.d-imaging.sony.co.jp/download/NEX/lvb4f975ad/BODYDATA.DAT?fm=cn',
     1194332648, 'f6f8db5735fd15584dfc0118f84e537876c3aaeba72a9971388c21334ce34614'),
)

class Cancelled(RuntimeError):
    pass


def checkpoint(cancel):
    if cancel.is_set():
        raise Cancelled('已停止準備，已下載的檔案會保留。')


def data_root():
    override = os.environ.get('A7CR_TOOLS_DATA')
    if override:
        return Path(override).resolve()
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'A7CRTools'


def reference_root():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / 'reference-pack' if getattr(sys, 'frozen', False) else base / 'build/a7cr/reference-pack'


def digest_file(path, cancel):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(8 * 1024 * 1024):
            checkpoint(cancel)
            digest.update(block)
    return digest.hexdigest()


def download_source(spec, folder, cancel, progress):
    checkpoint(cancel)
    model, url, expected_size, expected_hash = spec
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (model + '.DAT')
    part = folder / (model + '.DAT.part')
    if target.exists():
        progress('檢查已下載的官方檔案', 0)
        if target.stat().st_size != expected_size or digest_file(target, cancel) != expected_hash:
            raise ValueError('已下載的檔案校驗不符；已保留原檔，請使用「開啟資料夾」查看。')
        return target
    offset = part.stat().st_size if part.exists() else 0
    if offset > expected_size:
        raise ValueError('下載暫存檔尺寸異常，已保留檔案。')
    if offset < expected_size:
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=45) as response:
            if offset and (response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-')):
                raise ValueError('下載來源暫時無法續傳。暫存檔已保留，請稍後再試。')
            mode = 'ab' if offset else 'xb'
            with part.open(mode) as output:
                while block := response.read(1024 * 1024):
                    checkpoint(cancel)
                    offset += len(block)
                    if offset > expected_size:
                        raise ValueError('下載檔案超出預期尺寸，已停止。')
                    output.write(block)
                    progress('下載官方研究所需檔案', offset / expected_size)
                output.flush()
                os.fsync(output.fileno())
    checkpoint(cancel)
    progress('驗證下載內容', 1)
    if part.stat().st_size != expected_size or digest_file(part, cancel) != expected_hash:
        raise ValueError('下載內容不完整或校驗不符，暫存檔已保留。')
    part.replace(target)
    return target


@contextmanager
def single_job(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'preparation.lock').open('a+b') as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('另一個準備工作正在執行，請先關閉另一個視窗。') from exc
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def worker_command(stage, references, workspace, log):
    if getattr(sys, 'frozen', False):
        worker = Path(sys._MEIPASS) / 'worker'
        return [str(worker / 'python.exe'), str(worker / 'worker.py'), stage,
                str(references), str(workspace), str(log)]
    return [sys.executable, str(Path(__file__).with_name('a7cr_easy_gui.py')),
            '--research-worker', stage, str(references), str(workspace), str(log)]


def run_stage(stage, references, workspace, cancel):
    log = workspace / (stage + '.private.log')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen(worker_command(stage, references, workspace, log),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        while process.poll() is None:
            if cancel.wait(0.2):
                process.terminate()
                process.wait(timeout=15)
                raise Cancelled('已停止離線準備；檔案與紀錄已保留。')
        if process.returncode:
            raise RuntimeError({'inspect': '官方檔案解包', 'extract': '檔案整理', 'build': '候選驗證'}[stage] + '未完成。紀錄已保存在資料夾內；沒有寫入相機。')
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=15)


def prepare(root, references, cancel, emit, stage_runner=run_stage, downloader=download_source):
    import a7cr_research
    with single_job(root):
        checkpoint(cancel)
        emit('status', '檢查工具與可用空間', 0)
        a7cr_research.verify_references(references)
        if shutil.disk_usage(root).free < 7 * 1024**3:
            raise RuntimeError('磁碟可用空間不足，請先保留至少 7 GB。')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
        workspace = root / 'jobs' / stamp
        workspace.mkdir(parents=True, exist_ok=False)
        emit('workspace', str(workspace), 0)
        for index, spec in enumerate(SOURCES):
            model = 'A7CR' if index == 0 else '色彩參考'
            emit('status', f'準備 {model} 官方檔案', (index / 2) * 0.6)
            cached = downloader(spec, root / 'downloads', cancel,
                lambda label, fraction, i=index, m=model: emit('status', f'{m}：{label}', (i + fraction) / 2 * 0.6))
            destination = workspace / 'private/firmware' / spec[0] / 'BODYDATA.DAT'
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(cached, destination)
            except OSError:
                shutil.copyfile(cached, destination)
        for stage, title, fraction in [('inspect', '解開官方檔案', 0.65), ('extract', '整理 A7CR 所需內容', 0.8), ('build', '建立並驗證 FL2 / FL3 候選', 0.9)]:
            checkpoint(cancel)
            emit('status', title, fraction)
            stage_runner(stage, references, workspace, cancel)
        summary = json.loads((workspace / 'public-summary.json').read_text(encoding='utf-8'))
        if summary.get('device_access') is not False or summary.get('installable_package') is not False:
            raise ValueError('研究結果狀態不符，已停止。')
        result = {'version': VERSION, 'phase': 'PREPARED_NOT_INSTALLED', 'workspace': str(workspace),
                  'camera_accessed': False, 'installation_available': False}
        (workspace / 'easy-result.private.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        emit('complete', '準備與離線驗證完成，尚未安裝到相機。', 1)
        return result


def research_worker(args):
    stage, references, workspace, log = args
    with Path(log).open('w', encoding='utf-8', buffering=1) as output:
        sys.stdout = sys.stderr = output
        try:
            import a7cr_research
            a7cr_research.run(stage, Path(references), Path(workspace))
            return 0
        except BaseException:
            import traceback
            traceback.print_exc()
            return 1
