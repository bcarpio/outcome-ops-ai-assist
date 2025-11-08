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

3. **GitHub Personal Access Token** (for repository access)
   ```bash
   # Create at: https://github.com/settings/tokens
   # Scopes needed: repo (full control of private repositories)
   ```

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

### Step 3: Configure Terraform

Create your local Terraform variables file (not committed to git):

```bash
# Copy from example template
cp terraform/.tfvars.example terraform/dev.tfvars

# Edit terraform/dev.tfvars with your values:
# - aws_region (e.g., us-west-2)
# - repos_to_ingest (which repos to ingest into knowledge base)
```

Store your GitHub token securely in AWS SSM Parameter Store:

```bash
aws ssm put-parameter \
  --name /dev/outcome-ops-ai-assist/github/token \
  --value "YOUR_GITHUB_TOKEN" \
  --type SecureString \
  --overwrite
```

### Step 4: Deploy Infrastructure

```bash
cd terraform

# Initialize Terraform
terraform init

# Create dev workspace
terraform workspace new dev

# Plan the infrastructure
terraform plan -var-file=dev.tfvars -out=terraform.dev.out

# Review the plan output, then apply
terraform apply terraform.dev.out
```

This creates:
- S3 bucket for knowledge base documents
- DynamoDB table for embeddings
- Lambda function for ingesting documents
- EventBridge schedule for hourly ingestion
- SSM parameters for configuration

### Step 5: Verify Installation

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
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
# Review the plan
terraform apply terraform.dev.out
```

## Available Make Commands

```bash
make help          # Show all available commands
make setup         # Create venv and install dependencies
make install       # Install dependencies (venv must exist)
make fmt           # Format Terraform code
make validate      # Validate Terraform configuration
make test          # Run all tests
make test-unit     # Run only unit tests
make test-coverage # Run tests with coverage report
make clean         # Clean build artifacts
make all           # Run fmt, validate, and test
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
  --name /dev/outcome-ops-ai-assist/github/token \
  --value "YOUR_TOKEN" \
  --type SecureString \
  --overwrite
```

## Next Steps

1. **Read the Documentation**
   - [Architecture Overview](architecture.md) - System design and data flows
   - [Lambda: Ingest Docs](lambda-ingest-docs.md) - Knowledge base ingestion
   - [Deployment Guide](deployment.md) - Detailed operations guide

2. **Review Standards**
   - [ADR-001: Creating ADRs](../fatacyai-adrs/docs/adr/ADR-001-create-adrs.md) - How to write architectural decisions
   - [ADR-002: Development Workflow](../fatacyai-adrs/docs/adr/ADR-002-development-workflow.md) - Development standards

3. **Start Development**
   - Make code changes following ADR-002 workflow
   - Run `make all` before committing
   - Create Terraform plans for infrastructure changes
   - Test in dev before deploying to production

## Getting Help

- Check the [Troubleshooting](deployment.md#troubleshooting) section in the deployment guide
- Review existing ADRs for architectural questions
- Check CloudWatch logs: `aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-* --follow`
