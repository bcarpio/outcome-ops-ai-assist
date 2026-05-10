locals {
  workspace_is_dev = terraform.workspace == "dev"
  workspace_is_prd = terraform.workspace == "prd"

  # FQDN for UI: outcomeops.domain.com (prd) or outcomeops-dev.domain.com (dev)
  fqdn = var.domain != "" ? (var.environment == "prd" ? "${var.ui_subdomain}.${var.domain}" : "${var.ui_subdomain}-${var.environment}.${var.domain}") : ""

  # ADR-019: shared license layer attach values for every gated Lambda.
  outcomeops_license_env = {
    OUTCOMEOPS_LICENSE_PARAM      = var.outcomeops_license_ssm_param
    OUTCOMEOPS_LICENSE_SERVER_URL = var.outcomeops_license_server_url
    OUTCOMEOPS_USAGE_ENABLED      = "true"
  }

  outcomeops_license_iam = {
    outcomeops_license_ssm_read = {
      effect    = "Allow"
      actions   = ["ssm:GetParameter"]
      resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.outcomeops_license_ssm_param}"]
    }
    outcomeops_license_kms_decrypt = {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
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
