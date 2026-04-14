# ADR-004: Terraform Workflow and Module Standards

## Status: Accepted

## Context

OutcomeOps uses Terraform for infrastructure as code (IaC). We deploy to multiple environments (dev, prd) using workspaces. Consistent infrastructure requires:
- Standardized resource naming across all environments
- Community modules with exact version pinning
- Review of planned changes before applying
- Environment isolation to prevent accidental production changes

## Decision

### Resource Naming Convention

**All resources MUST follow this naming pattern:**

```
${var.environment}-${var.app_name}-{resource-name}
```

**Examples:**
```hcl
# DynamoDB table
name = "${var.environment}-${var.app_name}-licenses"
# Result: dev-outcomeops-licenses, prd-outcomeops-licenses

# Lambda function
function_name = "${var.environment}-${var.app_name}-generate-code"
# Result: dev-outcomeops-generate-code, prd-outcomeops-generate-code

# S3 bucket
bucket = "${var.environment}-${var.app_name}-artifacts"
# Result: dev-outcomeops-artifacts, prd-outcomeops-artifacts

# Secrets Manager
name = "${var.environment}-${var.app_name}/license/private-key"
# Result: dev-outcomeops/license/private-key
```

**Required variables in every Terraform project:**
```hcl
variable "environment" {
  description = "Environment name (dev, staging, prd)"
  type        = string
}

variable "app_name" {
  description = "Application name prefix for resources"
  type        = string
}
```

**Use locals for consistent prefixing:**
```hcl
locals {
  name_prefix = "${var.environment}-${var.app_name}"
}

# Then use throughout:
name = "${local.name_prefix}-licenses"
```

### Community Module Standards

**Always use terraform-aws-modules when available:**

```hcl
# DynamoDB
module "my_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.2.0"  # Exact version
}

# Lambda
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"  # Exact version
}

# S3
module "my_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "4.1.0"  # Exact version
}

# API Gateway
module "my_api" {
  source  = "terraform-aws-modules/apigateway-v2/aws"
  version = "5.0.0"  # Exact version
}
```

**Version pinning rules:**
- Pin exact `major.minor.patch` versions (e.g., `version = "4.2.0"`)
- NEVER use pessimistic operators (`~>`, `>=`)
- NEVER use `version = "*"` or omit version
- Update versions explicitly via code review

**Before adding a new module:**
1. Read the existing Terraform file first
2. Check what version is already in use for that module type
3. Use the exact same version as existing modules

### File Organization

```
terraform/
├── versions.tf      # Terraform and provider versions
├── variables.tf     # Input variables
├── main.tf          # Provider config and locals
├── dynamodb.tf      # DynamoDB tables
├── lambda.tf        # Lambda functions
├── s3.tf            # S3 buckets
├── secrets.tf       # Secrets Manager references
├── outputs.tf       # Output values
├── prd.tfvars       # Dev environment values
└── prd.tfvars       # Prd environment values
```

### Variable File Standards

**prd.tfvars:**
```hcl
environment = "dev"
app_name    = "outcomeops"
aws_region  = "us-east-1"
```

**prd.tfvars:**
```hcl
environment = "prd"
app_name    = "outcomeops"
aws_region  = "us-east-1"
```

### Terraform Deployment Workflow

**Always use plan output files for safety and review:**

```bash
cd terraform

# Step 1: Select workspace
terraform workspace select dev

# Step 2: Generate plan for dev environment
terraform plan -var-file=prd.tfvars -out=terraform.prd.out

# Step 3: Review the plan output
# Check what resources will be created, modified, or destroyed

# Step 4: Apply the plan (only after review)
terraform apply terraform.prd.out

# Step 5: Test in dev environment
# Verify features work as expected
# Check CloudWatch logs for errors

# Step 6: Deploy to production (only after dev is stable)
terraform workspace select prd
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
# Review and apply
terraform apply terraform.prd.out
```

### Plan File Naming Convention

- Dev environment: `terraform.prd.out`
- Prd environment: `terraform.prd.out`
- **Never commit plan files to git** (already in .gitignore)

### Terraform Commands Reference

```bash
cd terraform
terraform workspace list
terraform workspace select dev
terraform fmt -recursive
terraform validate
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
terraform apply terraform.prd.out
```

### Local Development vs CI/CD

**Always run locally before committing:**
- `terraform fmt -recursive` - Format code
- `terraform validate` - Validate syntax
- Review plan output

**Never run locally:**
- `terraform apply` to production - Use CI/CD or manual approval only
- Direct Lambda function deployments - Always use Terraform

### Safety Rules

**Never:**
- Apply Terraform without showing the plan first
- Apply to production without testing in dev first
- Force apply without reviewing the plan
- Apply infrastructure changes without a commit in git history
- Use different module versions in the same file

**Always:**
- Use `-out=` flag for terraform plan
- Review plan output before applying
- Test in dev environment first
- Use workspaces for environment isolation
- Prefix all resources with `${var.environment}-${var.app_name}`

## Consequences

### Positive
- Consistent resource naming across environments
- Easy to identify resource environment at a glance
- No version drift with exact pinning
- Plan review prevents infrastructure mistakes
- Output files ensure apply matches reviewed plan
- Workspace isolation prevents accidental production changes

### Tradeoffs
- Must maintain version consistency manually
- Plan review adds minor delay before apply (critical for safety)
- Two-step process (plan then apply) vs direct apply
- Must remember workspace selection for each environment

## Implementation

### Standard DynamoDB Module Usage

```hcl
module "licenses_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.2.0"

  name      = "${var.environment}-${var.app_name}-licenses"
  hash_key  = "PK"
  range_key = "SK"

  billing_mode = "PAY_PER_REQUEST"

  attributes = [
    { name = "PK", type = "S" },
    { name = "SK", type = "S" }
  ]

  point_in_time_recovery_enabled = true

  tags = {
    Name        = "${var.environment}-${var.app_name}-licenses"
    Environment = var.environment
  }
}
```

### Standard Lambda Module Usage

```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-my-function"
  description   = "Description of the function"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 30

  source_path = "../lambda/my-function"

  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  cloudwatch_logs_retention_in_days = 7

  tags = {
    Name        = "${var.environment}-${var.app_name}-my-function"
    Environment = var.environment
  }
}
```

### AI-Assisted Deployment Protocol

When Claude Code handles deployment:

1. **Claude generates terraform plan files** with `-out=` flag
2. **Claude displays plan output** to developer
3. **Claude explains the infrastructure changes** clearly
4. **Claude waits for developer approval** before applying
5. **Claude applies the reviewed plan** only after approval

## Related ADRs

- ADR-001: Terraform Infrastructure Patterns - Module and version standards
- ADR-002: Development Workflow Standards - Overall development workflow
- ADR-003: Git Commit Standards - Commit format for infrastructure changes

## Version History

- v1.0 (2025-01-06): Initial Terraform workflow standards
- v1.1 (2025-01-25): Added resource naming conventions and module standards

<!-- Confluence sync -->
