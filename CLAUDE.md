# Claude Guidance for OutcomeOps AI Assist

## What This System Is

OutcomeOps AI Assist is a **generic platform** for AI-powered code generation designed to support Fortune 500 companies with hundreds or thousands of repositories. The platform is language-agnostic and framework-agnostic by design.

**Key insight:** Different repos may use Terraform, Helm, CloudFormation, Python, Java, TypeScript, Spring Boot, Lambda, ECS, Kubernetes, etc. The platform code must remain generic - all domain-specific knowledge lives in the **Knowledge Base** (ADRs, docs, code maps).

## Core Principle: Documentation-Driven Decisions (ADR-007)

**Primary Rule:** When a problem can be solved by improving documentation (ADRs, READMEs, guides), prefer that over hardcoding logic in application code.

### When to Use Documentation (ADRs/Docs)
- Pattern definitions ("This is how we structure handlers")
- Convention guidance ("Use this naming pattern")
- Best practices ("Prefer X over Y because...")
- Domain knowledge ("Use module version 8.1.2")
- Architectural decisions, quality standards, examples

### When to Use Code Logic
- Workflow orchestration ("After tests pass, create PR")
- Error recovery, retries, infrastructure
- Integration glue, validation, automation

**Test:** If you're tempted to hardcode a pattern, ask: "Could an ADR solve this?"

**The platform queries the KB for standards - it does NOT hardcode them.** If generated code is wrong, the fix is usually in the ADRs/docs, not in the platform code.

## How to Get Standards

Query the knowledge base via MCP before implementing anything. The OutcomeOps MCP server
is configured in `~/.claude/mcp.json` and provides these tools automatically:

- `query_knowledge_base` - Search for standards, ADRs, patterns, and documentation
- `get_coding_standards` - Retrieve coding standards for a specific topic
- `list_workspaces` - List available workspaces
- `list_workspace_documents` - List documents in a workspace

**Use these MCP tools proactively** when building features, fixing bugs, or making
architectural decisions. Always query the KB before implementing anything to ensure
code matches organizational standards.

Example queries to make via MCP tools:
- "What are our Lambda handler standards?"
- "What are our Terraform module standards?"
- "How should tests be structured?"

The KB contains: ADRs, READMEs, code maps, infrastructure patterns, handler patterns, testing practices.

**Fallback (CLI):** If MCP tools are unavailable, use the CLI:
```bash
./scripts/outcome-ops-assist --env prd "What are our Lambda handler standards?"
```

## Essential Workflow Rules

### Ask, Don't Guess
- Never assume requirements, implementation approaches, or API signatures
- Use AskUserQuestion tool when unclear
- Query the KB before making assumptions

### Verify Before Changing
- Read files before editing them
- Check existing patterns before adding new code
- Verify module versions, outputs, and schemas from actual source

### Git Discipline
- **NEVER push** - Only commit. The user will push.
- **Never amend commits** - Create new commits for fixes
- **No emojis** in commits or documentation
- Use Conventional Commits format
- **NEVER commit `terraform/prd.tfvars` or `terraform/dev.tfvars`** - These contain environment-specific configuration and must not be committed. Only the `.example` files are committed.

### Before Committing (Required CI Steps)

**ALWAYS run these commands before committing any changes:**

```bash
# 1. Run unit tests (Lambda/Python)
make test-unit

# 2. Run UI tests (if any ui/ files changed)
make test-ui
# Note: Requires Node 20+. If local Node is older, tests will run in CI.
# When adding new API functions or modifying component props, update the
# vi.mock() blocks in the corresponding test files to include new exports.

# 3. Format and validate Terraform (if any .tf files changed)
make fmt
make validate-tf

# 4. Check Python formatting (if any .py files changed)
cd lambda && find . -name "*.py" -path "*/lambda/*" | head -20 | xargs -I {} python -m py_compile {}
```

**CI will fail if you skip these steps.** Run them every time before committing.

### Local Development
- Never run `terraform apply` locally - CI/CD only
- Use MCP tools (`query_knowledge_base`, `get_coding_standards`) to query the knowledge base

## When Investigating Issues

If generated code has problems (wrong versions, wrong patterns, missing standards):

1. **First check:** Is the relevant ADR in the KB? Query it.
2. **If ADR exists but wasn't used:** Check if the vector queries match the ADR content
3. **If ADR is missing:** The fix is creating/updating the ADR, not hardcoding logic
4. **Discuss before changing platform code** - Most fixes belong in documentation

## Project Structure

```
lambda/           # Lambda handlers (one dir per function)
lambda/tests/     # Centralized pytest tests
terraform/        # Infrastructure as Code
docs/             # Documentation and ADRs
scripts/          # CLI tools including outcome-ops-assist
ui/               # React frontend + Express proxy server
```

## Adding New API Endpoints

When adding new Lambda API endpoints, you must update **FOUR** places:

1. **Lambda handler** (`lambda/<function>/handler.py`)
   - Add the route in `lambda_handler()` function
   - Add the handler function

2. **FastAPI routes** (`lambda/<function>/main.py`)
   - Add the FastAPI route decorator that forwards to `lambda_handler`
   - **If you forget this step, the Lambda returns 404!**

3. **Express proxy server** (`ui/server/index.js`)
   - Add the route to proxy requests to the Lambda
   - Uses `proxyWorkspaceRequest()` for workspace Lambda routes
   - Uses `signedChatFetch()` for chat Lambda routes
   - **If you forget this step, the route returns HTML (index.html) instead of JSON!**

4. **Frontend API client** (`ui/src/lib/api.ts`)
   - Add the TypeScript function to call the API

Example for a new workspace-related endpoint `/foo/bar`:
```python
# In lambda/workspace-management/main.py
@app.get("/foo/bar")
async def foo_bar(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/foo/bar")
    result = lambda_handler(event, None)
    return _parse_response(result)
```

```javascript
// In ui/server/index.js
app.get("/api/foo/bar", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/foo/bar"));
```

## Questions and Escalation

If you encounter missing features, outdated docs, conflicting guidance, or unclear requirements: **Stop and ask.** Do not guess or work around issues silently.
