# OutcomeOps: Context Engineering for AI-Assisted Development

[![CI/CD](https://github.com/bcarpio/outcome-ops-ai-assist/actions/workflows/cicd.yml/badge.svg)](https://github.com/bcarpio/outcome-ops-ai-assist/actions/workflows/cicd.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

AI generates generic code. You need code that matches **YOUR** standards.

**The problem:** Engineers spend hours adapting AI-generated code to organizational patterns, standards, and architecture decisions.

**The solution:** Give AI access to your organizational knowledge -- ADRs, code-maps, architectural decisions -- so it generates code that already matches your patterns.

**The proof:**
- 16-hour tasks to 15 minutes
- $2-$4 per feature
- 100-200x ROI
- Production-tested at Fortune 500

This is Context Engineering. This is OutcomeOps.

[Learn more about the OutcomeOps Methodology](https://www.outcomeops.ai)

---

## The Problem With AI Tools Today

**Current AI coding tools:**
- Generate generic implementations
- Don't know your architectural decisions
- Ignore your coding standards
- Require hours of manual adaptation
- **You still spend most of your time rewriting AI code**

**OutcomeOps is different:**
- Understands YOUR codebase patterns
- Knows YOUR architectural decisions (via ADRs)
- Generates code matching YOUR standards
- Includes tests automatically
- **AI does the work. You do the review.**

---

## How OutcomeOps Works

**1. Ingest Organizational Knowledge**
- ADRs (Architecture Decision Records)
- Code-maps (repo structure and patterns)
- Existing code examples
- Standards documentation
- Confluence, Jira, SharePoint, Outlook, Teams content

**2. Vector Search + Reranking**
- When a feature is requested (Jira story, GitHub issue)
- OutcomeOps queries your knowledge base via semantic vector search
- Reranks results using Cohere Rerank 3.5 for precision ordering
- Finds the most relevant patterns, decisions, and examples

**3. AI Generation with Context**
- Claude generates code using YOUR context
- Matches YOUR patterns, not generic templates
- Includes tests based on YOUR testing standards
- Creates MR/PR with full implementation

**4. Automated Review**
- MR analyzer checks against standards
- Validates architectural compliance
- License compliance checking
- Comments on deviations
- **Code that's 90% done when you first see it**

[See detailed architecture](docs/architecture.md)

---

## Platform Capabilities

**Chat UI** - Real-time conversational RAG interface with streaming responses, source attribution, workspace-scoped access, conversation sharing, and custom AI voices per workspace.

**MCP Server** - Model Context Protocol integration for Claude Code, VS Code, Cursor, and other MCP-compatible tools. Proxy support for third-party MCP servers (SonarQube, Snyk).

**Multi-Tenant Workspaces** - Workspace isolation with role-based access control, cross-workspace knowledge sharing, and org-level administration.

**Microsoft 365 Integration** - Ingest knowledge from Outlook emails, Teams messages, SharePoint files and pages, Confluence spaces, and Jira issues.

**Security** - Azure AD OIDC authentication, KMS encryption at rest, audit trail logging, AI disclosure modal for regulatory compliance, ExpectedBucketOwner on all S3 operations.

---

## Get Started

**Prerequisites:**
- GitHub repo with some code
- AWS account (for Lambda deployment)
- Azure AD tenant (for authentication)
- ADRs or architectural docs (or we'll help you create them)

**Ready to get started?**

OutcomeOps is a commercial platform licensed for enterprise use. [Contact us](https://www.linkedin.com/in/briancarpio/) to discuss licensing and implementation for your organization.

Once licensed, deployment takes approximately 5 minutes:
- Deploy infrastructure to your AWS account
- Configure Azure AD OIDC
- Configure GitHub webhook
- Create test issue with label "approved-for-generation"
- Watch OutcomeOps generate a PR in ~15 minutes

**New to ADRs?** [Start here](docs/getting-started-with-adrs.md)

**Want to see it in action first?** [Watch demo video](docs/demo.md)

---

## OutcomeOps vs Other AI Coding Tools

**GitHub Copilot / Cursor:** Autocomplete and chat, but no organizational context. Generates generic code requiring manual adaptation to your standards.

**Devin:** Autonomous agent for tasks, but doesn't know YOUR patterns. Still generates code that needs refactoring to match your conventions.

**ChatGPT / Claude (standalone):** Powerful, but requires pasting context every time. Not scalable. No integration with your codebase.

**OutcomeOps:** Ingests YOUR ADRs, YOUR patterns, YOUR standards. AI generates code that already matches your organization. Review outcomes, not syntax.

---

## Who Built This

OutcomeOps was created by Brian Carpio, who previously built:

- **Golden Pipelines (2014)** - Took deployment from 6 weeks to 1 week at Aetna
- **AWS ProServe** - Led largest Healthcare & Life Sciences engagement in ProServe history
- **Platform Engineering** - Before it had a name, building self-service infrastructure at Pearson (2012), Aetna (2014), Comcast (2018)

OutcomeOps applies the same playbook to AI-assisted development:
- Make the easy path the right path
- Bake standards into infrastructure
- Create velocity through automation
- **This is golden pipelines for code generation**

[Read the full story](docs/backstory.md)

---

## Documentation

**Getting Started**
- [Getting Started Guide](docs/getting-started.md) - Prerequisites, setup, first steps
- [Getting Started with ADRs](docs/getting-started-with-adrs.md) - Learn about Architecture Decision Records
- [Demo Video](docs/demo.md) - See OutcomeOps in action

**Core Documentation**
- [Architecture Overview](docs/architecture.md) - System design and data flows
- [Technical Reference](docs/technical-reference.md) - Detailed technical documentation
- [CLI Usage Guide](docs/cli-usage.md) - Using the outcome-ops-assist CLI
- [Deployment Guide](docs/deployment-guide.md) - Operations and troubleshooting

**Integrations**
- [OutcomeOps MCP Server](docs/mcp-server.md) - Connect Claude Code, VS Code, and other MCP clients
- [SonarQube MCP Server](docs/sonarqube-mcp.md) - Code quality and security tools via MCP
- [Snyk MCP Server](docs/snyk-mcp.md) - Security vulnerability scanning via MCP
- [Azure AD Setup](docs/azure-ad-setup.md) - OIDC authentication configuration
- [GitHub App Setup](docs/github-app-setup.md) - Webhook and repo access
- [Jira Integration](docs/jira-integration-setup.md) - Issue-driven code generation

**Compliance**
- [AI Disclosure Modal](docs/ai-disclosure-modal.md) - Pre-use AI transparency notice for regulatory compliance
- [Security Overview](docs/security-overview.md) - Security architecture and controls

**Lambda Documentation**
- [Lambda: Query KB](docs/lambda-query-kb.md) - RAG orchestrator (vector search + reranking + answer generation)
- [Lambda: Ask Claude](docs/lambda-ask-claude.md) - RAG answer generation
- [Lambda: Generate Code](docs/lambda-generate-code.md) - AI-powered code generation from issues
- [Lambda: Analyze PR](docs/lambda-analyze-pr.md) - PR analysis orchestration
- [Lambda: Process PR Check](docs/lambda-process-pr-check.md) - PR check worker
- [Lambda: Run Tests](docs/lambda-run-tests.md) - Executes `make test` for generated branches

**Architecture Decision Records**
- [ADR-001: Creating ADRs](docs/adr/ADR-001-create-adrs.md) - How to document architectural decisions
- [ADR Template](docs/adr/TEMPLATE.md) - Template for new ADRs

**For Developers**
- [Backstory](docs/backstory.md) - Why OutcomeOps exists

---

## Project Structure

```
lambda/           # 38 Lambda handlers (one dir per function)
terraform/        # Infrastructure as Code (AWS)
docs/             # Documentation and ADRs
scripts/          # CLI tools including outcome-ops-assist
ui/               # React Chat UI (Fargate deployment)
diagrams/         # Architecture diagrams
```

---

## Get Started

**Ready to implement OutcomeOps?**

- **[Learn the Methodology](https://www.outcomeops.ai)** - Understand the OutcomeOps approach
- **[Learn ADRs](docs/getting-started-with-adrs.md)** - New to Architecture Decision Records?
- **[Watch Demo](docs/demo.md)** - See OutcomeOps in action
- **[Technical Documentation](docs/getting-started.md)** - Implementation guide for licensed users
- **Licensing & Support** - [Get in touch](https://www.linkedin.com/in/briancarpio/)

**For Licensed Customers:**
- Technical support and implementation assistance
- Custom feature development
- Training and onboarding
- Architecture consultation

---

**Built for engineering velocity.**

Outcome-driven development. Infrastructure by code. Engineering outcomes by AI. Your vision, automated execution.
