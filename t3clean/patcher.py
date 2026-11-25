"""Deterministic, idempotent de-telemetry source patcher for T3 Code.

Applies 6 targeted de-telemetry rules across upstream v0.1.11, v0.1.12, and main
while keeping model manifest and usage pricing services intact (rules 7 & 8).
"""

from dataclasses import dataclass
from pathlib import Path
import re

from . import PATCH_MARKER


class PatchAnchorNotFoundError(RuntimeError):
    """Raised when an expected upstream file or semantic patch anchor is missing."""


@dataclass(frozen=True)
class PatchOptions:
    """Configuration flags controlling optional patch rules and dry-run mode."""

    keep_updater: bool = False
    keep_cloud: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class FilePatchResult:
    """Outcome summary for a single source file processed by the patcher."""

    relative_path: str
    rule_id: str
    changed: bool
    already_patched: bool
    summary: str


_TS_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)

_TS_TYPE_PREFIX_KEYWORDS = frozenset(
    {
        "asserts",
        "extends",
        "infer",
        "is",
        "keyof",
        "new",
        "readonly",
        "typeof",
        "unique",
    }
)

_TS_OPEN_TO_CLOSE = {"{": "}", "(": ")", "[": "]", "<": ">"}
_TS_CLOSE_TO_OPEN = {"}": "{", ")": "(", "]": "[", ">": "<"}


def _ts_is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch in ("_", "$")


def _ts_is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch in ("_", "$")


def _ts_mask_non_code(source: str) -> str:
    """Return a same-length masked view of TypeScript source with non-code neutralised.

    - Preserves exact character offsets and newline positions.
    - Replaces ``//`` and ``/* ... */`` comments with spaces (preserving ``\\n``).
    - Replaces string and regex literal interiors with spaces inside ``"..."``
      so braces, parentheses, brackets, semicolons, and keywords inside literals
      cannot confuse downstream delimiter/statement scanning while literal
      tokens remain distinguishable from whitespace.
    - Replaces template literal text segments with spaces while preserving
      ``${ ... }`` interpolation code as balanced ``{ ... }`` blocks.
    """
    n = len(source)
    out = [" "] * n
    i = 0
    brace_depth = 0
    template_brace_stack: list[int] = []
    regex_allowed = True

    def _scan_template_text(start: int, opening_quote: bool) -> int:
        nonlocal brace_depth
        idx = start
        if opening_quote:
            out[idx] = "`"
            idx += 1
        while idx < n:
            ch = source[idx]
            if ch == "\\":
                if idx + 1 < n and source[idx + 1] == "\n":
                    out[idx + 1] = "\n"
                idx += 2
                continue
            if ch == "`":
                out[idx] = "`"
                template_brace_stack.pop()
                return idx + 1
            if ch == "$" and idx + 1 < n and source[idx + 1] == "{":
                out[idx + 1] = "{"
                brace_depth += 1
                return idx + 2
            if ch == "\n":
                out[idx] = "\n"
            idx += 1
        raise PatchAnchorNotFoundError("Unterminated template literal while scanning TypeScript source")

    while i < n:
        ch = source[i]

        if ch in " \t\r\f\v":
            out[i] = ch
            i += 1
            continue
        if ch == "\n":
            out[i] = "\n"
            i += 1
            continue

        # Single-line comment
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue

        # Block comment
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            closed = False
            while i < n:
                if source[i] == "*" and i + 1 < n and source[i + 1] == "/":
                    i += 2
                    closed = True
                    break
                if source[i] == "\n":
                    out[i] = "\n"
                i += 1
            if not closed:
                raise PatchAnchorNotFoundError("Unterminated block comment while scanning TypeScript source")
            continue

        # String literal
        if ch in ("'", '"'):
            quote = ch
            start = i
            out[start] = '"'
            i += 1
            closed = False
            while i < n:
                cur = source[i]
                if cur == "\\":
                    if i + 1 < n and source[i + 1] == "\n":
                        out[i + 1] = "\n"
                    i += 2
                    continue
                if cur == quote:
                    out[i] = '"'
                    i += 1
                    closed = True
                    break
                if cur == "\n":
                    out[i] = "\n"
                i += 1
            if not closed:
                raise PatchAnchorNotFoundError("Unterminated string literal while scanning TypeScript source")
            regex_allowed = False
            continue

        # Template literal
        if ch == "`":
            template_brace_stack.append(brace_depth)
            i = _scan_template_text(i, opening_quote=True)
            regex_allowed = True if template_brace_stack else False
            continue

        # Regex literal vs division operator
        if ch == "/" and regex_allowed:
            start = i
            out[start] = '"'
            i += 1
            in_char_class = False
            closed = False
            while i < n:
                cur = source[i]
                if cur == "\\":
                    i += 2
                    continue
                if cur == "\n":
                    break
                if cur == "[":
                    in_char_class = True
                elif cur == "]" and in_char_class:
                    in_char_class = False
                elif cur == "/" and not in_char_class:
                    i += 1
                    while i < n and source[i].isalpha():
                        i += 1
                    out[i - 1] = '"'
                    closed = True
                    break
                i += 1
            if not closed:
                raise PatchAnchorNotFoundError("Unterminated regex literal while scanning TypeScript source")
            regex_allowed = False
            continue

        # Identifiers / keywords
        if _ts_is_ident_start(ch):
            start = i
            i += 1
            while i < n and _ts_is_ident_part(source[i]):
                i += 1
            word = source[start:i]
            for pos in range(start, i):
                out[pos] = source[pos]
            regex_allowed = word in _TS_REGEX_PREFIX_KEYWORDS
            continue

        # Numeric literal
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in ("_", ".")):
                i += 1
            for pos in range(start, i):
                out[pos] = source[pos]
            regex_allowed = False
            continue

        # Braces (also tracks template interpolation exit)
        if ch == "{":
            out[i] = "{"
            brace_depth += 1
            regex_allowed = True
            i += 1
            continue

        if ch == "}":
            out[i] = "}"
            if template_brace_stack and brace_depth == template_brace_stack[-1] + 1:
                brace_depth -= 1
                i = _scan_template_text(i + 1, opening_quote=False)
                regex_allowed = True if template_brace_stack else False
                continue
            brace_depth = max(0, brace_depth - 1)
            regex_allowed = True
            i += 1
            continue

        # Postfix/prefix ++ and --
        if ch in ("+", "-") and i + 1 < n and source[i + 1] == ch:
            out[i] = ch
            out[i + 1] = ch
            regex_allowed = False
            i += 2
            continue

        out[i] = ch
        if ch in (")", "]"):
            regex_allowed = False
        else:
            regex_allowed = True
        i += 1

    return "".join(out)


def _ts_find_balanced_delim(masked: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Find the matching closing delimiter index for ``open_ch`` at ``open_idx``."""
    if open_idx >= len(masked) or masked[open_idx] != open_ch:
        raise PatchAnchorNotFoundError(
            f"Expected opening delimiter {open_ch!r} at offset {open_idx}"
        )
    depth = 0
    idx = open_idx
    n = len(masked)
    while idx < n:
        ch = masked[idx]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
        elif ch == '"' or ch == "`":
            quote = ch
            idx += 1
            while idx < n and masked[idx] != quote:
                idx += 1
        idx += 1
    raise PatchAnchorNotFoundError(
        f"Unbalanced delimiter {open_ch!r} starting at offset {open_idx}"
    )


def _ts_find_statement_end(masked: str, start_idx: int) -> int:
    """Find the index immediately after the terminating ``;`` at nesting depth 0."""
    stack: list[str] = []
    idx = start_idx
    n = len(masked)
    while idx < n:
        ch = masked[idx]
        if ch in ('"', "`"):
            quote = ch
            idx += 1
            while idx < n and masked[idx] != quote:
                idx += 1
        elif ch in ("{", "(", "["):
            stack.append(ch)
        elif ch in ("}", ")", "]"):
            if not stack or stack[-1] != _TS_CLOSE_TO_OPEN[ch]:
                raise PatchAnchorNotFoundError(
                    f"Unexpected closing delimiter {ch!r} at offset {idx}"
                )
            stack.pop()
        elif ch == ";" and not stack:
            return idx + 1
        idx += 1
    raise PatchAnchorNotFoundError(
        f"Missing terminating ';' for statement starting at offset {start_idx}"
    )


def _ts_find_assignment_equals(masked: str, start_idx: int) -> int:
    """Find the top-level ``=`` starting a variable initializer (skipping type annotations)."""
    stack: list[str] = []
    angle_depth = 0
    idx = start_idx
    n = len(masked)
    while idx < n:
        ch = masked[idx]
        if ch in ('"', "`"):
            quote = ch
            idx += 1
            while idx < n and masked[idx] != quote:
                idx += 1
        elif ch in ("{", "(", "["):
            stack.append(ch)
        elif ch in ("}", ")", "]"):
            if not stack or stack[-1] != _TS_CLOSE_TO_OPEN[ch]:
                raise PatchAnchorNotFoundError(
                    f"Unexpected closing delimiter {ch!r} in declaration header at offset {idx}"
                )
            stack.pop()
        elif ch == "=" and idx + 1 < n and masked[idx + 1] == ">":
            idx += 2
            continue
        elif ch == "<" and not stack:
            angle_depth += 1
        elif ch == ">" and not stack and angle_depth > 0:
            angle_depth -= 1
        elif ch == "=" and not stack and angle_depth == 0:
            prev_ch = masked[idx - 1] if idx > 0 else ""
            next_ch = masked[idx + 1] if idx + 1 < n else ""
            if prev_ch not in ("!", "=", "<", ">") and next_ch not in ("=", ">"):
                return idx
        elif ch == ";" and not stack and angle_depth == 0:
            break
        idx += 1
    raise PatchAnchorNotFoundError(
        f"Missing assignment '=' for declaration starting at offset {start_idx}"
    )


def _ts_skip_ws(masked: str, idx: int) -> int:
    n = len(masked)
    while idx < n and masked[idx].isspace():
        idx += 1
    return idx


def _ts_replace_const_declaration(
    source: str,
    header_pattern: re.Pattern[str],
    replacement: str,
    anchor_desc: str,
    remove_statement: bool = False,
) -> str:
    """Replace or remove a balanced ``const`` declaration statement using TS lexical scanning."""
    masked = _ts_mask_non_code(source)
    match = header_pattern.search(masked)
    if match is None:
        raise PatchAnchorNotFoundError(f"Missing anchor: {anchor_desc}")

    eq_idx = _ts_find_assignment_equals(masked, match.end())
    stmt_end = _ts_find_statement_end(masked, eq_idx + 1)

    if remove_statement:
        cut_end = stmt_end
        if cut_end < len(source) and source[cut_end] == "\r":
            cut_end += 1
        if cut_end < len(source) and source[cut_end] == "\n":
            cut_end += 1
        return source[: match.start()] + source[cut_end:]

    indent_match = re.match(r"[ \t]*", source[match.start() : match.end()])
    indent = indent_match.group(0) if indent_match is not None else ""
    indented = "\n".join(
        f"{indent}{line}" if line else "" for line in replacement.splitlines()
    )
    return source[: match.start()] + indented + source[stmt_end:]


def _ts_replace_function_body(
    source: str,
    func_name: str,
    new_body_lines: str,
    anchor_desc: str,
    fallback_return_type: str | None = None,
) -> str:
    """Replace the balanced ``{ ... }`` body of an exported function declaration."""
    masked = _ts_mask_non_code(source)
    header_re = re.compile(
        rf"^[ \t]*export\s+(?:async\s+)?function\s+{re.escape(func_name)}\b",
        re.MULTILINE,
    )
    match = header_re.search(masked)
    if match is None:
        raise PatchAnchorNotFoundError(f"Missing anchor: {anchor_desc}")

    n = len(masked)
    idx = _ts_skip_ws(masked, match.end())
    if idx < n and masked[idx] == "<":
        idx = _ts_find_balanced_delim(masked, idx, "<", ">") + 1
        idx = _ts_skip_ws(masked, idx)

    if idx >= n or masked[idx] != "(":
        raise PatchAnchorNotFoundError(
            f"Missing parameter list '(' for {anchor_desc}"
        )
    close_paren_idx = _ts_find_balanced_delim(masked, idx, "(", ")")
    after_params = close_paren_idx + 1
    idx = _ts_skip_ws(masked, after_params)

    has_return_type = False
    if idx < n and masked[idx] == ":":
        has_return_type = True
        idx += 1
        expecting_type = True
        while idx < n:
            idx = _ts_skip_ws(masked, idx)
            if idx >= n:
                break
            ch = masked[idx]
            if ch == "{":
                if not expecting_type:
                    break
                idx = _ts_find_balanced_delim(masked, idx, "{", "}") + 1
                expecting_type = False
                continue
            if ch in ("<", "(", "["):
                idx = _ts_find_balanced_delim(masked, idx, ch, _TS_OPEN_TO_CLOSE[ch]) + 1
                expecting_type = False
                continue
            if ch in ('"', "`"):
                quote = ch
                idx += 1
                while idx < n and masked[idx] != quote:
                    idx += 1
                idx += 1
                expecting_type = False
                continue
            if ch == "=" and idx + 1 < n and masked[idx + 1] == ">":
                idx += 2
                expecting_type = True
                continue
            if ch in ("|", "&", ".", "?"):
                idx += 1
                expecting_type = True
                continue
            if _ts_is_ident_start(ch):
                word_start = idx
                idx += 1
                while idx < n and _ts_is_ident_part(masked[idx]):
                    idx += 1
                word = masked[word_start:idx]
                expecting_type = word in _TS_TYPE_PREFIX_KEYWORDS
                continue
            if ch == ";":
                raise PatchAnchorNotFoundError(
                    f"Encountered ';' before function body for {anchor_desc}"
                )
            idx += 1
            expecting_type = False

    if idx >= n or masked[idx] != "{":
        raise PatchAnchorNotFoundError(
            f"Missing opening '{{' for function body of {anchor_desc}"
        )

    body_open_idx = idx
    body_close_idx = _ts_find_balanced_delim(masked, body_open_idx, "{", "}")
    formatted_body = "{\n" + "\n".join(f"  {line}" for line in new_body_lines.splitlines()) + "\n}"

    if fallback_return_type is not None and not has_return_type:
        return (
            source[:after_params]
            + f": {fallback_return_type} "
            + formatted_body
            + source[body_close_idx + 1 :]
        )
    return source[:body_open_idx] + formatted_body + source[body_close_idx + 1 :]


def _patch_is_already_patched(content: str) -> bool:
    return any(line.strip() == PATCH_MARKER for line in content.splitlines())


def _patch_prepend_marker(content: str) -> str:
    return f"{PATCH_MARKER}\n{content}"


def _patch_analytics_service(original: str) -> tuple[str, str]:
    """Rule 1: Replace AnalyticsService.ts with a zero-network Effect no-op stub."""
    required_anchors = (
        "export class AnalyticsService extends Context.Service",
        '"t3/telemetry/AnalyticsService"',
        "export const make",
        "export const layer",
    )
    for anchor in required_anchors:
        if anchor not in original:
            raise PatchAnchorNotFoundError(
                f"[analytics-service] apps/server/src/telemetry/AnalyticsService.ts: "
                f"missing anchor {anchor!r}"
            )

    masked = _ts_mask_non_code(original)
    returns_boolean = bool(
        re.search(
            r"readonly\s+record\s*:[\s\S]*?=>\s*Effect\.Effect<\s*boolean\s*>",
            masked,
        )
    )
    returns_void = bool(
        re.search(
            r"readonly\s+record\s*:[\s\S]*?=>\s*Effect\.Effect<\s*void\s*>",
            masked,
        )
    )

    if returns_boolean:
        patched = f"""{PATCH_MARKER}
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import type {{ TelemetryOwner }} from "./Identify.ts";

export class AnalyticsService extends Context.Service<
  AnalyticsService,
  {{
    /** Whether the event was queued to send; false when telemetry is off. */
    readonly record: (
      event: string,
      properties?: Readonly<Record<string, unknown>>,
      options?: {{ readonly anonymous?: boolean }},
    ) => Effect.Effect<boolean>;

    readonly flush: Effect.Effect<void>;

    readonly recordOwners: (
      owners: ReadonlyArray<TelemetryOwner>,
      observedBy: "server" | "client",
    ) => Effect.Effect<void>;
  }}
>()("t3/telemetry/AnalyticsService") {{}}

/** @public */
export const make = Effect.succeed(
  AnalyticsService.of({{
    record: () => Effect.succeed(false),
    flush: Effect.void,
    recordOwners: () => Effect.void,
  }}),
);

export const layer = Layer.effect(AnalyticsService, make);
"""
        return patched, "replaced AnalyticsService with boolean no-op stub"

    if returns_void:
        patched = f"""{PATCH_MARKER}
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import type {{ TelemetryOwner }} from "./Identify.ts";

export class AnalyticsService extends Context.Service<
  AnalyticsService,
  {{
    readonly record: (
      event: string,
      properties?: Readonly<Record<string, unknown>>,
    ) => Effect.Effect<void>;

    readonly flush: Effect.Effect<void>;

    readonly recordOwners: (
      owners: ReadonlyArray<TelemetryOwner>,
      observedBy: "server" | "client",
    ) => Effect.Effect<void>;
  }}
>()("t3/telemetry/AnalyticsService") {{}}

/** @public */
export const make = Effect.succeed(
  AnalyticsService.of({{
    record: () => Effect.void,
    flush: Effect.void,
    recordOwners: () => Effect.void,
  }}),
);

export const layer = Layer.effect(AnalyticsService, make);
"""
        return patched, "replaced AnalyticsService with legacy void no-op stub"

    raise PatchAnchorNotFoundError(
        "[analytics-service] apps/server/src/telemetry/AnalyticsService.ts: "
        "could not detect AnalyticsService.record return type (expected Effect.Effect<boolean> or Effect.Effect<void>)"
    )


def _patch_telemetry_identify(original: str) -> tuple[str, str]:
    """Rule 2: Replace Identify.ts with a dynamic version-aware zero-I/O stub."""
    required_anchors = (
        "export const TelemetryOwnerSource",
        "export interface TelemetryOwner",
        "export const getTelemetryIdentity",
        "export const getServerOwners",
    )
    for anchor in required_anchors:
        if anchor not in original:
            raise PatchAnchorNotFoundError(
                f"[telemetry-identify] apps/server/src/telemetry/Identify.ts: "
                f"missing anchor {anchor!r}"
            )

    has_legacy_owners = bool(
        re.search(
            r'export\s+const\s+TelemetryOwnerSource\s*=[^\n;]*["\']openai["\']',
            original,
        )
    )
    has_identity_source = bool(
        re.search(r"\bexport\s+const\s+TelemetryIdentitySource\b", original)
    )
    has_error_source_field = bool(
        re.search(
            r'TelemetryAnonymousIdGenerationError[\s\S]*?source:\s*Schema\.Literal\(["\']anonymous["\']\)',
            original,
        )
    )
    has_openai_user_id = bool(
        re.search(r"\bexport\s+(?:const|function)\s+openAiUserId\b", original)
    )
    has_identity_for_home = bool(
        re.search(r"\bexport\s+(?:const|function)\s+getTelemetryIdentityForHome\b", original)
    )
    has_owners_for_home = bool(
        re.search(r"\bexport\s+(?:const|function)\s+getServerOwnersForHome\b", original)
    )

    imports = [
        'import * as Effect from "effect/Effect";',
    ]
    if has_openai_user_id:
        imports.extend(
            [
                'import * as Base64Url from "effect/encoding/Base64Url";',
                'import * as Option from "effect/Option";',
                'import * as Result from "effect/Result";',
            ]
        )
    imports.append('import * as Schema from "effect/Schema";')

    owner_literals = (
        '["connect", "openai", "claude", "device"]'
        if has_legacy_owners
        else '["connect", "device"]'
    )

    sections: list[str] = [
        PATCH_MARKER,
        "\n".join(imports),
        f"""export const TelemetryOwnerSource = Schema.Literals({owner_literals});
export type TelemetryOwnerSource = typeof TelemetryOwnerSource.Type;

export interface TelemetryOwner {{
  readonly source: TelemetryOwnerSource;
  readonly ownerId: string;
}}""",
    ]

    if has_identity_source:
        sections.append(
            """export const TelemetryIdentitySource = Schema.Literals(["codex", "claude", "anonymous"]);
export type TelemetryIdentitySource = typeof TelemetryIdentitySource.Type;"""
        )

    error_source_line = (
        '    source: Schema.Literal("anonymous"),\n' if has_error_source_field else ""
    )
    sections.append(
        f"""export class TelemetryAnonymousIdGenerationError extends Schema.TaggedError<TelemetryAnonymousIdGenerationError>()(
  "TelemetryAnonymousIdGenerationError",
  {{
{error_source_line}    filePath: Schema.String,
    cause: Schema.Defect(),
  }},
) {{
  override get message(): string {{
    return `Failed to generate anonymous telemetry identity for '${{this.filePath}}'.`;
  }}
}}

export class TelemetryAnonymousIdPersistenceError extends Schema.TaggedError<TelemetryAnonymousIdPersistenceError>()(
  "TelemetryAnonymousIdPersistenceError",
  {{
{error_source_line}    filePath: Schema.String,
    cause: Schema.Defect(),
  }},
) {{
  override get message(): string {{
    return `Failed to persist anonymous telemetry identity at '${{this.filePath}}'.`;
  }}
}}"""
    )

    if has_openai_user_id:
        sections.append(
            """const IdTokenClaims = Schema.Struct({
  "https://api.openai.com/auth": Schema.Struct({ user_id: Schema.NonEmptyString }),
});
const decodeIdTokenClaims = Schema.decodeUnknownOption(Schema.fromJsonString(IdTokenClaims));

export const openAiUserId = (idToken: string): Option.Option<string> => {
  const payload = Base64Url.decodeString(idToken.split(".")[1] ?? "");
  return Result.isSuccess(payload)
    ? Option.map(
        decodeIdTokenClaims(payload.success),
        (claims) => claims["https://api.openai.com/auth"].user_id,
      )
    : Option.none<string>();
};"""
        )

    sections.append(
        """export const getTelemetryIdentity = Effect.succeed({
  distinctId: null as string | null,
  installId: null as string | null,
});"""
    )

    if has_identity_for_home:
        sections.append(
            "export const getTelemetryIdentityForHome = (_homeDirectory: string) => getTelemetryIdentity;"
        )

    sections.append(
        "export const getServerOwners = Effect.succeed([] as ReadonlyArray<TelemetryOwner>);"
    )

    if has_owners_for_home:
        sections.append(
            "export const getServerOwnersForHome = (_homeDirectory: string) => getServerOwners;"
        )

    patched = "\n\n".join(sections) + "\n"
    variant = "legacy v0.1.11-compatible" if (has_openai_user_id or has_identity_source) else "modern"
    return patched, f"replaced Identify.ts with {variant} zero-I/O stub"


def _patch_web_connection_platform(original: str) -> tuple[str, str]:
    """Rule 3a: Strip browser analytics device ID persistence from platform.ts."""
    updated = _ts_replace_const_declaration(
        original,
        re.compile(r"^[ \t]*const\s+ANALYTICS_DEVICE_ID_KEY\b", re.MULTILINE),
        replacement="",
        anchor_desc="[client-device-id] apps/web/src/connection/platform.ts: const ANALYTICS_DEVICE_ID_KEY",
        remove_statement=True,
    )
    updated = _ts_replace_const_declaration(
        updated,
        re.compile(r"^[ \t]*const\s+analyticsDevice\b", re.MULTILINE),
        replacement="const analyticsDevice = { id: Effect.succeed(Option.none<string>()) };",
        anchor_desc="[client-device-id] apps/web/src/connection/platform.ts: const analyticsDevice",
    )
    if "t3code:analytics-device-id" in updated:
        raise PatchAnchorNotFoundError(
            "[client-device-id] apps/web/src/connection/platform.ts: "
            "residual t3code:analytics-device-id reference remained after patch"
        )
    return _patch_prepend_marker(updated), "stripped browser analytics device ID"


def _patch_client_runtime_remote(original: str) -> tuple[str, str]:
    """Rule 3b: Stub readClientOwnerParams in client-runtime remote.ts."""
    updated = _ts_replace_const_declaration(
        original,
        re.compile(r"^[ \t]*export\s+const\s+readClientOwnerParams\b", re.MULTILINE),
        replacement="export const readClientOwnerParams: Effect.Effect<ClientOwnerParams> = Effect.succeed({});",
        anchor_desc="[client-device-id] packages/client-runtime/src/authorization/remote.ts: export const readClientOwnerParams",
    )
    return _patch_prepend_marker(updated), "stubbed readClientOwnerParams to empty object"


def _patch_relay_tracing(original: str) -> tuple[str, str]:
    """Rule 4: Replace relayTracing.ts with a pass-through zero-OTLP stub."""
    required_anchors = (
        "export interface RelayClientTracingConfig",
        "export interface RelayClientTracingResource",
        "export class RelayClientTracer",
        "export const withRelayClientTracing",
        "export const withLocalTracing",
        "export function nonInterferingTracer",
        "export function layer",
    )
    for anchor in required_anchors:
        if anchor not in original:
            raise PatchAnchorNotFoundError(
                f"[relay-tracing] packages/shared/src/relayTracing.ts: missing anchor {anchor!r}"
            )

    patched = f"""{PATCH_MARKER}
import * as Context from "effect/Context";
import type * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import type * as Tracer from "effect/Tracer";
import type {{ HttpClient }} from "effect/http";

export interface RelayClientTracingConfig {{
  readonly tracesUrl: string;
  readonly tracesDataset: string;
  readonly tracesToken: string;
}}

export interface RelayClientTracingResource {{
  readonly serviceName: string;
  readonly serviceVersion?: string;
  readonly runtime: string;
  readonly serviceInstanceId: string;
  readonly client: string;
  readonly component?: string;
  readonly attributes?: Readonly<Record<string, string>> | undefined;
}}

export class RelayClientTracer extends Context.Reference(
  "@t3tools/shared/relayTracing/RelayClientTracer",
  {{
    defaultValue: () => Option.none<Tracer.Tracer>(),
  }},
) {{}}

export const withRelayClientTracing = <A, E, R>(
  effect: Effect.Effect<A, E, R>,
): Effect.Effect<A, E, R> => effect;

export const withLocalTracing = <A, E, R>(
  effect: Effect.Effect<A, E, R>,
): Effect.Effect<A, E, R> => effect;

export function nonInterferingTracer(delegate: Tracer.Tracer): Tracer.Tracer {{
  return delegate;
}}

export function layer(
  _config: RelayClientTracingConfig | null,
  _resource: RelayClientTracingResource,
  _options?: {{ readonly isEnabled?: () => boolean }},
): Layer.Layer<never, never, HttpClient.HttpClient> {{
  return Layer.succeed(RelayClientTracer, Option.none());
}}
"""
    return patched, "replaced relayTracing.ts with zero-OTLP pass-through stub"


def _patch_desktop_update_policy(original: str) -> tuple[str, str]:
    """Rule 5: Set unmanagedUpdatePolicy mode to off with allowNightly disabled."""
    replacement = """export const unmanagedUpdatePolicy: UpdatePolicy = {
  mode: "off",
  feedUrl: undefined,
  allowNightly: false,
  message: null,
};"""
    updated = _ts_replace_const_declaration(
        original,
        re.compile(r"^[ \t]*export\s+const\s+unmanagedUpdatePolicy\b", re.MULTILINE),
        replacement=replacement,
        anchor_desc="[desktop-update-policy] apps/desktop/src/updates/DesktopUpdatePolicy.ts: export const unmanagedUpdatePolicy",
    )
    return _patch_prepend_marker(updated), "disabled unmanaged desktop auto-updates"


_RELAY_TRACING_RETURN_TYPE = (
    "{\n"
    "  readonly tracesUrl: string;\n"
    "  readonly tracesDataset: string;\n"
    "  readonly tracesToken: string;\n"
    "} | null"
)


def _patch_web_cloud_public_config(original: str) -> tuple[str, str]:
    """Rule 6a: Hard-lock web cloud and relay tracing public config to disabled."""
    updated = _ts_replace_function_body(
        original,
        func_name="hasCloudPublicConfig",
        new_body_lines="return false;",
        anchor_desc="[cloud-public-config] apps/web/src/cloud/publicConfig.ts: export function hasCloudPublicConfig",
    )
    updated = _ts_replace_function_body(
        updated,
        func_name="resolveRelayTracingConfig",
        new_body_lines="return null;",
        anchor_desc="[cloud-public-config] apps/web/src/cloud/publicConfig.ts: export function resolveRelayTracingConfig",
        fallback_return_type=_RELAY_TRACING_RETURN_TYPE,
    )
    return _patch_prepend_marker(updated), "hard-locked web cloud and relay tracing config off"


def _patch_server_cloud_public_config(original: str) -> tuple[str, str]:
    """Rule 6b: Hard-lock server cloud and relay tracing public config to disabled."""
    updated = _ts_replace_const_declaration(
        original,
        re.compile(r"^[ \t]*export\s+const\s+hasCloudPublicConfig\b", re.MULTILINE),
        replacement="export const hasCloudPublicConfig: boolean = false;",
        anchor_desc="[cloud-public-config] apps/server/src/cloud/publicConfig.ts: export const hasCloudPublicConfig",
    )
    updated = _ts_replace_function_body(
        updated,
        func_name="resolveRelayClientTracingConfig",
        new_body_lines="return null;",
        anchor_desc="[cloud-public-config] apps/server/src/cloud/publicConfig.ts: export function resolveRelayClientTracingConfig",
        fallback_return_type=_RELAY_TRACING_RETURN_TYPE,
    )
    return _patch_prepend_marker(updated), "hard-locked server cloud and relay tracing config off"


def patch_source_tree(
    source_dir: Path,
    options: PatchOptions | None = None,
) -> list[FilePatchResult]:
    """Apply all enabled de-telemetry patch rules to an upstream T3 Code checkout.

    The operation is strictly idempotent using ``PATCH_MARKER`` and fails fast
    with ``PatchAnchorNotFoundError`` if any required target file or semantic
    anchor is missing or structurally invalid.
    """
    opts = options if options is not None else PatchOptions()
    root = Path(source_dir)

    plan: list[tuple[str, str, object]] = [
        (
            "analytics-service",
            "apps/server/src/telemetry/AnalyticsService.ts",
            _patch_analytics_service,
        ),
        (
            "telemetry-identify",
            "apps/server/src/telemetry/Identify.ts",
            _patch_telemetry_identify,
        ),
        (
            "client-device-id",
            "apps/web/src/connection/platform.ts",
            _patch_web_connection_platform,
        ),
        (
            "client-device-id",
            "packages/client-runtime/src/authorization/remote.ts",
            _patch_client_runtime_remote,
        ),
        (
            "relay-tracing",
            "packages/shared/src/relayTracing.ts",
            _patch_relay_tracing,
        ),
    ]

    if not opts.keep_updater:
        plan.append(
            (
                "desktop-update-policy",
                "apps/desktop/src/updates/DesktopUpdatePolicy.ts",
                _patch_desktop_update_policy,
            )
        )

    if not opts.keep_cloud:
        plan.extend(
            [
                (
                    "cloud-public-config",
                    "apps/web/src/cloud/publicConfig.ts",
                    _patch_web_cloud_public_config,
                ),
                (
                    "cloud-public-config",
                    "apps/server/src/cloud/publicConfig.ts",
                    _patch_server_cloud_public_config,
                ),
            ]
        )

    results: list[FilePatchResult] = []
    staged_writes: list[tuple[Path, str]] = []

    for rule_id, rel_path, transform in plan:
        file_path = root / rel_path
        if not file_path.is_file():
            raise PatchAnchorNotFoundError(
                f"[{rule_id}] {rel_path}: target file not found in {root}"
            )

        original = file_path.read_text(encoding="utf-8")
        if _patch_is_already_patched(original):
            results.append(
                FilePatchResult(
                    relative_path=rel_path,
                    rule_id=rule_id,
                    changed=False,
                    already_patched=True,
                    summary="already patched",
                )
            )
            continue

        patched, summary = transform(original)  # type: ignore[operator]
        if not opts.dry_run:
            staged_writes.append((file_path, patched))
        else:
            summary = f"dry-run: {summary}"

        results.append(
            FilePatchResult(
                relative_path=rel_path,
                rule_id=rule_id,
                changed=True,
                already_patched=False,
                summary=summary,
            )
        )

    # Atomic-ish all-or-nothing validation: write only after every rule validated cleanly.
    for file_path, content in staged_writes:
        file_path.write_text(content, encoding="utf-8")

    return results
