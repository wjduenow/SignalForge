"""Airflow-free pure resolver for the SignalForge Airflow hook (#234).

US-003 of issue #234 (epic #228, v0.7 Airflow operator roadmap). This module is
the **airflow-free heart** of :class:`signalforge.airflow.hooks.SignalForgeHook`:
it turns an Airflow ``Connection`` (plus an injected ``Variable`` lookup) into a
typed :class:`HookResolution` — *without importing apache-airflow at all*.

That airflow-freedom is load-bearing (mirrors ``result.py`` / ``runner.py``):

* The gated hook (US-004) subclasses ``BaseHook`` and is therefore deselected
  from the default coverage run; this pure resolver carries **all** the
  decision logic so it can be unit-tested 100% in the **default** pytest suite
  (the codecov patch gate), with a tiny fake ``conn`` object and a dict-backed
  ``variable_lookup`` — no airflow rig required.
* It keeps the one-shim rule intact (``.claude/rules/airflow-integration.md``):
  the sole ``from airflow ...`` site stays ``_airflow_compat``. This module has
  **none** — verified by ``grep`` and by the import-confinement AST scan.

Design decisions (``plans/super/234-signalforge-hook.md``):

* **DEC-003 — lenient resolver.** ``api_key``/``provider`` come back as ``None``
  when absent; *requiredness is enforced by the consumer* (the Generate operator
  raises when the key is missing; PruneExisting needs neither). One resolver
  serves both operators.
* **DEC-004 — pure + typed result.** ``resolve_connection`` is a module-level
  pure function returning :class:`HookResolution`.
* **DEC-005 — closed provider allowlist.** A non-``None`` ``provider`` is
  validated against :data:`signalforge.llm.providers.PROVIDER_ENV_VAR_KEYS`; an
  unknown provider raises :class:`AirflowConfigError` (closing the
  arbitrary-env-var injection vector even for a Connection that only drives the
  no-LLM prune-existing path).
* **DEC-008 — symlink-hardened ``profiles_dir``.** When a containment anchor
  (``project_dir``) is available the path is routed through
  :func:`signalforge._common.path_safety.canonicalise_path`; otherwise it is
  accepted as-is (the documented bounded-defence gap — mirrors the init-demo
  seam, which also has no natural anchor).
* **DEC-009 — reuse :class:`AirflowConfigError` (tier 2).** Every misconfig
  (extra typo, unknown provider, escaping ``profiles_dir``) raises that one
  class with a case-specific remediation; no new error class.
* **DEC-010 — ``extra`` validated by an ``extra="forbid"`` Pydantic model.** A
  typo key (``cache_scop``) fails loud at resolution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.airflow.errors import AirflowConfigError
from signalforge.llm.providers import PROVIDER_ENV_VAR_KEYS

#: The Airflow Variable key consulted as the API-key fallback when the
#: Connection carries no ``password``. A single fixed, provider-agnostic name
#: keeps the convention trivial to document and works even when ``provider`` is
#: omitted (a prune-existing-only Connection). The gated hook (US-004) wires the
#: ``variable_lookup`` callable to ``Variable.get(key, default_var=None)``, so an
#: absent Variable yields ``None`` and the resolver stays lenient (DEC-003).
API_KEY_VARIABLE_KEY = "signalforge_api_key"


class _ConnectionLike(Protocol):
    """The duck-typed slice of an Airflow ``Connection`` the resolver reads.

    Declared as a :class:`typing.Protocol` so the resolver needs no airflow
    import: any object exposing ``password`` and ``extra_dejson`` satisfies it
    (the real ``airflow.models.Connection`` does; so does a one-line test fake).
    Airflow's ``extra_dejson`` returns ``{}`` for a null/blank ``extra`` and
    never raises, so the resolver can read it unconditionally.
    """

    @property
    def password(self) -> str | None: ...

    @property
    def extra_dejson(self) -> dict[str, object]: ...


@dataclass(frozen=True, repr=False)
class HookResolution:
    """The typed result of resolving a SignalForge Airflow Connection.

    Frozen and airflow-free. Carries what the operators need:
    ``profiles_dir`` (warehouse auth), ``provider`` (which LLM SKU family),
    ``api_key`` (the credential), and ``cache_scope`` (the Anthropic
    cached-prefix knob — precedence-merged by the Generate operator, DEC-012).
    All are ``None``-able — the resolver is lenient (DEC-003); the consuming
    operator enforces requiredness.

    The custom :meth:`__repr__` is a **leak-surface discipline** (DEC-007): it
    renders ``profiles_dir`` and ``provider`` only, and NEVER the ``api_key``
    value *or* a field-name label that would reveal a credential is present —
    mirroring ``SnowflakeAdapter.__repr__`` (which shows only ``account`` +
    ``warehouse``) and ``DbtProfileTarget``'s ``Field(repr=False)`` secrets. A
    debug-print / ``%r`` log line therefore cannot leak the key.
    """

    profiles_dir: str | None
    provider: str | None
    api_key: str | None
    cache_scope: str | None = None

    def __repr__(self) -> str:
        # Deliberately omits ``api_key`` entirely — no value, no label.
        return f"HookResolution(profiles_dir={self.profiles_dir!r}, provider={self.provider!r})"


class _ConnectionExtra(BaseModel):
    """Typed schema for an Airflow Connection's ``extra`` JSON (DEC-010).

    ``extra="forbid"`` so a typo key (``cache_scop`` for ``cache_scope``) fails
    loud at resolution rather than silently no-op'ing — the same fail-loud
    posture every other user-input config model in the project takes
    (``.claude/rules/safety-layer.md`` § ``extra="forbid"``). ``frozen=True``
    because the parsed extra is read-only once validated. Every field is
    optional: a prune-existing-only Connection legitimately omits ``provider``,
    and a Connection relying on the ambient ``DBT_PROFILES_DIR`` omits
    ``profiles_dir``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profiles_dir: str | None = None
    provider: str | None = None
    cache_scope: str | None = None


def resolve_connection(
    conn: _ConnectionLike,
    *,
    variable_lookup: Callable[[str], str | None],
    project_dir: str | Path | None = None,
) -> HookResolution:
    """Resolve an Airflow Connection (+ Variable lookup) to a :class:`HookResolution`.

    Pure and airflow-free. ``conn`` is duck-typed (``.password`` +
    ``.extra_dejson``); ``variable_lookup`` is injected by the caller (the gated
    hook wires it to ``Variable.get(key, default_var=None)``) so this function
    never touches ``airflow.models.Variable`` directly.

    Resolution rules:

    * **extra** — parsed through :class:`_ConnectionExtra`; a typo / unknown key
      raises :class:`AirflowConfigError` (DEC-010).
    * **api_key** — primary source is ``conn.password``; when that is falsy the
      fallback is ``variable_lookup(API_KEY_VARIABLE_KEY)``. The resolver is
      **lenient**: if neither yields a key, ``api_key`` is ``None`` and no error
      is raised (DEC-003) — the consumer enforces requiredness.
    * **provider** — read from the validated ``extra``; may be ``None``. When
      present it MUST be a key of
      :data:`~signalforge.llm.providers.PROVIDER_ENV_VAR_KEYS`, else
      :class:`AirflowConfigError` (the closed allowlist, DEC-005). The check
      runs regardless of consumer, so an arbitrary env-var name can never be
      derived from operator-supplied config.
    * **profiles_dir** — read from the validated ``extra``; may be ``None``. When
      present *and* a ``project_dir`` anchor is supplied, it is symlink-hardened
      via :func:`canonicalise_path`; a containment escape (or a bare ``OSError``
      from resolution) raises :class:`AirflowConfigError` (DEC-008). When no
      ``project_dir`` is given the path is accepted as-is (the documented
      bounded-defence gap — there is no anchor to contain against).
    * **cache_scope** — read from the validated ``extra`` and carried through
      unchanged (may be ``None``). The Generate operator precedence-merges it
      with its own ``cache_scope`` param (DEC-012); other consumers ignore it.

    Raises:
        AirflowConfigError: on any misconfiguration (DEC-009 — the one reused
            tier-2 class; no new error type).
    """
    extra_dict = conn.extra_dejson
    try:
        extra = _ConnectionExtra.model_validate(extra_dict)
    except ValidationError as exc:
        raise AirflowConfigError(
            "The SignalForge Connection `extra` JSON is invalid.",
            remediation=(
                "The Connection `extra` accepts only `profiles_dir`, `provider`, "
                "and `cache_scope` (all optional). Remove any unknown key (a typo "
                "like `cache_scop` fails loud) and ensure each value is a string. "
                f"Validation error: {exc}"
            ),
        ) from exc

    # provider — closed allowlist when present (DEC-005).
    provider = extra.provider
    if provider is not None and provider not in PROVIDER_ENV_VAR_KEYS:
        allowed = ", ".join(sorted(PROVIDER_ENV_VAR_KEYS))
        raise AirflowConfigError(
            f"The SignalForge Connection `extra.provider` is not a known provider: {provider!r}.",
            remediation=(
                f"Set `extra.provider` to one of: {allowed}. Leave it unset for a "
                "prune-existing-only Connection that makes no LLM call."
            ),
        )

    # api_key — password primary, Variable fallback; lenient (DEC-003).
    api_key = conn.password or None
    if api_key is None:
        api_key = variable_lookup(API_KEY_VARIABLE_KEY) or None

    # profiles_dir — symlink-hardened when an anchor is available (DEC-008).
    profiles_dir = extra.profiles_dir
    if profiles_dir is not None and project_dir is not None:
        try:
            resolved = canonicalise_path(profiles_dir, Path(project_dir))
        except (PathContainmentError, OSError) as exc:
            raise AirflowConfigError(
                "The SignalForge Connection `extra.profiles_dir` could not be "
                "resolved safely inside the project directory.",
                remediation=(
                    "`extra.profiles_dir` must resolve to a path inside the "
                    "operator's `project_dir` (symlink-hardened). Point it at a "
                    "directory containing your dbt `profiles.yml`. "
                    f"Resolution error: {exc}"
                ),
            ) from exc
        profiles_dir = str(resolved)

    return HookResolution(
        profiles_dir=profiles_dir,
        provider=provider,
        api_key=api_key,
        cache_scope=extra.cache_scope,
    )


__all__ = [
    "API_KEY_VARIABLE_KEY",
    "HookResolution",
    "resolve_connection",
]
