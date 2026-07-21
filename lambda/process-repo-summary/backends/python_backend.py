"""
Python backend for code map generation.

Discovers Python source files, groups them by module/function,
and supports incremental updates via git-based change detection.

This backend uses the generic discovery pattern from ADR-009 for Python
source code.
"""

import logging
import os
import sys
from collections import defaultdict
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


class PythonBackend(CodeMapBackend):
    """
    Backend for Python applications.

    Discovers Python source files organized as:
        lambda/
            function-name/
                handler.py
                requirements.txt
                ...

    Also discovers frontend files (pages, components, utils, types, tests)
    for full-stack applications.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Lambda backend.

        Config options:
            - lambda_directory: Directory containing Lambda functions (default: "lambda")
            - handler_file: Handler filename (default: "handler.py")
            - include_submodules: Include non-handler Python files (default: True)
            - max_file_size_tokens: Skip files larger than this (default: 7000)
            - source_roots: Override default source roots for code unit discovery
            - github_token: GitHub API token (required for change detection)
            - github_api_url: GitHub API base URL (default: f"{_github_api()}")
        """
        super().__init__(config)
        self.lambda_directory = config.get("lambda_directory", "lambda")
        self.handler_file = config.get("handler_file", "handler.py")
        self.include_submodules = config.get("include_submodules", True)
        self.max_file_size_tokens = config.get("max_file_size_tokens", 7000)
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", f"{_github_api()}")

    def get_backend_name(self) -> str:
        return "Lambda Serverless Backend"

    def get_backend_type(self) -> str:
        return "lambda"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate Lambda backend configuration."""
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration (ADR-009)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """Python source file extensions."""
        return {".py"}

    def get_source_roots(self) -> List[str]:
        """
        Source roots for Lambda serverless projects.

        Can be overridden via source_roots in .outcomeops.yaml config.
        Default: lambda/ directory and src/ for shared modules.
        """
        configured_roots = self.config.get("source_roots")
        if configured_roots:
            return configured_roots

        return [
            self.lambda_directory,  # lambda/
            "src",                  # src/ for shared modules
        ]

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from repository files.

        Creates CodeUnit objects for:
        - Lambda handler groups (one per lambda/ subdirectory)
        - Infrastructure (all .tf files)
        - Frontend pages (pages/ and routes/ directories)
        - Frontend components (components/ directory)
        - Frontend utilities (utils/, hooks/, lib/)
        - Frontend types (types/ directory and .types.ts files)
        - Backend tests (test files in lambda/tests/)
        - Frontend tests (.test.tsx, .spec.ts, etc.)
        - Backend schemas (schema/model files)
        - Shared utilities (src/, utils/, common/)
        - Documentation (all .md files)

        Args:
            files: List of file objects from GitHub API

        Returns:
            List of discovered CodeUnit objects
        """
        code_units = []

        # Filter relevant files (exclude node_modules, build artifacts, etc.)
        relevant_files = [
            f for f in files
            if f["type"] == "blob" and not self.is_excluded_path(f["path"])
        ]

        # Group 1: Lambda handler groups
        handler_units = self._discover_lambda_handlers(relevant_files)
        code_units.extend(handler_units)

        # Group 2: Infrastructure (uses base class generic method)
        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        # Group 3: Frontend pages/routes
        pages_unit = self._discover_frontend_pages(relevant_files)
        if pages_unit:
            code_units.append(pages_unit)

        # Group 4: Frontend components
        components_unit = self._discover_frontend_components(relevant_files)
        if components_unit:
            code_units.append(components_unit)

        # Group 5: Backend tests
        test_units = self._discover_backend_tests(relevant_files)
        code_units.extend(test_units)

        # Group 6: Frontend tests
        frontend_test_unit = self._discover_frontend_tests(relevant_files)
        if frontend_test_unit:
            code_units.append(frontend_test_unit)

        # Group 7: Backend shared utilities
        shared_unit = self._discover_backend_shared(relevant_files)
        if shared_unit:
            code_units.append(shared_unit)

        # Group 8: Frontend utilities
        frontend_utils_unit = self._discover_frontend_utils(relevant_files)
        if frontend_utils_unit:
            code_units.append(frontend_utils_unit)

        # Group 9: Backend schemas
        schemas_unit = self._discover_backend_schemas(relevant_files)
        if schemas_unit:
            code_units.append(schemas_unit)

        # Group 10: Frontend types
        types_unit = self._discover_frontend_types(relevant_files)
        if types_unit:
            code_units.append(types_unit)

        # Group 11: Documentation (uses base class generic method)
        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        # Group 12: Kubernetes manifests (uses base class generic method)
        k8s_unit = self.discover_kubernetes(files)
        if k8s_unit:
            code_units.append(k8s_unit)

        logger.info(f"Discovered {len(code_units)} code units from {len(relevant_files)} files")
        return code_units

    def _discover_lambda_handlers(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """Discover Lambda handler groups."""
        handler_files = [
            f for f in files
            if f["path"].startswith(f"{self.lambda_directory}/") and f["path"].endswith(".py")
        ]

        # Group by Lambda function directory
        handler_groups = defaultdict(list)
        for file in handler_files:
            path_parts = file["path"].split("/")
            if len(path_parts) >= 2 and path_parts[0] == self.lambda_directory:
                function_name = path_parts[1]
                handler_groups[function_name].append(file["path"])

        # Create CodeUnit for each handler group
        code_units = []
        for function_name, file_paths in handler_groups.items():
            code_units.append(CodeUnit(
                name=function_name,
                unit_type="handler-group",
                file_paths=file_paths,
                metadata={"batch_type": "handler-group"}
            ))

        logger.info(f"Discovered {len(code_units)} Lambda handler groups")
        return code_units

    def _discover_frontend_pages(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover frontend page/route files."""
        page_files = [
            f["path"] for f in files
            if ("pages/" in f["path"] or "routes/" in f["path"])
            and (f["path"].endswith(".tsx") or f["path"].endswith(".jsx") or
                 f["path"].endswith(".ts") or f["path"].endswith(".js"))
        ]
        if not page_files:
            return None

        return CodeUnit(
            name="pages-routes",
            unit_type="frontend-pages",
            file_paths=page_files,
            metadata={"batch_type": "frontend-pages"}
        )

    def _discover_frontend_components(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover frontend component files."""
        component_files = [
            f["path"] for f in files
            if "components/" in f["path"]
            and (f["path"].endswith(".tsx") or f["path"].endswith(".jsx"))
        ]
        if not component_files:
            return None

        return CodeUnit(
            name="components",
            unit_type="frontend-components",
            file_paths=component_files,
            metadata={"batch_type": "frontend-components"}
        )

    def _discover_backend_tests(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """Discover backend test files grouped by type (unit, integration, fixtures)."""
        test_files = [
            f["path"] for f in files
            if "test" in f["path"].lower() and f["path"].endswith(".py")
        ]

        # Group by test type
        test_groups = defaultdict(list)
        for file_path in test_files:
            if "unit" in file_path.lower():
                test_groups["unit"].append(file_path)
            elif "integration" in file_path.lower():
                test_groups["integration"].append(file_path)
            elif "fixture" in file_path.lower():
                test_groups["fixtures"].append(file_path)
            else:
                test_groups["other"].append(file_path)

        # Create CodeUnit for each test group
        code_units = []
        for group_name, file_paths in test_groups.items():
            code_units.append(CodeUnit(
                name=group_name,
                unit_type="tests",
                file_paths=file_paths,
                metadata={"batch_type": "tests", "test_type": group_name}
            ))

        return code_units

    def _discover_frontend_tests(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover frontend test files."""
        frontend_test_files = [
            f["path"] for f in files
            if "test" in f["path"].lower() and (
                f["path"].endswith(".test.ts") or f["path"].endswith(".test.tsx") or
                f["path"].endswith(".test.js") or f["path"].endswith(".test.jsx") or
                f["path"].endswith(".spec.ts") or f["path"].endswith(".spec.tsx") or
                f["path"].endswith(".spec.js") or f["path"].endswith(".spec.jsx")
            )
        ]
        if not frontend_test_files:
            return None

        return CodeUnit(
            name="frontend-tests",
            unit_type="frontend-tests",
            file_paths=frontend_test_files,
            metadata={"batch_type": "frontend-tests"}
        )

    def _discover_backend_shared(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover shared backend utilities and common code."""
        shared_files = [
            f["path"] for f in files
            if any(pattern in f["path"].lower() for pattern in ["src/", "utils/", "common/", "shared/"])
            and f["path"].endswith(".py")
            and "__init__" not in f["path"]
        ]
        if not shared_files:
            return None

        return CodeUnit(
            name="shared-utilities",
            unit_type="shared",
            file_paths=shared_files,
            metadata={"batch_type": "shared"}
        )

    def _discover_frontend_utils(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover frontend utilities, hooks, and lib files."""
        frontend_utils_files = [
            f["path"] for f in files
            if any(pattern in f["path"] for pattern in ["utils/", "helpers/", "lib/", "hooks/", "context/"])
            and (f["path"].endswith(".ts") or f["path"].endswith(".tsx") or
                 f["path"].endswith(".js") or f["path"].endswith(".jsx"))
            and "test" not in f["path"].lower()
        ]
        if not frontend_utils_files:
            return None

        return CodeUnit(
            name="frontend-utilities",
            unit_type="frontend-utils",
            file_paths=frontend_utils_files,
            metadata={"batch_type": "frontend-utils"}
        )

    def _discover_backend_schemas(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover backend schema and model files."""
        schema_files = [
            f["path"] for f in files
            if ("schema" in f["path"].lower() or "model" in f["path"].lower())
            and f["path"].endswith(".py")
        ]
        if not schema_files:
            return None

        return CodeUnit(
            name="schemas-and-models",
            unit_type="schemas",
            file_paths=schema_files,
            metadata={"batch_type": "schemas"}
        )

    def _discover_frontend_types(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """Discover frontend TypeScript types and interfaces."""
        types_files = [
            f["path"] for f in files
            if ("types/" in f["path"] or f["path"].endswith(".types.ts") or
                f["path"].endswith("types.ts"))
            and (f["path"].endswith(".ts") or f["path"].endswith(".tsx"))
        ]
        if not types_files:
            return None

        return CodeUnit(
            name="types-interfaces",
            unit_type="frontend-types",
            file_paths=types_files,
            metadata={"batch_type": "frontend-types"}
        )

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
            # Return all units as changed (will be discovered by caller)
            return ChangeDetectionResult(
                has_changes=True,
                changed_units=[],  # Will be populated by handler
                unchanged_units=[],
                last_commit_sha=last_state.get("commit_sha") if last_state else None,
                current_commit_sha=current_sha,
                changed_files=[]  # Empty means full regeneration
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

        # Return changed files for incremental filtering
        return ChangeDetectionResult(
            has_changes=True,
            changed_units=[],  # Will be populated by comparing changed_files to code units
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
            "backend_type": "lambda",
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit.

        Args:
            code_unit: Code unit to generate key for

        Returns:
            Storage key (SK) for DynamoDB

        Examples:
            handler-group: "summary#handler#ingest-docs"
            infrastructure: "summary#infrastructure"
            tests: "summary#tests#unit"
        """
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)

        if batch_type == "handler-group":
            return f"summary#handler#{code_unit.name}"
        elif batch_type == "tests":
            return f"summary#tests#{code_unit.name}"
        elif batch_type in ["frontend-pages", "frontend-components", "frontend-utils", "frontend-types"]:
            frontend_type = batch_type.replace("frontend-", "")
            return f"summary#frontend#{frontend_type}"
        elif batch_type == "kubernetes":
            return "summary#kubernetes"
        else:
            return f"summary#{batch_type}"


# Auto-register Python backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("python", PythonBackend)
