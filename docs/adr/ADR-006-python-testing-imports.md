# ADR-006: Python Testing Import Patterns for Lambda Functions

## Status: Accepted

## Context

OutcomeOps generates Lambda function tests automatically using AI. A common failure pattern emerged where generated tests use invalid Python import syntax:

```python
from lambda.list_recent_docs.handler import handler  # ❌ INVALID - 'lambda' is a Python keyword
```

This causes `SyntaxError: invalid syntax` during test collection, which is classified as a fixable error by run-tests, but auto-fix cannot resolve it because it's a structural problem requiring knowledge of Python import conventions.

**Root cause:** The knowledge base lacks clear guidance on Python import patterns for Lambda function testing, causing AI to generate syntactically invalid imports.

**Impact:**
- Tests fail immediately at collection time
- Auto-fix wastes resources attempting impossible fixes (3 attempts)
- PRs require human intervention for a preventable issue
- Slows down the automated code generation workflow

## Decision

### Use sys.path Manipulation for Lambda Function Imports

**Standard pattern for all Lambda function tests:**

```python
import sys
import os
from pathlib import Path

# Add the Lambda function directory to Python path
lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "function-name"
sys.path.insert(0, str(lambda_dir))

# Now import the handler
from handler import handler  # ✅ CORRECT
```

**Why this approach:**
1. **Explicit and clear** - Obvious what's being added to path
2. **Portable** - Works in CI/CD and local development
3. **No naming conflicts** - Avoids Python keyword issues
4. **Compatible with pytest** - Standard pytest pattern
5. **Works with moto** - AWS mocking still functions correctly

### Forbidden Patterns

**❌ NEVER use `lambda` as a module name:**
```python
from lambda.function_name.handler import handler  # FORBIDDEN - 'lambda' is keyword
import lambda.function_name.handler as handler    # FORBIDDEN - 'lambda' is keyword
```

**❌ NEVER use relative imports from test files:**
```python
from ...lambda.function_name.handler import handler  # FRAGILE - breaks easily
```

**❌ NEVER assume `lambda/` is on PYTHONPATH:**
```python
from function_name.handler import handler  # UNRELIABLE - depends on environment
```

### Standard Test File Template

Every Lambda function test must follow this structure:

```python
"""
Unit tests for <function-name> Lambda handler.

Tests cover happy path scenarios and error handling.
"""

import sys
import os
from pathlib import Path
import json
import pytest
from moto import mock_aws
import boto3

# Add Lambda function to Python path
lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "<function-name>"
sys.path.insert(0, str(lambda_dir))

# Import handler AFTER adding to path
from handler import handler


@pytest.mark.unit
class TestFunctionNameHappyPath:
    """Test class for happy path scenarios."""

    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch):
        """Set up environment variables for tests."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("TABLE_NAME", "test-table")
        # Add other required env vars

    @mock_aws
    def test_handler_success(self):
        """Test successful handler execution with valid input."""
        # Arrange
        event = {"key": "value"}
        context = {}

        # Act
        response = handler(event, context)

        # Assert
        assert response["statusCode"] == 200
        assert "body" in response
```

### Directory Structure Requirements

Tests must follow this structure:

```
lambda/
├── function-name/
│   ├── handler.py           # Lambda entry point
│   ├── requirements.txt
│   └── ...
└── tests/
    ├── unit/
    │   └── test_function_name.py   # Uses sys.path to import handler
    ├── integration/
    │   └── test_function_workflow.py
    └── conftest.py
```

**Path calculation in tests:**
```python
# From lambda/tests/unit/test_function_name.py
# Go up 2 levels (unit/ -> tests/ -> lambda/) then into function dir
lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "function-name"
```

### Common Pitfalls and Solutions

**Problem: Import Error - Module not found**
```python
# Wrong
from handler import handler  # ModuleNotFoundError

# Correct - Add to sys.path first
lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "function-name"
sys.path.insert(0, str(lambda_dir))
from handler import handler
```

**Problem: Imports work locally but fail in CI/CD**
- **Cause:** Local PYTHONPATH includes lambda/ but CI doesn't
- **Solution:** Always use explicit sys.path manipulation in tests
- **Prevention:** Test in clean venv before committing

**Problem: Multiple Lambda functions with same import name**
- **Cause:** sys.path has multiple Lambda dirs, wrong handler imported
- **Solution:** Clear sys.path or use unique handler names per test
- **Better:** Import once at module level, not in each test

**Problem: Pytest can't find tests**
- **Cause:** Import error prevents test collection
- **Solution:** Verify sys.path before importing handler
- **Debug:** Run `pytest --collect-only` to see collection errors

### Testing the Import Pattern

Before committing tests, verify imports work:

```bash
# From project root
cd lambda/tests
python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parent / 'function-name'))
from handler import handler
print('Import successful:', handler)
"
```

### Alternative Patterns (NOT Recommended)

**Dynamic module loading (complex, avoid unless necessary):**
```python
import importlib.util

handler_path = Path(__file__).parents[2] / "lambda" / "function-name" / "handler.py"
spec = importlib.util.spec_from_file_location("handler_module", handler_path)
handler_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler_module)
handler = handler_module.handler
```

**Use cases for dynamic loading:**
- Multiple test files need to import same handler name
- Need to reload handler module between tests
- Testing module initialization side effects

**Why not recommended:** Added complexity with minimal benefit for typical Lambda tests.

## Consequences

### Positive

- **Prevents syntax errors** - No more `from lambda.` imports
- **Faster test cycles** - Tests pass collection immediately
- **Less auto-fix churn** - No wasted attempts on unfixable errors
- **Consistent patterns** - All tests follow same import convention
- **Better AI generation** - Clear examples in knowledge base
- **Portable tests** - Work in any environment without PYTHONPATH hacks

### Tradeoffs

- **Boilerplate** - Every test file needs 3 lines of sys.path setup
- **Path calculations** - Must count parent levels correctly
- **Not "Pythonic"** - Explicit sys.path manipulation vs clean imports
- **Maintenance** - If directory structure changes, all tests need updates

### Negative (Mitigated)

- **Risk of wrong imports** - If sys.path has stale entries
  - **Mitigation:** Use `sys.path.insert(0, ...)` to prioritize
- **Debugging confusion** - sys.path modifications can hide issues
  - **Mitigation:** Always print imported module path in debug mode
- **IDE warnings** - Some IDEs flag imports after sys.path modifications
  - **Mitigation:** Add `# type: ignore` or configure IDE

## Implementation

### For New Lambda Functions

When generating tests for a new Lambda function:

1. **Start with template** - Copy standard test structure from this ADR
2. **Calculate path** - Verify parent levels match your structure
3. **Import handler** - Use `from handler import handler` pattern
4. **Verify locally** - Run `pytest --collect-only` to check collection
5. **Document exceptions** - If you must deviate, document why

### For Existing Tests with Invalid Imports

When fixing tests with `from lambda.` imports:

1. **Add sys.path manipulation** - Before any handler imports
2. **Change import** - From `from lambda.X.handler` to `from handler`
3. **Test locally** - Ensure tests still pass
4. **Check other tests** - Fix all tests in same file consistently

### For AI-Generated Tests

The OutcomeOps knowledge base will now include:

1. **This ADR** - Full guidance on Python import patterns
2. **Test template** - Standard starting point for new tests
3. **Example tests** - Real tests following correct pattern
4. **Common errors** - What not to do and why

When generate-code creates new Lambda tests, it will reference this ADR and generate syntactically valid imports from the start.

## Related ADRs

- **ADR-005: Testing Standards** - General testing patterns, this ADR specializes for Python imports
- **ADR-002: Development Workflow** - Pre-commit checklist includes running `pytest --collect-only`

## References

- [Python sys.path Documentation](https://docs.python.org/3/library/sys.html#sys.path)
- [Pytest Good Integration Practices](https://docs.pytest.org/en/stable/goodpractices.html#test-discovery)
- [Python Reserved Keywords](https://docs.python.org/3/reference/lexical_analysis.html#keywords)
- [PEP 8 - Import Conventions](https://peps.python.org/pep-0008/#imports)

## Examples

### Complete Test File Example

```python
"""Unit tests for query-kb Lambda handler."""

import sys
from pathlib import Path
import json
import pytest
from moto import mock_aws
import boto3

# Add Lambda function to Python path
lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "query-kb"
sys.path.insert(0, str(lambda_dir))

from handler import handler


@pytest.mark.unit
class TestQueryKbHappyPath:
    """Test successful query scenarios."""

    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch):
        """Set up test environment."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("DYNAMODB_TABLE_NAME", "test-kb")
        monkeypatch.setenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v1")

    @mock_aws
    def test_handler_returns_documents(self):
        """Test handler returns matching documents for valid query."""
        # Arrange
        dynamodb = boto3.client("dynamodb", region_name="us-west-2")
        dynamodb.create_table(
            TableName="test-kb",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        event = {"query": "test query", "limit": 5}
        context = {}

        # Act
        response = handler(event, context)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "documents" in body
        assert isinstance(body["documents"], list)
```

### Debugging Import Issues

```python
"""Debug helper for import issues."""

import sys
from pathlib import Path

# Before import
print("Current sys.path:", sys.path[:3])

lambda_dir = Path(__file__).resolve().parents[2] / "lambda" / "function-name"
print(f"Adding to path: {lambda_dir}")
print(f"Handler exists: {(lambda_dir / 'handler.py').exists()}")

sys.path.insert(0, str(lambda_dir))

from handler import handler
print(f"Imported handler from: {handler.__module__}")
```

## Version History

- v1.0 (2025-11-20): Initial ADR for Python testing import patterns
