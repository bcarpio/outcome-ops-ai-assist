# ============================================================================
# Lambda Layers
# - runtime-tools: Git, Make, and build tools for generate-code Lambda
# - terraform-tools: Terraform CLI for formatting and validating .tf files
#
# NOTE: The run-tests Lambda uses a container image (see ecr.tf) which includes
# all runtime tools (git, Python, Java, Node.js, Terraform). The runtime-tools
# layer is still used by generate-code Lambda for git operations.
# ============================================================================

# Build the runtime layer before packaging
resource "null_resource" "build_runtime_layer" {
  triggers = {
    # Rebuild when build script changes
    build_script = filemd5("${path.module}/../scripts/build-runtime-layer.sh")
    # Rebuild when layer binaries don't exist
    layer_exists = fileexists("${path.module}/../lambda/runtime-layer/bin/git") ? "exists" : "missing-${timestamp()}"
    # Force rebuild by changing this timestamp if needed
    force_rebuild = "2025-12-05T12:00:00Z"
  }

  provisioner "local-exec" {
    command     = "./scripts/build-runtime-layer.sh"
    working_dir = "${path.module}/.."
  }
}

module "runtime_tools_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  create_layer = true

  layer_name          = "${var.environment}-${var.app_name}-runtime-tools"
  description         = "Git, Make, and build tools for generate-code Lambda"
  compatible_runtimes = ["python3.12"]

  source_path = "${path.module}/../lambda/runtime-layer"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Ensure layer is built before packaging
  depends_on = [null_resource.build_runtime_layer]
}

# ============================================================================
# Terraform Tools Lambda Layer (for generate-code)
# ============================================================================

# Build the terraform layer before packaging
resource "null_resource" "build_terraform_layer" {
  triggers = {
    # Rebuild when build script changes
    build_script = filemd5("${path.module}/../scripts/build-terraform-layer.sh")
    # Force rebuild by changing this timestamp if needed
    force_rebuild = "2025-11-20T16:30:00Z"
  }

  provisioner "local-exec" {
    command     = "./scripts/build-terraform-layer.sh"
    working_dir = "${path.module}/.."
  }
}

module "terraform_tools_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  create_layer = true

  layer_name          = "${var.environment}-${var.app_name}-terraform-tools"
  description         = "Terraform CLI for formatting and validating .tf files"
  compatible_runtimes = ["python3.12"]

  source_path = "${path.module}/../lambda/terraform-layer"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Ensure layer is built before packaging
  depends_on = [null_resource.build_terraform_layer]
}
