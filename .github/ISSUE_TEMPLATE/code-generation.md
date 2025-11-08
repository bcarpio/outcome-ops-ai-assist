---
name: Code Generation Request
about: Request AI-assisted code generation for a new feature
title: '[Component]: [Action] [Feature]'
labels: ['needs-review']
assignees: ''
---

# Code Generation Request

This template captures story-specific information for AI-assisted code generation. The system will query the knowledge base for general patterns (Lambda standards, Terraform standards, testing requirements), so focus on what's unique to this feature.

After completing this template, add the `approved-for-generation` label to trigger code generation.

---

## User Story

```
As a [user/system/service],
I want to [action/capability],
So that [business outcome].
```

---

## Handler Specification

**Handler Name:** `[kebab-case-name]`

**Trigger Type:** (API Gateway / SQS / EventBridge / S3 Event / DynamoDB Stream / Direct Invoke / Scheduled)

**Timeout:** [seconds] (default: 30, max: 900)

**Memory:** [MB] (default: 512)

---

## Request Payload

```json
{
  "field1": "string",
  "field2": 123,
  "field3": true
}
```

**Validation Rules:**

- `field1`: Required, string, format: UUID
- `field2`: Optional, number, min: 1, max: 100, default: 10
- `field3`: Required, boolean

---

## Response Payload

```json
{
  "result": "string",
  "timestamp": "2025-01-05T12:00:00Z"
}
```

**Status Codes:**

- 200: Success
- 400: Validation error
- 404: Resource not found
- 500: Internal error

---

## AWS Resources Needed

**DynamoDB Tables:**

- `outcome-ops-ai-assist-[env]-table-name` (existing/new) - Read/Write/Full access

**S3 Buckets:**

- `outcome-ops-ai-assist-[env]-bucket-name` (existing/new) - Read/Write access

**SQS Queues:**

- `outcome-ops-ai-assist-[env]-queue-name` (existing/new) - Send/Receive messages

**SSM Parameters:**

- `/[env]/outcome-ops-ai-assist/parameter-name` - Read access

**Other Lambdas to Invoke:**

- `outcome-ops-ai-assist-[env]-lambda-name` - Invoke permission

**Other AWS Services:**

- Bedrock (model: anthropic.claude-sonnet-4-5)
- EventBridge (rule: rule-name)
- etc.

---

## Environment Variables

| Name             | Description          | Example Value                   |
| ---------------- | -------------------- | ------------------------------- |
| `ENV`            | Environment name     | `dev`                           |
| `APP_NAME`       | Application name     | `outcome-ops-ai-assist`         |
| `DYNAMODB_TABLE` | DynamoDB table name  | `dev-outcome-ops-ai-assist-...` |
| `QUEUE_URL`      | SQS queue URL        | `https://sqs.us-west-2...`      |

---

## Business Logic

Describe the specific processing flow for this handler:

1. [First step - e.g., Validate request payload]
2. [Second step - e.g., Query DynamoDB for existing record (PK=..., SK=...)]
3. [Third step - e.g., Process/transform data]
4. [Fourth step - e.g., Write results to DynamoDB]
5. [Final step - e.g., Return response]

**Error Handling:**

- [Condition] → [Response code] with [error details]
- [Condition] → [Response code], log error, [action]
- [Condition] → [Warning log but don't fail]

**Special Cases:**

- [Edge case description] → [Expected behavior]
- [Edge case description] → [Expected behavior]

---

## Test Scenarios

### Success Cases

1. [Description] → [Expected outcome with status code]
2. [Description] → [Expected outcome with status code]
3. [Description] → [Expected outcome with status code]

### Error Cases

1. [Description] → [Expected error response]
2. [Description] → [Expected error response]
3. [Description] → [Expected error response]

### Edge Cases

1. [Description] → [Expected behavior]
2. [Description] → [Expected behavior]

---

## Example (Remove this section after filling template)

### Title

`Lambda: Add user preference update endpoint`

### User Story

```
As a frontend application,
I want to update user notification preferences via API,
So that users can control their notification settings.
```

### Handler Specification

- **Handler Name:** `update-user-preferences`
- **Trigger Type:** API Gateway (POST /users/{userId}/preferences)
- **Timeout:** 30 seconds
- **Memory:** 512 MB

### Request Payload

```json
{
  "userId": "550e8400-e29b-41d4-a716-446655440000",
  "preferences": {
    "emailNotifications": true,
    "pushNotifications": false,
    "theme": "dark"
  }
}
```

**Validation Rules:**

- `userId`: Required, string, format: UUID
- `preferences.emailNotifications`: Optional, boolean, default: true
- `preferences.pushNotifications`: Optional, boolean, default: true
- `preferences.theme`: Optional, enum: [light, dark], default: light

### Response Payload

```json
{
  "userId": "550e8400-e29b-41d4-a716-446655440000",
  "preferences": {
    "emailNotifications": true,
    "pushNotifications": false,
    "theme": "dark"
  },
  "updatedAt": "2025-01-05T12:00:00Z"
}
```

**Status Codes:**

- 200: Success
- 400: Invalid request
- 404: User not found
- 500: Server error

### AWS Resources Needed

**DynamoDB Tables:**

- `outcome-ops-ai-assist-dev-users` (existing) - GetItem, PutItem

### Environment Variables

| Name             | Description | Example Value                     |
| ---------------- | ----------- | --------------------------------- |
| `DYNAMODB_TABLE` | Users table | `dev-outcome-ops-ai-assist-users` |

### Business Logic

1. Validate request payload (userId format, preferences structure)
2. Get user from DynamoDB (PK=userId, SK=METADATA)
3. If user not found, return 404 with error message
4. Merge incoming preferences with existing preferences (partial update)
5. Update user record in DynamoDB with merged preferences
6. Return updated preferences with ISO 8601 timestamp

**Error Handling:**

- Invalid userId format → 400 with validation error details
- User not found → 404 with "User not found" message
- DynamoDB GetItem error → 500, log error with request ID
- DynamoDB PutItem error → 500, log error with request ID

**Special Cases:**

- First-time preference update → Initialize with default values
- Partial preference update → Preserve unspecified fields

### Test Scenarios

#### Success Cases

1. Update all preference fields → 200 with complete preferences object
2. Update single preference field → 200 with merged preferences
3. Update with same values as existing → 200 with no changes

#### Error Cases

1. Missing userId field → 400 with validation error
2. Invalid userId format (not UUID) → 400 with format error
3. User does not exist → 404 with user not found message
4. Invalid preference value type → 400 with type error
5. DynamoDB unavailable → 500 with internal error

#### Edge Cases

1. Concurrent updates to same user → Last write wins (DynamoDB behavior)
2. Empty preferences object → 200 with existing preferences unchanged
3. Unknown preference fields → Ignored, only known fields updated

---

## Template Tips

### Focus on Story-Specific Details

Provide details unique to THIS feature:

- THIS endpoint's specific request/response structure
- THIS feature's specific business logic flow
- THIS handler's specific AWS resource requirements
- THIS feature's specific test scenarios and edge cases

### Don't Duplicate Knowledge Base Content

The knowledge base already contains:

- Lambda handler structure patterns (ADR-004)
- Pydantic validation patterns
- Error handling standards
- Testing standards (ADR-003)
- Terraform module patterns (ADR-002)
- IAM permission patterns
- DynamoDB key conventions (PK/SK)

### Be Specific

Good examples:

- "Get user from DynamoDB users table (PK=userId, SK=METADATA)"
- "Return 404 if user not found with message 'User not found'"
- "Merge preferences: preserve unspecified fields, update specified fields"

Avoid vague descriptions:

- "Process the data appropriately"
- "Handle errors correctly"
- "Store in database"

### Include Concrete Examples

- Show example payloads with realistic values
- List specific field names, types, and constraints
- Describe specific error scenarios with expected status codes
- Include actual error messages to return

---

## Workflow

1. Fill out this template with all required information
2. Review for completeness and clarity
3. Add the `approved-for-generation` label to trigger code generation
4. The AI will:
   - Generate an execution plan in a branch
   - Create code following standards from knowledge base
   - Generate tests based on test scenarios
   - Create a pull request for review
5. Review the generated code and tests
6. Request changes or merge the PR
