# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
React/Frontend backend for code map generation.

Discovers React code units using specialized patterns for:
- Components (functional components, layouts, UI primitives)
- Hooks (custom hooks for state, effects, data fetching)
- Contexts and Providers (React Context API)
- Pages and Routes (Next.js, React Router)
- State management (stores, reducers, slices)
- Styles (CSS modules, styled-components, Tailwind)

This backend extends the TypeScript backend with React-specific
discovery patterns for frontend applications.
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


class ReactBackend(CodeMapBackend):
    """
    Backend for React/Frontend projects.

    Supports:
    - React functional components and class components
    - Custom hooks (use*.ts/tsx)
    - Context providers and consumers
    - Next.js pages, app router, and layouts
    - React Router route components
    - State management (Redux, Zustand, Jotai)
    - Styling patterns (CSS modules, styled-components, Tailwind)
    - Storybook stories

    Uses React-specific discovery patterns that understand
    component hierarchies and frontend architecture.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize React backend.

        Config options:
            - source_roots: Override default source roots
            - component_dirs: Directories containing components
            - github_token: GitHub API token (required for change detection)
            - github_api_url: GitHub API base URL
        """
        super().__init__(config)
        self.component_dirs = config.get("component_dirs", [
            "components/",
            "src/components/",
            "app/components/",
            "ui/src/components/",
        ])
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", f"{_github_api()}")

    def get_backend_name(self) -> str:
        return "React Frontend Backend"

    def get_backend_type(self) -> str:
        return "react"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate React backend configuration."""
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """React/TypeScript source file extensions."""
        return {
            ".tsx", ".jsx",  # React components
            ".ts", ".js",    # Logic/utilities
            ".css", ".scss", ".sass", ".less",  # Styles
            ".module.css", ".module.scss",      # CSS modules
        }

    def get_source_roots(self) -> List[str]:
        """
        Source roots for React projects.

        Common patterns:
        - src/ - Create React App, Vite
        - app/ - Next.js app router
        - pages/ - Next.js pages router
        - components/ - Shared components
        - ui/src/ - UI package in monorepo
        """
        configured_roots = self.config.get("source_roots")
        if configured_roots:
            return configured_roots

        return [
            "src",
            "app",
            "pages",
            "components",
            "ui/src",
            "packages/ui/src",
            "apps/web/src",
        ]

    def get_excluded_patterns(self) -> Set[str]:
        """React-specific exclusions."""
        base_excluded = super().get_excluded_patterns()
        react_excluded = {
            "node_modules/",
            "dist/",
            "build/",
            ".next/",
            ".vercel/",
            "coverage/",
            "__tests__/",
            "__mocks__/",
            ".storybook/",
            "storybook-static/",
            "public/",
            "static/",
        }
        return base_excluded | react_excluded

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from React repository files.

        Groups files by:
        1. Components (by directory/category)
        2. Hooks (custom React hooks)
        3. Contexts and Providers
        4. Pages and Routes
        5. State management (stores, slices)
        6. Styles (global styles, themes)
        7. Types and Interfaces
        8. Utilities
        9. Tests
        10. Storybook stories
        11. Infrastructure (Terraform)
        12. Documentation

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

        # Group 1: Components by category
        component_units = self._discover_components(relevant_files)
        code_units.extend(component_units)

        # Group 2: Custom hooks
        hooks_unit = self._discover_hooks(relevant_files)
        if hooks_unit:
            code_units.append(hooks_unit)

        # Group 3: Contexts and Providers
        context_unit = self._discover_contexts(relevant_files)
        if context_unit:
            code_units.append(context_unit)

        # Group 4: Pages and Routes
        pages_units = self._discover_pages(relevant_files)
        code_units.extend(pages_units)

        # Group 5: State management
        state_unit = self._discover_state_management(relevant_files)
        if state_unit:
            code_units.append(state_unit)

        # Group 6: Styles and Themes
        styles_unit = self._discover_styles(relevant_files)
        if styles_unit:
            code_units.append(styles_unit)

        # Group 7: Types and Interfaces
        types_unit = self._discover_types(relevant_files)
        if types_unit:
            code_units.append(types_unit)

        # Group 8: Utilities and Helpers
        utils_units = self._discover_utilities(relevant_files)
        code_units.extend(utils_units)

        # Group 9: Tests
        test_units = self._discover_tests(relevant_files)
        code_units.extend(test_units)

        # Group 10: Storybook stories
        stories_unit = self._discover_storybook(relevant_files)
        if stories_unit:
            code_units.append(stories_unit)

        # Group 11: Config files
        config_unit = self._discover_config_files(relevant_files)
        if config_unit:
            code_units.append(config_unit)

        # Group 12: Infrastructure (uses base class)
        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        # Group 13: Documentation (uses base class)
        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        logger.info(f"Discovered {len(code_units)} React code units")
        return code_units

    def _discover_components(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover React components grouped by category/directory.

        Categories:
        - ui/ - Base UI primitives (Button, Input, Card)
        - layout/ - Layout components (Header, Footer, Sidebar)
        - features/ - Feature-specific components
        - common/ - Shared components
        - forms/ - Form components
        - modals/ - Modal/dialog components

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for component groups
        """
        component_extensions = {".tsx", ".jsx"}
        component_patterns = [
            "components/", "Components/",
            "ui/", "UI/",
        ]

        # Group components by their category directory
        component_groups: Dict[str, List[str]] = defaultdict(list)

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Check if it's a component file
            if not any(path.endswith(ext) for ext in component_extensions):
                continue

            # Skip test files, stories, and index files
            filename = path.split("/")[-1]
            if any(pattern in filename.lower() for pattern in [".test.", ".spec.", ".stories.", "index."]):
                continue

            # Check if in component directory
            for pattern in component_patterns:
                if pattern.lower() in path_lower:
                    # Extract category: components/ui/Button.tsx -> "ui"
                    parts = path.split("/")
                    comp_idx = next(
                        (i for i, p in enumerate(parts) if p.lower() in ["components", "ui"]),
                        -1
                    )
                    if comp_idx >= 0 and comp_idx + 1 < len(parts):
                        # Get the next directory after components/
                        if comp_idx + 2 < len(parts):
                            # components/ui/Button/Button.tsx -> "ui"
                            category = parts[comp_idx + 1].lower()
                        else:
                            # components/Button.tsx -> "root"
                            category = "root"
                        component_groups[category].append(path)
                    break

        # Create CodeUnits for each component category
        code_units = []
        for category, category_files in component_groups.items():
            if category_files:
                # Include associated files (styles, tests) for these components
                associated_files = self._get_associated_files(files, category_files)
                all_files = list(set(category_files + associated_files))

                code_units.append(CodeUnit(
                    name=f"components-{category}",
                    unit_type="components",
                    file_paths=sorted(all_files),
                    metadata={
                        "batch_type": "components",
                        "component_category": category,
                    }
                ))

        return code_units

    def _get_associated_files(
        self,
        all_files: List[Dict[str, Any]],
        component_files: List[str]
    ) -> List[str]:
        """
        Get associated files for components (styles, types, etc.).

        For a component like Button.tsx, also include:
        - Button.module.css
        - Button.styles.ts
        - Button.types.ts
        - index.ts (barrel export)

        Args:
            all_files: All repository files
            component_files: List of component file paths

        Returns:
            List of associated file paths
        """
        associated = []
        component_bases = set()

        # Get base names of components
        for comp_path in component_files:
            # Button.tsx -> Button
            parts = comp_path.rsplit("/", 1)
            if len(parts) == 2:
                dir_path = parts[0]
                filename = parts[1]
                base_name = filename.rsplit(".", 1)[0]
                component_bases.add((dir_path, base_name))

        # Find associated files
        for f in all_files:
            path = f["path"]
            if f["type"] != "blob":
                continue

            parts = path.rsplit("/", 1)
            if len(parts) != 2:
                continue

            dir_path = parts[0]
            filename = parts[1]

            for comp_dir, comp_base in component_bases:
                # Same directory, related filename
                if dir_path == comp_dir:
                    if filename.startswith(comp_base) and filename != f"{comp_base}.tsx":
                        associated.append(path)
                    # Also include index files in component directories
                    elif filename in ["index.ts", "index.tsx", "index.js"]:
                        associated.append(path)

        return associated

    def _discover_hooks(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover custom React hooks.

        Hooks are identified by:
        - Files in hooks/ directory
        - Files starting with "use" (useAuth.ts, useFetch.tsx)

        Args:
            files: List of file objects

        Returns:
            CodeUnit for hooks, or None
        """
        hook_files = []

        for f in files:
            path = f["path"]
            filename = path.split("/")[-1].lower()

            # Skip non-source files
            if not any(path.endswith(ext) for ext in [".ts", ".tsx", ".js", ".jsx"]):
                continue

            # Skip test files
            if any(pattern in filename for pattern in [".test.", ".spec."]):
                continue

            # Check if hook file
            is_hook = (
                "/hooks/" in path.lower() or
                "/hook/" in path.lower() or
                filename.startswith("use")
            )

            if is_hook:
                hook_files.append(path)

        if not hook_files:
            return None

        return CodeUnit(
            name="hooks",
            unit_type="hooks",
            file_paths=sorted(hook_files),
            metadata={"batch_type": "hooks"}
        )

    def _discover_contexts(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover React Context providers and consumers.

        Context files are identified by:
        - Files in context/ or contexts/ directory
        - Files in providers/ directory
        - Files with Context or Provider in name

        Args:
            files: List of file objects

        Returns:
            CodeUnit for contexts, or None
        """
        context_files = []
        context_patterns = [
            "/context/", "/contexts/",
            "/provider/", "/providers/",
            "context.ts", "context.tsx",
            "provider.ts", "provider.tsx",
        ]

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Skip non-source files
            if not any(path.endswith(ext) for ext in [".ts", ".tsx", ".js", ".jsx"]):
                continue

            # Check if context/provider file
            is_context = any(pattern in path_lower for pattern in context_patterns)

            if is_context:
                context_files.append(path)

        if not context_files:
            return None

        return CodeUnit(
            name="contexts",
            unit_type="contexts",
            file_paths=sorted(context_files),
            metadata={"batch_type": "contexts"}
        )

    def _discover_pages(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover page/route components.

        Supports:
        - Next.js pages/ directory
        - Next.js app/ directory (layouts, pages)
        - React Router routes

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for page groups
        """
        page_groups: Dict[str, List[str]] = defaultdict(list)

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Skip non-component files
            if not any(path.endswith(ext) for ext in [".tsx", ".jsx", ".ts", ".js"]):
                continue

            # Next.js pages directory
            if "/pages/" in path_lower and "api/" not in path_lower:
                # Group by first directory under pages/
                parts = path.split("/")
                pages_idx = next(
                    (i for i, p in enumerate(parts) if p.lower() == "pages"),
                    -1
                )
                if pages_idx >= 0 and pages_idx + 1 < len(parts):
                    next_part = parts[pages_idx + 1]
                    if next_part.endswith((".tsx", ".jsx", ".ts", ".js")):
                        page_groups["pages-root"].append(path)
                    else:
                        page_groups[f"pages-{next_part}"].append(path)

            # Next.js app directory
            elif "/app/" in path_lower:
                parts = path.split("/")
                app_idx = next(
                    (i for i, p in enumerate(parts) if p.lower() == "app"),
                    -1
                )
                if app_idx >= 0 and app_idx + 1 < len(parts):
                    filename = parts[-1].lower()
                    # Group layouts separately
                    if "layout" in filename:
                        page_groups["app-layouts"].append(path)
                    elif filename in ["page.tsx", "page.jsx", "page.ts", "page.js"]:
                        route = "/".join(parts[app_idx + 1:-1]) or "root"
                        page_groups[f"app-{route}"].append(path)
                    else:
                        page_groups["app-other"].append(path)

            # React Router routes
            elif "/routes/" in path_lower or "/views/" in path_lower:
                parts = path.split("/")
                route_idx = next(
                    (i for i, p in enumerate(parts) if p.lower() in ["routes", "views"]),
                    -1
                )
                if route_idx >= 0:
                    page_groups["routes"].append(path)

        # Create CodeUnits
        code_units = []
        for group_name, group_files in page_groups.items():
            if group_files:
                code_units.append(CodeUnit(
                    name=group_name,
                    unit_type="pages",
                    file_paths=sorted(group_files),
                    metadata={
                        "batch_type": "pages",
                        "page_group": group_name,
                    }
                ))

        return code_units

    def _discover_state_management(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover state management files.

        Supports:
        - Redux (store, slices, reducers, actions)
        - Zustand stores
        - Jotai atoms
        - Recoil atoms

        Args:
            files: List of file objects

        Returns:
            CodeUnit for state management, or None
        """
        state_patterns = [
            "/store/", "/stores/",
            "/slice/", "/slices/",
            "/reducer/", "/reducers/",
            "/actions/",
            "/atoms/",
            "/state/",
        ]
        state_filename_patterns = [
            "store.ts", "store.tsx",
            "slice.ts", "slice.tsx",
            "reducer.ts", "reducer.tsx",
            "atom.ts", "atoms.ts",
        ]

        state_files = []

        for f in files:
            path = f["path"]
            path_lower = path.lower()
            filename = path.split("/")[-1].lower()

            # Skip non-source files
            if not any(path.endswith(ext) for ext in [".ts", ".tsx", ".js", ".jsx"]):
                continue

            # Check patterns
            is_state = (
                any(pattern in path_lower for pattern in state_patterns) or
                any(filename.endswith(pattern) for pattern in state_filename_patterns)
            )

            if is_state:
                state_files.append(path)

        if not state_files:
            return None

        return CodeUnit(
            name="state-management",
            unit_type="state",
            file_paths=sorted(state_files),
            metadata={"batch_type": "state"}
        )

    def _discover_styles(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover global styles and theme files.

        Includes:
        - Global CSS/SCSS files
        - Theme configuration
        - Tailwind config
        - styled-components themes

        Args:
            files: List of file objects

        Returns:
            CodeUnit for styles, or None
        """
        style_extensions = {".css", ".scss", ".sass", ".less"}
        style_patterns = [
            "/styles/", "/style/",
            "/theme/", "/themes/",
            "globals.css", "global.css",
            "tailwind.config",
        ]

        style_files = []

        for f in files:
            path = f["path"]
            path_lower = path.lower()
            filename = path.split("/")[-1].lower()

            # Skip component-specific styles (*.module.css)
            if ".module." in filename:
                continue

            # Check style files
            is_style = (
                any(path.endswith(ext) for ext in style_extensions) or
                any(pattern in path_lower for pattern in style_patterns) or
                "tailwind" in filename
            )

            if is_style:
                style_files.append(path)

        if not style_files:
            return None

        return CodeUnit(
            name="styles",
            unit_type="styles",
            file_paths=sorted(style_files),
            metadata={"batch_type": "styles"}
        )

    def _discover_types(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover TypeScript type definitions.

        Includes:
        - types/ directory
        - *.types.ts files
        - *.d.ts declaration files

        Args:
            files: List of file objects

        Returns:
            CodeUnit for types, or None
        """
        type_files = []

        for f in files:
            path = f["path"]
            path_lower = path.lower()
            filename = path.split("/")[-1].lower()

            # Check type files
            is_type = (
                "/types/" in path_lower or
                "/interfaces/" in path_lower or
                filename.endswith(".types.ts") or
                filename.endswith(".types.tsx") or
                filename.endswith(".d.ts")
            )

            if is_type:
                type_files.append(path)

        if not type_files:
            return None

        return CodeUnit(
            name="types",
            unit_type="types",
            file_paths=sorted(type_files),
            metadata={"batch_type": "types"}
        )

    def _discover_utilities(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover utility and helper functions.

        Groups:
        - utils/ - General utilities
        - helpers/ - Helper functions
        - lib/ - Library code
        - api/ - API client code

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for utility groups
        """
        util_groups: Dict[str, List[str]] = defaultdict(list)
        util_patterns = {
            "utils": ["/utils/", "/util/"],
            "helpers": ["/helpers/", "/helper/"],
            "lib": ["/lib/"],
            "api": ["/api/", "/services/"],
        }

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Skip non-source files
            if not any(path.endswith(ext) for ext in [".ts", ".tsx", ".js", ".jsx"]):
                continue

            # Skip test files
            if any(pattern in path_lower for pattern in [".test.", ".spec.", "__tests__"]):
                continue

            # Check utility patterns
            for group_name, patterns in util_patterns.items():
                if any(pattern in path_lower for pattern in patterns):
                    util_groups[group_name].append(path)
                    break

        # Create CodeUnits
        code_units = []
        for group_name, group_files in util_groups.items():
            if group_files:
                code_units.append(CodeUnit(
                    name=group_name,
                    unit_type="utilities",
                    file_paths=sorted(group_files),
                    metadata={
                        "batch_type": "utilities",
                        "util_group": group_name,
                    }
                ))

        return code_units

    def _discover_tests(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover test files grouped by type.

        Groups:
        - unit tests (*.test.ts, *.spec.ts)
        - integration tests
        - e2e tests (Cypress, Playwright)

        Args:
            files: List of file objects

        Returns:
            List of CodeUnit objects for test groups
        """
        test_groups: Dict[str, List[str]] = {
            "unit": [],
            "integration": [],
            "e2e": [],
        }

        for f in files:
            path = f["path"]
            path_lower = path.lower()

            # Check if test file
            is_test = any(pattern in path_lower for pattern in [
                ".test.", ".spec.", "__tests__/"
            ])

            if not is_test:
                continue

            # Categorize
            if "e2e" in path_lower or "cypress" in path_lower or "playwright" in path_lower:
                test_groups["e2e"].append(path)
            elif "integration" in path_lower:
                test_groups["integration"].append(path)
            else:
                test_groups["unit"].append(path)

        # Create CodeUnits
        code_units = []
        for test_type, test_files in test_groups.items():
            if test_files:
                code_units.append(CodeUnit(
                    name=f"tests-{test_type}",
                    unit_type="tests",
                    file_paths=sorted(test_files),
                    metadata={"batch_type": "tests", "test_type": test_type}
                ))

        return code_units

    def _discover_storybook(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover Storybook story files.

        Args:
            files: List of file objects

        Returns:
            CodeUnit for stories, or None
        """
        story_files = []

        for f in files:
            path = f["path"]

            if ".stories." in path.lower() or ".story." in path.lower():
                story_files.append(path)

        if not story_files:
            return None

        return CodeUnit(
            name="storybook",
            unit_type="storybook",
            file_paths=sorted(story_files),
            metadata={"batch_type": "storybook"}
        )

    def _discover_config_files(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """
        Discover configuration files.

        Includes: Next.js config, Vite config, TypeScript config, etc.

        Args:
            files: List of file objects

        Returns:
            CodeUnit for config files, or None
        """
        config_patterns = {
            "next.config.js", "next.config.mjs", "next.config.ts",
            "vite.config.ts", "vite.config.js",
            "tsconfig.json", "tsconfig.base.json",
            "tailwind.config.js", "tailwind.config.ts",
            "postcss.config.js", "postcss.config.mjs",
            "jest.config.js", "jest.config.ts",
            "vitest.config.ts",
            ".eslintrc.js", ".eslintrc.json", "eslint.config.js",
            ".prettierrc", ".prettierrc.js", ".prettierrc.json",
            "package.json",
        }

        config_files = []
        for f in files:
            path = f["path"]
            filename = path.split("/")[-1]

            if filename in config_patterns:
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

        # Get changed and removed files
        changed_files, removed_files = self._get_changed_files(repo_project, last_sha, current_sha)
        logger.info(f"Found {len(changed_files)} changed files, {len(removed_files)} removed files")

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
                return data.get("default_branch", "main")
        except URLError as e:
            logger.warning(f"Failed to get default branch, using 'main': {e}")
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

    def _get_changed_files(
        self, repo_project: str, base_sha: str, head_sha: str
    ) -> tuple[List[str], List[str]]:
        """Get list of changed and removed files between two commits."""
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
                        changed_files.append(filename)

                return (changed_files, removed_files)
        except URLError as e:
            logger.error(f"Failed to get changed files: {e}")
            raise

    # =========================================================================
    # Batch Metadata and Storage
    # =========================================================================

    def generate_batch_metadata(self, code_unit: CodeUnit, repo: str) -> Dict[str, Any]:
        """Generate batch metadata for SQS message."""
        return {
            "batch_type": code_unit.metadata.get("batch_type", code_unit.unit_type),
            "group_name": code_unit.name,
            "files": [{"path": path} for path in code_unit.file_paths],
            "storage_key": self.get_storage_key(code_unit),
            "backend_type": "react",
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit.

        Examples:
            components: "summary#components#ui"
            hooks: "summary#hooks"
            pages: "summary#pages#app-dashboard"
            state: "summary#state"
        """
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)

        if batch_type == "components":
            category = code_unit.metadata.get("component_category", "default")
            return f"summary#components#{category}"
        elif batch_type == "pages":
            return f"summary#pages#{code_unit.name}"
        elif batch_type == "utilities":
            group = code_unit.metadata.get("util_group", "default")
            return f"summary#utils#{group}"
        elif batch_type == "tests":
            test_type = code_unit.metadata.get("test_type", "unit")
            return f"summary#tests#{test_type}"
        else:
            return f"summary#{batch_type}"


# Auto-register React backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("react", ReactBackend)
