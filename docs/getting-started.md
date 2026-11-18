# Getting Started

This is the onboarding quick-start: prerequisites, the GitHub token, and the
first-deploy outline. The **authoritative, step-by-step deploy + Day-2
operations** live in the [Deployment Guide](deployment-guide.md) — this page
hands off to it rather than duplicating (and drifting from) it.

## Prerequisites

### Tools on your machine

These steps assume a Unix-like shell — macOS, Linux, or **Windows via WSL2**
(Ubuntu). WSL2 is a fully supported environment; run everything below inside the
WSL2 distribution, not PowerShell.

1. **Python 3.12+**
   ```bash
   pyenv install 3.12 && pyenv local 3.12
   ```
   Using your distro's packaged Python instead of pyenv is fine (Ubuntu 24.04
   already ships 3.12), but `make setup` builds C-extension dependencies that
   need the Python **development headers** (`Python.h`) plus a C toolchain —
   without them the install fails with fatal `Python.h: No such file or
   directory` errors. On Ubuntu/WSL2, install them first:
   ```bash
   sudo apt install python3.12-dev build-essential zip
   ```
   (`zip` is also used by `make pre-deploy` to package the Lambda layers — a
   vanilla Ubuntu image doesn't ship it, and the layer build fails without it.)
   (pyenv-built interpreters bundle these headers, so pyenv users can skip this.)
2. **Terraform 1.5+** — install with your platform's package manager per the
   [HashiCorp install guide](https://developer.hashicorp.com/terraform/install):
   `apt` on Ubuntu/WSL2, `brew install terraform` on macOS.
3. **AWS CLI v2, configured** — `aws configure` (or an SSO/Identity Center
   profile). Every step from the state bucket onward uses it.
4. **Docker** — the layer/image builds (`make pre-deploy`) compile inside
   Amazon Linux 2 / Python 3.12 containers for Lambda ABI compatibility, so a
   running Docker daemon is required.
5. **GitHub Personal Access Token** — see [Creating the GitHub token](#creating-the-github-token) below.

### Things you must obtain before you can deploy

These are not in the repo and will block `terraform plan`/`apply` if missing:

- **A VPC with ≥2 subnets in different AZs** — the Chat UI **and** the MCP
  server are deployed **by default** (`deploy_ui` and `mcp_server_enabled`
  default to `true`) and both run on Fargate in your VPC, so `ui_vpc_id` and
  `ui_private_subnet_ids` are **required** — `terraform` errors if they're
  unset. The subnets may be **public or private** (despite the variable name):
  set `ui_fargate_assign_public_ip = true` for public/IGW subnets (no NAT — the
  cheap path), or `false` for private subnets with NAT/Transit Gateway/Direct
  Connect. See the Deployment Guide's network topology.
- **A domain + ACM/IAM TLS certificate** — for the UI's ALB (BYO DNS + cert).
- **Azure AD (Entra ID) app registration** — the UI authenticates via Azure AD
  OIDC. Set this up per [Azure AD Setup](azure-ad-setup.md) before enabling a
  public ALB. (For a headless deploy, set `deploy_ui = false` and
  `mcp_server_enabled = false` explicitly to skip the UI/MCP and the last three.)
- **Putting the UI on your own hostname** (custom domain + TLS) is a post-apply
  step — note that a custom domain turns on HTTPS *and* Azure AD OIDC together.
  See [Deployment Guide → Binding the UI to a Custom Domain](deployment-guide.md#binding-the-ui-to-a-custom-domain-tls--azure-ad-oidc).

### Creating the GitHub token

OutcomeOps needs a GitHub PAT to read repos/issues, create branches/commits/PRs,
and post PR comments.

1. [GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)](https://github.com/settings/tokens)
2. **Generate new token (classic)**, name it (e.g. "OutcomeOps AI Assist — Dev"),
   set an expiration.
3. Select scopes:

   | Scope | Required | Purpose |
   |-------|----------|---------|
   | `repo` | Yes | Read/write code, branches, PRs on private repos |
   | `workflow` | If using GitHub Actions | Trigger workflow runs |

4. **Generate token** and copy it immediately.

**Fine-grained PAT alternative:** select specific repositories, with Contents,
Issues, and Pull requests = Read and write (Metadata read-only is automatic).

## Quick Start

> First deploy is roughly **30–45 minutes** end to end — most of it is
> `make pre-deploy` building six container images and the Lambda layers, not
> the Terraform apply. This is an outline; the
> [Deployment Guide → Initial Deployment](deployment-guide.md#initial-deployment)
> is the authoritative, gated sequence.

### 1. Get the code

OutcomeOps delivers the platform source into **your own** repository — GitHub,
GitLab, or Azure DevOps — and the exact mechanism varies by customer. If you're
reading these docs you already have the repo by some means; clone your copy. If
not, email [support@outcomeops.ai](mailto:support@outcomeops.ai) for access.

```bash
git clone <your-outcomeops-repo-url>
cd outcome-ops-ai-assist        # or whatever your repo is named
```

### 2. Python environment

```bash
make setup                 # creates the repo-root venv/ and installs deps
source venv/bin/activate
```

### 3. Terraform state bucket + backend

Create an S3 bucket for state (per account + region):

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="us-west-2"
BUCKET_NAME="terraform-state-${ACCOUNT_ID}-${REGION}"

# us-east-1 is the S3 default and REJECTS a LocationConstraint; every other
# region REQUIRES one. Branch on it so the command works in any region.
if [ "$REGION" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION"
else
  aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION"
fi
aws s3api put-bucket-versioning --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET_NAME" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Then create your backend config from the template (`backend.tf` is gitignored —
it points at *your* bucket):

```bash
cp terraform/backend.tf.example terraform/backend.tf
# edit terraform/backend.tf: set bucket + region to yours
```

### 4. Configure tfvars

```bash
cp terraform/prd.tfvars.example terraform/prd.tfvars
# Edit terraform/prd.tfvars. FOUR variables have NO default -- you MUST set them:
#   aws_region, environment, ui_vpc_id,
#   ui_private_subnet_ids (>= 2 subnets across AZs).
# The UI + MCP server + workspaces deploy BY DEFAULT (deploy_ui,
#   mcp_server_enabled, enable_workspaces = true) -- that's why the VPC/subnets
#   are required; set any to false to skip that surface.
# PUBLIC subnets? also add ui_fargate_assign_public_ip = true. Cheap trial? add
#   fargate_enable_spot = true. Then ALB mode + which integrations.
```

Store your GitHub token in SSM:

```bash
aws ssm put-parameter --type SecureString --overwrite \
  --name /prd/outcome-ops-ai-assist/github/token \
  --value "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### 5. Initialize, select the workspace, then build

The build targets run targeted `terraform apply`s that need the backend
(step 3) and tfvars (step 4) in place **first**. They select/create the `prd`
workspace themselves, but their `terraform init` runs with output discarded —
so a missing `backend.tf` silently falls back to local state. Running
`terraform init` yourself here surfaces that before the build hides it, and
leaves the `prd` workspace selected for step 6.

```bash
cd terraform
terraform init                 # uses the backend.tf from step 3
terraform workspace new prd    # match `environment` in tfvars; `select prd` after first time
cd ..
ENVIRONMENT=prd make pre-deploy   # all layers + images, into the prd workspace's state
```

Rebuild via `pre-deploy` (or a specific `make build-*` target) whenever you pull
code that changes a handler/`requirements.txt`, edit a Dockerfile, or rotate a CA
SecureString.

### 6. Deploy

Init + workspace are done (step 5), so plan and apply. Full gated sequence and
rationale: [Deployment Guide → Initial Deployment](deployment-guide.md#initial-deployment).

> **The venv must be ACTIVE for this step.** `terraform apply` packages each
> Lambda by shelling out to `python3.12 -m pip install` (from `PATH`). If the
> venv isn't active it uses your *system* Python — and on Ubuntu 24.04 that
> interpreter is PEP-668 "externally-managed," so the pip install fails and
> **every** Lambda archive errors with `zip: Error during zip archive creation`
> / `CalledProcessError`. Activate it first:
> ```bash
> source venv/bin/activate      # from the repo root
> which python3.12              # sanity: should be .../venv/bin/python3.12
> ```

```bash
cd terraform
terraform plan  -var-file=prd.tfvars -out=terraform.prd.out   # ADR-004: always plan to a file
terraform apply terraform.prd.out                             # apply the reviewed plan
```

If `apply` reports the **saved plan is stale** ("Saved plan is stale" / the plan
no longer matches state — common after the build step or a partial first apply),
just re-run the `plan -out` and `apply` again; it's expected, not an error.

**After apply — accept Bedrock marketplace agreements** for the models you use;
third-party models (e.g. Cohere Rerank) fail *silently* without them. See
[Deployment Guide → Bedrock Model Access](deployment-guide.md#bedrock-model-access-and-marketplace-agreements).
Then overwrite the SSM SecureString placeholders for your enabled integrations.

### 7. Verify the deploy

Confirm the platform actually came up in **your** account. (A green unit-test
run only proves the repo is internally consistent — something you had before
you started; it does not tell you the deploy worked.) Replace `prd` with your
environment:

```bash
# 1. Lambdas landed with the code you just built -- catches a stale-tree apply:
aws lambda get-function-configuration \
  --function-name prd-outcome-ops-ai-assist-chat-streaming --query CodeSha256 --output text

# 2. Backend reachable -- grab the URLs the apply emitted, then hit them:
terraform output

# 3. UI + auth: open the UI URL in a browser and complete Azure AD sign-in
#    (public-ALB deploys). Internal-ALB deploys: reach it over your VPC path.
```

See [Health Check and Monitoring](deployment-guide.md#health-check-and-monitoring)
for the full set (MCP health, CloudWatch, audit alerting).

**Optional — verifying the _code_ (only if you'll modify the repo):** run the
developer suite, the same gates CI enforces:

```bash
make fmt && make validate-tf && make test-unit && make test-ui && make audit
```

## Configure the code-generation trigger

OutcomeOps can be triggered from **GitHub Issues** or **Jira**.

| Trigger | Best for | Setup |
|---------|----------|-------|
| GitHub Issues | GitHub-native teams | Webhook only |
| Jira | Jira-based teams | Automation rule + webhook |

### Option A — GitHub Issues

**1. Create a webhook secret**

```bash
WEBHOOK_SECRET=$(openssl rand -hex 32)
echo "Save this for the GitHub webhook config: $WEBHOOK_SECRET"
aws ssm put-parameter --type SecureString --overwrite \
  --name /prd/outcome-ops-ai-assist/github/webhook-secret --value "$WEBHOOK_SECRET"
```

**2. Configure the repo webhook**

Repo **Settings → Webhooks → Add webhook**:
- **Payload URL:** `https://YOUR_API_GATEWAY_URL/webhooks/github`
- **Content type:** `application/json`
- **Secret:** the value above
- **Events:** "Let me select individual events" → **Issues** only
- **Active:** checked

**Usage:** label an issue `approved-for-generation` (full code-gen → PR) or
`approved-for-plan` (plan-only PR, cheap, for testing).

### Option B — Jira

See the [Jira Integration Setup Guide](jira-integration-setup.md). The GitHub
PAT is still required (it creates the branches/commits/PRs).

## Trigger PR analysis (CI, no public webhook)

PR analysis (`analyze-pr` -> `process-pr-check`: the ADR / README / test-coverage /
breaking-change / duplication / license checks, posted as review comments including
inline line-level comments) is triggered by **your CI invoking the Lambda directly
via OIDC** -- deliberately not a public webhook. The product runs in your own AWS
account, so an internet-facing webhook endpoint would be unwanted inbound attack
surface, and GitHub Enterprise Server runners often cannot reach one; your CI
already holds a scoped identity, so it calls the Lambda directly.

See ADR-053 for the rationale. OutcomeOps is identity-agnostic -- it does not know
or care how you provision CI identities. Two options:

**1. Bring your own CI role.** You already have (or centrally provision) a role your
CI assumes. Give OutcomeOps its ARN; it grants that role invoke on its own
functions (a resource-policy grant) -- your role is untouched and stays generic:

```hcl
ci_invoker_role_arns = ["arn:aws:iam::<ACCOUNT_ID>:role/<your-ci-role>"]
```

Accepts any principal, however provisioned -- a cross-account role from your own
hub/spoke CI setup, a single-account OIDC role, a CI instance role. OutcomeOps
needs no knowledge of that setup. IAM-only apply (one statement per role on
`analyze-pr` + `handle-command`).

**2. Let OutcomeOps create the role.** No CI role to point at? Have this stack build
a GitHub-OIDC-assumable, invoke-only role in the deployment account, for OutcomeOps
only:

```hcl
ci_oidc_role_enabled    = true
ci_oidc_github_subjects = ["repo:<your-org>/<your-repo>:pull_request",
                           "repo:<your-org>/<your-repo>:ref:refs/heads/main"]
# ci_oidc_create_provider = false   # if the account already has the GitHub OIDC provider
# ci_oidc_provider_arn    = "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
```

After `terraform apply`, use the `ci_oidc_role_arn` output as the workflow's
`AWS_OIDC_ROLE_ARN` secret.

**2. Add the workflow to each repo you want analyzed.** Copy
`.github/workflows/pr-analysis.yml` into the repo and set the repo's
`AWS_OIDC_ROLE_ARN` secret (and the `prd` GitHub Environment). It invokes
`analyze-pr` on `pull_request` (opened / synchronize / reopened). The sibling
`.github/workflows/pr-commands.yml` (PR slash-commands -> `handle-command`) uses the
same OIDC direct-invoke mechanism.

If your CI identity is a hub/spoke (`AWS_OIDC_ROLE_ARN` is a hub role that can
only assume a workload-account role), also set a repo/org variable
`CI_SPOKE_ROLE_ARN` to that workload role's ARN -- the workflow then assumes it
(role-chaining) before invoking. Leave the variable unset for a single-account /
direct-OIDC role, where the first assume already holds invoke rights.

## Optional: MCP servers

### OutcomeOps MCP (Claude Code, VS Code, Cursor)

1. `mcp_server_enabled = true` in `prd.tfvars`, then
   `ENVIRONMENT=prd make build-mcp-server-image` and re-apply.
2. Generate an API key in the UI: **Settings → API Keys**.
3. Add it to your client:
   ```bash
   claude mcp add --transport http outcomeops \
     https://your-outcomeops-domain.com/mcp/v1 \
     --header "Authorization: Bearer ooa_your_key_here"
   ```

Full guide: [OutcomeOps MCP Server](mcp-server.md).

### SonarQube MCP (code-quality tools in chat)

```bash
aws ssm put-parameter --type SecureString \
  --name /prd/outcome-ops-ai-assist/mcp/sonarqube-token --value "your-token"
```
```hcl
mcp_sonarqube_enabled = true
mcp_sonarqube_url     = "https://sonarcloud.io"
mcp_sonarqube_org     = "your-org-key"   # SonarCloud only
```
Full guide: [SonarQube MCP Server Setup](sonarqube-mcp.md). Org admins assign
MCP servers to workspaces in the UI after deploy.

## Development workflow

This project follows
[ADR-002: Development Workflow Standards](adr/ADR-002-development-workflow.md).
Before committing:

```bash
# the CI gate, before committing:
make fmt && make validate-tf && make test-unit && make test-ui && make audit
```

Commit with Conventional Commits; branch and open a PR (direct pushes to `main`
require a PR per the repo ruleset).

## Operations & troubleshooting

Deploy verification, monitoring, credential rotation, backups/PITR, fault
handling, and recovery procedures are all in the
**[Deployment Guide](deployment-guide.md)** — it is the single source of truth
for Day-2. For architecture and data flows, see
[Architecture Overview](architecture.md).

## Next steps

- [Architecture Overview](architecture.md) — system design and data flows
- [Deployment Guide](deployment-guide.md) — full deploy + operations
- [Testing Environment](testing-environment.md) — stand up an isolated `dev`
  deploy to validate a new release before applying it to production
- [ADR-001: Creating ADRs](adr/ADR-001-create-adrs.md) and
  [ADR-002: Development Workflow](adr/ADR-002-development-workflow.md)
- [SonarQube MCP Server](sonarqube-mcp.md) — code-quality tools in chat

<!-- Confluence sync -->
