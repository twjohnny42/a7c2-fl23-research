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

VERSION = '0.3.1-backup-development'
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
            if stage == 'identify':
                import a7cr_reader
                a7cr_reader.save_json(Path(workspace) / 'device-check.private.json',
                    {'devices': a7cr_reader.descriptor_inventory(), 'windows_devices': a7cr_reader.windows_inventory(), 'authenticated': False,
                     'firmware_or_settings_written': False})
            elif stage in ('backup', 'backup-with-driver'):
                from a7cr_operations import backup_action
                backup_action(Path(workspace), prepare_drivers=stage == 'backup-with-driver')
            else:
                import a7cr_research
                a7cr_research.run(stage, Path(references), Path(workspace))
            return 0
        except BaseException:
            import traceback
            traceback.print_exc()
            return 1


def backup_failure_message(root):
    state_path = Path(root) / 'device-state.private.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    # Only this known pre-authentication result permits a manual new attempt.
    # Never suggest retrying a service session whose outcome is unknown.
    if (state.get('phase') == 'FAILED_WITH_KNOWN_READ_STATE'
            and state.get('error_type') == 'DriverLaunchNotStarted'):
        return ('Windows 未允許驅動安裝程式啟動，尚未進入相機服務模式。\n'
                '相機韌體與設定均未寫入，備份尚未開始。\n\n'
                '請關閉工具，從檔案總管直接雙擊 A7CR-Tools-BackupDev.exe，'
                '再按「自動備份」，並親自處理 Windows 管理員提示。\n'
                '若仍被阻擋，請保留紀錄供檢查，勿停用 Windows 安全功能。')
    return ('備份未完成。請保留 USB 連線與紀錄供檢查；沒有寫入韌體。'
            '若已進入服務模式，請勿自行重試。')


def run_backup_worker(root, emit, *, allow_driver_setup=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / 'latest-backup.private.json'
    old_first = json.loads(receipt.read_text(encoding='utf-8')).get('first') if receipt.exists() else None
    log = root / ('backup-' + uuid.uuid4().hex + '.private.log')
    emit('status', '正在自動備份相機；請勿拔線或關機。', 0)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen(worker_command('backup-with-driver' if allow_driver_setup else 'backup', reference_root(), root, log),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    while process.poll() is None:
        time.sleep(0.2)  # No cancel/terminate is offered during a camera session.
    if process.returncode:
        raise RuntimeError(backup_failure_message(root))
    report = json.loads((root / 'latest-backup.private.json').read_text(encoding='utf-8'))
    if (report.get('phase') != 'BACKUP_VERIFIED' or report.get('firmware_or_settings_written') is not False
            or report.get('first') == old_first):
        raise ValueError('Backup receipt is incomplete')
    emit('workspace', report['first'], 0)
    emit('complete', '雙副本備份完成並驗證。這不是完整晶片映像，也不能保證救回故障相機。', 1)
    return report


def run_identify_worker(root, emit):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    from a7cr_easy import single_job
    with single_job(root / 'device-session'):
        output = root / 'device-check.private.json'
        if output.exists():
            archived = root / ('device-check-' + uuid.uuid4().hex + '.private.json')
            output.rename(archived)
        log = root / ('identify-' + uuid.uuid4().hex + '.private.log')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen(worker_command('identify', reference_root(), root, log),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        while process.poll() is None:
            time.sleep(0.2)
        if process.returncode:
            raise RuntimeError('連線檢查未完成；未認證或改寫相機，紀錄已保留。')
        report = json.loads(output.read_text(encoding='utf-8'))
        if report.get('authenticated') is not False or report.get('firmware_or_settings_written') is not False:
            raise ValueError('Unexpected identification receipt')
        devices = report['devices']
        matches = [row for row in devices if str(row.get('product', '')).upper() == 'ILCE-7CR' and 8 in row.get('interfaces', [])]
        if len(matches) == 1:
            text = '已辨識 A7CR 大量儲存介面；尚未認證或備份。'
        elif any(row.get('usb_id') == 'VID_054C&PID_0EA9' for row in report.get('windows_devices', [])):
            text = '已看到 A7CR，但目前為 USB 串流模式。請切換影像傳輸／大量儲存後重新接線。未認證相機。'
        elif not devices and report.get('windows_devices'):
            text = 'Windows 看到 Sony 裝置，但 libusb 無法讀取；請先確認大量儲存模式與驅動。未認證相機。'
        elif not devices:
            text = '目前未偵測到 Sony USB。請確認線材、模式及 USB 連接埠。'
        elif any(row.get('descriptor_error') for row in devices):
            text = '看到 Sony USB，但描述資訊無法讀取；可能是驅動或存取權限問題。未認證相機。'
        else:
            text = '偵測到 Sony 裝置，但尚未確認唯一的 A7CR 大量儲存介面。未認證相機。'
        emit('complete', text, 1)
        return report
