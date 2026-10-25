"""Host workflow tests: automatic sequencing, cancellation and download integrity."""
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import a7cr_easy as easy


def source(raw=b'official-test-data'):
    return ('ILCE-7CR_2.01', 'https://example.test/official', len(raw), hashlib.sha256(raw).hexdigest())


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_download_validates_size_and_hash_before_promoting(tmp_path):
    raw=b'official-test-data';events=[]
    with patch.object(easy.urllib.request,'urlopen',return_value=Response(raw)):
        target=easy.download_source(source(raw),tmp_path,threading.Event(),lambda *event:events.append(event))
    assert target.read_bytes()==raw
    assert not target.with_suffix('.DAT.part').exists()


def test_corrupt_download_stays_partial(tmp_path):
    raw=b'official-test-data'
    with patch.object(easy.urllib.request,'urlopen',return_value=Response(b'X'*len(raw))),pytest.raises(ValueError):
        easy.download_source(source(raw),tmp_path,threading.Event(),lambda *event:None)
    assert not (tmp_path/'ILCE-7CR_2.01.DAT').exists()
    assert (tmp_path/'ILCE-7CR_2.01.DAT.part').exists()


def test_cached_corrupt_file_is_preserved_not_redownloaded(tmp_path):
    p=tmp_path/'ILCE-7CR_2.01.DAT';p.write_bytes(b'broken')
    with patch.object(easy.urllib.request,'urlopen') as network,pytest.raises(ValueError):
        easy.download_source(source(),tmp_path,threading.Event(),lambda *event:None)
    network.assert_not_called();assert p.read_bytes()==b'broken'


def test_server_ignoring_range_cannot_append_wrong_data(tmp_path):
    part=tmp_path/'ILCE-7CR_2.01.DAT.part';part.write_bytes(b'off')
    with patch.object(easy.urllib.request,'urlopen',return_value=Response(b'official-test-data')),pytest.raises(ValueError):
        easy.download_source(source(),tmp_path,threading.Event(),lambda *event:None)
    assert part.read_bytes()==b'off'


def test_successful_range_resumes_at_exact_offset(tmp_path):
    raw=b'official-test-data';part=tmp_path/'ILCE-7CR_2.01.DAT.part';part.write_bytes(raw[:3])
    response=Response(raw[3:]);response.status=206;response.headers={'Content-Range':f'bytes 3-{len(raw)-1}/{len(raw)}'}
    with patch.object(easy.urllib.request,'urlopen',return_value=response) as network:
        result=easy.download_source(source(raw),tmp_path,threading.Event(),lambda *event:None)
    assert network.call_args.args[0].headers['Range']=='bytes=3-'
    assert result.read_bytes()==raw


def test_cancellation_prevents_download_commit(tmp_path):
    stop=threading.Event();stop.set()
    with patch.object(easy.urllib.request,'urlopen',return_value=Response(b'official-test-data')),pytest.raises(easy.Cancelled):
        easy.download_source(source(),tmp_path,stop,lambda *event:None)
    assert not (tmp_path/'ILCE-7CR_2.01.DAT').exists()


def prepare_fakes(tmp_path):
    def downloader(spec,folder,cancel,progress):
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(spec[0]+'.DAT');path.write_bytes(b'input');return path
    def stage(name,references,workspace,cancel):
        if name=='build':
            (workspace/'public-summary.json').write_text(json.dumps({'device_access':False,'installable_package':False}))
    return downloader,stage


def test_button_pipeline_runs_stages_and_never_claims_installation(tmp_path):
    downloader,stage=prepare_fakes(tmp_path);calls=[];events=[]
    def run(name,*args):
        calls.append(name);return stage(name,*args)
    with patch('a7cr_research.verify_references'),patch.object(easy.shutil,'disk_usage',return_value=type('Disk',(),{'free':9*1024**3})()):
        result=easy.prepare(tmp_path,tmp_path/'refs',threading.Event(),lambda *e:events.append(e),run,downloader)
    assert calls==['inspect','extract','build']
    assert result['phase']=='PREPARED_NOT_INSTALLED'
    assert result['camera_accessed'] is False and result['installation_available'] is False
    assert events[-1][0]=='complete'


def test_stage_failure_does_not_retry_or_announce_completion(tmp_path):
    downloader,_=prepare_fakes(tmp_path);calls=[];events=[]
    def fail(name,*args):
        calls.append(name);raise RuntimeError('stop')
    with patch('a7cr_research.verify_references'),patch.object(easy.shutil,'disk_usage',return_value=type('Disk',(),{'free':9*1024**3})()),pytest.raises(RuntimeError):
        easy.prepare(tmp_path,tmp_path/'refs',threading.Event(),lambda *e:events.append(e),fail,downloader)
    assert calls==['inspect']
    assert not any(e[0]=='complete' for e in events)
    assert list((tmp_path/'jobs').iterdir())


def test_second_process_cannot_start_simultaneous_job(tmp_path):
    with easy.single_job(tmp_path),pytest.raises((OSError,RuntimeError)):
        with easy.single_job(tmp_path):
            pytest.fail('second lock acquired')


def test_packaged_worker_uses_isolated_bundled_python(tmp_path,monkeypatch):
    monkeypatch.setattr(easy.sys,'frozen',True,raising=False)
    monkeypatch.setattr(easy.sys,'_MEIPASS',str(tmp_path),raising=False)
    command=easy.worker_command('build',tmp_path/'refs',tmp_path/'job',tmp_path/'log')
    assert command[:2]==[str(tmp_path/'worker/python.exe'),str(tmp_path/'worker/worker.py')]
    assert '--research-worker' not in command
    assert command[2]=='build'
