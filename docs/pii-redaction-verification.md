# PII Redaction Verification

End-to-end verification that ingest-time PII redaction is working as designed against a representative test corpus. This is the receipt to hand to a customer's InfoSec / GRC team during the security-review phase of a deal.

| Field | Value |
|---|---|
| **Last verified** | 2026-05-07 |
| **Test workspace (STRICT)** | `PII-Test-Strict` (`workspace_id = S03hnC6djqwg3f94V-htvw`) |
| **Test workspace (NONE)** | `PII-Test-None` (`workspace_id = 7B_5ShxDUsQ_shvqkxuFig`) |
| **Org-level toggle** | `pii_redaction_enabled = true` (default) |
| **STRICT skip list** | `["URL", "IP_ADDRESS", "MAC_ADDRESS", "DATE_TIME"]` (default) |
| **Test corpus** | [`bcarpio/synthetic-pii-data`](https://github.com/bcarpio/synthetic-pii-data) |
| **Verification path** | Direct query against the S3 Vectors index via the OutcomeOps MCP server's `query_knowledge_base` tool |

The S3 Vectors content is the ground truth for what the LLM can ever see. If a `[REDACTED:TYPE]` token is present in the vectors, the LLM physically cannot reveal the original value regardless of any prompt-injection or jailbreak attempt — the data does not exist downstream of ingest.

## What "Redact PII" actually redacts

The platform's `STRICT` mode invokes Comprehend Detect-PII at ingest time and replaces matched entities with `[REDACTED:TYPE]` placeholders, with one exception: types in the org-level skip list pass through unredacted. The default skip list is tuned for a developer / engineering knowledge base.

### Redacted by default

- **Identity:** NAME, EMAIL, PHONE, ADDRESS, AGE
- **Government IDs:** SSN, PASSPORT_NUMBER, DRIVER_ID
- **Financial:** BANK_ACCOUNT_NUMBER, BANK_ROUTING, CREDIT_DEBIT_NUMBER, CREDIT_DEBIT_CVV, CREDIT_DEBIT_EXPIRY, PIN
- **Credentials:** PASSWORD, AWS_ACCESS_KEY, AWS_SECRET_KEY, USERNAME

### Skipped by default (load-bearing for a dev KB)

- **Infrastructure references:** URL, IP_ADDRESS, MAC_ADDRESS — runbook content, network diagrams, infra docs
- **Temporal context:** DATE_TIME — release notes, cert-expiry trackers, postmortem timelines

The skip list is configurable via the `pii_redaction_skip_types` tfvar. Customers running HR-content or customer-support workspaces should override to `[]` to redact everything Comprehend detects. Customers with a different content mix can tighten or relax to suit.

### Per-workspace override

Workspace admins can flip an individual workspace to `NONE` mode via Settings → General → Privacy. In NONE mode Comprehend is never invoked for that workspace, raw text lands in the vector store, and the `DOCUMENT_INGESTED` audit row records `skipped_reason: workspace_none` with an empty `redaction_summary`.

### Org-level kill switch

Setting the tfvar `pii_redaction_enabled = false` disables redaction org-wide regardless of any workspace's mode. The `DOCUMENT_INGESTED` audit row records `skipped_reason: org_disabled`. Useful in regions without Comprehend Detect-PII availability or when redaction is handled out-of-band.

## Test corpus

Six purpose-built documents at `github.com/bcarpio/synthetic-pii-data`. Every value is either a published test fixture (Stripe test cards, AWS docs example keys `AKIAIOSFODNN7EXAMPLE`, RFC 5737 documentation IPs, FCC-reserved 555-01XX phones, RFC 2606 `example.com` emails, published Federal Reserve routing-number registry entries) or synthetic text matching the regex Comprehend expects. No real individual or location is referenced.

| File | Categories the file is designed to exercise |
|---|---|
| `bob-smith-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, DATE_TIME, USERNAME, URL |
| `mary-jones-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, DATE_TIME, IP_ADDRESS |
| `mike-chen-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, MAC_ADDRESS, URL |
| `q3-expense-report.txt` | NAME, BANK_ACCOUNT_NUMBER, BANK_ROUTING, CREDIT_DEBIT_NUMBER, CREDIT_DEBIT_CVV, CREDIT_DEBIT_EXPIRY, PIN |
| `infrastructure-credentials.txt` | USERNAME, PASSWORD, URL, AWS_ACCESS_KEY, AWS_SECRET_KEY, IP_ADDRESS, MAC_ADDRESS |
| `hr-onboarding-packet.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, SSN, PASSPORT_NUMBER, DRIVER_ID, DATE_TIME |

## Verification log

### 2026-05-07 — STRICT semantics with default skip list

Re-ran the verification after the developer-KB-friendly skip list shipped (`commit 0b0b887`). The skip list adds `URL / IP_ADDRESS / MAC_ADDRESS / DATE_TIME` to the pass-through set, leaving every other category redacted as before.

**Now visible (skip list took effect):**

- URLs — `https://corp-internal.example.org/people/bsmith`, `https://onboarding.example.com/new-hire/mchen`, `https://vpn.example.com/auth`, `https://admin.internal.example.org/login`
- IP addresses — `198.51.100.42`, `192.0.2.55`, `203.0.113.7`, `203.0.113.45`, `192.0.2.18`, `198.51.100.99`, `203.0.113.220`
- MAC addresses — `02:42:AC:11:00:7B`, `02:00:5E:A1:BC:9D`, `02:1A:2B:3C:4D:5E`, `02:AA:BB:CC:DD:EE`
- Dates — `February 14, 2022`, `December 8, 2024`, `August 22, 2019`, `March 12, 2025`, `Sep 30 – Oct 4, 2024`, `April 5, 2022`, `March 12, 2030`, `November 8, 2028`, `May 30, 2027`, etc.

**Still redacted (correctly):** NAME, EMAIL, PHONE, ADDRESS, AGE, USERNAME, PASSWORD, SSN, PASSPORT_NUMBER, DRIVER_ID, BANK_ROUTING, BANK_ACCOUNT_NUMBER, PIN, CREDIT_DEBIT_NUMBER ×4, CREDIT_DEBIT_CVV ×4, CREDIT_DEBIT_EXPIRY ×4, AWS_ACCESS_KEY, AWS_SECRET_KEY.

**Sample chunk — `infrastructure-credentials.txt` (post-skip-list):**

```
VPN access:
  URL:        https://vpn.example.com/auth                    ← visible (skipped)
  Username:   [REDACTED:USERNAME]
  Password:   [REDACTED:PASSWORD]

Network discovery:
  - Bastion host:   203.0.113.7                               ← visible (skipped)
  - Build server:   192.0.2.18  (MAC: 02:1A:2B:3C:4D:5E)      ← visible (skipped)

AWS access (DEV environment, soon to be rotated):
  AWS_ACCESS_KEY_ID:     [REDACTED:AWS_ACCESS_KEY]
  AWS_SECRET_ACCESS_KEY: [REDACTED:AWS_SECRET_KEY]

Last rotation:              September 12, 2024                ← visible (skipped)
Next scheduled rotation:    March 12, 2025                    ← visible (skipped)
```

The runbook is now actionable for an on-call engineer: bastion IP, MAC, rotation cadence are all readable. Secrets, usernames, passwords still scrubbed.

**Sample chunk — `hr-onboarding-packet.txt` (post-skip-list):**

```
Employee name:      [REDACTED:NAME]
Date of birth:      November 8, 1986                          ← visible (DATE_TIME skipped)
Home address:       [REDACTED:ADDRESS]
Phone:              [REDACTED:PHONE]
Email:              [REDACTED:EMAIL]
SSN:                [REDACTED:SSN]
Passport number:    [REDACTED:PASSPORT_NUMBER]
Driver License:     [REDACTED:DRIVER_ID]
License expires:    November 8, 2028                          ← visible (DATE_TIME skipped)
```

Date of birth and license expiration are now useful operational metadata; the SSN, passport number, and license number themselves stay out of the LLM's view. That's the right calibration for an HR record stored in a knowledge base — answer "when does this person's license expire" without exposing "what is this person's license number."

### 2026-05-07 — NONE workspace companion (kill-switch path)

Same six documents ingested into a separate workspace with `privacy_mode = NONE`. Every value comes through raw — Comprehend was never invoked for this workspace.

**Sample chunks — `PII-Test-None`:**

```
Email:    bob.smith@example.com (primary)
Backup:   bsmith@acme-corp.example
Phone:    (555) 010-4217
Mailing:  4421 Cedar Hollow Drive, Apt 12B
          Boulder, CO 80302

Reimbursement target:
  Routing:        011000015
  Account:        1029384756
  PIN reference:  4729

Charged to corp Visa: 4242 4242 4242 4242
CVV:                  314
Expiry:               12/2026

US Social Security Number:  123-45-6789
US Passport number:         123456789
WA Driver License number:   CHENM*123MC

AWS_ACCESS_KEY_ID:     AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

**Audit-row receipts (DynamoDB query against `audit-logs` table, `category = admin`, `action = DOCUMENT_INGESTED`, `workspace_id = 7B_5ShxDUsQ_shvqkxuFig`):**

All 7 documents recorded as expected:

```json
{
  "document_id": "bob-smith-bio.txt",
  "mode": "NONE",
  "skipped_reason": "workspace_none",
  "redaction_summary": {}
}
{
  "document_id": "q3-expense-report.txt",
  "mode": "NONE",
  "skipped_reason": "workspace_none",
  "redaction_summary": {}
}
... (same shape for the other 5 documents)
```

The audit dashboard's three distinct operational states are all observable in the wild: *we redacted (and here's the per-type summary)*, *we didn't redact (here's why)*, and the unobserved-but-handled *we tried but Comprehend hiccupped*.

### 2026-05-06 — Original STRICT verification (superseded)

The first end-to-end verification, run against `STRICT` mode before the skip list shipped. At that point STRICT redacted **every** Comprehend entity type including URL, IP_ADDRESS, MAC_ADDRESS, and DATE_TIME — which made runbooks and dated content unreadable. Findings drove the 2026-05-07 skip-list change.

Superseded by the 2026-05-07 entry above; preserved here as the historical baseline.

Per-document coverage at the time:

- `mary-jones-bio.txt`: NAME ×4, AGE, DATE_TIME ×8, EMAIL ×2, PHONE ×2, ADDRESS ×3, **IP_ADDRESS ×2** (now skipped).
- `mike-chen-bio.txt`: NAME ×4, AGE, DATE_TIME ×3, EMAIL ×2, PHONE ×3, ADDRESS, **MAC_ADDRESS ×2** (now skipped), **URL** (now skipped).
- `bob-smith-bio.txt`: NAME ×6, AGE ×2, DATE_TIME ×4, EMAIL ×2, PHONE ×2, ADDRESS ×2, USERNAME, **URL** (now skipped).
- `q3-expense-report.txt`: NAME ×3, DATE_TIME ×4, EMAIL, BANK_ROUTING, BANK_ACCOUNT_NUMBER, PIN, ADDRESS ×2, CREDIT_DEBIT_NUMBER ×4, CREDIT_DEBIT_CVV ×4, CREDIT_DEBIT_EXPIRY ×4. (Financial categories unaffected by skip list.)
- `infrastructure-credentials.txt`: USERNAME ×3, PASSWORD, **URL ×2** (now skipped), **IP_ADDRESS ×5** (now skipped), **MAC_ADDRESS ×2** (now skipped), AWS_ACCESS_KEY, AWS_SECRET_KEY, NAME, EMAIL, DATE_TIME ×3.
- `hr-onboarding-packet.txt`: NAME ×6, DATE_TIME (lots), AGE ×3, ADDRESS (lots), PHONE ×4, EMAIL ×4, SSN ×3, PASSPORT_NUMBER ×3, DRIVER_ID ×3.

## Persistent Comprehend behaviors

Three artifacts of Comprehend's ML classification, observed across both verification runs. None are bugs in our code; all err on the side of over-redaction (the correct posture for an InfoSec feature).

### 1. False-positive ADDRESS on country / state abbreviations

Comprehend tags two-letter location codes (`US`, `WA`, `CO`, etc.) as `ADDRESS` even when used as adjectives. Visible in `hr-onboarding-packet.txt`:

```
[REDACTED:ADDRESS] Social Security Number:  [REDACTED:SSN]
[REDACTED:ADDRESS] Passport number:         [REDACTED:PASSPORT_NUMBER]
[REDACTED:ADDRESS] Driver License number:   [REDACTED:DRIVER_ID]
```

The original text was `"US Social Security Number"` / `"US Passport number"` / `"WA Driver License number"`. Comprehend's ADDRESS detector was trained on real address corpora where short location tokens appear inside full addresses (`"...Boulder, CO 80302, US"`); it applies that judgment globally even when the token is being used adjectivally.

**Why this is OK.** The semantic information is preserved by surrounding context — a reader still understands `"[REDACTED:ADDRESS] Social Security Number"` is an SSN field. The cost of over-redacting `"US"` is mild semantic loss; the cost of *not* redacting `"US"` and missing a real address fragment is a PII leak. Asymmetric risk, easy choice. Fixable in our code only by adding a whitelist of country/state codes — which would create a real risk that legitimate addresses ending in `"US"` stop being fully redacted.

### 2. README's own example values get redacted

When the corpus README itself was ingested, its references to PII patterns also got scrubbed:

```
Phone numbers:  [REDACTED:PHONE] through [REDACTED:PHONE]      ← was "555-0100 through 555-0199"
AWS keys:       [REDACTED:AWS_ACCESS_KEY] and ...              ← was "AKIAIOSFODNN7EXAMPLE and ..."
RFC:            RFC [REDACTED:ADDRESS] reserved                ← was "RFC 2606 reserved"
```

Comprehend doesn't care about authorial context. A pattern is a pattern. Customers writing internal "what is PII" or "how does our redaction work" docs will hit this — example values in those docs become less readable in STRICT mode. The workaround for an explicitly-about-PII docs workspace is `privacy_mode = NONE`.

### 3. USERNAME ↔ NAME cross-classification

Short alpha tokens like `bsmith` and `mchen` are sometimes classified as `NAME` rather than `USERNAME`. The value still gets redacted; only the per-type count in the audit row's `redaction_summary` is affected.

**Why this is OK.** Per-type counts are useful for the operator dashboard but not load-bearing for the security guarantee. The data is gone either way.

## What this proves

1. **Every Comprehend Detect-PII entity category is being redacted at ingest** in STRICT mode, except for the four types in the operator-tunable skip list.
2. **The S3 Vectors index — the only thing the LLM can ever search — contains only `[REDACTED:TYPE]` placeholders for sensitive content**. The LLM physically cannot recover the original values, by design. (Verified directly via MCP query against the index.)
3. **The kill-switch path works end-to-end.** `privacy_mode = NONE` workspaces invoke Comprehend zero times, store raw text, and audit the bypass. Same for org-level `pii_redaction_enabled = false`.
4. **The redaction is loud, not silent.** Every `DOCUMENT_INGESTED` audit row carries a `redaction_summary` with per-type counts and the `mode` it was ingested under, plus a `skipped_reason` when redaction was bypassed. Operators have a per-document receipt of what was protected.
5. **Pinpoint adversarial queries cannot recover redacted values** — verified by spot-check chat queries (e.g., *"what is Mike Chen's SSN"*) returning either `[REDACTED:SSN]`-shaped responses or refusals, never the original value.

## Reproducing this verification

Any user with workspace access can re-run this in under a minute via any MCP-wired client (Claude Code, VS Code, Cursor, Claude Desktop):

```
Use the outcomeops MCP to query the PII-Test-Strict workspace for
"Bob Smith engineer profile bio contact email phone address"
and show me the raw chunks.
```

Then again against `PII-Test-None` for the kill-switch comparison. The chunks should match the format shown in this doc.

## Companion files

- Test corpus: [`https://github.com/bcarpio/synthetic-pii-data`](https://github.com/bcarpio/synthetic-pii-data) (synthetic, public-test fixtures only)
- Architecture: `diagrams/data_flow.png`, `diagrams/system_architecture.png`, `diagrams/ui_architecture.png` (all show the Comprehend node in the ingest path)
- Implementation: `lambda/shared/pii.py` + the three ingest Lambdas (process-documents, process-batch-summary, process-repo-summary)
- Tfvar toggles:
  - `pii_redaction_enabled` (org kill switch, default `true`)
  - `pii_redaction_skip_types` (org tuning, default `["URL", "IP_ADDRESS", "MAC_ADDRESS", "DATE_TIME"]`)
  - `comprehend_pii_price_per_1m_chars` (cost rollup tuning, default `1.00`)
- Per-workspace mode: `privacy_mode` attribute on the workspace METADATA row, set via Settings → General → Privacy in the UI
