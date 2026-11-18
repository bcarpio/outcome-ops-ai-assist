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
- **Identity-agnostic product.** OutcomeOps does not provision, name, or know how
  the customer runs CI. It exposes two off-by-default hooks and nothing more:
  - `ci_invoker_role_arns` -- a resource-policy allow-list on `analyze-pr` and
    `handle-command`. The customer names any CI principal ARN (however
    provisioned -- a central hub/spoke CI role, a single-account OIDC role, a
    Jenkins instance role, GitLab CI, ...) and the product grants it invoke on
    its own functions. The named role is never modified and carries no knowledge
    of OutcomeOps.
  - `ci_oidc_role_enabled` (+ `ci_oidc_github_subjects`, `ci_oidc_create_provider`
    / `ci_oidc_provider_arn`) -- for a customer with no CI role to point at, the
    product creates a GitHub-OIDC-assumable, invoke-only role in its own
    deployment account.
- **The grant lives on the product's side, never the customer's role.** Invoke is
  authorized by a resource policy on the OutcomeOps functions (or on the role the
  product itself creates). The product never writes OutcomeOps Lambda ARNs into a
  customer-managed role. How the customer provisions CI identities (AFT hub/spoke
  or otherwise) is their own infrastructure concern and stays out of this repo.

### Example Implementation

```hcl
# terraform.tfvars -- point OutcomeOps at an existing CI role (option 1):
ci_invoker_role_arns = ["arn:aws:iam::<ACCOUNT_ID>:role/<your-ci-role>"]

# OR have OutcomeOps create its own OIDC role (option 2):
ci_oidc_role_enabled    = true
ci_oidc_github_subjects = ["repo:<org>/<repo>:pull_request"]
```

## Consequences

### Positive

- No internet-facing inbound endpoint in the customer's account; nothing new to
  expose, and no webhook secret to store or rotate.
- Works with GitHub Enterprise Server and air-gapped-ish networks where a public
  webhook could never be delivered.
- Least privilege and auditable: the invoker is a named IAM role scoped to
  invoking exactly two functions; every trigger is an AWS API call in CloudTrail.
- Topology-agnostic: the same product supports a hub/spoke CI identity, a
  single-account OIDC role, or any other CI identity without code changes, and
  without the customer's CI design touching this repo.

### Tradeoffs

- Onboarding is per-repo (add the workflow + grant invoke), not a single
  org-wide webhook. A repo without the workflow simply isn't analyzed.
- The customer must stand up a CI->AWS identity (or enable the fallback role).
- Workspace scoping is resolved per repo from the workspaces table
  (`shared/pr_check_scope.py`). `analyze-pr`'s symbol-graph structural pass uses
  the same consumer scope as BREAKING_CHANGES (ADR-056 §5): the repo's own
  workspace(s), every workspace they are shared with, and the `is_global`
  workspaces (all workspaces when the repo's workspace is global). Each changed
  symbol is queried in every workspace in scope, because a consumer's edges live
  in the consumer's workspace. If any workspace in scope errors, that symbol
  renders as unverified rather than as a partial list. A repo that is not
  connected to any workspace gets no structural-impact comment.
  `PR_WORKSPACE_ID` remains only as an operator override.

## Implementation

### Starting today

1. Add `.github/workflows/pr-analysis.yml` to each repo to analyze; set its
   `AWS_OIDC_ROLE_ARN` secret.
2. Grant invoke by ONE of: `ci_invoker_role_arns` (name your existing CI role) or
   `ci_oidc_role_enabled` (let OutcomeOps create the role).
3. Full `terraform apply` (IAM-only).

### Next phases

1. Optional org-wide onboarding tooling (templated workflow distribution).

Done (v1.1): per-repo -> workspace resolution for the structural pass, replacing
the static `PR_WORKSPACE_ID` (which was never set, so the pass never ran).

## Related ADRs

- ADR-011: API Gateway Exposure and Git Provider Support (Island-VPC / ingress)
- ADR-043: analyze-pr / generate-code are primary-region-only (no HA secondary)
- ADR-056: Cross-repo / cross-workspace symbol resolution (consumer scope)

## References

- `.github/workflows/pr-analysis.yml`, `.github/workflows/pr-commands.yml`
- `terraform/ci_oidc_role.tf`, `terraform/variables.tf` (`ci_invoker_role_arns`,
  `ci_oidc_*`)
- `docs/getting-started.md` -- "Trigger PR analysis (CI, no public webhook)"

Version History:

- v1.0 (2026-10-06): Initial decision.
- v1.1 (2026-11-18): analyze-pr resolves the structural pass's workspace scope
  per repo (repo workspace + shared + global, ADR-056 §5) from the workspaces
  table; `PR_WORKSPACE_ID` is an override only.

<!-- Confluence sync -->
