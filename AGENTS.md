# AGENTS.md

Stdlib-only Python 3.10+ toolchain (`t3clean.py`, `t3clean/`) that strips telemetry/tracking from upstream [T3 Code](https://github.com/pingdotgg/t3code), exports a minimal patch-based AUR recipe (`aur/`), and builds/deploys Linux desktop and server artifacts.

## Context Pointers

Load these files only when your task enters their branch:

- **Telemetry rules, patch targets, or upstream breakage**: read `PATCHES.md` for the full file-by-file audit, caller map, and intentionally kept read-only endpoints (rules 7–8).
- **CI release checks, PR/issue automation, or `master` sync**: read `.github/workflows/update-check.yml` and `.github/workflows/sync-master.yml`.
- **CLI flags or subcommand usage**: run `./t3clean.py --help` (or `<subcommand> --help`) rather than assuming flags.
- **User-facing documentation edits**: keep `README.md` (primary, Simplified Chinese) and `README.en.md` (English mirror) aligned in structure, commands, and plain-language tone; place deep technical patch breakdowns in `PATCHES.md`.

## Repository Invariants

- **Zero third-party Python dependencies**: keep `t3clean/` and `t3clean.py` strictly Python 3.10+ stdlib (`unittest`, `urllib`, `tarfile`, `sqlite3`, `difflib`, etc.).
- **Two-branch AUR workflow**:
  - `dev` is the default branch where code, tests, workflows, and `aur/` live.
  - `master` is an automated flat orphan branch synced by `.github/workflows/sync-master.yml` (strictly 6 root files: `PKGBUILD`, `.SRCINFO`, `0001-strip-telemetry.patch`, `0002-linux-build-compat.patch`, `.gitignore`, `LICENSE`).
  - Regenerate `aur/` via `./t3clean.py export-aur --out aur ...` whenever `t3clean/patcher.py`, `t3clean/aur.py`, or the target upstream ref changes. Keep `aur/.SRCINFO` free of `SKIP` checksums (`sync-master.yml` rejects `SKIP`).
- **Single-target latest-stable patcher**: `t3clean/patcher.py` targets only the current latest stable release (`t3clean/__init__.py#DEFAULT_REF`) and fails fast via `PatchAnchorNotFoundError` on structural drift. Drop obsolete version branches when adapting to a new release; historical versions remain reproducible through frozen GitHub Releases (`v<pkgver>-<pkgrel>`).
- **Verifier parity**: keep forbidden telemetry patterns aligned between `t3clean/verifier.py` and the inline `check()` scanner rendered by `t3clean/aur.py`. Skip `.repos/` (upstream vendored reference clones) and `.map` files during source scans.
- **Coexistence & safe server backups**:
  - Keep `t3-code-no-telemetry` free of `conflicts`/`provides` entries for `t3code-bin` or `t3code-nightly-bin` so packages install side by side.
  - Store server rollback backups under `~/.t3/runtime/no-telemetry-backups/` (outside `versions/`, so upstream `pruneRuntimesOnStart` preserves them).

## Build & Packaging Gotchas

- **Build on the same filesystem as `T3CLEAN_CACHE_DIR`**: run `makepkg` under `$HOME/` rather than `/tmp` (`tmpfs` in RAM blocks pnpm hardlinks from `$HOME/.cache`, forcing full re-downloads and consuming RAM).
- **Tailwind CSS v4 tarball OOM**: keep `@source not "../node_modules";` in `apps/web/src/index.css` (`0002-linux-build-compat.patch`) so tarball builds (no `.git`) inside ignored directories avoid scanning `node_modules`.
- **Host glibc alignment**: keep `scripts/linux-glibc.ts` configurable via `T3CODE_GLIBC_BUILD_CEILING` (`0002-linux-build-compat.patch`), populated from host `os.confstr("CS_GNU_LIBC_VERSION")` in `t3clean/builder.py` and `aur/PKGBUILD`.
- **Pinned `vite-plus` (`vp`)**: resolve the catalog version from upstream `pnpm-workspace.yaml` (embedded as `_vp_version` in `aur/PKGBUILD`, fallback `vite-plus@^1.4.0`), and require same-major compatibility so future `2.x` upgrades cannot break `1.x` tags.
- **Git hook ceiling**: preserve `GIT_CEILING_DIRECTORIES` pointing at `source_dir.parent` during builds so `vp install` prepare hooks inside extracted tarballs cannot mutate an enclosing repo's `.git/config`.
- **Clean `namcap` output**: match `aarch64` directly in `_t3clean_arch()` rather than naming `x86_64` in `PKGBUILD` (avoids `namcap` `$CARCH` warnings).

## Verification Gate

Run every check applicable to your changes before reporting done:

1. **Always (Python changes, tests, or ref bumps)**:
   ```bash
   python3 -W error::SyntaxWarning -m unittest discover -s tests -v
   ```
   Every test must pass with zero warnings.
2. **When `aur/`, `t3clean/aur.py`, or `t3clean/patcher.py` changes**:
   ```bash
   bash -n aur/PKGBUILD
   (cd aur && diff -u .SRCINFO <(makepkg --printsrcinfo))
   namcap aur/PKGBUILD
   ```
   `diff` and `namcap` must both produce zero errors and zero warnings; verify `aur/.SRCINFO` has no `SKIP` checksums.
3. **When `.github/workflows/*.yml` changes**: validate embedded bash blocks (`bash -n`) and Python heredocs (`compile`).
4. **When `README.md`, `README.en.md`, or `PATCHES.md` changes**: confirm all relative Markdown links resolve to existing repository files.
