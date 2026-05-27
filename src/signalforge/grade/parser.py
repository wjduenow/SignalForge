"""Single-criterion LLM-judge response parser (US-006).

Per #7 DEC-004 the grader makes one LLM call per ``(artifact, criterion)``
pair, so the anchor contract is single-key:
``returned.criterion_id == sent.criterion_id``. There is at most one
anchor violation per call, but the error class shape mirrors the
drafter's whole-draft fail-loud precedent (#5 DEC-003 / DEC-022) so a
caller branching on ``GradeOutputError.violation_type`` does not need to
distinguish the grader's response surface from the drafter's.

The parser converts a raw LLM-response text into a typed
:class:`signalforge.grade.models.GradingResult`:

1. Strip surrounding whitespace and a single optional Markdown code
   fence (the model occasionally wraps its JSON in `````json ...
   `````), then extract the embedded JSON value via
   :func:`signalforge._common.json_payload.extract_json_payload` — the
   judge can narrate a prose preamble before the ``{`` and the model
   does not support an assistant-turn prefill to force JSON-only output
   (issue #144). Extraction decodes at the first ``{``/``[`` only and
   returns the text unchanged when no JSON value is present, so a
   genuinely prose-only / truncated response still fails loud at step 2.
2. ``json.loads`` the extracted text. A :class:`json.JSONDecodeError`
   raises :class:`GradeOutputError` with
   ``violation_type="json_parse"``; a top-level non-object payload
   (list / scalar / etc.) likewise raises ``violation_type="json_parse"``
   because a top-level non-object is a parse-shape failure rather than a
   missing-field failure.
3. Validate the dictionary's required keys (``criterion_id``, ``score``,
   ``passed``) and the optional string keys (``evidence``, ``reasoning``,
   default empty string).
4. Single-key anchor: returned ``criterion_id`` must equal the
   ``criterion.id`` the prompt was authored against.
5. Score-shape: ``None`` is allowed (DEC-015 degraded path); otherwise
   the value must be a non-bool ``int``/``float``, finite, in
   ``[0.0, 1.0]``. ``True``/``False`` are rejected as
   ``score_not_a_number`` because :class:`bool` is a subclass of
   :class:`int` in Python and would otherwise sneak past a naive
   ``isinstance(value, (int, float))`` check.
6. ``passed`` must be a strict :class:`bool` (no ``0``/``1``/``"true"``).

Every malformed shape raises :class:`GradeOutputError` carrying a
``violation_type`` from the locked nine-value
:data:`GradeOutputViolationType` literal taxonomy. The parser performs
**no I/O**; the audit-write seam (US-007) wraps successful round-trips
only — bad-LLM-response drops do not write a receipt (mirrors #5
DEC-013).
"""

from __future__ import annotations

import json
import math

from signalforge._common.json_payload import extract_json_payload
from signalforge.grade.errors import GradeOutputError
from signalforge.grade.models import GradingResult
from signalforge.grade.rubric import Criterion

_REQUIRED_KEYS: tuple[str, ...] = ("criterion_id", "score", "passed")


def _strip_code_fence(text: str) -> str:
    """Strip a single optional Markdown code fence wrapping a JSON body.

    Handles the two common shapes the LLM occasionally emits:

    * `````json\\n{...}\\n`````
    * `````\\n{...}\\n`````

    The text is whitespace-stripped first. If the result starts with
    ``````` and ends with ```````, the
    surrounding fence (and optional leading language tag on the opening
    line) is removed and the inner body is returned trimmed. Otherwise
    the input is returned unchanged. Tolerant of common wrappers only —
    no attempt is made to extract JSON from arbitrary prose.
    """
    stripped = text.strip()
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return stripped
    inner = stripped[3:-3]
    # Drop the optional language tag on the opening line (e.g. ``json``)
    # by chopping everything up to and including the first newline. The
    # JSON body itself never starts with a non-newline opener after the
    # fence in the shapes we accept.
    newline_idx = inner.find("\n")
    if newline_idx == -1:
        return inner.strip()
    return inner[newline_idx + 1 :].strip()


def parse_grade_response(
    response_text: str,
    *,
    artifact_id: str,
    criterion: Criterion,
) -> GradingResult:
    """Parse one LLM-judge response into a typed :class:`GradingResult`.

    Single-criterion anchor contract (#7 DEC-004): the LLM was asked to
    score ``artifact_id`` against ``criterion``. The response is a
    single JSON object with keys ``criterion_id``, ``score``, ``passed``,
    ``evidence`` (optional), ``reasoning`` (optional). Returns a fully
    validated :class:`GradingResult`; raises :class:`GradeOutputError`
    with a ``violation_type`` from the locked taxonomy on every
    malformed shape.
    """
    # Strip a Markdown code fence, then extract the embedded JSON object —
    # the judge (claude-sonnet-4-6) can narrate a prose preamble before the
    # `{`, and the model does not support an assistant-turn prefill to force
    # JSON-only output (issue #144). `extract_json_payload` returns the text
    # unchanged when no JSON value is present so the error path still fires.
    cleaned = extract_json_payload(_strip_code_fence(response_text))
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GradeOutputError(
            f"LLM-judge response was not valid JSON: {exc.msg}",
            violation_type="json_parse",
        ) from exc

    if not isinstance(payload, dict):
        raise GradeOutputError(
            f"LLM-judge response top level must be a JSON object; got {type(payload).__name__}",
            violation_type="json_parse",
        )

    if "criterion_id" not in payload:
        raise GradeOutputError(
            "LLM-judge response missing required key 'criterion_id'.",
            violation_type="missing_criterion_id",
        )

    for key in _REQUIRED_KEYS:
        if key == "criterion_id":
            continue
        if key not in payload:
            raise GradeOutputError(
                f"LLM-judge response missing required key {key!r}.",
                violation_type="missing_required_field",
            )

    returned_criterion_id = payload["criterion_id"]
    if returned_criterion_id != criterion.id:
        raise GradeOutputError(
            f"LLM-judge returned criterion_id {returned_criterion_id!r} "
            f"but the prompt asked for {criterion.id!r}.",
            violation_type="criterion_id_mismatch",
        )

    score = payload["score"]
    if score is not None:
        # ``bool`` is a subclass of ``int`` — reject before the int/float
        # acceptance branch so ``True``/``False`` don't sneak past the
        # numeric type check.
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise GradeOutputError(
                f"LLM-judge score must be a number; got {type(score).__name__}: {score!r}.",
                violation_type="score_not_a_number",
            )
        score_f = float(score)
        if not math.isfinite(score_f) or score_f < 0.0 or score_f > 1.0:
            raise GradeOutputError(
                f"LLM-judge score must be a finite number in [0.0, 1.0]; got {score!r}.",
                violation_type="score_out_of_range",
            )
        score = score_f

    passed = payload["passed"]
    if not isinstance(passed, bool):
        # Strict bool — refuse 0/1/"true". The grader's verdict is
        # carried verbatim into the audit log; a coercion here would
        # silently lose the LLM's actual output shape.
        raise GradeOutputError(
            f"LLM-judge 'passed' must be a strict bool; got {type(passed).__name__}: {passed!r}.",
            violation_type="passed_not_a_bool",
        )

    evidence = payload.get("evidence", "")
    if not isinstance(evidence, str):
        raise GradeOutputError(
            f"LLM-judge 'evidence' must be a string when present; "
            f"got {type(evidence).__name__}: {evidence!r}.",
            violation_type="missing_required_field",
        )
    reasoning = payload.get("reasoning", "")
    if not isinstance(reasoning, str):
        raise GradeOutputError(
            f"LLM-judge 'reasoning' must be a string when present; "
            f"got {type(reasoning).__name__}: {reasoning!r}.",
            violation_type="missing_required_field",
        )

    return GradingResult(
        artifact_id=artifact_id,
        criterion_id=returned_criterion_id,
        score=score,
        passed=passed,
        evidence=evidence,
        reasoning=reasoning,
    )


__all__ = ("parse_grade_response",)
