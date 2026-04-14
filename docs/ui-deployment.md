# Chat UI Deployment

The Chat UI is deployed as a Fargate Spot service behind an Application Load Balancer (ALB) that can be configured as either internal (VPC-only) or public (internet-facing with Azure AD OIDC authentication). Enterprise customers use internal ALBs accessed via Direct Connect, Transit Gateway, or VPN, while demo and dev environments use public ALBs with OIDC.

## Key Features

- Dual access models: internal ALB for enterprise VPC-only access, public ALB with OIDC for demos
- Fargate Spot deployment for ~70% cost savings over on-demand
- Azure AD OIDC authentication on the public ALB with automatic ACM certificate provisioning
- Single Terraform toggle (`deploy_ui = true`) to provision the full UI stack
- Configurable domain and subdomain with Route53 integration
- Container image built and pushed via `make build-ui-image`
- Support for enterprise network connectivity (Direct Connect, Transit Gateway, VPN, ZTNA)

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
