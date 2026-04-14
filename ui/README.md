# OutcomeOps Chat UI

Real-time conversational RAG interface for querying organizational knowledge bases.

## Tech Stack

- **Frontend:** React + TypeScript + Vite + Tailwind CSS
- **Backend:** Express.js proxy with AWS SigV4 request signing
- **Authentication:** Azure AD OIDC via AWS ALB
- **Streaming:** Lambda Web Adapter (FastAPI + uvicorn) with NDJSON streaming
- **Deployment:** AWS Fargate with Application Load Balancer

## Pages

| Page | Description |
|------|-------------|
| **Chat** | Conversational RAG with streaming responses, source attribution, workspace-scoped search, MCP tool integration, conversation sharing, custom AI voices per workspace |
| **Workspaces** | Workspace listing with creation and management |
| **Workspace Settings** | Members, repos, documents, sharing, integrations (GitHub, Confluence, Jira, Outlook, Teams, SharePoint), custom system prompts |
| **Org Settings** | Organization member management, role assignments |
| **MCP Servers** | Configure and manage Model Context Protocol server connections |
| **Settings** | User preferences, API key management |

## Features

- Real-time streaming chat with markdown rendering (GFM tables, code blocks)
- Source attribution with expandable document references
- Workspace-scoped knowledge base queries with cross-workspace sharing
- Conversation history with pagination and search
- Conversation sharing (workspace-scoped, read-only for recipients)
- MCP server integration (SonarQube, Snyk, custom servers)
- Custom AI voices (system prompts) per workspace
- Advanced/Standard mode toggle for model selection
- Token usage and cost display
- AI disclosure modal for regulatory compliance
- Microsoft 365 connector management (Outlook, Teams, SharePoint)

## Deployment

The Chat UI is deployed as a Fargate service behind an ALB with OIDC authentication. See [UI Deployment Guide](../docs/ui-deployment.md) for details.

This is an enterprise component available via licensing only. See [https://www.outcomeops.ai](https://www.outcomeops.ai).
