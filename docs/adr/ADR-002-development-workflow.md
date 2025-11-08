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

### 4. Git Workflow and Commit Standards

See **ADR-003: Git Commit Standards** for complete details:
- Conventional commits format: `<type>(<scope>): <description>`
- Required commit types: feat, fix, docs, refactor, test, chore
- Pre-commit checklist requirements
- No emojis in commit messages

**Quick reference:**
```bash
git add .
git commit -m "feat(lambda): clear description"
git push origin main
```

### 5. Terraform Deployment Workflow

See **ADR-004: Terraform Workflow Standards** for complete details:
- Always use plan output files (`-out=terraform.dev.out`)
- Review plans before applying
- Test in dev before deploying to production
- Use workspaces for environment isolation

**Quick reference:**
```bash
terraform workspace select dev
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
```

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
- Claude commits changes following ADR-003 (conventional commits)
- Claude asks for approval before pushing to remote

**Claude handles deployment:**
- Claude follows ADR-004 for Terraform workflow
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
- Follow ADR-003 for git commits (no emojis, conventional format)
- Follow ADR-004 for Terraform deployments (plan files, review)
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
```

**Git commands** - See ADR-003 for details:
```bash
git pull origin main
git commit -m "feat(scope): description"
git push origin main
```

**Terraform commands** - See ADR-004 for details:
```bash
terraform workspace select dev
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
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
2. Follow git standards from ADR-003 (conventional commits, no emojis)
3. Follow Terraform workflow from ADR-004 (use plan files, review before apply)
4. Query `outcome-ops-assist` before implementing new features
5. Update docs/lambda-*.md when creating new Lambda functions

## Related ADRs

- ADR-001: Creating ADRs - How to document architectural decisions
- ADR-003: Git Commit Standards - Git workflow and commit message format
- ADR-004: Terraform Workflow Standards - Infrastructure deployment process

## Version History

- v1.0 (2025-01-03): Development workflow for outcome-ops-ai-assist with Make, testing standards, and AI assistance protocol
