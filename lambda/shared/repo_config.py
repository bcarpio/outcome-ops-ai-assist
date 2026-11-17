# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Canonical repository configuration schema.

This module is the single source of truth for ``.outcomeops.yaml`` parsing
and validation. It lives under ``lambda/shared/`` because multiple Lambdas
consume it:

* ``generate-code`` — uses ``RepoConfig`` to choose a code-generation
  backend and merge per-repo guidelines into prompts.
* ``process-batch-summary`` — uses ``ingestion.symbol_graph`` to opt
  repos in/out of structural extraction.

All input validation goes through Pydantic v2 BaseModels so callers get
typed access, default fallbacks, and ``ValidationError`` on malformed
configs. PyYAML only handles the surface-level deserialization; the
schema layer is Pydantic so the OutcomeOps standards (input validation
MUST use Pydantic) hold for every consumer.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    so module load order (check_handlers before sys.path fix) is safe."""
    from shared.github_base import github_api_base
    return github_api_base()


logger = logging.getLogger(__name__)


# =============================================================================
# Configuration Schema
# =============================================================================


class CodeGenerationConfig(BaseModel):
    """Code generation configuration."""

    # Backend to use (e.g., 'python', 'java', 'typescript')
    backend: str = Field(default="python")

    # Source-root directories for code-unit discovery. Overrides the backend's
    # default roots so a repo not laid out like the platform (e.g. a package at
    # the repo root, or a custom layout) is still discovered. ``None`` = use the
    # backend defaults.
    source_roots: Optional[List[str]] = None

    # Additional KB queries to include in every plan
    additional_kb_queries: List[str] = Field(default_factory=list)

    # Custom prompts to append to system prompts
    custom_guidelines: Optional[str] = None

    # File patterns to exclude from generation
    exclude_patterns: List[str] = Field(default_factory=list)


class TestingConfig(BaseModel):
    """Testing configuration."""

    # Test framework (pytest, junit, jest, etc.)
    framework: str = Field(default="pytest")

    # Test directory pattern
    test_directory: str = Field(default="tests/unit")

    # Minimum coverage requirement
    min_coverage: int = Field(default=80)


class InfrastructureConfig(BaseModel):
    """Infrastructure configuration."""

    # IaC tool (terraform, cloudformation, pulumi)
    tool: str = Field(default="terraform")

    # Infrastructure directory
    directory: str = Field(default="terraform")


class RequiredAdrsConfig(BaseModel):
    """
    Required ADRs configuration.

    Specifies ADRs that must always be included in code generation context.
    - repo: ADRs from the target repository (searched first)
    - global: ADRs from organization-wide ADR repositories
    """

    # ADRs from the target repository
    repo: List[str] = Field(default_factory=list)

    # ADRs from global/organization ADR repositories
    global_adrs: List[str] = Field(default_factory=list, alias="global")


# Languages recognized by the symbol-graph extractor.
# Kept in sync with ``lambda/process-batch-summary/extractor/__init__.py``
# (each side-effect import there registers exactly one of these names).
# Per-language strategy decisions: see ADR-017 (C# regex extractor) and
# ADR-020 (ABAP hybrid parser).
_SYMBOL_GRAPH_LANGUAGES = {
    "python",
    "typescript",
    "java",
    "go",
    "csharp",
    "abap",
}


class SymbolGraphConfig(BaseModel):
    """
    Symbol-graph extraction configuration.

    Lets a repository opt in or out of structural fact extraction without
    re-deploying the platform Lambda. Validated via Pydantic; callers
    catch ``ValidationError`` and fall back to a default permissive
    filter so a malformed knob never blocks the batch-summary path.

    Precedence rules (applied in ``SymbolGraphFilter.should_extract``):

    1. ``enabled=False`` short-circuits — no facts emitted for any file.
    2. ADR-009 base path excludes (``EXCLUDED_PATH_FRAGMENTS``) win over
       every repo-level setting; the extractor MUST agree with the
       code-unit discoverer on which files are in scope.
    3. ``exclude`` patterns beat ``include`` patterns. An empty
       ``include`` list means "include everything that isn't excluded".
    4. ``languages`` is an allow-list; an empty / unset value means "all
       registered languages".
    5. ``max_file_bytes`` caps per-file source size — files larger than
       the cap are skipped (Tree-sitter cost grows with input).
    """

    # Master switch. When False the filter rejects every file.
    enabled: bool = True

    # Repo-relative glob patterns to include. Empty list = include all.
    include: List[str] = Field(default_factory=list)

    # Repo-relative glob patterns to exclude. Beats ``include``.
    exclude: List[str] = Field(default_factory=list)

    # Hard cap on per-file source size in bytes. 0 = no cap.
    # Default 512 KiB matches the spike-validated Tree-sitter budget.
    max_file_bytes: int = Field(default=524288, ge=0)

    # Language allow-list. ``None`` = all registered languages.
    # Must be a subset of the extractor's registered language set.
    languages: Optional[List[str]] = None

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        bad = set(v) - _SYMBOL_GRAPH_LANGUAGES
        if bad:
            raise ValueError(
                "unknown symbol_graph languages: "
                f"{sorted(bad)}; must be a subset of "
                f"{sorted(_SYMBOL_GRAPH_LANGUAGES)}"
            )
        return v


class IngestionConfig(BaseModel):
    """
    Document ingestion configuration.

    Controls which paths are ingested and how they are classified.
    This allows repos to customize where their standards and docs live.
    """

    # Paths to ingest as standards/ADRs (doc_type = "adr")
    # Files in these paths are treated as authoritative standards for code generation
    # Defaults to ["docs/adr"]
    standards_paths: List[str] = Field(default_factory=lambda: ["docs/adr"])

    # Paths to ingest as documentation (doc_type = "doc")
    # Files in these paths are general documentation, not standards
    # Defaults to ["docs"]
    docs_paths: List[str] = Field(default_factory=lambda: ["docs"])

    # Symbol-graph extraction knobs.
    symbol_graph: SymbolGraphConfig = Field(default_factory=SymbolGraphConfig)


class RepoConfig(BaseModel):
    """
    Repository configuration schema.

    This is the schema for .outcomeops.yaml files in repositories.
    """

    # Schema version for future compatibility
    version: str = Field(default="1.0")

    # Code generation settings
    code_generation: CodeGenerationConfig = Field(
        default_factory=CodeGenerationConfig
    )

    # Testing settings
    testing: TestingConfig = Field(default_factory=TestingConfig)

    # Infrastructure settings
    infrastructure: InfrastructureConfig = Field(
        default_factory=InfrastructureConfig
    )

    # ADRs to always include in context
    required_adrs: RequiredAdrsConfig = Field(default_factory=RequiredAdrsConfig)

    # Document ingestion settings
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)

    # Project-specific context to include in prompts
    project_context: Optional[str] = None


# =============================================================================
# Configuration Loader
# =============================================================================


def parse_repo_config(yaml_content: str) -> RepoConfig:
    """
    Parse repository configuration from YAML content.

    Args:
        yaml_content: YAML string content

    Returns:
        RepoConfig instance

    Raises:
        ValueError: If YAML is invalid or fails Pydantic validation.
    """
    try:
        data = yaml.safe_load(yaml_content) or {}
        return RepoConfig(**data)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in .outcomeops.yaml: {e}")
    except Exception as e:
        # Pydantic ValidationError and any other constructor failure are
        # surfaced as ValueError so callers don't depend on the Pydantic
        # exception hierarchy directly.
        raise ValueError(f"Invalid configuration in .outcomeops.yaml: {e}")


def load_repo_config_from_github(
    repo_full_name: str,
    branch_name: str,
    github_token: str,
) -> Optional[RepoConfig]:
    """
    Load repository configuration from GitHub.

    Fetches .outcomeops.yaml from the repository root.

    Args:
        repo_full_name: Full repository name (owner/repo)
        branch_name: Branch to read from
        github_token: GitHub API token

    Returns:
        RepoConfig if found, None otherwise. ``None`` covers both the
        "no config file" case (HTTP 404) and any transient error — the
        canonical degraded path is "fall back to defaults".
    """
    import base64

    import urllib.request
    import urllib.error

    config_path = ".outcomeops.yaml"
    api_url = (
        f"{_github_api()}/repos/{repo_full_name}/contents/{config_path}"
        f"?ref={branch_name}"
    )

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "OutcomeOps-CodeGenerator",
    }

    try:
        request = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            import json

            data = json.loads(response.read().decode("utf-8"))

        # Decode base64 content
        content = base64.b64decode(data["content"]).decode("utf-8")
        config = parse_repo_config(content)

        logger.info(
            f"[repo-config] Loaded .outcomeops.yaml from {repo_full_name} "
            f"(backend: {config.code_generation.backend})"
        )
        return config

    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info(
                f"[repo-config] No .outcomeops.yaml found in {repo_full_name}, "
                "using defaults"
            )
            return None
        else:
            logger.warning(
                f"[repo-config] Failed to load .outcomeops.yaml: {e}"
            )
            return None
    except Exception as e:
        logger.warning(
            f"[repo-config] Error loading .outcomeops.yaml: {e}"
        )
        return None


def get_default_config() -> RepoConfig:
    """
    Get default repository configuration.

    Returns:
        RepoConfig with default values
    """
    return RepoConfig()


# =============================================================================
# Example Configuration
# =============================================================================

EXAMPLE_CONFIG = """# OutcomeOps Repository Configuration
# Place this file at .outcomeops.yaml in your repository root

version: "1.0"

code_generation:
  # Backend to use for code generation
  # Options: python, java, typescript
  backend: python

  # Additional KB queries to include in every plan
  additional_kb_queries:
    - "Project-specific authentication patterns"
    - "Internal API client usage"

  # Custom guidelines to append to prompts
  custom_guidelines: |
    - Always use our internal logging wrapper from utils.logging
    - Follow the repository's existing naming conventions

  # File patterns to exclude from generation
  exclude_patterns:
    - "*.generated.py"
    - "__pycache__/*"

testing:
  # Test framework
  framework: pytest

  # Test directory pattern
  test_directory: lambda/tests/unit

  # Minimum coverage requirement
  min_coverage: 80

infrastructure:
  # Infrastructure as Code tool
  tool: terraform

  # Infrastructure directory
  directory: terraform

# ADRs that must always be included in context
required_adrs:
  repo: []
  global:
    - ADR-001
    - ADR-006

# Document ingestion configuration
# Controls which paths are ingested and how they are classified
ingestion:
  # Paths containing standards/ADRs (ingested as doc_type="adr")
  # These are used for code generation standards queries
  # Defaults to ["docs/adr"]
  standards_paths:
    - "docs/adr"
    - "engineering-standards"  # Example: custom standards directory

  # Paths containing general documentation (ingested as doc_type="doc")
  # These are searchable but not used for standards queries
  # Defaults to ["docs"]
  docs_paths:
    - "docs"
    - "wiki"  # Example: wiki exports

  # Symbol-graph extraction.
  # Lets a repo opt in/out of structural fact extraction.
  symbol_graph:
    enabled: true
    # include: ["src/**/*.py"]
    # exclude: ["**/generated/**"]
    # languages: ["python", "typescript"]
    max_file_bytes: 524288

# Project context to include in all prompts
project_context: |
  This is a serverless knowledge base system for AI-assisted development.
  Key components:
  - Lambda functions in lambda/ directory
  - Terraform infrastructure in terraform/
  - Shared code in lambda/shared/
"""
