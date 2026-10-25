# Adapted from injectiver/a7cr-creative-look-tools at 48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f.
# MIT attribution: docs/licenses/injectiver-MIT.txt.
"""A7CR check-only preparation adapted from the two supplied repositories.

This CLI has no install/restore command. It stages only the two mode-0-only
workers, verifies unchanged complete programs/settings, and reads only the CP
units required by the A7CR notes (unit01/unit0d). Native temporary files are
created; camera firmware, saved settings and WBI state are not changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from datetime import datetime, timezone

from a7cr_reader import ROOT, Reader, backup_sections, private_owner, CP_UNITS
from a7cr_reader import save_json

TEMP = frozenset(("libJit.so", "a7cr.check-program.so", "a7cr.check-wbi.so",
                  "a7cr.install.request", "a7cr.install.result", "a7cr.wbi.request", "a7cr.wbi.result"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class NativeCheckRejected(ValueError):
    pass


class CheckSession(Reader):
    def temp_command(self, command, payload):
        if command not in (15, 16, 17, 18):
            raise ValueError("Unsupported temporary operation")
        result, raw = self.call(f"PREFLIGHT_TEMP_{command}", self.channel.sendSenserPacket,
                                0x40, struct.pack("<HH", 0x1001, command)+payload)
        if result or len(raw) < 4:
            self.healthy = False
            raise ValueError("Malformed temporary file response")
        return struct.unpack_from("<I", raw)[0], raw[4:]

    def select_temp(self, name):
        if name not in TEMP:
            raise ValueError("Unowned temporary file")
        status, _ = self.temp_command(15, b"\0/tmp/jiritsu/"+name.encode()+b"\0")
        if status:
            self.healthy = False
            raise ValueError("Temporary path selection failed")

    def read_temp(self, name, optional=False):
        self.select_temp(name)
        status, raw = self.temp_command(17, b"\0\xff\0")
        if optional and status == 0x10000006:
            return None
        if status:
            self.healthy = False
            raise ValueError("Temporary read failed")
        return raw

    def write_temp(self, name, raw, expected):
        if name not in expected or expected[name] != raw or not raw or len(raw) > 65536:
            raise ValueError("Unreviewed temporary contents")
        self.select_temp(name)
        status, _ = self.temp_command(16, b"\0\xff\0"+raw)
        if status:
            self.healthy = False
            raise ValueError("Temporary staging/invocation failed")

    def remove_temp(self, name, expected):
        actual = self.read_temp(name, True)
        if actual is None:
            return
        if actual != expected[name]:
            raise ValueError("Preserve unexpected temporary contents")
        self.select_temp(name)
        status, _ = self.temp_command(18, b"\0\xff")
        if status or self.read_temp(name, True) is not None:
            self.healthy = False
            raise ValueError("Temporary cleanup incomplete")

    def check_preload(self):
        path = b"\0/tmp/jiritsu/../../etc/ld.so.preload\0"
        status, _ = self.temp_command(15, path)
        if status:
            self.healthy = False
            raise ValueError("Preload inspection selection failed")
        status, raw = self.temp_command(17, b"\0\xff\0")
        if status == 0x10000006:
            return
        if status or raw:
            raise ValueError("Existing/unknown preload state requires inspection")

    def required_cp_read(self, key):
        sizes = {0x02A40001: 8192, 0x02A4000D: 28672}
        if key not in sizes:
            raise ValueError("Only the two documented FL23 units can be probed")
        previous_budget = getattr(self.backend, "operation_budget_seconds", 180)
        self.backend.operation_budget_seconds = 20
        try:
            raw = self.call(f"REQUIRED_CP_READ40_{key:08x}", self.camera._sendAdjustControlPacket,
                            0x603, 0x40, struct.pack("<II", key, 1))
        finally:
            self.backend.operation_budget_seconds = previous_budget
        if len(raw) != sizes[key]:
            self.healthy = False
            raise ValueError("Required CP unit length differs")
        return raw


def decode_program(raw, audit):
    if len(raw) != 240:
        raise ValueError("Unexpected program report length")
    values = struct.unpack("<8s4I7q4Q64s64s", raw)
    if values[:3] != (b"A7IRP002", 2, 0) or values[4] != 0:
        raise ValueError("Unexpected native mode/ABI/write count")
    ok = (values[3] == 9 and values[5] == 0 and all(values[index] == 0 for index in (6, 7, 8, 9, 10))
          and all(values[index] > 0 for index in (11, 12, 13)) and values[16] == bytes(64)
          and values[17] == bytes(64)
          and values[14:16] == tuple(audit["programs"][kind]["bytes"] for kind in ("AP", "CP")))
    return {"ok": ok, "phase": values[3], "writes": values[4], "status": values[5],
            "readonly_status": values[8], "cleanup": values[9], "euid": values[10]}


def decode_wbi(raw):
    if len(raw) != 192:
        raise ValueError("Unexpected WBI report length")
    values = struct.unpack("<8sII9q16s16s32s32sII", raw)
    if values[:3] != (b"A7WBR001", 1, 0) or any(values[index] for index in (7, 8, 9, 10)):
        raise ValueError("Unexpected WBI mode/ABI/write count")
    ok = (values[3] == 0 and values[4] == 0 and values[5] == 0 and values[6] > 0
          and values[11] == 0 and values[13] == bytes(16) and values[15] == bytes(32)
          and values[16:] == (0, 0)
          and values[12].rstrip(b"\0") == b"2\n" and values[14].rstrip(b"\0") == b"*wbi4\n")
    return {"ok": ok, "status": values[3], "euid": values[5],
            "image_mode": values[12].rstrip(b"\0").decode("ascii", "replace"),
            "image_dev": values[14].rstrip(b"\0").decode("ascii", "replace"), "writes": 0}


def invoke_check(camera, kind, blob, workspace, record, audit):
    if kind == "program_check":
        stage, request, result, magic = "a7cr.check-program.so", "a7cr.install.request", "a7cr.install.result", b"A7INS001"
        decode = lambda raw: decode_program(raw, audit)
    elif kind == "wbi_check":
        stage, request, result, magic = "a7cr.check-wbi.so", "a7cr.wbi.request", "a7cr.wbi.result", b"A7WBI001"
        decode = decode_wbi
    else:
        raise ValueError("Only check-only workers may be invoked")
    request_bytes = struct.pack("<8sII", magic, 0, 0)
    expected = {stage: blob, request: request_bytes, "libJit.so": blob}
    for name in (stage, request, result, "libJit.so"):
        if camera.read_temp(name, True) is not None:
            raise ValueError("Existing native state; no new invocation")
    camera.check_preload()
    for name in (stage, request):
        record("native_staging_intent", worker=kind, name=name)
        camera.write_temp(name, expected[name], expected)
        if camera.read_temp(name) != expected[name]:
            raise ValueError("Staged native bytes differ")
    record("native_check_invocation_intent", worker=kind, mode=0, sha256=sha(blob))
    try:
        camera.write_temp("libJit.so", blob, expected)
        raw = camera.read_temp(result)
        path = workspace / (kind+".result.private.bin")
        path.write_bytes(raw); private_owner(path)
        parsed = decode(raw)
    except BaseException:
        camera.healthy = False
        raise
    expected[result] = raw
    record("native_check_returned", worker=kind, report=parsed)
    for name in ("libJit.so", result, request, stage):
        camera.remove_temp(name, expected)
    record("native_check_cleanup_verified", worker=kind)
    if not parsed["ok"]:
        raise NativeCheckRejected(f"{kind} did not pass: {parsed}")
    return parsed


def compare_settings(before, after):
    for key in CP_UNITS:
        if before[1][key] != after[1][key]:
            raise ValueError("A protected CP unit changed during checks")
    select = lambda state: {key: (value.data, value.attr) for key, value in state[0].items()
                            if key >> 16 == 0x02CF and key != 0x02CF0B6E}
    if select(before) != select(after):
        raise ValueError("A writable look setting changed during checks")
