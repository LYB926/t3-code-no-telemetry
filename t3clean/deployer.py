"""Linux systemd user server deployment, backup preservation, and automatic rollback."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .source import EXACT_SERVICE_VERSION_RE
from .verifier import VerifyOptions, verify_artifact

SERVICE_UNIT_NAME = "t3code.service"
SERVICE_DROPIN_DIRNAME = f"{SERVICE_UNIT_NAME}.d"
BACKUPS_SUBDIR = "no-telemetry-backups"
_ARCHIVE_VERSION_RE = re.compile(
    r"^t3-(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"
    r"-(?:linux|darwin|win32)-(?:x64|arm64)(?:-bundle)?$"
)
_SAFE_LABEL_RE = re.compile(r"[^0-9A-Za-z._+-]+")


@dataclass(frozen=True)
class DeployOptions:
    """Configuration for deploying a verified server runtime archive to a Linux host."""

    archive_path: Path
    t3_home: Path
    bin_dir: Path
    unit_dir: Path | None = None
    version: str | None = None
    clean_anonymous_id: bool = True
    dry_run: bool = False
    skip_verify: bool = False
    skip_systemctl: bool = False
    max_backups: int | None = None


def _resolve_unit_dir(unit_dir: Path | None) -> Path:
    """Resolve the systemd user unit directory."""
    if unit_dir is not None:
        return Path(unit_dir).expanduser().resolve()
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        return (Path(xdg_config).expanduser() / "systemd" / "user").resolve()
    return (Path.home() / ".config" / "systemd" / "user").resolve()


def _backups_root(t3_home: Path) -> Path:
    """Return the backup directory strictly outside ``runtime/versions/``."""
    return t3_home / "runtime" / BACKUPS_SUBDIR


def _write_bytes_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    """Write raw bytes atomically to ``path`` via sibling temporary file replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        if mode is not None:
            tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_text_atomic(path: Path, content: str, *, mode: int | None = None) -> None:
    """Write UTF-8 text atomically to ``path`` via sibling temporary file replacement."""
    _write_bytes_atomic(path, content.encode("utf-8"), mode=mode)


def _posix_single_quote(value: str) -> str:
    """Single-quote a string safely for POSIX ``/bin/sh``."""
    return "'" + value.replace("'", "'\\''") + "'"


def _format_exec_start(binary_path: Path) -> str:
    """Format the systemd ``ExecStart=`` value for ``t3 __service-launcher``."""
    binary_str = str(binary_path)
    if any(ch.isspace() for ch in binary_str):
        escaped = binary_str.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}" __service-launcher'
    return f"{binary_str} __service-launcher"


def safe_extract_tar(archive_path: Path, dest_dir: Path) -> Path:
    """Extract a server runtime archive safely into ``dest_dir`` and return the runtime root.

    Rejects absolute paths, ``..`` traversal components, special devices, and symlinks or
    hardlinks resolving outside ``dest_dir``.
    """
    archive = Path(archive_path).expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Runtime archive not found: {archive}")

    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Unsafe tar member path: {member.name!r}")
            if member.isdev():
                raise ValueError(f"Refusing special device tar member: {member.name!r}")
            target = (dest_root / member.name).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError as exc:
                raise ValueError(f"Tar member escapes extraction root: {member.name!r}") from exc

            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise ValueError(
                        f"Refusing absolute symlink in tar: {member.name!r} -> {member.linkname!r}"
                    )
                resolved_link = (target.parent / member.linkname).resolve()
                try:
                    resolved_link.relative_to(dest_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Tar symlink escapes extraction root: {member.name!r} -> {member.linkname!r}"
                    ) from exc
            elif member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ValueError(
                        f"Refusing unsafe hardlink in tar: {member.name!r} -> {member.linkname!r}"
                    )
                resolved_link = (dest_root / member.linkname).resolve()
                try:
                    resolved_link.relative_to(dest_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Tar hardlink escapes extraction root: {member.name!r} -> {member.linkname!r}"
                    ) from exc

        if hasattr(tarfile, "tar_filter"):
            tar.extractall(dest_root, members=members, filter="tar")
        else:
            tar.extractall(dest_root, members=members)

    if (dest_root / "t3").is_file():
        return dest_root

    subdirs = [entry for entry in dest_root.iterdir() if entry.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "t3").is_file():
        return subdirs[0]

    raise ValueError(
        f"Extracted archive {archive.name!r} does not contain a top-level 't3' executable"
    )


def _infer_version_from_stem(stem: str) -> str | None:
    """Extract a SemVer version from an archive/directory stem like ``t3-0.1.12+notelemetry-linux-x64``."""
    match = _ARCHIVE_VERSION_RE.match(stem)
    if match is not None:
        return match.group("version")
    return None


def _resolve_deploy_version(
    explicit_version: str | None,
    archive_path: Path,
    extracted_root: Path,
) -> str:
    """Determine and validate the target runtime version string."""
    candidate = explicit_version.strip() if explicit_version else None

    if candidate is None:
        candidate = _infer_version_from_stem(extracted_root.name)

    if candidate is None:
        archive_name = archive_path.name
        for suffix in (".tar.gz", ".tgz", ".tar.xz", ".tar"):
            if archive_name.endswith(suffix):
                archive_name = archive_name[: -len(suffix)]
                break
        candidate = _infer_version_from_stem(archive_name)

    if candidate is None:
        for rel in ("package.json", "dist/package.json"):
            pkg_file = extracted_root / rel
            if pkg_file.is_file():
                try:
                    data = json.loads(pkg_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("version"), str):
                    candidate = data["version"].strip()
                    break

    if not candidate:
        raise ValueError(
            "Unable to infer version from archive; pass an explicit version via --version"
        )
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"Unsafe version string: {candidate!r}")
    if EXACT_SERVICE_VERSION_RE.match(candidate) is None:
        raise ValueError(
            f"Version {candidate!r} does not match upstream EXACT_SERVICE_VERSION pattern"
        )
    return candidate


def _read_service_state(service_state_path: Path) -> dict[str, Any]:
    """Read ``runtime/service-state.json`` if present, returning a dict."""
    if not service_state_path.is_file():
        return {}
    try:
        data = json.loads(service_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to parse {service_state_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON structure in {service_state_path}")
    return data


def _online_sqlite_backup(source_db: Path, dest_db: Path) -> None:
    """Create a consistent point-in-time backup of a SQLite database using sqlite3's backup API."""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = dest_db.with_suffix(dest_db.suffix + ".tmp")
    tmp_db.unlink(missing_ok=True)
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        try:
            src_conn = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            src_conn = sqlite3.connect(str(source_db))
        dst_conn = sqlite3.connect(str(tmp_db))
        with dst_conn:
            src_conn.backup(dst_conn)
    except Exception:
        tmp_db.unlink(missing_ok=True)
        raise
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()
    os.replace(tmp_db, dest_db)


def _create_backup_id(backups_root: Path, prev_version: str | None) -> str:
    """Generate a unique backup identifier named by the backed-up version."""
    raw_label = prev_version or "fresh"
    label = _SAFE_LABEL_RE.sub("_", raw_label).strip("_") or "fresh"
    if not (backups_root / label).exists():
        return label
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_id = f"{label}-{timestamp}"
    candidate = base_id
    counter = 1
    while (backups_root / candidate).exists():
        candidate = f"{base_id}-{counter}"
        counter += 1
    return candidate


def _backup_current_state(
    *,
    t3_home: Path,
    bin_dir: Path,
    unit_dir: Path,
    prev_version: str | None,
    target_version: str,
) -> tuple[str, Path]:
    """Snapshot current runtime, unit, state, wrapper, service.env, and SQLite DB outside ``versions/``."""
    backups_root = _backups_root(t3_home)
    backups_root.mkdir(parents=True, exist_ok=True)
    backup_id = _create_backup_id(backups_root, prev_version)
    backup_dir = backups_root / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)

    runtime_dir = t3_home / "runtime"
    versions_dir = runtime_dir / "versions"

    has_runtime_version = False
    if prev_version:
        prev_runtime_dir = versions_dir / prev_version
        if prev_runtime_dir.is_dir():
            shutil.copytree(prev_runtime_dir, backup_dir / "runtime-version", symlinks=True)
            has_runtime_version = True

    has_service_state = False
    service_state_path = runtime_dir / "service-state.json"
    if service_state_path.is_file():
        shutil.copy2(service_state_path, backup_dir / "service-state.json")
        has_service_state = True

    has_systemd_unit = False
    unit_path = unit_dir / SERVICE_UNIT_NAME
    if unit_path.is_file():
        shutil.copy2(unit_path, backup_dir / SERVICE_UNIT_NAME)
        has_systemd_unit = True

    has_systemd_dropins = False
    dropin_dir = unit_dir / SERVICE_DROPIN_DIRNAME
    if dropin_dir.is_dir():
        shutil.copytree(dropin_dir, backup_dir / SERVICE_DROPIN_DIRNAME, symlinks=True)
        has_systemd_dropins = True

    has_bin_wrapper = False
    bin_wrapper = bin_dir / "t3"
    if bin_wrapper.is_symlink():
        (backup_dir / "bin-t3.symlink").write_text(os.readlink(bin_wrapper), encoding="utf-8")
        has_bin_wrapper = True
    elif bin_wrapper.is_file():
        shutil.copy2(bin_wrapper, backup_dir / "bin-t3")
        has_bin_wrapper = True

    has_service_env = False
    service_env_path = t3_home / "service.env"
    if service_env_path.is_file():
        shutil.copy2(service_env_path, backup_dir / "service.env")
        has_service_env = True

    has_db_backup = False
    db_path = t3_home / "userdata" / "statev2.sqlite"
    if db_path.is_file():
        _online_sqlite_backup(db_path, backup_dir / "statev2.sqlite")
        has_db_backup = True

    manifest = {
        "backupId": backup_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "previousVersion": prev_version or "",
        "targetVersion": target_version,
        "t3Home": str(t3_home),
        "binDir": str(bin_dir),
        "unitDir": str(unit_dir),
        "hasRuntimeVersion": has_runtime_version,
        "hasServiceState": has_service_state,
        "hasSystemdUnit": has_systemd_unit,
        "hasSystemdDropIns": has_systemd_dropins,
        "hasBinWrapper": has_bin_wrapper,
        "hasServiceEnv": has_service_env,
        "hasDatabaseBackup": has_db_backup,
    }
    _write_text_atomic(
        backup_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return backup_id, backup_dir


def _render_updated_unit(
    existing_text: str | None,
    *,
    binary_path: Path,
    t3_home: Path,
) -> str:
    """Rewrite ``ExecStart=`` in an existing unit file or render a fresh systemd user unit."""
    exec_start_line = f"ExecStart={_format_exec_start(binary_path)}"
    if existing_text is None:
        return (
            "[Unit]\n"
            "Description=T3 Code Server\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"{exec_start_line}\n"
            f"Environment=T3CODE_HOME={t3_home}\n"
            f"Environment=T3_BOOT_SERVICE_UNIT={SERVICE_UNIT_NAME}\n"
            "Environment=T3CODE_TELEMETRY_ENABLED=false\n"
            "Environment=DO_NOT_TRACK=1\n"
            f"EnvironmentFile=-{t3_home / 'service.env'}\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    lines = existing_text.splitlines()
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        if not replaced and re.match(r"^\s*ExecStart\s*=", line):
            new_lines.append(exec_start_line)
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        inserted = False
        rebuilt: list[str] = []
        for line in new_lines:
            rebuilt.append(line)
            if not inserted and line.strip() == "[Service]":
                rebuilt.append(exec_start_line)
                inserted = True
        if not inserted:
            rebuilt.extend(["", "[Service]", exec_start_line])
        new_lines = rebuilt

    return "\n".join(new_lines) + "\n"


def _update_service_env(service_env_path: Path) -> None:
    """Ensure ``T3CODE_TELEMETRY_ENABLED=false`` and ``DO_NOT_TRACK=1`` exist in ``service.env``."""
    existing_lines = (
        service_env_path.read_text(encoding="utf-8").splitlines()
        if service_env_path.is_file()
        else []
    )
    desired = {
        "T3CODE_TELEMETRY_ENABLED": "false",
        "DO_NOT_TRACK": "1",
    }
    seen: set[str] = set()
    updated_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in desired:
                if key not in seen:
                    updated_lines.append(f"{key}={desired[key]}")
                    seen.add(key)
                continue
        updated_lines.append(line)

    for key, value in desired.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")

    _write_text_atomic(service_env_path, "\n".join(updated_lines) + "\n")


def _run_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute ``systemctl --user <args>`` and raise RuntimeError on non-zero exit."""
    cmd = ["systemctl", "--user", *args]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("systemctl executable not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit status {exc.returncode}"
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail}") from exc


SERVICE_SETTLE_SECONDS = 5.0


def _restart_and_check_service(*, settle_seconds: float = SERVICE_SETTLE_SECONDS) -> str:
    """Reload systemd user units, restart ``t3code.service``, and verify it stays active."""
    _run_systemctl(["daemon-reload"])
    _run_systemctl(["restart", SERVICE_UNIT_NAME])
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    active_result = _run_systemctl(["is-active", SERVICE_UNIT_NAME])
    state = active_result.stdout.strip()
    if state != "active":
        raise RuntimeError(
            f"{SERVICE_UNIT_NAME} is not active after restart (reported state: {state!r})"
        )
    return state


def _install_runtime_dir(
    source_root: Path,
    install_dir: Path,
    *,
    version: str | None = None,
) -> None:
    """Install ``source_root`` into ``install_dir`` with executable ``t3`` and ``.install-complete``."""
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{install_dir.name}.staging-", dir=str(install_dir.parent))
    )
    try:
        shutil.copytree(source_root, stage_dir, dirs_exist_ok=True, symlinks=True)
        binary = stage_dir / "t3"
        if not binary.is_file():
            raise FileNotFoundError(f"Runtime payload is missing 't3' executable: {binary}")
        binary.chmod(0o755)
        sentinel_version = (version or install_dir.name).strip()
        (stage_dir / ".install-complete").write_text(
            f"{sentinel_version}\n",
            encoding="utf-8",
        )
        if install_dir.exists():
            shutil.rmtree(install_dir)
        os.replace(stage_dir, install_dir)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def list_backups(t3_home: Path) -> list[dict[str, str]]:
    """List rollback snapshots stored in ``<t3_home>/runtime/no-telemetry-backups`` (newest first)."""
    backups_root = _backups_root(Path(t3_home).expanduser().resolve())
    if not backups_root.is_dir():
        return []

    records: list[tuple[str, dict[str, str]]] = []
    for entry in backups_root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, json.JSONDecodeError):
                manifest = {}

        backup_id = str(manifest.get("backupId") or entry.name)
        created_at = str(manifest.get("createdAt") or "")
        sort_key = f"{created_at}|{backup_id}"
        records.append(
            (
                sort_key,
                {
                    "backup_id": backup_id,
                    "created_at": created_at,
                    "previous_version": str(manifest.get("previousVersion") or ""),
                    "target_version": str(manifest.get("targetVersion") or ""),
                    "has_db_backup": "true"
                    if bool(manifest.get("hasDatabaseBackup", (entry / "statev2.sqlite").is_file()))
                    else "false",
                    "path": str(entry),
                },
            )
        )

    records.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in records]


def prune_backups(
    t3_home: Path,
    max_backups: int,
    *,
    keep_backup_ids: set[str] | None = None,
) -> list[str]:
    """Delete oldest backup snapshots beyond ``max_backups``, returning pruned IDs."""
    if max_backups < 1:
        raise ValueError(f"max_backups must be >= 1, got {max_backups}")
    backups = list_backups(t3_home)
    if len(backups) <= max_backups:
        return []

    protected = keep_backup_ids or set()
    kept = 0
    pruned: list[str] = []
    for entry in backups:
        backup_id = entry["backup_id"]
        if backup_id in protected or kept < max_backups:
            kept += 1
            continue
        shutil.rmtree(Path(entry["path"]), ignore_errors=False)
        pruned.append(backup_id)
    return pruned


def rollback_server(
    t3_home: Path,
    bin_dir: Path,
    *,
    unit_dir: Path | None = None,
    backup_id: str | None = None,
    restore_db: bool = False,
    skip_systemctl: bool = False,
) -> dict[str, str]:
    """Restore a previous server runtime snapshot from ``no-telemetry-backups/``."""
    resolved_home = Path(t3_home).expanduser().resolve()
    resolved_bin = Path(bin_dir).expanduser().resolve()
    resolved_unit = _resolve_unit_dir(unit_dir)
    backups_root = _backups_root(resolved_home)

    if backup_id is not None:
        if "/" in backup_id or "\\" in backup_id or ".." in backup_id or not backup_id.strip():
            raise ValueError(f"Invalid backup_id: {backup_id!r}")
        backup_dir = backups_root / backup_id
        if not backup_dir.is_dir():
            raise FileNotFoundError(f"Backup not found: {backup_dir}")
        selected_id = backup_id
    else:
        available = list_backups(resolved_home)
        if not available:
            raise FileNotFoundError(f"No backups found in {backups_root}")
        selected_id = available[0]["backup_id"]
        backup_dir = Path(available[0]["path"])

    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Backup manifest missing in {backup_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prev_version = str(manifest.get("previousVersion") or "")

    if restore_db:
        db_backup = backup_dir / "statev2.sqlite"
        if not db_backup.is_file():
            raise FileNotFoundError(f"Backup {selected_id!r} does not contain statev2.sqlite")
        if not skip_systemctl:
            try:
                _run_systemctl(["stop", SERVICE_UNIT_NAME])
            except RuntimeError:
                pass

    runtime_dir = resolved_home / "runtime"
    versions_dir = runtime_dir / "versions"

    backup_runtime_dir = backup_dir / "runtime-version"
    if prev_version and backup_runtime_dir.is_dir():
        _install_runtime_dir(backup_runtime_dir, versions_dir / prev_version)

    state_backup = backup_dir / "service-state.json"
    state_target = runtime_dir / "service-state.json"
    if state_backup.is_file():
        _write_text_atomic(state_target, state_backup.read_text(encoding="utf-8"))
    elif not manifest.get("hasServiceState", False) and state_target.exists():
        state_target.unlink()

    unit_backup = backup_dir / SERVICE_UNIT_NAME
    unit_target = resolved_unit / SERVICE_UNIT_NAME
    if unit_backup.is_file():
        _write_text_atomic(unit_target, unit_backup.read_text(encoding="utf-8"))
    elif not manifest.get("hasSystemdUnit", False) and unit_target.exists():
        unit_target.unlink()

    dropin_backup = backup_dir / SERVICE_DROPIN_DIRNAME
    dropin_target = resolved_unit / SERVICE_DROPIN_DIRNAME
    if dropin_backup.is_dir():
        if dropin_target.exists():
            shutil.rmtree(dropin_target)
        shutil.copytree(dropin_backup, dropin_target, symlinks=True)

    bin_target = resolved_bin / "t3"
    bin_symlink_backup = backup_dir / "bin-t3.symlink"
    bin_file_backup = backup_dir / "bin-t3"
    if bin_symlink_backup.is_file():
        resolved_bin.mkdir(parents=True, exist_ok=True)
        link_dest = bin_symlink_backup.read_text(encoding="utf-8").strip()
        if bin_target.exists() or bin_target.is_symlink():
            bin_target.unlink()
        os.symlink(link_dest, bin_target)
    elif bin_file_backup.is_file():
        _write_bytes_atomic(bin_target, bin_file_backup.read_bytes(), mode=0o755)
    elif not manifest.get("hasBinWrapper", False) and (bin_target.exists() or bin_target.is_symlink()):
        bin_target.unlink()

    service_env_backup = backup_dir / "service.env"
    service_env_target = resolved_home / "service.env"
    if service_env_backup.is_file():
        _write_text_atomic(service_env_target, service_env_backup.read_text(encoding="utf-8"))
    elif not manifest.get("hasServiceEnv", False) and service_env_target.exists():
        service_env_target.unlink()

    db_restored = False
    if restore_db:
        db_target = resolved_home / "userdata" / "statev2.sqlite"
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_target}{suffix}")
            sidecar.unlink(missing_ok=True)
        _online_sqlite_backup(backup_dir / "statev2.sqlite", db_target)
        db_restored = True

    systemctl_status = "skipped"
    if not skip_systemctl:
        _run_systemctl(["daemon-reload"])
        if unit_target.is_file():
            systemctl_status = _restart_and_check_service()
        else:
            systemctl_status = "unit-removed"

    return {
        "backup_id": selected_id,
        "backup_dir": str(backup_dir),
        "restored_version": prev_version,
        "db_restored": "true" if db_restored else "false",
        "systemctl_status": systemctl_status,
    }


def deploy_server(options: DeployOptions) -> dict[str, str]:
    """Deploy a server runtime archive into ``t3_home`` with backup and automatic rollback."""
    if options.max_backups is not None and options.max_backups < 1:
        raise ValueError(f"max_backups must be >= 1, got {options.max_backups}")
    archive_path = Path(options.archive_path).expanduser().resolve()
    t3_home = Path(options.t3_home).expanduser().resolve()
    bin_dir = Path(options.bin_dir).expanduser().resolve()
    unit_dir = _resolve_unit_dir(options.unit_dir)

    with tempfile.TemporaryDirectory(prefix="t3clean-deploy-") as tmp_dir:
        extracted_root = safe_extract_tar(archive_path, Path(tmp_dir))
        new_version = _resolve_deploy_version(options.version, archive_path, extracted_root)

        if not options.skip_verify:
            verify_opts = VerifyOptions()
            report = verify_artifact(extracted_root, verify_opts)
            all_findings = list(report.findings)
            t3_binary = extracted_root / "t3"
            if t3_binary.is_file():
                bin_report = verify_artifact(t3_binary, verify_opts)
                all_findings.extend(bin_report.findings)
            if all_findings:
                details = "; ".join(
                    f"{f.rule}@{f.path}: {f.detail}" for f in all_findings[:10]
                )
                raise RuntimeError(
                    f"Extracted server archive failed verification ({len(all_findings)} finding(s)): "
                    f"{details}"
                )

        runtime_dir = t3_home / "runtime"
        versions_dir = runtime_dir / "versions"
        service_state_path = runtime_dir / "service-state.json"
        existing_state = _read_service_state(service_state_path)
        prev_version = existing_state.get("activeVersion")
        if not isinstance(prev_version, str) or not prev_version.strip():
            prev_version = None

        install_dir = versions_dir / new_version
        unit_path = unit_dir / SERVICE_UNIT_NAME
        bin_wrapper = bin_dir / "t3"
        service_env_path = t3_home / "service.env"

        if options.dry_run:
            return {
                "status": "dry-run",
                "version": new_version,
                "previous_version": prev_version or "",
                "install_dir": str(install_dir),
                "backups_root": str(_backups_root(t3_home)),
                "unit_path": str(unit_path),
                "bin_wrapper": str(bin_wrapper),
                "service_env": str(service_env_path),
            }

        backup_id, backup_dir = _backup_current_state(
            t3_home=t3_home,
            bin_dir=bin_dir,
            unit_dir=unit_dir,
            prev_version=prev_version,
            target_version=new_version,
        )

        _install_runtime_dir(extracted_root, install_dir)

        updated_state = dict(existing_state)
        updated_state.setdefault("protocol", 4)
        updated_state["activeVersion"] = new_version
        _write_text_atomic(
            service_state_path,
            json.dumps(updated_state, indent=2) + "\n",
        )

        existing_unit = unit_path.read_text(encoding="utf-8") if unit_path.is_file() else None
        updated_unit = _render_updated_unit(
            existing_unit,
            binary_path=install_dir / "t3",
            t3_home=t3_home,
        )
        _write_text_atomic(unit_path, updated_unit)

        wrapper_content = f"#!/bin/sh\nexec {_posix_single_quote(str(install_dir / 't3'))} \"$@\"\n"
        _write_text_atomic(bin_wrapper, wrapper_content, mode=0o755)

        _update_service_env(service_env_path)

        anonymous_id_removed = False
        if options.clean_anonymous_id:
            anon_path = t3_home / "userdata" / "anonymous-id"
            if anon_path.exists() or anon_path.is_symlink():
                anon_path.unlink()
                anonymous_id_removed = True

        systemctl_status = "skipped"
        if not options.skip_systemctl:
            try:
                systemctl_status = _restart_and_check_service()
            except Exception as exc:
                try:
                    rollback_result = rollback_server(
                        t3_home,
                        bin_dir,
                        unit_dir=unit_dir,
                        backup_id=backup_id,
                        restore_db=False,
                        skip_systemctl=False,
                    )
                    rollback_note = (
                        f"automatic rollback to {rollback_result['restored_version'] or 'previous state'} "
                        f"succeeded (backup_id={backup_id})"
                    )
                except Exception as rollback_exc:
                    rollback_note = f"automatic rollback ALSO failed: {rollback_exc}"
                raise RuntimeError(
                    f"Deployment of {new_version} failed during service restart/health check: "
                    f"{exc}; {rollback_note}"
                ) from exc

        pruned_backups: list[str] = []
        if options.max_backups is not None:
            pruned_backups = prune_backups(
                t3_home,
                options.max_backups,
                keep_backup_ids={backup_id},
            )

        return {
            "status": "deployed",
            "version": new_version,
            "previous_version": prev_version or "",
            "backup_id": backup_id,
            "backup_dir": str(backup_dir),
            "pruned_backups": ",".join(pruned_backups),
            "install_dir": str(install_dir),
            "unit_path": str(unit_path),
            "bin_wrapper": str(bin_wrapper),
            "service_env": str(service_env_path),
            "anonymous_id_removed": "true" if anonymous_id_removed else "false",
            "systemctl_status": systemctl_status,
        }
