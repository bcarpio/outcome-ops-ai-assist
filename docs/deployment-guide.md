# Deployment Guide

OutcomeOps AI Assist deploys entirely into your own AWS account using Terraform. The deployment guide covers prerequisites, supported regions, cost estimation, license management, health checks, monitoring, backup and recovery, credential rotation, patching, and fault handling procedures.

## Key Features

- Deploys into the customer's own AWS account with no shared infrastructure
- Fully parameterized Terraform IaC with environment-specific tfvars
- Support for multiple AWS regions with Bedrock model availability
- Built-in health check and monitoring configuration
- Automated backup and recovery with DynamoDB PITR and S3 versioning
- Credential rotation procedures for GitHub tokens and AWS keys
- Upgrade path with zero-downtime patching
- Detailed cost estimation based on usage patterns

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
