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
# Stage customer-provided CA bundles (BYO trust roots for non-RDS DBs)
# ----------------------------------------------------------------------------
# Customers connecting to MSSQL backed by a CA the AWS RDS global bundle
# doesn't cover (Azure SQL with corporate roots, on-prem MSSQL signed by
# an internal CA, GCP Cloud SQL for MSSQL, etc.) can register one or more
# SSM Parameter Store SecureString ARNs via the
# customer_ca_bundle_ssm_param_arns tfvar. We fetch each here, validate
# the PEM with openssl x509, and write the bundles into
# lambda/.ca-bundles-staging/customer-<name>.pem so the database image
# Dockerfiles can COPY them into /etc/pki/ca-trust/source/anchors/ and
# update-ca-trust will bake them into the system trust store.
#
# The directory always exists (lambda/.ca-bundles-staging/.gitkeep ships
# in the repo); we just rm -f any stale *.pem from a previous run so a
# rotated-out bundle can't leak forward into the next image.
CA_STAGING_DIR="lambda/.ca-bundles-staging"
mkdir -p "${CA_STAGING_DIR}"
rm -f "${CA_STAGING_DIR}"/*.pem

CA_ARNS_JSON=$(terraform -chdir=terraform output -json customer_ca_bundle_ssm_param_arns 2>/dev/null || echo '[]')
CA_ARNS=$(echo "${CA_ARNS_JSON}" | jq -r '.[]?' 2>/dev/null || true)

if [[ -n "${CA_ARNS}" ]]; then
  echo ""
  echo "Staging customer CA bundles from SSM..."
  while IFS= read -r ca_arn; do
    [[ -z "${ca_arn}" ]] && continue
    # The leaf segment after the final '/' makes a stable file name; the
    # `customer-` prefix prevents collision with aws-rds-global-bundle.pem.
    name="$(basename "${ca_arn}")"
    out="${CA_STAGING_DIR}/customer-${name}.pem"

    # `aws ssm get-parameter` accepts either the full ARN or the bare
    # parameter name in --name; passing the ARN works across accounts /
    # regions without parsing it manually.
    if ! aws ssm get-parameter \
        --name "${ca_arn}" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region "${AWS_REGION}" > "${out}"; then
      echo "ERROR: failed to fetch SecureString from ${ca_arn}"
      echo "       Verify the ARN is correct and the deploy role has"
      echo "       ssm:GetParameter + kms:Decrypt on the encrypting key."
      rm -f "${out}"
      exit 1
    fi

    # Hard-fail the build on any invalid PEM so the customer fixes the
    # SecureString contents before deploy rather than discovering it at
    # first TLS handshake.
    if ! openssl x509 -in "${out}" -noout -dates -subject > /dev/null 2>&1; then
      echo "ERROR: ${ca_arn} did not decode to a valid X.509 PEM"
      echo "       SecureString contents must be a single PEM-encoded"
      echo "       root or intermediate cert. Inspect with:"
      echo "         aws ssm get-parameter --name '${ca_arn}' --with-decryption \\"
      echo "             --query Parameter.Value --output text | openssl x509 -text -noout"
      rm -f "${out}"
      exit 1
    fi

    # Echo the validated cert summary so the build log records exactly
    # which CA went into this image (subject + validity window).
    echo "  + ${ca_arn}"
    openssl x509 -in "${out}" -noout -dates -subject 2>&1 | sed 's/^/      /'
  done <<< "${CA_ARNS}"
  echo "Customer CA bundles staged at ${CA_STAGING_DIR}/"
else
  echo ""
  echo "No customer_ca_bundle_ssm_param_arns configured; only the AWS"
  echo "RDS global bundle will be baked into the database images."
fi

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

# Keep the directory (and its .gitkeep) but drop any fetched PEMs so a
# rotated-out cert doesn't sit on the local disk longer than necessary.
# The next build rebuilds from SSM regardless.
rm -f "${CA_STAGING_DIR}"/*.pem

echo ""
echo "=============================================="
echo "Both images built + pushed."
echo "=============================================="
echo ""
echo "Next: cd terraform && terraform apply -var-file=${ENVIRONMENT}.tfvars"
echo ""
