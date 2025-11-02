# ADR-004: Lambda Handler Standards

## Status: Accepted

## Context

OutcomeOps AI Assist Lambda handlers form the core of the serverless API. Establishing consistent patterns across all handlers ensures maintainability, security, and reliability. This document standardizes handler structure, authentication, validation, error handling, and configuration management.

## Decision

### 1. Handler Structure and Entry Point

Every Lambda handler follows this structure:

```python
# lambda/my_handler/handler.py
import boto3
import os
import json
import logging
from jwt_helper import decode_token
from pydantic import BaseModel, ValidationError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients (once per container)
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
s3 = boto3.client("s3")

# Load environment variables
env = os.environ.get("ENV", "dev")
app_name = os.environ.get("APP_NAME", "myfantasy")
region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Load configuration from SSM Parameter Store (happens once per container)
table_param = f"/{env}/{app_name}/dynamodb/table"
TABLE_NAME = ssm.get_parameter(Name=table_param)["Parameter"]["Value"]
table = dynamodb.Table(TABLE_NAME)

# CORS Response Helper
def _response(status_code, body):
    """Return standardized API response with CORS headers"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Idempotency-Key",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST,PUT,DELETE"
        },
        "body": json.dumps(body)
    }

# Main handler
def lambda_handler(event, context):
    """Main Lambda entry point"""
    try:
        # 1. Authenticate user
        claims = decode_token(event)
        user_email = claims.get("email") if claims else None
        if not user_email:
            return _response(401, {"error": "Unauthorized"})

        # 2. Extract and validate input
        body = json.loads(event.get("body", "{}"))
        try:
            input_data = MyHandlerInput(**body)
        except ValidationError as e:
            return _response(400, {"error": f"Validation error: {e.errors()}"})

        # 3. Authorize user
        user_item = table.get_item(Key={"PK": f"USER#{user_email}", "SK": "PROFILE"}).get("Item", {})
        if not user_item:
            return _response(404, {"error": "User not found"})

        # 4. Execute business logic
        result = business_logic(input_data, user_email)

        # 5. Return response
        return _response(200, result)

    except Exception as e:
        logger.error(f"[my_handler] Unexpected error: {str(e)}", exc_info=True)
        return _response(500, {"error": "Internal server error"})
```

### 2. CORS Response Helper

All handlers use a standardized CORS response helper:

```python
def _response(status_code, body):
    """
    Return standardized API response with CORS headers.

    Args:
        status_code: HTTP status code (200, 400, 401, 403, 404, 500, etc.)
        body: Response body as dict (will be JSON-encoded)

    Returns:
        AWS Lambda proxy integration response
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Idempotency-Key",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST,PUT,DELETE"
        },
        "body": json.dumps(body)
    }
```

Eventually, move this to a Lambda layer to avoid duplication.

### 3. Authentication and Authorization

**Authentication:** Extract and verify JWT token from request

```python
from jwt_helper import decode_token

# In lambda_handler
claims = decode_token(event)  # Returns decoded claims or None if invalid
user_email = claims.get("email") if claims else None

if not user_email:
    return _response(401, {"error": "Unauthorized"})
```

**Authorization:** Verify user has permission for the action

```python
# Get user from DynamoDB
user_item = table.get_item(Key={"PK": f"USER#{user_email}", "SK": "PROFILE"}).get("Item", {})

# Check role
user_role = user_item.get("role", "")
if user_role not in ["creator", "admin"]:
    return _response(403, {"error": "Access denied"})

# Check account status
if user_item.get("account_status") == "disabled":
    return _response(403, {"error": "Account disabled"})
```

### 4. Input Validation with Pydantic

Use Pydantic for input validation instead of manual string checks:

```python
from pydantic import BaseModel, Field, validator
from typing import Optional

class CreateCharacterInput(BaseModel):
    """Validate character creation input"""
    name: str = Field(..., min_length=1, max_length=255)
    genre: str = Field(..., min_length=1)
    gender: Optional[str] = "female"
    species: Optional[str] = "Human"
    age_appearance: Optional[int] = None
    subscription_price: Optional[float] = 20.0

    @validator('age_appearance')
    def validate_age(cls, v):
        if v is not None and (v < 13 or v > 100):
            raise ValueError('age_appearance must be between 13 and 100')
        return v

    @validator('subscription_price')
    def validate_price(cls, v):
        if v < 0:
            raise ValueError('subscription_price must be non-negative')
        return v

# In lambda_handler
try:
    input_data = CreateCharacterInput(**body)
except ValidationError as e:
    return _response(400, {
        "error": "Validation failed",
        "details": e.errors()
    })
```

**Benefits of Pydantic:**
- Type checking and coercion
- Automatic validation rules (min_length, max_length, regex, etc.)
- Custom validators for complex rules
- Clear error messages
- Self-documenting code

### 5. Configuration Management

**Load configuration once at container startup:**

```python
# These are loaded once per Lambda container and cached
env = os.environ.get("ENV", "dev")
app_name = os.environ.get("APP_NAME", "myfantasy")

# From SSM Parameter Store
table_param = f"/{env}/{app_name}/dynamodb/table"
TABLE_NAME = ssm.get_parameter(Name=table_param)["Parameter"]["Value"]
table = dynamodb.Table(TABLE_NAME)

# Additional resources
bucket_param = f"/{env}/{app_name}/s3/images"
IMAGES_BUCKET = ssm.get_parameter(Name=bucket_param)["Parameter"]["Value"]
```

**Parameter Store naming convention:**
```
/{env}/{app_name}/dynamodb/table
/{env}/{app_name}/s3/{resource_name}
/{env}/{app_name}/cloudfront/key_pair_id
/{env}/{app_name}/auth/jwt_secret
```

**Environment variables set in Terraform:**
```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 7.0"

  environment_variables = {
    ENV             = var.environment
    APP_NAME        = var.app_name
    AWS_DEFAULT_REGION = var.region
  }
}
```

### 6. Error Handling

**Standardized error handling with logging:**

```python
def lambda_handler(event, context):
    try:
        # ... authentication, validation, business logic ...
        return _response(200, result)

    except ValidationError as e:
        # Client error - malformed input
        logger.warning(f"[handler_name] Validation error: {str(e)}")
        return _response(400, {
            "error": "Validation failed",
            "details": e.errors()
        })

    except ValueError as e:
        # Client error - business logic validation
        logger.warning(f"[handler_name] Business logic error: {str(e)}")
        return _response(400, {"error": str(e)})

    except KeyError as e:
        # Client error - missing required field
        logger.warning(f"[handler_name] Missing field: {str(e)}")
        return _response(400, {"error": f"Missing required field: {str(e)}"})

    except Exception as e:
        # Unexpected error - log and return 500
        logger.error(f"[handler_name] Unexpected error: {str(e)}", exc_info=True)
        return _response(500, {"error": "Internal server error"})
```

**Error response format:**

```json
{
  "statusCode": 400,
  "body": {
    "error": "Brief error message",
    "details": ["Optional", "detailed", "error", "information"]
  }
}
```

**HTTP Status Codes:**
- `200` - Success
- `201` - Created
- `204` - No Content (successful DELETE/update with no response body)
- `400` - Bad Request (validation error, missing field, invalid input)
- `401` - Unauthorized (no valid JWT token)
- `403` - Forbidden (authorized but not allowed)
- `404` - Not Found (resource doesn't exist)
- `409` - Conflict (business logic prevents action, e.g., duplicate email)
- `500` - Internal Server Error (unexpected exception)

### 7. DynamoDB Access Patterns

**Single item retrieval:**

```python
user = table.get_item(
    Key={"PK": f"USER#{user_email}", "SK": "PROFILE"}
).get("Item", {})

if not user:
    return _response(404, {"error": "User not found"})
```

**Query by partition key:**

```python
from boto3.dynamodb.conditions import Key, Attr

response = table.query(
    KeyConditionExpression=Key("PK").eq(f"CHARACTER#{character_id}"),
    FilterExpression=Attr("status").eq("active")
)

items = response.get("Items", [])
```

**Pagination:**

```python
last_key = None
all_items = []

while True:
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(partition_key),
        "Limit": 25
    }
    if last_key:
        kwargs["ExclusiveStartKey"] = last_key

    response = table.query(**kwargs)
    all_items.extend(response.get("Items", []))
    last_key = response.get("LastEvaluatedKey")

    if not last_key:
        break
```

**Full table scans (use helper from jwt_helper.py):**

```python
from jwt_helper import full_table_scan
from boto3.dynamodb.conditions import Attr

results = full_table_scan(
    table,
    filter_expression=Attr("role").eq("admin"),
    projection_expression="PK,SK,email,role"
)
```

### 8. External Service Integration

**S3 Integration:**

```python
# Get object
try:
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(obj["Body"].read())
except s3.exceptions.NoSuchKey:
    return _response(404, {"error": "Object not found"})
except Exception as e:
    logger.error(f"[handler] S3 get_object failed: {str(e)}")
    return _response(500, {"error": "Failed to retrieve object"})

# Put object
try:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data),
        ContentType="application/json"
    )
except Exception as e:
    logger.error(f"[handler] S3 put_object failed: {str(e)}")
    return _response(500, {"error": "Failed to store object"})
```

**SQS Integration (sending messages):**

```python
sqs = boto3.client("sqs")

try:
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "character_id": character_id,
            "user_email": user_email
        })
    )
except Exception as e:
    logger.error(f"[handler] SQS send_message failed: {str(e)}")
    return _response(500, {"error": "Failed to queue job"})
```

### 9. Logging Standards

**Use structured logging:**

```python
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# In handler
logger.info(f"[handler_name] User created character: {character_id}")
logger.warning(f"[handler_name] Validation failed for user: {user_email}")
logger.error(f"[handler_name] DynamoDB error: {str(e)}", exc_info=True)
```

**Log format guidelines:**
- Start with handler name in brackets: `[handler_name]`
- Use appropriate log levels: INFO for actions, WARNING for non-blocking issues, ERROR for exceptions
- Include relevant context (user_email, resource_id, etc.)
- Use `exc_info=True` for exceptions to include stack trace

### 10. API Gateway Integration

Routes are defined in `terraform/api_gateway.tf`:

```hcl
module "api_gateway" {
  source  = "terraform-aws-modules/apigateway-v2/aws"
  version = "~> 4.0"

  name          = "${var.environment}-${var.app_name}-api"
  description   = "API Gateway for OutcomeOps AI Assist"
  protocol_type = "HTTP"

  integrations = {
    # GET routes
    "GET /api/profile" = {
      integration = {
        uri                    = module.profile_handler_lambda.lambda_function_invoke_arn
        integration_type       = "AWS_PROXY"
        integration_method     = "POST"
        payload_format_version = "2.0"
      }
    }

    # POST routes
    "POST /api/save-profile" = {
      integration = {
        uri                    = module.profile_handler_lambda.lambda_function_invoke_arn
        integration_type       = "AWS_PROXY"
        integration_method     = "POST"
        payload_format_version = "2.0"
      }
    }

    # Use AWS_PROXY integration type (Lambda Proxy)
    # This passes entire request to Lambda and expects standardized response
  }
}
```

Handler just returns response from `_response()` function - API Gateway handles the rest.

### 11. Handler File Organization

```
lambda/
├── my_handler/
│   ├── handler.py              # Main Lambda handler
│   ├── requirements.txt         # Python dependencies
│   ├── __tests__/
│   │   └── test_handler.py     # Unit tests
│   └── business_logic.py        # Optional: extracted business functions
├── jwt_helper.py               # Shared auth utilities (will become Lambda layer)
└── utils.py                    # Optional: shared utilities
```

### 12. Handler Checklist

Before considering a handler complete:

- [ ] Authenticate user with `decode_token(event)`
- [ ] Validate input with Pydantic model and catch `ValidationError`
- [ ] Authorize user (check role, account_status, permissions)
- [ ] Handle all known error cases (400, 403, 404, etc.)
- [ ] Catch unexpected exceptions and return 500
- [ ] Use structured logging with handler name
- [ ] Test successful path
- [ ] Test validation error (400)
- [ ] Test unauthorized (401)
- [ ] Test forbidden (403)
- [ ] Test not found (404)
- [ ] Update README.md with endpoint documentation
- [ ] Update terraform/api_gateway.tf with new route

## Consequences

### Positive
- Consistent handler structure across all endpoints
- Strong input validation with Pydantic reduces bugs
- Clear error handling and logging for debugging
- Security through authentication and authorization on every handler
- Easy to onboard developers (and Claude Code) with clear patterns
- Configuration centralized in SSM and Terraform

### Tradeoffs
- Requires Pydantic dependency (lightweight, very worth it)
- Initial setup more verbose than manual validation (pays off immediately)
- SSM Parameter Store adds ~50ms to cold start (acceptable tradeoff for flexibility)

## Implementation

### Starting today
1. Use Pydantic for all new handler input validation
2. Follow authentication/authorization pattern in every handler
3. Use `_response()` helper for all responses
4. Catch specific exceptions before general Exception
5. Log with handler name prefix

### Next phases
1. Extract `_response()` to Lambda layer
2. Migrate jwt_helper.py to full utility layer
3. Create Pydantic model library for common input types
4. Add distributed tracing with X-Ray

## Related ADRs

- ADR-002: Development Workflow Standards
- ADR-003: Testing Standards
- ADR-001: Terraform Infrastructure Patterns

## References

- Pydantic Documentation: https://docs.pydantic.dev/
- AWS Lambda Proxy Integration: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html
- boto3 DynamoDB: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html
- AWS Lambda Logging Best Practices: https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html

Version History:
- v1.0 (2025-01-02): Initial Lambda handler standards for OutcomeOps AI Assist
