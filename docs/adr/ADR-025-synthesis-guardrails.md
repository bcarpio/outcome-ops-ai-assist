# ADR-025: Chat synthesis guardrails — code-over-docs + SAST scope

## Status: Accepted

## Context

Two synthesis-discipline failures surfaced when demoing security-audit
questions against the chat, both downstream of retrieval (the right
context was retrieved; the model used it wrong):

1. **Doc-vs-code drift, doc wins.** The customer repo has
   `MAGIC_LINK_EXPIRY = 3600` in code and a security overview saying
   "links expire in 15 minutes." Both landed in the RAG context. The
   model confidently answered "15 minutes" — perpetuating the drift
   instead of catching it. For a regulated-buyer pitch whose value prop
   is *catching* drift (`project_moat_audit`), this is the worst shape.

2. **SAST grade used as design-security exoneration.** When SonarQube
   tool output ("security rating A, 13 hotspots SAFE") was in context,
   the model framed the auth flow as "Strong Implementation" and dropped
   real gaps (missing rate limiting, refresh rotation, replay
   protection) that a SAST-free run had found. SAST cannot see those
   design properties; their absence from SAST output is not evidence.

Neither is a router or graph problem — the retrieval was correct. The
synthesis prompt simply had no rule for context that contradicts itself,
nor for scoping SAST signals.

## Decision

Add two discipline rules to the chat synthesis prompt
(`lambda/shared/prompts.py`, `build_chat_system_prompt`).

### Key Points

- **Code over docs on value conflicts** (`CODE_OVER_DOCS_RULES`): when
  code and prose disagree about a *specific value* (expiry, limit,
  threshold, hash algorithm, version, schema field), cite the code as
  authoritative (with file:line), surface the prose claim as an explicit
  **documentation drift** finding, and never average/hedge/pick the more
  authoritative-sounding one. Scoped to specific-value disagreements so
  the model doesn't flag stylistic differences.

- **SAST scope** (`SAST_SCOPE_RULES`): SAST/SonarQube covers syntactic
  code-smell rules + known-CVE versions only. It does NOT cover
  design-level security (rate limiting, refresh rotation, replay
  protection, authz-model correctness, session depth, business-logic
  auth gaps). Treat SAST as one input to one audit row — never the
  overall verdict, never exoneration for properties it can't detect —
  while still surfacing genuine SAST findings.

Both rules live as named constants and are injected into the chat system
prompt. They are intentionally NOT added to `build_rag_system_prompt`
(ask-claude) for now; the failures were observed on the chat path and
that's where audit-shaped questions land.

## Consequences

### Positive

- Drift becomes a *finding*, matching the platform's audit value prop.
- SAST stops crowding out real design-security gaps.
- Robust to customers having stale docs (we never depend on doc
  freshness) — the discipline is in our synthesis, not their docs.

### Tradeoffs

- ~300 extra prompt tokens per chat turn — negligible at per-turn
  pricing.
- Over-flagging risk (model calls minor wording "drift", or becomes
  dismissive of SAST). Mitigated by scoping language ("specific value",
  "one input not the verdict") and unit coverage; live behavior is a
  post-deploy eval.

## Implementation

### Starting today

1. `CODE_OVER_DOCS_RULES` + `SAST_SCOPE_RULES` constants injected into
   `build_chat_system_prompt`.
2. Unit tests assert the chat prompt carries both rules and still
   carries the base FORMATTING/RULES sections.

### Next phases

1. E2E behavior test against the live `app.retrieveit.ai` workspace
   (the magic_link / Sonar-A query) — unit tests assert prompt assembly;
   only a live model call asserts behavior.
2. Consider porting the rules to ask-claude if drift/SAST misuse shows
   up on the synchronous RAG path.
3. The SAST rule references "the full coverage checklist" — made
   concrete by the audit-coverage checklist (chunk 05).

## Related ADRs

- ADR-024: Router verification-vs-definition (gets these questions to
  fire the graph in the first place).
- ADR-023: KB retrieval contract.

## References

- `lambda/shared/prompts.py` (`CODE_OVER_DOCS_RULES`, `SAST_SCOPE_RULES`)
- `lambda/tests/unit/test_shared_prompts.py` (`TestSynthesisGuardrails`)

Version History:

- v1.0 (2026-05-21): Initial decision — code-over-docs drift rule + SAST
  scope rule in the chat synthesis prompt.

<!-- Confluence sync -->
