# OutcomeOps AI Assist - Lambda Tests

Comprehensive unit tests for the RAG (Retrieval Augmented Generation) pipeline Lambda functions.

## Test Structure

Following ADR-003: Testing Standards, all Lambda tests are centralized in this directory:

```
lambda/tests/
├── conftest.py              # Pytest configuration and shared fixtures
├── pytest.ini               # Pytest settings
├── Makefile                 # Test runner commands
├── README.md                # This file
├── unit/                    # Unit tests (fast, isolated)
│   ├── test_vector_query.py      # Vector search tests
│   ├── test_ask_claude.py        # Claude RAG generation tests
│   └── test_query_kb.py          # Orchestrator tests
├── integration/             # Integration tests (AWS services)
└── fixtures/                # Test fixtures and sample data
    └── sample_data.py       # Shared test fixtures
```

## Running Tests

### All Tests
```bash
cd lambda/tests
make test
```

### Unit Tests Only
```bash
make test-unit
```

### With Coverage Report
```bash
make test-coverage
# Opens htmlcov/index.html with coverage visualization
```

### Specific Test File
```bash
pytest unit/test_vector_query.py -v
```

### Specific Test
```bash
pytest unit/test_vector_query.py::TestCosineSimilarity::test_cosine_similarity_identical_vectors -v
```

## Test Coverage

### Vector Query Lambda (`test_vector_query.py`)
- ✅ Embedding generation with Bedrock Titan v2
- ✅ Cosine similarity calculation (5 test cases)
- ✅ DynamoDB document scanning with pagination
- ✅ Document search and ranking
- ✅ Source formatting for different document types
- ✅ Handler success and error cases

### Ask Claude Lambda (`test_ask_claude.py`)
- ✅ RAG prompt construction
- ✅ Claude API invocation via Bedrock Converse
- ✅ Retry logic with exponential backoff (1s, 2s, 4s)
- ✅ No retry on validation errors
- ✅ Source extraction from context
- ✅ Handler success and error cases
- ✅ Empty context handling

### Query KB Lambda (`test_query_kb.py`)
- ✅ Lambda-to-Lambda invocation
- ✅ Full RAG pipeline orchestration
- ✅ Vector search integration
- ✅ Claude generation integration
- ✅ No results found handling (404 response)
- ✅ Component failure handling
- ✅ Default topK parameter

## Test Philosophy

From ADR-003:

1. **Arrange-Act-Assert** pattern for all tests
2. **One assertion per test** when possible
3. **Mock external dependencies** (AWS services, Bedrock)
4. **Test negative cases** (missing params, errors, edge cases)
5. **Fast execution** (unit tests < 100ms each)

## Example Test

```python
def test_cosine_similarity_identical_vectors():
    # Arrange
    vec = [1.0, 2.0, 3.0]

    # Act
    result = handler.cosine_similarity(vec, vec)

    # Assert
    assert pytest.approx(result, rel=1e-5) == 1.0
```

## CI/CD Integration

Tests run automatically in CI pipeline:
- On every pull request
- Before deployments
- Must pass before merge

## Adding New Tests

1. Create test file: `unit/test_<lambda_name>.py`
2. Import handler using importlib (see existing tests)
3. Follow Arrange-Act-Assert pattern
4. Mock AWS services with `@patch`
5. Run tests locally: `make test-unit`

## Related Documentation

- **ADR-003**: Testing Standards
- **Lambda Docs**:
  - `docs/lambda-vector-query.md`
  - `docs/lambda-ask-claude.md`
  - `docs/lambda-query-kb.md`
