# Adapted from injectiver/a7cr-creative-look-tools at 48f3cb6cb02a1ad1da29f12d7ca97bdad1cb310f.
# MIT attribution: docs/licenses/injectiver-MIT.txt.
"""Adapt upstream fixed-window workers to the captured A7CR programs.

The native implementation is from QAQLeonard/a7c2-fl23-research, MIT.
Generated Sony-derived headers and binaries stay under private storage.
Compilation is not authorization to install, and this script never opens USB.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from elftools.elf.elffile import ELFFile
from a7cr_reader import save_json
ROOT = Path(__file__).resolve().parent

UPSTREAM = ROOT / "a7cr_native"
OUT = ROOT / "private/native-workers"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def array(raw):
    return "{"+",".join(f"0x{x:02x}" for x in raw)+"}"


def manifest_header(before, after, sizes, windows, guard_windows=()):
    lines = ["/* Private edited-byte manifest. Whole-file hashes are not native admission gates. */",
             "static const u64 file_sizes[2]="+"{"+",".join(str(s) for s in sizes)+"};"]
    for index, window in enumerate(windows):
        old, new = bytes.fromhex(window["before_hex"]), bytes.fromhex(window["after_hex"])
        if not old or len(old) != len(new) or len(old) > 4096:
            raise ValueError("Invalid native window size")
        lines.extend([f"static const u8 old_{index}[]={array(old)};",
                      f"static const u8 new_{index}[]={array(new)};"])
    rows = [f"{{{w['offset']}UL,{w['bytes']},{w['chip']},old_{i},new_{i}}}" for i, w in enumerate(windows)]
    lines.extend(["static const struct Window windows[]={"+",".join(rows)+"};",
                  f"static const u32 window_count={len(windows)};"])
    for i,w in enumerate(guard_windows):
        raw=bytes.fromhex(w['before_hex'])
        if not raw or len(raw)!=w['bytes'] or len(raw)>4096:raise ValueError('Invalid dependency guard')
        lines.append(f'static const u8 guard_{i}[]={array(raw)};')
    rows=[f"{{{w['offset']}UL,{w['bytes']},{w['chip']},guard_{i},guard_{i}}}" for i,w in enumerate(guard_windows)]
    lines.append('static const struct Window guards[]={'+(','.join(rows) if rows else '{0}')+'};')
    lines.append(f'static const u32 guard_count={len(rows)};')
    return "\n".join(lines)+"\n"


def source(kind, read_only=False):
    name = "installer.c" if kind == "program" else "wbi.c"
    text = (UPSTREAM / name).read_text().replace("a7c2.", "a7cr.")
    # The same upstream implementation is used for install and inverse;
    # a separate check-only binary rejects every nonzero requested mode.
    if read_only:
        text = text.replace("r.mode=req.mode;", "r.mode=req.mode; if(req.mode) {r.status=-22;return;}", 1)
        if kind == "program":
            text = text.replace("if(!req.mode) {r.phase=9;return;}", "r.phase=9;return; /* Check-only: mutation code below is unreachable. */", 1)
        else:
            text = text.replace("if(!req.mode)return;", "return; /* Check-only: no retained-image mutation path. */", 1)
    text = text.replace("work();", "if(r.cleanup)r.status=r.cleanup;else work();")
    return "/* Derived from QAQLeonard/a7c2-fl23-research; MIT, see LICENSE.txt. */\n"+text


def compile_worker(folder, filename, text, header=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "LICENSE.txt").write_bytes((UPSTREAM / "LICENSE").read_bytes())
    (folder / "sha256.h").write_bytes((UPSTREAM / "sha256.h").read_bytes())
    if header is not None:
        (folder / "patch_manifest.h").write_text(header)
    src = folder / (filename+".c")
    src.write_text(text)
    dest = folder / (filename+".so")
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(folder.parent / "zig-global-cache"),
               ZIG_LOCAL_CACHE_DIR=str(folder / "zig-cache"))
    command = [sys.executable, "-m", "ziglang", "cc", "-target", "aarch64-linux-gnu",
               "-shared", "-fPIC", "-O2", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
               "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-nostdlib",
               "-Wl,--no-undefined", "-Wl,-Bsymbolic", "-Wl,-z,noexecstack", "-o", str(dest), str(src)]
    from a7cr_native_compile import portable_compile
    portable_compile(command, env=env)
    raw = dest.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Worker exceeds the reviewed Jiritsu staging limit")
    with dest.open("rb") as stream:
        elf = ELFFile(stream)
        if elf["e_machine"] != "EM_AARCH64" or elf["e_type"] != "ET_DYN":
            raise ValueError("Wrong worker architecture or ELF type")
        loads=[segment for segment in elf.iter_segments() if segment['p_type']=='PT_LOAD']
        if not loads:raise ValueError('Worker has no mapped load extent')
        ordered=sorted(loads,key=lambda segment:segment['p_vaddr'])
        for i,segment in enumerate(ordered):
            if (segment['p_filesz']>segment['p_memsz'] or segment['p_offset']+segment['p_filesz']>len(raw)
                    or segment['p_flags'] & 3 == 3):
                raise ValueError('Worker load/file/BSS bounds or writable executable mapping differ')
            if i and ordered[i-1]['p_vaddr']+ordered[i-1]['p_memsz']>segment['p_vaddr']:
                raise ValueError('Overlapping worker-owned load extents')
        def mapped(address,size=1,flags=0,file_backed=False):
            return any(segment['p_flags'] & flags == flags and segment['p_vaddr']<=address
                       and address+size<=segment['p_vaddr']+(segment['p_filesz'] if file_backed else segment['p_memsz'])
                       for segment in loads)
        for section in elf.iter_sections():
            if section['sh_type']=='SHT_NOBITS' and section['sh_size'] and section['sh_flags'] & 2:
                if not mapped(section['sh_addr'],section['sh_size'],2):raise ValueError('BSS exceeds worker-owned writable mappings')
        dynamic = elf.get_section_by_name(".dynamic")
        if any(tag.entry.d_tag == "DT_NEEDED" for tag in dynamic.iter_tags()):
            raise ValueError("Unexpected dynamic library dependency")
        symbols = list(elf.get_section_by_name(".dynsym").iter_symbols())
        if any(s.name and s["st_shndx"] == "SHN_UNDEF" for s in symbols):
            raise ValueError("Unexpected unresolved worker symbol")
        init = next(s["st_value"] for s in symbols if s.name == "_init")
        dt_init = next(t.entry.d_val for t in dynamic.iter_tags() if t.entry.d_tag == "DT_INIT")
        entry=next(s for s in symbols if s.name=='_init')
        if init != dt_init or not entry['st_size'] or not mapped(init,entry['st_size'],1,True):
            raise ValueError("Loader constructor entry differs or escapes mapped executable bytes")
        if any(t.entry.d_tag=='DT_INIT_ARRAYSZ' and t.entry.d_val for t in dynamic.iter_tags()):
            raise ValueError('Additional constructor entries could replay a worker')
        relocations = [r for section in elf.iter_sections() if section["sh_type"] == "SHT_RELA"
                       for r in section.iter_relocations()]
        if any(r['r_info_type']!=1027 or r['r_info_sym']
               or not mapped(r['r_offset'],8) or not mapped(r['r_addend']) for r in relocations):
            raise ValueError("Only local ELF RELATIVE relocations are allowed")
        if any(segment["p_type"] == "PT_GNU_STACK" and segment["p_flags"] & 1 for segment in elf.iter_segments()):
            raise ValueError("Executable worker stack")
    return {"file": str(dest.resolve()), "bytes": len(raw), "sha256": sha(raw),
            "source_sha256": sha(text.encode()), "constructor": init,
            "relative_relocations": len(relocations), "dynamic_dependencies": []}
