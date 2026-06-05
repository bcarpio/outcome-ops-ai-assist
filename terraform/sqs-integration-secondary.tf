# ============================================================================
# Secondary-region SQS queues for integration syncs (HA mode only).
#
# The 11 queue pairs below mirror the per-integration sync queues
# declared in lambda-*.tf (one pair per integration: github, confluence,
# onedrive, onenote, jira, teams, outlook, sharepoint, database) plus
# the file-ingestion + delete pairs from lambda-file-ingestion.tf.
#
# These secondary queues sit empty during normal active/passive
# operation — integration Lambdas only run in the primary region today.
# Their value is failover: when the operator flips AppConfig and the
# secondary becomes active, the integration Lambdas (deployed to
# secondary in a later chunk) drain these queues, NOT the primary's.
#
# All secondary queues use:
#   - identical names to primary (operator commands like
#     `aws sqs get-queue-url --queue-name ${env}-${app}-github-sync
#     --region us-east-1` work the same way as in primary)
#   - local.data_cmk_arn_secondary for KMS
#   - aws.secondary provider
#   - count gated on enable_workspaces && secondary_region != ""
# ============================================================================

# ----------------------------------------------------------------------------
# github_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "github_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-github-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.github_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "github-sync-queue"
  }
}

resource "aws_sqs_queue" "github_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-github-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "github-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# confluence_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "confluence_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-confluence-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.confluence_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "confluence-sync-queue"
  }
}

resource "aws_sqs_queue" "confluence_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-confluence-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "confluence-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# onedrive_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "onedrive_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-onedrive-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.onedrive_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "onedrive-sync-queue"
  }
}

resource "aws_sqs_queue" "onedrive_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-onedrive-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "onedrive-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# onenote_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "onenote_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-onenote-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.onenote_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "onenote-sync-queue"
  }
}

resource "aws_sqs_queue" "onenote_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-onenote-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "onenote-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# jira_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "jira_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-jira-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jira_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "jira-sync-queue"
  }
}

resource "aws_sqs_queue" "jira_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-jira-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "jira-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# teams_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "teams_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-teams-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.teams_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "teams-sync-queue"
  }
}

resource "aws_sqs_queue" "teams_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-teams-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "teams-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# outlook_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "outlook_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-outlook-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.outlook_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "outlook-sync-queue"
  }
}

resource "aws_sqs_queue" "outlook_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-outlook-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "outlook-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# sharepoint_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "sharepoint_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-sharepoint-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.sharepoint_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "sharepoint-sync-queue"
  }
}

resource "aws_sqs_queue" "sharepoint_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-sharepoint-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "sharepoint-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# database_sync (standard)
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "database_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-database-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.database_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "database-sync-queue"
  }
}

resource "aws_sqs_queue" "database_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-database-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "database-sync-dlq"
  }
}

# ----------------------------------------------------------------------------
# file_ingestion (FIFO) — central ingest pipeline
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "file_ingestion_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                        = "${var.environment}-${var.app_name}-file-ingestion.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  visibility_timeout_seconds = 300
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.file_ingestion_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "file-ingestion-queue"
  }
}

resource "aws_sqs_queue" "file_ingestion_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-file-ingestion-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "file-ingestion-dlq"
  }
}

# ----------------------------------------------------------------------------
# delete_queue (standard) — workspace/document delete operations
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "delete_queue_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-delete"
  visibility_timeout_seconds = 300
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delete_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "delete-queue"
  }
}

resource "aws_sqs_queue" "delete_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-delete-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "delete-dlq"
  }
}

# ----------------------------------------------------------------------------
# azure_devops_sync (standard) -- chunk 09
# ----------------------------------------------------------------------------

resource "aws_sqs_queue" "azure_devops_sync_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                       = "${var.environment}-${var.app_name}-azure-devops-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.azure_devops_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "azure-devops-sync-queue"
  }
}

resource "aws_sqs_queue" "azure_devops_sync_dlq_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-azure-devops-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "azure-devops-sync-dlq"
  }
}
