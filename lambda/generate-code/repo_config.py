"""
Repository configuration loader.

Loads and parses .outcomeops.yaml configuration files from repositories.
This allows per-repo customization of code generation behavior.
"""

import logging
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger()


# =============================================================================
# Configuration Schema
# =============================================================================


class CodeGenerationConfig(BaseModel):
    """Code generation configuration."""

    # Backend to use (e.g., 'python', 'java', 'typescript')
    backend: str = Field(default="python")

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
        ValueError: If YAML is invalid
    """
    try:
        data = yaml.safe_load(yaml_content) or {}
        return RepoConfig(**data)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in .outcomeops.yaml: {e}")
    except Exception as e:
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
        RepoConfig if found, None otherwise
    """
    import base64

    import urllib.request
    import urllib.error

    config_path = ".outcomeops.yaml"
    api_url = (
        f"https://api.github.com/repos/{repo_full_name}/contents/{config_path}"
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

# Project context to include in all prompts
project_context: |
  This is a serverless knowledge base system for AI-assisted development.
  Key components:
  - Lambda functions in lambda/ directory
  - Terraform infrastructure in terraform/
  - Shared code in lambda/shared/
"""
