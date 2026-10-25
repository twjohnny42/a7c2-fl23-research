from pathlib import Path
import sys,io,json,hashlib
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import a7cr_reader as reader
import a7cr_operations as operations


@pytest.fixture
def checkpoint(tmp_path):
    serial=bytes.fromhex('12345678')
    keys={0x02A40000:2,0x02A40001:3,0x02A40002:4,0x02A4000D:5}
    units={key:bytes([i+1])*length for i,(key,length) in enumerate(keys.items())}
    parsed=[{0x00E70003:SimpleNamespace(data=serial)}, {key:SimpleNamespace(data=raw) for key,raw in units.items()}]
    device=SimpleNamespace(identity={'model':'ILCE-7CR','usb_serial_sha256':hashlib.sha256(b'usb').hexdigest()},
                           read_program=Mock(return_value=b'abc'),snapshot=Mock(return_value=b'snapshot'))
    context=Mock();context.__enter__=Mock(return_value=device);context.__exit__=Mock(return_value=False)
    first,second=tmp_path/'first',tmp_path/'second'
    with patch.object(reader,'Reader',return_value=context),patch.object(reader,'PROGRAMS',{'/usr/lib/appFw.so':(3,'appFw.so'),'/cp/cpapp-b.bin':(3,'cpapp-b.bin')}),patch.object(reader,'CP_UNITS',keys),patch.object(reader,'backup_sections',return_value=parsed),patch('sys.stdout',io.StringIO()):
        reader.run_backup(first,second,'2.01')
        yield first,second,device


def test_both_backups_are_independently_reconstructable(checkpoint):
    first,second,_=checkpoint
    result=operations.verify_checkpoint(first,second)
    assert result['files']==8 and result['copies']==2
    assert result['physical_restoration_proven'] is False
    assert result['phase']=='BACKUP_VERIFIED'


@pytest.mark.parametrize('target',['appFw.so','cpapp-b.bin','unit-02a40001.private.bin','settings-flash.private.bin'])
def test_corruption_of_either_copy_stops_admission(checkpoint,target):
    first,second,_=checkpoint
    (second/target).write_bytes(b'broken')
    with pytest.raises(ValueError):operations.verify_checkpoint(first,second)


def test_complete_but_wrong_body_backup_rejected(checkpoint):
    first,second,_=checkpoint
    with pytest.raises(ValueError):operations.verify_checkpoint(first,second,expected_body={'usb_serial_sha256':'f'*64})


def test_missing_flash_copy_rejected(checkpoint):
    first,second,_=checkpoint
    (second/'settings-flash.private.bin').rename(second/'preserved-flash.bin')
    with pytest.raises(ValueError):operations.verify_checkpoint(first,second)


def test_missing_manifest_cannot_be_adopted(checkpoint):
    first,second,_=checkpoint
    (second/'backup.private.json').rename(second/'incomplete.private.json')
    with pytest.raises(OSError):operations.verify_checkpoint(first,second)


def test_same_directory_is_not_two_backups(checkpoint):
    first,_,_=checkpoint
    with pytest.raises(ValueError):operations.verify_checkpoint(first,first)


def test_snapshot_serial_mismatch_rejected_even_if_manifests_and_files_match(checkpoint):
    first,second,_=checkpoint
    bad=[{0x00E70003:SimpleNamespace(data=b'abcd')},{}]
    with patch.object(reader,'backup_sections',return_value=bad),pytest.raises(ValueError):operations.verify_checkpoint(first,second)


def test_wrong_model_manifest_never_authorizes_restore(checkpoint):
    first,second,_=checkpoint
    for folder in (first,second):
        p=folder/'backup.private.json';manifest=json.loads(p.read_text());manifest['identity']['model']='ILCE-7CM2';p.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):operations.verify_checkpoint(first,second)
