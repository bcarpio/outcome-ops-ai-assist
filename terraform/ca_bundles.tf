# ============================================================================
# CA bundles - BYO trust roots for outbound connections from platform code
# ============================================================================
# Two parallel bundles:
#
# 1. customer_ca_bundle_ssm_param_arns -> baked into database container
#    images at build time (database-integration, database-sync). Used
#    for non-RDS / on-prem databases backed by corporate CAs that
#    aren't in the system trust store of the base Lambda image.
#
# 2. platform_ca_bundle_ssm_param_arns -> baked into a Lambda Layer
#    attached to platform Lambdas that call back into the UI ALB
#    (today: chat -> MCP). Used when ui_acm_certificate_arn points at
#    a cert signed by a private / internal CA so requests/urllib3
#    actually trust the listener.
#
# Same SSM SecureString + rotate-and-rebuild model for both. Keep the
# variables side-by-side here so customers grep one file when
# onboarding their PKI.
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

variable "platform_ca_bundle_ssm_param_arns" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Optional SSM Parameter Store SecureString ARNs holding PEM-encoded
    CA roots / intermediates to bake into the platform-CA Lambda
    layer. The layer is attached to every platform Lambda that calls
    back into the UI ALB (today: chat -> MCP), with REQUESTS_CA_BUNDLE
    + SSL_CERT_FILE pointed at the merged bundle. Required when
    ui_acm_certificate_arn points at a cert signed by a private /
    internal CA - without it, chat -> MCP calls 500 with SSL
    verification errors because the Lambda runtime ships only the
    Mozilla certifi bundle. Same SSM rotate-and-rebuild model as the
    existing customer_ca_bundle_ssm_param_arns for database images.
  EOT
}

# Echoed as an output so scripts/build-database-images.sh can read the
# list via `terraform -chdir=terraform output -json
# customer_ca_bundle_ssm_param_arns` without re-parsing the tfvars file.
output "customer_ca_bundle_ssm_param_arns" {
  value       = var.customer_ca_bundle_ssm_param_arns
  description = "SSM ARNs pulled into the database container images at build time."
}

# Echoed as an output so scripts/build-platform-layer.sh can read the
# list via `terraform -chdir=terraform output -json
# platform_ca_bundle_ssm_param_arns` without re-parsing the tfvars file.
output "platform_ca_bundle_ssm_param_arns" {
  value       = var.platform_ca_bundle_ssm_param_arns
  description = "SSM ARNs pulled into the platform-CA Lambda layer at build time."
}
