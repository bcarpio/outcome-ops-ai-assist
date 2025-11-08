# ADR-005: Testing Standards for Lambda Functions

## Status: Accepted

## Context

OutcomeOps AI Assist is an open-source serverless application with 8 Lambda functions providing RAG capabilities, PR analysis, and documentation ingestion. As an open-source project, high test coverage signals quality and reliability to potential users and contributors.

Testing challenges specific to this project:
- Lambda functions with AWS service dependencies (DynamoDB, S3, Bedrock, SQS)
- Dynamically loaded modules using `importlib` for test isolation
- AI-powered features with non-deterministic responses
- Event-driven architecture with async workflows

We need standardized testing practices that ensure reliability while maintaining development velocity.

## Decision

### Coverage Target: 80% minimum

**Rationale:**
- Industry standard for production-grade open-source projects
- Builds trust with users and contributors
- Catches critical bugs before production
- Sustainable (not chasing 100% which has diminishing returns)

### Test Pyramid Structure

Follow the testing pyramid with emphasis on unit tests:

1. **Unit Tests (majority)** - Fast, isolated, many
   - Test individual functions and classes
   - Mock all AWS services using moto
   - Target: 90% of total test count

2. **Integration Tests (moderate)** - Real AWS services, fewer
   - Test interactions between components
   - Use LocalStack or real dev AWS resources
   - Target: 10% of total test count

3. **Functional Tests (minimal)** - End-to-end, limited scope
   - Test critical user workflows
   - Target: As needed for critical paths

### Required Test Coverage for All Lambda Functions

Every Lambda handler must test:

**1. Happy Path (primary functionality)**
```python
def test_handler_success():
    """Test successful execution with valid input"""
    event = {"query": "test query"}
    response = handler(event, context)
    assert response["statusCode"] == 200
    assert "body" in response
```

**2. Input Validation**
```python
def test_handler_missing_required_field():
    """Test handler rejects invalid input"""
    event = {}  # Missing required field
    response = handler(event, context)
    assert response["statusCode"] == 400
    assert "error" in json.loads(response["body"])
```

**3. AWS Service Errors**
```python
@patch('handler_module.dynamodb_client')
def test_handler_dynamodb_error(mock_dynamodb):
    """Test handler handles AWS service failures gracefully"""
    mock_dynamodb.get_item.side_effect = ClientError(
        {"Error": {"Code": "ServiceException"}}, "get_item"
    )
    response = handler(event, context)
    assert response["statusCode"] == 500
```

**4. Edge Cases**
- Empty responses from dependencies
- Null/undefined values in input
- Boundary conditions (empty lists, zero values, max limits)
- Timeout scenarios for long-running operations

**5. Error Recovery and Logging**
- Verify errors are logged with sufficient context
- Test retry logic (if applicable)
- Verify graceful degradation

### Testing Patterns for AWS Services

**Use moto for AWS mocking (NOT @patch decorators):**

```python
from moto import mock_aws

@mock_aws()
def test_dynamodb_integration():
    # Create real (mocked) DynamoDB table
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(...)

    # Test handler with mocked AWS
    result = handler(event, context)
    assert result is not None
```

**Dynamic module loading for test isolation:**

```python
import importlib.util

# Load handler module to avoid import-time side effects
handler_path = os.path.join(os.path.dirname(__file__), '../../my-lambda/handler.py')
spec = importlib.util.spec_from_file_location("my_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['my_handler'] = handler_module
spec.loader.exec_module(handler_module)
```

### Testing AI-Powered Features

For Lambda functions that call Bedrock (Claude, Titan embeddings):

**Mock AI responses for deterministic tests:**

```python
@patch('handler_module.bedrock_client')
def test_ai_feature(mock_bedrock):
    # Arrange: Mock AI response
    mock_response = Mock()
    mock_response.__getitem__.return_value = Mock(
        read=Mock(return_value=json.dumps({
            "content": [{"text": "Expected AI response"}]
        }).encode())
    )
    mock_bedrock.invoke_model.return_value = mock_response

    # Act & Assert
    result = generate_summary(text)
    assert "Expected AI response" in result
```

**Test AI error handling:**
- Throttling errors (retry logic)
- Invalid model responses
- Token limit exceeded
- Service unavailable

### Test Organization

```
lambda/tests/
├── unit/                      # Unit tests (fast, isolated)
│   ├── test_handler_name.py
│   └── test_module_name.py
├── integration/               # Integration tests (AWS services)
│   └── test_workflow_name.py
├── fixtures/                  # Shared test data
│   └── sample_data.py
├── conftest.py               # Pytest configuration
└── Makefile                  # Test execution targets
```

### Test Naming Convention

```python
class TestHandlerFunctionality:
    """Group related tests in classes"""

    def test_handler_success_with_valid_input(self):
        """Use descriptive names: test_[function]_[scenario]_[expected]"""
        pass

    def test_handler_fails_with_missing_field(self):
        """Clear what's being tested and expected outcome"""
        pass
```

### Coverage Enforcement

**CI/CD must:**
- Generate coverage reports for SonarCloud
- Fail builds below 80% coverage
- Report per-file coverage metrics
- Track coverage trends over time

**Developers should:**
- Run `make test` before committing
- Check coverage locally with `make test-coverage`
- Add tests for new features in the same PR
- Update tests when modifying existing code

## Consequences

### Positive

- **Reliability**: 80% coverage catches most bugs before production
- **Confidence**: Developers and contributors can refactor safely
- **Documentation**: Tests serve as usage examples
- **Quality signal**: High coverage attracts more users and contributors
- **Faster debugging**: Tests pinpoint failure locations quickly
- **Maintainability**: Well-tested code is easier to modify

### Tradeoffs

- **Development time**: Writing tests adds 30-50% to feature development time
- **Maintenance burden**: Tests need updates when functionality changes
- **False security**: 80% coverage doesn't guarantee bug-free code
- **Diminishing returns**: Getting from 80% to 90% takes disproportionate effort
- **Test complexity**: Mocking AWS services and AI responses adds complexity

## Implementation

### For New Lambda Functions

When creating a new Lambda function:

1. Create test file: `lambda/tests/unit/test_function_name.py`
2. Test happy path first
3. Add input validation tests
4. Test AWS service error handling
5. Add edge case tests
6. Verify 80%+ coverage before PR
7. Document test strategy in `docs/lambda-function-name.md`

### For Modifying Existing Functions

When changing Lambda functionality:

1. Update existing tests to match new behavior
2. Add tests for new code paths
3. Ensure coverage doesn't drop below 80%
4. Run full test suite: `make test`
5. Check coverage report: `make test-coverage`

### Coverage Gaps

If coverage falls below 80%:

1. Identify untested code paths with `coverage report --show-missing`
2. Prioritize error handling and edge cases
3. Add tests systematically (highest value first)
4. Don't test trivial code (simple getters, AWS SDK wrappers)
5. Don't chase 100% - focus on critical paths

## Related ADRs

- ADR-002: Development Workflow Standards - Pre-commit checklist includes running tests
- ADR-003: Git Commit Standards - Test-related commits use `test(scope):` prefix

## References

- [Testing Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html)
- [Moto: Mock AWS Services](https://github.com/getmoto/moto)
- [Pytest Best Practices](https://docs.pytest.org/en/stable/goodpractices.html)
- [Coverage.py Documentation](https://coverage.readthedocs.io/)

## Version History

- v1.0 (2025-01-06): Initial testing standards for OutcomeOps AI Assist
