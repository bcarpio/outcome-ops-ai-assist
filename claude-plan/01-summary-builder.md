# 01 — Summary builder (`lambda/analyze-pr/pr_summary.py`)

## Task
A new module with one public function that turns PR metadata + per-file diffs into
the markdown summary body, plus a captured token-usage object for the cost footer.
No handler edits here, no I/O to SCM — a pure, testable builder + one model call.

Suggested shape:

```python
def build_pr_summary(
    *,
    metadata,              # shared.pr_metadata.PRMetadata
    diffs_by_file,         # Dict[str, str]: path -> unified diff text (may be capped)
    checks_to_run,         # List[str]: human-readable check names already computed
    truncated_files=None,  # List[str]: files whose patch was omitted/capped
) -> tuple[str, object]:   # (markdown_body, usage)  usage has input_tokens/output_tokens
    ...
```

The markdown body has three parts, in order:
1. **"What changed and why"** — 2–4 sentences, model-written, describing ONLY what the
   diff shows.
2. **Per-file table** — `| File | Change | Note |`. One row per entry in
   `metadata.changed_files`. `Change` comes from `metadata.file_change_types[path]`
   (`added|modified|removed|renamed`), NOT from the model. `Note` is an optional
   short model phrase per file (may be empty). A header line states the count as
   `len(metadata.changed_files)` — a code value, never a model number.
3. **"Checks queued: …"** — join `checks_to_run` (already computed upstream by
   `parse_changed_files`). No model involvement.

Model call: `shared.model_client.invoke(workload="pr-analyzer", tier="basic", messages=[...])`.
Capture `resp.usage` (input_tokens/output_tokens) and return it so chunk 02 renders the
cost footer. Only part 1 (and optionally the `Note` column) is model-generated; parts 2's
structure/count and part 3 are deterministic.

## Standards to apply (inlined verbatim — do not go re-fetch)

### ADR-004 — Lambda handler / module standards (this is an internal helper module)
Core requirements (ALL Lambda code):
- Structured logging with handler/module name prefix (e.g. `logger.info("[pr-summary] ...")`).
- AWS clients (if any) initialized once per container, outside the function. (This module
  likely needs none — it calls `model_client`.)
- Environment variables loaded at module level.
- Try-except with `exc_info=True` for unexpected errors.
- Type hints on functions.
- Docstrings on the public function and any business-logic helpers.
This module is NOT an API Gateway handler: NO CORS headers, NO JWT auth, NO `_response`
envelope. It is an internal builder invoked in-process by `analyze-pr/handler.py`. Keep it
a plain function returning `(str, usage)`.

### ADR-025 — summarize the diff, not the description (documentation-driven, code-over-docs)
- Describe only what the diff shows. Do NOT invent behaviour that is not present in the
  diff.
- If the PR title/description claims X and the diff disagrees, SAY SO — do not average the
  two or smooth over the disagreement. The diff is the source of truth; the description is
  a claim to check against it.
- The prompt must instruct the model accordingly and must pass the diff as the primary
  evidence, with the title/author as secondary context.

### ADR-032 — list completeness; no fabricated counts (verbatim principle)
From ADR-032: "every cited value, every cited location, every cited symbol, and every
cited count is graph- or source-resolvable to a tool field — never to LLM memory or LLM
arithmetic." The 2026-06-02 failure it codifies: a model emitted a fabricated headline
("65 call sites") that was "correct on every specific line cited, fully wrong on the
aggregate."
Applied here:
- The per-file table is built by enumerating `metadata.changed_files` in code. The model
  does NOT decide which files are in the table, and does NOT compute "N files changed" —
  that number is `len(metadata.changed_files)`.
- When a file's patch was omitted or capped (oversized diff), the output must carry a
  visible truncation signal (a "Note: diff truncated" row/line and a footer note listing
  `truncated_files`), so a reviewer is never shown a clipped walkthrough as if it were
  whole. Truncation must be load-bearing in the output, not left implicit.

### ADR-035 / ADR-016 — model client + cost attribution
- Call via `from shared.model_client import invoke`. Unified response surface: `resp.text`
  (first text block), `resp.usage.input_tokens`, `resp.usage.output_tokens`,
  `resp.stop_reason`.
- Pass `workload="pr-analyzer"`, `tier="basic"` so Bedrock spend is tagged to the right
  app-inference-profile (ADR-016 workload attribution). Do not hardcode a model id.
- Return the usage object; chunk 02 renders the collapsed cost footer from it, matching the
  existing check comments' usage section.

## Large-diff handling
- Cap total included patch bytes (reuse the existing truncation-note pattern the checks use
  for oversized diffs). When capped, populate `truncated_files` and emit the ADR-032
  truncation signal described above. Never silently drop files from the table — a capped
  file still appears as a row (its `Note` says the diff was omitted).

## Done when
- `build_pr_summary` returns a markdown body whose table enumerates exactly
  `metadata.changed_files` with statuses from `file_change_types`, a count equal to
  `len(changed_files)`, a "Checks queued" line from `checks_to_run`, and a model-written
  prose paragraph grounded in the diff.
- Truncation is signalled when input is capped.
- Usage is captured and returned.
- Unit coverage lands in chunk 03 (do not inline tests here).
