# Getting Started

## Prerequisites

Before you begin, ensure you have the following installed:

1. **Python 3.12+**
   ```bash
   pyenv install 3.12
   pyenv local 3.12
   ```

2. **Terraform 1.5+**
   ```bash
   brew install terraform
   ```

3. **GitHub Personal Access Token (Classic)** - Required for repository operations

   OutcomeOps AI Assist needs a GitHub PAT to:
   - Read repository content and issues
   - Create branches and commits
   - Create and update pull requests
   - Post comments on issues and PRs

   **Creating the Token:**

   1. Go to [GitHub Settings > Developer settings > Personal access tokens > Tokens (classic)](https://github.com/settings/tokens)
   2. Click **Generate new token (classic)**
   3. Give it a descriptive name (e.g., "OutcomeOps AI Assist - Dev")
   4. Set expiration (recommend 90 days for security, or "No expiration" for convenience)
   5. Select the following scopes:

   | Scope | Required | Purpose |
   |-------|----------|---------|
   | `repo` | Yes | Full control of private repositories (read/write code, create branches, PRs) |
   | `workflow` | If using GitHub Actions | Allows triggering workflow runs |

   6. Click **Generate token** and copy it immediately (you won't see it again)

   **Fine-grained PAT Alternative:**

   For more restrictive access, you can use a fine-grained PAT with these permissions:
   - **Repository access:** Select specific repositories
   - **Permissions:**
     - Contents: Read and write
     - Issues: Read and write
     - Pull requests: Read and write
     - Metadata: Read-only (automatically included)

## Quick Start (5 Minutes)

### Step 1: Clone the Repository

```bash
git clone git@github.com:bcarpio/outcome-ops-ai-assist.git
cd outcome-ops-ai-assist
```

### Step 2: Set Up Python Environment

```bash
# Create virtual environment and install dependencies
make setup

# Activate the virtual environment
source venv/bin/activate
```

This runs:
- Creates Python 3.12 virtual environment
- Installs all Lambda handler dependencies
- Ready for testing and development

### Step 3: Create Terraform State Bucket

Terraform requires an S3 bucket to store its state file. This bucket must be created manually before running any Terraform commands.

```bash
# Set your variables
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="us-west-2"
ENV="prd"
BUCKET_NAME="terraform-state-${ACCOUNT_ID}-${REGION}-${ENV}"

# Create the bucket
aws s3api create-bucket \
  --bucket "${BUCKET_NAME}" \
  --region "${REGION}" \
  --create-bucket-configuration LocationConstraint="${REGION}"

# Enable versioning (required for state recovery)
aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

# Block all public access
aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Enable server-side encryption
aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Then update `terraform/backend.tf` to match your bucket name:

```hcl
terraform {
  backend "s3" {
    bucket = "terraform-state-ACCOUNT_ID-us-west-2-prd"  # Replace with your bucket name
    key    = "outcome-ops-ai-assist.tfstate"
    region = "us-west-2"                                  # Replace with your region
  }
}
```

### Step 4: Configure Terraform

Create your local Terraform variables file (not committed to git):

```bash
# Copy from example template
cp terraform/.tfvars.example terraform/prd.tfvars

# Edit terraform/prd.tfvars with your values:
# - aws_region (e.g., us-west-2)
# - repos_to_ingest (which repos to ingest into knowledge base)
```

Store your GitHub token (created in Prerequisites) securely in AWS SSM Parameter Store:

```bash
# Replace YOUR_GITHUB_TOKEN with the PAT you created earlier
aws ssm put-parameter \
  --name /prd/outcome-ops-ai-assist/github/token \
  --value "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  --type SecureString \
  --overwrite
```

Verify the token was stored correctly:
```bash
# This should return the parameter metadata (not the value, for security)
aws ssm get-parameter --name /prd/outcome-ops-ai-assist/github/token

# To verify the token works with GitHub (optional but recommended)
curl -H "Authorization: token $(aws ssm get-parameter --name /prd/outcome-ops-ai-assist/github/token --with-decryption --query Parameter.Value --output text)" \
  https://api.github.com/user
```

### Step 5: Build Run-Tests Docker Image

The run-tests Lambda requires a Docker image for executing test suites. This must be built and pushed to ECR before deploying the full infrastructure.

```bash
# Build the run-tests Docker image and push to ECR
# This automatically creates the ECR repository if it doesn't exist
ENVIRONMENT=prd make build-run-tests-image
```

The Makefile target handles everything:
1. Initializes Terraform and selects/creates the workspace
2. Creates the ECR repository via targeted Terraform apply
3. Builds the Docker image
4. Pushes to ECR

### Step 6: Deploy Infrastructure

```bash
cd terraform

# Plan the full infrastructure
terraform plan -var-file=prd.tfvars -out=terraform.prd.out

# Review the plan output, then apply
terraform apply terraform.prd.out
```

This creates:
- S3 bucket for knowledge base documents
- S3 Vectors bucket and index for embeddings
- DynamoDB table for state tracking
- Lambda functions for ingestion, query, and code generation
- EventBridge schedule for hourly ingestion
- SSM parameters for configuration
- API Gateway endpoint for webhooks

After deployment, note the API Gateway URL from the Terraform output:
```
api_gateway_url = "https://xxxxxx.execute-api.us-west-2.amazonaws.com"
```

### Step 7: Configure Code Generation Trigger

OutcomeOps AI Assist can be triggered from either **GitHub Issues** or **Jira**. Choose the option that fits your workflow:

| Trigger Source | Best For | Setup Complexity |
|----------------|----------|------------------|
| **GitHub Issues** | Teams already using GitHub for issue tracking | Simple (webhook only) |
| **Jira** | Teams using Jira for project management | Moderate (automation rule + webhook) |

---

#### Option A: GitHub Issues (Recommended for GitHub-native workflows)

**Step 6a: Create Webhook Secret**

Generate and store a webhook secret for GitHub webhook verification:

```bash
# Generate a random secret
WEBHOOK_SECRET=$(openssl rand -hex 32)
echo "Save this webhook secret for GitHub configuration: $WEBHOOK_SECRET"

# Store in SSM Parameter Store
aws ssm put-parameter \
  --name /prd/outcome-ops-ai-assist/github/webhook-secret \
  --value "$WEBHOOK_SECRET" \
  --type SecureString \
  --overwrite
```

**Step 6b: Configure GitHub Webhook**

Configure a webhook on each repository you want to enable code generation for:

1. Go to your repository's **Settings > Webhooks > Add webhook**

2. Configure the webhook:
   - **Payload URL:** `https://YOUR_API_GATEWAY_URL/webhooks/github`
   - **Content type:** `application/json`
   - **Secret:** The webhook secret you generated above
   - **Which events:** Select "Let me select individual events" and check only **Issues**
   - **Active:** Checked

3. Click **Add webhook**

4. GitHub will send a ping event. Check CloudWatch logs to verify it was received:
   ```bash
   aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-generate-code --follow
   ```

**Usage:** Add the `approved-for-generation` or `approved-for-plan` label to an issue to trigger code generation:
- `approved-for-generation`: Full code generation (creates plan, generates code, creates PR)
- `approved-for-plan`: Plan-only mode (creates plan and PR without generating code - useful for testing)

---

#### Option B: Jira Integration

If your team uses Jira for issue tracking, you can trigger code generation from Jira issues instead.

See the **[Jira Integration Setup Guide](jira-integration-setup.md)** for complete instructions on:
- Creating the Jira automation rule
- Configuring the webhook endpoint
- Mapping Jira fields to code generation parameters

**Note:** The GitHub PAT (configured in Step 3) is still required for Jira integration - it's used to create branches, commits, and pull requests in GitHub.

---

### Step 8: Verify Installation

Run tests to confirm everything works:

```bash
# From repo root
make test
```

You should see:
```
✓ 17 unit tests passed
✓ All checks complete
```

## Development Workflow

This project follows **[ADR-002: Development Workflow Standards](../fatacyai-adrs/docs/adr/ADR-002-development-workflow.md)**.

Before committing any changes:

```bash
# Format code
make fmt

# Validate Terraform
make validate

# Run tests
make test

# Or all at once
make all
```

Then commit with conventional commit format:
```bash
git add .
git commit -m "feat(component): description of change"
git push origin main
```

Then deploy infrastructure changes:
```bash
cd terraform
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
# Review the plan
terraform apply terraform.prd.out
```

## Available Make Commands

```bash
make help                  # Show all available commands
make setup                 # Create venv and install dependencies
make install               # Install dependencies (venv must exist)
make fmt                   # Format Terraform code
make validate              # Validate Terraform configuration
make test                  # Run all tests
make test-unit             # Run only unit tests
make test-coverage         # Run tests with coverage report
make build-run-tests-image # Build and push run-tests Docker image to ECR
make clean                 # Clean build artifacts
make all                   # Run fmt, validate, and test
```

## Troubleshooting

### Python Version Issues
```bash
# Verify you're using the right Python
python --version  # Should be 3.12.x

# Reset pyenv if needed
pyenv local 3.12
python --version
```

### Virtual Environment Not Activating
```bash
# Ensure venv exists
ls -la venv/

# Try creating it manually
python3.12 -m venv venv
source venv/bin/activate
make install
```

### Terraform Errors

**"Backend initialization required"**
```bash
cd terraform
terraform init -reconfigure
```

**"AWS credentials not found"**
```bash
# Configure AWS credentials
aws configure

# Or set environment variables
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_REGION=us-west-2
```

**"GitHub token not found in SSM"**
```bash
# Store the token
aws ssm put-parameter \
  --name /prd/outcome-ops-ai-assist/github/token \
  --value "YOUR_TOKEN" \
  --type SecureString \
  --overwrite
```

### GitHub Token Errors

**"Resource not accessible by integration" or 403 errors**

This usually means your PAT doesn't have the required scopes:
1. Go to [GitHub Settings > Personal access tokens](https://github.com/settings/tokens)
2. Find your token and click on it
3. Ensure the `repo` scope is selected (full control of private repositories)
4. If you modified scopes, regenerate and update SSM:
   ```bash
   aws ssm put-parameter \
     --name /prd/outcome-ops-ai-assist/github/token \
     --value "ghp_your_new_token" \
     --type SecureString \
     --overwrite
   ```

**"Bad credentials" or 401 errors**

Your token may have expired or been revoked:
```bash
# Test your token
TOKEN=$(aws ssm get-parameter --name /prd/outcome-ops-ai-assist/github/token --with-decryption --query Parameter.Value --output text)
curl -H "Authorization: token $TOKEN" https://api.github.com/user

# If it returns an error, generate a new token and update SSM
```

**Token doesn't have access to a specific repository**

For fine-grained PATs, you must explicitly grant access to each repository:
1. Go to [GitHub Settings > Personal access tokens > Fine-grained tokens](https://github.com/settings/tokens?type=beta)
2. Edit your token
3. Under "Repository access", add the repositories you need

## Updating Infrastructure

### Change Repository Allowlist

Update `prd.tfvars`:

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
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
terraform apply terraform.prd.out
```

This updates the SSM parameter without redeploying Lambda.

### Update Lambda Code

Changes to Lambda handler files or `requirements.txt` trigger Lambda redeploy:

```bash
terraform plan -var-file=prd.tfvars
# Shows: aws_lambda_function will be updated
terraform apply -var-file=prd.tfvars
```

## Terraform State Management

### Remote State

State is stored in S3 with locking via DynamoDB. Configuration in `terraform/backend.tf`.

### Viewing State

```bash
# List all resources
terraform state list

# Show specific resource
terraform state show module.ingest_docs_lambda.aws_lambda_function.this[0]

# Backup state
terraform state pull > terraform-backup.json
```

## Destroying Infrastructure

### Delete Specific Resources

```bash
# Delete only Lambda, keep S3 and DynamoDB
terraform destroy \
  -target=module.ingest_docs_lambda \
  -var-file=prd.tfvars
```

### Delete Everything

**Warning**: This deletes all data in S3 and DynamoDB!

```bash
# First, empty S3 bucket
aws s3 rm s3://prd-outcome-ops-ai-assist-kb --recursive

# Then destroy all infrastructure
terraform destroy -var-file=prd.tfvars
```

## Monitoring

### CloudWatch Logs

```bash
# Real-time logs from Lambda
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-ingest-docs --follow

# Filter for errors
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-ingest-docs \
  --filter-pattern "ERROR"
```

### Lambda Metrics

View in AWS Console:
- Invocations (hourly ingestion)
- Duration (how long ingestion takes)
- Errors (failed documents)
- Throttles (rate limiting)

## Rollback Procedure

If something goes wrong:

```bash
# Fastest fix: revert handler code in git and redeploy
git revert HEAD~1
terraform apply -var-file=prd.tfvars
```

For state rollback, restore from S3 versioned state bucket.

## Production Checklist

Before deploying to production (`prd.tfvars`):

- [ ] Test in dev environment completely
- [ ] Review Terraform plan (no surprises)
- [ ] Backup state: `terraform state pull > backup.json`
- [ ] Create prd.tfvars with production settings
- [ ] Store GitHub token in SSM for prd environment
- [ ] Plan production: `terraform plan -var-file=prd.tfvars`
- [ ] Apply production: `terraform apply -var-file=prd.tfvars`
- [ ] Test production Lambda manually
- [ ] Verify production data appears in DynamoDB/S3
- [ ] Set up CloudWatch alarms

## Next Steps

1. **Read the Documentation**
   - [Architecture Overview](architecture.md) - System design and data flows
   - [Lambda: Ingest Docs](lambda-ingest-docs.md) - Knowledge base ingestion

2. **Review Standards**
   - [ADR-001: Creating ADRs](../fatacyai-adrs/docs/adr/ADR-001-create-adrs.md) - How to write architectural decisions
   - [ADR-002: Development Workflow](../fatacyai-adrs/docs/adr/ADR-002-development-workflow.md) - Development standards

3. **Start Development**
   - Make code changes following ADR-002 workflow
   - Run `make all` before committing
   - Create Terraform plans for infrastructure changes
   - Test in dev before deploying to production

## Getting Help

- Review existing ADRs for architectural questions
- Check CloudWatch logs: `aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-* --follow`

<!-- Confluence sync -->
