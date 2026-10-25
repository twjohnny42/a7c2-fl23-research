from pathlib import Path
import sys,json
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import a7cr_drivers as drivers


def device(pid='0E86',service='USBSTOR'):
    return {'id':'USB\\VID_054C&PID_'+pid+'\\SYNTHETIC','service':service}


@pytest.mark.parametrize('pid',['0ea9','0e8a','9999','0e86 & bad','0336;anything'])
def test_unreviewed_driver_pids_cannot_generate_commands(tmp_path,pid):
    with pytest.raises(ValueError):drivers.driver_arguments(tmp_path/'helper.exe',pid,tmp_path)


def test_command_is_fixed_winusb_target_without_shell(tmp_path):
    command=drivers.driver_arguments(tmp_path/'helper.exe','0e86',tmp_path/'driver')
    assert command[1:7]==['--vid','0x054C','--pid','0x0e86','--type','0']


def test_owner_confirmation_required_before_helper(tmp_path):
    runner=Mock();setup=drivers.DriverSetup(tmp_path,inventory=lambda:[device()],runner=runner)
    with pytest.raises(PermissionError):setup.ensure('0e86')
    runner.assert_not_called()


def test_existing_winusb_driver_does_not_request_elevation(tmp_path):
    runner=Mock();setup=drivers.DriverSetup(tmp_path,inventory=lambda:[device(service='WinUSB')],runner=runner)
    assert setup.ensure('0e86') is False
    runner.assert_not_called()


@pytest.mark.parametrize('rows',[[],[device(),device()]])
def test_absent_or_multiple_targets_never_run_helper(tmp_path,rows):
    runner=Mock();setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:rows,runner=runner)
    with pytest.raises(ValueError):setup.ensure('0e86')
    runner.assert_not_called()


def test_unknown_helper_outcome_cannot_be_retried_after_restart(tmp_path):
    runner=Mock(side_effect=TimeoutError('unknown'));setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:[device()],runner=runner)
    with patch.object(drivers,'helper_path',return_value=tmp_path/'helper.exe'),pytest.raises(TimeoutError):setup.ensure('0e86')
    second=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:[device()],runner=runner)
    with pytest.raises(RuntimeError):second.ensure('0e86')
    assert runner.call_count==1


def test_driver_change_requires_positive_post_install_verification(tmp_path):
    inventory=Mock(side_effect=[[device()],[device(service='WinUSB')]])
    runner=Mock();setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=inventory,runner=runner)
    with patch.object(drivers,'helper_path',return_value=tmp_path/'helper.exe'):
        assert setup.ensure('0e86') is True
    assert json.loads((tmp_path/'result-0e86.private.json').read_text())['phase']=='DRIVER_VERIFIED'
    assert runner.call_count==1


def test_failed_post_install_verification_is_not_success(tmp_path):
    runner=Mock();setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:[device()],runner=runner,readiness_seconds=0)
    with patch.object(drivers,'helper_path',return_value=tmp_path/'helper.exe'),pytest.raises(RuntimeError):setup.ensure('0e86')
    assert not (tmp_path/'result-0e86.private.json').exists()


def test_preexisting_service_blocks_new_normal_authentication(tmp_path):
    runner=Mock();setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:[device(),device('0336')],runner=runner)
    with pytest.raises(ValueError):setup.prepare_normal()
    runner.assert_not_called()


def test_windows_problem_code_rejects_driver_change(tmp_path):
    row=device();row['problem']=43
    runner=Mock();setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=lambda:[row],runner=runner)
    with pytest.raises(ValueError):setup.ensure('0e86')
    runner.assert_not_called()


def test_changed_device_instance_after_helper_is_not_adopted(tmp_path):
    before=device();after=device(service='WinUSB');after['id']+='-OTHER-BODY'
    setup=drivers.DriverSetup(tmp_path,owner_confirmed=True,inventory=Mock(side_effect=[[before],[after]]),runner=Mock())
    with patch.object(drivers,'helper_path',return_value=tmp_path/'helper.exe'),pytest.raises(RuntimeError):setup.ensure('0e86')
