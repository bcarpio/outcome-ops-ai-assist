# Claude Guidance for OutcomeOps AI Assist

## What This System Is

OutcomeOps AI Assist is a **generic platform** for AI-powered code generation designed to support Fortune 500 companies with hundreds or thousands of repositories. The platform is language-agnostic and framework-agnostic by design.

**Deployment model:** The product deploys into each customer's own AWS account. All infrastructure is parameterized via `terraform/*.tfvars` files — nothing is hardcoded to a specific customer. Target customers are large enterprises that use Azure AD for identity. Authentication uses Azure AD (OIDC for the UI, OAuth 2.0 JWTs for the MCP server).

**Key insight:** Different repos may use Terraform, Helm, CloudFormation, Python, Java, TypeScript, Spring Boot, Lambda, ECS, Kubernetes, etc. The platform code must remain generic - all domain-specific knowledge lives in the **Knowledge Base** (ADRs, docs, code maps).

## Core Principle: Documentation-Driven Decisions (ADR-007)

**Primary Rule:** When a problem can be solved by improving documentation (ADRs, READMEs, guides), prefer that over hardcoding logic in application code.

### When to Use Documentation (ADRs/Docs)
- Pattern definitions ("This is how we structure handlers")
- Convention guidance ("Use this naming pattern")
- Best practices ("Prefer X over Y because...")
- Domain knowledge ("Use module version 8.1.2")
- Architectural decisions, quality standards, examples

### When to Use Code Logic
- Workflow orchestration ("After tests pass, create PR")
- Error recovery, retries, infrastructure
- Integration glue, validation, automation

**Test:** If you're tempted to hardcode a pattern, ask: "Could an ADR solve this?"

**The platform queries the KB for standards - it does NOT hardcode them.** If generated code is wrong, the fix is usually in the ADRs/docs, not in the platform code.

## How to Get Standards

Query the knowledge base via MCP before implementing anything. The following tools
are available:

- `query_knowledge_base` - Search for standards, ADRs, patterns, and documentation
- `get_coding_standards` - Retrieve coding standards for a specific topic
- `list_workspaces` - List available workspaces
- `list_workspace_documents` - List documents in a workspace

**Use these MCP tools proactively** when building features, fixing bugs, or making
architectural decisions. Always query the KB before implementing anything to ensure
code matches organizational standards.

Example queries to make via MCP tools:
- "What are our Lambda handler standards?"
- "What are our Terraform module standards?"
- "How should tests be structured?"

The KB contains: ADRs, READMEs, code maps, infrastructure patterns, handler patterns, testing practices.

**Fallback (CLI):** If MCP tools are unavailable, use the CLI:
```bash
./scripts/outcome-ops-assist --env prd "What are our Lambda handler standards?"
```

## Environment Semantics

The repo's `terraform/*.tfvars` map to environments, not customers:

- **`prd.tfvars`** targets the **repo owner's personal AWS account**, not a real
  customer production. It is effectively the maintainer's dev/diagnostics sandbox —
  the only running instance where in-flight work (HA, replication, new integrations)
  can be exercised end-to-end against live AWS services. Worst case for an outage is
  re-ingesting the owner's own repos to regenerate code-maps; Claude Code sessions
  can read ADRs directly from the source repo while the MCP server is down.
- **`dev.tfvars`** is a parallel deploy in the same AWS account, used for
  lower-stakes experiments.
- **Real customer production** runs in *the customer's* AWS account, not here. The
  repo never has direct access to it; changes propagate via the customer's own CI/CD
  bringing this codebase up.

**Implication for troubleshooting:** be willing to do real diagnostic work against
`prd` — sample DLQs, redrive queues, restart Fargate services, even purge if needed.
You are not poking a customer environment. Discuss truly destructive moves (DDB
table delete, KMS key destroy) before acting, but ordinary triage is in-scope.

**AWS credentials (Control Tower / Identity Center, since 2026-07-21):** the app
account is enrolled in an AWS Organization (Control Tower + AFT + Identity
Center). ALWAYS prefix AWS CLI and boto3/python invocations with
`AWS_PROFILE=outcomeops-prd-readonly` — that is an SSO **read-only** role and
the default for all troubleshooting. Do NOT use ambient/default credentials
(the old long-lived IAM-user keys are being retired). When a task genuinely
needs a WRITE (redrive, purge, DDB update, lambda invoke), **ask the operator
for write access first** — name the exact actions/resources needed. If a
read-only command fails with an SSO token expiry, ask the operator to run
`aws sso login --profile outcomeops-prd-readonly`.

## Essential Workflow Rules

### Ask, Don't Guess
- Never assume requirements, implementation approaches, or API signatures
- Use AskUserQuestion tool when unclear
- Query the KB before making assumptions

### Verify Before Changing
- Read files before editing them
- Check existing patterns before adding new code
- Verify module versions, outputs, and schemas from actual source

### HA: Primary/Secondary Parity

Under the active/passive multi-region topology (ADR-022), every primary-region resource
has a secondary counterpart in `terraform/*-secondary.tf`. The two MUST stay in sync.
Divergence is invisible in steady-state operation — the secondary only does real work
during failover — and **only failover surfaces it**, which is the worst time to find a
bug. Every primary-region change to any of the following must be mirrored on the
secondary:

- IAM grants (actions and resource ARNs — including grants that "feel" generic; a
  whole-bucket grant on the primary must not become a `code-maps/*` prefix grant on the
  secondary).
- Lambda function configuration: `handler`, env vars (region-specific values mapped),
  `function_response_types` on event source mappings, batch size, concurrency caps.
- SSM SecureString parameters consumed at module-init by per-region writer Lambdas
  (add to `scripts/ha-list-secrets` and `scripts/ha-sync-secrets` so a fresh bootstrap
  surfaces missing values rather than letting the Lambda silently fail to import).
- Any new resource that the failover runbook (`docs/HA.md`) mentions or relies on.

Before opening a PR that adds or modifies a primary-region resource, run
`make check-ha-parity` — it mechanically compares env-var keys, IAM statement
names, and merge()'d locals for every `*_secondary` module pair, and CI runs
the same check. It does NOT catch everything (values inside statements,
event-source-mapping settings, standalone resources without a `module` block),
so still eyeball the mirror for those. If anything intentional is left
primary-only, document why in an ADR AND add it to the allowlist in
`scripts/check-ha-parity.py` with the reason — don't leave a silent divergence.

### Communicating Deploy Impact

When proposing or asking for a deploy, **always state what is being deployed and which
artifact rebuild it requires**. Never just say "needs terraform apply." A deploy
recommendation should name:

- **Artifact type:** Lambda zip code, Lambda layer, Fargate container image, IAM only,
  SSM parameter, Route 53 record, terraform state only.
- **Prerequisite rebuilds:** whether `make build-all-layers` / `make build-platform-layer`
  / `make build-mcp-server-image` / `make build-database-images` is required before
  `terraform apply`.
- **Regions and functions affected:** primary, secondary, or both; which Lambda
  function(s) or ECS service(s).
- **Verify step:** the one-liner the operator can run after apply to confirm the change
  landed (typically `aws lambda get-function-configuration --query CodeSha256`,
  `aws lambda list-event-source-mappings`, or an `aws ecs describe-services` for
  Fargate). The verify step matters because **a `terraform apply` from a working tree
  that is behind `origin/main` will silently package stale code** — only the SHA check
  catches that.

When in doubt about whether something is a Lambda zip vs Fargate image, grep terraform
for `aws_ecs_service` / `container_image`; anything that ships as an image runs on
Fargate, not Lambda, and needs the image rebuild path.

### Plans Drive Implementation; ADRs Capture Decisions

Two distinct artifact families with no overlap:

- **Plans** live **directly under `plan/`** with `overview.md`, `PROGRESS.md`, and
  numbered chunk files (`01-…md`, `02-…md`). `plan/` is **gitignored** and is
  **always the active working plan** — one plan at a time, no `plan/<name>/`
  nesting. Plans describe the work being done now, not decisions that need to
  outlive the work.
- **`plan-<name>/`** directories (sibling to `plan/`, also gitignored) are
  **queue slots** that the user creates to scope NEW work while Claude is still
  executing the current plan in `plan/`. Claude does not create these — only the
  user does. When the active plan in `plan/` ships, the lifecycle is:
  1. Clear out `plan/` (remove `overview.md`, `PROGRESS.md`, every `NN-…md`).
  2. **Move** the next `plan-<name>/`'s contents into `plan/` (the named dir
     disappears; `plan/` is repopulated with the queued plan's files).
  3. Start executing the new `plan/`.
- **ADRs** live under `docs/adr/`, are **committed and customer-visible**, and capture
  decisions that need to outlive any specific implementation cycle. The pattern (not
  the code), the contract (not the wiring), the reasoning (not the diff).

The durable record is the ADR plus the commit history. Files lingering in `plan/`
after the work shipped (or `plan-<name>/` dirs that were never promoted) will rot
and confuse the next plan.

When a problem can be solved by improving an ADR rather than by code, prefer the ADR
(per ADR-007). When a chunk of implementation work needs sequencing, use a plan.

### Never Reference Plan Directories or Files in Code or Docs
- **NEVER reference `plan/` files OR `plan-*/` directories** from any committed file —
  not in code comments, docstrings, terraform comments, requirements files, tests,
  ADRs, or user-facing docs. `plan/` is the active in-flight working area (cleared
  between plans); `plan-<name>/` directories are sometimes created by the user to
  scope NEW work while Claude is mid-task in `plan/` (those also get cleared once the
  parallel work moves into `plan/` or ships). Both are gitignored and routinely
  deleted.
- A comment that says "see plan/02-foo.md for the contract" or "see plan-gkg/03 for
  the contract" becomes a dangling pointer the moment that file or directory is
  cleared at ship time. The provenance belongs in the **ADR** (`docs/adr/`) or the
  **commit message** — never in the code itself.
- If you're about to write a comment referencing a plan file or `plan-*/` dir,
  instead: either cite the ADR that captured the decision, or write the rationale
  inline and let the commit history carry the rest.

### Temporary migration files do NOT belong in `docs/`
- **`docs/` is customer-facing.** Anything that ships into the customer's deploy bundle
  (or could be read by a customer browsing the repo) belongs there. Migration scripts,
  one-shot backfill procedures, and runbooks for internal sandbox catch-up DO NOT.
- These belong in `plan/` (gitignored). When the work is shipped and the migration is
  run, the script + the plan dir get deleted together (per
  [[feedback_disposable_scripts]] and the plans-vs-ADRs split).
- If the migration logic is genuinely customer-relevant (e.g., a schema upgrade
  customers running their own deploys must perform), it goes in an **ADR** — not a
  loose markdown file under `docs/runbooks/`. ADRs are the durable artifact.

### Git Discipline
- **NEVER push** - Only commit. The user will push.
- **Never amend commits** - Create new commits for fixes
- **No emojis** in commits or documentation
- Use Conventional Commits format
- **NEVER commit `terraform/prd.tfvars` or `terraform/dev.tfvars`** - These contain environment-specific configuration and must not be committed. Only the `.example` files are committed.

### When You Ship a Feature — Three-Surface Rule

Every feature has three documentation surfaces. When one drifts, sales
gets "I don't know" answers to customer questions we do have coverage
on. Keep them in sync — update all three in the same PR / session.

1. **`docs/features.md`** (this repo) — dev/technical reference. ADR
   numbers, Lambda names, tfvar keys, resource paths. Terse. See file
   header for the "one line per feature" style.
2. **`../marketing-for-outcomeops/feature-list.md`** — sales-team-
   facing mirror. Customer-value framing (not ADR#s), categorized like
   the Confluence page. Ingested into the marketing.outcomeops.ai
   chatbot so sales partners can answer prospects.
3. **Confluence "OutcomeOps AI Assist - Feature List"** page (Space
   `OutcomeOps`, ID `57835580`) — sales-team + GTM-partner reference.
   Same content shape as `feature-list.md`, Confluence-formatted.

You can update Confluence via the Atlassian MCP
(`mcp__claude_ai_Atlassian__updateConfluencePage`, cloudId
`outcomeops.atlassian.net`, pageId `57835580`) once the local files
are staged. If a session drops the Confluence update, at minimum
leave a `TODO(confluence): mirror <what-changed>` line in the commit
message so the next session can catch up.

### Before Committing (Required CI Steps)

**ALWAYS run these commands before committing any changes:**

```bash
# 1. Run unit tests (Lambda/Python)
make test-unit

# 2. Run UI tests (if any ui/ files changed)
make test-ui
# Note: Requires Node 20+. If local Node is older, tests will run in CI.
# When adding new API functions or modifying component props, update the
# vi.mock() blocks in the corresponding test files to include new exports.

# 3. Format and validate Terraform (if any .tf files changed)
make fmt
make validate-tf

# 4. Check Python formatting (if any .py files changed)
cd lambda && find . -name "*.py" -path "*/lambda/*" | head -20 | xargs -I {} python -m py_compile {}

# 5. Run dependency audits
make audit         # runs BOTH npm audit (ui/) + pip-audit (lambda/*/requirements.txt)

# If you only touched one side of the tree, save time with:
#     make audit-ui    # UI only
#     make audit-py    # Python only (fast: 8-way parallel across 58+ requirements.txt)
#
# `for i in $(find . | grep requirements.txt); do pip-audit -r $i; done`
# is 5-15 minutes SERIAL and re-hits the vuln DB per file.
# `make audit-py` closes it to <1 minute -- always use it.
```

**CI will fail if you skip these steps.** Run them every time before committing.

### Local Development
- **Always activate the Python venv first:** `source lambda/.venv/bin/activate` — required for newer AWS SDK features (e.g., s3vectors)
- Never run `terraform apply` locally - CI/CD only
- Use MCP tools (`query_knowledge_base`, `get_coding_standards`) to query the knowledge base

## When Investigating Issues

If generated code has problems (wrong versions, wrong patterns, missing standards):

1. **First check:** Is the relevant ADR in the KB? Query it.
2. **If ADR exists but wasn't used:** Check if the vector queries match the ADR content
3. **If ADR is missing:** The fix is creating/updating the ADR, not hardcoding logic
4. **Discuss before changing platform code** - Most fixes belong in documentation

## Project Structure

```
lambda/           # Lambda handlers (one dir per function)
lambda/tests/     # Centralized pytest tests
terraform/        # Infrastructure as Code
docs/             # Documentation and ADRs
scripts/          # CLI tools including outcome-ops-assist
ui/               # React frontend + Express proxy server
```

## Adding New API Endpoints

When adding new Lambda API endpoints, you must update **FOUR** places:

1. **Lambda handler** (`lambda/<function>/handler.py`)
   - Add the route in `lambda_handler()` function
   - Add the handler function

2. **FastAPI routes** (`lambda/<function>/main.py`)
   - Add the FastAPI route decorator that forwards to `lambda_handler`
   - **If you forget this step, the Lambda returns 404!**

3. **Express proxy server** (`ui/server/index.js`)
   - Add the route to proxy requests to the Lambda
   - Uses `proxyWorkspaceRequest()` for workspace Lambda routes
   - Uses `signedChatFetch()` for chat Lambda routes
   - **If you forget this step, the route returns HTML (index.html) instead of JSON!**

4. **Frontend API client** (`ui/src/lib/api.ts`)
   - Add the TypeScript function to call the API

Example for a new workspace-related endpoint `/foo/bar`:
```python
# In lambda/workspace-management/main.py
@app.get("/foo/bar")
async def foo_bar(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/foo/bar")
    result = lambda_handler(event, None)
    return _parse_response(result)
```

```javascript
// In ui/server/index.js
app.get("/api/foo/bar", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/foo/bar"));
```

## Questions and Escalation

If you encounter missing features, outdated docs, conflicting guidance, or unclear requirements: **Stop and ask.** Do not guess or work around issues silently.
