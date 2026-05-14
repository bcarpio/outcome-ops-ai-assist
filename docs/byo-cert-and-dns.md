# BYO TLS Certificate + BYO DNS

The platform's UI listener and every MCP server share a single ALB. By
default, terraform owns DNS (in Route53 in this deployment account)
and the TLS cert (ACM public DNS-validated). Two orthogonal escape
hatches let you take over either or both:

- `manage_dns_in_route53` — when `false`, you wire DNS yourself.
- `ui_acm_certificate_arn` — when set, terraform uses your cert.
- `platform_ca_bundle_ssm_param_arns` — required when the cert above
  is signed by a private / internal CA.

These three variables are defined in `terraform/variables.tf` and
`terraform/ca_bundles.tf`. Example values are commented out in
`terraform/prd.tfvars.example` and `terraform/dev.tfvars.example`.

## When to use what

| `manage_dns_in_route53` | `ui_acm_certificate_arn` | `platform_ca_bundle_ssm_param_arns` | Behavior |
|---|---|---|---|
| `true` (default) | `""` (default) | `[]` (default) | Default path. Hosted zone lookup, ACM public cert auto-issued, A-ALIAS auto-created. |
| `true` | set (public CA) | `[]` | Auto-A-ALIAS, listener uses your ARN. Useful for org-wide ACM wildcards. |
| `false` | set (public CA) | `[]` | All R53 + cert-validation skipped. Outputs `alb_dns_name` + `fqdn_to_cname` for external DNS wiring. |
| `true` or `false` | set (internal CA) | set | Same as the row above (or default DNS) plus a Lambda layer baked with your CA roots, attached to every platform Lambda that egresses to the ALB. |
| `false` | `""` | any | **Invalid.** `terraform plan` fails on the `aws_acm_certificate.ui` precondition with: "manage_dns_in_route53 = false requires ui_acm_certificate_arn to be set — terraform cannot DNS-validate a public ACM cert without a Route53 zone in this account." |
| any | set (internal CA) | `[]` | **Permitted but broken at runtime.** `chat`, `workspace-management`, and `mcp-server` Lambda calls to the UI ALB will fail TLS verification because the customer root isn't in the layer. Terraform can't introspect cert chains, so this can't be enforced in HCL — set `platform_ca_bundle_ssm_param_arns` whenever the cert is internally signed. |

## Scenario walkthroughs

### Scenario A: default path (Route53 + ACM public)

Default. `var.domain` already resolves to a Route53 hosted zone in
this deployment account. Set nothing extra in `prd.tfvars`:

```hcl
domain = "example.com"
# manage_dns_in_route53        = true   # default
# ui_acm_certificate_arn       = ""     # default - terraform issues
# platform_ca_bundle_ssm_param_arns = []  # default - no extra CA roots
```

Terraform looks up the hosted zone, requests an ACM public
DNS-validated cert for `local.fqdn`, writes the DVO CNAMEs into the
zone, waits for validation, attaches the cert to the HTTPS listener,
and creates an A-ALIAS pointing `local.fqdn` at the ALB.

### Scenario B: BYO public cert + Route53 (org-wide ACM wildcard)

Customer already has an ACM public wildcard cert in the same region
covering `*.example.com`. Route53 for `example.com` lives in this
deployment account.

```hcl
domain                 = "example.com"
manage_dns_in_route53  = true   # default
ui_acm_certificate_arn = "arn:aws:acm:us-west-2:111122223333:certificate/abc-..."
```

Terraform skips ACM issuance + DVO record creation, attaches the
provided ARN to the listener, and still creates the A-ALIAS. ACM
public certs cannot be referenced cross-region, so the wildcard ARN
must live in the same region as the deployment.

### Scenario C: BYO public cert + external DNS

Customer's DNS for `example.com` lives outside this deployment
account — cross-account Route53, Infoblox, AD-DNS, NS1, BIND, etc.
Cert is publicly trusted (imported to ACM in this region, or
auto-issued ACM if the customer's own procurement uses ACM in this
account).

```hcl
domain                 = "example.com"
manage_dns_in_route53  = false
ui_acm_certificate_arn = "arn:aws:acm:us-west-2:111122223333:certificate/abc-..."
```

Terraform skips every Route53 resource and emits three outputs you
need to wire DNS externally:

```bash
terraform -chdir=terraform output alb_dns_name     # CNAME target
terraform -chdir=terraform output alb_zone_id      # for cross-account R53 A-ALIAS
terraform -chdir=terraform output fqdn_to_cname    # the source name
```

Wiring options:

- **Cross-account Route53**: in the account that owns the hosted
  zone, create an A-record with alias target set to `alb_dns_name`
  and alias hosted zone ID set to `alb_zone_id`. This is the
  AWS-native, no-extra-cost path.
- **Infoblox / AD-DNS / NS1 / BIND**: create a CNAME from
  `fqdn_to_cname` to `alb_dns_name`. Standard external-DNS flow.

### Scenario D: BYO internal-CA cert + external DNS

Customer mints the leaf cert from a private PKI (Microsoft AD CS,
HashiCorp Vault PKI, Sectigo Private CA, internal Venafi, etc.) and
will not pay for a public cert for an internal-only platform.

1. Import the leaf cert + the intermediate chain into ACM in this
   region. ACM accepts private-CA chains; only the auto-issuance
   path requires public roots.
2. Put the corporate root PEM (and any non-public intermediates) into
   SSM Parameter Store as `SecureString` parameters in this account.
   One parameter per root is fine.
3. Reference them in `prd.tfvars`:

   ```hcl
   domain                 = "example.com"
   manage_dns_in_route53  = false   # or true if R53 is in this account
   ui_acm_certificate_arn = "arn:aws:acm:us-west-2:111122223333:certificate/internal-..."
   platform_ca_bundle_ssm_param_arns = [
     "arn:aws:ssm:us-west-2:111122223333:parameter/prd/outcome-ops-ai-assist/ca/corp-root",
   ]
   ```

4. From a fresh checkout (or after rotating any of the referenced SSM
   parameters), rebuild the platform CA Lambda layer and apply:

   ```bash
   ENVIRONMENT=prd make build-platform-layer
   cd terraform && terraform apply -var-file=prd.tfvars
   ```

   The layer build fetches each SSM parameter, validates the PEM with
   `openssl x509 -noout -dates` (build fails on malformed input),
   concatenates everything onto the upstream Mozilla certifi bundle,
   and packages the result so the in-Lambda path is
   `/opt/ca-bundle.pem`. The terraform layer resource hashes the zip,
   bumps `aws_lambda_layer_version` on content change, and points
   `chat`, `workspace-management`, and `mcp-server` at the new
   version via `REQUESTS_CA_BUNDLE` + `SSL_CERT_FILE`.

5. Wire DNS externally as in Scenario C, then read the webhook
   compatibility section below before enabling GitHub Issue -> MR.

## Webhook compatibility

GitHub Issue -> MR is the only platform feature that takes inbound
webhooks from a public-internet vendor. GitHub's servers will reject
TLS to your platform if the cert chain doesn't trust back to a public
root.

If you need Issue -> MR and you're on Scenario D:

1. (Easy) Disable Issue -> MR. Most regulated customers don't enable
   it in early rollout.
2. (Practical) Use a publicly-trusted cert instead — Scenario B or C.
3. (Future) Stand up a separate webhook-receiver FQDN with a
   public-CA cert that forwards events to the internal endpoint. Not
   supported out-of-the-box; talk to us.

No other integration (Confluence, Jira, Outlook, Teams, SharePoint,
OneDrive, OneNote) takes inbound webhooks from a public vendor —
they're all polling-based or use internal-only flows that don't care
about the customer's TLS trust boundary.

## Rotation runbook

### Rotating an ACM-issued cert (Scenario A)

Automatic. ACM rotates DNS-validated certs roughly 60 days before
expiry as long as the DVO records stay in the Route53 zone. No
operator action.

### Rotating a BYO public cert (Scenarios B + C)

1. Customer obtains the new cert via their normal procurement.
2. Imports it to ACM in this region, or relies on the existing
   wildcard's own auto-rotation if the cert was issued by ACM.
3. Updates `ui_acm_certificate_arn` in their tfvars if the ARN
   changes. Wildcards usually keep the same ARN across rotations, in
   which case nothing needs to change in tfvars.
4. `terraform apply`. Brief listener config change, no downtime.

### Rotating a BYO internal-CA cert + root (Scenario D)

1. Customer mints the new leaf cert from their internal CA.
2. Imports leaf + chain to ACM in this region. Note the new ARN.
3. (Only if the internal CA *root* itself rotated) update the SSM
   SecureString contents pointed to by
   `platform_ca_bundle_ssm_param_arns` with the new root PEM. The
   parameter ARN stays the same; only the value changes.
4. Update `ui_acm_certificate_arn` if the leaf ARN changed.
5. From CI or operator laptop:

   ```bash
   ENVIRONMENT=prd make build-platform-layer
   cd terraform && terraform apply -var-file=prd.tfvars
   ```

   The layer rebuild picks up the SSM content change, the zip's
   `filebase64sha256` flips, terraform creates a new layer version,
   and the three Lambda modules update to point at it.

## OAuth callback registration

After your FQDN resolves to the ALB (regardless of which DNS path
you took), grab the OAuth callback URLs from terraform outputs and
SSM:

```bash
terraform -chdir=terraform output | grep -i callback
aws ssm get-parameters-by-path \
    --path "/prd/outcome-ops-ai-assist/oauth" \
    --recursive --with-decryption \
    --query "Parameters[?contains(Name, 'callback-url')].[Name,Value]" \
    --output table
```

Register each in the corresponding IdP / OAuth app:

- `/prd/outcome-ops-ai-assist/github/callback-url` -> GitHub App
  settings.
- `/prd/outcome-ops-ai-assist/oauth/atlassian/callback-url` ->
  Atlassian OAuth app (Confluence).
- `/prd/outcome-ops-ai-assist/oauth/atlassian/jira-callback-url` ->
  Atlassian OAuth app (Jira).
- `/prd/outcome-ops-ai-assist/oauth/microsoft/callback-url` ->
  Azure AD app registration (Outlook).
- `/prd/outcome-ops-ai-assist/oauth/microsoft/onedrive-callback-url`,
  `.../sharepoint-callback-url`, `.../teams-callback-url`,
  `.../onenote-callback-url` -> Azure AD app registration for the
  respective M365 integration.

These URLs all derive from `local.fqdn` so they're correct regardless
of the DNS path. They are only usable once DNS actually resolves —
nothing in `terraform apply` validates that the IdP can reach the
FQDN.

## Lambdas that need the platform CA layer

The platform CA Lambda layer built by
`scripts/build-platform-ca-layer.sh` is attached to every Lambda that
calls back into the UI ALB over HTTPS. As of this writing, three
Lambdas:

- `chat` — calls MCP servers during chat sessions (uses `requests`).
- `workspace-management` — calls MCP servers during registration and
  tool discovery (uses `requests`).
- `mcp-server` — calls downstream MCP servers via the gateway pattern
  (uses stdlib `urllib.request`, which only honors `SSL_CERT_FILE`,
  not `REQUESTS_CA_BUNDLE` — both env vars are set on all three
  Lambdas to cover `requests`, `httpx`, and stdlib TLS).

If you add a new Lambda that egresses to `local.fqdn`, attach the
layer and set both `REQUESTS_CA_BUNDLE=/opt/ca-bundle.pem` and
`SSL_CERT_FILE=/opt/ca-bundle.pem`. Forgetting the layer manifests as
silent TLS verification 500s only on BYO-internal-CA deploys, so the
failure mode skips local dev and most public-cert customers — easy
to miss in review.

## Terraform version note

The chunk-01 precondition that enforces "if you turn DNS off you must
BYO a cert" lives on `aws_acm_certificate.ui` rather than as a
`validation {}` block on the variable itself. Cross-variable
references inside `validation {}` require Terraform 1.9+, and this
repo pins `required_version = ">= 1.5"`. The precondition fires on
the same plan path the variable validation would have, just with the
error message attributed to the ACM resource instead of the variable.
If the floor is ever raised to 1.9+, the check can move onto the
variable.
