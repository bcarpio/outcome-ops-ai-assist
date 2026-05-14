# ============================================================================
# Customer-provided CA bundles (BYO trust roots for non-RDS / on-prem DBs)
# ============================================================================
# Lets a customer point the database container build at one or more SSM
# Parameter Store SecureString parameters holding PEM-encoded CA roots /
# intermediates. The image build script fetches them, validates each via
# `openssl x509 -noout -dates`, and bakes them into
# /etc/pki/ca-trust/source/anchors/ alongside the AWS RDS global bundle.
#
# Used for non-RDS or on-prem MSSQL backed by corporate CAs that aren't
# in the system trust store of the base Lambda image (Azure SQL with a
# corporate root, on-prem MSSQL signed by an internal CA, GCP Cloud SQL
# for MSSQL with custom certs, etc.). RDS coverage is unchanged - this
# is additive.
#
# This lives in its own file (not terraform/variables.tf /
# terraform/outputs.tf) so it can be added / removed / refactored
# without contending with unrelated chunks editing the same files.
# ============================================================================

variable "customer_ca_bundle_ssm_param_arns" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Optional SSM Parameter Store SecureString ARNs holding PEM-encoded
    CA bundles to bake into the database container images. The AWS RDS
    global bundle is always included regardless of this setting.

    Used for non-RDS or on-prem MSSQL backed by corporate CAs that
    aren't in the system trust store of the base Lambda image.

    Each parameter must contain a single valid X.509 PEM (root or
    intermediate). For multi-cert chains, either concatenate them
    into one SecureString Advanced parameter (8 KB cap) or split
    across multiple Standard parameters (4 KB each).

    Build-time validation runs `openssl x509 -noout -dates` per
    parameter; any malformed PEM hard-fails the image build with the
    offending ARN named in the build log. Customers rotate certs by
    updating the SecureString, then re-running
    `make build-database-images && terraform apply` - the runtime
    container reads the bundle from `/etc/pki/ca-trust/extracted/`
    which is baked at image-build time, so a redeploy is the
    rotation step.
  EOT
}

# Echoed as an output so scripts/build-database-images.sh can read the
# list via `terraform -chdir=terraform output -json
# customer_ca_bundle_ssm_param_arns` without re-parsing the tfvars file.
output "customer_ca_bundle_ssm_param_arns" {
  value       = var.customer_ca_bundle_ssm_param_arns
  description = "SSM ARNs pulled into the database container images at build time."
}
