# ADR-053: CI-Triggered PR Analysis (OIDC Direct-Invoke, not a Public Webhook)

## Status: Accepted

## Context

PR analysis (`analyze-pr` orchestrator -> `process-pr-check`: the ADR / README /
test-coverage / breaking-change / duplication / license checks, posted as PR/MR
review comments including inline line-level comments) must run when a pull request
is opened or updated. The obvious trigger -- a public webhook endpoint the Git
provider POSTs to -- does not fit this product:

- **Deploy-in-customer-account model.** OutcomeOps runs in the *customer's* own AWS
  account. A public, internet-facing webhook endpoint there is inbound attack
  surface a regulated buyer will not accept, and it conflicts with the Island-VPC
  / no-public-ingress posture of ADR-011.
- **GitHub Enterprise Server reachability.** Self-hosted GHES (and its runners)
  frequently cannot reach a public AWS endpoint, so a webhook would not be
  delivered at all.
- **CI already holds an identity.** The customer's CI (GitHub Actions, etc.)
  already authenticates to AWS via OIDC with a scoped role. It can invoke the
  Lambda directly -- no new public route, no webhook secret to store or rotate.

## Decision

**PR analysis is triggered by the customer's CI invoking `analyze-pr` directly
over the AWS API (via OIDC), never by a public API Gateway webhook.** The same
applies to `handle-command` (PR slash-commands).

### Key Points

- **Direct invoke, not webhook.** A CI workflow on `pull_request`
  (`.github/workflows/pr-analysis.yml`) assumes an AWS role via OIDC and runs
  `aws lambda invoke` on `analyze-pr` with `{repository, pr_number, provider}`.
  `pr-commands.yml` does the same for `handle-command` on `issue_comment`. The
  `/webhooks/github` API Gateway route is unrelated -- it serves only the GitHub
  Issues -> codegen path and subscribes `issues`, not `pull_request`.
- **Recommended identity model: hub/spoke (AFT).** A GitHub Actions OIDC role in
  the customer's Shared Services account (account customization) is allowed to
  `sts:AssumeRole` on an account-agnostic `*:role/outcomeops-ci`; a per-account
  `outcomeops-ci` spoke role (global customization) trusts that hub role and
  carries `terraform plan` read plus `lambda:InvokeFunction` on the PR Lambdas.
  Because the spoke role lives in the same account as the Lambda, its own
  identity policy authorizes the invoke -- **the product stack needs to create
  nothing.** (Mirrors the existing monitoring/prowler hub-spoke customizations.)
- **Configurability for customers without that pattern.** The product stack
  offers two opt-in hooks, both off by default:
  - `ci_invoker_role_arns` -- a resource-policy allow-list on `analyze-pr` and
    `handle-command`. Accepts *any* principal ARN (a cross-account spoke role, a
    single-account OIDC role, a Jenkins instance role, GitLab CI, ...). The CI
    role's own identity policy stays untouched.
  - `ci_oidc_role_enabled` (+ `ci_oidc_github_subjects`, `ci_oidc_create_provider`
    / `ci_oidc_provider_arn`) -- creates a GitHub-OIDC-assumable, invoke-only role
    *in the deployment account* so a customer with no Shared Services / AFT can
    onboard without hand-rolling IAM.
- **The product never creates the CI identity for an AFT customer.** Those roles
  are customer-managed infrastructure; the stack only grants invoke (resource
  policy) or, when explicitly enabled, provisions the standalone fallback role.

### Example Implementation

```hcl
# Spoke role (customer's AFT global customization), same-account invoke:
resource "aws_iam_role_policy" "outcomeops_ci_invoke" {
  role = aws_iam_role.outcomeops_ci.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [
        "arn:aws:lambda:*:${local.account_id}:function:*-analyze-pr",
        "arn:aws:lambda:*:${local.account_id}:function:*-handle-command",
      ]
    }]
  })
}

# OR, product-side allow-list for a bring-your-own principal:
# terraform.tfvars
ci_invoker_role_arns = ["arn:aws:iam::<ACCOUNT_ID>:role/outcomeops-ci"]
```

## Consequences

### Positive

- No internet-facing inbound endpoint in the customer's account; nothing new to
  expose, and no webhook secret to store or rotate.
- Works with GitHub Enterprise Server and air-gapped-ish networks where a public
  webhook could never be delivered.
- Least privilege and auditable: the invoker is a named IAM role scoped to
  invoking exactly two functions; every trigger is an AWS API call in CloudTrail.
- Topology-agnostic: the same product supports AFT hub/spoke, single-account
  OIDC, or any other CI identity without code changes.

### Tradeoffs

- Onboarding is per-repo (add the workflow + grant invoke), not a single
  org-wide webhook. A repo without the workflow simply isn't analyzed.
- The customer must stand up a CI->AWS identity (or enable the fallback role).
- KB scoping: `analyze-pr` currently resolves the workspace from a
  `PR_WORKSPACE_ID` env var, not per-repo; a repo->workspace lookup is a later
  phase (see Implementation).

## Implementation

### Starting today

1. Add `.github/workflows/pr-analysis.yml` to each repo to analyze; set its
   `AWS_OIDC_ROLE_ARN` secret.
2. Grant invoke by ONE of: the spoke role's identity policy (AFT),
   `ci_invoker_role_arns` (allow-list), or `ci_oidc_role_enabled` (fallback role).
3. Full `terraform apply` (IAM-only when using the product-side hooks).

### Next phases

1. Per-repo -> workspace resolution so KB retrieval is scoped without a static
   `PR_WORKSPACE_ID`.
2. Optional org-wide onboarding tooling (templated workflow distribution).

## Related ADRs

- ADR-011: API Gateway Exposure and Git Provider Support (Island-VPC / ingress)
- ADR-043: analyze-pr / generate-code are primary-region-only (no HA secondary)

## References

- `.github/workflows/pr-analysis.yml`, `.github/workflows/pr-commands.yml`
- `terraform/ci_oidc_role.tf`, `terraform/variables.tf` (`ci_invoker_role_arns`,
  `ci_oidc_*`)
- `docs/getting-started.md` -- "Trigger PR analysis (CI, no public webhook)"

Version History:

- v1.0 (2026-10-06): Initial decision.

<!-- Confluence sync -->
