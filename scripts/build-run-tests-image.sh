#!/usr/bin/env bash
# ============================================================================
# build-run-tests-image.sh - Build and push run-tests Lambda container image
# ============================================================================
# Builds the multi-language test runner container and pushes to ECR.
#
# Usage:
#   ./scripts/build-run-tests-image.sh --env dev
#   ./scripts/build-run-tests-image.sh --env prd
#
# Prerequisites:
#   - Docker installed and running
#   - AWS CLI configured with appropriate permissions
#   - ECR repository created via: terraform apply -target=aws_ecr_repository.run_tests
#
# ============================================================================

set -euo pipefail

# Default values
ENVIRONMENT="${ENVIRONMENT:-dev}"
APP_NAME="${APP_NAME:-outcome-ops-ai-assist}"
AWS_REGION="${AWS_REGION:-us-west-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Show help
show_help() {
  cat << EOF
build-run-tests-image.sh - Build and push run-tests Lambda container image

USAGE:
  ./scripts/build-run-tests-image.sh --env <dev|prd> [options]

OPTIONS:
  --env ENV         Environment (required: dev or prd)
  --region REGION   AWS region (default: us-west-2)
  --no-push         Build only, don't push to ECR
  --no-cache        Disable Docker layer caching (rebuilds everything)
  --help, -h        Show this help message

EXAMPLES:
  ./scripts/build-run-tests-image.sh --env dev
  ./scripts/build-run-tests-image.sh --env prd --region us-east-1
  ./scripts/build-run-tests-image.sh --env dev --no-push

ENVIRONMENT VARIABLES:
  AWS_PROFILE       AWS profile to use for authentication
  AWS_REGION        AWS region (default: us-west-2)
  ENVIRONMENT       Environment name (dev, prd)
  APP_NAME          Application name (default: outcome-ops-ai-assist)

BOOTSTRAP PROCESS:
  1. First, create the ECR repository:
     cd terraform
     terraform workspace select dev
     terraform apply -var-file=dev.tfvars -target=aws_ecr_repository.run_tests

  2. Build and push the image:
     ./scripts/build-run-tests-image.sh --env dev

  3. Deploy the Lambda using the container:
     cd terraform
     terraform apply -var-file=dev.tfvars

  For production, replace 'dev' with 'prd' in all commands above.
EOF
  exit 0
}

# Parse arguments
NO_PUSH=false
NO_CACHE=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --env)
      ENVIRONMENT="$2"
      shift 2
      ;;
    --region)
      AWS_REGION="$2"
      shift 2
      ;;
    --no-push)
      NO_PUSH=true
      shift
      ;;
    --no-cache)
      NO_CACHE=true
      shift
      ;;
    --help|-h)
      show_help
      ;;
    *)
      echo "Error: Unknown argument: $1" >&2
      echo "Run './scripts/build-run-tests-image.sh --help' for usage" >&2
      exit 1
      ;;
  esac
done

# Validate environment
if [[ ! "$ENVIRONMENT" =~ ^(dev|prd)$ ]]; then
  echo "Error: Environment must be 'dev' or 'prd'" >&2
  exit 1
fi

# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  echo "Error: Failed to get AWS account ID. Check your AWS credentials." >&2
  exit 1
fi

# ECR repository URL
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ENVIRONMENT}-${APP_NAME}-run-tests"

# Generate unique image tag using git commit hash and timestamp
# Format: v1.0.0-abc1234-20251206123456
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
TIMESTAMP=$(date +%Y%m%d%H%M%S)
IMAGE_TAG="v1.0.0-${GIT_SHA}-${TIMESTAMP}"
FULL_IMAGE="${ECR_REPO}:${IMAGE_TAG}"

echo "=============================================="
echo "Building run-tests Lambda container image"
echo "=============================================="
echo "Environment:  ${ENVIRONMENT}"
echo "AWS Region:   ${AWS_REGION}"
echo "AWS Account:  ${AWS_ACCOUNT_ID}"
echo "ECR Repo:     ${ECR_REPO}"
echo "Image Tag:    ${IMAGE_TAG}"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
  echo "Error: Docker is not running. Please start Docker and try again." >&2
  exit 1
fi

# Build the image
echo "Building Docker image..."
cd "${PROJECT_ROOT}/lambda/run-tests"

# Copy shared module into build context (cleaned up after build)
cp -r "${PROJECT_ROOT}/lambda/shared" shared/
trap 'rm -rf "${PROJECT_ROOT}/lambda/run-tests/shared"' EXIT

# Build with or without cache based on --no-cache flag
CACHE_ARG=""
if [[ "$NO_CACHE" == "true" ]]; then
  echo "Cache disabled (--no-cache)"
  CACHE_ARG="--no-cache"
fi

docker build \
  ${CACHE_ARG} \
  --platform linux/amd64 \
  --provenance=false \
  -t "${FULL_IMAGE}" \
  -t "${ENVIRONMENT}-${APP_NAME}-run-tests:latest" \
  .

echo ""
echo "Build complete!"

if [[ "$NO_PUSH" == "true" ]]; then
  echo ""
  echo "Skipping push (--no-push specified)"
  echo "Local image: ${ENVIRONMENT}-${APP_NAME}-run-tests:latest"
  exit 0
fi

# Login to ECR
echo ""
echo "Logging in to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Push to ECR
echo ""
echo "Pushing image to ECR..."
docker push "${FULL_IMAGE}"

# Store the image tag in SSM Parameter Store for Terraform to reference
echo ""
echo "Storing image tag in SSM Parameter Store..."
SSM_PARAM_NAME="/${ENVIRONMENT}/${APP_NAME}/run-tests/image-tag"
aws ssm put-parameter \
  --name "${SSM_PARAM_NAME}" \
  --value "${IMAGE_TAG}" \
  --type String \
  --overwrite \
  --region "${AWS_REGION}"

echo ""
echo "=============================================="
echo "Success!"
echo "=============================================="
echo "Image pushed: ${FULL_IMAGE}"
echo "SSM Parameter: ${SSM_PARAM_NAME} = ${IMAGE_TAG}"
echo ""
echo "Next steps:"
echo "  cd terraform"
echo "  terraform workspace select ${ENVIRONMENT}"
echo "  terraform apply -var-file=${ENVIRONMENT}.tfvars"
echo ""
