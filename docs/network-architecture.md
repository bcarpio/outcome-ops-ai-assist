# Network Architecture

This document explains the OutcomeOps AI Assist network topology and how the platform fits into customer environments that have **no NAT Gateway and no Internet Gateway** in the OutcomeOps AWS account — only Transit Gateway (TGW) and Direct Connect (DX) for internal users.

It is written for the customer's network / cloud-platform team. No changes to platform code are required to support this topology — it's the default deployment shape.

## TL;DR

- The OutcomeOps account needs **zero internet egress of its own**.
- All AWS service traffic uses VPC endpoints (PrivateLink + gateway endpoints).
- All user traffic comes in through DX → TGW → an internal ALB.
- The only internet-bound traffic is the ALB's OIDC handshake with Azure AD; that exits via TGW → the customer's existing centralized egress (corporate forward proxy / NAT in a shared services account).
- Lambda Function URLs are reachable privately via the Lambda interface endpoint — chat streaming works inside the VPC without ever touching the public internet.

## What the UI is

The OutcomeOps UI is a single-page React application served from an ECS Fargate task running an Express server. The Express server is **not** an application server — it is a thin SigV4-signing reverse proxy in front of two Lambda Function URLs:

```
[User browser]
     │
     ▼
[ALB (internal, VPC-only)]   ← OIDC handshake with Azure AD happens HERE
     │  injects x-amzn-oidc-data JWT (signed by AWS, not Microsoft)
     ▼
[ECS Fargate task: Express + React]
     │
     ├─► Static React SPA: served from /dist
     │
     └─► /api/* requests:
         • SigV4-sign (AWS_IAM auth on Lambda Function URLs)
         • Forward to one of:
             - workspace-management Lambda (CRUD, audit, MCP, billing)
             - chat-streaming Lambda (RAG + Bedrock streaming)
```

State lives in DynamoDB. Vectors live in S3 Vectors. Files live in S3. Generated code, audit rows, and analytics rollups all stay inside the customer's AWS account; the platform never phones home.

### Why Express in front of Lambda?

Two reasons:

1. **AWS_IAM auth on Lambda Function URLs.** Both `aws_lambda_function_url` resources are configured with `authorization_type = AWS_IAM`, so even if someone discovers the public URL they cannot invoke without SigV4-signed credentials. The Express server signs each request with the Fargate task role's credentials — the same task role that has the IAM permission to invoke the Lambdas. The browser never sees an AWS credential.

2. **Streaming.** The chat Lambda emits SSE (server-sent events) from Bedrock token streams. Express proxies the stream byte-for-byte to the browser without buffering, so users see tokens as they're generated.

### OIDC happens at the ALB, not in our code

This is a critical detail for the no-egress story. The ALB does the OAuth handshake with Azure AD (Entra ID) and injects an **AWS-signed** JWT into the `x-amzn-oidc-data` header. The Express server just decodes that header (`ui/server/index.js:113-123`).

The Fargate task therefore makes **zero callouts to Microsoft**. The only component that needs to talk to Azure AD is the ALB itself, during the per-login token exchange and during JWKS refresh.

## Network topology in a no-NAT / no-IGW environment

```
             ┌─────────────── Corporate Network ────────────────┐
             │                                                  │
  [User] ──► DX ──► TGW ──┐         ┌── Centralized Egress ──┐ │
                          │         │  (NAT / forward proxy) │ │
  ┌── OutcomeOps VPC ─────┼─────────┼────────────────────────┘ │
  │                       │         │                          │
  │  internal ALB ◄───────┘         │                          │
  │     │                           │                          │
  │     ▼                           │                          │
  │  Fargate (UI proxy) ─────► Lambda Func URLs (via VPC EP)   │
  │     │                                                      │
  │     ├──► DDB (gateway endpoint, free)                      │
  │     ├──► S3 (gateway endpoint, free)                       │
  │     ├──► Bedrock / Comprehend / SSM / KMS (interface EPs)  │
  │     │                                                      │
  │     └──► ALB → 0.0.0.0/0 → TGW → corporate egress → Azure AD
  │                                                            │
  └────────────────────────────────────────────────────────────┘
```

There are three categories of traffic, each with a different network path.

### Category 1: AWS-internal traffic → VPC endpoints

Every AWS service that the platform uses can be reached over PrivateLink without leaving the VPC. None of these require an IGW or NAT.

| Service | Used by | Endpoint type |
|---|---|---|
| **DynamoDB** | workspaces, audit logs, analytics rollups, conversations, MCP configs | Gateway (free) |
| **S3** | knowledge base bucket (uploaded docs), vector index storage | Gateway (free) |
| **Lambda** | Express → workspace-mgmt + chat Function URLs | Interface |
| **Bedrock Runtime** | chat streaming, RAG embeddings, Cohere rerank | Interface |
| **Comprehend** | PII redaction at ingest time (when `pii_redaction_enabled = true`) | Interface |
| **SSM Parameter Store** | configuration values, encrypted OAuth tokens | Interface |
| **KMS** | workspace-OAuth-token CMK; optional data-CMK if `enable_cmk_encryption = true` | Interface |
| **ECR API + ECR DKR** | Fargate task pulling the UI container image | Interface (both) |
| **CloudWatch Logs** | Lambda + Fargate log aggregation | Interface |
| **SQS** | file-ingestion FIFO queue, code-map summarization queue | Interface |
| **STS** | Fargate task role assumption + SigV4 caller identity | Interface |
| **S3 Vectors** | RAG vector storage (newer service; verify regional availability) | Interface |

**Note on Lambda Function URLs.** AWS added PrivateLink support for Lambda Function URLs in late 2023. The `lambda` interface endpoint resolves the public Function URL hostname (`xxxx.lambda-url.region.on.aws`) to a private IP within the VPC. SSE streaming from Bedrock through the chat Lambda works correctly over this path. This is what makes the "Express proxy → Lambda" architecture viable inside a fully private VPC.

**Endpoint policies.** Each interface endpoint can carry a resource policy that restricts callers to only the OutcomeOps task / Lambda execution roles. The platform doesn't ship endpoint policies (they're customer-network-team territory) but the IAM model is compatible: every caller already has a tight, role-based identity.

### Category 2: User-bound traffic → DX → TGW → ALB

```
Corporate user → DX → TGW → internal ALB (in OutcomeOps VPC) → Fargate task
```

The ALB is created with `internal = true` (`terraform/ui-fargate.tf:169`, gated on `var.ui_alb_internal` which defaults to `true`). It receives only RFC1918 private IP addresses. There is no public DNS name and no possibility of public-internet access.

For users to reach it:

1. The customer's network team advertises the OutcomeOps VPC's CIDR through Direct Connect.
2. The TGW route table sends corporate user subnets toward the OutcomeOps VPC.
3. The ALB security group allows inbound HTTPS (port 443) from the corporate user CIDRs (configurable via `var.ui_alb_internal` — when true, the SG is scoped to the VPC CIDR; the customer can extend it to corporate subnets via TGW).
4. Customers usually add a private Route53 zone (or their own DNS) that resolves a friendly name (e.g. `outcomeops.corp.example.com`) to the ALB's private IPs.

### Category 3: Internet-bound traffic → TGW → corporate egress

Two things actually need to reach the public internet, and both fit cleanly into the customer's existing centralized-egress pattern.

#### a) ALB → Azure AD (server-to-server)

During every user login, the ALB performs an OAuth 2.0 authorization-code exchange with `login.microsoftonline.com/{tenant}/oauth2/v2.0/token` and periodically refreshes the JWKS at `login.microsoftonline.com/{tenant}/discovery/v2.0/keys` to validate ID tokens.

This traffic exits the OutcomeOps VPC by following the default route in the VPC route table:

```
0.0.0.0/0 → TGW → customer's centralized egress VPC → corporate forward proxy / NAT → public internet
```

If the customer wants to lock this down further, they can replace the `0.0.0.0/0` route with specific Microsoft IP-prefix routes (Microsoft publishes the relevant ranges as the `MicrosoftEntra` and `MicrosoftOnline` service tags). The ALB only needs to reach `*.microsoftonline.com` and `login.windows.net`.

#### b) User browser → Azure AD (browser-to-server)

The OAuth flow includes a browser-side redirect to Microsoft's login page. The user's browser performs this directly — it doesn't go through the OutcomeOps VPC. Since the user is already on the corporate network and presumably authenticates to Office, Teams, etc. via Azure AD every day, this path is already provisioned by the corporate network team. No new firewall rule.

## Summary: what each side provides

### Customer network team

1. **Provision VPC endpoints** in the OutcomeOps VPC (the table in Category 1 above). The two gateway endpoints (S3, DynamoDB) are free; the interface endpoints incur per-hour and per-GB charges.
2. **Wire the OutcomeOps VPC into the TGW**, advertise its CIDR through Direct Connect, and add return routes from corporate user subnets.
3. **Provide a default route through TGW to centralized egress.** Strictly speaking, only Microsoft IP prefixes need to actually exit; everything else is AWS-internal and goes via PrivateLink.
4. **Allow corporate user CIDRs into the internal ALB's security group.** The platform's default SG only allows the VPC CIDR; customers extend it to their corporate ranges.
5. **(Optional) Private Route53 zone** mapping a friendly name to the ALB's private IPs.

### OutcomeOps platform (no-op, already configured)

- `ui_alb_internal = true` in tfvars (default `true` for the no-egress scenario).
- Both Lambda Function URLs use `authorization_type = AWS_IAM` — no public-IAM-bypass surface.
- Fargate task role has only the IAM it needs (Lambda invoke + SSM read + KMS decrypt + CloudWatch Logs).
- Lambdas are scoped to specific service actions and resource ARNs (DynamoDB tables, S3 bucket, KMS keys, etc.). When `pii_redaction_enabled = true`, the ingest Lambdas additionally have `comprehend:DetectPiiEntities` (resource `*` because AWS doesn't support resource-level scoping for that action).
- All audit rows, analytics rollups, generated code, and customer documents stay inside the customer's AWS account. The platform has no external "phone home" path.

## Optional: even more lockdown

For customers who want maximum isolation:

- **Replace `0.0.0.0/0` with Microsoft prefix-list routes.** Azure AD is the only outbound destination the platform requires. Routing only Microsoft prefixes through TGW removes any possibility of an unrelated SDK or dependency reaching arbitrary internet.
- **VPC endpoint policies.** Restrict each interface endpoint to only the platform's task/execution roles, so a compromised workload elsewhere in the account can't piggyback on the endpoints.
- **Customer-managed KMS for everything.** Set `enable_cmk_encryption = true` to use a CMK for DynamoDB, S3, SQS, SNS, ECR. The platform already supports this.
- **Disable Cohere Rerank.** If the customer doesn't want the AWS Marketplace billing relationship, set `bedrock_rerank_enabled = false`. The chat path falls back to raw vector-similarity ordering.
- **Disable PII redaction at the platform level if the customer handles it out-of-band.** Set `pii_redaction_enabled = false`. The Comprehend interface endpoint becomes optional.

## What's not in scope here

- **Cross-region failover.** This document describes single-region deployment. Multi-region is supported by deploying the stack twice and putting an active-passive Route53 record in front of the two ALBs; out-of-scope here.
- **Bring-your-own-VPC vs. platform-creates-VPC.** The platform expects the customer to provide the VPC + subnets via `var.ui_vpc_id` and `var.ui_private_subnet_ids`. The customer's network team owns subnet sizing, AZ distribution, and route tables.
- **Azure AD app registration steps.** See `docs/azure-ad-setup.md` for the OIDC app registration walk-through.

## Reference: relevant Terraform and code

| Concern | Location |
|---|---|
| Internal ALB SG + listener | `terraform/ui-fargate.tf` |
| Fargate service + task role | `terraform/ui-fargate.tf` |
| Lambda Function URLs (IAM-auth) | `terraform/lambda-workspace-mgmt.tf`, `terraform/lambda-chat.tf` |
| OIDC header-decode in Express | `ui/server/index.js:113-145` |
| SigV4 signing for Lambda calls | `ui/server/index.js:255-260` (chat), `ui/server/index.js:598` (workspace-mgmt) |
| Comprehend region precondition | `terraform/comprehend.tf` |
| Tfvars toggles (`ui_alb_internal`, `pii_redaction_enabled`, `bedrock_rerank_enabled`, `enable_cmk_encryption`) | `terraform/variables.tf`, `terraform/{dev,prd}.tfvars.example` |
