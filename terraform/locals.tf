locals {
  workspace_is_dev = terraform.workspace == "dev"
  workspace_is_prd = terraform.workspace == "prd"
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
