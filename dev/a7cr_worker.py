"""Isolated official Python worker; no PyInstaller bootloader in emulation process."""
from pathlib import Path
import sys
import traceback


def main(argv):
    if len(argv) != 4 or argv[0] not in ('inspect', 'extract', 'build', 'backup', 'backup-with-driver', 'identify', 'install', 'restore'):
        return 2
    stage, references, workspace, log = argv
    with Path(log).open('w', encoding='utf-8', buffering=1) as output:
        sys.stdout = sys.stderr = output
        try:
            if stage in ('install', 'restore'):
                from a7cr_actions import write_worker
                write_worker(stage, Path(workspace), Path(log))
            elif stage == 'identify':
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
            traceback.print_exc()
            return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
