# Deployment Guide

This guide covers deployment, operations, and maintenance for OutcomeOps AI Assist. For initial setup steps, see [Getting Started](getting-started.md). For security details, see [Security Overview](SECURITY_OVERVIEW.md).

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Deployment Options](#deployment-options)
- [Supported Regions](#supported-regions)
- [Root User Warning](#root-user-warning)
- [Billable Services](#billable-services)
- [Cost Estimation](#cost-estimation)
- [License Management](#license-management)
- [Health Check and Monitoring](#health-check-and-monitoring)
- [Backup and Recovery](#backup-and-recovery)
- [Credential Rotation](#credential-rotation)
- [Patching and Upgrades](#patching-and-upgrades)
- [Service Limits](#service-limits)
- [Fault Handling](#fault-handling)
- [Recovery Procedures](#recovery-procedures)
- [Support](#support)

---

## Prerequisites

### AWS Knowledge Required

Deploying and operating OutcomeOps AI Assist requires familiarity with:

- **Terraform** - All infrastructure is managed as IaC
- **AWS IAM** - Role and policy management
- **AWS Lambda** - Serverless function configuration
- **Amazon Bedrock** - Model access and quotas
- **Amazon S3** - Bucket management and access policies
- **Amazon DynamoDB** - Table configuration and PITR
- **Amazon ECS / Fargate** - Container orchestration (if deploying Chat UI)
- **Application Load Balancer** - HTTPS termination and routing
- **VPC Networking** - Subnets, security groups (if deploying Chat UI)
- **DNS / Route 53** - Custom domain configuration

### Software Requirements

- Python 3.12+
- Terraform 1.5+
- AWS CLI v2
- GitHub PAT or GitHub App credentials

---

## Deployment Options

OutcomeOps AI Assist supports two deployment topologies:

### Single-Region Deployment (Standard)

All resources deploy to a single AWS region. This is the default and recommended configuration.

```hcl
aws_region = "us-west-2"
```

### ALB Access Modes

| Mode | Terraform Variable | Access | Authentication |
|------|-------------------|--------|----------------|
| **Internal ALB** (default) | `ui_alb_internal = true` | VPC-only via Direct Connect, Transit Gateway, VPN, or ZTNA | Network-level (corporate connectivity) |
| **Public ALB with OIDC** | `ui_alb_internal = false` | Public internet | Azure AD OIDC via ALB |

**Internal ALB** is recommended for enterprise deployments where users access the application through existing corporate network connectivity.

**Public ALB** requires Azure AD (Microsoft Entra ID) OIDC configuration. See [Azure AD Setup](azure-ad-setup.md).

### Network Topology

Fargate tasks need outbound internet access to reach Lambda Function URLs, GitHub API, and Bedrock endpoints. Two network models are supported:

| Model | Variables | Subnets | Outbound Path |
|-------|-----------|---------|---------------|
| **Public subnets** (default) | `ui_fargate_assign_public_ip = true` | Public subnets with IGW route | Fargate task gets public IP, outbound via Internet Gateway |
| **Private subnets** | `ui_fargate_assign_public_ip = false` | Private subnets | Outbound via NAT Gateway, Transit Gateway, or Direct Connect |

**Enterprise deployments** typically use private subnets with outbound routed through a Transit Gateway to a DMZ VPC, Direct Connect to corporate network, or a centralized NAT/proxy. In these environments:

```hcl
ui_alb_internal              = true
ui_fargate_assign_public_ip  = false
ui_private_subnet_ids        = ["subnet-private-a", "subnet-private-b"]
```

**Dev/demo deployments** can use public subnets with public IPs to avoid NAT Gateway costs:

```hcl
ui_alb_internal              = false
ui_fargate_assign_public_ip  = true
ui_private_subnet_ids        = ["subnet-public-a", "subnet-public-b"]
```

### Headless Mode

Set `deploy_ui = false` to skip the Chat UI entirely. The platform operates via CLI (`./scripts/outcome-ops-assist`) and GitHub integrations only.

---

## Supported Regions

OutcomeOps AI Assist requires the following AWS services to be available in the deployment region:

- Amazon Bedrock (with Claude and Titan model access)
- Amazon S3 Vectors
- AWS Lambda
- Amazon DynamoDB
- Amazon ECS Fargate (if deploying Chat UI)

### Verified Regions

| Region | Code | Bedrock Claude | S3 Vectors | Notes |
|--------|------|----------------|------------|-------|
| US West (Oregon) | `us-west-2` | Yes | Yes | **Default / Recommended** |
| US East (N. Virginia) | `us-east-1` | Yes | Yes | |
| US East (Ohio) | `us-east-2` | Yes | Yes | |
| Europe (Frankfurt) | `eu-central-1` | Yes | Yes | EU data residency |
| Europe (Ireland) | `eu-west-1` | Yes | Yes | EU data residency |
| Asia Pacific (Tokyo) | `ap-northeast-1` | Yes | Yes | |
| Asia Pacific (Sydney) | `ap-southeast-2` | Yes | Yes | |

**Note:** Bedrock model availability varies by region. Verify that the specific model IDs configured in `bedrock_advanced_model_id`, `bedrock_basic_model_id`, and `bedrock_embedding_model_id` are available in your chosen region. Cross-region inference profiles (e.g., `us.anthropic.claude-*`) route automatically.

---

## Root User Warning

**Do NOT use the AWS root user to deploy or operate OutcomeOps AI Assist.**

- Use an IAM user or IAM Identity Center (SSO) role with appropriate permissions
- The root user should only be used for account-level tasks (billing, support plan changes)
- Enable MFA on the root user and store credentials securely
- Follow the [AWS Root User Best Practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_root-user.html)

The Terraform deployment requires permissions to create Lambda functions, DynamoDB tables, S3 buckets, SQS queues, IAM roles/policies, KMS keys, and optionally ECS/ALB/VPC resources.

---

## Billable Services

All infrastructure runs in the customer's AWS account. Costs are usage-based with no upfront commitment for AWS services.

### Mandatory Services

| Service | Usage | Pricing Model |
|---------|-------|---------------|
| **AWS Lambda** | 36 functions, various memory configs | Per-invocation + duration |
| **Amazon Bedrock** | Claude Sonnet/Haiku + Titan Embeddings | Per-token (input/output) |
| **Amazon S3** | Knowledge base document storage | Per-GB stored + requests |
| **Amazon S3 Vectors** | Vector embeddings (1024-dim) | Per-GB stored + queries |
| **Amazon DynamoDB** | 4 tables, on-demand billing | Per-read/write unit |
| **AWS KMS** | CloudWatch Logs encryption | Per-key + API calls |
| **Amazon SQS** | 4 FIFO queues + DLQs | Per-request |
| **AWS SSM Parameter Store** | Configuration and secrets | Free (standard) / per-advanced |
| **Amazon CloudWatch** | Logs (7-day retention) | Per-GB ingested |
| **Amazon EventBridge** | Custom event bus | Per-event |
| **Amazon SNS** | Audit alert notifications | Per-message |
| **Amazon ECR** | 2 container repositories | Per-GB stored |

### Chat UI Deployment Services

| Service | Usage | Pricing Model |
|---------|-------|---------------|
| **Amazon ECS Fargate** | 1 task (0.25 vCPU, 512MB) | Per-vCPU-hour + per-GB-hour |
| **Fargate Spot** | Default capacity provider | Up to 70% discount |
| **Application Load Balancer** | 1 ALB + target group | Per-hour + per-LCU |
| **Amazon ACM** | TLS certificate | Free |
| **Amazon Route 53** | DNS hosted zone + alias record | Per-hosted zone + queries |

### MCP Server Catalog Services

| Service | Usage | Pricing Model |
|---------|-------|---------------|
| **Amazon ECS Fargate Spot** | 1 task per MCP server (0.5 vCPU, 1GB) | Per-vCPU-hour + per-GB-hour (up to 70% discount) |
| **AWS Cloud Map** | Service discovery for internal DNS | Per-namespace + per-instance |
| **CloudWatch Logs** | MCP server container logs | Per-GB ingested |

See [SonarQube MCP Server Setup](sonarqube-mcp.md) for configuration details.

### Optional Services (Integrations)

| Service | Trigger | Pricing Model |
|---------|---------|---------------|
| **API Gateway v2** | GitHub webhooks | Per-request |
| Additional Lambda functions | Confluence, Jira, Teams, SharePoint, Outlook | Per-invocation + duration |

---

## Cost Estimation

Estimated monthly costs for typical deployments. Actual costs depend on usage patterns.

### Small Deployment (1-5 repos, 10 users, light chat usage)

| Service | Estimated Monthly Cost |
|---------|----------------------|
| Lambda (compute) | $5 - $15 |
| Bedrock (tokens) | $100 - $500 |
| DynamoDB (on-demand) | $1 - $5 |
| S3 + S3 Vectors | $1 - $5 |
| SQS + EventBridge | < $1 |
| CloudWatch Logs | $1 - $5 |
| KMS + SSM | $1 - $3 |
| **Subtotal (headless)** | **$110 - $535** |
| ECS Fargate Spot | $3 - $8 |
| ALB | $16 - $25 |
| Route 53 + ACM | $1 |
| **Subtotal (with Chat UI)** | **$130 - $570** |

### Medium Deployment (20-50 repos, 100 users, active chat usage)

| Service | Estimated Monthly Cost |
|---------|----------------------|
| Lambda (compute) | $20 - $60 |
| Bedrock (tokens) | $500 - $3,000 |
| DynamoDB (on-demand) | $5 - $20 |
| S3 + S3 Vectors | $5 - $15 |
| SQS + EventBridge | $1 - $5 |
| CloudWatch Logs | $5 - $15 |
| KMS + SSM | $1 - $3 |
| **Subtotal (headless)** | **$540 - $3,120** |
| ECS Fargate Spot | $3 - $8 |
| ALB | $16 - $25 |
| Route 53 + ACM | $1 |
| **Subtotal (with Chat UI)** | **$560 - $3,155** |

### Large Deployment (50+ repos, 500 users, heavy daily usage)

| Service | Estimated Monthly Cost |
|---------|----------------------|
| Lambda (compute) | $60 - $200 |
| Bedrock (tokens) | $3,000 - $10,000 |
| DynamoDB (on-demand) | $20 - $100 |
| S3 + S3 Vectors | $15 - $50 |
| SQS + EventBridge | $5 - $15 |
| CloudWatch Logs | $15 - $50 |
| KMS + SSM | $1 - $3 |
| **Subtotal (headless)** | **$3,120 - $10,420** |
| ECS Fargate Spot | $3 - $8 |
| ALB | $16 - $25 |
| Route 53 + ACM | $1 |
| **Subtotal (with Chat UI)** | **$3,140 - $10,455** |

**Note:** Bedrock is by far the largest cost driver. Token usage scales with chat volume, code generation frequency, document ingestion volume, and PR analysis activity. With 500 daily active users, Bedrock costs can reach $10,000+/month. Consider Bedrock Provisioned Throughput for predictable high-volume workloads.

### Licensing

OutcomeOps AI Assist requires a license. See [License Management](#license-management) below.

---

## License Management

OutcomeOps AI Assist uses JWT-based licensing with tiered plans (Pilot, Team, Division, Enterprise). Licenses are issued by OutcomeOps and enforced via a Lambda layer deployed to the customer's AWS account.

### How It Works

1. **OutcomeOps issues a JWT license key** signed with RS256 for your organization
2. **You store the key** in SSM Parameter Store in your AWS account
3. **OutcomeOps publishes a Lambda layer** to your account with the license validator module
4. **Your Lambda functions** include the layer, which validates the license locally (JWT signature) and phones home to `license.example.com` for limit enforcement and usage tracking

### License Tiers

| Tier | Repos | PRs/Month | Duration | Enforcement |
|------|-------|-----------|----------|-------------|
| **Dev** | 2 | 10 | 30 days | Hard |
| **PoC** | 20 | Unlimited | 14 days | Hard |
| **Pilot** | 10 | 100 | 90 days | Hard |
| **Team** | 50 | 300 | 1 year | Soft |
| **Division** | 150 | 1,000 | 1 year | Soft |
| **Enterprise** | Unlimited | Unlimited | 1 year | Soft |

**Hard enforcement:** Operations are blocked when limits are reached. Fails closed on network errors. **Soft enforcement:** Operations are allowed but alerts are sent at 80% and 100% thresholds. Fails open on network errors.

**Enterprise air-gap option:** Enterprise customers with contractual agreements can run without phone-home connectivity for deployments in isolated VPCs with no outbound internet access. OutcomeOps configures the air-gap option during onboarding. License validation still works offline via the embedded RSA public key in the Lambda layer (JWT signature and expiration verified locally).

### Customer Setup

The license key and Lambda layer ARN are provided by OutcomeOps during onboarding:

```bash
# Store the JWT license key in SSM
aws ssm put-parameter \
  --name "/${ENV}/outcomeops/license/key" \
  --value "${JWT_LICENSE_KEY}" \
  --type SecureString \
  --overwrite
```

The layer ARN is configured in Terraform via the `outcomeops_license_layer_arn` variable:

```hcl
outcomeops_license_layer_arn  = "arn:aws:lambda:us-west-2:123456789012:layer:prd-outcomeops-license-validator-v1-5-0:1"
outcomeops_license_ssm_param  = "/prd/outcomeops/license/key"
outcomeops_license_server_url = "https://license.example.com"
```

### License Renewal

License renewals are handled by OutcomeOps. When a new license is issued:

1. OutcomeOps provides a new JWT license key
2. Update the SSM parameter:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/outcomeops/license/key" \
     --value "${NEW_JWT_LICENSE_KEY}" \
     --type SecureString \
     --overwrite
   ```
3. If a new layer version is required, update the `outcomeops_license_layer_arn` in your tfvars and redeploy

License validation results are cached for 1 hour in Lambda memory, so the new key takes effect within an hour or on the next cold start.

### Expiration Alerts

OutcomeOps monitors license expiration and sends alerts at 30, 14, 7, and 1 day before expiration. Contact your OutcomeOps account representative to renew before expiration.

---

## Health Check and Monitoring

### Application Health Check

The Chat UI Fargate service exposes a health endpoint:

- **Endpoint:** `GET /health`
- **Interval:** Every 30 seconds
- **Healthy threshold:** 2 consecutive successes
- **Unhealthy threshold:** 3 consecutive failures
- **ALB target group** automatically deregisters unhealthy tasks

### CloudWatch Monitoring

All Lambda functions log to CloudWatch Logs with:

- **Retention:** 7 days (configurable)
- **Encryption:** KMS-encrypted
- **Log groups:** `/aws/lambda/{env}-{app_name}-{function}`

### Key Metrics to Monitor

| Metric | Source | Alert Threshold |
|--------|--------|-----------------|
| Lambda errors | CloudWatch Metrics | > 5 errors/min |
| Lambda duration | CloudWatch Metrics | > 80% of timeout |
| DLQ message count | SQS Metrics | > 0 messages |
| Bedrock throttling | CloudWatch Metrics | ThrottlingException count |
| ALB 5xx errors | ALB Metrics | > 1% of requests |
| Fargate task count | ECS Metrics | < 1 running task |

### Audit Alerting

The `audit-alert` Lambda monitors the DynamoDB `audit-logs` table via Streams. It sends SNS notifications when Bedrock refusals are detected, alerting the configured `audit_alert_email`.

---

## Backup and Recovery

### Automated Backups

| Resource | Backup Method | RPO | RTO |
|----------|--------------|-----|-----|
| **DynamoDB tables** | Point-in-Time Recovery (PITR) | Seconds | Minutes |
| **S3 Knowledge Base** | Versioning enabled | Zero (all versions retained) | Minutes |
| **S3 Vectors** | Rebuilt from S3 Knowledge Base | N/A (derived data) | 30-60 min (re-ingest) |
| **Terraform state** | S3 backend with versioning (see [Getting Started](getting-started.md#step-3-create-terraform-state-bucket)) | Zero | Minutes |
| **SSM Parameters** | Manual backup recommended | Manual | Manual |
| **ECR Images** | Immutable tags (run-tests) | Zero | Minutes |

### What Is NOT Backed Up

- **CloudWatch Logs** - Retained for 7 days then expired. Export to S3 if long-term retention is needed.
- **SQS Messages** - Transient; DLQ captures failed messages for inspection.

### RPO/RTO Summary

- **DynamoDB PITR:** RPO of seconds (continuous backup), RTO of minutes (restore to new table)
- **S3 Versioning:** RPO of zero (every version stored), RTO of minutes (restore specific version)
- **Terraform State:** RPO of zero (S3 versioned), RTO of minutes (revert state file version)
- **S3 Vectors:** Can be fully rebuilt by re-running ingestion pipeline from S3 Knowledge Base

---

## Credential Rotation

### GitHub Personal Access Token (PAT)

1. Generate a new PAT in GitHub (Settings > Developer Settings > Personal Access Tokens)
2. Update the SSM parameter:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github/token" \
     --value "ghp_newTokenValue" \
     --type SecureString \
     --overwrite
   ```
3. No Lambda restart required - tokens are read from SSM at invocation time

### GitHub App Private Key

1. Generate a new private key in GitHub App settings
2. Update the SSM parameter:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github-app/private-key" \
     --value "$(cat new-private-key.pem)" \
     --type SecureString \
     --overwrite
   ```

### GitHub Webhook Secret

1. Generate a new secret: `openssl rand -hex 32`
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github/webhook-secret" \
     --value "new-secret-hex" \
     --type SecureString \
     --overwrite
   ```
3. Update the webhook secret in GitHub App settings to match

### OIDC Client Secret (Azure AD - ALB Authentication)

This is the Azure AD app registration used by the ALB for user authentication (OIDC flow).

1. Generate a new client secret in Azure AD > App Registrations > Certificates & Secrets
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/oidc/client-secret" \
     --value "new-client-secret" \
     --type SecureString \
     --overwrite
   ```
3. Force new ALB OIDC session by clearing browser cookies or restarting the ALB listener

### Microsoft OAuth Client Secret (Outlook, Teams, SharePoint)

This is a separate Azure AD app registration used for data sync integrations (Outlook, Teams, SharePoint). It is distinct from the OIDC secret above.

1. Generate a new client secret in Azure AD > App Registrations > Certificates & Secrets (for the integration app, not the OIDC app)
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/oauth/microsoft/client-secret" \
     --value "new-client-secret" \
     --type SecureString \
     --overwrite
   ```
3. No Lambda restart required - secrets are read from SSM at invocation time
4. Existing user OAuth tokens remain valid until they expire; only new token refreshes use the updated secret

### Atlassian OAuth Client Secret (Confluence, Jira)

1. Rotate the client secret in the [Atlassian Developer Console](https://developer.atlassian.com/console/myapps/)
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/oauth/atlassian/client-secret" \
     --value "new-client-secret" \
     --type SecureString \
     --overwrite
   ```
3. Existing user OAuth tokens remain valid until they expire

### GitHub App OAuth Client Secret

This is the OAuth client secret for the GitHub App (used for user-level OAuth flows in the workspace UI).

1. Rotate the client secret in GitHub > Settings > Developer Settings > GitHub Apps > your app
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github/client-secret" \
     --value "new-client-secret" \
     --type SecureString \
     --overwrite
   ```

### KMS Keys

- KMS keys used for CloudWatch Logs encryption have **automatic annual rotation** enabled by default
- No manual action required
- Rotation creates new key material; old material is retained for decryption

### Rotation Schedule Recommendations

| Credential | SSM Path | Recommended Rotation | Notes |
|-----------|----------|---------------------|-------|
| GitHub PAT | `/{env}/{app}/github/token` | Every 90 days | Set expiration at creation time |
| GitHub App Private Key | `/{env}/{app}/github-app/private-key` | Annually | Revoke old key after rotation |
| GitHub Webhook Secret | `/{env}/{app}/github/webhook-secret` | Annually or on compromise | Update both SSM and GitHub |
| GitHub App OAuth Secret | `/{env}/{app}/github/client-secret` | Annually | Coordinate with GitHub App settings |
| OIDC Client Secret | `/{env}/{app}/oidc/client-secret` | Per Azure AD policy (1-2 years) | ALB authentication - coordinate with identity team |
| Microsoft OAuth Secret | `/{env}/{app}/oauth/microsoft/client-secret` | Per Azure AD policy (1-2 years) | Outlook, Teams, SharePoint integrations |
| Atlassian OAuth Secret | `/{env}/{app}/oauth/atlassian/client-secret` | Annually | Confluence, Jira integrations |
| SonarQube MCP Token | `/{env}/{app}/mcp/sonarqube-token` | Per SonarQube policy | Force new ECS deployment after rotation |
| KMS Keys | N/A | Automatic (annual) | Managed by AWS |

---

## Patching and Upgrades

### Lambda Runtime Updates

Lambda functions use Python 3.12. When AWS releases new Python runtimes:

1. Update `runtime` in Terraform Lambda resources
2. Test locally: `cd lambda && make test-unit`
3. Deploy via CI/CD: `terraform plan && terraform apply`
4. AWS automatically patches the underlying Lambda execution environment

### Container Image Updates

The Chat UI runs on ECS Fargate with a custom container image:

1. Rebuild the image with updated dependencies:
   ```bash
   cd ui && docker build -t outcome-ops-ai-assist-chat-ui .
   ```
2. Push to ECR:
   ```bash
   docker tag outcome-ops-ai-assist-chat-ui:latest ${ECR_URI}:latest
   docker push ${ECR_URI}:latest
   ```
3. Force new deployment:
   ```bash
   aws ecs update-service --cluster ${CLUSTER} --service ${SERVICE} --force-new-deployment
   ```

### Terraform Module Updates

1. Review the OutcomeOps release notes for breaking changes
2. Update Terraform files with new module versions
3. Run `terraform plan` to review changes
4. Apply via CI/CD pipeline (never run `terraform apply` locally)

### Dependency Updates

- **Python Lambda dependencies:** Update `requirements.txt` in each Lambda directory, rebuild layers
- **Node.js UI dependencies:** Run `cd ui && npm update`, rebuild container image
- **Terraform providers:** Update version constraints in `terraform/main.tf`

---

## Service Limits

Key AWS service quotas that may need adjustment for large deployments:

| Service | Quota | Default | Recommendation |
|---------|-------|---------|---------------|
| **Bedrock** | Tokens per minute (Claude) | Varies by model/region | Request increase for heavy usage |
| **Bedrock** | Tokens per minute (Titan) | Varies by model/region | Request increase for bulk ingestion |
| **Lambda** | Concurrent executions | 1,000 per region | Sufficient for most deployments |
| **Lambda** | Function timeout | 900s (max) | chat-streaming and run-tests use max |
| **DynamoDB** | On-demand throughput | 40,000 RCU / 40,000 WCU | Auto-scales; monitor for throttling |
| **S3 Vectors** | Vector dimensions | 1024 (configured) | Fixed; matches Titan v2 output |
| **S3 Vectors** | Vectors per index | Service limit varies | Monitor index size |
| **SQS FIFO** | Messages per second | 300 msg/s (with batching: 3,000) | Sufficient for most workloads |
| **ECS Fargate** | Tasks per service | 10 (soft limit) | 1 task is typical |
| **API Gateway** | Requests per second | 10,000 | Sufficient for webhook volume |

### Requesting Quota Increases

Use the AWS Service Quotas console or CLI:
```bash
aws service-quotas request-service-quota-increase \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --desired-value 3000
```

---

## Fault Handling

### Dead Letter Queues (DLQ)

All SQS FIFO queues have associated DLQs. Messages that fail processing after the configured retry count are moved to the DLQ.

| Queue | Max Retries | DLQ |
|-------|-------------|-----|
| pr-checks-queue.fifo | 3 | pr-checks-dlq.fifo |
| code-generation-queue.fifo | 4 | code-generation-dlq.fifo |
| repo-summaries-queue.fifo | 3 | repo-summaries-dlq.fifo |
| code-maps-queue | 3 | code-maps-dlq |

**Monitoring DLQs:**
```bash
# Check for messages in DLQs
aws sqs get-queue-attributes \
  --queue-url https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/${ENV}-outcome-ops-ai-assist-pr-checks-dlq.fifo \
  --attribute-names ApproximateNumberOfMessages
```

**Reprocessing DLQ messages:**
```bash
# Start DLQ redrive (reprocess failed messages)
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:${REGION}:${ACCOUNT}:${ENV}-outcome-ops-ai-assist-pr-checks-dlq.fifo \
  --destination-arn arn:aws:sqs:${REGION}:${ACCOUNT}:${ENV}-outcome-ops-ai-assist-pr-checks-queue.fifo
```

### Lambda Error Handling

- All Lambda functions log errors to CloudWatch
- Unhandled exceptions return HTTP 500 with error details in the response body
- SQS-triggered Lambdas retry automatically (visibility timeout controls retry delay)
- `chat-streaming` Lambda includes Bedrock cooldown logic (`enable_bedrock_cooldown`) to handle throttling

### Bedrock Throttling

When Bedrock returns `ThrottlingException`:

1. The Lambda retries with exponential backoff (built into the Bedrock SDK)
2. If `enable_bedrock_cooldown = true`, the chat Lambda adds a configurable delay between requests
3. For sustained throttling, request a quota increase via AWS Support

### API Gateway Errors

- GitHub webhook validation failures (invalid HMAC signature) return 401
- Rate limiting returns 429 - GitHub retries automatically
- Internal errors return 500 and are logged to CloudWatch

---

## Recovery Procedures

### DynamoDB Point-in-Time Recovery

Restore a table to any point within the last 35 days:

```bash
# Restore to a specific timestamp
aws dynamodb restore-table-to-point-in-time \
  --source-table-name ${ENV}-outcome-ops-ai-assist-chat \
  --target-table-name ${ENV}-outcome-ops-ai-assist-chat-restored \
  --restore-date-time "2026-03-01T00:00:00Z"

# After verification, update Terraform to point to restored table
# or rename tables using DynamoDB import/export
```

### S3 Version Restore

Restore a specific object version:

```bash
# List versions of a file
aws s3api list-object-versions \
  --bucket ${ENV}-outcome-ops-ai-assist-kb-${ACCOUNT_ID} \
  --prefix "path/to/document.md"

# Restore by copying the desired version
aws s3api copy-object \
  --bucket ${ENV}-outcome-ops-ai-assist-kb-${ACCOUNT_ID} \
  --copy-source "${ENV}-outcome-ops-ai-assist-kb-${ACCOUNT_ID}/path/to/document.md?versionId=VERSION_ID" \
  --key "path/to/document.md"
```

### Terraform State Recovery

If Terraform state is corrupted:

```bash
# List state file versions
aws s3api list-object-versions \
  --bucket ${TERRAFORM_STATE_BUCKET} \
  --prefix "terraform.tfstate"

# Restore a previous version
aws s3api copy-object \
  --bucket ${TERRAFORM_STATE_BUCKET} \
  --copy-source "${TERRAFORM_STATE_BUCKET}/terraform.tfstate?versionId=GOOD_VERSION_ID" \
  --key "terraform.tfstate"
```

### S3 Vectors Rebuild

S3 Vectors (embeddings) are derived from the S3 Knowledge Base and can be fully rebuilt:

1. The knowledge base source documents remain in S3
2. Re-trigger the `process-documents` Lambda via S3 events or manual invocation
3. All embeddings are regenerated from source content
4. Estimated time: 30-60 minutes depending on KB size

### Fargate Task Recovery

If the Fargate task becomes unhealthy:

```bash
# Force new deployment (launches new task, drains old)
aws ecs update-service \
  --cluster ${ENV}-outcome-ops-ai-assist \
  --service ${ENV}-outcome-ops-ai-assist-chat-ui \
  --force-new-deployment
```

ECS automatically replaces failed tasks based on the desired count and health check configuration.

### Full Environment Recovery

To rebuild the entire environment from scratch:

1. Ensure Terraform state is intact (S3 versioned backend)
2. Ensure SSM parameters are populated (secrets)
3. Run `terraform apply` - all infrastructure will be recreated
4. Re-trigger knowledge base ingestion to rebuild S3 Vectors
5. Chat history (DynamoDB) restored via PITR if needed

---

## Support

### Support Channels

| Channel | Contact | Use For |
|---------|---------|---------|
| **Email** | support@outcomeops.ai | General inquiries, bug reports |
| **Support Portal** | [OutcomeOps Service Desk](https://outcomeops.atlassian.net/servicedesk/customer/portals) | Ticket management, status updates |
| **Emergency** | emergency@outcomeops.ai | Production outages (Premium tier) |

### Support Tiers

#### Standard Support

- **Included with:** All licenses
- **Hours:** Business hours (9 AM - 6 PM ET, Monday - Friday)
- **Response Times:**
  - Severity 1 (Production down): 4 business hours
  - Severity 2 (Production impaired): 8 business hours
  - Severity 3 (General issue): 2 business days
  - Severity 4 (Question/guidance): 5 business days

#### Premium Support

- **Included with:** Enterprise licenses
- **Hours:** 24/7 for Severity 1, business hours for others
- **Response Times:**
  - Severity 1 (Production down): 1 hour
  - Severity 2 (Production impaired): 4 hours
  - Severity 3 (General issue): 1 business day
  - Severity 4 (Question/guidance): 2 business days
- **Extras:** Dedicated support engineer, quarterly architecture review

### Severity Definitions

| Severity | Definition | Example |
|----------|-----------|---------|
| **1 - Critical** | Production system completely down, no workaround | Chat UI inaccessible, all Lambda functions failing |
| **2 - High** | Production system impaired, workaround available | PR analysis failing but chat works; degraded performance |
| **3 - Medium** | Non-production issue or minor production impact | Development environment issue, UI cosmetic bug |
| **4 - Low** | Question, feature request, or documentation | How-to questions, enhancement requests |

### Customer Responsibilities

- Maintain AWS account and infrastructure
- Apply Terraform updates and security patches
- Monitor CloudWatch metrics and DLQs
- Rotate credentials per the [Credential Rotation](#credential-rotation) schedule
- Report issues with relevant CloudWatch log excerpts

### OutcomeOps Responsibilities

- Provide Terraform module updates and patches
- Investigate and resolve product defects
- Publish release notes and upgrade guides
- Maintain license management portal
