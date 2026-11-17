# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Python test backend for run-tests Lambda.

Handles Python/Lambda projects:
- Creates virtualenv and installs dependencies
- Runs pytest with coverage
- Supports auto-fix for import and syntax errors using Claude

Auto-fix uses Claude via Bedrock to analyze errors and suggest fixes,
with knowledge base context for organizational standards (ADR-007).
"""

import os
import re
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from test_backend_base import TestBackend, CommandResult
from factory import register_backend

# Import shared auto-fix module from parent directory
_run_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _run_tests_dir not in sys.path:
    sys.path.insert(0, _run_tests_dir)

from auto_fix import apply_fix_to_file, parse_error_file_path

logger = logging.getLogger(__name__)


class PythonBackend(TestBackend):
    """
    Test backend for Python projects.

    Supports:
    - Lambda functions with requirements.txt
    - pytest with JUnit XML output
    - Auto-fix for import and syntax errors
    """

    def get_backend_name(self) -> str:
        return "Python Backend"

    def get_backend_type(self) -> str:
        return "python"

    def setup_environment(self) -> bool:
        """
        Create virtualenv and install dependencies.

        Returns:
            True if setup succeeded
        """
        venv_dir = os.path.join(self.repo_dir, "venv")

        # Create virtualenv
        result = self._run_command(
            ["python3.12", "-m", "venv", "venv"],
            cwd=self.repo_dir
        )
        if not result.succeeded:
            logger.error(f"Failed to create virtualenv: {result.stderr}")
            return False

        pip_bin = os.path.join(venv_dir, "bin", "pip")

        # Upgrade pip
        result = self._run_command([pip_bin, "install", "--upgrade", "pip"])
        if not result.succeeded:
            logger.warning(f"Failed to upgrade pip: {result.stderr}")

        # Install requirements from lambda/ directory
        requirements_dir = Path(self.repo_dir) / "lambda"
        if requirements_dir.exists():
            for req_file in sorted(requirements_dir.rglob("requirements.txt")):
                rel = req_file.relative_to(self.repo_dir)
                logger.info(f"Installing requirements from {rel}")
                result = self._run_command(
                    [pip_bin, "install", "-r", str(req_file)]
                )
                if not result.succeeded:
                    logger.warning(f"Failed to install {rel}: {result.stderr}")

        # Install test dependencies
        test_packages = [
            "pytest",
            "pytest-cov",
            "pytest-asyncio",
            "boto3",
            "botocore",
            "moto",
            "requests",
            "coverage"
        ]
        result = self._run_command([pip_bin, "install", *test_packages])
        if not result.succeeded:
            logger.warning(f"Failed to install test packages: {result.stderr}")

        logger.info(f"Virtual environment ready: {os.path.join(venv_dir, 'bin', 'python3.12')}")
        return True

    def get_test_command(self) -> List[str]:
        """Get pytest command for Lambda tests."""
        # Get test directory from config, default to lambda/tests/
        testing_config = self.config.get("testing", {})
        test_dir = testing_config.get("test_directory", "lambda/tests/")

        return [
            "venv/bin/python3.12",
            "-m", "pytest",
            test_dir,
            "-v",
            "--junitxml=lambda/junit.xml"
        ]

    def get_junit_paths(self) -> List[Path]:
        """JUnit output paths for Python projects."""
        return [
            Path("lambda/junit.xml"),
            Path("junit.xml"),
            Path("test-results/junit.xml"),
        ]

    def classify_failure(self, output: str) -> str:
        """Classify Python test failure."""
        output_lower = output.lower()

        # Python-specific import errors
        if any(pattern in output_lower for pattern in [
            "modulenotfounderror",
            "importerror",
            "no module named",
        ]):
            return "import_error"

        # Python syntax errors
        if "syntaxerror" in output_lower:
            return "syntax_error"

        # Assertion failures are logic errors
        return "logic_error"

    def attempt_auto_fix(self, failure_type: str, output: str) -> bool:
        """
        Attempt to auto-fix Python test failures.

        Currently supports:
        - import_error: Add missing imports or fix import paths
        - syntax_error: Fix common syntax issues

        Returns:
            True if fix was applied
        """
        if failure_type == "import_error":
            return self._fix_import_error(output)
        elif failure_type == "syntax_error":
            return self._fix_syntax_error(output)
        return False

    def _fix_import_error(self, output: str) -> bool:
        """
        Attempt to fix import errors using Claude.

        First checks if the module is likely a local module (part of the project).
        If so, uses Claude to fix the import. Otherwise tries pip install for
        external packages, falling back to Claude if pip fails.
        """
        # Extract module name from error
        # Python may output: "No module named 'handler'" (with quotes)
        # or: "No module named handler" (without quotes)
        match = re.search(r"No module named ['\"]?([^'\";\s]+)['\"]?", output)
        if not match:
            logger.warning("[python] Could not parse module name from import error")
            return self._fix_with_claude(output, "import_error")

        missing_module = match.group(1)
        logger.info(f"[python] Attempting to fix missing module: {missing_module}")

        # Check if this is likely a local module
        if self._is_likely_local_module(missing_module):
            logger.info(f"[python] Module '{missing_module}' appears to be a local module - using Claude fix")
            return self._fix_with_claude(output, "import_error")

        # Try pip install for external packages
        pip_bin = os.path.join(self.repo_dir, "venv", "bin", "pip")
        result = self._run_command([pip_bin, "install", missing_module])

        if result.succeeded:
            logger.info(f"[python] Successfully installed {missing_module} via pip")
            return True

        # pip install failed - use Claude to fix the import
        logger.info(f"[python] pip install failed for {missing_module} - using Claude fix")
        return self._fix_with_claude(output, "import_error")

    def _get_source_paths(self) -> List[str]:
        """
        Get list of source paths to search for local modules.

        Checks .outcomeops.yaml for configured source_paths and combines
        with common Python project layouts (additive, not replacement).

        Config example in .outcomeops.yaml:
            code_generation:
              backend: "python"
              python:
                source_paths:
                  - "src"
                  - "app"
                  - "myproject"

        Returns:
            List of absolute paths to search for local modules
        """
        paths = []

        # Add configured source paths first (highest priority)
        python_config = self.config.get("code_generation", {}).get("python", {})
        configured_paths = python_config.get("source_paths", [])
        if configured_paths:
            logger.info(f"[python] Using configured source_paths: {configured_paths}")
            paths.extend([os.path.join(self.repo_dir, p) for p in configured_paths])

        # Always add default paths (don't replace, combine)
        default_paths = [
            # Root level
            "",
            # Common Python project layouts
            "src",
            "app",
            "apps",
            "lib",
            # Lambda-specific
            "lambda",
            "lambdas",
            "functions",
            # Django
            "django",
            # Flask
            "flask_app",
        ]

        for p in default_paths:
            full_path = os.path.join(self.repo_dir, p)
            if full_path not in paths:
                paths.append(full_path)

        return paths

    def _get_local_module_patterns(self) -> List[str]:
        """
        Get regex patterns that indicate local modules.

        Checks .outcomeops.yaml for configured patterns and combines
        with common naming conventions (additive, not replacement).

        Config example in .outcomeops.yaml:
            code_generation:
              backend: "python"
              python:
                local_module_patterns:
                  - "^myapp_"
                  - "^company_"

        Returns:
            List of regex patterns
        """
        patterns = []

        # Add configured patterns first (highest priority)
        python_config = self.config.get("code_generation", {}).get("python", {})
        configured_patterns = python_config.get("local_module_patterns", [])
        if configured_patterns:
            logger.info(f"[python] Using configured local_module_patterns: {configured_patterns}")
            patterns.extend(configured_patterns)

        # Always add default patterns (don't replace, combine)
        default_patterns = [
            r'^lambda_',      # Lambda function modules
            r'^test_',        # Test modules
            r'^tests?$',      # Test package
            r'_handler$',     # Handler modules
            r'_backend$',     # Backend modules
            r'_service$',     # Service modules
            r'_utils?$',      # Utility modules
            r'_helpers?$',    # Helper modules
            r'_models?$',     # Model modules
            r'_views?$',      # View modules (Django/Flask)
            r'_api$',         # API modules
            r'_config$',      # Config modules
        ]

        for p in default_patterns:
            if p not in patterns:
                patterns.append(p)

        return patterns

    def _is_likely_local_module(self, module_name: str) -> bool:
        """
        Check if a module is likely local (part of the project) vs external (PyPI).

        This is framework-agnostic and works for:
        - Lambda functions (lambda_*, *_handler)
        - Flask apps (app.*, flask_app.*)
        - Django apps (myapp.*, users.*, etc.)
        - Any Python project structure

        Uses .outcomeops.yaml config if available:
            code_generation:
              backend: "python"
              python:
                source_paths:      # Directories containing local modules
                  - "src"
                  - "myapp"
                local_module_patterns:  # Regex patterns for local module names
                  - "^mycompany_"

        Args:
            module_name: The module name from the import error

        Returns:
            True if the module appears to be local to the project
        """
        # Get the root module (first part before any dots)
        root_module = module_name.split('.')[0]

        # Dotted imports are usually local packages (e.g., myapp.models, users.views)
        if '.' in module_name:
            logger.info(f"[python] Module '{module_name}' contains dots - likely local package")
            return True

        # Check source paths for the module (uses config or defaults)
        for base_path in self._get_source_paths():
            if not os.path.isdir(base_path):
                continue

            # Check for package directory (with __init__.py)
            package_dir = os.path.join(base_path, root_module)
            if os.path.isdir(package_dir):
                init_file = os.path.join(package_dir, "__init__.py")
                if os.path.isfile(init_file):
                    logger.info(f"[python] Found local package: {package_dir}")
                    return True
                # Also count as local if it's a directory (namespace package)
                logger.info(f"[python] Found local directory (namespace package): {package_dir}")
                return True

            # Check for single-file module
            module_file = os.path.join(base_path, f"{root_module}.py")
            if os.path.isfile(module_file):
                logger.info(f"[python] Found local module file: {module_file}")
                return True

        # Check naming patterns (uses config or defaults)
        for pattern in self._get_local_module_patterns():
            if re.search(pattern, root_module, re.IGNORECASE):
                logger.info(f"[python] Module '{root_module}' matches local pattern: {pattern}")
                return True

        # Not detected as local - assume it's an external package
        logger.info(f"[python] Module '{module_name}' not detected as local - treating as external")
        return False

    def _fix_syntax_error(self, output: str) -> bool:
        """Attempt to fix syntax errors using Claude."""
        logger.info("[python] Attempting to fix syntax error with Claude")
        return self._fix_with_claude(output, "syntax_error")

    def _resolve_error_file_path(self, error_file: str) -> Optional[str]:
        """
        Resolve a file path that may be from a different machine (e.g., /tmp/).

        Uses configured source_paths from .outcomeops.yaml to find the file.

        Args:
            error_file: The file path from the error output

        Returns:
            Resolved absolute path if found, None otherwise
        """
        # Extract relative path from /tmp/ paths
        relative_path = error_file
        if error_file.startswith('/tmp/'):
            parts = error_file.split('/')
            # Find the start of the actual project path
            # Look for common markers in configured + default paths
            source_roots = set()
            for path in self._get_source_paths():
                # Extract the first directory component
                rel = os.path.relpath(path, self.repo_dir) if path.startswith(self.repo_dir) else path
                if rel and rel != '.':
                    source_roots.add(rel.split('/')[0])

            # Add common markers
            source_roots.update(['lambda', 'src', 'app', 'tests', 'test'])

            for i, part in enumerate(parts):
                if part in source_roots:
                    relative_path = '/'.join(parts[i:])
                    break

        # Try relative to repo root first
        full_path = os.path.join(self.repo_dir, relative_path)
        if os.path.exists(full_path):
            return full_path

        # Try each source path
        for source_path in self._get_source_paths():
            if not os.path.isdir(source_path):
                continue
            candidate = os.path.join(source_path, os.path.basename(error_file))
            if os.path.exists(candidate):
                return candidate

        return None

    def _fix_with_claude(self, output: str, failure_type: str) -> bool:
        """
        Use Claude to analyze and fix the error.

        This uses the shared auto_fix module which:
        1. Queries the knowledge base for relevant patterns and standards
        2. Invokes Claude with the file content and error
        3. Applies the suggested fix to the file

        Args:
            output: Test error output
            failure_type: Type of error (import_error, syntax_error)

        Returns:
            True if fix was applied successfully
        """
        logger.info(f"[python] Starting Claude fix for {failure_type}")

        # Parse the file path from the error output
        error_file = parse_error_file_path(output)
        if not error_file:
            # Log a sample of the output to help debug why parsing failed
            output_preview = output[:500].replace('\n', '\\n')
            logger.warning(f"[python] Could not parse file path from {failure_type}. Output preview: {output_preview}")
            return False

        logger.info(f"[python] Error in file: {error_file}")

        # Check if file exists (it might be a path from a different machine)
        if not os.path.exists(error_file):
            resolved_file = self._resolve_error_file_path(error_file)
            if resolved_file:
                error_file = resolved_file
                logger.info(f"[python] Resolved file path to: {error_file}")
            else:
                logger.warning(f"[python] File not found: {error_file}")
                return False

        # Apply Claude fix with KB context
        try:
            if apply_fix_to_file(error_file, output, failure_type, backend_type="python", config=self.config):
                logger.info(f"[python] Claude fix applied successfully to {error_file}")
                return True
            else:
                logger.warning(f"[python] Claude fix returned False for {error_file}")
                return False
        except Exception as e:
            logger.error(f"[python] Claude fix raised exception for {error_file}: {e}")
            return False


# Register the Python backend
register_backend("python", PythonBackend)
