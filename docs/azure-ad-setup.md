# Azure AD OIDC Authentication Setup

This guide covers configuring Azure AD (Entra ID) as an OIDC identity provider for the OutcomeOps Chat UI. When enabled, the Application Load Balancer handles the entire OIDC authentication flow, requiring users to log in through Azure AD before accessing the UI and forwarding identity headers to the Fargate backend.

## Key Features

- ALB-managed OIDC authentication flow with Azure AD (Entra ID)
- Automatic ACM certificate provisioning with DNS validation via Route53
- Environment-aware FQDN derivation (e.g., `outcomeops.example.com` for prd, `outcomeops-dev.example.com` for dev)
- Client secret stored securely in SSM Parameter Store with KMS encryption
- Simple toggle to enable or disable authentication via `oidc_enabled` tfvar
- Support for credential rotation without downtime
- Standard OpenID Connect scopes (openid, email, profile)

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
