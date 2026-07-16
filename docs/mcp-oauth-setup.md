# MCP server — Azure AD OAuth setup

The OutcomeOps MCP server accepts an **Azure AD (Entra) OAuth access token** as
the `Authorization: Bearer <jwt>` credential, validated as an OAuth 2.0 resource
server (see [ADR-049](adr/ADR-049-mcp-oauth-authentication.md)). Legacy static
API keys still work until you disable them, so migration is incremental.

## 1. Operator: register the MCP server in Entra

1. **App registrations → New** — name it e.g. `OutcomeOps MCP` → Register. Note
   its **Application (client) ID** from the Overview page.
2. **Expose an API → Application ID URI → Set.** Use the **app-ID-based** form
   **`api://<client-id>`** (the value Azure pre-fills). Do NOT invent a bare name
   like `api://outcomeops-mcp` — most tenants' default policy rejects it with
   `AADSTS` *"All newly added URIs must contain a tenant verified domain, tenant
   ID, or app ID"* (unless you set `requestedAccessTokenVersion = 2` in the
   manifest). This URI is the **audience** (`aud`) clients request tokens for and
   the value you put in `mcp_jwt_audience`.
3. **Set the app to issue v2 access tokens (required).** App **Manifest** → set
   `"requestedAccessTokenVersion": 2` (or via
   `az rest --method PATCH --uri https://graph.microsoft.com/v1.0/applications/<object-id> --body '{"api":{"requestedAccessTokenVersion":2}}'`).
   The server validates the **v2** issuer (`…/<tenant>/v2.0`); the default
   (`null`/1) issues v1 tokens (`iss = https://sts.windows.net/<tenant>/`) that
   are rejected with `Azure AD JWT rejected: Invalid issuer`.
   **Consequence:** a v2 token's `aud` is the **bare client-id GUID**, NOT the
   `api://<client-id>` URI. So `mcp_jwt_audience` must be the **GUID**
   (`25987dc4-…`), even though clients still *request* the token with
   `--resource api://<client-id>`.
4. **Expose an API → + Add a scope:**
   - Scope name: `access_as_user`
   - Who can consent: **Admins and users**
   - Admin consent display name: `Access OutcomeOps MCP as the signed-in user`
   - Admin consent description: `Allow the client application to call the
     OutcomeOps MCP server's tools and knowledge base on behalf of the signed-in
     user.`
   - User consent display name: `Access OutcomeOps MCP on your behalf`
   - User consent description: `Allow the app to access the OutcomeOps MCP server
     and its tools on your behalf.`
   - State: **Enabled**

   (The admin-consent display name + description are **required** fields; the
   values above are safe defaults.)
5. **Expose an API → Authorized client applications → + Add** each MCP client's
   app id and check the `access_as_user` scope, so tokens issue without an
   interactive consent prompt. For quick testing with the Azure CLI, add the
   Azure CLI's well-known id **`04b07795-8ddb-461a-bbee-02f9e1bf7b46`**.
6. If you want the `groups` claim in the token (for group→workspace resolution,
   [ADR-048](adr/ADR-048-azure-ad-group-provisioning.md)), set
   **Token configuration → groupMembershipClaims = SecurityGroup**. (Note: with
   ALB OIDC the groups claim does NOT reach the app anyway — see ADR-048 §11 —
   so this matters only for non-ALB MCP clients.)

## 2. Operator: configure + deploy

Set in `terraform/<env>.tfvars`:

```hcl
azure_ad_tenant_id      = "d83d3279-....-............"    # your Entra tenant GUID
mcp_jwt_audience        = "api://<mcp-app-client-id>"    # the App ID URI from step 1.2
mcp_static_keys_enabled = true                           # keep true during migration
```

Then rebuild + redeploy the MCP server (it runs on Fargate, so this needs an
**image rebuild + ECS force-redeploy**, not just `terraform apply`):

```bash
make build-mcp-server-image
terraform apply      # picks up the new env vars
# force a new MCP task so it loads the new image + env:
aws ecs update-service --cluster <cluster> --service outcomeops-mcp --force-new-deployment
```

Leaving `azure_ad_tenant_id`/`mcp_jwt_audience` empty disables the JWT path (the
server keeps accepting static keys only) — safe if you haven't set up Entra yet.

## 3. Clients: present an Azure AD token

The MCP client sends `Authorization: Bearer <jwt>`, where `<jwt>` is an Entra
access token for the audience above. How you obtain/configure it depends on the
client (support varies — see the matrix in ADR-049):

- **Claude Code / Cursor / Codex CLI** — configured via the client's MCP CLI
  (per [[feedback_mcp_transport_type]], config is CLI-driven). Two options:
  - **OAuth flow** (recommended): point the client at the MCP server URL; on a
    401 the client runs the browser-based OAuth login itself.
  - **Static/injected token**: supply `Authorization: Bearer <jwt>` as a header.
    For a short-lived token, Claude Code's `headersHelper` can run a command
    (e.g. `az account get-access-token --resource api://<mcp-app-client-id>`) to fetch
    a fresh token per connection.
- **ChatGPT (Developer Mode)** — **OAuth only**; it will not accept a static
  bearer/API key. It drives the OAuth flow against Entra.
- **Microsoft 365 Copilot** — cannot add a raw MCP URL; requires
  federated-connector registration + admin approval (a separate integration).

**Cursor caveat:** once the server advertises OAuth discovery, Cursor ignores a
static `headers` bearer and uses OAuth. Don't rely on a static header for Cursor.

## 4. Retire static keys (per deployment, when ready)

Every static-key use is logged `deprecated=true`. When that volume reaches zero
(all clients on OAuth), set:

```hcl
mcp_static_keys_enabled = false
```

and redeploy. The static path then returns 401 unconditionally; only Azure AD
JWTs are accepted.
