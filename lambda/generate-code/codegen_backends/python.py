# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Python code generator backend.

Handles code generation for Python applications with:
- Python source files (.py)
- requirements.txt dependency management
- Terraform infrastructure
- pytest testing patterns
"""

import ast
import importlib
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import CodeGeneratorBackend, FormattingResult, ValidationResult

logger = logging.getLogger()

# Standard library modules safe to validate imports from
# These are commonly used and the AI often hallucinates exports
STDLIB_MODULES_TO_VALIDATE = {
    "unittest",
    "unittest.mock",
    "typing",
    "collections",
    "collections.abc",
    "os",
    "os.path",
    "sys",
    "json",
    "datetime",
    "re",
    "logging",
    "pathlib",
    "functools",
    "itertools",
    "contextlib",
    "dataclasses",
    "abc",
    "enum",
}


class PythonCodeGenerator(CodeGeneratorBackend):
    """
    Code generator for Python applications.

    Assumes:
    - Python 3.11+ runtime
    - Terraform for infrastructure
    - pytest for testing
    - requirements.txt for dependencies (ADR-006 compliant)
    """

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def name(self) -> str:
        return "python"

    @property
    def display_name(self) -> str:
        return "Python"

    @property
    def source_extensions(self) -> Set[str]:
        return {".py"}

    @property
    def dependency_files(self) -> Set[str]:
        return {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile"}

    # =========================================================================
    # Knowledge Base Queries
    # =========================================================================

    def get_handler_standards_queries(self) -> List[str]:
        """
        Get KB queries for handler/service standards.

        These are generic queries - the KB will return framework-specific
        patterns based on what ADRs exist (Lambda, Flask, Django, FastAPI, etc.)
        """
        return [
            "Python handler standards and patterns",
            "Python error handling and logging best practices",
            "Python application entry point patterns",
        ]

    def get_testing_standards_queries(self) -> List[str]:
        """
        Get KB queries for testing standards.

        Generic pytest queries - framework-specific patterns (moto for Lambda,
        pytest-flask, Django TestCase) come from repo ADRs.
        """
        return [
            "pytest testing standards and patterns",
            "Python unit testing best practices",
            "Python mocking and fixture patterns",
        ]

    def get_infrastructure_standards_queries(self) -> List[str]:
        """
        Get KB queries for infrastructure standards.

        Generic infra queries - deployment target (Lambda, ECS, K8s) comes from
        repo ADRs and project_context in .outcomeops.yaml.
        """
        return [
            "Python application deployment patterns",
            "Infrastructure as code standards",
            "Python containerization patterns",
        ]

    def get_language_standards_queries(self) -> List[str]:
        return [
            "Python coding standards and conventions",
            "Python dependency management standards",
            "Python project structure patterns",
        ]

    # =========================================================================
    # Plan Generation Prompts
    # =========================================================================

    def get_plan_generation_system_prompt(self) -> str:
        return """You are a senior software engineer planning the implementation of a GitHub issue for a Python project.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Large files (handlers, views, services): 1 file per step
   - Small files (schemas, types, models): 2-3 files per step if related
   - Infrastructure: Separate step for Terraform/CloudFormation changes
3. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create unit tests" step
   - BREAK DOWN test creation into multiple focused steps:
     * Step N: Create unit tests for success/happy path cases (1-3 test functions)
     * Step N+1: Create unit tests for error handling (1-3 test functions)
     * Step N+2: Create unit tests for edge cases (1-3 test functions)
   - Each test step should generate 1-3 test functions maximum
4. Include ONLY step-specific KB queries (general standards are already provided)
5. Build incrementally on previous steps

IMPORTANT - PROJECT STRUCTURE:
- Follow the project structure defined in the Available Standards section below
- The standards from the knowledge base will tell you where files should go
- If no specific structure is defined, use common Python conventions

KB QUERY GUIDELINES:
- General standards (patterns, error handling, testing) are ALREADY available below
- ONLY include queries for step-specific context:
  - Specific patterns for this step (e.g., "Pydantic validation for pagination")
  - Examples relevant to this step (e.g., "boto3 DynamoDB scan examples")
  - ADRs specific to this feature (e.g., "ADR for API pagination standards")
- DO NOT query for general standards like "error handling" or "testing best practices"

DO NOT include steps for:
- Manual verification or compliance checks
- PR review or validation steps
- "Ensure code follows patterns" type steps

CRITICAL - filesToCreate REQUIREMENTS:
- EVERY step MUST have at least one file in filesToCreate
- Include infrastructure files if modifying them (terraform/*.tf, etc.)
- The filesToCreate array tells the system which files to fetch and provide as context
- Files NOT in filesToCreate will NOT be provided to the code generator

Return ONLY valid JSON in this exact format:
{
  "steps": [
    {
      "stepNumber": 1,
      "title": "Brief step title",
      "description": "What this step accomplishes",
      "filesToCreate": ["path/to/file.py"],
      "kbQueries": ["Step-specific query (NOT general standards)"],
      "status": "pending"
    }
  ]
}"""

    def get_code_generation_system_prompt(self) -> str:
        return """You are a senior Python developer implementing production-ready code.

Your task is to generate complete, working Python code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Follow Python best practices:
   - Type hints on all functions
   - Docstrings with Args/Returns sections
   - Proper error handling with logging
   - No hardcoded secrets or credentials

IMPORTANT - FOLLOW THE STANDARDS:
- The Available Standards section below contains project-specific patterns
- Follow the project structure, naming conventions, and patterns from those standards
- If standards specify a framework (Flask, Django, Lambda, FastAPI), follow its conventions
- Use the testing patterns defined in the standards

REQUIREMENTS.TXT / DEPENDENCIES:
- Pin exact versions (package==1.2.3, not package>=1.2)
- Only include packages actually used
- Validate packages exist on PyPI before including

INFRASTRUCTURE FILES (CRITICAL):
- If existing file content is provided, you MUST ADD to it, not replace it
- Use the same patterns as existing infrastructure code
- DO NOT invent new module paths - look at existing content
- Add your new resources AFTER existing definitions

OUTPUT FORMAT (MANDATORY - DO NOT USE JSON):
You MUST use the delimiter format below. Do NOT wrap output in ```json or return {"files": [...]}.

For each file, output:
===FILE: path/to/file.py===
<raw file content here - no escaping needed>
===FILE: path/to/next_file.py===
<next file content>
===END===

Example (follow this EXACTLY):
===FILE: src/handlers/my_handler.py===
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def handle_request(data: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("Processing request")
    return {"status": "success"}
===FILE: requirements.txt===
pydantic==2.5.0
===END===

CRITICAL RULES:
- Start your response with ===FILE: path=== (no preamble, no JSON)
- Output raw code exactly as it should appear in the file
- NO JSON wrapping, NO ```json blocks, NO escaping, NO base64
- Each file starts with ===FILE: path=== on its own line
- End with ===END=== on its own line
- The path after FILE: must match the expected file path exactly"""

    def get_plan_generation_guidelines(self) -> str:
        return """
PYTHON SPECIFIC GUIDELINES:
- Follow the project structure from the standards (may be Lambda, Flask, Django, FastAPI, etc.)
- requirements.txt should pin exact versions
- Tests use pytest - follow the testing patterns from the standards
- Infrastructure changes require separate Terraform/CloudFormation step
- Use type hints and docstrings on all public functions
"""

    # =========================================================================
    # File Validation
    # =========================================================================

    def validate_file(self, file_path: str, content: str) -> ValidationResult:
        """
        Validate Python files and requirements.txt.

        Validates:
        - requirements.txt: Packages exist on PyPI (ADR-006)
        - .py files: Valid Python syntax

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            ValidationResult
        """
        if file_path.endswith("requirements.txt"):
            return self._validate_requirements(file_path, content)
        elif file_path.endswith(".py"):
            return self._validate_python_syntax(file_path, content)
        else:
            # No validation for other files
            return ValidationResult(valid=True)

    def _validate_requirements(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate requirements.txt using pip --dry-run.

        Per ADR-006, all requirements.txt files must be validated before commit
        to prevent hallucinated or non-existent packages.
        """
        logger.info(f"[validation] Validating {file_path} per ADR-006")

        # Create temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            result = subprocess.run(
                ["pip", "install", "--dry-run", "-r", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                # Extract meaningful error, filtering out harmless warnings
                error_lines = result.stderr.strip().split("\n")

                # Filter out cache directory warnings (common in Lambda sandbox)
                filtered_lines = [
                    line for line in error_lines
                    if not any(ignore in line for ignore in [
                        "cache has been disabled",
                        "not owned or is not writable",
                        "WARNING: The directory",
                    ])
                ]

                # Check for actual package errors
                package_error = None
                for line in filtered_lines:
                    if (
                        "Could not find a version" in line
                        or "No matching distribution" in line
                    ):
                        package_error = line
                        break

                # If we found a package error, that's a real failure
                if package_error:
                    error_msg = f"requirements.txt validation failed: {package_error}"
                    logger.error(f"[validation] {error_msg}")
                    return ValidationResult(valid=False, error_message=error_msg)

                # If only cache warnings remain, treat as success
                if not filtered_lines:
                    logger.info(
                        f"[validation] {file_path} passed validation "
                        "(ignoring cache warnings)"
                    )
                    return ValidationResult(valid=True)

                # Other errors - report them
                error_msg = (
                    f"requirements.txt validation failed: "
                    f"{result.stderr[:200]}"
                )
                logger.error(f"[validation] {error_msg}")
                return ValidationResult(valid=False, error_message=error_msg)

            logger.info(f"[validation] {file_path} passed validation")
            return ValidationResult(valid=True)

        except subprocess.TimeoutExpired:
            error_msg = "requirements.txt validation timed out after 30s"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg)
        except Exception as e:
            error_msg = f"requirements.txt validation error: {str(e)}"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg)
        finally:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

    def _validate_python_syntax(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate Python syntax using compile() and validate imports.
        """
        # First check syntax
        try:
            compile(content, file_path, "exec")
        except SyntaxError as e:
            error_msg = f"Python syntax error at line {e.lineno}: {e.msg}"
            logger.error(f"[validation] {file_path}: {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg)

        # Then validate imports from standard library modules
        import_errors = self._validate_imports(file_path, content)
        if import_errors:
            error_msg = "; ".join(import_errors)
            logger.error(f"[validation] {file_path}: {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg)

        return ValidationResult(valid=True)

    def _validate_imports(
        self, file_path: str, content: str
    ) -> List[str]:
        """
        Validate imports from standard library modules.

        Uses AST to extract imports and verifies that names imported from
        standard library modules actually exist. This catches hallucinated
        imports like 'from unittest.mock import MockCall' (should be 'call').

        Args:
            file_path: Path to the file (for logging)
            content: File content

        Returns:
            List of error messages (empty if all imports are valid)
        """
        errors = []

        try:
            tree = ast.parse(content)
        except SyntaxError:
            # Syntax errors are caught by compile() in the caller
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_name = node.module or ""

                # Only validate imports from known stdlib modules
                if module_name not in STDLIB_MODULES_TO_VALIDATE:
                    continue

                # Try to import the module and check for the names
                try:
                    module = importlib.import_module(module_name)

                    for alias in node.names:
                        name = alias.name
                        if name == "*":
                            continue

                        if not hasattr(module, name):
                            errors.append(
                                f"Invalid import: '{name}' does not exist in '{module_name}' "
                                f"(line {node.lineno})"
                            )
                            logger.warning(
                                f"[validation] {file_path}:{node.lineno}: "
                                f"'{name}' not found in '{module_name}'"
                            )

                except ImportError as e:
                    # Module itself doesn't exist - this is a bigger problem
                    errors.append(
                        f"Cannot import module '{module_name}': {e} (line {node.lineno})"
                    )

        return errors

    # =========================================================================
    # Code Formatting
    # =========================================================================

    def get_formatters(self) -> List[Dict[str, Any]]:
        """
        Get formatters for Python Lambda project.

        Returns formatters for:
        - Terraform files (.tf) - terraform fmt
        - Python files (.py) - black (if available)
        """
        return [
            {
                "name": "terraform",
                "extensions": {".tf"},
                "command": ["terraform", "fmt"],
                "timeout": 30,
            },
            # Note: black formatting is optional - only if installed
            # {
            #     "name": "black",
            #     "extensions": {".py"},
            #     "command": ["black", "--quiet"],
            #     "timeout": 30,
            # },
        ]

    # =========================================================================
    # Test Generation
    # =========================================================================

    def get_test_file_pattern(self, source_file: str) -> str:
        """
        Get pytest test file path for a source file.

        Examples:
            lambda/ingest-docs/handler.py -> lambda/tests/unit/test_ingest_docs.py
            lambda/generate-code/utils.py -> lambda/tests/unit/test_generate_code_utils.py
        """
        # Extract function name from path
        parts = source_file.split("/")

        if len(parts) >= 2 and parts[0] == "lambda":
            function_name = parts[1].replace("-", "_")
            file_name = parts[-1].replace(".py", "")

            if file_name == "handler":
                return f"lambda/tests/unit/test_{function_name}.py"
            else:
                return f"lambda/tests/unit/test_{function_name}_{file_name}.py"

        # Default fallback
        return source_file.replace(".py", "_test.py")

    def get_test_step_breakdown(self) -> List[str]:
        """
        Get pytest test step breakdown.
        """
        return [
            "success/happy path cases using pytest fixtures (1-3 test functions)",
            "error handling with pytest.raises (1-3 test functions)",
            "edge cases and boundary conditions (1-3 test functions)",
        ]
