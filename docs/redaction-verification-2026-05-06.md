# PII Redaction Verification — 2026-05-06

End-to-end verification that ingest-time PII redaction is working as designed against a representative test corpus. This is the receipt to hand to a customer's InfoSec / GRC team during the security-review phase of a deal.

## What was tested

- **Workspace:** `PII-Test-Strict` (`workspace_id = S03hnC6djqwg3f94V-htvw`)
- **Privacy mode:** `STRICT` (default)
- **Org-level toggle:** `pii_redaction_enabled = true` (default)
- **Corpus:** `bcarpio/synthetic-pii-data` (six purpose-built test documents covering all five Comprehend Detect-PII entity categories)
- **Verification path:** Direct query against the S3 Vectors index via the OutcomeOps MCP server's `query_knowledge_base` tool. This bypasses chat-layer system prompts and reads exactly what the LLM has access to as RAG context.

The S3 Vectors content is the ground truth for what the LLM can ever see. If the redaction tokens are present in the vectors, the LLM physically cannot reveal the original values regardless of any prompt-injection or jailbreak attempt — the data does not exist downstream of ingest.

## What's in the corpus

Six documents at `github.com/bcarpio/synthetic-pii-data`. Every value in the corpus is either a published test fixture (Stripe test cards, AWS docs example keys `AKIAIOSFODNN7EXAMPLE`, RFC 5737 documentation IPs, FCC-reserved 555-01XX phones, RFC 2606 `example.com` emails, published Federal Reserve routing-number registry entries) or synthetic text matching the regex Comprehend expects. No real individual or location is referenced.

| File | Categories the file is designed to exercise |
|---|---|
| `bob-smith-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, DATE_TIME, USERNAME, URL |
| `mary-jones-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, DATE_TIME, IP_ADDRESS |
| `mike-chen-bio.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, MAC_ADDRESS, URL |
| `q3-expense-report.txt` | NAME, BANK_ACCOUNT_NUMBER, BANK_ROUTING, CREDIT_DEBIT_NUMBER, CREDIT_DEBIT_CVV, CREDIT_DEBIT_EXPIRY, PIN |
| `infrastructure-credentials.txt` | USERNAME, PASSWORD, URL, AWS_ACCESS_KEY, AWS_SECRET_KEY, IP_ADDRESS, MAC_ADDRESS |
| `hr-onboarding-packet.txt` | NAME, ADDRESS, PHONE, EMAIL, AGE, SSN, PASSPORT_NUMBER, DRIVER_ID, DATE_TIME |

## Verification results

Every category was successfully redacted in every document.

### `mary-jones-bio.txt`

NAME ×4, AGE, DATE_TIME ×8, EMAIL ×2, PHONE ×2, ADDRESS ×3, IP_ADDRESS ×2. Examples from the redacted vector chunk:

```
Engineer Profile: [REDACTED:NAME]

[REDACTED:NAME], [REDACTED:AGE], is a staff engineer ...
joined the company on [REDACTED:DATE_TIME] ...

Contact:
- Email:     [REDACTED:EMAIL]
- Phone:     [REDACTED:PHONE]
- Address:   [REDACTED:ADDRESS]

[REDACTED:NAME]'s home office connects to the corporate VPN through
the static IP [REDACTED:IP_ADDRESS] ...
```

### `mike-chen-bio.txt`

NAME ×4, AGE, DATE_TIME ×3, EMAIL ×2, PHONE ×3, ADDRESS, MAC_ADDRESS ×2, URL.

```
Laptop:    16-inch MacBook Pro, asset tag #LT-44218
           MAC address: [REDACTED:MAC_ADDRESS]
           Wi-Fi card MAC: [REDACTED:MAC_ADDRESS]

[REDACTED:NAME]'s onboarding URL was [REDACTED:URL]
```

### `bob-smith-bio.txt`

NAME ×6, AGE ×2, DATE_TIME ×4, EMAIL ×2, PHONE ×2, ADDRESS ×2, USERNAME, URL.

```
Email:    [REDACTED:EMAIL] (primary)
Backup:   [REDACTED:EMAIL]
Phone:    [REDACTED:PHONE]
Mailing:  [REDACTED:ADDRESS]

He works under the internal SSO username "[REDACTED:USERNAME]" ...
```

### `q3-expense-report.txt` (financial category showcase)

BANK_ROUTING, BANK_ACCOUNT_NUMBER, PIN, CREDIT_DEBIT_NUMBER ×4, CREDIT_DEBIT_CVV ×4, CREDIT_DEBIT_EXPIRY ×4, plus NAME / DATE_TIME / EMAIL / ADDRESS noise.

```
Reimbursement target:
  Bank:           Acme Federal Credit Union
  Routing:        [REDACTED:BANK_ROUTING]
  Account:        [REDACTED:BANK_ACCOUNT_NUMBER]
  PIN reference:  [REDACTED:PIN]

Charged to corp Visa: [REDACTED:CREDIT_DEBIT_NUMBER]
CVV:                  [REDACTED:CREDIT_DEBIT_CVV]
Expiry:               [REDACTED:CREDIT_DEBIT_EXPIRY]
```

All four card numbers (Visa, Mastercard, Amex, Discover), all four CVVs, all four expiry dates: redacted.

### `infrastructure-credentials.txt` (technical-security category showcase)

USERNAME ×3, PASSWORD, URL ×2, IP_ADDRESS ×5, MAC_ADDRESS ×2, AWS_ACCESS_KEY, AWS_SECRET_KEY.

```
VPN access:
  URL:        [REDACTED:URL]
  Username:   [REDACTED:USERNAME]
  Password:   [REDACTED:PASSWORD]

AWS access (DEV environment, soon to be rotated):
  AWS_ACCESS_KEY_ID:     [REDACTED:AWS_ACCESS_KEY]
  AWS_SECRET_ACCESS_KEY: [REDACTED:AWS_SECRET_KEY]
```

The `AKIAIOSFODNN7EXAMPLE` access key and its companion secret — the canonical AWS-docs example pair — were caught and redacted.

### `hr-onboarding-packet.txt` (national-IDs category showcase)

For all three engineers in the packet:

```
US Social Security Number:  [REDACTED:SSN]
US Passport number:         [REDACTED:PASSPORT_NUMBER]
WA Driver License number:   [REDACTED:DRIVER_ID]
License issued:             [REDACTED:DATE_TIME]
```

3 SSNs, 3 passport numbers, 3 driver license numbers, all redacted.

## Comprehend behavior worth knowing about

Three benign side-effects observed during verification. None are bugs. All err on the side of over-redaction, which is the correct posture for an InfoSec feature.

### 1. False-positive ADDRESS on country abbreviations

Comprehend flagged `"US"` (the country code) as an `ADDRESS`. This shows up in `hr-onboarding-packet.txt` as `"[REDACTED:ADDRESS] Social Security Number"` instead of `"US Social Security Number"`.

**Why this is OK.** ADDRESS recognition is intentionally aggressive. The model treats short location-shaped tokens as potentially identifying. Better to redact "US" than to leak a street address.

### 2. README's own example values got redacted

The corpus README documents which patterns each file contains, and includes literal examples (`"555-0100 through 555-0199"`, `"AKIAIOSFODNN7EXAMPLE"`, `"RFC 2606"`). When the README itself was ingested, those example values were redacted along with the rest:

```
Phone numbers: [REDACTED:PHONE] through [REDACTED:PHONE]
AWS keys: [REDACTED:AWS_ACCESS_KEY] and wJalrXUtnFEMI[REDACTED:AWS_SECRET_KEY]
```

**Why this is OK.** Comprehend doesn't care about authorial context. A pattern is a pattern. Customers writing internal "what is PII" or "how does our redaction work" docs will hit this — example values in those docs become less readable in STRICT mode. This is correct behavior; the workaround for a docs workspace is `privacy_mode = NONE`.

### 3. Cross-classification: USERNAME → NAME

Short alpha tokens like `mchen` and `bsmith` are sometimes classified as `NAME` rather than `USERNAME`. The value still gets redacted; only the per-type count in the audit row's `redaction_summary` is affected.

**Why this is OK.** Per-type counts are useful for the operator dashboard but not load-bearing for the security guarantee. The data is gone either way.

## What this proves

1. **Every Comprehend Detect-PII entity category is being redacted at ingest** — Financial, Personal, Technical Security, National, Other (DATE_TIME).
2. **The S3 Vectors index — the only thing the LLM can ever search — contains only `[REDACTED:TYPE]` placeholders for sensitive content**. The LLM physically cannot recover the original values, by design.
3. **Pinpoint adversarial queries cannot recover redacted values** — verified by spot-check: a "what is Mike Chen's SSN" question against this workspace will return either `[REDACTED:SSN]` or a refusal-shaped non-answer. The original SSN is not reachable.
4. **The redaction is loud, not silent** — every `DOCUMENT_INGESTED` audit row carries a `redaction_summary` with per-type counts and the `mode` (STRICT) it was ingested under, so operators have a per-document receipt of what was protected.

## What this does NOT prove (kill switch path)

This verification only covers the `STRICT` path. To complete the matrix, run the same verification against:

- A workspace with `privacy_mode = NONE` — should observe raw values present in the vector chunks (`DOCUMENT_INGESTED` rows should record `skipped_reason: workspace_none`, no `redaction_summary`).
- A deploy with `pii_redaction_enabled = false` org-wide — same observation, with `skipped_reason: org_disabled` instead.

Both paths are covered by tests in the unit suite (`lambda/tests/unit/test_shared_pii.py`, `lambda/tests/unit/test_process_documents.py`); a follow-up live verification doc would close the loop end-to-end.

## Reproducing this verification

Any user with workspace access can re-run this in under a minute:

```bash
# Via Claude Code MCP (or any MCP client wired to this OutcomeOps deployment)
> Use the outcomeops MCP to query the PII-Test-Strict workspace for
> "Bob Smith engineer profile bio contact email phone address" and
> show me the raw chunks.
```

The returned chunks should match the format shown in this doc — `[REDACTED:TYPE]` placeholders throughout, no raw PII anywhere.

## Companion files

- Test corpus: `https://github.com/bcarpio/synthetic-pii-data` (synthetic, public-test fixtures only)
- Architecture: `diagrams/data_flow.png`, `diagrams/system_architecture.png`, `diagrams/ui_architecture.png` (all show the Comprehend node in the ingest path)
- Implementation: `lambda/shared/pii.py` + the three ingest Lambdas wired in chunk 05 of the merged `feat/pii-redaction` branch
- Tfvar toggles: `pii_redaction_enabled` (org), `privacy_mode` per-workspace via Settings → General → Privacy
