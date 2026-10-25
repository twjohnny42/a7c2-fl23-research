# Adapted from injectiver/a7cr-creative-look-tools at 48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f.
# MIT attribution: docs/licenses/injectiver-MIT.txt.
"""Body-bound A7CR FL23 install/inspection/rollback; never an official update.

Native workers and Backup protocol are adapted from QAQLeonard's MIT-licensed
a7c2-fl23-research. A7CR offsets/order come from lzh2016zzz/a7cr-fl23-research.
No automatic retry, resume, firmware download, raw-flash write or reboot.
Windows adaptation omits case-specific continuation commands.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import sys

from a7cr_preflight import CheckSession, TEMP, sha, decode_program, decode_wbi, NativeCheckRejected
from a7cr_reader import ROOT, backup_sections, private_owner, CP_UNITS
from a7cr_reader import save_json

CP_KEYS = (0x02A40001, 0x02A4000D)
AP_KEYS = (0x02CF1287, 0x02CF1289)
OWNED = TEMP | frozenset(("a7cr.stage.so",))
PATHS = {"AP": "/usr/lib/appFw.so", "CP": "/cp/cpapp-b.bin"}
AP_DIAGNOSTIC = 0x02CF0B6E


def durable_json(path, value):
    save_json(path, value)
    private_owner(path)
    if os.name == "nt":
        return  # save_json fsyncs the temporary file before atomic replacement.
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_private(path, raw):
    with path.open("xb") as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    private_owner(path)


class Package:
    def __init__(self, workspace, *, verify_backups=True):
        if Path(workspace).is_symlink():raise ValueError("Installation job cannot be a symbolic link")
        self.root = workspace.resolve()
        if self.root.is_symlink():
            raise ValueError("Installation job cannot be a symbolic link")
        self.manifest = json.loads((self.root / "package.private.json").read_text())
        if (self.manifest["schema"] != "a7cr-fl23-local-installation/2"
                or self.manifest.get("model") != "ILCE-7CR" or self.manifest.get("version") != "2.01"):
            raise ValueError("Unknown installation package")
        import re
        self.binding = self.manifest["body_binding"]
        if any(not re.fullmatch('[0-9a-f]{64}', self.binding.get(k, ''))
               for k in ('usb_serial_sha256', 'ap_serial_sha256')):
            raise ValueError("Missing body binding")
        self.audit = self.manifest["audit"]
        sources=self.manifest['sources']
        if verify_backups:
            from a7cr_operations import verify_checkpoint
            verify_checkpoint(sources['first_backup'],sources['second_backup'],expected_body=self.binding)
        original_names={'original-AP':'appFw.so','original-CP':'cpapp-b.bin',
                        **{f'original-{key:08x}':f'unit-{key:08x}.private.bin' for key in CP_UNITS}}
        self.blobs = {}
        for name, spec in self.manifest['files'].items():
            if name in original_names:
                if spec.get('storage')!='backup2' or spec['file']!=original_names[name]:
                    raise ValueError('Original programs/settings must refer directly to verified backup2')
                path=Path(sources['second_backup'])/original_names[name]
            else:
                path=(self.root/spec['file']).resolve()
                if path.parent!=(self.root/'payload').resolve() or path.is_symlink():
                    raise ValueError('Payload escapes private job')
            raw=path.read_bytes()
            if len(raw)!=spec['bytes']:raise ValueError('Prepared payload length changed: '+name)
            # Code integrity for small generated workers, never a firmware fingerprint gate.
            if name in ('program_check','wbi_check','cp_stage','ap_stage','wbi_stage'):
                if not 0<len(raw)<=65536 or sha(raw)!=spec['sha256']:
                    raise ValueError('Generated native code changed')
            self.blobs[name]=raw
        required = ({f'{d}-{kind}' for d in ('original', 'candidate') for kind in PATHS}
                    | {f'original-{key:08x}' for key in CP_UNITS}
                    | {f'candidate-{key:08x}' for key in CP_KEYS}
                    | {'program_check', 'wbi_check', 'cp_stage', 'ap_stage', 'wbi_stage'})
        if set(self.blobs) != required or set(self.audit.get('programs', {})) != set(PATHS):
            raise ValueError("Unreviewed or incomplete installation payload")
        self.programs = {direction: {kind: self.blobs[f"{direction}-{kind}"] for kind in PATHS}
                         for direction in ("original", "candidate")}
        self.units = {direction: {key: self.blobs[f"{direction}-{key:08x}"] for key in CP_KEYS}
                      for direction in ("original", "candidate")}
        self.protected = {key: self.blobs[f"original-{key:08x}"] for key in CP_UNITS if key not in CP_KEYS}
        self.bindings = {"original": dict.fromkeys(AP_KEYS, b"\0"),
                         "candidate": dict(zip(AP_KEYS, (b"\x0a", b"\x0b")))}
        from a7cr_transaction_model import Plan
        Plan(self.binding['ap_serial_sha256'], self.programs['original'], self.programs['candidate'],
             self.units['original'], self.units['candidate'], self.bindings['original'],
             self.bindings['candidate']).validate()
        for key, value in self.protected.items():
            if len(value) != CP_UNITS[key]:
                raise ValueError("Protected CP unit length differs")
        self.states = {"original": self.programs["original"],
                       "cp_only": {"AP": self.programs["original"]["AP"], "CP": self.programs["candidate"]["CP"]},
                       "installed": self.programs["candidate"]}
        for kind, spec in self.audit["programs"].items():
            old, new = self.programs["original"][kind], self.programs["candidate"][kind]
            if len(old) != spec["bytes"] or len(new) != len(old):
                raise ValueError("Per-job program integrity differs")
            forward, inverse = bytearray(old), bytearray(new)
            end = 0
            for window in spec["windows"]:
                at, length = window["offset"], window["bytes"]
                before, after = bytes.fromhex(window["before_hex"]), bytes.fromhex(window["after_hex"])
                if (at < end or at < 0 or length <= 0 or length > 4096 or at+length > len(old)
                        or len(before) != length or len(after) != length
                        or old[at:at+length] != before or new[at:at+length] != after):
                    raise ValueError("Per-job fixed window differs")
                forward[at:at+length] = after; inverse[at:at+length] = before; end = at+length
            if bytes(forward) != new or bytes(inverse) != old:
                raise ValueError("Full program forward/inverse mismatch")


class Session(CheckSession):
    def __init__(self, package, record, service_driver_setup=None):
        super().__init__(record, mac_capture=False, service_driver_setup=service_driver_setup)
        self.expected_usb_serial_sha256 = package.binding["usb_serial_sha256"]
        self.package = package

    def __enter__(self):
        super().__enter__()
        try:
            raw=self.call('READ_AP_BODY_SERIAL',self.camera.readBackup,0x00E70003)
            if len(raw)!=4 or sha(raw.hex().encode('ascii'))!=self.package.binding['ap_serial_sha256']:
                raise ValueError('AP body identity differs; no native staging or persistent writes')
            self.record('ap_body_bound_after_service_authentication')
            return self
        except BaseException:
            self.healthy=False
            self.usb.util.dispose_resources(self.device)
            raise

    def call(self, label, function, *args):
        if not self.healthy:
            raise ValueError("Previous result unknown; all further device commands stopped")
        self.record("device_operation_intent", operation=label)
        self.backend.reset()
        try:
            result = function(*args)
        except BaseException:
            self.healthy = False
            raise
        self.record("device_operation_returned", operation=label)
        return result

    def select_temp(self, name):
        if name not in OWNED:
            raise ValueError("Unowned temporary path")
        status, _ = self.temp_command(15, b"\0/tmp/jiritsu/"+name.encode()+b"\0")
        if status:
            self.healthy = False
            raise ValueError("Temporary selection failed")

    def write_cp(self, key, direction):
        if key not in CP_KEYS or direction not in self.package.units:
            raise ValueError("Unreviewed CP target")
        raw = self.package.units[direction][key]
        if len(raw) != CP_UNITS[key]:
            raise ValueError("CP payload length differs")
        return self.call(f"WRITE41_CP_{key:08x}", self.camera._sendAdjustControlPacket,
                         0x603, 0x41, struct.pack("<II", key, 1)+raw)

    def write_ap(self, key, direction):
        if key not in AP_KEYS or direction not in self.package.bindings:
            raise ValueError("Unreviewed AP target")
        return self.call(f"WRITE2_AP_{key:08x}", self.camera.writeBackup,
                         key, self.package.bindings[direction][key])

    def save_group(self, group):
        if group not in (0x02A4, 0x02CF):
            raise ValueError("Unreviewed save subsystem")
        return self.call(f"SAVE3_{group:04x}", self.camera.saveBackup, group)


def decode_stage(raw, kind, mode, package):
    if mode not in (1, 2) or kind not in ("cp_stage", "ap_stage") or len(raw) != 240:
        raise ValueError("Unknown program stage/mode/ABI")
    v = struct.unpack("<8s4I7q4Q64s64s", raw)
    if v[:3] != (b"A7IRP002", 2, mode):
        raise ValueError("Native program stage ABI/mode differs")
    first, last = ("original", "cp_only") if kind == "cp_stage" else ("cp_only", "installed")
    if mode == 2:
        first, last = last, first
    count = len(package.audit["programs"]["CP" if kind == "cp_stage" else "AP"]["windows"])
    ok = (v[3] == 9 and v[4] == count and all(v[i] == 0 for i in range(5, 11))
          and all(v[i] > 0 for i in (11, 12, 13))
          and v[14:16] == tuple(len(package.programs["original"][chip]) for chip in PATHS)
          and v[16] == bytes(64) and v[17] == bytes(64))
    return {"ok": ok, "mode": mode, "phase": v[3], "writes": v[4], "status": v[5],
            "rollback_status": v[6], "readonly_status": v[8], "cleanup_status": v[9]}


def decode_wbi_drop(raw):
    if len(raw) != 192:
        raise ValueError("WBI report length differs")
    v = struct.unpack("<8sII9q16s16s32s32sII", raw)
    if v[:3] != (b"A7WBR001", 1, 1):
        raise ValueError("WBI report ABI/mode differs")
    ok = (all(v[i] == 0 for i in (3, 4, 5, 8, 10, 11)) and v[6] > 0
          and v[7] == v[9] == 1 and v[16:] == (1, 0)
          and v[12].rstrip(b"\0") == b"2\n" and v[13].rstrip(b"\0") == b"0\n"
          and v[14].rstrip(b"\0") == v[15].rstrip(b"\0") == b"*wbi4\n")
    return {"ok": ok, "status": v[3], "drop_write": v[7], "mode_write": v[9],
            "fresh_drop_log": bool(v[16]), "image_mode": v[13].rstrip(b"\0").decode("ascii", "replace")}


def invoke(camera, package, kind, mode, folder, record):
    if kind in ("program_check", "wbi_check") and mode != 0:
        raise ValueError("Check worker cannot be used to install")
    if kind in ("cp_stage", "ap_stage") and mode not in (1, 2):
        raise ValueError("Unknown program transition")
    if kind == "wbi_stage" and mode != 1:
        raise ValueError("Unknown WBI transition")
    if kind not in ("program_check", "wbi_check", "cp_stage", "ap_stage", "wbi_stage"):
        raise ValueError("Unknown native worker")
    wbi = kind.startswith("wbi")
    request = "a7cr.wbi.request" if wbi else "a7cr.install.request"
    result = "a7cr.wbi.result" if wbi else "a7cr.install.result"
    magic = b"A7WBI001" if wbi else b"A7INS001"
    blob = package.blobs[kind]
    expected = {"a7cr.stage.so": blob, "libJit.so": blob, request: struct.pack("<8sII", magic, mode, 0)}
    for name in sorted(OWNED):
        if camera.read_temp(name, True) is not None:
            raise ValueError("Previous native state remains; preserve it and inspect before any invocation")
    camera.check_preload()
    for name in ("a7cr.stage.so", request):
        camera.write_temp(name, expected[name], expected)
        if camera.read_temp(name) != expected[name]:
            raise ValueError("Native staging readback differs")
    record("native_invocation_intent", worker=kind, mode=mode, sha256=sha(blob))
    try:
        camera.write_temp("libJit.so", blob, expected)
        raw = camera.read_temp(result)
        store_private(folder / f"{kind}-mode{mode}.private.bin", raw)
        if kind == "program_check":
            parsed = decode_program(raw, package.audit)
        elif kind == "wbi_check":
            parsed = decode_wbi(raw)
        elif kind == "wbi_stage":
            parsed = decode_wbi_drop(raw)
        else:
            parsed = decode_stage(raw, kind, mode, package)
    except BaseException:
        camera.healthy = False
        raise
    record("native_invocation_returned", worker=kind, mode=mode, report=parsed)
    if not parsed['ok']:
        camera.healthy=False
        raise NativeCheckRejected('Native stage failed; preserve evidence, no cleanup/retry/inverse')
    expected[result] = raw
    for name in ("libJit.so", result, request, "a7cr.stage.so"):
        camera.remove_temp(name, expected)
    record("native_cleanup_verified", worker=kind, mode=mode)
    if not parsed["ok"]:
        raise NativeCheckRejected(f"Native stage did not complete: {kind}: {parsed}")
    return parsed


def preserved(before, after, expected):
    """Compare writable AP settings and four protected CP units.

    Scope follows upstream technical/integration.md and devicesettings.py.
    Full snapshots are retained. Runtime groups (including 02770015) are
    diagnostic observations, never write targets or install gates.
    """
    allowed = {(0, key) for key in AP_KEYS} | {(1, key) for key in CP_KEYS}
    if not set(expected) <= allowed:
        raise ValueError("Expected write is outside this job's settings scope")
    before_diag, after_diag = before[0].get(AP_DIAGNOSTIC), after[0].get(AP_DIAGNOSTIC)
    if before_diag is not None or after_diag is not None:
        if (before_diag is None or after_diag is None or before_diag.attr != after_diag.attr
                or len(before_diag.data) != len(after_diag.data)):
            raise ValueError("Diagnostic AP field layout changed")
    scope = lambda state: [
        {key: value for key, value in state[0].items() if key >> 16 == 0x02CF and key != AP_DIAGNOSTIC},
        {key: state[1][key] for key in CP_UNITS},
    ]
    if any(key not in state[1] for state in (before, after) for key in CP_UNITS):
        raise ValueError("Protected CP unit missing")
    scoped_before, scoped_after = scope(before), scope(after)
    if any(key not in scoped_before[chip] or key not in scoped_after[chip] for chip, key in expected):
        raise ValueError("Expected target is missing")
    count = 0
    for chip in (0, 1):
        if set(scoped_before[chip]) != set(scoped_after[chip]):
            raise ValueError("Protected setting property set changed")
        for key, old in scoped_before[chip].items():
            new = scoped_after[chip][key]
            if (chip, key) in expected:
                if new.data != expected[chip, key] or new.attr != old.attr:
                    raise ValueError(f"Target readback differs: {key:08x}")
            elif (old.data, old.attr) == (new.data, new.attr):
                count += 1
            else:
                raise ValueError(f"Unrelated property changed: chip{chip}/{key:08x}")
    diagnostic = []
    for chip in (0, 1):
        for key in sorted(set(before[chip]) | set(after[chip])):
            if key in scoped_before[chip] or key in scoped_after[chip]:
                continue
            old, new = before[chip].get(key), after[chip].get(key)
            if old is None or new is None or (old.data, old.attr) != (new.data, new.attr):
                diagnostic.append({"chip": chip, "key": f"{key:08x}",
                                   "before_sha256": sha(old.data) if old is not None else None,
                                   "after_sha256": sha(new.data) if new is not None else None})
    return {"preserved_properties": count, "scope": "AP:02cf except 0b6e; CP:unit00,01,02,0d",
            "diagnostic_changes_not_written": diagnostic}


class Job:
    def __init__(self, package, attempt):
        self.package, self.attempt = package, attempt
        self.events = []

    def record(self, phase, **details):
        self.events.append({"phase": phase, "utc": datetime.now(timezone.utc).isoformat(), **details})
        durable_json(self.attempt / "events.private.json", self.events)

    def phase(self, phase, **detail):
        durable_json(self.package.root / "state.private.json", {"phase": phase, "attempt": self.attempt.name,
                                                               "utc": datetime.now(timezone.utc).isoformat(), **detail})
        self.record(phase, **detail)

    def snapshots(self, camera, tag):
        result = {}
        for memory, name in ((True, "memory"), (False, "flash")):
            raw = camera.snapshot(memory)
            store_private(self.attempt / f"{tag}-{name}.private.bin", raw)
            sections = backup_sections(raw)
            serial = sections[0][0x00E70003].data
            if len(serial) != 4 or sha(serial.hex().encode("ascii")) != self.package.binding["ap_serial_sha256"]:
                raise ValueError("AP backup body binding differs")
            for key, original in self.package.protected.items():
                if sections[1][key].data != original:
                    raise ValueError("Protected CP calibration unit changed")
            result[name] = sections
        return result

    def body(self, camera):
        if camera.identity["usb_serial_sha256"] != self.package.binding["usb_serial_sha256"]:
            raise ValueError("Connected USB body differs from the backup")

    def program_state(self, camera, tag, expected=None):
        possible = set(self.package.states)
        for chip, path in PATHS.items():
            raw = camera.read_program(path)
            matching = {state for state, programs in self.package.states.items() if raw == programs[chip]}
            possible &= matching
            if not matching or (expected is not None and raw != self.package.states[expected][chip]):
                store_private(self.attempt / f"{tag}-unexpected-{chip}.private.bin", raw)
                raise ValueError("Complete program readback differs: " + chip)
            self.record("complete_program_byte_readback", tag=tag, chip=chip, bytes=len(raw), sha256=sha(raw))
        if len(possible) != 1:
            raise ValueError("Unsupported AP/CP program combination")
        state = possible.pop()
        self.record("program_state_verified", tag=tag, state=state)
        return state

    def targets(self, snapshots, cp_direction, ap_direction):
        for sections in snapshots.values():
            for key, raw in self.package.units[cp_direction].items():
                if sections[1][key].data != raw:
                    raise ValueError("CP target parameters differ")
            for key, raw in self.package.bindings[ap_direction].items():
                if sections[0][key].data != raw:
                    raise ValueError("AP slot binding differs")

    def known_settings(self, snapshots):
        for sections in snapshots.values():
            for key in CP_KEYS:
                from a7cr_transaction_model import merge_owned, owned_ranges
                merge_owned(sections[1][key].data, self.package.units["original"][key],
                            self.package.units["candidate"][key], owned_ranges(key), "restore")
            for key in AP_KEYS:
                if sections[0][key].data not in (self.package.bindings["original"][key], self.package.bindings["candidate"][key]):
                    raise ValueError("AP binding state is outside this job's original/candidate")

    def saveable(self, before, group):
        chip, keys = (1, CP_KEYS) if group == 0x02A4 else (0, AP_KEYS)
        memory, flash = before["memory"][chip], before["flash"][chip]
        for key, value in memory.items():
            if (key >> 16 == group and key not in keys and key != AP_DIAGNOSTIC
                    and (value.data, value.attr) != (flash[key].data, flash[key].attr)):
                raise ValueError("Unrelated unsaved subsystem setting; refusing SAVE3")

    def settings(self, camera, before, group, direction, tag):
        cp = group == 0x02A4
        chip = 1 if cp else 0
        desired = self.package.units[direction] if cp else self.package.bindings[direction]
        # Saving a subsystem must not unexpectedly commit other live settings.
        memory, flash = before["memory"][chip], before["flash"][chip]
        self.saveable(before, group)
        changed = False
        for key, target in desired.items():
            current = camera.required_cp_read(key) if cp else camera.call(f"READ_AP_{key:08x}", camera.camera.readBackup, key)
            if current != memory[key].data:
                raise ValueError("Direct parameter read differs from the fresh snapshot")
            if current != target:
                self.record("parameter_write_intent", key=f"{key:08x}", direction=direction)
                if cp:
                    camera.write_cp(key, direction)
                    got = camera.required_cp_read(key)
                else:
                    camera.write_ap(key, direction)
                    got = camera.call(f"READ_AP_{key:08x}", camera.camera.readBackup, key)
                if got != target:
                    camera.healthy = False
                    raise ValueError("Parameter write readback differs")
                self.record("parameter_write_readback_verified", key=f"{key:08x}", direction=direction)
                changed = True
            if flash[key].data != target:
                changed = True
        if changed:
            self.record("subsystem_save_intent", subsystem=f"{group:04x}")
            camera.save_group(group)
        after = self.snapshots(camera, tag)
        expected = {(chip, key): value for key, value in desired.items()}
        for name in ("memory", "flash"):
            report = preserved(before[name], after[name], expected)
            self.record("parameter_preservation_verified", source=name, subsystem=f"{group:04x}", **report)
        return after

    def native(self, camera, kind, mode):
        print(f"运行原生阶段：{kind}，模式 {mode}", flush=True)
        return invoke(camera, self.package, kind, mode, self.attempt, self.record)

    def install(self, camera):
        self.body(camera)
        self.program_state(camera, "initial", "original")
        initial = self.snapshots(camera, "initial")
        self.targets(initial, "original", "original")
        self.saveable(initial, 0x02A4); self.saveable(initial, 0x02CF)
        for key in CP_KEYS:
            if camera.required_cp_read(key) != self.package.units["original"][key]:
                raise ValueError("CP pre-write source differs")
        self.native(camera, "wbi_check", 0)
        self.phase("CP_PROGRAM_WRITE_INTENT")
        self.native(camera, "cp_stage", 1)
        self.program_state(camera, "after-cp", "cp_only")
        self.phase("CP_PROGRAM_VERIFIED")
        current = self.settings(camera, initial, 0x02A4, "candidate", "after-cp-settings")
        self.phase("CP_PARAMETERS_SAVED_VERIFIED")
        self.finish_ap(camera, current)

    def finish_ap(self, camera, current):
        self.native(camera, "ap_stage", 1)
        self.program_state(camera, "after-ap", "installed")
        self.phase("AP_PROGRAM_VERIFIED")
        current = self.settings(camera, current, 0x02CF, "candidate", "after-ap-settings")
        self.targets(current, "candidate", "candidate")
        self.phase("PROGRAMS_AND_SAVED_SETTINGS_VERIFIED")
        self.native(camera, "wbi_stage", 1)
        self.phase("WBI_INVALIDATED_VERIFIED")

    def inspect(self, camera, verify=False):
        self.body(camera)
        state = self.program_state(camera, "inspection", "installed" if verify else None)
        current = self.snapshots(camera, "inspection")
        self.known_settings(current)
        if verify:
            self.targets(current, "candidate", "candidate")
        found = {name: {f"{key:08x}": sha(sections[1][key].data) for key in CP_KEYS}
                 for name, sections in current.items()}
        temporaries = []
        for name in sorted(OWNED):
            raw = camera.read_temp(name, True)
            if raw is not None:
                store_private(self.attempt / (name+".preserved.private.bin"), raw)
                temporaries.append(name)
        self.record("inspection_verified", program_state=state, cp_units=found, preserved_temporary_files=temporaries,
                    loaded_programs_or_photo_behavior_proven=False)
        return state

    def restore(self, camera):
        self.body(camera)
        state = self.program_state(camera, "before-restore")
        current = self.snapshots(camera, "before-restore")
        self.known_settings(current)
        from a7cr_transaction_model import merge_owned, owned_ranges
        restored = {}
        for key in CP_KEYS:
            values = [merge_owned(current[name][1][key].data, self.package.units["original"][key],
                                  self.package.units["candidate"][key], owned_ranges(key), "restore")
                      for name in ("memory", "flash")]
            if values[0] != values[1]:
                raise ValueError("Unsaved non-project CP settings would be committed by restoration")
            restored[key] = values[0]
        self.package.units["restore"] = restored
        self.package.bindings["restore"] = dict(self.package.bindings["original"])
        self.saveable(current, 0x02A4); self.saveable(current, 0x02CF)
        if state == "original":
            try:
                self.targets(current, "restore", "restore")
            except ValueError:
                pass
            else:
                self.record("original_state_verified_no_mutation_needed")
                return False
        self.native(camera, "wbi_check", 0)
        self.phase("RESTORATION_STARTED")
        current = self.settings(camera, current, 0x02CF, "restore", "restore-ap-settings")
        if state == "installed":
            self.native(camera, "ap_stage", 2)
            self.program_state(camera, "restore-ap", "cp_only")
            state = "cp_only"
        current = self.settings(camera, current, 0x02A4, "restore", "restore-cp-settings")
        if state == "cp_only":
            self.native(camera, "cp_stage", 2)
        self.program_state(camera, "restore-final", "original")
        self.targets(current, "restore", "restore")
        self.phase("ORIGINAL_PROGRAMS_AND_TARGET_SETTINGS_VERIFIED")
        self.native(camera, "wbi_stage", 1)
        self.phase("RESTORE_WBI_INVALIDATED_VERIFIED")
        return True
