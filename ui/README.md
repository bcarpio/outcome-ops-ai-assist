# Chat UI - Enterprise Component

This directory contains the OutcomeOps Chat UI, a React-based web interface for interacting with the knowledge base.

## What This Component Provides

- **Real-time streaming chat** with Claude via Bedrock
- **Persistent conversations** stored in DynamoDB
- **RAG-augmented responses** grounded in organizational knowledge
- **Conversation memory** via S3 Vectors embeddings
- **Multi-turn dialogue** with context awareness

## Technology Stack

| Component | Technology |
|-----------|------------|
| Frontend | React + TypeScript + Vite |
| Styling | Tailwind CSS |
| Backend | Express.js proxy server |
| Hosting | AWS Fargate Spot + ALB |
| Auth | Azure AD OIDC (ALB integration) |
| API | Lambda Function URL (streaming) |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      User Browser                        │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTPS
                           ▼
┌─────────────────────────────────────────────────────────┐
│              Application Load Balancer                   │
│         (Internal or Public with OIDC Auth)             │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│               Fargate Spot (React + Express)            │
│                                                          │
│   ┌─────────────┐     ┌──────────────────────────┐     │
│   │  React App  │ ──► │  Express Proxy Server    │     │
│   │  (Chat UI)  │     │  (SigV4 signed requests) │     │
│   └─────────────┘     └────────────┬─────────────┘     │
└────────────────────────────────────┼────────────────────┘
                                     │ IAM Auth
                                     ▼
┌─────────────────────────────────────────────────────────┐
│          Lambda Function URL (chat-streaming)            │
│              Response Streaming via LWA                  │
└─────────────────────────────────────────────────────────┘
```

## Deployment Options

### Internal ALB (Default)
- VPC-only access via Direct Connect, Transit Gateway, or VPN
- No public internet exposure
- Enterprise customers provide network connectivity

### Public ALB with OIDC
- Internet-accessible with Azure AD authentication
- Suitable for dev/demo environments
- HTTPS with ACM certificate + Route53 DNS

## Enterprise Features

- **Streaming responses** - Real-time token-by-token output (bypasses API Gateway 30s limit)
- **OIDC authentication** - Azure AD integration at ALB level
- **IAM-secured API** - Lambda Function URLs with SigV4 signing
- **Conversation persistence** - Full chat history in DynamoDB
- **Knowledge-grounded** - RAG integration with organizational KB
- **Audit trail** - All conversations logged for compliance

## Configuration

Terraform variables for UI deployment:

```hcl
deploy_ui             = true
ui_vpc_id             = "vpc-xxxxxxxx"
ui_private_subnet_ids = ["subnet-xxx", "subnet-yyy"]
ui_container_image    = "123456789012.dkr.ecr.us-west-2.amazonaws.com/chat-ui:latest"

# For public ALB with OIDC:
ui_alb_internal = false
domain          = "example.com"
oidc_enabled    = true
oidc_client_id  = "azure-app-client-id"
oidc_tenant_id  = "azure-tenant-id"
```

## Support

This component is available only via licensed deployments.

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
