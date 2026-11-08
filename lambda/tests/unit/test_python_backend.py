"""Unit tests for Python backend."""

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

python_backend_path = os.path.join(os.path.dirname(__file__), '../../process-repo-summary/backends/python_backend.py')
spec = importlib.util.spec_from_file_location("python_backend", python_backend_path)
python_backend_module = importlib.util.module_from_spec(spec)
sys.modules['python_backend'] = python_backend_module
spec.loader.exec_module(python_backend_module)

# Import classes
CodeUnit = base_module.CodeUnit
PythonBackend = python_backend_module.PythonBackend


class TestPythonBackendInitialization:
    """Test Python backend initialization."""

    def test_initialization_default_config(self):
        """Test initialization with default configuration."""
        # Arrange
        config = {"github_token": "test-token"}

        # Act
        backend = PythonBackend(config)

        # Assert
        assert backend.lambda_directory == "lambda"
        assert backend.handler_file == "handler.py"
        assert backend.include_submodules is True
        assert backend.max_file_size_tokens == 7000
        assert backend.github_token == "test-token"

    def test_initialization_custom_config(self):
        """Test initialization with custom configuration."""
        # Arrange
        config = {
            "lambda_directory": "functions",
            "handler_file": "index.py",
            "include_submodules": False,
            "max_file_size_tokens": 10000,
            "github_token": "custom-token",
            "github_api_url": "https://custom-api.github.com"
        }

        # Act
        backend = PythonBackend(config)

        # Assert
        assert backend.lambda_directory == "functions"
        assert backend.handler_file == "index.py"
        assert backend.include_submodules is False
        assert backend.max_file_size_tokens == 10000
        assert backend.github_token == "custom-token"
        assert backend.github_api_url == "https://custom-api.github.com"

    def test_get_backend_name(self):
        """Test get_backend_name returns correct name."""
        # Arrange
        backend = PythonBackend({"github_token": "test"})

        # Act
        name = backend.get_backend_name()

        # Assert
        assert name == "Lambda Serverless Backend"

    def test_get_backend_type(self):
        """Test get_backend_type returns 'lambda'."""
        # Arrange
        backend = PythonBackend({"github_token": "test"})

        # Act
        backend_type = backend.get_backend_type()

        # Assert
        assert backend_type == "lambda"

    def test_validate_config_success(self):
        """Test validate_config with valid configuration."""
        # Arrange
        backend = PythonBackend({"github_token": "test-token"})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """Test validate_config fails when github_token is missing."""
        # Arrange
        backend = PythonBackend({})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is False
        assert "github_token is required" in error


class TestDiscoverCodeUnits:
    """Test code unit discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = PythonBackend({"github_token": "test-token"})

    def test_discover_lambda_handlers(self):
        """Test discovering Lambda handler groups."""
        # Arrange
        files = [
            {"path": "lambda/ingest-docs/handler.py", "type": "blob"},
            {"path": "lambda/ingest-docs/utils.py", "type": "blob"},
            {"path": "lambda/generate-code-maps/handler.py", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        handler_units = [u for u in code_units if u.unit_type == "handler-group"]
        assert len(handler_units) == 2

        ingest_unit = next(u for u in handler_units if u.name == "ingest-docs")
        assert len(ingest_unit.file_paths) == 2
        assert "lambda/ingest-docs/handler.py" in ingest_unit.file_paths

        gen_unit = next(u for u in handler_units if u.name == "generate-code-maps")
        assert len(gen_unit.file_paths) == 1

    def test_discover_infrastructure(self):
        """Test discovering infrastructure files."""
        # Arrange
        files = [
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "terraform/lambda.tf", "type": "blob"},
            {"path": "terraform/variables.tf", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        infra_units = [u for u in code_units if u.unit_type == "infrastructure"]
        assert len(infra_units) == 1
        assert infra_units[0].name == "infrastructure"
        assert len(infra_units[0].file_paths) == 3

    def test_discover_frontend_pages(self):
        """Test discovering frontend page files."""
        # Arrange
        files = [
            {"path": "pages/index.tsx", "type": "blob"},
            {"path": "pages/about.tsx", "type": "blob"},
            {"path": "routes/dashboard.jsx", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        page_units = [u for u in code_units if u.unit_type == "frontend-pages"]
        assert len(page_units) == 1
        assert page_units[0].name == "pages-routes"
        assert len(page_units[0].file_paths) == 3

    def test_discover_excludes_node_modules(self):
        """Test that node_modules files are excluded."""
        # Arrange
        files = [
            {"path": "lambda/handler/handler.py", "type": "blob"},
            {"path": "node_modules/package/index.js", "type": "blob"},
            {"path": "__pycache__/handler.cpython-312.pyc", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        # Should only find the lambda handler, not node_modules or __pycache__
        handler_units = [u for u in code_units if u.unit_type == "handler-group"]
        assert len(handler_units) == 1
        assert handler_units[0].name == "handler"

    def test_discover_backend_tests(self):
        """Test discovering backend test files."""
        # Arrange
        files = [
            {"path": "tests/unit/test_handler.py", "type": "blob"},
            {"path": "tests/unit/test_utils.py", "type": "blob"},
            {"path": "tests/integration/test_flow.py", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        test_units = [u for u in code_units if u.unit_type == "tests"]
        assert len(test_units) >= 2

        unit_test = next((u for u in test_units if u.name == "unit"), None)
        assert unit_test is not None
        assert len(unit_test.file_paths) == 2

    def test_discover_documentation(self):
        """Test discovering documentation files."""
        # Arrange
        files = [
            {"path": "README.md", "type": "blob"},
            {"path": "docs/architecture.md", "type": "blob"},
            {"path": "docs/adr/ADR-001.md", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        doc_units = [u for u in code_units if u.unit_type == "docs"]
        assert len(doc_units) == 1
        assert doc_units[0].name == "documentation"
        assert len(doc_units[0].file_paths) == 3

    def test_discover_mixed_files(self):
        """Test discovering code units from mixed file types."""
        # Arrange
        files = [
            {"path": "lambda/auth/handler.py", "type": "blob"},
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "pages/index.tsx", "type": "blob"},
            {"path": "tests/unit/test_auth.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        assert len(code_units) >= 5  # handler, infrastructure, pages, tests, docs
        unit_types = [u.unit_type for u in code_units]
        assert "handler-group" in unit_types
        assert "infrastructure" in unit_types
        assert "frontend-pages" in unit_types
        assert "tests" in unit_types
        assert "docs" in unit_types


class TestDetectChanges:
    """Test change detection."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = PythonBackend({"github_token": "test-token"})

    @patch.object(PythonBackend, "_get_current_commit_sha")
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

    @patch.object(PythonBackend, "_get_current_commit_sha")
    def test_detect_changes_force_full(self, mock_get_sha):
        """Test change detection with force_full=True."""
        # Arrange
        mock_get_sha.return_value = "def456"
        last_state = {"commit_sha": "abc123"}

        # Act
        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=True
        )

        # Assert
        assert result.has_changes is True
        assert result.last_commit_sha == "abc123"
        assert result.current_commit_sha == "def456"

    @patch.object(PythonBackend, "_get_current_commit_sha")
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
        assert result.last_commit_sha == same_sha
        assert result.current_commit_sha == same_sha

    @patch.object(PythonBackend, "_get_changed_files")
    @patch.object(PythonBackend, "_get_current_commit_sha")
    def test_detect_changes_with_changes(self, mock_get_sha, mock_get_files):
        """Test change detection when commit SHA has changed."""
        # Arrange
        mock_get_sha.return_value = "new_sha"
        # _get_changed_files returns (changed_files, removed_files)
        mock_get_files.return_value = (["lambda/auth/handler.py", "terraform/main.tf"], [])
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
        mock_get_files.assert_called_once_with("owner/test-repo", "old_sha", "new_sha")


class TestGenerateBatchMetadata:
    """Test batch metadata generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = PythonBackend({"github_token": "test-token"})

    def test_generate_batch_metadata_handler(self):
        """Test generating metadata for handler code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="ingest-docs",
            unit_type="handler-group",
            file_paths=["lambda/ingest-docs/handler.py"],
            metadata={"batch_type": "handler-group"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "handler-group"
        assert metadata["group_name"] == "ingest-docs"
        assert metadata["storage_key"] == "summary#handler#ingest-docs"
        assert metadata["backend_type"] == "lambda"
        assert len(metadata["files"]) == 1

    def test_generate_batch_metadata_infrastructure(self):
        """Test generating metadata for infrastructure code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=["terraform/main.tf", "terraform/lambda.tf"],
            metadata={"batch_type": "infrastructure"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "infrastructure"
        assert metadata["group_name"] == "infrastructure"
        assert metadata["storage_key"] == "summary#infrastructure"


class TestGetStorageKey:
    """Test storage key generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = PythonBackend({"github_token": "test-token"})

    def test_get_storage_key_handler_group(self):
        """Test storage key for handler-group."""
        # Arrange
        code_unit = CodeUnit(
            name="auth-handler",
            unit_type="handler-group",
            file_paths=[],
            metadata={"batch_type": "handler-group"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#handler#auth-handler"

    def test_get_storage_key_tests(self):
        """Test storage key for tests."""
        # Arrange
        code_unit = CodeUnit(
            name="unit",
            unit_type="tests",
            file_paths=[],
            metadata={"batch_type": "tests"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#tests#unit"

    def test_get_storage_key_frontend(self):
        """Test storage key for frontend components."""
        # Arrange
        code_unit = CodeUnit(
            name="components",
            unit_type="frontend-components",
            file_paths=[],
            metadata={"batch_type": "frontend-components"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#frontend#components"

    def test_get_storage_key_infrastructure(self):
        """Test storage key for infrastructure."""
        # Arrange
        code_unit = CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=[],
            metadata={"batch_type": "infrastructure"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#infrastructure"


class TestSourceRootsConfig:
    """Test source_roots configuration."""

    def test_default_source_roots(self):
        """Test default source roots when not configured."""
        # Arrange
        backend = PythonBackend({"github_token": "test-token"})

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert "lambda" in roots
        assert "src" in roots

    def test_configured_source_roots(self):
        """Test source roots can be overridden via config."""
        # Arrange
        backend = PythonBackend({
            "github_token": "test-token",
            "source_roots": [
                "lambda/",
                "lambda/layers/utils/python/",
                "src/"
            ]
        })

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert roots == ["lambda/", "lambda/layers/utils/python/", "src/"]
        assert len(roots) == 3

    def test_configured_source_roots_empty_uses_default(self):
        """Test empty source_roots falls back to default."""
        # Arrange
        backend = PythonBackend({
            "github_token": "test-token",
            "source_roots": []
        })

        # Act
        roots = backend.get_source_roots()

        # Assert - empty list is falsy, so defaults apply
        assert "lambda" in roots
        assert "src" in roots


class TestPythonSourceRootsOverride:
    """.outcomeops.yaml source_roots override is honored by discovery (previously
    read into config but never applied -- a generic/root-package Python repo got
    zero code units)."""

    @staticmethod
    def _blob(path):
        return {"type": "blob", "path": path, "name": path.split("/")[-1]}

    ROOT_PKG_FILES = [
        {"type": "blob", "path": "shared_lib/__init__.py", "name": "__init__.py"},
        {"type": "blob", "path": "shared_lib/math_utils.py", "name": "math_utils.py"},
        {"type": "blob", "path": "shared_lib/string_utils.py", "name": "string_utils.py"},
        {"type": "blob", "path": "README.md", "name": "README.md"},
    ]

    def _source_modules(self, backend):
        return [
            u for u in backend.discover_code_units(self.ROOT_PKG_FILES)
            if u.metadata.get("batch_type") == "source-module"
        ]

    def test_override_discovers_root_package(self):
        """With source_roots set, a package at the repo root becomes a code unit."""
        backend = PythonBackend({"github_token": "t", "source_roots": ["shared_lib"]})
        units = self._source_modules(backend)
        assert len(units) == 1
        assert units[0].name == "shared_lib"
        assert set(units[0].file_paths) == {
            "shared_lib/__init__.py",
            "shared_lib/math_utils.py",
            "shared_lib/string_utils.py",
        }

    def test_no_override_leaves_root_package_undiscovered(self):
        """Without an override the lambda-centric default is unchanged -- a root
        package is NOT discovered (regression guard for this platform's repo)."""
        backend = PythonBackend({"github_token": "t"})
        assert self._source_modules(backend) == []

    def test_lambda_layout_still_discovered_with_default(self):
        """The platform's own lambda/ discovery is untouched (no override)."""
        files = [
            self._blob("lambda/ingest-docs/handler.py"),
            self._blob("lambda/ingest-docs/util.py"),
        ]
        backend = PythonBackend({"github_token": "t"})
        names = [u.name for u in backend.discover_code_units(files)]
        # handler-group unit for the lambda directory is discovered as before
        assert any("ingest-docs" in n for n in names)
