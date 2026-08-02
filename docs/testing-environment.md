# Testing Environment (dev workspace)

The main deployment docs deploy your **production** environment (`prd`). This
guide covers the optional **testing environment** — a second, fully isolated
deploy you can stand up to validate a new OutcomeOps release, a config change,
or a new integration **before** you apply it to production.

You do not need this to run OutcomeOps. Reach for it when you want a safe place
to try something first.

## What it is

The platform ships two environment profiles, each driven by its own tfvars file
and its own Terraform **workspace**:

| | Production | Testing |
|---|---|---|
| tfvars | `terraform/prd.tfvars` | `terraform/dev.tfvars` |
| Terraform workspace | `prd` | `dev` |
| Resource names | `prd-outcome-ops-ai-assist-*` | `dev-outcome-ops-ai-assist-*` |
| SSM parameter paths | `/prd/outcome-ops-ai-assist/*` | `/dev/outcome-ops-ai-assist/*` |
| Hostname (default) | `outcomeops-prd.<domain>` | `outcomeops-dev.<domain>` |
| Terraform state | `prd` workspace | `dev` workspace (same backend) |

Because every resource name, SSM path, hostname, and state file is prefixed by
the environment, the two deployments are **fully isolated** — a change in `dev`
cannot touch `prd`. They can live in the **same AWS account** (cheapest, most
common) or in a **separate account** if your controls require harder isolation;
nothing in the platform assumes one or the other.

> **Hostnames.** The `-dev` / `-prd` suffix comes from
> `append_environment_to_fqdn` (default `true`). Production typically sets it to
> `false` for a clean `outcomeops.<domain>`; leave it `true` in `dev.tfvars` so
> the testing UI gets its own `outcomeops-dev.<domain>` hostname and its own ACM
> certificate.

## The workspace guard (why this is safe)

Terraform is configured to **hard-fail** if the selected workspace does not
match the `environment` in the tfvars you passed:

```
BLOCKED: Terraform workspace 'prd' does not match environment 'dev'.
Run 'terraform workspace select dev' to fix.
```

So you cannot accidentally apply `dev.tfvars` onto production or vice versa —
the mismatch is caught before any plan runs. Always keep the workspace and the
tfvars in agreement: **`dev` workspace ↔ `dev.tfvars`**, **`prd` workspace ↔
`prd.tfvars`**.

## First-time setup

1. **Create the tfvars** from the example and fill it in:

   ```bash
   cp terraform/dev.tfvars.example terraform/dev.tfvars
   # edit terraform/dev.tfvars — same shape as prd.tfvars, with dev values
   # (domain/subdomain, VPC/subnets, feature flags). Keep
   # append_environment_to_fqdn = true so the hostname is outcomeops-dev.<domain>.
   ```

   `dev.tfvars` is gitignored (like `prd.tfvars`); only the `.example` is
   committed.

2. **Store the dev secrets** at the `/dev/...` SSM paths (they are separate from
   production's `/prd/...` paths). For example the OIDC client secret:

   ```bash
   aws ssm put-parameter \
     --name "/dev/outcome-ops-ai-assist/oidc/client-secret" \
     --type SecureString \
     --value "your-dev-client-secret"
   ```

   You can reuse the same Azure AD app registration as production, or register a
   separate one — just add the dev redirect URI (`https://outcomeops-dev.<domain>/oauth2/idpresponse`)
   to whichever app you use.

3. **Select (or create) the dev workspace:**

   ```bash
   cd terraform
   terraform workspace select dev || terraform workspace new dev
   ```

4. **Build images/layers for dev** (only if you deploy the UI / MCP servers /
   database integration — the build targets are environment-aware):

   ```bash
   ENVIRONMENT=dev make build-all-images
   ENVIRONMENT=dev make build-all-layers
   ```

5. **Plan to a file, review, apply** (ADR-004 — never a bare `apply`):

   ```bash
   terraform plan  -var-file=dev.tfvars -out=terraform.dev.out
   terraform apply terraform.dev.out
   ```

   Keep the Python venv **active** for the apply — Terraform packages each
   Lambda during apply.

## Testing a new release before production

This is the main reason the testing environment exists:

1. Check out the release you want to validate (a tag, or `origin/main`) in your
   working tree, and `git pull`.
2. Apply it to **dev** first:

   ```bash
   cd terraform && terraform workspace select dev
   ENVIRONMENT=dev make build-all-images      # if container images changed
   terraform plan  -var-file=dev.tfvars -out=terraform.dev.out
   terraform apply terraform.dev.out
   ```

3. Smoke-test on `outcomeops-dev.<domain>`: sign in, run a chat query, exercise
   any integration or feature the release touches, and check CloudWatch logs.
4. Only once dev is green, apply the **same** checkout to production:

   ```bash
   terraform workspace select prd
   ENVIRONMENT=prd make build-all-images       # if container images changed
   terraform plan  -var-file=prd.tfvars -out=terraform.prd.out
   terraform apply terraform.prd.out
   ```

Because both environments are built from the same code, a clean dev apply is
strong evidence the prd apply will be clean too — the difference is only the
tfvars values.

## Cost and teardown

The testing environment is **real infrastructure and costs real money** while it
runs (Fargate tasks, ALB, NAT if any, DynamoDB, etc.). Tear it down when you are
done validating:

```bash
cd terraform && terraform workspace select dev
terraform plan  -var-file=dev.tfvars -destroy -out=terraform.dev.destroy.out
terraform apply terraform.dev.destroy.out
```

Destroying the `dev` workspace never touches `prd` — separate names, separate
state. The `dev` SSM secrets you created are not managed by Terraform; delete
them separately if you want a fully clean teardown.

## See also

- [Getting Started](getting-started.md) — production deployment.
- [Deployment Guide](deployment-guide.md) — full production reference.
- [Azure AD Setup](azure-ad-setup.md) — OIDC configuration (applies to both
  environments; use the matching `/dev/` or `/prd/` SSM paths).
