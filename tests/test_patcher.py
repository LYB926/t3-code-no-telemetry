"""Unit tests for t3clean.patcher across v0.1.11, v0.1.12/main, and TS parser edge cases."""

from pathlib import Path
import tempfile
import unittest

from t3clean import DEFAULT_NODE_VERSION, DEFAULT_REF, DEFAULT_REPO_URL, PATCH_MARKER
from t3clean.patcher import (
    FilePatchResult,
    PatchAnchorNotFoundError,
    PatchOptions,
    _ts_mask_non_code,
    _ts_replace_const_declaration,
    _ts_replace_function_body,
    patch_source_tree,
)
from t3clean.verifier import VerifyOptions, verify_artifact


SAMPLE_ANALYTICS_V0112 = """\
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { getTelemetryIdentity, type TelemetryOwner } from "./Identify.ts";

const TelemetryEnvConfig = {
  posthogKey: "phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m",
  posthogHost: "https://ingest.t3.codes",
  keyEnv: "T3CODE_POSTHOG_KEY",
  hostEnv: "T3CODE_POSTHOG_HOST",
};

export class AnalyticsService extends Context.Service<
  AnalyticsService,
  {
    readonly record: (
      event: string,
      properties?: Readonly<Record<string, unknown>>,
      options?: { readonly anonymous?: boolean },
    ) => Effect.Effect<boolean>;

    readonly flush: Effect.Effect<void>;

    readonly recordOwners: (
      owners: ReadonlyArray<TelemetryOwner>,
      observedBy: "server" | "client",
    ) => Effect.Effect<void>;
  }
>()("t3/telemetry/AnalyticsService") {}

export const make = Effect.gen(function* () {
  return AnalyticsService.of({
    record: () => Effect.succeed(true),
    flush: Effect.void,
    recordOwners: () => Effect.void,
  });
});

export const layer = Layer.effect(AnalyticsService, make);
"""

SAMPLE_ANALYTICS_V0111 = """\
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { getTelemetryIdentity, type TelemetryOwner } from "./Identify.ts";

const posthogHost = "https://ingest.t3.codes";

export class AnalyticsService extends Context.Service<
  AnalyticsService,
  {
    readonly record: (
      event: string,
      properties?: Readonly<Record<string, unknown>>,
    ) => Effect.Effect<void>;

    readonly flush: Effect.Effect<void>;

    readonly recordOwners: (
      owners: ReadonlyArray<TelemetryOwner>,
      observedBy: "server" | "client",
    ) => Effect.Effect<void>;
  }
>()("t3/telemetry/AnalyticsService") {}

export const make = Effect.gen(function* () {
  return AnalyticsService.of({
    record: () => Effect.void,
    flush: Effect.void,
    recordOwners: () => Effect.void,
  });
});

export const layer = Layer.effect(AnalyticsService, make);
"""

SAMPLE_IDENTIFY_V0112 = """\
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";

export const TelemetryOwnerSource = Schema.Literals(["connect", "device"]);
export type TelemetryOwnerSource = typeof TelemetryOwnerSource.Type;

export interface TelemetryOwner {
  readonly source: TelemetryOwnerSource;
  readonly ownerId: string;
}

export class TelemetryAnonymousIdGenerationError extends Schema.TaggedError<TelemetryAnonymousIdGenerationError>()(
  "TelemetryAnonymousIdGenerationError",
  {
    filePath: Schema.String,
    cause: Schema.Defect(),
  },
) {
  override get message(): string {
    return `Failed ${this.filePath}`;
  }
}

export class TelemetryAnonymousIdPersistenceError extends Schema.TaggedError<TelemetryAnonymousIdPersistenceError>()(
  "TelemetryAnonymousIdPersistenceError",
  {
    filePath: Schema.String,
    cause: Schema.Defect(),
  },
) {
  override get message(): string {
    return `Failed ${this.filePath}`;
  }
}

const dummy = ServerConfig.ServerConfig;
const dummy2 = ServerSecretStore.ServerSecretStore;
const p = "anonymousIdPath";

export const getTelemetryIdentity = Effect.succeed({ distinctId: "abc", installId: "abc" });
export const getServerOwners = Effect.succeed([] as ReadonlyArray<TelemetryOwner>);
"""

SAMPLE_IDENTIFY_V0111 = """\
import * as Effect from "effect/Effect";
import * as Base64Url from "effect/encoding/Base64Url";
import * as Option from "effect/Option";
import * as Result from "effect/Result";
import * as Schema from "effect/Schema";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";

export const TelemetryOwnerSource = Schema.Literals(["connect", "openai", "claude", "device"]);
export type TelemetryOwnerSource = typeof TelemetryOwnerSource.Type;

export interface TelemetryOwner {
  readonly source: TelemetryOwnerSource;
  readonly ownerId: string;
}

export const TelemetryIdentitySource = Schema.Literals(["codex", "claude", "anonymous"]);
export type TelemetryIdentitySource = typeof TelemetryIdentitySource.Type;

export class TelemetryAnonymousIdGenerationError extends Schema.TaggedError<TelemetryAnonymousIdGenerationError>()(
  "TelemetryAnonymousIdGenerationError",
  {
    source: Schema.Literal("anonymous"),
    filePath: Schema.String,
    cause: Schema.Defect(),
  },
) {
  override get message(): string {
    return `Failed ${this.filePath}`;
  }
}

export class TelemetryAnonymousIdPersistenceError extends Schema.TaggedError<TelemetryAnonymousIdPersistenceError>()(
  "TelemetryAnonymousIdPersistenceError",
  {
    source: Schema.Literal("anonymous"),
    filePath: Schema.String,
    cause: Schema.Defect(),
  },
) {
  override get message(): string {
    return `Failed ${this.filePath}`;
  }
}

export const openAiUserId = (idToken: string) => Option.none<string>();

const files = [".codex", "auth.json", ".claude.json", "anonymousIdPath"];
const store = ServerSecretStore;

export const getTelemetryIdentityForHome = (homeDirectory: string) =>
  Effect.succeed({ distinctId: homeDirectory, installId: homeDirectory });

export const getTelemetryIdentity = getTelemetryIdentityForHome("/home/u");

export const getServerOwnersForHome = (homeDirectory: string) =>
  Effect.succeed([] as ReadonlyArray<TelemetryOwner>);

export const getServerOwners = getServerOwnersForHome("/home/u");
"""

SAMPLE_PLATFORM_TS = """\
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

// Fake comment: const ANALYTICS_DEVICE_ID_KEY = "not-real";
/* Block comment: const analyticsDevice = { id: "fake" }; */
const ANALYTICS_DEVICE_ID_KEY = "t3code:analytics-device-id";

const analyticsDevice = {
  id: Effect.sync(
    Option.liftThrowable(() => {
      const decoy = `brace ${ { inner: "};" }.inner } end`;
      const existing = window.localStorage.getItem(ANALYTICS_DEVICE_ID_KEY);
      if (existing) return existing;
      const created = `${decoy}:uuid`;
      window.localStorage.setItem(ANALYTICS_DEVICE_ID_KEY, created);
      return created;
    }),
  ),
};

export const keepMe = 42;
"""

SAMPLE_REMOTE_TS = """\
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

export interface ClientOwnerParams {
  readonly connect?: string;
  readonly device?: string;
}

export const readClientOwnerParams = Effect.gen(function* () {
  const session = yield* Effect.succeed(Option.none<string>());
  const obj = { nested: { text: "};" } };
  return {
    ...(Option.isSome(session) ? { connect: session.value } : {}),
    ...(obj.nested.text ? { device: "abc" } : {}),
  } satisfies ClientOwnerParams;
});

export const afterRemote = true;
"""

SAMPLE_RELAY_TRACING_TS = """\
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Tracer from "effect/Tracer";
import type { HttpClient } from "effect/http";
import { OtlpSerialization, OtlpTracer } from "effect/observability";

export interface RelayClientTracingConfig {
  readonly tracesUrl: string;
  readonly tracesDataset: string;
  readonly tracesToken: string;
}

export interface RelayClientTracingResource {
  readonly serviceName: string;
  readonly serviceVersion?: string;
  readonly runtime: string;
  readonly serviceInstanceId: string;
  readonly client: string;
  readonly component?: string;
  readonly attributes?: Readonly<Record<string, string>> | undefined;
}

export class RelayClientTracer extends Context.Reference(
  "@t3tools/shared/relayTracing/RelayClientTracer",
  { defaultValue: () => Option.none<Tracer.Tracer>() },
) {}

export const withRelayClientTracing = <A, E, R>(effect: Effect.Effect<A, E, R>) => effect;
export const withLocalTracing = <A, E, R>(effect: Effect.Effect<A, E, R>) => effect;
export function nonInterferingTracer(delegate: Tracer.Tracer): Tracer.Tracer {
  return delegate;
}
export function layer(
  config: RelayClientTracingConfig | null,
  resource: RelayClientTracingResource,
  options?: { readonly isEnabled?: () => boolean },
): Layer.Layer<never, never, HttpClient.HttpClient> {
  const headers = { "X-Axiom-Dataset": config?.tracesDataset ?? "" };
  void headers;
  void OtlpTracer;
  void OtlpSerialization;
  return Layer.succeed(RelayClientTracer, Option.none());
}
"""

SAMPLE_DESKTOP_UPDATE_POLICY_TS = """\
export interface UpdatePolicy {
  readonly mode: "auto" | "off";
  readonly feedUrl: string | undefined;
  readonly allowNightly: boolean;
  readonly message: string | null;
}

export const unmanagedUpdatePolicy: UpdatePolicy = {
  mode: "auto",
  feedUrl: undefined,
  allowNightly: true,
  message: null,
};

export const preserved = 1;
"""

SAMPLE_WEB_PUBLIC_CONFIG_TS = """\
export function resolveRelayTracingConfig() {
  const re = /^clerk\\.{1,3}[/}];/u;
  if (re.test("x")) {
    return null;
  }
  return {
    tracesUrl: "https://example.invalid",
    tracesDataset: "ds",
    tracesToken: "tok",
  };
}

export function hasCloudPublicConfig(): boolean {
  const domain = "clerk.example.com".replace(/^clerk\\./u, "");
  return domain.length > 0 && "pk_live_1234567890abcdef".startsWith("pk_live_");
}

export function keepHelper(): string {
  return "ok";
}
"""

SAMPLE_SERVER_PUBLIC_CONFIG_TS = """\
export function resolveRelayClientTracingConfig(
  env: Readonly<Record<string, string | undefined>> = process.env,
  fallback = { tracesUrl: "", tracesDataset: "", tracesToken: "" },
) {
  const ratio = 10 / 2;
  if (ratio > 0 && env.T3CODE_RELAY_CLIENT_OTLP_TRACES_URL) {
    return {
      tracesUrl: env.T3CODE_RELAY_CLIENT_OTLP_TRACES_URL,
      tracesDataset: fallback.tracesDataset,
      tracesToken: fallback.tracesToken,
    };
  }
  return null;
}

export const hasCloudPublicConfig = Boolean(
  (process.env.T3CODE_RELAY_URL ?? "") &&
  (process.env.T3CODE_CLERK_PUBLISHABLE_KEY?.trim() || "pk_live_1234567890abcdef") &&
  (process.env.T3CODE_CLERK_CLI_OAUTH_CLIENT_ID?.trim() || ""),
);
"""

SAMPLE_MODEL_MANIFEST_TS = """\
const MODEL_MANIFEST_URL = "https://raw.githubusercontent.com/pingdotgg/t3code/main/apps/server/src/provider/model-manifest.json";
export { MODEL_MANIFEST_URL };
"""

SAMPLE_USAGE_SERVICE_TS = """\
const LITELLM_RATES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json";
export { LITELLM_RATES_URL };
"""


def _write_mock_tree(root: Path, *, legacy_v0111: bool = False) -> None:
    files = {
        "apps/server/src/telemetry/AnalyticsService.ts": (
            SAMPLE_ANALYTICS_V0111 if legacy_v0111 else SAMPLE_ANALYTICS_V0112
        ),
        "apps/server/src/telemetry/Identify.ts": (
            SAMPLE_IDENTIFY_V0111 if legacy_v0111 else SAMPLE_IDENTIFY_V0112
        ),
        "apps/web/src/connection/platform.ts": SAMPLE_PLATFORM_TS,
        "packages/client-runtime/src/authorization/remote.ts": SAMPLE_REMOTE_TS,
        "packages/shared/src/relayTracing.ts": SAMPLE_RELAY_TRACING_TS,
        "apps/desktop/src/updates/DesktopUpdatePolicy.ts": SAMPLE_DESKTOP_UPDATE_POLICY_TS,
        "apps/web/src/cloud/publicConfig.ts": SAMPLE_WEB_PUBLIC_CONFIG_TS,
        "apps/server/src/cloud/publicConfig.ts": SAMPLE_SERVER_PUBLIC_CONFIG_TS,
        "apps/server/src/provider/ModelManifest.ts": SAMPLE_MODEL_MANIFEST_TS,
        "apps/server/src/usage/UsageService.ts": SAMPLE_USAGE_SERVICE_TS,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class TestPackageConstants(unittest.TestCase):
    def test_default_constants(self) -> None:
        self.assertEqual(DEFAULT_REPO_URL, "https://github.com/pingdotgg/t3code.git")
        self.assertEqual(DEFAULT_REF, "v0.1.12")
        self.assertEqual(DEFAULT_NODE_VERSION, "26.13.1")
        self.assertEqual(PATCH_MARKER, "// @t3-no-telemetry-patched")


class TestTypeScriptBalancedHelpers(unittest.TestCase):
    def test_mask_handles_strings_comments_regex_and_nested_templates(self) -> None:
        src = (
            'const a = "}; /* fake */";\n'
            "// const b = { };\n"
            "/* multi\nline } comment */\n"
            "const r = /^foo{1,2}[/}];/gi;\n"
            "const d = (10 / 2) / 5;\n"
            "const t = `outer ${ `inner ${ { x: '};' }.x }` } end`;\n"
        )
        masked = _ts_mask_non_code(src)
        self.assertEqual(len(masked), len(src))
        self.assertEqual(masked.count("\n"), src.count("\n"))
        self.assertNotIn("fake", masked)
        self.assertNotIn("const b", masked)
        self.assertNotIn("multi", masked)

    def test_replace_function_body_with_object_return_type_and_literal_union(self) -> None:
        src = (
            'export function complexReturn(\n'
            '  x: Readonly<Record<string, () => { k: string }>> = {},\n'
            '): { readonly a: string } | "none" | null {\n'
            '  const s = "}";\n'
            '  return { a: s };\n'
            '}\n'
            'export const trailing = 1;\n'
        )
        updated = _ts_replace_function_body(
            src,
            func_name="complexReturn",
            new_body_lines="return null;",
            anchor_desc="complexReturn",
        )
        self.assertIn(': { readonly a: string } | "none" | null {\n  return null;\n}', updated)
        self.assertIn("export const trailing = 1;", updated)

    def test_replace_const_declaration_skips_arrow_in_type_annotation(self) -> None:
        import re

        src = (
            "export const fn: (a: number) => { ok: boolean } = (a) => {\n"
            "  return { ok: a > 0 };\n"
            "};\n"
            "export const nextVal = 2;\n"
        )
        updated = _ts_replace_const_declaration(
            src,
            re.compile(r"^[ \t]*export\s+const\s+fn\b", re.MULTILINE),
            replacement="export const fn = () => ({ ok: false });",
            anchor_desc="fn",
        )
        self.assertIn("export const fn = () => ({ ok: false });", updated)
        self.assertIn("export const nextVal = 2;", updated)
        self.assertNotIn("a > 0", updated)

    def test_unterminated_brace_raises_anchor_error(self) -> None:
        src = "export function broken() {\n  return 1;\n"
        with self.assertRaises(PatchAnchorNotFoundError):
            _ts_replace_function_body(
                src,
                func_name="broken",
                new_body_lines="return 0;",
                anchor_desc="broken",
            )


class TestPatchSourceTree(unittest.TestCase):
    def test_patch_v0112_tree_idempotent_and_clean_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_mock_tree(root, legacy_v0111=False)

            # Before patching, verifier should flag telemetry leakage.
            pre_report = verify_artifact(root)
            self.assertFalse(pre_report.ok)

            results = patch_source_tree(root)
            self.assertEqual(len(results), 8)
            self.assertTrue(all(isinstance(r, FilePatchResult) for r in results))
            self.assertTrue(all(r.changed and not r.already_patched for r in results))

            # AnalyticsService (v0.1.12 boolean signature)
            analytics = (root / "apps/server/src/telemetry/AnalyticsService.ts").read_text(
                encoding="utf-8"
            )
            self.assertTrue(analytics.startswith(PATCH_MARKER))
            self.assertIn("Effect.Effect<boolean>", analytics)
            self.assertIn("options?: { readonly anonymous?: boolean }", analytics)
            self.assertIn("Effect.succeed(false)", analytics)
            self.assertNotIn("ingest.t3.codes", analytics)
            self.assertNotIn("phc_", analytics)

            # Identify (v0.1.12 modern exports, zero legacy symbols)
            identify = (root / "apps/server/src/telemetry/Identify.ts").read_text(encoding="utf-8")
            self.assertTrue(identify.startswith(PATCH_MARKER))
            self.assertIn('Schema.Literals(["connect", "device"])', identify)
            self.assertIn("export const getTelemetryIdentity", identify)
            self.assertIn("export const getServerOwners", identify)
            self.assertNotIn("openAiUserId", identify)
            self.assertNotIn("TelemetryIdentitySource", identify)
            self.assertNotIn("ServerSecretStore", identify)
            self.assertNotIn("anonymousIdPath", identify)

            # Client device ID stripped
            platform = (root / "apps/web/src/connection/platform.ts").read_text(encoding="utf-8")
            self.assertTrue(platform.startswith(PATCH_MARKER))
            self.assertNotIn("t3code:analytics-device-id", platform)
            self.assertIn(
                "const analyticsDevice = { id: Effect.succeed(Option.none<string>()) };",
                platform,
            )
            self.assertIn("export const keepMe = 42;", platform)

            remote = (root / "packages/client-runtime/src/authorization/remote.ts").read_text(
                encoding="utf-8"
            )
            self.assertTrue(remote.startswith(PATCH_MARKER))
            self.assertIn(
                "export const readClientOwnerParams: Effect.Effect<ClientOwnerParams> = Effect.succeed({});",
                remote,
            )
            self.assertIn("export const afterRemote = true;", remote)

            # Relay tracing stubbed
            relay = (root / "packages/shared/src/relayTracing.ts").read_text(encoding="utf-8")
            self.assertTrue(relay.startswith(PATCH_MARKER))
            self.assertNotIn("OtlpTracer", relay)
            self.assertNotIn("X-Axiom-Dataset", relay)
            self.assertIn("export class RelayClientTracer", relay)
            self.assertIn("export function nonInterferingTracer", relay)
            self.assertIn("export function layer", relay)

            # Desktop update policy disabled
            updater = (root / "apps/desktop/src/updates/DesktopUpdatePolicy.ts").read_text(
                encoding="utf-8"
            )
            self.assertIn('mode: "off"', updater)
            self.assertIn("allowNightly: false", updater)
            self.assertIn("export const preserved = 1;", updater)

            # Cloud/Clerk hard-locked
            web_cloud = (root / "apps/web/src/cloud/publicConfig.ts").read_text(encoding="utf-8")
            self.assertIn("export function hasCloudPublicConfig(): boolean {\n  return false;\n}", web_cloud)
            self.assertIn("return null;", web_cloud)
            self.assertIn("export function keepHelper(): string", web_cloud)

            srv_cloud = (root / "apps/server/src/cloud/publicConfig.ts").read_text(encoding="utf-8")
            self.assertIn("export const hasCloudPublicConfig: boolean = false;", srv_cloud)
            self.assertIn("return null;", srv_cloud)

            # Rules 7 & 8 MUST remain untouched!
            self.assertEqual(
                (root / "apps/server/src/provider/ModelManifest.ts").read_text(encoding="utf-8"),
                SAMPLE_MODEL_MANIFEST_TS,
            )
            self.assertEqual(
                (root / "apps/server/src/usage/UsageService.ts").read_text(encoding="utf-8"),
                SAMPLE_USAGE_SERVICE_TS,
            )

            # Post-patch artifact verification must pass cleanly (default non-strict).
            post_report = verify_artifact(root)
            self.assertTrue(post_report.ok, f"Unexpected findings: {post_report.findings}")

            # Strict offline mode still detects untouched rules 7 & 8 URLs.
            strict_report = verify_artifact(root, VerifyOptions(strict_offline=True))
            self.assertFalse(strict_report.ok)
            self.assertEqual(
                {f.rule for f in strict_report.findings},
                {"strict-offline-model-manifest", "strict-offline-litellm-rates"},
            )

            # Second patch run must be strictly idempotent.
            second_results = patch_source_tree(root)
            self.assertEqual(len(second_results), 8)
            self.assertTrue(all((not r.changed) and r.already_patched for r in second_results))

    def test_patch_v0111_tree_preserves_legacy_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_mock_tree(root, legacy_v0111=True)

            results = patch_source_tree(root)
            self.assertEqual(len(results), 8)
            self.assertTrue(all(r.changed for r in results))

            analytics = (root / "apps/server/src/telemetry/AnalyticsService.ts").read_text(
                encoding="utf-8"
            )
            self.assertIn("Effect.Effect<void>", analytics)
            self.assertIn("record: () => Effect.void", analytics)
            self.assertNotIn("Effect.Effect<boolean>", analytics)

            identify = (root / "apps/server/src/telemetry/Identify.ts").read_text(encoding="utf-8")
            self.assertIn('Schema.Literals(["connect", "openai", "claude", "device"])', identify)
            self.assertIn("export const TelemetryIdentitySource", identify)
            self.assertIn("export const openAiUserId", identify)
            self.assertIn("export const getTelemetryIdentityForHome", identify)
            self.assertIn("export const getServerOwnersForHome", identify)
            self.assertIn('source: Schema.Literal("anonymous")', identify)
            for forbidden in (".codex", "auth.json", ".claude.json", "anonymousIdPath", "ServerSecretStore"):
                self.assertNotIn(forbidden, identify)

            post_report = verify_artifact(root)
            self.assertTrue(post_report.ok, f"Unexpected findings: {post_report.findings}")

    def test_keep_updater_and_keep_cloud_skip_optional_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_mock_tree(root, legacy_v0111=False)

            results = patch_source_tree(
                root,
                PatchOptions(keep_updater=True, keep_cloud=True),
            )
            self.assertEqual(len(results), 5)
            self.assertEqual(
                (root / "apps/desktop/src/updates/DesktopUpdatePolicy.ts").read_text(encoding="utf-8"),
                SAMPLE_DESKTOP_UPDATE_POLICY_TS,
            )
            self.assertEqual(
                (root / "apps/web/src/cloud/publicConfig.ts").read_text(encoding="utf-8"),
                SAMPLE_WEB_PUBLIC_CONFIG_TS,
            )
            self.assertEqual(
                (root / "apps/server/src/cloud/publicConfig.ts").read_text(encoding="utf-8"),
                SAMPLE_SERVER_PUBLIC_CONFIG_TS,
            )

    def test_dry_run_validates_without_modifying_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_mock_tree(root, legacy_v0111=False)

            results = patch_source_tree(root, PatchOptions(dry_run=True))
            self.assertEqual(len(results), 8)
            self.assertTrue(all(r.changed and not r.already_patched for r in results))
            self.assertTrue(all(r.summary.startswith("dry-run:") for r in results))
            self.assertEqual(
                (root / "apps/server/src/telemetry/AnalyticsService.ts").read_text(encoding="utf-8"),
                SAMPLE_ANALYTICS_V0112,
            )

    def test_missing_file_or_anchor_fails_fast_without_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_mock_tree(root, legacy_v0111=False)
            # Break the last file's anchor; earlier files must NOT be partially written.
            (root / "apps/server/src/cloud/publicConfig.ts").write_text(
                "export const unrelated = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(PatchAnchorNotFoundError):
                patch_source_tree(root)

            # Because writes are staged until all anchors pass, AnalyticsService.ts stays untouched.
            self.assertEqual(
                (root / "apps/server/src/telemetry/AnalyticsService.ts").read_text(encoding="utf-8"),
                SAMPLE_ANALYTICS_V0112,
            )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PatchAnchorNotFoundError):
                patch_source_tree(Path(tmp))


if __name__ == "__main__":
    unittest.main()
