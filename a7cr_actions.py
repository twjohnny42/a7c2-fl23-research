"""Explicit A7CR native preflight, install and restore orchestration.

No device activity at import. Install/restore require explicit experimental
write consent and power confirmation. No automatic retry, reboot or rollback.
"""
from pathlib import Path
from datetime import datetime,timezone
import json,uuid,threading
from a7cr_reader import save_json


def stopped_state(root):
    root=Path(root)
    for name in ('device-state.private.json','operation-state.private.json'):
        path=root/name
        if path.exists():
            phase=json.loads(path.read_text(encoding='utf-8')).get('phase')
            if phase in ('READ_ONLY_BACKUP_STARTED','OPERATION_STARTED','STOPPED_INSPECTION_REQUIRED'):
                raise RuntimeError('上次裝置操作結果不明；已停止，請保留連線及紀錄供檢查，不可重試。')


def preflight(job,camera):
    from a7cr_install import CP_KEYS,PATHS,preserved
    from a7cr_preflight import invoke_check
    p=job.package
    job.body(camera)
    job.program_state(camera,'preflight-before','original')
    before=job.snapshots(camera,'preflight-before')
    job.targets(before,'original','original')
    job.saveable(before,0x02A4);job.saveable(before,0x02CF)
    for key in CP_KEYS:
        if camera.required_cp_read(key)!=p.units['original'][key]:
            raise ValueError('Required CP direct read differs from same-body backup')
    reports={}
    for kind in ('program_check','wbi_check'):
        reports[kind]=invoke_check(camera,kind,p.blobs[kind],job.attempt,job.record,p.audit)
    after=job.snapshots(camera,'preflight-after')
    for name in ('memory','flash'):preserved(before[name],after[name],{})
    job.program_state(camera,'preflight-after','original')
    job.record('LIVE_PREFLIGHT_PASSED',native_reports=reports,firmware_or_settings_written=False)
    return reports


def execute_job(root,workspace,operation,*,experimental_writes_confirmed=False,power_confirmed=False,
                session_factory=None,prepare_drivers=False):
    """Only called by an explicit device action. May perform real camera writes."""
    from a7cr_easy import single_job
    from a7cr_install import Package,Job,Session
    from a7cr_operations import verify_checkpoint
    if operation not in ('preflight','install','restore','verify'):raise ValueError('Unknown device operation')
    if operation in ('install','restore') and not (experimental_writes_confirmed and power_confirmed):
        raise PermissionError('Experimental write and adequate-power confirmation are required')
    root=Path(root).resolve();workspace=Path(workspace).resolve()
    with single_job(root/'device-session'):
        stopped_state(root)
        package=Package(workspace)
        prior=json.loads((workspace/'state.private.json').read_text(encoding='utf-8')).get('phase')
        if operation in ('install','preflight') and prior!='PREPARED_NOT_INSTALLED':
            raise ValueError('This job has already been attempted; it cannot be replayed')
        if prior in ('OPERATION_STARTED','STOPPED_INSPECTION_REQUIRED'):
            raise ValueError('Unknown job outcome; inspect existing evidence instead of retrying')
        attempt=workspace/'attempts'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
        attempt.mkdir(parents=True,exist_ok=False)
        job=Job(package,attempt)
        state=root/'operation-state.private.json'
        save_json(state,{'phase':'OPERATION_STARTED','operation':operation,'job':str(workspace),'attempt':str(attempt)})
        camera=None
        try:
            callback=None
            if prepare_drivers:
                from a7cr_drivers import DriverSetup
                setup=DriverSetup(root/'drivers',owner_confirmed=True)
                setup.prepare_normal();callback=setup.service_if_present
            factory=session_factory or (lambda p,r:Session(p,r,service_driver_setup=callback))
            with factory(package,job.record) as camera:
                try:
                    if operation in ('preflight','install'):
                        preflight(job,camera)
                    if operation=='install':job.install(camera)
                    elif operation=='restore':job.restore(camera)
                    elif operation=='verify':job.inspect(camera,verify=True)
                except BaseException:
                    camera.healthy=False
                    raise
            if not any(e['phase']=='normal_usb_returned' for e in job.events):
                raise ValueError('Normal MSC return was not confirmed; no success receipt')
            phase={'install':'INSTALL_DISK_VERIFIED_PENDING_PHYSICAL_CHECK',
                   'restore':'RESTORE_DISK_VERIFIED_PENDING_PHYSICAL_CHECK',
                   'preflight':'PREPARED_NOT_INSTALLED','verify':'INSTALLED_DISK_REVERIFIED'}[operation]
            job.phase(phase)
            report={'phase':phase,'operation':operation,'job':str(workspace),'attempt':str(attempt),
                    'physical_boot_verified':False,'preview_and_photo_verified':False,
                    'physical_restoration_proven':False,'zero_brick_guarantee':False}
            save_json(state,report)
            return report
        except BaseException as error:
            phases={e['phase'] for e in job.events}
            unknown='normal_authentication_intent' in phases and 'normal_usb_returned' not in phases
            phase='STOPPED_INSPECTION_REQUIRED' if unknown else 'FAILED_BEFORE_AUTHENTICATION'
            report={'phase':phase,'operation':operation,'job':str(workspace),'attempt':str(attempt),
                    'error_type':type(error).__name__,'camera_write_outcome':'inspect_existing_events'}
            save_json(state,report)
            # Preserve the prepared state only when authentication provably never began.
            if unknown:job.phase('STOPPED_INSPECTION_REQUIRED',error_type=type(error).__name__)
            raise


def install_pipeline(root,emit,*,experimental_writes_confirmed=False,power_confirmed=False):
    if not (experimental_writes_confirmed and power_confirmed):raise PermissionError('Explicit experimental write consent required')
    from a7cr_easy import single_job,prepare,reference_root
    from a7cr_operations import backup_action
    from a7cr_job import prepare_job
    root=Path(root).resolve()
    with single_job(root/'install-pipeline'):
        stopped_state(root)
        emit('status','先準備 Windows 驅動並建立新的雙份機身備份；請勿拔線。',0)
        backup=backup_action(root,prepare_drivers=True)
        emit('status','機身備份已驗證，開始準備 FL2 / FL3 安裝資料。',0.15)
        prepared=prepare(root,reference_root(),threading.Event(),emit)
        destination=root/'install-jobs'/uuid.uuid4().hex
        emit('status','依這台機身的原始內容合併色彩參數並編譯安裝／還原程序。',0.9)
        prepare_job(destination,backup['first'],backup['second'],Path(prepared['workspace']))
        save_json(root/'latest-install-job.private.json',{'job':str(destination)})
        emit('workspace',str(destination),0)
        emit('status','即將進行機身檢查及分階段寫入；請勿拔線或關機。',0.95)
        result=execute_job(root,destination,'install',experimental_writes_confirmed=True,
                           power_confirmed=True,prepare_drivers=True)
        emit('complete','寫入及讀回驗證完成。USB 操作已結束；仍須關機重啟，檢查選單、預覽、新照片及播放。',1)
        return result


def restore_pipeline(root,emit,*,experimental_writes_confirmed=False,power_confirmed=False):
    root=Path(root).resolve()
    record=json.loads((root/'latest-install-job.private.json').read_text(encoding='utf-8'))
    job=Path(record['job']).resolve()
    if job.parent!=(root/'install-jobs').resolve():raise ValueError('Restoration job escapes private storage')
    emit('status','正在還原這台機身的原始程式與 FL2/FL3 目標欄位；請勿拔線。',0)
    result=execute_job(root,job,'restore',experimental_writes_confirmed=experimental_writes_confirmed,
                       power_confirmed=power_confirmed,prepare_drivers=True)
    emit('complete','還原寫入及讀回驗證完成。USB 操作已結束；仍須重啟及實機檢查。',1)
    return result


def write_worker(operation,root,log):
    def emit(*event):
        save_json(log.with_suffix('.progress.private.json'),event)
    callback=install_pipeline if operation=='install' else restore_pipeline
    result=callback(root,emit,experimental_writes_confirmed=True,power_confirmed=True)
    save_json(log.with_suffix('.result.private.json'),result)
