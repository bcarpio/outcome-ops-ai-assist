# ============================================================================
# Database Integration Lambda Container Image
# ============================================================================
# UI-facing Lambda for the database integration. Handles:
#   - validate-connection (engine connection check)
#   - list-schemas (engine-driven metadata introspection for the schema picker)
#   - connection CRUD against DynamoDB
#
# Ships as a container image (not a Lambda layer + zip) because the
# schema introspection engine + Postgres/MySQL/MSSQL drivers total over 350 MB unzipped
# - well past Lambda's 250 MB unzipped layer cap. Container images allow
# up to 10 GB.
#
# Build:
#   ENVIRONMENT=dev make build-database-images
# ============================================================================

FROM public.ecr.aws/lambda/python:3.12

# Build context is repo root (`docker build -f lambda/database-integration/Dockerfile .`)
# so we can also COPY lambda/shared/ into the image.

# Install the schema introspection engine plus the DB driver set the MVP
# supports. Drivers bring native components that the AWS-published
# Lambda Python image will compile against the runtime ABI.
COPY lambda/db-introspection-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    find /var/lang/lib/python3.12/site-packages -name "tests" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    find /var/lang/lib/python3.12/site-packages -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    rm /tmp/requirements.txt

# Lambda handler + shared module live alongside under /var/task. The
# Python runtime adds /var/task to PYTHONPATH automatically.
COPY lambda/database-integration/ ${LAMBDA_TASK_ROOT}/
COPY lambda/shared/ ${LAMBDA_TASK_ROOT}/shared/

# Strip __pycache__ from the COPY'd code
RUN find ${LAMBDA_TASK_ROOT} -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true && \
    find ${LAMBDA_TASK_ROOT} -name "Dockerfile" -delete 2>/dev/null || true

CMD [ "handler.lambda_handler" ]
