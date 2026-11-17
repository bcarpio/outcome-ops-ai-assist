# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Base class for code generator backends.

Each backend defines language-specific:
- Plan generation prompts and strategies
- Knowledge base queries for standards
- File validation (dependency files, syntax)
- Code formatting (terraform fmt, black, prettier)
- Test patterns and structure

See ADR-010 for the code generator backend pattern documentation.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ValidationResult:
    """Result of validating a generated file."""

    valid: bool
    error_message: Optional[str] = None
    file_path: Optional[str] = None


@dataclass
class FormattingResult:
    """Result of formatting files."""

    files_formatted: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class StandardsContext:
    """Standards retrieved from knowledge base for code generation."""

    # Core standards (always retrieved)
    handler_standards: List[str] = field(default_factory=list)
    testing_standards: List[str] = field(default_factory=list)

    # Infrastructure standards (if applicable)
    infrastructure_standards: List[str] = field(default_factory=list)

    # Language-specific standards
    language_standards: List[str] = field(default_factory=list)

    # Framework-specific standards
    framework_standards: List[str] = field(default_factory=list)

    # Code style/verbosity standards (ADR-012)
    code_style_standards: List[str] = field(default_factory=list)


class CodeGeneratorBackend(ABC):
    """
    Abstract base class for code generator backends.

    Each backend handles a specific language/framework combination:
    - PythonLambdaCodeGenerator: Python + AWS Lambda + Terraform
    - JavaSpringCodeGenerator: Java + Spring Boot (future)
    - TypeScriptExpressCodeGenerator: TypeScript + Express/Lambda (future)
    """

    def __init__(self, repo_config: Optional[Dict[str, Any]] = None):
        """
        Initialize backend with optional repo configuration.

        Args:
            repo_config: Repository configuration from .outcomeops.yaml
        """
        self.repo_config = repo_config or {}

    # =========================================================================
    # Abstract Properties - Must be implemented by subclasses
    # =========================================================================

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name for logging and config (e.g., 'python')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable backend name (e.g., 'Python Lambda')."""
        pass

    @property
    @abstractmethod
    def source_extensions(self) -> Set[str]:
        """File extensions this backend handles (e.g., {'.py'})."""
        pass

    @property
    @abstractmethod
    def dependency_files(self) -> Set[str]:
        """Dependency file names (e.g., {'requirements.txt', 'pyproject.toml'})."""
        pass

    # =========================================================================
    # Knowledge Base Queries - Override to customize
    # =========================================================================

    @abstractmethod
    def get_handler_standards_queries(self) -> List[str]:
        """
        Get KB queries for handler/service standards.

        Returns:
            List of search queries for the knowledge base
        """
        pass

    @abstractmethod
    def get_testing_standards_queries(self) -> List[str]:
        """
        Get KB queries for testing standards.

        Returns:
            List of search queries for the knowledge base
        """
        pass

    def get_infrastructure_standards_queries(self) -> List[str]:
        """
        Get KB queries for infrastructure standards.

        Default returns Terraform queries. Override for CloudFormation, Pulumi, etc.

        Returns:
            List of search queries for the knowledge base
        """
        return [
            "Terraform Lambda module configuration standards",
            "Terraform resource naming conventions",
        ]

    def get_language_standards_queries(self) -> List[str]:
        """
        Get KB queries for language-specific standards.

        Override to add language-specific queries (e.g., Python style, Java conventions).

        Returns:
            List of search queries for the knowledge base
        """
        return []

    def get_framework_standards_queries(self) -> List[str]:
        """
        Get KB queries for framework-specific standards.

        Override for framework-specific patterns (e.g., Spring Boot, Express).

        Returns:
            List of search queries for the knowledge base
        """
        return []

    def get_code_style_queries(self) -> List[str]:
        """
        Get KB queries for code style and verbosity standards.

        Returns:
            List of search queries for the knowledge base
        """
        return [
            "code generation verbosity standards",
            "concise code style guidelines",
        ]

    # =========================================================================
    # Plan Generation - Override to customize prompts
    # =========================================================================

    @abstractmethod
    def get_plan_generation_system_prompt(self) -> str:
        """
        Get system prompt for plan generation.

        This prompt instructs Claude on how to break down the issue into steps.

        Returns:
            System prompt string
        """
        pass

    @abstractmethod
    def get_code_generation_system_prompt(self) -> str:
        """
        Get system prompt for code generation.

        This prompt instructs Claude on how to generate code for each step.

        Returns:
            System prompt string
        """
        pass

    def get_plan_generation_guidelines(self) -> str:
        """
        Get additional guidelines for plan generation.

        Override to add backend-specific planning guidelines.

        Returns:
            Guidelines string to append to plan prompt
        """
        return ""

    # =========================================================================
    # File Validation - Override to add validation
    # =========================================================================

    @abstractmethod
    def validate_file(self, file_path: str, content: str) -> ValidationResult:
        """
        Validate a generated file before committing.

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            ValidationResult with valid=True/False and optional error message
        """
        pass

    def validate_files(
        self, files: List[Dict[str, str]]
    ) -> List[ValidationResult]:
        """
        Validate multiple files.

        Args:
            files: List of dicts with 'path' and 'content' keys

        Returns:
            List of ValidationResult for each file
        """
        results = []
        for file_info in files:
            result = self.validate_file(file_info["path"], file_info["content"])
            result.file_path = file_info["path"]
            results.append(result)
        return results

    # =========================================================================
    # Code Formatting - Override to add formatters
    # =========================================================================

    @abstractmethod
    def get_formatters(self) -> List[Dict[str, Any]]:
        """
        Get list of formatters to run on generated files.

        Each formatter dict should have:
        - 'name': Formatter name for logging
        - 'extensions': Set of file extensions to format
        - 'command': Command to run (list of strings)
        - 'timeout': Timeout in seconds

        Returns:
            List of formatter configurations
        """
        pass

    def should_format_file(self, file_path: str) -> bool:
        """
        Check if a file should be formatted.

        Args:
            file_path: Path to the file

        Returns:
            True if file should be formatted
        """
        for formatter in self.get_formatters():
            for ext in formatter.get("extensions", set()):
                if file_path.endswith(ext):
                    return True
        return False

    # =========================================================================
    # Test Generation - Override to customize test patterns
    # =========================================================================

    def get_test_file_pattern(self, source_file: str) -> str:
        """
        Get the test file path for a source file.

        Args:
            source_file: Path to source file (e.g., 'lambda/ingest-docs/handler.py')

        Returns:
            Path to test file (e.g., 'lambda/tests/unit/test_ingest_docs.py')
        """
        # Default implementation - override for different patterns
        return source_file.replace(".py", "_test.py")

    def get_test_step_breakdown(self) -> List[str]:
        """
        Get the breakdown of test steps.

        Used to guide Claude on how to split test generation into multiple steps.

        Returns:
            List of test categories (e.g., ['success cases', 'error handling', 'edge cases'])
        """
        return [
            "success/happy path cases (1-3 test functions)",
            "error handling cases (1-3 test functions)",
            "edge cases (1-3 test functions)",
        ]

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def is_source_file(self, file_path: str) -> bool:
        """Check if file is a source file for this backend."""
        return any(file_path.endswith(ext) for ext in self.source_extensions)

    def is_dependency_file(self, file_path: str) -> bool:
        """Check if file is a dependency file for this backend."""
        file_name = file_path.split("/")[-1]
        return file_name in self.dependency_files

    def is_test_file(self, file_path: str) -> bool:
        """Check if file is a test file."""
        # Common test file patterns
        file_name = file_path.split("/")[-1]
        return (
            "/test" in file_path
            or "/tests/" in file_path
            or file_path.endswith("_test.py")
            or file_path.endswith(".test.ts")
            or file_path.endswith(".test.js")
            or file_path.endswith(".spec.ts")
            or file_path.endswith(".spec.js")
            or file_name.endswith("Test.java")
            or file_name.endswith("Tests.java")
        )
