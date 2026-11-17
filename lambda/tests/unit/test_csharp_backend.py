# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for C# backend."""

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

# Load base module
base_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-repo-summary/backends/base.py',
)
spec = importlib.util.spec_from_file_location("backend_base", base_path)
base_module = importlib.util.module_from_spec(spec)
sys.modules['backend_base'] = base_module
sys.modules['backends.base'] = base_module
spec.loader.exec_module(base_module)

# Load C# backend (auto-loads factory via its own import-time auto-register)
csharp_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-repo-summary/backends/csharp_backend.py',
)
spec = importlib.util.spec_from_file_location("csharp_backend", csharp_path)
csharp_module = importlib.util.module_from_spec(spec)
sys.modules['csharp_backend'] = csharp_module
spec.loader.exec_module(csharp_module)

CodeUnit = base_module.CodeUnit
CSharpBackend = csharp_module.CSharpBackend


class TestCSharpBackendInitialization:
    """Test C# backend initialization."""

    def test_initialization_default_config(self):
        """Default config wires through token and uses default api url."""
        config = {"github_token": "test-token"}

        backend = CSharpBackend(config)

        assert backend.github_token == "test-token"
        assert backend.github_api_url == "https://api.github.com"
        assert backend.max_file_size_tokens == 7000

    def test_initialization_custom_config(self):
        """Custom config overrides defaults for url, source_roots, token size."""
        config = {
            "github_token": "custom-token",
            "github_api_url": "https://custom-api.github.com",
            "source_roots": ["src/", "Source/"],
            "max_file_size_tokens": 12000,
        }

        backend = CSharpBackend(config)

        assert backend.github_token == "custom-token"
        assert backend.github_api_url == "https://custom-api.github.com"
        assert backend.max_file_size_tokens == 12000
        assert backend.get_source_roots() == ["src/", "Source/"]

    def test_get_backend_name(self):
        """Backend name is 'C# / .NET Backend' per ADR-015."""
        backend = CSharpBackend({"github_token": "test"})

        assert backend.get_backend_name() == "C# / .NET Backend"

    def test_get_backend_type(self):
        """Backend type is 'csharp' per ADR-015."""
        backend = CSharpBackend({"github_token": "test"})

        assert backend.get_backend_type() == "csharp"

    def test_validate_config_success(self):
        """validate_config succeeds with github_token present."""
        backend = CSharpBackend({"github_token": "test-token"})

        is_valid, error = backend.validate_config()

        assert is_valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """validate_config fails when github_token is missing."""
        backend = CSharpBackend({})

        is_valid, error = backend.validate_config()

        assert is_valid is False
        assert "github_token is required" in error


class TestSourceConfiguration:
    """Test source extension and source root configuration."""

    def test_source_extensions_includes_cs(self):
        """Primary .cs extension is included."""
        backend = CSharpBackend({"github_token": "test"})

        assert ".cs" in backend.get_source_extensions()

    def test_source_extensions_includes_web(self):
        """Web Forms / Razor / WCF extensions are included."""
        backend = CSharpBackend({"github_token": "test"})
        exts = backend.get_source_extensions()

        assert ".cshtml" in exts
        assert ".aspx" in exts
        assert ".ascx" in exts
        assert ".ashx" in exts
        assert ".svc" in exts

    def test_source_extensions_excludes_fsharp(self):
        """F# is explicitly excluded per ADR-014."""
        backend = CSharpBackend({"github_token": "test"})

        assert ".fs" not in backend.get_source_extensions()

    def test_source_extensions_excludes_razor_blazor(self):
        """.razor (Blazor) is not in the C# backend's set."""
        backend = CSharpBackend({"github_token": "test"})

        assert ".razor" not in backend.get_source_extensions()

    def test_default_source_roots(self):
        """Default source roots include common .NET layouts."""
        backend = CSharpBackend({"github_token": "test"})

        roots = backend.get_source_roots()

        assert "src" in roots
        assert "source" in roots
        assert "server" in roots
        assert "web" in roots

    def test_configured_source_roots_override(self):
        """Configured source_roots override defaults."""
        backend = CSharpBackend({
            "github_token": "test",
            "source_roots": ["MyCustom/"],
        })

        assert backend.get_source_roots() == ["MyCustom/"]


class TestBuildModuleMarkers:
    """Test build module marker recognition."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_csproj_marks_module(self):
        """A directory containing a .csproj is a build module."""
        files = [
            {"path": "src/MyApp/MyApp.csproj", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("src/MyApp", files) is True

    def test_sln_marks_module(self):
        """A directory containing a .sln is a build module."""
        files = [
            {"path": "src/MyApp.sln", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("src", files) is True

    def test_directory_packages_props_marks_module(self):
        """Directory.Packages.props marks a build module."""
        files = [
            {"path": "src/Directory.Packages.props", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("src", files) is True

    def test_packages_config_marks_module(self):
        """packages.config marks a build module (legacy .NET Framework)."""
        files = [
            {"path": "src/MyApp/packages.config", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("src/MyApp", files) is True

    def test_global_json_marks_module(self):
        """global.json at repo root marks a build module."""
        files = [
            {"path": "global.json", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("", files) is True

    def test_random_xml_does_not_mark_module(self):
        """An arbitrary .xml file is not a build module marker."""
        files = [
            {"path": "src/MyConfig.xml", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("src", files) is False

    def test_inherited_pom_xml_still_marks(self):
        """Sanity: base BUILD_MODULE_MARKERS still respected."""
        files = [
            {"path": "module/pom.xml", "type": "blob"},
        ]

        assert self.backend.has_build_module_marker("module", files) is True


class TestExcludedPaths:
    """Test excluded path patterns."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_bin_excluded(self):
        assert self.backend.is_excluded_path("src/MyApp/bin/Debug/MyApp.dll") is True

    def test_obj_excluded(self):
        assert self.backend.is_excluded_path("src/MyApp/obj/Release/MyApp.csproj.assets") is True

    def test_packages_excluded(self):
        assert self.backend.is_excluded_path("packages/Newtonsoft.Json.13.0.1/lib/foo.dll") is True

    def test_vs_excluded(self):
        assert self.backend.is_excluded_path(".vs/MyApp/v17/.suo") is True

    def test_properties_excluded(self):
        assert self.backend.is_excluded_path("src/MyApp/Properties/AssemblyInfo.cs") is True

    def test_test_results_excluded(self):
        assert self.backend.is_excluded_path("TestResults/2024-01-01/coverage.xml") is True


class TestProjectDiscovery:
    """Test csharp-project unit discovery."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_single_project_one_unit(self):
        """One .csproj plus several .cs files should yield exactly one project unit."""
        files = [
            {"path": "src/MyApp.Web/MyApp.Web.csproj", "type": "blob"},
            {"path": "src/MyApp.Web/Program.cs", "type": "blob"},
            {"path": "src/MyApp.Web/Startup.cs", "type": "blob"},
            {"path": "src/MyApp.Web/Controllers/HomeController.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        project_units = [u for u in units if u.unit_type == "csharp-project"]
        assert len(project_units) == 1
        assert project_units[0].name == "MyApp.Web"

    def test_multiple_projects_separate_units(self):
        """Three sibling .csproj files yield three distinct project units, each scoped."""
        files = [
            {"path": "src/A/A.csproj", "type": "blob"},
            {"path": "src/A/A1.cs", "type": "blob"},
            {"path": "src/A/A2.cs", "type": "blob"},
            {"path": "src/B/B.csproj", "type": "blob"},
            {"path": "src/B/B1.cs", "type": "blob"},
            {"path": "src/B/B2.cs", "type": "blob"},
            {"path": "src/C/C.csproj", "type": "blob"},
            {"path": "src/C/C1.cs", "type": "blob"},
            {"path": "src/C/C2.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        project_units = [u for u in units if u.unit_type == "csharp-project"]
        assert len(project_units) == 3
        names = sorted(u.name for u in project_units)
        assert names == ["A", "B", "C"]

        a_unit = next(u for u in project_units if u.name == "A")
        for path in a_unit.file_paths:
            assert path.startswith("src/A/")

    def test_subdirectories_scoped_to_project(self):
        """Files in Controllers/ subdir belong to their containing project, not a global unit."""
        files = [
            {"path": "src/MyApp.Web/MyApp.Web.csproj", "type": "blob"},
            {"path": "src/MyApp.Web/Controllers/HomeController.cs", "type": "blob"},
            {"path": "src/MyApp.Web/Controllers/AccountController.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        project_units = [u for u in units if u.unit_type == "csharp-project"]
        assert len(project_units) == 1
        web_unit = project_units[0]
        assert web_unit.name == "MyApp.Web"
        assert "src/MyApp.Web/Controllers/HomeController.cs" in web_unit.file_paths
        assert "src/MyApp.Web/Controllers/AccountController.cs" in web_unit.file_paths

    def test_old_style_csproj_treated_same_as_sdk_style(self):
        """Old-style and SDK-style .csproj both yield a csharp-project unit."""
        # Old-style file structure (just looks like a normal .csproj from the discovery
        # perspective; the backend is filename-driven per ADR-017).
        old_files = [
            {"path": "src/Legacy/Legacy.csproj", "type": "blob"},
            {"path": "src/Legacy/Legacy.cs", "type": "blob"},
            {"path": "src/Legacy/Other.cs", "type": "blob"},
        ]
        sdk_files = [
            {"path": "src/Modern/Modern.csproj", "type": "blob"},
            {"path": "src/Modern/Modern.cs", "type": "blob"},
            {"path": "src/Modern/Program.cs", "type": "blob"},
        ]

        old_units = self.backend.discover_code_units(old_files)
        sdk_units = self.backend.discover_code_units(sdk_files)

        old_proj = [u for u in old_units if u.unit_type == "csharp-project"]
        sdk_proj = [u for u in sdk_units if u.unit_type == "csharp-project"]
        assert len(old_proj) == 1
        assert len(sdk_proj) == 1
        assert old_proj[0].unit_type == sdk_proj[0].unit_type

    def test_solution_root_only_no_units(self):
        """A repo with just a .sln (no .csproj, no .cs) yields zero csharp units."""
        files = [
            {"path": "MyApp.sln", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        csharp_units = [u for u in units if u.unit_type.startswith("csharp")]
        assert len(csharp_units) == 0


class TestMVCSubunits:
    """Test MVC subunit (controllers, services, views, models) discovery."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_controllers_subunit(self):
        """Project with multiple Controllers/*.cs emits a csharp-controllers unit."""
        files = [
            {"path": "src/Web/Web.csproj", "type": "blob"},
            {"path": "src/Web/Program.cs", "type": "blob"},
            {"path": "src/Web/Controllers/HomeController.cs", "type": "blob"},
            {"path": "src/Web/Controllers/AccountController.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        controllers = [u for u in units if u.unit_type == "csharp-controllers"]
        assert len(controllers) == 1
        assert controllers[0].name == "Web"
        assert all("Controllers/" in p for p in controllers[0].file_paths)

    def test_services_subunit(self):
        """Project with multiple Services/*.cs emits a csharp-services unit."""
        files = [
            {"path": "src/Web/Web.csproj", "type": "blob"},
            {"path": "src/Web/Program.cs", "type": "blob"},
            {"path": "src/Web/Services/UserService.cs", "type": "blob"},
            {"path": "src/Web/Services/EmailService.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        services = [u for u in units if u.unit_type == "csharp-services"]
        assert len(services) == 1
        assert services[0].name == "Web"

    def test_views_subunit_with_cshtml(self):
        """Views/ folder with .cshtml templates emits a csharp-views unit."""
        files = [
            {"path": "src/Web/Web.csproj", "type": "blob"},
            {"path": "src/Web/Program.cs", "type": "blob"},
            {"path": "src/Web/Views/Home/Index.cshtml", "type": "blob"},
            {"path": "src/Web/Views/Account/Login.cshtml", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        views = [u for u in units if u.unit_type == "csharp-views"]
        assert len(views) == 1
        assert views[0].name == "Web"
        assert any(p.endswith(".cshtml") for p in views[0].file_paths)

    def test_models_subunit(self):
        """Project with multiple Models/*.cs emits a csharp-models unit."""
        files = [
            {"path": "src/Web/Web.csproj", "type": "blob"},
            {"path": "src/Web/Program.cs", "type": "blob"},
            {"path": "src/Web/Models/User.cs", "type": "blob"},
            {"path": "src/Web/Models/Order.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        models = [u for u in units if u.unit_type == "csharp-models"]
        assert len(models) == 1
        assert models[0].name == "Web"

    def test_subunit_threshold(self):
        """A folder with only one matching file does NOT emit a subunit (default threshold is 2)."""
        files = [
            {"path": "src/Web/Web.csproj", "type": "blob"},
            {"path": "src/Web/Program.cs", "type": "blob"},
            {"path": "src/Web/Controllers/HomeController.cs", "type": "blob"},
        ]

        units = self.backend.discover_code_units(files)

        controllers = [u for u in units if u.unit_type == "csharp-controllers"]
        assert len(controllers) == 0


class TestTestProjectDiscovery:
    """Test test-project recognition per ADR-016."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def _discover(self, files):
        return self.backend.discover_code_units(files)

    def test_dot_tests_suffix_recognized(self):
        """A project named *.Tests is grouped as 'unit'."""
        files = [
            {"path": "tests/MyApp.Tests/MyApp.Tests.csproj", "type": "blob"},
            {"path": "tests/MyApp.Tests/UserServiceTests.cs", "type": "blob"},
        ]

        units = self._discover(files)

        test_units = [u for u in units if u.unit_type == "tests"]
        assert len(test_units) == 1
        assert test_units[0].name == "unit"

    def test_unittests_suffix_recognized(self):
        """A project named *.UnitTests is grouped as 'unit'."""
        files = [
            {"path": "tests/MyApp.UnitTests/MyApp.UnitTests.csproj", "type": "blob"},
            {"path": "tests/MyApp.UnitTests/Sample.cs", "type": "blob"},
        ]

        units = self._discover(files)

        test_units = [u for u in units if u.unit_type == "tests"]
        assert len(test_units) == 1
        assert test_units[0].name == "unit"

    def test_integrationtests_suffix_recognized(self):
        """A project named *.IntegrationTests is grouped as 'integration'."""
        files = [
            {"path": "tests/MyApp.IntegrationTests/MyApp.IntegrationTests.csproj", "type": "blob"},
            {"path": "tests/MyApp.IntegrationTests/Flow.cs", "type": "blob"},
        ]

        units = self._discover(files)

        groups = {u.name for u in units if u.unit_type == "tests"}
        assert "integration" in groups

    def test_e2etests_suffix_recognized(self):
        """A project named *.E2ETests is grouped as 'e2e'."""
        files = [
            {"path": "tests/MyApp.E2ETests/MyApp.E2ETests.csproj", "type": "blob"},
            {"path": "tests/MyApp.E2ETests/EndToEnd.cs", "type": "blob"},
        ]

        units = self._discover(files)

        groups = {u.name for u in units if u.unit_type == "tests"}
        assert "e2e" in groups

    def test_acceptancetests_suffix_recognized(self):
        """A project named *.AcceptanceTests is grouped as 'functional'."""
        files = [
            {"path": "tests/MyApp.AcceptanceTests/MyApp.AcceptanceTests.csproj", "type": "blob"},
            {"path": "tests/MyApp.AcceptanceTests/Acceptance.cs", "type": "blob"},
        ]

        units = self._discover(files)

        groups = {u.name for u in units if u.unit_type == "tests"}
        assert "functional" in groups

    def test_non_test_project_not_grouped(self):
        """A non-test project (e.g. MyApp.Web) is not classified as a test unit."""
        files = [
            {"path": "src/MyApp.Web/MyApp.Web.csproj", "type": "blob"},
            {"path": "src/MyApp.Web/Program.cs", "type": "blob"},
            {"path": "src/MyApp.Web/Startup.cs", "type": "blob"},
        ]

        units = self._discover(files)

        test_units = [u for u in units if u.unit_type == "tests"]
        assert test_units == []

    def test_test_project_metadata_includes_framework_field(self):
        """Even when the .csproj content is not scanned, framework is 'unknown'."""
        files = [
            {"path": "tests/MyApp.Tests/MyApp.Tests.csproj", "type": "blob"},
            {"path": "tests/MyApp.Tests/UserServiceTests.cs", "type": "blob"},
        ]

        units = self._discover(files)

        test_unit = next(u for u in units if u.unit_type == "tests")
        assert "framework" in test_unit.metadata
        assert test_unit.metadata["framework"] == "unknown"

    def test_test_project_storage_key_uses_summary_tests_prefix(self):
        """Test-unit storage keys match Python/Java convention: summary#tests#<group>."""
        files = [
            {"path": "tests/MyApp.Tests/MyApp.Tests.csproj", "type": "blob"},
            {"path": "tests/MyApp.Tests/Sample.cs", "type": "blob"},
        ]

        units = self._discover(files)

        test_unit = next(u for u in units if u.unit_type == "tests")
        key = self.backend.get_storage_key(test_unit)
        assert key == "summary#tests#unit"


class TestDetectChanges:
    """Test git-based change detection."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test-token"})

    @patch.object(CSharpBackend, "_get_current_commit_sha")
    def test_detect_changes_no_previous_state(self, mock_get_sha):
        """No previous state means full regeneration."""
        mock_get_sha.return_value = "abc123"

        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=None,
            force_full=False,
        )

        assert result.has_changes is True
        assert result.last_commit_sha is None
        assert result.current_commit_sha == "abc123"

    @patch.object(CSharpBackend, "_get_current_commit_sha")
    def test_detect_changes_force_full(self, mock_get_sha):
        """force_full=True triggers full regeneration even when state exists."""
        mock_get_sha.return_value = "def456"
        last_state = {"commit_sha": "abc123"}

        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=True,
        )

        assert result.has_changes is True
        assert result.last_commit_sha == "abc123"
        assert result.current_commit_sha == "def456"

    @patch.object(CSharpBackend, "_get_current_commit_sha")
    def test_detect_changes_no_changes(self, mock_get_sha):
        """Same commit SHA means no changes."""
        same_sha = "abc123def456"
        mock_get_sha.return_value = same_sha
        last_state = {"commit_sha": same_sha}

        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=False,
        )

        assert result.has_changes is False
        assert result.last_commit_sha == same_sha
        assert result.current_commit_sha == same_sha

    @patch.object(CSharpBackend, "_get_changed_files")
    @patch.object(CSharpBackend, "_get_current_commit_sha")
    def test_detect_changes_with_changes(self, mock_get_sha, mock_get_files):
        """Different SHAs trigger changed-file lookup."""
        mock_get_sha.return_value = "new_sha"
        mock_get_files.return_value = (
            ["src/MyApp.Web/Program.cs", "src/MyApp.Web/Startup.cs"],
            [],
        )
        last_state = {"commit_sha": "old_sha"}

        result = self.backend.detect_changes(
            repo="test-repo",
            repo_project="owner/test-repo",
            last_state=last_state,
            force_full=False,
        )

        assert result.has_changes is True
        assert result.last_commit_sha == "old_sha"
        assert result.current_commit_sha == "new_sha"
        mock_get_files.assert_called_once_with("owner/test-repo", "old_sha", "new_sha")


class TestGenerateBatchMetadata:
    """Test batch metadata generation."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_metadata_for_csharp_project(self):
        """csharp-project metadata carries language=csharp, framework=unknown, backend_type=csharp."""
        unit = CodeUnit(
            name="MyApp.Web",
            unit_type="csharp-project",
            file_paths=["src/MyApp.Web/MyApp.Web.csproj", "src/MyApp.Web/Program.cs"],
            metadata={
                "batch_type": "csharp-project",
                "project": "MyApp.Web",
                "framework": "unknown",
            },
        )

        meta = self.backend.generate_batch_metadata(unit, "test-repo")

        assert meta["batch_type"] == "csharp-project"
        assert meta["language"] == "csharp"
        assert meta["framework"] == "unknown"
        assert meta["backend_type"] == "csharp"
        assert meta["project"] == "MyApp.Web"
        assert meta["group_name"] == "MyApp.Web"
        assert meta["storage_key"] == "summary#csharp#project#MyApp.Web"

    def test_metadata_for_test_project_carries_framework(self):
        """When the CodeUnit metadata has a framework, it survives into batch metadata."""
        unit = CodeUnit(
            name="unit",
            unit_type="tests",
            file_paths=["tests/MyApp.Tests/UserServiceTests.cs"],
            metadata={
                "batch_type": "tests",
                "test_type": "unit",
                "language": "csharp",
                "framework": "xunit",
                "projects": ["MyApp.Tests"],
            },
        )

        meta = self.backend.generate_batch_metadata(unit, "test-repo")

        assert meta["batch_type"] == "tests"
        assert meta["framework"] == "xunit"
        assert meta["language"] == "csharp"
        assert meta["backend_type"] == "csharp"

    def test_metadata_for_infrastructure(self):
        """Infrastructure metadata has no project field set."""
        unit = CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=["terraform/main.tf"],
            metadata={"batch_type": "infrastructure"},
        )

        meta = self.backend.generate_batch_metadata(unit, "test-repo")

        assert meta["batch_type"] == "infrastructure"
        assert meta["storage_key"] == "summary#infrastructure"
        assert meta.get("project") is None


class TestStorageKeys:
    """Test storage key generation per ADR-015."""

    def setup_method(self):
        self.backend = CSharpBackend({"github_token": "test"})

    def test_storage_key_csharp_project(self):
        """csharp-project key is summary#csharp#project#<name>."""
        unit = CodeUnit(
            name="MyApp.Web",
            unit_type="csharp-project",
            file_paths=[],
            metadata={"batch_type": "csharp-project", "project": "MyApp.Web"},
        )

        assert self.backend.get_storage_key(unit) == "summary#csharp#project#MyApp.Web"

    def test_storage_key_csharp_controllers(self):
        """csharp-controllers key is summary#csharp#controllers#<project>."""
        unit = CodeUnit(
            name="MyApp.Web",
            unit_type="csharp-controllers",
            file_paths=[],
            metadata={"batch_type": "csharp-controllers", "project": "MyApp.Web"},
        )

        assert self.backend.get_storage_key(unit) == "summary#csharp#controllers#MyApp.Web"

    def test_storage_key_csharp_services(self):
        """csharp-services key is summary#csharp#services#<project>."""
        unit = CodeUnit(
            name="MyApp.Web",
            unit_type="csharp-services",
            file_paths=[],
            metadata={"batch_type": "csharp-services", "project": "MyApp.Web"},
        )

        assert self.backend.get_storage_key(unit) == "summary#csharp#services#MyApp.Web"

    def test_storage_key_csharp_views(self):
        """csharp-views key is summary#csharp#views#<project>."""
        unit = CodeUnit(
            name="MyApp.Web",
            unit_type="csharp-views",
            file_paths=[],
            metadata={"batch_type": "csharp-views", "project": "MyApp.Web"},
        )

        assert self.backend.get_storage_key(unit) == "summary#csharp#views#MyApp.Web"

    def test_storage_key_tests_unit(self):
        """tests storage key has no csharp infix: summary#tests#<group>."""
        unit = CodeUnit(
            name="unit",
            unit_type="tests",
            file_paths=[],
            metadata={"batch_type": "tests"},
        )

        assert self.backend.get_storage_key(unit) == "summary#tests#unit"

    def test_storage_key_infrastructure(self):
        """infrastructure storage key is summary#infrastructure."""
        unit = CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=[],
            metadata={"batch_type": "infrastructure"},
        )

        assert self.backend.get_storage_key(unit) == "summary#infrastructure"
