# ADR-001: Terraform Infrastructure Patterns and Module Standards

## Status: Accepted

## Context

MyFantasy.ai infrastructure spans multiple AWS services (Lambda, DynamoDB, S3, SQS, API Gateway, CloudFront, etc.). As the codebase grows, maintaining consistency in how infrastructure is defined becomes critical for:
- Security (least privilege access controls)
- Maintainability (consistent patterns across modules)
- Auditability (clear resource ownership and dependencies)
- Scalability (reusable components that compose cleanly)

## Decision

### 1. Use Terraform Community Modules (terraform-aws-modules)

All new AWS resources MUST use community modules from `terraform-aws-modules` when available. Community modules are:
- Battle-tested and maintained by the Terraform community
- Pre-configured with security best practices
- Provide consistent interfaces across AWS services
- Reduce boilerplate and configuration errors

**Standard modules in use:**
- `terraform-aws-modules/lambda/aws` - Lambda function provisioning
- `terraform-aws-modules/dynamodb-table/aws` - DynamoDB tables
- `terraform-aws-modules/sqs/aws` - SQS queues (including FIFO and DLQ support)
- `terraform-aws-modules/s3-bucket/aws` - S3 buckets (preferred for all new buckets)
- `terraform-aws-modules/apigateway-v2/aws` - API Gateway v2 resources
- `terraform-aws-modules/cloudfront/aws` - CloudFront distributions
- `terraform-aws-modules/iam/aws/iam-role` - IAM roles with scoped policies

### 2. Pin All Module Versions

All module source blocks MUST include explicit version pinning to prevent unexpected changes:

**Correct pattern:**
```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"  # Allows patch updates but not minor

  # ... configuration
}
```

**Version pinning strategy:**
- Use pessimistic constraint operators (~>) to allow patch updates
- For major versions: `version = "~> 7.0"` allows `7.x.x` but not `8.x`
- For single version: `version = "7.2.1"` pins exact version (rarely needed)
- NEVER use unversioned modules or `version = "*"`

**Why version pinning matters:**
- Prevents breaking changes from automatic module updates
- Ensures reproducible deployments across environments
- Allows controlled upgrading on your schedule
- Enables clear upgrade decision-making for each module

### 3. Least Privilege Access Control (IAM)

Every Lambda function and service must operate with the minimum permissions required for its job. IAM policies follow the principle: Grant what's needed, nothing more.

**Pattern for Lambda execution roles:**

```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"

  function_name = "my-function"
  handler       = "index.handler"
  runtime       = "python3.12"

  attach_policy_statements = true
  policy_statements = {
    # Only specific DynamoDB actions needed
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",    # Only read what's needed
        "dynamodb:Query"       # Not Scan, not PutItem unless required
      ]
      resources = [
        module.main_table.dynamodb_table_arn  # Specific table, not all tables
      ]
    }

    # Only specific S3 actions and paths
    s3 = {
      effect = "Allow"
      actions = [
        "s3:GetObject"        # Read only, not Write
      ]
      resources = [
        "${module.images_bucket.s3_bucket_arn}/*"  # Specific bucket, not all buckets
      ]
    }

    # Only specific SSM parameter access
    ssm = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"    # Get, not Put or Delete
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/myfantasy/*"
      ]
    }
  }
}
```

**Least privilege checklist:**
- Specify exact actions (not *)
- Specify exact resources (not arn:aws:service:region:account:*)
- Use module outputs for ARNs (not hardcoded strings)
- Document why each permission is needed in comments
- Review quarterly - remove unused permissions

### 4. Module Composition and Dependencies

Modules interact through input/output references. This creates explicit dependency chains that are:
- Testable and auditable
- Self-documenting (showing data flow between services)
- Easy to modify (change one module's output affects all dependent modules)

**Pattern: Output-to-Input Composition**

```hcl
# Step 1: Create the data layer
module "main_dynamodb_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "~> 4.0"

  name      = "${var.environment}-myfantasy-main"
  hash_key  = "PK"
  range_key = "SK"

  # ... table configuration
}

# Step 2: Create the queue for async processing
module "character_generation_queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "~> 4.3"

  name                        = "${var.environment}-myfantasy-character-generation.fifo"
  fifo_queue                  = true

  # Queue depends on DLQ
  redrive_policy = {
    deadLetterTargetArn = module.character_generation_dlq.queue_arn
    maxReceiveCount     = 5
  }
}

# Step 3: Create Lambda that uses both
module "character_generator_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"

  function_name = "${var.environment}-myfantasy-character-generator"
  handler       = "handler.generate_character"
  runtime       = "python3.12"

  # Lambda uses outputs from other modules
  environment_variables = {
    DYNAMODB_TABLE = module.main_dynamodb_table.dynamodb_table_name  # Output reference
    SQS_QUEUE_URL  = module.character_generation_queue.queue_url      # Output reference
  }

  # Lambda can read/write to the specific DynamoDB table and queue
  attach_policy_statements = true
  policy_statements = {
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem"
      ]
      resources = [
        module.main_dynamodb_table.dynamodb_table_arn  # Use module output
      ]
    }
    sqs = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        module.character_generation_queue.queue_arn  # Use module output
      ]
    }
  }
}

# Step 4: Create storage with appropriate access
module "images_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket = "${var.environment}-myfantasy-images"

  # CloudFront needs to read images
  attach_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = module.cloudfront_distribution.cloudfront_origin_access_identity_iam_arns[0]
        }
        Action   = "s3:GetObject"
        Resource = "${module.images_bucket.s3_bucket_arn}/*"
      }
    ]
  })
}

# Step 5: Create distribution that uses bucket
module "cloudfront_distribution" {
  source  = "terraform-aws-modules/cloudfront/aws"
  version = "~> 3.0"

  origin = {
    s3_bucket = {
      domain_name = module.images_bucket.s3_bucket_regional_domain_name  # Output reference
      # ...
    }
  }

  # ... distribution configuration
}
```

**Key principles in composition:**
1. Data flows one direction: DynamoDB -> Lambda -> CloudFront/API
2. Module outputs as inputs: Never hardcode ARNs or IDs
3. Dependency ordering: Terraform automatically handles depends_on via output references
4. Explicit permissions: Each module only accesses what it needs from other modules

### 5. Module Organization

Structure Terraform modules in the root terraform directory:

```
terraform/
├── main.tf                    # Root module with top-level resources
├── variables.tf               # Input variables
├── outputs.tf                 # Module outputs for other modules
├── locals.tf                  # Local values for naming, common config
├── data.tf                    # Data sources
├── backend.tf                 # Terraform state backend configuration
├── dev.tfvars                 # Dev environment values
├── prd.tfvars                 # Production environment values
│
├── modules/                   # Custom modules (if needed)
│   ├── lambda/
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── vpc/
│       └── ...
```

Environment-specific configurations are managed via separate tfvars files (dev.tfvars and prd.tfvars) and terraform workspaces.

### 6. S3 Bucket Best Practices

When creating S3 buckets, always use `terraform-aws-modules/s3-bucket/aws`:

```hcl
module "my_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket = "${var.environment}-myfantasy-my-purpose"

  # Enable versioning for recoverability
  versioning = {
    enabled = true
  }

  # Implement lifecycle rules to manage storage costs
  lifecycle_rule = [
    {
      id      = "expire-old-versions"
      status  = "Enabled"
      noncurrent_version_expiration = {
        days = 30  # Keep old versions for recovery
      }
      abort_incomplete_multipart_upload_days = 7
    }
  ]

  # Enforce encryption
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  # Block public access
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  # Enable access logging for security audit
  logging = {
    target_bucket = module.logging_bucket.s3_bucket_id
    target_prefix = "my-bucket-logs/"
  }

  tags = {
    Environment = var.environment
    App         = var.app_name
    Purpose     = "data-storage"
  }
}
```

## Consequences

### Positive
- Security: Least privilege principle reduces attack surface
- Maintainability: Consistent patterns across all infrastructure
- Auditability: Module outputs create explicit dependency tracking
- Reliability: Version pinning prevents unexpected breaking changes
- Scalability: Modules compose cleanly as system grows

### Tradeoffs
- Slightly more verbose: Module definitions are longer than raw resources (worth the clarity)
- Learning curve: Team must understand module composition patterns
- Version upgrade cost: Occasionally must evaluate module upgrades (avoidable with planning)

## Implementation

### For New Infrastructure
1. Always use community modules when available
2. Pin versions explicitly (use ~> X.0 for minor version flexibility)
3. Apply least privilege to all IAM policies
4. Use module outputs as inputs to dependent modules
5. Document permission needs in comments

### For Existing Infrastructure
- Gradually migrate raw resources to modules as you touch them
- Don't require immediate refactoring of working code
- Prioritize S3 buckets and Lambda functions for module migration

## Examples

### Example 1: Character Generator Lambda with DynamoDB and SQS

See "Module Composition and Dependencies" section above for complete example.

### Example 2: Adding New S3 Bucket

```hcl
module "new_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket = "${var.environment}-myfantasy-new-purpose"

  versioning = {
    enabled = true
  }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Grant Lambda read-only access
module "reader_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"

  # ... configuration

  attach_policy_statements = true
  policy_statements = {
    s3_read = {
      effect = "Allow"
      actions = [
        "s3:GetObject"
      ]
      resources = [
        "${module.new_bucket.s3_bucket_arn}/*"
      ]
    }
  }
}
```

## References

- Terraform AWS Modules: https://github.com/terraform-aws-modules
- AWS Least Privilege Access: https://docs.aws.amazon.com/IAM/latest/userguide/best-practices.html#grant-least-privilege
- Semantic Versioning for Modules: https://www.terraform.io/language/modules/syntax#version
- Terraform Module Composition Pattern: https://www.terraform.io/language/modules/develop

Version History:
- v1.0 (2025-01-02): Initial decision on module standards and IAM patterns
