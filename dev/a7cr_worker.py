"""Isolated official Python worker; no PyInstaller bootloader in emulation process."""
from pathlib import Path
import sys
import traceback


def main(argv):
    if len(argv) != 4 or argv[0] not in ('inspect', 'extract', 'build'):
        return 2
    stage, references, workspace, log = argv
    with Path(log).open('w', encoding='utf-8', buffering=1) as output:
        sys.stdout = sys.stderr = output
        try:
            import a7cr_research
            a7cr_research.run(stage, Path(references), Path(workspace))
            return 0
        except BaseException:
            traceback.print_exc()
            return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
