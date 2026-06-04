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
# azure_devops_integration_enabled is true.
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

variable "azure_devops_integration_enabled" {
  type        = bool
  description = "Master switch. When false, the azure-devops Lambdas are still deployed but the UI tab and OAuth endpoints return 404. Customers without ADO leave this false."
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
  description = "Enable Jira Automation integration via SSM Document"
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

# ============================================================================
# Bedrock Model Configuration
# ============================================================================

variable "bedrock_advanced_model_id" {
  type        = string
  description = "Bedrock model ID for advanced/primary inference (e.g., Claude Sonnet)"
  default     = "us.anthropic.claude-sonnet-4-6"
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
  description = "Make the ALB internal (VPC-only access). Set to false for public ALB secured by OIDC."
  type        = bool
  default     = false
}

variable "ui_fargate_assign_public_ip" {
  description = "Assign public IP to Fargate tasks. Set to true for public subnets without NAT. Set to false for private subnets with outbound via NAT, Transit Gateway, or Direct Connect."
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
  description = "Deploy SonarQube MCP server as a Fargate Spot task"
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
