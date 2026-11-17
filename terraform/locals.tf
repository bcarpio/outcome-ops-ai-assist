# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
