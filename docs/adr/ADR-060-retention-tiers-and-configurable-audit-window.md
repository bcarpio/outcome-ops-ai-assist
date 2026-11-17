# ADR-060: Retention tiers — configurable audit hot-window, bounded chat cache, and conversation TTL parity

## Status: Accepted

## Context

A 2026-11-16 investigation (an old chat opening empty in the UI) surfaced three
entangled facts about how long the platform keeps data, and showed the retention
model was partly undocumented and partly accidental.

The platform keeps conversational/audit data in three distinct places with very
different purposes, but only one of them had its retention consciously chosen:

| Store | Purpose | Holds | Retention (before this ADR) |
|---|---|---|---|
| **chat table** (`dynamodb-chat.tf`) | live UI conversation thread — a cache | message text, conversation metadata | 90d TTL on `MSG#` rows (`CHAT_MESSAGE_TTL_SECONDS`), tunable; **metadata had no TTL** |
| **audit table** (`dynamodb-audit.tf`) | hot, in-app-queryable record for Analytics + the audit UI | `input_text` + `output_text` (≤50k each), cost, tokens, actor, IP | **365d, hardcoded** in `shared/audit.py` |
| **Kinesis export** (`kinesis-audit-export.tf`, `docs/audit-stream-export.md`) | real-time feed into the customer's own SIEM / data lake | every audit event (OCSF) | conveyor (24h buffer); durable retention is the customer's downstream |

Three problems followed from this:

1. **The durable-retention story was implicit.** Long-term/compliance retention
   is by design the customer's responsibility, fed by the Kinesis SIEM export
   (`docs/audit-stream-export.md`, ADR-018). This is the correct shape for the
   deploy-in-customer-account model (the vendor never hoards customer data), but
   it was never stated as a *retention decision*, so "where does perpetual
   retention live" had no documented answer.

2. **The audit hot-window was hardcoded at 365 days.** This is the window
   Analytics and the audit UI can show. It is a legitimately customer-variable
   parameter: the security/SIEM team and the team operating the OutcomeOps
   platform can be two different groups with two different needs, and a regulated
   buyer may want more (or less) *in-app queryable* history independent of what
   their SIEM retains long-term. Changing it required a code change.

3. **Chat conversations outlived their own messages ("ghost" conversations).**
   The 90d TTL (fable-scale chunk 10) was applied to `MSG#` rows only. The
   `METADATA` row (and `SHARE#` rows) carried no `expires_at`, so once the
   messages aged out, the conversation kept listing — with a stored
   `message_count` — but opened empty. Observed live on a 2026-08-01 conversation
   (107 days old; its messages passed the 90d TTL ~2026-10-30 and were reaped by
   DynamoDB, metadata survived).

## Decision

### 1. Name the retention tiers; durable retention is the SIEM export.

The three stores above are the retention model. **Long-term and compliance
retention live downstream in the customer's SIEM**, fed by the Kinesis export —
not in any platform DynamoDB table. The two DynamoDB tables are bounded working
stores: the chat table is a UI cache, the audit table is a queryable hot window.

### 2. The audit hot-window is tunable, via `audit_retention_days`.

`shared/audit.py` reads `AUDIT_RETENTION_DAYS` (env, default 365) to compute the
`ttl` attribute, replacing the hardcoded 365. A new tfvars variable
`audit_retention_days` (default 365) is wired as `AUDIT_RETENTION_DAYS` on every
Lambda/Fargate task that writes audit rows — everywhere `AUDIT_TABLE` is already
wired, primary and secondary (HA parity).

### 3. Both DynamoDB windows are **bounded — no `0 = forever`.**

Neither the chat TTL nor the audit retention supports a perpetual setting:

- The audit row holds up to ~100KB of prompt+response text at Bedrock write
  volume; an unbounded hot store grows cost without limit, and the export already
  covers "keep it forever, cheaply, in the customer's store." So perpetual DDB
  retention is a cost footgun, and the right answer to "keep audit forever" is
  **consume the export**, not raise this to 0.
- For chat, perpetual retention of a UI cache is pointless — the durable copy of
  any Q&A is the audit row.

Enforcement is defense-in-depth:
- tfvars `validation` blocks: `audit_retention_days >= 1`,
  `chat_message_ttl_seconds >= 86400` (1 day). `0` is rejected at plan time —
  which also closes the pre-existing footgun that `chat_message_ttl_seconds = 0`
  meant *instant* expiry (every message reaped on write), not "forever".
- Code floors: `shared/audit.py` falls back to 365 on a non-positive/unparseable
  `AUDIT_RETENTION_DAYS`; `chat` falls back to 90d on a sub-1-day/unparseable
  `CHAT_MESSAGE_TTL_SECONDS` (`_chat_ttl_seconds`). A misconfigured env can never
  write an instant-expiry or never-expiring row.

### 4. Conversation TTL parity — conversations age out whole.

The chat `METADATA` row (and `SHARE#` rows) now carry the same `expires_at` as
their messages:
- `create_conversation` stamps `METADATA.expires_at = now + chat_ttl` (so an
  abandoned, never-messaged conversation also ages out).
- `update_conversation` refreshes `METADATA.expires_at = now + chat_ttl` on every
  message, and `_sync_share_items` refreshes the `SHARE#` rows to match.
- `handle_share_conversation` stamps new `SHARE#` rows with `expires_at`.

Because `update_conversation` runs immediately after `save_message`, the metadata
expiry is always `>=` the newest message's expiry — the conversation outlives its
last message by a hair and never orphans a message (which would 404 the thread),
then ages out whole once the last message expires. No more ghost conversations.

This is **forward-only**: rows written before this ADR have no `expires_at` on the
metadata and keep today's behavior (perpetual metadata). Already-expired message
rows are gone — DynamoDB TTL deletion is irreversible; there is no backfill.

## Consequences

### Positive
- The retention model is explicit and documented: UI cache (chat) vs. queryable
  hot window (audit, now tunable) vs. durable/compliance (customer SIEM export).
- Platform operators and security/SIEM teams can set the in-app audit window
  independently of long-term SIEM retention.
- Ghost conversations are eliminated; conversations and their shares age out as a
  unit.
- Both DynamoDB stores stay cost-bounded by construction (validation + code
  floors); perpetual retention is steered to the cheap, correct place (the export).

### Tradeoffs / limits
- A longer `audit_retention_days` directly increases audit-table storage cost
  (the row carries up to 100KB of text). The default stays 365; raising it is a
  conscious cost choice, and the variable description says so.
- No perpetual in-DDB option by design — a customer who wants perpetual *in-app
  queryable* audit (not just in their SIEM) is not served by this table; that
  would be a separate decision (e.g. an archival query path over the export),
  deliberately out of scope.
- Historical ghost conversations (metadata written before this ADR) persist until
  an operator cleans them up; only their emptiness is now explained, not removed.

## Related ADRs / docs
- ADR-018: audit logging design (the audit table + stream).
- `docs/audit-stream-export.md`: the Kinesis SIEM export — the durable-retention
  tier this ADR leans on.
- ADR-022: multi-region active/passive (why the audit env wiring mirrors to the
  secondary).
- fable-scale chunk 10 (`ee7fb0b`): introduced the chat message TTL + content cap
  that this ADR completes (metadata parity) and reframes (bounded cache, not
  retention policy).

## References
- `terraform/variables.tf` — `audit_retention_days` (new, default 365, `>= 1`);
  `chat_message_ttl_seconds` (now `>= 86400` validation).
- `lambda/shared/audit.py` — reads `AUDIT_RETENTION_DAYS`, bounded fallback to 365.
- `lambda/chat/handler.py` — `_chat_ttl_seconds` (bounded helper); `METADATA` and
  `SHARE#` TTL parity in `create_conversation`, `update_conversation`,
  `_sync_share_items`, `handle_share_conversation`.
- `AUDIT_RETENTION_DAYS` wired alongside `AUDIT_TABLE` across all audit-writing
  Lambda/Fargate modules (primary + secondary).

## Version History
- v1.0 (2026-11-16): Initial. Names the three retention tiers, makes the audit
  hot-window tunable (`audit_retention_days`, default 365), forbids `0=forever`
  in both DynamoDB stores (validation + code floors, also closing the
  `chat_message_ttl_seconds=0` instant-expiry footgun), and adds conversation
  METADATA/SHARE# TTL parity to end ghost conversations.
