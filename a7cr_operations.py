"""Explicit A7CR read-only device jobs and independently verified checkpoints."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

MODEL = 'ILCE-7CR'
VERSION = '2.01'
WRITE_BACKEND_RELEASED = False


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def verify_checkpoint(first, second, expected_body=None):
    import a7cr_reader as reader
    first, second = Path(first).resolve(), Path(second).resolve()
    if first == second or first in second.parents or second in first.parents:
        raise ValueError('Backup copies must be independent directories')
    a = json.loads((first / 'backup.private.json').read_text(encoding='utf-8'))
    b = json.loads((second / 'backup.private.json').read_text(encoding='utf-8'))
    if a != b or a.get('schema') != 'a7cr-read-only-backup/1' or a.get('phase') != 'READ_ONLY_BACKUP_COMPLETE':
        raise ValueError('Backup manifests differ or are incomplete')
    if a.get('firmware_version_user_confirmed') != VERSION or a.get('camera_files_or_settings_written') is not False:
        raise ValueError('Unsupported backup provenance')
    identity = a['identity']
    if identity.get('model') != MODEL:
        raise ValueError('Wrong camera model')
    for name in ('usb_serial_sha256', 'ap_serial_sha256'):
        if not re.fullmatch('[a-f0-9]{64}', identity.get(name, '')):
            raise ValueError('Missing private body binding')
    if expected_body is not None and any(identity.get(key) != value for key, value in expected_body.items()):
        raise ValueError('Backup belongs to a different body')
    sizes = {name: length for length, name in reader.PROGRAMS.values()}
    sizes.update({f'unit-{key:08x}.private.bin': length for key, length in reader.CP_UNITS.items()})
    expected = set(sizes) | {'settings-memory.private.bin', 'settings-flash.private.bin'}
    if set(a.get('files', {})) != expected:
        raise ValueError('Backup is missing required files')
    for name, spec in a['files'].items():
        if not re.fullmatch('[a-f0-9]{64}', spec.get('sha256', '')):
            raise ValueError('Invalid backup digest')
        for folder in (first, second):
            path = folder / name
            if path.resolve().parent != folder or path.is_symlink() or not path.is_file():
                raise ValueError('Backup file escapes its directory')
            if path.stat().st_size != spec['bytes'] or (name in sizes and spec['bytes'] != sizes[name]):
                raise ValueError('Backup length differs')
            if sha_file(path) != spec['sha256']:
                raise ValueError('Backup content changed')
    for folder in (first, second):
        snapshots = [reader.backup_sections((folder / name).read_bytes()) for name in
                     ('settings-memory.private.bin', 'settings-flash.private.bin')]
        for sections in snapshots:
            serial = sections[0][0x00E70003].data
            if len(serial) != 4 or hashlib.sha256(serial.hex().encode('ascii')).hexdigest() != identity['ap_serial_sha256']:
                raise ValueError('Backup snapshot belongs to a different body')
            for key, length in reader.CP_UNITS.items():
                raw = sections[1][key].data
                if len(raw) != length or raw != (folder / f'unit-{key:08x}.private.bin').read_bytes():
                    raise ValueError('CP units disagree with checksum-valid snapshots')
    return {'phase': 'BACKUP_VERIFIED', 'model': MODEL, 'files': len(expected), 'copies': 2,
            'body_binding': identity, 'same_volume': first.stat().st_dev == second.stat().st_dev,
            'physical_restoration_proven': False}


def backup_action(root, *, prepare_drivers=False):
    from a7cr_easy import single_job
    from a7cr_reader import run_backup, save_json
    root = Path(root).resolve()
    state_file = root / 'device-state.private.json'
    with single_job(root / 'device-session'):
        from a7cr_actions import stopped_state
        stopped_state(root)
        if state_file.exists():
            previous = json.loads(state_file.read_text(encoding='utf-8'))
            if previous['phase'] in ('READ_ONLY_BACKUP_STARTED', 'STOPPED_INSPECTION_REQUIRED'):
                raise RuntimeError('上次相機連線結果不明，已阻止再次認證。請保留連線及紀錄供檢查。')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
        first, second = root / 'backups' / stamp, root / 'backup-copies' / stamp
        save_json(state_file, {'phase': 'READ_ONLY_BACKUP_STARTED', 'first': str(first), 'second': str(second)})
        try:
            factory = None
            if prepare_drivers:
                from a7cr_drivers import DriverSetup
                from a7cr_reader import Reader
                setup = DriverSetup(root / 'drivers', owner_confirmed=True)
                setup.prepare_normal()
                factory = lambda record, mac_capture=False: Reader(record, mac_capture=False,
                            service_driver_setup=setup.service_if_present)
            run_backup(first, second, VERSION, mac_capture=False, reader_factory=factory)
            report = verify_checkpoint(first, second)
            result = dict(report, first=str(first), second=str(second), firmware_or_settings_written=False)
            save_json(root / 'latest-backup.private.json', result)
            save_json(state_file, {'phase': 'BACKUP_VERIFIED', 'first': str(first), 'second': str(second)})
            return result
        except BaseException as error:
            events_path = first / 'events.private.json'
            events = json.loads(events_path.read_text(encoding='utf-8')) if events_path.exists() else []
            phases = {event['phase'] for event in events}
            unknown = 'normal_authentication_intent' in phases and 'normal_usb_returned' not in phases
            save_json(state_file, {'phase': 'STOPPED_INSPECTION_REQUIRED' if unknown else 'FAILED_WITH_KNOWN_READ_STATE',
                                  'first': str(first), 'second': str(second), 'error_type': type(error).__name__,
                                  'firmware_or_settings_written': False})
            raise


def admission_status():
    # Stable release remains unavailable; experimental actions have separate consent and live checks.
    return {'model': MODEL, 'backup_available': True, 'install_available': WRITE_BACKEND_RELEASED,
            'restore_available': WRITE_BACKEND_RELEASED, 'brick_free_guarantee': False,
            'experimental_install_available': True, 'experimental_restore_available': True,
            'physical_windows_installation_verified': False,
            'reason': '穩定版尚無 Windows/A7CR 實機寫入及復原驗證；實驗流程另需明確同意與原生檢查。'}


def require_released_write_backend():
    if not WRITE_BACKEND_RELEASED:
        raise RuntimeError(admission_status()['reason'])
