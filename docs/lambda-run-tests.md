# Lambda: run-tests

Automated test runner that validates generated branches before OutcomeOps posts a PR update. This Lambda consumes `OutcomeOps.CodeGeneration.Completed` events, clones the branch associated with the issue, installs dependencies, runs `make test`, and reports the results back to EventBridge plus S3.

## Container Image

The run-tests Lambda uses a Docker container image to provide a multi-language test environment. The image is built from `lambda/run-tests/Dockerfile` and includes:

| Runtime | Version | Purpose |
|---------|---------|---------|
| Python | 3.12 | Lambda base, pytest, Python projects |
| Java | 21 (Corretto) | Maven/Gradle projects, Spring Boot |
| Node.js | 20 | TypeScript/JavaScript projects, npm |
| Terraform | 1.9.8 | Infrastructure validation |
| Git | 2.43.0 | Repository cloning (HTTPS support) |

### Building the Image

```bash
# Build and push to ECR (required before terraform apply)
# This automatically creates the ECR repository if it doesn't exist
ENVIRONMENT=prd make build-run-tests-image

# Or use the script directly (requires ECR to exist)
./scripts/build-run-tests-image.sh --env prd
```

## Customizing the Image

If your project requires additional packages (e.g., custom databases, build tools, or language runtimes), modify the Dockerfile at `lambda/run-tests/Dockerfile`.

### Adding System Packages

Add packages to the `dnf install` block in the final image section:

```dockerfile
# Install system packages
RUN dnf install -y \
    # ... existing packages ...
    # Add your packages here:
    postgresql-devel \    # PostgreSQL client libraries
    redis \               # Redis CLI
    graphviz \            # For documentation generation
    && dnf clean all
```

### Adding Python Packages

Add packages to `lambda/run-tests/requirements.txt`:

```
# Existing packages
pytest>=7.0.0
boto3>=1.34.0
pydantic>=2.0.0

# Add your packages:
psycopg2-binary>=2.9.0    # PostgreSQL driver
redis>=5.0.0              # Redis client
celery>=5.3.0             # Task queue
```

### Adding Node.js Packages

Add global npm packages to the Dockerfile:

```dockerfile
# Install global npm packages for TypeScript
RUN npm install -g \
    typescript \
    ts-node \
    @types/node \
    # Add your packages:
    jest \
    eslint \
    prettier
```

### Adding Other Languages

Example: Adding Go support:

```dockerfile
# Install Go
ARG GO_VERSION=1.21.5
RUN curl -sL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz
ENV PATH=/usr/local/go/bin:${PATH}
```

Example: Adding Ruby support:

```dockerfile
# Install Ruby
RUN dnf install -y ruby ruby-devel && \
    gem install bundler
```

### After Customizing

After modifying the Dockerfile:

```bash
# Rebuild and push the image
ENVIRONMENT=prd make build-run-tests-image

# Redeploy Lambda to use the new image
cd terraform
terraform apply -var-file=prd.tfvars
```

## Trigger & Event Flow

1. `generate-code` finishes executing all plan steps and creates a PR.
2. It emits an EventBridge event:
   - `source`: `outcomeops.generate-code`
   - `detail-type`: `OutcomeOps.CodeGeneration.Completed`
   - `detail.environment`: matches the Terraform workspace (`dev` or `prd`)
   - `detail` payload includes repo, branch, PR number, plan file, and latest commit SHA.
3. EventBridge rule on the environment-specific bus invokes `run-tests`.
4. `run-tests`:
   - Clones the repository via HTTPS using the GitHub PAT from SSM.
   - Installs project dependencies (detected automatically: pip, npm, maven, gradle).
   - Executes `make test` from repo root.
   - Writes command logs and (if present) `lambda/junit.xml` to `s3://{ENV}-{app}-kb/test-results/...`.
   - Emits `OutcomeOps.Tests.Completed` back onto the same EventBridge bus with pointers to the artifacts.

## Environment Variables

| Name | Description |
| --- | --- |
| `ENV` | Environment name (`dev` / `prd`) |
| `APP_NAME` | Application name (defaults to `outcome-ops-ai-assist`) |
| `TEST_RESULTS_BUCKET` | S3 bucket for log/junit uploads (shared with knowledge base) |
| `TEST_RESULTS_PREFIX` | Folder prefix inside the bucket (`test-results`) |
| `GITHUB_TOKEN_PARAM` | SSM path for the GitHub PAT (default `/{ENV}/{APP_NAME}/github/token`) |
| `EVENT_BUS_NAME` | EventBridge bus name (`{ENV}-{APP}-bus`) |
| `TEST_COMMAND` | Command executed to run tests (default `make test`) |
| `MAX_COMMAND_SECONDS` | Timeout applied to shell commands (default `900`) |

## IAM Requirements

The Terraform module grants the Lambda role:

- `ssm:GetParameter` + `kms:Decrypt` for `/{ENV}/{APP}/*` secrets.
- `s3:PutObject` to `${knowledge_base_bucket}/*` for log + junit uploads.
- `events:PutEvents` to the automation bus for publishing `OutcomeOps.Tests.Completed`.
- `bedrock:InvokeModel` for auto-fix functionality (import and syntax error fixes).

## Failure Handling

- Git clone, dependency installs, and `make test` output are captured line-by-line.
- On any failure the Lambda still uploads the command log, includes the exit codes, and emits a failure event that downstream automation can route back to Claude.
- Workspaces under `/tmp` are always cleaned up to avoid disk pressure.

## Local Validation

1. Build the container image: `ENVIRONMENT=prd make build-run-tests-image`
2. Deploy with Terraform: `terraform apply -var-file=prd.tfvars`
3. (Optional) Invoke the Lambda using `aws lambda invoke --function-name prd-outcome-ops-ai-assist-run-tests ...` with a sample EventBridge payload (see below).
4. Inspect uploaded artifacts under `s3://prd-outcome-ops-ai-assist-kb/test-results/...`.

### Sample Event Payload

```json
{
  "source": "outcomeops.generate-code",
  "detail-type": "OutcomeOps.CodeGeneration.Completed",
  "detail": {
    "issueNumber": 6,
    "issueTitle": "Add list-recent-docs handler for KB verification",
    "repoFullName": "bcarpio/outcome-ops-ai-assist",
    "branchName": "6-lambda-add-list-recent-docs-handler-for-kb-verific",
    "baseBranch": "main",
    "prNumber": 123,
    "prUrl": "https://github.com/bcarpio/outcome-ops-ai-assist/pull/123",
    "planFile": "issues/6-lambda-add-list-recent-docs-handler-for-kb-verific-plan.md",
    "commitSha": "abc123",
    "environment": "dev",
    "appName": "outcome-ops-ai-assist",
    "eventVersion": "2024-11-09"
  }
}
```

### Test Result Event

`run-tests` publishes:

```json
{
  "source": "outcomeops.run-tests",
  "detail-type": "OutcomeOps.Tests.Completed",
  "detail": {
    "issueNumber": 6,
    "branchName": "6-lambda-add-list-recent-docs-handler-for-kb-verific",
    "repoFullName": "bcarpio/outcome-ops-ai-assist",
    "prNumber": 123,
    "prUrl": "https://github.com/bcarpio/outcome-ops-ai-assist/pull/123",
    "status": "passed",
    "success": true,
    "testCommand": "make test",
    "durationSeconds": 215.3,
    "artifactBucket": "prd-outcome-ops-ai-assist-kb",
    "artifactPrefix": "test-results/issue-6/branch-6-lambda-add-list-recent-docs-handler-for-kb-verific/20241109T190000Z",
    "logObjectKey": ".../test-output.log",
    "junitObjectKey": ".../junit.xml",
    "environment": "dev",
    "appName": "outcome-ops-ai-assist",
    "setupExitCode": 0,
    "testExitCode": 0,
    "eventVersion": "2024-11-09"
  }
}
```

Downstream automation (e.g., PR commenters or Claude fix-up Lambda) can subscribe to this event to pull logs or mark the branch ready for review.

<!-- Confluence sync -->
