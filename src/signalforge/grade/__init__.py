"""SignalForge quality grader — score LLM-drafted artefacts via LLM-as-judge.

This subpackage implements the README's "evaluation in the loop" architectural
commitment at the post-prune boundary: every kept candidate (column doc, test,
or model description) goes through a configurable rubric and is scored by a
second LLM call. Rubric design and the judge prompt template land in later
US-00x stories; this scaffold only defines the typed exception hierarchy.

**Safety boundary, by design (DEC-013).** The PII safety redaction boundary
established by issue #4 closed at *draft time* — `signalforge.safety` redacts
column names and values before the drafting LLM call, and the drafter's
`<MODEL_SQL>` envelope (DEC-007 of #5) is the prompt-injection defence on the
input side. Post-draft, :class:`signalforge.draft.CandidateSchema` carries
*real* column names; the grader sends those real names to the LLM-judge by
design, and writes them into the sidecar JSON the operator reviews. The
`<ARTIFACT>` envelope is the only LLM-prompt defence applied inside the
grader. Re-redaction here would defeat both the rubric (judges need real
names to score documentation quality) and the explainable-diffs commitment
(reviewers need to see what was scored).

See ``plans/super/7-quality-grader.md`` for the full design and the DEC log.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
