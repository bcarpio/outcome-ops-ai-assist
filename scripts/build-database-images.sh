#!/bin/bash
set -euo pipefail

# ============================================================================
# Build + push the database integration container images.
#
# Two images:
#   - database-integration : UI-facing picker / connection CRUD
#   - database-sync        : SQS worker that runs Databao introspection
#
# The database-sync image also bakes the OutcomeOps license validator
# layer content INTO the image (container-image Lambdas can't attach
# Lambda layers). We resolve the license layer ARN from the customer's
# tfvars file, download its content via get-layer-version-by-arn, unzip
# into lambda/.license-layer-staging/, then `docker build` copies that
# directory into the image's python site-packages.
#
# Usage:
#   ./scripts/build-database-images.sh --env dev
#   ./scripts/build-database-images.sh --env prd
# ============================================================================

ENVIRONMENT="${ENVIRONMENT:-}"
APP_NAME="${APP_NAME:-outcome-ops-ai-assist}"
AWS_REGION="${AWS_REGION:-us-west-2}"

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

if [[ -z "${ENVIRONMENT}" ]]; then
  echo "Error: --env or ENVIRONMENT required (dev|prd)"
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "=============================================="
echo "Building database integration container images"
echo "Environment: ${ENVIRONMENT}"
echo "=============================================="

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# ----------------------------------------------------------------------------
# Stage license validator layer for the sync image
# ----------------------------------------------------------------------------
# Resolve the layer ARN from tfvars. Customers set
# outcomeops_license_layer_arn = "arn:aws:lambda:...:layer:...:N" in their
# environment's tfvars file.
TFVARS="terraform/${ENVIRONMENT}.tfvars"
if [[ ! -f "${TFVARS}" ]]; then
  echo "Error: ${TFVARS} not found"
  exit 1
fi
LICENSE_LAYER_ARN=$(grep -E '^outcomeops_license_layer_arn\s*=' "${TFVARS}" | sed -E 's/.*=\s*"([^"]+)".*/\1/' | head -1)
if [[ -z "${LICENSE_LAYER_ARN}" ]]; then
  echo "Error: outcomeops_license_layer_arn not set in ${TFVARS}"
  echo "       The license validator must be baked into the sync image at build time."
  exit 1
fi

echo ""
echo "License layer: ${LICENSE_LAYER_ARN}"

STAGING_DIR="lambda/.license-layer-staging"
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

echo "Fetching license layer content (presigned URL)..."
LAYER_URL=$(aws lambda get-layer-version-by-arn \
  --arn "${LICENSE_LAYER_ARN}" \
  --query 'Content.Location' \
  --output text \
  --region "${AWS_REGION}")

TMP_ZIP=$(mktemp -t license-layer-XXXXXX.zip)
curl -sSL -o "${TMP_ZIP}" "${LAYER_URL}"
unzip -q -o "${TMP_ZIP}" -d "${STAGING_DIR}"
rm -f "${TMP_ZIP}"

# The Dockerfile expects ${STAGING_DIR}/python/ to exist with the
# outcomeops_license module inside. Some layer authoring conventions put
# things at the root; normalize either way.
if [[ ! -d "${STAGING_DIR}/python" ]]; then
  if [[ -d "${STAGING_DIR}/outcomeops_license" ]]; then
    mkdir -p "${STAGING_DIR}/python"
    mv "${STAGING_DIR}/outcomeops_license" "${STAGING_DIR}/python/"
  else
    echo "Error: license layer zip has neither python/ nor outcomeops_license/ at the root"
    ls -la "${STAGING_DIR}"
    exit 1
  fi
fi

echo "License layer staged at ${STAGING_DIR}/python/"

# ----------------------------------------------------------------------------
# ECR login (once for both images)
# ----------------------------------------------------------------------------
echo ""
echo "Logging in to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# ----------------------------------------------------------------------------
# Helper: build + push one image and write its tag to SSM
# ----------------------------------------------------------------------------
build_one() {
  local lambda_name="$1"
  local repo_name="${ENVIRONMENT}-${APP_NAME}-${lambda_name}"
  local dockerfile="lambda/${lambda_name}/Dockerfile"
  local image_tag="$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  local full_image="${REGISTRY}/${repo_name}:${image_tag}"
  local ssm_param="/${ENVIRONMENT}/${APP_NAME}/${lambda_name}/image-tag"

  echo ""
  echo "---- ${lambda_name} ----"
  echo "Building: ${full_image}"

  # Build context is the repo root so Dockerfile COPYs can grab
  # lambda/shared/, lambda/db-introspection-requirements.txt, and (for sync)
  # lambda/.license-layer-staging/.
  docker build \
    --platform linux/amd64 \
    --provenance=false \
    -f "${dockerfile}" \
    -t "${full_image}" \
    .

  echo "Pushing: ${full_image}"
  docker push "${full_image}"

  echo "Storing image tag in SSM: ${ssm_param} = ${image_tag}"
  aws ssm put-parameter \
    --name "${ssm_param}" \
    --value "${image_tag}" \
    --type String \
    --overwrite \
    --region "${AWS_REGION}" \
    > /dev/null
}

build_one database-integration
build_one database-sync

# ----------------------------------------------------------------------------
# Clean up staging
# ----------------------------------------------------------------------------
echo ""
echo "Cleaning license layer staging..."
rm -rf "${STAGING_DIR}"

echo ""
echo "=============================================="
echo "Both images built + pushed."
echo "=============================================="
echo ""
echo "Next: cd terraform && terraform apply -var-file=${ENVIRONMENT}.tfvars"
echo ""
