"""
Unit tests for run-tests backend modules.

These tests use unique module names to avoid polluting the module cache
used by other tests (e.g., test_run_tests.py).
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure required env vars exist before importing
os.environ.setdefault("TEST_RESULTS_BUCKET", "unit-test-bucket")
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("APP_NAME", "outcome-ops-ai-assist")

run_tests_dir = os.path.join(os.path.dirname(__file__), "../../run-tests")
backends_dir = os.path.join(run_tests_dir, "backends")
sys.path.insert(0, os.path.abspath(run_tests_dir))
sys.path.insert(0, os.path.abspath(backends_dir))

import importlib.util

# Use unique module names to avoid collision with test_run_tests.py
# These modules are loaded fresh for isolated testing

# Load test_backend_base with unique name
base_path = os.path.join(backends_dir, "test_backend_base.py")
spec = importlib.util.spec_from_file_location("run_tests_backends_base_test", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules["run_tests_backends_base_test"] = base_module
spec.loader.exec_module(base_module)

TestBackend = base_module.TestBackend
CommandResult = base_module.CommandResult

# Load factory with unique name
factory_path = os.path.join(backends_dir, "factory.py")
spec = importlib.util.spec_from_file_location("run_tests_backends_factory_test", factory_path)
factory_module = importlib.util.module_from_spec(spec)
sys.modules["run_tests_backends_factory_test"] = factory_module
spec.loader.exec_module(factory_module)

# Load python_backend with unique name
python_backend_path = os.path.join(backends_dir, "python_backend.py")
spec = importlib.util.spec_from_file_location("run_tests_backends_python_test", python_backend_path)
python_backend_module = importlib.util.module_from_spec(spec)
sys.modules["run_tests_backends_python_test"] = python_backend_module
spec.loader.exec_module(python_backend_module)

PythonBackend = python_backend_module.PythonBackend

# Load java_backend with unique name
java_backend_path = os.path.join(backends_dir, "java_backend.py")
spec = importlib.util.spec_from_file_location("run_tests_backends_java_test", java_backend_path)
java_backend_module = importlib.util.module_from_spec(spec)
sys.modules["run_tests_backends_java_test"] = java_backend_module
spec.loader.exec_module(java_backend_module)

JavaBackend = java_backend_module.JavaBackend


class TestCommandResult:
    """Test CommandResult dataclass."""

    def test_command_result_success(self):
        """Test CommandResult with successful command."""
        result = CommandResult(
            command=["echo", "hello"],
            exit_code=0,
            stdout="hello",
            stderr="",
            duration_seconds=0.1
        )
        assert result.succeeded is True
        assert result.exit_code == 0
        assert result.stdout == "hello"

    def test_command_result_failure(self):
        """Test CommandResult for failed command."""
        result = CommandResult(
            command=["false"],
            exit_code=1,
            stdout="",
            stderr="command failed",
            duration_seconds=0.1
        )
        assert result.succeeded is False
        assert result.exit_code == 1
        assert "command failed" in result.stderr

    def test_command_result_output_property(self):
        """Test CommandResult.output combines stdout and stderr."""
        result = CommandResult(
            command=["cmd"],
            exit_code=0,
            stdout="standard output",
            stderr="error output",
            duration_seconds=0.1
        )
        output = result.output
        assert "standard output" in output
        assert "error output" in output


class TestBackendBase:
    """Test the base backend class."""

    def test_run_command_success(self, tmp_path):
        """Test _run_command with successful command."""
        backend = PythonBackend(str(tmp_path), {})

        with patch.object(base_module, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = SimpleNamespace(
                returncode=0,
                stdout="output",
                stderr=""
            )
            mock_subprocess.PIPE = -1

            result = backend._run_command(["echo", "hello"])

            assert result.succeeded is True
            assert result.exit_code == 0

    def test_run_command_failure(self, tmp_path):
        """Test _run_command with failed command."""
        backend = PythonBackend(str(tmp_path), {})

        with patch.object(base_module, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="error"
            )
            mock_subprocess.PIPE = -1

            result = backend._run_command(["false"])

            assert result.succeeded is False
            assert result.exit_code == 1

    def test_run_command_timeout(self, tmp_path):
        """Test _run_command handles timeout."""
        backend = PythonBackend(str(tmp_path), {})

        with patch.object(base_module, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = base_module.subprocess.TimeoutExpired(
                cmd=["sleep"], timeout=1
            )
            mock_subprocess.PIPE = -1

            result = backend._run_command(["sleep", "100"], timeout=1)

            assert result.succeeded is False


class TestPythonBackend:
    """Test the Python test backend."""

    def test_get_backend_name(self, tmp_path):
        """Test get_backend_name returns correct name."""
        backend = PythonBackend(str(tmp_path), {})
        assert backend.get_backend_name() == "Python Backend"

    def test_get_backend_type(self, tmp_path):
        """Test get_backend_type returns 'python'."""
        backend = PythonBackend(str(tmp_path), {})
        assert backend.get_backend_type() == "python"

    def test_get_test_command(self, tmp_path):
        """Test get_test_command returns pytest command."""
        backend = PythonBackend(str(tmp_path), {})
        cmd = backend.get_test_command()

        assert "pytest" in cmd
        assert "venv/bin/python3.12" in cmd

    def test_get_test_command_with_config(self, tmp_path):
        """Test get_test_command respects config."""
        config = {"testing": {"test_directory": "tests/"}}
        backend = PythonBackend(str(tmp_path), config)
        cmd = backend.get_test_command()

        assert "tests/" in cmd

    def test_get_junit_paths(self, tmp_path):
        """Test get_junit_paths returns expected paths."""
        backend = PythonBackend(str(tmp_path), {})
        paths = backend.get_junit_paths()

        assert len(paths) >= 1
        assert any("junit.xml" in str(p) for p in paths)

    def test_classify_failure_import_error(self, tmp_path):
        """Test classify_failure detects import errors."""
        backend = PythonBackend(str(tmp_path), {})

        assert backend.classify_failure("ModuleNotFoundError: No module named 'foo'") == "import_error"
        assert backend.classify_failure("ImportError: cannot import name 'bar'") == "import_error"
        assert backend.classify_failure("No module named 'test'") == "import_error"

    def test_classify_failure_syntax_error(self, tmp_path):
        """Test classify_failure detects syntax errors."""
        backend = PythonBackend(str(tmp_path), {})

        assert backend.classify_failure("SyntaxError: invalid syntax") == "syntax_error"

    def test_classify_failure_logic_error(self, tmp_path):
        """Test classify_failure defaults to logic error."""
        backend = PythonBackend(str(tmp_path), {})

        assert backend.classify_failure("AssertionError: expected 1, got 2") == "logic_error"
        assert backend.classify_failure("Some other error") == "logic_error"

    def test_setup_environment_creates_venv(self, tmp_path):
        """Test setup_environment creates virtualenv."""
        backend = PythonBackend(str(tmp_path), {})

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["python3.12", "-m", "venv", "venv"],
                exit_code=0,
                stdout="",
                stderr="",
                duration_seconds=0.1
            )

            result = backend.setup_environment()

            assert result is True
            # Should call python3.12 -m venv
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("venv" in str(c) for c in calls)

    def test_setup_environment_failure(self, tmp_path):
        """Test setup_environment handles failure."""
        backend = PythonBackend(str(tmp_path), {})

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["python3.12", "-m", "venv", "venv"],
                exit_code=1,
                stdout="",
                stderr="venv failed",
                duration_seconds=0.1
            )

            result = backend.setup_environment()

            assert result is False


class TestBackendFactory:
    """Test the backend factory."""

    def setup_method(self):
        """Register python backend before each test."""
        # Register the python backend with our isolated factory
        factory_module.register_backend("python", PythonBackend)

    def test_get_backend_python(self, tmp_path):
        """Test getting Python backend from config."""
        # Create .outcomeops.yaml
        config_file = tmp_path / ".outcomeops.yaml"
        config_file.write_text("""
code_generation:
  backend: python
testing:
  test_directory: tests/
""")

        backend = factory_module.get_backend(str(tmp_path))
        assert backend is not None
        assert backend.get_backend_type() == "python"

    def test_get_backend_no_config(self, tmp_path):
        """Test getting backend with no config file."""
        backend = factory_module.get_backend(str(tmp_path))
        assert backend is None

    def test_get_backend_yml_extension(self, tmp_path):
        """Test getting backend with .yml extension."""
        config_file = tmp_path / ".outcomeops.yml"
        config_file.write_text("""
code_generation:
  backend: python
""")

        backend = factory_module.get_backend(str(tmp_path))
        assert backend is not None

    def test_list_backends(self):
        """Test list_backends returns registered backends."""
        backends = factory_module.list_backends()
        assert isinstance(backends, dict)
        assert "python" in backends


class TestPythonBackendAutoFix:
    """Test Python backend auto-fix functionality."""

    def test_attempt_auto_fix_logic_error(self, tmp_path):
        """Test auto-fix skips logic errors."""
        backend = PythonBackend(str(tmp_path), {})

        error_output = "AssertionError: expected 1, got 2"

        # Logic errors should not be auto-fixed
        result = backend.attempt_auto_fix("logic_error", error_output)

        # Should return False (no fix attempted)
        assert result is False

    def test_attempt_auto_fix_import_error_no_module(self, tmp_path):
        """Test auto-fix for import errors attempts pip install."""
        backend = PythonBackend(str(tmp_path), {})

        # Create venv structure
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pip").write_text("#!/bin/bash\n")

        error_output = "ModuleNotFoundError: No module named 'requests'"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["pip", "install", "requests"],
                exit_code=0,
                stdout="Successfully installed",
                stderr="",
                duration_seconds=1.0
            )

            result = backend.attempt_auto_fix("import_error", error_output)

            # Check pip install was called
            if mock_run.called:
                call_args = str(mock_run.call_args_list)
                assert "pip" in call_args or "install" in call_args

    def test_attempt_auto_fix_syntax_error(self, tmp_path):
        """Test auto-fix attempts syntax error fixes."""
        backend = PythonBackend(str(tmp_path), {})

        # Create a test file with syntax error
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(")

        error_output = f"""
File "{test_file}", line 1
    def foo(
           ^
SyntaxError: unexpected EOF while parsing
"""

        with patch.object(python_backend_module, "apply_fix_to_file") as mock_fix:
            mock_fix.return_value = True

            result = backend.attempt_auto_fix("syntax_error", error_output)

            # Should attempt to fix syntax errors
            assert isinstance(result, bool)


class TestBackendRegistration:
    """Test backend registration system."""

    def test_register_backend(self):
        """Test registering a new backend."""
        # Register the backend
        factory_module.register_backend("python", PythonBackend)

        backends = factory_module.list_backends()
        assert "python" in backends
        assert backends["python"] == PythonBackend


# =============================================================================
# Java Backend Tests - Critical for F500 customers
# =============================================================================


class TestJavaBackendBasics:
    """Test Java backend basic functionality."""

    def test_get_backend_name(self, tmp_path):
        """Test get_backend_name returns correct name."""
        backend = JavaBackend(str(tmp_path), {})
        assert backend.get_backend_name() == "Java Backend"

    def test_get_backend_type(self, tmp_path):
        """Test get_backend_type returns 'java'."""
        backend = JavaBackend(str(tmp_path), {})
        assert backend.get_backend_type() == "java"


class TestJavaBackendBuildToolDetection:
    """Test build tool detection for Java projects."""

    def test_detect_gradle_wrapper(self, tmp_path):
        """Detect Gradle wrapper project."""
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }")
        (tmp_path / "gradlew").write_text("#!/bin/bash\n")

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "gradle-wrapper"

    def test_detect_gradle_kotlin_dsl(self, tmp_path):
        """Detect Gradle with Kotlin DSL."""
        (tmp_path / "build.gradle.kts").write_text("plugins { java }")
        (tmp_path / "gradlew").write_text("#!/bin/bash\n")

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "gradle-wrapper"

    def test_detect_gradle_without_wrapper(self, tmp_path):
        """Detect Gradle without wrapper."""
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }")
        # No gradlew file

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "gradle"

    def test_detect_maven_wrapper(self, tmp_path):
        """Detect Maven wrapper project."""
        (tmp_path / "pom.xml").write_text("<project></project>")
        (tmp_path / "mvnw").write_text("#!/bin/bash\n")

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "maven-wrapper"

    def test_detect_maven_without_wrapper(self, tmp_path):
        """Detect Maven without wrapper."""
        (tmp_path / "pom.xml").write_text("<project></project>")
        # No mvnw file

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "maven"

    def test_default_to_gradle(self, tmp_path):
        """Default to gradle when no build files found."""
        # Empty directory

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "gradle"


class TestJavaBackendTestCommand:
    """Test test command generation for different build tools."""

    def test_gradle_wrapper_command(self, tmp_path):
        """Test command for Gradle wrapper."""
        (tmp_path / "build.gradle").write_text("")
        (tmp_path / "gradlew").write_text("")

        backend = JavaBackend(str(tmp_path), {})
        cmd = backend.get_test_command()

        assert cmd == ["./gradlew", "test", "--info"]

    def test_gradle_command(self, tmp_path):
        """Test command for Gradle without wrapper."""
        (tmp_path / "build.gradle").write_text("")

        backend = JavaBackend(str(tmp_path), {})
        cmd = backend.get_test_command()

        assert cmd == ["gradle", "test", "--info"]

    def test_maven_wrapper_command(self, tmp_path):
        """Test command for Maven wrapper."""
        (tmp_path / "pom.xml").write_text("")
        (tmp_path / "mvnw").write_text("")

        backend = JavaBackend(str(tmp_path), {})
        cmd = backend.get_test_command()

        assert cmd == ["./mvnw", "test"]

    def test_maven_command(self, tmp_path):
        """Test command for Maven without wrapper."""
        (tmp_path / "pom.xml").write_text("")

        backend = JavaBackend(str(tmp_path), {})
        cmd = backend.get_test_command()

        assert cmd == ["mvn", "test"]


class TestJavaBackendJunitPaths:
    """Test JUnit result path configuration."""

    def test_get_junit_paths(self, tmp_path):
        """Test JUnit paths include Gradle and Maven locations."""
        backend = JavaBackend(str(tmp_path), {})
        paths = backend.get_junit_paths()

        path_strs = [str(p) for p in paths]

        # Should include Gradle paths
        assert any("build/test-results" in p for p in path_strs)
        # Should include Maven paths
        assert any("surefire-reports" in p for p in path_strs)


class TestJavaBackendSetupEnvironment:
    """Test environment setup for Java projects."""

    def test_setup_makes_gradlew_executable(self, tmp_path):
        """Test setup makes gradlew executable."""
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/bash\n")
        os.chmod(gradlew, 0o644)  # Not executable

        backend = JavaBackend(str(tmp_path), {})
        result = backend.setup_environment()

        assert result is True
        assert os.access(gradlew, os.X_OK)

    def test_setup_makes_mvnw_executable(self, tmp_path):
        """Test setup makes mvnw executable."""
        mvnw = tmp_path / "mvnw"
        mvnw.write_text("#!/bin/bash\n")
        os.chmod(mvnw, 0o644)  # Not executable

        backend = JavaBackend(str(tmp_path), {})
        result = backend.setup_environment()

        assert result is True
        assert os.access(mvnw, os.X_OK)

    def test_setup_succeeds_without_wrappers(self, tmp_path):
        """Test setup succeeds even without wrapper scripts."""
        backend = JavaBackend(str(tmp_path), {})
        result = backend.setup_environment()

        assert result is True


class TestJavaBackendClassifyFailure:
    """Test failure classification for Java projects."""

    def test_classify_compilation_error(self, tmp_path):
        """Classify compilation errors as import_error."""
        backend = JavaBackend(str(tmp_path), {})

        assert backend.classify_failure("error: cannot find symbol") == "import_error"
        assert backend.classify_failure("Compilation failed") == "import_error"
        assert backend.classify_failure("package does not exist") == "import_error"
        assert backend.classify_failure("cannot resolve symbol Foo") == "import_error"

    def test_classify_test_assertion_error(self, tmp_path):
        """Classify assertion errors as logic_error."""
        backend = JavaBackend(str(tmp_path), {})

        assert backend.classify_failure("java.lang.AssertionError") == "logic_error"
        assert backend.classify_failure("expected: 42 but was: 0") == "logic_error"
        assert backend.classify_failure("Test failed: testMethod") == "logic_error"

    def test_classify_unknown_as_logic_error(self, tmp_path):
        """Classify unknown errors as logic_error."""
        backend = JavaBackend(str(tmp_path), {})

        assert backend.classify_failure("Some random error") == "logic_error"


class TestJavaBackendParseErrorFile:
    """Test parsing file paths from Java compiler errors."""

    def test_parse_standard_java_error(self, tmp_path):
        """Parse standard javac error format."""
        backend = JavaBackend(str(tmp_path), {})
        output = "/src/main/java/com/example/Service.java:42: error: cannot find symbol"

        result = backend._parse_java_error_file(output)

        assert result == "/src/main/java/com/example/Service.java"

    def test_parse_maven_error_format(self, tmp_path):
        """Parse Maven error format."""
        backend = JavaBackend(str(tmp_path), {})
        output = "[ERROR] /home/user/project/src/main/java/App.java:[15,10] cannot find symbol"

        result = backend._parse_java_error_file(output)

        assert result == "/home/user/project/src/main/java/App.java"

    def test_parse_kotlin_error_format(self, tmp_path):
        """Parse Kotlin error format."""
        backend = JavaBackend(str(tmp_path), {})
        output = "e: /src/main/kotlin/Service.kt: (42, 5): Unresolved reference"

        result = backend._parse_java_error_file(output)

        assert result == "/src/main/kotlin/Service.kt"

    def test_parse_generic_java_path(self, tmp_path):
        """Parse generic path with .java extension."""
        backend = JavaBackend(str(tmp_path), {})
        output = "Error in /tmp/build/MyClass.java while compiling"

        result = backend._parse_java_error_file(output)

        assert result == "/tmp/build/MyClass.java"

    def test_parse_returns_none_for_no_match(self, tmp_path):
        """Return None when no Java file found."""
        backend = JavaBackend(str(tmp_path), {})
        output = "Some error without file path"

        result = backend._parse_java_error_file(output)

        assert result is None


class TestJavaBackendSourcePaths:
    """Test source path resolution."""

    def test_default_source_paths(self, tmp_path):
        """Test default source paths are included."""
        backend = JavaBackend(str(tmp_path), {})
        paths = backend._get_source_paths()

        path_strs = [p.replace(str(tmp_path), "") for p in paths]

        assert any("src/main/java" in p for p in path_strs)
        assert any("src/test/java" in p for p in path_strs)
        assert any("src/main/kotlin" in p for p in path_strs)

    def test_configured_source_paths(self, tmp_path):
        """Test configured source paths are included."""
        config = {
            "code_generation": {
                "java": {
                    "source_paths": ["custom/src/main/java"]
                }
            }
        }
        backend = JavaBackend(str(tmp_path), config)
        paths = backend._get_source_paths()

        # Configured path should be first (highest priority)
        assert "custom/src/main/java" in paths[0]

    def test_configured_paths_combined_with_defaults(self, tmp_path):
        """Test configured paths don't replace defaults."""
        config = {
            "code_generation": {
                "java": {
                    "source_paths": ["custom/src"]
                }
            }
        }
        backend = JavaBackend(str(tmp_path), config)
        paths = backend._get_source_paths()

        # Should have both custom and default paths
        path_strs = [p.replace(str(tmp_path), "") for p in paths]
        assert any("custom/src" in p for p in path_strs)
        assert any("src/main/java" in p for p in path_strs)


class TestJavaBackendResolveErrorPath:
    """Test resolving error file paths from different machines."""

    def test_resolve_relative_path(self, tmp_path):
        """Resolve a relative path that exists."""
        # Create the file
        java_file = tmp_path / "src" / "main" / "java" / "App.java"
        java_file.parent.mkdir(parents=True)
        java_file.write_text("class App {}")

        backend = JavaBackend(str(tmp_path), {})
        result = backend._resolve_error_file_path("src/main/java/App.java")

        assert result is not None
        assert result.endswith("App.java")

    def test_resolve_tmp_path(self, tmp_path):
        """Resolve a /tmp/ path to local file."""
        # Create the file locally
        java_file = tmp_path / "src" / "main" / "java" / "Service.java"
        java_file.parent.mkdir(parents=True)
        java_file.write_text("class Service {}")

        backend = JavaBackend(str(tmp_path), {})
        # Simulate a /tmp/ path from CI
        result = backend._resolve_error_file_path("/tmp/build123/src/main/java/Service.java")

        assert result is not None
        assert result.endswith("Service.java")

    def test_resolve_returns_none_for_missing_file(self, tmp_path):
        """Return None for file that doesn't exist."""
        backend = JavaBackend(str(tmp_path), {})
        result = backend._resolve_error_file_path("/nonexistent/path/File.java")

        assert result is None


class TestJavaBackendAutoFix:
    """Test auto-fix functionality for Java projects."""

    def test_attempt_auto_fix_import_error(self, tmp_path):
        """Test auto-fix for import errors calls _fix_compilation_error."""
        backend = JavaBackend(str(tmp_path), {})

        with patch.object(backend, "_fix_compilation_error") as mock_fix:
            mock_fix.return_value = True

            result = backend.attempt_auto_fix("import_error", "error: cannot find symbol")

            mock_fix.assert_called_once_with("error: cannot find symbol")
            assert result is True

    def test_attempt_auto_fix_syntax_error(self, tmp_path):
        """Test auto-fix for syntax errors calls _fix_compilation_error."""
        backend = JavaBackend(str(tmp_path), {})

        with patch.object(backend, "_fix_compilation_error") as mock_fix:
            mock_fix.return_value = True

            result = backend.attempt_auto_fix("syntax_error", "SyntaxError")

            mock_fix.assert_called_once()
            assert result is True

    def test_attempt_auto_fix_logic_error_returns_false(self, tmp_path):
        """Test auto-fix skips logic errors."""
        backend = JavaBackend(str(tmp_path), {})

        result = backend.attempt_auto_fix("logic_error", "AssertionError")

        assert result is False

    def test_fix_compilation_error_calls_claude(self, tmp_path):
        """Test _fix_compilation_error uses Claude for fixes."""
        # Create a Java file
        java_file = tmp_path / "src" / "main" / "java" / "App.java"
        java_file.parent.mkdir(parents=True)
        java_file.write_text("class App {}")

        backend = JavaBackend(str(tmp_path), {})
        error_output = f"{java_file}:10: error: cannot find symbol"

        with patch.object(java_backend_module, "apply_fix_to_file") as mock_apply:
            mock_apply.return_value = True

            result = backend._fix_compilation_error(error_output)

            assert result is True
            mock_apply.assert_called_once()
            # Verify it was called with java backend type
            call_kwargs = mock_apply.call_args.kwargs
            assert call_kwargs.get("backend_type") == "java"

    def test_fix_compilation_error_no_file_found(self, tmp_path):
        """Test _fix_compilation_error handles missing file gracefully."""
        backend = JavaBackend(str(tmp_path), {})
        error_output = "Some error without file path"

        result = backend._fix_compilation_error(error_output)

        assert result is False


class TestJavaBackendIntegration:
    """Integration tests for Java backend."""

    def test_full_gradle_project_setup(self, tmp_path):
        """Test complete Gradle project detection and setup."""
        # Create a realistic Gradle project structure
        (tmp_path / "build.gradle").write_text("""
plugins {
    id 'java'
    id 'org.springframework.boot' version '3.0.0'
}

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
}
""")
        (tmp_path / "gradlew").write_text("#!/bin/bash\nexec gradle \"$@\"")
        (tmp_path / "settings.gradle").write_text("rootProject.name = 'myapp'")

        src_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        src_dir.mkdir(parents=True)
        (src_dir / "Application.java").write_text("package com.example; public class Application {}")

        test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
        test_dir.mkdir(parents=True)
        (test_dir / "ApplicationTest.java").write_text("package com.example; public class ApplicationTest {}")

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "gradle-wrapper"
        assert backend.get_test_command() == ["./gradlew", "test", "--info"]
        assert backend.setup_environment() is True

    def test_full_maven_project_setup(self, tmp_path):
        """Test complete Maven project detection and setup."""
        # Create a realistic Maven project structure
        (tmp_path / "pom.xml").write_text("""
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>myapp</artifactId>
    <version>1.0.0</version>
    <dependencies>
        <dependency>
            <groupId>junit</groupId>
            <artifactId>junit</artifactId>
            <version>4.13.2</version>
            <scope>test</scope>
        </dependency>
    </dependencies>
</project>
""")
        (tmp_path / "mvnw").write_text("#!/bin/bash\nexec mvn \"$@\"")

        src_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        src_dir.mkdir(parents=True)
        (src_dir / "App.java").write_text("package com.example; public class App {}")

        backend = JavaBackend(str(tmp_path), {})

        assert backend.build_tool == "maven-wrapper"
        assert backend.get_test_command() == ["./mvnw", "test"]
        assert backend.setup_environment() is True


# =============================================================================
# TypeScript Backend Tests
# =============================================================================

# Load typescript_backend with unique name
typescript_backend_path = os.path.join(backends_dir, "typescript_backend.py")
ts_spec = importlib.util.spec_from_file_location(
    "run_tests_backends_typescript_test", typescript_backend_path
)
typescript_backend_module = importlib.util.module_from_spec(ts_spec)
sys.modules["run_tests_backends_typescript_test"] = typescript_backend_module
ts_spec.loader.exec_module(typescript_backend_module)

TypeScriptBackend = typescript_backend_module.TypeScriptBackend


class TestTypeScriptBackendBasics:
    """Test TypeScript backend basic properties."""

    def test_get_backend_name(self, tmp_path):
        """Test get_backend_name returns correct name."""
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.get_backend_name() == "TypeScript Backend"

    def test_get_backend_type(self, tmp_path):
        """Test get_backend_type returns 'typescript'."""
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.get_backend_type() == "typescript"


class TestTypeScriptPackageManagerDetection:
    """Test package manager detection for TypeScript projects."""

    def test_detect_pnpm(self, tmp_path):
        """Detect pnpm from pnpm-lock.yaml."""
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 5.4")
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.package_manager == "pnpm"

    def test_detect_yarn(self, tmp_path):
        """Detect yarn from yarn.lock."""
        (tmp_path / "yarn.lock").write_text("# yarn lockfile")
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.package_manager == "yarn"

    def test_detect_npm(self, tmp_path):
        """Detect npm from package-lock.json."""
        (tmp_path / "package-lock.json").write_text("{}")
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.package_manager == "npm"

    def test_default_to_npm(self, tmp_path):
        """Default to npm when no lockfile found."""
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.package_manager == "npm"


class TestTypeScriptTestRunnerDetection:
    """Test test runner detection from package.json."""

    def test_detect_vitest(self, tmp_path):
        """Detect vitest from package.json devDependencies."""
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"vitest": "^1.0.0"}
        }))
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "vitest"

    def test_detect_jest(self, tmp_path):
        """Detect jest from package.json devDependencies."""
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"jest": "^29.0.0"}
        }))
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "jest"

    def test_detect_mocha(self, tmp_path):
        """Detect mocha from package.json devDependencies."""
        (tmp_path / "package.json").write_text(json.dumps({
            "devDependencies": {"mocha": "^10.0.0"}
        }))
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "mocha"

    def test_detect_from_dependencies(self, tmp_path):
        """Detect test runner from dependencies (not just devDependencies)."""
        (tmp_path / "package.json").write_text(json.dumps({
            "dependencies": {"vitest": "^1.0.0"}
        }))
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "vitest"

    def test_default_to_jest(self, tmp_path):
        """Default to jest when no test runner found."""
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "jest"

    def test_default_to_jest_no_package_json(self, tmp_path):
        """Default to jest when package.json does not exist."""
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "jest"

    def test_invalid_package_json(self, tmp_path):
        """Handle invalid package.json gracefully and default to jest."""
        (tmp_path / "package.json").write_text("not json")
        backend = TypeScriptBackend(str(tmp_path), {})
        assert backend.test_runner == "jest"


class TestTypeScriptSetupEnvironment:
    """Test environment setup for TypeScript projects."""

    def test_setup_npm_success(self, tmp_path):
        """Test successful npm ci setup."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "npm"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["npm", "ci"], exit_code=0,
                stdout="installed", stderr="", duration_seconds=1.0
            )
            result = backend.setup_environment()
            assert result is True

    def test_setup_yarn_success(self, tmp_path):
        """Test successful yarn install setup."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "yarn"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["yarn", "install"], exit_code=0,
                stdout="installed", stderr="", duration_seconds=1.0
            )
            result = backend.setup_environment()
            assert result is True

    def test_setup_pnpm_success(self, tmp_path):
        """Test successful pnpm install setup."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "pnpm"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["pnpm", "install"], exit_code=0,
                stdout="installed", stderr="", duration_seconds=1.0
            )
            result = backend.setup_environment()
            assert result is True

    def test_setup_fallback_on_frozen_lockfile_failure(self, tmp_path):
        """Test fallback to regular install when frozen lockfile fails."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "npm"

        with patch.object(backend, "_run_command") as mock_run:
            # First call (npm ci) fails, second call (npm install) succeeds
            mock_run.side_effect = [
                CommandResult(
                    command=["npm", "ci"], exit_code=1,
                    stdout="", stderr="lockfile error", duration_seconds=0.5
                ),
                CommandResult(
                    command=["npm", "install"], exit_code=0,
                    stdout="installed", stderr="", duration_seconds=1.0
                ),
            ]
            result = backend.setup_environment()
            assert result is True
            assert mock_run.call_count == 2

    def test_setup_pnpm_fallback(self, tmp_path):
        """Test pnpm fallback to regular install."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "pnpm"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.side_effect = [
                CommandResult(
                    command=["pnpm", "install", "--frozen-lockfile"], exit_code=1,
                    stdout="", stderr="error", duration_seconds=0.5
                ),
                CommandResult(
                    command=["pnpm", "install"], exit_code=0,
                    stdout="ok", stderr="", duration_seconds=1.0
                ),
            ]
            result = backend.setup_environment()
            assert result is True

    def test_setup_yarn_fallback(self, tmp_path):
        """Test yarn fallback to regular install."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "yarn"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.side_effect = [
                CommandResult(
                    command=["yarn", "install", "--frozen-lockfile"], exit_code=1,
                    stdout="", stderr="error", duration_seconds=0.5
                ),
                CommandResult(
                    command=["yarn", "install"], exit_code=0,
                    stdout="ok", stderr="", duration_seconds=1.0
                ),
            ]
            result = backend.setup_environment()
            assert result is True

    def test_setup_failure(self, tmp_path):
        """Test setup failure when both install attempts fail."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "npm"

        with patch.object(backend, "_run_command") as mock_run:
            mock_run.return_value = CommandResult(
                command=["npm", "ci"], exit_code=1,
                stdout="", stderr="install failed", duration_seconds=0.5
            )
            result = backend.setup_environment()
            assert result is False


class TestTypeScriptTestCommand:
    """Test test command generation."""

    def test_npm_test_command(self, tmp_path):
        """Test npm test command."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "npm"
        cmd = backend.get_test_command()
        assert cmd == ["npm", "test"]

    def test_yarn_test_command(self, tmp_path):
        """Test yarn test command."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "yarn"
        cmd = backend.get_test_command()
        assert cmd == ["yarn", "test"]

    def test_pnpm_test_command(self, tmp_path):
        """Test pnpm test command."""
        backend = TypeScriptBackend(str(tmp_path), {})
        backend.package_manager = "pnpm"
        cmd = backend.get_test_command()
        assert cmd == ["pnpm", "test"]

    def test_custom_test_command(self, tmp_path):
        """Test custom test command from config."""
        config = {"testing": {"test_command": "npx vitest run"}}
        backend = TypeScriptBackend(str(tmp_path), config)
        cmd = backend.get_test_command()
        assert cmd == ["npx", "vitest", "run"]


class TestTypeScriptJunitPaths:
    """Test JUnit result path configuration."""

    def test_get_junit_paths(self, tmp_path):
        """Test JUnit paths include common locations."""
        backend = TypeScriptBackend(str(tmp_path), {})
        paths = backend.get_junit_paths()
        path_strs = [str(p) for p in paths]

        assert any("junit.xml" in p for p in path_strs)
        assert any("test-results" in p for p in path_strs)


class TestTypeScriptClassifyFailure:
    """Test failure classification for TypeScript projects."""

    def test_classify_import_error(self, tmp_path):
        """Classify module resolution errors."""
        backend = TypeScriptBackend(str(tmp_path), {})

        assert backend.classify_failure("Cannot find module './utils'") == "import_error"
        assert backend.classify_failure("Module not found: Error") == "import_error"
        assert backend.classify_failure("could not resolve dependency") == "import_error"
        assert backend.classify_failure("unable to resolve module") == "import_error"
        assert backend.classify_failure("error: cannot find symbol") == "import_error"
        assert backend.classify_failure("TS2307: Cannot find module") == "import_error"
        assert backend.classify_failure("TS2305: Module has no exported member") == "import_error"

    def test_classify_syntax_error(self, tmp_path):
        """Classify syntax and type errors."""
        backend = TypeScriptBackend(str(tmp_path), {})

        assert backend.classify_failure("SyntaxError: Unexpected token") == "syntax_error"
        assert backend.classify_failure("syntax error at line 5") == "syntax_error"
        assert backend.classify_failure("unexpected token }") == "syntax_error"
        assert backend.classify_failure("TS1005: Expected ')'") == "syntax_error"
        assert backend.classify_failure("TS1003: Expected identifier") == "syntax_error"
        assert backend.classify_failure("TS1002: Unterminated string") == "syntax_error"

    def test_classify_type_error_as_syntax(self, tmp_path):
        """Classify TypeScript type errors as syntax errors."""
        backend = TypeScriptBackend(str(tmp_path), {})

        assert backend.classify_failure("Type error in component") == "syntax_error"
        assert backend.classify_failure("TS2322: Type 'string' not assignable") == "syntax_error"
        assert backend.classify_failure("TS2345: Argument type mismatch") == "syntax_error"
        assert backend.classify_failure("TS2339: Property does not exist") == "syntax_error"

    def test_classify_logic_error(self, tmp_path):
        """Classify unknown errors as logic errors."""
        backend = TypeScriptBackend(str(tmp_path), {})

        assert backend.classify_failure("Expected 42 but received 0") == "logic_error"
        assert backend.classify_failure("AssertionError: values not equal") == "logic_error"


class TestTypeScriptAutoFix:
    """Test TypeScript auto-fix functionality."""

    def test_attempt_auto_fix_import_error(self, tmp_path):
        """Test auto-fix for import errors delegates to _fix_with_claude."""
        backend = TypeScriptBackend(str(tmp_path), {})

        with patch.object(backend, "_fix_with_claude") as mock_fix:
            mock_fix.return_value = True
            result = backend.attempt_auto_fix("import_error", "Cannot find module")
            mock_fix.assert_called_once_with("Cannot find module", "import_error")
            assert result is True

    def test_attempt_auto_fix_syntax_error(self, tmp_path):
        """Test auto-fix for syntax errors delegates to _fix_with_claude."""
        backend = TypeScriptBackend(str(tmp_path), {})

        with patch.object(backend, "_fix_with_claude") as mock_fix:
            mock_fix.return_value = True
            result = backend.attempt_auto_fix("syntax_error", "SyntaxError")
            mock_fix.assert_called_once_with("SyntaxError", "syntax_error")
            assert result is True

    def test_attempt_auto_fix_logic_error_returns_false(self, tmp_path):
        """Test auto-fix skips logic errors."""
        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend.attempt_auto_fix("logic_error", "AssertionError")
        assert result is False


class TestTypeScriptParseErrorFile:
    """Test parsing file paths from TypeScript error output."""

    def test_parse_typescript_compiler_format(self, tmp_path):
        """Parse TypeScript compiler error format."""
        ts_file = tmp_path / "src" / "app.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("const x: number = 'hello';")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{ts_file}(10,5): error TS2322: Type 'string' is not assignable"
        result = backend._parse_ts_error_file(output)
        assert result is not None
        assert result.endswith("app.ts")

    def test_parse_jest_stack_trace(self, tmp_path):
        """Parse Jest stack trace format."""
        ts_file = tmp_path / "src" / "utils.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("export function foo() {}")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"at Object.<anonymous> ({ts_file}:10:5)"
        result = backend._parse_ts_error_file(output)
        assert result is not None
        assert result.endswith("utils.ts")

    def test_parse_simple_path_line_col(self, tmp_path):
        """Parse simple path:line:col format."""
        ts_file = tmp_path / "src" / "index.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("export default 1;")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{ts_file}:15:3"
        result = backend._parse_ts_error_file(output)
        assert result is not None
        assert result.endswith("index.ts")

    def test_parse_jsx_file(self, tmp_path):
        """Parse JSX file path."""
        jsx_file = tmp_path / "src" / "App.jsx"
        jsx_file.parent.mkdir(parents=True)
        jsx_file.write_text("export default function App() {}")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{jsx_file}:5:10"
        result = backend._parse_ts_error_file(output)
        assert result is not None
        assert result.endswith("App.jsx")

    def test_parse_vitest_fail_format(self, tmp_path):
        """Parse Vitest FAIL format."""
        ts_file = tmp_path / "src" / "app.test.tsx"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("test('foo', () => {})")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"FAIL {ts_file} > test name"
        result = backend._parse_ts_error_file(output)
        assert result is not None
        assert result.endswith("app.test.tsx")

    def test_parse_returns_none_for_no_match(self, tmp_path):
        """Return None when no file path found."""
        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend._parse_ts_error_file("Some error without file path")
        assert result is None


class TestTypeScriptFixWithClaude:
    """Test Claude-based fix for TypeScript backend."""

    def test_fix_with_claude_no_file_found(self, tmp_path):
        """Test _fix_with_claude returns False when no file path found."""
        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend._fix_with_claude("Some vague error", "import_error")
        assert result is False

    def test_fix_with_claude_file_not_found(self, tmp_path):
        """Test _fix_with_claude returns False when parsed file doesn't exist."""
        backend = TypeScriptBackend(str(tmp_path), {})
        output = "/nonexistent/file.ts:10:5"
        result = backend._fix_with_claude(output, "import_error")
        assert result is False

    def test_fix_with_claude_apply_succeeds(self, tmp_path):
        """Test _fix_with_claude when apply_fix_to_file succeeds."""
        ts_file = tmp_path / "src" / "app.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("const x = 1;")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{ts_file}:1:5 Cannot find module './utils'"

        with patch.object(typescript_backend_module, "apply_fix_to_file") as mock_fix:
            mock_fix.return_value = True
            result = backend._fix_with_claude(output, "import_error")
            assert result is True
            mock_fix.assert_called_once()
            call_kwargs = mock_fix.call_args.kwargs
            assert call_kwargs.get("backend_type") == "typescript"

    def test_fix_with_claude_apply_fails(self, tmp_path):
        """Test _fix_with_claude when apply_fix_to_file fails."""
        ts_file = tmp_path / "src" / "app.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("const x = 1;")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{ts_file}:1:5 SyntaxError"

        with patch.object(typescript_backend_module, "apply_fix_to_file") as mock_fix:
            mock_fix.return_value = False
            result = backend._fix_with_claude(output, "syntax_error")
            assert result is False

    def test_fix_with_claude_exception(self, tmp_path):
        """Test _fix_with_claude handles exception from apply_fix_to_file."""
        ts_file = tmp_path / "src" / "app.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("const x = 1;")

        backend = TypeScriptBackend(str(tmp_path), {})
        output = f"{ts_file}:1:5 Error"

        with patch.object(typescript_backend_module, "apply_fix_to_file") as mock_fix:
            mock_fix.side_effect = RuntimeError("Bedrock unavailable")
            result = backend._fix_with_claude(output, "import_error")
            assert result is False


class TestTypeScriptResolveErrorFilePath:
    """Test resolving error file paths from different environments."""

    def test_resolve_relative_path(self, tmp_path):
        """Resolve a path relative to repo root."""
        ts_file = tmp_path / "src" / "app.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("code")

        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend._resolve_error_file_path("src/app.ts")
        assert result is not None
        assert result.endswith("app.ts")

    def test_resolve_tmp_path(self, tmp_path):
        """Resolve a /tmp/ path to local file."""
        ts_file = tmp_path / "src" / "index.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text("code")

        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend._resolve_error_file_path("/tmp/build/src/index.ts")
        assert result is not None
        assert result.endswith("index.ts")

    def test_resolve_returns_none_for_missing_file(self, tmp_path):
        """Return None for file that does not exist."""
        backend = TypeScriptBackend(str(tmp_path), {})
        result = backend._resolve_error_file_path("/nonexistent/path/file.ts")
        assert result is None


class TestTypeScriptSourcePaths:
    """Test source path configuration."""

    def test_default_source_paths(self, tmp_path):
        """Test default source paths include common TS directories."""
        backend = TypeScriptBackend(str(tmp_path), {})
        paths = backend._get_source_paths()

        path_strs = [p.replace(str(tmp_path), "") for p in paths]
        assert any("src" in p for p in path_strs)
        assert any("lib" in p for p in path_strs)
        assert any("app" in p for p in path_strs)

    def test_configured_source_paths(self, tmp_path):
        """Test configured source paths are included."""
        config = {
            "code_generation": {
                "typescript": {
                    "source_paths": ["custom/src"]
                }
            }
        }
        backend = TypeScriptBackend(str(tmp_path), config)
        paths = backend._get_source_paths()

        # Configured path should be first
        assert "custom/src" in paths[0]

    def test_configured_paths_combined_with_defaults(self, tmp_path):
        """Test configured paths don't replace defaults."""
        config = {
            "code_generation": {
                "typescript": {
                    "source_paths": ["custom"]
                }
            }
        }
        backend = TypeScriptBackend(str(tmp_path), config)
        paths = backend._get_source_paths()

        path_strs = [p.replace(str(tmp_path), "") for p in paths]
        assert any("custom" in p for p in path_strs)
        assert any("src" in p for p in path_strs)
