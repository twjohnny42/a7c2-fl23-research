"""A7CR offline research boundaries; no physical camera operations."""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import a7cr_research as research


def record(model='ILCE-7CR'):
    version, model_id = research.MODELS[model]
    return dict(model=model, version=version, fdat_version=version,
                fdat_model=model_id, outer_crc_valid=True, fdat_header_crc_valid=True)


@pytest.mark.parametrize('model', ['ILCE-7CR', 'ILCE-1M2'])
def test_expected_firmware_identity(model):
    research.validate_model_record(record(model), model)


@pytest.mark.parametrize('key,value', [
    ('model', 'ILCE-7CM2'), ('fdat_model', '0x40030023'),
    ('version', '2.00'), ('fdat_version', '2.00'),
    ('outer_crc_valid', False), ('fdat_header_crc_valid', False),
])
def test_wrong_model_version_or_container_is_rejected(key, value):
    data = record(); data[key] = value
    with pytest.raises(ValueError):
        research.validate_model_record(data, 'ILCE-7CR')


def test_line_endings_do_not_change_reference_identity():
    assert research.text_digest(b'a\r\nb\r\n') == research.text_digest(b'a\nb\n')
    assert research.text_digest(b'a\nb\n') != research.text_digest(b'a\nc\n')


def test_changed_reference_stops_before_parser_or_compiler(tmp_path):
    relative = next(iter(research.REFERENCES))
    target = tmp_path / relative
    target.parent.mkdir(parents=True); target.write_bytes(b'changed')
    with patch.object(research.subprocess, 'run') as process, pytest.raises(ValueError):
        research.verify_references(tmp_path)
    process.assert_not_called()


def test_existing_mismatched_reference_is_never_overwritten(tmp_path):
    relative = next(iter(research.REFERENCES))
    target = tmp_path / relative
    target.parent.mkdir(parents=True); target.write_bytes(b'keep evidence')
    with patch.object(research.urllib.request, 'urlopen') as network, pytest.raises(ValueError):
        research.bootstrap(tmp_path)
    network.assert_not_called()
    assert target.read_bytes() == b'keep evidence'


def test_assembler_is_bounded_and_uses_thumb_cortex_a9(tmp_path, monkeypatch):
    monkeypatch.setenv('ZIG_LOCAL_CACHE_DIR', str(tmp_path))
    command = ['/Library/Developer/CommandLineTools/usr/bin/clang', '-target',
               'armv7-none-eabi', '-mthumb', '-c', 'input.S', '-o', 'output.o']
    with patch.object(research.subprocess, 'run', return_value='ok') as run:
        assert research.portable_assembler(command, check=True) == 'ok'
    actual = run.call_args.args[0]
    assert actual == [sys.executable, '-m', 'ziglang', 'clang', '-cc1as',
                      '-triple', 'thumbv7-none-eabi', '-target-cpu', 'cortex-a9',
                      '-filetype', 'obj', '-o', 'output.o', 'input.S']
    assert run.call_args.kwargs['check'] is True
    assert Path(run.call_args.kwargs['env']['TEMP']).is_relative_to(tmp_path)


@pytest.mark.parametrize('command', [
    ['powershell', 'write-camera'],
    ['/Library/Developer/CommandLineTools/usr/bin/clang', '-target', 'aarch64-linux-gnu'],
    ['/Library/Developer/CommandLineTools/usr/bin/clang', '-target', 'armv7-none-eabi', '-other'],
])
def test_unexpected_compiler_command_never_runs(command):
    with patch.object(research.subprocess, 'run') as run, pytest.raises(ValueError):
        research.portable_assembler(command)
    run.assert_not_called()


@pytest.mark.parametrize('command', ['install', 'restore', 'backup', 'identify'])
def test_research_cli_has_no_device_actions(command):
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        research.main([command, '--references', 'build/references'])


def test_private_outputs_cannot_be_placed_in_tracked_source():
    repo = Path(research.__file__).resolve().parent
    for path in (repo, repo / 'src/new-private-data'):
        with pytest.raises(ValueError):
            research.private_location(path)
    assert research.private_location(repo / 'build/a7cr') == repo / 'build/a7cr'
