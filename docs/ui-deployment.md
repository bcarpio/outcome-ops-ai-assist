# Chat UI Deployment

The Chat UI is deployed as a Fargate Spot service behind an Application Load Balancer (ALB). The ALB can be configured as internal (VPC-only) or public (internet-facing with OIDC authentication).

## Architecture

```
Public ALB (ui_alb_internal = false)          Internal ALB (ui_alb_internal = true)
────────────────────────────────────          ─────────────────────────────────────

     Internet                                    Corporate Network
         │                                              │
         │ HTTPS (443)                                  │ Direct Connect / TGW / VPN
         v                                              v
   ┌───────────┐                                  ┌───────────┐
   │ Public    │                                  │ Internal  │
   │ ALB       │──> Azure AD OIDC                 │ ALB       │
   │           │    Authentication                │           │
   └─────┬─────┘                                  └─────┬─────┘
         │                                              │
         v                                              v
   ┌───────────┐                                  ┌───────────┐
   │ Fargate   │                                  │ Fargate   │
   │ Spot      │                                  │ Spot      │
   └───────────┘                                  └───────────┘
```

## Access Models

### Public ALB with OIDC (Recommended for demos/dev)

Internet-facing ALB secured by Azure AD authentication. Users must log in via Azure AD before accessing the UI.

```hcl
# terraform/prd.tfvars
deploy_ui       = true
ui_alb_internal = false  # Public ALB

# OIDC Configuration
domain         = "example.com"
oidc_enabled   = true
oidc_client_id = "your-azure-app-id"
oidc_tenant_id = "your-azure-tenant-id"
```

Access: `https://outcomeops.example.com` (or `https://outcomeops-dev.example.com` for dev)

See [Azure AD Setup](azure-ad-setup.md) for OIDC configuration.

### Internal ALB (Enterprise)

VPC-only access. Enterprise customers use their existing network connectivity:

- **Direct Connect** - Dedicated network connection to AWS
- **Transit Gateway** - Hub-and-spoke connectivity between VPCs and on-premises
- **Site-to-Site VPN** - Encrypted tunnel over public internet
- **Zscaler/ZTNA** - Zero-trust network access solutions

```hcl
# terraform/prd.tfvars
deploy_ui       = true
ui_alb_internal = true  # Internal ALB (default)
```

The customer is responsible for:
- VPC networking (NAT, VPC endpoints if needed)
- Network access to the ALB (VPN, DX, TGW)
- DNS resolution for the ALB

## Terraform Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `deploy_ui` | bool | false | Deploy Fargate + ALB infrastructure |
| `ui_vpc_id` | string | "" | VPC ID for UI deployment |
| `ui_private_subnet_ids` | list(string) | [] | Subnet IDs for Fargate and ALB |
| `ui_container_image` | string | "" | ECR image URI for the chat UI container |
| `ui_alb_internal` | bool | true | Internal (VPC-only) or public ALB |
| `domain` | string | "" | Domain for HTTPS/OIDC (requires Route53 hosted zone) |
| `ui_subdomain` | string | "outcomeops" | Subdomain prefix for the UI |
| `oidc_enabled` | bool | false | Enable Azure AD OIDC authentication |
| `oidc_client_id` | string | "" | Azure AD Application (client) ID |
| `oidc_tenant_id` | string | "" | Azure AD Directory (tenant) ID |

## Building the UI Container

```bash
# Build and push container image
ENVIRONMENT=prd make build-ui-image
```

This command:
1. Creates the ECR repository if it doesn't exist
2. Builds the React app
3. Creates a container with the built assets + streaming proxy
4. Pushes to ECR

## Infrastructure Components

When `deploy_ui = true`:
- ALB (internal or public based on `ui_alb_internal`)
- Fargate Spot cluster (single task, 0.25 vCPU, 512MB)
- Security groups for ALB and Fargate tasks
- ACM certificate + Route53 record (when OIDC enabled)

## Cost Considerations

- **Fargate Spot**: ~70% cheaper than on-demand, with interruption handling
- **ALB**: Charged per hour + LCU usage
- **ACM Certificate**: Free
- **Route53**: $0.50/hosted zone/month + query charges
