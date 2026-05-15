# Symbol-Graph Retrieval — Validation Runbook

This runbook walks the operator through the 8 validation queries in
`plan-gkg/08-rollout-and-validation.md` that gate the symbol-graph
feature's "v1 is done" milestone.

Run all validations **after** `terraform apply` of the chunk-08
changes lands and **after** the per-workspace backfill script
(`scripts/backfill_symbol_graph.py`) finishes for the target
workspace.

The owner runs this runbook against live deployments — the platform
code-side work is done. Each validation has:

- **Input** — what to type / commit / open.
- **Expected output** — concrete shape the answer must take.
- **Verification command** — ground-truth check the operator runs
  locally so the live answer can be compared against it.
- **Fail signal** — what counts as a regression.

If any validation fails, follow the **Rollback** section at the
bottom of this doc before investigating root cause.

---

## Audit-row checks (every validation)

In addition to the per-validation expectations below, every chat turn
issued during validation must produce **two** audit rows that the
operator confirms before signing off (plan-gkg chunk 09 — the audit
story is part of the v1 definition of done, not optional):

1. **Cost row** — Query the audit table for `lambda_name="chat:router"`
   with a timestamp inside the validation window. Confirms the Haiku
   classifier cost was attributed to `chat:router` (not buried inside
   the chat total) and that the per-workspace spend dashboard splits
   router cost as its own line.

   ```bash
   # Spot-check the latest router cost row landed.
   aws dynamodb scan \
     --table-name ${env}-${app_name}-audit-logs \
     --filter-expression "lambda_name = :ln" \
     --expression-attribute-values '{":ln": {"S": "chat:router"}}' \
     --max-items 5 \
     --projection-expression "#ts, lambda_name, model_id, cost_usd, flagged_reason" \
     --expression-attribute-names '{"#ts": "timestamp"}'
   ```

   The `flagged_reason` column carries `q_sha256=<hex>` — the SHA256
   hash of the question. The plaintext question must never appear in
   any audit field.

2. **Routing-decision row** — Query for
   `action="CHAT_ROUTING_DECISION"` matching the same timestamp window.
   Confirms `shared.audit_writer.log_routing_decision()` fired with the
   expected `intent` field (matches the chat-router CloudWatch log
   line `[chat.augment] router decision: intent=...`) plus the
   `retrievals_fired`, `graph_sha`, `graph_staleness_seconds`, and
   `graph_result_count` details.

   ```bash
   aws dynamodb scan \
     --table-name ${env}-${app_name}-audit-logs \
     --filter-expression "#ac = :ac" \
     --expression-attribute-values '{":ac": {"S": "CHAT_ROUTING_DECISION"}}' \
     --expression-attribute-names '{"#ac": "action"}' \
     --max-items 5 \
     --projection-expression "#ts, workspace_id, details"
   ```

   The `details` map must include `question_hash` (matching the cost
   row's SHA256), `intent`, `retrievals_fired`, and (when the graph
   fired) `graph_sha` / `graph_staleness_seconds` / `graph_result_count`.

**Fail signal** (applies to every validation below): if either audit
row is missing for the chat turn under test — or if the question text
appears in any audit field instead of its SHA256 hash — the
validation fails regardless of whether the user-facing answer was
correct. Audit completeness is a hard gate, not aspirational.

---

## Validation 1 — Internal-symbol caller query (the disproof case)

**Workspace**: `Engineering` (`B1MgdLzM1j0YeyAqCWbyxg`) on dev or prd.

**Input** — paste into chat:

```
Which Lambda functions call report_repos_snapshot? List every caller
and the file path.
```

**Expected output**:

- Direct list of the 9 `*-integration` connect/disconnect handlers
  plus `license-heartbeat-scheduler`.
- Each entry has the correct file path and line number.
- Response size < 10KB (vs the 121KB pre-feature baseline).
- Visible "Symbol facts" / graph block in the response; no fabricated
  callers; no `track_repo_active` (the obsolete v1 API).

**Verification command** (ground truth on the local checkout):

```bash
grep -rn 'report_repos_snapshot' lambda/ | grep -v '__pycache__'
```

The chat answer must mention every caller this command surfaces, with
matching file paths.

**Fail signal**:

- Any caller missing.
- Any fabricated caller (a file the grep above doesn't list).
- Response > 30KB (back to prose-dump shape).
- `track_repo_active` referenced anywhere in the answer.

---

## Validation 2 — Subpath import query (Hyatt)

**Workspace**: Hyatt Engineering equivalent (deferred to phase 3 if
the workspace isn't in this account).

**Input**:

```
Which applications use @hyatt-hpe/hpe-utils/http2?
```

**Expected output**:

- Every consumer listed (verifiable against
  `grep -rn "@hyatt-hpe/hpe-utils/http2"` in the customer checkout).
- Each entry has file path + line of the import statement.
- Empty-set discipline: repos with zero subpath imports do not appear.

**Verification command** (customer checkout, NOT this repo):

```bash
grep -rn '@hyatt-hpe/hpe-utils/http2' <customer-checkout-root>/ \
  --include='*.ts' --include='*.tsx' --include='*.js'
```

**Fail signal**:

- Answer returns "2" or any short count below the grep total.
- Answer claims a consumer the grep doesn't surface.

---

## Validation 3 — Boeing CVE response (regression check)

**Workspace**: Boeing demo workspace.

**Input**:

```
CVE-2026-21884 was just published — a XSS vulnerability in
react-router's ScrollRestoration API affecting versions 7.0.0
through 7.11.0, patched in 7.12.0. Search across all applications
in our portfolio and tell me which apps need to upgrade.
```

**Expected output**:

- No regression vs the existing manifest-layer answer (this query
  succeeded pre-feature; chunk 08 must not break it).
- Bonus: if any consumer imports `react-router/ScrollRestoration` via
  subpath specifically, those consumers are listed separately.

**Verification command**: re-run this same query against the
pre-feature deployment (if a snapshot exists) or against a workspace
with the kill-switch flipped to `false` (rollback path below). The
answers must be substantively equivalent.

**Fail signal**: the answer regresses (fewer apps listed, missing
versions, lost citations).

---

## Validation 4 — Pattern-shaped Boeing demo question (regression check)

**Workspace**: Boeing demo workspace.

**Input**:

```
How does our existing analytics platform meet or exceed industry
best practices, and what gaps if any should we address?
```

**Expected output**:

- No regression vs pre-feature behavior — pattern-shaped questions
  must still return the rich RAG synthesis.
- Router classifies as `pattern`; **no graph block** in response.

**Verification command**: same as Validation 3 — compare against
pre-feature deployment or kill-switch-disabled workspace.

**Fail signal**: response truncates, response includes a graph block
("Symbol facts: ..."), or RAG synthesis is materially worse.

---

## Validation 5 — Sev2 blast radius (mixed case)

**Workspace**: Engineering.

**Input**:

```
A Sev 2 alert just fired — RetrieveIT's document ingestion pipeline
is returning 500 errors on large PDF uploads. Based on the code
maps, what's the most likely root cause, which Lambda functions are
in the blast radius, and what's the recommended first remediation
step?
```

**Expected output**:

- Router classifies as `mixed` (look at `chat` Lambda CloudWatch
  logs: search for `[chat.router] decision: mixed`).
- Response includes both RAG synthesis (root-cause hypothesis) and
  graph-grounded blast-radius list (concrete Lambdas with file
  paths).
- Blast-radius list is verifiable against actual import / call graph.

**Verification command**:

```bash
grep -rn 'ingest\|document\|process' lambda/retrieveit-integration/ \
  lambda/process-documents/ lambda/file-ingestion/ \
  2>/dev/null | head -30
```

**Fail signal**: blast-radius list missing or fabricated.

---

## Validation 6 — PR review with signature change

**Setup**: Open a PR against `outcome-ops-ai-assist-private` that
changes the signature of `report_repos_snapshot` (e.g., adds a new
parameter).

**Expected output from `analyze-pr`**:

- The PR receives a comment with a "Structural impact" section.
- All 9 `*-integration` callers listed with file paths and lines.
- A reviewer note flags "signature change; verify each caller is
  updated".
- ADR-grounded review continues unchanged.

**Verification command**: same grep as Validation 1 to confirm
caller count.

**Fail signal**:

- No structural-impact comment on the PR.
- Caller list incomplete.
- ADR-grounded review missing.

---

## Validation 7 — Codegen modify task

**Setup**: File a Jira issue describing a rename of
`report_repos_snapshot` → `submit_repo_snapshot`. Apply the
`approved-for-generation` label.

**Expected output from `generate-code`**:

- The generated plan includes updates to all 9 `*-integration`
  callers in the same PR.
- No "callers not updated" deferred-migration entries.
- PR body documents the cross-Lambda nature of the change.

**Verification command**: open the generated PR and confirm the
"Files changed" tab includes every caller surfaced by the
Validation 1 grep.

**Fail signal**: any caller skipped, or a "TODO: update caller in
follow-up PR" line in the PR body.

---

## Validation 8 — Empty set (the audit unlock)

**Workspace**: Engineering.

**Input**:

```
Which applications call nonexistent_function_xyz?
```

**Expected output**:

- Honest "No callers found in this workspace" response.
- No fabricated list.
- Graph block shows `Direct callers in this workspace: 0`
  explicitly.

**Verification command**:

```bash
grep -rn 'nonexistent_function_xyz' . 2>/dev/null | wc -l
```

Should print `0`.

**Fail signal**: chat fabricates any caller, or claims it "might be"
called by something.

---

## Phase rollout sequence

Per `plan-gkg/08`:

1. **Phase 1 — Dev validation**: run validations 1, 4, 5, 6, 7, 8
   against the dev `outcome-ops-ai-assist-private` workspace after
   the dev backfill completes. Validations 2 and 3 require Hyatt /
   Boeing workspaces; defer to phase 3.

2. **Phase 2 — Prd validation (owner-driven)**: run validations 1, 4,
   5 against prd. Confirm parity with dev.

3. **Phase 3 — Customer rollout**: schedule per-customer maintenance
   windows for backfill, then run the relevant validations against
   each customer workspace.

4. **Phase 4 — Announcement**: only after at least one customer +
   Engineering pass all validations, update `docs/features.md` /
   `docs/architecture.md` with customer-facing language.

---

## Latency monitoring

After rollout, the
`${env}-${app_name}-symbol-graph-query-latency-p99` CloudWatch
alarm (`terraform/cloudwatch_symbol_graph.tf`) watches
`OutcomeOps/SymbolGraph::QueryLatency`. The reader emits this metric
per GSI query (dimension: `Index = target-kind-index` /
`target-prefix-index`).

- Target p99: < 50ms.
- Alarm threshold: > 200ms over 2 consecutive 1-minute periods.
- Notification: routes to the `cost-alerts` SNS topic (the same
  destination as the `analytics-cost-alert` Lambda's threshold
  notifications). When `var.cost_alert_enabled = false`, the alarm
  still raises in the AWS console but no email goes out.

If the alarm fires, investigate:

1. DynamoDB throttling on `target-kind-index` or
   `target-prefix-index` (CloudWatch DDB metrics).
2. Item-size regression in the writer (chunk 02) — large items
   inflate per-page bytes.
3. Cold-start spikes (per-Lambda CloudWatch logs filtered on
   `Init Duration`).

---

## Rollback procedure

Each consumer Lambda gates the graph path with the
`GRAPH_RETRIEVAL_ENABLED` env var (default `"true"`, set in
`terraform/lambda-chat.tf` /
`terraform/lambda-pr-analysis.tf` /
`terraform/lambda-code-generation.tf`).

**To disable graph retrieval without redeploy** — flip the env var
to `"false"` on the target Lambda via the AWS console
(Configuration → Environment variables) and click Save. The change
takes effect on the next cold start.

Affected Lambdas:

- `${env}-${app_name}-chat-streaming` — chat reverts to RAG-only.
- `${env}-${app_name}-analyze-pr` — PR reviews lose the
  "Structural impact" comment; ADR review continues.
- `${env}-${app_name}-generate-code` — modify-path impact analysis
  skipped; greenfield path is unaffected by design.

For backward compat (deploys that haven't been re-rolled with the
chunk-08 variable name), the legacy `GRAPH_AUGMENT_ENABLED` is still
honored as a fallback. The canonical var is the one above.

**No data loss**: the symbol-graph data in DynamoDB stays in place
during rollback. Flipping `GRAPH_RETRIEVAL_ENABLED` back to `"true"`
re-enables the path with zero re-extraction.

After rollback, file a follow-up issue with the failing validation
number and a CloudWatch logs link.

---

## Sign-off

- [ ] All 8 validations pass on dev for the owner-controlled
  workspace.
- [ ] Validations 1 + 4 + 8 pass on prd for the owner-controlled
  workspace.
- [ ] Latency alarm exists and shows < 50ms p99 over the validation
  window.
- [ ] Rollback exercised once (env var flip → re-enable) — proves
  the one-touch toggle works.
- [ ] Owner sign-off to begin customer phase 3 rollout.

## Per-repo config (plan-gkg chunk 11)

Each repository can opt in or out of symbol-graph extraction through
its `.outcomeops.yaml`. The schema lives in
`lambda/shared/repo_config.py` (Pydantic v2) and is validated on every
load; malformed values fall back to a permissive default and never
block the batch-summary path.

```yaml
ingestion:
  symbol_graph:
    enabled: true                       # master switch
    include:                            # repo-relative globs (empty = all)
      - "lambda/**/*.py"
      - "ui/src/**/*.ts"
    exclude:                            # beats include
      - "**/generated/**"
      - "**/*.pb.py"
    languages: ["python", "typescript"] # null/omitted = all registered
    max_file_bytes: 524288              # 512 KiB; 0 disables the cap
```

Precedence (top-most wins):

1. `enabled: false` short-circuits to no facts emitted for any file.
2. ADR-009 base path excludes (`node_modules/`, `.venv/`, `build/`,
   `generated/`, …) always win — the extractor MUST agree with the
   code-unit discoverer on which files are in scope.
3. `exclude` globs beat `include` globs (`fnmatch` semantics, forward
   slashes).
4. `include` empty / unset means "include everything that survives the
   excludes".
5. `languages` is an allow-list; values must be a subset of `python`,
   `typescript`, `java`, `go`, `csharp`, `abap`. Unknown languages
   raise `ValidationError` at parse time.
6. `max_file_bytes` caps per-file source size (Tree-sitter cost grows
   with input). `0` disables the cap; the default of `524288` matches
   the spike-validated budget.

Operators verify the filter from the extraction Lambda logs: each
rejected file logs the reason at debug level; an enabled=false repo
emits a single info-level "symbol-graph disabled by config" line per
batch.

See also:

- `plan-gkg/08-rollout-and-validation.md` — full spec.
- `plan-gkg/overview.md` — why this feature exists.
- `docs/architecture.md` — system context (graph-store callout).
- `docs/features.md` — customer-facing feature description.
