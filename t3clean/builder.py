"""Multi-target build orchestration, environment scrubbing, and isolated toolchain bootstrap."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from . import DEFAULT_NODE_VERSION
from .patcher import PatchOptions, patch_source_tree
from .source import read_required_node_version, read_required_vp_version, read_source_version
from .verifier import VerifyOptions, verify_artifact

MIN_VP_VERSION: tuple[int, int, int] = (1, 4, 0)
DEFAULT_VP_FALLBACK_MAJOR: int = 1
DEFAULT_VP_FALLBACK_SPEC: str = "vite-plus@^1.4.0"
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

LOW_TOTAL_MEM_MIB: int = 16 * 1024 + 512
VERY_LOW_TOTAL_MEM_MIB: int = 10 * 1024
LOW_AVAILABLE_WARN_MIB: int = 6 * 1024
VERY_LOW_AVAILABLE_MIB: int = 4 * 1024

CONCURRENCY_ENV_KEYS: tuple[str, ...] = (
    "RAYON_NUM_THREADS",
    "CARGO_BUILD_JOBS",
    "npm_config_child_concurrency",
)

VALID_TARGETS: tuple[str, ...] = (
    "desktop-dir",
    "desktop-deb",
    "desktop-appimage",
    "server-sea",
    "server-bundle",
)
VALID_ARCHES: tuple[str, ...] = ("x64", "arm64")

SCRUB_ENV_PREFIXES: tuple[str, ...] = (
    "T3CODE_CLERK_",
    "VITE_CLERK_",
    "EXPO_PUBLIC_CLERK_",
    "T3CODE_RELAY_",
    "VITE_T3CODE_RELAY_",
    "VITE_RELAY_OTLP_",
    "T3CODE_MOBILE_OTLP_",
    "EXPO_PUBLIC_OTLP_",
    "T3CODE_POSTHOG_",
)

SCRUB_ENV_EXACT: frozenset[str] = frozenset(
    {
        "T3CODE_TELEMETRY_KEY",
        "T3CODE_TELEMETRY_HOST",
        "T3CODE_DESKTOP_UPDATE_REPOSITORY",
        "GITHUB_REPOSITORY",
        "CSC_LINK",
        "CSC_KEY_PASSWORD",
    }
)


@dataclass(frozen=True)
class HostMemoryInfo:
    """Snapshot of host memory and swap layout from ``/proc``."""

    total_mib: int | None = None
    available_mib: int | None = None
    has_disk_swap: bool = False
    has_zram_swap: bool = False


@dataclass(frozen=True)
class BuildOptions:
    """Configuration options for building a de-telemetrized T3 Code artifact."""

    source_dir: Path
    target: str = "desktop-dir"
    arch: str = "x64"
    version: str | None = None
    output_dir: Path | None = None
    cache_dir: Path | None = None
    jobs: int | None = None
    bootstrap_toolchain: bool = False
    use_system_electron: bool = False
    system_electron_bin: str = "electron"
    sourcemap: bool = False
    skip_install: bool = False
    skip_verify: bool = False
    keep_updater: bool = False
    keep_cloud: bool = False
    strict_offline: bool = False
    skip_patch: bool = True


def detect_host_glibc_version() -> str | None:
    """Return host glibc ``X.Y`` version via ``os.confstr('CS_GNU_LIBC_VERSION')`` or ``None``."""
    try:
        raw = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, OSError, ValueError):
        return None
    if not raw:
        return None
    match = re.search(r"glibc\s+(\d+(?:\.\d+)+)", raw, re.IGNORECASE)
    return match.group(1) if match else None


def read_host_memory_info(
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    swaps_path: Path = Path("/proc/swaps"),
) -> HostMemoryInfo:
    """Read Linux ``/proc/meminfo`` and ``/proc/swaps`` best-effort without raising."""
    total_mib: int | None = None
    available_mib: int | None = None
    try:
        meminfo_text = meminfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        meminfo_text = ""

    for line in meminfo_text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                total_mib = int(parts[1]) // 1024
        elif line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                available_mib = int(parts[1]) // 1024

    has_disk_swap = False
    has_zram_swap = False
    try:
        swaps_text = swaps_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        swaps_text = ""

    for line in swaps_text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        device = stripped.split()[0]
        if device.startswith("/dev/zram") or "/zram" in device:
            has_zram_swap = True
        else:
            has_disk_swap = True

    return HostMemoryInfo(
        total_mib=total_mib,
        available_mib=available_mib,
        has_disk_swap=has_disk_swap,
        has_zram_swap=has_zram_swap,
    )


def compute_adaptive_concurrency(
    mem_info: HostMemoryInfo,
    *,
    explicit_jobs: int | None = None,
    cpu_count: int | None = None,
) -> dict[str, str]:
    """Compute Rust/Rolldown/npm concurrency limits for explicit ``jobs`` or low-RAM hosts.

    Returns an empty dict on high-memory hosts (> 16 GiB RAM with >= 6 GiB available)
    so 32/64 GiB workstations keep full build speed.
    """
    if explicit_jobs is not None:
        if explicit_jobs < 1:
            raise ValueError(f"jobs must be >= 1, got {explicit_jobs}")
        return {
            "RAYON_NUM_THREADS": str(explicit_jobs),
            "CARGO_BUILD_JOBS": str(explicit_jobs),
            "npm_config_child_concurrency": str(max(1, min(explicit_jobs, 4))),
        }

    total = mem_info.total_mib
    avail = mem_info.available_mib
    if total is None and avail is None:
        return {}

    cpus = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 4))

    if (total is not None and total <= VERY_LOW_TOTAL_MEM_MIB) or (
        avail is not None and avail < VERY_LOW_AVAILABLE_MIB
    ):
        jobs = max(1, min(2, cpus))
        child_concurrency = 1
    elif (total is not None and total <= LOW_TOTAL_MEM_MIB) or (
        avail is not None and avail < LOW_AVAILABLE_WARN_MIB
    ):
        jobs = max(1, min(4, cpus))
        child_concurrency = max(1, min(2, jobs))
    else:
        return {}

    return {
        "RAYON_NUM_THREADS": str(jobs),
        "CARGO_BUILD_JOBS": str(jobs),
        "npm_config_child_concurrency": str(child_concurrency),
    }


def _resolve_explicit_jobs(options_jobs: int | None, env: Mapping[str, str]) -> int | None:
    """Resolve explicit concurrency override from ``BuildOptions.jobs`` or ``T3CLEAN_JOBS``."""
    if options_jobs is not None:
        if options_jobs < 1:
            raise ValueError(f"jobs must be >= 1, got {options_jobs}")
        return options_jobs
    raw = env.get("T3CLEAN_JOBS", "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"T3CLEAN_JOBS must be a positive integer, got {raw!r}") from exc
    if parsed < 1:
        raise ValueError(f"T3CLEAN_JOBS must be >= 1, got {parsed}")
    return parsed


def apply_concurrency_env(
    env: dict[str, str],
    mem_info: HostMemoryInfo,
    *,
    explicit_jobs: int | None = None,
    cpu_count: int | None = None,
) -> dict[str, str]:
    """Apply explicit or adaptive concurrency limits into ``env`` and return effective settings.

    When ``explicit_jobs`` is ``None`` (auto-adaptive mode), existing user-set values for
    ``RAYON_NUM_THREADS``, ``CARGO_BUILD_JOBS``, or ``npm_config_child_concurrency`` are
    preserved via ``setdefault``.
    """
    desired = compute_adaptive_concurrency(
        mem_info,
        explicit_jobs=explicit_jobs,
        cpu_count=cpu_count,
    )
    if not desired:
        return {}

    applied: dict[str, str] = {}
    for key, value in desired.items():
        if explicit_jobs is not None:
            env[key] = value
            applied[key] = value
        else:
            effective = env.setdefault(key, value)
            applied[key] = effective
    return applied


def format_low_memory_warning(
    mem_info: HostMemoryInfo,
    *,
    applied_limits: Mapping[str, str] | None = None,
) -> str | None:
    """Return a non-blocking low-memory advisory string when ``MemAvailable < 6 GiB``."""
    avail = mem_info.available_mib
    if avail is None or avail >= LOW_AVAILABLE_WARN_MIB:
        return None

    avail_gib = avail / 1024.0
    if mem_info.total_mib is not None:
        mem_desc = f"{avail_gib:.1f} GiB available / {mem_info.total_mib / 1024.0:.1f} GiB total"
    else:
        mem_desc = f"{avail_gib:.1f} GiB available"

    swap_note = ""
    if mem_info.has_zram_swap and not mem_info.has_disk_swap:
        swap_note = " (zram-only swap detected, no disk swap — high risk of desktop thrashing)"
    elif not mem_info.has_zram_swap and not mem_info.has_disk_swap:
        swap_note = " (no swap detected)"

    limits_note = ""
    if applied_limits:
        pairs = ", ".join(f"{k}={v}" for k, v in applied_limits.items())
        limits_note = f" Active concurrency limits: {pairs}."

    return (
        f"WARNING: Low available memory before build: {mem_desc}{swap_note}.{limits_note} "
        "Consider closing heavy browsers/Electron apps, or wrap the build with "
        "'systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=10G ...' "
        "(see README.md)."
    )


def sanitized_build_env(
    base_env: Mapping[str, str],
    *,
    cache_dir: Path | None,
    sourcemap: bool = False,
) -> dict[str, str]:
    """Return a clean build environment stripped of Clerk, Relay, OTLP, updater, and signing vars.

    When ``cache_dir`` is provided, redirects package/toolchain caches into ``cache_dir``.
    When ``cache_dir`` is ``None``, leaves cache variables untouched for AUR/makepkg compliance.
    """
    env: dict[str, str] = {}
    for key, value in base_env.items():
        if key in SCRUB_ENV_EXACT:
            continue
        if any(key.startswith(prefix) for prefix in SCRUB_ENV_PREFIXES):
            continue
        env[key] = value

    env["T3CODE_TELEMETRY_ENABLED"] = "false"
    env["DO_NOT_TRACK"] = "1"
    env["T3CODE_TELEMETRY_CHANNEL"] = "dev"
    env["T3CODE_WEB_SOURCEMAP"] = "true" if sourcemap else "false"
    env.setdefault("VP_NODE_MANAGER", "no")

    if cache_dir is not None:
        resolved_cache = Path(cache_dir).expanduser().resolve()
        env["ELECTRON_CACHE"] = str(resolved_cache / "electron")
        env["ELECTRON_BUILDER_CACHE"] = str(resolved_cache / "electron-builder")
        env["CARGO_HOME"] = str(resolved_cache / "cargo")
        env["PNPM_HOME"] = str(resolved_cache / "pnpm")
        env["npm_config_store_dir"] = str(resolved_cache / "pnpm-store")
        env["XDG_CACHE_HOME"] = str(resolved_cache / "xdg")

    return env


def _parse_shasums256(shasums_text: str, filename: str) -> str:
    """Extract the expected SHA-256 hex digest for ``filename`` from ``SHASUMS256.txt``."""
    for raw_line in shasums_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*")
        if name == filename:
            return digest.lower()
    raise RuntimeError(f"Checksum entry for {filename!r} not found in SHASUMS256.txt")


def _sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of ``path``."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def _parse_semver_tuple(raw: str | None) -> tuple[int, int, int] | None:
    """Extract the first ``(major, minor, patch)`` tuple from ``raw``."""
    if not raw:
        return None
    match = _SEMVER_RE.search(raw)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _detect_node_version(node_bin: str) -> str | None:
    """Return the normalized ``X.Y.Z`` version of ``node_bin`` or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            [node_bin, "-v"],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    version = result.stdout.strip().lstrip("v")
    return version or None


def _detect_vp_version(vp_bin: str) -> tuple[int, int, int] | None:
    """Return the ``(major, minor, patch)`` version of ``vp_bin`` without blocking on TTY prompts."""
    probe_env = os.environ.copy()
    probe_env["VP_NODE_MANAGER"] = "no"
    probe_env["VP_ENV_FORBIDDEN"] = "1"
    try:
        result = subprocess.run(
            [vp_bin, "--version"],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=probe_env,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout or result.stderr or ""
    vp_line = re.search(r"\bvp\s+v?(\d+\.\d+\.\d+)\b", output)
    if vp_line:
        return _parse_semver_tuple(vp_line.group(1))
    return _parse_semver_tuple(output)


def _node_version_satisfies(found: str | None, required: str) -> bool:
    """Return True if ``found`` matches ``required`` (same major and >= required minor/patch)."""
    if not found:
        return False
    if found == required:
        return True
    found_tuple = _parse_semver_tuple(found)
    req_tuple = _parse_semver_tuple(required)
    if found_tuple is None or req_tuple is None:
        return False
    return found_tuple[0] == req_tuple[0] and found_tuple >= req_tuple


def _vp_version_satisfies(
    found: tuple[int, int, int] | None,
    required: str | None = None,
) -> bool:
    """Return True if detected ``vp`` version matches ``required``'s major line and minimum.

    When ``required`` is provided (e.g. ``"1.8.0"`` from ``pnpm-workspace.yaml``), requires the
    same major version and ``found >= required``. When ``required`` is ``None``, requires
    ``1.x >= MIN_VP_VERSION`` (blocking breaking ``2.x`` upgrades).
    """
    if found is None:
        return False
    if required is not None:
        req_tuple = _parse_semver_tuple(required)
        if req_tuple is not None:
            return found[0] == req_tuple[0] and found >= req_tuple
    return found[0] == DEFAULT_VP_FALLBACK_MAJOR and found >= MIN_VP_VERSION


def _vp_npm_package_spec(vp_version: str | None = None) -> str:
    """Return the version-pinned npm package specifier for ``vite-plus``."""
    if vp_version:
        cleaned = vp_version.strip().lstrip("v")
        if cleaned:
            return f"vite-plus@{cleaned}"
    return DEFAULT_VP_FALLBACK_SPEC


def _install_vp_into_prefix(
    npm_exe: str,
    prefix_dir: Path,
    *,
    prepend_bin: Path | None = None,
    vp_version: str | None = None,
) -> Path:
    """Install pinned ``vite-plus`` globally into ``prefix_dir`` and return ``prefix_dir / 'bin'``."""
    prefix_dir.mkdir(parents=True, exist_ok=True)
    npm_env = os.environ.copy()
    if prepend_bin is not None:
        npm_env["PATH"] = f"{prepend_bin}{os.pathsep}{npm_env.get('PATH', '')}"
    npm_env["npm_config_prefix"] = str(prefix_dir)
    npm_env["VP_NODE_MANAGER"] = "no"
    package_spec = _vp_npm_package_spec(vp_version)
    subprocess.run(
        [npm_exe, "install", "--global", "--prefix", str(prefix_dir), package_spec],
        check=True,
        env=npm_env,
        stdin=subprocess.DEVNULL,
    )
    return prefix_dir / "bin"


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


def _download_bytes(url: str) -> bytes:
    """Download URL content using stdlib urllib."""
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def _download_to_file(url: str, dest: Path) -> None:
    """Stream a URL to ``dest`` atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as response, tmp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        os.replace(tmp_path, dest)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def ensure_isolated_toolchain(
    cache_root: Path,
    node_version: str = DEFAULT_NODE_VERSION,
    arch: str = "x64",
    vp_version: str | None = None,
) -> Path:
    """Ensure a matching ``node`` and version-pinned ``vp`` toolchain without modifying system Node.

    - If current ``PATH`` already provides a compatible ``node`` and a compatible ``vp``
      (matching ``vp_version``'s major line and minimum, or ``1.x >= 1.4.0`` when unpinned),
      returns the directory containing ``vp`` (reusing host tools directly).
    - If current ``PATH`` already provides a compatible ``node`` and ``npm``, but ``vp`` is
      missing or incompatible, installs pinned ``vite-plus@<vp_version>`` (or ``vite-plus@^1.4.0``
      fallback) into ``cache_root/toolchains/vp-isolated[-v<vp_version>]`` using host ``npm``
      (reusing an already-compatible ``vp-isolated`` cache when present) and returns its ``bin/``.
    - Otherwise downloads official Node.js from ``nodejs.org``, verifies its ``SHASUMS256.txt``
      digest, extracts into ``cache_root/toolchains/node-v<ver>-linux-<arch>``, installs pinned
      ``vite-plus`` into that isolated prefix, and returns its ``bin/`` directory.
    """
    normalized_version = node_version.strip().lstrip("v")
    normalized_vp = (
        vp_version.strip().lstrip("v") if vp_version is not None and vp_version.strip() else None
    )
    if arch not in VALID_ARCHES:
        raise ValueError(f"Unsupported architecture {arch!r}; expected one of {', '.join(VALID_ARCHES)}")

    current_node = shutil.which("node")
    current_npm = shutil.which("npm")
    current_vp = shutil.which("vp")
    host_node_ok = current_node is not None and _node_version_satisfies(
        _detect_node_version(current_node), normalized_version
    )
    host_vp_ver = _detect_vp_version(current_vp) if current_vp is not None else None
    host_vp_ok = _vp_version_satisfies(host_vp_ver, normalized_vp)

    if host_node_ok and host_vp_ok:
        assert current_vp is not None
        return Path(current_vp).resolve().parent

    resolved_cache = Path(cache_root).expanduser().resolve()

    if host_node_ok and current_npm is not None:
        legacy_vp_prefix = resolved_cache / "toolchains" / "vp-isolated"
        if normalized_vp is not None:
            vp_prefix = resolved_cache / "toolchains" / f"vp-isolated-v{normalized_vp}"
            vp_bin_dir = vp_prefix / "bin"
            cached_vp = vp_bin_dir / "vp"
            cached_vp_ver = _detect_vp_version(str(cached_vp)) if cached_vp.is_file() else None
            if _vp_version_satisfies(cached_vp_ver, normalized_vp):
                return vp_bin_dir
            legacy_vp = legacy_vp_prefix / "bin" / "vp"
            legacy_vp_ver = _detect_vp_version(str(legacy_vp)) if legacy_vp.is_file() else None
            if _vp_version_satisfies(legacy_vp_ver, normalized_vp):
                return legacy_vp_prefix / "bin"
            _install_vp_into_prefix(current_npm, vp_prefix, vp_version=normalized_vp)
            return vp_bin_dir

        vp_prefix = legacy_vp_prefix
        vp_bin_dir = vp_prefix / "bin"
        cached_vp = vp_bin_dir / "vp"
        cached_vp_ver = _detect_vp_version(str(cached_vp)) if cached_vp.is_file() else None
        if not _vp_version_satisfies(cached_vp_ver, None):
            _install_vp_into_prefix(current_npm, vp_prefix, vp_version=None)
        return vp_bin_dir

    stem = f"node-v{normalized_version}-linux-{arch}"
    toolchain_dir = resolved_cache / "toolchains" / stem
    bin_dir = toolchain_dir / "bin"
    node_exe = bin_dir / "node"
    npm_exe = bin_dir / "npm"
    vp_exe = bin_dir / "vp"

    if not (node_exe.is_file() and _detect_node_version(str(node_exe)) == normalized_version):
        tarball_name = f"{stem}.tar.xz"
        base_url = f"https://nodejs.org/dist/v{normalized_version}"
        shasums_text = _download_bytes(f"{base_url}/SHASUMS256.txt").decode("utf-8")
        expected_sha = _parse_shasums256(shasums_text, tarball_name)

        downloads_dir = resolved_cache / "downloads"
        tarball_path = downloads_dir / tarball_name
        if not tarball_path.is_file() or _sha256_file(tarball_path) != expected_sha:
            _download_to_file(f"{base_url}/{tarball_name}", tarball_path)
            actual_sha = _sha256_file(tarball_path)
            if actual_sha != expected_sha:
                tarball_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"SHA-256 verification failed for {tarball_name}: "
                    f"expected {expected_sha}, got {actual_sha}"
                )

        toolchains_root = resolved_cache / "toolchains"
        toolchains_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{stem}-", dir=toolchains_root) as tmp_dir:
            tmp_path = Path(tmp_dir)
            _safe_extract_tar_to(tarball_path, tmp_path)
            extracted_root = tmp_path / stem
            if not extracted_root.is_dir():
                raise RuntimeError(
                    f"Extracted Node archive did not contain expected directory {stem!r}"
                )
            if toolchain_dir.exists():
                shutil.rmtree(toolchain_dir)
            shutil.move(str(extracted_root), str(toolchain_dir))

    if not vp_exe.is_file():
        _install_vp_into_prefix(
            str(npm_exe),
            toolchain_dir,
            prepend_bin=bin_dir,
            vp_version=normalized_vp,
        )
    else:
        existing_vp_ver = _detect_vp_version(str(vp_exe))
        if not _vp_version_satisfies(existing_vp_ver, normalized_vp):
            _install_vp_into_prefix(
                str(npm_exe),
                toolchain_dir,
                prepend_bin=bin_dir,
                vp_version=normalized_vp,
            )

    return bin_dir


def _run_cmd(cmd: list[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    """Execute a build command, raising a descriptive RuntimeError on failure."""
    try:
        subprocess.run(cmd, cwd=str(cwd), env=dict(env), check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required build tool not found: {cmd[0]!r}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Build command failed with exit code {exc.returncode}: {' '.join(cmd)}"
        ) from exc


def _remove_repo_env_files(source_dir: Path) -> None:
    """Remove ``.env`` and ``.env.local`` so upstream public-config cannot inject secrets."""
    for name in (".env", ".env.local"):
        env_file = source_dir / name
        if env_file.exists() or env_file.is_symlink():
            env_file.unlink()


def _find_unpacked_desktop_dir(output_dir: Path, arch: str) -> Path:
    """Locate the unpacked Linux Electron directory created by electron-builder."""
    preferred_names = (
        ("linux-arm64-unpacked", "linux-unpacked")
        if arch == "arm64"
        else ("linux-unpacked", "linux-arm64-unpacked")
    )
    for name in preferred_names:
        candidate = output_dir / name
        if candidate.is_dir():
            return candidate
    for entry in sorted(output_dir.iterdir()):
        if entry.is_dir() and entry.name.startswith("linux") and entry.name.endswith("-unpacked"):
            return entry
    raise FileNotFoundError(f"Expected linux-unpacked directory in {output_dir}")


def _prepare_system_electron_dir(
    unpacked_dir: Path,
    output_dir: Path,
    system_electron_bin: str,
) -> Path:
    """Strip bundled Electron runtime and stage ``resources/`` for system Electron execution."""
    resources_src = unpacked_dir / "resources"
    if not resources_src.is_dir():
        raise FileNotFoundError(
            f"Unpacked desktop directory {unpacked_dir} is missing resources/"
        )

    target_dir = output_dir / "linux-system-electron"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(resources_src, target_dir / "resources", symlinks=True)

    (target_dir / ".use-system-electron").write_text(
        f"{system_electron_bin.strip() or 'electron'}\n",
        encoding="utf-8",
    )
    (target_dir / "SYSTEM_ELECTRON_WARNING.txt").write_text(
        (
            "WARNING: System Electron mode strips the bundled Electron binary and relies on\n"
            f"'{system_electron_bin}' from the host system.\n"
            "Ensure the system Electron major version matches upstream's Electron ABI, otherwise\n"
            "native addons (node-pty, @napi-rs/keyring, @ff-labs/fff-node) may fail to load.\n"
        ),
        encoding="utf-8",
    )
    return target_dir


def _find_single_artifact(output_dir: Path, suffix: str) -> Path:
    """Locate the newest artifact file in ``output_dir`` matching ``suffix``."""
    matches = [
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.name.endswith(suffix)
    ]
    if not matches:
        raise FileNotFoundError(f"Build completed but no *{suffix} artifact was found in {output_dir}")
    matches.sort(key=lambda item: (item.stat().st_mtime, item.name))
    return matches[-1]


def _check_server_sea_prerequisites(env: Mapping[str, str]) -> None:
    """Validate host prerequisites for ``server-sea`` (especially Linux dpkg-query + patchelf)."""
    path_env = env.get("PATH")
    required = ["cargo"]
    if sys.platform.startswith("linux"):
        required.extend(["patchelf", "dpkg-query"])
    missing = [tool for tool in required if shutil.which(tool, path=path_env) is None]
    if missing:
        raise RuntimeError(
            "Missing required prerequisites for target 'server-sea': "
            f"{', '.join(missing)}. "
            "On Linux, upstream scripts/build-cli-archive.ts requires 'cargo', 'patchelf', "
            "and 'dpkg-query' (Debian/Ubuntu) to bundle libatomic.so.1. "
            "Install the missing tools on Debian/Ubuntu or use '--target server-bundle' "
            "on non-Debian systems."
        )


def _locate_resource_monitor_binary(source_dir: Path) -> Path:
    """Find the compiled ``t3-resource-monitor`` binary produced by Cargo."""
    candidates = (
        source_dir / "native" / "resource-monitor" / "target" / "release" / "t3-resource-monitor",
        source_dir / "target" / "release" / "t3-resource-monitor",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Cargo build completed but t3-resource-monitor binary was not found at "
        + " or ".join(str(p) for p in candidates)
    )


def _build_server_sea(
    source_dir: Path,
    *,
    arch: str,
    version: str,
    output_dir: Path,
    env: Mapping[str, str],
) -> Path:
    """Build the standalone Node SEA server tarball using upstream build scripts."""
    _check_server_sea_prerequisites(env)
    _run_cmd(["vp", "run", "--filter", "t3", "build"], cwd=source_dir, env=env)
    _run_cmd(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--manifest-path",
            "native/resource-monitor/Cargo.toml",
        ],
        cwd=source_dir,
        env=env,
    )

    built_monitor = _locate_resource_monitor_binary(source_dir)
    staged_monitor = (
        source_dir
        / "apps"
        / "server"
        / "dist"
        / "resource-monitor"
        / f"linux-{arch}"
        / "t3-resource-monitor"
    )
    staged_monitor.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_monitor, staged_monitor)
    staged_monitor.chmod(0o755)

    _run_cmd(["node", "apps/server/scripts/cli.ts", "build-exe"], cwd=source_dir, env=env)
    _run_cmd(
        [
            "node",
            "scripts/build-cli-archive.ts",
            "--platform",
            "linux",
            "--arch",
            arch,
            "--version",
            version,
            "--output-dir",
            str(output_dir),
        ],
        cwd=source_dir,
        env=env,
    )

    expected = output_dir / f"t3-{version}-linux-{arch}.tar.gz"
    if expected.is_file():
        return expected
    return _find_single_artifact(output_dir, ".tar.gz")


def _build_server_bundle(
    source_dir: Path,
    *,
    arch: str,
    version: str,
    output_dir: Path,
    env: Mapping[str, str],
    sourcemap: bool,
    isolated_node_bin: Path | None,
) -> Path:
    """Build a portable non-SEA server bundle archive with wrapper script and optional Node."""
    _run_cmd(["vp", "run", "--filter", "t3", "build"], cwd=source_dir, env=env)

    server_dist = source_dir / "apps" / "server" / "dist"
    if not (server_dist / "bin.mjs").is_file():
        raise FileNotFoundError(f"Expected built server entrypoint at {server_dist / 'bin.mjs'}")

    stem = f"t3-{version}-linux-{arch}-bundle"
    archive_path = output_dir / f"{stem}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="t3clean-server-bundle-") as tmp_dir:
        bundle_dir = Path(tmp_dir) / stem
        bundle_dir.mkdir(parents=True, exist_ok=True)

        ignore = None if sourcemap else shutil.ignore_patterns("*.map")
        shutil.copytree(server_dist, bundle_dir / "dist", symlinks=True, ignore=ignore)

        server_pkg = source_dir / "apps" / "server" / "package.json"
        if server_pkg.is_file():
            shutil.copy2(server_pkg, bundle_dir / "package.json")

        server_node_modules = source_dir / "apps" / "server" / "node_modules"
        if server_node_modules.is_dir():
            shutil.copytree(
                server_node_modules,
                bundle_dir / "node_modules",
                symlinks=True,
                ignore=shutil.ignore_patterns(".bin", ".pnpm"),
            )

        bundled_node = False
        if isolated_node_bin is not None and isolated_node_bin.is_file():
            node_dest = bundle_dir / "node" / "bin" / "node"
            node_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(isolated_node_bin, node_dest)
            node_dest.chmod(0o755)
            bundled_node = True

        wrapper_path = bundle_dir / "t3"
        if bundled_node:
            wrapper_body = (
                "#!/bin/sh\n"
                'DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                'exec "$DIR/node/bin/node" "$DIR/dist/bin.mjs" "$@"\n'
            )
        else:
            wrapper_body = (
                "#!/bin/sh\n"
                'DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                'exec "${T3_NODE_BIN:-node}" "$DIR/dist/bin.mjs" "$@"\n'
            )
        wrapper_path.write_text(wrapper_body, encoding="utf-8")
        wrapper_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        (bundle_dir / "BUNDLE_README.txt").write_text(
            (
                "T3 Code No-Telemetry Server Bundle\n"
                "Run via: ./t3 (or: node dist/bin.mjs)\n"
                "Ensure production native externals are installed if node_modules/ was not staged.\n"
            ),
            encoding="utf-8",
        )

        if archive_path.exists():
            archive_path.unlink()
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(bundle_dir, arcname=stem)

    return archive_path


def _locate_extracted_t3_binary(root: Path) -> Path | None:
    """Return the extracted ``t3`` executable path if present at top level or single subdir."""
    direct = root / "t3"
    if direct.is_file():
        return direct
    subdirs = [entry for entry in root.iterdir() if entry.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "t3").is_file():
        return subdirs[0] / "t3"
    return None


def _verify_built_artifact(artifact_path: Path, options: BuildOptions) -> None:
    """Run post-build artifact verification, extracting tarballs into a temp dir when needed."""
    verify_opts = VerifyOptions(
        keep_updater=options.keep_updater,
        strict_offline=options.strict_offline,
    )
    findings = []
    if artifact_path.is_file() and artifact_path.name.endswith((".tar.gz", ".tgz")):
        with tempfile.TemporaryDirectory(prefix="t3clean-verify-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            _safe_extract_tar_to(artifact_path, tmp_path)
            report = verify_artifact(tmp_path, verify_opts)
            findings.extend(report.findings)
            t3_binary = _locate_extracted_t3_binary(tmp_path)
            if t3_binary is not None:
                bin_report = verify_artifact(t3_binary, verify_opts)
                findings.extend(bin_report.findings)
    else:
        report = verify_artifact(artifact_path, verify_opts)
        findings.extend(report.findings)

    if findings:
        details = "; ".join(f"{f.rule}@{f.path}: {f.detail}" for f in findings[:10])
        raise RuntimeError(
            f"Artifact verification failed for {artifact_path} "
            f"({len(findings)} finding(s)): {details}"
        )


def build_target(options: BuildOptions) -> Path:
    """Orchestrate a de-telemetrized build for ``options.target`` and return the artifact path."""
    if options.target not in VALID_TARGETS:
        raise ValueError(
            f"Invalid build target {options.target!r}; valid targets: {', '.join(VALID_TARGETS)}"
        )
    if options.arch not in VALID_ARCHES:
        raise ValueError(
            f"Invalid architecture {options.arch!r}; valid architectures: {', '.join(VALID_ARCHES)}"
        )

    source_dir = Path(options.source_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

    _remove_repo_env_files(source_dir)

    if not options.skip_patch:
        patch_source_tree(
            source_dir,
            PatchOptions(
                keep_updater=options.keep_updater,
                keep_cloud=options.keep_cloud,
            ),
        )

    version = options.version or read_source_version(source_dir)
    output_dir = (
        Path(options.output_dir).expanduser().resolve()
        if options.output_dir is not None
        else (source_dir / "dist-t3clean")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    env = sanitized_build_env(
        os.environ,
        cache_dir=options.cache_dir,
        sourcemap=options.sourcemap,
    )
    # Prevent git/vp prepare hooks inside a tarball build dir from walking up
    # and mutating an enclosing git repository's .git/config (e.g. core.hooksPath).
    ceiling_dir = str(source_dir.parent)
    existing_ceiling = env.get("GIT_CEILING_DIRECTORIES", "")
    if ceiling_dir not in existing_ceiling.split(os.pathsep):
        env["GIT_CEILING_DIRECTORIES"] = (
            f"{existing_ceiling}{os.pathsep}{ceiling_dir}" if existing_ceiling else ceiling_dir
        )
    if not env.get("T3CODE_GLIBC_BUILD_CEILING", "").strip():
        host_glibc = detect_host_glibc_version()
        if host_glibc:
            env["T3CODE_GLIBC_BUILD_CEILING"] = host_glibc
    explicit_jobs = _resolve_explicit_jobs(options.jobs, env)
    mem_info = read_host_memory_info()
    applied_limits = apply_concurrency_env(env, mem_info, explicit_jobs=explicit_jobs)
    if applied_limits and explicit_jobs is None:
        limit_summary = ", ".join(f"{k}={v}" for k, v in applied_limits.items())
        print(
            f"NOTE: Auto-throttled build concurrency for tight memory ({limit_summary}; "
            "override via --jobs / T3CLEAN_JOBS or explicit env vars).",
            file=sys.stderr,
        )
    low_mem_warning = format_low_memory_warning(mem_info, applied_limits=applied_limits)
    if low_mem_warning is not None:
        print(low_mem_warning, file=sys.stderr)
    if options.cache_dir is not None:
        for cache_key in (
            "ELECTRON_CACHE",
            "ELECTRON_BUILDER_CACHE",
            "CARGO_HOME",
            "PNPM_HOME",
            "npm_config_store_dir",
            "XDG_CACHE_HOME",
        ):
            Path(env[cache_key]).mkdir(parents=True, exist_ok=True)

    isolated_node_bin: Path | None = None
    if options.bootstrap_toolchain:
        required_node = read_required_node_version(source_dir)
        required_vp = read_required_vp_version(source_dir)
        toolchain_cache = (
            Path(options.cache_dir).expanduser().resolve()
            if options.cache_dir is not None
            else (source_dir / ".t3clean-cache")
        )
        toolchain_bin = ensure_isolated_toolchain(
            toolchain_cache,
            required_node,
            arch=options.arch,
            vp_version=required_vp,
        )
        env["PATH"] = f"{toolchain_bin}{os.pathsep}{env.get('PATH', '')}"
        candidate_node = toolchain_bin / "node"
        if candidate_node.is_file():
            isolated_node_bin = candidate_node

    if not options.skip_install:
        _run_cmd(["vp", "install", "--frozen-lockfile"], cwd=source_dir, env=env)

    if options.target == "desktop-dir":
        _run_cmd(
            [
                "node",
                "scripts/build-desktop-artifact.ts",
                "--platform",
                "linux",
                "--target",
                "dir",
                "--arch",
                options.arch,
                "--build-version",
                version,
                "--output-dir",
                str(output_dir),
            ],
            cwd=source_dir,
            env=env,
        )
        unpacked_dir = _find_unpacked_desktop_dir(output_dir, options.arch)
        if options.use_system_electron:
            artifact_path = _prepare_system_electron_dir(
                unpacked_dir,
                output_dir,
                options.system_electron_bin,
            )
        else:
            artifact_path = unpacked_dir
    elif options.target == "desktop-deb":
        _run_cmd(
            [
                "node",
                "scripts/build-desktop-artifact.ts",
                "--platform",
                "linux",
                "--target",
                "deb",
                "--arch",
                options.arch,
                "--build-version",
                version,
                "--output-dir",
                str(output_dir),
            ],
            cwd=source_dir,
            env=env,
        )
        artifact_path = _find_single_artifact(output_dir, ".deb")
    elif options.target == "desktop-appimage":
        _run_cmd(
            [
                "node",
                "scripts/build-desktop-artifact.ts",
                "--platform",
                "linux",
                "--target",
                "AppImage",
                "--arch",
                options.arch,
                "--build-version",
                version,
                "--output-dir",
                str(output_dir),
            ],
            cwd=source_dir,
            env=env,
        )
        artifact_path = _find_single_artifact(output_dir, ".AppImage")
    elif options.target == "server-sea":
        artifact_path = _build_server_sea(
            source_dir,
            arch=options.arch,
            version=version,
            output_dir=output_dir,
            env=env,
        )
    elif options.target == "server-bundle":
        artifact_path = _build_server_bundle(
            source_dir,
            arch=options.arch,
            version=version,
            output_dir=output_dir,
            env=env,
            sourcemap=options.sourcemap,
            isolated_node_bin=isolated_node_bin,
        )
    else:
        raise ValueError(f"Unhandled build target: {options.target!r}")

    if not options.skip_verify:
        _verify_built_artifact(artifact_path, options)

    return artifact_path
