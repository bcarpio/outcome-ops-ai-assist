# ============================================================================
# Databao Context Engine Lambda Layer
#
# Bundles the Databao Context Engine (Apache 2.0, JetBrains) plus the
# Postgres / MySQL / MS SQL Server drivers needed by database-sync. Built
# inside the Amazon Linux 2 / Python 3.12 image so native components in
# the drivers resolve to ABI-compatible wheels for the Lambda runtime.
#
# Size watch: Lambda layers are capped at 250 MB unzipped. With all three
# drivers the layer typically lands ~150-200 MB. If a future driver
# (Oracle, SAP HANA) pushes us over, split into per-engine sub-layers and
# attach selectively per Lambda.
# ============================================================================

resource "null_resource" "build_databao_layer" {
  count = var.enable_workspaces ? 1 : 0

  triggers = {
    # Rebuild when the requirements list or build script changes
    requirements = filemd5("${path.module}/../lambda/databao-layer/requirements.txt")
    build_script = filemd5("${path.module}/../scripts/build-databao-layer.sh")
    # Rebuild when the layer dir is missing
    layer_exists = fileexists("${path.module}/../lambda/databao-layer/python/databao_context_engine/__init__.py") ? "exists" : "missing-${timestamp()}"
    # Bump this to force a fresh rebuild (e.g., to pick up an upstream Databao release)
    force_rebuild = "2026-05-12T00:00:00Z"
  }

  provisioner "local-exec" {
    command     = "./scripts/build-databao-layer.sh"
    working_dir = "${path.module}/.."
  }
}

module "databao_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  create_layer = true

  layer_name          = "${var.environment}-${var.app_name}-databao"
  description         = "Databao Context Engine + DB drivers (Postgres / MySQL / MSSQL)"
  compatible_runtimes = ["python3.12"]

  source_path = "${path.module}/../lambda/databao-layer"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Ensure layer is built before packaging
  depends_on = [null_resource.build_databao_layer]
}

output "databao_layer_arn" {
  description = "ARN of the Databao Context Engine Lambda layer"
  value       = var.enable_workspaces ? module.databao_layer[0].lambda_layer_arn : null
}
