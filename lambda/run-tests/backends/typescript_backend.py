# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
TypeScript test backend for run-tests Lambda.

Handles TypeScript/JavaScript projects:
- npm test / yarn test / pnpm test
- Jest, Vitest, Mocha test runners
- Next.js, React, Node.js applications

Auto-fix uses Claude via Bedrock to analyze errors and suggest fixes,
with knowledge base context for organizational standards (ADR-007).
"""

import os
import re
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from test_backend_base import TestBackend
from factory import register_backend

# Import shared auto-fix module from parent directory
_run_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _run_tests_dir not in sys.path:
    sys.path.insert(0, _run_tests_dir)

from auto_fix import apply_fix_to_file

logger = logging.getLogger(__name__)


class TypeScriptBackend(TestBackend):
    """
    Test backend for TypeScript/JavaScript projects.

    Supports:
    - npm, yarn, pnpm package managers
    - Jest, Vitest, Mocha test runners
    - JUnit XML output for test results
    """

    def __init__(self, repo_dir: str, config: Dict[str, Any]):
        super().__init__(repo_dir, config)
        self.package_manager = self._detect_package_manager()
        self.test_runner = self._detect_test_runner()

    def get_backend_name(self) -> str:
        return "TypeScript Backend"

    def get_backend_type(self) -> str:
        return "typescript"

    def _detect_package_manager(self) -> str:
        """Detect which package manager the project uses."""
        repo_path = Path(self.repo_dir)

        # Check for lock files (most specific indicator)
        if (repo_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (repo_path / "yarn.lock").exists():
            return "yarn"
        if (repo_path / "package-lock.json").exists():
            return "npm"

        # Default to npm
        return "npm"

    def _detect_test_runner(self) -> str:
        """Detect which test runner the project uses."""
        repo_path = Path(self.repo_dir)
        package_json = repo_path / "package.json"

        if package_json.exists():
            try:
                import json
                with open(package_json) as f:
                    pkg = json.load(f)

                # Check devDependencies and dependencies
                all_deps = {
                    **pkg.get("dependencies", {}),
                    **pkg.get("devDependencies", {})
                }

                if "vitest" in all_deps:
                    return "vitest"
                if "jest" in all_deps:
                    return "jest"
                if "mocha" in all_deps:
                    return "mocha"

            except Exception as e:
                logger.warning(f"Failed to parse package.json: {e}")

        # Default to jest (most common)
        return "jest"

    def setup_environment(self) -> bool:
        """
        Install npm dependencies.

        Returns:
            True if setup succeeded
        """
        logger.info(f"TypeScript project detected with {self.package_manager} and {self.test_runner}")

        # Install dependencies
        if self.package_manager == "pnpm":
            install_cmd = ["pnpm", "install", "--frozen-lockfile"]
        elif self.package_manager == "yarn":
            install_cmd = ["yarn", "install", "--frozen-lockfile"]
        else:
            install_cmd = ["npm", "ci"]

        result = self._run_command(install_cmd)
        if not result.succeeded:
            # Try without frozen lockfile as fallback
            logger.warning("Frozen lockfile install failed, trying regular install")
            if self.package_manager == "pnpm":
                result = self._run_command(["pnpm", "install"])
            elif self.package_manager == "yarn":
                result = self._run_command(["yarn", "install"])
            else:
                result = self._run_command(["npm", "install"])

        if not result.succeeded:
            logger.error(f"Failed to install dependencies: {result.stderr}")
            return False

        logger.info("Dependencies installed successfully")
        return True

    def get_test_command(self) -> List[str]:
        """Get test command based on package manager and test runner."""
        # Check for custom test command in config
        testing_config = self.config.get("testing", {})
        custom_command = testing_config.get("test_command")
        if custom_command:
            return custom_command.split()

        # Use package manager to run test script
        if self.package_manager == "pnpm":
            return ["pnpm", "test"]
        elif self.package_manager == "yarn":
            return ["yarn", "test"]
        else:
            return ["npm", "test"]

    def get_junit_paths(self) -> List[Path]:
        """JUnit output paths for TypeScript projects."""
        return [
            # Jest with jest-junit reporter
            Path("junit.xml"),
            Path("test-results/junit.xml"),
            Path("coverage/junit.xml"),
            # Vitest
            Path("test-results.xml"),
            # Custom paths
            Path("reports/junit.xml"),
        ]

    def classify_failure(self, output: str) -> str:
        """Classify TypeScript/JavaScript test failure."""
        output_lower = output.lower()

        # Module/import errors
        if any(pattern in output_lower for pattern in [
            "cannot find module",
            "module not found",
            "could not resolve",
            "unable to resolve",
            "error: cannot find",
            "ts2307",  # TypeScript "Cannot find module" error code
            "ts2305",  # TypeScript "Module has no exported member" error code
        ]):
            return "import_error"

        # Syntax/compilation errors
        if any(pattern in output_lower for pattern in [
            "syntaxerror",
            "syntax error",
            "unexpected token",
            "parsing error",
            "ts1005",  # TypeScript syntax error codes
            "ts1003",
            "ts1002",
        ]):
            return "syntax_error"

        # Type errors (treat as syntax for auto-fix purposes)
        if any(pattern in output_lower for pattern in [
            "type error",
            "ts2322",  # Type not assignable
            "ts2345",  # Argument type not assignable
            "ts2339",  # Property does not exist
        ]):
            return "syntax_error"

        # Default to logic error
        return "logic_error"

    def attempt_auto_fix(self, failure_type: str, output: str) -> bool:
        """
        Attempt to auto-fix TypeScript test failures using Claude.

        Supports:
        - import_error: Missing imports, module resolution issues
        - syntax_error: TypeScript/JavaScript syntax and type errors

        Returns:
            True if fix was applied
        """
        if failure_type == "import_error":
            return self._fix_with_claude(output, "import_error")
        elif failure_type == "syntax_error":
            return self._fix_with_claude(output, "syntax_error")
        return False

    def _fix_with_claude(self, output: str, failure_type: str) -> bool:
        """
        Use Claude to analyze and fix the error.

        Args:
            output: Test error output
            failure_type: Type of error (import_error, syntax_error)

        Returns:
            True if fix was applied successfully
        """
        logger.info(f"[typescript] Starting Claude fix for {failure_type}")

        # Parse the file path from the error output
        error_file = self._parse_ts_error_file(output)
        if not error_file:
            # Log a sample of the output to help debug why parsing failed
            output_preview = output[:500].replace('\n', '\\n')
            logger.warning(f"[typescript] Could not parse file path from {failure_type}. Output preview: {output_preview}")
            return False

        logger.info(f"[typescript] Error in file: {error_file}")

        # Check if file exists (it might be a path from a different machine)
        if not os.path.exists(error_file):
            resolved_file = self._resolve_error_file_path(error_file)
            if resolved_file:
                error_file = resolved_file
                logger.info(f"[typescript] Resolved file path to: {error_file}")
            else:
                logger.warning(f"[typescript] File not found: {error_file}")
                return False

        # Apply Claude fix with KB context
        try:
            if apply_fix_to_file(error_file, output, failure_type, backend_type="typescript", config=self.config):
                logger.info(f"[typescript] Claude fix applied successfully to {error_file}")
                return True
            else:
                logger.warning(f"[typescript] Claude fix returned False for {error_file}")
                return False
        except Exception as e:
            logger.error(f"[typescript] Claude fix raised exception for {error_file}: {e}")
            return False

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

            # Add common TypeScript markers
            source_roots.update(['src', 'app', 'lib', 'pages', 'components', 'test', 'tests', '__tests__'])

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

    def _parse_ts_error_file(self, output: str) -> Optional[str]:
        """
        Parse TypeScript/JavaScript error output to find the file path.

        Handles formats:
        - TypeScript: "src/file.ts(10,5): error TS2322: ..."
        - Jest: "at Object.<anonymous> (src/file.ts:10:5)"
        - ESLint: "/path/to/file.ts:10:5"
        - Vitest: "FAIL src/file.test.ts > test name"

        Returns:
            Path to the file, or None if not found
        """
        # Try TypeScript compiler format: file.ts(line,col): error
        match = re.search(r'([^\s(]+\.tsx?)\(\d+,\d+\):', output)
        if match:
            return self._resolve_path(match.group(1))

        # Try Jest/Node stack trace format: at ... (file.ts:line:col)
        match = re.search(r'\(([^)]+\.tsx?):\d+:\d+\)', output)
        if match:
            return self._resolve_path(match.group(1))

        # Try simple path:line:col format
        match = re.search(r'([^\s:]+\.tsx?):\d+:\d+', output)
        if match:
            return self._resolve_path(match.group(1))

        # Try JavaScript files too
        match = re.search(r'([^\s:]+\.jsx?):\d+:\d+', output)
        if match:
            return self._resolve_path(match.group(1))

        # Try Vitest FAIL format
        match = re.search(r'FAIL\s+([^\s]+\.tsx?)', output)
        if match:
            return self._resolve_path(match.group(1))

        return None

    def _resolve_path(self, file_path: str) -> Optional[str]:
        """Resolve a potentially relative path to absolute."""
        if os.path.isabs(file_path) and os.path.exists(file_path):
            return file_path

        # Try relative to repo root
        full_path = os.path.join(self.repo_dir, file_path)
        if os.path.exists(full_path):
            return full_path

        # Try common source directories
        for src_dir in self._get_source_paths():
            candidate = os.path.join(src_dir, file_path)
            if os.path.exists(candidate):
                return candidate

        return None

    def _get_source_paths(self) -> List[str]:
        """
        Get list of source paths to search for local modules.

        Checks .outcomeops.yaml for configured source_paths and combines
        with common TypeScript project layouts.

        Config example in .outcomeops.yaml:
            code_generation:
              backend: "typescript"
              typescript:
                source_paths:
                  - "src"
                  - "lib"

        Returns:
            List of absolute paths to search for local modules
        """
        paths = []

        # Add configured source paths first
        ts_config = self.config.get("code_generation", {}).get("typescript", {})
        configured_paths = ts_config.get("source_paths", [])
        if configured_paths:
            logger.info(f"[typescript] Using configured source_paths: {configured_paths}")
            paths.extend([os.path.join(self.repo_dir, p) for p in configured_paths])

        # Always add default paths (don't replace, combine)
        default_paths = [
            # Root level
            "",
            # Common TypeScript/JavaScript layouts
            "src",
            "lib",
            "app",
            "pages",        # Next.js
            "components",   # React
            "utils",
            "services",
            # Test directories
            "test",
            "tests",
            "__tests__",
            "spec",
        ]

        for p in default_paths:
            full_path = os.path.join(self.repo_dir, p)
            if full_path not in paths:
                paths.append(full_path)

        return paths


# Register the TypeScript backend
register_backend("typescript", TypeScriptBackend)
