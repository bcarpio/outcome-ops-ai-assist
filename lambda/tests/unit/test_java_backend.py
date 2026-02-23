"""Unit tests for Java backend."""

import pytest
import sys
import os
import json
from unittest.mock import patch, MagicMock
import importlib.util

# Load required modules
base_path = os.path.join(os.path.dirname(__file__), '../../process-repo-summary/backends/base.py')
spec = importlib.util.spec_from_file_location("backend_base", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules['backend_base'] = base_module
sys.modules['backends.base'] = base_module
spec.loader.exec_module(base_module)

java_backend_path = os.path.join(os.path.dirname(__file__), '../../process-repo-summary/backends/java_backend.py')
spec = importlib.util.spec_from_file_location("java_backend", java_backend_path)
java_backend_module = importlib.util.module_from_spec(spec)
sys.modules['java_backend'] = java_backend_module
spec.loader.exec_module(java_backend_module)

# Import classes
CodeUnit = base_module.CodeUnit
JavaBackend = java_backend_module.JavaBackend


class TestJavaBackendInitialization:
    """Test Java backend initialization."""

    def test_initialization_default_config(self):
        """Test initialization with default configuration."""
        # Arrange
        config = {"github_token": "test-token"}

        # Act
        backend = JavaBackend(config)

        # Assert
        assert backend.include_kotlin is True
        assert backend.github_token == "test-token"
        assert backend.github_api_url == "https://api.github.com"

    def test_initialization_custom_config(self):
        """Test initialization with custom configuration."""
        # Arrange
        config = {
            "include_kotlin": False,
            "github_token": "custom-token",
            "github_api_url": "https://custom-api.github.com",
            "source_roots": ["src/main/java"],
        }

        # Act
        backend = JavaBackend(config)

        # Assert
        assert backend.include_kotlin is False
        assert backend.github_token == "custom-token"
        assert backend.github_api_url == "https://custom-api.github.com"

    def test_get_backend_name(self):
        """Test get_backend_name returns correct name."""
        # Arrange
        backend = JavaBackend({"github_token": "test"})

        # Act
        name = backend.get_backend_name()

        # Assert
        assert name == "Java Backend"

    def test_get_backend_type(self):
        """Test get_backend_type returns 'java'."""
        # Arrange
        backend = JavaBackend({"github_token": "test"})

        # Act
        backend_type = backend.get_backend_type()

        # Assert
        assert backend_type == "java"

    def test_validate_config_success(self):
        """Test validate_config with valid configuration."""
        # Arrange
        backend = JavaBackend({"github_token": "test-token"})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """Test validate_config fails when github_token is missing."""
        # Arrange
        backend = JavaBackend({})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is False
        assert "github_token is required" in error


class TestLanguageConfiguration:
    """Test language configuration methods."""

    def test_get_source_extensions_default(self):
        """Test default source extensions include Java and Kotlin."""
        # Arrange
        backend = JavaBackend({"github_token": "test", "include_kotlin": True})

        # Act
        extensions = backend.get_source_extensions()

        # Assert
        assert ".java" in extensions
        assert ".kt" in extensions
        assert ".kts" in extensions

    def test_get_source_extensions_java_only(self):
        """Test source extensions with Kotlin disabled."""
        # Arrange
        backend = JavaBackend({"github_token": "test", "include_kotlin": False})

        # Act
        extensions = backend.get_source_extensions()

        # Assert
        assert ".java" in extensions
        assert ".kt" not in extensions
        assert ".kts" not in extensions

    def test_get_source_roots_default(self):
        """Test default source roots follow Maven/Gradle conventions."""
        # Arrange
        backend = JavaBackend({"github_token": "test"})

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert "src/main/java" in roots
        assert "src/main/kotlin" in roots

    def test_get_source_roots_custom(self):
        """Test custom source roots override defaults."""
        # Arrange
        backend = JavaBackend({
            "github_token": "test",
            "source_roots": ["custom/src"]
        })

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert roots == ["custom/src"]


class TestDiscoverCodeUnits:
    """Test code unit discovery using ADR-009 algorithm."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_discover_java_packages_by_file_count(self):
        """Test discovering code units by file count threshold."""
        # Arrange - simulate com/apriori/common/applicationkernel with 3 files
        files = [
            {"path": "src/main/java/com/apriori/common/applicationkernel/Application.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/applicationkernel/Kernel.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/applicationkernel/Config.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert source_units[0].name == "common-applicationkernel"
        assert len(source_units[0].file_paths) == 3

    def test_discover_nested_packages(self):
        """Test discovering deeply nested packages like core/model/application."""
        # Arrange
        files = [
            {"path": "src/main/java/com/apriori/common/core/model/application/AppModel.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/core/model/application/AppState.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        # Should strip com/apriori prefix and create meaningful name
        assert "application" in source_units[0].name.lower()
        assert len(source_units[0].file_paths) == 2

    def test_discover_by_boundary_pattern(self):
        """Test discovering code unit by boundary pattern (e.g., 'service')."""
        # Arrange - a 'service' directory with only 1 file should still be discovered
        files = [
            {"path": "src/main/java/com/company/notification/service/EmailService.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert "service" in source_units[0].name.lower()

    def test_discover_multiple_packages(self):
        """Test discovering multiple separate code units."""
        # Arrange - simulate the apriori example structure
        files = [
            # applicationkernel - 3 files
            {"path": "src/main/java/com/apriori/common/applicationkernel/App.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/applicationkernel/Kernel.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/applicationkernel/Config.java", "type": "blob"},
            # notification/email - 3 files
            {"path": "src/main/java/com/apriori/common/notification/email/EmailService.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/notification/email/Template.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/notification/email/SmtpClient.java", "type": "blob"},
            # restapiclient - 2 files
            {"path": "src/main/java/com/apriori/common/restapiclient/RestClient.java", "type": "blob"},
            {"path": "src/main/java/com/apriori/common/restapiclient/HttpConfig.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 3

        # Verify each unit has the expected files
        names = [u.name for u in source_units]
        assert any("applicationkernel" in n for n in names)
        assert any("email" in n for n in names)
        assert any("restapiclient" in n for n in names)

    def test_discover_excludes_target_directory(self):
        """Test that target/ (build output) is excluded."""
        # Arrange
        files = [
            {"path": "src/main/java/com/company/Service.java", "type": "blob"},
            {"path": "src/main/java/com/company/Model.java", "type": "blob"},
            {"path": "target/classes/com/company/Service.class", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        for unit in source_units:
            assert not any("target/" in path for path in unit.file_paths)

    def test_discover_kotlin_files(self):
        """Test discovering Kotlin files alongside Java."""
        # Arrange
        files = [
            {"path": "src/main/kotlin/com/company/service/UserService.kt", "type": "blob"},
            {"path": "src/main/kotlin/com/company/service/AuthService.kt", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert all(path.endswith(".kt") for path in source_units[0].file_paths)


class TestDiscoverJavaTests:
    """Test Java test file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_discover_unit_tests(self):
        """Test discovering unit tests in src/test/java."""
        # Arrange
        files = [
            {"path": "src/test/java/com/company/service/UserServiceTest.java", "type": "blob"},
            {"path": "src/test/java/com/company/service/AuthServiceTest.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        test_units = [u for u in code_units if u.unit_type == "tests"]
        assert len(test_units) >= 1
        unit_test = next((u for u in test_units if "unit" in u.name), None)
        assert unit_test is not None
        assert len(unit_test.file_paths) == 2

    def test_discover_integration_tests(self):
        """Test discovering integration tests."""
        # Arrange
        files = [
            {"path": "src/integrationTest/java/com/company/IntegrationTest.java", "type": "blob"},
            {"path": "src/test/java/com/company/integration/ApiIntegrationTest.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        test_units = [u for u in code_units if u.unit_type == "tests"]
        integration_test = next((u for u in test_units if "integration" in u.name), None)
        assert integration_test is not None
        assert len(integration_test.file_paths) == 2

    def test_discover_e2e_tests(self):
        """Test discovering end-to-end tests."""
        # Arrange
        files = [
            {"path": "src/e2e/java/com/company/E2ETest.java", "type": "blob"},
            {"path": "src/test/java/com/company/acceptance/AcceptanceTest.java", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        test_units = [u for u in code_units if u.unit_type == "tests"]
        e2e_test = next((u for u in test_units if "e2e" in u.name), None)
        assert e2e_test is not None


class TestDiscoverBuildConfig:
    """Test build configuration discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_discover_maven_config(self):
        """Test discovering Maven pom.xml files."""
        # Arrange
        files = [
            {"path": "pom.xml", "type": "blob"},
            {"path": "module1/pom.xml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        build_units = [u for u in code_units if u.unit_type == "build"]
        assert len(build_units) == 1
        assert len(build_units[0].file_paths) == 2

    def test_discover_gradle_config(self):
        """Test discovering Gradle build files."""
        # Arrange
        files = [
            {"path": "build.gradle", "type": "blob"},
            {"path": "settings.gradle", "type": "blob"},
            {"path": "module/build.gradle.kts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        build_units = [u for u in code_units if u.unit_type == "build"]
        assert len(build_units) == 1
        assert len(build_units[0].file_paths) == 3


class TestDiscoverKubernetesManifests:
    """Test Kubernetes manifest discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_discover_k8s_manifests(self):
        """Test discovering K8s manifests in k8s/ directory."""
        # Arrange
        files = [
            {"path": "k8s/deployment.yaml", "type": "blob"},
            {"path": "k8s/service.yaml", "type": "blob"},
            {"path": "k8s/configmap.yml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        k8s_units = [u for u in code_units if u.unit_type == "kubernetes"]
        assert len(k8s_units) == 1
        assert len(k8s_units[0].file_paths) == 3

    def test_discover_helm_charts(self):
        """Test discovering Helm chart files."""
        # Arrange
        files = [
            {"path": "helm/values.yaml", "type": "blob"},
            {"path": "charts/myapp/Chart.yaml", "type": "blob"},
            {"path": "charts/myapp/templates/deployment.yaml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        k8s_units = [u for u in code_units if u.unit_type == "kubernetes"]
        assert len(k8s_units) == 1


class TestDiscoverInfrastructure:
    """Test infrastructure file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_discover_terraform(self):
        """Test discovering Terraform files."""
        # Arrange
        files = [
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "terraform/variables.tf", "type": "blob"},
            {"path": "terraform/outputs.tf", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        infra_units = [u for u in code_units if u.unit_type == "infrastructure"]
        assert len(infra_units) == 1
        assert len(infra_units[0].file_paths) == 3


class TestDetectChanges:
    """Test change detection."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    @patch.object(JavaBackend, "_get_current_commit_sha")
    def test_detect_changes_no_previous_state(self, mock_get_sha):
        """Test change detection with no previous state."""
        # Arrange
        mock_get_sha.return_value = "abc123"

        # Act
        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=None,
            force_full=False
        )

        # Assert
        assert result.has_changes is True
        assert result.last_commit_sha is None
        assert result.current_commit_sha == "abc123"

    @patch.object(JavaBackend, "_get_current_commit_sha")
    def test_detect_changes_no_changes(self, mock_get_sha):
        """Test change detection when commit SHA hasn't changed."""
        # Arrange
        same_sha = "abc123def456"
        mock_get_sha.return_value = same_sha
        last_state = {"commit_sha": same_sha}

        # Act
        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=False
        )

        # Assert
        assert result.has_changes is False

    @patch.object(JavaBackend, "_get_changed_files")
    @patch.object(JavaBackend, "_get_current_commit_sha")
    def test_detect_changes_with_changes(self, mock_get_sha, mock_get_files):
        """Test change detection when commit SHA has changed."""
        # Arrange
        mock_get_sha.return_value = "new_sha"
        # _get_changed_files returns (changed_files, removed_files)
        mock_get_files.return_value = (["src/main/java/com/company/Service.java"], [])
        last_state = {"commit_sha": "old_sha"}

        # Act
        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=False
        )

        # Assert
        assert result.has_changes is True
        assert result.last_commit_sha == "old_sha"
        assert result.current_commit_sha == "new_sha"


class TestGenerateBatchMetadata:
    """Test batch metadata generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_generate_batch_metadata_source_module(self):
        """Test generating metadata for source module code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="common-notification-email",
            unit_type="source-module",
            file_paths=["src/main/java/com/company/notification/email/EmailService.java"],
            metadata={"batch_type": "source-module"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "source-module"
        assert metadata["group_name"] == "common-notification-email"
        assert metadata["storage_key"] == "summary#module#common-notification-email"
        assert metadata["backend_type"] == "java"

    def test_generate_batch_metadata_tests(self):
        """Test generating metadata for test code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="tests-unit",
            unit_type="tests",
            file_paths=["src/test/java/com/company/ServiceTest.java"],
            metadata={"batch_type": "tests"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "tests"
        assert metadata["storage_key"] == "summary#tests#tests-unit"


class TestGetStorageKey:
    """Test storage key generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = JavaBackend({"github_token": "test-token"})

    def test_get_storage_key_source_module(self):
        """Test storage key for source-module."""
        # Arrange
        code_unit = CodeUnit(
            name="notification-email",
            unit_type="source-module",
            file_paths=[],
            metadata={"batch_type": "source-module"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#module#notification-email"

    def test_get_storage_key_tests(self):
        """Test storage key for tests."""
        # Arrange
        code_unit = CodeUnit(
            name="tests-unit",
            unit_type="tests",
            file_paths=[],
            metadata={"batch_type": "tests"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#tests#tests-unit"

    def test_get_storage_key_kubernetes(self):
        """Test storage key for kubernetes."""
        # Arrange
        code_unit = CodeUnit(
            name="kubernetes",
            unit_type="kubernetes",
            file_paths=[],
            metadata={"batch_type": "kubernetes"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#kubernetes"

    def test_get_storage_key_build(self):
        """Test storage key for build config."""
        # Arrange
        code_unit = CodeUnit(
            name="build-config",
            unit_type="build",
            file_paths=[],
            metadata={"batch_type": "build"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#build"
