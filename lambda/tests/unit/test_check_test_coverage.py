"""
Unit tests for test_coverage check handler.

Tests cover:
- No configuration found (skips check)
- Python backend defaults
- Java backend defaults
- TypeScript backend defaults
- Explicit .outcomeops.yaml pattern overrides
- Handler with corresponding test file
- Handler missing test file
- Hyphen to underscore normalization
- ADR reference extraction from KB response
- New format: require_tests / exclude_from_tests
- Fetching all test files from repository
- Excluding files from test requirements (e.g., Spring Data repositories)
- Usage tracking for KB queries (local/global standards)
"""

import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import importlib.util
from types import ModuleType

# First, load bedrock_utils module (dependency for test_coverage)
bedrock_utils_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create a fake check_handlers package that contains bedrock_utils
# This is needed because test_coverage.py uses relative import: from .bedrock_utils import ...
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [os.path.dirname(bedrock_utils_path)]
check_handlers_package.bedrock_utils = bedrock_module
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Load the test_coverage handler module as part of check_handlers package
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/test_coverage.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.test_coverage",
    handler_path,
    submodule_search_locations=[os.path.dirname(handler_path)]
)
handler_module = importlib.util.module_from_spec(spec)
handler_module.__package__ = "check_handlers"
sys.modules['check_handlers.test_coverage'] = handler_module
sys.modules['test_coverage_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from loaded module
check_test_coverage = handler_module.check_test_coverage
matches_pattern = handler_module.matches_pattern
normalize_name = handler_module.normalize_name
find_source_files = handler_module.find_source_files
find_test_files = handler_module.find_test_files
extract_name = handler_module.extract_name
BACKEND_DEFAULTS = handler_module.BACKEND_DEFAULTS
# New imports for enhanced testing config
TestRequirement = handler_module.TestRequirement
TestExclusion = handler_module.TestExclusion
TestingConfig = handler_module.TestingConfig
parse_testing_config = handler_module.parse_testing_config
is_file_excluded = handler_module.is_file_excluded
file_requires_tests = handler_module.file_requires_tests
fetch_repo_test_files = handler_module.fetch_repo_test_files


class TestPatternMatching:
    """Tests for pattern matching utilities"""

    def test_matches_simple_pattern(self):
        """Test simple glob pattern matching"""
        assert matches_pattern("lambda/hello/handler.py", ["lambda/*/handler.py"])
        assert not matches_pattern("src/hello/handler.py", ["lambda/*/handler.py"])

    def test_matches_double_star_pattern(self):
        """Test ** glob pattern matching"""
        assert matches_pattern("src/main/java/com/example/Service.java", ["src/main/java/**/*.java"])
        assert matches_pattern("src/components/Button.tsx", ["src/**/*.tsx"])

    def test_matches_test_patterns(self):
        """Test test file pattern matching"""
        assert matches_pattern("test_hello.py", ["**/test_*.py"])
        assert matches_pattern("lambda/tests/unit/test_hello.py", ["**/test_*.py"])
        assert matches_pattern("ServiceTest.java", ["**/*Test.java"])


class TestNormalization:
    """Tests for name normalization"""

    def test_normalize_hyphens_to_underscores(self):
        """Test hyphen to underscore conversion"""
        assert normalize_name("list-recent-docs") == "list_recent_docs"

    def test_normalize_lowercase(self):
        """Test lowercase conversion"""
        assert normalize_name("MyHandler") == "myhandler"

    def test_normalize_combined(self):
        """Test combined normalization"""
        assert normalize_name("My-Handler-Name") == "my_handler_name"


class TestNameExtraction:
    """Tests for source file name extraction"""

    def test_extract_python_lambda_handler_name(self):
        """Test extracting name from Python Lambda handler path"""
        name = extract_name("lambda/hello/handler.py", "python")
        assert name == "hello"

    def test_extract_python_hyphenated_handler_name(self):
        """Test extracting hyphenated name from Python Lambda handler"""
        name = extract_name("lambda/list-recent-docs/handler.py", "python")
        assert name == "list-recent-docs"

    def test_extract_java_class_name(self):
        """Test extracting name from Java class path"""
        name = extract_name("src/main/java/com/example/UserService.java", "java")
        assert name == "UserService"

    def test_extract_typescript_component_name(self):
        """Test extracting name from TypeScript component path"""
        name = extract_name("src/components/Button.tsx", "typescript")
        assert name == "Button"

    def test_extract_unknown_backend_fallback(self):
        """Test fallback for unknown backend"""
        name = extract_name("src/something/file.xyz", "unknown")
        assert name == "file"


class TestFindFiles:
    """Tests for finding source and test files"""

    def test_find_python_source_files(self):
        """Test finding Python Lambda handlers"""
        changed_files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py",
            "README.md"
        ]
        sources = find_source_files(
            changed_files,
            BACKEND_DEFAULTS["python"]["source_patterns"],
            BACKEND_DEFAULTS["python"]["exclude_patterns"]
        )
        assert sources == ["lambda/hello/handler.py"]

    def test_find_python_test_files(self):
        """Test finding Python test files"""
        changed_files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py",
            "tests/test_integration.py"
        ]
        tests = find_test_files(
            changed_files,
            BACKEND_DEFAULTS["python"]["test_patterns"]
        )
        assert "lambda/tests/unit/test_hello.py" in tests


class TestCheckTestCoverage:
    """Tests for the main check_test_coverage function"""

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_no_config_skips_check(self, mock_kb, mock_yaml, mock_token):
        """Test: No .outcomeops.yaml returns PASS with warning"""
        mock_token.return_value = "token"
        mock_yaml.return_value = None  # No config found
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["src/hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "skipped" in result["message"].lower()
        assert "no testing configuration found" in result["message"].lower()

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_python_backend_defaults(self, mock_kb, mock_yaml, mock_token):
        """Test: Python backend uses correct default patterns"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "All source files have test coverage" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_python_handler_missing_test(self, mock_kb, mock_yaml, mock_token):
        """Test: Python handler without test fails"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/new-handler/handler.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "FAIL"
        assert "1 source file(s) missing required tests" in result["message"]
        assert len(result["details"]) >= 1
        assert "new-handler" in result["details"][0]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_hyphen_underscore_matching(self, mock_kb, mock_yaml, mock_token):
        """Test: Handler with hyphens matches test with underscores"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/list-recent-docs/handler.py", "lambda/tests/unit/test_list_recent_docs.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "All source files have test coverage" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_explicit_patterns_override(self, mock_kb, mock_yaml, mock_token):
        """Test: Explicit patterns in .outcomeops.yaml take precedence"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {
            "testing": {
                "source_patterns": ["custom/src/**/*.py"],
                "test_patterns": ["custom/tests/**/*.py"],
                "exclude_patterns": ["custom/tests/**"]
            }
        }
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["custom/src/module.py", "custom/tests/test_module.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_customer_adr_reference(self, mock_kb, mock_yaml, mock_token):
        """Test: Customer's ADR reference is used in messages"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_kb.return_value = ("According to ADR-005, all code must have tests...", "ADR-005", {})

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "ADR-005" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_customer_adr_in_failure_message(self, mock_kb, mock_yaml, mock_token):
        """Test: Customer's ADR reference appears in failure message"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_kb.return_value = ("Per ADR-007, tests are mandatory", "ADR-007", {})

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/new-handler/handler.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "FAIL"
        assert "ADR-007 violation" in result["message"]
        # Details may have more items due to local/global standards - check first detail
        assert "ADR-007" in result["details"][0] or "new-handler" in result["details"][0]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_java_backend_defaults(self, mock_kb, mock_yaml, mock_token):
        """Test: Java backend uses correct default patterns"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "java"}}
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            [
                "src/main/java/com/example/UserService.java",
                "src/test/java/com/example/UserServiceTest.java"
            ],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_typescript_backend_defaults(self, mock_kb, mock_yaml, mock_token):
        """Test: TypeScript backend uses correct default patterns"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "typescript"}}
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            [
                "src/components/Button.tsx",
                "src/components/Button.test.tsx"
            ],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.fetch_repo_test_files')
    @patch('test_coverage_handler.query_testing_standards')
    def test_no_source_files_passes(self, mock_kb, mock_repo_tests, mock_yaml, mock_token):
        """Test: No source files to check passes"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        mock_repo_tests.return_value = []
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["README.md", "docs/guide.md"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        # When there are no source files matching patterns, it still passes
        assert "All source files have test coverage" in result["message"]


class TestParseTestingConfig:
    """Tests for parsing testing config from .outcomeops.yaml"""

    def test_parse_require_tests_config(self):
        """Test parsing require_tests format"""
        raw_config = {
            "code_generation": {"backend": "java"},
            "testing": {
                "require_tests": [
                    {
                        "pattern": "src/main/java/**/*Controller.java",
                        "test_pattern": "src/test/java/**/{name}Tests.java",
                        "description": "Controllers require tests"
                    }
                ],
                "exclude_from_tests": [
                    {
                        "pattern": "src/main/java/**/*Repository.java",
                        "reason": "Tested via integration tests"
                    }
                ]
            }
        }

        config = parse_testing_config(raw_config, "java")

        assert len(config.require_tests) == 1
        assert config.require_tests[0].pattern == "src/main/java/**/*Controller.java"
        assert config.require_tests[0].description == "Controllers require tests"
        assert len(config.exclude_from_tests) == 1
        assert config.exclude_from_tests[0].pattern == "src/main/java/**/*Repository.java"
        assert config.exclude_from_tests[0].reason == "Tested via integration tests"

    def test_parse_legacy_format(self):
        """Test parsing legacy source_patterns/test_patterns format"""
        raw_config = {
            "testing": {
                "source_patterns": ["src/**/*.py"],
                "test_patterns": ["**/test_*.py"],
                "exclude_patterns": ["**/tests/**"]
            }
        }

        config = parse_testing_config(raw_config)

        assert config.source_patterns == ["src/**/*.py"]
        assert config.test_patterns == ["**/test_*.py"]
        assert config.exclude_patterns == ["**/tests/**"]

    def test_parse_empty_config(self):
        """Test parsing empty testing config"""
        raw_config = {}
        config = parse_testing_config(raw_config)

        assert config.require_tests == []
        assert config.exclude_from_tests == []
        assert config.test_patterns == []


class TestIsFileExcluded:
    """Tests for file exclusion logic"""

    def test_file_matches_exclusion_pattern(self):
        """Test file matching exclusion pattern"""
        exclusions = [
            TestExclusion(
                pattern="src/main/java/**/*Repository.java",
                reason="Repos tested via integration tests"
            )
        ]

        is_excluded, reason = is_file_excluded(
            "src/main/java/com/example/UserRepository.java",
            exclusions
        )

        assert is_excluded
        assert "Repos tested" in reason

    def test_file_not_excluded(self):
        """Test file not matching any exclusion"""
        exclusions = [
            TestExclusion(
                pattern="src/main/java/**/*Repository.java",
                reason="Repos tested via integration tests"
            )
        ]

        is_excluded, reason = is_file_excluded(
            "src/main/java/com/example/UserController.java",
            exclusions
        )

        assert not is_excluded
        assert reason == ""


class TestFileRequiresTests:
    """Tests for determining if a file requires tests"""

    def test_controller_requires_tests(self):
        """Test that controller matches require_tests pattern"""
        requirements = [
            TestRequirement(
                pattern="src/main/java/**/*Controller.java",
                test_pattern="src/test/java/**/{name}Tests.java",
                description="Controllers require @WebMvcTest"
            )
        ]

        requires, req = file_requires_tests(
            "src/main/java/com/example/UserController.java",
            requirements
        )

        assert requires
        assert req.description == "Controllers require @WebMvcTest"

    def test_model_does_not_require_tests(self):
        """Test that model doesn't match require_tests patterns"""
        requirements = [
            TestRequirement(
                pattern="src/main/java/**/*Controller.java",
                test_pattern="src/test/java/**/{name}Tests.java",
                description="Controllers require tests"
            )
        ]

        requires, req = file_requires_tests(
            "src/main/java/com/example/model/User.java",
            requirements
        )

        assert not requires
        assert req is None


class TestFetchRepoTestFiles:
    """Tests for fetching all test files from repository"""

    @patch('test_coverage_handler.requests.get')
    def test_fetch_repo_test_files_success(self, mock_get):
        """Test successfully fetching test files from repo"""
        # Mock repo info response
        repo_response = MagicMock()
        repo_response.status_code = 200
        repo_response.json.return_value = {"default_branch": "main"}

        # Mock tree response
        tree_response = MagicMock()
        tree_response.status_code = 200
        tree_response.json.return_value = {
            "tree": [
                {"path": "src/main/java/App.java", "type": "blob"},
                {"path": "src/test/java/AppTests.java", "type": "blob"},
                {"path": "src/test/java/ServiceTests.java", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ]
        }

        mock_get.side_effect = [repo_response, tree_response]

        test_files = fetch_repo_test_files(
            "owner/repo",
            "token",
            ["src/test/java/**/*Tests.java"]
        )

        assert len(test_files) == 2
        assert "src/test/java/AppTests.java" in test_files
        assert "src/test/java/ServiceTests.java" in test_files

    @patch('test_coverage_handler.requests.get')
    def test_fetch_repo_test_files_api_error(self, mock_get):
        """Test handling API error gracefully"""
        mock_get.side_effect = Exception("API error")

        test_files = fetch_repo_test_files(
            "owner/repo",
            "token",
            ["src/test/java/**/*Tests.java"]
        )

        assert test_files == []


class TestNewFormatIntegration:
    """Integration tests for the new require_tests/exclude_from_tests format"""

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.fetch_repo_test_files')
    @patch('test_coverage_handler.query_testing_standards')
    def test_java_repository_excluded_from_tests(self, mock_kb, mock_repo_tests, mock_yaml, mock_token):
        """Test: Java repository files are excluded from test requirements"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {
            "code_generation": {"backend": "java"},
            "testing": {
                "require_tests": [
                    {
                        "pattern": "src/main/java/**/*Controller.java",
                        "test_pattern": "src/test/java/**/{name}Tests.java",
                        "description": "Controllers require tests"
                    }
                ],
                "exclude_from_tests": [
                    {
                        "pattern": "src/main/java/**/*Repository.java",
                        "reason": "Spring Data repos tested via integration tests"
                    }
                ],
                "test_patterns": ["src/test/java/**/*Tests.java"]
            }
        }
        mock_repo_tests.return_value = []  # No existing tests
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            [
                "src/main/java/com/example/PetRepository.java",
                "src/main/java/com/example/VisitRepository.java"
            ],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        # Should PASS because repositories are excluded
        assert result["status"] == "PASS"
        assert "excluded per config" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.fetch_repo_test_files')
    @patch('test_coverage_handler.query_testing_standards')
    def test_java_controller_requires_test(self, mock_kb, mock_repo_tests, mock_yaml, mock_token):
        """Test: Java controller without test fails"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {
            "code_generation": {"backend": "java"},
            "testing": {
                "require_tests": [
                    {
                        "pattern": "src/main/java/**/*Controller.java",
                        "test_pattern": "src/test/java/**/{name}Tests.java",
                        "description": "Controllers require @WebMvcTest"
                    }
                ],
                "exclude_from_tests": [
                    {
                        "pattern": "src/main/java/**/*Repository.java",
                        "reason": "Tested via integration tests"
                    }
                ],
                "test_patterns": ["src/test/java/**/*Tests.java"]
            }
        }
        mock_repo_tests.return_value = []  # No existing tests
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["src/main/java/com/example/PetStatisticsController.java"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "FAIL"
        assert "1 source file(s) missing required tests" in result["message"]
        assert "PetStatisticsController" in result["details"][0]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.fetch_repo_test_files')
    @patch('test_coverage_handler.query_testing_standards')
    def test_java_controller_with_existing_repo_test(self, mock_kb, mock_repo_tests, mock_yaml, mock_token):
        """Test: Controller passes when test exists in repo (not in PR)"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {
            "code_generation": {"backend": "java"},
            "testing": {
                "require_tests": [
                    {
                        "pattern": "src/main/java/**/*Controller.java",
                        "test_pattern": "src/test/java/**/{name}Tests.java",
                        "description": "Controllers require tests"
                    }
                ],
                "test_patterns": ["src/test/java/**/*Tests.java"]
            }
        }
        # Test file exists in repo but not in PR changed files
        mock_repo_tests.return_value = [
            "src/test/java/com/example/stats/PetStatisticsControllerTests.java"
        ]
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["src/main/java/com/example/stats/PetStatisticsController.java"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "All source files have test coverage" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.fetch_repo_test_files')
    @patch('test_coverage_handler.query_testing_standards')
    def test_mixed_files_correct_handling(self, mock_kb, mock_repo_tests, mock_yaml, mock_token):
        """Test: Mixed files - some excluded, some require tests"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {
            "code_generation": {"backend": "java"},
            "testing": {
                "require_tests": [
                    {
                        "pattern": "src/main/java/**/*Controller.java",
                        "test_pattern": "src/test/java/**/{name}Tests.java",
                        "description": "Controllers require tests"
                    }
                ],
                "exclude_from_tests": [
                    {
                        "pattern": "src/main/java/**/*Repository.java",
                        "reason": "Tested via integration tests"
                    }
                ],
                "test_patterns": ["src/test/java/**/*Tests.java"]
            }
        }
        # Test exists for controller
        mock_repo_tests.return_value = [
            "src/test/java/com/example/PetControllerTests.java"
        ]
        mock_kb.return_value = ("", None, {})  # 3-tuple: (answer, adr_ref, usage)

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            [
                "src/main/java/com/example/PetController.java",  # Has test
                "src/main/java/com/example/PetRepository.java",  # Excluded
                "src/main/java/com/example/model/Pet.java",  # No pattern match
            ],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"


class TestUsageTracking:
    """Tests for usage tracking in test coverage check"""

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_usage_tracker_returned_in_result(self, mock_kb, mock_yaml, mock_token):
        """Test: UsageTracker is returned in check result"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        # Return usage data from KB query
        mock_kb.return_value = ("Testing standards", None, {"input_tokens": 100, "output_tokens": 50})

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        assert "usage" in result
        # UsageTracker should have usage data from both local and global queries
        usage = result["usage"]
        assert hasattr(usage, 'total_tokens')

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_usage_aggregated_from_local_and_global_queries(self, mock_kb, mock_yaml, mock_token):
        """Test: Usage is aggregated from both local and global KB queries"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        # Return usage data from KB query (called twice: local and global)
        mock_kb.return_value = ("Testing standards", None, {"input_tokens": 100, "output_tokens": 50})

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        usage = result["usage"]
        # query_testing_standards is called twice (local + global)
        # Each call adds 100 input + 50 output tokens
        assert usage.total_tokens == 300  # (100+50) * 2 = 300

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_empty_usage_when_no_kb_response(self, mock_kb, mock_yaml, mock_token):
        """Test: Empty usage when KB returns no usage data"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}
        # Return empty usage
        mock_kb.return_value = ("", None, {})

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        usage = result["usage"]
        assert usage.total_tokens == 0


class TestLocalGlobalStandards:
    """Tests for local/global testing standards (ADR-007)"""

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_local_adr_takes_precedence(self, mock_kb, mock_yaml, mock_token):
        """Test: Local ADR reference takes precedence over global"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}

        # First call (local) returns local ADR, second call (global) returns global ADR
        mock_kb.side_effect = [
            ("Local testing standards per ADR-LOCAL", "ADR-LOCAL", {}),
            ("Global testing standards per ADR-GLOBAL", "ADR-GLOBAL", {}),
        ]

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        # Local ADR should take precedence
        assert "ADR-LOCAL" in result["message"]

    @patch('test_coverage_handler.get_github_token')
    @patch('test_coverage_handler.fetch_outcomeops_yaml')
    @patch('test_coverage_handler.query_testing_standards')
    def test_falls_back_to_global_when_no_local(self, mock_kb, mock_yaml, mock_token):
        """Test: Falls back to global ADR when no local ADR found"""
        mock_token.return_value = "token"
        mock_yaml.return_value = {"code_generation": {"backend": "python"}}

        # First call (local) returns no ADR, second call (global) returns global ADR
        mock_kb.side_effect = [
            ("", None, {}),  # No local standards
            ("Global testing standards per ADR-GLOBAL", "ADR-GLOBAL", {}),
        ]

        result = check_test_coverage(
            "TEST_COVERAGE", 123, "owner/repo",
            ["lambda/hello/handler.py", "lambda/tests/unit/test_hello.py"],
            github_token_param="/test/token",
            query_kb_lambda_name="test-query-kb"
        )

        assert result["status"] == "PASS"
        # Global ADR should be used as fallback
        assert "ADR-GLOBAL" in result["message"]
