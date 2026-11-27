# t3-code-no-telemetry

**简体中文** | [English](README.en.md) | [Patch Reference (EN)](PATCHES.md)

从官方源码构建的 **T3 Code 去遥测版本**，移除了使用分析上报、设备指纹追踪、云端追踪日志、内置自动更新以及云端登录钩子，让你可以在 Linux 上纯粹以本地方式运行 T3 Code。

> **已实测通过的环境**（`v0.1.13`）：
> - **桌面端**：`CachyOS x86_64`（`Linux 7.3.6-1-cachyos`）— 本地源码编译并运行正常；
> - **服务端**：`Ubuntu 24.04.4 LTS (Noble Numbat) x86_64`（`Linux 6.8.0-110-generic`）— 单文件 ELF（`server-sea`）源码编译、热备升级与 `systemd` 托管运行正常；
> - **跨机联调**：在 CachyOS 上通过去遥测版 T3 Code 桌面端以及浏览器连接远程 Ubuntu 工作站上的 T3 Code Server 均工作正常。

本工具只使用 Python 3.10+ 标准库，不需要安装额外 Python 依赖；同时提供面向 Arch / CachyOS 的极简 `PKGBUILD` + `.patch` 配方（位于 `master` 分支）以及面向通用 Linux 的命令行构建脚本 `t3clean.py`。

---

## 一、改了什么（What's Patched）

### 受影响的文件清单

补丁拆分为两份标准 `.patch` 文件，你可以直接打开审阅每一行改动：

- **去遥测与外联清理**（[`aur/0001-strip-telemetry.patch`](aur/0001-strip-telemetry.patch)，共 8 个文件）：
  - `apps/server/src/telemetry/AnalyticsService.ts`
  - `apps/server/src/telemetry/Identify.ts`
  - `apps/web/src/connection/platform.ts`
  - `packages/client-runtime/src/authorization/remote.ts`
  - `packages/shared/src/relayTracing.ts`
  - `apps/desktop/src/updates/DesktopUpdatePolicy.ts`
  - `apps/web/src/cloud/publicConfig.ts`
  - `apps/server/src/cloud/publicConfig.ts`
- **Linux 源码编译兼容修复**（[`aur/0002-linux-build-compat.patch`](aur/0002-linux-build-compat.patch)，共 2 个文件）：
  - `apps/web/src/index.css`（排除 `../node_modules`，避免从源码压缩包编译时 Tailwind 扫描依赖目录导致内存暴涨）
  - `scripts/linux-glibc.ts`（解除上游为 Ubuntu 24.04 写死的 glibc 2.39 上限，改为按本机 glibc 版本自动对齐，让 Arch/CachyOS 等滚动发行版能顺利打包）

### 所有遥测与默认外联的处理明细

| 涉及文件 | 原始功能 | 处理状态 | 修改内容 | 处理原因 |
|---|---|---|---|---|
| `apps/server/src/telemetry/AnalyticsService.ts` | 将使用事件发送到 `ingest.t3.codes`（内置 PostHog 密钥） | **已剥离** | 删掉上报地址、密钥和网络发送逻辑，把记录和刷新函数改为空实现，直接忽略所有事件 | 彻底切断服务端使用行为上报 |
| `apps/server/src/telemetry/Identify.ts` | 生成并持久化本地 `anonymous-id` 等设备/用户追踪标识 | **已剥离** | 不再读写本机追踪标识文件，查询身份和归属时固定返回空值 | 防止为本机生成长期追踪指纹 |
| `apps/web/src/connection/platform.ts`<br>`packages/client-runtime/src/authorization/remote.ts` | 在前端 `localStorage` 里存设备 ID（`t3code:analytics-device-id`），并随连接请求发给服务端 | **已剥离** | 删掉前端读写设备 ID 的逻辑，让请求附带的设备识别参数恒为空 | 防止前端界面继续生成或回传设备标识 |
| `packages/shared/src/relayTracing.ts` | 通过 OpenTelemetry / Axiom 上报中继链路追踪日志（含 `X-Axiom-Dataset`） | **已剥离** | 移除追踪数据上报逻辑，仅保留正常业务流程透传 | 避免运行过程向第三方追踪平台发送日志 |
| `apps/desktop/src/updates/DesktopUpdatePolicy.ts` | 桌面端默认开启自动检查更新与夜间版更新检查 | **已修改** | 将默认更新策略改为关闭（`mode: "off"`），并关闭夜间版更新检查 | 避免后台静默联网查更新，同时防止绕过系统包管理器更新软件 |
| `apps/web/src/cloud/publicConfig.ts`<br>`apps/server/src/cloud/publicConfig.ts` | 读取 Clerk 登录配置与 T3 Connect 云端中继/追踪配置 | **已修改** | 将云端配置开关固定为关闭、追踪配置固定返回空 | 关闭云端账号登录与中继遥测入口，保持纯本地运行 |
| `apps/server/src/provider/ModelManifest.ts` | 从 GitHub 拉取公开的模型列表与能力说明 | **刻意保留** | 不修改 | 只读公开元数据，用于让本地代理正确识别最新模型的能力与上下文长度 |
| `apps/server/src/usage/UsageService.ts` | 从 LiteLLM 仓库拉取公开的模型单价表 | **刻意保留** | 不修改 | 只读公开价格表，仅在本地估算 Token 费用，不上传任何用户数据 |

> 想逐项核对具体的上游代码位置、调用链路和修改细节，可查阅完整英文说明文档：**[PATCHES.md](PATCHES.md)**。

除此之外，编译和启动脚本还会做两层兜底保护：
- **编译时**：自动清理源码根目录的 `.env` / `.env.local` 以及当前终端里的 Clerk / Relay / PostHog / 签名等环境变量，并在构建完成后自动扫描最终产物（包括 `app.asar`）确保没有遗漏任何上报域名或密钥。
- **运行时**：启动脚本默认注入 `T3CODE_TELEMETRY_ENABLED=false` 和 `DO_NOT_TRACK=1`。

> [!WARNING]
> **安装或切换前请先备份你的数据和正在进行的工作！**
>
> - 本版本与官方 `t3code-bin` / `t3code-nightly-bin` 安装路径完全隔离（不会造成包冲突），但默认会共用同一份本地数据目录（`~/.t3/userdata/statev2.sqlite` 和 `~/.config/t3code-v2/`）。
> - 切换前请先**彻底退出**正在运行的官方 T3 Code，并建议备份数据库：
>   ```bash
>   cp ~/.t3/userdata/statev2.sqlite ~/.t3/userdata/statev2.sqlite.bak
>   ```
> - **请勿同时启动**官方版与去遥测版，避免两个进程同时写入同一个 SQLite 数据库。
> - 如果你之前使用的是较新的 **Nightly 版本**（例如 `0.1.13-nightly`），其数据库结构可能比当前稳定版更新；强烈建议先使用下方“隔离模式”试用，避免旧版本直接打开新版数据库出错。

---

## 二、Arch / CachyOS（AUR 方式）安装使用

目前尚未发布到 `aur.archlinux.org`，可直接克隆本仓库的 **`master` 分支**（里面只包含平铺的 `PKGBUILD`、`.SRCINFO`、两份 `.patch`、`LICENSE` 和 `.gitignore`，与正式 AUR 仓库结构完全一致）并运行 `makepkg` 编译安装：

```bash
# 请放在家目录（如 ~/）下构建，不要放在 /tmp（/tmp 为内存盘，既占内存又无法复用磁盘上的 pnpm 缓存）
git clone --branch master --single-branch https://github.com/LYB926/t3-code-no-telemetry.git ~/t3-code-no-telemetry-aur
cd ~/t3-code-no-telemetry-aur
makepkg -si
```

> 所有编译依赖（`git`、`python`、`nodejs>=26.12.1`、`npm`、`gcc`、`make`、`rust`、`pkgconf`、`imagemagick`）均来自 Arch/CachyOS 官方源，无需额外安装任何 AUR 构建依赖。构建脚本会自动按上游版本隔离安装 `vite-plus`，不会污染系统全局目录。

安装完成后，在终端运行 `t3-code-no-telemetry` 或从桌面应用菜单启动 **T3 Code (No Telemetry)** 即可。

### 常用加速与保护选项

- **复用构建缓存（强烈推荐日后升级时使用）**：跨次保留已下载的依赖与 Electron/Cargo 缓存，大幅缩短二次编译时间：
  ```bash
  T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" makepkg -Csi
  ```
- **16 GiB 及以下内存机器防卡顿**：构建脚本在检测到内存偏紧时会自动降低并发数（也可用 `T3CLEAN_JOBS=4` 手动指定）。如果桌面同时开着浏览器等大内存应用，可以用 `systemd-run` 给编译进程加一层内存上限：
  ```bash
  T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" \
  systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=10G \
    makepkg -Csi
  ```
- **与官方版 / Nightly 完全隔离试用（零风险，甚至可以同时双开）**：
  ```bash
  T3CODE_HOME="$HOME/.t3-clean" t3-code-no-telemetry --user-data-dir="$HOME/.config/t3code-no-telemetry"
  ```
- **安装某个历史版本**：前往本仓库的 [Releases](https://github.com/LYB926/t3-code-no-telemetry/releases) 页面，下载对应版本的 `t3-code-no-telemetry-v<版本>-aur-recipe.tar.gz`，解压后进入目录执行 `makepkg -si` 即可复现该历史版本。

---

## 三、通用 Linux（Ubuntu / Debian 等）去遥测与重编译

克隆本仓库默认分支（`dev`），直接通过 `t3clean.py` 完成拉取、打补丁、验净与编译：

```bash
git clone https://github.com/LYB926/t3-code-no-telemetry.git
cd t3-code-no-telemetry
```

### 1. 只做去遥测补丁与检查（不编译）

```bash
# 拉取上游最新正式版源码
./t3clean.py fetch --ref latest-release --dest ./work/t3code

# 执行去遥测补丁（加 --dry-run 可只预览不写文件）
./t3clean.py patch --source ./work/t3code

# 扫描确认无遥测残留
./t3clean.py verify --artifact ./work/t3code
```

### 2. 编译桌面端

如果系统自带的 Node.js 版本低于上游要求（如 Ubuntu 常见情况），加上 `--bootstrap-toolchain` 会在缓存目录里自动下载官方 Node.js 和对应版本 `vite-plus`，不会改动系统原本的 `/usr/bin/node`。

```bash
# 编译免安装目录版（linux-unpacked）
./t3clean.py build \
  --ref latest-release \
  --target desktop-dir \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean" \
  --output-dir ./out

# 如需 .deb 或 AppImage，将 --target 改为 desktop-deb 或 desktop-appimage 即可
```

常用可选参数：
- `--arch arm64`：编译 ARM64 架构
- `--jobs 4`：手动限制编译并发数
- `--keep-updater` / `--keep-cloud`：按需保留自动更新或云端配置
- `--use-system-electron`（配合 `desktop-dir`）：剥离内置 Electron，改用系统已安装的 Electron

### 3. 编译、部署与回滚服务端（如 Ubuntu 服务器）

```bash
# 方式 A：构建单文件可执行服务端归档（需系统已装 cargo、patchelf、dpkg-query）
./t3clean.py build \
  --ref latest-release \
  --target server-sea \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean-server" \
  --output-dir ./dist-server

# 方式 B：如果不想编译单文件版，可构建自带 Node 运行时的绿色压缩包
./t3clean.py build \
  --ref latest-release \
  --target server-bundle \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean-server" \
  --output-dir ./dist-server

# 安全部署到 systemd 用户服务（部署前自动验净、热备旧程序与数据库；重启失败会自动回滚）
./t3clean.py deploy-server --archive ./dist-server/t3-*.tar.gz --dry-run   # 先预览
./t3clean.py deploy-server --archive ./dist-server/t3-*.tar.gz             # 正式部署

# 查看备份与手动回滚（默认只回滚程序不覆盖新产生的会话；需要连库一起回退时再加 --restore-db）
./t3clean.py rollback-server --list
./t3clean.py rollback-server
```

---

## 四、补充说明

### 仓库分支与自动跟踪机制

- **`dev`（默认分支）**：存放完整工具源码、单元测试、GitHub Actions 工作流和 `aur/` 目录。
- **`master`（独立扁平分支）**：只保留 AUR 所需的 6 个根目录文件，零子目录，直接面向 `makepkg` 和未来的 AUR 推送。
- **自动更新巡检**：[`.github/workflows/update-check.yml`](.github/workflows/update-check.yml) 每天定时检查上游 `pingdotgg/t3code` 的最新正式 Release。如果发现新版本且补丁与全源码扫描全部通过，会自动向 `dev` 提 PR 供人工审核；如果上游改了代码结构导致补丁失效或扫出新残留，会自动开 Issue 提醒修复。
- **自动同步与配方发版**：PR 合并进 `dev` 后，[`.github/workflows/sync-master.yml`](.github/workflows/sync-master.yml) 会自动把 `aur/` 同步到 `master`，并在 GitHub Releases 发布一份轻量源码配方包（不分发预编译二进制，方便任何人直接审计补丁并自行编译）。
- **只维护最新稳定版**：`patcher.py` 只紧跟当前最新正式版，不堆积历史版本的兼容分支；老版本需要重编时直接从对应的 GitHub Release 获取冻结的 `PKGBUILD` 和 `.patch` 即可。

### 提前检查上游 Nightly 与手动导出 AUR 目录（`export-aur`）

想提前看看上游还没发正式版的 Nightly 有没有新增遥测，或者想自己为某个特定版本生成一份极简 AUR 目录，可以用下面几条命令：

```bash
# 查看当前上游最新正式版版本号
./t3clean.py latest-release

# 5 秒快速检查某个 Nightly 版本的遥测代码是否变动
./t3clean.py fetch --ref v0.1.13-nightly.20261125.5258 --dest ~/t3-nightly-check
./t3clean.py patch --source ~/t3-nightly-check
./t3clean.py verify --artifact ~/t3-nightly-check

# 为最新正式版（或任意指定 tag / nightly）生成极简 .patch 版 AUR 目录
./t3clean.py export-aur --out ./aur --ref latest-release --pkgrel 1

# 也可以把某个 Nightly 导出到独立目录直接试编 Arch 包
./t3clean.py export-aur --out ~/t3-aur-nightly --ref v0.1.13-nightly.20261125.5258
cd ~/t3-aur-nightly && T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" makepkg -s
```

如果本机已经下载了源码压缩包，传 `--source-tarball /path/to/t3code-v0.1.12.tar.gz` 可以免联网直接校验并生成补丁。

### 严格离线审计

如果你希望连模型列表和价格表这类公开只读拉取也一并排查出来，可以在校验时加上 `--strict-offline`：

```bash
./t3clean.py verify --artifact ./work/t3code --strict-offline
```

### 运行本地测试与校验

```bash
python3 -W error::SyntaxWarning -m unittest discover -s tests -v
bash -n aur/PKGBUILD
(cd aur && diff -u .SRCINFO <(makepkg --printsrcinfo))
namcap aur/PKGBUILD
```
