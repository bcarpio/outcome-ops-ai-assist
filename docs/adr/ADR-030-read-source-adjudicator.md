# ADR-030: Read-Source Adjudicator — Verified Code Values at Answer Time

## Status: Accepted

## Context

The platform's retrieval stack carries three layers, each with a
deliberate gap:

- **Code maps** are LLM narrative summaries grounded on a verified
  symbol skeleton (ADR-028). Per ADR-027 they omit literals and exact
  values — magic key prefixes, byte sizes, thresholds, default
  argument values. The summary will say "uses a token-urlsafe call"
  but not "uses `token_urlsafe(16)`".
- **The symbol graph** stores parser-extracted relationships, signatures
  (per plan-signature-extraction), and file:line anchors for every
  defined symbol. It is ground truth for *where* a construct lives and
  *what it relates to*, but does not store exact runtime values
  (literals, defaults, algorithm choices, etc.).
- **The knowledge base** vectorizes documents and selected code-map
  chunks for similarity retrieval. It does not vectorize raw source
  files (vectorizing every line of every repo is expensive and
  unnecessary — the on-demand path below is cheaper).

So when a chat answer needs an **exact** code value, the three retrieval
layers leave a gap. The summary tier deliberately doesn't carry the
value; the graph tier stores its anchor but not its content; the doc
tier may not mention it. ADR-025 ("code wins over docs") tells the
synthesizer to prefer code over docs on a specific-value disagreement —
but it cannot enforce that when the code's actual value never reaches
the prompt.

Observed failure modes that motivated this ADR:

1. **`token_urlsafe(16)` vs `(32)` confusion.** Several distinct
   constructs in one Lambda used `token_urlsafe` with different sizes.
   The summary correctly named the construct but omitted the size; the
   synthesizer recalled a neighbor's size from training data.
2. **`MAGIC_TOKEN#` vs `USED_TOKEN#` / `JTI#` confusion.** Keyed
   DynamoDB rows in the same handler used several SK prefixes; the
   summary mentioned the rows but omitted the literal prefix. The
   synthesizer invented a plausible-but-wrong prefix.
3. **The no-contradicting-doc silent case.** A fabricated value with
   no corresponding documentation entry never trips a doc-vs-code
   discrepancy gate. A verifier that only fires on observed disagreement
   would let this ship silently.

## Decision

Add a **read-source adjudicator**: an MCP tool that fetches a bounded
window of actual source from the customer's GitHub via the workspace's
GitHub App installation. The chat synthesizer is instructed to call it
**on every specific value claim**, not only when documentation and
code-map appear to disagree.

### Contract

1. **The tool name is `read_source`.** It is exposed on the OutcomeOps
   MCP server alongside `find_callers` / `find_definitions` / etc., and
   appears in the chat synthesis tool catalog for any workspace whose
   MCP catalog includes the OutcomeOps server.

2. **Required inputs:** `workspace_id`. Conditional inputs:
   - **Explicit-anchor mode:** `repo` (`owner/name`), `file_path`,
     `line` (1-indexed). Optional `ref` (commit SHA, branch, or tag —
     default is the repository's default branch HEAD).
   - **Graph-resolution mode:** `at_graph_sha: true` + `symbol`
     (canonical id). The tool resolves the symbol via the workspace
     symbol graph, pins `ref` to the workspace's current `graph_sha`,
     and reads at that pinned ref.
   - Optional `context_lines` (default 20, max 200).

3. **Output shape (JSON in the MCP `text` envelope):**
   - `repo`, `file_path`, `ref`, `anchor_line`, `total_lines`,
     `window_start`, `window_end`, `clipped_to_file_bounds`, `lines`
     (list of `{line, text}`).
   - `symbol` (echoed when supplied) and `relocated_to_line` — the
     line within the returned window where the symbol's bare name was
     found, or `null` when not found. `relocated: true` when the
     symbol was found at a line other than `anchor_line` (HEAD
     drifted from the graph-anchored line).
   - `graph_sha` — the SHA the read was pinned to under
     graph-resolution mode, or `null` otherwise.

4. **Boundedness.** The whole-file fetch is capped at 1 MB; an oversize
   file returns a structured error rather than silently truncating.
   Window size is bounded by `context_lines` × 2 + 1.

5. **Authorization.** The tool reuses the same workspace-access gate
   as the symbol-graph tools (`_check_symbol_graph_workspace_access`).
   A caller without workspace membership receives an error before any
   GitHub call is made. The GitHub App installation token is minted
   from the workspace's own installation; the tool does not cross
   workspace boundaries.

6. **SHA-skew handling.** Out of the two viable read strategies — read
   at HEAD (fresh but stale anchor) vs. read at `graph_sha` (matched
   anchor but possibly stale content) — the tool supports both and the
   prompt instructs the synthesizer to prefer `at_graph_sha: true`
   when a canonical symbol id is available. For explicit-anchor reads,
   the synthesizer can supply `symbol` so the tool reports
   `relocated_to_line` and the answer can self-correct against drift.

7. **The synthesis prompt MUST trigger verification on every specific
   value claim**, not only on observed doc-vs-code disagreement. The
   shared chat system prompt carries a
   `READ_SOURCE_VERIFICATION_RULES` block (see
   `lambda/shared/prompts.py`) that names the tool, explains when to
   call it, and tells the model to quote the returned line with a
   `file:line` citation.

8. **When the source disagrees with a code-map or doc, the source
   wins.** This is the contract ADR-025 already states, but it
   becomes safely enforceable only when `read_source` reliably
   supplies the actual value. The synthesizer is told to surface the
   disagreement explicitly in the user-facing answer.

## Non-goals

- **Vectorizing raw source.** On-demand reads via the GitHub Contents
  API are cheaper than indexing every line of every repo, and they
  are always fresh.
- **Replacing code-maps.** Code-maps remain the narrative + structure
  layer. `read_source` supplies values when summarization fidelity is
  not enough.
- **Exhaustive default-value extraction at ingest.** ADR-027 stands:
  defaults stay out of the graph. `read_source` is the on-demand
  path.

## Consequences

- A specific-value answer that survives this contract cites
  `<file>:<line>` and quotes the actual source line. Reviewers can
  click through and verify.
- A workspace whose MCP catalog does not include the OutcomeOps
  server (e.g., a customer running their own MCP servers only) loses
  the verification tool. The synthesizer in that case must
  acknowledge the value isn't verifiable and decline to guess.
- A read against a repo the workspace's GitHub installation can no
  longer access (revoked, removed, renamed) returns a clear error.
  The synthesizer is told not to fall back to a recalled value.
- IAM: the MCP server's task role grants `ssm:GetParameter` on
  `/<env>/<app>/github/app-id` and `/<env>/<app>/github/private-key`
  in both regions (primary and secondary per the HA-parity rule in
  CLAUDE.md). The GitHub App installation token is minted at request
  time and not cached across requests.
- Rate-limiting: GitHub installation tokens have a per-installation
  request quota; the tool's per-call cost is one
  `access_tokens` call + one Contents API call. Heavy use under a
  single installation could trigger throttling; if observed, the
  follow-up is a short-TTL installation-token cache.
