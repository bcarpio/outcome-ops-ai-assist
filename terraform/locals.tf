locals {
  workspace_is_dev = terraform.workspace == "dev"
  workspace_is_prd = terraform.workspace == "prd"

  # FQDN for UI: {ui_subdomain}.{domain} when append_environment_to_fqdn=false
  # (production-style: outcomeops.outcomeops.ai), otherwise
  # {ui_subdomain}-{environment}.{domain} (dev-style:
  # outcomeops-dev.outcomeops.ai). Default true so dev/test envs auto-
  # disambiguate; production tfvars opt out so they sit on first-class
  # subdomains.
  fqdn = var.domain != "" ? (
    var.append_environment_to_fqdn
    ? "${var.ui_subdomain}-${var.environment}.${var.domain}"
    : "${var.ui_subdomain}.${var.domain}"
  ) : ""

  # ADR-019: shared license layer attach values for every gated Lambda.
  outcomeops_license_env = {
    OUTCOMEOPS_LICENSE_PARAM      = var.outcomeops_license_ssm_param
    OUTCOMEOPS_LICENSE_SERVER_URL = var.outcomeops_license_server_url
    OUTCOMEOPS_USAGE_ENABLED      = "true"
  }

  # When HA is on, the license SSM param lives in BOTH regions so
  # secondary-region Lambdas can read it locally (cross-region SSM reads
  # would couple secondary availability to primary). Grant access to
  # both region ARNs in the same statement; without this, secondary
  # Lambdas attaching this policy get only the primary-region ARN and
  # fail license validation with AccessDeniedException.
  outcomeops_license_iam = {
    outcomeops_license_ssm_read = {
      effect  = "Allow"
      actions = ["ssm:GetParameter"]
      resources = compact([
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.outcomeops_license_ssm_param}",
        var.secondary_region != "" ? "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.outcomeops_license_ssm_param}" : "",
      ])
    }
    outcomeops_license_kms_decrypt = {
      effect  = "Allow"
      actions = ["kms:Decrypt"]
      resources = compact([
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm",
        var.secondary_region != "" ? "arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm" : "",
      ])
    }
  }
}

# Hard-fail if workspace does not match environment variable.
# check blocks only warn; a precondition on a resource causes a real error
# that blocks both plan and apply.
resource "terraform_data" "workspace_environment_guard" {
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "BLOCKED: Terraform workspace '${terraform.workspace}' does not match environment '${var.environment}'. Run 'terraform workspace select ${var.environment}' to fix."
    }
  }
}
