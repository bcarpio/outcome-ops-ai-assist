# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ABAP backend."""

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

abap_backend_path = os.path.join(os.path.dirname(__file__), '../../process-repo-summary/backends/abap_backend.py')
spec = importlib.util.spec_from_file_location("abap_backend", abap_backend_path)
abap_backend_module = importlib.util.module_from_spec(spec)
sys.modules['abap_backend'] = abap_backend_module
spec.loader.exec_module(abap_backend_module)

# Import classes
CodeUnit = base_module.CodeUnit
ABAPBackend = abap_backend_module.ABAPBackend


class TestABAPBackendInitialization:
    """Test ABAP backend initialization."""

    def test_initialization_default_config(self):
        """Test initialization with default configuration."""
        # Arrange
        config = {"github_token": "test-token"}

        # Act
        backend = ABAPBackend(config)

        # Assert
        assert backend.github_token == "test-token"
        assert backend.github_api_url == "https://api.github.com"

    def test_initialization_custom_config(self):
        """Test initialization with custom configuration."""
        # Arrange
        config = {
            "github_token": "custom-token",
            "github_api_url": "https://custom-api.github.com",
            "source_roots": ["custom/src"],
        }

        # Act
        backend = ABAPBackend(config)

        # Assert
        assert backend.github_token == "custom-token"
        assert backend.github_api_url == "https://custom-api.github.com"

    def test_get_backend_name(self):
        """Test get_backend_name returns correct name."""
        # Arrange
        backend = ABAPBackend({"github_token": "test"})

        # Act
        name = backend.get_backend_name()

        # Assert
        assert name == "SAP ABAP Backend"

    def test_get_backend_type(self):
        """Test get_backend_type returns 'abap'."""
        # Arrange
        backend = ABAPBackend({"github_token": "test"})

        # Act
        backend_type = backend.get_backend_type()

        # Assert
        assert backend_type == "abap"

    def test_validate_config_success(self):
        """Test validate_config with valid configuration."""
        # Arrange
        backend = ABAPBackend({"github_token": "test-token"})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is True
        assert error is None

    def test_validate_config_missing_token(self):
        """Test validate_config fails when github_token is missing."""
        # Arrange
        backend = ABAPBackend({})

        # Act
        is_valid, error = backend.validate_config()

        # Assert
        assert is_valid is False
        assert "github_token is required" in error


class TestLanguageConfiguration:
    """Test language configuration methods."""

    def test_get_source_extensions(self):
        """Test source extensions include ABAP and CDS files but not XML."""
        # Arrange
        backend = ABAPBackend({"github_token": "test"})

        # Act
        extensions = backend.get_source_extensions()

        # Assert
        assert ".abap" in extensions
        assert ".asddls" in extensions
        # XML should NOT be included - it's metadata, not source code
        assert ".xml" not in extensions

    def test_get_source_roots_default(self):
        """Test default source roots follow abapGit conventions."""
        # Arrange
        backend = ABAPBackend({"github_token": "test"})

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert "src" in roots

    def test_get_source_roots_custom(self):
        """Test custom source roots override defaults."""
        # Arrange
        backend = ABAPBackend({
            "github_token": "test",
            "source_roots": ["custom/abap", "other/src"]
        })

        # Act
        roots = backend.get_source_roots()

        # Assert
        assert roots == ["custom/abap", "other/src"]


class TestABAPObjectTypeDetection:
    """Test ABAP object type detection from filenames."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_detect_class_type(self):
        """Test detecting ABAP class from filename."""
        assert self.backend.get_abap_object_type("src/zcl_flight.clas.abap") == "class"

    def test_detect_interface_type(self):
        """Test detecting ABAP interface from filename."""
        assert self.backend.get_abap_object_type("src/zif_flight.intf.abap") == "interface"

    def test_detect_function_group_type(self):
        """Test detecting function group from filename."""
        assert self.backend.get_abap_object_type("src/zfm_travel.fugr.abap") == "function_group"

    def test_detect_cds_view_type(self):
        """Test detecting CDS view from filename."""
        assert self.backend.get_abap_object_type("src/zi_agency.ddls.asddls") == "cds_view"

    def test_detect_program_type(self):
        """Test detecting ABAP program from filename."""
        assert self.backend.get_abap_object_type("src/zreport.prog.abap") == "program"

    def test_detect_service_definition(self):
        """Test detecting service definition from filename."""
        assert self.backend.get_abap_object_type("src/zsrv.srvd.srvdsrv") == "service_definition"

    def test_fallback_generic_abap(self):
        """Test fallback for generic .abap files."""
        assert self.backend.get_abap_object_type("src/unknown.abap") == "unknown_abap"

    def test_fallback_generic_asddls(self):
        """Test fallback for generic .asddls files."""
        assert self.backend.get_abap_object_type("src/unknown.asddls") == "cds_view"

    def test_returns_none_for_non_abap(self):
        """Test returns None for non-ABAP files."""
        assert self.backend.get_abap_object_type("src/readme.md") is None
        assert self.backend.get_abap_object_type("src/config.xml") is None

    def test_analyze_object_types(self):
        """Test analyzing multiple files for object types."""
        files = [
            "src/zcl_flight.clas.abap",
            "src/zcl_booking.clas.abap",
            "src/zif_flight.intf.abap",
            "src/zi_agency.ddls.asddls",
        ]

        result = self.backend.analyze_object_types(files)

        assert result["total_objects"] == 4
        assert result["object_type_counts"]["class"] == 2
        assert result["object_type_counts"]["interface"] == 1
        assert result["object_type_counts"]["cds_view"] == 1
        assert len(result["files_by_type"]["class"]) == 2


class TestDiscoverCodeUnits:
    """Test code unit discovery using ADR-009 algorithm."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_discover_abap_packages_by_file_count(self):
        """Test discovering code units by file count threshold."""
        # Arrange - simulate abapGit structure with multiple ABAP files
        files = [
            {"path": "src/legacy/zcl_flight_legacy.clas.abap", "type": "blob"},
            {"path": "src/legacy/zif_flight_legacy.intf.abap", "type": "blob"},
            {"path": "src/legacy/zcx_flight_legacy.clas.abap", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert len(source_units[0].file_paths) == 3

    def test_discover_includes_object_type_metadata(self):
        """Test that discovered units include ABAP object type metadata."""
        # Arrange
        files = [
            {"path": "src/legacy/zcl_flight.clas.abap", "type": "blob"},
            {"path": "src/legacy/zif_flight.intf.abap", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert "abap_objects" in source_units[0].metadata
        assert source_units[0].metadata["abap_objects"]["total_objects"] == 2
        assert source_units[0].metadata["abap_objects"]["object_type_counts"]["class"] == 1
        assert source_units[0].metadata["abap_objects"]["object_type_counts"]["interface"] == 1

    def test_discover_multiple_packages(self):
        """Test discovering multiple separate code units."""
        # Arrange - simulate the reference project structure
        files = [
            # legacy package - 3 files
            {"path": "src/legacy/zcl_flight_legacy.clas.abap", "type": "blob"},
            {"path": "src/legacy/zif_flight_legacy.intf.abap", "type": "blob"},
            {"path": "src/legacy/zcx_flight_legacy.clas.abap", "type": "blob"},
            # reuse package - 2 files
            {"path": "src/reuse/zi_agency.ddls.asddls", "type": "blob"},
            {"path": "src/reuse/zi_airport.ddls.asddls", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 2

    def test_discover_cds_views(self):
        """Test discovering CDS view files (.asddls)."""
        # Arrange
        files = [
            {"path": "src/readonly/zi_connection_r.ddls.asddls", "type": "blob"},
            {"path": "src/readonly/zi_flight_r.ddls.asddls", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 1
        assert all(path.endswith(".asddls") for path in source_units[0].file_paths)

    def test_discover_excludes_xml_metadata(self):
        """Test that XML metadata files are not included in source units."""
        # Arrange - XML files should be excluded from code-maps
        files = [
            {"path": "src/legacy/zcl_flight.clas.abap", "type": "blob"},
            {"path": "src/legacy/zcl_flight.clas.xml", "type": "blob"},
            {"path": "src/legacy/zif_flight.intf.abap", "type": "blob"},
            {"path": "src/legacy/zif_flight.intf.xml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        for unit in source_units:
            # No XML files should be in source code units
            assert not any(path.endswith(".xml") for path in unit.file_paths)

    def test_discover_empty_files(self):
        """Test discovery with no files."""
        # Arrange
        files = []

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        assert code_units == []

    def test_discover_no_abap_files(self):
        """Test discovery with no ABAP source files."""
        # Arrange - only XML metadata files
        files = [
            {"path": "src/legacy/zcl_flight.clas.xml", "type": "blob"},
            {"path": "src/legacy/package.devc.xml", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        source_units = [u for u in code_units if u.unit_type == "source-module"]
        assert len(source_units) == 0


class TestDiscoverInfrastructure:
    """Test infrastructure file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_discover_terraform(self):
        """Test discovering Terraform files."""
        # Arrange
        files = [
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "terraform/variables.tf", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        infra_units = [u for u in code_units if u.unit_type == "infrastructure"]
        assert len(infra_units) == 1
        assert len(infra_units[0].file_paths) == 2


class TestDiscoverDocumentation:
    """Test documentation file discovery."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_discover_markdown_docs(self):
        """Test discovering markdown documentation files."""
        # Arrange
        files = [
            {"path": "README.md", "type": "blob"},
            {"path": "docs/architecture.md", "type": "blob"},
        ]

        # Act
        code_units = self.backend.discover_code_units(files)

        # Assert
        docs_units = [u for u in code_units if u.unit_type == "docs"]
        assert len(docs_units) == 1
        assert len(docs_units[0].file_paths) == 2


class TestDetectChanges:
    """Test change detection."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    @patch.object(ABAPBackend, "_get_current_commit_sha")
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

    @patch.object(ABAPBackend, "_get_current_commit_sha")
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

    @patch.object(ABAPBackend, "_get_changed_files")
    @patch.object(ABAPBackend, "_get_current_commit_sha")
    def test_detect_changes_with_changes(self, mock_get_sha, mock_get_files):
        """Test change detection when commit SHA has changed."""
        # Arrange
        mock_get_sha.return_value = "new_sha"
        mock_get_files.return_value = (["src/legacy/zcl_flight.clas.abap"], [])
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
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_generate_batch_metadata_source_module(self):
        """Test generating metadata for source module code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="legacy",
            unit_type="source-module",
            file_paths=["src/legacy/zcl_flight.clas.abap"],
            metadata={"batch_type": "source-module"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "source-module"
        assert metadata["group_name"] == "legacy"
        assert metadata["storage_key"] == "summary#abap-module#legacy"
        assert metadata["backend_type"] == "abap"

    def test_generate_batch_metadata_tests(self):
        """Test generating metadata for test code unit."""
        # Arrange
        code_unit = CodeUnit(
            name="tests-unit",
            unit_type="tests",
            file_paths=["src/test/zcl_flight_test.clas.abap"],
            metadata={"batch_type": "tests"}
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert metadata["batch_type"] == "tests"
        assert metadata["storage_key"] == "summary#tests#tests-unit"

    def test_generate_batch_metadata_includes_abap_objects(self):
        """Test that batch metadata includes ABAP object type information."""
        # Arrange
        code_unit = CodeUnit(
            name="legacy",
            unit_type="source-module",
            file_paths=["src/legacy/zcl_flight.clas.abap", "src/legacy/zif_flight.intf.abap"],
            metadata={
                "batch_type": "source-module",
                "abap_objects": {
                    "object_type_counts": {"class": 1, "interface": 1},
                    "total_objects": 2,
                }
            }
        )

        # Act
        metadata = self.backend.generate_batch_metadata(code_unit, "test-repo")

        # Assert
        assert "abap_objects" in metadata
        assert metadata["abap_objects"]["total_objects"] == 2
        assert metadata["abap_objects"]["object_type_counts"]["class"] == 1


class TestGetStorageKey:
    """Test storage key generation."""

    def setup_method(self):
        """Set up test backend."""
        self.backend = ABAPBackend({"github_token": "test-token"})

    def test_get_storage_key_source_module(self):
        """Test storage key for source-module."""
        # Arrange
        code_unit = CodeUnit(
            name="legacy",
            unit_type="source-module",
            file_paths=[],
            metadata={"batch_type": "source-module"}
        )

        # Act
        key = self.backend.get_storage_key(code_unit)

        # Assert
        assert key == "summary#abap-module#legacy"

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
