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

## v1.1: Platform-agnostic dispatch (Azure DevOps parity)

`read_source` is platform-agnostic. The workspace's connected
code-hosting platform decides which side the fetch runs on:

- **GitHub-connected workspace** (has a `GITHUB_INSTALLATION#` row):
  fetch via `api.github.com/repos/{owner}/{name}/contents/{path}`
  using an installation access token minted from the GitHub App
  JWT.
- **Azure DevOps-connected workspace** (has an `AZDO_REPO#` row
  whose `connection_id` points at an `AZDO_CONNECTION#` OAuth
  connection): fetch via
  `dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items?path=...&$format=text`
  using an OAuth access token refreshed through the customer's
  Entra ID tenant.

The `repo` parameter shape identifies the platform:
- `owner/name` (1 slash) — GitHub.
- `org/project/repo` (2 slashes) — Azure DevOps.

The router cross-checks the shape against the workspace's actual
connection and rejects mismatches with a routing error rather than
silently trying the wrong fetcher.

The response payload carries a `platform: "github" | "azure_devops"`
field so external agents can log which side served the read.

`at_graph_sha` behavior is identical across platforms: the symbol
graph's `graph_sha` is a Git-style 40-char commit SHA on both
sides. On the ADO branch the SHA is passed to the items API as
`versionDescriptor.version=<sha>` +
`versionDescriptor.versionType=commit`.

### v1.1 Consequences

- **A workspace connected via Azure DevOps can now use
  `read_source`.** Prior to this change, ADO workspaces got
  "no GitHub App installation linked" regardless of what they
  connected. This closed a completeness gap in ADR-030 relative
  to the ADO parity work in ADR-018 (ADO ingestion) and the ADO
  PR-analysis pipeline.
- **IAM addition (ADO branch).** The MCP server's task role now
  needs `ssm:GetParameter` on the parameter named in
  `AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM` plus `kms:Decrypt` on
  that SecureString's KMS key. `dynamodb:GetItem` on the
  workspaces table already covers the `AZDO_CONNECTION#` and
  `AZDO_REPO#` reads.
- **Rate-limiting (ADO branch).** ADO has separate throttling
  thresholds from GitHub. The tool's per-call cost is one
  `refresh_access_token` call + one items API call. Heavy use
  under a single tenant could trigger throttling; if observed,
  the follow-up is a short-TTL OAuth-token cache.
- **Mixed-connection workspaces (both GitHub AND ADO on one
  workspace) are not supported today.** The resolver
  short-circuits to GitHub; the router's cross-shape check will
  reject an ADO-shaped repo against such a workspace. Deferred
  until a customer surfaces the requirement — no in-product
  design for this yet.

## v1.2: Chat reads the source itself

### Context

The v1.0 decision told "the chat synthesizer" to call `read_source` on
every specific value claim. Chat never had that tool: `read_source` is an
MCP-server tool, and only an MCP client's own model (Claude Code, Cursor)
can call it. Chat and the RAG path (`ask-claude`, used by query-kb) are
single-shot synthesis with no tool loop. Both prompts still carried the
"call the `read_source` MCP tool" rule, so an answer could read as if a
value had been checked when it had not.

The gap showed up on a magic-link question. The code-map for the auth
handler was current, but, as ADR-027 intends, it described the error
outcomes by role instead of quoting their codes, and it merged two of
them. With no source line in context, the model filled the gap with
plausible but wrong error codes. ADR-033 v1.1 makes code-maps win the
retrieval contest more often, which raises the cost of that gap.

### Decision

1. **One shared reader.** Host resolution, token minting, repo-slug
   resolution, the file fetch and the window slice move from the MCP
   server into `lambda/shared/source_reader.py`. The MCP `read_source`
   tool and chat both call it, so the two surfaces cannot drift.
2. **Chat attaches source lines server-side.** When hybrid retrieval
   (ADR-059) resolves the question's symbol in the graph, it returns
   the definition sites (`graph_definitions`). Chat reads a window of
   real lines for up to `chat_source_max_symbols` sites (default 3),
   `chat_source_lines_per_symbol` lines each (default 80), pinned to the
   symbol graph's commit SHA so the graph's line numbers match the
   content. The windows go into the context as a `SOURCE LINES` block
   labelled with file, line span and commit.
3. **Prompt rule names the block, not a tool.** `SOURCE_LINES_GROUNDING_RULES`
   replaces the tool-call rule in both the chat and RAG prompts:
   - exact values come from `SOURCE LINES`, quoted and cited as file:line;
   - source lines win over code-map summaries and documentation;
   - a summary's paraphrase is never turned into a specific value;
   - with no source line, the answer describes the outcome by role and
     says the exact value needs checking in source.
4. **Fail-soft and bounded.** A read failure (no host connection, token
   refresh failure, 404, file over 1 MB) never fails the chat turn. The
   block lists the symbol as unreadable so the model says the value is
   unverified. The block is capped at about 24,000 characters.
   `chat_source_lines_enabled` is the kill switch.

### v1.2 Consequences

- **Exact values in chat come from code** for any question whose
  symbol resolves in the graph. This is the read-from-source guarantee
  ADR-027 names as its durable fix.
- **Coverage follows the graph.** A question that names no resolvable
  symbol gets no source lines. The prompt rule still stops the model
  from inventing a value; it says the value needs checking instead.
- **Chat mints code-host tokens.** The chat Lambda (primary and
  secondary) gains the same env and grants analyze-pr already has:
  - Azure DevOps and GitLab OAuth env;
  - `kms:Encrypt` on the OAuth-tokens key and `PutItem` / `UpdateItem` on
    the workspaces table, so a refresh can persist the rotated refresh
    token;
  - read on customer-set client-secret SSM paths;
  - `PyJWT[crypto]` to sign the GitHub App JWT.
- **Cost and latency.** At most `chat_source_max_symbols` file fetches
  per graph-firing turn, plus one token mint per host. No change for
  turns where the graph does not fire.

## v1.3: Source lines when the question names no symbol

### Context

v1.2 reads source only when hybrid retrieval resolves a symbol the
question names. A question that names behavior instead of a function
("how do magic links work, what is in the token, how long is it
valid") resolves nothing, so no code was read. Code is indexed only as
code-maps, and code-maps omit literal values (ADR-027), so the only
retrieved text stating those values was documentation. A stale doc
then supplied the literals. The prompt rule made the model label them
doc-sourced, but it could not settle a disagreement between two docs,
because the code that settles it was never in the context.

The top code-map hits for such a question already name the functions
that implement the behavior: the auth handler summary names
`create_magic_token`, `verify_magic_token` and `_claim_magic_token_jti`.

### Decision

When the graph lane returns no definition sites, chat derives them
from the top code-map hits (`hybrid_augment.code_map_definitions`):

1. Take the top 10 code-map hits (by the merged ranking) whose workspace
   the caller searched. The summary naming the function that holds the
   answer is often not in the top three.
2. Extract snake_case and camelCase names from each hit's summary text.
3. Keep names that share at least one word with the question (plurals
   folded, stopwords dropped). Rank first by how many question words
   the name shares, so a name matching one incidental word ("app") never
   beats one matching two. Break ties by rarity: each shared word counts
   1 / (number of candidates containing it), so a topic word every
   candidate shares ("magic" in a magic-link question) counts for little
   and an attribute word few share ("token") decides. Remaining ties go
   to the larger share of the name's words matched, then to order of
   appearance.
4. When reranking is enabled, send the top 20 of that ranking to the
   reranker chat already uses (Cohere Rerank, one call), each judged on
   the summary sentences that mention it, and take its order. Word
   statistics cannot tell the topic of a question ("magic link") from
   what is asked about it ("token", "how long"): in a real auth summary
   "token" is in more names than "link", so step 3 alone picked the
   request-side functions. If the rerank fails, the step 3 order stands.
   At most 6 candidates are resolved per turn.
5. Resolve each name through the bare-name index and take the first
   definition that lives in the hit's own repo and is not under a test
   path.
6. Feed the result to the same reader as v1.2, under the same
   `chat_source_max_symbols` and `chat_source_lines_per_symbol` caps
   and the same `chat_source_lines_enabled` kill switch.

It makes DynamoDB reads and at most one rerank call (no LLM call), and
it is fail-soft: a rerank error keeps the word ranking, and any other
error yields no definitions, so the turn answers as before.

### v1.3 Consequences

- **"Code wins" no longer depends on the question naming a function.**
  Behavior-shaped questions get the implementing functions' real lines
  whenever a top code-map hit names them.
- **Selection is lexical.** A function whose name shares no word with
  the question is not chosen, even when it holds the answer. The cap of
  3 definitions can also leave a relevant helper out. Both fail toward
  the v1.2 behavior: the value is reported as needing a check in
  source, not guessed.
- **Code-map metadata does not record covered files or symbols.** Names
  come from summary prose. Recording each code-map's file paths and top
  defined symbols at generation time would make the mapping exact and
  is deferred to the next code-map regeneration (ADR-027 v1.3).
- **Cost.** At most 6 bare-name lookups plus one definitions lookup per
  resolved canonical, only on turns where the graph lane found nothing.

## Version History

- v1.0: MCP `read_source` tool, GitHub via the workspace's App installation.
- v1.1: Platform-agnostic dispatch (Azure DevOps; GitLab via ADR-050).
- v1.2 (2026-11-22): Shared `source_reader` module; chat attaches source
  lines for graph-matched symbols server-side; the chat and RAG prompts
  name the `SOURCE LINES` block instead of a tool they do not have.
- v1.3 (2026-11-23): When the question names no symbol, chat derives
  definition sites from the top code-map hits and attaches their source
  lines.
