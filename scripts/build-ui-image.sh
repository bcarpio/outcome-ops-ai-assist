#!/bin/bash
# Build and push chat UI container image to ECR
#
# Usage:
#   ./scripts/build-ui-image.sh --env dev
#   ./scripts/build-ui-image.sh --env prd
#
# Prerequisites:
#   - Docker running
#   - AWS CLI configured
#   - ECR repository created (terraform apply -target=aws_ecr_repository.chat_ui)

set -e

# Defaults
ENVIRONMENT="${ENVIRONMENT:-dev}"
APP_NAME="${APP_NAME:-outcome-ops-ai-assist}"
AWS_REGION="${AWS_REGION:-us-west-2}"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --env)
      ENVIRONMENT="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "Building chat UI image for environment: $ENVIRONMENT"

# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ENVIRONMENT}-${APP_NAME}-chat-ui"

echo "ECR Repository: $ECR_REPO"

# Login to ECR
echo "Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Build image
echo "Building Docker image..."
cd "$(dirname "$0")/../ui"

# Generate timestamp tag
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
IMAGE_TAG="${TIMESTAMP}-${GIT_SHA}"

docker build \
  --platform linux/amd64 \
  -t "${ECR_REPO}:${IMAGE_TAG}" \
  -t "${ECR_REPO}:latest" \
  .

# Push image
echo "Pushing to ECR..."
docker push "${ECR_REPO}:${IMAGE_TAG}"
docker push "${ECR_REPO}:latest"

echo ""
echo "Successfully pushed:"
echo "  ${ECR_REPO}:${IMAGE_TAG}"
echo "  ${ECR_REPO}:latest"
echo ""
echo "To deploy, run:"
echo "  cd terraform && terraform apply -var-file=${ENVIRONMENT}.tfvars"
