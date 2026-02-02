#!/bin/bash
# ============================================================================
# Build Chat UI Container Image - Enterprise Component
# ============================================================================
#
# This script builds the Chat UI container image and pushes it to ECR
# for deployment on Fargate.
#
# The container includes:
# - Node.js 20 for the Express proxy server
# - Built React application (Vite production build)
# - Nginx or Express for serving static files
# - SigV4 signing for Lambda Function URL authentication
#
# Usage:
#   ENVIRONMENT=dev make build-ui-image
#   ENVIRONMENT=prd make build-ui-image
#
# Prerequisites:
# - Docker installed and running
# - AWS CLI configured with ECR push permissions
# - ECR repository created via Terraform (deploy_ui = true)
#
# The script:
# 1. Installs npm dependencies
# 2. Runs Vite production build
# 3. Builds container image with Express server
# 4. Authenticates to ECR
# 5. Pushes image with :latest tag
#
# This component is available only via licensed deployments.
# For enterprise briefings: https://www.outcomeops.ai
# ============================================================================

echo "This is an enterprise component."
echo "Visit https://www.outcomeops.ai for deployment options."
exit 1
