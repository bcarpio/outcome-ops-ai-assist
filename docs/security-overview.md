# Security Overview

OutcomeOps AI Assist is deployed entirely within the customer's own AWS account. There is no shared infrastructure, no cross-customer data access, and no OutcomeOps-operated backend. All data, compute, and infrastructure are owned and controlled by the customer. Authentication uses Azure AD with OIDC for the UI and OAuth 2.0 JWTs for the MCP server.

## Key Features

- Customer-owned deployment with full data residency control
- Azure AD integration for enterprise identity (OIDC + OAuth 2.0)
- TLS 1.3 encryption for all traffic via ALB
- Server-side encryption (AES-256) for S3, DynamoDB, SQS, and ECR
- KMS-managed encryption for CloudWatch Logs
- IAM least-privilege policies for all Lambda functions and services
- Private subnets for ECS Fargate workloads
- SSM Parameter Store with SecureStrings for secrets management

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
