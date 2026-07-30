# Data Privacy Impact Assessment (DPIA)

**System:** OutcomeOps AI Assist
**Assessment Date:** 2026-03-12
**Version:** 1.0
**Prepared by:** OutcomeOps
**Status:** Draft

---

## 1. Purpose and Legal Basis for Data Processing

### 1.1 Purpose

OutcomeOps AI Assist is an AI-powered developer platform that:

- Ingests organizational documentation (ADRs, coding standards, architecture docs) into a vector knowledge base
- Provides AI-assisted code generation, chat-based developer guidance, and pull request reviews
- Exposes knowledge base tools via MCP (Model Context Protocol) for integration with developer tools
- Acts as a centralized knowledge management system for engineering organizations

### 1.2 Legal Basis

Processing is based on the deploying organization's legitimate interest in improving developer productivity and code quality. The platform is deployed within the organization's own AWS account -- no data is transmitted to third parties outside of AWS managed services.

Organizations deploying OutcomeOps are responsible for establishing their own legal basis for data processing under applicable regulations (GDPR, CCPA, etc.) and for obtaining any required employee consent.

### 1.3 Data Controller and Processor

| Role | Entity |
|------|--------|
| Data Controller | The deploying organization (e.g., a large enterprise) |
| Data Processor | AWS (infrastructure and AI model hosting) |
| Software Provider | OutcomeOps (provides the platform software; no access to customer data or AWS accounts) |

---

## 2. Description of Processing Activities

### 2.1 Data Categories and Storage Locations

#### Personal Information

| Data Element | Source | Storage Location | Retention | Encryption |
|---|---|---|---|---|
| Employee email addresses | OIDC authentication (Azure AD) | DynamoDB workspaces table (`USER#{email}`) | Persistent (no TTL) | SSE (AWS managed) |
| Employee names | OIDC JWT claims | DynamoDB workspaces table | Persistent (no TTL) | SSE (AWS managed) |
| User role assignments | Admin configuration | DynamoDB workspaces table | Persistent (no TTL) | SSE (AWS managed) |
| OAuth access/refresh tokens | Azure AD, Atlassian, Microsoft, GitHub | DynamoDB workspaces table | Persistent (no TTL) | KMS encrypted (`kms-oauth.tf`) |
| API key hashes | User-generated | DynamoDB workspaces table (`API_KEY#{key_id}`) | Persistent (no TTL) | SHA-256 hashed; plaintext never stored |
| API key metadata | User-generated | DynamoDB workspaces table | Persistent (no TTL) | SSE (AWS managed) |
| Employee emails in ingested content | Git commit history, Confluence authorship, Jira assignees | S3 KB bucket, S3 Vectors | S3: 30-day noncurrent version expiry; Vectors: no lifecycle | AES256 (SSE-S3) |

#### Operational Data

| Data Element | Source | Storage Location | Retention | Encryption |
|---|---|---|---|---|
| Chat conversations (full text) | User interactions | DynamoDB chat table (`CONV#{id}`, `MSG#{timestamp}`) | Persistent (no TTL) | SSE (AWS managed) |
| AI audit records (full prompts and responses) | All Bedrock invocations | DynamoDB audit table | 365 days (TTL auto-delete) | SSE (AWS managed) |
| Token usage and cost per request | Bedrock API responses | DynamoDB audit table | 365 days (TTL auto-delete) | SSE (AWS managed) |
| Refusal detection alerts | Audit pipeline | SNS → email (first 500 chars of input/output) | Transient (email delivery) | In transit via SNS |
| Application logs | Lambda, ECS, ALB | CloudWatch Logs | 7 days | KMS encrypted (`kms.tf`) |
| Source code and documentation | GitHub, Confluence, Jira, file uploads | S3 KB bucket (`workspaces/{id}/{source}/`) | 30-day noncurrent version expiry | AES256 (SSE-S3) |
| Vector embeddings | Bedrock Titan embeddings | S3 Vectors bucket | No lifecycle rules | AES256 (SSE-S3) |
| Ingestion state tracking | Ingestion pipeline | DynamoDB code-maps table | Persistent (no TTL) | SSE (AWS managed) |

### 2.2 Data Flows

```
                                        Organization's AWS Account
                                        +-----------------------------------------+
                                        |                                         |
Developer → OIDC (Azure AD) → ALB ─────→ UI (Fargate)                            |
     │                          │       |    │                                    |
     │                          │       |    ├─→ Chat Lambda                      |
     │                          │       |    │     ├─→ S3 Vectors (KB search)     |
     │                          │       |    │     ├─→ Bedrock (Claude)           |
     │                          │       |    │     ├─→ DynamoDB Chat (store)      |
     │                          │       |    │     └─→ DynamoDB Audit (log)       |
     │                          │       |    │                                    |
     │                          │       |    ├─→ Workspace Mgmt Lambda            |
     │                          │       |    │     ├─→ DynamoDB Workspaces        |
     │                          │       |    │     └─→ S3 KB (uploads)            |
     │                          │       |    │                                    |
     │                          │       |    └─→ Generate Code Lambda             |
     │                          │       |          ├─→ S3 Vectors (KB search)     |
     │                          │       |          ├─→ Bedrock (Claude)           |
     │                          │       |          ├─→ GitHub API (create PR)     |
     │                          │       |          └─→ DynamoDB Audit (log)       |
     │                          │       |                                         |
MCP Client → API Key Auth → ALB ───────→ MCP Server (Fargate)                    |
                                        |    ├─→ DynamoDB Workspaces (auth)       |
                                        |    ├─→ S3 Vectors (KB search)           |
                                        |    └─→ Bedrock (Claude)                 |
                                        |                                         |
EventBridge (hourly) ───────────────────→ Ingestion Lambdas                      |
                                        |    ├─→ GitHub/Confluence/Jira APIs      |
                                        |    ├─→ S3 KB (store docs)              |
                                        |    ├─→ Bedrock Titan (embeddings)      |
                                        |    └─→ S3 Vectors (store vectors)      |
                                        |                                         |
DynamoDB Streams (audit) ───────────────→ Audit Alert Lambda                     |
                                        |    └─→ SNS (refusal notifications)     |
                                        +-----------------------------------------+
```

### 2.3 Who Has Access

| Role | Access Scope |
|------|-------------|
| org_admin | All workspaces, user management, MCP server configuration, API keys |
| workspace_admin | Specific workspace, member management, integrations, document management |
| member | Chat, KB queries, MCP tools within assigned workspaces |
| OutcomeOps (vendor) | No access to customer AWS accounts or data |
| AWS | Infrastructure operator; data encrypted at rest and in transit |

### 2.4 Data Sent to AWS Bedrock

The following data is sent to Anthropic Claude (via AWS Bedrock managed service) for each AI interaction:

1. User's query or message (full text)
2. Retrieved knowledge base context (up to 50,000 characters)
3. Conversation history/memory summaries
4. System prompt (generic, not user-specific; defined in `lambda/shared/prompts.py`)
5. MCP tool schemas and results (when applicable)

**No *additional* PII redaction is performed at inference time before sending to Bedrock.** The user prompt is sent as-is; the retrieved KB context is the content already redacted at INGEST via Amazon Comprehend (STRICT mode) before it entered the vector store (see ADR-051). No further redaction is applied to the prompt/response at model-call time.

Per AWS Bedrock's data privacy policy: customer prompts and completions are not stored by AWS or used for model training. Data is processed in transit and discarded after response generation.

---

## 3. Necessity and Proportionality Assessment

### 3.1 Necessity

| Processing Activity | Necessity | Justification |
|---|---|---|
| OIDC authentication (email, name) | Required | User identification and access control |
| Chat message storage | Required | Conversation continuity and context |
| Audit logging (full prompts/responses) | Required | Enterprise compliance, cost tracking, refusal detection |
| KB document ingestion | Required | Core functionality -- RAG-based AI guidance |
| Vector embeddings | Required | Semantic search capability |
| OAuth token storage | Required | Integration with external sources (GitHub, Confluence, Jira) |
| API key management | Required | MCP client authentication |
| CloudWatch logs | Required | Operational monitoring and debugging |

### 3.2 Proportionality

| Concern | Assessment |
|---|---|
| Audit table stores full prompts/responses | Proportionate for enterprise compliance requirements; 365-day TTL limits retention |
| Chat messages stored indefinitely | **Gap:** No TTL configured on chat table; should align with organizational retention policy |
| Workspaces/user data stored indefinitely | **Gap:** No TTL or cleanup for deactivated users; should implement user offboarding |
| No additional inference-time redaction before Bedrock calls (KB context is already ingest-redacted via Comprehend, ADR-051) | Accepted risk; Bedrock does not store/train on data; content is enterprise technical data |
| Employee PI in ingested content | Incidental; content is primarily code/documentation; organizations control what is ingested |
| OAuth tokens in DynamoDB | Mitigated by KMS encryption; tokens are encrypted at application level before storage |

---

## 4. Risk Identification

### 4.1 Risk Register

| ID | Risk | Who Could Be Harmed | Category |
|---|---|---|---|
| R1 | Unauthorized access to audit table exposes full conversation history including any PI in prompts | Employees whose data appears in conversations | Confidentiality |
| R2 | OAuth token compromise allows access to connected services (GitHub, Confluence, Jira, Microsoft) | Organization (data breach via connected systems) | Confidentiality |
| R3 | API key theft allows unauthorized KB queries and tool access | Organization (data exfiltration) | Confidentiality |
| R4 | AI hallucination generates incorrect code that introduces security vulnerabilities | Organization (security incident if code is merged) | Integrity |
| R5 | Knowledge base contains stale or incorrect standards, leading to wrong AI guidance | Organization (technical debt, standard violations) | Integrity |
| R6 | Employee PI in ingested content (git history, Confluence authors) becomes searchable via RAG | Employees whose names/emails appear in source content | Privacy |
| R7 | Refusal alert emails contain first 500 chars of user input, transmitted via SNS | Employee who triggered the refusal | Privacy |
| R8 | Chat data stored indefinitely without retention policy | Former employees whose conversations persist | Privacy |
| R9 | CloudWatch logs may contain user emails and request details | Employees (limited exposure -- 7-day retention) | Privacy |
| R10 | S3 Vectors bucket has no lifecycle rules; embeddings persist indefinitely | Organization (data minimization compliance) | Compliance |
| R11 | Ingested content IS redacted at ingest (Amazon Comprehend, STRICT default, ADR-051); residual risk is the raw source mirror, deliberately left unredacted for read_source fidelity and protected by IAM + KMS | Employees whose PI is in source documents | Privacy |
| R12 | MCP server sessions stored in-memory; no persistent session audit trail | Organization (accountability gap) | Accountability |

---

## 5. Risk Severity and Likelihood Assessment

| ID | Risk | Likelihood | Severity | Overall Rating | Rationale |
|---|---|---|---|---|---|
| R1 | Audit table unauthorized access | Low | High | Medium | DynamoDB encrypted, IAM-controlled; but data is plaintext once accessed |
| R2 | OAuth token compromise | Low | Critical | High | KMS encrypted, but compromise enables cascading access to connected systems |
| R3 | API key theft | Medium | Medium | Medium | Keys are hashed (SHA-256), but if intercepted in transit they grant KB access |
| R4 | AI hallucination in generated code | High | Medium | High | Inherent to LLMs; mitigated by human-in-the-loop PR review |
| R5 | Stale knowledge base | Medium | Low | Low | Hourly ingestion refreshes; impact is suboptimal guidance, not data breach |
| R6 | Employee PI in RAG results | Medium | Low | Low | Limited to names/emails in technical context; not sensitive PI |
| R7 | PI in refusal alert emails | Low | Low | Low | Only 500 chars; only sent on refusals; limited distribution |
| R8 | Indefinite chat retention | Medium | Medium | Medium | No TTL on chat table; conversations accumulate without cleanup |
| R9 | PI in CloudWatch logs | Low | Low | Low | 7-day retention; KMS encrypted |
| R10 | No vector lifecycle rules | Low | Low | Low | Embeddings don't contain raw PI; metadata is limited |
| R11 | Raw PII persists in the S3 source mirror at rest | Low | Medium | Low | Comprehend redacts the synthesis/vector path at ingest (STRICT default); the residual is the deliberately-unredacted read_source mirror, bounded by IAM + KMS (ADR-051) |
| R12 | No MCP session audit trail | Low | Low | Low | MCP interactions go through existing audit pipeline for Bedrock calls |

---

## 6. Technical and Organizational Mitigations in Place

### 6.1 Authentication and Access Control

| Control | Implementation | Source |
|---|---|---|
| OIDC authentication | ALB-integrated Azure AD/Cognito; JWT validation | `ui-fargate.tf`, `ui/server/index.js` |
| Role-based access control | org_admin, workspace_admin, member roles | `lambda/workspace-management/handler.py` |
| Workspace isolation | S3 paths and vector queries scoped by workspace_id | `lambda/chat/handler.py`, `s3.tf` |
| API key authentication | SHA-256 hashed keys with optional expiration | `lambda/workspace-management/handler.py` |
| IAM least privilege | Per-Lambda IAM roles with specific resource ARNs | `lambda-chat.tf`, `lambda-workspace-mgmt.tf` |

### 6.2 Encryption

| Control | Implementation | Source |
|---|---|---|
| Encryption at rest (DynamoDB) | AWS-managed server-side encryption | All DynamoDB table definitions |
| Encryption at rest (S3) | AES256 (SSE-S3) | `s3.tf`, `s3-vectors.tf` |
| Encryption at rest (logs) | KMS customer-managed key | `kms.tf` |
| OAuth token encryption | KMS application-level encryption before DynamoDB storage | `kms-oauth.tf` |
| Encryption in transit | HTTPS (ALB), TLS (AWS SDK), SigV4 (Lambda invocations) | `ui-fargate.tf`, `ui/server/index.js` |
| API key storage | SHA-256 hash; plaintext never stored | `lambda/workspace-management/handler.py` |

### 6.3 Data Retention

| Control | Implementation | Source |
|---|---|---|
| Audit log TTL | 365-day automatic deletion | `dynamodb-audit.tf` |
| CloudWatch log retention | 7-day retention | `kms.tf` (log group definitions) |
| S3 version expiry | 30-day noncurrent version lifecycle | `s3.tf` |
| S3 multipart cleanup | 7-day incomplete upload cleanup | `s3.tf` |

### 6.4 Monitoring and Accountability

| Control | Implementation | Source |
|---|---|---|
| Full audit trail | All Bedrock calls logged with user, input/output, tokens, cost | `lambda/shared/audit.py` |
| Refusal detection | Regex pattern matching on AI responses; SNS alerts | `lambda/shared/audit.py`, `lambda/audit-alert/handler.py` |
| DynamoDB Streams | Real-time audit event processing | `dynamodb-audit.tf` |
| Point-in-time recovery | Enabled on chat table | `dynamodb-chat.tf` |

### 6.5 Network Security

| Control | Implementation | Source |
|---|---|---|
| Private subnets | Fargate tasks in private subnets (configurable) | `ui-fargate.tf` |
| Security groups | ALB → Fargate restricted by port and SG | `ui-fargate.tf` |
| S3 public access blocked | All public access settings disabled | `s3.tf` |
| No VPC for Lambda | Lambda calls AWS services via service endpoints (no inbound exposure) | Lambda function configurations |

### 6.6 Human Oversight

| Control | Implementation | Source |
|---|---|---|
| Human-in-the-loop code review | All generated code delivered as PRs requiring human approval | `lambda/generate-code/handler.py` |
| Advisory-only chat | Chat responses are guidance, not autonomous actions | `lambda/chat/handler.py` |
| Admin-controlled ingestion | Organizations configure which sources are ingested | Terraform `repos_to_ingest` variable |

---

## 7. Residual Risk Assessment

### 7.1 Accepted Residual Risks

| Risk | Residual Level | Acceptance Rationale |
|---|---|---|
| AI hallucination (R4) | Medium | Inherent to LLMs; human-in-the-loop PR review is the primary mitigation; RAG reduces but does not eliminate hallucination |
| Employee PI in ingested content (R6) | Low | PI is incidental (names/emails in git history); organizations control what is ingested; data is technical in nature |
| Raw PII in the source mirror + no inference-time redaction (R11) | Low | Ingest redaction (Comprehend, STRICT) sanitizes the synthesis/vector path; the raw read_source mirror is IAM + KMS-protected; Bedrock does not store/train on data; content is primarily code + technical documentation (ADR-051) |

### 7.2 Risks Requiring Remediation

| Risk | Residual Level | Recommended Action | Priority |
|---|---|---|---|
| Indefinite chat retention (R8) | Medium | Implement TTL on DynamoDB chat table aligned with organizational retention policy | High |
| No user offboarding process (R8) | Medium | Implement process to purge user data (conversations, API keys, profile) when employees leave | High |
| No S3 Vectors lifecycle (R10) | Low | Add lifecycle rules to S3 Vectors bucket for stale workspace data | Low |
| OAuth token compromise blast radius (R2) | High (if exploited) | Implement token rotation schedule; monitor for anomalous API usage on connected services | Medium |
| Raw PII in the read_source mirror (R11) | Low | Ingest PII scanning is IMPLEMENTED (Comprehend, STRICT default, ADR-051). Residual is the raw source mirror -- for highly sensitive content, add an S3 lifecycle/TTL on the mirror prefix or tighten bucket access | Low |

---

## 8. Third-Party Data Processors

### 8.1 Amazon Web Services (AWS)

| Attribute | Detail |
|---|---|
| Role | Infrastructure provider and AI model host |
| Services Used | EC2/Fargate, Lambda, S3, DynamoDB, Bedrock, CloudWatch, KMS, ALB, Route53, ACM, SSM, SNS, SQS, EventBridge |
| Data Location | Configurable AWS region (default: us-west-2) |
| Data Processing Agreement | AWS Enterprise Agreement / standard DPA |
| Sub-processor | Anthropic (Claude model via Bedrock); no direct data access by Anthropic |
| Data retention by AWS | Bedrock: prompts/completions not stored or used for training; other services: customer-controlled retention |

### 8.2 Anthropic (via AWS Bedrock)

| Attribute | Detail |
|---|---|
| Role | AI model provider (Claude) |
| Access method | Exclusively through AWS Bedrock managed service |
| Direct data access | None -- Anthropic does not receive, store, or access customer data directly |
| Model training | Customer data is NOT used for model training via Bedrock |
| Data retention | Prompts and completions are processed in transit and discarded after response generation |

### 8.3 OutcomeOps

| Attribute | Detail |
|---|---|
| Role | Software provider |
| Data access | None -- OutcomeOps does not have access to customer AWS accounts, data, or infrastructure |
| Deployment model | Customer-deployed; software runs entirely within the customer's AWS account |
| Support | Does not require access to production data; debugging via customer-shared logs if needed |

---

## 9. Identified Gaps

The following gaps were identified from code review where data is processed but controls are unclear or missing:

| Gap | Description | Affected Component |
|---|---|---|
| No chat retention policy | DynamoDB chat table has no TTL; conversations persist indefinitely | `dynamodb-chat.tf` |
| No user data cleanup | No automated process to remove user data when employees leave the organization | `lambda/workspace-management/handler.py` |
| No S3 Vectors lifecycle | Vector embeddings bucket has no lifecycle rules for stale data | `s3-vectors.tf` |
| Raw source mirror unredacted at rest | Ingested content IS scanned + redacted (Comprehend, STRICT) before embedding into the vector store; the residual is the raw source mirror kept unredacted for read_source fidelity (ADR-051), protected by IAM + KMS | `lambda/shared/pii.py`, `lambda/process-documents/handler.py` |
| No inference-time redaction to Bedrock | User prompt sent as-is; the KB context is already ingest-redacted (ADR-051); no additional redaction at model-call time | `lambda/chat/handler.py` |
| No AI pre-use notice | No explicit notice to users that they are interacting with an AI system | `ui/src/` (pending implementation) |
| Workspaces table no TTL | User profiles, memberships, and workspace data persist indefinitely | `dynamodb-workspaces.tf` |
| Code maps table no TTL | Ingestion state tracking data persists indefinitely | `dynamodb.tf` |
| Refusal alerts contain user content | SNS email alerts include first 500 characters of user input and AI output | `lambda/audit-alert/handler.py` |

---

## 10. Review Schedule

This DPIA should be reviewed:

- Annually (next review: 2027-03-12)
- When new data sources are added to the ingestion pipeline
- When new AI models are integrated
- When new third-party integrations are added
- When significant changes are made to data storage or retention
- When deploying to new regulatory jurisdictions

---

## Appendix A: Data Store Reference

| Store | Table/Bucket Name Pattern | Terraform Source |
|---|---|---|
| DynamoDB Audit | `{env}-{app_name}-audit-logs` | `dynamodb-audit.tf` |
| DynamoDB Chat | `{env}-{app_name}-chat` | `dynamodb-chat.tf` |
| DynamoDB Workspaces | `{env}-{app_name}-workspaces` | `dynamodb-workspaces.tf` |
| DynamoDB Code Maps | `{env}-{app_name}-code-maps` | `dynamodb.tf` |
| S3 Knowledge Base | `{env}-{app_name}-kb-{account_id}` | `s3.tf` |
| S3 Vectors | `{env}-{app_name}-vectors` | `s3-vectors.tf` |
| CloudWatch Logs | `/aws/lambda/{env}-{app_name}-*` | Various Lambda TF files |

## Appendix B: Encryption Key Reference

| Key | Purpose | Rotation | Terraform Source |
|---|---|---|---|
| CloudWatch KMS | Encrypts all Lambda log groups | Enabled | `kms.tf` |
| OAuth KMS | Encrypts OAuth tokens and MCP credentials in DynamoDB | Enabled | `kms-oauth.tf` |
| S3 SSE (AES256) | Encrypts S3 objects at rest | AWS-managed | `s3.tf`, `s3-vectors.tf` |
| DynamoDB SSE | Encrypts table data at rest | AWS-managed | Default on all tables |
