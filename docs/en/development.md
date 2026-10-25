# Development guide

[Home](../../README.md) · [简体中文](../zh-CN/development.md)

One overview for maintainers and AI agents. Read [AGENTS.md](../../AGENTS.md) first: it holds the rules that must not be broken. This page explains how the code fits together; for details, read the code it points to.

## Scope

- Camera: Sony ILCE-7CM2, firmware 2.01 only.
- Product: the 3.0 desktop app (`desktop.py`), on Windows 10/11 x64 and macOS 14+ Apple Silicon. Windows and macOS share the GUI, planners, camera writes and recovery. Only USB drivers, privilege elevation and media handling differ per host.
- Legacy command-line tools (`FL23.cmd`, `lut.py`, `recovery.py`, `shutter.py`, `vertical.py`, `a7look_*.py`, `raw_*.py`) remain for advanced use. Their backup-based workflows are separate from the desktop's backup-free direct operations.

## Repository map

| Path | Responsibility |
| --- | --- |
| `desktop.py` | Entry point: GUI, `--worker`, `--privileged-worker` (macOS), `--raw-profile-worker` (Lightroom), `--offline-self-test` |
| `src/desktopapp/` | GUI (`window.py`, `lookeditor.py`; colors and states in `theme.py`), jobs (`jobs.py`), worker dispatch (`worker.py`), one-click coordinator (`allinone.py`), dialog texts (`reloadguide.py`, `connectionguide.py`, `lookguide.py`, `completion.py`) |
| `src/desktopapp/actions/` | `catalog.py`: which actions touch the camera or need power confirmation. `look_compiler.py` and `sound.py`: adapters for offline conversion and the sound runtime |
| `src/desktopapp/platforms/` | Host-specific code: `usb_drivers.py` (Windows WinUSB via libwdi, UAC, uninstall), `windows_pnp.py`, `macos_authorization.py` (Terminal + sudo), `macos_media.py`. `drivers.py`/`macadmin.py` are compatibility aliases |
| `src/looks/looktool/` | Core engines: `direct.py` (backup-free transactions), `bundle.py` (combined plan), `factoryrecover.py`, `compilelook.py` (DCP/CUBE → `.a7look`), `rawauto.py`/`rawidentity.py` (Lightroom), plus legacy checkpoint/job modules |
| `src/camera_runtime/` | USB service session (`transport.py`), FL23 planner/assets, macOS host helpers (`macos_host.py`), vendored Sony-PMCA-RE |
| `src/shutter_sound/` | Sound conversion/volume, audition, persistent install; `native/` holds the AArch64 camera workers |
| `src/vertical/` | Vertical Display workers and portrait resources (**being rewritten**) |
| `src/fl23/` | Standalone legacy FL23 installer; its `native/` workers are reused by the engines |
| `plugins/A7LookAuto.lrplugin/` | Lightroom Classic auto-profile plugin |
| `calibration/` | Virtual 2.01 response model used by the compiler |
| `dev/desktop/` | Packaging (`build.py`, `desktop.spec`, `prepare_assets.py`, `prepare_presets.py`, `profilepack.py`) and the desktop test suite |
| `dev/shutter_sound/`, `dev/vertical/`, `dev/usb/` | Tests, emulators and research tools |
| `dev/release/` | Historical v2.x CLI release scripts and `check_publication.py` |
| `desktop-assets/` (ignored) | Distribution-only inputs: six default looks, paired profile library, Windows/macOS helpers |
| `build/`, `dist/` (ignored) | Caches, private evidence and build outputs |

## How a button becomes a camera write

1. **GUI** (`window.launch`). Write actions show a power/model confirmation; the request then carries `power_confirmed=True`. The UI thread never calls camera, driver or native-worker code.
2. **Job** (`jobs.new_job`). Creates `<data root>/runs/<UTC time>-<action>-<id>/` with `request.private.json` (schema `a7c2-desktop-job/1`) and `status.private.json`.
3. **Worker** (`desktop.py --worker`). A fresh child process for every job. On macOS, camera actions first re-run the same worker as root through a per-job Terminal sudo script. The worker writes `worker.log` and sets the status to `RUNNING`, then `SUCCEEDED` with `result`, or `FAILED` with `error`, `error_type` and the full traceback.
4. **Dispatch** (`worker.run`). Routes the action to an engine (see the table below).
5. **GUI completion** (`window.finished`). Reads the status and `result.phase`, then shows the matching dialog: cold reload, resource reload, sound notice or completion.

`SUCCEEDED` only means the worker returned. The phase says what happens next:

| Phase | Meaning |
| --- | --- |
| `AWAITING_RELOAD` / one-click `WAIT_RELOAD` | Written and fully read back; the user must cold-reload |
| `AWAITING_RESOURCE_RELOAD` / `WAIT_RESOURCE_RELOAD` | Names written, programs not yet; cold-reload, then continue |
| `LOADED` / `COMPLETE` | Verified after a new boot (`COMPLETE` also records the owner's physical check) |
| `NEEDS_REVIEW` | Stopped. Keep the evidence; never replay an unknown write |

Data root: `%LOCALAPPDATA%\A7C2Tools` on Windows, `~/Library/Application Support/A7C2Tools` on macOS. Override it with `A7C2_DESKTOP_DATA`. GUI settings are stored in `desktop.json`.

## Action catalog

| Action | Engine | Notes |
| --- | --- | --- |
| `setup_all` | `allinone.run` | One-click install/continue; calls the other actions in-process |
| `driver_scan`, `usb_prepare`, `driver_install`, `driver_uninstall`, `mac_unmount` | `platforms.usb_drivers` | Only `054C:0E8A` and `054C:0336`; existing WinUSB/libusbK/libusb0 bindings are kept |
| `connect`, `usb_stability` | `devicesession.Camera`, `usbcheck` | Read-only |
| `direct_factory_reset` (→ `direct_resume_factory` / `direct_verify`) | `looktool.direct` | Built-in factory reference; the GUI resolves pending transactions first |
| `direct_fl23_enable`, `direct_fl23_disable` | `looktool.direct` | |
| `direct_looks_import` (→ `direct_resume_looks`) | `lookbatch` + `looktool.direct` (`install_all`) | `operations`: `slot`, `action=write/restore`, `path`, optional `name` |
| `vertical_enable`, `vertical_disable` | `looktool.direct` (persistent) | Being rewritten |
| `sound_on`, `sound_off` | `looktool.direct` (`sound_enable/disable`) | |
| `sound_import`, `sound_volume` | `actions.sound` → `shutter_sound.sound_cli` | Computer only, no camera access |
| `sound_audition`, `sound_install`, `sound_verify`, `sound_restore_sound` | `actions.sound` | Camera actions |
| `compile_look` | `actions.look_compiler` → `looktool.compilelook` | Offline |
| `plugin_install` | `desktopapp.plugins` | Optional Lightroom plugin; bundles ExifTool |
| `fl23_*`, `look_*`, `restore*` | Legacy engines | Older backup-based flows, kept for history and CLI parity |

A replacement frontend only needs `jobs.new_job(action, arguments)` plus `jobs.worker_command(...)`, and must interpret phases as above. Keep the argument names used by `Window.common()` and the catalog.

## Core flows

**Direct transaction** (`looktool/direct.py`, `DirectJob`):
1. `plan()` is pure. It turns the camera's current bytes into target bytes and wanted settings, and changes only the regions each mode declares. `bundle.plan()` stacks several modes (FL23, looks, ST resets, sound, vertical) into one plan.
2. `run_direct()` order: capture current state → plan → save `PLANNED` → native preflight → names first, then program windows → full program readback → name resources → settings → WBI → `AWAITING_RELOAD`. Every step is journaled in `direct.private.json`.
3. If the read-only remount returns EBUSY after names are written, the job stops at `AWAITING_RESOURCE_RELOAD`. Programs are written in a fresh transaction after a verified new boot.
4. `verify_direct()` re-plans from current bytes and must find nothing left to change. It then checks RAM and saved settings, requires a changed `boot_id` (proof of a cold reload), and checks that loaded program spans match the disk. Factory reset also requires `/usr` to be mounted read-only.

**One-click install** (`allinone.py`, state in `setup.private.json`):
`READY → DRIVERS → PREPARED → INSTALLING → WAIT_RELOAD → (verify) → FINALIZING → COMPLETE`, with a `WAIT_RESOURCE_RELOAD` branch.
- `FINALIZING` adopts sound, installs the plugin and RAW mapping, and checks the persisted Vertical Display.
- After a failure, a child that failed before any write gets a fresh `install-retry-*` plan. Unknown results stop with their evidence intact.

**Factory restore:** the built-in reference `src/looks/assets/factory-regions-2.01.json` holds generic stock bytes only for the regions this project changes. It contains no full firmware and no body data. Bytes outside those regions, and body calibration, are preserved. Interrupted restores continue from current bytes (`factoryrecover.pending`).

**USB service entry** (`camera_runtime/transport.Session`):
1. Check the normal USB product string.
2. Authenticate.
3. Wait for `0336`.
4. Authenticate again.
5. Bind the body by property `00e70003` (hex-text serial digest).

If `0336` never appears and the camera returns to `0E8A`, the session cools down for 30 s and retries, up to 3 attempts. Nothing is sent to the camera after an authentication that never reached `0336`.

**Sound** (`src/shutter_sound/`): `media.py`/`native_codec.py` convert audio on the computer. `sound_cli.py` drives the workflow for both the desktop and `shutter.py`. `candidate.py`/`boot_sound.py`/`files_worker.py` build the fixed native changes. Audition and confirmation records are optional and never gate saving.

**Look conversion:** `compilelook.py` parses DCP LookTable/tone curves, or ST/sRGB CUBEs, and fits them to the native table with `calibration/a7c2-2.01.virtual.npz`. No Lightroom is needed. See [LUT formats](lut-formats.md) for supported inputs.

## Build and test

Use native 64-bit Python 3.11.

```text
python -m pip install --no-build-isolation -r desktop-requirements.txt pyinstaller==6.16.0 pytest==8.3.5
python -m pytest dev/desktop dev/shutter_sound/tests dev/vertical -q
python desktop.py
```

The root `conftest.py` blocks physical USB enumeration in every test. Other suites: `python -m unittest discover -s src/looks/tests`, `src/fl23/tests`, `src/camera_runtime/tests`, `dev/usb/tests`. Private fixtures (`A7C2_RESEARCH_FIXTURES`) are optional; without them those tests skip.

**Packaging:**
1. Prepare the ignored distribution assets: `python dev/desktop/prepare_assets.py`. Use `--preset-archive <zip>` to import the six defaults and the paired library from an exported pack or a Windows build; use `--export-presets <zip>` to create such a pack.
2. Build: `python dev/desktop/build.py`.

Outputs:
- **Windows:** `dist/A7C2Tools-Windows-x64.zip`. It contains `A7C2Tools.exe`, `_internal/`, `Profiles/` (paired library), both READMEs and `LICENSE`.
- **macOS (Apple Silicon only):** `dist/A7C2Tools-macOS-arm64.dmg`. The paired library lives inside the app (`desktop-assets/profile-library-v1`).
- `profilepack.py` refuses to package unless all 11 Fujifilm and 4 Ricoh color pairs are present. Monochrome pairs are allowed only if they meet the independent error limits (MAE ≤3/255, P95 ≤12/255).

CI: `.github/workflows/desktop-preview.yml` builds on native Windows and `macos-15` runners and uploads artifacts without publishing a Release. Updating `dev/desktop/native-build-request.json` triggers an Apple Silicon build. Keystone/Unicorn build from source on ARM64 (Xcode CLT, CMake 3.31.6); Zig is 0.13.0.

Packages have no Developer ID signature. Offline tests, packaged self-tests and byte readback do not prove camera boot safety, colors or sound. Physical acceptance is recorded separately in the [changelog](changelog.md).

## Documentation policy

Keep the documentation small:

| File | Audience |
| --- | --- |
| `README.md` / `README.zh-CN.md` | Users: download, step-by-step guide, troubleshooting |
| `docs/*/lut-formats.md` | Users: supported LUT/DCP inputs |
| `docs/*/changelog.md` | Everyone: version history and test records |
| `docs/*/notices.md` | Everyone: sources and licenses |
| `docs/*/development.md` | Maintainers and AI: this page |
| `AGENTS.md` | AI: rules that must not be broken |

Every English page has a Simplified Chinese counterpart at the same path; keep them in sync. Don't add per-feature guides or research notes. Read the code, and record decisions in the changelog or commit messages instead. Historical documents remain in git history.

## A7CR offline research fork

This fork adds `a7cr_research.py` without changing the desktop/legacy model guards or A7C II payloads. It uses pinned sources from lzh2016zzz (A7CR specification), injectiver (candidate builder), the historical A7C II recipe, and DavidBuchanan314/fwtool.py. Original notices remain local; no firmware, donor data, body backup or write payload is published.

The Windows adapter uses Zig 0.13.0's bundled LLVM assembler directly (`clang -cc1as`, Thumb v7 / Cortex-A9), replacing the hard-coded Apple clang path. Expected-byte/placement checks, instruction emulation, file lengths and complete-file inverse checks remain active. CRC/local hashes are not Sony signature authentication.

From the repository root, with Python 3.12 and Git:

```powershell
python -m venv .venv-a7cr
.\.venv-a7cr\Scripts\python.exe -m pip install -r dev/a7cr-requirements.txt
.\.venv-a7cr\Scripts\python.exe a7cr_research.py bootstrap --references build/a7cr/references
```

Obtain official A7CR 2.01 and Alpha 1 II 4.00 updates yourself. Save separate `BODYDATA.DAT` files under `build/a7cr/workspace/private/firmware/ILCE-7CR_2.01/` and `build/a7cr/workspace/private/firmware/ILCE-1M2_4.00/`. The workspace must exist. Never put the donor update on an A7CR memory card.

```powershell
.\.venv-a7cr\Scripts\python.exe a7cr_research.py inspect --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe a7cr_research.py extract --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe a7cr_research.py build --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe -m pytest dev/desktop/test_a7cr_research.py -q
```

Private outputs stay in the ignored workspace; a repeated build preserves an existing candidate and stops. Bootstrap fetches pinned research references, not Sony firmware. This CLI never opens USB, authenticates, changes drivers, writes programs/settings/WBI, or produces a flashable DAT. AP/CP candidates still need a fresh same-body parameter merge, verified duplicate backups, Windows transport/native transactions, staged readback, and physical restoration acceptance. Do not use them with the A7C II installer.

`dev/a7cr-validation.json` records 32 source checks, 2304 emulated cases and exact AP/CP inverse round trips on Windows, plus 20 new tests. No physical camera was accessed; boot, preview, color and recovery remain unverified.

## One-button Windows preparation app

`a7cr_easy_gui.py` is an independent Tk GUI, not the A7C II desktop frontend. It automatically downloads and size/hash-checks the two official inputs, creates a private job, and runs inspect/extract/build in worker processes. Partial downloads resume only with an exact HTTP range response; corrupt files, failed jobs and interrupted candidates are preserved. Cancellation terminates only offline workers. Startup opens no camera and starts no download. A process lock prevents concurrent preparation. Completion is always PREPARED_NOT_INSTALLED; This earlier preparation-only stage was superseded by the backup development section below.

The earlier packaged `A7CR-Preparation.exe` needs no Python or Git. It embeds pinned, integrity-checked reference sources and compiler/runtime files, never Sony update data or derived candidate programs. Data is stored under LOCALAPPDATA/A7CRTools, overridable with A7CR_TOOLS_DATA for tests. First-run downloads total about 2.3 GB; at least 7 GiB free is required. The source builder is `python dev/build_a7cr_easy.py` after installing `dev/a7cr-requirements.txt` and `pyinstaller==6.16.0`. Test both A7CR test modules under dev/desktop.

The GUI keeps its normal Windows CFG-enabled bootloader. Native emulation runs in the bundled official Python 3.12.10 embedded interpreter, isolated with python312._pth, to avoid the documented Unicorn/PyInstaller CFG incompatibility. No Windows security setting is changed.

## A7CR device development

The backup development app separates three concerns: host USB driver preparation, read-only service backup, and a simulation-only install/restore transaction model. Startup and offline preparation perform no USB enumeration. Identification reads descriptors and Windows SetupAPI metadata only; serial-bearing PnP instance paths are excluded from the identification receipt.

`a7cr_reader.py` adapts injectiver's MIT A7CR reader for Windows (license retained under docs/licenses). It selects only AP/CP program paths and reads checksum-valid live/saved Backup snapshots. It never uploads native code or writes camera files/settings. It rejects wrong model/interface/body binding, short reads and unknown results without retrying authentication. Two fresh independent backup directories contain programs, both settings snapshots and four protected CP units; every file is read back and verified again by `a7cr_operations.verify_checkpoint`. These are current-state scope backups, not certified stock or complete eMMC/NOR images. Two directories on one disk are not separate-disk disaster recovery.

`a7cr_drivers.py` does nothing until explicitly confirmed. It admits only one present 054C:0E86 or 054C:0336 node, preserves usable drivers, verifies a pinned helper, records before/intent/after state, and requires usable post-install driver plus the same device instance. Unknown helper results persistently block repetition. The 0336 callback observes re-enumeration and prepares its host driver once, without repeating authentication. The user must handle Windows UAC; no signature enforcement or Windows security setting is disabled. The helper is the same third-party libwdi build referenced by the parent project. This driver path and live backups still require hardware acceptance; offline tests are not that evidence.

`a7cr_transaction_model.py` accepts only its in-memory SimulationDevice. It tests CP-program -> CP-parameters -> AP-program -> AP-bindings -> WBI ordering, selected-range inverses, later unrelated-setting preservation, lost acknowledgements and mismatching readback. It refuses real backends. `WRITE_BACKEND_RELEASED` remains false and the two GUI write buttons remain disabled. Do not turn an editable JSON flag or a successful backup into permission to flash.

Developer native checks: `python dev/check_a7cr_native.py --workspace build/a7cr-native-check`. The first run obtains hash-pinned historical MIT worker sources. It compiles AArch64 code through Zig's LLVM frontend/linker and runs 20 synthetic syscall cases, including short reads, partial writes, inverse stages, read-only remount and WBI ordering. These are historical reference-worker checks, not a production Windows camera worker, boot simulation or physical power-loss recovery proof.

Run the six `dev/desktop/test_a7cr_*.py` modules for host tests. Physical acceptance must separately cover Win11 service entry/exit, body backup comparison, native preflight, staged writes/full readback, new boot, preview/new photos/playback and actual restoration. No arbitrary CP-damage or power-loss rescue has been demonstrated. A request for zero brick risk cannot be met by these software checks.
