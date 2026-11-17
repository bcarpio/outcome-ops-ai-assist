# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests to increase coverage for process-repo-summary/backends/react_backend.py.

Covers:
- ReactBackend initialization and configuration
- get_backend_name, get_backend_type, validate_config
- get_source_extensions, get_source_roots, get_excluded_patterns
- discover_code_units: full discovery pipeline
- _discover_components: component grouping by category
- _get_associated_files: associated file discovery
- _discover_hooks: custom hook detection
- _discover_contexts: context/provider detection
- _discover_pages: Next.js pages, app router, React Router routes
- _discover_state_management: Redux, Zustand, etc.
- _discover_styles: global styles, themes
- _discover_types: TypeScript types and interfaces
- _discover_utilities: utils, helpers, lib, api
- _discover_tests: unit, integration, e2e tests
- _discover_storybook: Storybook stories
- _discover_config_files: configuration files
- get_storage_key: storage key generation for different types
- generate_batch_metadata: batch metadata generation
"""

import os
import sys
import json
from unittest.mock import MagicMock, Mock, patch

import pytest

# Add backends directory to path
BACKENDS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../process-repo-summary/backends")
)
if BACKENDS_DIR not in sys.path:
    sys.path.insert(0, BACKENDS_DIR)

# Add generate-code-maps for base class
CODE_MAPS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../generate-code-maps")
)
if CODE_MAPS_DIR not in sys.path:
    sys.path.insert(0, CODE_MAPS_DIR)

from base import CodeUnit
from react_backend import ReactBackend


def _make_files(paths):
    """Helper to create file objects from paths."""
    return [{"path": p, "type": "blob"} for p in paths]


def _make_backend(config=None):
    """Helper to create a ReactBackend with default config."""
    default_config = {"github_token": "test-token"}
    if config:
        default_config.update(config)
    return ReactBackend(default_config)


# ############################################################################
# INITIALIZATION TESTS
# ############################################################################


class TestReactBackendInit:
    """Test ReactBackend initialization."""

    def test_default_config(self):
        """Test default configuration."""
        backend = _make_backend()
        assert backend.get_backend_name() == "React Frontend Backend"
        assert backend.get_backend_type() == "react"

    def test_custom_component_dirs(self):
        """Test custom component directories."""
        backend = _make_backend({"component_dirs": ["custom/"]})
        assert "custom/" in backend.component_dirs

    def test_validate_config_valid(self):
        """Test config validation with token."""
        backend = _make_backend()
        valid, error = backend.validate_config()
        assert valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """Test config validation without token."""
        backend = ReactBackend({})
        valid, error = backend.validate_config()
        assert valid is False
        assert "github_token" in error


# ############################################################################
# LANGUAGE CONFIGURATION TESTS
# ############################################################################


class TestLanguageConfig:
    """Test language configuration methods."""

    def test_source_extensions(self):
        """Test source file extensions include React types."""
        backend = _make_backend()
        exts = backend.get_source_extensions()
        assert ".tsx" in exts
        assert ".jsx" in exts
        assert ".ts" in exts
        assert ".css" in exts

    def test_default_source_roots(self):
        """Test default source roots."""
        backend = _make_backend()
        roots = backend.get_source_roots()
        assert "src" in roots
        assert "app" in roots
        assert "pages" in roots

    def test_custom_source_roots(self):
        """Test custom source roots override."""
        backend = _make_backend({"source_roots": ["custom/src"]})
        roots = backend.get_source_roots()
        assert roots == ["custom/src"]

    def test_excluded_patterns(self):
        """Test excluded patterns include React-specific dirs."""
        backend = _make_backend()
        excluded = backend.get_excluded_patterns()
        assert "node_modules/" in excluded
        assert ".next/" in excluded
        assert "coverage/" in excluded


# ############################################################################
# COMPONENT DISCOVERY TESTS
# ############################################################################


class TestDiscoverComponents:
    """Test React component discovery."""

    def test_discovers_components_by_category(self):
        """Test components are grouped by category directory."""
        backend = _make_backend()
        files = _make_files([
            "src/components/ui/Button.tsx",
            "src/components/ui/Input.tsx",
            "src/components/layout/Header.tsx",
        ])
        units = backend._discover_components(files)
        assert len(units) >= 2
        names = [u.name for u in units]
        assert "components-ui" in names
        assert "components-layout" in names

    def test_discovers_root_components(self):
        """Test components directly under components/ go to root category."""
        backend = _make_backend()
        files = _make_files([
            "src/components/App.tsx",
        ])
        units = backend._discover_components(files)
        assert len(units) == 1
        assert units[0].name == "components-root"

    def test_skips_test_files(self):
        """Test component discovery skips test files."""
        backend = _make_backend()
        files = _make_files([
            "src/components/ui/Button.test.tsx",
            "src/components/ui/Button.spec.tsx",
            "src/components/ui/Button.stories.tsx",
            "src/components/ui/index.tsx",
        ])
        units = backend._discover_components(files)
        assert len(units) == 0

    def test_skips_non_component_extensions(self):
        """Test only .tsx and .jsx files are considered."""
        backend = _make_backend()
        files = _make_files([
            "src/components/ui/styles.css",
            "src/components/ui/types.ts",
        ])
        units = backend._discover_components(files)
        assert len(units) == 0


# ############################################################################
# ASSOCIATED FILES TESTS
# ############################################################################


class TestGetAssociatedFiles:
    """Test associated file discovery for components."""

    def test_finds_style_files(self):
        """Test finding associated style files."""
        backend = _make_backend()
        all_files = _make_files([
            "src/components/ui/Button.tsx",
            "src/components/ui/Button.module.css",
            "src/components/ui/Button.styles.ts",
        ])
        component_files = ["src/components/ui/Button.tsx"]

        result = backend._get_associated_files(all_files, component_files)
        assert "src/components/ui/Button.module.css" in result
        assert "src/components/ui/Button.styles.ts" in result

    def test_finds_index_files(self):
        """Test finding associated index files."""
        backend = _make_backend()
        all_files = _make_files([
            "src/components/ui/Button.tsx",
            "src/components/ui/index.ts",
        ])
        component_files = ["src/components/ui/Button.tsx"]

        result = backend._get_associated_files(all_files, component_files)
        assert "src/components/ui/index.ts" in result

    def test_skips_non_blob_files(self):
        """Test non-blob files are skipped."""
        backend = _make_backend()
        all_files = [
            {"path": "src/components/ui", "type": "tree"},
            {"path": "src/components/ui/Button.tsx", "type": "blob"},
        ]
        component_files = ["src/components/ui/Button.tsx"]

        result = backend._get_associated_files(all_files, component_files)
        # Only blob type files should be considered
        assert "src/components/ui" not in result


# ############################################################################
# HOOK DISCOVERY TESTS
# ############################################################################


class TestDiscoverHooks:
    """Test custom hook discovery."""

    def test_discovers_hooks_by_directory(self):
        """Test discovering hooks in hooks/ directory."""
        backend = _make_backend()
        files = _make_files([
            "src/hooks/useAuth.ts",
            "src/hooks/useFetch.tsx",
        ])
        unit = backend._discover_hooks(files)
        assert unit is not None
        assert unit.name == "hooks"
        assert len(unit.file_paths) == 2

    def test_discovers_hooks_by_filename(self):
        """Test discovering hooks by use* filename pattern."""
        backend = _make_backend()
        files = _make_files([
            "src/useLocalStorage.ts",
        ])
        unit = backend._discover_hooks(files)
        assert unit is not None

    def test_skips_test_hooks(self):
        """Test hook discovery skips test files."""
        backend = _make_backend()
        files = _make_files([
            "src/hooks/useAuth.test.ts",
        ])
        unit = backend._discover_hooks(files)
        assert unit is None

    def test_returns_none_when_no_hooks(self):
        """Test returns None when no hooks found."""
        backend = _make_backend()
        files = _make_files(["src/components/Button.tsx"])
        unit = backend._discover_hooks(files)
        assert unit is None


# ############################################################################
# CONTEXT DISCOVERY TESTS
# ############################################################################


class TestDiscoverContexts:
    """Test context/provider discovery."""

    def test_discovers_context_files(self):
        """Test discovering context files."""
        backend = _make_backend()
        files = _make_files([
            "src/contexts/AuthContext.tsx",
            "src/providers/ThemeProvider.tsx",
        ])
        unit = backend._discover_contexts(files)
        assert unit is not None
        assert unit.name == "contexts"
        assert len(unit.file_paths) == 2

    def test_discovers_by_filename_pattern(self):
        """Test discovering context by filename pattern."""
        backend = _make_backend()
        files = _make_files([
            "src/auth/context.tsx",
            "src/theme/provider.ts",
        ])
        unit = backend._discover_contexts(files)
        assert unit is not None

    def test_returns_none_when_no_contexts(self):
        """Test returns None when no contexts found."""
        backend = _make_backend()
        files = _make_files(["src/utils/helper.ts"])
        unit = backend._discover_contexts(files)
        assert unit is None


# ############################################################################
# PAGE DISCOVERY TESTS
# ############################################################################


class TestDiscoverPages:
    """Test page/route discovery."""

    def test_discovers_nextjs_pages(self):
        """Test discovering Next.js pages directory."""
        backend = _make_backend()
        files = _make_files([
            "src/pages/index.tsx",
            "src/pages/about.tsx",
            "src/pages/blog/index.tsx",
        ])
        units = backend._discover_pages(files)
        assert len(units) >= 1
        names = [u.name for u in units]
        assert any("pages" in n for n in names)

    def test_discovers_nextjs_app_router(self):
        """Test discovering Next.js app directory pages."""
        backend = _make_backend()
        files = _make_files([
            "src/app/page.tsx",
            "src/app/dashboard/page.tsx",
            "src/app/layout.tsx",
            "src/app/other.tsx",
        ])
        units = backend._discover_pages(files)
        assert len(units) >= 1
        names = [u.name for u in units]
        assert any("app" in n for n in names)

    def test_discovers_react_router_routes(self):
        """Test discovering React Router routes."""
        backend = _make_backend()
        files = _make_files([
            "src/routes/Home.tsx",
            "src/views/Dashboard.tsx",
        ])
        units = backend._discover_pages(files)
        assert len(units) >= 1

    def test_skips_api_routes_in_pages(self):
        """Test skipping API routes in pages directory."""
        backend = _make_backend()
        files = _make_files([
            "pages/api/auth.ts",
        ])
        units = backend._discover_pages(files)
        assert len(units) == 0

    def test_returns_empty_when_no_pages(self):
        """Test returns empty list when no pages found."""
        backend = _make_backend()
        files = _make_files(["src/utils/helper.ts"])
        units = backend._discover_pages(files)
        assert units == []


# ############################################################################
# STATE MANAGEMENT DISCOVERY TESTS
# ############################################################################


class TestDiscoverStateManagement:
    """Test state management discovery."""

    def test_discovers_redux_store(self):
        """Test discovering Redux store files."""
        backend = _make_backend()
        files = _make_files([
            "src/store/index.ts",
            "src/store/slices/authSlice.ts",
        ])
        unit = backend._discover_state_management(files)
        assert unit is not None
        assert unit.name == "state-management"

    def test_discovers_zustand_store(self):
        """Test discovering Zustand store files."""
        backend = _make_backend()
        files = _make_files([
            "src/stores/useAuthStore.ts",
        ])
        unit = backend._discover_state_management(files)
        assert unit is not None

    def test_discovers_by_filename_pattern(self):
        """Test discovering state files by filename pattern."""
        backend = _make_backend()
        files = _make_files([
            "src/auth/store.ts",
            "src/counter/reducer.ts",
        ])
        unit = backend._discover_state_management(files)
        assert unit is not None

    def test_returns_none_when_no_state(self):
        """Test returns None when no state management found."""
        backend = _make_backend()
        files = _make_files(["src/components/Button.tsx"])
        unit = backend._discover_state_management(files)
        assert unit is None


# ############################################################################
# STYLES DISCOVERY TESTS
# ############################################################################


class TestDiscoverStyles:
    """Test styles and theme discovery."""

    def test_discovers_global_styles(self):
        """Test discovering global style files."""
        backend = _make_backend()
        files = _make_files([
            "src/styles/globals.css",
            "src/styles/theme.scss",
        ])
        unit = backend._discover_styles(files)
        assert unit is not None
        assert unit.name == "styles"

    def test_discovers_tailwind_config(self):
        """Test discovering Tailwind config."""
        backend = _make_backend()
        files = _make_files([
            "tailwind.config.js",
        ])
        unit = backend._discover_styles(files)
        assert unit is not None

    def test_skips_css_modules(self):
        """Test CSS modules are skipped."""
        backend = _make_backend()
        files = _make_files([
            "src/components/Button.module.css",
        ])
        unit = backend._discover_styles(files)
        assert unit is None

    def test_returns_none_when_no_styles(self):
        """Test returns None when no styles found."""
        backend = _make_backend()
        files = _make_files(["src/handler.ts"])
        unit = backend._discover_styles(files)
        assert unit is None


# ############################################################################
# TYPES DISCOVERY TESTS
# ############################################################################


class TestDiscoverTypes:
    """Test TypeScript type discovery."""

    def test_discovers_types_directory(self):
        """Test discovering types in types/ directory."""
        backend = _make_backend()
        files = _make_files([
            "src/types/user.ts",
            "src/types/api.ts",
        ])
        unit = backend._discover_types(files)
        assert unit is not None
        assert unit.name == "types"

    def test_discovers_types_files(self):
        """Test discovering .types.ts files."""
        backend = _make_backend()
        files = _make_files([
            "src/components/Button.types.ts",
            "src/global.d.ts",
        ])
        unit = backend._discover_types(files)
        assert unit is not None

    def test_discovers_interfaces_directory(self):
        """Test discovering interfaces directory."""
        backend = _make_backend()
        files = _make_files([
            "src/interfaces/IUser.ts",
        ])
        unit = backend._discover_types(files)
        assert unit is not None

    def test_returns_none_when_no_types(self):
        """Test returns None when no types found."""
        backend = _make_backend()
        files = _make_files(["src/handler.ts"])
        unit = backend._discover_types(files)
        assert unit is None


# ############################################################################
# UTILITIES DISCOVERY TESTS
# ############################################################################


class TestDiscoverUtilities:
    """Test utility discovery."""

    def test_discovers_utils(self):
        """Test discovering utility files."""
        backend = _make_backend()
        files = _make_files([
            "src/utils/format.ts",
            "src/helpers/validate.ts",
            "src/lib/api.ts",
            "src/api/client.ts",
        ])
        units = backend._discover_utilities(files)
        assert len(units) >= 3
        names = [u.name for u in units]
        assert "utils" in names
        assert "helpers" in names
        assert "lib" in names

    def test_discovers_services_as_api(self):
        """Test discovering services directory as API group."""
        backend = _make_backend()
        files = _make_files([
            "src/services/authService.ts",
        ])
        units = backend._discover_utilities(files)
        assert len(units) == 1
        assert units[0].name == "api"

    def test_skips_test_files(self):
        """Test utility discovery skips test files."""
        backend = _make_backend()
        files = _make_files([
            "src/utils/format.test.ts",
            "src/__tests__/helper.ts",
        ])
        units = backend._discover_utilities(files)
        assert len(units) == 0

    def test_returns_empty_when_no_utils(self):
        """Test returns empty list when no utilities found."""
        backend = _make_backend()
        files = _make_files(["src/components/Button.tsx"])
        units = backend._discover_utilities(files)
        assert units == []


# ############################################################################
# TEST DISCOVERY TESTS
# ############################################################################


class TestDiscoverTests:
    """Test test file discovery."""

    def test_discovers_unit_tests(self):
        """Test discovering unit test files."""
        backend = _make_backend()
        files = _make_files([
            "src/components/Button.test.tsx",
            "src/utils/format.spec.ts",
        ])
        units = backend._discover_tests(files)
        assert len(units) == 1
        assert units[0].name == "tests-unit"

    def test_discovers_e2e_tests(self):
        """Test discovering e2e test files."""
        backend = _make_backend()
        files = _make_files([
            "cypress/e2e/login.test.ts",
            "e2e/dashboard.spec.ts",
        ])
        units = backend._discover_tests(files)
        assert len(units) >= 1
        names = [u.name for u in units]
        assert "tests-e2e" in names

    def test_discovers_integration_tests(self):
        """Test discovering integration test files."""
        backend = _make_backend()
        files = _make_files([
            "src/integration/api.test.ts",
        ])
        units = backend._discover_tests(files)
        assert len(units) == 1
        assert units[0].name == "tests-integration"

    def test_returns_empty_when_no_tests(self):
        """Test returns empty list when no tests found."""
        backend = _make_backend()
        files = _make_files(["src/handler.ts"])
        units = backend._discover_tests(files)
        assert units == []


# ############################################################################
# STORYBOOK DISCOVERY TESTS
# ############################################################################


class TestDiscoverStorybook:
    """Test Storybook story discovery."""

    def test_discovers_stories(self):
        """Test discovering Storybook story files."""
        backend = _make_backend()
        files = _make_files([
            "src/components/Button.stories.tsx",
            "src/components/Card.story.tsx",
        ])
        unit = backend._discover_storybook(files)
        assert unit is not None
        assert unit.name == "storybook"
        assert len(unit.file_paths) == 2

    def test_returns_none_when_no_stories(self):
        """Test returns None when no stories found."""
        backend = _make_backend()
        files = _make_files(["src/handler.ts"])
        unit = backend._discover_storybook(files)
        assert unit is None


# ############################################################################
# CONFIG FILES DISCOVERY TESTS
# ############################################################################


class TestDiscoverConfigFiles:
    """Test configuration file discovery."""

    def test_discovers_config_files(self):
        """Test discovering various config files."""
        backend = _make_backend()
        files = _make_files([
            "package.json",
            "tsconfig.json",
            "next.config.js",
            "tailwind.config.ts",
            "vitest.config.ts",
        ])
        unit = backend._discover_config_files(files)
        assert unit is not None
        assert unit.name == "config"
        assert len(unit.file_paths) == 5

    def test_returns_none_when_no_config(self):
        """Test returns None when no config files found."""
        backend = _make_backend()
        files = _make_files(["src/handler.ts"])
        unit = backend._discover_config_files(files)
        assert unit is None


# ############################################################################
# STORAGE KEY TESTS
# ############################################################################


class TestGetStorageKey:
    """Test storage key generation."""

    def test_components_key(self):
        """Test storage key for components."""
        backend = _make_backend()
        unit = CodeUnit(
            name="components-ui",
            unit_type="components",
            file_paths=["f.tsx"],
            metadata={"batch_type": "components", "component_category": "ui"}
        )
        assert backend.get_storage_key(unit) == "summary#components#ui"

    def test_pages_key(self):
        """Test storage key for pages."""
        backend = _make_backend()
        unit = CodeUnit(
            name="app-dashboard",
            unit_type="pages",
            file_paths=["f.tsx"],
            metadata={"batch_type": "pages"}
        )
        assert backend.get_storage_key(unit) == "summary#pages#app-dashboard"

    def test_utilities_key(self):
        """Test storage key for utilities."""
        backend = _make_backend()
        unit = CodeUnit(
            name="utils",
            unit_type="utilities",
            file_paths=["f.ts"],
            metadata={"batch_type": "utilities", "util_group": "utils"}
        )
        assert backend.get_storage_key(unit) == "summary#utils#utils"

    def test_tests_key(self):
        """Test storage key for tests."""
        backend = _make_backend()
        unit = CodeUnit(
            name="tests-unit",
            unit_type="tests",
            file_paths=["f.test.ts"],
            metadata={"batch_type": "tests", "test_type": "unit"}
        )
        assert backend.get_storage_key(unit) == "summary#tests#unit"

    def test_default_key(self):
        """Test storage key for other types."""
        backend = _make_backend()
        unit = CodeUnit(
            name="hooks",
            unit_type="hooks",
            file_paths=["f.ts"],
            metadata={"batch_type": "hooks"}
        )
        assert backend.get_storage_key(unit) == "summary#hooks"


# ############################################################################
# BATCH METADATA TESTS
# ############################################################################


class TestGenerateBatchMetadata:
    """Test batch metadata generation."""

    def test_generates_metadata(self):
        """Test batch metadata generation."""
        backend = _make_backend()
        unit = CodeUnit(
            name="hooks",
            unit_type="hooks",
            file_paths=["src/hooks/useAuth.ts"],
            metadata={"batch_type": "hooks"}
        )

        metadata = backend.generate_batch_metadata(unit, "test-repo")
        assert metadata["batch_type"] == "hooks"
        assert metadata["group_name"] == "hooks"
        assert metadata["backend_type"] == "react"
        assert metadata["storage_key"] == "summary#hooks"
        assert len(metadata["files"]) == 1


# ############################################################################
# FULL DISCOVERY PIPELINE TESTS
# ############################################################################


class TestDiscoverCodeUnits:
    """Test the full code unit discovery pipeline."""

    def test_discovers_all_unit_types(self):
        """Test full discovery with various file types."""
        backend = _make_backend()
        files = _make_files([
            # Components
            "src/components/ui/Button.tsx",
            # Hooks
            "src/hooks/useAuth.ts",
            # Contexts
            "src/contexts/AuthContext.tsx",
            # Pages
            "pages/index.tsx",
            # State management
            "src/store/index.ts",
            # Styles
            "src/styles/globals.css",
            # Types
            "src/types/user.ts",
            # Utils
            "src/utils/format.ts",
            # Tests
            "src/components/Button.test.tsx",
            # Stories
            "src/components/Button.stories.tsx",
            # Config
            "package.json",
            # Terraform
            "terraform/main.tf",
            # Docs
            "README.md",
        ])

        units = backend.discover_code_units(files)
        assert len(units) >= 5  # Should discover multiple unit types

        unit_types = set(u.unit_type for u in units)
        assert "hooks" in unit_types
        assert "contexts" in unit_types

    def test_excludes_node_modules(self):
        """Test that node_modules files are excluded."""
        backend = _make_backend()
        files = _make_files([
            "node_modules/react/index.js",
            "src/components/App.tsx",
        ])
        units = backend.discover_code_units(files)
        # node_modules files should not appear in any unit
        for unit in units:
            for fp in unit.file_paths:
                assert "node_modules" not in fp
