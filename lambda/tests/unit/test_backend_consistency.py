# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Tests for backend naming consistency across Lambda functions.

This test ensures that backend names used in .outcomeops.yaml configurations
work consistently across all Lambda functions that use backends:
- generate-code-maps: Discovers code units and generates summaries
- generate-code: Generates code from plans

The mapping between config values and internal backend names:
- "typescript" -> typescript backend in both systems (ALIGNED)
- "java" -> java backend in both systems (ALIGNED)
- "python" -> python backend in both systems (ALIGNED)
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Add lambda directories to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../generate-code'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../process-repo-summary'))

# Mock boto3 before importing modules that create clients at import time
mock_boto3_client = MagicMock()
with patch('boto3.client', return_value=mock_boto3_client):
    from codegen_backends import list_backends as list_codegen_backends
    from codegen_backends.factory import get_backend_for_repo
    from backends import list_backends as list_codemaps_backends
    from models import ExecutionPlan


# =============================================================================
# Backend Configuration Mapping
# =============================================================================

# These are the backend names that users can specify in .outcomeops.yaml
# and their expected availability in each system.
#
# Format: config_value -> (available_in_generate_code_maps, available_in_generate_code)
# NOTE: All backends are now aligned to use consistent short names
EXPECTED_BACKEND_AVAILABILITY = {
    # TypeScript - consistent naming across both systems
    "typescript": (True, True),

    # Java - consistent naming across both systems
    "java": (True, True),

    # Python - consistent naming across both systems
    "python": (True, True),

    # React - consistent naming across both systems (React/Next.js frontend)
    "react": (True, True),

    # ABAP - currently only in generate-code-maps (code generation backend to be added later)
    "abap": (True, False),

    # C# / .NET - code-maps only for v1 (code generation backend intentionally
    # out of scope; see plan/overview.md and ADR-015). Mirrors the ABAP pattern.
    "csharp": (True, False),

    # Go - code-maps only (symbol-graph + code-unit discovery via the Go
    # package-per-directory backend). No code-generation backend yet; mirrors
    # the ABAP / C# pattern.
    "go": (True, False),
}


class TestBackendConsistency:
    """Test that backend naming is consistent across systems."""

    def test_typescript_backend_available_in_both_systems(self):
        """Test: 'typescript' backend is available in both generate-code and generate-code-maps."""
        # Get available backends
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # TypeScript should be available in both
        assert "typescript" in codegen_names, \
            f"'typescript' not in generate-code backends: {codegen_names}"
        assert "typescript" in codemaps_names, \
            f"'typescript' not in generate-code-maps backends: {codemaps_names}"

    def test_all_codegen_backends_documented(self):
        """Test: All generate-code backends are documented in EXPECTED_BACKEND_AVAILABILITY."""
        codegen_backends = list_codegen_backends()

        for backend_name in codegen_backends.keys():
            assert backend_name in EXPECTED_BACKEND_AVAILABILITY, \
                f"Backend '{backend_name}' not documented in EXPECTED_BACKEND_AVAILABILITY. " \
                f"Please add it to the mapping."

    def test_all_codemaps_backends_documented(self):
        """Test: All generate-code-maps backends are documented in EXPECTED_BACKEND_AVAILABILITY."""
        codemaps_backends = list_codemaps_backends()

        for backend_info in codemaps_backends:
            backend_name = backend_info["name"]
            assert backend_name in EXPECTED_BACKEND_AVAILABILITY, \
                f"Backend '{backend_name}' not documented in EXPECTED_BACKEND_AVAILABILITY. " \
                f"Please add it to the mapping."

    def test_backend_availability_matches_expected(self):
        """Test: Backend availability matches expected mapping."""
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        for backend_name, (expected_codemaps, expected_codegen) in EXPECTED_BACKEND_AVAILABILITY.items():
            actual_codemaps = backend_name in codemaps_names
            actual_codegen = backend_name in codegen_names

            if expected_codemaps:
                assert actual_codemaps, \
                    f"Backend '{backend_name}' expected in generate-code-maps but not found"
            if expected_codegen:
                assert actual_codegen, \
                    f"Backend '{backend_name}' expected in generate-code but not found"


class TestBackendNamingConventions:
    """Test backend naming conventions and recommendations."""

    def test_consistent_backends_use_same_name(self):
        """
        Test: Backends that exist in both systems should use the same name.

        This is the key consistency check. If a backend name works in both
        generate-code-maps and generate-code, users can use one config value
        for both systems.
        """
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # Find backends that exist in both systems
        shared_backends = codegen_names & codemaps_names

        # All three backends should be shared after alignment
        assert "typescript" in shared_backends, \
            "'typescript' should be available in both systems"
        assert "java" in shared_backends, \
            "'java' should be available in both systems"
        assert "python" in shared_backends, \
            "'python' should be available in both systems"

        # Document the shared backends
        print(f"\nBackends available in both systems: {shared_backends}")
        print(f"  generate-code only: {codegen_names - codemaps_names}")
        print(f"  generate-code-maps only: {codemaps_names - codegen_names}")

    def test_recommended_config_values(self):
        """
        Test: Document recommended .outcomeops.yaml backend values.

        Users should use these values in their .outcomeops.yaml files:
        - typescript: For TypeScript/Node.js projects (works in both systems)
        - java: For Java projects (works in both systems)
        - python: For Python projects (works in both systems)
        """
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # Verify recommended values exist in both systems
        assert "typescript" in codegen_names
        assert "typescript" in codemaps_names
        assert "java" in codegen_names
        assert "java" in codemaps_names
        assert "python" in codegen_names
        assert "python" in codemaps_names


class TestBackendAlignment:
    """
    Tests that verify all backends are aligned across both systems.

    After alignment work, all backends use consistent short names:
    - TypeScript: 'typescript' in both systems
    - Java: 'java' in both systems
    - Python: 'python' in both systems
    """

    def test_python_backend_is_aligned(self):
        """
        Test: Verify Python backend is now aligned across both systems.

        After alignment:
        - generate-code-maps: 'python'
        - generate-code: 'python'
        """
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # Verify Python is aligned in both systems
        assert "python" in codegen_names, \
            "'python' should be in generate-code backends"
        assert "python" in codemaps_names, \
            "'python' should be in generate-code-maps backends"

    def test_java_backend_is_aligned(self):
        """
        Test: Verify Java backend is now aligned across both systems.

        After alignment:
        - generate-code-maps: 'java'
        - generate-code: 'java'
        """
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # Verify Java is aligned in both systems
        assert "java" in codegen_names, \
            "'java' should be in generate-code backends"
        assert "java" in codemaps_names, \
            "'java' should be in generate-code-maps backends"

    def test_typescript_backend_is_aligned(self):
        """
        Test: Verify TypeScript backend is now aligned across both systems.

        After alignment:
        - generate-code-maps: 'typescript'
        - generate-code: 'typescript'
        """
        codegen_backends = list_codegen_backends()
        codemaps_backends = list_codemaps_backends()

        codegen_names = set(codegen_backends.keys())
        codemaps_names = {b["name"] for b in codemaps_backends}

        # Verify TypeScript is aligned in both systems
        assert "typescript" in codegen_names, \
            "'typescript' should be in generate-code backends"
        assert "typescript" in codemaps_names, \
            "'typescript' should be in generate-code-maps backends"


class TestModelDefaults:
    """
    Tests that verify Pydantic model defaults use valid backend names.

    This prevents bugs where model defaults reference old/invalid backend names
    that don't exist in the registry.
    """

    def test_execution_plan_default_backend_is_valid(self):
        """
        Test: ExecutionPlan.backend_name default is a valid registered backend.

        This test catches the bug where ExecutionPlan had default="python-lambda"
        but the backend registry only had "python" after ADR-010 alignment.
        """
        codegen_backends = list_codegen_backends()
        available_backends = set(codegen_backends.keys())

        # Create a plan with defaults
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test issue",
            issue_description="Test description",
            repo_full_name="test/repo",
            branch_name="test-branch",
            steps=[],
        )

        # The default backend_name must be a valid registered backend
        assert plan.backend_name in available_backends, \
            f"ExecutionPlan default backend_name '{plan.backend_name}' " \
            f"is not a registered backend. Available: {available_backends}"

    def test_factory_default_backend_is_valid(self):
        """
        Test: get_backend_for_repo default parameter is a valid registered backend.

        This ensures the factory function's default matches a registered backend.
        """
        codegen_backends = list_codegen_backends()
        available_backends = set(codegen_backends.keys())

        # Get backend with no config (uses default)
        backend = get_backend_for_repo(repo_config=None)

        # The backend name must be registered
        assert backend.name in available_backends, \
            f"Factory default backend '{backend.name}' " \
            f"is not a registered backend. Available: {available_backends}"

    def test_model_and_factory_defaults_match(self):
        """
        Test: ExecutionPlan default and factory default should match.

        If these defaults diverge, it can cause confusion and bugs.
        """
        # Get model default
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test issue",
            issue_description="Test description",
            repo_full_name="test/repo",
            branch_name="test-branch",
            steps=[],
        )
        model_default = plan.backend_name

        # Get factory default
        backend = get_backend_for_repo(repo_config=None)
        factory_default = backend.name

        assert model_default == factory_default, \
            f"Model default '{model_default}' != factory default '{factory_default}'. " \
            f"These should match to avoid confusion."
