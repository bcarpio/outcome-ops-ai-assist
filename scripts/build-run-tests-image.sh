#!/bin/bash
# ============================================================================
# Build Run Tests Container Image - Enterprise Component
# ============================================================================
#
# This script builds the multi-language test runner container image and
# pushes it to ECR for use by the run-tests Lambda.
#
# The container includes:
# - Python 3.12 with pytest
# - Java 21 (Amazon Corretto) with Maven/Gradle
# - Node.js 20 with npm/yarn
# - Terraform CLI for IaC validation
# - Git for repository cloning
#
# Usage:
#   ./scripts/build-run-tests-image.sh --env dev
#   ./scripts/build-run-tests-image.sh --env prd
#
# Prerequisites:
# - Docker installed and running
# - AWS CLI configured with ECR push permissions
# - ECR repository created via Terraform
#
# The script:
# 1. Authenticates to ECR
# 2. Builds the container image with all runtimes
# 3. Tags with git commit SHA for immutability
# 4. Pushes to ECR
# 5. Updates SSM parameter with new image tag
#
# This component is available only via licensed deployments.
# For enterprise briefings: https://www.outcomeops.ai
# ============================================================================

echo "This is an enterprise component."
echo "Visit https://www.outcomeops.ai for deployment options."
exit 1
