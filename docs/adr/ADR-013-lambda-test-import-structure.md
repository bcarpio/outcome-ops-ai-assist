# ADR-013: Lambda Test Import Structure and Module Resolution

## Status: Accepted

## Context

This repository contains multiple Lambda functions, each in its own directory under `lambda/`. Each Lambda has its own `handler.py`, `models.py`, and other modules. Tests are centralized in `lambda/tests/unit/`.

When generating tests for a Lambda handler, the test file must import from the correct handler directory. Python's module resolution can cause imports to resolve to the wrong module if multiple directories contain files with the same name (e.g., multiple `models.py` files).

**Problem example:**
```
lambda/
  list-recent-docs/
    handler.py
    models.py          # Contains DocumentItem, ListRecentDocsRequest
  generate-code/
    models.py          # Contains ExecutionPlan, PlanStep (different models!)
  tests/unit/
    test_list_recent_docs_handler.py
```

If a test does `from models import DocumentItem`, Python may resolve to the wrong `models.py` depending on `sys.path` order.

## Decision

### Key Points

- **Each Lambda is isolated**: Lambda handlers live in `lambda/<handler-name>/` with their own modules
- **Tests must use explicit path manipulation**: Tests add the specific handler directory to `sys.path` before importing
- **Never use bare imports for handler modules**: Always ensure the correct directory is in the path first

### Example Implementation

**Correct test file structure:**

```python
"""
Tests for list-recent-docs Lambda handler.

This test module imports from lambda/list-recent-docs/.
"""

import os
import sys

# Add the handler directory to path BEFORE any handler imports
handler_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'list-recent-docs')
sys.path.insert(0, os.path.abspath(handler_dir))

# Now imports will resolve to the correct modules
from handler import handler  # lambda/list-recent-docs/handler.py
from models import DocumentItem, ListRecentDocsRequest  # lambda/list-recent-docs/models.py

import pytest
from unittest.mock import patch, MagicMock


class TestListRecentDocsHandler:
    """Test cases for the list-recent-docs handler."""

    def test_handler_returns_documents(self):
        # Test implementation
        pass
```

**Key pattern:**
```python
# At the TOP of every test file, BEFORE other imports:
import os
import sys

# Calculate path to the specific Lambda directory being tested
handler_dir = os.path.join(os.path.dirname(__file__), '..', '..', '<handler-name>')
sys.path.insert(0, os.path.abspath(handler_dir))

# Now import from that handler
from handler import handler
from models import SomeModel  # Resolves to lambda/<handler-name>/models.py
```

### Directory Structure Reference

```
lambda/
  <handler-name>/           # Each Lambda in its own directory
    handler.py              # Main Lambda handler
    models.py               # Pydantic models for this handler
    requirements.txt        # Dependencies for this handler
    __init__.py             # Optional, for package imports
  tests/
    unit/
      test_<handler>_handler.py    # Tests for handler
      test_<handler>_models.py     # Tests for models
      conftest.py                  # Shared pytest fixtures
    pytest.ini                     # Pytest configuration
```

## Consequences

### Positive

- Tests always import from the correct handler directory
- No ambiguity when multiple handlers have same-named modules
- Clear, explicit code that documents which handler is being tested
- Works with pytest's collection without PYTHONPATH manipulation

### Tradeoffs

- Boilerplate at top of each test file (but it's explicit and clear)
- Must remember to add path manipulation when creating new test files
- IDE autocomplete may not work until the path is added at runtime

## Implementation

### Starting today

1. All new test files must include the `sys.path.insert` pattern
2. Test file names must match pattern `test_<handler-name>_*.py`
3. The path calculation must point to the specific handler directory

### Code generation guidance

When generating tests for a Lambda handler:

1. Determine the handler directory name from the issue or context
2. Add `sys.path.insert(0, ...)` pointing to that specific directory
3. Import handler modules AFTER the path manipulation
4. Import third-party modules (pytest, unittest.mock) AFTER handler imports

## Related ADRs

- ADR-004: Lambda Handler Standards (handler structure)
- ADR-006: Python Dependency Management (requirements.txt per handler)

## References

- Python sys.path documentation: https://docs.python.org/3/library/sys.html#sys.path
- Pytest import mechanisms: https://docs.pytest.org/en/stable/explanation/pythonpath.html

Version History:

- v1.0 (2025-12-10): Initial decision - standardize test imports for multi-Lambda repos

<!-- Confluence sync -->
