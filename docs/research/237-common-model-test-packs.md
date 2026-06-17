# Issue #237 — Research: sample test packs for common shared dbt models + an "install" path

**Status:** RESEARCH COMPLETE (2026-06-17). **Recommendation: NO-GO on a static
pre-baked test pack.** A scoped **"seed-and-prune"** alternative (curated
*business-rule candidate seeds* distributed via the existing `install-skill`
mechanism, graded against the user's own warehouse) is the only shape that
survives the counter-arguments — and even it is **gated** on a marginal-value
spike vs. plain `generate`. See § "Go / No-Go recommendation".

This is an exploratory / backlog research document. It is **not** part of any
roadmap milestone (the `README.md` roadmap table is the source of truth for
shipped scope). It is internal-only and excluded from the published site via
`exclude_docs: research/` (see `.claude/rules/docs-publishing.md`).

---

## tl;dr

- **The opportunity is real but mis-shaped.** The most popular shared dbt model
  packages (Fivetran connectors; Snowplow) ship **only structural tests** —
  primary-key `not_null` + some `unique`, and essentially **zero** semantic
  coverage (`relationships`, `accepted_values`, range/expression, volume). The
  "they already ship lots of tests, so marginal value is low" counter-argument
  **does not hold**: there is a large, uniform semantic gap.
- **But the *static pack* form is blocked four independent ways**, any one of
  which is close to fatal:
  1. **dbt mechanically forbids it.** You cannot YAML-patch generic tests onto
     a model owned by *another* installed package — dbt silently warns and drops
     the patch ("Did not find matching node"). Model-by-name override is an
     unimplemented feature request ([dbt-core #4157](https://github.com/dbt-labs/dbt-core/issues/4157)).
     A third party can attach tests *only* via **singular tests using two-arg
     `ref('pkg','model')`** or a **passthrough model it owns**.
  2. **Warehouse drift breaks static SQL.** Quoted identifiers and
     type-name/column-name comparisons against `information_schema` differ
     across BigQuery / Snowflake / Redshift casing + type spellings.
  3. **Version churn rots the pack fast.** These packages release *very*
     frequently (zendesk: 81 releases, ~33 in the last year). N connectors × M
     versions × W warehouses is an unbounded maintenance matrix.
  4. **It is the exact noise SignalForge exists to drop.** A pre-baked,
     ungraded, always-pass test is precisely what the prune step removes
     (Architectural Commitment #1). Shipping one undercuts the product thesis.
- **`generate` / `prune-existing` already deliver a *better* result today.**
  Pointed at the user's own materialized package models, they produce
  warehouse-correct, **graded** tests — making a static pack a strictly worse
  version of the shipped product (cannibalization).
- **The only defensible shape is "seed-and-prune":** package the *one* thing an
  LLM cannot infer from schema alone — **curated business-rule knowledge**
  (Stripe enum domains, the NetSuite FK graph, Shopify amount-sign invariants) —
  as `business_rules` / `custom_sql` **candidate drafts** that flow through
  `prune` / `generate` / `prune-existing` against the user's warehouse. This
  resolves all four blockers. **But it is gated** on a spike proving the curated
  seed yields materially more graded signal than plain `generate` on the same
  models; if `generate`'s LLM already infers most of it, the seed isn't worth the
  upkeep.

---

## Methodology & a load-bearing data caveat

All package metrics were pulled **2026-06-17** live from the GitHub REST API
(`api.github.com/repos/...`) for stars/forks/releases and
`raw.githubusercontent.com` for file contents; test counts are `grep` tallies
across **every** `models/**/*.yml` in each package. Usage / dbt-mechanics claims
are grounded in dbt official docs, Fivetran/dbt-labs package READMEs, and
dbt-core GitHub issues (cited inline).

**dbt Hub does not publish download or install counts.** The hub package pages
([e.g. hub.getdbt.com/fivetran/stripe](https://hub.getdbt.com/fivetran/stripe/latest/))
and the only public feed
([`hub.getdbt.com/api/v1/index.json`](https://github.com/dbt-labs/hub.getdbt.com/))
carry version/compatibility metadata with **no download statistics**. The
ticket's §A asks for "dbt Hub download/install counts"; that metric is **not
obtainable**. **GitHub stars are used throughout as the popularity proxy** — an
imperfect one (stars under-count install reach for vendor packages installed via
`dbt deps` without ever visiting the repo), and this weakens any
"reach × marginal-value" ranking. Stated up front because it shapes §A's
confidence.

---

## A. Catalog & metrics — which models, how common, what already ships

### A.1 Fivetran connector packages (the primary candidates)

| Package | GH stars | GH forks | # models¹ | Warehouses | Release cadence | Tests already shipped (exact `models/**/*.yml` counts) |
|---|---|---|---|---|---|---|
| `dbt_ad_reporting`² | **213** | 78 | 20 | BQ/SF/RS/DB/PG | very active | `not_null` 7 · everything else **0** · freshness ❌ |
| `dbt_shopify` | 80 | 47 | 264³ | BQ/SF/RS/DB/PG | very active (53 rel.) | `not_null` 154 · `unique` 66 · rel/acc/expectations **0** · freshness ❌ |
| `dbt_stripe` | 59 | 40 | 69 | BQ/SF/RS/DB/PG | very active (50 rel.) | `not_null` 17 · `unique` 0 · rel/acc/expectations **0** · freshness ✅ |
| `dbt_netsuite` | 54 | 42 | 110³ | BQ/SF/RS/DB/PG | very active (52 rel.) | `not_null` 62 · `unique` 18 · rel/acc/expectations **0** · freshness ✅ |
| `dbt_salesforce` | 52 | 38 | 26 | BQ/SF/RS/DB/PG | active (27 rel.) | `not_null` 23 · `unique` 23 · rel/acc/expectations **0** · freshness ✅ |
| `dbt_hubspot` | 44 | 47 | 154³ | BQ/SF/RS/DB/PG | very active (57 rel.) | `not_null` 68 · `unique` 5 · rel/acc/expectations **0** · freshness ✅ |
| `dbt_zendesk` | 31 | 29 | 98³ | BQ/SF/RS/DB/PG | very active (81 rel.) | `not_null` 13 · `unique` 0 · rel/acc/expectations **0** · freshness ✅ |

Other Fivetran packages by stars: `facebook_ads` 51, `quickbooks` 38, `google_ads`
26, `github` 22, `jira` 12, `xero` 11, `marketo` 8, `linkedin` 3.

¹ Counts include staging `*__tmp`/base + intermediate models, so they overstate
user-facing final models. ² Roll-up meta-package over the ad-source packages; no
sources of its own, hence no freshness. ³ Includes `tmp/` helper + API-variant
models (e.g. shopify spans GraphQL + REST sets).

*Sources:* per-repo GitHub REST endpoints
(`api.github.com/repos/fivetran/dbt_<pkg>`), recursive trees
(`.../git/trees/main?recursive=1`), releases, and raw `models/**/*.yml`; READMEs
for warehouse support.

### A.2 Non-Fivetran model packages (Snowplow, Segment, dbt-labs, GitLab)

| Package | Stars | Forks | # models | Warehouses | Cadence | not_null | unique | relationships | accepted_values | dbt_utils/expectations |
|---|---|---|---|---|---|---|---|---|---|---|
| [`snowplow/dbt-snowplow-web`](https://github.com/snowplow/dbt-snowplow-web) | 65 | 19 | 47 | BQ/SF/DB/RS/PG | active (40 tags) | 91 | 28 | **0** | 1 | **0** |
| [`snowplow/dbt-snowplow-unified`](https://github.com/snowplow/dbt-snowplow-unified) | 22 | 20 | 46 | same | active (1.0.0 Jan 2026) | 110 | 32 | **0** | 1 | **0** |
| [`snowplow/dbt-snowplow-mobile`](https://github.com/snowplow/dbt-snowplow-mobile) | 15 | 5 | 25 | same | active (22 tags) | 142 | 24 | **0** | 0 | **0** |
| [`dbt-labs/segment`](https://github.com/dbt-labs/segment) | 75 | 63 | 6 | (historical) | **archived** (0.9.0, 2022) | 6 | 6 | **0** | 0 | **0** |
| `dbt-labs/snowplow` (legacy) | 130 | 43 | 18 | RS/BQ/SF era | stale (0.15.1, 2023) | 12 | 6 | 3 | 0 | 1 |

`gitlab-data/analytics` is an internal, un-packaged dbt project on gitlab.com
(not a GitHub/dbt Hub package — not installable via `dbt deps`). The active
first-party dbt-labs connector packages are gone (`segment` archived; `snowplow`
superseded by Snowplow's own packages).

### A.3 Utility/macro packages (ecosystem context — NOT candidates, they ship no models)

`elementary` 2,364★ · `dbt-utils` 1,761★ · `dbt-expectations` 1,227★ (canonical
repo dormant since 2024-12; active fork
[metaplane/dbt-expectations](https://github.com/metaplane/dbt-expectations)) ·
`codegen` 659★ · `dbt-project-evaluator` 561★ · `audit-helper` 411★ · `dbt-date`
266★. These are the *macro vocabulary* a pack would draw from, not pack targets.

### A.4 Marginal-value gap — the decisive metric

**The gap is large and uniform.** Across *every* package audited — Fivetran and
Snowplow alike — the shipped test surface is **primary-key structural integrity
only**: `not_null` (always) + `unique` (sometimes; absent in zendesk, stripe
transform, ad_reporting). The entire **semantic** layer is unshipped:

- **`relationships`: 0** across all Fivetran packages and all three Snowplow
  packages (the only relationship tests anywhere live in the *deprecated*
  legacy `dbt-labs/snowplow`).
- **`accepted_values`: 0** Fivetran; ≤1 per Snowplow package.
- **`dbt_expectations.*` / range / `dbt_utils.expression_is_true`: 0** everywhere.
- **Singular/custom tests: 0 installed.** Every `tests/*.sql` in these repos is
  under `integration_tests/` — CI-only, **not** pulled by `dbt deps`.

Concretely: install `fivetran/zendesk` (98 models) and you get **13 `not_null`
checks + source freshness, nothing else.** Snowplow-web — a sophisticated
47-model incremental package — ships ~99% PK-shape coverage and **0 relational
integrity, 1 value-domain check.** The tests assert "the grain is what we say it
is" and stop. **So a test pack adding `relationships` across the modeled FK
graph, `accepted_values` on status/enum columns, and range/volume checks on
amounts and dates is additive on every one of these packages.** §E.2 ("low
marginal value") is therefore *refuted* — but that does not make a static pack a
good idea (see §C, §D, §E.1/3/4/6).

---

## B. Usage research — how operators actually consume these models

1. **Workflow (confirmed).** Connector lands raw data → declare package in
   `packages.yml` (or `dependencies.yml`) → `dbt deps` installs into
   **`dbt_packages/`** (gitignored, treated as read-only build output;
   re-pulled on every `dbt deps`) → the package's models **merge into your DAG**
   and materialize on your normal `dbt run`/`dbt build` → analysts build
   downstream ([packages doc](https://docs.getdbt.com/docs/build/packages)).
   `package-lock.yml` pins exact resolved versions.

2. **Consume-as-is dominates; editing models is structurally discouraged.**
   Because `dbt_packages/` is gitignored and re-pulled, hand-edits there are
   untracked and wiped. The designed lever is **config from your project**:
   package vars (`stripe__using_invoices`, source-location vars, pass-through
   columns), project-level `+enabled`/`+schema`/`+materialized` overrides, source
   `overrides:`, and macro `dispatch`. **Overriding a package model *by name* is
   unsupported** — [dbt-core #4157](https://github.com/dbt-labs/dbt-core/issues/4157)
   (never implemented; same-name collisions raise ambiguous-reference errors,
   [#8327](https://github.com/dbt-labs/dbt-core/issues/8327)). When config runs
   out, the supported escapes are a **downstream model** that selects from the
   package output, or a **fork**.

3. **Consolidation changes the attach surface.** Fivetran is folding the old
   `*_source` staging package into the transform package (Stripe, Salesforce,
   Shopify confirmed; `*_source` deprecated). The stable contract is the
   `<connector>_database`/`_schema` vars + pass-through columns — *not* a fixed
   set of model names, which shift across major versions.

4. **Schema drift.** Fivetran's `fill_staging_columns` macro **null-fills absent
   columns** (`cast(null as <type>) as <col>`) so staging keeps a stable column
   set; breaking column changes are gated behind major version bumps with
   deprecation windows. Without model contracts, dbt does **not** statically
   validate column existence — a reference to a dropped column compiles and
   fails only at `dbt run`. A static test with hardcoded column names is exactly
   the fragile artifact this produces.

---

## C. Install-path design — how would "download / install" work

### C.1 The mechanical wall (decides everything below)

**You cannot ship a dbt package that YAML-patches generic tests onto another
package's models.** A `schema.yml` is scoped to its own package; patching
`models: - name: X` only matches an unpatched `X` *in the same package*. Against
a model owned by a *different* installed package, dbt emits a **warning, not an
error**, and **silently does not apply the patch** (`"Did not find matching node
for patch..."` — [dbt-utils #924](https://github.com/dbt-labs/dbt-utils/issues/924),
[Discourse 17289](https://discourse.getdbt.com/t/warning-schema-yml/17289)).
Worse, because it's only a warning, a broken pack passes CI unnoticed unless
warnings are errors. The **only** third-party attach paths:

- **Singular tests via two-arg `ref('package_name','model')`** — fully supported
  ([data tests](https://docs.getdbt.com/docs/build/data-tests),
  [ref](https://docs.getdbt.com/reference/dbt-jinja-functions/ref)). Note the
  first arg is the package's `name` from its `dbt_project.yml` (`stripe`), not
  the hub path (`fivetran/stripe`).
- **A passthrough model you own** (`select * from {{ ref('stripe','...') }}`) +
  normal generic `tests:` attached to *that* model.

**This is decisive: a "pack" cannot be a schema.yml overlay. It must be singular
`.sql` tests (or passthrough models).** And a singular test = a SELECT returning
failing rows via `ref()` — which is **exactly SignalForge's `custom_sql`
business-rule primitive** (`.claude/rules/business-rule-tests.md`), and exactly
what `prune-existing --tests-dir` already ingests.

### C.2 Distribution mechanisms, ranked

| Mechanism | Verdict | Why |
|---|---|---|
| **CLI copy-out-of-wheel (mirrors `install-skill`)** | **Best fit, *if* anything ships** | Reuses the proven `signalforge.skill.install_skill` seam verbatim: bundled package-data under `src/signalforge/`, copied via `importlib.resources.files(...)`, wheel `include` directive, bounded symlink defence, parity gate. Lands singular `.sql` candidate seeds into the user's `tests/` (or a seed dir) where `prune-existing`/`generate` then grade them. Warehouse-agnostic concern handled downstream (compiled against the real schema), not baked in. |
| **dbt package on dbt Hub / git** | Poor | Can't patch tests cross-package (§C.1). Reduces to "a package of singular tests + passthrough models" — heavy, and it bypasses SignalForge's prune/grade entirely (ships ungraded always-pass tests → Commitment #1 violation). |
| **GitHub-released bundles (curl)** | Poor | Same content problem as dbt-package, plus no parity/freshness story and worse trust posture. |
| **Registry the CLI fetches on demand** | Premature | Real infrastructure (hosting, versioning, signing) for a feature whose *content* hasn't cleared the go/no-go bar. Revisit only post-"go". |

### C.3 Reuse assessment of the `install-skill` precedent (required by AC)

The `install-skill` mechanism (`src/signalforge/skill/__init__.py::install_skill`;
package-data tree `src/signalforge/skills/signalforge/`; wheel
`include = ["src/signalforge/_demo", "src/signalforge/skills"]`; parity gate
`tests/cli/test_skill_cli_parity.py`; see `.claude/rules/skill-parity.md`) is a
**strong mechanical reuse** for the *copy-out-of-wheel install* step:

- **Reuse the copy seam directly.** A hypothetical `install-pack <connector>`
  would mirror `install_skill` almost line-for-line: enumerate from
  `importlib.resources.files("signalforge").joinpath("packs")`, copy into a
  destination under the user's project, bounded symlink containment, four-tier
  exit codes. The two-name `skills/` (package-data) vs `skill/` (lib) convention
  applies: ship `src/signalforge/packs/<connector>/` (package-data, no
  `__init__.py`) + a `signalforge.pack` lib module.
- **The parity-gate lesson transfers.** Any new subcommand is a 6th-surface
  change (`SKILL.md` parity gate) and must keep the skill in lockstep.
- **What does NOT transfer is the *content* model.** `install-skill` ships a
  static, warehouse-independent prose artifact. A *test* pack's content cannot be
  static (§B.4, §D) — so the install mechanism is reusable but the thing it
  installs must be **candidate seeds**, not finished tests. The precedent solves
  delivery, not the central tension.

### C.4 Licensing / attribution

Referencing Fivetran's (and Snowplow's) **model/schema definitions** (column
names, FK structure) to author tests touches their package surface. The Fivetran
packages are Apache-2.0 (permissive) but redistributing or deriving from their
schema layouts warrants explicit attribution and a license review per source
package before any distribution. A seed that ships *business-rule prose*
(SignalForge-authored domain knowledge) rather than copied schema layouts
sidesteps most of this; a pack that mirrors their column graphs does not.

---

## D. Sample-test design — what would actually ship, and how it's graded

A pack of **pre-baked, passing** tests is a direct Commitment #1 violation: a
generic test that always passes on a clean warehouse is the exact noise the prune
step drops. So the only design that fits SignalForge is **seed-and-prune**:

- **Ship candidates as *drafts*, not finished tests.** The pack content is a
  curated set of `custom_sql` singular-test seeds (via two-arg `ref()`) and/or
  `meta.signalforge.business_rules` entries for known model shapes. They flow
  through `prune` / `generate` / `prune-existing` against the user's **own**
  warehouse — so always-pass candidates are *dropped*, warehouse casing/types are
  resolved correctly (the compiler emits unquoted, dialect-driven SQL —
  `.claude/rules/prune-engine.md`), and every shipped artifact is **graded** with
  a one-line "why."
- **A "pack" is therefore a curated `business_rules` / candidate seed for a known
  model shape** — reconciling cleanly with the eight existing primitives +
  `custom_sql`. The pack's *only* irreducible value is the **semantic knowledge an
  LLM can't read off the schema**: Stripe's `charge.status` enum domain,
  NetSuite's transaction→account FK graph, Shopify's `total_price >= 0` invariant,
  expected per-day order-volume bands. Everything mechanical (`not_null`,
  `unique`, obvious `relationships`) `generate` already infers — which is the
  cannibalization risk (§E.6).
- **Warehouse-agnostic by construction.** Because seeds are compiled and pruned
  per-warehouse rather than shipped as static SQL, the BigQuery/Snowflake/Redshift
  casing + type-spelling fragility (§B.4) never reaches the user. This is the
  single biggest argument for seed-and-prune over a static pack.

---

## E. Counter-arguments & adoption challenges (devil's advocate)

1. **Thesis conflict — fatal for the static form.** Pre-baked tests are the
   opposite of prune-against-real-data. A pack of generic passing tests *is* the
   noise SignalForge drops; shipping one undercuts the product's own argument.
   Seed-and-prune neutralizes this (everything is graded/pruned) — but only by
   abandoning "pre-baked" entirely.

2. **Low marginal value — REFUTED (a finding, not a risk).** §A.4 shows the
   shared packages ship PK-integrity only; the semantic layer is unshipped
   everywhere. There *is* a large net-new-signal gap. This counter-argument
   fails — value is the *reason to keep looking*, not the reason to stop.

3. **Combinatorial maintenance — severe.** N connectors × M package versions × W
   warehouse dialects. These packages churn fast (zendesk 81 releases / ~33 in
   the last year; shopify mid-migration between REST and GraphQL model sets). A
   static pack pinned to model names + columns rots on nearly every minor bump.
   Seed-and-prune reduces but does not eliminate this — `ref()` targets and
   business-rule prose still drift with major versions, though far slower than
   column-level static SQL.

4. **Fit / drift mismatch — high for static, moderate for seeds.** Per-user
   config (enabled sources, pass-through custom fields, version pins) means a
   one-size pack mismatches real projects; `fill_staging_columns` null-fills, so a
   static `not_null` on a column the user's connector didn't sync would
   *always-pass-on-NULL-filled-data* — noise. Seeds pruned against the user's
   actual warehouse self-correct (a candidate referencing an absent/NULL column is
   dropped with evidence).

5. **Distribution friction & trust.** dbt users already have `dbt deps`; a
   competing install path adds friction, and §C.1 shows a dbt-package form can't
   even do the obvious thing (patch tests onto package models). Operators running
   third-party *tests* blindly also incurs reviewer-attention cost — the very cost
   Commitment #1 exists to minimize. The CLI copy-out-of-wheel path (§C.2) is the
   least-friction option but still asks the user to adopt a SignalForge-specific
   step.

6. **Cannibalization — the strongest argument against building anything.**
   `generate` / `prune-existing` pointed at the user's already-materialized
   package models **today** produce a warehouse-correct, graded, explainable
   result. A static pack is a strictly worse version of that. Even a seed pack
   only adds value to the extent its curated business-rule knowledge exceeds what
   `generate`'s LLM already infers from the model SQL + schema + neighbors. If
   that delta is small, the pack is maintenance burden for marginal lift over a
   feature that already ships.

**Adoption-risk findings.** The feature is *worth building only if* all of the
following hold: (a) the curated business-rule seed demonstrably yields materially
more graded signal than plain `generate` on the same models (refutes §E.6); (b)
the seed is authored as **prune-able candidates**, never pre-baked passing tests
(neutralizes §E.1); (c) distribution reuses the `install-skill` copy-out-of-wheel
seam, not a dbt-package overlay (respects §C.1); (d) seeds carry **business-rule
prose**, not copied schema layouts (limits §C.4 + §E.3 drift). If (a) fails — if
`generate` already covers most of the gap — the correct outcome is **no feature
at all**, and the product answer to "test my Stripe models" stays "run
`signalforge generate` / `prune-existing` against them."

---

## Go / No-Go recommendation

**NO-GO on a static, pre-baked test pack** (any form: dbt-package, GitHub bundle,
or copied static SQL). It conflicts with the prune thesis (§E.1), is mechanically
blocked by dbt (§C.1), breaks across warehouses and versions (§B.4, §E.3/4), and
is cannibalized by what `generate` already ships (§E.6). The marginal-value gap is
real (§A.4) but does not rescue a form this constrained.

**CONDITIONAL / GATED on a scoped "seed-and-prune" spike.** The only shape worth a
follow-up is: curated `business_rules` / `custom_sql` **candidate seeds** for the
3–5 highest-reach model shapes (Snowplow-web, Stripe, Salesforce, NetSuite,
Shopify), distributed via the `install-skill` copy-out-of-wheel mechanism, that
flow through `prune` / `generate` / `prune-existing` against the user's own
warehouse. **Gate it on a single empirical question** (the implementation ticket's
first deliverable): *does a curated Stripe (or Snowplow-web) seed produce
materially more graded, kept tests than plain `generate` on the same models?*

- **If yes** → a narrow seed-pack feature is justified, scoped to curated
  business-rule knowledge only, reusing the install-skill seam, gated through
  prune/grade. Implementation is a separate ticket (out of scope here).
- **If no** → **no feature.** The shipped `generate` / `prune-existing` commands
  already are the answer; document "point SignalForge at your installed package
  models" as the recommended workflow and close the opportunity.

This is an eyes-open recommendation: the popularity and the semantic-test gap are
genuine, but every path to capturing them either violates the product thesis,
hits a dbt mechanical wall, or duplicates a shipped feature — *unless* the value
is narrowed to curated domain knowledge and delivered through the existing
prune-and-grade pipeline, and even then only if a spike proves it beats
`generate`.

---

## References

- `.claude/rules/skill-parity.md` + `signalforge.skill.install_skill`
  (`src/signalforge/skill/__init__.py`) — the copy-out-of-wheel install precedent
  (§C.3).
- `.claude/rules/prune-engine.md`, `.claude/rules/ingest-layer.md` — `prune` /
  `prune-existing` + `--tests-dir` singular-test ingest (the seed-and-prune path).
- `.claude/rules/business-rule-tests.md` — `custom_sql` (= a singular test via
  `ref()`) + the eight test primitives a seed would draw from.
- dbt mechanics: [packages](https://docs.getdbt.com/docs/build/packages),
  [data tests](https://docs.getdbt.com/docs/build/data-tests),
  [ref](https://docs.getdbt.com/reference/dbt-jinja-functions/ref),
  [dbt-core #4157 (model override, unimplemented)](https://github.com/dbt-labs/dbt-core/issues/4157),
  [dbt-utils #924 (cross-package patch warning)](https://github.com/dbt-labs/dbt-utils/issues/924).
- Package metrics: GitHub REST API per repo under `github.com/fivetran/dbt_*`,
  `github.com/snowplow/dbt-snowplow-*`, `github.com/dbt-labs/*` (snapshot
  2026-06-17). dbt Hub ([hub.getdbt.com](https://hub.getdbt.com)) — confirmed to
  publish **no** download counts.
