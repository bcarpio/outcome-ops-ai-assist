# ============================================================================
# SQS Queue for Code Maps Batch Processing
# ============================================================================

# Dead Letter Queue for failed batch processing
module "code_maps_dlq" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-code-maps-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true

  tags = {
    Purpose = "code-maps-dlq"
  }
}

# Main FIFO queue for code maps batch processing
module "code_maps_queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-code-maps-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # We provide explicit MessageDeduplicationId
  visibility_timeout_seconds  = 900   # Match Lambda timeout

  # Dead letter queue configuration
  redrive_policy = {
    deadLetterTargetArn = module.code_maps_dlq.queue_arn
    maxReceiveCount     = 3 # Retry failed messages 3 times
  }

  tags = {
    Purpose = "code-maps-queue"
  }
}

# Store SQS queue URL in SSM for Lambda to retrieve
resource "aws_ssm_parameter" "code_maps_queue_url" {
  name  = "/${var.environment}/${var.app_name}/sqs/code-maps-queue-url"
  type  = "String"
  value = module.code_maps_queue.queue_url

  description = "URL of the code maps batch processing queue"

  tags = {
    Purpose = "code-maps-queue-url"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "code_maps_queue_url" {
  description = "URL of the code maps batch processing queue"
  value       = module.code_maps_queue.queue_url
}

output "code_maps_queue_arn" {
  description = "ARN of the code maps batch processing queue"
  value       = module.code_maps_queue.queue_arn
}

output "code_maps_dlq_url" {
  description = "URL of the code maps dead letter queue"
  value       = module.code_maps_dlq.queue_url
}

output "code_maps_dlq_arn" {
  description = "ARN of the code maps dead letter queue"
  value       = module.code_maps_dlq.queue_arn
}
