# Feature: AI-Enhanced Pull Request Analysis

## Summary

Extend the existing `analyze-pr` Lambda to incorporate AI-powered semantic analysis and contextual commenting for Pull Requests.
This elevates OutcomeOps from deterministic rule checks to intelligent, standards-aware code review—fully self-hosted within enterprise boundaries.

---

## Goals

1. Perform **semantic understanding of code diffs** to identify logical change types (auth flow, data model, infra change, etc.).
2. Generate **contextual AI review comments** aligned with organizational ADRs and standards.
3. Integrate **Guardrails-based moderation** to ensure professional tone and policy compliance.
4. Expose a **status dashboard endpoint** (`/api/pr-status`) summarizing queued checks and AI comment results.
5. Enable **configurable rule sets** via SSM (e.g., toggle specific check types per org).

---

## Technical Approach

### 1. Semantic Diff Analysis

- Add embedding generation for each changed file or diff hunk using Bedrock Titan or Claude embeddings.
- Classify change type using a lightweight classifier or similarity search against existing vectorized ADRs/code maps.
- Append `change_type` metadata to SQS messages for targeted check selection.

### 2. AI Comment Generation

- For each check job, invoke Claude Sonnet 4.5 (Bedrock) with:
  - Changed file content
  - Relevant ADRs and standards
  - Detected `change_type`
- Generate actionable comments such as:
  > _"This Lambda handler modifies the API contract defined in ADR-002. Ensure backward compatibility or increment version."_
- Post comments through GitHub API.

### 3. Guardrails Integration

- Wrap model responses in NeMo Guardrails or local moderation logic.
- Enforce tone, safety, and compliance rules for all AI comments.

### 4. PR Status Dashboard

- Aggregate queued check states and results in DynamoDB.
- Expose REST endpoint `/api/pr-status` returning:
  ```json
  {"pr_number":123,"checks":[{"type":"ADR_COMPLIANCE","status":"PASS"}]}
  ```
- Optional: render in admin UI.

### 5. Configurable Rules

- Add SSM JSON parameter:
  ```
  /{env}/{app}/config/pr-analysis
  ```

  Example:
  ```json
  {
    "adr_compliance": true,
    "readme_freshness": true,
    "test_coverage": false,
    "semantic_diff": true
  }
  ```

---

## Expected Impact

- Reduces manual code review load by ~60%.
- Enforces standards automatically through AI-generated comments.
- Positions OutcomeOps as a **Greptile-class**, enterprise-grade, context-aware review system.

---

## Dependencies

- Bedrock (Claude Sonnet 4.5 or Titan Embeddings)
- NeMo Guardrails (optional)
- DynamoDB (PR status tracking)
- Existing SQS + GitHub API integration

---

## Related Docs

- [Lambda: Analyze PR](../lambda-analyze-pr.md)
- [Architecture Overview](../architecture.md)
- [ADR-003: Git Commit Standards](../adr/ADR-003-git-commit-standards.md)
