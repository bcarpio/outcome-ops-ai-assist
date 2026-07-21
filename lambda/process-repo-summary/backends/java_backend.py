"""
Java backend for code map generation.

Discovers Java/Kotlin code units using the ADR-009 algorithm, supporting:
- Maven and Gradle projects
- Spring Boot microservices
- AWS Lambda handlers
- Standard Java package conventions

This backend uses the generic discovery pattern from base.py, configured
for Java/Kotlin source files and Maven/Gradle project structures.
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import CodeMapBackend, CodeUnit, ChangeDetectionResult
except ImportError:
    # Add backends directory to path for testing
    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)
    from base import CodeMapBackend, CodeUnit, ChangeDetectionResult  # noqa: F401


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    so module load order (check_handlers before sys.path fix) is safe."""
    from shared.github_base import github_api_base
    return github_api_base()


logger = logging.getLogger(__name__)


class JavaBackend(CodeMapBackend):
    """
    Backend for Java/Kotlin projects.

    Supports:
    - Maven projects (src/main/java, src/test/java)
    - Gradle projects (same structure)
    - Spring Boot microservices
    - AWS Lambda handlers
    - Multi-module projects

    Uses ADR-009 generic discovery algorithm to find code unit boundaries
    based on package depth, file count, and boundary pattern matching.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Java backend.

        Config options:
            - source_roots: Override default source roots (default: Maven/Gradle conventions)
            - include_kotlin: Include .kt files (default: True)
            - min_files_threshold: Minimum files to form code unit (default: 2)
            - custom_boundary_patterns: Additional boundary patterns
            - github_token: GitHub API token (required for change detection)
            - github_api_url: GitHub API base URL (default: f"{_github_api()}")
        """
        super().__init__(config)
        self.include_kotlin = config.get("include_kotlin", True)
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", f"{_github_api()}")

    def get_backend_name(self) -> str:
        return "Java Backend"

    def get_backend_type(self) -> str:
        return "java"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate Java backend configuration."""
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration (ADR-009)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """Java and Kotlin source file extensions."""
        extensions = {".java"}
        if self.include_kotlin:
            extensions.add(".kt")
            extensions.add(".kts")
        return extensions

    def get_source_roots(self) -> List[str]:
        """
        Source roots for Java/Maven/Gradle projects.

        Standard Maven/Gradle convention is:
        - src/main/java - main source code
        - src/main/kotlin - Kotlin source code
        - src/test/java - test code
        - src/test/kotlin - Kotlin tests

        We also support non-standard layouts like:
        - src/ - simple projects
        - java/ - some legacy layouts
        """
        configured_roots = self.config.get("source_roots")
        if configured_roots:
            return configured_roots

        roots = [
            "src/main/java",
            "src/main/kotlin",
            "src",
            "java",
        ]
        return roots

    def get_test_source_roots(self) -> List[str]:
        """Test source roots for Java projects."""
        return [
            "src/test/java",
            "src/test/kotlin",
            "src/integrationTest/java",
            "src/integrationTest/kotlin",
        ]

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from Java repository files.

        Uses the generic ADR-009 algorithm for source code discovery,
        plus Java-specific discovery for:
        - Build configuration (pom.xml, build.gradle)
        - Infrastructure (Terraform, Kubernetes manifests)
        - Tests (grouped by type)
        - Documentation

        Args:
            files: List of file objects from GitHub API

        Returns:
            List of discovered CodeUnit objects
        """
        code_units = []

        # Group 1: Source code units (uses generic ADR-009 algorithm)
        source_units = self.discover_source_units(files)
        code_units.extend(source_units)

        # Group 2: Test code units
        test_units = self._discover_java_tests(files)
        code_units.extend(test_units)

        # Group 3: Build configuration
        build_unit = self._discover_build_config(files)
        if build_unit:
            code_units.append(build_unit)

        # Group 4: Infrastructure (Terraform)
        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        # Group 5: Kubernetes manifests (uses base class generic method)
        k8s_unit = self.discover_kubernetes(files)
        if k8s_unit:
            code_units.append(k8s_unit)

        # Group 6: Documentation
        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        logger.info(f"Discovered {len(code_units)} code units")
        return code_units

    def _discover_java_tests(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover Java test files grouped by type.

        Groups tests into:
        - unit: Tests in src/test/java or with "unit" in path
        - integration: Tests in src/integrationTest or with "integration" in path
        - e2e: End-to-end tests

        Args:
            files: List of file objects from GitHub API

        Returns:
            List of CodeUnit objects for test groups
        """
        test_roots = self.get_test_source_roots()

        # Find all test files
        # Note: We don't use is_excluded_path here because test directories
        # are intentionally excluded from source discovery but should be
        # discovered by this test-specific method
        test_files = []
        for f in files:
            if f["type"] != "blob":
                continue
            path = f["path"]
            if not self.is_source_file(path):
                continue

            # Skip build outputs and dependencies (but NOT test directories)
            skip_patterns = {"target/", "build/", "dist/", "node_modules/", "__pycache__/"}
            if any(pattern in path.lower() for pattern in skip_patterns):
                continue

            # Check if file is in a test directory
            is_test = False
            for root in test_roots:
                if path.startswith(root):
                    is_test = True
                    break
            # Also check for "test" in filename (Java convention: *Test.java)
            if path.lower().endswith("test.java") or path.lower().endswith("test.kt"):
                is_test = True
            if "test" in path.lower():
                is_test = True

            if is_test:
                test_files.append(path)

        if not test_files:
            return []

        # Group by test type
        test_groups: Dict[str, List[str]] = {
            "unit": [],
            "integration": [],
            "e2e": [],
            "other": [],
        }

        for file_path in test_files:
            path_lower = file_path.lower()
            if "integration" in path_lower or "integ" in path_lower:
                test_groups["integration"].append(file_path)
            elif "e2e" in path_lower or "end-to-end" in path_lower or "acceptance" in path_lower:
                test_groups["e2e"].append(file_path)
            elif "src/test/" in path_lower or "unit" in path_lower:
                test_groups["unit"].append(file_path)
            else:
                test_groups["other"].append(file_path)

        # Create CodeUnits for non-empty groups
        code_units = []
        for group_name, group_files in test_groups.items():
            if group_files:
                code_units.append(CodeUnit(
                    name=f"tests-{group_name}",
                    unit_type="tests",
                    file_paths=sorted(group_files),
                    metadata={"batch_type": "tests", "test_type": group_name}
                ))

        return code_units

    def _discover_build_config(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover build configuration files.

        Includes:
        - pom.xml (Maven)
        - build.gradle, build.gradle.kts (Gradle)
        - settings.gradle, settings.gradle.kts (Gradle)
        - gradle.properties
        - gradlew, gradlew.bat

        Args:
            files: List of file objects from GitHub API

        Returns:
            CodeUnit for build configuration, or None
        """
        build_patterns = {
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradle.properties",
        }

        build_files = []
        for f in files:
            if f["type"] != "blob":
                continue
            filename = f["path"].split("/")[-1]
            if filename in build_patterns:
                build_files.append(f["path"])
            # Also include gradle wrapper
            if "gradle" in f["path"].lower() and f["path"].endswith(".properties"):
                build_files.append(f["path"])

        if not build_files:
            return None

        return CodeUnit(
            name="build-config",
            unit_type="build",
            file_paths=sorted(build_files),
            metadata={"batch_type": "build"}
        )

    # =========================================================================
    # Change Detection
    # =========================================================================

    def detect_changes(
        self,
        repo: str,
        repo_project: str,
        last_state: Optional[Dict[str, Any]],
        force_full: bool = False
    ) -> ChangeDetectionResult:
        """
        Detect changes using git commit comparison.

        Args:
            repo: Repository name
            repo_project: Repository project (owner/repo)
            last_state: Previous state with commit_sha
            force_full: If True, return all units as changed

        Returns:
            ChangeDetectionResult with changed/unchanged units
        """
        # Get current commit SHA from GitHub API
        current_sha = self._get_current_commit_sha(repo_project)

        if force_full or not last_state or "commit_sha" not in last_state:
            logger.info(f"Full regeneration requested or no previous state for {repo}")
            return ChangeDetectionResult(
                has_changes=True,
                changed_units=[],
                unchanged_units=[],
                last_commit_sha=last_state.get("commit_sha") if last_state else None,
                current_commit_sha=current_sha,
                changed_files=[]
            )

        last_sha = last_state["commit_sha"]

        if current_sha == last_sha:
            logger.info(f"No changes detected for {repo}: {current_sha}")
            return ChangeDetectionResult(
                has_changes=False,
                changed_units=[],
                unchanged_units=[],
                last_commit_sha=last_sha,
                current_commit_sha=current_sha,
                changed_files=[],
                removed_files=[]
            )

        # Get changed and removed files via git compare API
        changed_files, removed_files = self._get_changed_files(repo_project, last_sha, current_sha)
        logger.info(f"Found {len(changed_files)} changed files, {len(removed_files)} removed files between {last_sha[:7]}..{current_sha[:7]}")

        return ChangeDetectionResult(
            has_changes=True,
            changed_units=[],
            unchanged_units=[],
            last_commit_sha=last_sha,
            current_commit_sha=current_sha,
            changed_files=changed_files,
            removed_files=removed_files
        )

    def _get_default_branch(self, repo_project: str) -> str:
        """Get the default branch for a repository."""
        url = f"{self.github_api_url}/repos/{repo_project}"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcome-ops-generate-code-maps",
        }

        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                default_branch = data.get("default_branch", "main")
                logger.info(f"Default branch for {repo_project}: {default_branch}")
                return default_branch
        except URLError as e:
            logger.warning(f"Failed to get default branch for {repo_project}, using 'main': {e}")
            return "main"

    def _get_current_commit_sha(self, repo_project: str) -> str:
        """Get current commit SHA from GitHub API."""
        default_branch = self._get_default_branch(repo_project)
        url = f"{self.github_api_url}/repos/{repo_project}/branches/{default_branch}"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcome-ops-generate-code-maps",
        }

        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data["commit"]["sha"]
        except URLError as e:
            logger.error(f"Failed to get current commit SHA: {e}")
            raise

    def _get_changed_files(self, repo_project: str, base_sha: str, head_sha: str) -> tuple[List[str], List[str]]:
        """
        Get list of changed and removed files between two commits.

        Args:
            repo_project: Repository project path (owner/repo)
            base_sha: Base commit SHA
            head_sha: Head commit SHA

        Returns:
            Tuple of (changed_files, removed_files) where:
            - changed_files: List of added or modified file paths
            - removed_files: List of deleted file paths
        """
        url = f"{self.github_api_url}/repos/{repo_project}/compare/{base_sha}...{head_sha}"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcome-ops-generate-code-maps",
        }

        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                files = data.get("files", [])

                changed_files = []
                removed_files = []

                for f in files:
                    filename = f["filename"]
                    status = f.get("status", "modified")

                    if status == "removed":
                        removed_files.append(filename)
                    else:
                        # added, modified, renamed, copied, changed
                        changed_files.append(filename)

                logger.info(f"Changed files: {len(changed_files)}, Removed files: {len(removed_files)}")
                return (changed_files, removed_files)
        except URLError as e:
            logger.error(f"Failed to get changed files: {e}")
            raise

    # =========================================================================
    # Batch Metadata and Storage
    # =========================================================================

    def generate_batch_metadata(self, code_unit: CodeUnit, repo: str) -> Dict[str, Any]:
        """
        Generate batch metadata for SQS message.

        Args:
            code_unit: Code unit to generate metadata for
            repo: Repository name

        Returns:
            Batch metadata dictionary
        """
        return {
            "batch_type": code_unit.metadata.get("batch_type", code_unit.unit_type),
            "group_name": code_unit.name,
            "files": [{"path": path} for path in code_unit.file_paths],
            "storage_key": self.get_storage_key(code_unit),
            "backend_type": "java",
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit.

        Args:
            code_unit: Code unit to generate key for

        Returns:
            Storage key (SK) for DynamoDB

        Examples:
            source-module: "summary#module#common-notification-email"
            tests: "summary#tests#unit"
            build: "summary#build"
        """
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)

        if batch_type == "source-module":
            return f"summary#module#{code_unit.name}"
        elif batch_type == "tests":
            return f"summary#tests#{code_unit.name}"
        elif batch_type == "kubernetes":
            return "summary#kubernetes"
        elif batch_type == "build":
            return "summary#build"
        else:
            return f"summary#{batch_type}"


# Auto-register Java backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("java", JavaBackend)
