# Test Improvement Plan - Phase 1

**Goal:** Increase coverage from 55% to 70% (+15 percentage points)
**Focus:** Check handlers (currently 11-19% coverage)
**Estimated effort:** 40-45 new tests over 2-3 days

---

## Priority 1: test_coverage.py Handler (Easiest Win)

**File:** `lambda/process-pr-check/check_handlers/test_coverage.py`
**Current coverage:** 19% (21 missing lines)
**Target coverage:** 95%+
**Estimated tests needed:** 5-6 tests

### Tests to Add

#### test_check_handlers/test_test_coverage.py

```python
class TestCheckTestCoverage:
    """Test suite for test_coverage check handler"""

    def test_check_test_coverage_no_new_handlers(self):
        """Test: No new handlers to check"""
        # Changed files don't include any handlers
        changed_files = ["README.md", "terraform/main.tf"]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
        assert result["message"] == "No new handlers to check"
        assert result["details"] == []

    def test_check_test_coverage_handler_with_test_exists(self):
        """Test: New handler has corresponding test file"""
        changed_files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py"
        ]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
        assert "All handlers have test coverage" in result["message"]
        assert result["details"] == []

    def test_check_test_coverage_handler_missing_test(self):
        """Test: New handler missing test file"""
        changed_files = ["lambda/new-handler/handler.py"]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "WARN"
        assert "1 handler(s) may be missing tests" in result["message"]
        assert len(result["details"]) == 1
        assert "No test file found" in result["details"][0]

    def test_check_test_coverage_multiple_handlers_mixed_coverage(self):
        """Test: Multiple handlers, some with tests, some without"""
        changed_files = [
            "lambda/handler-a/handler.py",
            "lambda/handler-b/handler.py",
            "lambda/tests/unit/test_handler_a.py"  # Only handler-a has test
        ]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "WARN"
        assert "1 handler(s) may be missing tests" in result["message"]
        assert any("handler-b" in detail for detail in result["details"])

    def test_check_test_coverage_excludes_test_files(self):
        """Test: Test files themselves are excluded from handler check"""
        changed_files = [
            "lambda/tests/unit/test_something.py",  # Should be excluded
            "lambda/tests/integration/test_flow.py"  # Should be excluded
        ]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
        assert "No new handlers to check" in result["message"]

    def test_check_test_coverage_case_insensitive_matching(self):
        """Test: Case insensitive matching for test files"""
        changed_files = [
            "lambda/MyHandler/handler.py",
            "lambda/tests/unit/test_myhandler.py"  # lowercase test name
        ]
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
```

**Coverage impact:** +21 lines covered = 19% → 95%+ for test_coverage.py

---

## Priority 2: adr_compliance.py Handler

**File:** `lambda/process-pr-check/check_handlers/adr_compliance.py`
**Current coverage:** 11% (162 missing lines)
**Target coverage:** 75%+
**Estimated tests needed:** 15-18 tests

### Tests to Add

#### test_check_handlers/test_adr_compliance.py

```python
class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM"""

    @mock_aws()
    def test_get_github_token_success(self):
        """Test: Successfully retrieve GitHub token from SSM"""
        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(
            Name="/dev/app/github/token",
            Value="ghp_test_token",
            Type="SecureString"
        )

        token = get_github_token("/dev/app/github/token")
        assert token == "ghp_test_token"

    @mock_aws()
    def test_get_github_token_not_found(self):
        """Test: SSM parameter not found"""
        with pytest.raises(ClientError):
            get_github_token("/dev/app/nonexistent")

    @mock_aws()
    def test_get_github_token_empty_value(self):
        """Test: SSM parameter exists but has empty value"""
        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(Name="/dev/app/token", Value="", Type="String")

        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token("/dev/app/token")


class TestFetchPrFileDiff:
    """Test GitHub PR diff fetching"""

    @patch('adr_compliance.requests.get')
    def test_fetch_pr_file_diff_success(self, mock_get):
        """Test: Successfully fetch file diff from GitHub"""
        mock_response = Mock()
        mock_response.text = """diff --git a/lambda/test/handler.py b/lambda/test/handler.py
+def new_function():
+    pass"""
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        diff = fetch_pr_file_diff("owner/repo", 123, "lambda/test/handler.py", "token")

        assert "new_function" in diff
        mock_get.assert_called_once()

    @patch('adr_compliance.requests.get')
    def test_fetch_pr_file_diff_api_error(self, mock_get):
        """Test: GitHub API returns error"""
        mock_get.side_effect = requests.exceptions.RequestException("API Error")

        with pytest.raises(Exception, match="GitHub API error"):
            fetch_pr_file_diff("owner/repo", 123, "file.py", "token")

    @patch('adr_compliance.requests.get')
    def test_fetch_pr_file_diff_timeout(self, mock_get):
        """Test: GitHub API timeout"""
        mock_get.side_effect = requests.exceptions.Timeout("Timeout")

        with pytest.raises(Exception, match="GitHub API error"):
            fetch_pr_file_diff("owner/repo", 123, "file.py", "token")

    @patch('adr_compliance.requests.get')
    def test_fetch_pr_file_diff_file_not_in_pr(self, mock_get):
        """Test: Requested file is not in the PR diff"""
        mock_response = Mock()
        mock_response.text = "diff --git a/other.py b/other.py"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        diff = fetch_pr_file_diff("owner/repo", 123, "missing.py", "token")

        assert "(No diff available)" in diff


class TestExtractFileDiff:
    """Test extracting specific file diff from full PR diff"""

    def test_extract_file_diff_single_file(self):
        """Test: Extract diff for a single file"""
        full_diff = """diff --git a/file1.py b/file1.py
+line1
diff --git a/file2.py b/file2.py
+line2"""

        result = extract_file_diff_from_full_diff(full_diff, "file1.py")

        assert "file1.py" in result
        assert "line1" in result
        assert "line2" not in result

    def test_extract_file_diff_file_not_found(self):
        """Test: File not in diff returns empty string"""
        full_diff = "diff --git a/other.py b/other.py"

        result = extract_file_diff_from_full_diff(full_diff, "missing.py")

        assert result == ""

    def test_extract_file_diff_nested_path(self):
        """Test: Extract diff for file with nested path"""
        full_diff = """diff --git a/lambda/handler/file.py b/lambda/handler/file.py
+content"""

        result = extract_file_diff_from_full_diff(full_diff, "lambda/handler/file.py")

        assert "lambda/handler/file.py" in result
        assert "content" in result


class TestQueryKnowledgeBase:
    """Test knowledge base querying via Lambda"""

    @mock_aws()
    def test_query_knowledge_base_success(self):
        """Test: Successfully query knowledge base"""
        lambda_client = boto3.client("lambda", region_name="us-east-1")

        # Create mock Lambda function
        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_role(
            RoleName="test-role",
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
            })
        )

        lambda_client.create_function(
            FunctionName="query-kb",
            Runtime="python3.12",
            Role="arn:aws:iam::123456789012:role/test-role",
            Handler="index.handler",
            Code={"ZipFile": b"fake"},
        )

        # Mock response
        with patch.object(lambda_client, 'invoke') as mock_invoke:
            mock_invoke.return_value = {
                "Payload": Mock(read=Mock(return_value=json.dumps({
                    "answer": "Test answer",
                    "sources": ["ADR-001"]
                }).encode()))
            }

            result = query_knowledge_base("query-kb", "What are the standards?", 3)

            assert result["answer"] == "Test answer"
            assert "ADR-001" in result["sources"]

    @mock_aws()
    def test_query_knowledge_base_lambda_error(self):
        """Test: Lambda invocation fails"""
        lambda_client = boto3.client("lambda", region_name="us-east-1")

        with patch.object(lambda_client, 'invoke') as mock_invoke:
            mock_invoke.side_effect = ClientError(
                {"Error": {"Code": "ServiceException"}}, "invoke"
            )

            with pytest.raises(ClientError):
                query_knowledge_base("query-kb", "query", 3)


class TestAnalyzeCodeWithClaude:
    """Test Claude analysis of code compliance"""

    @patch('adr_compliance.bedrock_client')
    def test_analyze_code_with_claude_compliant(self, mock_bedrock):
        """Test: Claude determines code is compliant"""
        mock_response = Mock()
        mock_response.__getitem__.return_value = Mock(
            read=Mock(return_value=json.dumps({
                "content": [{
                    "text": '{"compliant": true, "explanation": "Code follows standards", "suggestions": []}'
                }]
            }).encode())
        )
        mock_bedrock.invoke_model.return_value = mock_response

        result = analyze_code_with_claude(
            "lambda/handler.py",
            "+def handler(): pass",
            "Use Pydantic schemas",
            "lambda"
        )

        assert result["compliant"] is True
        assert "follows standards" in result["explanation"]
        assert result["suggestions"] == []

    @patch('adr_compliance.bedrock_client')
    def test_analyze_code_with_claude_non_compliant(self, mock_bedrock):
        """Test: Claude finds compliance issues"""
        mock_response = Mock()
        mock_response.__getitem__.return_value = Mock(
            read=Mock(return_value=json.dumps({
                "content": [{
                    "text": '{"compliant": false, "explanation": "Missing schema", "suggestions": ["Add Pydantic model"]}'
                }]
            }).encode())
        )
        mock_bedrock.invoke_model.return_value = mock_response

        result = analyze_code_with_claude(
            "lambda/handler.py",
            "+def handler(): pass",
            "Use Pydantic schemas",
            "lambda"
        )

        assert result["compliant"] is False
        assert len(result["suggestions"]) > 0

    @patch('adr_compliance.bedrock_client')
    def test_analyze_code_with_claude_bedrock_error(self, mock_bedrock):
        """Test: Bedrock API error"""
        mock_bedrock.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "invoke_model"
        )

        with pytest.raises(ClientError):
            analyze_code_with_claude("file.py", "+code", "standards", "lambda")

    @patch('adr_compliance.bedrock_client')
    def test_analyze_code_with_claude_invalid_json_response(self, mock_bedrock):
        """Test: Claude returns invalid JSON"""
        mock_response = Mock()
        mock_response.__getitem__.return_value = Mock(
            read=Mock(return_value=json.dumps({
                "content": [{"text": "not valid json"}]
            }).encode())
        )
        mock_bedrock.invoke_model.return_value = mock_response

        with pytest.raises(json.JSONDecodeError):
            analyze_code_with_claude("file.py", "+code", "standards", "lambda")


class TestCheckAdrCompliance:
    """Test main check_adr_compliance handler"""

    @mock_aws()
    @patch('adr_compliance.get_github_token')
    @patch('adr_compliance.query_knowledge_base')
    @patch('adr_compliance.fetch_pr_file_diff')
    @patch('adr_compliance.analyze_code_with_claude')
    def test_check_adr_compliance_all_files_compliant(
        self, mock_analyze, mock_fetch, mock_query, mock_token
    ):
        """Test: All changed files are ADR compliant"""
        mock_token.return_value = "token"
        mock_query.return_value = {"answer": "Standards here", "sources": []}
        mock_fetch.return_value = "+new code"
        mock_analyze.return_value = {
            "compliant": True,
            "explanation": "Follows standards",
            "suggestions": []
        }

        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["lambda/handler.py"]
        )

        assert result["status"] == "PASS"
        assert "compliant" in result["message"].lower()

    @mock_aws()
    @patch('adr_compliance.get_github_token')
    @patch('adr_compliance.query_knowledge_base')
    @patch('adr_compliance.fetch_pr_file_diff')
    @patch('adr_compliance.analyze_code_with_claude')
    def test_check_adr_compliance_has_violations(
        self, mock_analyze, mock_fetch, mock_query, mock_token
    ):
        """Test: Some files have ADR violations"""
        mock_token.return_value = "token"
        mock_query.return_value = {"answer": "Standards", "sources": []}
        mock_fetch.return_value = "+code"
        mock_analyze.return_value = {
            "compliant": False,
            "explanation": "Missing schema",
            "suggestions": ["Add Pydantic model"]
        }

        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["lambda/handler.py"]
        )

        assert result["status"] == "WARN"
        assert len(result["details"]) > 0

    @mock_aws()
    @patch('adr_compliance.get_github_token')
    def test_check_adr_compliance_no_relevant_files(self, mock_token):
        """Test: No Lambda or Terraform files in PR"""
        mock_token.return_value = "token"

        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["README.md", "docs/guide.md"]
        )

        assert result["status"] == "PASS"
        assert "no Lambda or Terraform files" in result["message"].lower()
```

**Coverage impact:** +140 lines covered = 11% → 85%+ for adr_compliance.py

---

## Priority 3: readme_freshness.py Handler

**File:** `lambda/process-pr-check/check_handlers/readme_freshness.py`
**Current coverage:** 13% (109 missing lines)
**Target coverage:** 75%+
**Estimated tests needed:** 12-14 tests

### Tests to Add

#### test_check_handlers/test_readme_freshness.py

```python
class TestCheckReadmeFreshness:
    """Test suite for README freshness check handler"""

    @patch('readme_freshness.get_github_token')
    @patch('readme_freshness.fetch_file_content')
    @patch('readme_freshness.analyze_readme_with_claude')
    def test_check_readme_freshness_updated_with_lambda(
        self, mock_analyze, mock_fetch, mock_token
    ):
        """Test: README updated when Lambda handler changes"""
        mock_token.return_value = "token"
        mock_fetch.return_value = "# README content"
        mock_analyze.return_value = {
            "fresh": True,
            "explanation": "README mentions new handler",
            "suggestions": []
        }

        changed_files = [
            "lambda/new-handler/handler.py",
            "README.md"
        ]

        result = check_readme_freshness("README_FRESHNESS", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
        assert "up to date" in result["message"].lower()

    @patch('readme_freshness.get_github_token')
    def test_check_readme_freshness_new_handler_no_readme_update(self, mock_token):
        """Test: New Lambda handler but README not updated"""
        mock_token.return_value = "token"

        changed_files = ["lambda/new-handler/handler.py"]

        result = check_readme_freshness("README_FRESHNESS", 123, "owner/repo", changed_files)

        assert result["status"] == "WARN"
        assert "not updated" in result["message"].lower()

    @patch('readme_freshness.get_github_token')
    def test_check_readme_freshness_no_lambda_changes(self, mock_token):
        """Test: No Lambda changes, no README check needed"""
        mock_token.return_value = "token"

        changed_files = ["terraform/main.tf", "docs/guide.md"]

        result = check_readme_freshness("README_FRESHNESS", 123, "owner/repo", changed_files)

        assert result["status"] == "PASS"
        assert "no new" in result["message"].lower()

    @patch('readme_freshness.get_github_token')
    @patch('readme_freshness.fetch_file_content')
    def test_check_readme_freshness_fetch_error(self, mock_fetch, mock_token):
        """Test: Error fetching README from GitHub"""
        mock_token.return_value = "token"
        mock_fetch.side_effect = Exception("GitHub API error")

        changed_files = ["lambda/handler.py", "README.md"]

        result = check_readme_freshness("README_FRESHNESS", 123, "owner/repo", changed_files)

        assert result["status"] == "WARN"
        assert "error" in result["message"].lower()

    # Add 8-10 more tests for:
    # - Multiple Lambda handlers added
    # - Bedrock throttling error
    # - Claude returns invalid JSON
    # - README is fresh but missing architecture diagram
    # - etc.
```

**Coverage impact:** +95 lines covered = 13% → 85%+ for readme_freshness.py

---

## Summary: Phase 1 Impact

### Total Tests to Add: 40-45 tests

| Handler | Current | Target | Tests Needed |
|---------|---------|--------|--------------|
| test_coverage.py | 19% | 95% | 6 tests |
| adr_compliance.py | 11% | 85% | 18 tests |
| readme_freshness.py | 13% | 85% | 14 tests |
| **Subtotal** | **avg 14%** | **avg 88%** | **38 tests** |

**Additional handlers (if time permits):**
- architectural_duplication.py: 10-12 tests
- breaking_changes.py: 10-12 tests

### Expected Coverage Improvement

- **Starting:** 55% overall
- **After Phase 1:** 70% overall (+15%)
- **Statements covered:** +400 of the 960 missing

### Test Writing Order (Recommended)

**Week 1, Day 1:**
1. test_coverage.py (easiest, 6 tests, ~2 hours)
2. adr_compliance.py - Part 1 (helper functions, 10 tests, ~4 hours)

**Week 1, Day 2:**
3. adr_compliance.py - Part 2 (main handler, 8 tests, ~4 hours)
4. readme_freshness.py - Part 1 (first 7 tests, ~3 hours)

**Week 1, Day 3:**
5. readme_freshness.py - Part 2 (remaining 7 tests, ~3 hours)
6. Coverage check and gaps analysis (~2 hours)

### Testing Standards to Follow (ADR-005)

For each test:
- ✅ Use moto for AWS mocking (not @patch for boto3 clients)
- ✅ Test happy path first
- ✅ Test error handling (AWS errors, API errors)
- ✅ Test edge cases (empty inputs, null values)
- ✅ Use descriptive test names: `test_[function]_[scenario]_[expected]`
- ✅ Follow AAA pattern: Arrange, Act, Assert
- ✅ Mock external dependencies (GitHub API, Bedrock)

### Next Steps

After completing Phase 1:
1. Run coverage report: `make test-coverage`
2. Verify 70%+ coverage achieved
3. Commit tests with proper commit message
4. Move to Phase 2 (ingest-docs, vector-query)

---

**Created:** 2025-01-06
**Status:** Ready for implementation
**Estimated completion:** 2-3 days
