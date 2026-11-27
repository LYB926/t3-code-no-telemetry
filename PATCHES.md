# Patch Reference & Telemetry Audit

This document provides a complete, file-by-file breakdown of every telemetry, tracking, and default outbound network path audited in upstream [T3 Code](https://github.com/pingdotgg/t3code), explaining what each piece does, how this project handles it, and why.

The actual diffs applied during AUR builds live in two standard patch files that you can inspect line by line:
- [`aur/0001-strip-telemetry.patch`](aur/0001-strip-telemetry.patch) — removes telemetry, device tracking, auto-updates, and cloud auth hooks (8 files)
- [`aur/0002-linux-build-compat.patch`](aur/0002-linux-build-compat.patch) — Linux source-build compatibility fixes (2 files)

The patch generator itself lives in [`t3clean/patcher.py`](t3clean/patcher.py), and the post-build scanner lives in [`t3clean/verifier.py`](t3clean/verifier.py).

---

## Overview

| # | Area | Target File(s) | Status | Patch File |
|---|---|---|---|---|
| 1 | Product analytics (`ingest.t3.codes`) | `apps/server/src/telemetry/AnalyticsService.ts` | Stripped | `0001-strip-telemetry.patch` |
| 2 | Server install fingerprint & owner linking | `apps/server/src/telemetry/Identify.ts` | Stripped | `0001-strip-telemetry.patch` |
| 3 | Web device ID & WebSocket tracking params | `apps/web/src/connection/platform.ts`<br>`packages/client-runtime/src/authorization/remote.ts` | Stripped | `0001-strip-telemetry.patch` |
| 4 | Relay OTLP tracing (Axiom) | `packages/shared/src/relayTracing.ts` | Stripped | `0001-strip-telemetry.patch` |
| 5 | Desktop auto-updater | `apps/desktop/src/updates/DesktopUpdatePolicy.ts` | Disabled | `0001-strip-telemetry.patch` |
| 6 | Clerk auth & T3 Connect cloud hooks | `apps/web/src/cloud/publicConfig.ts`<br>`apps/server/src/cloud/publicConfig.ts` | Disabled | `0001-strip-telemetry.patch` |
| 7 | Model capability manifest & provider CLI checks | `apps/server/src/provider/ModelManifest.ts` | Kept as-is | — |
| 8 | Model pricing table (LiteLLM) | `apps/server/src/usage/UsageService.ts` | Kept as-is | — |
| 9 | Tailwind CSS v4 source-scan OOM fix | `apps/web/src/index.css` | Patched | `0002-linux-build-compat.patch` |
| 10 | Linux glibc build ceiling override | `scripts/linux-glibc.ts` | Patched | `0002-linux-build-compat.patch` |

---

## Part 1: Telemetry & Outbound Hooks Removed (`0001-strip-telemetry.patch`)

### 1. Product Analytics Batch Reporting (PostHog -> `ingest.t3.codes`)

- **Target file**: `apps/server/src/telemetry/AnalyticsService.ts`
- **What upstream does**:
  - Embeds a hardcoded PostHog API key (`phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m`) and ingestion endpoint (`https://ingest.t3.codes`).
  - Enabled by default outside CI unless explicitly opted out via `T3CODE_TELEMETRY_ENABLED=false`, `DO_NOT_TRACK=1`, or `/etc/t3code/policy.json`.
  - Uses Effect's built-in `HttpClient` (not a third-party PostHog SDK) to POST batched events to `https://ingest.t3.codes/batch/` every 10 seconds (up to 20 events per batch), with a final flush on exit and up to 30 minutes of retry backoff.
  - Mounted once at server startup (`apps/server/src/server.ts`) and used as the single reporting exit by all server event callers, including startup heartbeats (`serverRuntimeStartup.ts`), turn/feature usage (`ws.ts`, `FeatureUsage.ts`), provider usage (`ProviderUsageAnalytics.ts`), delegated task and MCP tool metrics (`DelegatedTaskAnalytics.ts`, `McpHttpServer.ts`), ChatGPT auth transfers (`CodexChatGptAuth.ts`), guest sharing (`GuestShareService.ts`), and agent feedback (`FeedbackStore.ts`).
- **How we patch it**:
  - Replace `AnalyticsService.ts` with a minimal stub that preserves the public `AnalyticsService`, `make`, and `layer` exports while removing the hardcoded key, endpoint URL, HTTP client calls, background flush loop, and exit hook.
  - `record(...)` immediately returns `false`, while `flush` and `recordOwners(...)` do nothing.
- **Result**:
  - Every analytics caller across the server is silenced in one place without touching application logic. Returning `false` from `record` lets callers such as `FeedbackStore` cleanly recognize that nothing was sent, and no background timer is spawned.

---

### 2. Server Anonymous Install Fingerprint & Account Linking

- **Target file**: `apps/server/src/telemetry/Identify.ts`
- **What upstream does**:
  - On first launch, `getTelemetryIdentity` generates a persistent random UUIDv4, writes it to the local state directory (`anonymous-id`), and SHA-256 hashes it into `distinctId` and `installId`.
  - `getServerOwners` reads any linked T3 Connect account ID (`CLOUD_LINKED_USER_ID`) from the local `ServerSecretStore`, hashes it, and passes it to `AnalyticsService.recordOwners`.
  - *(Note: Earlier T3 Code releases also read `~/.claude.json`, `~/.codex/auth.json`, or `~/.gemini` for telemetry identity; upstream has already removed that code in current releases.)*
- **How we patch it**:
  - Replace `Identify.ts` with a zero-I/O stub that keeps all exported types and tagged error classes intact (`TelemetryOwnerSource`, `TelemetryOwner`, `TelemetryAnonymousIdGenerationError`, `TelemetryAnonymousIdPersistenceError`).
  - `getTelemetryIdentity` always returns `{ distinctId: null, installId: null }` and `getServerOwners` always returns `[]`.
- **Result**:
  - T3 Code never creates or reads `~/.t3/userdata/anonymous-id` and never pulls account IDs out of the secret store for analytics. This also serves as a second safety layer behind `AnalyticsService`.

---

### 3. Web Client Device Fingerprint & WebSocket Tracking Parameters

- **Target files**:
  - `apps/web/src/connection/platform.ts`
  - `packages/client-runtime/src/authorization/remote.ts`
- **What upstream does**:
  - `platform.ts` creates and stores a permanent random device ID in browser/Electron `localStorage` under the key `t3code:analytics-device-id`.
  - `remote.ts` hashes that device ID (together with any active T3 Connect account ID) in `readClientOwnerParams` and appends `clientOwner` and `clientDevice` query parameters to WebSocket connection URLs. The server then reads those parameters in `apps/server/src/ws.ts` and forwards them to `AnalyticsService.recordOwners`.
  - **Why this matters**: This frontend path does **not** check `T3CODE_TELEMETRY_ENABLED=false` or `policy.json`. Even with server telemetry disabled locally, connecting the unpatched client to an unpatched remote T3 Server would still transmit a persistent device fingerprint.
- **How we patch it**:
  - In `platform.ts`, remove `ANALYTICS_DEVICE_ID_KEY` and replace `analyticsDevice` so `analyticsDevice.id` always resolves to `Option.none()`.
  - In `remote.ts`, replace `readClientOwnerParams` so it always returns `{}`.
- **Result**:
  - No tracking ID is written to `localStorage`, and WebSocket URLs no longer include `clientOwner` or `clientDevice` hashes. Non-tracking protocol parameters (such as client surface and version negotiation) remain untouched so normal compatibility is preserved.

---

### 4. T3 Connect / Relay OTLP Tracing (Axiom)

- **Target file**: `packages/shared/src/relayTracing.ts`
- **What upstream does**:
  - Provides the shared `OtlpTracer` exporter used by the server (`apps/server/src/cloud/relayTracing.ts`), web UI (`apps/web/src/lib/runtime.ts`), and CLI.
  - When OTLP tracing config is supplied at build time or runtime (`T3CODE_RELAY_CLIENT_OTLP_TRACES_*` / `VITE_RELAY_OTLP_TRACES_*`), sends JSON trace spans with `Authorization: Bearer <token>` and `X-Axiom-Dataset: <dataset>` headers.
  - While the open-source repo does not hardcode an Axiom token, stray environment variables at build or runtime could still activate it.
- **How we patch it**:
  - Replace `packages/shared/src/relayTracing.ts` with a pass-through stub that preserves every exported type and helper (`RelayClientTracingConfig`, `RelayClientTracingResource`, `RelayClientTracer`, `withRelayClientTracing`, `withLocalTracing`, `nonInterferingTracer`, `layer`).
  - `layer(...)` always returns `Option.none()` for `RelayClientTracer`, tracing wrappers return the original effect unchanged, and all `OtlpTracer` / `X-Axiom-Dataset` code is removed.
  - Build scripts also unset all `*_OTLP_*` variables before compiling.
- **Result**:
  - OTLP/Axiom tracing is completely stripped across desktop, web, and server bundles, and cannot be re-enabled via environment variables.

---

### 5. Desktop Auto-Updater (`electron-updater`)

- **Target file**: `apps/desktop/src/updates/DesktopUpdatePolicy.ts`
- **What upstream does**:
  - When no enterprise policy is configured, `unmanagedUpdatePolicy` defaults to `{ mode: "auto", feedUrl: undefined, allowNightly: true, message: null }`.
  - When an update repository or `app-update.yml` is present, `DesktopUpdates.ts` checks GitHub Releases 15 seconds after launch and every 4 hours thereafter.
- **How we patch it**:
  - Change `unmanagedUpdatePolicy` to `{ mode: "off", feedUrl: undefined, allowNightly: false, message: null }`.
  - Clear `T3CODE_DESKTOP_UPDATE_REPOSITORY` and `GITHUB_REPOSITORY` during build, and verify `app-update.yml` is not present in the final artifact.
  - *(When building directly via `t3clean.py build`, you can opt back in with `--keep-updater`.)*
- **Result**:
  - The desktop app performs no background update polling against GitHub Releases and leaves upgrades strictly to your package manager (`pacman` / `makepkg`).

---

### 6. Cloud Account Auth (Clerk) & T3 Connect Relay Hooks

- **Target files**:
  - `apps/web/src/cloud/publicConfig.ts`
  - `apps/server/src/cloud/publicConfig.ts`
- **What upstream does**:
  - Reads Clerk publishable keys and T3 Connect relay URLs from `.env`, `.env.local`, build-time variables (`scripts/lib/public-config.ts`), or runtime environment variables.
  - When `hasCloudPublicConfig` is `true`, mounts `ClerkProvider` in the web UI and enables T3 Connect cloud login and relay features.
  - *(Note: `apps/desktop/src/app/DesktopClerk.ts` is intentionally **not** deleted because it also manages Electron's single-instance lock and `t3code://` deep-link handler; without cloud config, it makes no external network calls.)*
- **How we patch it**:
  - Lock `hasCloudPublicConfig` to `false` and lock `resolveRelayTracingConfig` / `resolveRelayClientTracingConfig` to return `null` in both web and server.
  - Remove `.env` / `.env.local` and scrub all `T3CODE_CLERK_*`, `VITE_CLERK_*`, `EXPO_PUBLIC_CLERK_*`, `T3CODE_RELAY_*`, and `VITE_T3CODE_RELAY_*` variables before building.
  - *(When building directly via `t3clean.py build`, you can opt back in with `--keep-cloud`.)*
- **Result**:
  - Cloud login and relay hooks stay permanently disabled even if stray environment variables exist on the host, while single-instance locking and local desktop behavior remain intact.

---

## Part 2: Read-Only Metadata Fetches Kept Intentionally

These two outbound requests download **public, static, read-only JSON files** and send no user or device telemetry. They are left untouched by default so local coding agents continue to work smoothly.

### 7. Model Capability Manifest & Provider CLI Checks

- **Primary file**: `apps/server/src/provider/ModelManifest.ts` (controlled by `enableProviderUpdateChecks` in `packages/contracts/src/settings.ts`)
- **What it does**:
  - Periodically fetches `https://raw.githubusercontent.com/pingdotgg/t3code/main/apps/server/src/provider/model-manifest.json` so T3 Code recognizes newly released model IDs and context limits without waiting for an app rebuild.
  - When `enableProviderUpdateChecks` is enabled in app settings, checks upstream release metadata for installed provider CLIs (Claude, Codex, etc.).
- **Why we keep it**:
  - Purely read-only public metadata; you can turn off `enableProviderUpdateChecks` anytime in T3 Code's settings if you prefer completely quiet local operation.

### 8. Model Pricing Table (LiteLLM)

- **Primary file**: `apps/server/src/usage/UsageService.ts`
- **What it does**:
  - When you open the Usage view, downloads `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` (cached for 24 hours) to calculate local dollar cost estimates from your token counts.
- **Why we keep it**:
  - Downloads a public static pricing file and sends no usage data outward.

> If you need to audit a build for a strictly air-gapped environment, run `./t3clean.py verify --artifact <path> --strict-offline`, which also flags both URLs above.

---

## Part 3: Linux Source-Build Compatibility Patches (`0002-linux-build-compat.patch`)

These two tiny patches do not change app features; they fix Linux source-build issues on rolling distributions and tarball builds.

### 9. Tailwind CSS v4 `node_modules` Scan Guard

- **Target file**: `apps/web/src/index.css`
- **Why it's needed**:
  - When building from a GitHub source tarball (which has no `.git` folder) extracted inside a directory ignored by an outer `.gitignore`, Tailwind CSS v4 (`@tailwindcss/oxide`) disables `.gitignore` filtering and scans the entire `node_modules` tree, causing 20+ GiB RAM spikes and OOM crashes.
- **What we change**:
  - Insert `@source not "../node_modules";` immediately after `@import "tailwindcss";`.

### 10. Host-Aware Linux glibc Build Ceiling

- **Target file**: `scripts/linux-glibc.ts`
- **Why it's needed**:
  - Upstream CI builds on Ubuntu 24.04 and hardcodes `const GLIBC_BUILD_CEILING = "2.39"`, causing `electron-builder`'s post-pack check to abort on rolling Linux distros (Arch, CachyOS, Fedora, Debian 13+) where native addons like `node-pty` naturally link against the host's newer glibc.
- **What we change**:
  - Replace the hardcoded literal with `process.env.T3CODE_GLIBC_BUILD_CEILING?.trim() || "99.0"`. Both `PKGBUILD` and `t3clean.py build` automatically detect the host glibc version via `os.confstr("CS_GNU_LIBC_VERSION")` and pass it in, preserving full `readelf` scanning and `.deb` dependency detection.

---

## Part 4: Defense-in-Depth Safeguards

1. **Build environment scrubbing**: Both `PKGBUILD` and `t3clean/builder.py` delete `.env` / `.env.local`, unset Clerk/Relay/OTLP/PostHog/updater/signing environment variables, and set `T3CODE_TELEMETRY_ENABLED=false`, `DO_NOT_TRACK=1`, `T3CODE_TELEMETRY_CHANNEL=dev`, and `T3CODE_WEB_SOURCEMAP=false`.
2. **Runtime launcher defaults**: `/usr/bin/t3-code-no-telemetry` exports `T3CODE_TELEMETRY_ENABLED=false` and `DO_NOT_TRACK=1` on every launch.
3. **Post-build artifact verification (`check()`)**: Every build runs a streaming binary and text scan over the compiled output (including `resources/app.asar`) and aborts packaging if any forbidden string or pattern survives:
   - `ingest.t3.codes`
   - `phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m`
   - `X-Axiom-Dataset`
   - `T3CODE_POSTHOG_KEY` / `T3CODE_POSTHOG_HOST` / `T3CODE_TELEMETRY_KEY` / `T3CODE_TELEMETRY_HOST`
   - `t3code:analytics-device-id`
   - `pk_live_...` / `pk_test_...` (Clerk publishable keys)
   - `app-update.yml`
