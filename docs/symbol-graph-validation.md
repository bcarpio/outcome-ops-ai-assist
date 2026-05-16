# Symbol-Graph Retrieval — Validation Runbook

This runbook walks the operator through the 8 validation queries that
gate the symbol-graph feature's "v1 is done" milestone, the audit-row
checks that confirm the routing-decision + cost-tracking story holds,
the phase rollout sequence, and the v2 migration procedure.

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
operator confirms before signing off (the audit story is part of the
v1 definition of done, not optional):

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

## Per-repo config

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

- `docs/features.md` — user-facing description of the structural
  retrieval feature.
- `docs/architecture.md` — system context (graph-store callout).
- `docs/adr/ADR-020-abap-parser-strategy.md` — ABAP hybrid extractor.
- `docs/adr/ADR-021-gsi-naming-symbol-graph.md` — GSI naming.


## v2 migration (one-shot, per workspace+repo)

The symbol-graph table moved from per-commit-SHA partitions to a
single `(workspace, repo)` partition. Existing data needs a one-shot
migration. The writer code (`lambda/shared/symbol_graph_writer.py`)
now constructs `PK = ${workspace_id}#${repo}` instead of
`PK = ${workspace_id}#${repo}#${commit_sha}`. Reader is unchanged
because GSI1 / GSI2 queries were already SHA-agnostic.

**When to run**: after deploying the chunk-01 writer code, BEFORE
allowing scheduled syncs to fire on the new code. If a sync runs
between deploy and migration, you get a window where the new writer
creates v2-shape items while old v1-shape items still exist; the
reader returns duplicates for that window. The script picks up both
and converges on the next pass, but minimizing the window is the
operator's job.

**Why not run from terraform**: terraform-managed migrations are a
footgun at customer rollout (state divergence on retry, no clean
abort path). The script is intentionally operator-run.

**The script** (paste into `scripts/migrate-symbol-graph-v2.py`, run
once, delete per `feedback_disposable_scripts.md`):

```python
#!/usr/bin/env python3
"""One-shot migration: symbol-graph table from per-SHA PK to (workspace, repo) PK.

Run AFTER chunk-01's writer code is deployed. Idempotent.

Usage:
    ENVIRONMENT=prd python scripts/migrate-symbol-graph-v2.py
"""

import os
import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
ENV = os.environ.get("ENVIRONMENT", "prd")
TABLE = f"{ENV}-outcome-ops-ai-assist-symbol-graph"

ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE)


def needs_migration(item):
    """Old shape: PK = workspace#repo#sha. New shape: workspace#repo."""
    pk = item.get("PK", "")
    parts = pk.split("#")
    # Old shape has 3+ segments with non-empty third (the commit SHA).
    return len(parts) >= 3 and bool(parts[2])


def new_pk(item):
    parts = item["PK"].split("#")
    return f"{parts[0]}#{parts[1]}"


migrated = 0
skipped = 0
errors = 0

print(f"Scanning {TABLE}...")
paginator = table.meta.client.get_paginator("scan")
for page in paginator.paginate(TableName=TABLE):
    for item in page.get("Items", []):
        try:
            if not needs_migration(item):
                skipped += 1
                continue
            new_item = dict(item)
            new_item["PK"] = new_pk(item)
            table.put_item(Item=new_item)
            table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            migrated += 1
            if migrated % 1000 == 0:
                print(f"  migrated={migrated} skipped={skipped} errors={errors}")
        except Exception as e:
            errors += 1
            print(f"  ERROR on {item.get('PK')} / {item.get('SK')}: {e}")

print(f"\nDone. migrated={migrated} skipped={skipped} errors={errors}")
```

**Sequencing**:

1. Land chunk 01's commit locally + push to `origin/main`.
2. Build images and `terraform apply` by hand.
3. DO NOT trigger any sync yet.
4. Run the script:
   ```bash
   ENVIRONMENT=prd python scripts/migrate-symbol-graph-v2.py
   ```
5. Wait for completion. Eyeball the migrated / skipped / errors counts.
6. Validate (next section).
7. Resume scheduled syncs.
8. Delete the script:
   ```bash
   rm scripts/migrate-symbol-graph-v2.py
   ```

**Time estimate**: ~1 minute per 10k facts. A workspace with 60k facts
takes ~6 minutes.

**Idempotency**: rerunning is safe. Already-migrated items are skipped.
Deletes of already-gone items are 404-silent.

### Post-migration validation

1. **No old-shape items remain**:

   ```bash
   python3 -c "
   import boto3
   t = boto3.resource('dynamodb', region_name='us-west-2').Table('prd-outcome-ops-ai-assist-symbol-graph')
   old = 0
   total = 0
   for page in t.meta.client.get_paginator('scan').paginate(TableName=t.name):
       for it in page.get('Items', []):
           total += 1
           parts = it['PK'].split('#')
           if len(parts) >= 3 and parts[2]:
               old += 1
   print(f'old-shape items: {old} / {total}')"
   ```

   Expected: `old-shape items: 0 / <total>`.

2. **MCP `find_callers` still resolves** — same query as the pre-migration
   validation queries (Validation 1):

   ```
   mcp__outcomeops__find_callers(
       symbol_id="python:lambda.database-integration.handler::report_repos_snapshot",
       workspace_id="B1MgdLzM1j0YeyAqCWbyxg"
   )
   ```

   Expected: returns the caller at `lambda/database-integration/handler.py:111`.

3. **`report_repos_snapshot` returns 10 production callers** — DDB scan:

   ```bash
   python3 -c "
   import boto3
   ddb = boto3.client('dynamodb', region_name='us-west-2')
   kwargs = {'TableName': 'prd-outcome-ops-ai-assist-symbol-graph',
             'FilterExpression': '#k = :k AND contains(target_symbol_id, :s)',
             'ExpressionAttributeNames': {'#k': 'kind'},
             'ExpressionAttributeValues': {':k': {'S': 'calls'}, ':s': {'S': 'report_repos_snapshot'}}}
   files = set()
   while True:
       r = ddb.scan(**kwargs)
       for i in r['Items']:
           fp = i.get('file_path',{}).get('S','')
           if 'tests/' not in fp:
               files.add(fp)
       if 'LastEvaluatedKey' not in r: break
       kwargs['ExclusiveStartKey'] = r['LastEvaluatedKey']
   print(f'Production callers: {len(files)}/10')
   for f in sorted(files): print(f'  {f}')"
   ```

   Expected: 10/10 (the 9 *-integration handlers + license-heartbeat-scheduler).

### Rollback

If the migration produces unexpected results, rollback is straightforward
because the writer's v2 schema is forward-compatible with reads of v1
data (reader uses GSI, which doesn't filter by PK shape):

1. Stop the script (Ctrl-C). Already-migrated items stay migrated.
2. Revert the chunk-01 commit, redeploy. v1 writer creates per-SHA-PK
   items going forward.
3. Either accept the brief dual-shape window or run the script in
   reverse (write v1-shape items from v2-shape items) — straightforward
   to invert.

In practice rollback hasn't been needed; the migration is mechanical
and idempotent.
- `docs/features.md` — customer-facing feature description.

## Fuzzy-resolve health (target-name-index)

The chat handler resolves user-typed bare names ("require_auth")
to canonical IDs ("python:lambda.auth.handler::require_auth") via
the `target-name-index` GSI on the symbol-graph table. The GSI is
partitioned by `(workspace_id, bare_name)`, so each resolve is a
single-partition Query bounded by actual matches — replaces the
original Scan-based fuzzy that did not scale past one repo per
workspace.

### Verify the GSI is populated for a workspace

```bash
TABLE=$(aws ssm get-parameter \
  --name "/${ENV}/${APP}/dynamodb/symbol-graph-table" \
  --query Parameter.Value --output text)

aws dynamodb query \
  --table-name "$TABLE" \
  --index-name target-name-index \
  --key-condition-expression "GSI3PK = :pk" \
  --expression-attribute-values '{":pk":{"S":"<workspace_id>#<bare_name>"}}' \
  --select COUNT
```

Pick a bare name you know exists in the workspace (e.g.
`require_auth` for any Python codebase, `lambda_handler` for
AWS-shaped repos). A non-zero `Count` confirms the GSI is
populated.

### Symptom: chat says "Graph: callers (0 results)" for a name that obviously has callers

Three things to check, in order:

1. **IAM** — the chat Lambda role needs `dynamodb:Query` on
   `arn:aws:dynamodb:*:*:table/<symbol-graph-table>/index/target-name-index`.
   Without it the reader catches `AccessDeniedException`, returns
   `[]`, and the augment falls through to a bare-name caller
   query that always misses. Look for
   `[chat.augment] fuzzy resolve failed for ...` in the chat
   Lambda log group. The exact-ARN policy lives in
   `terraform/iam_symbol_graph.tf` (`local.symbol_graph_arns`).
2. **Writer redeploy** — `bare_name` is populated at write time
   by `lambda/shared/symbol_graph_writer.py`. If
   `process-batch-summary` is running an image older than the
   writer change, facts written after the redeploy will lack
   `bare_name` and the GSI will stay empty for those rows.
   Disconnect + reconnect the affected repo to force a fresh
   ingest under the new writer.
3. **Re-ingest scope** — incremental regen only re-extracts
   changed files. If the file holding the canonical was
   unchanged since the writer redeploy, its facts retain the
   pre-redeploy attribute set. Disconnect + reconnect bypasses
   incremental detection.

The chat handler logs the per-request fuzzy result:

```
[chat.augment] fuzzy resolve '<bare_name>' -> <N> canonicals
```

Grep CloudWatch for that line around the time of the failing
chat turn — `N=0` means no GSI3 rows; `N>=1` means the fuzzy
succeeded and the empty-result path is somewhere downstream
(canonical has no callers, IAM on a different ARN, etc.).
