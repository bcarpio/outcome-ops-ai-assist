"""Unit tests for backend factory."""

import pytest
import sys
import os
import importlib.util

# Load the backend modules
base_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/backends/base.py')
spec = importlib.util.spec_from_file_location("backend_base", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules['backend_base'] = base_module
sys.modules['base'] = base_module  # Also register as 'base' for factory.py imports
spec.loader.exec_module(base_module)

factory_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/backends/factory.py')
spec = importlib.util.spec_from_file_location("backend_factory", factory_path)
factory_module = importlib.util.module_from_spec(spec)
sys.modules['backend_factory'] = factory_module
sys.modules['factory'] = factory_module  # Also register as 'factory' for imports
spec.loader.exec_module(factory_module)

# Import from modules
CodeMapBackend = base_module.CodeMapBackend
ChangeDetectionResult = base_module.ChangeDetectionResult
register_backend = factory_module.register_backend
get_backend = factory_module.get_backend
list_backends = factory_module.list_backends
is_backend_registered = factory_module.is_backend_registered


class TestBackend(CodeMapBackend):
    """Test backend implementation for testing factory."""

    def discover_code_units(self, files):
        return []

    def detect_changes(self, repo, repo_project, last_state, force_full=False):
        return ChangeDetectionResult(False, [], [], None, None)

    def generate_batch_metadata(self, code_unit, repo):
        return {"batch_type": "test"}

    def get_storage_key(self, code_unit):
        return "test#key"

    def get_backend_type(self):
        return "test"


class TestRegisterBackend:
    """Test backend registration."""

    def setup_method(self):
        """Clear registry before each test."""
        factory_module._BACKEND_REGISTRY.clear()

    def test_register_backend_success(self):
        """Test successful backend registration."""
        # Act
        register_backend("test", TestBackend)

        # Assert
        assert is_backend_registered("test")
        assert factory_module._BACKEND_REGISTRY["test"] == TestBackend

    def test_register_backend_invalid_class(self):
        """Test registering non-backend class raises ValueError."""
        # Arrange
        class NotABackend:
            pass

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            register_backend("invalid", NotABackend)

        assert "must inherit from CodeMapBackend" in str(exc_info.value)

    def test_register_backend_replaces_existing(self):
        """Test registering same name replaces existing backend."""
        # Arrange
        class Backend1(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        class Backend2(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        register_backend("test", Backend1)

        # Act
        register_backend("test", Backend2)

        # Assert
        assert factory_module._BACKEND_REGISTRY["test"] == Backend2

    def test_register_multiple_backends(self):
        """Test registering multiple different backends."""
        # Arrange
        class LambdaBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        class K8sBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        # Act
        register_backend("lambda", LambdaBackend)
        register_backend("k8s", K8sBackend)

        # Assert
        assert is_backend_registered("lambda")
        assert is_backend_registered("k8s")
        assert len(factory_module._BACKEND_REGISTRY) == 2


class TestGetBackend:
    """Test backend retrieval."""

    def setup_method(self):
        """Clear registry and register test backend before each test."""
        factory_module._BACKEND_REGISTRY.clear()
        register_backend("test", TestBackend)

    def test_get_backend_success(self):
        """Test successful backend retrieval."""
        # Arrange
        config = {"test_config": "value"}

        # Act
        backend = get_backend("test", config)

        # Assert
        assert isinstance(backend, TestBackend)
        assert backend.config == config

    def test_get_backend_not_found(self):
        """Test getting non-existent backend raises ValueError."""
        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            get_backend("nonexistent", {})

        assert "Backend 'nonexistent' not found" in str(exc_info.value)
        assert "Available backends: test" in str(exc_info.value)

    def test_get_backend_invalid_config(self):
        """Test getting backend with invalid config raises ValueError."""
        # Arrange
        class ValidatedBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

            def validate_config(self):
                if "required_field" not in self.config:
                    return False, "required_field is required"
                return True, None

        register_backend("validated", ValidatedBackend)

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            get_backend("validated", {})

        assert "Invalid backend configuration" in str(exc_info.value)
        assert "required_field is required" in str(exc_info.value)

    def test_get_backend_with_valid_config(self):
        """Test getting backend with valid config succeeds."""
        # Arrange
        class ValidatedBackend(CodeMapBackend):
            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

            def validate_config(self):
                if "required_field" not in self.config:
                    return False, "required_field is required"
                return True, None

        register_backend("validated", ValidatedBackend)
        config = {"required_field": "value"}

        # Act
        backend = get_backend("validated", config)

        # Assert
        assert isinstance(backend, ValidatedBackend)
        assert backend.config["required_field"] == "value"


class TestListBackends:
    """Test backend listing."""

    def setup_method(self):
        """Clear registry before each test."""
        factory_module._BACKEND_REGISTRY.clear()

    def test_list_backends_empty(self):
        """Test listing backends when registry is empty."""
        # Act
        backends = list_backends()

        # Assert
        assert backends == []

    def test_list_backends_single(self):
        """Test listing single backend."""
        # Arrange
        register_backend("test", TestBackend)

        # Act
        backends = list_backends()

        # Assert
        assert len(backends) == 1
        assert backends[0]["name"] == "test"
        assert backends[0]["class"] == "TestBackend"
        assert "description" in backends[0]

    def test_list_backends_multiple(self):
        """Test listing multiple backends."""
        # Arrange
        class Backend1(CodeMapBackend):
            """Backend 1 description."""

            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        class Backend2(CodeMapBackend):
            """Backend 2 description."""

            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        register_backend("backend1", Backend1)
        register_backend("backend2", Backend2)

        # Act
        backends = list_backends()

        # Assert
        assert len(backends) == 2
        names = [b["name"] for b in backends]
        assert "backend1" in names
        assert "backend2" in names

    def test_list_backends_includes_description(self):
        """Test that listed backends include docstring description."""
        # Arrange
        class DocumentedBackend(CodeMapBackend):
            """This is a documented backend for testing."""

            def discover_code_units(self, files):
                return []

            def detect_changes(self, repo, repo_project, last_state, force_full=False):
                return ChangeDetectionResult(False, [], [], None, None)

            def generate_batch_metadata(self, code_unit, repo):
                return {}

            def get_storage_key(self, code_unit):
                return "key"

        register_backend("documented", DocumentedBackend)

        # Act
        backends = list_backends()

        # Assert
        assert len(backends) == 1
        assert backends[0]["description"] == "This is a documented backend for testing."


class TestIsBackendRegistered:
    """Test backend registration checking."""

    def setup_method(self):
        """Clear registry before each test."""
        factory_module._BACKEND_REGISTRY.clear()

    def test_is_backend_registered_true(self):
        """Test checking registered backend returns True."""
        # Arrange
        register_backend("test", TestBackend)

        # Act
        result = is_backend_registered("test")

        # Assert
        assert result is True

    def test_is_backend_registered_false(self):
        """Test checking non-registered backend returns False."""
        # Act
        result = is_backend_registered("nonexistent")

        # Assert
        assert result is False

    def test_is_backend_registered_after_registration(self):
        """Test checking backend before and after registration."""
        # Arrange
        name = "dynamic-backend"

        # Act & Assert - Before registration
        assert is_backend_registered(name) is False

        # Register
        register_backend(name, TestBackend)

        # Act & Assert - After registration
        assert is_backend_registered(name) is True
