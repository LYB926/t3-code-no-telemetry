# t3-code-no-telemetry

[简体中文](README.md) | **English** | [Patch Reference](PATCHES.md)

A telemetry-free source build of **T3 Code** for Linux, with usage analytics reporting, device fingerprinting, cloud trace logging, built-in auto-updates, and cloud auth hooks stripped out so you can run T3 Code purely locally.

> **Verified Environments** (`v0.1.13`):
> - **Desktop**: `CachyOS x86_64` (`Linux 7.3.6-1-cachyos`) — built from source and running locally;
> - **Server**: `Ubuntu 24.04.4 LTS (Noble Numbat) x86_64` (`Linux 6.8.0-110-generic`) — single-executable ELF (`server-sea`) built from source, upgraded with hot backups, and running under `systemd`;
> - **Remote connectivity**: both the de-telemetrized T3 Code desktop app on CachyOS and web browsers connect cleanly to the remote Ubuntu workstation server.

The build toolchain uses Python 3.10+ standard library only (zero third-party Python dependencies). It provides both a minimal `PKGBUILD` + `.patch` AUR recipe (on the `master` branch) for Arch / CachyOS and a standalone `t3clean.py` CLI for generic Linux distributions.

---

## 1. What's Patched

### Affected Files

All source modifications are split into two standard `.patch` files that you can inspect line by line:

- **Telemetry & outbound cleanup** ([`aur/0001-strip-telemetry.patch`](aur/0001-strip-telemetry.patch), 8 files):
  - `apps/server/src/telemetry/AnalyticsService.ts`
  - `apps/server/src/telemetry/Identify.ts`
  - `apps/web/src/connection/platform.ts`
  - `packages/client-runtime/src/authorization/remote.ts`
  - `packages/shared/src/relayTracing.ts`
  - `apps/desktop/src/updates/DesktopUpdatePolicy.ts`
  - `apps/web/src/cloud/publicConfig.ts`
  - `apps/server/src/cloud/publicConfig.ts`
- **Linux source-build compatibility fixes** ([`aur/0002-linux-build-compat.patch`](aur/0002-linux-build-compat.patch), 2 files):
  - `apps/web/src/index.css` (excludes `../node_modules` so Tailwind CSS v4 does not scan dependencies and blow up memory when building from a source tarball)
  - `scripts/linux-glibc.ts` (replaces upstream's hardcoded Ubuntu 24.04 glibc 2.39 ceiling with host-detected glibc so rolling distros like Arch / CachyOS can package cleanly)

### Telemetry & Default Outbound Summary

| Affected File(s) | Original Behavior | Status | Modification | Rationale |
|---|---|---|---|---|
| `apps/server/src/telemetry/AnalyticsService.ts` | Sends usage events to `ingest.t3.codes` using a hardcoded PostHog key | **Stripped** | Removed endpoint URL, API key, and network sender; replaced record/flush calls with empty implementations that ignore all events | Cuts off all server-side product analytics reporting |
| `apps/server/src/telemetry/Identify.ts` | Generates and persists a local `anonymous-id` and reads linked account IDs for tracking | **Stripped** | Stopped reading or writing local tracking ID files; identity and owner lookups always return empty | Prevents creating a persistent tracking fingerprint on the machine |
| `apps/web/src/connection/platform.ts`<br>`packages/client-runtime/src/authorization/remote.ts` | Stores a device ID (`t3code:analytics-device-id`) in browser `localStorage` and attaches it to WebSocket URLs | **Stripped** | Removed frontend device ID storage and cleared owner/device tracking parameters from WebSocket requests | Stops the frontend from generating or transmitting a device fingerprint |
| `packages/shared/src/relayTracing.ts` | Exports OpenTelemetry trace spans to Axiom (with `X-Axiom-Dataset`) when relay tracing is configured | **Stripped** | Removed trace exporting and kept normal execution pass-through only | Prevents sending diagnostic trace logs to third-party services |
| `apps/desktop/src/updates/DesktopUpdatePolicy.ts` | Enables desktop auto-update and nightly update checks by default | **Modified** | Locked default update policy to disabled (`mode: "off"`) and turned off nightly update checks | Prevents silent background update checks and avoids bypassing your system package manager |
| `apps/web/src/cloud/publicConfig.ts`<br>`apps/server/src/cloud/publicConfig.ts` | Reads Clerk login keys and T3 Connect cloud relay/tracing settings | **Modified** | Locked cloud config flags to disabled and relay tracing config resolvers to empty | Disables cloud account login and relay hooks for local-only use |
| `apps/server/src/provider/ModelManifest.ts` | Fetches the public model list and capabilities from GitHub | **Kept as-is** | Unchanged | Read-only public metadata so local agents recognize newly released models and context windows |
| `apps/server/src/usage/UsageService.ts` | Fetches LiteLLM's public model pricing table from GitHub | **Kept as-is** | Unchanged | Read-only public pricing data used only for local token cost estimates; no user data is uploaded |

> For the complete file-by-file technical breakdown, call sites, and rationale, see **[PATCHES.md](PATCHES.md)**.

In addition to source patching, build and runtime scripts apply two extra layers of protection:
- **At build time**: Removes `.env` / `.env.local` from the source root, unsets Clerk / Relay / PostHog / updater / signing environment variables, and scans the final output (including `app.asar`) to verify no forbidden hosts or keys remain.
- **At runtime**: The launcher exports `T3CODE_TELEMETRY_ENABLED=false` and `DO_NOT_TRACK=1` on every start.

> [!WARNING]
> **Back up your data and active work before installing or switching!**
>
> - This package installs to isolated paths and does **not** conflict with `t3code-bin` or `t3code-nightly-bin`, but by default it shares the same local user data directories (`~/.t3/userdata/statev2.sqlite` and `~/.config/t3code-v2/`).
> - Fully quit any running official T3 Code instance first, and back up your database before switching:
>   ```bash
>   cp ~/.t3/userdata/statev2.sqlite ~/.t3/userdata/statev2.sqlite.bak
>   ```
> - **Do not run** the official build and this build at the same time on the same data directory, to avoid concurrent writes to the same SQLite database.
> - If you are coming from a newer **Nightly** build (e.g., `0.1.13-nightly`), its SQLite schema may be newer than the current stable release. Use the isolated launch command below first so an older stable build does not touch your Nightly database.

---

## 2. Arch / CachyOS Installation (AUR Recipe)

This package is not yet published on `aur.archlinux.org`. You can clone the **`master` branch** directly—it contains only the 6 flat AUR files (`PKGBUILD`, `.SRCINFO`, two `.patch` files, `LICENSE`, and `.gitignore`)—and build with `makepkg`:

```bash
# Build inside your home directory (not /tmp, which is tmpfs in RAM and breaks pnpm hardlink cache reuse)
git clone --branch master --single-branch https://github.com/LYB926/t3-code-no-telemetry.git ~/t3-code-no-telemetry-aur
cd ~/t3-code-no-telemetry-aur
makepkg -si
```

> All build dependencies (`git`, `python`, `nodejs>=26.12.1`, `npm`, `gcc`, `make`, `rust`, `pkgconf`, `imagemagick`) come from official Arch / CachyOS repositories—no extra AUR makedepends are needed. `vite-plus` is automatically installed in an isolated build cache pinned to upstream's version without touching global system directories.

Once installed, launch `t3-code-no-telemetry` from the terminal or **T3 Code (No Telemetry)** from your application menu.

### Helpful Build & Trial Options

- **Reuse build cache across upgrades (recommended)**: Keeps downloaded packages and Electron/Cargo caches so future rebuilds are much faster:
  ```bash
  T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" makepkg -Csi
  ```
- **Low-RAM protection (16 GiB or less)**: Build concurrency automatically throttles when available memory is tight (override with `T3CLEAN_JOBS=4`). On desktop systems with heavy browsers open, you can also cap memory via `systemd-run`:
  ```bash
  T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" \
  systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=10G \
    makepkg -Csi
  ```
- **Fully isolated trial alongside official / Nightly (zero risk; can even run side by side)**:
  ```bash
  T3CODE_HOME="$HOME/.t3-clean" t3-code-no-telemetry --user-data-dir="$HOME/.config/t3code-no-telemetry"
  ```
- **Installing an older version**: Download the frozen `t3-code-no-telemetry-v<ver>-aur-recipe.tar.gz` from [Releases](https://github.com/LYB926/t3-code-no-telemetry/releases), extract it, and run `makepkg -si`.

---

## 3. Generic Linux (Ubuntu / Debian, etc.) Patching & Building

Clone the default `dev` branch and use `t3clean.py` directly:

```bash
git clone https://github.com/LYB926/t3-code-no-telemetry.git
cd t3-code-no-telemetry
```

### 1. Fetch, Patch, and Verify Source Only

```bash
# Fetch upstream's latest stable release
./t3clean.py fetch --ref latest-release --dest ./work/t3code

# Apply de-telemetry patches (add --dry-run to preview without writing)
./t3clean.py patch --source ./work/t3code

# Scan the tree to confirm zero telemetry indicators remain
./t3clean.py verify --artifact ./work/t3code
```

### 2. Build Desktop Artifacts

If your distro's Node.js is older than upstream's requirement (common on Ubuntu), pass `--bootstrap-toolchain` to download official Node.js and pinned `vite-plus` into the cache directory without modifying `/usr/bin/node`.

```bash
# Build unpacked Linux desktop directory (linux-unpacked)
./t3clean.py build \
  --ref latest-release \
  --target desktop-dir \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean" \
  --output-dir ./out

# For .deb or AppImage packages, change --target to desktop-deb or desktop-appimage
```

Useful optional flags:
- `--arch arm64`: build for ARM64
- `--jobs 4`: cap build concurrency manually
- `--keep-updater` / `--keep-cloud`: opt in to keeping auto-updates or cloud hooks
- `--use-system-electron` (with `desktop-dir`): strip bundled Electron and stage `resources/` for a system Electron binary

### 3. Build, Deploy, and Roll Back Server (e.g., Ubuntu Server)

```bash
# Option A: Single-executable server archive (requires cargo, patchelf, dpkg-query)
./t3clean.py build \
  --ref latest-release \
  --target server-sea \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean-server" \
  --output-dir ./dist-server

# Option B: Portable bundle with isolated Node runtime
./t3clean.py build \
  --ref latest-release \
  --target server-bundle \
  --arch x64 \
  --bootstrap-toolchain \
  --cache-dir "$HOME/.cache/t3clean-server" \
  --output-dir ./dist-server

# Safe systemd user-service deployment (verifies archive, backs up runtime & SQLite DB, auto-rolls back on failure)
./t3clean.py deploy-server --archive ./dist-server/t3-*.tar.gz --dry-run   # preview
./t3clean.py deploy-server --archive ./dist-server/t3-*.tar.gz             # deploy

# List backups or roll back manually (keeps newer DB sessions by default; add --restore-db to restore DB too)
./t3clean.py rollback-server --list
./t3clean.py rollback-server
```

---

## 4. Additional Notes

### Branch Layout & Automated Updates

- **`dev` (default branch)**: Full toolchain source, unit tests, GitHub Actions workflows, and the `aur/` directory.
- **`master` (flat orphan branch)**: Contains only the 6 flat root-level files required by AUR—zero subdirectories, ready for `makepkg` and direct AUR publishing.
- **Daily release tracking**: [`.github/workflows/update-check.yml`](.github/workflows/update-check.yml) checks `pingdotgg/t3code`'s latest stable GitHub Release daily. When a new version arrives and all patches, full-tree verification, `.SRCINFO`, `namcap`, and unit tests pass, it opens a PR against `dev` for human review; if upstream changes break a patch anchor or introduce a new leak, it opens a deduplicated GitHub Issue so it can be fixed promptly.
- **Automated sync & recipe releases**: Merging `aur/` updates into `dev` triggers [`.github/workflows/sync-master.yml`](.github/workflows/sync-master.yml), which syncs the 6 flat files to `master` and publishes a lightweight source-recipe GitHub Release (no prebuilt binaries, keeping everything easy to audit and build from upstream source).
- **Latest-stable focus**: `t3clean/patcher.py` targets only the current latest stable release instead of accumulating legacy compatibility branches; historical versions remain permanently reproducible via their frozen GitHub Releases.

### Previewing Upstream Nightlies & Exporting AUR Directories (`export-aur`)

To check whether an upcoming upstream Nightly changes any telemetry code, or to generate a minimal `.patch`-based AUR directory for a specific tag:

```bash
# Check upstream's latest stable release tag
./t3clean.py latest-release

# Quick ~5-second check against an upstream Nightly tag
./t3clean.py fetch --ref v0.1.13-nightly.20261125.5258 --dest ~/t3-nightly-check
./t3clean.py patch --source ~/t3-nightly-check
./t3clean.py verify --artifact ~/t3-nightly-check

# Export minimal patch-based AUR files for the latest stable release
./t3clean.py export-aur --out ./aur --ref latest-release --pkgrel 1

# Or export a Nightly into a separate folder and test-build it with makepkg
./t3clean.py export-aur --out ~/t3-aur-nightly --ref v0.1.13-nightly.20261125.5258
cd ~/t3-aur-nightly && T3CLEAN_CACHE_DIR="$HOME/.cache/t3-code-no-telemetry" makepkg -s
```

If you already have the upstream tarball locally, pass `--source-tarball /path/to/t3code-v0.1.12.tar.gz` to hash and generate patches completely offline.

### Strict Offline Audit

To also flag the intentionally kept read-only model manifest and pricing table URLs:

```bash
./t3clean.py verify --artifact ./work/t3code --strict-offline
```

### Running Tests & Packaging Checks

```bash
python3 -W error::SyntaxWarning -m unittest discover -s tests -v
bash -n aur/PKGBUILD
(cd aur && diff -u .SRCINFO <(makepkg --printsrcinfo))
namcap aur/PKGBUILD
```
