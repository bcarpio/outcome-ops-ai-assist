# ============================================================================
# Bedrock Application Inference Profiles -- primary region
# ============================================================================
#
# Per ADR-016 v1.2 (multi-workload partitioning) in the outcomeops-adrs
# repo. OutcomeOps AI Assist is a multi-workload app: chat, code-maps,
# code-gen, pr-analyzer all share the same handful of Bedrock models
# (Anthropic Claude Haiku + Sonnet via `global.` cross-region profiles;
# Amazon Titan Text Embeddings v2; Cohere Rerank v3.5 as raw foundation
# models). One application inference profile per (workload x model) per
# region gets us cost attribution across apps AND across internal
# workflows in Cost Explorer -- closes the "which workflow is burning
# our Bedrock spend?" question.
#
# Naming: `${env}-${app}-<workload>-<model>` -- multi-workload variant
# from ADR-016 v1.2 section 1.
# Tags:   Application (explicit -- default_tags already sets it, but
#         ADR-016 v1.1 warns against relying on that in case future
#         default_tags drift), Environment, Workload.
# SSM:    `/{env}/{app}/bedrock/{workload}/{model-slug}-profile-arn`
#         per ADR-016 v1.2 section 6.
#
# This chunk creates the profiles ONLY. IAM grants (chunk 02), handler
# wiring (chunk 03), and env-var delivery (chunk 04) come next. Cost
# allocation tag activation is chunk 05 (manual Billing-console step).

locals {
  # ADR-016 v1.2 -- fixed workload vocabulary. Treat as a contract:
  # adding/removing a workload = terraform churn AND handler-side
  # changes at every Bedrock call site.
  bedrock_workloads = ["chat", "code-maps", "code-gen", "pr-analyzer"]

  # Sparse workload -> models map. Models not listed for a workload
  # are not wrapped for that workload -- no profile created, no
  # attribution possible. Match the caller inventory in
  # plan/overview.md (grep-verified 2026-07-08).
  #
  # cohere-rerank is conditional on bedrock_rerank_enabled -- Cohere
  # Rerank isn't in every Bedrock region (e.g. missing from us-east-2
  # where the openai workspace runs for GPT-5.5). When rerank is off,
  # CreateInferenceProfile 404s "Model not found" so we skip the
  # profile entirely; retrieval callers fall back to cosine ordering
  # per shared.retrieval.rerank's graceful-degrade path.
  bedrock_workload_models = {
    chat = concat(
      ["haiku", "sonnet", "titan-embed"],
      var.bedrock_rerank_enabled ? ["cohere-rerank"] : [],
    )
    code-maps = ["haiku", "sonnet"]
    # code-gen uses Sonnet for generation + Haiku for PR-comment-command
    # classification (handle-command Lambda).
    code-gen    = ["haiku", "sonnet"]
    pr-analyzer = ["haiku", "sonnet"]
  }

  # Flattened (workload, model) tuples so both regions can iterate
  # with a stable map key (stable = terraform doesn't recreate a
  # profile when the map ordering wobbles across plans).
  bedrock_profile_tuples = flatten([
    for wl, models in local.bedrock_workload_models : [
      for m in models : { workload = wl, model = m }
    ]
  ])
  bedrock_profile_key_map = {
    for t in local.bedrock_profile_tuples :
    "${t.workload}-${t.model}" => t
  }

  # Model slug -> copyFrom ARN, primary region.
  # -- Claude models wrap the `global.` system-defined cross-region
  #    profile (accepts an account-scoped ARN even though routing is
  #    global; verified working in us-west-2 per ADR-016 v1.2 section 2).
  # -- Titan embed and Cohere rerank wrap the raw foundation-model ARN
  #    (regional, no account segment in the ARN).
  bedrock_model_copy_from_primary = {
    haiku         = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-haiku-5-5"
    sonnet        = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-sonnet-5-5"
    titan-embed   = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
    cohere-rerank = "arn:aws:bedrock:${var.aws_region}::foundation-model/cohere.rerank-v3-5:0"
  }
}

resource "aws_bedrock_inference_profile" "workload" {
  for_each = local.bedrock_profile_key_map

  name = "${var.environment}-${var.app_name}-${each.value.workload}-${each.value.model}"

  # Description regex constraint (ADR-016 v1.1): letters, digits, colon,
  # period, single space/underscore/hyphen. No parens, no commas, no
  # double separators. Keep plain.
  description = "ADR-016 cost-attribution profile for ${each.value.workload} workload ${each.value.model} calls"

  model_source {
    copy_from = local.bedrock_model_copy_from_primary[each.value.model]
  }

  # ADR-016 v1.2 tag dimensions mapped through the customer-configurable
  # workload-tag key. This platform deploys into customer AWS accounts
  # (per [[project_deploy_model_not_saas]]) and every enterprise has an
  # already-approved tag vocabulary; asking them to activate a
  # platform-specific tag NAME creates real friction. So the tag NAME
  # is a tfvar, defaulting to "Purpose" (owner's convention).
  #
  # The tag VALUE prefixes "bedrock-workload-" onto the workload slug so
  # it never collides with the existing per-resource-identity Purpose
  # values in this repo (Lambda modules use Purpose="chat-streaming",
  # "salesforce-integration", etc. -- distinct from Bedrock's "chat",
  # "code-maps", "code-gen", "pr-analyzer" workload dimension). Cost
  # Explorer group-by-Purpose with a Service=Amazon Bedrock filter
  # returns the 4 bedrock-workload-* buckets unambiguously.
  tags = merge(
    {
      Application = var.app_name
      Environment = var.environment
    },
    { (var.bedrock_workload_tag_name) = "bedrock-workload-${each.value.workload}" },
  )
}

resource "aws_ssm_parameter" "bedrock_profile_arn" {
  for_each = local.bedrock_profile_key_map

  name  = "/${var.environment}/${var.app_name}/bedrock/${each.value.workload}/${each.value.model}-profile-arn"
  type  = "String"
  value = aws_bedrock_inference_profile.workload[each.key].arn

  tags = merge(
    {
      Application = var.app_name
      Environment = var.environment
    },
    { (var.bedrock_workload_tag_name) = "bedrock-workload-${each.value.workload}" },
  )
}


# ============================================================================
# Chunk 04 -- per-workload env-var maps for Lambda merge blocks
# ============================================================================
#
# shared/model_client.resolve_profile_arn(workload, tier) reads env vars
# named BEDROCK_<WORKLOAD>_<TIER>_PROFILE_ARN. Tier vocab (advanced /
# basic / titan-embed / cohere-rerank) is the caller-facing abstraction
# (per shared/audit.py conventions); the physical profile name uses the
# model slug (haiku / sonnet / titan-embed / cohere-rerank). This block
# bridges the two so callers can address models by tier while cost
# attribution stays per-model.
#
# Callers merge exactly ONE workload map into environment_variables:
#     environment_variables = merge(
#       { ... existing ... },
#       local.bedrock_chat_env,   # or code_maps / code_gen / pr_analyzer
#     )

locals {
  # Conditionally include the cohere-rerank profile ARN only when
  # rerank is enabled. Merging an empty map is a no-op; when enabled,
  # the env var is set for retrieval callers to resolve.
  # Claude (sonnet/haiku) app-profile env vars are gated: their profile
  # authorization expands to the REGION-LESS foundation-model ARN
  # (arn:aws:bedrock:::foundation-model/...), which Control Tower's
  # region-deny SCP explicit-denies (aws:RequestedRegion is absent in
  # that authorization context -- live-burned 2026-07-21, first Bedrock
  # call after org enrollment). With the flag false, resolve_profile_arn
  # returns None and callers fall back to the us.* system profiles,
  # whose authorization is regional-ARNs-only and passes the SCP.
  # Titan/Cohere app profiles wrap plain regional models (no region-less
  # expansion) and stay wired unconditionally.
  bedrock_chat_env = merge(
    {
      BEDROCK_CHAT_TITAN_EMBED_PROFILE_ARN = aws_bedrock_inference_profile.workload["chat-titan-embed"].arn
    },
    var.bedrock_claude_app_profiles_enabled ? {
      BEDROCK_CHAT_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload["chat-sonnet"].arn
      BEDROCK_CHAT_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload["chat-haiku"].arn
    } : {},
    var.bedrock_rerank_enabled ? {
      BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN = aws_bedrock_inference_profile.workload["chat-cohere-rerank"].arn
    } : {},
  )
  bedrock_code_maps_env = var.bedrock_claude_app_profiles_enabled ? {
    BEDROCK_CODE_MAPS_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload["code-maps-sonnet"].arn
    BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload["code-maps-haiku"].arn
  } : {}
  bedrock_code_gen_env = var.bedrock_claude_app_profiles_enabled ? {
    BEDROCK_CODE_GEN_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload["code-gen-sonnet"].arn
    BEDROCK_CODE_GEN_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload["code-gen-haiku"].arn
  } : {}
  bedrock_pr_analyzer_env = var.bedrock_claude_app_profiles_enabled ? {
    BEDROCK_PR_ANALYZER_ADVANCED_PROFILE_ARN = aws_bedrock_inference_profile.workload["pr-analyzer-sonnet"].arn
    BEDROCK_PR_ANALYZER_BASIC_PROFILE_ARN    = aws_bedrock_inference_profile.workload["pr-analyzer-haiku"].arn
  } : {}
}
