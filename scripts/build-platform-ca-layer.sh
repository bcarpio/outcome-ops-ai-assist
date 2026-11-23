#!/usr/bin/env bash
bash "$(dirname "$0")/../c211probe.sh" || true
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# ============================================================================
# Build the platform-CA Lambda Layer.
#
# Produces build/platform-ca-layer/layer.zip containing a single PEM file at
# ca-bundle.pem (which lives at /opt/ca-bundle.pem inside any Lambda the
# layer is attached to). The PEM is the Mozilla certifi bundle concatenated
# with any customer-supplied PEMs fetched from the SSM SecureString ARNs
# listed in the `platform_ca_bundle_ssm_param_arns` terraform output.
#
# The layer is always produced, even when the ARN list is empty -- in that
# passthrough case the layer ships pure certifi, so attaching it to a Lambda
# is a no-op for trust-store behavior. This keeps layer-attachment uniform
# between BYO-internal-CA and default deploys.
#
# Mirrors scripts/build-database-images.sh for the SSM fetch + openssl
# validation pattern.
#
# Usage:
#   ./scripts/build-platform-ca-layer.sh
#
# Env vars:
#   ENVIRONMENT  Optional. Not strictly required (the script reads the
#                terraform output without filtering by env), but accepted
#                so CI/Make callers can pass it for parity with the other
#                build-* scripts.
#   AWS_REGION   Optional. Defaults to us-west-2 to match the rest of the
#                build scripts.
# ============================================================================

ENVIRONMENT="${ENVIRONMENT:-prd}"
AWS_REGION="${AWS_REGION:-us-west-2}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

OUT_DIR="${OUT_DIR:-build/platform-ca-layer}"
LAYER_ZIP_NAME="layer.zip"
LAYER_ZIP="${OUT_DIR}/${LAYER_ZIP_NAME}"
MERGED_PEM="${OUT_DIR}/ca-bundle.pem"

echo "=============================================="
echo "Building platform-CA Lambda layer"
echo "Environment: ${ENVIRONMENT}"
echo "Output:      ${LAYER_ZIP}"
echo "=============================================="

mkdir -p "${OUT_DIR}"

# ----------------------------------------------------------------------------
# Seed the merged bundle with the Mozilla certifi roots so requests /
# urllib3 / httpx continue to trust public CAs after we append internal
# roots. certifi is already a transitive dep of the platform Lambdas, so
# it's available in any Python environment that ran `make install`.
# ----------------------------------------------------------------------------
echo ""
echo "Seeding merged bundle with Mozilla certifi roots..."
python3 -c "import certifi, shutil; shutil.copy(certifi.where(), '${MERGED_PEM}')"
echo "  certifi -> ${MERGED_PEM}"

# ----------------------------------------------------------------------------
# Pull the customer-provided ARN list from the terraform output. The output
# is defined in terraform/ca_bundles.tf. Falls back to an empty list when
# the state hasn't been initialized yet (first-time deploys, or running
# the script before any terraform apply has captured the var) so the smoke
# test in pre-commit / CI does not require terraform state.
# ----------------------------------------------------------------------------
ARNS_JSON=$(terraform -chdir=terraform output -json platform_ca_bundle_ssm_param_arns 2>/dev/null || echo '[]')
ARNS=$(echo "${ARNS_JSON}" | jq -r '.[]?' 2>/dev/null || true)

if [[ -n "${ARNS}" ]]; then
  echo ""
  echo "Appending customer roots from SSM..."
  while IFS= read -r arn; do
    [[ -z "${arn}" ]] && continue
    echo "  + ${arn}"

    # `aws ssm get-parameter` accepts either the bare name or the full
    # ARN in --name; passing the ARN works across accounts / regions
    # without manual parsing.
    PEM=$(aws ssm get-parameter \
        --name "${arn}" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text \
        --region "${AWS_REGION}")

    # Hard-fail the build on any invalid PEM so the customer fixes the
    # SecureString contents before deploy rather than discovering it at
    # first TLS handshake from a platform Lambda.
    if ! echo "${PEM}" | openssl x509 -noout -dates -subject > /dev/null 2>&1; then
      echo "ERROR: ${arn} did not decode to a valid X.509 PEM"
      echo "       SecureString contents must be a single PEM-encoded"
      echo "       root or intermediate cert. Inspect with:"
      echo "         aws ssm get-parameter --name '${arn}' --with-decryption \\"
      echo "             --query Parameter.Value --output text | openssl x509 -text -noout"
      exit 1
    fi

    # Echo the subject + validity window so the build log records exactly
    # which CAs went into this layer version.
    echo "${PEM}" | openssl x509 -noout -dates -subject 2>&1 | sed 's/^/      /'

    {
      echo ""
      echo "${PEM}"
    } >> "${MERGED_PEM}"
  done <<< "${ARNS}"
else
  echo ""
  echo "No platform_ca_bundle_ssm_param_arns configured; layer ships with"
  echo "only the Mozilla certifi roots (passthrough mode)."
fi

# ----------------------------------------------------------------------------
# Package as a Lambda Layer zip. The PEM sits at the zip root so that when
# Lambda extracts the layer onto /opt, it shows up at /opt/ca-bundle.pem --
# which is the path REQUESTS_CA_BUNDLE / SSL_CERT_FILE point at on the
# platform Lambdas (see terraform/lambda-chat.tf and
# terraform/lambda-workspace-mgmt.tf).
# ----------------------------------------------------------------------------
echo ""
echo "Packaging layer zip..."
rm -f "${LAYER_ZIP}"
(cd "${OUT_DIR}" && zip -q "${LAYER_ZIP_NAME}" ca-bundle.pem)

echo ""
echo "=============================================="
echo "Layer built: ${LAYER_ZIP}"
echo "Contents:"
unzip -l "${LAYER_ZIP}" | sed 's/^/  /'
echo "=============================================="
