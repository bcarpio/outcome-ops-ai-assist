# ADR-002: Development Workflow Standards

## Status: Accepted

## Context

OutcomeOps AI Assist is developed by a solo developer with AI-assisted development via Claude Code. A consistent development workflow ensures code quality, prevents broken builds, and maintains velocity. This document standardizes what gets done locally versus what the CI/CD pipeline handles automatically.

## Decision

### 1. Task Runners for Local Development

Use Make for consistency and self-documentation:

```bash
make fmt          # Format terraform code
make lint         # Lint markdown files
make validate     # Validate Terraform and documentation
make test         # Run all Lambda tests
make test-unit    # Run unit tests only
make all          # Run fmt, validate, and all tests
```

**Why:**
- Consistent commands across projects
- Self-documenting (run `make help` to see all commands)
- Single source of truth for how tasks are run
- Easy to add pre/post hooks

### 2. Pre-Commit Checklist

Before running `git commit`, ALWAYS execute:

1. **Format code**
   ```bash
   make fmt
   ```

2. **Run validation**
   ```bash
   make validate
   ```

3. **Run tests**
   ```bash
   make test
   # All tests must pass
   ```

4. **Update documentation if needed**
   - Did you add new features or Lambda functions?
   - Did you change how the system works?
   - Update README.md or relevant docs/lambda-*.md files

**Only commit if all checks pass successfully.**

### 3. Local Development Execution

**Always run locally before committing:**
- Code formatting (terraform fmt)
- Linting (markdown file size validation)
- Unit and integration tests
- README/documentation updates

**Never run locally:**
- `terraform apply` - Infrastructure is deployed via CI/CD or manual approval only
- Direct Lambda function deployments

### 4. Git Workflow (Solo Developer)

**Branch strategy:** None - all work goes to main

Since this is solo development, create commits directly to main:

```bash
# Pull latest
git pull origin main

# Make changes locally
# ... edit files, run all pre-commit checks ...

# Stage and commit with conventional commits
git add .
git commit -m "feat(component): clear description of what changed"

# Push to main
git push origin main
```

### Git Commit Message Standards

**All commit messages MUST follow the conventional commits format:**

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Required commit types:**
- `feat(scope):` - New features or functionality
- `fix(scope):` - Bug fixes
- `docs(scope):` - Documentation changes only
- `refactor(scope):` - Code improvements without changing functionality
- `test(scope):` - Test additions or updates
- `chore(scope):` - Maintenance tasks, dependency updates, configuration

**Examples:**
```
feat(lambda): add query-kb Lambda function
fix(terraform): correct IAM policy for DynamoDB access
docs(readme): update installation instructions
refactor(handler): improve error handling logic
test(query-kb): add unit tests for vector search
chore(deps): upgrade boto3 to version 1.28.0
```

**Rules:**
- Scope is required (e.g., lambda, terraform, cli, docs)
- Description must be clear and concise
- Use lowercase for type and description
- No period at the end of the description
- **No emojis in commit messages**

### 5. Terraform Deployment Workflow

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

**Plan file naming:**
- Dev environment: `terraform.dev.out`
- Prd environment: `terraform.prd.out`
- Never commit plan files to git (already in .gitignore)

**Never:**
- Apply Terraform without showing the plan first
- Apply to production without testing in dev first
- Force apply without reviewing the plan
- Apply infrastructure changes without a commit in git history

### 6. Testing Strategy

Tests are organized in `lambda/tests/` directory.

**Test structure:**
```
lambda/tests/
├── unit/                   # Unit tests
│   ├── test_query_kb.py
│   ├── test_vector_query.py
│   └── test_ask_claude.py
├── integration/            # Integration tests
│   └── test_rag_pipeline.py
├── fixtures/               # Test fixtures
│   └── sample_events.json
├── conftest.py            # Pytest configuration
└── Makefile               # Test execution targets
```

**Running tests:**
```bash
# Run all tests
make test

# Run only unit tests
make test-unit

# Run only integration tests
make test-integration

# Run with coverage
make test-coverage
```

**Test requirements:**
- Use pytest for all Lambda handler tests
- Follow Arrange-Act-Assert pattern
- Use moto for AWS service mocking (not @patch decorators)
- Use importlib for dynamic module loading to avoid test hangs
- See claude-guidance.md for detailed testing patterns

### 7. Linting and Code Quality

**Terraform:**
```bash
# Format
make fmt
# or
terraform fmt -recursive

# Validate syntax
make validate
# or
terraform validate
```

**Documentation:**
```bash
# Lint markdown files for embedding size
make lint
# Ensures all .md files are < 7000 tokens for Bedrock embeddings
```

**Makefile targets available:**
```bash
make help         # Show all available commands
make fmt          # Format terraform code
make lint         # Lint markdown files
make validate     # Validate Terraform and documentation
make test         # Run all tests
make all          # Run fmt, validate, and all tests
make clean        # Clean build artifacts
```

### 8. AI-Assisted Development

Claude Code assistance follows this protocol:

**Claude generates code:**
- Claude writes code according to ADRs and patterns in the knowledge base
- Claude queries `outcome-ops-assist` for standards before implementing
- Claude runs local checks: fmt, lint, test
- Claude commits changes with proper conventional commit messages
- Claude asks for approval before pushing to remote

**Claude handles deployment:**
- Claude generates terraform plan files with `-out=` flag
- Claude displays plan output to developer
- Claude explains the infrastructure changes clearly
- Claude waits for developer approval before applying

**Developer approves:**
- Review Claude's code changes and approach
- Verify business logic is correct
- Approve to push to main (for code)
- Review terraform plan output
- Approve to apply (for infrastructure)

**Key principles:**
- Always query `outcome-ops-assist` for standards before implementation
- No emojis in commits or documentation
- Follow conventional commit format
- Include KMS decrypt permissions in all Lambda IAM policies
- Use PK/SK keys for all DynamoDB items

### 9. README and Documentation Maintenance

Update documentation when:
- Adding new Lambda functions (create docs/lambda-*.md)
- Adding new features or functionality
- Changing how the system works
- Adding new dependencies
- Changing deployment procedures
- Updating environment variables or configuration

**Documentation files:**
- `README.md` - Project overview and quick start
- `docs/getting-started.md` - Setup and prerequisites
- `docs/architecture.md` - System design
- `docs/deployment.md` - Deployment procedures
- `docs/lambda-*.md` - Lambda-specific documentation
- `docs/cli-usage.md` - CLI tool documentation
- `docs/claude-guidance.md` - AI assistant development guide
- `docs/adr/*.md` - Architecture Decision Records

### 10. Tools and Commands Reference

**Local development commands:**
```bash
# Format code
make fmt

# Lint markdown
make lint

# Validate everything
make validate

# Run tests
make test
make test-unit
make test-integration

# Run full pipeline
make all

# Terraform commands
cd terraform
terraform workspace list
terraform workspace select dev
terraform validate
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
```

**Git commands:**
```bash
git pull origin main
git status
git add .
git commit -m "conventional-format: description"
git push origin main
git log --oneline -10
```

**Query knowledge base:**
```bash
outcome-ops-assist "What are our Lambda handler standards?"
outcome-ops-assist "How should I structure tests?"
outcome-ops-assist "What Terraform module version should I use?"
```

## Consequences

### Positive
- Clear, repeatable development process
- Consistent code quality before commits
- Reduced bugs through automated checking
- Plan review prevents infrastructure mistakes
- AI-assisted development has clear approval gates
- Knowledge base captures workflow for Claude to follow
- Documentation stays within embedding size limits

### Tradeoffs
- Slightly more steps before committing (worth the quality)
- Plan review adds minor delay before apply (critical for safety)
- Must query knowledge base for standards (ensures consistency)

## Implementation

### Already implemented
1. Makefile with fmt, lint, validate, test, all targets
2. lambda/tests/ directory structure with unit and integration tests
3. Documentation linting for embedding size limits
4. outcome-ops-assist CLI tool for querying standards
5. claude-guidance.md for AI assistant development patterns

### Best practices
1. Run `make all` before every commit
2. Always use `-out=` flag for terraform plan
3. Always review plans before apply
4. Query `outcome-ops-assist` before implementing new features
5. Follow conventional commit format without emojis
6. Update docs/lambda-*.md when creating new Lambda functions

## Related ADRs

- ADR-001: Creating ADRs - How to document architectural decisions

## Version History

- v1.0 (2025-01-03): Development workflow for outcome-ops-ai-assist with Make, testing standards, and AI assistance protocol
