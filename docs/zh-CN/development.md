# 开发说明

[首页](../../README.zh-CN.md) · [English](../en/development.md)

这一篇是给维护者和 AI 的唯一总览。先读 [AGENTS.md](../../AGENTS.md)：那里是不能违反的规则。本页讲代码怎么组织、怎么协作；细节请直接看本页指向的代码。

## 范围

- 相机：只支持 Sony ILCE-7CM2、固件 2.01。
- 产品：3.0 桌面程序（`desktop.py`），支持 Windows 10／11 x64 和 macOS 14+ Apple Silicon。两个平台共用界面、规划、相机写入和恢复；只有 USB 驱动、提权和存储卷处理因系统而异。
- 旧版命令行工具（`FL23.cmd`、`lut.py`、`recovery.py`、`shutter.py`、`vertical.py`、`a7look_*.py`、`raw_*.py`）保留给高级用户。它们基于备份的流程，与桌面版不需要备份的直接操作相互独立。

## 目录

| 路径 | 职责 |
| --- | --- |
| `desktop.py` | 入口：界面、`--worker`、`--privileged-worker`（macOS）、`--raw-profile-worker`（Lightroom）、`--offline-self-test` |
| `src/desktopapp/` | 界面（`window.py`、`lookeditor.py`；颜色和状态样式在 `theme.py`）、任务（`jobs.py`）、worker 分发（`worker.py`）、一键安装协调（`allinone.py`）、弹窗文字（`reloadguide.py`、`connectionguide.py`、`lookguide.py`、`completion.py`） |
| `src/desktopapp/actions/` | `catalog.py`：哪些动作会访问相机、哪些需要确认供电。`look_compiler.py`、`sound.py`：离线转换和声音运行时的适配器 |
| `src/desktopapp/platforms/` | 系统相关代码：`usb_drivers.py`（Windows 用 libwdi 装 WinUSB、UAC 提权、卸载）、`windows_pnp.py`、`macos_authorization.py`（终端 + sudo）、`macos_media.py`。`drivers.py`、`macadmin.py` 只是兼容别名 |
| `src/looks/looktool/` | 核心引擎：`direct.py`（不需要备份的事务）、`bundle.py`（组合计划）、`factoryrecover.py`、`compilelook.py`（DCP／CUBE → `.a7look`）、`rawauto.py`／`rawidentity.py`（Lightroom），以及旧版检查点／任务模块 |
| `src/camera_runtime/` | USB 服务会话（`transport.py`）、FL23 规划与资源、macOS 主机辅助（`macos_host.py`）、内置 Sony-PMCA-RE |
| `src/shutter_sound/` | 声音转换／音量、试听、持久安装；`native/` 是 AArch64 相机 worker |
| `src/vertical/` | 竖屏显示 worker 与竖屏资源（**正在重写**） |
| `src/fl23/` | 旧版独立 FL23 安装器；其 `native/` worker 被各引擎复用 |
| `plugins/A7LookAuto.lrplugin/` | Lightroom Classic 自动识别插件 |
| `calibration/` | 编译器使用的 2.01 虚拟响应模型 |
| `dev/desktop/` | 打包（`build.py`、`desktop.spec`、`prepare_assets.py`、`prepare_presets.py`、`profilepack.py`）与桌面测试 |
| `dev/shutter_sound/`、`dev/vertical/`、`dev/usb/` | 测试、模拟器和研究工具 |
| `dev/release/` | v2.x 命令行时代的发布脚本，以及 `check_publication.py` |
| `desktop-assets/`（忽略） | 仅用于发行的输入：默认六款、配对配置库、Windows／macOS 辅助程序 |
| `build/`、`dist/`（忽略） | 缓存、私有证据和构建产物 |

## 一次点击如何变成相机写入

1. **界面**（`window.launch`）：写入类动作先弹出供电和机型确认，请求里带上 `power_confirmed=True`。界面线程从不直接调用相机、驱动或原生 worker。
2. **任务**（`jobs.new_job`）：创建 `<数据目录>/runs/<UTC 时间>-<动作>-<id>/`，写入 `request.private.json`（schema `a7c2-desktop-job/1`）和 `status.private.json`。
3. **Worker**（`desktop.py --worker`）：每个任务一个新子进程。macOS 上的相机动作会先通过每个任务单独生成的终端 sudo 脚本，以 root 重新运行同一个 worker。Worker 写 `worker.log`，状态依次为 `RUNNING`，然后是带 `result` 的 `SUCCEEDED`，或带 `error`、`error_type` 和完整堆栈的 `FAILED`。
4. **分发**（`worker.run`）：把动作交给对应引擎，见下方动作表。
5. **界面收尾**（`window.finished`）：读取状态和 `result.phase`，弹出相应提示：断电重开、资源重开、声音提示或安装完成。

`SUCCEEDED` 只代表 worker 正常返回，下一步要看阶段（phase）：

| 阶段 | 含义 |
| --- | --- |
| `AWAITING_RELOAD`／一键安装的 `WAIT_RELOAD` | 已写入并完整读回，需要用户断电重开 |
| `AWAITING_RESOURCE_RELOAD`／`WAIT_RESOURCE_RELOAD` | 名称已写、程序未写；断电重开后继续 |
| `LOADED`／`COMPLETE` | 新开机后核对通过（`COMPLETE` 另外记录了机主的实机检查） |
| `NEEDS_REVIEW` | 已停止。保留现场，结果不明的写入绝不重放 |

数据目录：Windows 为 `%LOCALAPPDATA%\A7C2Tools`，macOS 为 `~/Library/Application Support/A7C2Tools`，可用 `A7C2_DESKTOP_DATA` 覆盖。界面设置保存在 `desktop.json`。

## 动作表

| 动作 | 引擎 | 说明 |
| --- | --- | --- |
| `setup_all` | `allinone.run` | 一键安装／继续；在同一进程内调用其他动作 |
| `driver_scan`、`usb_prepare`、`driver_install`、`driver_uninstall`、`mac_unmount` | `platforms.usb_drivers` | 只处理 `054C:0E8A`、`054C:0336`；已有 WinUSB／libusbK／libusb0 绑定直接沿用 |
| `connect`、`usb_stability` | `devicesession.Camera`、`usbcheck` | 只读 |
| `direct_factory_reset`（→ `direct_resume_factory`／`direct_verify`） | `looktool.direct` | 内置原厂参考；界面会先识别未完成的事务 |
| `direct_fl23_enable`、`direct_fl23_disable` | `looktool.direct` | |
| `direct_looks_import`（→ `direct_resume_looks`） | `lookbatch` + `looktool.direct`（`install_all`） | `operations`：`slot`、`action=write/restore`、`path`、可选 `name` |
| `vertical_enable`、`vertical_disable` | `looktool.direct`（持久） | 正在重写 |
| `sound_on`、`sound_off` | `looktool.direct`（`sound_enable/disable`） | |
| `sound_import`、`sound_volume` | `actions.sound` → `shutter_sound.sound_cli` | 只在电脑上处理，不连接相机 |
| `sound_audition`、`sound_install`、`sound_verify`、`sound_restore_sound` | `actions.sound` | 相机操作 |
| `compile_look` | `actions.look_compiler` → `looktool.compilelook` | 离线 |
| `plugin_install` | `desktopapp.plugins` | 可选 Lightroom 插件，自带 ExifTool |
| `fl23_*`、`look_*`、`restore*` | 旧版引擎 | 基于备份的旧流程，为兼容历史和命令行保留 |

替换前端时，只需调用 `jobs.new_job(action, arguments)` 和 `jobs.worker_command(...)`，并按上表理解阶段。参数名与 `Window.common()` 和动作表保持一致。

## 核心流程

**直接事务**（`looktool/direct.py` 的 `DirectJob`）：
1. `plan()` 是纯函数：把相机当前字节转换为目标字节和要改的设置，每种模式只改自己声明的区域。`bundle.plan()` 把多个模式（FL23、外观、ST 恢复、声音、竖屏）叠成一个计划。
2. `run_direct()` 的顺序：读取当前状态 → 生成计划 → 保存 `PLANNED` → 原生预检 → 先写名称、再写程序窗口 → 完整读回程序 → 名称资源 → 设置 → WBI → `AWAITING_RELOAD`。每一步都记在 `direct.private.json` 里。
3. 名称写完后，如果只读重新挂载返回 EBUSY，就停在 `AWAITING_RESOURCE_RELOAD`。等核对到新的开机后，再用新事务写程序。
4. `verify_direct()` 用当前字节重新算一遍计划，必须已经无事可做；再核对 RAM 和已保存设置、确认 `boot_id` 已经变化（证明确实冷启动过），并确认已加载的程序与磁盘一致。恢复原厂另外要求 `/usr` 为只读挂载。

**一键安装**（`allinone.py`，状态保存在 `setup.private.json`）：
`READY → DRIVERS → PREPARED → INSTALLING → WAIT_RELOAD →（核对）→ FINALIZING → COMPLETE`，另有 `WAIT_RESOURCE_RELOAD` 分支。
- `FINALIZING` 阶段接入声音、安装插件和 RAW 映射，并核对竖屏的持久化状态。
- 失败后再点继续：写入前就失败的子事务会改用新的 `install-retry-*` 计划；结果不明的则停止，保留现场。

**恢复原厂**：内置参考 `src/looks/assets/factory-regions-2.01.json` 只包含本项目会修改的区域的通用原厂字节，不含完整固件或机身数据。这些区域以外的字节和机身校准都保持不变。中断后从当前字节继续（`factoryrecover.pending`）。

**USB 服务入口**（`camera_runtime/transport.Session`）：
1. 检查普通 USB 的产品字符串。
2. 认证。
3. 等待 `0336` 出现。
4. 再次认证。
5. 用属性 `00e70003`（十六进制文本序列号摘要）绑定机身。

如果 `0336` 始终没出现、相机回到了 `0E8A`，会冷却 30 秒后重试，最多 3 次。认证没到达 `0336` 时，之后不会再向相机发送任何命令。

**声音**（`src/shutter_sound/`）：`media.py`／`native_codec.py` 在电脑上转换音频；`sound_cli.py` 同时驱动桌面版和 `shutter.py` 的流程；`candidate.py`／`boot_sound.py`／`files_worker.py` 构建固定的原生修改。试听和确认记录是可选的，不影响保存。

**外观转换**：`compilelook.py` 解析 DCP 的 LookTable／色调曲线，或 ST／sRGB 的 CUBE，再用 `calibration/a7c2-2.01.virtual.npz` 拟合成机内色表，不需要 Lightroom。支持哪些输入见 [LUT 格式](lut-formats.md)。

## 构建与测试

使用原生 64 位 Python 3.11：

```text
python -m pip install --no-build-isolation -r desktop-requirements.txt pyinstaller==6.16.0 pytest==8.3.5
python -m pytest dev/desktop dev/shutter_sound/tests dev/vertical -q
python desktop.py
```

根目录的 `conftest.py` 在所有测试中禁止枚举实体 USB。其他测试：`python -m unittest discover -s src/looks/tests`，以及 `src/fl23/tests`、`src/camera_runtime/tests`、`dev/usb/tests`。私有样本（`A7C2_RESEARCH_FIXTURES`）是可选的，没有时相关测试会跳过。

**打包：**
1. 准备被 Git 忽略的发行资源：`python dev/desktop/prepare_assets.py`。用 `--preset-archive <zip>` 从导出的资源包或 Windows 安装包中导入默认六款和配对库；用 `--export-presets <zip>` 导出资源包。
2. 构建：`python dev/desktop/build.py`。

产物：
- **Windows**：`dist/A7C2Tools-Windows-x64.zip`，包含 `A7C2Tools.exe`、`_internal/`、`Profiles/`（配对库）、两份 README 和 `LICENSE`。
- **macOS（仅 Apple Silicon）**：`dist/A7C2Tools-macOS-arm64.dmg`，配对库在 APP 内部（`desktop-assets/profile-library-v1`）。
- `profilepack.py` 要求富士 11 组、理光彩色 4 组配对全部齐全，否则拒绝打包。黑白配对只有满足独立误差标准（MAE ≤3/255、P95 ≤12/255）才能加入。

CI：`.github/workflows/desktop-preview.yml` 在原生 Windows 和 `macos-15` runner 上构建并上传产物，不发布 Release。修改 `dev/desktop/native-build-request.json` 会触发 Apple Silicon 构建。ARM64 上 Keystone／Unicorn 从源码编译（需要 Xcode 命令行工具和 CMake 3.31.6）；Zig 版本为 0.13.0。

安装包没有 Developer ID 签名。离线测试、打包自检和字节读回都不能证明相机能安全启动，也不能证明颜色或声音正确。实机验收单独记录在[更新日志](changelog.md)中。

## 文档规则

文档保持精简：

| 文件 | 读者 |
| --- | --- |
| `README.md`／`README.zh-CN.md` | 用户：下载、操作指南、常见问题 |
| `docs/*/lut-formats.md` | 用户：支持的 LUT／DCP 输入 |
| `docs/*/changelog.md` | 所有人：版本历史和测试记录 |
| `docs/*/notices.md` | 所有人：来源与许可 |
| `docs/*/development.md` | 维护者和 AI：本页 |
| `AGENTS.md` | AI：不能违反的规则 |

每篇英文文档在同一路径下都有对应的简体中文版，两者保持同步。不要再新增按功能拆分的说明或研究笔记：直接读代码，把决策记在更新日志或提交说明里。历史文档保留在 git 历史中。

## A7CR 离线研究分支

此 fork 新增 `a7cr_research.py`，保留桌面/旧版命令行的机型限制与 A7C II 载荷。使用固定版本的 lzh2016zzz A7CR 规格、injectiver 候选构建器、历史 A7C II 配方和 DavidBuchanan314/fwtool.py。原许可保留在本地，不发布固件、供体数据、机身备份或写入载荷。

Windows 适配直接调用 Zig 0.13.0 内置 LLVM 汇编器（`clang -cc1as`，Thumb v7 / Cortex-A9），替换写死的 Apple clang 路径。保留预期字节/位置检查、指令模拟、文件长度与完整反向校验。CRC/本地哈希不等于 Sony 签名认证。

在仓库根目录使用 Python 3.12 与 Git：

```powershell
python -m venv .venv-a7cr
.\.venv-a7cr\Scripts\python.exe -m pip install -r dev/a7cr-requirements.txt
.\.venv-a7cr\Scripts\python.exe a7cr_research.py bootstrap --references build/a7cr/references
```

自行取得官方 A7CR 2.01 与 Alpha 1 II 4.00 更新包，分别以 `BODYDATA.DAT` 保存至 `build/a7cr/workspace/private/firmware/ILCE-7CR_2.01/` 和 `build/a7cr/workspace/private/firmware/ILCE-1M2_4.00/`。工作目录须存在；切勿把供体更新包放入 A7CR 存储卡。

```powershell
.\.venv-a7cr\Scripts\python.exe a7cr_research.py inspect --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe a7cr_research.py extract --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe a7cr_research.py build --references build/a7cr/references --workspace build/a7cr/workspace
.\.venv-a7cr\Scripts\python.exe -m pytest dev/desktop/test_a7cr_research.py -q
```

私有输出位于被 Git 忽略的目录；重复构建会保留已有候选并停止。bootstrap 仅获取固定研究引用，不下载 Sony 固件。CLI 不枚举 USB、不认证、不改驱动、不写程序/设置/WBI，也不生成可刷写 DAT。AP/CP 候选仍需新鲜同机参数合并、验证过的双备份、Windows 传输/原生事务、分阶段回读及实机恢复验收。切勿交给 A7C II 安装器。

`dev/a7cr-validation.json` 记录 Windows 上 32 项源条件、2304 个模拟案例与 AP/CP 完整反向校验通过，另有 20 个新增测试通过。未存取相机；开机、预览、色彩及复原尚未验证。

## Windows 一键准备工具

`a7cr_easy_gui.py` 是独立 Tk 界面，不是 A7C II 桌面前端。自动下载并检查两个官方输入的长度/哈希，创建私有任务，在子进程执行 inspect/extract/build。仅当 HTTP Range 精确匹配时续传；损坏文件、失败任务与中断候选均保留。取消只会终止离线 worker。启动不连接相机、不开始下载；进程锁阻止并发准备。完成状态始终是 PREPARED_NOT_INSTALLED，没有安装/恢复控件，也不存取相机。

打包后的 A7CR-Preparation.exe 不需要 Python/Git。内含固定版本且通过校验的研究源码和编译/运行工具，不内含 Sony 更新数据或衍生候选。资料保存至 LOCALAPPDATA/A7CRTools；测试可用 A7CR_TOOLS_DATA 覆盖。首次下载约 2.3 GB，要求至少 7 GiB 可用空间。开发者安装 dev/a7cr-requirements.txt 与 pyinstaller==6.16.0 后运行 python dev/build_a7cr_easy.py 构建。测试位于 dev/desktop 的两个 A7CR 测试模块。

GUI 保留正常启用 CFG 的 Windows bootloader。原生模拟在内附的官方 Python 3.12.10 嵌入解释器中执行，以 python312._pth 隔离，避开已有记录的 Unicorn/PyInstaller CFG 不兼容问题；不改变 Windows 安全设置。

## A7CR 设备开发

备份开发版分开处理 Windows USB 驱动、唯读服务备份，以及仅限内存模拟的安装/恢复事务。启动和离线准备不枚举 USB；连接检查仅读取描述符与 Windows SetupAPI 资料，检测回执不包含带序号的 PnP 实例路径。

`a7cr_reader.py` 将 injectiver 的 MIT A7CR 读取器适配到 Windows（许可保存在 docs/licenses）。只选择 AP/CP 程序路径并读取校验有效的内存/已保存 Backup 快照；不上载原生代码，不写相机文件或设置。机型、接口、机身绑定、读取长度或结果异常时停止，不重试认证。两个新鲜独立目录保存程序、两份设置快照与四个受保护 CP 单元；每个文件回读后再由 a7cr_operations.verify_checkpoint 独立验证。这是当前状态的修改范围备份，不代表原厂状态或完整 eMMC/NOR；同一磁碟两个目录不等于异盘灾难恢复。

`a7cr_drivers.py` 只有经明确确认才动作；仅接受一个当前存在的 054C:0E86 或 054C:0336 节点，沿用可用驱动，校验固定 helper，保存前态/意图/后态，并要求安装后可用且仍是同一实例。结果不明会持久阻止重试。0336 回调只观察重枚举并准备一次主机驱动，不重复认证；UAC 由机主亲自处理，不关闭签名验证或修改 Windows 安全设置。helper 与上游项目引用的第三方 libwdi 版本相同。驱动流程和实机备份仍待硬件验收，离线测试不算实机证据。

`a7cr_transaction_model.py` 仅接受内存 SimulationDevice，测试 CP 程序→CP 参数→AP 程序→AP 绑定→WBI 的顺序、选定范围反向恢复、保留后续无关设置、确认丢失及回读不符；拒绝真实后端。0.3 备份版的两个写入按钮保持禁用；稳定版策略仍为 false。0.4 实验操作采用下文独立、明确确认的原生路径。不能通过可编辑 JSON 或成功备份就开放刷机。

开发者原生检查：python dev/check_a7cr_native.py --workspace build/a7cr-native-check。首次获取哈希固定的历史 MIT worker 源码，通过 Zig LLVM 前端/链接器编译 AArch64，再模拟 20 个系统调用案例，包含短读、部分写入、反向阶段、恢复只读挂载及 WBI 顺序。它们只是历史参考 worker 的模拟，不是生产用 Windows 相机 worker、开机模拟或断电救援证明。

主机测试为 dev/desktop 下六个 test_a7cr_*.py 模块。实机须分别验收 Win11 服务进入/退出、同机备份比较、原生预检、分阶段写入/完整回读、新开机、预览/新照片/播放和实际恢复；尚无任意 CP 损坏或断电救援证据。软件检查无法满足零变砖风险保证。


## A7CR 实验安装流程

0.4.0 是独立 A7CR 应用，不是替换 desktop.py 的机型名称。依用户要求纳入自动备份；上游桌面的无完整备份直写操作及旧 CLI 契约保持不变。已获取上游 ef7db116e13e37be91e9af20360dc9bba8d64a9b 的 AGENTS.md，与本地规则相同。未使用额外代理，未修改主分支或重写既有发布。

`a7cr_job.py` 验证同机独立备份，在机身程序上只覆盖预期源字节的位置，合并 FL2/FL3 选择器及未引用的空闲色彩表，并编译五个私有原生程序。v2 工作直接引用 backup2 的原始数据，不额外复制原始程序。整文件 SHA 只作审计记录，不作固件准入白名单；小型原生代码及官方下载仍保留完整性检查。通过整文件前向与逆向字节变换验证，大小或修改位置冲突即停止。

`a7cr_native/installer.c` 改写历史 MIT 固定位置程序，采用 A7IRP002/version 2 ABI，原哈希字段保留为零。以修改字节及依赖位置检查代替固件 SHA 准入，并检查 inode、大小、挂载及主机完整读回。写失败不自动逆写或重试；只重试已知的读取 EINTR 或本机日志原子替换冲突。`a7cr_native_build.py` 校验 AArch64 ET_DYN、构造入口、重定位、无外部依赖及不可执行堆栈；`dev/a7cr_native_cases.py` 以生产源代码/编译器进行模拟系统调用，不是相机启动模拟。

`a7cr_preflight.py`、`a7cr_install.py`、`a7cr_actions.py` 依普通 USB 型号及 AP 00e70003 的传统序号 hex 文本摘要绑定机身；不使用 TUR/getCameraInfo 准入或 HASP 替代。服务模式进入后立即校验 AP 机身身份，再依序执行 mode-0 预检、CP 程序/参数、AP 程序/绑定、WBI drop1/mode0。丢失回覆、断线或读回不符会持久停止新装置工作。还原把项目字段合并到新读出的当前数据，SAVE3 前拒绝未保存的无关差异；这不是完整芯片或断电损坏救援。

GUI 写入前要求明确确认实验风险及供电；启动和识别不会写入。原生预检会暂存并执行相机临时文件，与只读备份不同。失败保留证据；确认普通 USB 返回后才报告磁盘读回完成及提示重启/实机检查。完成状态不宣称已证明开机、预览、照片、播放、保留映像、实机还原或零变砖。文件夹分发避免随机暂存的驱动路径，不改变 Windows 安全设置；WinUSB/UAC 实机验收仍待完成。

测试时明确列出 `dev/desktop/test_a7cr_*.py`。完整上游桌面测试另需 Qt/NumPy；本套独立测试不代表整个上游应用都已验证。公开夹具与报告不包含机身数据、固件、照片或私人路径；工厂预设合成夹具不是机身拥有权证据。当前实机证据仍只有 MSC/USBSTOR 识别及驱动未启动的 Windows 1223。
