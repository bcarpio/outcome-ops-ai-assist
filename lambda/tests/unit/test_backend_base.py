"""Unit tests for backend base classes."""

import pytest
import sys
import os
import importlib.util

# Load the backend base module
base_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/backends/base.py')
spec = importlib.util.spec_from_file_location("backend_base", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules['backend_base'] = base_module
spec.loader.exec_module(base_module)

# Import classes from the loaded module
CodeUnit = base_module.CodeUnit
ChangeDetectionResult = base_module.ChangeDetectionResult
CodeMapBackend = base_module.CodeMapBackend


class TestCodeUnit:
    """Test CodeUnit data class."""

    def test_code_unit_initialization(self):
        """Test CodeUnit initialization with required parameters."""
        # Arrange & Act
        code_unit = CodeUnit(
            name="ingest-docs",
            unit_type="handler-group",
            file_paths=["lambda/ingest-docs/handler.py", "lambda/ingest-docs/utils.py"]
        )

        # Assert
        assert code_unit.name == "ingest-docs"
        assert code_unit.unit_type == "handler-group"
        assert len(code_unit.file_paths) == 2
        assert code_unit.metadata == {}

    def test_code_unit_with_metadata(self):
        """Test CodeUnit initialization with metadata."""
        # Arrange
        metadata = {
            "batch_type": "handler-group",
            "runtime": "python3.12",
            "dependencies": ["boto3", "requests"]
        }

        # Act
        code_unit = CodeUnit(
            name="generate-code-maps",
            unit_type="handler-group",
            file_paths=["lambda/generate-code-maps/handler.py"],
            metadata=metadata
        )

        # Assert
        assert code_unit.metadata == metadata
        assert code_unit.metadata["runtime"] == "python3.12"
        assert len(code_unit.metadata["dependencies"]) == 2

    def test_code_unit_repr(self):
        """Test CodeUnit string representation."""
        # Arrange
        code_unit = CodeUnit(
            name="test-handler",
            unit_type="lambda-handler",
            file_paths=["file1.py", "file2.py", "file3.py"]
        )

        # Act
        result = repr(code_unit)

        # Assert
        assert "CodeUnit" in result
        assert "test-handler" in result
        assert "lambda-handler" in result
        assert "files=3" in result


class TestChangeDetectionResult:
    """Test ChangeDetectionResult data class."""

    def test_change_detection_no_changes(self):
        """Test ChangeDetectionResult with no changes."""
        # Arrange & Act
        result = ChangeDetectionResult(
            has_changes=False,
            changed_units=[],
            unchanged_units=[],
            last_commit_sha="abc123def456",
            current_commit_sha="abc123def456"
        )

        # Assert
        assert result.has_changes is False
        assert len(result.changed_units) == 0
        assert len(result.unchanged_units) == 0
        assert result.last_commit_sha == result.current_commit_sha

    def test_change_detection_with_changes(self):
        """Test ChangeDetectionResult with changes detected."""
        # Arrange
        changed_unit = CodeUnit(
            name="auth-handler",
            unit_type="handler-group",
            file_paths=["lambda/auth/handler.py"]
        )
        unchanged_unit = CodeUnit(
            name="utils",
            unit_type="shared",
            file_paths=["src/utils/helpers.py"]
        )

        # Act
        result = ChangeDetectionResult(
            has_changes=True,
            changed_units=[changed_unit],
            unchanged_units=[unchanged_unit],
            last_commit_sha="abc123",
            current_commit_sha="def456"
        )

        # Assert
        assert result.has_changes is True
        assert len(result.changed_units) == 1
        assert len(result.unchanged_units) == 1
        assert result.changed_units[0].name == "auth-handler"
        assert result.unchanged_units[0].name == "utils"

    def test_change_detection_repr(self):
        """Test ChangeDetectionResult string representation."""
        # Arrange
        result = ChangeDetectionResult(
            has_changes=True,
            changed_units=[
                CodeUnit("unit1", "type1", []),
                CodeUnit("unit2", "type2", [])
            ],
            unchanged_units=[
                CodeUnit("unit3", "type3", [])
            ],
            last_commit_sha="abc",
            current_commit_sha="def"
        )

        # Act
        repr_str = repr(result)

        # Assert
        assert "ChangeDetectionResult" in repr_str
        assert "has_changes=True" in repr_str
        assert "changed=2" in repr_str
        assert "unchanged=1" in repr_str


class TestCodeMapBackend:
    """Test CodeMapBackend abstract base class."""

    def test_backend_cannot_be_instantiated(self):
        """Test that abstract base class cannot be instantiated directly."""
        # Act & Assert
        with pytest.raises(TypeError) as exc_info:
            CodeMapBackend({})

        assert "Can't instantiate abstract class" in str(exc_info.value)

    def test_backend_subclass_must_implement_abstract_methods(self):
        """Test that subclasses must implement all abstract methods."""
        # Arrange
        class IncompleteBackend(CodeMapBackend):
            pass

        # Act & Assert
        with pytest.raises(TypeError) as exc_info:
            IncompleteBackend({})

        error_msg = str(exc_info.value)
        assert "Can't instantiate abstract class" in error_msg
        assert "discover_code_units" in error_msg or "abstract methods" in error_msg

    def test_backend_subclass_with_all_methods(self):
        """Test that complete backend subclass can be instantiated."""
        # Arrange
        class CompleteBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "test-key"

        # Act
        backend = CompleteBackend({"test": "config"})

        # Assert
        assert backend.config == {"test": "config"}
        assert backend.get_backend_name() == "CompleteBackend"
        assert backend.get_backend_type() == "unknown"

    def test_backend_validate_config_default(self):
        """Test default validate_config returns True."""
        # Arrange
        class TestBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "test-key"

        # Act
        backend = TestBackend({})
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is True
        assert error is None

    def test_backend_custom_validate_config(self):
        """Test custom validate_config implementation."""
        # Arrange
        class ValidatedBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "test-key"

            def validate_config(self):
                if "required_field" not in self.config:
                    return False, "required_field is missing"
                return True, None

        # Act
        backend_invalid = ValidatedBackend({})
        is_valid_invalid, error_invalid = backend_invalid.validate_config()

        backend_valid = ValidatedBackend({"required_field": "value"})
        is_valid_valid, error_valid = backend_valid.validate_config()

        # Assert
        assert is_valid_invalid is False
        assert "required_field is missing" in error_invalid
        assert is_valid_valid is True
        assert error_valid is None

    def test_backend_get_backend_name(self):
        """Test get_backend_name returns class name."""
        # Arrange
        class MyCustomBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "test-key"

        # Act
        backend = MyCustomBackend({})
        name = backend.get_backend_name()

        # Assert
        assert name == "MyCustomBackend"

    def test_backend_get_backend_type_override(self):
        """Test get_backend_type can be overridden."""
        # Arrange
        class TypedBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "test-key"

            def get_backend_type(self):
                return "custom-type"

        # Act
        backend = TypedBackend({})
        backend_type = backend.get_backend_type()

        # Assert
        assert backend_type == "custom-type"
