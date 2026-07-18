# ADR-051: PII redaction runs at ingest via Comprehend, not Bedrock Guardrails

## Status: Accepted

- v1.0 (2026-07-18) — Records why PII redaction is an ingest-time Amazon
  Comprehend stage rather than a Bedrock Guardrails filter, and why the raw
  source mirror is deliberately left unredacted. Written to answer the recurring
  security-review question "why not Bedrock Guardrails for PII?" (analogous to
  ADR-045 for Function URLs). No behavior change; documents existing design.

## Context

The platform self-hosts its ingestion + retrieval pipeline — it does NOT use the
Bedrock Knowledge Bases managed service. Content flows:

```
source (git / Confluence / Jira / ...) 
  --(sync worker)-->  RAW text in the S3 KB mirror  (s3://KB_BUCKET/workspaces/{ws}/{provider}/{slug}/{path})
  --(S3 event -> process-documents / process-repo-summary / process-batch-summary)-->
       redact (Comprehend) IN MEMORY  -->  embed (Bedrock Titan)  -->  REDACTED text + vectors in S3 Vectors
chat:  query -> retrieve REDACTED chunks from S3 Vectors -> synthesize (Bedrock/Mantle)
```

The requirement is to keep PII out of the knowledge base that the LLM reasons
over. That is a **data-at-rest / ingest** concern: sanitize content before it
enters the retrievable store. The recurring review question is whether Bedrock
Guardrails could do this instead.

## Decision

1. **Redact at ingest with Amazon Comprehend `DetectPiiEntities`.** The redaction
   stage lives in `process-documents`, `process-repo-summary`, and
   `process-batch-summary` (`lambda/shared/pii.py`). It reads the raw text,
   replaces detected entities with `[REDACTED:TYPE]` placeholders, and only the
   **redacted** text is embedded and written to S3 Vectors. Controls:
   per-workspace `STRICT` / `NONE` mode, an org-level kill switch
   (`pii_redaction_enabled`), and an operator-tunable skip list
   (`pii_redaction_skip_types`) built on Comprehend's entity taxonomy. Every
   `DOCUMENT_INGESTED` audit row records a per-type `redaction_summary` (or a
   `skipped_reason`), feeding the audit dashboard — auditability is the product's
   governance moat.

2. **NOT Bedrock Guardrails, because it is a different layer.** Guardrails filters
   the **model call** — prompt/response on `InvokeModel`/`Converse`, or the
   standalone `ApplyGuardrail` API. It is an inference-time I/O filter; it never
   touches S3 and cannot sanitize data at rest. Using it here would leave RAW
   content in the S3 mirror and the vector store untouched, since the sensitive
   text lands in storage independent of any model call. Comprehend is the right
   tool precisely because it redacts the **text content itself** before it enters
   the retrievable store. Comprehend is also **model/provider-agnostic**, which
   matters given model-swappability (Bedrock <-> Mantle, ADR-035) — Guardrails
   would bind PII handling to the Bedrock path.

3. **The raw source mirror is deliberately NOT redacted.** `read_source`
   (ADR-030) serves faithful source code for line-grounded citations
   (`_fetch_from_s3_mirror`), so redacting the mirror would replace real code with
   `[REDACTED]` and break grounding. The mirror therefore stays raw, protected at
   rest by bucket IAM + KMS encryption, and only the **synthesis/retrieval path**
   (S3 Vectors) is redacted. This is a deliberate fidelity-vs-minimization
   tradeoff, not an oversight.

## Consequences

- The KB **synthesis** path the LLM reasons over is redacted and auditable
  (typed entities + per-type summary per ingest).
- The raw source mirror retains PII/secrets at rest and can surface them via
  `read_source` code citations. This residual risk is accepted and bounded by
  IAM + KMS, in exchange for `read_source` fidelity (see the DPIA).
- **Inference time is a separate, currently-unfiltered layer.** User prompts are
  sent to the model as-is; the retrieved KB context is the already-ingest-redacted
  content, but no additional inference-time redaction is applied (accepted risk —
  Bedrock does not store or train on the data). If a customer ever needs
  inference-time PII/topic filtering, **Bedrock Guardrails is the correct tool for
  that layer** — complementary to, not a replacement for, this ingest stage.

## Alternatives considered

- **Bedrock Guardrails for PII.** Rejected for the ingest goal: it is a model-I/O
  filter and cannot sanitize data at rest or the ingest text before it is stored
  (Decision 2). Retained as a future option for the inference layer.
- **Redact the raw source mirror too.** Rejected: it would corrupt `read_source`
  code citations (Decision 3). At-rest protection is IAM + KMS instead.
- **Bedrock Knowledge Bases managed ingestion.** Not used — the platform runs its
  own ingest -> S3 -> S3 Vectors pipeline, so the redaction stage is ours to place.

## Related

- ADR-030 (read_source adjudicator — the raw-mirror consumer), ADR-035 (model
  swappability), ADR-045 (Function URL posture — the sibling "why not X" review
  artifact). Verification: `docs/pii-redaction-verification.md`. Risk register:
  `docs/dpia.md` (R11).
