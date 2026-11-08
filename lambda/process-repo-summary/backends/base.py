"""
Base abstract class for code map generation backends.

Each backend implements methods for:
- Discovering code units (e.g., Lambda handlers, K8s services, modules)
- Detecting changes since last run (git diff, timestamps, etc.)
- Generating architecture-specific prompts for Claude
- Parsing Claude's output into structured code maps

See ADR-009 for the code unit discovery pattern documentation.
"""

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Default boundary pattern suffixes that indicate a logical code unit boundary
# See ADR-009 for rationale
DEFAULT_BOUNDARY_PATTERNS: Set[str] = {
    # Service/Business Logic
    "service", "services",
    "handler", "handlers",
    "controller", "controllers",
    "api", "apis",
    "client", "clients",
    "worker", "workers",
    "processor", "processors",
    "listener", "listeners",
    # Data/Models
    "model", "models",
    "entity", "entities",
    "dto", "dtos",
    "schema", "schemas",
    "domain",
    # Infrastructure
    "repository", "repositories",
    "dao", "daos",
    "adapter", "adapters",
    "gateway", "gateways",
    # Utilities
    "util", "utils", "utility", "utilities",
    "helper", "helpers",
    "common",
    "shared",
    "core",
    "lib",
    # Configuration
    "config", "configuration",
    "security",
    "auth", "authentication",
    # Implementation patterns (common in Java)
    "impl",
    "internal",
    "legacy",
}

# Default excluded paths
DEFAULT_EXCLUDED_PATTERNS: Set[str] = {
    # Build outputs
    "target/", "build/", "dist/", "out/", "bin/",
    # Dependencies
    "node_modules/", "vendor/", ".venv/", "venv/",
    # IDE/tooling
    ".git/", ".idea/", ".vscode/", "__pycache__/", ".pytest_cache/",
    # Test directories (tests are discovered separately)
    "src/test/java/", "src/test/kotlin/", "src/test/resources/",
    "src/integrationTest/", "src/e2e/",
    "test-resources/", "testdata/",
    # Generated code
    "generated/", "gen/",
    # Other
    "coverage/",
}

# Build module marker files that create automatic boundaries
BUILD_MODULE_MARKERS: Set[str] = {
    "pom.xml",              # Maven
    "build.gradle",         # Gradle
    "build.gradle.kts",     # Gradle Kotlin DSL
    "settings.gradle",      # Gradle multi-module root
    "settings.gradle.kts",  # Gradle Kotlin multi-module root
    "gradle.properties",    # Gradle properties (often indicates module)
    "package.json",         # Node.js
    "pyproject.toml",       # Python
    "setup.py",             # Python legacy
    "go.mod",               # Go
    "Cargo.toml",           # Rust
}


class CodeUnit:
    """
    Represents a logical unit of code for analysis.

    For Lambda: A single Lambda function with its handler and dependencies
    For K8s: A microservice with its deployment/service manifests
    For Monolith: A module or package boundary
    """

    def __init__(
        self,
        name: str,
        unit_type: str,
        file_paths: List[str],
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize a code unit.

        Args:
            name: Unique name for this code unit (e.g., "ingest-docs", "auth-service")
            unit_type: Type of unit (e.g., "lambda-handler", "k8s-service", "module")
            file_paths: List of file paths belonging to this unit
            metadata: Additional metadata (e.g., runtime, dependencies, config)
        """
        self.name = name
        self.unit_type = unit_type
        self.file_paths = file_paths
        self.metadata = metadata or {}

    def __repr__(self) -> str:
        return f"CodeUnit(name={self.name}, type={self.unit_type}, files={len(self.file_paths)})"


class ChangeDetectionResult:
    """
    Result of change detection analysis.

    Contains information about which code units have changed since last run.
    """

    def __init__(
        self,
        has_changes: bool,
        changed_units: List[CodeUnit],
        unchanged_units: List[CodeUnit],
        last_commit_sha: Optional[str] = None,
        current_commit_sha: Optional[str] = None,
        changed_files: Optional[List[str]] = None,
        removed_files: Optional[List[str]] = None
    ):
        """
        Initialize change detection result.

        Args:
            has_changes: True if any changes detected
            changed_units: List of code units that have changed
            unchanged_units: List of code units that haven't changed
            last_commit_sha: Previous commit SHA (for git-based detection)
            current_commit_sha: Current commit SHA
            changed_files: List of changed file paths (for incremental filtering)
            removed_files: List of deleted file paths (for stale data cleanup)
        """
        self.has_changes = has_changes
        self.changed_units = changed_units
        self.unchanged_units = unchanged_units
        self.last_commit_sha = last_commit_sha
        self.current_commit_sha = current_commit_sha
        self.changed_files = changed_files or []
        self.removed_files = removed_files or []

    def __repr__(self) -> str:
        return (
            f"ChangeDetectionResult(has_changes={self.has_changes}, "
            f"changed={len(self.changed_units)}, unchanged={len(self.unchanged_units)}, "
            f"files={len(self.changed_files)}, removed={len(self.removed_files)})"
        )


class CodeMapBackend(ABC):
    """
    Abstract base class for code map generation backends.

    Each backend implements architecture-specific logic for discovering,
    analyzing, and generating code maps.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize backend with configuration.

        Args:
            config: Backend-specific configuration dictionary
        """
        self.config = config

    @abstractmethod
    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from repository file structure.

        Args:
            files: List of file objects from GitHub API (path, type, name)

        Returns:
            List of discovered code units

        Example:
            Lambda backend: Returns one CodeUnit per Lambda function directory
            K8s backend: Returns one CodeUnit per microservice
            Monolith backend: Returns CodeUnit per module/package
        """
        pass

    @abstractmethod
    def detect_changes(
        self,
        repo: str,
        repo_project: str,
        last_state: Optional[Dict[str, Any]],
        force_full: bool = False
    ) -> ChangeDetectionResult:
        """
        Detect which code units have changed since last run.

        Args:
            repo: Repository name
            repo_project: Repository project path (owner/repo)
            last_state: Previous state (commit SHA, timestamp, etc.)
            force_full: If True, return all units as changed (full regeneration)

        Returns:
            ChangeDetectionResult with changed and unchanged units

        Example:
            Git-based: Compare current commit SHA to last_state['commit_sha']
            Timestamp-based: Compare file modification times
            Full: Return all units as changed if force_full=True
        """
        pass

    @abstractmethod
    def generate_batch_metadata(self, code_unit: CodeUnit, repo: str) -> Dict[str, Any]:
        """
        Generate batch metadata for SQS processing.

        Args:
            code_unit: Code unit to generate metadata for
            repo: Repository name

        Returns:
            Batch metadata dictionary for SQS message

        Example:
            {
                "batch_type": "handler-group",
                "group_name": "ingest-docs",
                "files": [...],
                "storage_key": "summary#handler#ingest-docs",
                "backend_type": "lambda-serverless"
            }
        """
        pass

    @abstractmethod
    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit summary.

        Args:
            code_unit: Code unit to generate key for

        Returns:
            Storage key string (SK in DynamoDB)

        Example:
            Lambda: "summary#handler#ingest-docs"
            K8s: "summary#service#auth-api"
            Monolith: "summary#module#user-management"
        """
        pass

    def get_backend_name(self) -> str:
        """
        Get human-readable backend name.

        Returns:
            Backend name (e.g., "Lambda Serverless", "Kubernetes", "Monolith")
        """
        return self.__class__.__name__

    def get_backend_type(self) -> str:
        """
        Get backend type identifier.

        Returns:
            Backend type string (e.g., "lambda", "k8s", "monolith")
        """
        return "unknown"

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """
        Validate backend configuration.

        Returns:
            Tuple of (is_valid, error_message)

        Example:
            Lambda backend: Check lambda_directory exists
            K8s backend: Check manifest_path exists
        """
        return True, None

    # =========================================================================
    # Generic Code Unit Discovery (ADR-009)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """
        Get source file extensions for this backend's language.

        Override in subclasses to specify language-specific extensions.

        Returns:
            Set of file extensions (e.g., {".py"}, {".java", ".kt"})
        """
        return set()

    def get_source_roots(self) -> List[str]:
        """
        Get source root directories to scan.

        Override in subclasses to specify language-specific source roots.

        Returns:
            List of source root paths (e.g., ["src/main/java"], ["lambda"])
        """
        return []

    def get_boundary_patterns(self) -> Set[str]:
        """
        Get directory name patterns that indicate code unit boundaries.

        Override to add custom patterns beyond DEFAULT_BOUNDARY_PATTERNS.

        Returns:
            Set of boundary pattern names
        """
        custom = set(self.config.get("custom_boundary_patterns", []))
        return DEFAULT_BOUNDARY_PATTERNS | custom

    def get_excluded_patterns(self) -> Set[str]:
        """
        Get path patterns to exclude from discovery.

        Override to add custom exclusions beyond DEFAULT_EXCLUDED_PATTERNS.

        Returns:
            Set of exclusion patterns
        """
        custom = set(self.config.get("exclude_patterns", []))
        return DEFAULT_EXCLUDED_PATTERNS | custom

    def get_min_files_threshold(self) -> int:
        """
        Get minimum number of source files to form a code unit.

        Returns:
            Minimum file count (default: 2)
        """
        return self.config.get("min_files_threshold", 2)

    def is_excluded_path(self, path: str) -> bool:
        """
        Check if a path should be excluded from analysis.

        Args:
            path: File or directory path

        Returns:
            True if path should be excluded
        """
        path_lower = path.lower()
        for pattern in self.get_excluded_patterns():
            if pattern.lower() in path_lower:
                return True
        return False

    def is_source_file(self, path: str) -> bool:
        """
        Check if a file is a source file for this backend's language.

        Args:
            path: File path

        Returns:
            True if file has a recognized source extension
        """
        for ext in self.get_source_extensions():
            if path.endswith(ext):
                return True
        return False

    def is_boundary_directory(self, dir_name: str) -> bool:
        """
        Check if a directory name matches a boundary pattern.

        Args:
            dir_name: Directory name (not full path)

        Returns:
            True if directory name matches a boundary pattern
        """
        return dir_name.lower() in self.get_boundary_patterns()

    def _match_source_root(self, dir_path: str, source_roots: List[str]) -> Optional[str]:
        """
        Find the source root that matches this directory path.

        Handles both simple projects (src/main/java/...) and multi-module
        projects (module-name/src/main/java/...) by checking if any source
        root appears as a path segment boundary within the directory path.

        Args:
            dir_path: Full directory path
            source_roots: List of source root paths

        Returns:
            The matched prefix up to and including the source root, or None
        """
        for root in source_roots:
            if dir_path.startswith(root + "/") or dir_path == root:
                return root
            # Multi-module: root appears after a module prefix
            marker = "/" + root
            idx = dir_path.find(marker + "/")
            if idx == -1 and dir_path.endswith(marker):
                idx = dir_path.find(marker)
            if idx != -1:
                return dir_path[:idx + len(marker)]
        return None

    def is_namespace_container(self, dir_path: str, file_count: int, source_roots: List[str]) -> bool:
        """
        Check if a directory is a pure namespace container (should be traversed through).

        Handles Java-style deep package hierarchies like:
        com/company/project/service/impl/deep/nested

        A directory is a namespace container if:
        1. It has 0 source files directly in it
        2. Its depth relative to source root is > 4
        3. All path parts are simple identifiers (no special chars)

        This prevents creating code units for directories that are just
        namespace organization with no actual code.

        Args:
            dir_path: Full directory path
            file_count: Number of source files directly in this directory
            source_roots: List of source root paths

        Returns:
            True if this is a pure namespace container to traverse through
        """
        if file_count > 0:
            return False

        # Calculate depth relative to source root
        relative_path = dir_path
        matched_root = self._match_source_root(dir_path, source_roots)
        if matched_root:
            relative_path = dir_path[len(matched_root):].lstrip("/")

        path_parts = relative_path.split("/") if relative_path else []
        depth = len(path_parts)

        # If depth > 4 and all parts are simple identifiers, it's a namespace container
        if depth > 4:
            # Check if all parts are simple identifiers (alphanumeric, common Java package names)
            for part in path_parts:
                if not part.replace("_", "").replace("-", "").isalnum():
                    return False
            return True

        return False

    def has_build_module_marker(self, dir_path: str, all_files: List[Dict[str, Any]]) -> bool:
        """
        Check if a directory contains a build module marker file.

        Args:
            dir_path: Directory path to check
            all_files: List of all files from GitHub API

        Returns:
            True if directory contains a build marker (pom.xml, build.gradle, etc.)
        """
        for f in all_files:
            if f["type"] != "blob":
                continue
            file_path = f["path"]
            # Check if file is directly in this directory
            if "/" in file_path:
                file_dir = "/".join(file_path.split("/")[:-1])
                file_name = file_path.split("/")[-1]
            else:
                file_dir = ""
                file_name = file_path

            if file_dir == dir_path and file_name in BUILD_MODULE_MARKERS:
                return True
        return False

    def discover_source_units(
        self,
        files: List[Dict[str, Any]],
        source_roots: Optional[List[str]] = None
    ) -> List[CodeUnit]:
        """
        Generic discovery of source code units using ADR-009 algorithm.

        Traverses source directories and creates CodeUnits based on:
        1. Minimum file threshold (2+ source files)
        2. Boundary pattern matching (service, model, handler, etc.)
        3. Build module markers (pom.xml, build.gradle, etc.)

        Args:
            files: List of file objects from GitHub API (path, type, name)
            source_roots: Optional override for source roots to scan

        Returns:
            List of discovered CodeUnit objects
        """
        roots = source_roots or self.get_source_roots()
        if not roots:
            logger.warning("No source roots configured for discovery")
            return []

        # Filter to relevant source files
        source_files = [
            f for f in files
            if f["type"] == "blob"
            and self.is_source_file(f["path"])
            and not self.is_excluded_path(f["path"])
        ]

        if not source_files:
            logger.info("No source files found matching extensions")
            return []

        # Group files by directory
        files_by_dir: Dict[str, List[str]] = defaultdict(list)
        for f in source_files:
            path = f["path"]
            if "/" in path:
                dir_path = "/".join(path.split("/")[:-1])
            else:
                dir_path = ""
            files_by_dir[dir_path].append(path)

        # Find all directories under source roots
        # Supports both simple (src/main/java/...) and multi-module
        # (module-name/src/main/java/...) project layouts
        dirs_to_check = set()
        for dir_path in files_by_dir.keys():
            if self._match_source_root(dir_path, roots) is not None:
                dirs_to_check.add(dir_path)

        # Sort by depth (deepest first) so we can mark parent dirs as traversed
        sorted_dirs = sorted(dirs_to_check, key=lambda d: d.count("/"), reverse=True)

        code_units = []
        processed_dirs: Set[str] = set()

        for dir_path in sorted_dirs:
            # Skip if already processed (a child became a code unit)
            if dir_path in processed_dirs:
                continue

            # Skip if a parent directory already became a code unit
            parent_processed = False
            for processed in processed_dirs:
                if dir_path.startswith(processed + "/"):
                    parent_processed = True
                    break
            if parent_processed:
                continue

            dir_files = files_by_dir.get(dir_path, [])
            dir_name = dir_path.split("/")[-1] if "/" in dir_path else dir_path
            file_count = len(dir_files)

            # Skip pure namespace containers (deep Java package hierarchies with no files)
            # This handles com/company/project/service/impl/deep/nested patterns
            if self.is_namespace_container(dir_path, file_count, roots):
                logger.debug(f"Skipping namespace container: {dir_path}")
                continue

            # Check if this directory should be a code unit
            is_code_unit = False
            reason = ""

            # Criterion 1: Has build module marker
            if self.has_build_module_marker(dir_path, files):
                is_code_unit = True
                reason = "build-module"

            # Criterion 2: Meets minimum file threshold
            elif file_count >= self.get_min_files_threshold():
                is_code_unit = True
                reason = f"file-count-{file_count}"

            # Criterion 3: Matches boundary pattern
            elif file_count >= 1 and self.is_boundary_directory(dir_name):
                is_code_unit = True
                reason = f"boundary-pattern-{dir_name}"

            if is_code_unit:
                # Collect all files in this directory and subdirectories
                all_unit_files = []
                for check_dir, check_files in files_by_dir.items():
                    if check_dir == dir_path or check_dir.startswith(dir_path + "/"):
                        all_unit_files.extend(check_files)
                        processed_dirs.add(check_dir)

                # Create descriptive unit name from path
                unit_name = self._generate_unit_name(dir_path, roots)

                code_units.append(CodeUnit(
                    name=unit_name,
                    unit_type="source-module",
                    file_paths=sorted(all_unit_files),
                    metadata={
                        "batch_type": "source-module",
                        "source_root": next((r for r in roots if dir_path.startswith(r)), ""),
                        "directory": dir_path,
                        "discovery_reason": reason,
                    }
                ))

                logger.debug(f"Created code unit '{unit_name}' ({reason}): {len(all_unit_files)} files")

        logger.info(f"Discovered {len(code_units)} source code units")
        return code_units

    def _generate_unit_name(self, dir_path: str, source_roots: List[str]) -> str:
        """
        Generate a human-readable name for a code unit from its directory path.

        Strips source root prefix and converts path to readable name.

        Args:
            dir_path: Full directory path
            source_roots: List of source root paths

        Returns:
            Human-readable unit name (e.g., "common-notification-email")
        """
        # Strip source root prefix (supports multi-module layouts)
        relative_path = dir_path
        matched_root = self._match_source_root(dir_path, source_roots)
        if matched_root:
            relative_path = dir_path[len(matched_root):].lstrip("/")

        # When the directory IS the source root (nothing left after stripping),
        # name the unit after the root's own basename rather than producing an
        # empty name (e.g. source_roots=["shared_lib"] -> "shared_lib").
        if not relative_path:
            base_name = (matched_root or dir_path).rstrip("/").rsplit("/", 1)[-1]
            return base_name or "root"

        # For Java-style paths, strip the package prefix (com/company/project)
        # We keep the meaningful part after common prefixes
        parts = relative_path.split("/")

        # Skip common package prefixes (com, org, net, etc. plus company name)
        skip_prefixes = {"com", "org", "net", "io", "ai", "dev"}
        start_idx = 0
        for i, part in enumerate(parts):
            if part.lower() in skip_prefixes:
                start_idx = i + 2  # Skip prefix + company name
                break

        # Use remaining parts, but cap at reasonable length
        meaningful_parts = parts[start_idx:] if start_idx < len(parts) else parts[-3:]

        if not meaningful_parts:
            meaningful_parts = parts[-2:] if len(parts) >= 2 else parts

        return "-".join(meaningful_parts)

    def discover_infrastructure(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover infrastructure-as-code files.

        Args:
            files: List of file objects from GitHub API

        Returns:
            CodeUnit for infrastructure files, or None if not found
        """
        infra_extensions = {".tf", ".tfvars"}
        infra_files = [
            f["path"] for f in files
            if f["type"] == "blob"
            and any(f["path"].endswith(ext) for ext in infra_extensions)
            and not self.is_excluded_path(f["path"])
        ]

        if not infra_files:
            return None

        return CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=sorted(infra_files),
            metadata={"batch_type": "infrastructure"}
        )

    def discover_documentation(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover documentation files.

        Args:
            files: List of file objects from GitHub API

        Returns:
            CodeUnit for documentation, or None if not found
        """
        doc_files = [
            f["path"] for f in files
            if f["type"] == "blob"
            and f["path"].endswith(".md")
            and not self.is_excluded_path(f["path"])
        ]

        if not doc_files:
            return None

        return CodeUnit(
            name="documentation",
            unit_type="docs",
            file_paths=sorted(doc_files),
            metadata={"batch_type": "docs"}
        )

    def discover_tests(
        self,
        files: List[Dict[str, Any]],
        test_patterns: Optional[List[str]] = None
    ) -> List[CodeUnit]:
        """
        Discover test files grouped by type.

        Args:
            files: List of file objects from GitHub API
            test_patterns: Optional patterns to identify test files

        Returns:
            List of CodeUnit objects for test groups
        """
        # Default test identification: "test" in path and matches source extension
        test_files = [
            f["path"] for f in files
            if f["type"] == "blob"
            and "test" in f["path"].lower()
            and self.is_source_file(f["path"])
            and not self.is_excluded_path(f["path"])
        ]

        if not test_files:
            return []

        # Group by test type
        test_groups: Dict[str, List[str]] = defaultdict(list)
        for file_path in test_files:
            path_lower = file_path.lower()
            if "unit" in path_lower:
                test_groups["unit"].append(file_path)
            elif "integration" in path_lower or "integ" in path_lower:
                test_groups["integration"].append(file_path)
            elif "e2e" in path_lower or "end-to-end" in path_lower:
                test_groups["e2e"].append(file_path)
            elif "fixture" in path_lower:
                test_groups["fixtures"].append(file_path)
            else:
                test_groups["other"].append(file_path)

        code_units = []
        for group_name, group_files in test_groups.items():
            code_units.append(CodeUnit(
                name=f"tests-{group_name}",
                unit_type="tests",
                file_paths=sorted(group_files),
                metadata={"batch_type": "tests", "test_type": group_name}
            ))

        return code_units

    def discover_kubernetes(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover Kubernetes manifest files.

        This is language-agnostic - any application (Python, Java, TypeScript, Go, etc.)
        can be deployed to Kubernetes.

        Looks for YAML files in common K8s directories:
        - k8s/
        - kubernetes/
        - deploy/
        - manifests/
        - helm/
        - charts/

        Args:
            files: List of file objects from GitHub API

        Returns:
            CodeUnit for K8s manifests, or None if not found
        """
        k8s_dirs = {"k8s/", "kubernetes/", "deploy/", "manifests/", "helm/", "charts/"}

        k8s_files = []
        for f in files:
            if f["type"] != "blob":
                continue
            path = f["path"]

            # Check if in K8s directory
            in_k8s_dir = any(k8s_dir in path.lower() for k8s_dir in k8s_dirs)

            # Check if YAML file
            is_yaml = path.endswith(".yaml") or path.endswith(".yml")

            if in_k8s_dir and is_yaml:
                k8s_files.append(path)

        if not k8s_files:
            return None

        return CodeUnit(
            name="kubernetes",
            unit_type="kubernetes",
            file_paths=sorted(k8s_files),
            metadata={"batch_type": "kubernetes"}
        )
