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
# SQS Queue for PR Checks Processing
# ============================================================================

# Dead Letter Queue for failed PR check jobs
module "pr_checks_dlq" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-pr-checks-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true

  tags = {
    Purpose = "pr-checks-dlq"
  }
}

# Main FIFO queue for PR checks processing
module "pr_checks_queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-pr-checks-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # We provide explicit MessageDeduplicationId
  visibility_timeout_seconds  = 720   # 12 minutes (Lambda timeout is 10 minutes)

  # Dead letter queue configuration
  redrive_policy = {
    deadLetterTargetArn = module.pr_checks_dlq.queue_arn
    maxReceiveCount     = 3 # Retry failed checks 3 times
  }

  tags = {
    Purpose = "pr-checks-queue"
  }
}

# Store SQS queue URL in SSM for Lambda to retrieve
resource "aws_ssm_parameter" "pr_checks_queue_url" {
  name  = "/${var.environment}/${var.app_name}/sqs/pr-checks-queue-url"
  type  = "String"
  value = module.pr_checks_queue.queue_url

  description = "URL of the PR checks processing queue"

  tags = {
    Purpose = "pr-checks-queue-url"
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

output "pr_checks_queue_url" {
  description = "URL of the PR checks processing queue"
  value       = module.pr_checks_queue.queue_url
}

output "pr_checks_queue_arn" {
  description = "ARN of the PR checks processing queue"
  value       = module.pr_checks_queue.queue_arn
}

output "pr_checks_dlq_url" {
  description = "URL of the PR checks dead letter queue"
  value       = module.pr_checks_dlq.queue_url
}

output "pr_checks_dlq_arn" {
  description = "ARN of the PR checks dead letter queue"
  value       = module.pr_checks_dlq.queue_arn
}

# ============================================================================
# SQS Queue for Code Generation Steps
# ============================================================================

# Dead Letter Queue for failed code generation steps
module "code_generation_dlq" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-code-generation-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true

  tags = {
    Purpose = "code-generation-dlq"
  }
}

# Main FIFO queue for code generation step execution
module "code_generation_queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.3.0"

  name                        = "${var.environment}-${var.app_name}-code-generation-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # We provide explicit MessageDeduplicationId
  visibility_timeout_seconds  = 900   # Match Lambda timeout (15 minutes)

  # Dead letter queue configuration
  redrive_policy = {
    deadLetterTargetArn = module.code_generation_dlq.queue_arn
    maxReceiveCount     = 2 # Retry failed steps 2 times
  }

  tags = {
    Purpose = "code-generation-queue"
  }
}

# Store SQS queue URL in SSM for Lambda to retrieve
resource "aws_ssm_parameter" "code_generation_queue_url" {
  name  = "/${var.environment}/${var.app_name}/sqs/code-generation-queue-url"
  type  = "String"
  value = module.code_generation_queue.queue_url

  description = "URL of the code generation step execution queue"

  tags = {
    Purpose = "code-generation-queue-url"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "code_generation_queue_url" {
  description = "URL of the code generation step execution queue"
  value       = module.code_generation_queue.queue_url
}

output "code_generation_queue_arn" {
  description = "ARN of the code generation step execution queue"
  value       = module.code_generation_queue.queue_arn
}

output "code_generation_dlq_url" {
  description = "URL of the code generation dead letter queue"
  value       = module.code_generation_dlq.queue_url
}

output "code_generation_dlq_arn" {
  description = "ARN of the code generation dead letter queue"
  value       = module.code_generation_dlq.queue_arn
}
