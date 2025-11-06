# ADR-004: Terraform Workflow Standards

## Status: Accepted

## Context

OutcomeOps AI Assist uses Terraform for infrastructure as code (IaC). We deploy to multiple environments (dev, prd) using workspaces. Safe infrastructure changes require:
- Review of planned changes before applying
- Environment isolation to prevent accidental production changes
- Consistent deployment process that AI assistants can follow
- Validation before deployment to catch configuration errors

## Decision

### Terraform Deployment Workflow

**Always use plan output files for safety and review:**

```bash
cd terraform

# Step 1: Select workspace
terraform workspace select dev

# Step 2: Generate plan for dev environment
terraform plan -var-file=dev.tfvars -out=terraform.dev.out

# Step 3: Review the plan output
# Check what resources will be created, modified, or destroyed

# Step 4: Apply the plan (only after review)
terraform apply terraform.dev.out

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

- Dev environment: `terraform.dev.out`
- Prd environment: `terraform.prd.out`
- **Never commit plan files to git** (already in .gitignore)

### Terraform Commands Reference

```bash
cd terraform
terraform workspace list
terraform workspace select dev
terraform validate
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
terraform fmt -recursive
```

### Local Development vs CI/CD

**Always run locally before committing:**
- `terraform fmt` - Format code
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

**Always:**
- Use `-out=` flag for terraform plan
- Review plan output before applying
- Test in dev environment first
- Use workspaces for environment isolation

## Consequences

### Positive
- Plan review prevents infrastructure mistakes
- Output files ensure apply matches reviewed plan
- Workspace isolation prevents accidental production changes
- Consistent process for AI-assisted deployment
- Git history tracks all infrastructure changes

### Tradeoffs
- Plan review adds minor delay before apply (critical for safety)
- Two-step process (plan then apply) vs direct apply
- Must remember workspace selection for each environment

## Implementation

### Makefile Integration

```bash
make fmt          # Format terraform code
make validate     # Validate terraform configuration
```

### AI-Assisted Deployment Protocol

When Claude Code handles deployment:

1. **Claude generates terraform plan files** with `-out=` flag
2. **Claude displays plan output** to developer
3. **Claude explains the infrastructure changes** clearly
4. **Claude waits for developer approval** before applying
5. **Claude applies the reviewed plan** only after approval

### Workspace Management

```bash
# List workspaces
terraform workspace list

# Select workspace
terraform workspace select dev

# Current workspace shown in prompt
```

### Environment Configuration

- `dev.tfvars` - Development environment variables
- `prd.tfvars` - Production environment variables
- Both files use same variable structure defined in `variables.tf`

## Related ADRs

- ADR-002: Development Workflow Standards - Overall development workflow
- ADR-003: Git Commit Standards - Commit format for infrastructure changes

## Version History

- v1.0 (2025-01-06): Initial Terraform workflow standards for outcome-ops-ai-assist
