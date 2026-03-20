locals {
  workspace_is_dev = terraform.workspace == "dev"
  workspace_is_prd = terraform.workspace == "prd"

  # FQDN for UI: outcomeops.domain.com (prd) or outcomeops-dev.domain.com (dev)
  fqdn = var.domain != "" ? (var.environment == "prd" ? "${var.ui_subdomain}.${var.domain}" : "${var.ui_subdomain}-${var.environment}.${var.domain}") : ""
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
