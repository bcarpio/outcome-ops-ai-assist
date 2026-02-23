"""
Unit tests for code generator backends.

Tests the backend abstraction layer for code generation across different languages.
"""

import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add lambda directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../generate-code'))

from codegen_backends.base import (
    CodeGeneratorBackend,
    ValidationResult,
    FormattingResult,
    StandardsContext,
)
from codegen_backends.python import PythonCodeGenerator
from codegen_backends.java import JavaCodeGenerator
from codegen_backends.typescript import TypeScriptCodeGenerator
from codegen_backends.factory import (
    register_backend,
    get_backend,
    list_backends,
    get_backend_for_repo,
    _BACKEND_REGISTRY,
)


# =============================================================================
# PythonCodeGenerator Tests
# =============================================================================


class TestPythonCodeGenerator:
    """Tests for PythonCodeGenerator backend."""

    @pytest.fixture
    def backend(self):
        """Create a PythonCodeGenerator instance."""
        return PythonCodeGenerator()

    def test_name(self, backend):
        """Test backend name."""
        assert backend.name == "python"

    def test_display_name(self, backend):
        """Test backend display name."""
        assert backend.display_name == "Python"

    def test_source_extensions(self, backend):
        """Test source file extensions."""
        assert backend.source_extensions == {".py"}

    def test_dependency_files(self, backend):
        """Test dependency file names."""
        expected = {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile"}
        assert backend.dependency_files == expected

    def test_is_source_file(self, backend):
        """Test is_source_file method."""
        assert backend.is_source_file("handler.py") is True
        assert backend.is_source_file("utils.py") is True
        assert backend.is_source_file("handler.js") is False
        assert backend.is_source_file("handler.java") is False

    def test_is_dependency_file(self, backend):
        """Test is_dependency_file method."""
        assert backend.is_dependency_file("requirements.txt") is True
        assert backend.is_dependency_file("lambda/ingest-docs/requirements.txt") is True
        assert backend.is_dependency_file("pyproject.toml") is True
        assert backend.is_dependency_file("package.json") is False
        assert backend.is_dependency_file("pom.xml") is False

    def test_is_test_file(self, backend):
        """Test is_test_file method."""
        assert backend.is_test_file("tests/unit/test_handler.py") is True
        assert backend.is_test_file("handler_test.py") is True
        assert backend.is_test_file("handler.py") is False

    def test_get_handler_standards_queries(self, backend):
        """Test handler standards queries - generic language-focused queries."""
        queries = backend.get_handler_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, framework patterns come from ADRs
        assert any("Python" in q or "handler" in q.lower() for q in queries)

    def test_get_testing_standards_queries(self, backend):
        """Test testing standards queries."""
        queries = backend.get_testing_standards_queries()
        assert len(queries) > 0
        assert any("test" in q.lower() for q in queries)

    def test_get_infrastructure_standards_queries(self, backend):
        """Test infrastructure standards queries - generic IaC queries."""
        queries = backend.get_infrastructure_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, specific IaC tool comes from project ADRs
        assert any("infrastructure" in q.lower() or "iac" in q.lower() for q in queries)

    def test_get_language_standards_queries(self, backend):
        """Test language-specific standards queries."""
        queries = backend.get_language_standards_queries()
        assert len(queries) > 0
        assert any("Python" in q for q in queries)

    def test_get_plan_generation_system_prompt(self, backend):
        """Test plan generation system prompt."""
        prompt = backend.get_plan_generation_system_prompt()
        assert "Python" in prompt or "Lambda" in prompt
        assert "step" in prompt.lower()
        assert "JSON" in prompt

    def test_get_code_generation_system_prompt(self, backend):
        """Test code generation system prompt."""
        prompt = backend.get_code_generation_system_prompt()
        assert "Python" in prompt
        assert "base64" in prompt.lower()

    def test_get_formatters(self, backend):
        """Test formatters list."""
        formatters = backend.get_formatters()
        assert len(formatters) >= 1

        # Should have terraform formatter
        terraform_formatter = next(
            (f for f in formatters if f["name"] == "terraform"), None
        )
        assert terraform_formatter is not None
        assert ".tf" in terraform_formatter["extensions"]

    def test_should_format_file(self, backend):
        """Test should_format_file method."""
        assert backend.should_format_file("main.tf") is True
        assert backend.should_format_file("terraform/lambda.tf") is True
        assert backend.should_format_file("handler.py") is False  # black not enabled

    def test_get_test_file_pattern(self, backend):
        """Test get_test_file_pattern method."""
        # Handler file
        pattern = backend.get_test_file_pattern("lambda/ingest-docs/handler.py")
        assert pattern == "lambda/tests/unit/test_ingest_docs.py"

        # Non-handler file
        pattern = backend.get_test_file_pattern("lambda/generate-code/utils.py")
        assert pattern == "lambda/tests/unit/test_generate_code_utils.py"

    def test_get_test_step_breakdown(self, backend):
        """Test test step breakdown."""
        breakdown = backend.get_test_step_breakdown()
        assert len(breakdown) >= 3
        assert any("success" in s.lower() or "happy" in s.lower() for s in breakdown)
        assert any("error" in s.lower() for s in breakdown)
        assert any("edge" in s.lower() for s in breakdown)


class TestPythonValidation:
    """Tests for PythonCodeGenerator validation."""

    @pytest.fixture
    def backend(self):
        """Create a PythonCodeGenerator instance."""
        return PythonCodeGenerator()

    def test_validate_python_syntax_valid(self, backend):
        """Test validation of valid Python syntax."""
        content = '''
def handler(event, context):
    """Lambda handler."""
    return {"statusCode": 200}
'''
        result = backend.validate_file("handler.py", content)
        assert result.valid is True
        assert result.error_message is None

    def test_validate_python_syntax_invalid(self, backend):
        """Test validation of invalid Python syntax."""
        content = '''
def handler(event, context)  # Missing colon
    return {"statusCode": 200}
'''
        result = backend.validate_file("handler.py", content)
        assert result.valid is False
        assert result.error_message is not None
        assert "syntax" in result.error_message.lower()

    def test_validate_non_python_file(self, backend):
        """Test validation of non-Python file (should pass)."""
        content = "some random content"
        result = backend.validate_file("config.json", content)
        assert result.valid is True

    @patch("subprocess.run")
    def test_validate_requirements_valid(self, mock_run, backend):
        """Test validation of valid requirements.txt."""
        mock_run.return_value = MagicMock(returncode=0)

        content = "boto3==1.34.0\npydantic==2.0.0"
        result = backend.validate_file("requirements.txt", content)

        assert result.valid is True
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_validate_requirements_invalid_package(self, mock_run, backend):
        """Test validation of requirements.txt with invalid package."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="No matching distribution found for nonexistent-package"
        )

        content = "nonexistent-package==1.0.0"
        result = backend.validate_file("requirements.txt", content)

        assert result.valid is False
        assert "validation failed" in result.error_message.lower()

    def test_validate_files_multiple(self, backend):
        """Test validating multiple files."""
        files = [
            {"path": "handler.py", "content": "def handler(): pass"},
            {"path": "utils.py", "content": "def util(): pass"},
            {"path": "config.json", "content": "{}"},
        ]

        results = backend.validate_files(files)

        assert len(results) == 3
        assert all(r.valid for r in results)


# =============================================================================
# Backend Factory Tests
# =============================================================================


class TestBackendFactory:
    """Tests for backend factory functions."""

    def test_get_backend_python_lambda(self):
        """Test getting python backend."""
        backend = get_backend("python")
        assert backend is not None
        assert backend.name == "python"
        assert isinstance(backend, PythonCodeGenerator)

    def test_get_backend_with_config(self):
        """Test getting backend with repo config."""
        config = {"custom_setting": "value"}
        backend = get_backend("python", repo_config=config)
        assert backend.repo_config == config

    def test_get_backend_unknown(self):
        """Test getting unknown backend raises error."""
        with pytest.raises(ValueError) as exc_info:
            get_backend("unknown-backend")
        assert "Unknown code generator backend" in str(exc_info.value)

    def test_list_backends(self):
        """Test listing all backends."""
        backends = list_backends()
        assert "python" in backends
        assert backends["python"] == "Python"

    def test_get_backend_for_repo_default(self):
        """Test getting backend for repo with no config."""
        backend = get_backend_for_repo(repo_config=None)
        assert backend.name == "python"  # Default

    def test_get_backend_for_repo_with_config(self):
        """Test getting backend for repo with config."""
        config = {
            "code_generation": {
                "backend": "python"
            }
        }
        backend = get_backend_for_repo(repo_config=config)
        assert backend.name == "python"


# =============================================================================
# StandardsContext Tests
# =============================================================================


class TestStandardsContext:
    """Tests for StandardsContext dataclass."""

    def test_default_values(self):
        """Test default values."""
        context = StandardsContext()
        assert context.handler_standards == []
        assert context.testing_standards == []
        assert context.infrastructure_standards == []
        assert context.language_standards == []
        assert context.framework_standards == []

    def test_with_values(self):
        """Test with values."""
        context = StandardsContext(
            handler_standards=["standard1"],
            testing_standards=["test1"],
            infrastructure_standards=["infra1"],
        )
        assert context.handler_standards == ["standard1"]
        assert context.testing_standards == ["test1"]
        assert context.infrastructure_standards == ["infra1"]


# =============================================================================
# ValidationResult Tests
# =============================================================================


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_valid_result(self):
        """Test valid result."""
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.error_message is None

    def test_invalid_result(self):
        """Test invalid result."""
        result = ValidationResult(
            valid=False,
            error_message="Invalid syntax",
            file_path="handler.py"
        )
        assert result.valid is False
        assert result.error_message == "Invalid syntax"
        assert result.file_path == "handler.py"


# =============================================================================
# TypeScriptCodeGenerator Tests
# =============================================================================


class TestTypeScriptCodeGenerator:
    """Tests for TypeScriptCodeGenerator backend."""

    @pytest.fixture
    def backend(self):
        """Create a TypeScriptCodeGenerator instance."""
        return TypeScriptCodeGenerator()

    def test_name(self, backend):
        """Test backend name."""
        assert backend.name == "typescript"

    def test_display_name(self, backend):
        """Test backend display name."""
        assert backend.display_name == "TypeScript"

    def test_source_extensions(self, backend):
        """Test source file extensions."""
        assert ".ts" in backend.source_extensions
        assert ".tsx" in backend.source_extensions
        assert ".js" in backend.source_extensions
        assert ".jsx" in backend.source_extensions

    def test_dependency_files(self, backend):
        """Test dependency file names."""
        expected = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        assert backend.dependency_files == expected

    def test_is_source_file(self, backend):
        """Test is_source_file method."""
        assert backend.is_source_file("handler.ts") is True
        assert backend.is_source_file("handler.tsx") is True
        assert backend.is_source_file("handler.js") is True
        assert backend.is_source_file("handler.py") is False
        assert backend.is_source_file("handler.java") is False

    def test_is_dependency_file(self, backend):
        """Test is_dependency_file method."""
        assert backend.is_dependency_file("package.json") is True
        assert backend.is_dependency_file("src/package.json") is True
        assert backend.is_dependency_file("yarn.lock") is True
        assert backend.is_dependency_file("requirements.txt") is False
        assert backend.is_dependency_file("pom.xml") is False

    def test_is_test_file(self, backend):
        """Test is_test_file method."""
        assert backend.is_test_file("users.test.ts") is True
        assert backend.is_test_file("users.spec.ts") is True
        assert backend.is_test_file("handler.ts") is False

    def test_get_handler_standards_queries(self, backend):
        """Test handler standards queries - generic language-focused queries."""
        queries = backend.get_handler_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, framework patterns come from ADRs
        assert any("TypeScript" in q or "handler" in q.lower() for q in queries)

    def test_get_testing_standards_queries(self, backend):
        """Test testing standards queries."""
        queries = backend.get_testing_standards_queries()
        assert len(queries) > 0
        assert any("Jest" in q or "test" in q.lower() for q in queries)

    def test_get_infrastructure_standards_queries(self, backend):
        """Test infrastructure standards queries - generic IaC queries."""
        queries = backend.get_infrastructure_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, specific IaC tool comes from project ADRs
        assert any("infrastructure" in q.lower() or "iac" in q.lower() for q in queries)

    def test_get_language_standards_queries(self, backend):
        """Test language-specific standards queries."""
        queries = backend.get_language_standards_queries()
        assert len(queries) > 0
        assert any("TypeScript" in q for q in queries)

    def test_get_framework_standards_queries(self, backend):
        """Test framework-specific standards queries - generic queries."""
        queries = backend.get_framework_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, framework patterns come from ADRs
        assert any("TypeScript" in q or "api" in q.lower() or "framework" in q.lower() for q in queries)

    def test_get_plan_generation_system_prompt(self, backend):
        """Test plan generation system prompt."""
        prompt = backend.get_plan_generation_system_prompt()
        assert "TypeScript" in prompt or "Express" in prompt
        assert "step" in prompt.lower()
        assert "JSON" in prompt

    def test_get_code_generation_system_prompt(self, backend):
        """Test code generation system prompt."""
        prompt = backend.get_code_generation_system_prompt()
        assert "TypeScript" in prompt
        assert "base64" in prompt.lower()

    def test_get_formatters(self, backend):
        """Test formatters list."""
        formatters = backend.get_formatters()
        assert len(formatters) >= 2

        # Should have terraform formatter
        terraform_formatter = next(
            (f for f in formatters if f["name"] == "terraform"), None
        )
        assert terraform_formatter is not None
        assert ".tf" in terraform_formatter["extensions"]

        # Should have prettier formatter
        prettier_formatter = next(
            (f for f in formatters if f["name"] == "prettier"), None
        )
        assert prettier_formatter is not None
        assert ".ts" in prettier_formatter["extensions"]

    def test_should_format_file(self, backend):
        """Test should_format_file method."""
        assert backend.should_format_file("main.tf") is True
        assert backend.should_format_file("handler.ts") is True
        assert backend.should_format_file("package.json") is True

    def test_get_test_file_pattern(self, backend):
        """Test get_test_file_pattern method."""
        # Route file -> integration test
        pattern = backend.get_test_file_pattern("src/routes/users.ts")
        assert pattern == "tests/integration/users.test.ts"

        # Service file -> unit test
        pattern = backend.get_test_file_pattern("src/services/userService.ts")
        assert pattern == "tests/unit/userService.test.ts"

    def test_get_test_step_breakdown(self, backend):
        """Test test step breakdown."""
        breakdown = backend.get_test_step_breakdown()
        assert len(breakdown) >= 3
        assert any("success" in s.lower() or "happy" in s.lower() for s in breakdown)
        assert any("error" in s.lower() for s in breakdown)
        assert any("integration" in s.lower() or "supertest" in s.lower() for s in breakdown)


class TestTypeScriptExpressValidation:
    """Tests for TypeScriptCodeGenerator validation."""

    @pytest.fixture
    def backend(self):
        """Create a TypeScriptCodeGenerator instance."""
        return TypeScriptCodeGenerator()

    def test_validate_package_json_valid(self, backend):
        """Test validation of valid package.json."""
        content = '{"name": "my-app", "version": "1.0.0", "dependencies": {}}'
        result = backend.validate_file("package.json", content)
        assert result.valid is True
        assert result.error_message is None

    def test_validate_package_json_missing_name(self, backend):
        """Test validation of package.json missing name."""
        content = '{"version": "1.0.0"}'
        result = backend.validate_file("package.json", content)
        assert result.valid is False
        assert "name" in result.error_message.lower()

    def test_validate_package_json_invalid_json(self, backend):
        """Test validation of invalid JSON in package.json."""
        content = '{"name": "my-app", invalid}'
        result = backend.validate_file("package.json", content)
        assert result.valid is False
        assert "json" in result.error_message.lower()

    def test_validate_package_json_invalid_dependencies(self, backend):
        """Test validation of package.json with invalid dependencies."""
        content = '{"name": "my-app", "dependencies": "not-an-object"}'
        result = backend.validate_file("package.json", content)
        assert result.valid is False
        assert "dependencies" in result.error_message.lower()

    def test_validate_non_ts_file(self, backend):
        """Test validation of non-TypeScript file (should pass)."""
        content = "some random content"
        result = backend.validate_file("config.yaml", content)
        assert result.valid is True


# =============================================================================
# JavaCodeGenerator Tests
# =============================================================================


class TestJavaCodeGenerator:
    """Tests for JavaCodeGenerator backend."""

    @pytest.fixture
    def backend(self):
        """Create a JavaCodeGenerator instance."""
        return JavaCodeGenerator()

    def test_name(self, backend):
        """Test backend name."""
        assert backend.name == "java"

    def test_display_name(self, backend):
        """Test backend display name."""
        assert backend.display_name == "Java"

    def test_source_extensions(self, backend):
        """Test source file extensions."""
        assert backend.source_extensions == {".java"}

    def test_dependency_files(self, backend):
        """Test dependency file names."""
        expected = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}
        assert backend.dependency_files == expected

    def test_is_source_file(self, backend):
        """Test is_source_file method."""
        assert backend.is_source_file("UserController.java") is True
        assert backend.is_source_file("UserService.java") is True
        assert backend.is_source_file("handler.py") is False
        assert backend.is_source_file("handler.ts") is False

    def test_is_dependency_file(self, backend):
        """Test is_dependency_file method."""
        assert backend.is_dependency_file("pom.xml") is True
        assert backend.is_dependency_file("build.gradle") is True
        assert backend.is_dependency_file("build.gradle.kts") is True
        assert backend.is_dependency_file("requirements.txt") is False
        assert backend.is_dependency_file("package.json") is False

    def test_is_test_file(self, backend):
        """Test is_test_file method."""
        assert backend.is_test_file("UserControllerTest.java") is True
        assert backend.is_test_file("UserServiceTests.java") is True
        assert backend.is_test_file("UserController.java") is False

    def test_get_handler_standards_queries(self, backend):
        """Test handler standards queries - generic language-focused queries."""
        queries = backend.get_handler_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, framework patterns come from ADRs
        assert any("Java" in q or "handler" in q.lower() for q in queries)

    def test_get_testing_standards_queries(self, backend):
        """Test testing standards queries."""
        queries = backend.get_testing_standards_queries()
        assert len(queries) > 0
        assert any("JUnit" in q or "test" in q.lower() for q in queries)

    def test_get_infrastructure_standards_queries(self, backend):
        """Test infrastructure standards queries - generic IaC queries."""
        queries = backend.get_infrastructure_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, specific IaC tool comes from project ADRs
        assert any("infrastructure" in q.lower() or "iac" in q.lower() for q in queries)

    def test_get_language_standards_queries(self, backend):
        """Test language-specific standards queries."""
        queries = backend.get_language_standards_queries()
        assert len(queries) > 0
        assert any("Java" in q for q in queries)

    def test_get_framework_standards_queries(self, backend):
        """Test framework-specific standards queries - generic queries."""
        queries = backend.get_framework_standards_queries()
        assert len(queries) > 0
        # KB-driven: queries are generic, framework patterns come from ADRs
        assert any("Java" in q or "api" in q.lower() or "framework" in q.lower() for q in queries)

    def test_get_plan_generation_system_prompt(self, backend):
        """Test plan generation system prompt."""
        prompt = backend.get_plan_generation_system_prompt()
        assert "Java" in prompt or "Spring" in prompt
        assert "step" in prompt.lower()
        assert "JSON" in prompt

    def test_get_code_generation_system_prompt(self, backend):
        """Test code generation system prompt."""
        prompt = backend.get_code_generation_system_prompt()
        assert "Java" in prompt
        assert "base64" in prompt.lower()

    def test_get_formatters(self, backend):
        """Test formatters list."""
        formatters = backend.get_formatters()
        assert len(formatters) >= 2

        # Should have terraform formatter
        terraform_formatter = next(
            (f for f in formatters if f["name"] == "terraform"), None
        )
        assert terraform_formatter is not None
        assert ".tf" in terraform_formatter["extensions"]

        # Should have java formatter
        java_formatter = next(
            (f for f in formatters if f["name"] == "google-java-format"), None
        )
        assert java_formatter is not None
        assert ".java" in java_formatter["extensions"]

    def test_should_format_file(self, backend):
        """Test should_format_file method."""
        assert backend.should_format_file("main.tf") is True
        assert backend.should_format_file("UserController.java") is True

    def test_get_test_file_pattern(self, backend):
        """Test get_test_file_pattern method."""
        # Controller file
        pattern = backend.get_test_file_pattern("src/main/java/com/example/controller/UserController.java")
        assert pattern == "src/test/java/com/example/controller/UserControllerTest.java"

        # Service file
        pattern = backend.get_test_file_pattern("src/main/java/com/example/service/UserService.java")
        assert pattern == "src/test/java/com/example/service/UserServiceTest.java"

    def test_get_test_step_breakdown(self, backend):
        """Test test step breakdown."""
        breakdown = backend.get_test_step_breakdown()
        assert len(breakdown) >= 3
        assert any("success" in s.lower() or "happy" in s.lower() for s in breakdown)
        assert any("error" in s.lower() or "assertThrows" in s for s in breakdown)
        assert any("integration" in s.lower() or "SpringBootTest" in s for s in breakdown)


class TestJavaValidation:
    """Tests for JavaCodeGenerator validation."""

    @pytest.fixture
    def backend(self):
        """Create a JavaCodeGenerator instance."""
        return JavaCodeGenerator()

    def test_validate_pom_xml_valid(self, backend):
        """Test validation of valid pom.xml."""
        content = '''<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <groupId>com.example</groupId>
    <artifactId>my-app</artifactId>
    <version>1.0.0</version>
</project>'''
        result = backend.validate_file("pom.xml", content)
        assert result.valid is True
        assert result.error_message is None

    def test_validate_pom_xml_missing_artifactid(self, backend):
        """Test validation of pom.xml missing artifactId."""
        content = '''<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <groupId>com.example</groupId>
    <version>1.0.0</version>
</project>'''
        result = backend.validate_file("pom.xml", content)
        assert result.valid is False
        assert "artifactId" in result.error_message

    def test_validate_pom_xml_with_parent(self, backend):
        """Test validation of pom.xml with parent (no groupId required)."""
        content = '''<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>3.0.0</version>
    </parent>
    <artifactId>my-app</artifactId>
</project>'''
        result = backend.validate_file("pom.xml", content)
        assert result.valid is True

    def test_validate_pom_xml_invalid_xml(self, backend):
        """Test validation of invalid XML in pom.xml."""
        content = '<project><groupId>com.example</groupId><unclosed>'
        result = backend.validate_file("pom.xml", content)
        assert result.valid is False
        assert "xml" in result.error_message.lower()

    def test_validate_gradle_valid(self, backend):
        """Test validation of valid build.gradle."""
        content = '''plugins {
    id 'java'
    id 'org.springframework.boot' version '3.0.0'
}

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
}'''
        result = backend.validate_file("build.gradle", content)
        assert result.valid is True
        assert result.error_message is None

    def test_validate_gradle_unbalanced_braces(self, backend):
        """Test validation of build.gradle with unbalanced braces."""
        content = '''plugins {
    id 'java'

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
}'''
        result = backend.validate_file("build.gradle", content)
        assert result.valid is False
        assert "brace" in result.error_message.lower()

    def test_validate_non_java_file(self, backend):
        """Test validation of non-Java file (should pass)."""
        content = "some random content"
        result = backend.validate_file("config.yaml", content)
        assert result.valid is True


# =============================================================================
# Backend Factory Extended Tests
# =============================================================================


class TestBackendFactoryExtended:
    """Extended tests for backend factory with all backends."""

    def test_get_backend_java_spring(self):
        """Test getting java backend."""
        backend = get_backend("java")
        assert backend is not None
        assert backend.name == "java"
        assert isinstance(backend, JavaCodeGenerator)

    def test_get_backend_typescript_express(self):
        """Test getting typescript backend."""
        backend = get_backend("typescript")
        assert backend is not None
        assert backend.name == "typescript"
        assert isinstance(backend, TypeScriptCodeGenerator)

    def test_list_backends_includes_all(self):
        """Test listing all backends includes all three."""
        backends = list_backends()
        assert "python" in backends
        assert "java" in backends
        assert "typescript" in backends
        assert backends["python"] == "Python"
        assert backends["java"] == "Java"
        assert backends["typescript"] == "TypeScript"

    def test_get_backend_for_repo_java_spring(self):
        """Test getting java backend for repo."""
        config = {
            "code_generation": {
                "backend": "java"
            }
        }
        backend = get_backend_for_repo(repo_config=config)
        assert backend.name == "java"

    def test_get_backend_for_repo_typescript_express(self):
        """Test getting typescript backend for repo."""
        config = {
            "code_generation": {
                "backend": "typescript"
            }
        }
        backend = get_backend_for_repo(repo_config=config)
        assert backend.name == "typescript"
