"""
TypeScript/Node.js backend for code map generation.

Discovers TypeScript/JavaScript code units using the ADR-009 algorithm, supporting:
- Serverless Lambda handlers (handler.ts, *-handler.ts)
- Express/Fastify/NestJS API routes
- React/Vue/Svelte frontend components
- Next.js/Remix pages and routes
- Monorepo structures (packages/, apps/, libs/)

This backend uses the generic discovery pattern from base.py, configured
for TypeScript/JavaScript source files and Node.js project structures.
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

logger = logging.getLogger(__name__)


class TypeScriptBackend(CodeMapBackend):
    """
    Backend for TypeScript/JavaScript projects.

    Supports:
    - Serverless Lambda handlers (*-handler.ts, handler.ts)
    - Express/Fastify API routes (routes/, api/)
    - React/Vue/Svelte frontend components
    - Next.js pages and app router
    - Monorepo structures (packages/, apps/, libs/)
    - NestJS modules and controllers

    Uses ADR-009 generic discovery algorithm plus TypeScript-specific
    patterns for handlers, routes, and components.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize TypeScript backend.

        Config options:
            - source_roots: Override default source roots
            - include_javascript: Include .js files (default: True)
            - handler_patterns: Patterns to identify handler files
            - handler_directories: Parent directories where Lambda handlers live.
              Each subdirectory containing a handler file becomes a code unit.
              Default: ["src/", "handlers/", "lambda/", "functions/"]
              Example: ["api/"] discovers api/login/, api/register/, etc.
            - github_token: GitHub API token (required for change detection)
            - github_api_url: GitHub API base URL (default: "https://api.github.com")
        """
        super().__init__(config)
        self.include_javascript = config.get("include_javascript", True)
        self.handler_patterns = config.get("handler_patterns", [
            "-handler.ts", "-handler.js",
            "handler.ts", "handler.js",
            ".handler.ts", ".handler.js",
        ])
        # Handler directories - where to look for Lambda handlers
        # Defaults to common TypeScript/Node.js patterns, can be overridden in .outcomeops.yaml
        self.handler_directories = config.get("handler_directories", [
            "src/",
            "handlers/",
            "lambda/",
            "functions/",
        ])
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", "https://api.github.com")

    def get_backend_name(self) -> str:
        return "TypeScript Backend"

    def get_backend_type(self) -> str:
        return "typescript"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate TypeScript backend configuration."""
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration (ADR-009)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """TypeScript and JavaScript source file extensions."""
        extensions = {".ts", ".tsx"}
        if self.include_javascript:
            extensions.add(".js")
            extensions.add(".jsx")
            extensions.add(".mjs")
            extensions.add(".cjs")
        return extensions

    def get_source_roots(self) -> List[str]:
        """
        Source roots for TypeScript/Node.js projects.

        Common patterns:
        - src/ - standard source directory
        - handlers/ - serverless handlers
        - app/ - Next.js app router
        - pages/ - Next.js pages router
        - packages/ - monorepo packages
        - apps/ - monorepo apps
        - libs/ - monorepo libraries
        """
        configured_roots = self.config.get("source_roots")
        if configured_roots:
            return configured_roots

        return [
            "src",
            "handlers",
            "app",
            "pages",
            "packages",
            "apps",
            "libs",
            "services",
        ]

    def get_excluded_patterns(self) -> Set[str]:
        """TypeScript-specific exclusions."""
        base_excluded = super().get_excluded_patterns()
        ts_excluded = {
            "node_modules/",
            "dist/",
            "build/",
            "builds/",
            ".next/",
            ".nuxt/",
            "coverage/",
            "__tests__/",
            "__mocks__/",
            ".turbo/",
            ".vercel/",
        }
        return base_excluded | ts_excluded

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from TypeScript repository files.

        Groups files by:
        1. Lambda handlers (by handler directory)
        2. API routes (by route directory)
        3. Frontend components (by component directory)
        4. Schemas (validation files)
        5. Shared/common code
        6. Infrastructure (Terraform)
        7. Tests

        Args:
            files: List of file objects from GitHub API

        Returns:
            List of discovered CodeUnit objects
        """
        code_units = []

        # Filter relevant files
        relevant_files = [
            f for f in files
            if f["type"] == "blob" and not self.is_excluded_path(f["path"])
        ]

        # Group 1: Lambda handler groups (by directory)
        handler_units = self._discover_handler_groups(relevant_files)
        code_units.extend(handler_units)

        # Group 2: API routes (by directory)
        api_units = self._discover_api_routes(relevant_files)
        code_units.extend(api_units)

        # Group 3: Frontend components
        frontend_units = self._discover_frontend_components(relevant_files)
        code_units.extend(frontend_units)

        # Group 4: Schemas
        schema_unit = self._discover_schemas(relevant_files)
        if schema_unit:
            code_units.append(schema_unit)

        # Group 5: Shared/common code
        shared_units = self._discover_shared_code(relevant_files)
        code_units.extend(shared_units)

        # Group 6: Infrastructure (uses base class)
        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        # Group 7: Tests
        test_units = self._discover_tests(relevant_files)
        code_units.extend(test_units)

        # Group 8: Config files
        config_unit = self._discover_config_files(relevant_files)
        if config_unit:
            code_units.append(config_unit)

        # Group 9: Documentation (uses base class)
        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        # Group 10: Kubernetes manifests (uses base class)
        k8s_unit = self.discover_kubernetes(files)
        if k8s_unit:
            code_units.append(k8s_unit)

        logger.info(f"Discovered {len(code_units)} code units")
        return code_units

    def _discover_handler_groups(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover Lambda handler groups by directory.

        Looks for files matching handler patterns (*-handler.ts, handler.ts)
        and groups all files in the same handler directory together.

        Also supports custom handler_directories from config, where each
        subdirectory containing a handler file becomes a code unit.

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for handler groups
        """
        handler_groups: Dict[str, List[str]] = defaultdict(list)

        # Method 1: Custom handler directories from .outcomeops.yaml
        # e.g., handler_directories: ["api/", "./"]
        # "./" or "" means look for handlers at root level (e.g., email-notification/handler.js)
        if self.handler_directories:
            for f in files:
                path = f["path"]
                if not self.is_source_file(path):
                    continue

                # Check if this file matches a handler pattern
                filename = path.split("/")[-1].lower()
                is_handler_file = any(filename.endswith(pattern) for pattern in self.handler_patterns)

                for handler_dir in self.handler_directories:
                    # Special case: "./" or "" means root-level handler directories
                    if handler_dir in ("./", ""):
                        # For root-level handlers like "email-notification/handler.js"
                        # Only match if file is a handler and is at depth 2 (dir/file.js)
                        parts = path.split("/")
                        if len(parts) == 2 and is_handler_file:
                            # email-notification/handler.js -> email-notification
                            group_key = parts[0]
                            handler_groups[group_key].append(path)
                            break
                        continue

                    # Normalize: ensure trailing slash for comparison
                    handler_dir_normalized = handler_dir.rstrip("/") + "/"

                    if path.startswith(handler_dir_normalized) or path.startswith(handler_dir.rstrip("/")):
                        parts = path.split("/")
                        # For "api/login/handler.js" -> group_key = "api/login"
                        if len(parts) >= 2:
                            # Find the handler_dir in parts and get the subdir
                            handler_dir_parts = handler_dir.rstrip("/").split("/")
                            if len(handler_dir_parts) == 1:
                                # Single-level dir like "api/"
                                if parts[0] == handler_dir_parts[0]:
                                    if len(parts) >= 3:
                                        # api/login/handler.js -> api/login
                                        group_key = f"{parts[0]}/{parts[1]}"
                                    else:
                                        # api/handler.js -> api (unlikely but handled)
                                        group_key = parts[0]
                                    handler_groups[group_key].append(path)
                        break

        # Method 2: Standard handlers/ directory pattern
        for f in files:
            path = f["path"]
            filename = path.split("/")[-1].lower()

            # Check if file matches handler patterns
            is_handler = any(filename.endswith(pattern) for pattern in self.handler_patterns)

            # Also check if in handlers/ directory
            if "handlers/" in path.lower():
                is_handler = True

            if is_handler and self.is_source_file(path):
                # Extract handler group: src/handlers/api-gw/action-mapper/* -> api-gw/action-mapper
                if "handlers/" in path.lower():
                    parts = path.split("/")
                    handlers_idx = next(
                        (i for i, p in enumerate(parts) if p.lower() == "handlers"),
                        -1
                    )
                    if handlers_idx >= 0 and handlers_idx + 1 < len(parts):
                        # Get up to 2 levels after handlers/
                        remaining = parts[handlers_idx + 1:-1]  # Exclude filename
                        if remaining:
                            group_key = "/".join(remaining[:2])  # e.g., "api-gw/action-mapper"
                            handler_groups[group_key].append(path)

        # Also add all files in handler directories (not just handler files)
        for f in files:
            path = f["path"]
            if not self.is_source_file(path):
                continue
            if "handlers/" in path.lower():
                parts = path.split("/")
                handlers_idx = next(
                    (i for i, p in enumerate(parts) if p.lower() == "handlers"),
                    -1
                )
                if handlers_idx >= 0 and handlers_idx + 1 < len(parts):
                    remaining = parts[handlers_idx + 1:-1]
                    if remaining:
                        group_key = "/".join(remaining[:2])
                        if group_key in handler_groups and path not in handler_groups[group_key]:
                            handler_groups[group_key].append(path)

        # Create CodeUnits
        code_units = []
        for group_key, group_files in handler_groups.items():
            if group_files:
                unit_name = group_key.replace("/", "-")
                code_units.append(CodeUnit(
                    name=unit_name,
                    unit_type="handler-group",
                    file_paths=sorted(set(group_files)),
                    metadata={
                        "batch_type": "handler-group",
                        "handler_group": group_key,
                    }
                ))

        logger.info(f"Discovered {len(code_units)} handler groups")
        return code_units

    def _discover_api_routes(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover API route files grouped by directory.

        Looks for files in routes/, api/, controllers/ directories.

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for API routes
        """
        route_dirs = {"routes/", "api/", "controllers/"}

        # Find files in route directories
        route_files: Dict[str, List[str]] = defaultdict(list)

        for f in files:
            path = f["path"]
            if not self.is_source_file(path):
                continue

            path_lower = path.lower()

            # Check if in route directory
            for route_dir in route_dirs:
                if route_dir in path_lower:
                    # Extract group: src/routes/users/* -> routes/users
                    parts = path.split("/")
                    dir_idx = next(
                        (i for i, p in enumerate(parts) if p.lower().rstrip("/") in ["routes", "api", "controllers"]),
                        -1
                    )
                    if dir_idx >= 0 and dir_idx + 1 < len(parts):
                        group_parts = parts[dir_idx:dir_idx + 2]  # e.g., ["routes", "users"]
                        if len(group_parts) == 2:
                            group_key = "/".join(group_parts)
                            route_files[group_key].append(path)
                    break

        # Create CodeUnits
        code_units = []
        for group_key, group_files in route_files.items():
            if group_files:
                unit_name = group_key.replace("/", "-")
                code_units.append(CodeUnit(
                    name=unit_name,
                    unit_type="api-routes",
                    file_paths=sorted(set(group_files)),
                    metadata={
                        "batch_type": "api-routes",
                        "route_group": group_key,
                    }
                ))

        return code_units

    def _discover_frontend_components(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover frontend component files grouped by directory.

        Looks for React (.tsx, .jsx), Vue (.vue), Svelte (.svelte) files
        in src/, app/, components/ directories.

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for frontend components
        """
        frontend_extensions = {".tsx", ".jsx", ".vue", ".svelte"}
        frontend_dirs = {"src/", "app/", "components/", "pages/"}

        # Find frontend files
        frontend_files: Dict[str, List[str]] = defaultdict(list)

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Check if frontend file type
            is_frontend = any(path_lower.endswith(ext) for ext in frontend_extensions)
            if not is_frontend:
                continue

            # Check if in frontend directory
            for front_dir in frontend_dirs:
                if front_dir in path_lower:
                    # Extract group: src/components/Button/* -> src/components
                    parts = path.split("/")
                    dir_idx = next(
                        (i for i, p in enumerate(parts) if p.lower() in ["src", "app", "components", "pages"]),
                        -1
                    )
                    if dir_idx >= 0 and dir_idx + 1 < len(parts):
                        group_parts = parts[dir_idx:dir_idx + 2]
                        group_key = "/".join(group_parts)
                        frontend_files[group_key].append(path)
                    break

        # Create CodeUnits
        code_units = []
        for group_key, group_files in frontend_files.items():
            if group_files:
                unit_name = group_key.replace("/", "-")
                code_units.append(CodeUnit(
                    name=unit_name,
                    unit_type="frontend",
                    file_paths=sorted(set(group_files)),
                    metadata={
                        "batch_type": "frontend",
                        "component_group": group_key,
                    }
                ))

        return code_units

    def _discover_schemas(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover schema/validation files.

        Looks for files matching *-schema.ts, *.schema.ts patterns.

        Args:
            files: List of file objects

        Returns:
            CodeUnit for schemas, or None
        """
        schema_patterns = ["-schema.", ".schema.", "/schemas/"]

        schema_files = []
        for f in files:
            path = f["path"]
            path_lower = path.lower()

            if not self.is_source_file(path):
                continue

            # Check if schema file
            is_schema = any(pattern in path_lower for pattern in schema_patterns)
            if is_schema:
                schema_files.append(path)

        if not schema_files:
            return None

        return CodeUnit(
            name="schemas",
            unit_type="schemas",
            file_paths=sorted(schema_files),
            metadata={"batch_type": "schemas"}
        )

    def _discover_shared_code(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover shared/common code files.

        Looks for files in shared/, common/, utils/, lib/ directories
        (excluding those inside handlers/).

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for shared code
        """
        shared_dirs = {"shared/", "common/", "utils/", "lib/", "helpers/"}

        shared_files: Dict[str, List[str]] = defaultdict(list)

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            if not self.is_source_file(path):
                continue

            # Skip if inside handlers directory
            if "handlers/" in path_lower:
                continue

            # Check if in shared directory
            for shared_dir in shared_dirs:
                if shared_dir in path_lower:
                    # Extract group
                    parts = path.split("/")
                    dir_idx = next(
                        (i for i, p in enumerate(parts) if p.lower() in ["shared", "common", "utils", "lib", "helpers"]),
                        -1
                    )
                    if dir_idx >= 0:
                        if dir_idx + 1 < len(parts) - 1:  # Has subdirectory
                            group_key = f"{parts[dir_idx]}/{parts[dir_idx + 1]}"
                        else:
                            group_key = parts[dir_idx]
                        shared_files[group_key].append(path)
                    break

        # Create CodeUnits
        code_units = []
        for group_key, group_files in shared_files.items():
            if group_files:
                unit_name = group_key.replace("/", "-")
                code_units.append(CodeUnit(
                    name=unit_name,
                    unit_type="shared",
                    file_paths=sorted(set(group_files)),
                    metadata={
                        "batch_type": "shared",
                        "shared_group": group_key,
                    }
                ))

        return code_units

    def _discover_tests(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover test files grouped by type.

        Looks for files matching *.test.ts, *.spec.ts, in __tests__/ directories.

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for test groups
        """
        test_patterns = [".test.", ".spec.", "/__tests__/"]

        test_files: Dict[str, List[str]] = {
            "unit": [],
            "integration": [],
            "e2e": [],
            "other": [],
        }

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            if not self.is_source_file(path):
                continue

            # Check if test file
            is_test = any(pattern in path_lower for pattern in test_patterns)
            if not is_test:
                continue

            # Categorize test
            if "integration" in path_lower or "integ" in path_lower:
                test_files["integration"].append(path)
            elif "e2e" in path_lower or "end-to-end" in path_lower or "cypress" in path_lower:
                test_files["e2e"].append(path)
            elif "__tests__" in path_lower or ".test." in path_lower or ".spec." in path_lower:
                test_files["unit"].append(path)
            else:
                test_files["other"].append(path)

        # Create CodeUnits
        code_units = []
        for test_type, type_files in test_files.items():
            if type_files:
                code_units.append(CodeUnit(
                    name=f"tests-{test_type}",
                    unit_type="tests",
                    file_paths=sorted(type_files),
                    metadata={"batch_type": "tests", "test_type": test_type}
                ))

        return code_units

    def _discover_config_files(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover configuration files.

        Includes: serverless.yml, tsconfig.json, vite.config.ts, etc.

        Args:
            files: List of file objects

        Returns:
            CodeUnit for config files, or None
        """
        config_patterns = {
            "serverless.yml", "serverless.yaml",
            ".serverless.yml", ".serverless.yaml",
            "tsconfig.json", "tsconfig.base.json",
            "vite.config.ts", "vite.config.js",
            "webpack.config.js", "webpack.config.ts",
            "next.config.js", "next.config.mjs",
            "tailwind.config.js", "tailwind.config.ts",
            "jest.config.js", "jest.config.ts",
            "vitest.config.ts",
            ".gitlab-ci.yml", ".github/workflows",
            "justfile", "makefile", "Makefile",
        }

        config_files = []
        for f in files:
            path = f["path"]
            filename = path.split("/")[-1]

            if filename in config_patterns:
                config_files.append(path)
            elif ".gitlab-ci" in path or ".github/workflows" in path:
                config_files.append(path)

        if not config_files:
            return None

        return CodeUnit(
            name="config",
            unit_type="config",
            file_paths=sorted(config_files),
            metadata={"batch_type": "config"}
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
            with urlopen(request) as response:
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
            with urlopen(request) as response:
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
            with urlopen(request) as response:
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
            "backend_type": "typescript",
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit.

        Args:
            code_unit: Code unit to generate key for

        Returns:
            Storage key (SK) for DynamoDB

        Examples:
            handler-group: "summary#handler#api-gw-action-mapper"
            api-routes: "summary#api#routes-users"
            frontend: "summary#frontend#src-components"
            schemas: "summary#schemas"
            tests: "summary#tests#unit"
        """
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)

        if batch_type == "handler-group":
            return f"summary#handler#{code_unit.name}"
        elif batch_type == "api-routes":
            return f"summary#api#{code_unit.name}"
        elif batch_type == "frontend":
            return f"summary#frontend#{code_unit.name}"
        elif batch_type == "shared":
            return f"summary#shared#{code_unit.name}"
        elif batch_type == "schemas":
            return "summary#schemas"
        elif batch_type == "tests":
            return f"summary#tests#{code_unit.name}"
        elif batch_type == "config":
            return "summary#config"
        elif batch_type == "kubernetes":
            return "summary#kubernetes"
        else:
            return f"summary#{batch_type}"


# Auto-register TypeScript backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("typescript", TypeScriptBackend)
