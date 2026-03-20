# Security Incident Response

**Document:** OutcomeOps Security Incident Response Policy
**Version:** 1.0
**Last Updated:** 2026-03-20
**Owner:** OutcomeOps LLC

---

## 1. Scope and Responsibility Boundary

OutcomeOps AI Assist is deployed entirely within the **customer's own AWS account**. This creates a clear division of security responsibility:

| Responsibility | Owner |
|---|---|
| Vulnerabilities in OutcomeOps software (application code, Lambda functions, Terraform modules) | OutcomeOps |
| Vulnerabilities in the licensing layer and OutcomeOps-operated licensing endpoint | OutcomeOps |
| Operational incident response (breach detection, containment, forensics) | Customer (SOC/SRE team) |
| AWS infrastructure security (IAM, GuardDuty, CloudTrail, VPC) | Customer |
| Identity and access management (Azure AD, MFA, conditional access) | Customer |
| MCP API key lifecycle (creation, rotation, revocation, monitoring for misuse) | Customer |

Customers are expected to integrate OutcomeOps deployments into their existing incident response plans and security monitoring posture.

---

## 2. Reporting a Vulnerability to OutcomeOps

If you discover a security vulnerability in OutcomeOps software, please report it responsibly:

**Email:** security@outcomeops.ai
**Subject line:** `[SECURITY] Brief description`
**PGP:** Available on request

Please include:
- Description of the vulnerability and affected component
- Steps to reproduce
- Potential impact assessment
- Your contact information for follow-up

**Do not** open a public GitHub issue for security vulnerabilities. Do not disclose publicly until OutcomeOps has had an opportunity to investigate and issue a patch.

OutcomeOps commits to acknowledging receipt within **2 business days** and providing an initial assessment within **7 business days**. Patch timelines follow the severity-based schedule in [Section 3.2](#32-response-timelines).

---

## 3. OutcomeOps-Discovered Vulnerabilities

When OutcomeOps identifies a vulnerability in its own software — through internal security testing, penetration testing, automated scanning, or code review — the following process applies.

### 3.1 Severity Classification

| Severity | CVSS Range | Description | Example |
|---|---|---|---|
| Critical | 9.0–10.0 | Unauthenticated remote code execution, authentication bypass, data exfiltration | Auth bypass allowing unauthenticated API access |
| High | 7.0–8.9 | Authenticated privilege escalation, significant data exposure | Path traversal exposing KB documents across workspaces |
| Medium | 4.0–6.9 | Limited scope impact, requires specific conditions | Information disclosure in error responses |
| Low | 0.1–3.9 | Minimal impact, defense-in-depth improvements | Missing security header |

### 3.2 Response Timelines

| Severity | Patch Available | Customer Notification |
|---|---|---|
| Critical | 7 days | Immediate (same day as discovery) |
| High | 30 days | Within 5 business days of discovery |
| Medium | Next scheduled release | Release notes |
| Low | Next scheduled release | Release notes |

### 3.3 Customer Notification

For Critical and High severity findings, OutcomeOps will notify customers via:

1. **Direct email** to the technical contact on record
2. **GitHub Security Advisory** published at time of patch release
3. **Release notes** documenting the fix and affected versions

Notifications will include:
- Nature of the vulnerability (without exploit details prior to patch)
- Affected component and versions
- Whether a workaround exists
- Target patch date
- Instructions for applying the fix (Terraform module version bump or layer ARN update)

**Note:** Because OutcomeOps software is customer-deployed, OutcomeOps cannot push patches. Customers are responsible for applying updates within a reasonable timeframe following notification.

---

## 4. Dependency Vulnerabilities (CVEs)

OutcomeOps uses automated dependency scanning (Snyk, SonarCloud, ECR scan-on-push) in the CI/CD pipeline. The pipeline blocks deployment of any code with known Medium or higher CVSS vulnerabilities in dependencies.

### 4.1 Dependency Response Policy

| Severity | Response | Customer Notification |
|---|---|---|
| Critical (9.0+) | Emergency patch within 7 days | Proactive notification per Section 3.3 |
| High (7.0–8.9) | Patch within 30 days | Noted in release notes |
| Medium (4.0–6.9) | Patch in next release cycle | Noted in release notes |
| Low (0.1–3.9) | Addressed opportunistically | No notification required |

Dependency vulnerabilities that affect Lambda runtimes or container base images follow the same severity-based timeline. OutcomeOps monitors Snyk continuously for newly disclosed CVEs against the dependency tree.

---

## 5. Licensing Layer and Shared Endpoint

OutcomeOps operates a licensing endpoint used by deployments that have not configured a custom enterprise Lambda layer ARN. This is the one component of OutcomeOps that operates as shared infrastructure across customer deployments.

### 5.1 Scope of the Licensing Component

| Component | Description |
|---|---|
| Lambda layer | Distributed as a versioned ARN; contains JWT verification logic |
| Licensing endpoint | OutcomeOps-operated; validates license keys and checks compliance counters |
| Data transmitted | PR count, repository count per workspace — no code, content, or PII |
| Frequency | Per PR generation event (non-Enterprise deployments without custom layer ARN) |

### 5.2 Accelerated Response for Licensing Component

Because the licensing layer runs inside the customer's AWS account but contains OutcomeOps-operated code, and because the licensing endpoint is OutcomeOps-operated infrastructure, vulnerabilities affecting these components are treated with elevated urgency:

- **Any severity** affecting the licensing layer or endpoint: patch available within **7 days**, mandatory customer notification regardless of CVSS score
- Customers are notified of the new layer ARN and must update their `tfvars` to pull in the patched layer

### 5.3 Disabling Phone-Home (Enterprise)

Customers with an Enterprise license agreement may disable the phone-home licensing check by providing a custom Lambda layer ARN in their Terraform configuration. This completely removes the outbound licensing call from their deployment. Enterprise customers requiring this arrangement should contact their OutcomeOps account contact to establish an alternative compliance verification method (e.g., quarterly audit, contractual attestation).

---

## 6. Coordinated Disclosure Policy

OutcomeOps follows a **90-day coordinated disclosure** policy for externally reported vulnerabilities:

1. Researcher reports vulnerability to security@outcomeops.ai
2. OutcomeOps acknowledges within 2 business days
3. OutcomeOps investigates and develops a patch
4. Patch is released and customers are notified
5. Public disclosure occurs at patch release or at the 90-day mark, whichever comes first
6. OutcomeOps will credit the reporter in the security advisory unless anonymity is requested

If a vulnerability is being actively exploited in the wild, OutcomeOps reserves the right to accelerate public disclosure to protect customers.

---

## 7. Customer Responsibilities

Because OutcomeOps software runs in the customer's AWS account, customers are responsible for:

- Monitoring their deployment using their existing security tooling (GuardDuty, CloudTrail, Security Hub, SIEM)
- Applying OutcomeOps software updates in a timely manner following security notifications
- Maintaining their own incident response plan covering the OutcomeOps deployment
- Rotating secrets per the [Credential Rotation](deployment-guide.md#credential-rotation) schedule, including: GitHub PAT, GitHub App private key and OAuth secret, GitHub webhook secret, OIDC client secret, Microsoft OAuth secret (Outlook/Teams/SharePoint), Atlassian OAuth secret (Confluence/Jira), and SonarQube/Snyk MCP tokens
- Managing MCP API keys — revoking keys for departing employees, rotating keys on suspected compromise, and monitoring `last_used_at` timestamps for anomalous activity. MCP API keys bypass OIDC authentication and grant access to the knowledge base and all tools assigned to the user's workspaces.
- Reporting suspected vulnerabilities in OutcomeOps software to security@outcomeops.ai

---

## 8. Security Contact

| Contact | Details |
|---|---|
| Security disclosures | security@outcomeops.ai |
| General security questions | security@outcomeops.ai |
| Enterprise licensing / phone-home opt-out | Contact your OutcomeOps account representative |
| Security advisories | Published at github.com/outcomeops (GitHub Security Advisories) |

---

## 9. Document History

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-03-20 | Initial release |
