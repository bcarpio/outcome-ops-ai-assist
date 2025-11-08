# ADR-003: Git Commit Standards

## Status: Accepted

## Context

OutcomeOps AI Assist uses git for version control with a solo developer workflow. Consistent commit message formatting enables:
- Clear project history and changelog generation
- Automated tooling (semantic versioning, release notes)
- Easy searching for specific types of changes
- AI assistant (Claude Code) to follow commit conventions

We need standardized git workflow and commit message format that works for solo development with AI assistance.

## Decision

### Git Workflow (Solo Developer)

**Branch strategy:** None - all work goes directly to main

Since this is solo development with AI assistance, we commit directly to main:

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

**Rationale:**
- Solo developer = no merge conflicts
- CI/CD runs on main branch pushes
- Simpler workflow for AI-assisted development
- Feature branches add overhead without benefit

### Commit Message Format

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
feat(cicd): add GitHub Actions workflow for security scans
```

**Rules:**
- **Scope is required** (e.g., lambda, terraform, cli, docs, cicd)
- Description must be clear and concise
- Use lowercase for type and description
- No period at the end of the description
- **No emojis in commit messages**

### Pre-Commit Requirements

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

### Git Commands Reference

```bash
git pull origin main
git status
git add .
git commit -m "conventional-format: description"
git push origin main
git log --oneline -10
```

## Consequences

### Positive
- Clear, searchable project history
- Conventional commits enable automated changelog generation
- Scope requirement forces developers to think about impact area
- AI assistants (Claude Code) can follow consistent patterns
- No emojis keeps commits professional and parseable

### Tradeoffs
- Slightly more verbose than free-form commit messages
- Requires discipline to follow format consistently
- No feature branches means no PR reviews (acceptable for solo development)

## Implementation

### Claude Code Protocol

When Claude Code commits changes:
1. Claude runs pre-commit checks (fmt, validate, test)
2. Claude writes commit message in conventional format
3. Claude includes attribution footer:
   ```
   🤖 Generated with [Claude Code](https://claude.com/claude-code)

   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
4. Claude asks for approval before pushing to remote

### Enforcement

- CI/CD pipeline validates commit format (future enhancement)
- Knowledge base ingestion system uses these standards
- Claude Code queries knowledge base for standards before committing

## Related ADRs

- ADR-001: Creating ADRs - How to document architectural decisions
- ADR-002: Development Workflow Standards - Overall development workflow

## Version History

- v1.0 (2025-01-06): Initial git commit standards for outcome-ops-ai-assist
