# OutcomeOps Code-Intelligence MCP Toolset — Interface Spec

**Audience:** $PARTNER (Worker Agent integration), under NDA.

This document defines the **interface** an external agent sees — the tool contract
(inputs and outputs) — not the implementation. It does not describe how the symbol
graph is constructed, how repositories are indexed, or any internal retrieval/ranking
pipeline.

**Provenance rule (internal only):** every resource_type below is annotated with the
backing module/function in this codebase so the maintainer can verify it is real. Those
citations are for internal verification only and would be stripped before sharing
externally.

---

## 1. What this exposes

OutcomeOps runs a code-intelligence layer over a customer's repositories: a cross-repo
**symbol graph** (who calls / imports / implements / defines what, with `file:line`
grounding), a semantic **knowledge base** of ingested ADRs / docs / code-maps, and a
grounded **source-read** capability. Today these are reachable over MCP as **11 native
tools** (see §9). This spec re-packages that surface into a small set of **generic verb
tools dispatched by a `resource_type` parameter**, to mirror the $PARTNER MCP Server's
conventions and read as natively interoperable.

> **Read this before the tables.** Every capability marked **✅ Demo-ready** is backed by
> an MCP tool that ships and runs today (under its native name, listed in §9). The
> generic-verb facade described here (`list` / `search` / `get` / `describe` dispatched
> by `resource_type`) is itself a **thin re-packaging layer that does not exist yet** — it
> is small, additive engineering over primitives that already work. So "demo-ready"
> means *the underlying capability is live and can be demonstrated today via its native
> tool*; the $PARTNER-shaped wrapper is roadmap. Items marked 🟡 or 🔴 are additionally
> gated as noted.

---

## 2. Connection & authentication

| Property | Value |
|---|---|
| Transport | MCP **Streamable HTTP** (JSON-RPC 2.0; protocol version `2025-03-26`) |
| Endpoint | `POST https://<deployment-fqdn>/mcp/v1` |
| Health check | `GET /mcp/v1` or `/health` (unauthenticated) |
| Session | `initialize` mints a session (`Mcp-Session-Id` header); `DELETE /mcp/v1` ends it |
| Auth | `Authorization: Bearer <api_key>` — an **OutcomeOps-issued API key** (opaque secret) |
| Deployment model | **Single-tenant, in-customer-account.** The whole stack (compute, DDB, vector store) runs in one customer's AWS account. There is no cross-customer tenant dimension in the protocol. |
| Scoping | Within a deployment there are multiple **workspaces**. The API key resolves to a user identity whose accessible workspace set bounds every call. `workspace_id` is a **required** parameter on all symbol-graph and source tools, and **optional** on knowledge-base tools (omit → search all workspaces the key can see). |

> **Correction vs. prior internal notes:** the MCP server authenticates with an
> **OutcomeOps API key as a bearer token** (SHA-256 matched against the workspaces table,
> checked for `active` status + expiry). Azure AD OIDC is the **UI browser login only** —
> it is *not* the MCP auth path. Do not describe the MCP surface as "Azure AD JWT."

*Backing (verification only):* `lambda/mcp-server/handler.py` — server + transport
(≈L2149), `validate_api_key` (L579), tool registration `MCP_TOOLS` (L161), dispatch
`TOOL_HANDLERS` (L2107). Deploy: `terraform/mcp-outcomeops.tf`, `terraform/mcp.tf`
(Fargate service on the shared ALB, listener path `/mcp/v1`).

---

## 3. Design conventions ($PARTNER-mirroring)

- **Generic verbs, not one-tool-per-endpoint.** Four verb tools — `list`, `search`,
  `get`, `describe` — each take a `resource_type` discriminator plus resource-specific
  parameters.
- **`list`** enumerates a collection. **`search`** does semantic / fuzzy retrieval and
  returns *ranked* results. **`get`** retrieves a specific resource by a precise
  identifier. **`describe`** returns a structural summary of a resource.
- **Every call is workspace-scoped** where the underlying data is (symbol/source tools);
  knowledge tools default to the caller's full accessible set.
- **Output envelope.** At the MCP transport layer every result is
  `{ "content": [ { "type": "text", "text": "<string>" } ], "isError": <bool> }`.
  The `text` payload is JSON for the symbol-graph/source resources (caller does
  `json.loads`) and an inline-cited human-readable blob for the knowledge resources
  today. The **normalized JSON output shapes** below are the proposed facade contract;
  where today's primitive returns a text blob instead of structured JSON, that
  normalization is called out as small facade work (not a new capability).

---

## 4. Tool × resource_type matrix

| Verb | `resource_type` | Purpose | Backing primitive | Status |
|---|---|---|---|---|
| `list` | `workspaces` | Enumerate workspaces the key can access | `list_workspaces` | ✅ Demo-ready |
| `list` | `documents` | Enumerate ingested docs (ADRs, code-maps, READMEs, summaries) in a workspace | `list_workspace_documents` | ✅ Demo-ready (top-500 cap) |
| `search` | `knowledge` | Semantic search over ADRs/docs/code-maps; returns ranked, cited snippets | `query_knowledge_base` | ✅ Demo-ready |
| `search` | `coding_standards` | Topic-scoped standards/patterns retrieval | `get_coding_standards` | ✅ Demo-ready |
| `search` | `symbols` | Resolve a bare name (e.g. `require_auth`) → canonical symbol IDs | `find_symbol_by_name` | ✅ Demo-ready |
| `get` | `symbol_edges` | Directional graph edges for a symbol/module: `callers` \| `importers` \| `implementers` \| `definitions` | `find_callers` / `find_importers` / `find_implementers` / `find_definitions` | ✅ Demo-ready |
| `get` | `symbol_at_line` | Innermost enclosing symbol at `(repo, file, line)` | `find_symbol_at_line` | ✅ Demo-ready |
| `get` | `source` | Bounded, graph-anchored window of real source from `(repo, file, line)` | `read_source` | ✅ Demo-ready |
| `get` | `blast_radius` | Transitive impact set of a change to a symbol/file | *(composed from primitives; internal engine not exposed)* | 🟡 Roadmap |
| `describe` | `code_map` | Return a repo's structural code-map (typed summary set) | *(content reachable only via `search knowledge`; no first-class retrieval)* | 🟡 Roadmap |
| `describe` | `workspace` | Return a single workspace's metadata | *(metadata subset of `list_workspaces`)* | ✅ Demo-ready (partial) |

**Not in the matrix — see §8 (Aspirational).** `search component_guidance`,
`get relevant_decision_record` (change → ADR), an `ask` verb (router-synthesized answer),
and any inter-service / application-topology graph are **not** real capabilities today.

---

## 5. `list` — collections

### 5.1 `resource_type: "workspaces"`  ✅

Enumerate the workspaces the authenticated key can access. This is the discovery call
that yields the `workspace_id` values every other tool needs.

**Input**

| Param | Type | Required | Default |
|---|---|---|---|
| `resource_type` | `"workspaces"` | yes | — |

**Output (normalized)**

```json
{
  "workspaces": [
    { "workspace_id": "ws_abc123", "name": "Payments Platform",
      "description": "…", "is_global": false },
    { "workspace_id": "ws_sec789", "name": "Security Standards",
      "description": "…", "is_global": true }
  ]
}
```

The `is_global` flag marks workspaces whose content is available in
every user's chat and PR-check retrieval scope across the deployment.
An external agent can use this signal to reason about
cross-workspace applicability — a `Global` workspace's ADRs apply
to the whole portfolio, not just its own members.

*Backing:* `handle_list_workspaces` → `lambda/mcp-server/handler.py:1198`. Native tool
returns a markdown list today (JSON normalization = small facade work).
The `is_global` field is populated from the DDB row (default `false`
for pre-2026-07-03 workspace rows).

### 5.2 `resource_type: "documents"`  ✅ (top-500 cap)

Enumerate distinct ingested documents in a workspace (ADRs, code-maps, READMEs, per-area
summaries). Identity only — no document bodies.

**Input**

| Param | Type | Required | Default |
|---|---|---|---|
| `resource_type` | `"documents"` | yes | — |
| `workspace_id` | string | yes | — |

**Output (normalized)**

```json
{
  "documents": [
    { "repo": "payments-api", "file_path": "docs/adr/ADR-014-idempotency.md",
      "type": "adr", "name": "ADR-014-idempotency.md" }
  ],
  "capped_at": 500
}
```

> **Honesty flag:** enumeration is capped at the top-500 vectors for the workspace, so a
> very large corpus can under-list. State this to $PARTNER — do not present it as a
> guaranteed-complete document index.

*Backing:* `handle_list_workspace_documents` → `lambda/mcp-server/handler.py:1232`.

---

## 6. `search` — ranked retrieval

All three `search` resources return the same ranked-result shape. Results are reranked
and snippet-windowed; **citations are per-result** (`repo`, `type`, `file_path`, `score`).
This is retrieval, **not** an LLM-synthesized answer.

### 6.1 `resource_type: "knowledge"`  ✅

Semantic search over the full ingested corpus (ADRs, docs, READMEs, and code-map
summaries surface here when relevant).

**Input**

| Param | Type | Required | Default |
|---|---|---|---|
| `resource_type` | `"knowledge"` | yes | — |
| `query` | string | yes | — |
| `workspace_id` | string | no | all accessible workspaces |
| `top_k` | integer | no | 5 (clamp 1–20) |
| `max_tokens` | integer | no | 4000 (clamp 1–16000) |
| `min_score` | number | no | 0.0 (clamp 0.0–1.0) |

**Output (normalized)**

```json
{
  "results": [
    { "rank": 1, "repo": "payments-api", "type": "adr",
      "file_path": "docs/adr/ADR-014-idempotency.md",
      "score": 0.82, "snippet": "Idempotency keys are required on all POST …" }
  ],
  "omitted": 0
}
```

Empty result set → `{ "results": [], "omitted": 0 }` (native tool emits the literal
string `"No relevant documents found."`).

*Backing:* `handle_query_knowledge_base` → `lambda/mcp-server/handler.py:1174` →
`search_knowledge_base` (L722) → `format_results` (L781).

### 6.2 `resource_type: "coding_standards"`  ✅

Convenience variant of `knowledge` tuned for standards/patterns. Takes a `topic` and runs
the same retrieval against a standards-shaped query.

**Input**

| Param | Type | Required | Default |
|---|---|---|---|
| `resource_type` | `"coding_standards"` | yes | — |
| `topic` | string | yes | — |
| `workspace_id` | string | no | all accessible workspaces |
| `top_k` / `max_tokens` / `min_score` | — | no | same defaults as `knowledge` |

**Output:** identical shape to `knowledge`.

> **Honesty flag:** this is a **query-templating wrapper**, not a component-aware or
> doc-type-filtered lookup. It reformulates `topic` into a standards-phrased query and
> runs semantic search. It does not take a symbol/file/module and return guidance scoped
> to that unit (see §8).

*Backing:* `handle_get_coding_standards` → `lambda/mcp-server/handler.py:1306`.

### 6.3 `resource_type: "symbols"`  ✅

Resolve a **bare name** (e.g. `require_auth`) to the set of matching **canonical symbol
IDs**. This is the entry point you feed into `get symbol_edges`.

**Input**

| Param | Type | Required | Default |
|---|---|---|---|
| `resource_type` | `"symbols"` | yes | — |
| `bare_name` | string | yes | — |
| `workspace_id` | string | yes | — |
| `limit` | integer | no | 25 distinct canonicals |

**Output (normalized)**

```json
{
  "canonicals": [
    "python:lambda.shared.auth::require_auth",
    "python:lambda.chat.handler::require_auth"
  ],
  "complete": true,
  "truncated": false,
  "total_known": 2
}
```

*Backing:* `handle_find_symbol_by_name` → `lambda/mcp-server/handler.py:1558` →
`symbol_graph_reader.fuzzy_resolve_by_name`.

---

## 7. `get` — precise retrieval

### 7.1 `resource_type: "symbol_edges"`  ✅

Directional graph edges for a symbol or module, selected by the `edge` discriminator. All
four edge kinds return the **same envelope** (`sites[]` + completeness metadata).

**Input**

| Param | Type | Required | Notes |
|---|---|---|---|
| `resource_type` | `"symbol_edges"` | yes | — |
| `edge` | `"callers"` \| `"importers"` \| `"implementers"` \| `"definitions"` | yes | selects the query |
| `target` | string | yes | canonical symbol ID for `callers`/`implementers`/`definitions`; module subpath or `pkg/*` prefix for `importers` |
| `workspace_id` | string | yes | — |
| `limit` | integer | no | default per edge: callers **200**, importers **500**, implementers **100**, definitions **50** |

Per-edge semantics (all **across the workspace's repos**):
- `callers` — every call-site of the target symbol.
- `importers` — every importer of the target module (exact subpath, or prefix when the
  target ends `/*`).
- `implementers` — every implementer of the target interface/abstract type.
- `definitions` — every definition site of the target symbol (list; cross-repo duplicates
  all surface; empty list = not found).

**Output (normalized)**

```json
{
  "edge": "callers",
  "sites": [
    { "file_path": "lambda/license-layer/handler.py", "line": 212,
      "repo": "ai-assist", "source_symbol_id": "python:lambda.license-layer.handler::heartbeat",
      "language": "python" }
  ],
  "complete": true,
  "truncated": false,
  "total_known": 1,
  "graph_sha": "a1b2c3d",
  "staleness_seconds": 900
}
```

> **Ownership note:** each site carries `repo` (bare repo name — no org owner, no
> "service" abstraction). You can cross-reference a symbol to its owning **repository**;
> there is **no service/team/domain ownership field** in the graph output.
> **Traversal note:** each query is **one hop**. There is no transitive/recursive edge
> tool — transitive reach must be assembled client-side (see `blast_radius`, §7.4).

*Backing:* `handle_find_callers` (L1412) / `handle_find_importers` (L1448) /
`handle_find_implementers` (L1483) / `handle_find_definitions` (L1518) in
`lambda/mcp-server/handler.py` → `lambda/shared/symbol_graph_reader.py`.

### 7.2 `resource_type: "symbol_at_line"`  ✅

Resolve `(repo, file, line)` to the innermost enclosing symbol — grounds a line citation
to the symbol that actually owns it.

**Input**

| Param | Type | Required | Notes |
|---|---|---|---|
| `resource_type` | `"symbol_at_line"` | yes | — |
| `workspace_id` | string | yes | — |
| `repo` | string | yes | `owner/name` or bare `name` |
| `file_path` | string | yes | repo-relative |
| `line` | integer | yes | 1-indexed |

**Output (normalized)**

```json
{
  "symbol": { "target_symbol_id": "python:lambda.mcp-server.handler::validate_api_key",
              "file_path": "lambda/mcp-server/handler.py", "line": 579,
              "repo": "ai-assist", "language": "python" },
  "queried_file_path": "lambda/mcp-server/handler.py",
  "queried_line": 601
}
```

Not found → `{ "symbol": null, "queried_file_path": "...", "queried_line": 601 }`.

*Backing:* `handle_find_symbol_at_line` → `lambda/mcp-server/handler.py:1607` →
`symbol_graph_reader.symbol_at_line`.

### 7.3 `resource_type: "source"`  ✅

Fetch a bounded window of **real source** at `(repo, file, line)`, optionally pinned to the
symbol graph's commit so the read is consistent with graph answers. Use this to verify
exact literals/values the code-map summaries deliberately omit.

**Input**

| Param | Type | Required | Notes |
|---|---|---|---|
| `resource_type` | `"source"` | yes | — |
| `workspace_id` | string | yes | — |
| `repo` | string | cond. | `owner/name`; required unless `at_graph_sha` |
| `file_path` | string | cond. | repo-relative; required unless `at_graph_sha` |
| `line` | integer | cond. | 1-indexed anchor; required unless `at_graph_sha` |
| `context_lines` | integer | no | default 20, max 200 (window ≤ `2*context+1` lines) |
| `ref` | string | no | git SHA/branch/tag; default = repo default-branch HEAD |
| `symbol` | string | no | canonical ID; enables self-correction; **required when `at_graph_sha=true`** |
| `at_graph_sha` | boolean | no | resolve `repo`/`file`/`line` from `symbol` and pin `ref` to the workspace graph SHA |

**Output (normalized)**

```json
{
  "repo": "ai-assist", "file_path": "lambda/mcp-server/handler.py",
  "ref": "a1b2c3d", "anchor_line": 579,
  "lines": [ { "line": 579, "text": "def validate_api_key(raw_key):" } ],
  "total_lines": 2400, "window_start": 559, "window_end": 599,
  "clipped_to_file_bounds": false, "symbol": "python:…::validate_api_key",
  "relocated_to_line": null, "relocated": false, "graph_sha": "a1b2c3d"
}
```

*Backing:* `handle_read_source` → `lambda/mcp-server/handler.py:1941` (reads via the
GitHub Contents API under the installation token; graph-anchored mode resolves through
`find_definitions` + the code-maps pointer).

### 7.4 `resource_type: "blast_radius"`  🟡 Roadmap

Transitive impact set of a change to a symbol or file — the closure over callers (and
module importers) an external agent would use to scope a change's review surface.

**Why roadmap, not aspirational:** the capability **exists in the platform** — an internal
impact engine (`lambda/generate-code/impact_analysis.py::run_impact_analysis`, and
`lambda/analyze-pr/structural_review.py`) already composes transitive reach over the same
`callers`/`importers` primitives for codegen and PR review. It is simply **not exposed
over MCP**. Today an external agent can *approximate* it by composing `search symbols` →
`get symbol_edges (callers/importers)` and recursing client-side.

**Work to expose:** wrap the existing internal impact engine (or a bounded transitive
traversal over the reader) as a first-class MCP resource. Proposed contract:

- **Input:** `{ resource_type: "blast_radius", workspace_id, target (symbol ID or repo/file), max_depth?, limit? }`
- **Output (proposed):** `{ "impacted": [ { symbol_id, repo, file_path, line, depth, edge } ], "depth_reached": int, "complete": bool, "graph_sha": string }`

---

## 8. `describe` — structural summaries

### 8.1 `resource_type: "workspace"`  ✅ (partial)

Return a single workspace's metadata (name, description, id). Backed by the same data
`list workspaces` returns; a dedicated single-item form is trivial facade work.

**Input:** `{ resource_type: "workspace", workspace_id }`
**Output:** `{ "workspace_id": "...", "name": "...", "description": "..." }`

*Backing:* metadata subset of `handle_list_workspaces` (`lambda/mcp-server/handler.py:1198`).

### 8.2 `resource_type: "code_map"`  🟡 Roadmap

Return a repo's structural **code-map** — the typed set of platform-authored structural
summaries (primary purpose, architecture pattern, key components, tech stack,
infrastructure, external dependencies, testing strategy; plus per-area summaries such as
handler-group / source-module / infrastructure / frontend).

**Why roadmap:** the code-map content **is** ingested and reachable, but **only indirectly**
— it surfaces as a result of `search knowledge` when it wins retrieval, and its existence
shows up in `list documents`. There is **no first-class "give me repo X's code-map" tool**,
and the internal code-map retrieval lane is not exposed over MCP.

**Work to expose:** add a resource that fetches a repo's code-map document set directly by
`(workspace_id, repo)`. Proposed contract:

- **Input:** `{ resource_type: "code_map", workspace_id, repo, sections? }`
- **Output (proposed):** `{ "repo": "...", "sections": [ { "type": "code-map"|"handler-group-summary"|…, "text": "…" } ], "graph_sha": "…" }`

> The code-map is **narrative prose per repo**, not a structured architecture graph, and
> deliberately omits literals (use `get source` to fetch exact values). Present it as a
> per-repo structural summary, not a machine-diffable model.

---

## 9. Authoritative native tool inventory (what backs this today)

For verification: the MCP server registers exactly **11 native tools** (`MCP_TOOLS`,
`lambda/mcp-server/handler.py:161`). This spec's facade is a re-packaging of these.

| Native tool | Facade mapping |
|---|---|
| `list_workspaces` | `list workspaces`, `describe workspace` |
| `list_workspace_documents` | `list documents` |
| `query_knowledge_base` | `search knowledge` |
| `get_coding_standards` | `search coding_standards` |
| `find_symbol_by_name` | `search symbols` |
| `find_callers` | `get symbol_edges (edge=callers)` |
| `find_importers` | `get symbol_edges (edge=importers)` |
| `find_implementers` | `get symbol_edges (edge=implementers)` |
| `find_definitions` | `get symbol_edges (edge=definitions)` |
| `find_symbol_at_line` | `get symbol_at_line` |
| `read_source` | `get source` |

> **Also dynamically present:** the server can append **proxied tools** from other MCP
> servers assigned to a workspace by an org admin (e.g. a SonarQube catalog, namespaced
> `sonarqube__…`). These are **not** part of the guaranteed native contract — an external
> agent should treat the 11 above as the baseline and call `tools/list` to discover any
> assigned proxied tools.

---

## 10. Aspirational — do NOT present as demonstrable

These read naturally as "next resource_types," but they are **not** real capabilities over
MCP today. Listed here so they are not mistaken for the shipping surface.

| Proposed | Reality | Status |
|---|---|---|
| `ask` verb — hand a natural-language question to the platform's router/orchestrator and get a **synthesized, citation-grounded answer** | The smart router (`lambda/chat/router.py::classify_intent`) is an **intent classifier only**; the orchestration + LLM synthesis lives inside the **chat Lambda** and is reachable **only via the UI/chat channel (SigV4 Function URL)**, never via MCP. An external MCP client gets the granular primitives and must orchestrate itself. | 🔴 Not exposed. Would need a new MCP `ask`/`chat` tool forwarding to chat orchestration. |
| `search component_guidance` — give a symbol/file/module, get guidance scoped to it | No component-aware guidance tool exists. `search coding_standards` is a topic-string wrapper with **no component awareness**. | 🔴 Does not exist. |
| `get relevant_decision_record` — give a diff/change, get the ADR(s) that govern it | Change→ADR relevance logic exists **internally** (`lambda/process-pr-check/check_handlers/adr_compliance.py`) but runs inside the PR-check pipeline and is **not an MCP tool**. Externally you can only retrieve ADRs by semantic `search knowledge`. | 🔴 Not exposed. |
| Inter-service / application-topology graph — "which service calls which service" | **Does not exist.** Cross-repo relationships exist only at **symbol granularity** (`symbol_edges`) or as **prose** inside per-repo code-map summaries. There is no service/application-node topology entity. | 🔴 Does not exist. |
| `blast_radius` (see §7.4) | Internal engine exists; not exposed. Composable client-side today. | 🟡 Roadmap (listed above, not aspirational). |

---

## 11. Demo-ready vs roadmap — one-glance summary

**Demo-ready today (live primitive, demonstrable now under its native name):**
`list workspaces`, `list documents` (top-500 cap), `search knowledge`,
`search coding_standards` (query-templated), `search symbols`,
`get symbol_edges` (callers / importers / implementers / definitions),
`get symbol_at_line`, `get source` (incl. graph-anchored), `describe workspace`.

**Roadmap (capability exists internally or is composable; needs additive MCP work):**
the generic-verb facade itself (thin), `get blast_radius` (wrap existing impact engine),
`describe code_map` (first-class per-repo code-map retrieval).

**Aspirational (do not demo):** `ask`/router-synthesized answers, component-scoped
guidance, change→ADR retrieval, inter-service topology graph.

**Two honesty caveats to carry into any external conversation:**
1. Symbol-graph ownership is **repo-level, not service-level** — there is no service
   ownership metadata and no application-topology graph.
2. `list documents` and any corpus enumeration are **top-500 capped** per workspace, so
   they are not a guaranteed-complete index of ingested documents.
