# Airflow test-environment spike (#229)

> Status: **decided** · Epic [#228](https://github.com/wjduenow/SignalForge/issues/228) · First child.
> This doc is the contract every later Airflow child references for the version floor,
> the `airflow` pytest marker, the env-var gate, the constraints-pinned install, and
> the CI job shape — so no sibling re-litigates them.

Apache Airflow is not a normal dev dependency: it is heavy, version-pinned via a
per-`(airflow, python)` constraints file, Python-version-sensitive, and split across a
2.x / 3.x packaging boundary. This spike decides the concrete install + test stack and
writes it down. It does **not** ship operator code (that is the *skeleton* child) — it
ships the test target everything else certifies against.

---

## Decisions

### DEC-1 — Airflow version floor + matrix

**`apache-airflow>=2.8,<3` for v0.7; certify against `2.10.4`.**

- 2.8+ gives stable TaskFlow and `dag.test()` (added 2.5) for headless, scheduler-free
  E2E. It is the widest-deployed line in dbt shops today.
- The certified pin is **`2.10.4` on Python 3.11** — the safe, broadly-available target.
- **3.x is deferred, not forgotten.** Airflow 3.x moved import paths and split packaging
  (`apache-airflow-core`, provider packages). Our operators will import from the
  `airflow.*` paths that are stable across late-2.x; the 3.x port is a tracked follow-up
  in the epic, not a v0.7 blocker. We do **not** smoke-test 3.x in v0.7 — adding a
  `"3.0.x"` matrix entry is a one-line change to the gated job (below) when we do.
- **Matrix:** one cell — `airflow 2.10.4 × python 3.11`. Airflow's tree is too heavy and
  version-pinned to fan out across our full 3.11/3.12/3.13 matrix; one certified cell is
  the signal. (The default `lint-test` matrix stays 3.11–3.13 and never imports Airflow.)

### DEC-2 — Packaging: the `[airflow]` optional extra installs under constraints

The core install (`pip install signalforge-dbt`) **never** gains an unconditional Airflow
dependency (epic guardrail). Airflow ships behind the `[airflow]` optional extra (defined
by the *skeleton* child). The constraints-pinned incantation that installs the extra
cleanly on Python 3.11 is recorded under **[Local setup](#local-setup)** below; the gated
CI job (**[CI](#ci)**) exercises it on every relevant PR.

> Why a constraints file at all: installing `apache-airflow` un-constrained routinely
> breaks resolution because Airflow pins a large transitive tree. The constraints file
> published per `(airflow, python)` pair is the only supported way to get a reproducible
> install.

### DEC-3 — The `airflow` pytest marker (gated, like `snowflake` / `e2e`)

Registered in `pyproject.toml` `[tool.pytest.ini_options].markers` **and** added to the
default `addopts` exclusion (`-m '... and not airflow'`). A plain `uv run pytest` must
never import Airflow. Belt-and-suspenders gating per `.claude/rules/testing-signal.md`:

1. `pytestmark = pytest.mark.airflow` on every Airflow test module (deselected by default).
2. A runtime `pytest.importorskip("airflow", ...)` so a maintainer running `-m airflow`
   in an env without Airflow gets a clear skip-with-reason, not an import error.

Run the gated tests with:

```bash
uv run --no-sync pytest -m airflow --no-cov
```

`--no-cov` because `--cov-fail-under=80` in `addopts` fails marker-only runs that exercise
a fraction of the codebase (the `snowflake` / `wheel_smoke` precedent). `--no-sync` so uv
doesn't re-resolve and clobber the hand-built constraints-pinned Airflow venv.

`tests/airflow/` is also added to `[tool.pyright].exclude`: Airflow is deliberately not a
typecheck dependency, so the default `uv run pyright` stays green without installing it.

### DEC-4 — CI gating: a separate, path-filtered job (not in `lint-test`)

A dedicated `airflow` job in `.github/workflows/ci.yml`, sibling to `lint-test`, **opt-in**
so it doesn't run on every push. Trigger: PRs carrying the `airflow` label **or**
`workflow_dispatch`. Rationale for label-gating over an always-on path filter: the heavy
constraints install is wasteful on unrelated PRs, and the epic's own children already wear
the `airflow` label, so the job fires exactly when it's relevant. (A `paths:` filter on
`signalforge/airflow/**` is the documented alternative if label discipline slips.)

The parse + fake-backed legs run unconditionally inside that job; the live-DAG leg
self-skips without the live env (DEC-5).

### DEC-5 — Fake-first test split; one live E2E DAG

Most operator tests need **no** warehouse and **no** LLM. They assert:

- argv construction handed to the `signalforge` CLI/library seam,
- the four-tier exit-code → Airflow task-state mapping
  (success / `AirflowSkipException` / `AirflowFailException`),
- the XCom push shape (diff sidecar tier counts).

These run against fakes, unconditionally, in the gated CI job. The **only** piece needing
real Airflow + warehouse + LLM is the live E2E DAG (the epic's *test+docs* child, "A8"),
which self-skips without `SF_RUN_AIRFLOW=1` plus the existing warehouse/LLM env gates.

**Env-var contract** (reuses existing gates — nothing new beyond `SF_RUN_AIRFLOW`):

| Var | Purpose |
|---|---|
| `SF_RUN_AIRFLOW=1` | opt into the live-DAG leg (belt-and-suspenders with the marker) |
| `SF_RUN_BQ=1` + `GOOGLE_CLOUD_PROJECT` | warehouse leg (existing BigQuery gate) |
| `ANTHROPIC_API_KEY` | LLM leg (existing drafter/grader gate) |
| `AIRFLOW_HOME`, `AIRFLOW__CORE__DAGS_FOLDER` | point standalone Airflow at `examples/airflow` |

**In-process vs. subprocess (decision input for the operator children):** the **fast
default is `DagBag` (parse) / `dag.test()` (execute)** — in-process, no scheduler. It is
what the gated CI job runs and the right call for parse + fake-backed legs. A subprocess
`airflow standalone` is only for a maintainer eyeballing the DAG in the UI; the live E2E
child decides whether its real-credential leg also wants a subprocess (likely not —
`dag.test()` executes a full DAG end-to-end in one process).

> **DB-init split (certification finding, see below).** The **parse** leg needs **no
> metadata DB** — read `DagBag(...).import_errors` + the in-memory `DagBag(...).dags`
> dict. Do **not** use `bag.get_dag(dag_id)` for the parse leg: `get_dag()` consults the
> ORM (`DagModel.get_current`), which requires `airflow db init` and fails with
> `sqlite3.OperationalError: no such table: dag` in a fresh venv. The **execute** leg
> (`dag.test()`) does initialise/need a backend — that's the live E2E child's concern,
> not the parse certification's.

---

## Local setup

### A. Constraints-pinned local Airflow (isolated venv)

```bash
# Pick the (airflow, python) pair. 2.10.4 on 3.11 is the certified target (DEC-1).
AIRFLOW_VERSION=2.10.4
PYTHON_VERSION=3.11
CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# Isolated venv so Airflow's heavy tree never pollutes the dev env (uv sync --dev).
uv venv .venv-airflow --python ${PYTHON_VERSION}
uv pip install --python .venv-airflow \
  "apache-airflow==${AIRFLOW_VERSION}" \
  --constraint "${CONSTRAINTS}"
# SignalForge editable. The --constraint is LOAD-BEARING: without it uv upgrades
# protobuf 4->5 (and pydantic, requests, typing-extensions) and risks breaking
# Airflow 2.10.4. With it, signalforge resolves cleanly inside Airflow's pins.
# Use `-e .` until the skeleton child (epic #228) ships the `[airflow]` extra,
# then `-e ".[airflow]"`.
uv pip install --python .venv-airflow -e . --constraint "${CONSTRAINTS}"
```

> `.venv-airflow/` is git-ignored scratch — it is the maintainer's isolated Airflow env,
> never committed.

### B. Run Airflow standalone (eyeball a DAG in the UI)

```bash
export AIRFLOW_HOME="$(pwd)/.airflow-home"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__DAGS_FOLDER="$(pwd)/examples/airflow"
.venv-airflow/bin/airflow standalone   # prints the admin password; UI on :8080
```

`examples/airflow/signalforge_generate_dag.py` should appear as `signalforge_generate`.

### C. Headless DAG-parse certification (the acceptance signal)

```bash
# Inside the constraints-pinned venv:
.venv-airflow/bin/python -m pytest -m airflow --no-cov tests/airflow/
# or: uv run --no-sync --python .venv-airflow pytest -m airflow --no-cov
```

`tests/airflow/test_dag_parse.py` loads `examples/airflow/` via `DagBag`, asserts zero
import errors, and that `signalforge_generate` parses with its `generate`/`gate` tasks — proving the
constraints-pinned install and our DAG-authoring pattern are compatible.

### D. Operator unit test (no scheduler, no warehouse — the *skeleton*/operator children)

Construct the operator and call `.execute(context)` directly against a fake `signalforge`
invocation; assert argv, the exit-code → task-state mapping, and the XCom push shape. No
Airflow scheduler, no metadata DB, no warehouse.

---

## CI

A dedicated job in `.github/workflows/ci.yml`, gated so it doesn't run on every push.
SHAs below are the repo's already-pinned versions (`ci-supply-chain.md`):

```yaml
  airflow:
    # Opt-in: PRs labelled `airflow`, or manual dispatch. NOT in the default
    # 3.11/3.12/3.13 lint-test matrix — Airflow's tree is too heavy + version-pinned.
    if: contains(github.event.pull_request.labels.*.name, 'airflow') || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        airflow-version: ["2.10.4"]   # add "3.0.x" when the 3.x story is decided (DEC-1)
        python-version: ["3.11"]
    steps:
      - name: Checkout
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
        with:
          persist-credentials: false
      - name: Set up uv
        uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b  # v8.1.0
        with:
          python-version: "${{ matrix.python-version }}"
          enable-cache: false
      - name: Install Airflow + SignalForge under constraints
        run: |
          CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${{ matrix.airflow-version }}/constraints-${{ matrix.python-version }}.txt"
          uv venv
          uv pip install "apache-airflow==${{ matrix.airflow-version }}" --constraint "$CONSTRAINTS"
          # `-e .` until the skeleton child ships the `[airflow]` extra (then `.[airflow]`).
          # --constraint is load-bearing (keeps Airflow's protobuf/pydantic pins).
          uv pip install -e . --constraint "$CONSTRAINTS"
          # Test tooling under the same constraints — the `[airflow]` extra carries no
          # test deps, so without pytest-cov the `--no-cov` flag is unrecognised.
          uv pip install pytest pytest-cov pytest-asyncio --constraint "$CONSTRAINTS"
      - name: Airflow operator + DAG-parse tests (gated marker)
        # Run in the env we just populated (NOT `uv run`, which targets a fresh sync).
        # `--no-cov` because `--cov-fail-under` in addopts fails marker-only runs.
        run: .venv/bin/python -m pytest -m airflow --no-cov
```

Load-bearing CI notes:

- **SHA-pin every third-party action** + `enable-cache: false` per `ci-supply-chain.md`
  (the public-repo cache-poisoning vector).
- `--no-cov` — `--cov-fail-under` in `addopts` fails marker-only runs.
- `--no-sync` — keep the hand-installed constrained Airflow; don't let uv re-resolve.
- The live-credential leg self-skips without `SF_RUN_AIRFLOW=1` + warehouse/LLM env; CI
  runs parse + fake-backed legs unconditionally, the live leg only when secrets exist.

---

## What this spike ships

- This doc — the recorded decisions + reproducible local/CI steps.
- The `airflow` marker registered in `pyproject.toml` (gated out of default `addopts`),
  and `tests/airflow` excluded from pyright.
- `examples/airflow/signalforge_generate_dag.py` — the shipped example DAG (a `PythonOperator`
  wrapping `signalforge generate`, exit-code→task-state, XCom tier counts, a `gate` task).
  Promoted from the spike's original inline-placeholder DAG; see `docs/airflow-ops.md`.
- `tests/airflow/test_dag_parse.py` — the gated `DagBag` parse certification.

## What it does NOT ship (later children)

- The `[airflow]` optional extra + `signalforge.airflow` package skeleton (*skeleton* child).
- Real operators, the result→task-state/XCom contract, the connection hook, drift mode.
- The live E2E DAG + `docs/airflow-ops.md` + MkDocs nav (*test+docs* child).

## Certification results (2026-06-15, py3.11)

Certified live on an isolated `uv venv .venv-airflow --python 3.11`:

- ✅ **DEC-2 install** — `apache-airflow==2.10.4` installs cleanly under
  `constraints-2.10.4/constraints-3.11.txt` (no resolution breakage).
- ✅ **Parse** — `examples/airflow/signalforge_generate_dag.py` parses with
  `DagBag(...).import_errors == {}`; `signalforge_generate` + its `generate`/`gate`
  tasks appear in `DagBag(...).dags`.
- ✅ **Gated test** — `pytest -m airflow --no-cov tests/airflow/` → `1 passed` under
  real Airflow 2.10.4.
- 🔧 **Finding (folded in):** the parse leg must read `bag.dags`, not `bag.get_dag()`
  (the latter needs `airflow db init`) — see the DB-init note in DEC-5; the test was
  fixed accordingly.
- 🔧 **Finding (folded in):** the airflow env needs pytest/pytest-cov/pytest-asyncio
  installed for `--no-cov` to be a valid flag — added to the CI install step.
- 🔧 **Finding (folded in):** the SignalForge editable install **must** carry
  `--constraint`. Without it, `google-cloud-bigquery` drags protobuf 4→5 (plus pydantic /
  requests / typing-extensions upgrades) over Airflow 2.10.4's pins — it "works" for the
  parse leg but is fragile. With `--constraint`, signalforge resolves cleanly *inside*
  Airflow's pins (protobuf stays 4.25.5, pydantic 2.10.3); `airflow` + `signalforge` both
  import and the DAG parses. Certified.
- 🔧 **Finding (folded in):** the original `uv pip install --system …` + `uv run --no-sync`
  recipe was inconsistent — `uv run` targets the project `.venv`, not the `--system` env.
  CI now does `uv venv` → constrained `uv pip install` → `.venv/bin/python -m pytest`
  (the exact invocation shape certified here).
- ⚠️ **Benign:** Airflow's constraints pin an older pytest (7.x) that emits
  `PytestConfigWarning: Unknown config option: strict_markers`. Harmless — the
  `--strict-markers` *flag* in `addopts` still applies; only the pytest-9 `strict_markers`
  *ini bool* is unread on the older pytest.

## Maintainer certification checklist

The acceptance bar for #229 is "a maintainer can reproduce a local Airflow run and the
gated CI job from this doc." To certify (one-time, on a machine with Python 3.11):

- [ ] Run [Local setup A](#a-constraints-pinned-local-airflow-isolated-venv) — install succeeds under the constraints file.
- [ ] Run [C](#c-headless-dag-parse-certification-the-acceptance-signal) — `tests/airflow/` passes (zero import errors, `signalforge_generate` parses).
- [ ] Optionally run [B](#b-run-airflow-standalone-eyeball-a-dag-in-the-ui) — `signalforge_generate` shows in the UI.
- [ ] Wire the [CI job](#ci) into `.github/workflows/ci.yml` (can land with the skeleton child).
