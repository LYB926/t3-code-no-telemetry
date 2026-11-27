"""Minimal, patch-based flat AUR package exporter for t3-code-no-telemetry.

Generates a clean, auditable flat AUR directory ready to sync to an orphan
``master`` branch and push directly to AUR:
- PKGBUILD (self-contained, standard ``patch -Np1``, inline launcher/.desktop/check)
- .SRCINFO (exact match for ``makepkg --printsrcinfo``)
- 0001-strip-telemetry.patch (unified diff of the 6 de-telemetry/cloud/updater rules)
- 0002-linux-build-compat.patch (Tailwind v4 scan guard & Linux glibc ceiling)
- .gitignore (ignores local makepkg build artifacts)
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request

from . import DEFAULT_REF, DEFAULT_REPO_URL
from .patcher import PatchOptions, generate_patch_series
from .source import (
    read_required_vp_version,
    resolve_latest_github_release,
    resolve_latest_stable_tag,
)
from .verifier import VerifyOptions, verify_artifact

PKGNAME = "t3-code-no-telemetry"
PKGDESC = (
    "T3 Code desktop application built from source with telemetry, "
    "device tracking, and cloud auth stripped"
)
UPSTREAM_URL = "https://github.com/pingdotgg/t3code"
MAINTAINER = "Ke Zhang <zhangke23 at m dot fudan dot edu dot cn>"

ARCH_DEPENDS: tuple[str, ...] = (
    "alsa-lib",
    "at-spi2-core",
    "cairo",
    "dbus",
    "expat",
    "gdk-pixbuf2",
    "glib2",
    "gtk3",
    "hicolor-icon-theme",
    "libcups",
    "libdbusmenu-glib",
    "libdrm",
    "libsecret",
    "libx11",
    "libxcb",
    "libxcomposite",
    "libxdamage",
    "libxext",
    "libxfixes",
    "libxkbcommon",
    "libxrandr",
    "libxss",
    "libxtst",
    "mesa",
    "nspr",
    "nss",
    "pango",
    "systemd-libs",
    "xdg-utils",
)

ARCH_MAKEDEPENDS: tuple[str, ...] = (
    "git",
    "python",
    "nodejs>=26.12.1",
    "npm",
    "gcc",
    "make",
    "rust",
    "pkgconf",
    "imagemagick",
)

ARCH_OPTDEPENDS: tuple[str, ...] = (
    "openai-codex: use the system-installed Codex CLI",
)

ARCH_OPTIONS: tuple[str, ...] = ("!strip", "!debug")

AUR_GITIGNORE_CONTENT = """\
*.pkg.tar.*
*.tar.gz
*.tar.xz
*.tar.zst
pkg/
src/
"""

_OBSOLETE_AUR_FILES: tuple[str, ...] = (
    "t3clean-standalone.py",
    "t3-code-no-telemetry.sh",
    "t3-code-no-telemetry.desktop",
)

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_AUDIT_SKIP_DIRS = frozenset({".git", ".repos", "node_modules", "dist", "target", "__pycache__"})
_TEST_FILE_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")

_AUR_VP_HELPERS = r'''_t3clean_vp_ok() {
  local _vp_bin="$1"
  local _req_ver="${2:-}"
  [[ -x "${_vp_bin}" ]] || return 1
  local _ver
  _ver="$(VP_NODE_MANAGER=no VP_ENV_FORBIDDEN=1 "${_vp_bin}" --version </dev/null 2>/dev/null | grep -oE 'vp v?[0-9]+\.[0-9]+\.[0-9]+' | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
  [[ -n "${_ver}" ]] || return 1
  python3 -c 'import re, sys
def p(s):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s)
    return tuple(int(x) for x in m.groups()) if m else None
found = p(sys.argv[1])
req = p(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
if found is None:
    sys.exit(1)
if req is not None:
    sys.exit(0 if (found[0] == req[0] and found >= req) else 1)
sys.exit(0 if (found[0] == 1 and found >= (1, 4, 0)) else 1)' "${_ver}" "${_req_ver}"
}

_t3clean_ensure_vp() {
  local _req_vp="${_vp_version:-}"
  local _vp_spec="vite-plus@^1.4.0"
  if [[ -n "${_req_vp}" ]]; then
    _vp_spec="vite-plus@${_req_vp}"
  fi

  local _sys_vp
  _sys_vp="$(command -v vp 2>/dev/null || true)"
  if [[ -n "${_sys_vp}" ]] && _t3clean_vp_ok "${_sys_vp}" "${_req_vp}"; then
    return 0
  fi

  local _cache_dir _vp_prefix _legacy_prefix
  _cache_dir="$(_t3clean_cache_dir)"
  _legacy_prefix="${_cache_dir}/toolchains/vp-isolated"
  if [[ -n "${_req_vp}" ]]; then
    _vp_prefix="${_cache_dir}/toolchains/vp-isolated-v${_req_vp}"
    if _t3clean_vp_ok "${_vp_prefix}/bin/vp" "${_req_vp}"; then
      export PATH="${_vp_prefix}/bin:${PATH}"
      return 0
    fi
    if _t3clean_vp_ok "${_legacy_prefix}/bin/vp" "${_req_vp}"; then
      export PATH="${_legacy_prefix}/bin:${PATH}"
      return 0
    fi
  else
    _vp_prefix="${_legacy_prefix}"
    if _t3clean_vp_ok "${_vp_prefix}/bin/vp" ""; then
      export PATH="${_vp_prefix}/bin:${PATH}"
      return 0
    fi
  fi

  mkdir -p "${_vp_prefix}"
  VP_NODE_MANAGER=no npm install --global --prefix "${_vp_prefix}" "${_vp_spec}" </dev/null
  export PATH="${_vp_prefix}/bin:${PATH}"
}'''

_AUR_CHECK_SCANNER = r'''  python3 - "${_artifact_dir}" <<'PY'
import os, pathlib, re, sys

root = pathlib.Path(sys.argv[1])
literals = (
    ("posthog-ingest-host", b"ingest.t3.codes"),
    ("posthog-api-key", b"phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m"),
    ("axiom-dataset-header", b"X-Axiom-Dataset"),
    ("posthog-key-env", b"T3CODE_POSTHOG_KEY"),
    ("posthog-host-env", b"T3CODE_POSTHOG_HOST"),
    ("telemetry-key-env", b"T3CODE_TELEMETRY_KEY"),
    ("telemetry-host-env", b"T3CODE_TELEMETRY_HOST"),
    ("analytics-device-id", b"t3code:analytics-device-id"),
)
clerk_re = re.compile(rb"pk_(?:live|test)_[A-Za-z0-9_-]{12,}")
text_exts = {".js", ".mjs", ".cjs", ".json", ".yml", ".yaml", ".html", ".txt"}
findings = []
scanned = 0

def scan_bytes(path, rel):
    tail = b""
    hit_rules = set()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            win = tail + chunk
            for rule, needle in literals:
                if rule not in hit_rules and needle in win:
                    hit_rules.add(rule)
                    findings.append((rel, rule, needle.decode("utf-8", "replace")))
            if "clerk-publishable-key" not in hit_rules:
                m = clerk_re.search(win)
                if m:
                    hit_rules.add("clerk-publishable-key")
                    findings.append((rel, "clerk-publishable-key", m.group(0).decode("utf-8", "replace")))
            tail = win[-4096:]

for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        p = pathlib.Path(dirpath) / name
        if p.is_symlink() or not p.is_file():
            continue
        lower = name.lower()
        rel = p.relative_to(root).as_posix()
        if lower == "app-update.yml":
            findings.append((rel, "app-update-yml", "forbidden auto-updater metadata present"))
            continue
        if lower.endswith(".map"):
            continue
        if p.suffix.lower() in text_exts or p.suffix.lower() == ".asar":
            scanned += 1
            scan_bytes(p, rel)

if findings:
    print(f"FAILED: {len(findings)} forbidden telemetry/cloud indicator(s) across {scanned} file(s):", file=sys.stderr)
    for rel, rule, detail in findings:
        print(f"  - [{rule}] {rel}: {detail}", file=sys.stderr)
    sys.exit(1)
print(f"OK: verified {scanned} file(s) under {root} with 0 telemetry findings.")
PY'''


@dataclass(frozen=True)
class AurExportResult:
    """Structured summary returned by :func:`export_aur_package`."""

    out_dir: Path
    ref: str
    resolved_ref: str
    pkgver: str
    pkgrel: int
    source_archive: str
    source_url: str
    source_sha256: str
    vp_version: str | None
    patch_files: tuple[str, ...]
    verified_files: int
    audit_files: tuple[str, ...]
    files: tuple[Path, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _download_to_file(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"{PKGNAME}/t3clean"},
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=300) as response, tmp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        os.replace(tmp_path, dest)
    except (urllib.error.URLError, OSError) as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def _sha256_url(url: str) -> str:
    """Stream ``url`` and return its lowercase SHA-256 hex digest without storing a file."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"{PKGNAME}/t3clean"},
    )
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Failed to download {url} to compute SHA-256: {exc}") from exc
    return digest.hexdigest().lower()


def _safe_extract_tar_to(archive_path: Path, dest_dir: Path) -> None:
    """Safely extract a tar archive into ``dest_dir``, blocking path traversal and unsafe links."""
    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Unsafe tar member path: {member.name!r}")
            if member.isdev():
                raise ValueError(f"Refusing to extract special device member: {member.name!r}")
            resolved_target = (dest_root / member.name).resolve()
            try:
                resolved_target.relative_to(dest_root)
            except ValueError as exc:
                raise ValueError(f"Tar member escapes destination: {member.name!r}") from exc
            if member.issym():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute():
                    raise ValueError(
                        f"Refusing absolute symlink in tar: {member.name!r} -> {member.linkname!r}"
                    )
                resolved_link = (resolved_target.parent / member.linkname).resolve()
                try:
                    resolved_link.relative_to(dest_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Tar symlink escapes destination: {member.name!r} -> {member.linkname!r}"
                    ) from exc
            elif member.islnk():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute() or ".." in link_path.parts:
                    raise ValueError(
                        f"Refusing unsafe hardlink in tar: {member.name!r} -> {member.linkname!r}"
                    )
                resolved_link = (dest_root / member.linkname).resolve()
                try:
                    resolved_link.relative_to(dest_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Tar hardlink escapes destination: {member.name!r} -> {member.linkname!r}"
                    ) from exc

        if hasattr(tarfile, "tar_filter"):
            tar.extractall(dest_root, members=members, filter="tar")
        else:
            tar.extractall(dest_root, members=members)


def _locate_extracted_source_root(extract_dir: Path) -> Path:
    if (extract_dir / "package.json").is_file():
        return extract_dir
    subdirs = sorted(p for p in extract_dir.iterdir() if p.is_dir())
    for subdir in subdirs:
        if (subdir / "package.json").is_file():
            return subdir
    if len(subdirs) == 1:
        return subdirs[0]
    raise FileNotFoundError(f"Could not locate extracted T3 Code source root under {extract_dir}")


def _github_archive_source(ref: str, repo_url: str = DEFAULT_REPO_URL) -> tuple[str, str]:
    """Return ``(archive_filename, archive_url)`` for a GitHub tag, commit, or branch."""
    cleaned = ref.strip()
    repo_base = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    safe_ref = cleaned.replace("/", "-")
    archive_name = f"t3code-{safe_ref}.tar.gz"
    if re.match(r"^v[0-9]", cleaned):
        return archive_name, f"{repo_base}/archive/refs/tags/{cleaned}.tar.gz"
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", cleaned):
        return archive_name, f"{repo_base}/archive/{cleaned}.tar.gz"
    return archive_name, f"{repo_base}/archive/refs/heads/{cleaned}.tar.gz"


def resolve_upstream_ref(ref: str, *, repo_url: str = DEFAULT_REPO_URL) -> str:
    """Resolve symbolic refs (``latest-release``, ``latest-stable``) to a concrete Git ref."""
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("upstream_ref must be a non-empty Git ref")
    if cleaned == "latest-release":
        return resolve_latest_github_release(repo_url)["tag"]
    if cleaned == "latest-stable":
        return resolve_latest_stable_tag(repo_url)
    return cleaned


def resolve_source_sha256(
    upstream_ref: str,
    *,
    source_sha256: str | None = None,
    source_tarball: Path | None = None,
    repo_url: str = DEFAULT_REPO_URL,
) -> str:
    """Resolve the upstream tarball SHA-256 checksum for ``PKGBUILD`` and ``.SRCINFO``.

    Accepts:
    - ``source_tarball``: computes SHA-256 from an already-downloaded local archive file.
    - ``source_sha256="auto"``: streams the GitHub archive URL for ``upstream_ref``.
    - ``source_sha256="<64-hex>"``: validates and normalizes an explicit SHA-256 hex digest.
    - ``source_sha256="SKIP"`` or ``None``: returns ``"SKIP"``.
    """
    if source_sha256 is not None and source_tarball is not None:
        raise ValueError("Cannot specify both source_sha256 and source_tarball; choose one.")

    if source_tarball is not None:
        tarball_path = Path(source_tarball).expanduser().resolve()
        if not tarball_path.is_file():
            raise FileNotFoundError(f"Source tarball not found: {tarball_path}")
        return _sha256_file(tarball_path)

    if source_sha256 is None:
        return "SKIP"

    cleaned = source_sha256.strip()
    if not cleaned:
        raise ValueError(
            "source_sha256 must not be empty; pass a 64-character hex SHA-256, 'auto', or 'SKIP'."
        )
    if cleaned.upper() == "SKIP":
        return "SKIP"
    if cleaned.lower() == "auto":
        _, archive_url = _github_archive_source(upstream_ref, repo_url=repo_url)
        return _sha256_url(archive_url)
    if _SHA256_HEX_RE.fullmatch(cleaned):
        return cleaned.lower()

    raise ValueError(
        f"Invalid source_sha256 {source_sha256!r}: expected 64 hex characters, 'auto', or 'SKIP'."
    )


def _pkgver_for_ref(ref: str) -> str:
    cleaned = ref.strip()
    if re.match(r"^v?\d+\.\d+\.\d+", cleaned):
        ver = cleaned.lstrip("v")
        ver = re.sub(r"[^A-Za-z0-9._]+", ".", ver).strip(".")
        return ver
    fallback_base = DEFAULT_REF.lstrip("v")
    safe_ref = re.sub(r"[^A-Za-z0-9._]+", ".", cleaned).strip(".")
    return f"{fallback_base}.{safe_ref}" if safe_ref else fallback_base


def _format_bash_array(name: str, items: tuple[str, ...] | list[str]) -> str:
    rendered = "\n".join(f"  '{item}'" for item in items)
    return f"{name}=(\n{rendered}\n)"


def _collect_audit_files(source_dir: Path) -> tuple[str, ...]:
    """List upstream non-test files under ``telemetry/`` or ``cloud/`` dirs for PR review."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _AUDIT_SKIP_DIRS)
        rel_dir = Path(dirpath).relative_to(source_dir)
        parts = set(rel_dir.parts)
        if not (parts & {"telemetry", "cloud"}):
            continue
        for filename in sorted(filenames):
            if filename.lower().endswith(_TEST_FILE_SUFFIXES):
                continue
            found.append((rel_dir / filename).as_posix())
    return tuple(sorted(found))


def _apply_patch_file(source_dir: Path, patch_path: Path, *, dry_run: bool = False) -> None:
    """Apply a unified patch file using GNU ``patch -Np1``, raising on any hunk failure."""
    cmd = ["patch", "--batch", "--forward", "-Np1"]
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend(["-i", str(patch_path)])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(source_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GNU 'patch' executable not found in PATH") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit status {proc.returncode}").strip()
        mode = "dry-run " if dry_run else ""
        raise RuntimeError(f"Generated patch {patch_path.name} failed {mode}apply: {detail}")


def _copy_clean_tree_for_verify(source_dir: Path, dest_dir: Path) -> None:
    shutil.copytree(
        source_dir,
        dest_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".repos", "node_modules", "dist", "target", "__pycache__"),
    )


def _render_flat_pkgbuild(
    *,
    upstream_ref: str,
    pkgver: str,
    pkgrel: int,
    vp_version: str | None,
    sha_source: str,
    patch_checksums: list[tuple[str, str]],
    repo_url: str = DEFAULT_REPO_URL,
) -> str:
    archive_name, archive_url = _github_archive_source(upstream_ref, repo_url=repo_url)
    ref_no_v = upstream_ref[1:] if re.match(r"^v[0-9]", upstream_ref) else upstream_ref
    ref_safe = upstream_ref.replace("/", "-")
    depends_block = _format_bash_array("depends", ARCH_DEPENDS)
    makedepends_block = _format_bash_array("makedepends", ARCH_MAKEDEPENDS)
    optdepends_block = _format_bash_array("optdepends", ARCH_OPTDEPENDS)
    options_str = " ".join(f"'{opt}'" for opt in ARCH_OPTIONS)

    source_entries = [f'  "{archive_name}::{archive_url}"']
    checksum_entries = [f"  '{sha_source}'"]
    patch_cmds: list[str] = []
    for patch_name, patch_sha in patch_checksums:
        source_entries.append(f"  '{patch_name}'")
        checksum_entries.append(f"  '{patch_sha}'")
        patch_cmds.append(f'  patch -Np1 -i "${{srcdir}}/{patch_name}"')

    source_block = "\n".join(source_entries)
    checksum_block = "\n".join(checksum_entries)
    patch_apply_block = "\n".join(patch_cmds)
    pinned_vp = (vp_version or "").strip()

    return f"""# Maintainer: {MAINTAINER}
# Generated by t3clean.py export-aur (https://github.com/LYB926/t3-code-no-telemetry)

pkgname={PKGNAME}
pkgver={pkgver}
pkgrel={pkgrel}
pkgdesc='{PKGDESC}'
arch=('x86_64' 'aarch64')
url='{UPSTREAM_URL}'
license=('MIT')
options=({options_str})

# Pinned Vite+ CLI version from upstream pnpm-workspace.yaml (empty -> fallback ^1.4.0)
_vp_version='{pinned_vp}'
_jobs="${{T3CLEAN_JOBS:-}}"

{depends_block}
# Official Arch/CachyOS repo build dependencies only; Vite+ (vp) is reused from
# PATH when compatible or installed into the build cache via host npm pinned to
# _vp_version (avoids outdated AUR vite-plus-bin, 2.x breaks, and rustup conflicts).
{makedepends_block}
{optdepends_block}

# Coexists cleanly with t3code-bin (no conflicts/provides overlap).
source=(
{source_block}
)
sha256sums=(
{checksum_block}
)

_t3clean_source_dir() {{
  local _cand
  for _cand in \\
    "${{srcdir}}/t3code-{ref_no_v}" \\
    "${{srcdir}}/t3code-{ref_safe}" \\
    "${{srcdir}}/t3code"; do
    if [[ -d "${{_cand}}" ]]; then
      printf '%s' "${{_cand}}"
      return 0
    fi
  done
  _cand="$(find "${{srcdir}}" -maxdepth 1 -mindepth 1 -type d -name 't3code-*' -print -quit)"
  if [[ -n "${{_cand}}" && -f "${{_cand}}/package.json" ]]; then
    printf '%s' "${{_cand}}"
    return 0
  fi
  echo "ERROR: Extracted T3 Code source directory not found under ${{srcdir}}" >&2
  return 1
}}

_t3clean_arch() {{
  case "${{CARCH}}" in
    aarch64) printf 'arm64' ;;
    *)       printf 'x64' ;;
  esac
}}

_t3clean_semver() {{
  if [[ "${{pkgver}}" =~ ^([0-9]+\\.[0-9]+\\.[0-9]+)\\.(.+)$ ]]; then
    printf '%s-%s+notelemetry' "${{BASH_REMATCH[1]}}" "${{BASH_REMATCH[2]}}"
  else
    printf '%s+notelemetry' "${{pkgver}}"
  fi
}}

_t3clean_cache_dir() {{
  if [[ -n "${{T3CLEAN_CACHE_DIR:-}}" ]]; then
    printf '%s' "${{T3CLEAN_CACHE_DIR}}"
  else
    printf '%s' "${{srcdir}}/.cache"
  fi
}}

{_AUR_VP_HELPERS}

_t3clean_scrub_env() {{
  local _var
  while IFS='=' read -r _var _; do
    case "${{_var}}" in
      T3CODE_CLERK_*|VITE_CLERK_*|EXPO_PUBLIC_CLERK_*|T3CODE_RELAY_*|VITE_T3CODE_RELAY_*|VITE_RELAY_OTLP_*|T3CODE_MOBILE_OTLP_*|EXPO_PUBLIC_OTLP_*|T3CODE_POSTHOG_*)
        unset "${{_var}}"
        ;;
    esac
  done < <(env)
  unset T3CODE_TELEMETRY_KEY T3CODE_TELEMETRY_HOST T3CODE_DESKTOP_UPDATE_REPOSITORY GITHUB_REPOSITORY CSC_LINK CSC_KEY_PASSWORD

  export T3CODE_TELEMETRY_ENABLED=false
  export DO_NOT_TRACK=1
  export T3CODE_TELEMETRY_CHANNEL=dev
  export T3CODE_WEB_SOURCEMAP=false
  export VP_NODE_MANAGER=no
  if [[ -z "${{T3CODE_GLIBC_BUILD_CEILING:-}}" ]]; then
    local _host_glibc
    _host_glibc="$(python3 -c 'import os, re; m = re.search(r"glibc\\s+(\\d+(?:\\.\\d+)+)", os.confstr("CS_GNU_LIBC_VERSION") or "", re.I); print(m.group(1) if m else "")' 2>/dev/null || true)"
    if [[ -n "${{_host_glibc}}" ]]; then
      export T3CODE_GLIBC_BUILD_CEILING="${{_host_glibc}}"
    fi
  fi

  local _cache_dir
  _cache_dir="$(_t3clean_cache_dir)"
  mkdir -p "${{_cache_dir}}/electron" "${{_cache_dir}}/electron-builder" "${{_cache_dir}}/cargo" "${{_cache_dir}}/pnpm" "${{_cache_dir}}/pnpm-store" "${{_cache_dir}}/xdg"
  export ELECTRON_CACHE="${{_cache_dir}}/electron"
  export ELECTRON_BUILDER_CACHE="${{_cache_dir}}/electron-builder"
  export CARGO_HOME="${{_cache_dir}}/cargo"
  export PNPM_HOME="${{_cache_dir}}/pnpm"
  export npm_config_store_dir="${{_cache_dir}}/pnpm-store"
  export XDG_CACHE_HOME="${{_cache_dir}}/xdg"
}}

_t3clean_apply_memory_guards() {{
  local _mem_total_kb="" _mem_avail_kb="" _mem_total_mib=0 _mem_avail_mib=0
  if [[ -r /proc/meminfo ]]; then
    _mem_total_kb="$(awk '/^MemTotal:/ {{print $2; exit}}' /proc/meminfo)"
    _mem_avail_kb="$(awk '/^MemAvailable:/ {{print $2; exit}}' /proc/meminfo)"
    [[ -n "${{_mem_total_kb}}" ]] && _mem_total_mib=$(( _mem_total_kb / 1024 ))
    [[ -n "${{_mem_avail_kb}}" ]] && _mem_avail_mib=$(( _mem_avail_kb / 1024 ))
  fi

  local _cpus
  _cpus="$(nproc 2>/dev/null || echo 4)"
  local _target_jobs="" _target_child=""
  if [[ -n "${{_jobs}}" ]]; then
    if ! [[ "${{_jobs}}" =~ ^[1-9][0-9]*$ ]]; then
      echo "ERROR: T3CLEAN_JOBS must be a positive integer, got '${{_jobs}}'" >&2
      return 1
    fi
    _target_jobs="${{_jobs}}"
    _target_child=$(( _jobs < 4 ? _jobs : 4 ))
    export RAYON_NUM_THREADS="${{_target_jobs}}"
    export CARGO_BUILD_JOBS="${{_target_jobs}}"
    export npm_config_child_concurrency="${{_target_child}}"
  elif (( _mem_total_mib > 0 || _mem_avail_mib > 0 )); then
    if (( (_mem_total_mib > 0 && _mem_total_mib <= 10240) || (_mem_avail_mib > 0 && _mem_avail_mib < 4096) )); then
      _target_jobs=$(( _cpus < 2 ? _cpus : 2 ))
      _target_child=1
    elif (( (_mem_total_mib > 0 && _mem_total_mib <= 16896) || (_mem_avail_mib > 0 && _mem_avail_mib < 6144) )); then
      _target_jobs=$(( _cpus < 4 ? _cpus : 4 ))
      _target_child=$(( _target_jobs < 2 ? _target_jobs : 2 ))
    fi
    if [[ -n "${{_target_jobs}}" ]]; then
      export RAYON_NUM_THREADS="${{RAYON_NUM_THREADS:-${{_target_jobs}}}}"
      export CARGO_BUILD_JOBS="${{CARGO_BUILD_JOBS:-${{_target_jobs}}}}"
      export npm_config_child_concurrency="${{npm_config_child_concurrency:-${{_target_child}}}}"
    fi
  fi
}}

prepare() {{
  local _src_dir
  _src_dir="$(_t3clean_source_dir)"
  cd "${{_src_dir}}"
{patch_apply_block}
}}

build() {{
  local _src_dir
  _src_dir="$(_t3clean_source_dir)"
  cd "${{_src_dir}}"
  rm -f .env .env.local
  export GIT_CEILING_DIRECTORIES="${{GIT_CEILING_DIRECTORIES:+${{GIT_CEILING_DIRECTORIES}}:}}$(dirname "${{_src_dir}}")"
  _t3clean_scrub_env
  _t3clean_apply_memory_guards
  _t3clean_ensure_vp

  local _arch _build_ver
  _arch="$(_t3clean_arch)"
  _build_ver="$(_t3clean_semver)"

  vp install --frozen-lockfile
  node scripts/build-desktop-artifact.ts \\
    --platform linux \\
    --target dir \\
    --arch "${{_arch}}" \\
    --build-version "${{_build_ver}}" \\
    --output-dir "${{srcdir}}/out"
}}

check() {{
  local _artifact_dir
  _artifact_dir="$(find "${{srcdir}}/out" -maxdepth 1 -type d -name 'linux*-unpacked' -print -quit)"
  if [[ -z "${{_artifact_dir}}" || ! -d "${{_artifact_dir}}" ]]; then
    echo "ERROR: Built desktop artifact directory not found under ${{srcdir}}/out" >&2
    return 1
  fi
{_AUR_CHECK_SCANNER}
}}

package() {{
  local _payload_dir _src_dir
  _src_dir="$(_t3clean_source_dir)"
  _payload_dir="$(find "${{srcdir}}/out" -maxdepth 1 -type d -name 'linux*-unpacked' -print -quit)"
  if [[ -z "${{_payload_dir}}" || ! -d "${{_payload_dir}}" ]]; then
    echo "ERROR: Packaged payload directory missing under ${{srcdir}}/out" >&2
    return 1
  fi

  install -d "${{pkgdir}}/usr/lib/${{pkgname}}"
  cp -a "${{_payload_dir}}/." "${{pkgdir}}/usr/lib/${{pkgname}}/"
  chmod -R a+rX "${{pkgdir}}/usr/lib/${{pkgname}}"

  install -Dm755 /dev/stdin "${{pkgdir}}/usr/bin/${{pkgname}}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# Defense-in-depth telemetry suppression
export T3CODE_TELEMETRY_ENABLED=false
export DO_NOT_TRACK=1

if [[ -z "${{CODEX_CLI_PATH-}}" ]] && command -v codex >/dev/null 2>&1; then
  export CODEX_CLI_PATH="$(command -v codex)"
fi

extra_flags=()
if [[ -n "${{WAYLAND_DISPLAY-}}" || "${{XDG_SESSION_TYPE-}}" == "wayland" ]]; then
  extra_flags+=(--enable-features=UseOzonePlatform --ozone-platform=wayland --ozone-platform-hint=wayland)
else
  extra_flags+=(--ozone-platform-hint=auto)
fi

exec /usr/lib/t3-code-no-telemetry/t3code "${{extra_flags[@]}}" "$@"
EOF

  install -Dm644 /dev/stdin "${{pkgdir}}/usr/share/applications/${{pkgname}}.desktop" <<'EOF'
[Desktop Entry]
Name=T3 Code (No Telemetry)
GenericName=Coding Agent IDE
Comment=Minimal desktop GUI for coding agents (de-telemetrized source build)
Exec=t3-code-no-telemetry %U
Terminal=false
Type=Application
Icon=t3-code-no-telemetry
StartupWMClass=com.t3tools.T3Code
StartupNotify=true
Categories=Development;IDE;
MimeType=x-scheme-handler/t3code;
EOF

  local _icon_src="${{_src_dir}}/assets/prod/black-universal-1024.png"
  if [[ -f "${{_icon_src}}" ]]; then
    local _resize_tool=""
    if command -v magick >/dev/null 2>&1; then
      _resize_tool="magick"
    elif command -v convert >/dev/null 2>&1; then
      _resize_tool="convert"
    fi
    local _size
    for _size in 16 22 24 32 48 64 128 256 512; do
      install -d "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps"
      if [[ -n "${{_resize_tool}}" ]]; then
        "${{_resize_tool}}" "${{_icon_src}}" -resize "${{_size}}x${{_size}}" \\
          "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps/${{pkgname}}.png"
      elif [[ "${{_size}}" == "512" ]]; then
        install -Dm644 "${{_icon_src}}" \\
          "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps/${{pkgname}}.png"
      fi
    done
    if [[ -f "${{pkgdir}}/usr/share/icons/hicolor/512x512/apps/${{pkgname}}.png" ]]; then
      install -Dm644 "${{pkgdir}}/usr/share/icons/hicolor/512x512/apps/${{pkgname}}.png" \\
        "${{pkgdir}}/usr/share/pixmaps/${{pkgname}}.png"
    else
      install -Dm644 "${{_icon_src}}" "${{pkgdir}}/usr/share/pixmaps/${{pkgname}}.png"
    fi
  fi

  install -Dm644 "${{_src_dir}}/LICENSE" "${{pkgdir}}/usr/share/licenses/${{pkgname}}/LICENSE"
}}
"""


def _render_srcinfo(
    *,
    upstream_ref: str,
    pkgver: str,
    pkgrel: int,
    sha_source: str,
    patch_checksums: list[tuple[str, str]],
    repo_url: str = DEFAULT_REPO_URL,
) -> str:
    archive_name, archive_url = _github_archive_source(upstream_ref, repo_url=repo_url)
    lines: list[str] = [
        f"pkgbase = {PKGNAME}",
        f"\tpkgdesc = {PKGDESC}",
        f"\tpkgver = {pkgver}",
        f"\tpkgrel = {pkgrel}",
        f"\turl = {UPSTREAM_URL}",
        "\tarch = x86_64",
        "\tarch = aarch64",
        "\tlicense = MIT",
    ]
    for dep in ARCH_MAKEDEPENDS:
        lines.append(f"\tmakedepends = {dep}")
    for dep in ARCH_DEPENDS:
        lines.append(f"\tdepends = {dep}")
    for opt in ARCH_OPTDEPENDS:
        lines.append(f"\toptdepends = {opt}")
    for opt in ARCH_OPTIONS:
        lines.append(f"\toptions = {opt}")
    lines.append(f"\tsource = {archive_name}::{archive_url}")
    for patch_name, _ in patch_checksums:
        lines.append(f"\tsource = {patch_name}")
    lines.append(f"\tsha256sums = {sha_source}")
    for _, patch_sha in patch_checksums:
        lines.append(f"\tsha256sums = {patch_sha}")
    lines.extend(["", f"pkgname = {PKGNAME}", ""])
    return "\n".join(lines)


def export_aur_package(
    repo_root: Path,
    out_dir: Path,
    *,
    upstream_ref: str = DEFAULT_REF,
    pkgrel: int = 1,
    source_dir: Path | None = None,
    source_tarball: Path | None = None,
    source_sha256: str | None = None,
    skip_verify: bool = False,
    repo_url: str = DEFAULT_REPO_URL,
) -> AurExportResult:
    """Generate a minimal, patch-based flat AUR package directory.

    Source input modes (mutually exclusive between ``source_dir`` and ``source_tarball``):
    - ``source_tarball``: hashes and extracts the local tarball (zero network).
    - ``source_dir``: generates patches from an existing clean checkout without mutating it.
    - Neither provided: downloads the GitHub archive once, hashes and extracts it,
      generates + verifies the unified ``.patch`` series, and writes real ``sha256sums``.
    """
    _ = repo_root  # Retained in signature for caller clarity/compatibility.
    if pkgrel < 1:
        raise ValueError(f"pkgrel must be >= 1, got {pkgrel}")
    if source_dir is not None and source_tarball is not None:
        raise ValueError("Cannot specify both source_dir and source_tarball; choose one.")
    if source_tarball is not None and source_sha256 is not None:
        raise ValueError("Cannot specify both source_sha256 and source_tarball; choose one.")

    resolved_ref = resolve_upstream_ref(upstream_ref, repo_url=repo_url)
    archive_name, archive_url = _github_archive_source(resolved_ref, repo_url=repo_url)
    pkgver = _pkgver_for_ref(resolved_ref)
    out_dir = Path(out_dir).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="t3clean-aur-work-") as tmp_root:
        tmp_path = Path(tmp_root)
        owns_work_tree = False

        if source_dir is not None:
            work_src = Path(source_dir).expanduser().resolve()
            if not work_src.is_dir():
                raise FileNotFoundError(f"Source directory does not exist: {work_src}")
            sha_source = resolve_source_sha256(
                resolved_ref,
                source_sha256=source_sha256,
                repo_url=repo_url,
            )
        elif source_tarball is not None:
            tarball_path = Path(source_tarball).expanduser().resolve()
            if not tarball_path.is_file():
                raise FileNotFoundError(f"Source tarball not found: {tarball_path}")
            sha_source = _sha256_file(tarball_path)
            extract_dir = tmp_path / "extracted"
            _safe_extract_tar_to(tarball_path, extract_dir)
            work_src = _locate_extracted_source_root(extract_dir)
            owns_work_tree = True
        else:
            downloaded_tarball = tmp_path / archive_name
            _download_to_file(archive_url, downloaded_tarball)
            actual_sha = _sha256_file(downloaded_tarball)
            if source_sha256 is not None:
                expected_sha = resolve_source_sha256(
                    resolved_ref,
                    source_sha256=source_sha256,
                    repo_url=repo_url,
                )
                if expected_sha not in ("SKIP", actual_sha):
                    raise ValueError(
                        f"Downloaded archive SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
                    )
                sha_source = actual_sha if expected_sha != "SKIP" else "SKIP"
            else:
                sha_source = actual_sha
            extract_dir = tmp_path / "extracted"
            _safe_extract_tar_to(downloaded_tarball, extract_dir)
            work_src = _locate_extracted_source_root(extract_dir)
            owns_work_tree = True

        vp_version = read_required_vp_version(work_src)
        audit_files = _collect_audit_files(work_src)
        patch_series = generate_patch_series(work_src, PatchOptions())
        non_empty_patches = [(name, body) for name, body in patch_series.items() if body]
        if not non_empty_patches:
            raise RuntimeError("No patches were generated from upstream source tree")

        # Stage patches in temp dir and validate real GNU patch application + telemetry verification.
        staged_patch_dir = tmp_path / "patches"
        staged_patch_dir.mkdir(parents=True, exist_ok=True)
        staged_patch_paths: list[Path] = []
        for patch_name, patch_body in non_empty_patches:
            patch_path = staged_patch_dir / patch_name
            patch_path.write_text(patch_body, encoding="utf-8")
            staged_patch_paths.append(patch_path)

        verified_files = 0
        if owns_work_tree:
            for patch_path in staged_patch_paths:
                _apply_patch_file(work_src, patch_path, dry_run=False)
            if not skip_verify:
                report = verify_artifact(work_src, VerifyOptions())
                verified_files = report.scanned_files
                if not report.ok:
                    details = "; ".join(f"{f.rule}@{f.path}: {f.detail}" for f in report.findings[:10])
                    raise RuntimeError(
                        f"Patched upstream source failed telemetry verification "
                        f"({len(report.findings)} finding(s)): {details}"
                    )
        else:
            if skip_verify:
                for patch_path in staged_patch_paths:
                    _apply_patch_file(work_src, patch_path, dry_run=True)
            else:
                verify_copy = tmp_path / "verify-copy"
                _copy_clean_tree_for_verify(work_src, verify_copy)
                for patch_path in staged_patch_paths:
                    _apply_patch_file(verify_copy, patch_path, dry_run=False)
                report = verify_artifact(verify_copy, VerifyOptions())
                verified_files = report.scanned_files
                if not report.ok:
                    details = "; ".join(f"{f.rule}@{f.path}: {f.detail}" for f in report.findings[:10])
                    raise RuntimeError(
                        f"Patched upstream source failed telemetry verification "
                        f"({len(report.findings)} finding(s)): {details}"
                    )

        # Write final flat AUR files only after all validations pass.
        out_dir.mkdir(parents=True, exist_ok=True)
        for obsolete in _OBSOLETE_AUR_FILES:
            obsolete_path = out_dir / obsolete
            if obsolete_path.exists() or obsolete_path.is_symlink():
                obsolete_path.unlink()

        written_patches: list[Path] = []
        patch_checksums: list[tuple[str, str]] = []
        for patch_name, patch_body in non_empty_patches:
            dest_patch = out_dir / patch_name
            dest_patch.write_text(patch_body, encoding="utf-8")
            written_patches.append(dest_patch)
            patch_checksums.append((patch_name, _sha256_file(dest_patch)))

        pkgbuild_path = out_dir / "PKGBUILD"
        pkgbuild_path.write_text(
            _render_flat_pkgbuild(
                upstream_ref=resolved_ref,
                pkgver=pkgver,
                pkgrel=pkgrel,
                vp_version=vp_version,
                sha_source=sha_source,
                patch_checksums=patch_checksums,
                repo_url=repo_url,
            ),
            encoding="utf-8",
        )

        srcinfo_path = out_dir / ".SRCINFO"
        srcinfo_path.write_text(
            _render_srcinfo(
                upstream_ref=resolved_ref,
                pkgver=pkgver,
                pkgrel=pkgrel,
                sha_source=sha_source,
                patch_checksums=patch_checksums,
                repo_url=repo_url,
            ),
            encoding="utf-8",
        )

        gitignore_path = out_dir / ".gitignore"
        gitignore_path.write_text(AUR_GITIGNORE_CONTENT, encoding="utf-8")

        created_files = tuple([pkgbuild_path, srcinfo_path, *written_patches, gitignore_path])
        return AurExportResult(
            out_dir=out_dir,
            ref=upstream_ref,
            resolved_ref=resolved_ref,
            pkgver=pkgver,
            pkgrel=pkgrel,
            source_archive=archive_name,
            source_url=archive_url,
            source_sha256=sha_source,
            vp_version=vp_version,
            patch_files=tuple(name for name, _ in patch_checksums),
            verified_files=verified_files,
            audit_files=audit_files,
            files=created_files,
        )
