# MCP server — Azure AD OAuth setup

The OutcomeOps MCP server accepts an **Azure AD (Entra) OAuth access token** as
the `Authorization: Bearer <jwt>` credential, validated as an OAuth 2.0 resource
server (see [ADR-049](adr/ADR-049-mcp-oauth-authentication.md)). Legacy static
API keys still work until you disable them, so migration is incremental.

## 1. Operator: register the MCP server in Entra

1. **App registrations → New** — name it e.g. `OutcomeOps MCP`.
2. **Expose an API → Add a scope** (or set an **Application ID URI**, e.g.
   `api://outcomeops-mcp`). The **audience** clients request tokens for is this
   App ID URI (or the app's client ID).
3. If you want the `groups` claim in the token (for group→workspace resolution,
   [ADR-048](adr/ADR-048-azure-ad-group-provisioning.md)), set
   **Token configuration → groupMembershipClaims = SecurityGroup**.

## 2. Operator: configure + deploy

Set in `terraform/<env>.tfvars`:

```hcl
azure_ad_tenant_id      = "d83d3279-....-............"  # your Entra tenant GUID
mcp_jwt_audience        = "api://outcomeops-mcp"        # the aud clients request
mcp_static_keys_enabled = true                          # keep true during migration
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
    (e.g. `az account get-access-token --resource api://outcomeops-mcp`) to fetch
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
