variable "environment" {
  type        = string
  description = "Environment name (e.g., dev, prd, cmk)"
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.environment))
    error_message = "Environment must contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "app_name" {
  type        = string
  description = "Application name"
  default     = "outcome-ops-ai-assist"
}

variable "aws_region" {
  type        = string
  description = "Primary AWS region for this deployment. Terraform providers are configured against this region."
}

# Optional HA: when set, every stateful resource is provisioned
# in both regions, the platform runs cross-region replication
# via EventBridge, and an AppConfig flag in each region
# controls which is currently active.
variable "secondary_region" {
  type        = string
  description = "Secondary AWS region for HA active/passive deployment. Set to \"\" or omit for single-region (default)."
  default     = ""
}

# Initial AppConfig role values per region at first deploy. Primary
# region starts "active", secondary starts "passive". Operators flip
# these values during failover via the AWS AppConfig console — they
# do NOT update tfvars or re-run terraform during failover (see
# docs/HA.md). The lifecycle block on the AppConfig
# hosted_configuration_version ignores changes to content after
# first apply, so subsequent terraform applies won't clobber an
# operator-driven flip.
variable "initial_region_role_primary" {
  type        = string
  description = "Initial AppConfig role for the primary region: \"active\" or \"passive\"."
  default     = "active"
  validation {
    condition     = contains(["active", "passive"], var.initial_region_role_primary)
    error_message = "initial_region_role_primary must be active or passive"
  }
}

variable "initial_region_role_secondary" {
  type        = string
  description = "Initial AppConfig role for the secondary region: \"active\" or \"passive\"."
  default     = "passive"
  validation {
    condition     = contains(["active", "passive"], var.initial_region_role_secondary)
    error_message = "initial_region_role_secondary must be active or passive"
  }
}

variable "github_usernames_to_tag" {
  type        = string
  description = "Comma-separated GitHub usernames to tag when tests fail (e.g., 'user1,user2' for teams or 'user1' for single user)"
  default     = ""
  validation {
    condition     = can(regex("^[a-zA-Z0-9_,-]*$", var.github_usernames_to_tag))
    error_message = "GitHub usernames must contain only alphanumeric characters, underscores, hyphens, and commas."
  }
}

# ============================================================================
# Integration Toggles
# ============================================================================

variable "enable_workspaces" {
  type        = bool
  description = "Enable workspaces feature for team/project groupings with OAuth integrations (Confluence, Jira, Outlook)"
  default     = false
}

variable "initial_org_admin_email" {
  type        = string
  description = "Email of the initial org admin who can manage all workspaces and add other org admins. Required when enable_workspaces is true."
  default     = ""
}

variable "atlassian_oauth_client_id" {
  type        = string
  description = "Atlassian OAuth App Client ID for Confluence/Jira integration"
  default     = ""
}

variable "microsoft_oauth_client_id" {
  type        = string
  description = "Microsoft OAuth App Client ID for Outlook 365 integration"
  default     = ""
}

# ============================================================================
# Azure DevOps integration (source code parity with GitHub)
#
# Uses a SEPARATE Entra ID app registration from microsoft_oauth_client_id
# above. The Outlook / M365 app grants Microsoft Graph scopes; ADO is a
# distinct API surface (resource ID 499b84ac-1321-427f-aa17-267ca6975798)
# and the consent-screen blast radius is cleaner with a dedicated app.
#
# All five variables default to empty so the integration is OFF by default;
# customers without ADO pay no operational tax. The integration enables
# itself only when all four config values are populated AND
# enable_azure_devops_integration is true.
# ============================================================================

variable "azure_devops_tenant_id" {
  type        = string
  description = "Entra ID tenant ID hosting the Azure DevOps OAuth app registration. Empty disables the integration."
  default     = ""
}

variable "azure_devops_client_id" {
  type        = string
  description = "Entra ID app registration client ID for the Azure DevOps integration."
  default     = ""
}

variable "azure_devops_client_secret_ssm_param" {
  type        = string
  description = "SSM SecureString parameter name holding the Entra ID app client secret (operator-managed; terraform does NOT create or read the value)."
  default     = ""
}

variable "azure_devops_redirect_uri" {
  type        = string
  description = "OAuth callback URI registered with the Entra ID app. Must match the path /api/azure-devops/callback on the deploy's FQDN."
  default     = ""
}

variable "enable_azure_devops_integration" {
  type        = bool
  description = "Provision the workspace Azure DevOps (Repos) integration surface: integration Lambda + Function URL + sync + scheduler + SQS + DLQ + EventBridge rule + secondary-region mirror. Default false. Customers without ADO leave this false and pay zero footprint."
  default     = false
}

variable "enable_azure_devops_boards_integration" {
  type        = bool
  description = "Provision the workspace Azure DevOps Boards (work items) integration surface. Reuses the same Entra ID app + tenant + client-secret config as the Repos integration; requires the vso.work API permission granted on that app registration. Default false."
  default     = false
}

variable "enable_github_integration" {
  type        = bool
  description = "Provision the workspace GitHub App integration surface (integration Lambda + Function URL + sync + scheduler + SQS + DLQ + SSM parameters for App credentials). Distinct from enable_github_issue_integration, which gates the code-generation API Gateway. Default false."
  default     = false
}

variable "enable_confluence_integration" {
  type        = bool
  description = "Provision the workspace Confluence integration surface (integration Lambda + Function URL + sync + scheduler + SQS + DLQ). Shares Atlassian OAuth SSM parameters with the Jira integration -- either flag being true keeps those SSM parameters. Default false."
  default     = false
}

variable "enable_outlook_integration" {
  type        = bool
  description = "Provision the workspace Outlook (Microsoft 365) integration surface. Shares Microsoft OAuth SSM parameters with Teams/SharePoint/OneDrive/OneNote -- any of those flags being true keeps the shared parameters. Default false."
  default     = false
}

variable "enable_teams_integration" {
  type        = bool
  description = "Provision the workspace Microsoft Teams integration surface. Shares Microsoft OAuth SSM parameters with Outlook/SharePoint/OneDrive/OneNote. Default false."
  default     = false
}

variable "enable_sharepoint_integration" {
  type        = bool
  description = "Provision the workspace SharePoint integration surface. Shares Microsoft OAuth SSM parameters with Outlook/Teams/OneDrive/OneNote. Default false."
  default     = false
}

variable "enable_onedrive_integration" {
  type        = bool
  description = "Provision the workspace OneDrive integration surface. Shares Microsoft OAuth SSM parameters with Outlook/Teams/SharePoint/OneNote. Default false."
  default     = false
}

variable "enable_onenote_integration" {
  type        = bool
  description = "Provision the workspace OneNote integration surface. Shares Microsoft OAuth SSM parameters with Outlook/Teams/SharePoint/OneDrive. Default false."
  default     = false
}

variable "enable_servicenow_integration" {
  type        = bool
  description = "Provision the workspace ServiceNow Knowledge Base integration surface (integration Lambda + Function URL + sync + scheduler + SQS + DLQ). Requires servicenow_instance_url + servicenow_oauth_client_id populated in tfvars and the /{env}/{app}/oauth/servicenow/client-secret SSM SecureString populated by the operator. Default false."
  default     = false
}

variable "servicenow_instance_url" {
  type        = string
  description = "ServiceNow instance URL (e.g. https://acme.service-now.com). The Application Registry OAuth app must be registered on this instance with a Redirect URL of https://<ui_fqdn>/api/servicenow/callback."
  default     = ""
}

variable "servicenow_oauth_client_id" {
  type        = string
  description = "OAuth Client ID from the ServiceNow Application Registry entry. Public value, safe in tfvars."
  default     = ""
}

variable "enable_salesforce_integration" {
  type        = bool
  description = "Provision the workspace Salesforce CRM integration surface (integration Lambda + Function URL + sync + scheduler + SQS + DLQ). Ingests Account/Contact/Opportunity + attached files + embedded recent activities. Requires salesforce_oauth_client_id populated in tfvars and the /{env}/{app}/oauth/salesforce/client-secret SSM SecureString populated by the operator. Default false."
  default     = false
}

variable "salesforce_oauth_client_id" {
  type        = string
  description = "Consumer Key from the Salesforce Connected App. Public value, safe in tfvars. Instance URL is discovered per-workspace at OAuth time -- not a deploy config."
  default     = ""
}

variable "enable_database_integration" {
  type        = bool
  description = "Provision the workspace Database integration surface (integration Lambda + Function URL + sync + scheduler + SQS + DLQ + drift-notification SNS topic). Default false."
  default     = false
}

variable "github_app_slug" {
  type        = string
  description = "GitHub App URL slug (from the App settings page URL, e.g., 'my-company-ai-assist')"
  default     = ""
}

variable "github_app_id" {
  type        = string
  description = "GitHub App ID (numeric, from App settings > General > About)"
  default     = ""
}

variable "github_client_id" {
  type        = string
  description = "GitHub App OAuth client ID (from App settings > General > About)"
  default     = ""
}

variable "enable_github_issue_integration" {
  type        = bool
  description = "Enable GitHub Issues integration - webhook triggers code generation from issue labels"
  default     = true
}

variable "enable_jira_integration" {
  type        = bool
  description = "Provision the Jira integration surface -- BOTH the workspace-level Jira Lambdas (integration + sync + scheduler + SQS + DLQ) AND the SSM Automation cross-account document + IAM role that let Jira Automation trigger code generation. Shares Atlassian OAuth SSM parameters with Confluence. Default false."
  default     = false
}

variable "jira_external_id" {
  type        = string
  description = "External ID from Jira Automation AWS connector setup (required if enable_jira_integration is true). Generated by Atlassian when configuring the AWS integration."
  default     = ""
}

# ============================================================================
# OutcomeOps License Configuration
# ============================================================================

variable "outcomeops_license_layer_arn" {
  type        = string
  description = "ARN of the OutcomeOps license validator Lambda layer (required - ADR-019)"

  validation {
    condition     = length(var.outcomeops_license_layer_arn) > 0
    error_message = "outcomeops_license_layer_arn is required (ADR-019). Set it in dev.tfvars / prd.tfvars."
  }
}

# Secondary-region copy of the license-validator layer. The license server
# push-replicates the layer to every region per AWS account, so for
# single-account multi-region deploys (the owner's prd) the operator
# fetches the us-east-2 ARN via:
#   aws lambda list-layer-versions --region us-east-2 \
#     --layer-name ${env}-outcomeops-license-validator-v2-0-0
# and pastes it into prd.tfvars. Required when secondary_region is set;
# chat + integration handler Lambdas in secondary fail-close on license
# validation without this.
variable "outcomeops_license_layer_arn_secondary" {
  type        = string
  description = "ARN of the OutcomeOps license validator Lambda layer in the secondary region (ADR-019). Required before chunk 9c (secondary integration Lambdas) can apply; chunk 9a (ECR / Route53 / ACM) doesn't consume it."
  default     = ""
}

variable "outcomeops_license_ssm_param" {
  type        = string
  description = "SSM parameter path for the OutcomeOps license key"
  default     = "/outcome-ops-ai-assist/license/key"
}

variable "outcomeops_license_server_url" {
  type        = string
  description = "URL of the OutcomeOps license server for usage reporting"
  default     = "https://license.example.com"
}

# ============================================================================
# Performance Tuning
# ============================================================================

variable "enable_bedrock_cooldown" {
  type        = bool
  description = "Enable cooldown between Bedrock API calls to prevent throttling. Disable for enterprise accounts with high quotas."
  default     = true
}

variable "code_map_target_shard_tokens" {
  type        = number
  description = "Soft cap the code-map splitter (process-repo-summary) aims for when packing files into shards. Default 120000; leaves ~30k tokens of headroom under code_map_max_prompt_tokens for prompt template + merge instructions + output tokens. Tunable without redeploy -- new value picks up on next Lambda cold start."
  default     = 120000
}

variable "code_map_max_prompt_tokens" {
  type        = number
  description = "Hard ceiling for one Bedrock summarize call. Default 150000 (Bedrock limit is 200K; reserve 50K for output + safety). Consumer (process-batch-summary) reads this so an oversize shard fails loudly rather than triggering the slow K+1 sub-batch path."
  default     = 150000
}

# ============================================================================
# Bedrock Model Configuration
# ============================================================================

variable "bedrock_advanced_model_id" {
  type        = string
  description = "Bedrock model ID for advanced/primary inference (e.g., Claude Sonnet)"
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "bedrock_default_backend" {
  type        = string
  description = <<-EOT
    Fallback backend for model IDs not matched by shared.model_client._DISPATCH.

    Unset (default): prd's posture. Unknown model IDs raise
    UnsupportedModelError -- the operator must explicitly add the
    family to _DISPATCH before using it. This is the no-silent-fallback
    safety per ADR-035.

    "responses": the openai workspace's posture. Mantle-only deploys
    that exclusively use bedrock-mantle.{region}.api.aws can opt every
    unknown family (qwen/deepseek/mistral/etc.) into the Responses
    backend without needing a code change per new vendor.

    "converse": symmetric case; unlikely to be useful on its own.
  EOT
  default     = ""
  validation {
    condition     = contains(["", "converse", "responses"], var.bedrock_default_backend)
    error_message = "bedrock_default_backend must be \"\", \"converse\", or \"responses\"."
  }
}

variable "bedrock_responses_reasoning_effort" {
  type        = string
  description = <<-EOT
    Reasoning effort hint sent to the Bedrock Mantle Responses API for
    GPT-5 family models (gpt-5.4 / gpt-5.5 / codex-*). Has no effect on
    Converse-routed models -- Claude has no equivalent dial.

    GPT-5 reasoning models bill internal reasoning tokens against
    max_output_tokens and can spend much longer than non-reasoning
    models for the same answer length. "none" disables the
    deliberation chain entirely to keep chat latency tolerable.

    NOTE: Mantle's vocabulary differs from OpenAI direct. Direct
    uses "minimal" for the chat-friendly setting; Mantle uses "none."
    This variable accepts only Mantle's vocabulary -- the wire
    rejects "minimal" with HTTP 400.

    "none" (default): chat-friendly. Disables the reasoning chain
        entirely; GPT-5 answers directly like a non-reasoning model.
        Live-burned 2026-06-25: the OpenAI-recommended default
        "medium" let GPT-5.5 reason for >15min on a chat query,
        tripping Lambda's 900s timeout three invocations in a row.
    "low" / "medium" / "high" / "xhigh": progressively more
        thorough but slower.

    Only relevant when shared.model_client routes a request through
    _responses_backend (i.e. an openai.gpt-5* / openai.codex-* model
    or a deploy with bedrock_default_backend = "responses").
  EOT
  default     = "none"
  validation {
    condition     = contains(["none", "low", "medium", "high", "xhigh"], var.bedrock_responses_reasoning_effort)
    error_message = "bedrock_responses_reasoning_effort must be one of: none, low, medium, high, xhigh (Mantle vocabulary -- 'minimal' is OpenAI direct only)."
  }
}

variable "bedrock_basic_model_id" {
  type        = string
  description = "Bedrock model ID for fast/default inference (e.g., Claude Haiku)"
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_embedding_model_id" {
  type        = string
  description = "Bedrock model ID for text embedding"
  default     = "amazon.titan-embed-text-v2:0"
}

variable "bedrock_rerank_model_id" {
  type        = string
  description = "Bedrock model ID for reranking search results"
  default     = "cohere.rerank-v3-5:0"
}

variable "bedrock_rerank_enabled" {
  type        = bool
  description = <<-EOT
    Toggle Cohere-Rerank-on-Bedrock for RAG retrieval. When true (default),
    chat + query Lambdas over-fetch candidates and rerank them via
    cohere.rerank-v3-5:0 before answering. When false, the rerank pass is
    skipped and we use the raw vector-similarity ordering. Set to false in
    regions where Cohere Rerank isn't in Bedrock, or for customers who don't
    want the marketplace billing relationship.
  EOT
  default     = true
}

variable "rerank_max_fetch" {
  type        = number
  description = "Maximum number of documents to over-fetch for reranking (caps topK * 4)"
  default     = 90
}

variable "rerank_min_score" {
  type        = number
  description = "Minimum rerank relevance score (0-1) to include in results. 0 = no filtering."
  default     = 0.15
}

# Bedrock model pricing (USD per million tokens)
variable "bedrock_advanced_input_price" {
  type        = number
  description = "Price per million input tokens for the advanced model"
  default     = 3.0
}

variable "bedrock_advanced_output_price" {
  type        = number
  description = "Price per million output tokens for the advanced model"
  default     = 15.0
}

variable "bedrock_basic_input_price" {
  type        = number
  description = "Price per million input tokens for the basic model"
  default     = 0.80
}

variable "bedrock_basic_output_price" {
  type        = number
  description = "Price per million output tokens for the basic model"
  default     = 4.0
}

variable "bedrock_embedding_input_price" {
  type        = number
  description = "Price per million input tokens for the embedding model"
  default     = 0.02
}

variable "bedrock_embedding_output_price" {
  type        = number
  description = "Price per million output tokens for the embedding model (typically 0)"
  default     = 0.0
}

variable "bedrock_rerank_price_per_1k" {
  type        = number
  description = "Price per 1,000 search queries for the Cohere rerank model. Cohere Rerank 3.5 on Bedrock is billed per search query (one rerank invocation regardless of doc count up to platform limits), not per token. Default matches the AWS public price."
  default     = 2.00
}

variable "rag_max_doc_chars" {
  type        = number
  description = "Maximum characters per document passed to the model during RAG. Larger values give the model more context but use more tokens."
  default     = 3000
}

# ============================================================================
# Resource Tags
# ============================================================================

variable "tags" {
  type        = map(string)
  description = "Additional tags to apply to all resources (merged with default tags)"
  default     = {}
}

# ============================================================================
# Chat UI Deployment (Fargate + Internal ALB)
# ============================================================================

variable "deploy_ui" {
  description = "Whether to deploy the chat UI (Fargate + ALB)"
  type        = bool
  default     = false
}

variable "ui_vpc_id" {
  description = "VPC ID for UI deployment"
  type        = string
  default     = ""
}

variable "ui_private_subnet_ids" {
  description = "Private subnet IDs for Fargate tasks and internal ALB"
  type        = list(string)
  default     = []
}

variable "ui_container_image" {
  description = "Container image for UI (nginx + React build)"
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------
# Multi-region UI ingress (HA chunk 9)
# Operator-provided VPC + subnets in the secondary region. ECR cross-region
# replication (terraform/ecr.tf) handles image syncing automatically; the
# *_secondary container image var below is optional and defaults to the
# primary image URI with the region rewritten.
# ----------------------------------------------------------------------------

variable "ui_vpc_id_secondary" {
  description = "VPC ID in the secondary region for the UI ALB + Fargate. Required before chunk 9b (secondary ALB) can apply; chunk 9a (ECR replication / Route53 / ACM) doesn't consume it."
  type        = string
  default     = ""
}

variable "ui_private_subnet_ids_secondary" {
  description = "Private subnet IDs in the secondary region for UI Fargate tasks. At least two subnets across AZs are needed for ECS spread. Required before chunk 9b."
  type        = list(string)
  default     = []
}

variable "ui_container_image_secondary" {
  description = "ECR image URI in the secondary region for the chat UI. Defaults to the same digest as ui_container_image, with the region substring rewritten."
  type        = string
  default     = ""
}

variable "ui_alb_internal" {
  description = "Make the ALB internal (VPC-only access). Set to false for public ALB secured by OIDC. fable-scale chunk 09 flipped the default from false → true so a fresh enterprise deploy defaults to the safer posture; sandbox / demo deploys explicitly set `ui_alb_internal = false` in their tfvars."
  type        = bool
  default     = true
}

variable "ui_fargate_assign_public_ip" {
  description = "Assign public IP to Fargate tasks. Set to true for public subnets without NAT. Set to false for private subnets with outbound via NAT, Transit Gateway, or Direct Connect."
  type        = bool
  default     = false
}

# ============================================================================
# UI proxy Fargate sizing + autoscaling (fable-scale chunk 01)
# ----------------------------------------------------------------------------
# Historically the UI proxy shipped as one 0.25 vCPU / 512 MB FARGATE_SPOT
# task with deployment_minimum_healthy_percent = 0. Fine as a sandbox demo;
# every SSE chat stream for the whole company can't fit through it, and a
# spot reclamation drops every in-flight stream. These knobs let operators
# right-size for the deployment tier without editing terraform. Defaults
# target a small-enterprise workload (2 tasks base, scale to 20, mixed
# on-demand + spot). Enterprise operators bump max + min in tfvars.
# ============================================================================

variable "ui_fargate_cpu" {
  description = "CPU units for each UI proxy Fargate task (256 = 0.25 vCPU). 512 covers the enterprise chat + streaming workload; 256 was the sandbox default."
  type        = number
  default     = 512
}

variable "ui_fargate_memory" {
  description = "Memory (MB) for each UI proxy Fargate task. 1024 covers the Node event-loop headroom under high SSE concurrency."
  type        = number
  default     = 1024
}

variable "ui_fargate_desired_count" {
  description = "Initial desired task count for the UI proxy service. Autoscaling can adjust this out-of-band; terraform ignores subsequent drift (module lifecycle)."
  type        = number
  default     = 2
}

variable "ui_fargate_min_count" {
  description = "Autoscaling minimum task count for the UI proxy service. Below 2 there is no fault tolerance under spot reclamation."
  type        = number
  default     = 2
}

variable "ui_fargate_max_count" {
  description = "Autoscaling maximum task count for the UI proxy service. Cap at the point where the ALB target group starts to see diminishing returns (per-task connection budget)."
  type        = number
  default     = 20
}

variable "ui_fargate_target_requests_per_task" {
  description = "Target-tracking value for ALBRequestCountPerTarget on the UI proxy service. Sizing driver: chat SSE holds a socket 30-900s, so this is really 'concurrent streams per task'."
  type        = number
  default     = 500
}

variable "ui_fargate_ondemand_base" {
  description = "FARGATE (on-demand) base count in the capacity provider strategy. Set to 1+ to guarantee at least N always-on tasks that never spot-reclaim."
  type        = number
  default     = 1
}

variable "ui_fargate_ondemand_weight" {
  description = "FARGATE (on-demand) weight in the capacity provider strategy. Combined with spot_weight, controls the on-demand:spot ratio above the base."
  type        = number
  default     = 1
}

variable "ui_fargate_spot_weight" {
  description = "FARGATE_SPOT weight in the capacity provider strategy. Higher = more spot use above the on-demand base. Cost knob."
  type        = number
  default     = 3
}

variable "ui_fargate_min_healthy_percent" {
  description = "deployment_minimum_healthy_percent for the UI proxy service. 100 guarantees at least the current desired_count of healthy tasks during a deploy; 0 permits full replacement (sandbox default). Enterprise deploys should stay at 100."
  type        = number
  default     = 100
}

# ============================================================================
# MCP server Fargate sizing + autoscaling (fable-scale chunk 01)
# ----------------------------------------------------------------------------
# Same story as ui_fargate_*: sandbox default was 0.5 vCPU / 1 GB / 1
# FARGATE_SPOT task. At 2000 engineers running Claude Code with persistent
# MCP connections this melts. Autoscaling requires session state to move
# out of the module-global _sessions dict — see fable-scale chunk 02.
# ============================================================================

variable "mcp_server_cpu" {
  description = "CPU units for each MCP server Fargate task (256 = 0.25 vCPU). MCP holds persistent connections; 1024 gives thread-pool + FD headroom for the enterprise workload."
  type        = number
  default     = 1024
}

variable "mcp_server_memory" {
  description = "Memory (MB) for each MCP server Fargate task. 2048 covers the Python stdlib ThreadingHTTPServer + boto client caches."
  type        = number
  default     = 2048
}

variable "mcp_server_desired_count" {
  description = "Initial desired task count for the MCP server service. Requires DDB-backed sessions (fable-scale chunk 02); with in-memory sessions, second+ tasks won't share state."
  type        = number
  default     = 2
}

variable "mcp_server_min_count" {
  description = "Autoscaling minimum task count for the MCP server service."
  type        = number
  default     = 2
}

variable "mcp_server_max_count" {
  description = "Autoscaling maximum task count for the MCP server service."
  type        = number
  default     = 20
}

# chunk 11b (ADR-049): Azure AD OAuth (JWT) auth for the MCP server.
variable "azure_ad_tenant_id" {
  description = "Azure AD (Entra) tenant ID (GUID) for MCP OAuth JWT validation. Empty disables the JWT auth path (static keys only)."
  type        = string
  default     = ""
}

variable "mcp_jwt_audience" {
  description = "Expected `aud` claim for MCP Azure AD access tokens -- the MCP server's App Registration client ID or Application ID URI. Empty disables JWT auth."
  type        = string
  default     = ""
}

variable "mcp_static_keys_enabled" {
  description = "Allow legacy static API-key auth on the MCP server. Default true for back-compat; set false in the enterprise profile once every client uses Azure AD OAuth (ADR-049 deprecation path)."
  type        = bool
  default     = true
}

# chunk 03 (ADR-048 v1.1): Azure AD group selection UX -- naming-convention
# eligibility scope + toggleable app-only Graph read for the binding picker and
# the >200-group memberOf fallback.
variable "azure_ad_group_patterns" {
  description = "Case-insensitive globs scoping which Entra groups are eligible/visible for workspace binding (e.g. [\"outcomeops-*\", \"*-outcomeops-*\"]). Visibility filter only -- bindings stay explicit per-group. Empty = no restriction (v1.0 behavior)."
  type        = list(string)
  default     = []
}

variable "azure_ad_group_sync_enabled" {
  description = "Master on/off for Azure AD group -> workspace provisioning (OIDC groups claim + on-login reconcile)."
  type        = bool
  default     = true
}

variable "azure_ad_group_name_claim_enabled" {
  description = "True when the enterprise app emits on-prem group names (sAMAccountName) in the groups claim for synced groups. Enables the credential-free name-matching path."
  type        = bool
  default     = false
}

variable "azure_ad_group_graph_fallback_enabled" {
  description = "Allow an app-only Microsoft Graph read (env-SSM secret) to populate the full binding picker and resolve the >200-group memberOf overage. Off = credential-free (own-token picker + paste)."
  type        = bool
  default     = false
}

variable "azure_ad_graph_client_id" {
  description = "OPTIONAL override. App (client) ID of a DEDICATED Entra App Registration granted Group.Read.All (Application) for the group picker. Leave empty (default) to reuse the deploy's OIDC login app (oidc_client_id) -- just add the Group.Read.All Application permission + admin consent to that existing app. Only consulted when azure_ad_group_graph_fallback_enabled."
  type        = string
  default     = ""
}

variable "azure_ad_graph_secret_ssm_path" {
  description = "OPTIONAL override. SSM SecureString path for a DEDICATED Graph app's client secret. Leave empty (default) to reuse the OIDC login app's secret at the derived house path /<env>/<app>/oidc/client-secret (never set an SSM path in tfvars for the common case). Read at runtime by the workspace-management Lambda when the Graph fallback is enabled."
  type        = string
  default     = ""
}

variable "mcp_server_target_requests_per_task" {
  description = "Target-tracking value for ALBRequestCountPerTarget on the MCP server service. MCP tool calls are short-lived so this is closer to a QPS cap than a concurrency cap."
  type        = number
  default     = 200
}

variable "mcp_server_ondemand_base" {
  description = "FARGATE (on-demand) base count in the MCP server capacity provider strategy."
  type        = number
  default     = 1
}

variable "mcp_server_ondemand_weight" {
  description = "FARGATE (on-demand) weight in the MCP server capacity provider strategy."
  type        = number
  default     = 1
}

variable "mcp_server_spot_weight" {
  description = "FARGATE_SPOT weight in the MCP server capacity provider strategy."
  type        = number
  default     = 3
}

variable "mcp_server_min_healthy_percent" {
  description = "deployment_minimum_healthy_percent for the MCP server service."
  type        = number
  default     = 100
}

variable "mcp_server_enable_autoscaling" {
  description = "Whether to attach app-autoscaling to the MCP server service. Requires session externalization (fable-scale chunk 02). Disable when running in-memory sessions."
  type        = bool
  default     = true
}

# ============================================================================
# Bedrock fairness + graceful degradation (fable-scale chunk 05)
# ----------------------------------------------------------------------------
# All four workloads (chat, code-maps, code-gen, pr-analyzer) share the same
# per-model Bedrock on-demand quota. Without concurrency caps and fairness
# controls, a batch of PR checks or a re-ingestion burst starves the
# interactive chat surface. These knobs cap the batch workloads and give
# operators room to reserve Lambda concurrency for chat.
# ============================================================================

variable "pr_check_max_concurrency" {
  description = "Maximum concurrent process-pr-check Lambda invocations from the SQS event source. Caps PR-analyzer's share of the Bedrock quota so a Monday burst of 200 PRs doesn't 500 interactive chat streams."
  type        = number
  default     = 20
}

variable "chat_reserved_concurrency" {
  description = "Reserved concurrent executions for the chat Lambda. Null = shared account pool (sandbox default). Set to e.g. 100 in enterprise deploys so interactive chat never contends with batch jobs for account concurrency."
  type        = number
  default     = -1 # -1 == unreserved (Lambda module maps this to null)
}

variable "chat_timeout_seconds" {
  description = "Chat Lambda timeout. Historical default was 900 (max for streaming); tool-use worst case fits comfortably in 300. Shorter timeout unblocks Lambda concurrency slots sooner under sustained load."
  type        = number
  default     = 300
}

# ============================================================================
# Ingestion throughput (fable-scale chunk 08)
# ----------------------------------------------------------------------------
# Prior sandbox defaults capped github-sync at 5 concurrent + repo/batch
# summaries at 20. At thousands of repos re-ingesting on every push-to-main
# the pipeline lags by days. The 10s hardcoded cooldown in process-batch-
# summary was defensive against low Bedrock quotas — enterprise deploys
# with dedicated capacity should disable it (default 0). Merge budget was
# hardcoded 10x60s = 10 min; at scale we want a longer runway.
# ============================================================================

variable "github_sync_max_conc" {
  description = "SQS ESM MaximumConcurrency on process-github-sync (was 5). At enterprise repo counts, 5 concurrent syncs bottlenecks the entire ingestion pipeline."
  type        = number
  default     = 25
}

variable "repo_summary_max_conc" {
  description = "SQS ESM MaximumConcurrency on process-repo-summary (was 20). Repo summary work is Bedrock-heavy; bumping requires Bedrock quota headroom + the workload gate from chunk 05."
  type        = number
  default     = 50
}

variable "batch_summary_max_conc" {
  description = "SQS ESM MaximumConcurrency on process-batch-summary (was 20). Same Bedrock-quota constraint as repo_summary_max_conc."
  type        = number
  default     = 50
}

variable "batch_summary_msg_cooldown_ms" {
  description = "Per-message cooldown in process-batch-summary after Bedrock synthesis (was hardcoded 10000ms = 10s). Set to 0 (default) for enterprise Bedrock quotas; raise for low-quota accounts hitting throttles."
  type        = number
  default     = 0
}

variable "batch_summary_merge_attempts" {
  description = "Max requeue-with-delay attempts before the code-map merge fan-in gives up (was hardcoded 10). At enterprise repo scale, some shards take longer than 10 attempts × 60s = 10 min."
  type        = number
  default     = 30
}

variable "batch_summary_merge_delay_s" {
  description = "Requeue delay per merge attempt in seconds (was hardcoded 60). Combined with merge_attempts, sets the total merge-window budget."
  type        = number
  default     = 60
}

variable "github_scheduler_timeout_seconds" {
  description = "github-scheduler Lambda timeout. Was 60s (hardcoded); with parallel enqueue the scheduler completes much faster than the timeout, but 300 gives headroom for enterprise repo counts + SQS latency variance."
  type        = number
  default     = 300
}

variable "github_scheduler_parallel_workers" {
  description = "ThreadPoolExecutor worker count for github-scheduler's SQS enqueue fan-out (was serial). 32 workers × ~10ms per SQS send handles ~3000 repos inside the 300s timeout with plenty of margin."
  type        = number
  default     = 32
}

# ============================================================================
# Data-layer hygiene (fable-scale chunk 10)
# ============================================================================

variable "chat_message_ttl_seconds" {
  description = "TTL applied to new chat table rows via `expires_at`. Prior behavior: unbounded — every historical chat message survived forever. Default 90 days (7776000s). Rows written before this landed have no attribute and are ignored by TTL."
  type        = number
  default     = 7776000 # 90 days
}

variable "chat_max_message_content_chars" {
  description = "Cap on chat message content length in chars before head+tail truncation. Historical writes had no cap; large pastes approached the 400KB DDB item limit and inflated every subsequent synthesis prompt (chat re-sends the last 10 messages verbatim). 50000 covers legitimate code pastes; larger content gets truncated with a middle-of-message notice."
  type        = number
  default     = 50000
}

variable "bedrock_prompt_cache_enabled" {
  description = "Enable Bedrock Converse cachePoint on chat synthesis system prompts. Rollout-gated (default false); flip to true once cache-hit-rate telemetry validates the win. Real input-token savings on stable system prompts."
  type        = bool
  default     = false
}

# ============================================================================
# OIDC Authentication (Azure AD)
# ============================================================================

variable "domain" {
  description = "Domain name for the application (e.g., 'outcomeops.ai'). Requires a Route53 hosted zone for this domain."
  type        = string
  default     = ""
}

variable "manage_dns_in_route53" {
  type        = bool
  default     = true
  description = <<-EOT
    When true (default), terraform looks up var.domain in Route53 in
    this deployment account and creates the A-ALIAS for local.fqdn
    pointing at the UI ALB, plus the ACM DVO validation records. Set
    false when DNS is managed elsewhere - cross-account Route53,
    Infoblox, AD-DNS, NS1, BIND, etc. With this off, terraform skips
    all Route53 + ACM-validation resources and outputs the ALB DNS
    name + the FQDN you must CNAME to it. Requires
    ui_acm_certificate_arn to also be set (we cannot DNS-validate a
    public ACM cert without writing to a Route53 zone we control).
  EOT
}

variable "ui_acm_certificate_arn" {
  type        = string
  default     = ""
  description = <<-EOT
    ARN of a pre-existing ACM certificate covering local.fqdn. Leave
    empty (default) to have terraform request a new ACM public
    DNS-validated cert and write the validation CNAMEs into Route53 -
    only valid when manage_dns_in_route53 = true. Set this to BYO a
    cert: a wildcard you already have in ACM, a public-CA cert
    imported to ACM, or a private / internal-CA cert imported to ACM.
    See docs/byo-cert-and-dns.md for the platform-trust-store
    implications when this points at an internal-CA cert.
  EOT
}

variable "cors_allowed_origins" {
  description = "Additional CORS origins for S3 bucket (browser uploads). The domain variable is automatically included if set."
  type        = list(string)
  default     = ["http://localhost:*"]
}

variable "ui_subdomain" {
  description = "Subdomain for the chat UI (e.g., 'outcomeops' creates outcomeops.domain.com for prd, outcomeops-dev.domain.com for dev). Pair with append_environment_to_fqdn to control whether the environment is suffixed."
  type        = string
  default     = "outcomeops"
}

variable "append_environment_to_fqdn" {
  description = "When true, the chat-UI FQDN is {ui_subdomain}-{environment}.{domain} (dev/test style). When false, it's {ui_subdomain}.{domain} (production-style, first-class subdomain). prd.tfvars and openai.tfvars set this to false."
  type        = bool
  default     = true
}

variable "oidc_enabled" {
  description = "Enable OIDC authentication via Azure AD on the ALB"
  type        = bool
  default     = false
}

variable "oidc_client_id" {
  description = "Azure AD Application (client) ID for OIDC authentication"
  type        = string
  default     = ""
}

variable "oidc_tenant_id" {
  description = "Azure AD Directory (tenant) ID for OIDC authentication"
  type        = string
  default     = ""
}

# ============================================================================
# MCP Server Catalog
# ============================================================================

variable "mcp_sonarqube_enabled" {
  description = "Enable the native SonarQube Cloud tools in the OutcomeOps MCP server (sonarcloud.py). When true, the sonarqube__* tools are listed and call SonarCloud's REST API using mcp_sonarqube_url/org + the token at /{env}/{app}/mcp/sonarqube-token. (Formerly deployed a third-party Fargate task; replaced due to its I/O-reactor instability.)"
  type        = bool
  default     = false
}

variable "mcp_sonarqube_url" {
  description = "SonarQube server URL (e.g., https://sonarcloud.io or https://your-sonarqube.internal)"
  type        = string
  default     = ""
}

variable "mcp_sonarqube_org" {
  description = "SonarQube organization key from Organization Settings > Edit organization key (required for SonarCloud, not needed for self-hosted)"
  type        = string
  default     = ""
}

variable "mcp_snyk_enabled" {
  description = "Deploy Snyk MCP server as a Fargate Spot task"
  type        = bool
  default     = false
}

variable "mcp_snyk_org" {
  description = "Snyk organization ID (for multi-org accounts)"
  type        = string
  default     = ""
}

variable "mcp_server_enabled" {
  description = "Deploy the MCP server for external MCP client access (Claude Code, VS Code, Cursor)"
  type        = bool
  default     = false
}


# ============================================================================
# Audit & Compliance
# ============================================================================

variable "audit_alert_email" {
  description = "Email for audit alert notifications (empty = no alerts)"
  type        = string
  default     = ""
}

# ============================================================================
# Analytics Cost Alerts
# Daily scheduled Lambda reads the analytics rollups for the current month to
# date and publishes an SNS notification the first time a workspace or the
# organization crosses its budget. Set thresholds to 0 to disable.
# ============================================================================

variable "cost_alert_enabled" {
  description = "Enable the daily analytics cost-alert Lambda."
  type        = bool
  default     = false
}

variable "cost_alert_email" {
  description = "Email address subscribed to the cost-alert SNS topic. Empty = no subscription."
  type        = string
  default     = ""
}

variable "cost_alert_org_monthly_usd" {
  description = "Org-wide monthly spend threshold in USD. 0 disables the org-level alert."
  type        = number
  default     = 0
}

variable "cost_alert_workspace_monthly_usd" {
  description = "Per-workspace monthly spend threshold in USD. 0 disables per-workspace alerts."
  type        = number
  default     = 0
}

variable "cost_alert_schedule_expression" {
  description = "EventBridge schedule expression for the cost-alert Lambda."
  type        = string
  default     = "cron(0 13 * * ? *)" # 13:00 UTC daily (~6am PT / 9am ET)
}

# ============================================================================
# Security Scanner (AWS Security Agent)
# ============================================================================

variable "security_scanner_enabled" {
  type        = bool
  description = "Enable security scanner auth endpoint for AWS Security Agent"
  default     = false
}

variable "security_scanner_email" {
  type        = string
  description = "Email address for the security scanner service account"
  default     = "security-scanner@outcomeops.local"
}

# ============================================================================
# Encryption (Customer Managed Keys)
# ============================================================================

variable "enable_cmk_encryption" {
  type        = bool
  description = "Use a Customer Managed KMS Key (CMK) for data-at-rest encryption on S3, DynamoDB, SQS, SNS, and ECR. When false, services use AWS-managed encryption (AES256/SSE-SQS/aws/sns)."
  default     = false
}

# ============================================================================
# PII Redaction
# ============================================================================

variable "pii_redaction_enabled" {
  type        = bool
  description = <<-EOT
    Org-level kill switch for PII redaction at ingest time. When true
    (default), workspaces with privacy_mode=STRICT scrub PII via Amazon
    Comprehend before embedding documents. When false, every workspace
    behaves as privacy_mode=NONE regardless of its setting - useful for
    regions where Comprehend isn't available, or for customers who handle
    PII redaction in a separate pipeline.

    Comprehend availability check happens in the comprehend region-guard
    precondition. If you set this true in a region that doesn't support
    Comprehend Detect-PII, terraform plan fails fast.
  EOT
  default     = true
}

variable "comprehend_pii_price_per_1m_chars" {
  type        = number
  description = "USD per 1,000,000 characters scanned by Comprehend Detect-PII. Default matches the AWS public price ($1.00 / 1M chars). Override only if you have a negotiated rate."
  default     = 1.00
}

variable "pii_redaction_skip_types" {
  type        = list(string)
  description = <<-EOT
    Comprehend entity types that pass through unredacted even in STRICT
    mode. The default tunes the platform for a developer / engineering
    knowledge base, where URL / IP_ADDRESS / MAC_ADDRESS / DATE_TIME are
    load-bearing references in runbooks, infra docs, release notes, and
    cert-expiry trackers - not personally-identifying. Override with []
    to redact everything Comprehend detects (right call for HR-content
    or customer-support workspaces). Override with a custom list to
    tighten or relax for your content mix.
  EOT
  default     = ["URL", "IP_ADDRESS", "MAC_ADDRESS", "DATE_TIME"]
}

# ============================================================================
# Audit Stream Export (SIEM hook)
# ============================================================================

variable "audit_stream_export_enabled" {
  type        = bool
  description = <<-EOT
    Provision a Kinesis Data Stream and a publisher Lambda that re-emits
    every audit-log INSERT as an OCSF v1.3.0 envelope. Customers' security
    teams point their own consumer (Splunk, Datadog, Sumo, Security Lake,
    or a Firehose to S3) at the stream and decide which events they want
    via FilterCriteria on their consumer. Stream is on-demand mode, 24h
    retention. CMK flows from enable_cmk_encryption automatically.

    See docs/audit-stream-export.md for the OCSF schema reference and
    consumer FilterCriteria patterns.
  EOT
  default     = false
}

# ============================================================================
# Bedrock cost-attribution tag (ADR-016 v1.2 / v1.3)
# ============================================================================

variable "bedrock_workload_tag_name" {
  type        = string
  description = <<-EOT
    Tag key used to dimension Bedrock cost by internal workflow (chat /
    code-maps / code-gen / pr-analyzer). The tag VALUE is the workload
    slug; the tag NAME defers to your org's approved cost allocation
    tag vocabulary so this platform never asks a customer to modify
    their AWS tag strategy (which is typically locked down at the
    payer/org level, backed by Config rules and Service Control
    Policies, and painful to change).

    Default is "Purpose" because that's the maintainer's deploy
    convention (Application / Environment / Project / Purpose is the
    approved vocabulary here). Enterprises pick from their own vocab:
    common alternatives are "Purpose", "Component", "Function",
    "Workload", "Service", or "CostCenter" for org-specific chargeback
    dimensions.

    Whatever value is chosen, the tag MUST be activated in Billing >
    Cost Allocation Tags for the split to appear in Cost Explorer.

    See ADR-016 v1.2 (outcomeops-adrs) for the org-wide standard.
  EOT
  default     = "Purpose"

  validation {
    condition     = length(trimspace(var.bedrock_workload_tag_name)) > 0
    error_message = "bedrock_workload_tag_name must not be empty."
  }
}
