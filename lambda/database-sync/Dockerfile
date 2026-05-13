# ============================================================================
# Database Sync Lambda Container Image
# ============================================================================
# SQS-driven worker for the database integration. Receives sync requests
# from DATABASE_SYNC_QUEUE, runs schema introspection against the customer
# DB, strips top_values, queues per-table YAML chunks to file-ingestion.
#
# Ships as a container image (not a Lambda layer + zip) because the
# schema introspection engine +
# DB drivers exceed Lambda's 250 MB unzipped layer cap.
#
# License-validator layer integration: Lambda container images CANNOT
# attach Lambda layers, so the license-validator module is baked into the
# image at build time. The build script downloads the license layer
# content via `aws lambda get-layer-version-by-arn` (presigned S3 URL) and
# unpacks it into /var/lang/lib/python3.12/site-packages/ via the
# build-time ARG below.
#
# Build:
#   ENVIRONMENT=dev make build-database-images
# ============================================================================

FROM public.ecr.aws/lambda/python:3.12

# Install the introspection engine + DB drivers (shared requirements with database-integration)
COPY lambda/db-introspection-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    find /var/lang/lib/python3.12/site-packages -name "tests" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    find /var/lang/lib/python3.12/site-packages -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    rm /tmp/requirements.txt

# Bake the OutcomeOps license validator layer content into the image.
# The build script downloads the layer zip (via get-layer-version-by-arn)
# and stages it at lambda/.licence-layer-staging/ before docker build, so
# this COPY just lifts it into the python site-packages path the validator
# expects (`outcomeops_license` module imports as if attached as a layer).
COPY lambda/.license-layer-staging/python/ /var/lang/lib/python3.12/site-packages/

# Lambda handler + shared module
COPY lambda/database-sync/ ${LAMBDA_TASK_ROOT}/
COPY lambda/shared/ ${LAMBDA_TASK_ROOT}/shared/

RUN find ${LAMBDA_TASK_ROOT} -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    find ${LAMBDA_TASK_ROOT} -name "Dockerfile" -delete 2>/dev/null || true

CMD [ "handler.lambda_handler" ]
