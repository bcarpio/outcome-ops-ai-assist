# Deployment Guide

## Prerequisites

1. **Terraform 1.5+**
   ```bash
   brew install terraform
   terraform version
   ```

2. **AWS CLI**
   ```bash
   aws --version
   aws configure  # Set credentials
   ```

3. **GitHub Personal Access Token**
   - Create at: https://github.com/settings/tokens
   - Scopes needed: `repo` (full control)
   - Store securely for next step

4. **Python 3.12+** (for local testing)
   ```bash
   pyenv install 3.12
   pyenv local 3.12
   ```

## Environment Setup

### 1. Clone Repository

```bash
git clone git@github.com:bcarpio/outcome-ops-ai-assist.git
cd outcome-ops-ai-assist
```

### 2. Create GitHub Token in SSM

Store your GitHub token in AWS SSM Parameter Store (encrypted):

```bash
# For dev environment
aws ssm put-parameter \
  --name /dev/outcome-ops-ai-assist/github/token \
  --value "YOUR_GITHUB_TOKEN" \
  --type SecureString \
  --overwrite

# For prod environment
aws ssm put-parameter \
  --name /prd/outcome-ops-ai-assist/github/token \
  --value "YOUR_GITHUB_TOKEN" \
  --type SecureString \
  --overwrite
```

### 3. Configure Terraform Variables

Create `terraform/dev.tfvars`:

```hcl
aws_region   = "us-west-2"
environment  = "dev"
app_name     = "outcome-ops-ai-assist"

repos_to_ingest = [
  {
    name    = "outcome-ops-ai-assist"
    project = "bcarpio/outcome-ops-ai-assist"
    type    = "standards"
  },
  {
    name    = "my-standards-repo"
    project = "myorg/my-standards-repo"
    type    = "standards"
  },
  {
    name    = "my-app"
    project = "myorg/my-app"
    type    = "application"
  }
]
```

Similarly, create `terraform/prd.tfvars` for production.

**Note**: These files are in `.gitignore` (sensitive data). Use `.tfvars.example` as reference.

## Deployment Process

### 1. Initialize Terraform

```bash
cd terraform
terraform init

# If switching between envs, reconfigure backend:
terraform init -reconfigure
```

### 2. Plan Infrastructure

```bash
# Dev environment
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
```

Review the plan output. Key resources:
- S3 bucket for documents
- DynamoDB table for embeddings
- Lambda function for ingestion
- EventBridge schedule
- SSM parameters

### 3. Apply Infrastructure

```bash
# Apply the saved plan (always use plan file, not -auto-approve)
terraform apply terraform.dev.out

# Or apply directly with tfvars
terraform apply -var-file=dev.tfvars
```

This creates:
- S3 knowledge base bucket
- DynamoDB code-maps table
- ingest-docs Lambda function
- EventBridge hourly schedule
- SSM parameter store entries

### 4. Test Deployment

Verify everything is working:

```bash
# Check Lambda function
aws lambda get-function \
  --function-name dev-outcome-ops-ai-assist-ingest-docs \
  --region us-west-2

# Manually trigger ingestion
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-ingest-docs \
  /tmp/response.json \
  --region us-west-2

cat /tmp/response.json

# Check CloudWatch logs
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ingest-docs \
  --follow \
  --region us-west-2
```

### 5. Verify Data Ingestion

Check if documents were stored:

```bash
# List documents in S3
aws s3 ls s3://dev-outcome-ops-ai-assist-kb/ --recursive

# Scan DynamoDB table
aws dynamodb scan \
  --table-name dev-outcome-ops-ai-assist-code-maps \
  --max-items 5 \
  --region us-west-2
```

## Terraform State Management

### Remote State

State is stored in S3 with locking via DynamoDB. Configuration in `terraform/backend.tf`:

```hcl
backend "s3" {
  bucket         = "terraform-state-bucket"
  key            = "outcome-ops/terraform.tfstate"
  region         = "us-west-2"
  dynamodb_table = "terraform-locks"
  encrypt        = true
}
```

### Viewing State

```bash
# List all resources
terraform state list

# Show specific resource
terraform state show module.ingest_docs_lambda.aws_lambda_function.this[0]
```

### Backup State

```bash
terraform state pull > terraform-backup.json
```

## Updating Infrastructure

### Change Repository Allowlist

Update `dev.tfvars` or `prd.tfvars`:

```hcl
repos_to_ingest = [
  # Add new repo here
  {
    name    = "new-repo"
    project = "bcarpio/new-repo"
    type    = "standards"
  }
]
```

Then:

```bash
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
```

This updates the SSM parameter without redeploying Lambda.

### Update Lambda Code

Changes to `lambda/ingest-docs/handler.py` or `requirements.txt` trigger Lambda redeploy:

```bash
terraform plan -var-file=dev.tfvars
# Shows: aws_lambda_function will be updated
terraform apply -var-file=dev.tfvars
```

### Update Lambda Timeout

Change in `terraform/lambda.tf`:

```hcl
module "ingest_docs_lambda" {
  timeout = 600  # Changed from 300
}
```

Then plan and apply.

## Destroying Infrastructure

### Delete Specific Resources

```bash
# Delete only Lambda, keep S3 and DynamoDB
terraform destroy \
  -target=module.ingest_docs_lambda \
  -var-file=dev.tfvars
```

### Delete Everything

**Warning**: This deletes all data in S3 and DynamoDB!

```bash
# First, empty S3 bucket
aws s3 rm s3://dev-outcome-ops-ai-assist-kb --recursive

# Disable DynamoDB versioning (if enabled)
aws dynamodb update-table \
  --table-name dev-outcome-ops-ai-assist-code-maps \
  --stream-specification StreamEnabled=false

# Then destroy all infrastructure
terraform destroy -var-file=dev.tfvars
```

## Monitoring Deployments

### CloudWatch Logs

```bash
# Real-time logs from Lambda
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ingest-docs --follow

# Filter for errors
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ingest-docs \
  --filter-pattern "ERROR"
```

### Metrics

View Lambda metrics in AWS Console:
- Invocations (hourly ingestion)
- Duration (how long ingestion takes)
- Errors (failed documents)
- Throttles (rate limiting)

### Alarms

Set CloudWatch alarms for:

```bash
# High error rate
aws cloudwatch put-metric-alarm \
  --alarm-name dev-outcome-ops-ingest-errors \
  --alarm-description "Ingest Lambda errors > 1" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions Name=FunctionName,Value=dev-outcome-ops-ai-assist-ingest-docs
```

## Troubleshooting

### Lambda fails with "ParameterNotFound"

**Issue**: GitHub token not in SSM

**Fix**:
```bash
aws ssm put-parameter \
  --name /dev/outcome-ops-ai-assist/github/token \
  --value "YOUR_TOKEN" \
  --type SecureString \
  --overwrite
```

### Lambda fails with "InvalidAction"

**Issue**: IAM role doesn't have Bedrock permissions

**Fix**: Re-apply Terraform to update role:
```bash
terraform apply -var-file=dev.tfvars -refresh=true
```

### S3 bucket already exists

**Issue**: Bucket name collision (S3 buckets are globally unique)

**Fix**: Change bucket name in Terraform or restore from backup state

### DynamoDB throttling

**Issue**: Many documents or large embeddings causing throttle

**Fix**:
```bash
# Increase on-demand capacity
aws dynamodb update-table \
  --table-name dev-outcome-ops-ai-assist-code-maps \
  --billing-mode PAY_PER_REQUEST
```

## Production Checklist

Before deploying to production (`prd.tfvars`):

- [ ] Test in dev environment completely
- [ ] Review Terraform plan (no surprises)
- [ ] Backup dev infrastructure: `terraform state pull > backup.json`
- [ ] Create prd.tfvars with production settings
- [ ] Store GitHub token in SSM for prd environment
- [ ] Plan production: `terraform plan -var-file=prd.tfvars`
- [ ] Review production plan (different resource names, etc.)
- [ ] Apply production: `terraform apply -var-file=prd.tfvars`
- [ ] Test production Lambda manually
- [ ] Verify production data appears in DynamoDB/S3
- [ ] Set up CloudWatch alarms
- [ ] Document any custom configurations

## Rollback Procedure

If something goes wrong:

### Rollback to Previous Terraform State

```bash
# List previous states
aws s3api list-object-versions \
  --bucket terraform-state-bucket \
  --prefix outcome-ops/

# Restore specific version
aws s3api get-object \
  --bucket terraform-state-bucket \
  --key outcome-ops/terraform.tfstate \
  --version-id YOUR_VERSION_ID \
  terraform-rollback.json

terraform state push terraform-rollback.json

# Re-apply from rolled-back state
terraform apply -var-file=dev.tfvars
```

### Rollback Lambda Code

```bash
# Previous Lambda version is in deployment package history
# Fastest fix: revert handler.py in git and redeploy

git revert HEAD~1
terraform apply -var-file=dev.tfvars
```

## Related Documentation

- **Architecture**: See `docs/architecture.md` for system design
- **Lambda Functions**: See `docs/lambda-*.md` for specific function details
- **ADRs**: See `docs/adr/` for architectural decisions
- **Infrastructure Code**: See `terraform/` for all IaC definitions
