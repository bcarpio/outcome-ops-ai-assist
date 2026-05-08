# Refusal Detection Design

OutcomeOps treats AI refusals as classified events, not as response
text. Every Bedrock invocation runs through a four-part contract: two
pieces of system-prompt guidance shape *how* Claude refuses, and two
pieces of detector logic on the response shape *what counts* as a
refusal. The combination keeps the audit table's `status=refusal`
count meaningful at any traffic scale.

This page explains the design, why each piece matters, and how
refusals surface to operators and downstream consumers (audit
dashboard, SNS alerts, the SIEM export over Kinesis).

---

## The problem with naive refusal detection

A platform that just regex-matches "I cannot" / "I won't" / "I refuse"
against model output produces both:

- **False positives** when the model is being helpful about scope:
  *"I cannot fully help with that because the workspace doesn't have
  ABAP docs, but I can offer..."* — long, polite, contains the
  canonical refusal phrase. Not a real refusal.
- **False negatives** when the model is verbose about declining:
  *"I'd like to help, but generating phishing material would
  violate..."* — long, indirect, doesn't start with the canonical
  phrase. Real refusal but escapes detection.

A SOC analyst monitoring `status=refusal` on a platform with this
design gets alert fatigue from the false positives and misses the
false negatives that matter. The dashboard's refusal count becomes a
number with no operational meaning.

---

## The four-part contract

OutcomeOps engineers a stable refusal surface through four
coordinated components — two on the prompt side (what Claude is
told), two on the detector side (what gets counted):

| # | Component | Location | What it does |
|---|---|---|---|
| 1 | Refusal-style guidance | `lambda/shared/prompts.py:114` | Tells Claude to refuse briefly with templated phrasing — no context, no alternatives, no preamble |
| 2 | Counter-balance ("don't over-refuse") | `lambda/shared/prompts.py:72,111` | Tells Claude **not** to refuse benign questions about unfamiliar tech (SAP, ABAP, Java) if relevant docs are in the workspace KB |
| 3 | Pattern matcher | `lambda/shared/audit.py:172` `REFUSAL_PATTERNS` | Regex against canonical refusal openers (I cannot / I won't / I must decline / violates my guidelines / etc.) |
| 4 | Length cap | `lambda/shared/audit.py` `REFUSAL_MAX_LENGTH = 250` | Filters out long "I cannot fully help because..." responses that match the patterns but aren't templated refusals |

A response is classified as `status=refusal` only when **both** the
pattern matches **and** the response is at or below 250 characters.
Either signal alone is unreliable; both together produce a stable
signal.

### Why the length cap is the load-bearing decision

The 250-char cap is what most platforms wouldn't think to add and
what makes the rest work:

- *"I'm sorry, I cannot fully help with that request because [600
  words explaining the workspace doesn't have ABAP docs but the user
  could try X, Y, Z]"* → matches the `I'm sorry, I cannot` pattern →
  without the cap, false positive.
- *"I won't help with [vague long thing] because [paragraph of
  context]"* → matches the `I won't` pattern → without the cap, false
  positive.

With the cap, only responses that **start** with a canonical refusal
phrase **and** are short enough to be templated get classified. The
model can't accidentally trigger the classifier by being chatty about
scope; the classifier can't accidentally fire by matching a phrase
inside a long helpful response.

The system-prompt guidance is what makes the 250-char cap reliable in
turn: without instruction #1 telling Claude to keep refusals short
and templated, the model could produce 800-char "thoughtful
explanation of why I'm declining" responses that escape the
classifier. Pattern + length + prompt-shaping are coupled by design.

### Why the counter-balance matters

Without instruction #2 ("don't refuse benign technology questions"),
the model would over-refuse — declining to help with SAP / ABAP /
legacy-tech questions because it's unfamiliar with them, or refusing
to discuss security tooling concepts even when the user is a SOC
analyst building defenses.

Over-refusal pollutes the dashboard with non-events. The paired
guidance — *don't refuse benign things, but when you do refuse,
refuse cleanly* — is what gives the refusal dashboard's count
operational meaning. The number isn't a measure of how cautious the
model is; it's a measure of policy-violating user attempts.

---

## Categories the system actively shapes

The platform's system prompt is engineered around the cybersecurity-
relevant refusal categories an enterprise engineering platform
actually encounters. Anthropic's underlying training refuses on a
broader set (CBRN, election interference, child safety, etc.); the
engineered subset focuses on:

- Phishing / pretexting / social-engineering content
- Credential theft / token extraction / secret exfiltration
- Authentication-bypass tooling (MFA bypass, JWT forgery, session hijacking)
- Brute-force / credential-stuffing payloads
- Data-exfiltration tooling
- Malware / ransomware / backdoors
- Privilege escalation / IAM trust-policy abuse
- Falsifying compliance / audit reports
- Cross-tenant access (reading another workspace's data)
- Injection payloads (SQL, command, etc.) against own-platform infrastructure

These are the surfaces the four-part contract is calibrated for. They
appear in `scripts/backfill-demo-audit-data.py` `REFUSAL_PROMPTS`,
which doubles as the canonical reference for what the platform
should reliably catch.

---

## How refusals surface

A refusal classification triggers the same audit / alert plumbing as
any other Bedrock event, plus an SNS publish for SOC alerting:

### 1. Audit table

Bedrock-category row with `status=refusal` and `flagged_reason`
populated. Queryable via:

```
GSI2PK = "CAT#bedrock"
FilterExpression: status = "refusal"
```

The handler internally paginates because refusals are sparse (<1% of
the bedrock partition).

### 2. SNS topic — `${env}-${app}-audit-alerts`

`audit-alert` Lambda fires on the audit DDB stream and publishes a
structured email to the configured `audit_alert_email` whenever a
refusal row lands. The alert includes actor, IP, prompt, model, and
timestamp.

The Lambda special-cases `flagged_reason=test_refusal_trigger` with a
`[TEST]` SNS subject prefix so on-call distinguishes drills from real
events. Operators verify the pipeline by typing the literal string
`1234567890` in chat — the chat handler short-circuits before calling
Bedrock, returns a hardcoded refusal, and the audit row is tagged with
the test reason. No real ToS-violating prompt is sent to Bedrock, so
there's no risk of an account flag from drilling the alert pipeline.

### 3. Audit dashboard (`/audit`)

Refusals render in red on the Recent Activity feed. Clicking opens a
drawer with the full prompt and response, plus a *"View user's chat
history"* button that narrows the page to that user's bedrock rows in
1h / 3h / 24h windows — the InfoSec drill flow for *"show me what
this user was doing in the lead-up."*

### 4. OCSF / Kinesis (when audit stream export is enabled)

The refusal envelope on the Kinesis stream carries:

| Field | Value |
|---|---|
| `class_uid` | `6003` (API Activity) |
| `class_name` | `"API Activity"` |
| `activity_id` | `2` (Read) |
| `severity_id` | `3` (Medium) |
| `severity` | `"Medium"` |
| `status_id` | `2` (Failure) |
| `status_detail` | `<flagged_reason>` (e.g., `test_refusal_trigger`) |
| `unmapped.input_text` | The user's prompt |
| `unmapped.output_text` | The refusal text |

A SOC pipeline subscribing to the Kinesis stream can filter to
refusals only with the FilterCriteria pattern documented in
[`audit-stream-export.md`](audit-stream-export.md):

```json
{ "data": { "status_id": [2], "class_uid": [6003] } }
```

---

## Why OCSF severity Medium (not High or Informational)

A refusal is the safety substrate functioning correctly — the user
attempted a policy-violating action, the model declined, the platform
logged it. Two boundary calls:

- **Not Critical / High.** If every refusal triggered a Critical
  alert, a SOC analyst with thousands of users in scope would
  (correctly) tune them out. Refusals aren't intrusions; they're the
  system saying no.
- **Not Low / Informational.** Each refusal still represents a
  *potentially malicious user action* worth investigating —
  especially across patterns. One user generating five refusals a
  day is a behavioral signal even if no individual refusal is
  critical.

Medium is the right floor. SOC sees them in normal triage; pattern
detection (`same user generating > N refusals in 24h`) escalates as
needed in the customer's own pipeline.

---

## Why class_uid 6003 (API Activity) and not 2003 (Compliance Finding)

OCSF 2003 Compliance Finding is semantically closer to *"the model
declined a request"* than 6003 API Activity. The platform
deliberately uses 6003 anyway:

- Every Bedrock invocation maps to 6003. Refusals stay on the same
  class as successful invocations, distinguished by `status_id=2` and
  `severity_id=3`. Consumer queries are uniform: filter the bedrock
  class by status_id rather than splitting between two classes.
- 2003 lives in OCSF category 2 (Findings), shaped for security-
  product output (SIEM rules firing on log content). Application
  activity belongs in category 6, where its volume and shape don't
  pollute findings dashboards.

If a customer specifically wants 2003 in their SIEM, the OCSF mapper
in `lambda/shared/ocsf_mapper.py` can be extended later — the wire
format is versioned via `metadata.version` and the `unmapped` field
preserves enough context that re-classification at the consumer side
is straightforward.

---

## Limitations

- **Pattern-and-length drift.** The 250-char cap and the canonical-
  phrase patterns are calibrated against current Claude refusal
  styles. Anthropic model upgrades can shift defaults — re-validate
  `prompts.py` and `audit.py` against the new model on every Sonnet
  / Haiku version bump.
- **Best-effort, not enforcement.** The system prompt is guidance,
  not a hard control. The canonical refusal authority is Anthropic's
  training; the four-part contract is a stability layer on top that
  produces a clean classification surface for downstream consumers.
- **Future model versions could escape the cap.** A future model that
  produces longer refusals would silently degrade the classifier's
  recall. The audit dashboard's refusal count is the canary — a
  sudden drop with no other change is the signal to re-tune.
- **The canonical patterns are English-only.** Customers running the
  platform in non-English-speaking regions need to extend
  `REFUSAL_PATTERNS` for their target languages.

---

## Why this is harder than it looks

A naive refusal-detection design — pattern only, or length only — is
enough to check the box on a feature list. It just produces a number
nobody trusts.

Building the four-part contract requires:

1. **Owning the system prompt actively** (not "send the user message
   to the model and hope")
2. **Treating the prompt as a contract with the detector**, not as
   standalone instructions
3. **Auditing the contract against new model versions** on every
   Sonnet / Haiku rev
4. **Documenting it well enough** that the next engineer doesn't
   break it accidentally by tweaking one of the four pieces in
   isolation

That's the difference between auditing what an LLM *says* and
auditing what an LLM *refuses to do*. SIEM consumers can subscribe
to the Kinesis OCSF stream, key on `class_uid=6003 AND status_id=2`,
and get a clean signal — every event is a real policy-relevant
refusal, and every refusal generates exactly one event.

---

## See also

- [`audit-coverage.md`](audit-coverage.md) — every event the platform
  records, including the bedrock category that refusals live in.
- [`audit-dashboard.md`](audit-dashboard.md) — operator's guide to
  the `/audit` page, including the refusal drill flow.
- [`audit-stream-export.md`](audit-stream-export.md) — Kinesis / OCSF
  wire format for SIEM consumers; FilterCriteria pattern to subscribe
  to refusals only.
- [ADR-018](adr/ADR-018-audit-logging-design.md) — audit table schema
  and writer split.
- `lambda/shared/prompts.py` — system-prompt guidance (instructions 1
  and 2).
- `lambda/shared/audit.py` — refusal pattern matcher and length cap
  (instructions 3 and 4).
- `lambda/shared/ocsf_mapper.py` — OCSF envelope construction
  including the refusal severity / status mapping.
- `lambda/audit-alert/handler.py` — SNS publisher for the SOC
  alert path.
- `scripts/backfill-demo-audit-data.py` — `REFUSAL_PROMPTS` list,
  canonical reference for categories the contract is calibrated for.
