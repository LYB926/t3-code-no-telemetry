# t3-code-no-telemetry (`t3clean`)

纯 Python 3.10+ 标准库实现的 **T3 Code 去遥测源码补丁引擎、产物验净器、极简标准 AUR 包生成器与 Linux Server 安全部署/回滚工具链**（零第三方 Python 依赖）。

当前面向最新稳定版上游（`v0.1.12` 起），所有补丁具备幂等性（`// @t3-no-telemetry-patched`）与锚点缺失快速失败保护（`PatchAnchorNotFoundError`）；不再堆砌历史旧版本兼容分支，历史版本的完整 `PKGBUILD` 与静态 `.patch` 均通过 Git Tag / GitHub Release 永久冻结。

---

## 目录

- [分支架构与自动化发布流](#分支架构与自动化发布流)
- [去遥测补丁策略（6 条强制规则 + 2 条构建兼容 + 2 条刻意保留）](#去遥测补丁策略6-条强制规则--2-条构建兼容--2-条刻意保留)
- [CachyOS / Arch Linux 本地 `makepkg` 构建](#cachyos--arch-linux-本地-makepkg-构建)
- [预检上游 Nightly 与生成极简 AUR 目录（`export-aur`）](#预检上游-nightly-与生成极简-aur-目录export-aur)
- [快速开始（CLI 子命令一览）](#快速开始cli-子命令一览)
- [Ubuntu / `aiseon` 服务器自构建、部署与回滚](#ubuntu--aiseon-服务器自构建部署与回滚)
- [与官方 `t3code-bin` 共存及数据库回滚注意事项](#与官方-t3code-bin-共存及数据库回滚注意事项)
- [运行测试](#运行测试)

---

## 分支架构与自动化发布流

参考标准的双分支 AUR 维护模式：

- **`dev`（GitHub 默认开发分支）**：包含完整工具链 `t3clean/`、`t3clean.py`、`tests/`、GitHub Actions 工作流，以及面向 AUR 的扁平导出目录 `aur/`。
- **`master`（AUR 专用 orphan 扁平分支）**：根目录仅包含 **6 个极简文件**（零子目录、零 Python 打包脚本），可直接克隆或推送至 AUR：
  1. `PKGBUILD`（标准 `patch -Np1`、内联启动器/`.desktop`/`check()` 扫描、锁定 `_vp_version`、全量真实 `sha256sums`）
  2. `.SRCINFO`（与 `makepkg --printsrcinfo` 逐字节一致）
  3. `0001-strip-telemetry.patch`（6 条去遥测/云端/自动更新剥离规则的标准 unified diff，一目了然）
  4. `0002-linux-build-compat.patch`（Tailwind CSS v4 扫描排除 + Linux glibc 构建上限对齐）
  5. `.gitignore`
  6. `LICENSE`
- **自动化工作流**：
  - [`.github/workflows/update-check.yml`](.github/workflows/update-check.yml)：每日查询 `pingdotgg/t3code` 的 `releases/latest`；若发现新稳定版，自动拉取源码、生成 `.patch`、验证 GNU `patch -Np1` 与全源码树 0 遥测残留、校验 `.SRCINFO` / `namcap` / 单元测试。
    - **全部通过**：自动向 `dev` 发起 PR（附上游 `telemetry/` 与 `cloud/` 目录文件清单供人工审阅），由 GitHub 通知你审核合并；
    - **任何锚点失效或扫描出新残留**：按版本号去重自动创建 GitHub Issue 并标红失败，及时通知你适配新版本。
  - [`.github/workflows/sync-master.yml`](.github/workflows/sync-master.yml)：每当你将更新合并到 `dev` 的 `aur/` 时，自动将上述 6 个扁平文件同步至 orphan `master` 分支，并发布一份**轻量 AUR 配方 GitHub Release**（`v<pkgver>-<pkgrel>`，附冻结的 `aur-recipe.tar.gz`、`PKGBUILD`、`.SRCINFO`、`.patch` 与 `SHA256SUMS.txt`），确保任何历史版本在未来都能 100% 离线/可复现编译。

---

## 去遥测补丁策略（6 条强制规则 + 2 条构建兼容 + 2 条刻意保留）

### 强制剥离的 6 条规则（导出至 `0001-strip-telemetry.patch`）

| # | 目标文件 | 处理方式 |
|---|---|---|
| 1 | `apps/server/src/telemetry/AnalyticsService.ts` | 全文件替换为 No-Op Stub：保留 `AnalyticsService`（`record -> Effect<boolean>`）、`make`、`layer` 导出签名，彻底移除 `ingest.t3.codes`、硬编码 PostHog Key（`phc_...`）、HTTP 上报、定时刷新与退出 flush。 |
| 2 | `apps/server/src/telemetry/Identify.ts` | 替换为零 I/O Stub：`getTelemetryIdentity` 恒返回 `{ distinctId: null, installId: null }`，`getServerOwners` 恒返回 `[]`，不读写 `anonymous-id` / `ServerSecretStore`。 |
| 3 | `apps/web/src/connection/platform.ts`<br>`packages/client-runtime/src/authorization/remote.ts` | 使用词法平衡替换：移除 `t3code:analytics-device-id` 的 `localStorage` 持久化逻辑并让 `analyticsDevice.id` 返回 `Option.none()`；让 `readClientOwnerParams` 恒返回 `Effect.succeed({})`。 |
| 4 | `packages/shared/src/relayTracing.ts` | 全文件替换为 No-Op Stub：保留所有导出符号，`layer()` 恒返回 `Layer.succeed(RelayClientTracer, Option.none())`，彻底移除 `OtlpTracer` 与 `X-Axiom-Dataset`（Axiom/OTLP 链路追踪）。 |
| 5 | `apps/desktop/src/updates/DesktopUpdatePolicy.ts` | 将 `unmanagedUpdatePolicy` 锁定为 `{ mode: "off", feedUrl: undefined, allowNightly: false, message: null }`，彻底禁用桌面端自动更新（CLI 构建可通过 `--keep-updater` 保留）。 |
| 6 | `apps/web/src/cloud/publicConfig.ts`<br>`apps/server/src/cloud/publicConfig.ts` | 强锁 `hasCloudPublicConfig` 为 `false`，`resolveRelayTracingConfig` / `resolveRelayClientTracingConfig` 恒返回 `null`，彻底切断 Clerk / T3 Connect 云端遥测与登录钩子（CLI 构建可通过 `--keep-cloud` 保留）。 |

### Linux 构建兼容规则（导出至 `0002-linux-build-compat.patch`）

| 目标文件 | 处理方式 |
|---|---|
| `apps/web/src/index.css` | 在 `@import "tailwindcss";` 后注入 `@source not "../node_modules";`，防止从无 `.git` 的源码压缩包在外层忽略目录构建时 `@tailwindcss/oxide` 扫描 `node_modules` 触发 20+ GiB OOM。 |
| `scripts/linux-glibc.ts` | 将写死的 Ubuntu 24.04 CI 上限 `GLIBC_BUILD_CEILING = "2.39"` 改为 `process.env.T3CODE_GLIBC_BUILD_CEILING?.trim() || "99.0"`，构建期自动对齐宿主机 glibc 版本（如 Ubuntu 24.04 仍保持 `2.39`，Arch/CachyOS 自动对齐 `2.44`）。 |

此外，构建阶段会自动清理 `.env` / `.env.local` 并剥离环境变量中的 Clerk / Relay / OTLP / 更新仓库 / 签名凭据，同时注入 `T3CODE_TELEMETRY_ENABLED=false`、`DO_NOT_TRACK=1`、`T3CODE_TELEMETRY_CHANNEL=dev`、`T3CODE_WEB_SOURCEMAP=false`。

### 刻意保留的 2 项（规则 7 & 8）

以下两项为本地代码代理正常运行所需的公开只读元数据拉取，**默认不做修改**：

- **规则 7**：`apps/server/src/provider/ModelManifest.ts`（拉取模型目录/能力清单）
- **规则 8**：`apps/server/src/usage/UsageService.ts`（拉取模型价格表用于本地 Token 费用估算）及 `enableProviderUpdateChecks`

如果处于严格离线环境，可对构建产物执行 `python3 t3clean.py verify --artifact <path> --strict-offline` 进行离线审计。

---

## CachyOS / Arch Linux 本地 `makepkg` 构建

直接使用 [aur/PKGBUILD](aur/PKGBUILD) 即可（与最终推送到 AUR `master` 的文件完全一致）：
- **零 AUR 构建依赖**：所有 `makedepends` 均来自 Arch/CachyOS 官方源；构建时按 `_vp_version`（来自上游 `pnpm-workspace.yaml`，如 `1.8.0`，兜底 `^1.4.0` 防 2.x 破坏性升级）复用兼容的系统 `vp` 或通过宿主机 `npm` 隔离安装到缓存目录。
- **轻量源码压缩包 + 全量真实 `sha256sums`**（零 `SKIP`）。
- 不依赖 `fuse2`，且与官方 `t3code-bin` 完全共存。

### 基础构建（clean-chroot 兼容默认缓存 `${srcdir}/.cache`）

```bash
cd aur
makepkg -s
# 确认 check() 验净通过后安装（或直接 makepkg -si）：
# sudo pacman -U t3-code-no-telemetry-*.pkg.tar.zst
```

### 开启持久化构建缓存（推荐日常反复升级使用）

通过 `T3CLEAN_CACHE_DIR` 可跨次复用隔离 `vp`、`ELECTRON_CACHE`、`ELECTRON_BUILDER_CACHE`、`CARGO_HOME`、`PNPM_HOME` 与 `npm_config_store_dir`，大幅加速后续重编：

```bash
cd aur
T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" makepkg -Csi
```

### 低内存机器（16 GiB 及以下）防卡死编译建议

构建脚本在宿主机内存 `≤ 16 GiB`（或可用内存 `< 6 GiB`）时会自动限流 `RAYON_NUM_THREADS`、`CARGO_BUILD_JOBS` 与 `npm_config_child_concurrency`（随时可通过 `T3CLEAN_JOBS=N` 手动覆盖）。在纯 zram 的 16 GiB 桌面环境下，也可配合 `systemd-run` 给构建进程树加上 cgroup 内存保护：

```bash
cd aur
T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" \
systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=10G \
  makepkg -Csi
```

### 安装历史版本

前往 GitHub **Releases** 下载对应版本的 `t3-code-no-telemetry-v<ver>-<rel>-aur-recipe.tar.gz`，解压后执行 `makepkg -si` 即可完全复现该历史版本的构建。

---

## 预检上游 Nightly 与生成极简 AUR 目录（`export-aur`）

### 1. 提前检查上游 Nightly 的遥测变化（~5 秒）

```bash
./t3clean.py fetch --ref v0.1.13-nightly.20261125.5258 --dest /tmp/t3-nightly
./t3clean.py patch --source /tmp/t3-nightly
./t3clean.py verify --artifact /tmp/t3-nightly
```

### 2. 一键导出任意版本的极简 AUR 目录（含自动生成 `.patch` 与真实 `sha256sums`）

```bash
# 方式 1：跟踪上游最新正式 Release（单次下载归档、生成并实测 patch、全源码树验净、写入真实 sha256sums）
./t3clean.py export-aur --out aur --ref latest-release --pkgrel 1

# 方式 2：复用本机已下载的源码压缩包（零网络请求）
./t3clean.py export-aur --out aur --ref v0.1.12 --pkgrel 1 --source-tarball /path/to/t3code-v0.1.12.tar.gz

# 方式 3：试编某个上游 Nightly 为 Arch 包（与正式 AUR 链路 100% 一致）
./t3clean.py export-aur --out /tmp/aur-nightly --ref v0.1.13-nightly.20261125.5258
cd /tmp/aur-nightly && T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" makepkg -Csi
```

---

## 快速开始（CLI 子命令一览）

```bash
./t3clean.py --help

# 1. 查询上游最新稳定 GitHub Release
./t3clean.py latest-release

# 2. 拉取上游源码（支持固定标签、latest-release、latest-stable、nightly 或分支）
./t3clean.py fetch --ref latest-release --dest ./work/t3code

# 3. 对源码目录执行幂等去遥测补丁（支持 --dry-run / --keep-updater / --keep-cloud / --json）
./t3clean.py patch --source ./work/t3code

# 4. 校验源码目录、解包目录或 Electron app.asar（流式二进制扫描，无需外部 asar CLI）
./t3clean.py verify --artifact ./work/t3code
./t3clean.py verify --artifact ./out/linux-unpacked

# 5. 完整构建（支持 desktop-dir / desktop-deb / desktop-appimage / server-sea / server-bundle）
./t3clean.py build --source ./work/t3code --skip-fetch --target desktop-dir --output-dir ./out

# 6. 导出极简 .patch 版扁平 AUR 目录
./t3clean.py export-aur --out ./aur --ref latest-release --pkgrel 1

# 7. 部署与回滚 Linux systemd 用户服务
./t3clean.py deploy-server --archive ./out/t3-0.1.12+notelemetry-linux-x64.tar.gz
./t3clean.py rollback-server --list
./t3clean.py rollback-server
```

---

## Ubuntu / `aiseon` 服务器自构建、部署与回滚

在 Ubuntu 服务器（例如 `aiseon`）上，系统自带的 Node.js 版本通常低于上游要求的 `26.13.1`。使用 `--bootstrap-toolchain` 会在指定缓存目录内下载官方 Node.js 发行版并校验 SHA-256，按上游 `pnpm-workspace.yaml` 锁定版本隔离安装 `vite-plus`，**绝不触碰 `/usr/bin/node`**。

### 1. 构建 `server-sea`（单文件可执行归档）

构建 `server-sea` 需要系统具备 `cargo`、`patchelf` 与 `dpkg-query`（Ubuntu/Debian 默认已含 `dpkg-query`）：

```bash
./t3clean.py build \
  --ref latest-release \
  --target server-sea \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "${HOME}/.cache/t3clean-server" \
  --output-dir ./dist-server
```

如果目标机器不方便编译 SEA，也可以构建基于隔离 Node 运行时的 `server-bundle`：

```bash
./t3clean.py build \
  --ref latest-release \
  --target server-bundle \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "${HOME}/.cache/t3clean-server" \
  --output-dir ./dist-server
```

### 2. 安全部署（自动校验 + 自动备份 + 失败自动回滚）

```bash
# 先试运行确认路径与计划
./t3clean.py deploy-server \
  --archive ./dist-server/t3-0.1.12+notelemetry-linux-x64.tar.gz \
  --dry-run

# 正式部署
./t3clean.py deploy-server \
  --archive ./dist-server/t3-0.1.12+notelemetry-linux-x64.tar.gz
```

`deploy-server` 的安全机制：

- 防御 tar 路径穿越（拒绝绝对路径、`..` 与越界符号链接）
- 部署前对解包产物执行 `verify_artifact`（可用 `--skip-verify` 跳过）
- 将旧版本运行时、`t3code.service`、`service-state.json`、`~/.local/bin/t3` 及在线 `sqlite3` 热备的 `userdata/statev2.sqlite` 完整备份至 **`~/.t3/runtime/no-telemetry-backups/<backup-id>/`**（置于 `versions/` 目录之外，避免上游 `pruneRuntimesOnStart` 自动清理）
- 自动在 `~/.t3/service.env` 写入 `T3CODE_TELEMETRY_ENABLED=false`，并默认清除 `~/.t3/userdata/anonymous-id`（可通过 `--keep-anonymous-id` 保留）
- 执行 `systemctl --user daemon-reload && systemctl --user restart t3code.service` 并检测服务健康状态；若重启失败，**自动触发回滚恢复旧版本**

### 3. 查看备份与手动回滚

```bash
# 列出所有可回滚备份
./t3clean.py rollback-server --list

# 回滚到最近一次备份（仅恢复二进制与服务配置，保留当前数据库中的新会话）
./t3clean.py rollback-server

# 回滚到指定备份 ID，并同时还原备份时的 SQLite 数据库
./t3clean.py rollback-server --backup <backup-id> --restore-db
```

---

## 与官方 `t3code-bin` 共存及数据库回滚注意事项

### 桌面端共存设计

- **安装路径完全隔离**：
  - 程序目录：`/usr/lib/t3-code-no-telemetry/`
  - 启动命令：`/usr/bin/t3-code-no-telemetry`
  - 桌面入口：`/usr/share/applications/t3-code-no-telemetry.desktop`（应用菜单显示为 **T3 Code (No Telemetry)**）
  - 图标名称：`t3-code-no-telemetry`
  - `PKGBUILD` **不声明** `conflicts=('t3code-bin')`，可与 `t3code-bin` 同时安装对比测试
- **用户数据目录共享**：
  - 两者共享 `~/.t3/userdata/` 与 `~/.config/t3code-v2/`，无缝继承既有工作区、会话与设置。
  - 请勿在同一时刻并发启动官方 `t3code-bin` 与 `t3-code-no-telemetry`，避免两个进程同时写入同一 SQLite 数据库。

### 数据库备份与 `--restore-db` 提示

- `deploy-server` 每次升级前都会用 Python 标准库 `sqlite3.Connection.backup()` 在线热备 `~/.t3/userdata/statev2.sqlite` 到 `no-telemetry-backups/<backup-id>/`。
- **默认回滚（不带 `--restore-db`）不会覆盖现有 `statev2.sqlite`**，仅当新版本执行了不兼容的 SQLite Schema 迁移时才应显式传入 `--restore-db`。

---

## 运行测试

```bash
python3 -W error::SyntaxWarning -m unittest discover -s tests -v
bash -n aur/PKGBUILD
(cd aur && diff -u .SRCINFO <(makepkg --printsrcinfo))
namcap aur/PKGBUILD
```
