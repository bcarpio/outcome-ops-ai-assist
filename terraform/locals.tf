locals {
  workspace_is_dev = terraform.workspace == "dev"
  workspace_is_prd = terraform.workspace == "prd"

  # FQDN for UI: outcomeops.domain.com (prd) or outcomeops-dev.domain.com (dev)
  fqdn = var.domain != "" ? (var.environment == "prd" ? "${var.ui_subdomain}.${var.domain}" : "${var.ui_subdomain}-${var.environment}.${var.domain}") : ""
}

# Validate that workspace matches environment to prevent deploying wrong config
check "workspace_matches_environment" {
  assert {
    condition = (
      (terraform.workspace == "dev" && var.environment == "dev") ||
      (terraform.workspace == "prd" && var.environment == "prd")
    )
    error_message = "Terraform workspace '${terraform.workspace}' does not match environment variable '${var.environment}'. Run 'terraform workspace select ${var.environment}' to fix."
  }
}
