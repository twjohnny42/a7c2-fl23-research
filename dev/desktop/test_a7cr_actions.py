"""A7CR live-backend failure injection, using in-memory fixtures only."""
from pathlib import Path
import json,sys
from copy import deepcopy
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import a7cr_actions as actions
import a7cr_install as install
import a7cr_job as planner
from test_a7cr_install import fake_package,FakeDevice,decode_snapshot,prop


def test_write_consent_is_checked_before_package_or_usb(tmp_path):
    with patch.object(install,'Package') as package:
        with pytest.raises(PermissionError):actions.execute_job(tmp_path,tmp_path,'install')
        package.assert_not_called()


def test_unknown_prior_operation_blocks_even_a_new_package(tmp_path):
    (tmp_path/'operation-state.private.json').write_text(json.dumps({'phase':'STOPPED_INSPECTION_REQUIRED'}))
    with patch.object(install,'Package') as package:
        with pytest.raises(RuntimeError):actions.execute_job(tmp_path,tmp_path,'install',experimental_writes_confirmed=True,power_confirmed=True)
        package.assert_not_called()


def test_restore_preserves_later_unrelated_bytes_inside_written_cp_units(tmp_path):
    p=fake_package(tmp_path);attempt=tmp_path/'attempt';attempt.mkdir()
    job=install.Job(p,attempt);camera=FakeDevice(p,job.record,program_state='installed',settings='candidate')
    for state in (camera.memory,camera.flash):
        unit=bytearray(state[1][install.CP_KEYS[0]].data);unit[0x1300]=73
        state[1][install.CP_KEYS[0]]=prop(bytes(unit))
    with patch.object(install,'backup_sections',side_effect=decode_snapshot),patch.object(install,'invoke',side_effect=lambda cam,*args:cam.native(*args)):
        job.restore(camera)
    assert camera.memory[1][install.CP_KEYS[0]].data[0x1300]==73
    assert camera.flash[1][install.CP_KEYS[0]].data[0x1300]==73
    assert camera.program_bytes==p.states['original']


def test_restore_rejects_unsaved_nonproject_bytes_before_any_write(tmp_path):
    p=fake_package(tmp_path);attempt=tmp_path/'attempt';attempt.mkdir()
    job=install.Job(p,attempt);camera=FakeDevice(p,job.record,program_state='installed',settings='candidate')
    raw=bytearray(camera.memory[1][install.CP_KEYS[0]].data);raw[0x1300]=73
    camera.memory[1][install.CP_KEYS[0]]=prop(bytes(raw))
    with patch.object(install,'backup_sections',side_effect=decode_snapshot),pytest.raises(ValueError):job.restore(camera)
    assert camera.operations==[]


@pytest.mark.parametrize('offset,length',[(0,0),(-1,1),(2,10)])
def test_invalid_patch_ranges_are_rejected(offset,length):
    with pytest.raises(ValueError):planner.apply_windows(b'abc',{'bytes':3,'windows':[{'offset':offset,'bytes':length,'before_hex':'61','after_hex':'62'}]})


def test_captured_nonpatch_program_bytes_are_preserved():
    spec={'bytes':4,'windows':[{'offset':1,'bytes':1,'before_hex':'61','after_hex':'62'}]}
    assert planner.apply_windows(b'ZaQ!',spec)==b'ZbQ!'
    with pytest.raises(ValueError):planner.apply_windows(b'ZcQ!',spec)


def test_existing_stop_blocks_ordinary_backup_without_reading_camera(tmp_path):
    from a7cr_operations import backup_action
    (tmp_path/'operation-state.private.json').write_text(json.dumps({'phase':'OPERATION_STARTED'}))
    with patch('a7cr_reader.run_backup') as backup:
        with pytest.raises(RuntimeError):backup_action(tmp_path)
        backup.assert_not_called()

def test_orchestrator_persists_unknown_write_and_blocks_next_session(tmp_path):
    root=tmp_path/'app';workspace=root/'install-jobs'/'fixture';workspace.mkdir(parents=True)
    package=fake_package(workspace);package.manifest={'sources':{'first_backup':'synthetic','second_backup':'synthetic'}}
    (workspace/'state.private.json').write_text(json.dumps({'phase':'PREPARED_NOT_INSTALLED'}))
    made=[]
    class ContextDevice(FakeDevice):
        def __enter__(self):self.record('normal_authentication_intent');return self
        def __exit__(self,*args):
            if self.healthy:self.record('normal_usb_returned')
    def factory(p,record):
        camera=ContextDevice(p,record,fail='cp-timeout');made.append(camera);return camera
    with patch.object(install,'Package',return_value=package),patch('a7cr_operations.verify_checkpoint'),patch.object(actions,'preflight'),patch.object(install,'backup_sections',side_effect=decode_snapshot),patch.object(install,'invoke',side_effect=lambda cam,*args:cam.native(*args)):
        with pytest.raises(TimeoutError):actions.execute_job(root,workspace,'install',experimental_writes_confirmed=True,power_confirmed=True,session_factory=factory)
        state=json.loads((root/'operation-state.private.json').read_text())
        assert state['phase']=='STOPPED_INSPECTION_REQUIRED'
        assert len([op for op in made[0].operations if op[:2]==('cp',0x41)])==1
        second=Mock()
        with pytest.raises(RuntimeError):actions.execute_job(root,workspace,'restore',experimental_writes_confirmed=True,power_confirmed=True,session_factory=second)
        second.assert_not_called()


def test_completed_disk_transaction_does_not_claim_physical_boot(tmp_path):
    root=tmp_path/'app';workspace=root/'install-jobs'/'fixture';workspace.mkdir(parents=True)
    package=fake_package(workspace);package.manifest={'sources':{'first_backup':'synthetic','second_backup':'synthetic'}}
    (workspace/'state.private.json').write_text(json.dumps({'phase':'PREPARED_NOT_INSTALLED'}))
    class ContextDevice(FakeDevice):
        def __enter__(self):self.record('normal_authentication_intent');return self
        def __exit__(self,*args):
            if self.healthy:self.record('normal_usb_returned')
    with patch.object(install,'Package',return_value=package),patch('a7cr_operations.verify_checkpoint'),patch.object(actions,'preflight'),patch.object(install,'backup_sections',side_effect=decode_snapshot),patch.object(install,'invoke',side_effect=lambda cam,*args:cam.native(*args)):
        result=actions.execute_job(root,workspace,'install',experimental_writes_confirmed=True,power_confirmed=True,session_factory=lambda p,r:ContextDevice(p,r))
    assert result['phase']=='INSTALL_DISK_VERIFIED_PENDING_PHYSICAL_CHECK'
    assert result['physical_boot_verified'] is False
    assert result['preview_and_photo_verified'] is False

def test_host_journal_rename_retry_never_repeats_write_body(tmp_path):
    import a7cr_reader
    real=Path.replace;calls=[]
    def replace(source,target):
        calls.append(source.read_bytes())
        if len(calls)<3:raise PermissionError('Synthetic Windows sharing conflict')
        return real(source,target)
    with patch.object(Path,'replace',replace),patch.object(a7cr_reader.time,'sleep'):
        a7cr_reader.save_json(tmp_path/'journal.json',{'phase':'WRITE_RETURNED'})
    assert len(calls)==3 and len(set(calls))==1
    assert json.loads((tmp_path/'journal.json').read_text())['phase']=='WRITE_RETURNED'

@pytest.mark.parametrize('matching',[True,False])
def test_service_body_binding_precedes_native_or_program_operations(matching):
    import hashlib
    from types import SimpleNamespace
    package=SimpleNamespace(binding={'usb_serial_sha256':'a'*64,'ap_serial_sha256':hashlib.sha256(b'1234'.hex().encode()).hexdigest()})
    record=Mock();session=install.Session(package,record)
    session.backend=Mock();session.camera=Mock();session.camera.readBackup.return_value=b'1234' if matching else b'abcd'
    session.usb=Mock();session.device=object()
    with patch.object(install.CheckSession,'__enter__',return_value=session):
        if matching:assert session.__enter__() is session
        else:
            with pytest.raises(ValueError):session.__enter__()
            assert not session.healthy
            session.usb.util.dispose_resources.assert_called_once()
    session.camera.readBackup.assert_called_once_with(0x00E70003)
    session.camera.saveBackup.assert_not_called()
