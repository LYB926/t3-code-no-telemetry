# t3-code-no-telemetry (`t3clean`)

纯 Python 3.10+ 标准库实现的 **T3 Code 去遥测源码构建、产物校验、Arch/CachyOS 打包、AUR 扁平导出与 Linux Server 安全部署/回滚工具链**（零第三方 Python 依赖）。

默认锁定上游稳定标签 `v0.1.12`，兼容 `v0.1.11`、`v0.1.12` 与 `main` 分支，所有补丁具备幂等性（基于 `// @t3-no-telemetry-patched` 标记）与锚点缺失快速失败保护（`PatchAnchorNotFoundError`）。

---

## 目录

- [去遥测补丁策略（6 条强制规则 + 2 条刻意保留项）](#去遥测补丁策略6-条强制规则--2-条刻意保留项)
- [快速开始（CLI 子命令一览）](#快速开始cli-子命令一览)
- [CachyOS / Arch Linux 本地 `makepkg` 构建](#cachyos--arch-linux-本地-makepkg-构建)
- [导出扁平化 AUR 仓库包（`export-aur`）](#导出扁平化-aur-仓库包export-aur)
- [Ubuntu / `aiseon` 服务器自构建、部署与回滚](#ubuntu--aiseon-服务器自构建部署与回滚)
- [与官方 `t3code-bin` 共存及数据库回滚注意事项](#与官方-t3code-bin-共存及数据库回滚注意事项)
- [运行测试](#运行测试)

---

## 去遥测补丁策略（6 条强制规则 + 2 条刻意保留项）

### 强制剥离的 6 条规则

| # | 目标文件 | 处理方式 |
|---|---|---|
| 1 | `apps/server/src/telemetry/AnalyticsService.ts` | 全文件替换为 No-Op Stub：保留 `AnalyticsService`、`make`、`layer` 导出签名，自动适配 `v0.1.11`（`record -> Effect<void>`）与 `v0.1.12+/main`（`record -> Effect<boolean>`），彻底移除 `ingest.t3.codes`、硬编码 PostHog Key（`phc_...`）、HTTP 上报、定时刷新与退出 flush。 |
| 2 | `apps/server/src/telemetry/Identify.ts` | 动态 No-Op Stub：`getTelemetryIdentity` 恒返回 `{ distinctId: null, installId: null }`，`getServerOwners` 恒返回 `[]`，不读写 `anonymous-id` / `ServerSecretStore` / `.codex` / `.claude.json`。在 `v0.1.11` 上自动保留纯本地 Base64 JWT 解码函数 `openAiUserId` 与旧导出以保证编译兼容。 |
| 3 | `apps/web/src/connection/platform.ts`<br>`packages/client-runtime/src/authorization/remote.ts` | 使用括号/语句平衡替换（非脆弱正则）：移除 `t3code:analytics-device-id` 的 `localStorage` 持久化逻辑并让 `analyticsDevice.id` 返回 `Option.none()`；让 `readClientOwnerParams` 恒返回 `Effect.succeed({})`。 |
| 4 | `packages/shared/src/relayTracing.ts` | 全文件替换为 No-Op Stub：保留所有导出符号，`layer()` 恒返回 `Layer.succeed(RelayClientTracer, Option.none())`，彻底移除 `OtlpTracer` 与 `X-Axiom-Dataset`（Axiom/OTLP 链路追踪）。 |
| 5 | `apps/desktop/src/updates/DesktopUpdatePolicy.ts` | 将 `unmanagedUpdatePolicy` 锁定为 `{ mode: "off", feedUrl: undefined, allowNightly: false, message: null }`，彻底禁用桌面端自动更新（可通过 `--keep-updater` 保留）。 |
| 6 | `apps/web/src/cloud/publicConfig.ts`<br>`apps/server/src/cloud/publicConfig.ts` | 强锁 `hasCloudPublicConfig` 为 `false`，`resolveRelayTracingConfig` / `resolveRelayClientTracingConfig` 恒返回 `null`，彻底切断 Clerk / T3 Connect 云端遥测与登录钩子（可通过 `--keep-cloud` 保留）。 |
| 构建防护 | `apps/web/src/index.css` | 在 `@import "tailwindcss";` 后注入 `@source not "../node_modules";`：防止从源码压缩包（无 `.git`）在外层仓库的被 `.gitignore` 忽略目录（如 `packaging/arch/src/`）构建时，Tailwind CSS v4（`@tailwindcss/oxide`）停用 `.gitignore` 过滤并扫描整个 `node_modules` 引发 20+ GiB OOM。 |
| 构建兼容 | `scripts/linux-glibc.ts` | 将上游写死的 Ubuntu 24.04 CI 上限 `const GLIBC_BUILD_CEILING = "2.39"` 改为支持 `T3CODE_GLIBC_BUILD_CEILING` 覆盖，并由构建器自动对齐宿主机 glibc 版本（如 Ubuntu 24.04 仍保持 `2.39`，Arch/CachyOS 自动对齐 `2.44`），保留完整 `readelf` 扫描与 `.deb` 的 `libc6` 依赖推导。 |

此外，构建阶段（`t3clean/builder.py`）会自动清理 `.env` / `.env.local` 并剥离环境变量中的 Clerk / Relay / OTLP / 更新仓库 / 签名凭据，同时注入 `T3CODE_TELEMETRY_ENABLED=false`、`DO_NOT_TRACK=1`、`T3CODE_TELEMETRY_CHANNEL=dev`、`T3CODE_WEB_SOURCEMAP=false`。

### 刻意保留的 2 项（规则 7 & 8）

以下两项为本地代码代理正常运行所需的公开只读元数据拉取，**默认不做修改**：

- **规则 7**：`apps/server/src/provider/ModelManifest.ts`（拉取模型目录/能力清单）
- **规则 8**：`apps/server/src/usage/UsageService.ts`（拉取模型价格表用于本地 Token 费用估算）及 `enableProviderUpdateChecks`

如果处于严格离线环境，可对构建产物执行 `python3 t3clean.py verify --artifact <path> --strict-offline` 进行离线审计。

---

## 快速开始（CLI 子命令一览）

```bash
./t3clean.py --help

# 1. 拉取上游源码（支持固定标签、latest-stable、分支或 commit）
./t3clean.py fetch --ref v0.1.12 --dest ./work/t3code
./t3clean.py fetch --ref latest-stable --dest ./work/t3code

# 2. 对源码目录执行幂等去遥测补丁（支持 --dry-run / --keep-updater / --keep-cloud / --json）
./t3clean.py patch --source ./work/t3code

# 3. 校验源码目录、解包目录或 Electron app.asar（流式二进制扫描，无需外部 asar CLI）
./t3clean.py verify --artifact ./work/t3code
./t3clean.py verify --artifact ./out/linux-unpacked

# 4. 完整构建（支持 desktop-dir / desktop-deb / desktop-appimage / server-sea / server-bundle）
./t3clean.py build --source ./work/t3code --skip-fetch --target desktop-dir --output-dir ./out

# 5. 导出 100% 扁平、可直接推送 AUR 的包目录
./t3clean.py export-aur --out /tmp/aur-t3-code-no-telemetry --ref v0.1.12 --pkgrel 1

# 6. 部署与回滚 Linux systemd 用户服务
./t3clean.py deploy-server --archive ./out/t3-0.1.12+notelemetry-linux-x64.tar.gz
./t3clean.py rollback-server --list
./t3clean.py rollback-server
```

---

## CachyOS / Arch Linux 本地 `makepkg` 构建

仓库内置的 [packaging/arch/PKGBUILD](packaging/arch/PKGBUILD) 为薄包装脚本，直接调用根目录的 `t3clean.py`：
- **零 AUR 构建依赖**：所有 `makedepends` 均来自 Arch/CachyOS 官方源；构建时若系统未安装 `vp >= 1.4.0`，会自动使用宿主机 `npm` 将最新预编译版 `vite-plus` 隔离安装到缓存目录（避免 AUR `vite-plus-bin` 过旧或源码版 `vite-plus` 与系统 `rust` 的 `rustup` 冲突）。
- **轻量源码压缩包**：直接下载 GitHub `archive/refs/tags/v0.1.12.tar.gz`（仅几十 MB），避开 `makepkg` `git+https://` 全量 `--mirror`（~3.5 GiB）。
- 不依赖 `fuse2`，且与官方 `t3code-bin` 完全共存。

### 基础构建（默认干净沙箱缓存 `${srcdir}/.cache`）

```bash
cd packaging/arch
makepkg -s
# 确认 check() 验净通过后手动安装（或直接用 makepkg -si 一键编译并安装）：
# sudo pacman -U t3-code-no-telemetry-*.pkg.tar.zst
```

### 开启持久化构建缓存（推荐日常反复升级使用）

默认遵循 AUR/clean-chroot 规范将缓存放在 `${srcdir}/.cache`。通过 `T3CLEAN_CACHE_DIR` 可跨次复用隔离 `vp`、`ELECTRON_CACHE`、`ELECTRON_BUILDER_CACHE`、`CARGO_HOME`、`PNPM_HOME` 与 `npm_config_store_dir`，大幅加速后续重编：

```bash
cd packaging/arch
T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" makepkg -si
```

### 可选开关（默认全部关闭）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `T3CODE_REF` | `v0.1.12` | 指定上游 tag / branch / commit（如 `T3CODE_REF=main` 或 `T3CODE_REF=v0.1.13`） |
| `T3CLEAN_CACHE_DIR` | `${srcdir}/.cache` | 指定跨次构建持久化缓存目录 |
| `T3CLEAN_BUILD_DEB` | `0` | 设为 `1` 时额外构建一份 Linux `.deb` 安装包（默认输出到 `${srcdir}/out-deb`，可用 `T3CLEAN_DEB_OUT_DIR` 指定目录） |
| `T3CLEAN_USE_SYSTEM_ELECTRON` | `0` | 设为 `1` 时剥离内嵌 Electron 二进制，仅打包 `resources/`（含 `app.asar` 与 extraResources），并写入 `.use-system-electron` 标记改用系统 Electron（**注意**：需自行保证系统 Electron 版本与 `node-pty` 等原生插件 ABI 匹配） |
| `T3CLEAN_SYSTEM_ELECTRON_BIN` | `electron` | 配合 `T3CLEAN_USE_SYSTEM_ELECTRON=1` 指定系统 Electron 可执行文件名（如 `electron34`） |
| `T3CLEAN_KEEP_UPDATER` | `0` | 设为 `1` 时保留桌面端自动更新策略 |
| `T3CLEAN_KEEP_CLOUD` | `0` | 设为 `1` 时保留 Clerk / T3 Connect 云端配置 |
| `T3CLEAN_STRICT_OFFLINE` | `0` | 设为 `1` 时在 `check()` 阶段启用 `--strict-offline` 校验 |
| `T3CLEAN_JOBS` | 自动 | 手动指定 Rust / Rolldown / npm 编译并发数（如 `T3CLEAN_JOBS=4`，对应 CLI `--jobs 4`）；不设置时仅在 `≤ 16 GiB` 内存或可用内存偏紧时自动降并发，32/64 GiB 高配机保持全速 |

### 低内存机器（16 GiB 及以下）防卡死编译建议

前端打包（Vite+/Rolldown + React Compiler + Tailwind Oxide）与 Rust 原生组件（`t3-resource-monitor`）在并发拉满时会产生较高的原生堆内存峰值。构建器默认采取 **“自适应限并发 + 低内存预警”** 策略（不在脚本里硬编码 `MemoryMax` 或 `ulimit -v`，以保证 clean-chroot / Docker / CI 兼容性且避免误杀正常构建）：

- **自动降并发**：当检测到宿主机物理内存 `≤ 16 GiB`（或可用内存 `< 6 GiB`）且用户未手动指定并发环境变量时，自动设置 `RAYON_NUM_THREADS`、`CARGO_BUILD_JOBS`（默认收敛至 `4`；`≤ 10 GiB` 或 `< 4 GiB` 可用时收敛至 `2`）与 `npm_config_child_concurrency`（`1~2`）；随时可通过 `--jobs N` / `T3CLEAN_JOBS=N` 或显式环境变量覆盖。
- **低内存预警**：编译前若 `/proc/meminfo` 显示 `MemAvailable < 6 GiB`（尤其仅有 zram 无磁盘 swap），会在 stderr 打印友好提示（不中断构建）。
- **16 GiB 桌面端强保护（可选）**：如果在纯 zram、未启用 `systemd-oomd`/`earlyoom` 的桌面环境下编译，建议先关掉占内存的浏览器/Electron 应用，并用 `systemd-run` 给构建进程树套一个 cgroup 内存上限（就算爆内存也只杀编译进程，绝不拖死桌面）：

```bash
cd packaging/arch
T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" \
systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=10G \
  makepkg -Csi
```

示例（使用持久化缓存、构建 `main` 分支、额外输出 `.deb` 并使用系统 Electron）：

```bash
cd packaging/arch
T3CODE_REF=main \
T3CLEAN_CACHE_DIR="${HOME}/.cache/t3-code-no-telemetry" \
T3CLEAN_BUILD_DEB=1 \
T3CLEAN_USE_SYSTEM_ELECTRON=1 \
T3CLEAN_SYSTEM_ELECTRON_BIN=electron \
makepkg -s
```

---

## 导出扁平化 AUR 仓库包（`export-aur`）

AUR Git 仓库要求所有源文件位于顶层目录，不允许存在 `../../` 相对引用或子目录。使用 `export-aur` 可一键生成 **100% 扁平、零子目录、可直接 `git push` 到 AUR** 的目录：

```bash
./t3clean.py export-aur --out /tmp/aur-t3-code-no-telemetry --ref v0.1.12 --pkgrel 1
```

导出目录仅包含以下 5 个顶层文件：

1. `PKGBUILD` — 自包含构建脚本，无任何 `../` 路径，内置全部本地文件的真实 `sha256sums`
2. `.SRCINFO` — 与 `PKGBUILD` 元数据、源文件与校验和完全一致
3. `t3clean-standalone.py` — 由 `t3clean/__init__.py`、`t3clean/patcher.py`、`t3clean/verifier.py` 自动合并生成的独立单文件 CLI（支持 `patch` 与 `verify`）
4. `t3-code-no-telemetry.sh` — Wayland/X11 自动检测与防御性环境变量启动脚本
5. `t3-code-no-telemetry.desktop` — 独立桌面入口文件

若已克隆 AUR 仓库，可直接将 `--out` 指向该仓库目录（不会删除 `.git/`）：

```bash
./t3clean.py export-aur --out ~/aur/t3-code-no-telemetry --ref v0.1.12 --pkgrel 1
```

---

## Ubuntu / `aiseon` 服务器自构建、部署与回滚

在 Ubuntu 服务器（例如 `aiseon`）上，系统自带的 Node.js 版本通常低于上游要求的 `26.13.1`。使用 `--bootstrap-toolchain` 会在指定缓存目录内下载官方 Node.js 发行版并校验 SHA-256，隔离安装 `vite-plus`，**绝不触碰 `/usr/bin/node`**。

### 1. 构建 `server-sea`（单文件可执行归档）

构建 `server-sea` 需要系统具备 `cargo`、`patchelf` 与 `dpkg-query`（Ubuntu/Debian 默认已含 `dpkg-query`）：

```bash
./t3clean.py build \
  --ref v0.1.12 \
  --target server-sea \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "${HOME}/.cache/t3clean-server" \
  --output-dir ./dist-server
```

如果目标机器不方便编译 SEA，也可以构建基于隔离 Node 运行时的 `server-bundle`：

```bash
./t3clean.py build \
  --ref v0.1.12 \
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
- 将旧版本运行时、`t3code.service`、`service-state.json`、`~/.local/bin/t3` 及在线 `sqlite3` 热备的 `userdata/statev2.sqlite` 完整备份至 **`~/.t3/runtime/no-telemetry-backups/<backup-id>/`**（刻意置于 `versions/` 目录之外，避免上游 `pruneRuntimesOnStart` 自动清理掉回滚副本）
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
  - 若曾运行过官方版本，`~/.t3/userdata/anonymous-id` 可能已存在；本版本代码层面已不再读取或上报该文件，也可手动删除或通过 `deploy-server` 自动清除。

### 数据库备份与 `--restore-db` 提示

- `deploy-server` 每次升级前都会用 Python 标准库 `sqlite3.Connection.backup()` 在线热备 `~/.t3/userdata/statev2.sqlite` 到 `no-telemetry-backups/<backup-id>/`。
- **默认回滚（不带 `--restore-db`）不会覆盖现有 `statev2.sqlite`**，以避免误删升级后新产生的对话记录。
- 仅当新版本执行了不兼容的 SQLite Schema 迁移、导致旧版本无法启动时，才应显式传入 `--restore-db` 还原备份时的数据库快照。

---

## 运行测试

```bash
python3 -m unittest discover -s tests -v
bash -n packaging/arch/PKGBUILD packaging/arch/t3-code-no-telemetry.sh
```
