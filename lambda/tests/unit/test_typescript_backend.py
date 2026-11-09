"""Unit tests for TypeScript backend."""

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

typescript_backend_path = os.path.join(os.path.dirname(__file__), '../../process-repo-summary/backends/typescript_backend.py')
spec = importlib.util.spec_from_file_location("typescript_backend", typescript_backend_path)
typescript_backend_module = importlib.util.module_from_spec(spec)
sys.modules['typescript_backend'] = typescript_backend_module
spec.loader.exec_module(typescript_backend_module)

# Import classes
CodeUnit = base_module.CodeUnit
TypeScriptBackend = typescript_backend_module.TypeScriptBackend


class TestTypeScriptBackendInitialization:
    """Test TypeScript backend initialization."""

    def test_initialization_default_config(self):
        """Test initialization with default configuration."""
        # Arrange
        config = {"github_token": "test-token"}

        # Act
        backend = TypeScriptBackend(config)

        # Assert
        assert backend.include_javascript is True
        assert backend.github_token == "test-token"
        assert backend.github_api_url == "https://api.github.com"
        assert "-handler.ts" in backend.handler_patterns

    def test_initialization_custom_config(self):
        """Test initialization with custom configuration."""
        # Arrange
        config = {
            "include_javascript": False,
            "github_token": "custom-token",
            "github_api_url": "https://custom-api.github.com",
            "handler_patterns": [".handler.ts"],
        }

        # Act
        backend = TypeScriptBackend(config)

        # Assert
        assert backend.include_javascript is False
        assert backend.github_token == "custom-token"
        assert backend.github_api_url == "https://custom-api.github.com"
        assert backend.handler_patterns == [".handler.ts"]

    def test_get_backend_name(self):
        """Test get_backend_name returns correct name."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test"})

        # Act
        name = backend.get_backend_name()

        # Assert
        assert name == "TypeScript Backend"

    def test_get_backend_type(self):
        """Test get_backend_type returns 'typescript'."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test"})

        # Act
        backend_type = backend.get_backend_type()

        # Assert
        assert backend_type == "typescript"

    def test_validate_config_success(self):
        """Test validate_config with valid configuration."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test-token"})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """Test validate_config fails when github_token is missing."""
        # Arrange
        backend = TypeScriptBackend({})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is False
        assert "github_token is required" in error


class TestLanguageConfiguration:
    """Test language configuration methods."""

    def test_get_source_extensions_default(self):
        """Test default source extensions include TS and JS."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test", "include_javascript": True})

        # Act
        extensions = backend.get_source_extensions()

        # Assert
        assert ".ts" in extensions
        assert ".tsx" in extensions
        assert ".js" in extensions
        assert ".jsx" in extensions

    def test_get_source_extensions_typescript_only(self):
        """Test source extensions with JavaScript disabled."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test", "include_javascript": False})

        # Act
        extensions = backend.get_source_extensions()

        # Assert
        assert ".ts" in extensions
        assert ".tsx" in extensions
        assert ".js" not in extensions
        assert ".jsx" not in extensions

    def test_get_source_roots_default(self):
        """Test default source roots for Node.js projects."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test"})

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert "src" in roots
        assert "handlers" in roots
        assert "app" in roots
        assert "pages" in roots

    def test_get_excluded_patterns(self):
        """Test TypeScript-specific exclusion patterns."""
        # Arrange
        backend = TypeScriptBackend({"github_token": "test"})

        # Act
        excluded = backend.get_excluded_patterns()

        # Assert
        assert "node_modules/" in excluded
        assert "dist/" in excluded
        assert ".next/" in excluded


class TestDiscoverHandlerGroups:
    """Test Lambda handler group discovery."""

    def setup_method(self):
        """Set up test backend with no custom handler_directories to test standard handlers/ pattern."""
        self.backend = TypeScriptBackend({"github_token": "test-token", "handler_directories": []})

    def test_discover_handler_groups_by_pattern(self):
        """Test discovering handlers by *-handler.ts pattern."""
        # Arrange
        files = [
            {"path": "src/handlers/api-gw/action-mapper/action-mapper-handler.ts", "type": "blob"},
            {"path": "src/handlers/api-gw/action-mapper/utils.ts", "type": "blob"},
            {"path": "src/handlers/api-gw/user-auth/user-auth-handler.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        handler_units = [u for u in code_units if u.unit_type == "handler-group"]
        assert len(handler_units) == 2

        action_mapper = next((u for u in handler_units if "action-mapper" in u.name), None)
        assert action_mapper is not None
        assert len(action_mapper.file_paths) == 2  # handler + utils

    def test_discover_handler_groups_includes_related_files(self):
        """Test that handler groups include all files in the directory."""
        # Arrange
        files = [
            {"path": "src/handlers/users/users-handler.ts", "type": "blob"},
            {"path": "src/handlers/users/users-schema.ts", "type": "blob"},
            {"path": "src/handlers/users/index.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        handler_units = [u for u in code_units if u.unit_type == "handler-group"]
        assert len(handler_units) == 1
        assert len(handler_units[0].file_paths) == 3


class TestDiscoverApiRoutes:
    """Test API route discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_api_routes(self):
        """Test discovering API routes by directory."""
        # Arrange
        files = [
            {"path": "src/routes/users/index.ts", "type": "blob"},
            {"path": "src/routes/users/create.ts", "type": "blob"},
            {"path": "src/api/products/list.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        api_units = [u for u in code_units if u.unit_type == "api-routes"]
        assert len(api_units) == 2

        routes_users = next((u for u in api_units if "routes-users" in u.name), None)
        assert routes_users is not None
        assert len(routes_users.file_paths) == 2


class TestDiscoverFrontendComponents:
    """Test frontend component discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_react_components(self):
        """Test discovering React components."""
        # Arrange
        files = [
            {"path": "src/components/Button.tsx", "type": "blob"},
            {"path": "src/components/Card.tsx", "type": "blob"},
            {"path": "app/page.tsx", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        frontend_units = [u for u in code_units if u.unit_type == "frontend"]
        assert len(frontend_units) == 2

    def test_discover_vue_components(self):
        """Test discovering Vue components."""
        # Arrange
        files = [
            {"path": "src/components/Header.vue", "type": "blob"},
            {"path": "src/components/Footer.vue", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        frontend_units = [u for u in code_units if u.unit_type == "frontend"]
        assert len(frontend_units) == 1
        assert len(frontend_units[0].file_paths) == 2


class TestDiscoverSchemas:
    """Test schema file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_schema_files(self):
        """Test discovering schema files by pattern."""
        # Arrange
        files = [
            {"path": "src/handlers/users/users-schema.ts", "type": "blob"},
            {"path": "src/schemas/product.schema.ts", "type": "blob"},
            {"path": "src/utils/helper.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        schema_units = [u for u in code_units if u.unit_type == "schemas"]
        assert len(schema_units) == 1
        assert len(schema_units[0].file_paths) == 2


class TestDiscoverSharedCode:
    """Test shared/common code discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_shared_code(self):
        """Test discovering shared code directories."""
        # Arrange
        files = [
            {"path": "src/shared/database/client.ts", "type": "blob"},
            {"path": "src/common/logger/index.ts", "type": "blob"},
            {"path": "src/utils/format.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        shared_units = [u for u in code_units if u.unit_type == "shared"]
        assert len(shared_units) >= 2

    def test_discover_shared_excludes_handlers(self):
        """Test that shared discovery excludes utils inside handlers."""
        # Arrange
        files = [
            {"path": "src/handlers/users/utils/helper.ts", "type": "blob"},
            {"path": "src/utils/common.ts", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        shared_units = [u for u in code_units if u.unit_type == "shared"]
        # Only src/utils should be in shared, not handlers/users/utils
        for unit in shared_units:
            assert not any("handlers" in path for path in unit.file_paths)


class TestDiscoverTests:
    """Test test file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_test_files(self):
        """Test discovering test files by pattern."""
        # Arrange
        files = [
            {"path": "src/handlers/users/__tests__/users.test.ts", "type": "blob"},
            {"path": "src/utils/format.spec.ts", "type": "blob"},
            {"path": "e2e/app.test.ts", "type": "blob"},  # e2e needs .test. pattern
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        test_units = [u for u in code_units if u.unit_type == "tests"]
        assert len(test_units) >= 2

        unit_tests = next((u for u in test_units if "unit" in u.name), None)
        assert unit_tests is not None

        e2e_tests = next((u for u in test_units if "e2e" in u.name), None)
        assert e2e_tests is not None


class TestDiscoverConfigFiles:
    """Test configuration file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_config_files(self):
        """Test discovering configuration files."""
        # Arrange
        files = [
            {"path": "serverless.yml", "type": "blob"},
            {"path": "tsconfig.json", "type": "blob"},
            {"path": "vite.config.ts", "type": "blob"},
            {"path": ".gitlab-ci.yml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        config_units = [u for u in code_units if u.unit_type == "config"]
        assert len(config_units) == 1
        assert len(config_units[0].file_paths) == 4


class TestDiscoverInfrastructure:
    """Test infrastructure file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_discover_terraform(self):
        """Test discovering Terraform files."""
        # Arrange
        files = [
            {"path": "infrastructure/main.tf", "type": "blob"},
            {"path": "infrastructure/lambda.tf", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        infra_units = [u for u in code_units if u.unit_type == "infrastructure"]
        assert len(infra_units) == 1
        assert len(infra_units[0].file_paths) == 2


class TestExcludedPaths:
    """Test path exclusion."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_excludes_node_modules(self):
        """Test that node_modules is excluded."""
        # Arrange
        files = [
            {"path": "src/index.ts", "type": "blob"},
            {"path": "node_modules/lodash/index.js", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        all_paths = []
        for unit in code_units:
            all_paths.extend(unit.file_paths)
        assert not any("node_modules" in path for path in all_paths)

    def test_excludes_dist(self):
        """Test that dist is excluded."""
        # Arrange
        files = [
            {"path": "src/index.ts", "type": "blob"},
            {"path": "dist/index.js", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        all_paths = []
        for unit in code_units:
            all_paths.extend(unit.file_paths)
        assert not any("dist/" in path for path in all_paths)


class TestDetectChanges:
    """Test change detection."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    @patch.object(TypeScriptBackend, "_get_current_commit_sha")
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

    @patch.object(TypeScriptBackend, "_get_current_commit_sha")
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

    @patch.object(TypeScriptBackend, "_get_changed_files")
    @patch.object(TypeScriptBackend, "_get_current_commit_sha")
    def test_detect_changes_with_changes(self, mock_get_sha, mock_get_files):
        """Test change detection when commit SHA has changed."""
        # Arrange
        mock_get_sha.return_value = "new_sha"
        # _get_changed_files returns (changed_files, removed_files)
        mock_get_files.return_value = (["src/handlers/users/users-handler.ts"], [])
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
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_generate_batch_metadata_handler(self):
        """Test generating metadata for handler code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="api-gw-action-mapper",
            unit_type="handler-group",
            file_paths=["src/handlers/api-gw/action-mapper/action-mapper-handler.ts"],
            metadata={"batch_type": "handler-group"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "handler-group"
        assert metadata["group_name"] == "api-gw-action-mapper"
        assert metadata["storage_key"] == "summary#handler#api-gw-action-mapper"
        assert metadata["backend_type"] == "typescript"


class TestGetStorageKey:
    """Test storage key generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = TypeScriptBackend({"github_token": "test-token"})

    def test_get_storage_key_handler_group(self):
        """Test storage key for handler-group."""
        # Arrange
        code_unit = CodeUnit(
            name="api-gw-users",
            unit_type="handler-group",
            file_paths=[],
            metadata={"batch_type": "handler-group"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#handler#api-gw-users"

    def test_get_storage_key_api_routes(self):
        """Test storage key for api-routes."""
        # Arrange
        code_unit = CodeUnit(
            name="routes-users",
            unit_type="api-routes",
            file_paths=[],
            metadata={"batch_type": "api-routes"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#api#routes-users"

    def test_get_storage_key_frontend(self):
        """Test storage key for frontend."""
        # Arrange
        code_unit = CodeUnit(
            name="src-components",
            unit_type="frontend",
            file_paths=[],
            metadata={"batch_type": "frontend"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#frontend#src-components"

    def test_get_storage_key_schemas(self):
        """Test storage key for schemas."""
        # Arrange
        code_unit = CodeUnit(
            name="schemas",
            unit_type="schemas",
            file_paths=[],
            metadata={"batch_type": "schemas"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#schemas"

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

    def test_get_storage_key_config(self):
        """Test storage key for config."""
        # Arrange
        code_unit = CodeUnit(
            name="config",
            unit_type="config",
            file_paths=[],
            metadata={"batch_type": "config"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#config"


class TestGenericLibrarySourceDiscovery:
    """A plain TypeScript LIBRARY repo (no handlers/routes/components, no
    handler_directories) must still have its source discovered (Group 5b)."""

    def setup_method(self):
        # handler_directories=[] is the default get_backend_for_repo passes when
        # .outcomeops.yaml omits it -- the config that previously found no source.
        self.backend = TypeScriptBackend({"github_token": "test-token",
                                          "handler_directories": []})

    def test_library_src_discovered_as_source_module(self):
        files = [
            {"path": "package.json", "type": "blob"},
            {"path": "README.md", "type": "blob"},
            {"path": "src/mathx.ts", "type": "blob"},
            {"path": "src/strutil.ts", "type": "blob"},
        ]
        units = self.backend.discover_code_units(files)
        source = [u for u in units if u.unit_type == "source-module"]
        assert len(source) == 1
        assert set(source[0].file_paths) == {"src/mathx.ts", "src/strutil.ts"}
        # Storage key is name-scoped so multiple modules don't collide.
        key = self.backend.get_storage_key(source[0])
        assert key == f"summary#module#{source[0].name}"

    def test_handlers_not_double_counted_as_source_module(self):
        # A handler dir is claimed by the handler group; it must NOT also appear
        # as a generic source-module.
        files = [
            {"path": "src/handlers/api/login-handler.ts", "type": "blob"},
            {"path": "src/handlers/api/helpers.ts", "type": "blob"},
        ]
        units = self.backend.discover_code_units(files)
        handler = [u for u in units if u.unit_type == "handler-group"]
        source = [u for u in units if u.unit_type == "source-module"]
        assert len(handler) == 1
        assert source == []  # claimed by the handler group, not re-discovered
