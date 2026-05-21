# ADR-026: Stable coverage checklist for audit-shaped questions

## Status: Accepted

## Context

Two runs of the *same* security-audit question, against the same
workspace and code, produced **different** finding sets — Run 1 flagged
rate limiting and refresh-token rotation as gaps; Run 2 silently dropped
both and assessed transport security and audit logging instead. Whatever
chunks rose to the top of retrieval drove the findings, so coverage
drifted with retrieval. For a regulated-buyer pitch (`project_moat_audit`),
non-reproducible audit output is disqualifying — auditors need the same
report every time, not a different one each Tuesday.

The cause is that audit-shaped questions were answered via free-form
synthesis with no fixed coverage contract.

## Decision

When the router classifies a question as **HYBRID** *and* the question
is **audit-shaped**, inject a stable coverage checklist into the
synthesis context. The model fills the table cells; the row set is fixed.

### Key Points

- **Post-routing detection, not a 6th intent.** `is_audit_shaped()` is a
  conservative regex/keyword classifier (`lambda/chat/context_builder.py`)
  applied after the router returns HYBRID. This keeps the router's
  5-intent contract stable (ADR-024 already widened HYBRID), needs no
  Bedrock call, and treats the checklist as a *rendering* concern —
  retrieval already fans out correctly for HYBRID.
- **Two-layer gate:** (1) router → HYBRID; (2) context builder →
  HYBRID + audit-shape → inject `build_audit_coverage_checklist_block()`.
- **Fixed row set** (`AUDIT_COVERAGE_ROWS`, configurable in one place for
  future per-vertical SOC 2 / HIPAA / PCI variants): token expiry,
  single-use, replay, rate limiting, refresh/rotation, hashing,
  transport, audit logging, secret storage, session management.
- **Empty cell is a finding.** The prompt instructs the model to write
  "(not in retrieved context)" + mark the row "not verified" when there's
  no evidence — and to NEVER invent evidence. A silently-dropped control
  (Run 2's "rate limiting") becomes an explicit "not verified" row.
- **Verdict follows the rows**, never a SAST grade alone (ties to
  ADR-025's SAST-scope rule).
- **Conservative detector** — requires explicit verification framing
  ("is X secure", "audit", "gaps in", "does X meet <standard>"), not a
  plain "how does X work" explanation, to avoid over-structuring
  ordinary questions.

## Consequences

### Positive

- Reproducible audit output: the row set is stable across runs even as
  retrieval varies.
- Surfaces what was missed instead of silently dropping controls.
- One-place configurability for vertical-specific row sets later.

### Tradeoffs

- ~400 prompt tokens on audit-shaped turns (low volume; negligible).
- Detector false positives/negatives — tuned conservatively; unit tests
  pin positive + negative shapes. Cell-content quality is a live eval.
- Risk the model invents evidence to fill rows — mitigated by explicit
  "never invent / mark not verified" instruction.

## Implementation

### Starting today

1. `is_audit_shaped()`, `AUDIT_COVERAGE_ROWS`,
   `build_audit_coverage_checklist_block()` in `context_builder.py`.
2. Handler injects the checklist when intent==hybrid and the question is
   audit-shaped.
3. Unit tests: detector positive/negative shapes; checklist determinism +
   complete row set + "never invent" rules.

### Next phases

1. Live stability eval — run the same audit question twice against
   `app.retrieveit.ai` and diff the row headers (must match).
2. Vertical-specific row sets (SOC 2 CC-mapped, HIPAA, PCI) driven by
   `project_gtm_strategy`.
3. Multi-question audit reports (orchestrating many rows across turns).

## Related ADRs

- ADR-024: Router verification-vs-definition (gets the question to
  HYBRID).
- ADR-025: Synthesis guardrails (SAST scope — the verdict comes from the
  checklist, not a SAST grade).

## References

- `lambda/chat/context_builder.py`
  (`is_audit_shaped`, `AUDIT_COVERAGE_ROWS`,
  `build_audit_coverage_checklist_block`)
- `lambda/chat/handler.py` (injection at the synthesis-assembly site)
- `lambda/tests/unit/test_chat_context_builder.py`
  (`TestAuditCoverageChecklist`)

Version History:

- v1.0 (2026-05-21): Initial decision — stable coverage checklist for
  audit-shaped HYBRID questions.

<!-- Confluence sync -->
