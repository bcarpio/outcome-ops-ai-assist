# ADR-045: Backend Lambda invocation stays on IAM-signed Function URLs

## Status: Accepted

## Context

The enterprise deployment profile (private VPC, Transit Gateway, a
centralized egress proxy such as Zscaler, and IP address management such
as Infoblox) requires the customer's network team to justify every
outbound DNS name that appears in the firewall allowlist. During
deployment reviews the recurring question is:

> "Why is `*.lambda-url.<region>.on.aws` in your egress allowlist?"

The architecture invokes backend Lambdas over **Lambda Function URLs**,
not API Gateway. Concretely, as of this ADR:

- **20** first-party `aws_lambda_function_url` resources exist across the
  request path -- chat, workspace-management, PR analysis (github), and
  every source integration (Azure DevOps, Azure DevOps Boards, Confluence,
  database, GitLab, Jira, OneDrive, OneNote, Outlook, Salesforce, ServiceNow,
  SharePoint, Teams), including their secondary-region counterparts. (GitLab
  and its secondary-region counterpart were added by ADR-050.)
- **Every one** is declared `authorization_type = "AWS_IAM"`. There are
  zero `NONE`/public Function URLs in first-party terraform.
- At least the chat path uses `invoke_mode = "RESPONSE_STREAM"` so the UI
  can stream NDJSON tokens to the browser.
- The request-path Lambdas run **outside any VPC** (no `vpc_config`); the
  callers are the UI-proxy and MCP-server Fargate tasks, which sign each
  request with their task-role credentials.

Function URL DNS is always `*.lambda-url.<region>.on.aws` -- a public DNS
name. There is **no interface VPC endpoint (PrivateLink) for Function
URLs**. The `com.amazonaws.<region>.lambda` VPC endpoint exists, but it
serves only the `lambda:InvokeFunction` control-plane API -- it does not
route Function URL HTTP requests, and it has no `RESPONSE_STREAM`
equivalent. So the public DNS name is unavoidable as long as we invoke
via Function URLs. IAM sigv4 protects *invocation*; it does not make the
*DNS answer* private.

This is a documentation gap, not a code gap. The trust model is sound;
what was missing was a written decision sales and network teams can point
to. Per ADR-007 (documentation-driven decisions), the fix is this ADR,
not a code change.

## Decision

Retain Function URLs with `authorization_type = "AWS_IAM"` for all
backend Lambda invocations. Do not migrate to VPC-endpoint invocation,
an internal ALB in front of every Lambda, or API Gateway, at this time.

**Trust model.** Every request to a Function URL is:

- Signed with the calling Fargate task role's credentials (SigV4) via
  botocore-signed HTTPS. The Fargate task lives inside the customer VPC.
- HTTPS-encrypted end to end; the payload is never in cleartext on the
  wire.
- Rejected at the Lambda side unless the signature is valid --
  `authorization_type = "AWS_IAM"` drops any unsigned or wrong-principal
  request before the handler runs.

AWS resolves `*.lambda-url.<region>.on.aws` on public DNS, but a public
DNS answer is not a public API: without a valid SigV4 signature from an
allowed principal, the endpoint returns 403. The outbound TLS is
TGW-routed through the customer's controlled egress path (NAT, Direct
Connect, or centralized proxy), so the customer retains full egress
visibility and control.

## What we are NOT doing, and why

- **`lambda:InvokeFunction` via the Lambda VPC endpoint** -- loses
  `RESPONSE_STREAM`, which is table stakes for the chat UX (NDJSON
  streaming). The VPC endpoint has no streaming-invoke equivalent.
- **An internal ALB (target-type `lambda`) in front of every Function
  URL** -- drops streaming, adds a hop, and duplicates the OIDC-terminated
  ingress ALB already in front of the UI proxy (ADR-042). Acceptable only
  for non-streaming surfaces, and only as a customer policy choice.
- **API Gateway (HTTP/WebSocket) + VPC link** -- a significant rewrite,
  higher per-request cost, and an added regional service dependency, for
  no change to the actual trust boundary.

## Enterprise network-team talking points

- Firewall rule: allow `*.lambda-url.<region>.on.aws` **outbound from the
  Fargate subnets only**; no inbound rule is needed.
- Every invocation is recorded in CloudTrail (data-plane events) and is
  attributable to the invoker's IAM ARN -- there are no anonymous invokes.
- The endpoints are not browser-reachable: only the Fargate task roles
  hold `lambda:InvokeFunctionUrl` on these functions.
- If policy requires it, the customer MAY front all Function URLs with an
  internal ALB as a deployment-time decision; document the trade-offs
  (loss of streaming on chat, extra hop) in the deployment guide.

## Migration criteria (revisit if any becomes true)

- AWS ships a PrivateLink / interface-endpoint path for Function URLs.
- Enterprise policy hard-blocks `*.lambda-url` egress at the TGW or proxy
  layer and no exception is grantable.
- The streaming transport changes (e.g. chat moves to WebSocket/SSE over
  the ingress ALB -> Fargate), removing the direct Function URL invoke that
  forces the streaming requirement.

## Consequences

- The deployment guide gains a "Network egress endpoints" section listing
  the `*.lambda-url.<region>.on.aws` pattern with this justification,
  alongside the private-ingress posture from ADR-042 and the
  enterprise-profile defaults.
- Sales and network teams have a concrete, written answer instead of
  "I don't know."
- No code change; the runtime trust boundary is unchanged.

## Related

- ADR-042 (UI ingress ALB in a private VPC) -- the inbound counterpart to
  this outbound posture.
- ADR-007 (documentation-driven decisions) -- why this is an ADR and not
  platform code.
