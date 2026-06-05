# ADR-036: Azure DevOps source-code integration

## Status: Proposed

## Context

Per the GTM strategy (project moat sits in auditability + governance for
regulated buyers, not in chat or codegen), the buyer profile includes
shops running Azure DevOps (ADO) for source control. Several regulated
prospects vetting the platform discovered the gap during PoC scoping;
not supporting ADO disqualified the platform before any feature
discussion. Adding ADO removes the disqualifier.

The platform already ingests GitHub repos via OAuth-App installation +
hourly EventBridge poller + SQS-fan-out sync workers. ADO has the same
shape — pull-based ingest, code-map generation, PR-check comment
posting — but a different identity story (Microsoft Entra ID, not
GitHub's OAuth App), a different REST API surface
(`dev.azure.com` / `app.vssps.visualstudio.com`), and a different
three-level namespace (`{org}/{project}/{repo}`).

This ADR captures the load-bearing decisions across the implementation.
The reasoning and alternatives matter more than the code; the code will
evolve, the constraints behind these choices won't.

## Decisions

### Decision 1: Auth — Microsoft Entra ID OAuth, single-tenant per customer

ADO source-code APIs accept Entra ID OAuth access tokens with the
well-known resource ID `499b84ac-1321-427f-aa17-267ca6975798` and the
`.default` scope. The platform integrates as a customer-tenant-registered
single-tenant app — the customer's IT admin registers the app in their
own Entra ID directory, hands the operator the tenant_id + client_id +
client secret, and the operator populates `tfvars`.

**Why single-tenant per customer:**

- Aligns with the platform's Azure AD-first identity story (per
  `CLAUDE.md` — customers federate through their own AAD)
- Avoids the Microsoft Partner Center publication process required for
  multi-tenant apps; that flow is operationally hostile and adds
  customer-acquisition friction
- Per-customer single-tenant matches the platform's deploy model — one
  AWS account per customer, one Entra ID app per customer

**Implementation consequence:** the authorize URL must use the
customer-specific tenant (`https://login.microsoftonline.com/{tenant_id}/...`),
NOT `/common/`. Entra ID rejects `/common/` for single-tenant
registrations. A regression test pins this; if a future contributor
defaults the tenant to `"common"` for "simplicity", ADO connections
break.

**Alternatives rejected:**

- **Per-user PAT (Personal Access Token).** Expires in 90 days; every
  user re-pastes a token every quarter. Audit story is "user X created
  this PAT" — no consent flow, no admin oversight. Enterprise IT
  rejects PAT distribution flows.
- **Service principal (client-credentials flow).** No user attribution
  on PR-comment posts — every comment looks like "platform bot did
  this." Loses the audit trail that compliance customers need.
- **Multi-tenant Entra ID app.** Requires Microsoft publication + cross-
  tenant token validation; tenants must individually consent. Adds
  Microsoft Partner Center as a gating dependency on the platform's
  release cadence. Acceptable for a future SaaS variant of the
  platform but not for the current per-customer-deploy model.

### Decision 1a: Entra ID OAuth lives in `lambda/shared/entra_id_oauth.py`

When ADO support was planned the codebase already had Microsoft Entra
ID OAuth helpers for Outlook + Teams + SharePoint + OneDrive +
OneNote, plus the file-ingestion Outlook variant. Five copies. Audit
revealed the five had drifted across multiple dimensions:
refresh-token rotation, KMS at-rest encoding, error wrapping, retry
behavior.

Adding ADO as a sixth copy would compound the drift. Refresh-token
rotation correctness is the kind of code that should have one source
of truth — three copies is three places to ship a token-replay bug,
six copies is six places.

**Decision:** consolidate the five existing wrappers + the new ADO
wrapper into `lambda/shared/entra_id_oauth.py`. The shared module
owns HTTP, KMS encrypt/decrypt, DDB read/write, and the
refresh-rotation contract. Each integration's `token_manager.py` is a
thin wrapper (~85-140 LoC) that holds only the constants that
genuinely vary: scope string, PK prefix, and (for ADO) per-customer
tenant_id. Live-verified prior to the ADO work shipping via Outlook
mail-sync + SharePoint sites-sync round-trips against real AAD
endpoints + KMS.

**Rule for future contributors:** if a change to one integration's
`token_manager.py` wrapper would also benefit any of the others, it
belongs in `shared.entra_id_oauth` — not in N separate wrappers.

**Alternatives rejected:**

- **Keep the five copies + add a sixth.** Drift is already a known
  problem; status-quo strictly compounds it.
- **Class hierarchy (`EntraIDTokenManager` base class).** Adds an
  abstraction layer without leverage. The wrappers carry constants,
  not behavior — pure functions with explicit parameters are clearer
  and don't pull a `super().__init__` ceremony into every consumer.
- **Feature-flag the shared module per integration.** Encourages
  perpetual drift; tells future contributors they're free to skip
  the shared module's contract.

### Decision 2: DDB namespace — `AZDO_REPO#{org}/{project}/{repo}`

ADO repos live under a three-level namespace. GitHub repos in DDB use
`GITHUB_REPO#{owner}/{repo}` per `feedback_ddb_repo_sk_convention`;
the ADO equivalent extends that to three segments.

**Why `/` as the segment separator inside the SK:**

- Matches Git URL convention (`org/project/repo`) — the slug shape a
  user copy-pastes from a browser address bar IS the slug the
  platform stores
- Single namespace prefix (`AZDO_REPO#`) lets the license layer
  count ADO repos with one additional allow-list entry per
  `project_license_layer_v2`
- Connection and repo rows carry explicit `org`, `project`, `repo`,
  `provider` attributes so consumers can query without parsing the
  SK

**Alternatives rejected:**

- **`#` as the segment separator (`AZDO_REPO#{org}#{project}#{repo}`).**
  Consistent with the existing `#` prefix style but visually noisy
  and inconsistent with the Git convention. The save in
  consistency-with-prefix-style is dominated by the loss in
  consistency-with-the-URL-the-user-typed.
- **Per-org separate prefix (`AZDO_REPO_ORG_{org}#`).** Fragments the
  scan; license-layer counting needs N allow-list entries instead of
  one; workspace-cross-org views break.

### Decision 3: Sync model — pull-based, mirroring GitHub

GitHub today is ingest-via-hourly-poller + manual UI/API trigger; no
webhooks. ADO Service Hooks would add real-time semantics on the ADO
side but would not on the GitHub side. Keeping the providers
symmetric until both customers ask for push gives the platform a
single ingest mental model.

**Why pull:**

- Simpler operational story — one ingest pipeline, no inbound-webhook
  attack surface to harden
- More testable — replays just rerun the poller against the same
  state
- Easier to rate-limit — the platform controls cadence rather than
  reacting to bursty webhook traffic
- No customer-side Service Hook configuration required during onboarding

**Alternatives rejected:**

- **ADO Service Hooks for repo events.** Asymmetric with GitHub.
  Webhook receivers introduce a new public attack surface and a new
  reliability dimension (replay, ordering, idempotency).
- **Hybrid (webhooks for PR events only, polling for source content).**
  Added complexity for marginal latency improvement. Defer to a
  future plan if buyer pain surfaces.

### Decision 4: PR-check trigger — UI button + customer's ADO Pipeline

PR-check today is triggered from outside the platform — UI button or
customer's GitHub Action POSTs to `pr-checks-queue`. ADO mirrors that:
UI button OR customer's `azure-pipelines.yml` step calls the same
endpoint with `provider="azure_devops"`.

**Why not webhooks:** see Decision 3. Same reasoning.

**What the customer adds to their pipeline:** the runbook in
`docs/runbooks/azure-devops-setup.md` carries a copy-paste
`PowerShell@2` task step. Customer stores their platform API token
as a pipeline secret.

### Decision 5: API client — raw `urllib.request`, no SDK

ADO's `azure-devops-python` SDK pulls `msrest`, `azure-core`, and
~30 transitive packages — inflates Lambda cold-start by ~400ms. The
platform uses ~10 ADO endpoints; the SDK adds 200+. Mirror the
Outlook integration's raw-urllib pattern. Revisit only if a future
buyer requirement makes an SDK feature load-bearing (paged
iterators, retry strategies, etc.).

### Scope explicitly deferred

These are NOT in v1 and changing that requires a new ADR:

- **Azure DevOps Server (on-prem TFS).** Different URL pattern,
  PAT-only auth, different release cadence. Separate plan if buyers
  ask.
- **ADO Service Hooks (webhooks).** See Decisions 3 + 4.
- **ADO Pipelines integration (reading customer pipeline definitions).**
  The platform is a code-knowledge layer, not a CI surface. PR-check
  is the boundary.
- **ADO Wiki / Test Plans / Boards.** Separate plan if customers ask,
  analogous to how Confluence/Jira are separate integrations.
- **Multi-tenant Entra ID app.** See Decision 1.

## Consequences

Locked in by this ADR:

- `AZDO_REPO#` SK prefix — schema migrations are expensive
- Per-customer single-tenant auth — adding a multi-tenant SaaS variant
  would be a parallel auth path, not a replacement
- Raw `urllib` client — adding the SDK later is a refactor, not a
  rewrite, but the precedent set here is "no SDKs"

Stays flexible:

- Pull-based sync — webhooks can be added later as an additive trigger
  source without changing the data plane
- Three-segment namespace — could absorb four-segment (e.g. on-prem
  TFS collections) by extending the slug rather than changing the
  prefix

## Live verification

End-to-end verification is gated on the operator standing up an Entra
ID app registration in a test customer tenant and populating
`prd.tfvars` (the maintainer's sandbox per `project_prd_environment`).
The runbook walks through the customer-facing setup; verification is:

- Connect flow lands an `AZDO_CONNECTION#` row + an encrypted
  `AZDO_TOKEN#` row in DDB
- Repo picker lists orgs / projects / repos via the live ADO REST API
- Add-repo enqueues a sync job to `azure-devops-sync-queue` and the
  sync worker processes it without error against a real ADO repo
- Manual PR-check from the UI posts a comment to a real ADO PR thread
- Disconnect cascade-removes the connection + tokens + every
  `AZDO_REPO#` row tied to that connection

Until the operator stands up the test ADO tenant, the implementation
is "shipped, unit-tested, live-deferred" — the same posture as the
GPT-5.5 path while it waited on AWS Marketplace entitlement.
