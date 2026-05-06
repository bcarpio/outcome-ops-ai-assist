# ============================================================================
# Comprehend Detect-PII region availability precondition
# ============================================================================
#
# When pii_redaction_enabled = true, every ingest Lambda calls
# comprehend:DetectPiiEntities at ingest time. The action is only
# available in a subset of AWS regions; deploying with the var on in
# an unsupported region would fail at runtime, often days into a
# customer trial. Failing here at plan time is loud and obvious.
#
# Reference: https://docs.aws.amazon.com/general/latest/gr/comprehend.html
# Last reviewed: 2026-05-05.

locals {
  comprehend_pii_supported_regions = toset([
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2",
    "ap-northeast-1", "ap-northeast-2",
    "ca-central-1",
  ])
}

resource "null_resource" "comprehend_region_guard" {
  count = var.pii_redaction_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition = contains(
        local.comprehend_pii_supported_regions,
        var.aws_region,
      )
      error_message = format(
        "PII redaction (var.pii_redaction_enabled = true) requires Amazon Comprehend Detect-PII, which is not available in %s. Either set pii_redaction_enabled = false or deploy to a supported region: %s.",
        var.aws_region,
        join(", ", sort(tolist(local.comprehend_pii_supported_regions))),
      )
    }
  }
}
