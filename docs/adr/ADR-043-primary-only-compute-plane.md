# ADR-043: Primary-Only Compute Plane (Codegen / PR / Query Family)

## Status: Accepted

## Context

ADR-022 established active/passive multi-region HA and named the
customer-facing read plane (chat, KB, MCP, integration ingest) as the
scope of what the platform mirrors into the secondary region. The
implicit corollary was that some primary-region compute is NOT mirrored
— but that boundary was never written down. The result was a repeatable
review-time contradiction: "why does chat have a secondary Lambda but
`generate-code` doesn't?" answered only by verbal folklore.

Worse, four SQS queues had been provisioned in the secondary region
without matching consumers:

- `code_generation_queue_secondary` + `code_generation_dlq_secondary`
- `pr_checks_queue_secondary` + `pr_checks_dlq_secondary`

Producers writing to those queues post-failover would succeed at the
API level; the queues would accept the message; no Lambda would ever
read it; the message would age out after SQS's default retention (4
days). Silent work loss — the worst failure mode a queue can have.

This ADR closes both gaps: it names the primary-only compute plane
explicitly and removes the orphan secondary queues so the topology
matches the intent.

## Decision

The following Lambdas + API surface remain **primary-only** and are NOT
mirrored to the secondary region:

- **Code generation family** (`lambda-code-generation.tf`):
  `generate_code_lambda`, `generate_code_dlq_lambda`, `run_tests_lambda`,
  `handle_command_lambda`.
- **PR analysis family** (`lambda-pr-analysis.tf`): `analyze_pr_lambda`,
  `process_pr_check_lambda`.
- **Direct query family** (`lambda-query.tf`): `ask_claude_lambda`,
  `query_kb_lambda`.
- **`outcome_ops_api` API Gateway v2** (`api_gateway.tf`) — the
  GitHub-webhook entry that fans out to the above.
- **`mcp_snyk` Fargate** (`mcp-snyk.tf`) — consistent with `mcp_snyk`
  not appearing in the secondary Fargate plane today.
- **`runtime_tools_layer` and `terraform_tools_layer`** (`lambda-layers.tf`)
  — consumed only by the codegen family; primary-only follows.

### Failover behavior for the excluded plane

- **GitHub webhooks** pointed at the primary API Gateway keep hitting
  the primary after failover. GitHub retries webhook deliveries up to
  3 times over ~30 minutes; sustained primary-region unavailability
  causes those deliveries to drop.
- **Code-gen via Jira Automation** (SSM Automation → `generate-code`)
  fails on the SSM invoke while the primary is down; Jira Automation's
  audit log shows the failure. Retriggering after failback works.
- **PR-check calls from customer CI** (`aws lambda invoke
  --function-name prd-outcome-ops-ai-assist-analyze-pr`) fail while the
  primary is down. Customer pipelines pause with the primary until
  failback.

**Acceptable degradation**: humans keep pushing code, reviewing PRs
directly on GitHub, and running tests. The AI-opens-a-PR loop pauses
during the outage window; on failback everything resumes.

### Queue-topology rule

**No secondary queue without a secondary consumer.** Any future addition
of a queue in the secondary region implies committing to a Lambda (or
Fargate service) that consumes from it. A queue-only mirror is worse
than not mirroring at all because it silently accepts work that will
never be processed.

### When to revisit

If a customer contracts an SLA on PR-automation availability during
regional outages, mirror the excluded plane:

- Add `generate_code_lambda_secondary`, `run_tests_lambda_secondary`,
  etc. (7-8 Lambda modules).
- Add `outcome_ops_api_secondary` API Gateway.
- Add `runtime_tools_layer_secondary` +
  `terraform_tools_layer_secondary`.
- Mirror `mcp_snyk` in `fargate-secondary.tf`.
- The customer's DNS failover playbook (already manual per `docs/HA.md`)
  covers the webhook URL flip: `webhooks.<customer>/github` points at
  the primary API Gateway; failing over that CNAME to the secondary
  resumes webhook delivery.

Order of magnitude for that expansion: ~8 Lambdas + one API Gateway v2
+ two duplicate Lambda layers + one Fargate service. No fundamentally
new complexity; it fits the existing HA parity pattern. Not on the
current roadmap.

## Consequences

### Positive

- Reviews querying the platform's HA topology now see ADR-022 (the
  scope) alongside ADR-043 (the excluded plane) and can reason about
  what fails how.
- The orphan-queue anti-pattern is closed. Producers in the excluded
  plane fail loudly (nowhere to write) instead of silently succeeding
  and losing the work.
- The queue-topology rule is a durable guardrail against re-introducing
  the same mistake.

### Tradeoffs

- Customers whose primary workflow is "AI opens PRs from Jira /
  GitHub Issues" get a degraded posture during regional outages. This
  is documented in `docs/administration/high-availability.md` so
  SecOps + engineering leaders can weigh the trade before signing.
- Failing back from a real outage requires manually re-triggering any
  code-gen requests that were queued at Jira / GitHub during the
  window. Not a lot of work, but worth telling operators to expect.

### What this ADR does NOT do

- Does not remove any primary-region resource. The excluded plane
  keeps running as before in the primary region.
- Does not change chat, KB, MCP, ingest, or any customer-facing read
  surface. Those stay HA'd per ADR-022.

## Implementation

Committed 2026-07-06 as part of the fable-code-review remediation plan
(chunk 11). Terraform state changes: removes four SQS module blocks
(`pr_checks_dlq_secondary`, `pr_checks_queue_secondary`,
`code_generation_dlq_secondary`, `code_generation_queue_secondary`) and
their associated SSM URL parameters + outputs from `terraform/sqs.tf`.
The plan checked that no Terraform module, Lambda handler, or script
references these queues outside `sqs.tf` itself.

`docs/HA.md` "What runs in the secondary region" updated to name the
excluded plane explicitly.

## Related ADRs

- ADR-022: Multi-region active/passive HA — the scope of what the
  platform mirrors.
- ADR-042: UI ingress via ALB in a private VPC — the ingress posture
  the HA topology inherits.

## References

- `terraform/sqs.tf` — the four orphan queues removed by this ADR.
- `terraform/lambda-code-generation.tf`,
  `terraform/lambda-pr-analysis.tf`, `terraform/lambda-query.tf` —
  the primary-only Lambda families.
- `terraform/api_gateway.tf` — the primary-only webhook entry.
- `docs/administration/high-availability.md` — customer-facing failover
  documentation, updated to name the excluded plane.

Version History:

- v1.0 (2026-07-06): Initial decision.

<!-- Confluence sync -->
