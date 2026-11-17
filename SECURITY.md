# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately. Do **not** open a public issue for a
suspected vulnerability.

- Preferred: use GitHub's **Private vulnerability reporting** ("Report a vulnerability"
  button on the Security tab of this repository).
- Or email **security@outcomeops.ai**.

Please include:
- a description of the issue and its impact,
- the affected component (Lambda function, Terraform module, UI, MCP server, etc.),
- steps to reproduce or a proof of concept,
- any relevant logs or configuration (with secrets redacted).

## Response Commitment

- We aim to acknowledge a report within **3 business days**.
- We will provide an assessment and a remediation plan within **10 business days** of
  acknowledgement, and keep you updated through resolution.
- We support coordinated disclosure: please give us a reasonable window to ship a fix before
  any public disclosure. We are happy to credit reporters who wish to be named.

## Scope

OutcomeOps AI Assist deploys **into the operator's own AWS account**; there is no
vendor-operated multi-tenant service holding customer data. Relevant reports include:

- vulnerabilities in the platform code (Lambda handlers, shared libraries, MCP server),
- Terraform that provisions insecure defaults,
- authentication/authorization flaws (Azure AD OIDC/OAuth, workspace scoping),
- handling of credentials and tokens (SSM, Secrets Manager, OAuth refresh tokens).

Because the platform runs in your account, you are responsible for the security of your own
deployment (IAM, network, key management). See
[docs/security-overview.md](docs/security-overview.md) for the security architecture and the
control boundaries.

## Supported Versions

Security fixes land on the default branch (`main`) and the latest release. Older revisions
are not separately patched; update to the latest `main` to pick up fixes.
