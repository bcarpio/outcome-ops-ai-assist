# ADR-003: Testing Standards

## Status: Accepted

## Context

MyFantasy.ai is a Python-based serverless application built on AWS Lambda. As the codebase grows, having consistent testing practices ensures code quality, prevents regressions, and maintains confidence in deployments. This document establishes testing standards for unit tests, integration tests, and testing best practices.

## Decision

### 1. Testing Philosophy

A story is DONE when tests covering new functionality are written, passing locally, and passing in CI.

**Core principles:**
- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)
- Study and follow the Test Pyramid (many unit tests, fewer integration tests, minimal functional tests)

### 2. Testing Framework and Structure

**Framework:** pytest with pytest-cov for coverage reporting

**Directory structure:**
```
tests/
├── unit/
│   ├── __init__.py
│   ├── test_handler_1.py
│   ├── test_handler_2.py
│   └── test_utils.py
├── integration/
│   ├── __init__.py
│   └── test_api_flows.py
└── fixtures/
    ├── __init__.py
    ├── sample_events.py
    ├── sample_responses.py
    └── mock_data.py
```

**Running tests:**
```bash
# Run all tests
make test

# Run only unit tests
make test-unit

# Run only integration tests
make test-integration

# Run with coverage report
make test-coverage

# Run specific test file
pytest tests/unit/test_handler_1.py -v

# Run specific test
pytest tests/unit/test_handler_1.py::test_valid_input -v
```

### 3. Test Types and Definitions

**Unit Tests:**
- Test a single function or method in isolation
- Do not depend on external services (AWS, databases, APIs)
- Should be fast (< 100ms per test ideally)
- Should comprise the bulk of test suite (60-70%)
- Use mocking/stubbing for dependencies
- Live in `tests/unit/`

**Integration Tests:**
- Test modules/components as they interact with each other
- May invoke external services (AWS DynamoDB, S3, etc.)
- Require AWS credentials and access to dev environment
- Slower to run (multiple seconds)
- Should be limited in scope (10-20% of tests)
- Live in `tests/integration/`

**Functional/API Tests:**
- Test slices of functionality end-to-end
- Exercise full request/response flows
- May depend on running infrastructure
- Slowest and most expensive (5-10% of tests)
- Used for critical user journeys
- Can be separate from main test suite

**Test Pyramid for MyFantasy.ai:**
```
         Functional Tests
            (few, slow)
         /---------------\
      Integration Tests
      (moderate, medium)
    /-----------------------\
      Unit Tests
    (many, fast)
  /-------------------------------\
```

### 4. Unit Testing Best Practices

**Structure: Arrange-Act-Assert pattern**

```python
def test_create_character_with_valid_input():
    # Arrange - Set up test data
    input_data = {
        "name": "Elvira",
        "gender": "female",
        "species": "Elf"
    }

    # Act - Execute the function
    result = create_character(input_data)

    # Assert - Verify the result
    assert result["character_id"] is not None
    assert result["name"] == "Elvira"
    assert result["status"] == "pending_generation"
```

**Test naming convention:**
- `test_<function_name>_<scenario>`
- Examples:
  - `test_create_character_with_valid_input`
  - `test_create_character_with_missing_name`
  - `test_dynamodb_put_handles_throttling`

**Test one thing per test:**
```python
# Good - tests one behavior
def test_create_character_generates_unique_id():
    result = create_character(valid_input)
    assert result["character_id"] != None

# Bad - tests multiple things
def test_create_character():
    result = create_character(valid_input)
    assert result["character_id"] != None
    assert result["name"] == "Elvira"
    assert result["status"] == "pending"
    assert len(result["avatar_url"]) > 0
```

**Use fixtures for common test data:**

```python
# tests/fixtures/sample_events.py
import pytest

@pytest.fixture
def valid_character_input():
    return {
        "character_id": "char-123",
        "name": "Elvira Moonwhisper",
        "gender": "female",
        "species": "Elf",
        "age_appearance": "25"
    }

@pytest.fixture
def invalid_character_input():
    return {
        "character_id": "char-123"
        # missing required fields
    }

# tests/unit/test_character.py
from tests.fixtures.sample_events import valid_character_input

def test_create_character(valid_character_input):
    result = create_character(valid_character_input)
    assert result["status"] == "pending_generation"
```

### 5. Testing Negative Cases and Edge Conditions

**Test error conditions (negative testing):**

```python
def test_create_character_with_invalid_gender():
    input_data = {
        "name": "Elvira",
        "gender": "invalid_gender"
    }

    with pytest.raises(ValueError) as exc_info:
        create_character(input_data)

    assert "Gender must be one of:" in str(exc_info.value)

def test_create_character_missing_name():
    input_data = {
        "gender": "female"
    }

    with pytest.raises(KeyError) as exc_info:
        create_character(input_data)

    assert "name" in str(exc_info.value)
```

**Test boundary values:**

```python
def test_character_name_max_length():
    long_name = "A" * 256  # Exceeds max length

    with pytest.raises(ValueError) as exc_info:
        create_character({"name": long_name})

    assert "max length" in str(exc_info.value)

def test_character_name_empty_string():
    with pytest.raises(ValueError):
        create_character({"name": ""})

def test_character_age_appearance_boundary():
    # Test minimum
    result = create_character({"age_appearance": "13"})
    assert result is not None

    # Test below minimum
    with pytest.raises(ValueError):
        create_character({"age_appearance": "12"})
```

**Test exception handling:**

```python
@patch('lambda.character_handler.dynamodb_client')
def test_create_character_handles_dynamodb_error(mock_dynamodb):
    mock_dynamodb.put_item.side_effect = Exception("DynamoDB error")

    with pytest.raises(Exception) as exc_info:
        create_character(valid_input)

    assert "DynamoDB error" in str(exc_info.value)
```

### 6. Mocking and Stubbing

**Mock AWS services for unit tests:**

```python
from unittest.mock import patch, MagicMock
import pytest

@patch('lambda.character_handler.dynamodb_client')
def test_create_character_saves_to_dynamodb(mock_dynamodb):
    # Arrange
    mock_dynamodb.put_item.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

    # Act
    result = create_character(valid_input)

    # Assert
    assert result["status"] == "created"
    mock_dynamodb.put_item.assert_called_once()
    call_args = mock_dynamodb.put_item.call_args
    assert call_args[1]["Item"]["name"] == "Elvira"

@patch('lambda.character_handler.s3_client')
def test_create_character_uploads_avatar(mock_s3):
    # Mock the S3 upload
    mock_s3.put_object.return_value = {"ETag": "abc123"}

    result = create_character(valid_input)

    mock_s3.put_object.assert_called_once()
```

**Use pytest mocking fixtures:**

```python
@pytest.fixture
def mock_dynamodb():
    with patch('lambda.character_handler.dynamodb_client') as mock:
        yield mock

@pytest.fixture
def mock_s3():
    with patch('lambda.character_handler.s3_client') as mock:
        yield mock

def test_character_creation_flow(mock_dynamodb, mock_s3):
    mock_dynamodb.put_item.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    mock_s3.put_object.return_value = {"ETag": "abc123"}

    result = create_character(valid_input)

    assert result["character_id"] is not None
    mock_dynamodb.put_item.assert_called_once()
    mock_s3.put_object.assert_called_once()
```

### 7. Integration Testing

**When to use integration tests:**
- Testing Lambda handler with mocked DynamoDB/S3 (integration of components)
- Testing API Gateway -> Lambda -> DynamoDB flow in dev environment
- Testing SQS message processing
- Do not use for simple unit tests of business logic

**Integration test requirements:**
- Must have AWS credentials configured
- Should use dev environment (not prd)
- Can be slower (multiple seconds)
- Keep integration tests focused (test one flow at a time)

**Example integration test:**

```python
import boto3
import pytest
from moto import mock_dynamodb

@mock_dynamodb
def test_character_creation_integration():
    # Use moto to mock DynamoDB
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.create_table(
        TableName='dev-myfantasy-main',
        KeySchema=[
            {'AttributeName': 'PK', 'KeyType': 'HASH'},
            {'AttributeName': 'SK', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'PK', 'AttributeType': 'S'},
            {'AttributeName': 'SK', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )

    # Test the full flow
    result = create_character(valid_input)

    # Verify item was stored in DynamoDB
    response = table.get_item(Key={'PK': f"CHAR#{result['character_id']}"})
    assert 'Item' in response
```

### 8. Test Coverage Goals

**Phase 1 (Now):** Establish testing patterns for critical Lambda handlers
- Focus on new handlers as you build them
- Aim for 70%+ coverage on new code
- Document test patterns for developers (including Claude Code)

**Phase 2 (Next):** Add tests for existing critical handlers
- Character creation and management
- Subscription and payment flows
- Content generation handlers

**Phase 3 (Future):** Expand coverage to 80%+ across all critical paths

**Check coverage:**
```bash
make test-coverage
# Opens HTML coverage report
```

### 9. Makefile Test Commands

Include these targets in your Makefile:

```makefile
.PHONY: test test-unit test-integration test-coverage

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-coverage:
	pytest tests/ --cov=lambda --cov=src --cov-report=html
	open htmlcov/index.html

test-watch:
	pytest-watch tests/ -v
```

### 10. Testing Lambda Handlers

**Lambda handler testing pattern:**

```python
# lambda/create_character_lambda/handler.py
def lambda_handler(event, context):
    try:
        character_id = event.get('character_id')
        if not character_id:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "character_id required"})
            }

        result = create_character(event)

        return {
            "statusCode": 201,
            "body": json.dumps(result)
        }
    except Exception as e:
        logger.error(f"Error creating character: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"})
        }

# tests/unit/test_create_character_lambda.py
@patch('lambda.create_character_lambda.handler.create_character')
def test_lambda_handler_valid_input(mock_create):
    mock_create.return_value = {
        "character_id": "char-123",
        "name": "Elvira"
    }

    event = {
        "character_id": "char-123",
        "name": "Elvira"
    }

    response = lambda_handler(event, None)

    assert response["statusCode"] == 201
    result = json.loads(response["body"])
    assert result["character_id"] == "char-123"

def test_lambda_handler_missing_required_field():
    event = {}  # Missing character_id

    response = lambda_handler(event, None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert "character_id required" in body["error"]
```

### 11. Test File Organization

**For each Lambda handler, create a test file:**

```
lambda/create_character_lambda/
├── handler.py
├── requirements.txt
└── __tests__/
    └── test_handler.py

# Or use central tests directory
tests/unit/
└── test_create_character_lambda.py
```

**For shared utilities:**

```
src/utils/
├── character_utils.py
├── formatting.py
└── validation.py

tests/unit/
├── test_character_utils.py
├── test_formatting.py
└── test_validation.py
```

## Consequences

### Positive
- Clear expectations for test coverage
- Fast feedback loop (unit tests run in seconds)
- Easy to identify regressions early
- Test pyramid prevents brittle integration tests
- New developers/AI-assisted development has clear testing guidelines
- Code quality remains high with comprehensive testing

### Tradeoffs
- Writing tests takes time upfront (saves time overall in debugging)
- Some tests require mocking/stubbing setup (reduces brittleness)
- Phase-in approach means some existing code lacks tests (acceptable, improving over time)

## Implementation

### Starting today
1. Create `tests/` directory structure (unit, integration, fixtures)
2. Add Makefile test targets
3. Write tests for all new Lambda handlers
4. Use pytest as test runner
5. Aim for 70%+ coverage on new code

### Next phases
1. Backfill tests for critical existing handlers
2. Add integration tests for key API flows
3. Improve overall test coverage to 80%+

## Related ADRs

- ADR-002: Development Workflow Standards
- ADR-001: Terraform Infrastructure Patterns

## References

- The Practical Test Pyramid: https://martinfowler.com/articles/practical-test-pyramid.html
- Pytest Documentation: https://docs.pytest.org/
- Mocking with unittest.mock: https://docs.python.org/3/library/unittest.mock.html
- Shift-Left Testing: https://www.stickyminds.com/article/shift-left-approach-software-testing

Version History:
- v1.0 (2025-01-02): Initial testing standards for MyFantasy.ai Python codebase
