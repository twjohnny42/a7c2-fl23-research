"""Explicit camera-only Windows WinUSB preparation. Never writes camera firmware.

No helper execution on import/startup. Owner confirmation is required, each
identity must be currently present and unique, and no authentication is retried.
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HELPER_SHA256 = '27b6d9847d874674b7c412af124acf64b0b1efa397e3bbea8eab9950a6a08ee0'
PIDS = {'0e86': 'Sony A7CR - Mass Storage', '0336': 'Sony A7CR - Service'}
READY_DRIVERS = {'winusb', 'libusbk', 'libusb0'}


def rows():
    from a7cr_windows_pnp import devices
    return devices()


def match_devices(inventory, pid):
    if pid not in PIDS:
        raise ValueError('Only Sony A7CR 0E86 and its 0336 service identity are supported')
    pattern = re.compile(r'^USB\\VID_054C&PID_' + pid + r'(?:[\\&]|$)', re.I)
    return [row for row in inventory if pattern.match(row.get('id', ''))]


def driver_arguments(executable, pid, folder):
    if pid not in PIDS:
        raise ValueError('Unreviewed driver target')
    return [str(executable), '--vid', '0x054C', '--pid', '0x' + pid,
            '--type', '0', '--name', PIDS[pid], '--dest', str(folder), '--log', '0']


def helper_path():
    root = Path(__file__).resolve().parent
    candidate = root / 'drivers/wdi-simple.exe'
    if not candidate.exists() and not getattr(sys, 'frozen', False):
        candidate = root / 'build/a7cr/driver-helper/wdi-simple.exe'
    if not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != HELPER_SHA256:
        raise ValueError('Bundled driver helper is missing or changed; no driver action')
    return candidate


class DriverLaunchNotStarted(OSError):
    pass


def elevated_run(command):
    if os.name != 'nt':
        raise OSError('Windows only')
    class Info(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    info = Info();info.cbSize = ctypes.sizeof(info);info.fMask = 0x40
    info.lpVerb = 'runas';info.lpFile = command[0];info.lpParameters = subprocess.list2cmdline(command[1:]);info.nShow = 0
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(Info)];shell.ShellExecuteExW.restype = wintypes.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise DriverLaunchNotStarted(ctypes.get_last_error(), 'Administrator driver process was not started')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD];kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)];kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE];kernel.CloseHandle.restype = wintypes.BOOL
    try:
        wait = kernel.WaitForSingleObject(info.hProcess, 0xffffffff)
        if wait != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value:
            raise RuntimeError('Driver helper failed: ' + str(code.value))
    finally:
        kernel.CloseHandle(info.hProcess)


class DriverSetup:
    def __init__(self, root, *, owner_confirmed=False, inventory=rows, runner=elevated_run, readiness_seconds=15):
        self.root = Path(root)
        self.owner_confirmed = owner_confirmed
        self.inventory, self.runner = inventory, runner
        self.attempted = set()
        self.normal_prepared = False
        self.readiness_seconds = readiness_seconds

    def ensure(self, pid):
        from a7cr_reader import save_json
        if pid not in PIDS:
            raise ValueError('Unreviewed PID')
        inventory = self.inventory()
        matches = match_devices(inventory, pid)
        if len(matches) != 1:
            raise ValueError('Exactly one currently present camera identity is required')
        if matches[0].get('problem', 0):
            raise ValueError('Windows reports a device problem; no driver helper started')
        if str(matches[0].get('service', '')).lower() in READY_DRIVERS:
            return False
        if not self.owner_confirmed:
            raise PermissionError('Explicit owner permission for Windows driver preparation is required')
        if pid in self.attempted:
            raise RuntimeError('Prior helper outcome requires inspection; no retry')
        intent = self.root / ('intent-' + pid + '.private.json')
        receipt = self.root / ('result-' + pid + '.private.json')
        if intent.exists():
            import json
            status = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
            if status.get('phase') not in ('DRIVER_VERIFIED', 'HELPER_NOT_STARTED'):
                raise RuntimeError('Previous driver helper outcome unknown; inspect instead of retrying')
        executable = helper_path()
        self.root.mkdir(parents=True, exist_ok=True)
        save_json(self.root / ('before-' + pid + '.private.json'), inventory)
        self.attempted.add(pid)
        save_json(self.root / ('intent-' + pid + '.private.json'), {'pid': pid, 'helper_sha256': HELPER_SHA256,
                  'firmware_or_settings_written': False})
        try:
            self.runner(driver_arguments(executable, pid, self.root / ('driver-' + pid)))
        except DriverLaunchNotStarted:
            save_json(receipt, {'phase': 'HELPER_NOT_STARTED', 'pid': pid})
            raise
        deadline = time.monotonic() + self.readiness_seconds
        while True:
            after = self.inventory()
            final = match_devices(after, pid)
            ready = (len(final) == 1 and not final[0].get('problem', 0)
                     and str(final[0].get('service', '')).lower() in READY_DRIVERS)
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(0.2)  # Only observe driver readiness; never repeat installation.
        save_json(self.root / ('after-' + pid + '.private.json'), after)
        if not ready or final[0]['id'].casefold() != matches[0]['id'].casefold():
            raise RuntimeError('Driver/instance verification failed; no authentication')
        save_json(receipt, {'phase': 'DRIVER_VERIFIED', 'pid': pid, 'firmware_or_settings_written': False})
        return True

    def prepare_normal(self):
        if match_devices(self.inventory(), '0336'):
            raise ValueError('An existing service device must be inspected before starting another session')
        self.ensure('0e86')
        self.normal_prepared = True

    def service_if_present(self):
        if not self.normal_prepared:
            raise ValueError('Normal camera identity was not prepared')
        if match_devices(self.inventory(), '0336'):
            self.ensure('0336')
            return True
        return False
