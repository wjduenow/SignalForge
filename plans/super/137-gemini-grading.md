# Super Plan — #137: Gemini model support for grading

## Meta

- **Ticket:** https://github.com/wjduenow/SignalForge/issues/137
- **Parent epic:** #134 (pluggable LLM provider for grading — OpenAI/Gemini). Milestone v0.3.
- **Depends on:**
  - **#135** (provider-neutral LLM seam) — **merged** (`32b298f`, PR #148 → dev).
  - **#136** (OpenAI grading) — plan landed as PR #152, awaiting approval. #137 **sequences after #136**: #136 ships the `LLMProvider.estimate_input_tokens` ABC extension, the `pricing.py` per-provider SKU pattern, and the `--estimate` strategy refactor (with an Anthropic byte-identity snapshot as the floor). #137 piggybacks on that shape with a Gemini-native implementation (DEC-019).
- **Phase:** devolved (PR #151 → dev)
- **Branch:** `feature/137-gemini-grading`
- **Worktree:** `../worktrees/SignalForge/137-gemini-grading`

## Beads manifest

- **Epic:** `bd_1-scaffolding-txe`
- **Tasks** (linear chain except where noted parallel-safe; all P2):
  - `.1` US-001 — `_gemini_client.py` shim + new AST confinement scan — **READY**
  - `.2` US-002 — `GeminiProvider(LLMProvider)` + registration (incl. `response_mime_type="application/json"`) — blocked by `.1`
  - `.3` US-003 — `pyproject.toml` `[gemini]` extra + dev-group sync — blocked by `.1`
  - `.4` US-004 — `FakeGeminiClient` + offline provider unit tests — blocked by `.2`
  - `.5` US-005 — Provider-neutrality end-to-end tests (draft + grade, fake-driven) — blocked by `.4`
  - `.6` US-006 — Gemini pricing SKUs in `pricing.py` — **READY** (parallel-safe; no deps)
  - `.7` US-007 — `GeminiProvider.estimate_input_tokens` + `--estimate` integration — blocked by `.2`, `.6`, **cross-epic on #136 landing first per DEC-019**
  - `.8` US-008 — Live tests (`@pytest.mark.gemini`, raw + draft + grade) + CONTRIBUTING update — blocked by `.2`, `.5`
  - `.9` US-009 — Operator-facing docs + CHANGELOG — blocked by `.2`
  - `.10` Quality Gate (code-review ×4 + CodeRabbit + canonical validate + `wheel_smoke` + `anthropic` + `gemini` markers) — blocked by `.1`–`.9`
  - `.11` Patterns & Memory **(orchestrator-only — edits `.claude/rules/`)** — blocked by `.10`
- **Sessions:** 3 (2026-05-27 — initial plan; 2026-05-27 — extension after #136 plan comparison; 2026-05-28 — devolve)

## What / Why

Add **Google Gemini** as a selectable LLM provider for both the grader and the drafter,
via `grade.provider: gemini` / `llm.provider: gemini`. Anthropic stays the default; no
existing draft/grade fixtures or snapshots move. v0.3 ships Gemini **without prompt
caching** — every call shipping the full system+rubric prompt — to keep the first cut
simple and the request shape uniform; explicit Gemini context caching is a follow-up.

This is the second concrete provider the #135 seam was designed for. The seam itself is
unchanged — the work is one new shim, one new provider class, an `extra=` entry, and
tests/docs. The plan deliberately mirrors #135's shape so #136 (OpenAI) can copy this
plan and substitute the vendor.

## Discovery findings

### The seam #137 plugs into (all in `src/signalforge/llm/`)

- `providers.py` — `LLMProvider` ABC + process-level registry (`register_provider` /
  `provider_for`); `AnthropicProvider` registered at module scope (line 362). Capability
  flags drive every Anthropic-specific branch in the orchestrator.
- `_anthropic_client.py` — the per-vendor shim pattern: `<Vendor>ClientProtocol`,
  `_make_<vendor>_client`, `_load_<vendor>_exception_classes`. **Every `# pyright: ignore`
  for the SDK is confined here** (DEC-012 of #5; renamed by #135 DEC-004). The convention
  in `.claude/rules/llm-drafter.md` is explicit: a new vendor gets `_<vendor>_client.py`.
- `client.py` `call_llm` — generic orchestrator. Capability-gated branches we'll rely on:
  `supports_prompt_caching=False` ⇒ no `cache_control` marker, no beta header, cache
  tokens reported as 0, no dual-zero WARNING (DEC-008 of #135). `supports_token_count=False`
  ⇒ skip the pre-send count gate entirely.
- `models.py::LLMResult` — `cache_*_input_tokens` default 0; the no-cache path is
  first-class on the result type.

### Config surface (already provider-aware via #135)

- `DraftConfig.provider: str = "anthropic"` (`src/signalforge/draft/config.py:113`) and
  `GradeConfig.provider: str = "anthropic"` (`src/signalforge/grade/config.py:127`). Both
  `@field_validator("provider")` call `provider_for(v)` and propagate `UnknownProviderError`
  raw (it's an `LLMError`, not `ValueError`/`TypeError`/`AssertionError`, so Pydantic
  doesn't wrap it). Registering `GeminiProvider` makes `provider: gemini` validate.
- `cache_ttl: Literal["5m","1h"]` stays on both configs — Anthropic-specific, ignored
  when `supports_prompt_caching=False` (DEC-009 of #135). No churn there.

### Test fakes + the no-cache neutrality proof (DEC-011 of #135)

- `tests/llm/_fake.py::FakeAnthropicClient` — the `expect_*` API to mirror.
- `tests/llm/_fake_provider.py::FakeNoCacheProvider` — already proves the seam handles
  `False/False` capability flags through `grade_artifacts` end-to-end (audit JSONL,
  sidecar, drift detectors, blake2b-8 reproducibility hashes intact). The Gemini provider
  reuses this proven path; #137's neutrality test is `FakeGeminiClient`-driven (not
  `FakeNoCacheProvider`-driven) to exercise the real Gemini request shape + safety-filter
  branch.
- `tests/grade/test_provider_neutrality.py` — the test pattern to mirror for the new
  Gemini neutrality tests.

### SDK choice — `google-genai` (the actively-maintained one)

The ticket flags this: prefer `google-genai` (new unified SDK, supersedes
`google-generativeai`). Module surface: `from google import genai; client = genai.Client(api_key=...)`;
calls via `client.models.generate_content(model=..., contents=..., config=...)`;
exceptions in `google.genai.errors` (`APIError` / `ClientError` / `ServerError`).
Safety-filter responses surface as a candidate with `finish_reason` ∈ {`SAFETY`,
`RECITATION`, `OTHER`, …} and no `text` parts — the shim must detect this and route to
a typed `LLMError` (DEC-005 below).

### `pricing.py` + `cli/_estimate.py` — the seam #136 generalises, #137 piggybacks on

- `src/signalforge/llm/pricing.py` ships three Anthropic SKUs today
  (`claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`); `PRICE_TABLE_VERSION =
  "2026-05-11"`. `lookup(model)` raises `EstimateUnknownModelError` for any non-Anthropic
  id, so `--estimate` is silently un-usable for `grade.provider: gemini` until SKUs land.
- `src/signalforge/cli/_estimate.py` is **Anthropic-coupled**: it threads a single
  `anthropic_client: AnthropicClientProtocol` through `_count_draft_tokens` (line 309)
  and the grader-side equivalent (line 348), both hard-calling
  `anthropic_client.messages.count_tokens(...)`.
- **#136 (DEC-003, DEC-013, US-005) generalises this** by adding
  `LLMProvider.estimate_input_tokens(model, text) -> int` to the ABC, threading the
  resolved provider strategy through `cli/_estimate.py`, and pinning an Anthropic
  byte-identity snapshot as the floor. OpenAI implements via `tiktoken` (local BPE).
- **#137 piggybacks** on that ABC extension with a Gemini-native implementation via
  `client.models.count_tokens(model=, contents=)` — the google-genai SDK exposes a real
  count_tokens method, so no `tiktoken`-equivalent local-tokeniser dep is needed (the
  shim wraps the SDK call; cost is one extra API round-trip per estimate call, identical
  in shape to the Anthropic path). See DEC-016 + US-007.

### Snowflake `[snowflake]` extra — the pattern to mirror (`pyproject.toml`)

`snowflake-connector-python>=3,<4` appears in **both** `[project.optional-dependencies].snowflake`
(operator install: `pip install signalforge-dbt[snowflake]`) **and** `[dependency-groups].dev`
(so offline tests can construct real `snowflake.connector.errors.*` instances for the
exception mapper without needing a live warehouse). The same dual-listing is required for
Gemini — see `warehouse-adapters.md` § "Snowflake test harness" `_sfe()` lazy-import
gotcha (full-suite ordering deletes `snowflake.connector` from `sys.modules`; lazy import
inside each test).

### Already-neutral — do NOT touch

- Grade prompts (`<ARTIFACT>` envelope, rubric criterion list, blake2b-8 reproducibility
  hashes in `grade/prompts.py`) are provider-neutral by design (#7 DEC-008/010/019). The
  `<ARTIFACT>` envelope is the only prompt-injection defence for judge-prompt content and
  applies identically regardless of provider.
- `LLMResult` / `GradeEvent` / `LLMResponseEvent` shapes — already accommodate
  `cache_*_input_tokens = 0` via #135 DEC-009. No drift-detector or fixture moves.
- `tests/llm/test_prompt_cache_stability.py` — pins the Anthropic cached-block bytes;
  unaffected (Gemini takes a different code path through `call_llm`).
- `tests/test_audit_completeness.py` scans 1–7 — unchanged. Scan **8** (fail-closed
  writers) and the Anthropic-construction scan stay. We add a **9th** scan for
  `genai.Client(...)` confinement to `_gemini_client.py`.

## Scoping decisions (Phase 1 — answered)

- **Token counting:** `supports_token_count = False`. Skip the pre-send 8000-token cap
  gate. Simplest path; matches `FakeNoCacheProvider` precedent. The cap exists primarily
  to bound the Anthropic cache block — without caching, the marginal value doesn't
  justify a count-tokens round-trip per call. Documented deferral.
- **Provider/model coherence:** No validation. `GradeConfig.model` / `DraftConfig.model`
  stay free-form `str`. Model-name allowlists rot the moment Google ships a new family;
  the documented Gemini model id in ops docs is the soft guidance.
- **Safety-filter / blocked response:** Raise typed `LLMResponseFormatError` (an
  `LLMError`) naming the `finish_reason` in the message. `grade_artifacts` wraps to
  `GradeLLMError` and degrades the pair with `reasoning="call failed: GradeLLMError"`
  (DEC-015 of #7). Explicit, meaningful — not a JSON-parse failure masquerading as a
  response-shape bug.
- **Scope:** Cover **both** drafter (`llm.provider: gemini`) and grader
  (`grade.provider: gemini`). The provider is shared seam infrastructure — once
  registered, both paths use it automatically. Cost is one extra test file per side
  (mostly mechanical) for a measurable broadening of operator value.

## Architecture review (Phase 2)

| Area | Rating | Notes |
|---|---|---|
| **SDK confinement / supply-chain** | pass | `google-genai` import lazy + confined to `_gemini_client.py` (and `_load_gemini_exception_classes`); AST scan #9 enforces. Mirrors `_anthropic_client.py` exactly. |
| **Performance** | concern → accepted | No caching ⇒ every grade call ships full system+rubric prompt. For default 4 criteria × ~12 artifacts = ~48 sequential calls, this is the dominant cost. Documented as cost guidance in `docs/grade-ops.md`; explicit Gemini caching deferred. |
| **Capability degrade** | pass | Both flags `False`. Identical path to `FakeNoCacheProvider` which #135 already proves end-to-end. No new orchestrator branches. |
| **Safety filter / no-content** | pass | Detected in `extract_text_blocks`; routes via `LLMResponseFormatError` → `GradeLLMError` → degrade. Pinned by a dedicated test driving `FakeGeminiClient.expect_create(returns=<safety-blocked response>)`. |
| **Exception taxonomy** | pass | Five categories cover Gemini's `google.genai.errors` surface (auth via 401/403 on `ClientError`; 429 → RATE_LIMIT; 5xx via `ServerError` → SERVER_ERROR; connection-flavoured → CONNECTION; default NO_RETRY). Mapper unit-tested offline against genuine SDK exception instances. |
| **Config / registry validation** | pass | `provider="gemini"` validates the moment `register_provider(GeminiProvider())` runs at module import (`signalforge.llm.providers`). `UnknownProviderError` lists registered providers; no Pydantic wrap. |
| **Reproducibility hashes** | pass | `rubric_hash`, `prompt_version_template`, `criterion_prompt_hash`, `response_text_hash`, `args_hash` are provider-neutral. Cache-token fields default 0 — already round-tripped by drift detectors. |
| **Testing strategy** | pass | Hand-rolled `FakeGeminiClient` + `expect_*` for offline behaviour; offline exception-map tests use genuine `google.genai.errors.*` instances (SDK is a dev dep); `@pytest.mark.gemini` for live (gated by `SF_RUN_GEMINI=1` + `GOOGLE_API_KEY`). Live tests run `--no-cov`. |
| **Observability** | pass | No new logging beyond what `call_llm` already emits (and most of that is gated off by capability flags). Cleanup-boundary fail-soft N/A — no session state. |
| **`--estimate` integration** | concern → addressed | The ABC extension (`estimate_input_tokens`) ships in #136; #137 implements it via Gemini's native `client.models.count_tokens` and adds 3 Gemini SKUs to `pricing.py`. Documented as DEC-016/017 + US-006/007. **Sequencing: merge after #136** so the ABC + estimate refactor are in place (DEC-019). |
| **Server-side JSON enforcement** | concern → addressed | `extract_json_payload` (issue #144) already tolerates a prose preamble, but server-side enforcement eliminates the drift class entirely. `GeminiProvider.build_create_kwargs` sets `GenerateContentConfig(response_mime_type="application/json")`. Mirrors #136 DEC-006 (`response_format={"type":"json_object"}`). DEC-018. |
| **Pricing-table churn** | pass | `_PRICES_MUTABLE` gains 3 Gemini SKUs (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash`) with cache fields = 0.0; `PRICE_TABLE_VERSION` bumps. Additive; no Anthropic SKU moves. DEC-017. |
| **Wheel-build smoke** | pass | New `[gemini]` extra changes packaging metadata. QG runs `uv run pytest -m wheel_smoke --no-cov` per the maintainer marker convention (`python-build.md` § "wheel_smoke maintainer-gate"). |
| **Docs / 5-surface parity** | concern → addressed | Provider list mention in `docs/{draft,grade}-ops.md` ("today only `anthropic` is registered" → "`anthropic`, `openai`, `gemini`" once #136 lands first); cost-guidance bullet about no-caching; `docs/cost-estimate-ops.md` mention of the Gemini estimate path; CONTRIBUTING line for `uv run pytest -m gemini --no-cov`; README provider list; **CHANGELOG entry under `[Unreleased]`**. `.claude/rules/llm-drafter.md` + `grade-layer.md` updates handled by orchestrator-only Patterns & Memory story (Ralph workers can't write `.claude/`, per memory). |
| **Worker-writability** | pass | All shipped code + tests + `docs/` + `README` + `pyproject.toml` + `CHANGELOG.md` are worker-writable. The two `.claude/rules/` updates land in the orchestrator-handled P&M story. |

No blockers. One concern (performance/cost) accepted with explicit docs; one concern (5-surface parity) addressed by the story split.

## Refinement log (Phase 3 — decisions)

- **DEC-001 — Per-vendor shim confinement (mirrors DEC-012 of #5 / DEC-004 of #135).**
  `src/signalforge/llm/_gemini_client.py` is the sole module that imports
  `google.genai` / `google.genai.errors`. Exposes `GeminiClientProtocol`
  (`@runtime_checkable`, duck-typed at the surface `GeminiProvider` consumes),
  `_make_gemini_client(api_key=None) -> GeminiClientProtocol`, and
  `_load_gemini_exception_classes() -> _GeminiExceptionClasses` (lazy import in the
  function body — same shape as `_load_anthropic_exception_classes`). Every
  `# pyright: ignore[...]` and `# type: ignore[...]` for the Gemini SDK lives here.

- **DEC-002 — SDK choice: `google-genai`.** The newer unified SDK (`from google import
  genai`). The legacy `google-generativeai` is no longer actively maintained. Pinned
  loosely as `google-genai>=0.5,<1` in `[gemini]` and `dev` until v1 stabilises;
  bump bounds with each maintainer-driven SDK upgrade (mirrors the `snowflake-connector-python>=3,<4`
  pattern).

- **DEC-003 — Capability flags `False / False`.** `GeminiProvider.supports_prompt_caching
  = False` and `supports_token_count = False`. Both branches in `call_llm` degrade exactly
  as the `FakeNoCacheProvider` proves: no cache marker, no beta header, cache tokens
  reported as 0, no dual-zero WARNING, no pre-send count gate, no `LLMCacheTooLargeError`
  pre-send. `cache_marker_active` evaluates `False` regardless (both flags must be `True`
  per the QG lesson in #135). `build_count_tokens_kwargs` raises `NotImplementedError`
  with an explicit "unreachable when supports_token_count=False" message (matches
  `FakeNoCacheProvider`).

- **DEC-004 — Request shape: system_instruction + single user turn.** `build_create_kwargs`
  maps `system` → `config.system_instruction`; concatenates `cached_block + "\n\n" +
  dynamic_block` into one user-role `contents` entry. No cache control. Returned dict
  follows the SDK's `models.generate_content(model=, contents=, config=)` call shape (the
  shim's `GeminiClientProtocol.models.generate_content` consumes it).

  > The orchestrator passes the dict via `client.messages.create(**kwargs)` today — for
  > Gemini, the protocol surface `GeminiClientProtocol.messages.create` is the **shim's
  > façade** over `client.models.generate_content`. The shim adapts the call shape so
  > `call_llm` stays vendor-agnostic. See US-001 / US-002 for the precise façade.

- **DEC-005 — Safety-filter / no-content → `LLMResponseFormatError`.** `extract_text_blocks`
  inspects `response.candidates`. When no candidate yields a non-empty text part (blocked
  by safety filter, recitation, length, or any other non-`STOP` finish reason that
  produces no content), it raises
  `LLMResponseFormatError(f"Gemini response produced no text (finish_reason={fr!r}).")`.
  An `LLMError` subclass propagates out of `call_llm` (extraction runs AFTER the retry
  loop), so the grade engine wraps it as `GradeLLMError` and degrades the pair with
  `reasoning="call failed: GradeLLMError"`. The drafter path surfaces it directly to the
  CLI's exit-code tier 2.

- **DEC-006 — Exception → `ExceptionCategory` taxonomy.** Loaded lazily in the shim:
  - `google.genai.errors.ClientError` with `code == 401` or `403` → `AUTH`
  - `google.genai.errors.ClientError` with `code == 429` → `RATE_LIMIT`
  - `google.genai.errors.ServerError` (5xx family) → `SERVER_ERROR`
  - Connection-flavoured: `httpx.ConnectError` / `httpx.TimeoutException` (or the SDK's
    wrapped equivalent — verified against the real `google-genai` exception tree at
    implementation) → `CONNECTION`
  - Anything else → `NO_RETRY`

  Mirrors `AnthropicProvider.classify_exception`. The retry-budget knobs
  (`max_retries_429`, `max_retries_5xx`, `max_retries_conn`) on `GradeConfig`/`DraftConfig`
  apply unchanged.

- **DEC-007 — No provider/model coherence check.** `GradeConfig.model` and
  `DraftConfig.model` stay free-form `str`. Document the recommended Gemini model id in
  `docs/grade-ops.md` § Configuration and `docs/draft-ops.md` § Configuration. An
  operator setting `provider: gemini` + `model: claude-sonnet-4-6` fails at the first
  API call with a typed `LLMError` from the mapper — late, but not silently wrong.

- **DEC-008 — API-key resolution via `_make_gemini_client(api_key=None)`.**
  `genai.Client(api_key=api_key)`. When `api_key is None`, the SDK reads
  `GOOGLE_API_KEY` (or `GEMINI_API_KEY`, depending on SDK version — verified at
  implementation). Explicit `api_key=` overrides. No SignalForge-specific env var. Tests
  that need a real key set `GOOGLE_API_KEY=...` and gate behind `SF_RUN_GEMINI=1`.

- **DEC-009 — New AST confinement scan: `genai.Client(...)` only in `_gemini_client.py`.**
  Extend `tests/test_audit_completeness.py` with a `_QualifiedNameCallFinder` mirror
  matching `Call(func=Attribute(value=Name(id="genai"), attr="Client"))` (and the
  three bypass patterns from `testing-signal.md`: bare via `from google.genai import
  Client`, import-alias `from google.genai import Client as C`, attribute via
  `from google import genai; genai.Client(...)`). The 7th-AST-scan helper already
  generalises; reuse it. Sanity test asserts ≥1 construction in
  `_gemini_client.py`. **AST-scan tally is merge-order-dependent:** per DEC-019,
  #137 ships **after** #136, so #136's `openai.OpenAI(...)` scan bumps 8 → 9 first
  and #137's scan here bumps **9 → 10**. If the sequencing slips and #137 lands
  first, it owns the 8 → 9 bump and #136 inherits 9 → 10 — update the docstring
  count + the `_QualifiedNameCallFinder` test-discovery count to whichever number
  is current at merge time. (The docstring "AST scans" tally in `safety-layer.md`
  is the surface that needs updating; the scan-7 discovery count counts a different
  thing — per-stage `errors.py` modules — and is unaffected.)

- **DEC-010 — `pyproject.toml` `[gemini]` extra + dual dev-group listing.** `google-genai`
  appears in **three** places, in lockstep (Snowflake precedent):
  - `[project.optional-dependencies].gemini = ["google-genai>=0.5,<1"]` (operator
    install: `pip install signalforge-dbt[gemini]`).
  - `[project.optional-dependencies].dev` (pip back-compat for dev install).
  - `[dependency-groups].dev` (uv-native; CI uses this).

  `uv.lock` refreshes in the same commit. Default install stays Gemini-free — the base
  package depends only on `anthropic` (the default provider).

- **DEC-011 — `tests/llm/_fake_gemini.py::FakeGeminiClient` with `expect_*` API.**
  Mirrors `FakeAnthropicClient` shape: `expect_generate_content(matching, returns)`
  (and `expect_messages_create` as the shim-façade alias the orchestrator actually
  calls — the orchestrator hits `client.messages.create`; the shim adapts to
  `client.models.generate_content` under the hood, so the fake's `.messages.create`
  is the load-bearing entry point). Inspector property `create_calls` for assertion
  on `extra_headers` (must be absent: no cache beta) and `cache_control` (must not
  appear on any content block). `assert_all_expectations_met()` matches the
  precedent. Supports queuing exceptions for the retry-loop tests.

- **DEC-012 — `@pytest.mark.gemini` + `SF_RUN_GEMINI=1` + `GOOGLE_API_KEY` env-gate
  for live tests.** Marker registered in `pyproject.toml`'s
  `[tool.pytest.ini_options].markers`, added to the default `addopts` exclusion list
  (`-m 'not ... and not gemini'`). Belt-and-suspenders: `_skip_reason()` helper that
  surfaces a clear skip when env vars are missing under a maintainer `pytest -m gemini`
  run. Three live tests (mirrors Snowflake): `tests/llm/test_gemini_live.py` (raw
  `call_llm`), `tests/draft/test_gemini_draft_live.py` (drafter via `draft_schema`),
  `tests/grade/test_gemini_grade_live.py` (grader via `grade_artifacts`). Marker-runs
  use `--no-cov` (matches the `bigquery` / `cli_subprocess` / `wheel_smoke` /
  `snowflake` precedent in `testing-signal.md`).

- **DEC-013 — Cost guidance + 5-surface parity in docs.** Add a paragraph to
  `docs/grade-ops.md` § "Cost guidance" and the equivalent section in
  `docs/draft-ops.md`: "**Gemini (v0.3) ships without prompt caching.** Every call
  transmits the full system + rubric (grade) / system + cached-block (draft); there is
  no Anthropic-style discount on the cached prefix. For a default 4-criterion grade run
  over a 12-column model (~48 calls), budget accordingly. Explicit Gemini context
  caching is tracked as a follow-up." Update the "today only `anthropic` is registered"
  text in both ops docs to "today `anthropic` and `gemini` are registered." Update
  `README.md` provider list (if any) likewise.

- **DEC-014 — Both drafter and grader covered.** US-005 ships `FakeGeminiClient`-driven
  end-to-end tests for both `draft_schema` (via `tests/draft/test_gemini_neutrality.py`)
  and `grade_artifacts` (via `tests/grade/test_gemini_neutrality.py`). US-006 ships the
  corresponding live tests behind `@pytest.mark.gemini`. Drafter coverage is light by
  design — the drafter has only one LLM call per model; the value is proving the request
  shape + audit JSONL round-trip survive the shared seam, which the test does.

- **DEC-015 — `_GeminiExceptionClasses` empty-tuple fallback when SDK absent.**
  `_load_gemini_exception_classes()` returns a frozen dataclass with empty tuples for
  every category when `import google.genai` raises `ImportError` (exact mirror of
  `_load_anthropic_exception_classes`'s `pragma: no cover` branch). Lets
  `classify_exception` route every exception to `NO_RETRY` cleanly under a base install
  without the `[gemini]` extra — the operator just never gets `provider: gemini` to
  resolve a real call, but import-time behaviour is graceful.

- **DEC-016 — `GeminiProvider.estimate_input_tokens` via native `count_tokens`.**
  google-genai exposes `client.models.count_tokens(model=, contents=)` as a real,
  server-side token counter — no `tiktoken`-equivalent local-tokeniser dependency
  needed (cleaner than #136's OpenAI path, which leans on `tiktoken` because OpenAI
  has no first-party count endpoint on Chat Completions). The shim wraps the call;
  `GeminiProvider.estimate_input_tokens(model, text)` delegates via the shim. Cost:
  one extra API round-trip per `--estimate` call (identical in shape to Anthropic).
  **Depends on the ABC method shipping with #136** (DEC-019).

- **DEC-017 — 3 Gemini pricing SKUs + `PRICE_TABLE_VERSION` bump.** Add
  `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash` to `_PRICES_MUTABLE` in
  `src/signalforge/llm/pricing.py`. Each carries `input_per_mtok` + `output_per_mtok`
  per Google's public price page at PR-prep time; cache fields = 0.0 (no Anthropic-
  equivalent discount). Bump `PRICE_TABLE_VERSION` to the ship date. Mirrors #136
  DEC-007 (which adds 4 OpenAI SKUs in the same dict). Additive — no Anthropic SKU
  moves; Anthropic-config `--estimate` byte-identity unaffected.

- **DEC-018 — Server-side JSON enforcement via `response_mime_type="application/json"`.**
  `GeminiProvider.build_create_kwargs` constructs a `GenerateContentConfig` with
  `response_mime_type="application/json"` (and `system_instruction=system`). Belt-and-
  braces with the existing tolerant `extract_json_payload` parser (issue #144):
  server-side enforcement eliminates the prose-preamble drift class, the parser
  remains the fallback if a future model strips the flag. The grade system prompt
  already names "JSON" so any prompt-requirement check passes. Mirrors #136 DEC-006
  exactly — same defence, different vendor flag. Deliberately NOT setting
  `response_schema` for v0.3: the parser is the canonical structural gate, and a full
  Pydantic-derived schema adds surface for marginal benefit.

- **DEC-019 — Sequence after #136.** #137 merges **after** #136 so the
  `LLMProvider.estimate_input_tokens` ABC extension, the per-provider pricing pattern,
  and the `--estimate` strategy refactor are in place. The contingency is small:
  if #137 ships first, an extra story is needed to add the ABC extension + the
  Anthropic byte-identity snapshot + the OpenAI-less estimate refactor — work that
  fundamentally belongs to #136. Avoid by gating PR-merge order; document the
  dependency on the PR body and in the bead description.
  **If the sequencing slips:** raise a coordination bead in the parent epic (#134) to
  re-base #137 on top of merged-#136 rather than racing the rebases on `providers.py`
  / `pricing.py` / `cli/_estimate.py`.

## Story breakdown (Phase 4)

Each story includes its trace to DECs, acceptance criteria, "Done when," files, and TDD
notes. The canonical validation command is the project's:
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
(per CLAUDE.md). Live `@pytest.mark.gemini` runs are maintainer-only post-merge and not
part of the validation gate.

### US-001 — `_gemini_client.py` shim + AST confinement scan extension

**Traces to:** DEC-001, DEC-008, DEC-009, DEC-015.

**Description.** Create the per-vendor shim that confines every `google.genai` import +
SDK ignore. Add the 9th AST audit-completeness scan asserting `genai.Client(...)`
construction only happens here.

**Files.**
- `src/signalforge/llm/_gemini_client.py` (new): `GeminiClientProtocol` (with nested
  `_GeminiModelsProtocol` for `models.generate_content` and the shim's `messages.create`
  façade), `_make_gemini_client(api_key=None) -> GeminiClientProtocol`,
  `_load_gemini_exception_classes() -> _GeminiExceptionClasses`,
  `_GeminiExceptionClasses` frozen dataclass.
- `tests/test_audit_completeness.py` (edit): add 9th scan + the planted-violation
  regression test covering bare / import-alias / module-attribute bypass patterns
  (`testing-signal.md` § "AST single-construction-seam scans").
- `tests/llm/test_gemini_client_confinement.py` (new): asserts every
  `google.genai`-typed import and `# pyright: ignore` for the SDK lives only in
  `_gemini_client.py` (mirrors `tests/warehouse/test_snowflake_client_confinement.py`).

**TDD.** Write the planted-violation tests first; write the scan; assert it fires; write
the shim; assert it passes the confinement test.

**Acceptance criteria.**
- `from signalforge.llm._gemini_client import GeminiClientProtocol, _make_gemini_client`
  works in a fresh `uv sync --dev` env (SDK installed via `dev` group).
- `tests/test_audit_completeness.py` 9th scan rejects a planted
  `genai.Client(...)` in any module other than `_gemini_client.py`.
- `tests/llm/test_gemini_client_confinement.py` passes.
- Canonical validation command passes.

**Done when.** Shim file exists, AST scan fires on plant + passes on the real tree,
confinement test passes, no `google.genai` symbol appears in `git grep` outside
`_gemini_client.py` / `_load_gemini_exception_classes`.

### US-002 — `GeminiProvider(LLMProvider)` + registration

**Traces to:** DEC-001, DEC-003, DEC-004, DEC-005, DEC-006, DEC-013 (capability flags
drive docs wording), DEC-018 (server-side JSON enforcement).

**Description.** Implement the `LLMProvider` subclass and register it at module import.

**Files.**
- `src/signalforge/llm/providers.py` (edit): add `GeminiProvider` after
  `AnthropicProvider`; call `register_provider(GeminiProvider())` at module scope
  (line below the existing Anthropic registration). Implement all six abstract methods
  + the two capability-flag class attrs. `build_create_kwargs` constructs the call
  with `GenerateContentConfig(system_instruction=system, response_mime_type="application/json")`
  per DEC-004 + DEC-018; cached_block + dynamic_block concatenated into one
  user-role contents entry. No `cache_control` anywhere.
- `src/signalforge/llm/__init__.py` (edit): re-export `GeminiProvider` alongside
  `AnthropicProvider`.

**TDD.** Tests under US-004 drive the behaviour; this story implements to pass them.
Pure-logic methods (`build_create_kwargs`, `extract_text_blocks` including the
safety-filter branch, `extract_usage`, `classify_exception`) get unit tests in US-004.

**Acceptance criteria.**
- `signalforge.llm.providers.provider_for("gemini")` returns a `GeminiProvider`
  instance after `signalforge.llm` import.
- `provider.supports_prompt_caching is False` and `provider.supports_token_count is False`.
- `provider.build_count_tokens_kwargs(...)` raises `NotImplementedError` with the
  unreachable-when-supports_token_count=False message (matches `FakeNoCacheProvider`).
- `provider.build_create_kwargs(...)` returns a dict with no `cache_control` block
  anywhere and no `extra_headers` key (capability-gated, DEC-008 of #135 / DEC-003 here).
- The kwargs dict carries the JSON-mime config (asserted by inspecting the dict
  string-representation OR the `config` arg's `response_mime_type` field; DEC-018).
- Canonical validation command passes.

**Done when.** Provider registered, all abstract methods implemented, capability flags
both `False`, JSON-mime enforcement wired, `__init__` exports updated, US-004 tests green.

### US-003 — `pyproject.toml` `[gemini]` extra + dev-group sync

**Traces to:** DEC-010.

**Description.** Wire the optional dependency in lockstep across all three locations;
refresh the uv lock.

**Files.**
- `pyproject.toml` (edit): add `gemini = ["google-genai>=0.5,<1"]` under
  `[project.optional-dependencies]`; append `"google-genai>=0.5,<1"` to BOTH
  `[project.optional-dependencies].dev` and `[dependency-groups].dev`.
- `uv.lock` (regenerated): `uv lock` commits the resolution.

**TDD.** N/A (pure config). The validation gate `uv sync --dev` is the test.

**Acceptance criteria.**
- `pip install signalforge-dbt[gemini]` would resolve to `google-genai`. (Verified
  locally by `uv pip install --dry-run -e ".[gemini]"`.)
- `uv sync --dev` installs `google-genai`.
- `uv.lock` round-trip is clean (no spurious churn).
- Canonical validation command passes.

**Done when.** Three pyproject entries land, uv.lock refreshed, `uv sync --dev` succeeds
in a clean checkout.

### US-004 — `FakeGeminiClient` + offline provider unit tests

**Traces to:** DEC-005, DEC-006, DEC-011, DEC-015.

**Description.** Hand-rolled fake mirroring `FakeAnthropicClient`'s `expect_*` API,
plus the offline test suite for every `GeminiProvider` method including the safety-filter
branch and the full exception-mapper taxonomy (against genuine
`google.genai.errors.*` instances). Lazy SDK import inside each test (Snowflake `_sfe()`
pattern from `warehouse-adapters.md` — avoids the full-suite `sys.modules` deletion
gotcha).

**Files.**
- `tests/llm/_fake_gemini.py` (new): `FakeGeminiClient` + `FakeGeminiMessages`,
  `expect_messages_create(matching, returns)`, `create_calls` inspector property,
  `assert_all_expectations_met()`, support dataclasses (`FakeGeminiCandidate`,
  `FakeGeminiContent`, `FakeGeminiPart`, `FakeGeminiUsageMetadata`, `FakeGeminiResponse`).
- `tests/llm/test_gemini_provider.py` (new): unit tests for
  `build_create_kwargs` (system_instruction shape, no cache_control, no extra_headers),
  `extract_text_blocks` (happy path, safety-blocked → `LLMResponseFormatError`,
  finish_reason quoted in message), `extract_usage` (cache fields zero), `make_client`
  (calls `_make_gemini_client`), `classify_exception` for each ExceptionCategory.
- `tests/llm/test_gemini_exception_mapping.py` (new): drives `classify_exception`
  against genuine `google.genai.errors.*` instances; lazy import inside each test.

**TDD.** Write the test list first (one assertion per `ExceptionCategory`, one per
finish_reason branch, one per request-shape invariant); implement to pass.

**Acceptance criteria.**
- Every `ExceptionCategory` has at least one test mapping a real
  `google.genai.errors.*` instance to it.
- The safety-blocked test asserts the raised `LLMResponseFormatError`'s message names
  the finish_reason verbatim (case-sensitive).
- `build_create_kwargs` test asserts the kwargs dict has no `cache_control` substring
  anywhere AND no `extra_headers` key.
- `FakeGeminiClient.assert_all_expectations_met()` is invoked at the end of every test
  that queued expectations.
- Canonical validation command passes.

**Done when.** Fake + three new test files exist, all asserting behaviour pinned by DECs.

### US-005 — Provider-neutrality end-to-end tests (draft + grade)

**Traces to:** DEC-011, DEC-014, DEC-003 (audit/sidecar round-trip with zero cache
tokens), DEC-005 (safety-blocked → grade degrade).

**Description.** Drive `draft_schema` AND `grade_artifacts` end-to-end with
`provider="gemini"` using `FakeGeminiClient` injection. Mirrors
`tests/grade/test_provider_neutrality.py` (the `FakeNoCacheProvider` proof) but with the
real Gemini provider + request shape exercised. Asserts:

- Audit JSONL records exist with `cache_creation_input_tokens == cache_read_input_tokens == 0`.
- All blake2b-8 reproducibility hashes (rubric, prompt_version_template,
  criterion_prompt_hash, response_text_hash, args_hash) are populated.
- Drift detectors (`Strict<X>(extra="forbid")` mirrors) accept the produced JSONL/sidecar.
- No cache-anomaly WARNING surfaces (gated off by `supports_prompt_caching=False`).
- A safety-blocked grade response degrades the pair to
  `GradingResult(score=None, passed=False, reasoning="call failed: GradeLLMError")` —
  not a crash, not a `GradeOutputError`.

**Files.**
- `tests/grade/test_gemini_neutrality.py` (new): grade end-to-end + safety-blocked degrade.
- `tests/draft/test_gemini_neutrality.py` (new): drafter end-to-end (one schema draft
  call via `draft_schema`, asserts `LLMResponseEvent` JSONL round-trip).

**TDD.** Mirror `tests/grade/test_provider_neutrality.py` test names + `_isolate_registry`
fixture; substitute `FakeGeminiClient` injection + real `GeminiProvider`.

**Acceptance criteria.** Every assertion above passes. Canonical validation command
passes.

**Done when.** Both test files exist; each test asserts the listed invariants; runs
green in `uv run pytest`.

### US-006 — Gemini pricing SKUs in `pricing.py`

**Traces to:** DEC-017.

**Description.** Add three Gemini SKUs to `_PRICES_MUTABLE` in `pricing.py`; bump
`PRICE_TABLE_VERSION`. Mirrors #136 US-004's OpenAI-SKU additions; parallel-safe.

**Files.**
- `src/signalforge/llm/pricing.py` (edit): extend `_PRICES_MUTABLE` with `gemini-2.5-pro`,
  `gemini-2.5-flash`, `gemini-2.0-flash`. Each carries `input_per_mtok` +
  `output_per_mtok` (USD per 1M tokens) from Google's public price page at PR-prep
  time; `cache_write_5m_per_mtok = 0.0`; `cache_read_per_mtok = 0.0`. Bump
  `PRICE_TABLE_VERSION` to the ship date (e.g. `"2026-05-27"`).
- `tests/llm/test_pricing.py` (edit): assert `lookup("gemini-2.5-pro")`,
  `lookup("gemini-2.5-flash")`, `lookup("gemini-2.0-flash")` each return a non-zero
  `input_per_mtok` + `output_per_mtok` and zero cache fields. Assert
  `lookup("gemini-9-unicorn")` still raises `EstimateUnknownModelError`. Assert the
  three existing Anthropic SKUs round-trip unchanged.

**TDD.** Pricing-lookup tests first (red); add SKU entries (green); pin the version bump.

**Acceptance criteria.**
- All three Gemini SKUs resolve via `lookup()` with non-zero input/output rates and
  zero cache rates.
- `PRICE_TABLE_VERSION` bumped (asserted via byte-equal string match against the new
  ship date).
- Unknown Gemini model still raises `EstimateUnknownModelError`.
- The three Anthropic SKUs are byte-identical (their `ModelPricing` instances unchanged).

**Done when.** Pricing extended, tests green, validation passes.

**Depends on:** none (parallel-safe with US-001 … US-005).

### US-007 — `GeminiProvider.estimate_input_tokens` + `--estimate` integration

**Traces to:** DEC-016, DEC-017, DEC-019.

**Description.** Implement Gemini's side of the `LLMProvider.estimate_input_tokens`
ABC method shipped by #136. Uses google-genai's native `client.models.count_tokens`
(no `tiktoken`-equivalent local dep) — cleaner story than OpenAI's path because
Gemini has a first-party count endpoint. Pin a fake-driven `--estimate` test that
drives the path end-to-end.

**Files.**
- `src/signalforge/llm/_gemini_client.py` (edit): add a thin wrapper around
  `client.models.count_tokens(model=, contents=)` (e.g. `_count_gemini_tokens(client,
  model, text) -> int`) — confined to the shim per DEC-001.
- `src/signalforge/llm/providers.py` (edit): implement
  `GeminiProvider.estimate_input_tokens(model, text) -> int` delegating to the shim
  helper. The orchestrator-side strategy threading lives in #136's US-005.
- `tests/llm/test_gemini_provider.py` (edit): add a test driving
  `GeminiProvider.estimate_input_tokens` against `FakeGeminiClient` queued with a
  `count_tokens` response (extend `FakeGeminiClient` from US-004 with
  `expect_count_tokens(matching, returns)` if not already covered).
- `tests/cli/test_estimate.py` (edit): add a fake-driven test running
  `signalforge generate --estimate` with `grade.provider: gemini` + `grade.model:
  gemini-2.5-flash` (and the drafter-side equivalent), asserting the report renders
  with non-zero token counts + non-zero USD figures. Mirrors #136 US-005's OpenAI
  fake-driven estimate test.

**TDD.** Provider unit test first (`GeminiProvider.estimate_input_tokens` returns
the count_tokens response's `total_tokens` field). Then the CLI integration test
driving `--estimate` end-to-end. Anthropic byte-identity is #136's gate (this story
inherits the snapshot test landed there).

**Acceptance criteria.**
- `GeminiProvider.estimate_input_tokens("gemini-2.5-flash", "hello world")` returns
  a positive int (against a `FakeGeminiClient` queued with the expected response).
- `signalforge generate --estimate` with `grade.provider: gemini` produces an
  `EstimateReport` with non-zero grader token counts and non-zero USD figures.
- Anthropic byte-identity snapshot (from #136) remains green — no estimate-path
  regression from the Gemini wiring.

**Done when.** Above ACs pass; validation green; no `--cov-fail-under` regression.

**Depends on:** US-002 (provider class exists), US-006 (pricing SKUs exist), and
**#136 merged** (provides the ABC extension + the cli/_estimate.py strategy
refactor). If #136 is not yet merged at devolve time, this story is blocked.

### US-008 — Live tests + CONTRIBUTING update

**Traces to:** DEC-012, DEC-016.

**Description.** Maintainer-gated live tests against the real Gemini API. Registers
`gemini` marker; threads `_skip_reason()` env-var gate. Covers `call_llm`, `draft_schema`,
`grade_artifacts`, AND `--estimate` (the last one is the live counterpart to US-007's
fake-driven CLI test).

**Files.**
- `pyproject.toml` (edit): register `gemini` marker; add `and not gemini` to default
  `addopts`.
- `tests/llm/test_gemini_live.py` (new): one `@pytest.mark.gemini` test calling
  `call_llm(provider="gemini", ...)` directly; asserts non-empty `text_blocks`,
  `cache_*_input_tokens == 0`, `input_tokens > 0`.
- `tests/draft/test_gemini_draft_live.py` (new): `@pytest.mark.gemini` `draft_schema`
  against a small in-test manifest fixture; asserts `CandidateSchema` validates +
  `LLMResponseEvent` JSONL written.
- `tests/grade/test_gemini_grade_live.py` (new): `@pytest.mark.gemini` `grade_artifacts`
  against a 1-criterion rubric over a 1-artifact candidate; asserts one
  `GradingResult` with `score is not None` and `aggregate_complete is True`.
- `tests/cli/test_e2e_estimate_gemini.py` (new): `@pytest.mark.gemini`
  `signalforge generate --estimate ...` with `grade.provider: gemini` + `grade.model:
  gemini-2.5-flash`; asserts the rendered report includes non-zero grader USD
  estimate, exit code 0, no traceback. Mirrors #136 US-006's OpenAI live-estimate test.
- `CONTRIBUTING.md` (edit): add `uv run pytest -m gemini --no-cov` to the maintainer
  marker-run list, alongside the existing `snowflake` / `anthropic` / `openai` lines.
  Note required env vars: `SF_RUN_GEMINI=1 GOOGLE_API_KEY=...`.

**TDD.** Live tests are integration smokes; structure assertions are deliberately
modest (no LLM-output-byte assertions; engineered determinism via 1-criterion rubric +
the same `not_null`-on-clean-column trick `testing-signal.md` § "Engineered determinism"
documents is not needed here — we're proving the wire, not the output quality).

**Acceptance criteria.**
- Default `pytest` does NOT collect `@pytest.mark.gemini` tests.
- `pytest -m gemini --no-cov` with no env vars surfaces four clear `pytest.skip(reason)`
  outputs (one per test) naming the missing var.
- Each live test, with env vars set, exits 0 against the real API.

**Done when.** Marker registered + excluded; four live tests exist with the env-gate;
CONTRIBUTING line landed.

### US-009 — Operator-facing docs + CHANGELOG

**Traces to:** DEC-007 (recommended model id), DEC-010 (`[gemini]` install), DEC-013
(cost guidance + provider list), DEC-016/017 (estimate path + pricing).

**Description.** Worker-writable docs only. `.claude/rules/*` updates live in P&M
(orchestrator-only, per `skill-parity.md` + memory). Update the operator surface:

**Files.**
- `docs/grade-ops.md` (edit):
  - In `signalforge.yml` `grade:` block example, add a comment showing `provider: gemini`
    + recommended model id alternative (`gemini-2.5-flash` for judge work).
  - Update the registered-providers wording to enumerate `anthropic`, `openai` (from
    #136), and `gemini`.
  - Add a "Gemini cost note (v0.3)" paragraph to § Cost guidance with the DEC-013 text.
  - Note the `[gemini]` install (`pip install signalforge-dbt[gemini]`).
- `docs/draft-ops.md` (edit): equivalent updates for the drafter `llm:` block (provider
  list, install hint, cost note).
- `docs/cost-estimate-ops.md` (edit, or create if absent following #136 US-007): name
  Gemini's `client.models.count_tokens` as the estimate-path source — no
  `tiktoken`-equivalent local dep, one extra round-trip per estimate call. Reference
  the three Gemini SKUs in the pricing table.
- `README.md` (edit): if the README lists supported providers, add Gemini.
- `CHANGELOG.md` (edit): under `[Unreleased]` "Added": `Gemini as a grading + drafting
  provider (#137). Set grade.provider: gemini or llm.provider: gemini in
  signalforge.yml; requires the [gemini] install extra and GOOGLE_API_KEY. v0.3 ships
  without prompt caching; explicit Gemini context caching is a follow-up.`

**TDD.** N/A (docs).

**Acceptance criteria.** Each ops doc names `gemini` as a registered provider AND ships
a cost-guidance paragraph naming the no-caching deferral. The install hint appears in
both ops docs. CHANGELOG carries the new entry under `[Unreleased]`. Canonical
validation command passes (the docs gate runs `mkdocs build` on PR via the `docs-build`
job per `docs-publishing.md`).

**Done when.** Docs edits land, CHANGELOG entry landed, mkdocs build is clean.

### Quality Gate

Run `/code-review` (or equivalent) **4 times** across the full changeset, fixing every
real bug found each pass. Run CodeRabbit on the PR. Canonical validation command must
pass after every round of fixes. **Additionally:**

- `uv run pytest -m anthropic --no-cov` passes (proves Anthropic byte-identity in the
  `--estimate` strategy refactor end-to-end — inherits #136's snapshot).
- `uv run pytest -m gemini --no-cov` passes locally with credentials (live smoke).
- `uv run pytest -m wheel_smoke --no-cov` passes (new `[gemini]` extra doesn't break
  wheel build — per `python-build.md` § "wheel_smoke maintainer-gate"; the test asserts
  the canonical demo file set still appears under the expected wheel path with the new
  optional-dep declaration).
- Coverage stays at or above the current threshold.

**Depends on:** US-001 … US-009.

### Patterns & Memory (orchestrator-only)

**Files.**
- `.claude/rules/llm-drafter.md` (edit):
  - Update "Provider-neutral seam — generic orchestrator + per-provider strategy (#135)"
    section to note the second concrete provider (Gemini) shipped under #137 and the
    `_gemini_client.py` confinement; bump "AST audit-completeness scans" from four to
    five (the 9th scan).
  - Add a paragraph: **"v0.3 Gemini ships no-cache."** Both capability flags `False`;
    request shape collapses `system + cached_block + dynamic_block` into
    `system_instruction + single user turn`; safety-filter no-content responses surface
    as `LLMResponseFormatError` → grade degrade. Explicit Gemini context caching is a
    follow-up.
- `.claude/rules/grade-layer.md` (edit): one sentence in the degrade taxonomy noting
  Gemini safety-filter responses route through the same `GradeLLMError` degrade as
  Anthropic retry-exhaustion — the contract is provider-neutral.

**Done when.** Both rule files updated, the lesson is durably captured for #136 (OpenAI)
to copy this plan and substitute the vendor.

## Worker-writability routing

Per `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/ralph-worker-claude-dir-perms.md`: **Ralph workers cannot Write under `.claude/` in worktrees** — only the orchestrator can. The story split honours this:

- US-001 through US-008 + US-009 (docs + CHANGELOG) + Quality Gate touch only worker-writable paths (`src/`, `tests/`, `pyproject.toml`, `docs/`, `CHANGELOG.md`, `README.md`, `uv.lock`, `CONTRIBUTING.md`).
- **Patterns & Memory is orchestrator-only** because it edits `.claude/rules/llm-drafter.md` and `.claude/rules/grade-layer.md`. The beads-manifest line already labels the task "(orchestrator: …)"; if a worker is dispatched against P&M the bead fails with a write-denied error — route it to the orchestrator at devolve time.

The OpenAI plan (#136) codifies the same routing; mirroring it here so the rule lands durably for both #136 and #137. Future provider plans (#138+ if a fourth vendor ever ships) should copy this section verbatim.

## Open notes for implementation

- **Verify the exact `google.genai.errors` exception class names + status-code attrs
  against the installed SDK version at implementation time.** DEC-006's mapping is the
  shape; the precise SDK-class names (`ClientError` vs `APIError`, status-code attr name
  `code` vs `status_code`) may need a tiny adjustment. The offline exception-mapper test
  drives this — write the test against the installed SDK, then implement to pass.
- **`response_mime_type="application/json"` requires NO keyword in the prompt.** Unlike
  OpenAI's `response_format={"type":"json_object"}` (which fails server-side unless
  "json" appears in the prompt — see #136's Open notes), Gemini's structured-output
  enforcement is purely a request flag. Don't add a defensive "JSON" sentence to the
  grade or draft system prompts on Gemini's behalf — the existing prompts already name
  JSON for the OpenAI path, and that's exclusively an OpenAI requirement. If a future
  refactor splits the system prompts per-provider, this is the only Gemini-specific
  prompt note to preserve.
- **`pricing.lookup` returns zero cache fields for the three Gemini SKUs.** US-006:
  assert this in `tests/llm/test_pricing.py`. `cli/_estimate.py` cache-cost math (the
  `cache_write_5m_per_mtok` / `cache_read_per_mtok` multiplications) should produce 0.0
  contributions without raising — verify the multiplication path doesn't break on a zero
  `cache_write_5m_per_mtok`. Mirrors the symmetric verification item from #136.
- **`messages.create` façade in `GeminiClientProtocol`.** The orchestrator calls
  `llm_client.messages.create(**kwargs)`. The shim adapts this to
  `client.models.generate_content(...)` internally — the protocol exposes
  `.messages.create` so the orchestrator stays vendor-neutral. The fake mirrors the
  façade. Confirm during US-001 that this is the cleanest adaptation; if the protocol
  needs an extra method (e.g. count_tokens, even though we never call it), add it as
  `NotImplementedError` stub for protocol-completeness.
- **`extract_text_blocks` finish-reason enumeration.** Google's `FinishReason` enum
  ships values like `STOP`, `MAX_TOKENS`, `SAFETY`, `RECITATION`, `OTHER`,
  `MALFORMED_FUNCTION_CALL`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`. Treat anything
  other than `STOP` (or `STOP` with empty text) as the no-content branch — the message
  quotes the reason verbatim so the operator sees exactly which filter fired.
- **No new `WarehouseError`-style sub-hierarchy.** Reuse `LLMResponseFormatError` /
  `LLMHelperError` / `LLMAuthError` / etc. The taxonomy is provider-neutral by design.
  No new entries in the `_EXCEPTION_TO_EXIT_CODE` table (per `cli-layer.md` § 7th AST
  scan).
- **The 9th AST scan re-uses `_QualifiedNameCallFinder`.** Don't roll a new visitor;
  the existing helper handles all three bypass patterns (`testing-signal.md` § "AST
  single-construction-seam scans must catch all three bypass patterns").
