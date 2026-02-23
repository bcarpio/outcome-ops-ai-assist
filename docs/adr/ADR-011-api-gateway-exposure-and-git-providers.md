# ADR-011: API Gateway Exposure and Git Provider Support

## Status: Accepted

## Context

OutcomeOps AI Assist receives webhooks from Git providers to trigger code generation. Different customers have different Git provider deployments:

| Git Provider | Location | Webhook Source |
|--------------|----------|----------------|
| GitHub Cloud | SaaS | Public internet |
| GitHub Enterprise Server | Self-hosted | Internal network |
| GitLab Cloud | SaaS | Public internet |
| GitLab Self-Managed | Self-hosted | Internal network |

**Regulated Industry Requirements:**

Customers in regulated industries (healthcare, finance, etc.) often have strict network security requirements:
- TGW-connected VPCs have strict rules, no public endpoints allowed
- Security teams require network isolation from corporate crown jewels
- Public API endpoints trigger extensive security reviews

**The Island VPC Pattern:**

For regulated customers, OutcomeOps deploys in an "Island VPC" - a VPC with no Transit Gateway attachment:

```
Customer AWS Account
├── Island VPC (No TGW)
│   ├── Lambda functions
│   ├── DynamoDB tables
│   ├── API Gateway (public OR internal)
│   └── NAT Gateway / VPC Endpoints (outbound only)
│
├── No TGW attachment
├── No route to corporate network
├── Outbound to Bedrock only
└── Code stays in account
```

This pattern simplifies security review because:
- No network path to sensitive internal systems
- Code never leaves the customer's AWS account
- Only outbound traffic is to AWS Bedrock

## Decision

### Git Provider Support

Support multiple Git providers with provider-specific webhook handlers:

```hcl
variable "git_provider" {
  type        = string
  description = "Git provider type"
  validation {
    condition     = contains(["github_cloud", "github_enterprise", "gitlab_cloud", "gitlab_self_managed"], var.git_provider)
    error_message = "git_provider must be one of: github_cloud, github_enterprise, gitlab_cloud, gitlab_self_managed"
  }
}
```

### API Gateway Exposure Auto-Detection

API Gateway exposure is determined by Git provider location:

```hcl
variable "api_gateway_exposure" {
  type        = string
  default     = null
  description = "API Gateway exposure: public, internal, or null for auto-detect based on git_provider"
  validation {
    condition     = var.api_gateway_exposure == null || contains(["public", "internal"], var.api_gateway_exposure)
    error_message = "api_gateway_exposure must be public, internal, or null"
  }
}

locals {
  # Auto-detect based on git provider
  api_exposure = coalesce(
    var.api_gateway_exposure,
    contains(["github_cloud", "gitlab_cloud"], var.git_provider) ? "public" : "internal"
  )
}
```

**Auto-Detection Matrix:**

| Git Provider | Default Exposure | Override Allowed |
|--------------|------------------|------------------|
| github_cloud | public | Yes (to internal) |
| github_enterprise | internal | Yes (to public) |
| gitlab_cloud | public | Yes (to internal) |
| gitlab_self_managed | internal | Yes (to public) |

### Internal API Gateway Configuration

For internal exposure, use VPC Link with private ALB:

```hcl
resource "aws_apigatewayv2_vpc_link" "internal" {
  count = local.api_exposure == "internal" ? 1 : 0

  name               = "${var.environment}-${var.app_name}-vpc-link"
  security_group_ids = [aws_security_group.api_gateway.id]
  subnet_ids         = var.private_subnet_ids
}
```

### Dashboard Exposure

Dashboard follows the same exposure pattern as API Gateway:

| API Gateway | Dashboard |
|-------------|-----------|
| Public | Public (default) or Internal |
| Internal | Internal only |

## Consequences

### Positive

- Simplified security review for regulated customers (Island VPC pattern)
- Auto-detection reduces configuration errors
- Supports hybrid scenarios (cloud Git with internal API)
- Clear upgrade path from cloud to self-hosted Git

### Negative

- Additional Terraform complexity for internal API Gateway
- Customers with self-hosted Git on internal network need network connectivity to Island VPC
- Internal exposure requires VPC configuration (subnet IDs, security groups)

### Neutral

- License validation still works (outbound HTTPS to license server)
- Bedrock access via NAT Gateway or VPC endpoints (customer choice)

## Implementation

### Phase 1: GitHub Cloud (Current)

- Public API Gateway
- GitHub webhook signature validation
- No VPC configuration required

### Phase 2: GitHub Enterprise

- Internal API Gateway via VPC Link
- Same webhook signature validation
- Requires: VPC, subnets, security groups

### Phase 3: GitLab Support

- New webhook handler for GitLab events
- GitLab token validation
- Support both cloud and self-managed

## Related

- ADR-004: Terraform Workflow
- Roadmap: Jira Integration (completed)
- Roadmap: GitLab Support (planned)

<!-- Confluence sync -->
