# Audit Stream Export — SIEM Hook

For customers whose security team wants every audit-log event in their
own SIEM (Splunk, Datadog, Sumo Logic, AWS Security Lake, Elastic, a
Firehose to S3 — wherever). This page describes the architecture, how
to enable it, the OCSF wire format, and the FilterCriteria patterns
their consumer should use.

For *what* the platform records, see
[`audit-coverage.md`](audit-coverage.md). For *how to use* the
operator dashboard, see [`audit-dashboard.md`](audit-dashboard.md).

---

## Architecture

```
+----------------+     DDB Streams     +-----------------------+
|  audit-logs    |  ---- INSERT ---->  |  audit-stream-        |
|  DynamoDB      |                     |  publisher Lambda     |
+----------------+                     +-----------+-----------+
                                                   |
                                       OCSF v1.3.0 envelope
                                                   |
                                                   v
                                       +-----------+-----------+
                                       |  Kinesis Data Stream  |
                                       |  ${env}-${app}-       |
                                       |  audit-export         |
                                       |  (on-demand, 24h)     |
                                       +-----------+-----------+
                                                   |
                                                   v
                                       +-----------------------+
                                       |  Customer's consumer  |
                                       |  - Splunk add-on      |
                                       |  - Firehose -> S3     |
                                       |  - Lambda -> Sumo /   |
                                       |    Datadog / Elastic  |
                                       |  - Security Lake      |
                                       +-----------------------+
```

**The publisher Lambda is the only thing in the platform that writes
to the export stream.** Customers build their own consumer; we don't
prescribe one. Every event that lands in the audit table flows
through, in OCSF format, with the full raw OutcomeOps payload preserved
under `unmapped`.

**Why Kinesis Data Streams (not Firehose):** customers pick their own
consumer pattern. Firehose is "fire and forget to S3 / Splunk"; KDS
lets them attach a Splunk add-on, a Datadog forwarder, a Lambda that
fans out to multiple SIEMs, or a Firehose if they actually want one.
KDS keeps the platform agnostic.

---

## Enabling

One tfvar:

```hcl
# terraform/prd.tfvars
audit_stream_export_enabled = true
```

That's it. CMK encryption flows automatically from the existing
`enable_cmk_encryption` toggle:

| `enable_cmk_encryption` | Stream encryption                        |
|-------------------------|------------------------------------------|
| `false` (default)       | AWS-managed `alias/aws/kinesis`          |
| `true`                  | Platform CMK (same key as DDB / S3 / SQS) |

After `terraform apply`, the stream ARN is published as both a
Terraform output and an SSM parameter so customer infrastructure can
discover it without parsing state:

```bash
aws ssm get-parameter \
  --name "/${ENV}/${APP_NAME}/audit-export/stream-arn" \
  --query 'Parameter.Value' --output text
```

Stream is on-demand (no shard math, scales automatically), 24h
retention. Customers who want longer in-stream retention can spin up
their own Firehose consumer and land events in S3 with their own
lifecycle policy.

---

## OCSF wire format

Every record on the stream is a JSON object conforming to OCSF
v1.3.0. The envelope structure:

```json
{
  "metadata": {
    "version": "1.3.0",
    "product": {
      "name": "OutcomeOps AI Assist",
      "vendor_name": "OutcomeOps",
      "feature": { "name": "<lambda_name>" }
    },
    "uid": "<request_id>",
    "log_name": "<bedrock|admin|auth>"
  },
  "time": 1715179800000,
  "category_uid": 6,
  "category_name": "Application Activity",
  "class_uid": 6005,
  "class_name": "Application Activity",
  "activity_id": 5,
  "activity_name": "WORKSPACE_CREATED",
  "type_uid": 600505,
  "severity_id": 1,
  "severity": "Informational",
  "status_id": 1,
  "actor": {
    "user": {
      "email_addr": "alice@example.com",
      "name": "alice@example.com"
    }
  },
  "src_endpoint": {
    "ip": "10.0.0.42",
    "user_agent": "Mozilla/5.0 ..."
  },
  "resources": [
    { "type": "workspace", "uid": "ws_abc123" }
  ],
  "unmapped": {
    "category": "admin",
    "action": "WORKSPACE_CREATED",
    "request_id": "...",
    "workspace_id": "ws_abc123",
    "user_email": "alice@example.com",
    "timestamp": "2026-05-08T14:30:00Z",
    "details": { "...full audit row..." }
  }
}
```

### Class mapping

| OutcomeOps category / action group | OCSF class                  | UID  |
|------------------------------------|-----------------------------|------|
| `bedrock` (every Bedrock call)     | API Activity                | 6003 |
| `auth` (LOGIN_SUCCEEDED, API_KEY_USED) | Authentication          | 3002 |
| Workspace lifecycle (`WORKSPACE_*`) | Account Change             | 3001 |
| Membership (`*_MEMBER_*`)          | Account Change              | 3001 |
| Code-gen (`CODE_GENERATED`, `PR_ANALYZED`, etc.) | Application Activity | 6005 |
| OAuth (`OAUTH_CONNECTION_ESTABLISHED`) | Authentication          | 3002 |
| Budget (`BUDGET_THRESHOLD_CROSSED`) | Application Activity (Medium) | 6005 |
| Everything else (default)          | Application Activity        | 6005 |

The default is intentional: rather than enumerate all 42 audit actions
into bespoke OCSF classes, v1 maps the major categories explicitly and
leaves the long tail under 6005 with the action name in
`activity_name` and `unmapped.action`. Specific class promotions can
land later without breaking consumers — they just see a more specific
`class_uid` for events that previously fell through.

### Severity mapping

| OCSF severity_id | Used for                                        |
|------------------|-------------------------------------------------|
| 1 Informational  | Default for successful operations                |
| 2 Low            | `*_SHARING_UPDATED`, org-level membership       |
| 3 Medium         | Bedrock refusals, `WORKSPACE_DELETED`, `BUDGET_THRESHOLD_CROSSED`, `CODE_GEN_DLQ_SALVAGE_PR_CREATED` |
| 4 High           | (Reserved — not currently emitted)              |

### Status mapping

| OCSF status_id | When                                              |
|----------------|---------------------------------------------------|
| 1 Success      | Default                                            |
| 2 Failure      | Bedrock refusals (with `status_detail`=flagged_reason), Bedrock errors |

### `unmapped`

The entire DynamoDB row is preserved, fully unwrapped from the
DynamoDB Streams shape (so `{"S": "foo"}` becomes `"foo"`). Nothing
is lost — drill-down logic that needs `cost_usd`, `input_tokens`,
`redaction_summary`, `flagged_reason`, etc., works against `unmapped.*`.

---

## Customer-side consumer patterns

The customer chooses their consumer; we publish everything. Below are
the FilterCriteria patterns that come up most often.

### Pattern 1: drop high-volume infrastructure events

For a generic SIEM ingest where you don't need every per-document /
per-sync event:

```json
{
  "filters": [
    {
      "pattern": "{ \"data\": { \"unmapped\": { \"action\": [{ \"anything-but\": [\"DOCUMENT_INGESTED\", \"REPO_SYNCED\", \"CONFLUENCE_SYNCED\", \"JIRA_SYNCED\", \"OUTLOOK_SYNCED\", \"TEAMS_SYNCED\", \"SHAREPOINT_SYNCED\", \"API_KEY_USED\", \"MCP_TOOL_INVOKED\", \"LOGIN_SUCCEEDED\"] }] } } }"
    }
  ]
}
```

Apply this on the consumer Lambda's event source mapping (Splunk
forwarder, Firehose-with-transformation, etc.). Matches the platform's
own `HIDDEN_DASHBOARD_ACTIONS` policy — same noise, different filter.

### Pattern 2: refusals only (dedicated SOC alert pipeline)

```json
{
  "filters": [
    { "pattern": "{ \"data\": { \"status_id\": [2], \"class_uid\": [6003] } }" }
  ]
}
```

`status_id=2` (Failure) on `class_uid=6003` (API Activity) =
Bedrock refusals or errors. Pair with a dedicated consumer that
fans out to PagerDuty / Slack / OpsGenie.

### Pattern 3: identity events only (Account Change + Auth)

```json
{
  "filters": [
    { "pattern": "{ \"data\": { \"category_uid\": [3] } }" }
  ]
}
```

Everything in OCSF category 3 (IAM): logins, API key usage, every
workspace / member / org membership change. Useful for an identity-
focused SIEM index.

### Pattern 4: strip prompt and response (compliance-minimal)

If your SIEM ingest is governed by a "log metadata, not content"
policy (some HIPAA / GDPR posture), put a transformer Lambda in front
of your forwarder that drops `unmapped.input_text` and
`unmapped.output_text`:

```python
import base64
import json

def lambda_handler(event, _ctx):
    out = []
    for record in event["records"]:
        payload = json.loads(base64.b64decode(record["data"]))
        unmapped = payload.get("unmapped", {})
        unmapped.pop("input_text", None)
        unmapped.pop("output_text", None)
        record_out = {
            "recordId": record["recordId"],
            "result": "Ok",
            "data": base64.b64encode(
                json.dumps(payload).encode()
            ).decode(),
        }
        out.append(record_out)
    return {"records": out}
```

That's the entirety of a Firehose data-transformation Lambda for the
"strip content, keep envelope" policy.

---

## Consumer IAM (in customer account, same account as platform)

The customer's consumer needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "kinesis:DescribeStream",
        "kinesis:DescribeStreamSummary",
        "kinesis:GetRecords",
        "kinesis:GetShardIterator",
        "kinesis:ListShards",
        "kinesis:SubscribeToShard"
      ],
      "Resource": "<stream-arn-from-ssm>"
    }
  ]
}
```

If `enable_cmk_encryption=true`, also add `kms:Decrypt` on the
platform CMK (look up the key ARN from
`/${env}/${app_name}/data-cmk/key-arn` — or from the Terraform
output).

---

## Operational notes

- **At-least-once delivery.** DDB Streams + Kinesis are both at-least-
  once. Idempotent consumption: dedupe on `metadata.uid` (=
  `request_id`). The same `uid` arriving twice is a redelivery.
- **Ordering.** The Kinesis partition key is the `workspace_id` (with
  fallback to `request_id` for SYSTEM rows). Events for the same
  workspace stay in-order on a single shard. Cross-workspace ordering
  is not preserved — generally fine for SIEM use.
- **Backpressure.** On-demand Kinesis auto-scales. The publisher
  Lambda's event source mapping uses `ReportBatchItemFailures` so a
  single bad record doesn't block the whole batch.
- **Catastrophic Kinesis outage.** If Kinesis is unavailable for >1h
  the DDB stream age cap (`maximum_record_age_in_seconds = 3600`)
  drops the events. Audit rows themselves remain in DynamoDB — the
  customer can backfill from there if they need a perfect tape.
- **Cost.** On-demand Kinesis pricing is per-MB and per-GB-month
  retention. A typical workload (4,000 chat queries + 130 admin
  actions per day, ~1 KB OCSF envelope each) lands at single-digit
  USD/month. Customer's consumer cost is on them.

---

## Disabling

```hcl
audit_stream_export_enabled = false
```

`terraform apply` removes the publisher Lambda, the Kinesis stream,
and the SSM parameter. The DynamoDB Streams + analytics aggregator
keep working — only the SIEM hook goes away.

---

## See also

- [`audit-coverage.md`](audit-coverage.md) — every event the platform
  records, organized by category.
- [`audit-dashboard.md`](audit-dashboard.md) — operator's guide to
  the in-platform dashboard.
- [ADR-018](adr/ADR-018-audit-logging-design.md) — schema design
  and writer split.
- `lambda/shared/ocsf_mapper.py` — the mapping logic.
- `lambda/audit-stream-publisher/handler.py` — the publisher itself.
- `terraform/audit-stream-export.tf` — the infra.
