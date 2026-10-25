from pathlib import Path
import sys
import json
import hashlib
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from a7cr_transaction_model import Plan, SimulationDevice, Transaction, CP_KEYS, AP_KEYS, owned_ranges
import a7cr_operations as operations


def plan():
    before={CP_KEYS[0]:bytes(8192),CP_KEYS[1]:bytes(28672)}
    after={}
    for key,raw in before.items():
        new=bytearray(raw)
        for index,(lo,hi) in enumerate(owned_ranges(key)):
            new[lo:hi]=bytes([index+1])*(hi-lo)
        after[key]=bytes(new)
    return Plan('a'*64,{'AP':b'A'*100,'CP':b'C'*200},{'AP':b'B'+b'A'*99,'CP':b'D'+b'C'*199},
                before,after,dict.fromkeys(AP_KEYS,b'\0'),dict(zip(AP_KEYS,(b'\x0a',b'\x0b'))))


def test_simulated_install_restore_preserves_later_user_and_non_target_unit_changes(tmp_path):
    p=plan();device=SimulationDevice(p)
    installed=Transaction(p,device,tmp_path/'install.json').run('install',backups_verified=True,power_confirmed=True)
    assert installed['programs']==p.after_programs
    assert device.calls[-3:]==['drop1','mode0','read']
    device.state['wbi_mode']=2  # explicit simulated reboot
    device.state['unrelated']['user-settings']=b'new-user-settings'
    raw=bytearray(device.state['units'][CP_KEYS[0]]);raw[0]=77;device.state['units'][CP_KEYS[0]]=bytes(raw)
    restored=Transaction(p,device,tmp_path/'restore.json').run('restore',backups_verified=True,power_confirmed=True)
    assert restored['programs']==p.before_programs
    assert restored['bindings']==p.before_bindings
    assert restored['unrelated']['user-settings']==b'new-user-settings'
    assert restored['units'][CP_KEYS[0]][0]==77
    for key in CP_KEYS:
        for lo,hi in owned_ranges(key):
            assert restored['units'][key][lo:hi]==p.before_units[key][lo:hi]


@pytest.mark.parametrize('failure',['CP_PROGRAM','CP_PARAMETERS','AP_PROGRAM','AP_BINDINGS','WBI','PARTIAL_CP_PARAMETERS'])
def test_lost_acknowledgement_stops_without_read_retry_or_automatic_rollback(tmp_path,failure):
    p=plan();device=SimulationDevice(p,fail=failure);job=Transaction(p,device,tmp_path/'journal.json')
    with pytest.raises(TimeoutError):job.run('install',backups_verified=True,power_confirmed=True)
    calls=list(device.calls)
    with pytest.raises(RuntimeError):job.capture()
    assert device.calls==calls
    assert json.loads((tmp_path/'journal.json').read_text())['events'][-1]['phase']=='STOPPED_INSPECTION_REQUIRED'
    assert not any(event['phase'].startswith('SIMULATED_') for event in job.events)


@pytest.mark.parametrize('stage',['CP_PROGRAM','AP_PROGRAM'])
def test_mismatching_full_program_readback_stops_before_later_stages(tmp_path,stage):
    p=plan();device=SimulationDevice(p,fail='MISMATCH_'+stage)
    with pytest.raises(ValueError):Transaction(p,device,tmp_path/'journal.json').run('install',backups_verified=True,power_confirmed=True)
    assert device.calls[-2:]==[stage,'read']
    assert 'WBI' not in device.calls


def test_partial_known_cp_state_can_only_be_restored_in_a_new_reviewed_simulation(tmp_path):
    p=plan();device=SimulationDevice(p,fail='PARTIAL_CP_PARAMETERS')
    with pytest.raises(TimeoutError):Transaction(p,device,tmp_path/'failed.json').run('install',backups_verified=True,power_confirmed=True)
    device.fail=None
    restored=Transaction(p,device,tmp_path/'new-restoration.json').run('restore',backups_verified=True,power_confirmed=True)
    assert restored['programs']==p.before_programs and restored['units']==p.before_units


@pytest.mark.parametrize('condition',['body','program','binding','wbi','missing-backup','missing-power'])
def test_preflight_rejections_send_no_simulated_writes(tmp_path,condition):
    p=plan();device=SimulationDevice(p)
    if condition=='body':device.state['body']='b'*64
    if condition=='program':device.state['programs']['AP']=b'unknown'
    if condition=='binding':device.state['bindings'][AP_KEYS[0]]=b'unknown'
    if condition=='wbi':device.state['wbi_mode']=0
    with pytest.raises(ValueError):Transaction(p,device,tmp_path/'journal.json').run('install',backups_verified=condition!='missing-backup',power_confirmed=condition!='missing-power')
    assert set(device.calls)<={'read'}


def test_calibration_changes_outside_owned_ranges_rejected(tmp_path):
    p=plan();new=bytearray(p.after_units[CP_KEYS[0]]);new[0]=1
    p.after_units[CP_KEYS[0]]=bytes(new)
    with pytest.raises(ValueError):Transaction(p,SimulationDevice(p),tmp_path/'journal.json')


def test_unknown_owned_unit_data_is_not_overwritten_on_restore(tmp_path):
    p=plan();device=SimulationDevice(p)
    raw=bytearray(device.state['units'][CP_KEYS[0]]);raw[0x1200+55]=127;device.state['units'][CP_KEYS[0]]=bytes(raw)
    with pytest.raises(ValueError):Transaction(p,device,tmp_path/'journal.json').run('restore',backups_verified=True,power_confirmed=True)
    assert device.calls==['read']


def test_live_backends_are_not_admitted_by_simulator(tmp_path):
    with pytest.raises(ValueError):Transaction(plan(),object(),tmp_path/'journal.json')
    assert operations.admission_status()['brick_free_guarantee'] is False
    assert operations.admission_status()['install_available'] is False
    with pytest.raises(RuntimeError):operations.require_released_write_backend()


def test_interrupted_live_backup_blocks_reauthentication(tmp_path):
    (tmp_path/'device-state.private.json').write_text(json.dumps({'phase':'READ_ONLY_BACKUP_STARTED'}))
    with patch('a7cr_reader.run_backup') as read,pytest.raises(RuntimeError):operations.backup_action(tmp_path)
    read.assert_not_called()
