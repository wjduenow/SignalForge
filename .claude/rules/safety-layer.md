# Safety layer (PII redaction + audit log)

Established by issue #4 (PII safety layer). Apply to every module under `signalforge.safety` and to any new code that constructs an `LLMRequest`, redacts data heading to an LLM, or writes an audit record.

The safety layer sits between the warehouse adapter (#3) and the LLM-drafting pipeline (#5). It enforces SignalForge's deployment-blocker safety posture: schema-only by default, sample is explicit opt-in, every LLM call leaves a durable audit receipt.

## Fail-closed audit semantics (DEC-011)

Any exception inside `audit.write` propagates as `AuditWriteError` from `build_llm_request`. The function never returns an `LLMRequest` whose audit record didn't durably hit disk. Concretely:

- `audit.write` opens with `O_APPEND | O_CREAT | 0o600`, writes one JSONL line, calls `os.fsync`, closes.
- Catches **no** exceptions internally — `OSError`, `PermissionError`, `IOError`, encoding failures all propagate.
- Size cap (`_AUDIT_RECORD_LIMIT_BYTES = 4000`) checked **before** any file open, so an oversize record leaves no artifact.
- `build_llm_request` calls `audit.write` AFTER constructing the request but BEFORE returning it. If audit fails, the partial request is dropped.

An unaudited LLM call is, by definition, PII leaving the warehouse without a receipt — exactly the failure mode this layer exists to prevent. Don't add try/except around audit writes "to be defensive"; the propagation IS the defence.

This is the **primary-work fail-closed** pattern: the audit write is part of the unit of work, and the unit either fully succeeds or fully aborts. Contrast with the **cleanup-boundary fail-soft** pattern in `warehouse-adapters.md` DEC-013/DEC-014: cleanup fires AFTER the user's actual work has succeeded, so blocking on cleanup failure would punish the operator for housekeeping issues they can't fix mid-flight; cleanup paths swallow + emit an operator-actionable WARNING instead. The two look superficially similar (both deal with "I/O failed at a defended boundary") but apply at different points in the lifecycle. Don't conflate them when introducing a new boundary in v0.3 — primary-work boundaries propagate; cleanup-boundary swallows-and-warns.

## Column NAMES leak PII too — redact them with stable hashes (DEC-010)

A column named `customer_ssn` or `john_smith_email` leaks PII via the name itself, even when no values are sampled. The layer redacts NAMES in `schema-only` and `aggregate-only` modes, not just values in `sample` mode.

The hash is `f"col_{blake2b(name.encode(), digest_size=4).hexdigest()}"` — 8 hex chars, deterministic per name, computed once and recorded in `RedactionRecord.hashed_name`. The (real → hashed) mapping is persisted to the audit JSONL so a reviewer can map back; the LLM only ever sees the hash.

When adding a new mode or surface, make sure the LLM-bound payload uses hashed names for every column where `RedactionRecord.redacted is True`.

## AuditEvent reproducibility fields (DEC-014)

Every `AuditEvent` carries three fields that look minor but are load-bearing:

- `signalforge_version: str` — read from `signalforge.__version__` at write time. Lets a reviewer know which code produced the record.
- `policy_hash: str` — 16-hex-char SHA-256 of the resolved `SafetyPolicy.model_dump_json` (sorted keys, canonical form via the `_compute_policy_hash` helper). Lets a reviewer verify all records in a run came from the same policy.
- `audit_schema_version: int = 1` — frozen at the constant in production code. Bump when the JSONL schema evolves; v0.2 readers gate on this.

The drift-detector test (`tests/safety/test_drift_detector.py`) pairs production `AuditEvent` (`extra="ignore"`) with a one-off `StrictAuditEvent` (`extra="forbid"`) validated against the committed JSONL fixture. Adding a field to production without updating the strict model OR the fixture breaks the test loudly. Don't bypass.

## Config-shaped models use `extra="forbid"`; read-back models use `extra="ignore"` (DEC-015)

The default in `manifest-readers.md` is `extra="ignore"` for forward-compat. The safety layer **deliberately overrides** for config files:

- `SafetyPolicy`, `_SafetyPolicyContent` (the inner `signalforge.yml` block), `_SafetyConfigFile` → `extra="forbid"`. A typo like `redacts:` (vs `redact:`) in a user-authored YAML file MUST fail loud — silent no-op is exactly the failure mode this ticket exists to prevent.
- `AuditEvent`, `RedactionRecord`, `LLMRequest` → `extra="ignore"`. These are read back from JSONL files / consumed by downstream stages where forward-compat matters.

Pair every `extra="ignore"` reader-shaped model with a one-off `extra="forbid"` drift detector.

## The four opt-out signals + precedence (DEC-003)

`_classify_column` returns `RedactionRecord | None` based on (in precedence order, first match wins, column-level always beats model-level when both fire):

1. Column-level `meta.signalforge.sample == False` → `column_meta_optout`
2. Column-level `tags: ["pii"]` (case-insensitive) → `tag_pii_column`
3. Column-level `meta.contains_pii` truthy → `meta_contains_pii_column`
4. Model-level `meta.signalforge.sample == False` → `model_meta_optout`
5. Model-level `tags: ["pii"]` → `tag_pii_model`
6. Model-level `meta.contains_pii` truthy → `meta_contains_pii_model`
7. Pattern match against `policy.redact_patterns` (case-insensitive `fnmatch`) → `pattern_match`

Every coercion path emits a DEBUG log when it normalises (e.g., `tags: [PII]` → lowercase, `meta.contains_pii: "yes"` → True). The seven reasons are a `Literal[...]` so audit-log consumers can pattern-match exhaustively. Don't add an eighth without updating both production `RedactionReason` and the drift detector.

## ANSI-safe lazy-format logger (DEC-022)

Every `_LOGGER.{info,warning,debug,error}` call in `signalforge.safety.*` uses lazy-format with `json.dumps()` for any user-controlled string:

```python
_LOGGER.info("audit event: %s", json.dumps({"unique_id": event.model_unique_id, ...}))
```

**Never** `_LOGGER.info(f"... {model_unique_id} ...")` — a column name or model id containing ANSI escapes (`\x1b[31m...`) would inject into log viewers. JSON encoding handles this; f-string interpolation does not. Quality-gate validation greps for `_LOGGER.\w+\(f"` and rejects any hits in `src/signalforge/safety/`.

## AST audit-completeness scan (DEC-020(a))

`tests/safety/test_public_api.py::test_llm_request_construction_only_in_request_module` scans every `.py` under `src/signalforge/safety/` (excluding `request.py`) for `Call(func=Name(id="LLMRequest"))` and rejects any hits. The convention is "construct `LLMRequest` only via `build_llm_request`" — the docstring on `LLMRequest` says so, and the AST scan enforces it.

If you add a new module that genuinely needs to construct an `LLMRequest` (e.g., a deserialiser for resumption), update the AST-scan exclusion list AND document the audit-write seam. Don't suppress the test.

## Pydantic v2 `with_mode` re-runs validators (DEC-018)

`SafetyPolicy.with_mode(mode)` does NOT use `model_copy(update=...)` — that path silently skips `@model_validator(mode="after")`, which means going through the documented CLI override seam would silently enable sample mode without emitting the DEC-021 WARNING.

Use `model_validate({**self.model_dump(), "mode": mode})` so every validator re-runs. This was caught by Quality-Gate review and is a regression worth defending against in any future "factory that produces a mutated copy" helpers — `model_copy` is the wrong tool any time a validator side-effect is part of the contract.

## `signalforge.yml` top-level namespace: `safety:` (DEC-025)

The config file's top level is `{ safety: { ... } }`. Other top-level keys (`llm:`, `prune:`, `grade:`, ...) are reserved for future stages and silently ignored by the safety loader. Each stage validates its own subtree independently.

When introducing a new pipeline stage with config, claim its own top-level key. Don't pile config under `safety:` — that violates the namespacing reservation and forces a v2-config migration when you eventually split.

## Reference

`plans/super/4-pii-safety.md` — DEC-001 … DEC-026. `src/signalforge/safety/` — current implementation. `tests/safety/_fake_adapter.py` — `FakeAdapter` + `expect_*` API. `docs/safety-ops.md` — operational reference. `tests/fixtures/safety/manifest_with_pii_meta.json` — fixture exercising all four opt-out signals.
