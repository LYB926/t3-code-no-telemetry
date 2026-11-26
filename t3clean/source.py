"""Source repository fetching, SemVer tag resolution, and metadata inspection."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import DEFAULT_NODE_VERSION, DEFAULT_REF, DEFAULT_REPO_URL

EXACT_SERVICE_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_STABLE_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_VERSION_REF_RE = re.compile(
    r"^v?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)
_NODE_VERSION_RE = re.compile(r"^(?:>=|<=|>|<|=|\^|~|\s)*v?(\d+\.\d+\.\d+)$")
_VP_VERSION_RE = re.compile(r"^(?:>=|<=|>|<|=|\^|~|\s)*v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
_YAML_CATALOG_HEADER_RE = re.compile(r"^catalog\s*:\s*(?:#.*)?$")
_YAML_VP_ENTRY_RE = re.compile(r"""^(?:"vite-plus"|'vite-plus'|vite-plus)\s*:\s*(.+?)\s*$""")
_EXCLUDED_TAG_MARKERS = ("nightly", "preview")


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command and raise a descriptive RuntimeError on failure."""
    cmd = ["git", *args]
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or f"exit status {exc.returncode}"
        raise RuntimeError(f"git command failed ({' '.join(cmd)}): {detail}") from exc


def pick_latest_stable_tag(ls_remote_output: str) -> str:
    """Parse ``git ls-remote --tags --refs`` output and return the newest stable SemVer tag.

    Excludes tags containing ``nightly`` or ``preview`` (case-insensitive),
    peeled tag refs (``^{}``), pre-release tags, and non-SemVer tags.
    """
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for raw_line in ls_remote_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ref_name = parts[1]
        if ref_name.endswith("^{}"):
            continue
        if not ref_name.startswith("refs/tags/"):
            continue
        tag = ref_name[len("refs/tags/") :]
        lowered = tag.lower()
        if any(marker in lowered for marker in _EXCLUDED_TAG_MARKERS):
            continue
        match = _STABLE_TAG_RE.match(tag)
        if match is None:
            continue
        semver = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        candidates.append((semver, tag))

    if not candidates:
        raise ValueError("No stable SemVer tags found in git ls-remote output")

    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


_GITHUB_SLUG_RE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def parse_github_repo_slug(repo_url: str = DEFAULT_REPO_URL) -> tuple[str, str]:
    """Extract ``(owner, repo)`` from a GitHub HTTPS or SSH repository URL."""
    match = _GITHUB_SLUG_RE.search(repo_url.strip())
    if match is None:
        raise ValueError(f"Unsupported GitHub repository URL: {repo_url!r}")
    return match.group("owner"), match.group("repo")


def resolve_latest_github_release(
    repo_url: str = DEFAULT_REPO_URL,
    *,
    token: str | None = None,
) -> dict[str, str]:
    """Query GitHub Releases API ``/releases/latest`` for the newest stable release.

    Validates that the returned release is neither a draft nor a prerelease, and that
    its ``tag_name`` is a clean stable SemVer tag (``vX.Y.Z`` without nightly/preview).
    Returns ``{"tag", "version", "html_url", "published_at"}``.
    """
    owner, repo = parse_github_repo_slug(repo_url)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "t3-code-no-telemetry/t3clean",
    }
    auth_token = (token or os.environ.get("GITHUB_TOKEN") or "").strip()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to query latest GitHub release from {api_url}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected GitHub release payload from {api_url}")
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError(f"Latest GitHub release is marked draft/prerelease: {payload.get('tag_name')!r}")

    tag = str(payload.get("tag_name") or "").strip()
    lowered = tag.lower()
    if any(marker in lowered for marker in _EXCLUDED_TAG_MARKERS) or _STABLE_TAG_RE.match(tag) is None:
        raise ValueError(f"Latest GitHub release tag is not a stable SemVer tag: {tag!r}")

    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "html_url": str(payload.get("html_url") or f"https://github.com/{owner}/{repo}/releases/tag/{tag}"),
        "published_at": str(payload.get("published_at") or ""),
    }


def resolve_latest_stable_tag(repo_url: str = DEFAULT_REPO_URL) -> str:
    """Query ``repo_url`` for the newest stable SemVer tag (for example ``v0.1.12``)."""
    result = _run_git(["ls-remote", "--tags", "--refs", repo_url, "v[0-9]*"])
    return pick_latest_stable_tag(result.stdout)


def _read_package_version(package_json_path: Path) -> str | None:
    """Return the trimmed ``version`` string from a package.json file, if valid."""
    if not package_json_path.is_file():
        return None
    try:
        data = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _read_exact_head_stable_tag(source_dir: Path) -> str | None:
    """Return the highest stable ``vX.Y.Z`` tag pointing at HEAD, if ``source_dir`` is a git repo."""
    if not (source_dir / ".git").exists():
        return None
    try:
        result = _run_git(["tag", "--points-at", "HEAD"], cwd=source_dir)
    except RuntimeError:
        return None
    stable: list[tuple[tuple[int, int, int], str]] = []
    for line in result.stdout.splitlines():
        tag = line.strip()
        match = _STABLE_TAG_RE.match(tag)
        if match is not None:
            stable.append(
                ((int(match.group(1)), int(match.group(2)), int(match.group(3))), tag[1:])
            )
    if not stable:
        return None
    stable.sort(key=lambda item: item[0])
    return stable[-1][1]


def _apply_version_suffix(base_version: str, suffix: str) -> str:
    """Attach build metadata or pre-release suffix while keeping valid SemVer syntax."""
    if not suffix:
        return base_version
    if suffix.startswith("+"):
        meta = suffix[1:]
        if not meta:
            return base_version
        if "+" in base_version:
            existing_meta = base_version.split("+", 1)[1]
            meta_parts = existing_meta.split(".")
            if meta in meta_parts:
                return base_version
            return f"{base_version}.{meta}"
        return f"{base_version}{suffix}"
    if base_version.endswith(suffix):
        return base_version
    return f"{base_version}{suffix}"


def read_source_version(
    source_dir: Path,
    ref: str | None = None,
    suffix: str = "+notelemetry",
) -> str:
    """Determine a clean SemVer version string matching upstream ``EXACT_SERVICE_VERSION``.

    If ``ref`` is a SemVer tag such as ``v0.1.12``, ``0.1.12`` is used as the base version.
    Otherwise, checks a stable tag pointing at HEAD (when ``ref`` is omitted) and falls back
    to ``apps/server/package.json`` or ``apps/desktop/package.json``.
    """
    source_path = Path(source_dir)
    base_version: str | None = None

    if ref is not None:
        match = _VERSION_REF_RE.match(ref.strip())
        if match is not None:
            base_version = match.group("version")
    else:
        base_version = _read_exact_head_stable_tag(source_path)

    if base_version is None:
        for rel_path in ("apps/server/package.json", "apps/desktop/package.json"):
            candidate = _read_package_version(source_path / rel_path)
            if candidate is not None:
                base_version = candidate.lstrip("v")
                break

    if base_version is None:
        raise ValueError(
            f"Could not determine source version from ref={ref!r} or package manifests in {source_path}"
        )

    version = _apply_version_suffix(base_version, suffix)
    if EXACT_SERVICE_VERSION_RE.match(version) is None:
        raise ValueError(
            f"Computed version {version!r} does not match upstream EXACT_SERVICE_VERSION pattern"
        )
    return version


def read_required_node_version(source_dir: Path) -> str:
    """Read ``package.json#engines.node`` (normalized to ``X.Y.Z``) or return ``DEFAULT_NODE_VERSION``."""
    package_json = Path(source_dir) / "package.json"
    if not package_json.is_file():
        return DEFAULT_NODE_VERSION
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_NODE_VERSION
    if not isinstance(data, dict):
        return DEFAULT_NODE_VERSION
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return DEFAULT_NODE_VERSION
    raw_node = engines.get("node")
    if not isinstance(raw_node, str):
        return DEFAULT_NODE_VERSION
    match = _NODE_VERSION_RE.match(raw_node.strip())
    if match is None:
        return DEFAULT_NODE_VERSION
    return match.group(1)


def _extract_yaml_scalar(raw_value: str) -> str:
    """Extract a quoted or unquoted YAML scalar value, stripping trailing comments."""
    val = raw_value.strip()
    if not val:
        return ""
    if val.startswith('"'):
        m = re.match(r'^"([^"]+)"(?:\s*#.*)?$', val)
        return m.group(1).strip() if m else ""
    if val.startswith("'"):
        m = re.match(r"^'([^']+)'(?:\s*#.*)?$", val)
        return m.group(1).strip() if m else ""
    return val.split("#", 1)[0].strip()


def read_required_vp_version(source_dir: Path) -> str | None:
    """Read the pinned ``vite-plus`` version from ``pnpm-workspace.yaml`` or ``package.json``.

    Checks the top-level ``catalog:`` mapping in ``pnpm-workspace.yaml`` first (matching
    upstream T3 Code's pnpm catalog layout), then falls back to root ``package.json``
    dependency fields (ignoring ``catalog:``/``workspace:`` specifiers). Returns a normalized
    SemVer string such as ``"1.8.0"``, or ``None`` when no concrete version is declared.
    """
    source_path = Path(source_dir)
    workspace_yaml = source_path / "pnpm-workspace.yaml"
    if workspace_yaml.is_file():
        try:
            lines = workspace_yaml.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        in_catalog = False
        catalog_indent: int | None = None
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" \t"))
            if indent == 0:
                in_catalog = bool(_YAML_CATALOG_HEADER_RE.match(stripped))
                catalog_indent = None
                continue
            if not in_catalog:
                continue
            if catalog_indent is None:
                catalog_indent = indent
            if indent != catalog_indent:
                continue
            entry_match = _YAML_VP_ENTRY_RE.match(stripped)
            if entry_match is None:
                continue
            scalar = _extract_yaml_scalar(entry_match.group(1))
            ver_match = _VP_VERSION_RE.match(scalar)
            if ver_match is not None:
                return ver_match.group(1)

    package_json = source_path / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for section_name in ("devDependencies", "dependencies", "optionalDependencies"):
                section = data.get(section_name)
                if not isinstance(section, dict):
                    continue
                raw_spec = section.get("vite-plus")
                if not isinstance(raw_spec, str):
                    continue
                ver_match = _VP_VERSION_RE.match(raw_spec.strip())
                if ver_match is not None:
                    return ver_match.group(1)

    return None


_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _fetch_shallow_ref(dest_path: Path, repo_url: str, resolved_ref: str) -> None:
    """Shallow-fetch ``resolved_ref`` into ``dest_path``, preserving local tag refs for tags."""
    if not resolved_ref.startswith("refs/") and _FULL_SHA_RE.match(resolved_ref) is None:
        tag_refspec = f"refs/tags/{resolved_ref}:refs/tags/{resolved_ref}"
        try:
            _run_git(
                ["fetch", "--depth", "1", "--no-tags", "--force", repo_url, tag_refspec],
                cwd=dest_path,
            )
            return
        except RuntimeError:
            pass

    _run_git(
        ["fetch", "--depth", "1", "--no-tags", "--force", repo_url, resolved_ref],
        cwd=dest_path,
    )


def fetch_source(
    dest: Path,
    ref: str = DEFAULT_REF,
    repo_url: str = DEFAULT_REPO_URL,
) -> dict[str, str]:
    """Shallow-clone or shallow-fetch ``repo_url`` at ``ref`` into ``dest``.

    Supports ``ref == "latest-release"`` (GitHub Releases API) and
    ``ref == "latest-stable"`` (git ls-remote SemVer tags).
    Returns ``{"ref", "resolved_ref", "commit", "version"}``.
    """
    dest_path = Path(dest).expanduser().resolve()
    if ref == "latest-release":
        resolved_ref = resolve_latest_github_release(repo_url)["tag"]
    elif ref == "latest-stable":
        resolved_ref = resolve_latest_stable_tag(repo_url)
    else:
        resolved_ref = ref

    if (dest_path / ".git").exists():
        _fetch_shallow_ref(dest_path, repo_url, resolved_ref)
        _run_git(["checkout", "--force", "FETCH_HEAD"], cwd=dest_path)
        _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=dest_path)
    else:
        if dest_path.exists():
            if not dest_path.is_dir():
                raise FileExistsError(f"Destination exists and is not a directory: {dest_path}")
            if any(dest_path.iterdir()):
                raise FileExistsError(
                    f"Destination directory is non-empty and not a git repository: {dest_path}"
                )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    resolved_ref,
                    "--single-branch",
                    repo_url,
                    str(dest_path),
                ]
            )
        except RuntimeError:
            # Support raw commit SHAs or refs not accepted by `git clone --branch`.
            if dest_path.exists():
                shutil.rmtree(dest_path)
            dest_path.mkdir(parents=True, exist_ok=True)
            _run_git(["init"], cwd=dest_path)
            _run_git(["remote", "add", "origin", repo_url], cwd=dest_path)
            _fetch_shallow_ref(dest_path, repo_url, resolved_ref)
            _run_git(["checkout", "--force", "FETCH_HEAD"], cwd=dest_path)

    commit = _run_git(["rev-parse", "HEAD"], cwd=dest_path).stdout.strip()
    version = read_source_version(dest_path, ref=resolved_ref)
    return {
        "ref": ref,
        "resolved_ref": resolved_ref,
        "commit": commit,
        "version": version,
    }
