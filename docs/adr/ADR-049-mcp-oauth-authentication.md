# ADR-049: MCP server authenticates with Azure AD OAuth (JWT)

## Status: Accepted

## Context

The MCP server authenticated callers with **static API keys**:
`sha256(bearer)` → a GSI1 lookup on the ApiKeys table. That is a
credential-distribution flow — the operator mints a long-lived key and hands it
to each client — which regulated enterprise security teams reject
([[feedback_enterprise_security]]), and it diverges from CLAUDE.md's stated
contract ("OAuth 2.0 JWTs for the MCP server").

It also caps client reach. A survey of MCP clients (2025–2026) shows the
authenticated-remote-MCP landscape splits on whether a static bearer header is
even allowed:

| Client | Static `Bearer` header | OAuth flow | Note |
|---|---|---|---|
| Claude Code | yes (out of the box) | yes | header wins; `headersHelper` can inject a fresh JWT |
| Cursor | yes | yes (v1.0, Jun 2025) | **ignores the static header if the server advertises OAuth discovery** |
| Codex CLI | yes (`bearer_token_env_var`) | yes | — |
| ChatGPT (Developer Mode) | **no** | **required** | no custom API keys/bearer at all — OAuth 2.1 only |
| Copilot Studio | yes (API-key header) | optional | — |
| M365 Copilot (end-user) | **no** | required (OAuth/Entra) | federated-connector registration + admin approval |

So a static-key-only server is reachable by the coding agents but **locked out of
ChatGPT and M365 Copilot**. OAuth is the only way to reach those, and it is the
posture regulated buyers expect.

## Decision

**The MCP server validates an Azure AD-issued JWT bearer as an OAuth 2.0
resource server, with the legacy static-key path kept behind a feature flag for
backward-compatible migration.**

1. **Shape-dispatch** (`validate_bearer`). A compact JWS bearer (its base64url
   header starts `eyJ`) goes to `_validate_azure_ad_jwt`; anything else is a
   legacy static key.

2. **JWT validation** (`_validate_azure_ad_jwt`). Fetch the tenant JWKS from
   Azure AD discovery (`.../{tenant}/discovery/v2.0/keys`) via PyJWT's
   `PyJWKClient`, cached in-process 1h (same cadence as the DDB-session and
   api-key caches). Verify the RS256 signature, `iss`
   (`https://login.microsoftonline.com/{tenant}/v2.0`), `aud`
   (`MCP_JWT_AUDIENCE` — the MCP server's App Registration), `exp`, and `nbf`.
   Map `preferred_username | upn | email` to the user identity.

3. **Static-key feature flag** (`mcp_static_keys_enabled` tfvar →
   `MCP_STATIC_KEYS_ENABLED` env). Default `true` for back-compat; set `false`
   in the enterprise profile once every client uses OAuth, at which point the
   static branch returns 401 unconditionally. Every static-key use is logged
   `deprecated=true` so the operator can watch the migration and alarm on
   volume before flipping the flag.

4. **Keep both auth methods live during migration.** This is deliberate given
   the client matrix: implementing OAuth (advertising discovery) is required for
   ChatGPT, and Cursor will then use OAuth too; Claude Code and Codex can use
   either. Ripping out static keys immediately would break every currently
   configured client. The flag lets each deployment migrate on its own timeline.

5. **Config gating.** When `AZURE_AD_TENANT_ID` or `MCP_JWT_AUDIENCE` is unset,
   the JWT path is disabled (static keys only) — so a deploy that hasn't set up
   an App Registration is unaffected.

## Consequences

- The MCP server honors the CLAUDE.md contract and is reachable by ChatGPT /
  M365 Copilot (OAuth-only clients), not just the coding agents.
- No credential-distribution flow once static keys are disabled — the win for
  regulated buyers.
- **Cursor caveat:** because we advertise OAuth, Cursor drops any static header
  and uses OAuth. That is fine (OAuth works), but a bearer-only Cursor config
  will stop working the moment discovery is advertised — documented in the setup
  guide.
- **M365 Copilot** needs more than OAuth on our side — federated-connector
  registration + Microsoft/admin approval. That is a separate integration, not
  covered here.
- Deploy impact: MCP server is Fargate ([[feedback_mcp_server_is_fargate]]) —
  image rebuild + ECS force-redeploy + client reconnect; `terraform apply` for
  the new tfvars. JWKS cache is per-task; both regions cache independently.

## Alternatives considered

- **Keep static keys only.** Rejected — credential-distribution flow; locks out
  ChatGPT/M365 Copilot; violates the stated contract.
- **Drop static keys immediately (no flag).** Rejected — breaks every existing
  client at once. The flag + `deprecated=true` logging gives a measured sunset.
- **Validate via Azure AD introspection endpoint instead of local JWKS.**
  Rejected — a network round-trip per MCP call; local JWKS validation with a
  cached key set is the standard resource-server pattern.
