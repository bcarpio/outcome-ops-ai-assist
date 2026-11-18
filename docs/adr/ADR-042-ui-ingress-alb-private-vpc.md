# ADR-042: UI Ingress via ALB in a Private VPC; No WAF

## Status: Accepted

## Context

The organization's shared standards knowledge base
(`bcarpio/outcomeops-adrs`, ADR-001, ADR-014) mandates that customer-facing
web properties front their origin with **CloudFront** (module
`cloudfront/aws 3.4.1`) plus a **prd-only Web ACL** via
`cloudposse/waf 1.17.0`. That standard was written for public-facing marketing
sites (`outcomeops.ai`, `retrieveit.ai`) where the origin is a static bucket
or a serverless backend fronted by a public edge.

**This product does not fit that shape.** OutcomeOps AI Assist is a
self-hosted, per-customer deploy that lands into each enterprise customer's
own AWS account. The UI is served by a **Fargate service** behind an
Application Load Balancer (`terraform/ui-fargate.tf`, `alb/aws 9.4.0`), not by
CloudFront. Authentication is handled at the Express layer via **Azure AD
OIDC** — every route, including static asset paths, goes through the OIDC
middleware (`ui/server/index.js:96-108`) before the app renders.

Retrieval against the shared-standards KB will keep surfacing ADR-014
("CloudFront + WAF is mandatory") when reviewers or codegen queries the
product's repository, producing a false-positive contradiction that has to
be re-explained every time. This ADR resolves it by scoping ADR-014 to
the public marketing site pattern and documenting the product's ingress
posture as an intentional divergence, per ADR-007.

## Decision

**The UI is served by an ALB inside the customer's VPC. No CloudFront.
No WAF Web ACL.**

### Key points

1. **ALB, not CloudFront.**
    - OIDC authentication happens at the Express layer, not at an edge
      auth-provider. CloudFront-in-front would add a hop with no security
      value (the OIDC session gate is unchanged; the edge just proxies).
    - No static-edge caching benefit — the UI is a React SPA authenticated
      per-user; the vast majority of responses are dynamic (streaming chat,
      workspace state, integration callbacks). Edge cache hit rate would be
      near zero for the payload the platform actually serves.
    - Fargate + ALB is a standard AWS reference pattern and is what
      customer platform-engineering teams already know how to run.
2. **No WAF.**
    - The default enterprise posture is `ui_alb_internal = true` — the ALB
      has no public IP; it is only reachable from inside the customer's
      VPC (Direct Connect, Transit Gateway, VPN, or on-VPC compute). WAF
      protects the internet edge; an internal ALB has no internet edge to
      protect.
    - When customers do run `ui_alb_internal = false` (public ALB) —
      typically for the OutcomeOps maintainer's own sandbox or a
      lightweight PoC — every route is still gated by Azure AD OIDC at
      the Express layer. The threat model in that configuration is
      "authenticated attacker" (which WAF doesn't help with) rather than
      "opportunistic public exploit scan" (which the OIDC gate blocks
      before WAF would ever see the request).
    - WAF on an internal ALB adds standing cost and rule-tuning burden
      with no threat coverage. Enterprise SecOps teams that specifically
      want WAF-style L7 filtering typically already run their own network
      appliances (Zscaler, Palo Alto, F5) upstream of the platform's VPC
      and prefer that over a per-app Web ACL.
3. **`ui_alb_internal` semantics.**
    - `true` — **customer default.** Private ALB, VPC-only reachability.
      Customer routes traffic to it via their existing enterprise ingress
      pattern (Direct Connect, TGW, VPN). This is the SOC 2 / HIPAA /
      FedRAMP-friendly posture the product is designed around.
    - `false` — public ALB with an internet-facing subnet allocation. Used
      by the OutcomeOps maintainer's own sandbox for diagnostics and by
      customers who want a public URL guarded only by OIDC. **This is a
      deliberate configuration** — the tfvar description and the
      `.example` files both document that OIDC is the sole authentication
      gate in that mode.

### Example implementation

```hcl
# terraform/ui-fargate.tf (existing shape; kept for reference)
resource "aws_lb" "ui" {
  name               = "${local.name_prefix}-ui"
  internal           = var.ui_alb_internal   # true = enterprise default
  load_balancer_type = "application"
  # ...
}

# terraform/prd.tfvars (enterprise recommended default)
ui_alb_internal = true
```

```javascript
// ui/server/index.js -- OIDC middleware applies to every non-public route,
// regardless of ALB visibility.
app.use((req, res, next) => {
  if (isPublicRoute(req.path)) return next();
  if (!req.session || !req.session.claims) return res.status(401).end();
  next();
});
```

## Consequences

### Positive

- **Reviews stop flagging a false positive.** Any codegen or PR-check that
  queries the org-standards KB now sees ADR-040 alongside ADR-014 and can
  reason about which applies where.
- **The product's ingress posture is documented** rather than folklore.
  Enterprise SecOps reviews get a paper trail explaining why the deploy
  they're evaluating does not have a Web ACL.
- **Configuration is honest.** The `ui_alb_internal = false` mode is
  documented as a maintainer-diagnostic / PoC pattern, not a
  customer-recommended posture. Customers who see it and ask "should we
  do that?" get a direct answer.

### Tradeoffs

- **No L7 attack-surface filtering at the ALB.** Customers who want WAF
  because their security team specifically requires it can add a
  Web ACL themselves (the ALB supports it; the platform's Terraform doesn't
  provision one). Not part of the default deploy.
- **CloudFront-in-front is a viable pattern** for customers who want
  edge caching, geo restrictions, or DDoS shielding at the AWS-edge level.
  Not provided out of the box; the platform's HTTPS termination happens at
  the ALB with ACM. Adding CloudFront is an operator-driven layer, not
  an OutcomeOps concern.

### When to reconsider

- If a customer's regulated deploy explicitly requires WAF on the ALB, add
  it via their own Terraform overlay. The platform's design accommodates it
  (nothing about the ALB or the routing prevents an aws_wafv2_web_acl
  attachment); it's just not the default.
- If a future feature exposes a public HTTP endpoint (e.g., a customer-hosted
  MCP catalog served publicly), that endpoint's ingress posture is a
  separate decision; ADR-040 applies to the UI ALB only.

## Implementation

Committed 2026-07-06 as part of a code-review remediation
No terraform resource changes — this ADR captures the standing
posture, does not alter it.

## Related ADRs

- ADR-014 (org standards KB): CloudFront + WAF for public web properties.
  Scoped to marketing sites; not applicable to this product's ingress.
- ADR-007: Documentation-driven decisions — when a problem can be resolved
  by clarifying documentation (like a standards contradiction), do that
  before adding code or infrastructure.
- ADR-022: Multi-region HA. The UI ALB pattern in each region uses the
  same `ui_alb_internal` semantics; ADR-040 covers both primary and
  secondary.

## References

- `terraform/ui-fargate.tf` — the primary-region ALB module block.
- `terraform/ui-fargate-secondary.tf` — the secondary-region mirror.
- `terraform/variables.tf` — `ui_alb_internal` variable definition.
- `terraform/{prd,dev}.tfvars.example` — the enterprise-recommended value
  and the comment guiding customer choice.

Version History:

- v1.0 (2026-07-06): Initial decision.

<!-- Confluence sync -->
