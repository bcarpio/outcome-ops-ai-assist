# ============================================================================
# Replicator-Applier Lambda (HA mode)
# ----------------------------------------------------------------------------
# Per-region small Lambda that subscribes to the local EventBridge
# replication bus and applies each cross-region event to local
# DDB / S3 / S3 Vectors. Gated on var.secondary_region != "".
#
# Two copies — one per region — each invoking ONLY against its local
# resources. The self-event short-circuit in handler.py (checks
# detail.source_region == THIS_REGION) ensures we never replay our own
# emissions.
#
# Failure path: EventBridge retries 5x over 1h; permanent failures land
# in the applier DLQ + the depth alarm fires for operator attention.
# ============================================================================

locals {
  # Shared list of every DDB table the applier needs PutItem/UpdateItem/
  # DeleteItem on, in both regions. The primary applier needs the primary
  # ARNs; the secondary applier needs the secondary ARNs.
  applier_dynamodb_arns_primary = compact([
    var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_arn : "",
    module.audit_logs_table.dynamodb_table_arn,
    module.chat_table.dynamodb_table_arn,
    module.analytics_table.dynamodb_table_arn,
    module.code_maps_table.dynamodb_table_arn,
    module.symbol_graph_table.dynamodb_table_arn,
    module.code_map_pending_work_table.dynamodb_table_arn,
    module.code_map_failed_files_table.dynamodb_table_arn,
  ])

  applier_dynamodb_arns_secondary = var.secondary_region != "" ? compact([
    var.enable_workspaces ? module.workspaces_table_secondary[0].dynamodb_table_arn : "",
    module.audit_logs_table_secondary[0].dynamodb_table_arn,
    module.chat_table_secondary[0].dynamodb_table_arn,
    module.analytics_table_secondary[0].dynamodb_table_arn,
    module.code_maps_table_secondary[0].dynamodb_table_arn,
    module.symbol_graph_table_secondary[0].dynamodb_table_arn,
    module.code_map_pending_work_table_secondary[0].dynamodb_table_arn,
    module.code_map_failed_files_table_secondary[0].dynamodb_table_arn,
  ]) : []
}

# ----------------------------------------------------------------------------
# Primary-region applier — replays events that came FROM secondary.
# ----------------------------------------------------------------------------

module "replication_applier" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-replication-applier"
  # Primary: keep the default role name (matches function_name) so the
  # already-deployed IAM role isn't renamed. Only the SECONDARY applier
  # needs a -secondary suffix to avoid collision (IAM is global).
  description = "Applies cross-region replication events to local DDB / S3 / S3 Vectors"
  handler     = "handler.lambda_handler"
  runtime     = "python3.12"
  timeout     = 60
  memory_size = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/replication-applier"
      pip_requirements = true
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true

  environment_variables = {
    ENV                 = var.environment
    APP_NAME            = var.app_name
    THIS_REGION         = var.aws_region
    LOCAL_KB_BUCKET     = module.knowledge_base_bucket.s3_bucket_id
    LOCAL_VECTOR_BUCKET = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name
    LOCAL_VECTOR_INDEX  = aws_s3vectors_index.knowledge_base.index_name
  }

  attach_policy_statements = true
  # IMPORTANT: `policy_statements` map entries must each have non-null
  # actions. Use merge() to conditionally INCLUDE kms_use rather than
  # mapping its key to null — the lambda module passes the map straight
  # into the inline policy, and a null entry makes IAM reject the policy
  # with "Policy statement must contain actions" (MalformedPolicyDocument).
  policy_statements = merge({
    dynamodb_write = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      resources = local.applier_dynamodb_arns_primary
    }
    s3_cross_region_read = {
      effect  = "Allow"
      actions = ["s3:GetObject"]
      # The applier in primary reads from the SECONDARY's bucket
      # (which has the -secondary suffix per chunk 02e).
      resources = var.secondary_region != "" ? [
        "${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*",
      ] : []
    }
    s3_local_write = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${module.knowledge_base_bucket.s3_bucket_arn}/*"]
    }
    s3vectors_write = {
      effect    = "Allow"
      actions   = ["s3vectors:PutVectors", "s3vectors:DeleteVectors"]
      resources = [aws_s3vectors_index.knowledge_base.index_arn]
    }
    },
    var.enable_cmk_encryption ? {
      kms_use = {
        effect    = "Allow"
        actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
        resources = [local.data_cmk_arn]
      }
    } : {},
  )

  tags = {
    Purpose = "ha-replication"
  }
}

# EventBridge rule on the local replication bus — every event reaches
# the local applier (the handler short-circuits self-region events).
resource "aws_cloudwatch_event_rule" "applier_local" {
  count = var.secondary_region != "" ? 1 : 0

  name           = "replication-applier-local"
  event_bus_name = aws_cloudwatch_event_bus.replication.name
  description    = "Route incoming HA replication events to the local applier Lambda"

  event_pattern = jsonencode({
    source = ["${var.environment}.${var.app_name}.ha-replication"]
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_target" "applier_local" {
  count = var.secondary_region != "" ? 1 : 0

  rule           = aws_cloudwatch_event_rule.applier_local[0].name
  event_bus_name = aws_cloudwatch_event_bus.replication.name
  target_id      = "replication-applier-local"

  arn = module.replication_applier[0].lambda_function_arn

  dead_letter_config {
    arn = aws_sqs_queue.applier_dlq[0].arn
  }

  retry_policy {
    maximum_retry_attempts       = 5
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "applier_eventbridge_invoke" {
  count = var.secondary_region != "" ? 1 : 0

  statement_id  = "AllowEventBridgeInvokeApplier"
  action        = "lambda:InvokeFunction"
  function_name = module.replication_applier[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.applier_local[0].arn
}

resource "aws_sqs_queue" "applier_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-replication-applier-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_sqs_queue_policy" "applier_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  queue_url = aws_sqs_queue.applier_dlq[0].url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.applier_dlq[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.applier_local[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "applier_dlq_depth" {
  count = var.secondary_region != "" ? 1 : 0

  alarm_name          = "${var.environment}-${var.app_name}-replication-applier-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Replication applier DLQ has messages — replication apply path failing. See docs/HA.md."
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.applier_dlq[0].name
  }

  tags = {
    Purpose = "ha-replication"
  }
}

# ----------------------------------------------------------------------------
# Secondary-region applier — replays events that came FROM primary.
# ----------------------------------------------------------------------------

module "replication_applier_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-replication-applier"
  role_name     = "${var.environment}-${var.app_name}-replication-applier-secondary"
  description   = "Applies cross-region replication events to local DDB / S3 / S3 Vectors (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/replication-applier"
      pip_requirements = true
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = {
    ENV                 = var.environment
    APP_NAME            = var.app_name
    THIS_REGION         = var.secondary_region
    LOCAL_KB_BUCKET     = module.knowledge_base_bucket_secondary.s3_bucket_id
    LOCAL_VECTOR_BUCKET = aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name
    LOCAL_VECTOR_INDEX  = aws_s3vectors_index.knowledge_base_secondary[0].index_name
  }

  attach_policy_statements = true
  # See note on primary above re merge() vs ternary-to-null.
  policy_statements = merge({
    dynamodb_write = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      resources = local.applier_dynamodb_arns_secondary
    }
    s3_cross_region_read = {
      effect  = "Allow"
      actions = ["s3:GetObject"]
      # The applier in secondary reads from the PRIMARY's bucket.
      resources = ["${module.knowledge_base_bucket.s3_bucket_arn}/*"]
    }
    s3_local_write = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*"]
    }
    s3vectors_write = {
      effect    = "Allow"
      actions   = ["s3vectors:PutVectors", "s3vectors:DeleteVectors"]
      resources = [aws_s3vectors_index.knowledge_base_secondary[0].index_arn]
    }
    },
    var.enable_cmk_encryption ? {
      kms_use = {
        effect    = "Allow"
        actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
        resources = [local.data_cmk_arn_secondary]
      }
    } : {},
  )

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_rule" "applier_local_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name           = "replication-applier-local"
  event_bus_name = aws_cloudwatch_event_bus.replication_secondary[0].name
  description    = "Route incoming HA replication events to the local applier Lambda"

  event_pattern = jsonencode({
    source = ["${var.environment}.${var.app_name}.ha-replication"]
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_target" "applier_local_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  rule           = aws_cloudwatch_event_rule.applier_local_secondary[0].name
  event_bus_name = aws_cloudwatch_event_bus.replication_secondary[0].name
  target_id      = "replication-applier-local"

  arn = module.replication_applier_secondary[0].lambda_function_arn

  dead_letter_config {
    arn = aws_sqs_queue.applier_dlq_secondary[0].arn
  }

  retry_policy {
    maximum_retry_attempts       = 5
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "applier_eventbridge_invoke_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvokeApplier"
  action        = "lambda:InvokeFunction"
  function_name = module.replication_applier_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.applier_local_secondary[0].arn
}

resource "aws_sqs_queue" "applier_dlq_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-replication-applier-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_sqs_queue_policy" "applier_dlq_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  queue_url = aws_sqs_queue.applier_dlq_secondary[0].url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.applier_dlq_secondary[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.applier_local_secondary[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "applier_dlq_depth_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  alarm_name          = "${var.environment}-${var.app_name}-replication-applier-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Replication applier DLQ has messages — replication apply path failing. See docs/HA.md."
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.applier_dlq_secondary[0].name
  }

  tags = {
    Purpose = "ha-replication"
  }
}

# Outputs
output "replication_applier_arn" {
  description = "ARN of the primary-region replication-applier Lambda (null when HA disabled)"
  value       = var.secondary_region != "" ? module.replication_applier[0].lambda_function_arn : null
}

output "replication_applier_arn_secondary" {
  description = "ARN of the secondary-region replication-applier Lambda (null when HA disabled)"
  value       = var.secondary_region != "" ? module.replication_applier_secondary[0].lambda_function_arn : null
}

output "replication_applier_dlq_arn" {
  description = "ARN of the primary-region applier DLQ (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_sqs_queue.applier_dlq[0].arn : null
}

output "replication_applier_dlq_arn_secondary" {
  description = "ARN of the secondary-region applier DLQ (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_sqs_queue.applier_dlq_secondary[0].arn : null
}
