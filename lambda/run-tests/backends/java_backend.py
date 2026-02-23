"""
Java test backend for run-tests Lambda.

Handles Java/Kotlin projects:
- Gradle (./gradlew test)
- Maven (./mvnw test)
- Spring Boot applications
- Standard JUnit tests

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


class JavaBackend(TestBackend):
    """
    Test backend for Java/Kotlin projects.

    Supports:
    - Gradle projects (./gradlew test)
    - Maven projects (./mvnw test)
    - JUnit test results
    """

    def __init__(self, repo_dir: str, config: Dict[str, Any]):
        super().__init__(repo_dir, config)
        self.build_tool = self._detect_build_tool()

    def get_backend_name(self) -> str:
        return "Java Backend"

    def get_backend_type(self) -> str:
        return "java"

    def _detect_build_tool(self) -> str:
        """Detect whether project uses Gradle or Maven."""
        repo_path = Path(self.repo_dir)

        # Check for Gradle
        if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
            if (repo_path / "gradlew").exists():
                return "gradle-wrapper"
            return "gradle"

        # Check for Maven
        if (repo_path / "pom.xml").exists():
            if (repo_path / "mvnw").exists():
                return "maven-wrapper"
            return "maven"

        # Default to gradle
        return "gradle"

    def setup_environment(self) -> bool:
        """
        Set up Java build environment.

        For Gradle/Maven wrapper projects, no setup is needed.
        The wrapper scripts handle dependency resolution.

        Returns:
            True (Java projects are self-contained with wrappers)
        """
        logger.info(f"Java project detected with build tool: {self.build_tool}")

        # Make wrapper scripts executable
        repo_path = Path(self.repo_dir)
        for wrapper in ["gradlew", "mvnw"]:
            wrapper_path = repo_path / wrapper
            if wrapper_path.exists():
                os.chmod(wrapper_path, 0o755)
                logger.info(f"Made {wrapper} executable")

        # For now, we don't do any additional setup
        # Future: could download dependencies ahead of time
        return True

    def get_test_command(self) -> List[str]:
        """Get test command based on build tool."""
        if self.build_tool == "gradle-wrapper":
            return ["./gradlew", "test", "--info"]
        elif self.build_tool == "gradle":
            return ["gradle", "test", "--info"]
        elif self.build_tool == "maven-wrapper":
            return ["./mvnw", "test"]
        elif self.build_tool == "maven":
            return ["mvn", "test"]
        else:
            # Fallback to gradle wrapper
            return ["./gradlew", "test", "--info"]

    def get_junit_paths(self) -> List[Path]:
        """JUnit output paths for Java projects."""
        # Gradle puts results in build/test-results/test/
        # Maven puts results in target/surefire-reports/
        return [
            # Gradle paths
            Path("build/test-results/test"),
            Path("build/reports/tests/test/index.html"),
            # Maven paths
            Path("target/surefire-reports"),
            # Multi-module Gradle
            Path("*/build/test-results/test"),
        ]

    def classify_failure(self, output: str) -> str:
        """Classify Java test failure."""
        output_lower = output.lower()

        # Compilation errors
        if any(pattern in output_lower for pattern in [
            "compilation failed",
            "cannot find symbol",
            "package does not exist",
            "cannot resolve",
            "error: ",
        ]):
            return "import_error"  # Treat compilation as import-like

        # Test assertion failures
        if any(pattern in output_lower for pattern in [
            "assertionerror",
            "expected:",
            "but was:",
            "test failed",
        ]):
            return "logic_error"

        return "logic_error"

    def attempt_auto_fix(self, failure_type: str, output: str) -> bool:
        """
        Attempt to auto-fix Java test failures using Claude.

        Supports:
        - import_error: Missing imports, package resolution issues
        - syntax_error: Compilation errors

        Returns:
            True if fix was applied
        """
        if failure_type == "import_error":
            return self._fix_compilation_error(output)
        elif failure_type == "syntax_error":
            return self._fix_compilation_error(output)
        return False

    def _fix_compilation_error(self, output: str) -> bool:
        """
        Attempt to fix Java compilation errors using Claude.

        Parses the error output to find the problematic file and uses
        Claude to suggest a fix.
        """
        logger.info("[java] Starting Claude fix for compilation error")

        # Parse the file path from Java compiler error output
        # Format: "/path/to/File.java:10: error: cannot find symbol"
        error_file = self._parse_java_error_file(output)
        if not error_file:
            # Log a sample of the output to help debug why parsing failed
            output_preview = output[:500].replace('\n', '\\n')
            logger.warning(f"[java] Could not parse file path from compilation error. Output preview: {output_preview}")
            return False

        logger.info(f"[java] Compilation error in file: {error_file}")

        # Check if file exists (it might be a path from a different machine)
        if not os.path.exists(error_file):
            resolved_file = self._resolve_error_file_path(error_file)
            if resolved_file:
                error_file = resolved_file
                logger.info(f"[java] Resolved file path to: {error_file}")
            else:
                logger.warning(f"[java] File not found: {error_file}")
                return False

        # Apply Claude fix with KB context
        try:
            if apply_fix_to_file(error_file, output, "import_error", backend_type="java", config=self.config):
                logger.info(f"[java] Claude fix applied successfully to {error_file}")
                return True
            else:
                logger.warning(f"[java] Claude fix returned False for {error_file}")
                return False
        except Exception as e:
            logger.error(f"[java] Claude fix raised exception for {error_file}: {e}")
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

            # Add common Java markers
            source_roots.update(['src', 'app', 'main', 'test', 'java', 'kotlin'])

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

    def _parse_java_error_file(self, output: str) -> Optional[str]:
        """
        Parse Java compiler error output to find the file path.

        Handles formats:
        - Gradle: "/path/to/File.java:10: error: ..."
        - Maven: "[ERROR] /path/to/File.java:[10,5] ..."
        - Kotlin: "e: /path/to/File.kt: (10, 5): ..."

        Returns:
            Absolute path to the file, or None if not found
        """
        # Try standard Java compiler format
        match = re.search(r'([^\s:]+\.java):(\d+):', output)
        if match:
            return match.group(1)

        # Try Maven format
        match = re.search(r'\[ERROR\]\s+([^\s:]+\.java):\[(\d+)', output)
        if match:
            return match.group(1)

        # Try Kotlin format
        match = re.search(r'e:\s+([^\s:]+\.kt):\s*\((\d+)', output)
        if match:
            return match.group(1)

        # Try generic path with .java extension
        match = re.search(r'(/[^\s:]+\.java)', output)
        if match:
            return match.group(1)

        return None

    def _get_source_paths(self) -> List[str]:
        """
        Get list of source paths to search for local classes.

        Checks .outcomeops.yaml for configured source_paths and combines
        with common Java project layouts (additive, not replacement).

        Config example in .outcomeops.yaml:
            code_generation:
              backend: "java"
              java:
                source_paths:
                  - "src/main/java"
                  - "src/test/java"

        Returns:
            List of absolute paths to search for local classes
        """
        paths = []

        # Add configured source paths first (highest priority)
        java_config = self.config.get("code_generation", {}).get("java", {})
        configured_paths = java_config.get("source_paths", [])
        if configured_paths:
            logger.info(f"[java] Using configured source_paths: {configured_paths}")
            paths.extend([os.path.join(self.repo_dir, p) for p in configured_paths])

        # Always add default paths (don't replace, combine)
        default_paths = [
            # Standard Maven/Gradle layout
            "src/main/java",
            "src/test/java",
            # Kotlin
            "src/main/kotlin",
            "src/test/kotlin",
            # Multi-module projects
            "*/src/main/java",
            "*/src/test/java",
            # Android
            "app/src/main/java",
            "app/src/test/java",
        ]

        for p in default_paths:
            full_path = os.path.join(self.repo_dir, p)
            if full_path not in paths:
                paths.append(full_path)

        return paths


# Register the Java backend
register_backend("java", JavaBackend)
