# ============================================================================
# Optional: GitHub Actions OIDC role for CI-triggered PR analysis
#
# Self-contained option for customers with no existing CI role to point
# OutcomeOps at (see docs/getting-started.md "Trigger PR analysis" and ADR-053).
# When enabled, creates an IAM role in THIS (the OutcomeOps deployment) account
# that GitHub Actions can assume directly via OIDC -- scoped to the customer's
# org/repo via the `sub` claim -- with permission to invoke ONLY the PR Lambdas
# (analyze-pr, handle-command). Put the role's ARN (output ci_oidc_role_arn) in
# each repo's AWS_OIDC_ROLE_ARN secret; see .github/workflows/pr-analysis.yml.
#
# Leave ci_oidc_role_enabled=false and use ci_invoker_role_arns instead if you
# already have a CI role to grant. OutcomeOps is identity-agnostic -- how you
# provision CI roles is out of scope here. PR analysis is triggered by CI
# invoking the Lambda directly (OIDC), never a public webhook; see ADR-053.
# ============================================================================

locals {
  ci_oidc_enabled = var.ci_oidc_role_enabled

  # Trust the provider we create, else the operator-supplied existing one.
  ci_oidc_provider_arn = (
    var.ci_oidc_create_provider
    ? try(aws_iam_openid_connect_provider.github[0].arn, null)
    : var.ci_oidc_provider_arn
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.ci_oidc_enabled && var.ci_oidc_create_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # aws provider 6.x validates GitHub's OIDC endpoint against its CA store, so
  # thumbprint_list is optional and deliberately omitted (no fragile pinned
  # fingerprint to rotate).

  tags = {
    Purpose = "ci-oidc-pr-analysis"
  }
}

data "aws_iam_policy_document" "ci_oidc_trust" {
  count = local.ci_oidc_enabled ? 1 : 0

  statement {
    sid     = "GitHubActionsOIDC"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.ci_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scope to the customer's org/repo(s); never leave this unconstrained.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.ci_oidc_github_subjects
    }
  }
}

resource "aws_iam_role" "ci_oidc" {
  count = local.ci_oidc_enabled ? 1 : 0

  name               = var.ci_oidc_role_name
  assume_role_policy = data.aws_iam_policy_document.ci_oidc_trust[0].json

  tags = {
    Purpose = "ci-oidc-pr-analysis"
  }

  lifecycle {
    precondition {
      condition     = length(var.ci_oidc_github_subjects) > 0
      error_message = "ci_oidc_github_subjects must list at least one GitHub OIDC subject (e.g. \"repo:org/repo:*\") when ci_oidc_role_enabled is true -- an unconstrained trust is refused."
    }
    precondition {
      condition     = var.ci_oidc_create_provider || var.ci_oidc_provider_arn != ""
      error_message = "When ci_oidc_role_enabled is true, set ci_oidc_create_provider=true or supply ci_oidc_provider_arn."
    }
  }
}

# Least privilege: invoke ONLY the two CI-triggered PR Lambdas, nothing else.
data "aws_iam_policy_document" "ci_oidc_invoke" {
  count = local.ci_oidc_enabled ? 1 : 0

  statement {
    sid     = "InvokePRLambdas"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      module.analyze_pr_lambda.lambda_function_arn,
      module.handle_command_lambda.lambda_function_arn,
    ]
  }
}

resource "aws_iam_role_policy" "ci_oidc_invoke" {
  count = local.ci_oidc_enabled ? 1 : 0

  name   = "invoke-pr-lambdas"
  role   = aws_iam_role.ci_oidc[0].id
  policy = data.aws_iam_policy_document.ci_oidc_invoke[0].json
}

output "ci_oidc_role_arn" {
  description = "ARN of the GitHub Actions OIDC CI role (when ci_oidc_role_enabled). Put this in each repo's AWS_OIDC_ROLE_ARN secret."
  value       = local.ci_oidc_enabled ? aws_iam_role.ci_oidc[0].arn : null
}
