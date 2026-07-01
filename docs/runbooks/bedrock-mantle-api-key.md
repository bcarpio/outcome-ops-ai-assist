# Bedrock Mantle API key — operator setup and rotation runbook

The platform can dispatch chat, code-generation, PR analysis, and
knowledge-base synthesis calls to either Anthropic Claude models
(via Bedrock Converse) or OpenAI GPT-5-family models (via Bedrock
Mantle Responses). Claude works over standard IAM. GPT-5-family and
xAI Grok require a **long-term Bedrock API key** stored in AWS Systems
Manager Parameter Store. This runbook walks the platform operator
through the one-time provisioning and the recurring rotation.

Time budget:
- **First-time provisioning:** ~15 minutes of console work + up to
  several business days waiting for AWS Sales to clear model
  entitlement (GPT-5 family only; Grok and other Mantle families are
  self-serve).
- **Rotation:** ~5 minutes.

## 1. What this does

The Responses API on Bedrock Mantle authenticates with a bearer token
issued to your AWS account, not with SigV4/IAM. The platform's Lambda
runtime loads that bearer token from SSM at every cold start and
attaches it to every Bedrock Mantle HTTP request through the OpenAI
SDK. Without this key, any tfvars pointing at an `openai.*` or `xai.*`
model returns `Mantle API key … is unconfigured` and the affected
Lambda fails loudly (per [ADR-035](../adr/ADR-035-bedrock-converse-vs-responses-dispatch.md)'s
loud-failure stance).

Explicitly NOT in scope for this runbook:

- **Claude-only deploys.** If your tfvars only reference
  `anthropic.*` model IDs, you can skip this runbook entirely — the
  Converse path uses standard IAM and never touches the key. The
  SSM parameter's absence is only a problem for Mantle-routed
  requests.
- **Bedrock inference profile setup.** Provisioning cross-region
  inference profiles and reserved throughput is separate; this
  runbook only covers the bearer-token surface Mantle needs.
- **Anthropic direct API keys.** Some documentation online refers to
  Anthropic-issued keys for `api.anthropic.com`. The platform does
  not use those; Claude access is via Bedrock IAM.

## 2. Prerequisites

- **AWS account with Bedrock enabled** in the region the platform
  deploys to (`aws_region` in your tfvars). For an HA deploy with
  `secondary_region` set (see [ADR-022](../adr/ADR-022-multi-region-active-passive.md)),
  Bedrock must be enabled in BOTH regions.
- **IAM permissions to create Bedrock long-term API keys.** The
  managed policy `AmazonBedrockFullAccess` covers this; the specific
  API is `bedrock:CreateApiKey`. If your organization uses least-
  privilege custom roles, request that action in the console-user
  role you'll use for the console steps below.
- **IAM permissions to write SSM SecureString parameters** at the
  path the platform reads from (see §4 for the exact path).
- **A KMS key** for encrypting the SSM SecureString value. Either
  the default `aws/ssm` AWS-managed key or your organization's
  customer-managed KMS key — the platform's Lambda IAM policy
  already grants `kms:Decrypt` on `alias/aws/ssm`; if you use a
  CMK, extend the Lambda role's key policy to grant Decrypt.
- **Approval to use GPT-5 family models on Bedrock.** This is an
  AWS Sales / account-team engagement, not a console click. Skip to
  §3 if you don't already know whether your account is entitled.

## 3. Confirm or request AWS Sales entitlement (GPT-5 family only)

**Skip this section entirely if you are only enabling non-OpenAI
Mantle models** — Grok, Qwen, DeepSeek, Mistral, MoonshotAI, ZAI,
MiniMax families are self-serve on Mantle and do not require an
entitlement gate. **This section applies only to `openai.gpt-5.5`,
`openai.gpt-5.4`, and `openai.codex-*`.**

The GPT-5 family carries a per-model commercial entitlement that AWS
Sales controls on a per-account basis. A brand-new account with
correct IAM will still receive `401 access_denied` from Mantle until
this entitlement clears. This gate is NOT visible in:

- The Bedrock console's "Model access" page
- The IAM Policy Simulator
- `aws bedrock list-foundation-models`

To confirm your account status, run this probe from a machine with
your account's credentials configured (adjust the region to your
deploy target):

```bash
curl -s -X POST \
  https://bedrock-mantle.us-east-2.api.aws/openai/v1/responses \
  -H "Authorization: Bearer <your-bedrock-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai.gpt-5.5","input":"ok","max_output_tokens":16,"reasoning":{"effort":"none"}}'
```

- If the response body contains a valid `output_text`, entitlement
  is clear — skip to §4.
- If the response body is `{"error":{"code":"access_denied", …}}`
  with HTTP 401, entitlement is NOT clear.

To request entitlement, contact your AWS account team (Technical
Account Manager, Solutions Architect, or Account Manager). Reference
the OpenAI-on-Bedrock GA announcement (June 2026) and ask specifically
for `openai.gpt-5.5` invoke entitlement (and `openai.gpt-5.4` /
`openai.codex-*` if you want them). Clearing time historically has
been anywhere from a same-day to a several-business-day turnaround
depending on account context.

Once cleared, no code or terraform change is required — the Lambda
picks up the change on its next cold start (bearer-token auth happens
per-invocation).

## 4. Generate a Bedrock long-term API key

1. Sign in to the **AWS Console** in the region the platform deploys
   to (`aws_region` in your tfvars — typically `us-east-2` or
   `us-west-2`).
2. Navigate to **Amazon Bedrock → API keys → Long-term API keys**.
   Console URL:
   `https://<region>.console.aws.amazon.com/bedrock/home?region=<region>#/api-keys/long-term/create`
3. Click **Create long-term API key**.
4. **Name**: `outcomeops-ai-assist-mantle` (or a name that identifies
   this as the platform's key so future operators know not to
   revoke it during unrelated cleanup).
5. **Associated IAM user or role**: pick the IAM identity the key
   should inherit permissions from. The platform's Lambda execution
   roles already have `bedrock:InvokeModel*` grants, so this identity
   only needs to reach Bedrock — no additional service permissions
   required.
6. **Expiration**: choose an expiration that aligns with your
   organization's secret rotation policy. AWS supports up to
   `no expiration`, but regulated environments typically require
   90 days maximum. **Set a calendar reminder to rotate before
   expiration** — an expired key produces `401 access_denied` from
   Mantle exactly like the entitlement gate does, so it can be
   easy to misdiagnose.
7. Click **Create**.
8. **Copy the API key value immediately.** AWS displays it exactly
   once. If you close the panel without copying, you must delete
   the key and start over.

The key format is a long bearer token starting with `ABSK…`. Treat
this value as a first-class secret: anyone with the string can
invoke Bedrock on your account and incur charges. Do not paste it
into chat, tickets, commit messages, screenshots, or non-encrypted
handoff channels.

## 5. Store the key in SSM SecureString

The platform's Lambda functions read the key from SSM at this exact
path (parameterized by your tfvars):

```
/{environment}/{app_name}/bedrock/mantle-api-key
```

For a default deploy this resolves to
`/prd/outcome-ops-ai-assist/bedrock/mantle-api-key`. Confirm the
actual path by inspecting your tfvars:

```bash
grep -E "^environment|^app_name" terraform/prd.tfvars
```

Create the SecureString via CLI (recommended — avoids the console
showing the value on-screen):

```bash
aws ssm put-parameter \
  --region us-east-2 \
  --name "/prd/outcome-ops-ai-assist/bedrock/mantle-api-key" \
  --type "SecureString" \
  --value "ABSK…paste-key-here…" \
  --description "Bedrock Mantle bearer token for GPT-5 / Grok / other Responses-API models"
```

If your deploy uses a customer-managed KMS key rather than the
default `aws/ssm` alias, add `--key-id "alias/your-cmk"`.

**HA note:** if your tfvars sets `secondary_region`, create the SAME
parameter with the SAME value in the secondary region as well. The
platform's failover Lambdas in the secondary region read the SSM
param from the secondary region's Parameter Store, not the primary
region's. Bedrock long-term API keys are account-scoped, so the same
key value works in both regions — you're mirroring the SSM
parameter, not generating a second key.

```bash
# secondary region mirror (only if secondary_region is set)
aws ssm put-parameter \
  --region us-west-2 \
  --name "/prd/outcome-ops-ai-assist/bedrock/mantle-api-key" \
  --type "SecureString" \
  --value "ABSK…paste-same-key-here…" \
  --description "Bedrock Mantle bearer token (mirror of primary)"
```

## 6. Verify the key is picked up

The next cold start of any migrated Lambda picks up the key from
SSM. Two ways to trigger and verify:

**Option A: verify via chat UI.** Open the platform's chat UI, ask
any question. If your tfvars sets an `openai.*` or `xai.*` model as
the basic or advanced tier, the answer streams back token-by-token.
If SSM is misconfigured, you'll see a loud error from the chat
Lambda logged to CloudWatch (`Mantle API key … is unconfigured`) —
per ADR-035's loud-failure stance, this does NOT silently fall back
to a different model.

**Option B: verify via CloudWatch logs.** Tail the chat streaming
Lambda while triggering a chat turn:

```bash
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-chat-streaming \
  --region us-east-2 \
  --follow
```

Expect:
- No `Mantle API key … is unconfigured` errors.
- No `401 access_denied` errors (if you see these on GPT-5.x, revisit
  §3 — the entitlement gate is upstream of the key).
- Normal streaming behavior on chat responses.

If a mismatched-region issue surfaces, check that
`AWS_REGION` matches the region the SSM parameter was written to.
The Lambda's SSM client is region-scoped and will `ParameterNotFound`
against a param that lives in the wrong region.

## 7. Rotate the key

Rotation cadence should match your organization's secret rotation
policy — regulated environments typically 90 days, high-security
tenants 30-60 days. Bedrock long-term API keys do NOT auto-rotate
and can be created with `no expiration` if you rely purely on the
console/CLI revoke path. The rotation procedure below is
zero-downtime:

1. **Create a new key** in the Bedrock console (§4). Copy the new
   value.
2. **Update the SSM SecureString** with the new value:
   ```bash
   aws ssm put-parameter \
     --region us-east-2 \
     --name "/prd/outcome-ops-ai-assist/bedrock/mantle-api-key" \
     --type "SecureString" \
     --value "ABSK…new-key…" \
     --overwrite
   ```
   Repeat in the secondary region if HA is enabled.
3. **Force new Lambda cold starts** so the old key is flushed from
   Lambda execution environments that have it cached in module
   scope. Two ways:
   - Wait ~15 minutes for natural cold-start turnover (Lambda
     recycles execution environments over time).
   - Force it immediately by updating the Lambda's environment
     configuration (any no-op env-var change works):
     ```bash
     aws lambda update-function-configuration \
       --region us-east-2 \
       --function-name prd-outcome-ops-ai-assist-chat-streaming \
       --environment "Variables={ROTATION_MARKER=$(date +%s),…other vars…}"
     ```
     Do NOT drop other environment variables — the CLI replaces
     the entire environment block. In practice, `terraform apply`
     is the safer path to force a cold start: bump a benign tag or
     description, apply, and every Lambda picks up new versions
     with fresh cold starts.
4. **Verify** per §6 that the new key is in use — CloudWatch shows
   normal Mantle traffic.
5. **Revoke the old key** in the Bedrock console. Do this AFTER
   verifying the new key works, not before. If the new key
   provisioning had a typo, keeping the old key valid for the
   verification window is your fallback.

## 8. Rotation timing incidents to plan for

- **Expiration collision.** If the key expires while nobody is on
  call, the next Mantle-routed request returns `401 access_denied`
  and every downstream feature (chat, PR analysis, code
  generation) that uses a Mantle-routed model fails loudly. The
  Lambda logs are clear (`401 access_denied` from Mantle), but the
  UX to end users looks like a platform outage. Set a rotation
  reminder at least 7 days before expiration.
- **Secondary-region drift.** If HA is enabled and only the primary
  region's SSM param is updated during rotation, failover to
  secondary during that window produces `Mantle API key … is
  unconfigured` errors from the secondary Lambdas. Always mirror
  the rotation to both regions in the same maintenance window.
- **KMS key rotation.** If you rotate the KMS key that encrypts the
  SSM SecureString, existing values continue to decrypt (KMS
  handles key versions transparently), but a KMS key deletion or
  policy change that removes the Lambda role's Decrypt grant
  breaks the platform. Coordinate KMS key rotation with the
  platform operator.

## 9. Where the key is consumed in code

For platform operators debugging integration issues, the exact call
site is `lambda/shared/_responses_backend.py`:

```python
def _load_api_key() -> str:
    param_name = os.environ.get("BEDROCK_MANTLE_API_KEY_PARAM")
    …
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    value = resp["Parameter"]["Value"]
    if not value or value == "PLACEHOLDER_CONFIGURE_ME":
        raise BackendConfigError(…)
    return value
```

`BEDROCK_MANTLE_API_KEY_PARAM` is wired on every migrated Lambda's
terraform module (see `terraform/lambda-*.tf`). Every Lambda carries
the env var, even Converse-only Lambdas that don't call the Mantle
path — this way a future `bedrock_advanced_model_id` swap from
`anthropic.*` to `openai.*` does not require a follow-up terraform
apply.

## References

- [ADR-035: Bedrock Converse vs Responses API dispatch via unified
  model client](../adr/ADR-035-bedrock-converse-vs-responses-dispatch.md)
  — architectural rationale for why the platform speaks two Bedrock
  APIs and how the dispatch table decides which to call.
- AWS documentation: [Amazon Bedrock long-term API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-long-term.html)
- AWS documentation: [Bedrock Mantle model catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)
