"""Post-patch and post-build telemetry artifact verifier for T3 Code.

Scans text files and binary Electron ASAR archives (via overlapping byte-chunk
streaming) for forbidden PostHog, Axiom, Clerk, analytics device-ID, and
optional updater or strict-offline network indicators.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class VerifyOptions:
    """Configuration flags for artifact telemetry verification."""

    keep_updater: bool = False
    strict_offline: bool = False


@dataclass(frozen=True)
class Finding:
    """A single forbidden telemetry or updater indicator discovered in an artifact."""

    path: str
    rule: str
    detail: str


@dataclass(frozen=True)
class VerifyReport:
    """Aggregated verification result across all scanned artifact files."""

    scanned_files: int
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        """Return True when zero forbidden indicators were detected."""
        return len(self.findings) == 0


_VERIFY_TEXT_EXTENSIONS = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".json",
        ".yml",
        ".yaml",
        ".html",
        ".txt",
    }
)

_VERIFY_SKIP_DIRS = frozenset({".git", ".repos", "__pycache__"})
_VERIFY_TEST_FILE_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.js",
    ".spec.js",
    ".test.mjs",
    ".test.cjs",
)

_VERIFY_CHUNK_SIZE = 1024 * 1024  # 1 MiB
_VERIFY_CHUNK_OVERLAP = 4096  # 4 KiB

_VERIFY_ALWAYS_LITERALS: tuple[tuple[str, str], ...] = (
    ("posthog-ingest-host", "ingest.t3.codes"),
    ("posthog-api-key", "phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m"),
    ("axiom-dataset-header", "X-Axiom-Dataset"),
    ("posthog-key-env", "T3CODE_POSTHOG_KEY"),
    ("posthog-host-env", "T3CODE_POSTHOG_HOST"),
    ("telemetry-key-env", "T3CODE_TELEMETRY_KEY"),
    ("telemetry-host-env", "T3CODE_TELEMETRY_HOST"),
    ("analytics-device-id", "t3code:analytics-device-id"),
)

_VERIFY_STRICT_OFFLINE_LITERALS: tuple[tuple[str, str], ...] = (
    ("strict-offline-model-manifest", "raw.githubusercontent.com/pingdotgg/t3code"),
    ("strict-offline-litellm-rates", "raw.githubusercontent.com/BerriAI/litellm"),
)

_VERIFY_CLERK_KEY_TEXT_RE = re.compile(r"pk_(?:live|test)_[A-Za-z0-9_-]{12,}")
_VERIFY_CLERK_KEY_BYTES_RE = re.compile(rb"pk_(?:live|test)_[A-Za-z0-9_-]{12,}")


def _verify_active_literals(options: VerifyOptions) -> tuple[tuple[str, str], ...]:
    if options.strict_offline:
        return _VERIFY_ALWAYS_LITERALS + _VERIFY_STRICT_OFFLINE_LITERALS
    return _VERIFY_ALWAYS_LITERALS


def _verify_truncate_snippet(value: str, limit: int = 80) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _scan_text_file(
    file_path: Path,
    display_path: str,
    literals: tuple[tuple[str, str], ...],
) -> list[Finding]:
    """Scan a UTF-8 text file for forbidden literals and Clerk publishable keys."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []

    for rule_name, literal in literals:
        first_idx = content.find(literal)
        if first_idx != -1:
            line_no = content.count("\n", 0, first_idx) + 1
            count = content.count(literal)
            findings.append(
                Finding(
                    path=display_path,
                    rule=rule_name,
                    detail=f"found {literal!r} at line {line_no} ({count} occurrence(s))",
                )
            )

    clerk_matches = list(_VERIFY_CLERK_KEY_TEXT_RE.finditer(content))
    if clerk_matches:
        first_match = clerk_matches[0]
        line_no = content.count("\n", 0, first_match.start()) + 1
        snippet = _verify_truncate_snippet(first_match.group(0))
        findings.append(
            Finding(
                path=display_path,
                rule="clerk-publishable-key",
                detail=(
                    f"found {snippet!r} at line {line_no} "
                    f"({len(clerk_matches)} occurrence(s))"
                ),
            )
        )

    return findings


def _scan_binary_file(
    file_path: Path,
    display_path: str,
    literals: tuple[tuple[str, str], ...],
    chunk_size: int = _VERIFY_CHUNK_SIZE,
    overlap: int = _VERIFY_CHUNK_OVERLAP,
) -> list[Finding]:
    """Stream a binary file (e.g. ``app.asar`` or SEA executable) in overlapping chunks."""
    byte_literals: tuple[tuple[str, bytes], ...] = tuple(
        (rule, literal.encode("utf-8")) for rule, literal in literals
    )
    # Track matched byte offsets per rule -> longest matched byte snippet at that offset.
    matched_by_rule: dict[str, dict[int, bytes]] = {}

    with file_path.open("rb") as handle:
        tail = b""
        file_offset = 0
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            window = tail + chunk
            window_base = file_offset - len(tail)

            for rule_name, needle in byte_literals:
                search_pos = 0
                while True:
                    found = window.find(needle, search_pos)
                    if found == -1:
                        break
                    abs_offset = window_base + found
                    rule_hits = matched_by_rule.setdefault(rule_name, {})
                    rule_hits[abs_offset] = needle
                    search_pos = found + 1

            for match in _VERIFY_CLERK_KEY_BYTES_RE.finditer(window):
                abs_offset = window_base + match.start()
                token = match.group(0)
                rule_hits = matched_by_rule.setdefault("clerk-publishable-key", {})
                previous = rule_hits.get(abs_offset)
                if previous is None or len(token) > len(previous):
                    rule_hits[abs_offset] = token

            file_offset += len(chunk)
            tail = window[-overlap:] if overlap > 0 else b""

    findings: list[Finding] = []
    ordered_rules = [rule for rule, _ in literals] + ["clerk-publishable-key"]
    for rule_name in ordered_rules:
        hits = matched_by_rule.get(rule_name)
        if not hits:
            continue
        first_offset = min(hits)
        snippet = _verify_truncate_snippet(hits[first_offset].decode("utf-8", errors="replace"))
        findings.append(
            Finding(
                path=display_path,
                rule=rule_name,
                detail=(
                    f"found {snippet!r} at byte offset {first_offset} "
                    f"({len(hits)} occurrence(s))"
                ),
            )
        )

    return findings


def _verify_is_test_source(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in _VERIFY_TEST_FILE_SUFFIXES)


def verify_artifact(
    target_path: Path,
    options: VerifyOptions | None = None,
) -> VerifyReport:
    """Recursively inspect a built artifact or patched path for telemetry leakage.

    Accepts either a single file (text, ``.asar``, or standalone binary) or a
    directory tree. Returns a ``VerifyReport`` with ``ok == True`` when zero
    forbidden indicators are found.
    """
    opts = options if options is not None else VerifyOptions()
    root = Path(target_path)
    if not root.exists():
        raise FileNotFoundError(f"Artifact target does not exist: {root}")

    literals = _verify_active_literals(opts)
    findings: list[Finding] = []
    scanned_files = 0

    if root.is_file():
        display = root.name
        if not opts.keep_updater and root.name.lower() == "app-update.yml":
            findings.append(
                Finding(
                    path=display,
                    rule="app-update-yml",
                    detail="forbidden auto-updater metadata file app-update.yml present",
                )
            )
        scanned_files = 1
        if root.suffix.lower() in _VERIFY_TEXT_EXTENSIONS:
            findings.extend(_scan_text_file(root, display, literals))
        else:
            findings.extend(_scan_binary_file(root, display, literals))
        return VerifyReport(scanned_files=scanned_files, findings=findings)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _VERIFY_SKIP_DIRS)
        current_dir = Path(dirpath)
        for filename in sorted(filenames):
            file_path = current_dir / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue

            lower_name = filename.lower()
            if lower_name.endswith(".map"):
                continue

            rel_display = file_path.relative_to(root).as_posix()

            if not opts.keep_updater and lower_name == "app-update.yml":
                findings.append(
                    Finding(
                        path=rel_display,
                        rule="app-update-yml",
                        detail="forbidden auto-updater metadata file app-update.yml present",
                    )
                )

            if _verify_is_test_source(filename):
                continue

            suffix = file_path.suffix.lower()
            if suffix in _VERIFY_TEXT_EXTENSIONS:
                scanned_files += 1
                findings.extend(_scan_text_file(file_path, rel_display, literals))
            elif suffix == ".asar":
                scanned_files += 1
                findings.extend(_scan_binary_file(file_path, rel_display, literals))

    return VerifyReport(scanned_files=scanned_files, findings=findings)
