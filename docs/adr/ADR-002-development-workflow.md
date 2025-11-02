# ADR-002: Development Workflow Standards

## Status: Accepted

## Context

MyFantasy.ai is developed by a solo developer with AI-assisted development via Claude Code. A consistent development workflow ensures code quality, prevents broken builds, and maintains velocity. This document standardizes what gets done locally versus what the CI/CD pipeline handles automatically.

## Decision

### 1. Task Runners for Local Development

Use task runners (Make, Just) instead of direct tool invocations for consistency and clarity:

**Preferred pattern:**
```bash
make build
make lint
make test
make fmt
```

**Why:**
- Consistent commands across all projects
- Hides implementation details (developers don't need to know the exact tool)
- Self-documenting (run `make` or `just --list` to see all commands)
- Easy to add pre/post hooks
- Single source of truth for how tasks are run

### 2. Pre-Commit Checklist

Before running `git commit`, ALWAYS execute this checklist:

1. **Update documentation if needed**
   - Did you add new features or infrastructure?
   - Did you change how the system works?
   - Did you add new directories or files?
   - If yes to any, update README.md or relevant documentation first

2. **Format code**
   ```bash
   make fmt
   # or
   terraform fmt -recursive
   python -m black .
   ```

3. **Run linting**
   ```bash
   make lint
   # Must pass with no errors
   ```

4. **Build/compile (if applicable)**
   ```bash
   make build
   terraform validate
   ```

5. **Run tests**
   ```bash
   make test
   # All tests must pass
   ```

**Only commit if all checks pass successfully.**

### 3. Local Development Execution

**Always run locally before committing:**
- Code formatting (black, terraform fmt, prettier, etc.)
- Linting and type checking (eslint, pylint, terraform tfsec, etc.)
- Unit and integration tests
- README/documentation updates

**Run locally after commit for deployment:**
- Terraform plan with output file
- Review plan with developer
- Terraform apply from plan file

**Important:** Terraform apply should only be done locally after code has been committed and thoroughly reviewed. This ensures your git history matches your infrastructure state.

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
git commit -m "feat(feature-name): clear description of what changed"

# Push to main
git push origin main

# Only AFTER successful push, deploy locally
cd terraform
terraform workspace select dev
terraform plan -var-file=dev.tfvars -out=terraform.out
# Claude Code: Review and discuss plan with developer
# Developer: Approve deployment
terraform apply terraform.out
```

**Commit messages follow conventional commits:**
```
feat(component): add new feature
fix(component): fix specific bug
docs(component): update documentation
refactor(component): improve code quality
test(component): add or update tests
chore(component): maintenance tasks
```

### 5. Terraform Deployment Workflow

**Always use plan output files for safety and review:**

```bash
cd terraform

# Step 1: Generate plan for dev environment
terraform workspace select dev
terraform plan -var-file=dev.tfvars -out=terraform.dev.out

# Step 2: Claude Code reviews the plan output
# Claude Code displays the plan to show:
# - What resources will be created
# - What resources will be modified
# - What resources will be destroyed
# - Any warnings or errors
#
# Claude Code then discusses with developer:
# "I've created the plan file. Here are the changes:
#  + aws_lambda_function.my_handler (new)
#  ~ aws_iam_role.lambda_execution (modified - adding new policy)
#
#  These changes implement the new user profile API handler.
#  Should I proceed with terraform apply?"

# Step 3: Developer reviews and approves
# Developer: "Looks good, go ahead"

# Step 4: Apply the plan
terraform apply terraform.dev.out

# Step 5: Test in dev environment
# Verify features work as expected
# Check CloudWatch logs for errors

# Step 6: Deploy to production (only after dev is stable)
terraform workspace select prd
terraform plan -var-file=prd.tfvars -out=terraform.prd.out

# Claude Code reviews and discusses with developer
# Developer approves or requests changes

terraform apply terraform.prd.out

# Step 7: Verify in production
# Monitor CloudWatch logs
# Check API endpoints
```

**Plan file naming:**
- Dev environment: `terraform.dev.out`
- Prd environment: `terraform.prd.out`
- Never commit plan files to git (add to .gitignore)

**Claude Code protocol for Terraform:**
1. Generate plan file with `-out=terraform.ENVIRONMENT.out`
2. Display the plan output to the developer
3. Summarize the key changes (resources added, modified, destroyed)
4. Ask for explicit approval before running apply
5. Only apply if developer confirms
6. Report apply results and any deployment issues

**Never:**
- Apply Terraform without showing the plan first
- Apply to production without testing in dev first
- Force apply without reviewing the plan
- Apply infrastructure changes without a commit in git history
- Create plan files and apply without developer awareness

### 6. Testing Strategy

Since test coverage is currently limited, establish a testing standard:

**Testing approach:**
- Write tests in same directory as code or in `tests/` directory
- Use pytest for Python Lambda handlers
- Use unittest for Python utilities
- Test fixtures in `tests/fixtures/`
- Run all tests before committing: `make test`

**Test structure:**
```
lambda/my_handler/
├── handler.py
├── requirements.txt
└── __tests__/
    ├── __init__.py
    ├── test_handler.py
    └── fixtures/
        └── sample_events.json

# Or

tests/
├── unit/
│   └── test_handler.py
├── integration/
│   └── test_api_flow.py
└── fixtures/
    └── sample_events.json
```

**Running tests:**
```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=lambda --cov-report=html

# Run specific test file
pytest tests/unit/test_handler.py -v
```

**Test coverage goals (phased):**
- Phase 1 (now): Establish testing patterns for critical paths
- Phase 2 (next): Add tests for all new Lambda handlers
- Phase 3 (future): Improve coverage on existing handlers to 70%+

### 7. Linting and Code Quality

**Python:**
```bash
# Linting
pylint lambda/ src/

# Formatting
black lambda/ src/

# Type checking (optional)
mypy lambda/ src/
```

**Terraform:**
```bash
# Validate syntax
terraform validate

# Format
terraform fmt -recursive

# Lint/security (optional)
tfsec .
```

**Include in Makefile:**
```makefile
.PHONY: fmt lint test build

fmt:
	black lambda/ src/
	terraform fmt -recursive

lint:
	pylint lambda/ src/
	terraform validate

test:
	pytest --cov=lambda

build:
	# If applicable: tsc, compile, etc.

test-and-lint: fmt lint test

check: test-and-lint build
```

### 8. AI-Assisted Development

Claude Code assistance follows this protocol:

**Claude generates code:**
- Claude writes code according to ADRs and patterns in the knowledge base
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

**Workflow example:**
```
Claude: "I've implemented the user profile handler. Changes are:
- Updated handler in lambda/profile_handler/
- Added tests in tests/unit/test_profile_handler.py
- Updated README.md
- All checks pass (fmt, lint, test)

Should I commit and push to main?"

Developer: "Looks good, go ahead"

Claude: commits and pushes

Claude: "Code deployed. Now generating terraform plan for new Lambda handler..."

Claude: "Here's the infrastructure plan:
+ aws_lambda_function.profile_handler (will be created)
+ aws_iam_role.profile_handler (will be created)
+ aws_lambda_permission.profile_api_gateway (will be created)
~ aws_api_gateway_integration.routes (will be modified)

The plan shows 3 resources to create and 1 to modify.
Ready to apply to dev?"

Developer: "Looks good, apply to dev"

Claude: "Applying terraform.dev.out...
Success! Lambda function created and API Gateway routes updated.
Ready to test in dev environment."

Developer: "Tests pass in dev. Deploy to production."

Claude: "Generating production plan...
[same resources, applying to prd]
Production deployment complete."
```

### 9. README Maintenance

Update README.md when:
- Adding new features or functionality
- Changing how the system works
- Adding new dependencies
- Completing major milestones
- Adding new directories or Lambda handlers
- Changing deployment procedures
- Updating environment variables or configuration

README should include:
- Project purpose and overview
- Architecture diagram or explanation
- Prerequisites and setup instructions
- How to run locally
- How to run tests
- How to deploy (dev and production)
- Environment variables needed
- Troubleshooting common issues
- Links to related ADRs

### 10. Tools and Commands Reference

**Local development commands:**
```bash
# Format code
make fmt

# Lint code
make lint

# Run tests
make test

# Check everything (fmt + lint + test + validate)
make check

# Build (if applicable)
make build

# Terraform commands
cd terraform
terraform validate
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
terraform workspace list
terraform workspace select prd
```

**Git commands:**
```bash
git pull origin main
git add .
git commit -m "conventional-format: description"
git push origin main
git log --oneline -10
```

## Consequences

### Positive
- Clear, repeatable development process
- Consistent code quality before commits
- Reduced bugs through automated checking
- Plan review prevents infrastructure mistakes
- AI-assisted development has clear approval gates
- Easy to scale if team grows later
- Knowledge base captures workflow for Claude to follow

### Tradeoffs
- Slightly more steps before committing (worth the quality)
- Test coverage phase-in means some code has no tests initially (acceptable, improving over time)
- Plan review adds minor delay before apply (critical for safety)

## Implementation

### Starting today
1. Define Makefile or Justfile with fmt, lint, test, check targets
2. Create tests/ directory structure
3. Run `make check` before every commit
4. Always use `-out=` flag for terraform plan
5. Always review plans before apply
6. Document commands in README.md
7. Follow conventional commit format

### Next phases
1. Improve test coverage on critical paths
2. Add integration tests for API flows
3. Document test patterns for each Lambda handler type

## Related ADRs

- ADR-001: Terraform Infrastructure Patterns and Module Standards
- ADR-003: Lambda Handler Standards (future)
- ADR-004: Testing Standards (future)

Version History:
- v1.0 (2025-01-02): Initial development workflow for solo developer with AI assistance
- v1.1 (2025-01-02): Add terraform plan output file workflow and Claude Code review protocol
