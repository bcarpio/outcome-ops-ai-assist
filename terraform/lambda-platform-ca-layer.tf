# ============================================================================
# Platform-CA Lambda Layer
# ============================================================================
# Ships a single merged PEM at /opt/ca-bundle.pem inside any Lambda the
# layer is attached to. The PEM is Mozilla certifi (always) concatenated
# with the customer-supplied roots fetched from the SSM SecureString ARNs
# in var.platform_ca_bundle_ssm_param_arns.
#
# Built by scripts/build-platform-ca-layer.sh (invoked via
# `make build-platform-layer`). The zip lives at
# build/platform-ca-layer/layer.zip relative to the repo root.
#
# The layer is attached UNCONDITIONALLY to every platform Lambda that
# egresses to the UI ALB (today: chat, workspace-management; see
# lambda-chat.tf + lambda-workspace-mgmt.tf). Passthrough mode -- empty
# platform_ca_bundle_ssm_param_arns -- ships pure certifi, so it's a no-op
# for default-public-cert deploys. Keeping attachment uniform means
# BYO-CA and default deploys exercise the same code path.
#
# Rotation: when a SecureString in platform_ca_bundle_ssm_param_arns is
# updated, re-run `make build-platform-layer` then `terraform apply`. The
# filebase64sha256 below will detect the new zip, terraform publishes a
# new layer version, and each attached Lambda updates its layer
# reference. The Lambda itself does NOT need to be redeployed.
# ============================================================================

resource "aws_lambda_layer_version" "platform_ca" {
  layer_name       = "${var.environment}-${var.app_name}-platform-ca"
  filename         = "${path.module}/../build/platform-ca-layer/layer.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/platform-ca-layer/layer.zip")

  # Matches the runtime of every platform Lambda the layer is attached to
  # (chat, workspace-management). Verified via grep against lambda-*.tf.
  compatible_runtimes = ["python3.12"]

  description = "PEM-merged trust bundle (Mozilla certifi + customer roots from platform_ca_bundle_ssm_param_arns). Mount path inside Lambda is /opt/ca-bundle.pem."
}

output "platform_ca_layer_arn" {
  description = "ARN of the platform-CA Lambda Layer. Attached to every Lambda that calls back into the UI ALB so REQUESTS_CA_BUNDLE / SSL_CERT_FILE can resolve to /opt/ca-bundle.pem."
  value       = aws_lambda_layer_version.platform_ca.arn
}
