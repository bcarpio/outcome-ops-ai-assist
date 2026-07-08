# ============================================================================
# Bedrock Application Inference Profiles -- secondary region (HA parity)
# ============================================================================
#
# Mirrors terraform/bedrock-inference-profiles.tf into the secondary
# region. Shared workload vocab + profile key map come from that file
# (locals are module-scoped so both files see them).
#
# HA parity rule per CLAUDE.md: primary-region resources must have
# secondary-region counterparts, or failover picks up untagged
# Bedrock spend and Cost Explorer attribution collapses under load.
# Only region-specific values (copyFrom ARNs, provider alias) diverge.
#
# Gated on `var.secondary_region != ""` -- when HA is off, no
# secondary-region profiles get created (matches the pattern in
# `lambda-*-secondary.tf`).

locals {
  # Model slug -> copyFrom ARN, secondary region. Same shape as the
  # primary map but with `var.secondary_region` in every ARN so the
  # profile is scoped to the correct region.
  bedrock_model_copy_from_secondary = {
    haiku         = "arn:aws:bedrock:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    sonnet        = "arn:aws:bedrock:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-sonnet-4-6"
    titan-embed   = "arn:aws:bedrock:${var.secondary_region}::foundation-model/amazon.titan-embed-text-v2:0"
    cohere-rerank = "arn:aws:bedrock:${var.secondary_region}::foundation-model/cohere.rerank-v3-5:0"
  }
}

resource "aws_bedrock_inference_profile" "workload_secondary" {
  for_each = var.secondary_region != "" ? local.bedrock_profile_key_map : {}

  provider = aws.secondary

  name = "${var.environment}-${var.app_name}-${each.value.workload}-${each.value.model}"

  # Same regex-safe description shape as primary (ADR-016 v1.1).
  description = "ADR-016 cost-attribution profile for ${each.value.workload} workload ${each.value.model} calls"

  model_source {
    copy_from = local.bedrock_model_copy_from_secondary[each.value.model]
  }

  # Customer-configurable workload tag with "bedrock-workload-" prefix --
  # see primary file for rationale.
  tags = merge(
    {
      Application = var.app_name
      Environment = var.environment
    },
    { (var.bedrock_workload_tag_name) = "bedrock-workload-${each.value.workload}" },
  )
}

resource "aws_ssm_parameter" "bedrock_profile_arn_secondary" {
  for_each = var.secondary_region != "" ? local.bedrock_profile_key_map : {}

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/bedrock/${each.value.workload}/${each.value.model}-profile-arn"
  type  = "String"
  value = aws_bedrock_inference_profile.workload_secondary[each.key].arn

  tags = merge(
    {
      Application = var.app_name
      Environment = var.environment
    },
    { (var.bedrock_workload_tag_name) = "bedrock-workload-${each.value.workload}" },
  )
}


# Chunk 04 -- secondary-region per-workload env-var maps.
# Same shape as primary; empty map when HA is off so secondary-region
# Lambda modules can merge unconditionally without a null check.
locals {
  bedrock_chat_env_secondary = var.secondary_region != "" ? {
    BEDROCK_CHAT_ADVANCED_PROFILE_ARN      = aws_bedrock_inference_profile.workload_secondary["chat-sonnet"].arn
    BEDROCK_CHAT_BASIC_PROFILE_ARN         = aws_bedrock_inference_profile.workload_secondary["chat-haiku"].arn
    BEDROCK_CHAT_TITAN_EMBED_PROFILE_ARN   = aws_bedrock_inference_profile.workload_secondary["chat-titan-embed"].arn
    BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN = aws_bedrock_inference_profile.workload_secondary["chat-cohere-rerank"].arn
  } : {}
  bedrock_code_maps_env_secondary = var.secondary_region != "" ? {
    BEDROCK_CODE_MAPS_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload_secondary["code-maps-sonnet"].arn
    BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload_secondary["code-maps-haiku"].arn
  } : {}
  bedrock_code_gen_env_secondary = var.secondary_region != "" ? {
    BEDROCK_CODE_GEN_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload_secondary["code-gen-sonnet"].arn
    BEDROCK_CODE_GEN_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload_secondary["code-gen-haiku"].arn
  } : {}
  bedrock_pr_analyzer_env_secondary = var.secondary_region != "" ? {
    BEDROCK_PR_ANALYZER_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload_secondary["pr-analyzer-sonnet"].arn
    BEDROCK_PR_ANALYZER_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload_secondary["pr-analyzer-haiku"].arn
  } : {}
}
