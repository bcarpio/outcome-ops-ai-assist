# ============================================================================
# Secondary-region sync worker Lambdas (HA chunk 9g)
# ----------------------------------------------------------------------------
# 9 SQS-triggered sync workers under aws.secondary, consuming from the
# secondary's integration-sync queues (chunk 02g).
#
# Idle until secondary becomes active and schedulers in secondary
# (chunk 9f) fire and enqueue work. Data they produce (workspaces
# updates, audit logs, S3 KB uploads) replicates back to primary via
# dual_write (chunks 04/06a).
#
# All gated on var.enable_workspaces && var.secondary_region != "".
#
# OAuth-token decryption gotcha: tokens encrypted in primary's
# oauth_tokens KMS key are NOT decryptable in secondary. After
# failover, users re-establish OAuth integrations. Chunk 13
# (multi-region KMS) fixes this.
# ============================================================================

# ----------------------------------------------------------------------------
# github-sync (secondary)
# ----------------------------------------------------------------------------

module "github_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-github-sync"
  role_name     = "${var.environment}-${var.app_name}-github-sync-secondary"
  description   = "GitHub repo sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/github-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      REPO_SUMMARIES_QUEUE = module.repo_summaries_queue_secondary.queue_url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      KB_BUCKET            = module.knowledge_base_bucket_secondary.s3_bucket_id
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
      PENDING_WORK_TABLE   = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE   = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      sha_truth = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        resources = local.sha_truth_table_arns_secondary
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.github_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          module.repo_summaries_queue_secondary.queue_arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
        ]
      }
      s3_kb = {
        effect    = "Allow"
        actions   = ["s3:PutObject"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*"]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "github-sync" }
}

resource "aws_lambda_event_source_mapping" "github_sync_secondary" {
  count    = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  event_source_arn = aws_sqs_queue.github_sync_secondary[0].arn
  function_name    = module.github_sync_lambda_secondary[0].lambda_function_name

  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# OAuth-flavor sync workers (Confluence, Jira, Outlook, Teams, SharePoint,
# OneDrive, OneNote). All share: oauth_tokens_secondary KMS + file_ingestion +
# delete_queue. SSM-read scope per OAuth provider (Atlassian or Microsoft).
# ----------------------------------------------------------------------------

# Confluence sync (secondary)
module "confluence_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-confluence-sync"
  role_name     = "${var.environment}-${var.app_name}-confluence-sync-secondary"
  description   = "Confluence sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/confluence-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/atlassian/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.confluence_sync_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.file_ingestion_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "confluence-sync" }
}

resource "aws_lambda_event_source_mapping" "confluence_sync_secondary" {
  count            = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.confluence_sync_secondary[0].arn
  function_name    = module.confluence_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# Jira sync (secondary)
module "jira_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-jira-sync"
  role_name     = "${var.environment}-${var.app_name}-jira-sync-secondary"
  description   = "Jira sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/jira-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/atlassian/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.jira_sync_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.file_ingestion_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "jira-sync" }
}

resource "aws_lambda_event_source_mapping" "jira_sync_secondary" {
  count            = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.jira_sync_secondary[0].arn
  function_name    = module.jira_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# Microsoft-flavor sync workers (Outlook, Teams, SharePoint, OneDrive, OneNote)
# Same shape as Atlassian-flavor but SSM scope is /oauth/microsoft/* and
# the queue/env-var names differ per integration.
# ----------------------------------------------------------------------------

module "outlook_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-outlook-sync"
  role_name     = "${var.environment}-${var.app_name}-outlook-sync-secondary"
  description   = "Outlook sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/outlook-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env. Without
      # this, the handler's continuation branch logs "not configured for
      # continuation" and stops, silently truncating large mailbox syncs
      # in the secondary region post-failover.
      OUTLOOK_SYNC_QUEUE = aws_sqs_queue.outlook_sync_secondary[0].url
      AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.outlook_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant continuation SendMessage on OWN queue
        # so paginated syncs self-requeue after failover.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.outlook_sync_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "outlook-sync" }
}

resource "aws_lambda_event_source_mapping" "outlook_sync_secondary" {
  count            = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.outlook_sync_secondary[0].arn
  function_name    = module.outlook_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# Teams sync
module "teams_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-teams-sync"
  role_name     = "${var.environment}-${var.app_name}-teams-sync-secondary"
  description   = "Teams sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/teams-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env. Without
      # this, the handler's continuation branch logs "not configured for
      # continuation" and stops -- large syncs silently truncate in the
      # secondary region post-failover.
      TEAMS_SYNC_QUEUE = aws_sqs_queue.teams_sync_secondary[0].url
      AUDIT_TABLE      = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.teams_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant continuation SendMessage on OWN queue
        # so paginated syncs self-requeue after failover.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.teams_sync_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "teams-sync" }
}

resource "aws_lambda_event_source_mapping" "teams_sync_secondary" {
  count            = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.teams_sync_secondary[0].arn
  function_name    = module.teams_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# SharePoint sync
module "sharepoint_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-sharepoint-sync"
  role_name     = "${var.environment}-${var.app_name}-sharepoint-sync-secondary"
  description   = "SharePoint sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/sharepoint-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env. Without
      # this, the handler's continuation branch logs "not configured for
      # continuation" and stops -- large syncs silently truncate in the
      # secondary region post-failover.
      SHAREPOINT_SYNC_QUEUE = aws_sqs_queue.sharepoint_sync_secondary[0].url
      AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.sharepoint_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant continuation SendMessage on OWN queue
        # so paginated syncs self-requeue after failover.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.sharepoint_sync_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "sharepoint-sync" }
}

resource "aws_lambda_event_source_mapping" "sharepoint_sync_secondary" {
  count            = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.sharepoint_sync_secondary[0].arn
  function_name    = module.sharepoint_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# OneDrive sync
module "onedrive_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-onedrive-sync"
  role_name     = "${var.environment}-${var.app_name}-onedrive-sync-secondary"
  description   = "OneDrive sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/onedrive-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env. Without
      # this, the handler's continuation branch logs "not configured for
      # continuation" and stops -- large syncs silently truncate in the
      # secondary region post-failover.
      ONEDRIVE_SYNC_QUEUE = aws_sqs_queue.onedrive_sync_secondary[0].url
      AUDIT_TABLE         = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.onedrive_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant continuation SendMessage on OWN queue
        # so paginated syncs self-requeue after failover.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.onedrive_sync_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "onedrive-sync" }
}

resource "aws_lambda_event_source_mapping" "onedrive_sync_secondary" {
  count            = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.onedrive_sync_secondary[0].arn
  function_name    = module.onedrive_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# OneNote sync
module "onenote_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-onenote-sync"
  role_name     = "${var.environment}-${var.app_name}-onenote-sync-secondary"
  description   = "OneNote sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/onenote-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env. Without
      # this, the handler's continuation branch logs "not configured for
      # continuation" and stops -- large syncs silently truncate in the
      # secondary region post-failover.
      ONENOTE_SYNC_QUEUE = aws_sqs_queue.onenote_sync_secondary[0].url
      AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.onenote_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant continuation SendMessage on OWN queue
        # so paginated syncs self-requeue after failover.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.onenote_sync_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "onenote-sync" }
}

resource "aws_lambda_event_source_mapping" "onenote_sync_secondary" {
  count            = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.onenote_sync_secondary[0].arn
  function_name    = module.onenote_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# Database sync (secondary). Uses db_credentials_secondary KMS (not oauth).
# Container Lambda — engine drivers exceed the 250 MB layer cap, same
# reasoning as the primary deployment.
# ----------------------------------------------------------------------------

data "aws_ssm_parameter" "database_sync_image_tag_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary
  name     = "/${var.environment}/${var.app_name}/database-sync/image-tag"
}

module "database_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-database-sync"
  role_name     = "${var.environment}-${var.app_name}-database-sync-secondary"
  description   = "Database sync (secondary region)"
  timeout       = 900
  # chunk 08 (HA parity): bumped 1024 -> 2048 to match primary. Introspection
  # against a large schema was OOMing here post-failover.
  memory_size = 2048
  publish     = true

  create_package = false
  package_type   = "Image"
  image_uri      = "${aws_ecr_repository.database_sync_secondary[0].repository_url}:${data.aws_ssm_parameter.database_sync_image_tag_secondary[0].value}"
  architectures  = ["x86_64"]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn

  environment_variables = merge(
    {
      ENV                       = var.environment
      APP_NAME                  = var.app_name
      WORKSPACES_TABLE          = module.workspaces_table_secondary[0].dynamodb_table_id
      DB_CREDENTIAL_KMS_KEY_ARN = aws_kms_replica_key.db_credentials_secondary[0].arn
      FILE_INGESTION_QUEUE      = aws_sqs_queue.file_ingestion_secondary[0].url
      # chunk 08 (HA parity): mirror primary continuation env
      # (self-requeue after per-page ingestion) + schema-drift SNS topic.
      # Without these two, database syncs silently truncate in the
      # secondary region and drift alerts stop firing post-failover.
      DATABASE_SYNC_QUEUE      = aws_sqs_queue.database_sync_secondary[0].url
      DELETE_QUEUE             = aws_sqs_queue.delete_queue_secondary[0].url
      DATABASE_DRIFT_TOPIC_ARN = length(aws_sns_topic.database_drift_secondary) > 0 ? aws_sns_topic.database_drift_secondary[0].arn : ""
      AUDIT_TABLE              = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      kms_credentials = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.db_credentials_secondary[0].arn]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.database_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 08 (HA parity): grant SendMessage on OWN queue for
        # continuation self-requeue.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.database_sync_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
      # chunk 08 (HA parity): publish-grant on the secondary drift
      # topic. Same nonexistent-arn fallback pattern as primary so the
      # policy stays well-formed when no subscribers are configured.
      publish_drift = {
        effect    = "Allow"
        actions   = ["sns:Publish"]
        resources = length(aws_sns_topic.database_drift_secondary) > 0 ? [aws_sns_topic.database_drift_secondary[0].arn] : ["arn:aws:sns:*:*:nonexistent"]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "database-sync" }
}

resource "aws_lambda_event_source_mapping" "database_sync_secondary" {
  count            = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.database_sync_secondary[0].arn
  function_name    = module.database_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Chunk 02: partial-batch-failure. See lambda-github.tf primary
  # ESM for rationale (handler returns batchItemFailures for failures).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# Azure DevOps sync (secondary) -- chunk 09 finalization
#
# Mirrors module.sharepoint_sync_lambda_secondary. The primary's azure-
# devops-sync worker was added in commit d6e9574 (chunk 05); this is the
# secondary-region copy that completes chunk 09.
# ----------------------------------------------------------------------------

module "azure_devops_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-azure-devops-sync"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-sync-secondary"
  description   = "Azure DevOps sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/azure-devops-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

      # chunk 05a: ADO sync writes blobs directly to the KB bucket
      # (process-documents is S3-triggered + source-agnostic). Skips
      # the file-ingestion / repo-summaries hop entirely so we don't
      # contaminate the github-shaped consumers' DLQs.
      KB_BUCKET               = module.knowledge_base_bucket_secondary.s3_bucket_id
      AZURE_DEVOPS_SYNC_QUEUE = aws_sqs_queue.azure_devops_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary chunk-08a. Without this env
      # var + the sqs_send_repo_summaries grant below, ADO syncs in the
      # secondary region never enqueue repo-summary jobs post-failover,
      # so code-map + symbol-graph regen silently stops for ADO repos.
      REPO_SUMMARIES_QUEUE = module.repo_summaries_queue_secondary.queue_url
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_secret = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.azure_devops_sync_secondary[0].arn]
      }
      # chunk 05a: write blobs directly to the secondary-region KB
      # bucket. Chunk 08 (HA parity): widened to bucket-root (${bucket}/*)
      # to match the primary. The old workspaces/* prefix scoping was the
      # exact anti-pattern CLAUDE.md warns about -- generic-feeling but
      # silently divergent from primary, invisible until failover.
      s3_kb_write = {
        effect    = "Allow"
        actions   = ["s3:PutObject", "s3:PutObjectAcl"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*"]
      }
      # chunk 08 (HA parity, chunk-08a mirror): grant SendMessage on the
      # secondary repo-summaries queue so post-failover ADO syncs fan
      # into the same code-map + symbol-graph pipeline.
      sqs_send_repo_summaries = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [module.repo_summaries_queue_secondary.queue_arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "azure-devops-sync" }
}

resource "aws_lambda_event_source_mapping" "azure_devops_sync_secondary" {
  count                   = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0
  provider                = aws.secondary
  event_source_arn        = aws_sqs_queue.azure_devops_sync_secondary[0].arn
  function_name           = module.azure_devops_sync_lambda_secondary[0].lambda_function_name
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# GitLab sync (secondary) -- ADR-050 chunk 08
# ----------------------------------------------------------------------------

module "gitlab_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-gitlab-sync"
  role_name     = "${var.environment}-${var.app_name}-gitlab-sync-secondary"
  description   = "GitLab sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/gitlab-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      GITLAB_BASE_URL                = var.gitlab_base_url
      GITLAB_CLIENT_ID               = var.gitlab_client_id
      GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path

      # Mirror of the primary's direct-to-S3 + repo-summaries fan-out.
      # Without REPO_SUMMARIES_QUEUE + its grant, post-failover GitLab
      # syncs never enqueue code-map / symbol-graph regen (the exact
      # silent-divergence failure mode the parity rule exists for).
      KB_BUCKET            = module.knowledge_base_bucket_secondary.s3_bucket_id
      GITLAB_SYNC_QUEUE    = aws_sqs_queue.gitlab_sync_secondary[0].url
      REPO_SUMMARIES_QUEUE = module.repo_summaries_queue_secondary.queue_url
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_secret = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${local.gitlab_secret_ssm_path}"]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable after failover routes an
      # SQS message to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.gitlab_sync_secondary[0].arn]
      }
      # Bucket-root scope to match the primary exactly (the ADO chunk-08
      # live-burn: a prefix-scoped secondary grant diverged silently).
      s3_kb_write = {
        effect    = "Allow"
        actions   = ["s3:PutObject", "s3:PutObjectAcl"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*"]
      }
      sqs_send_repo_summaries = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [module.repo_summaries_queue_secondary.queue_arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "gitlab-sync" }
}

resource "aws_lambda_event_source_mapping" "gitlab_sync_secondary" {
  count                              = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0
  provider                           = aws.secondary
  event_source_arn                   = aws_sqs_queue.gitlab_sync_secondary[0].arn
  function_name                      = module.gitlab_sync_lambda_secondary[0].lambda_function_name
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# ServiceNow sync (secondary) -- chunk 07 (HA parity, ADR-044)
# ----------------------------------------------------------------------------

module "servicenow_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-servicenow-sync"
  role_name     = "${var.environment}-${var.app_name}-servicenow-sync-secondary"
  description   = "ServiceNow KB sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/servicenow-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                   = var.environment
      APP_NAME              = var.app_name
      WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN     = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE  = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE          = aws_sqs_queue.delete_queue_secondary[0].url
      SERVICENOW_SYNC_QUEUE = aws_sqs_queue.servicenow_sync_secondary[0].url
      KB_BUCKET             = module.knowledge_base_bucket_secondary.s3_bucket_id
      AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.servicenow_common_env,
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      # Same two-region CMK grant as the integration secondary: tokens
      # minted in primary must be decryptable here after failover.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      # ServiceNow client_secret SSM param (replicated to secondary via
      # scripts/ha-sync-secrets).
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/servicenow/*"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.servicenow_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # Includes the sync queue itself for continuation self-requeue.
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.servicenow_sync_secondary[0].arn,
        ]
      }
      # Orphan cleanup lists S3 keys under the ServiceNow prefix.
      s3_kb_list = {
        effect    = "Allow"
        actions   = ["s3:ListBucket"]
        resources = [module.knowledge_base_bucket_secondary.s3_bucket_arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "servicenow-sync" }
}

resource "aws_lambda_event_source_mapping" "servicenow_sync_secondary" {
  count            = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.servicenow_sync_secondary[0].arn
  function_name    = module.servicenow_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  # Partial-batch-failure per the chunk 02 remediation contract.
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# salesforce_sync -- HA parity
# ----------------------------------------------------------------------------

module "salesforce_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-salesforce-sync"
  role_name     = "${var.environment}-${var.app_name}-salesforce-sync-secondary"
  description   = "Salesforce CRM sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/salesforce-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                   = var.environment
      APP_NAME              = var.app_name
      WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN     = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      FILE_INGESTION_QUEUE  = aws_sqs_queue.file_ingestion_secondary[0].url
      DELETE_QUEUE          = aws_sqs_queue.delete_queue_secondary[0].url
      SALESFORCE_SYNC_QUEUE = aws_sqs_queue.salesforce_sync_secondary[0].url
      KB_BUCKET             = module.knowledge_base_bucket_secondary.s3_bucket_id
      AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.salesforce_common_env,
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/salesforce/*"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.salesforce_sync_secondary[0].arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.file_ingestion_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.salesforce_sync_secondary[0].arn,
        ]
      }
      s3_kb_list = {
        effect    = "Allow"
        actions   = ["s3:ListBucket"]
        resources = [module.knowledge_base_bucket_secondary.s3_bucket_arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "salesforce-sync" }
}

resource "aws_lambda_event_source_mapping" "salesforce_sync_secondary" {
  count            = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0
  provider         = aws.secondary
  event_source_arn = aws_sqs_queue.salesforce_sync_secondary[0].arn
  function_name    = module.salesforce_sync_lambda_secondary[0].lambda_function_name
  batch_size       = 1

  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}
