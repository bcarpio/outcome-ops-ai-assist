"""
C# / .NET backend for code map generation.

Discovers C# source files in F500 .NET repositories, supporting:
- .NET Framework 4.x and modern .NET (Core / 5 / 6 / 7 / 8)
- Old-style and SDK-style .csproj files
- ASP.NET MVC, ASP.NET Core MVC, Web API
- ASP.NET Web Forms (.aspx, .ascx, .ashx) and WCF (.svc)
- xUnit, NUnit, MSTest test projects

Per ADR-017, this backend uses regex/filesystem heuristics only.
No parser library (no tree-sitter, no Roslyn).

See ADRs 014-017 for the full discovery contract.
"""

import json
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import CodeMapBackend, CodeUnit, ChangeDetectionResult
except ImportError:
    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)
    from base import CodeMapBackend, CodeUnit, ChangeDetectionResult  # noqa: F401

logger = logging.getLogger(__name__)

# Source extensions per ADR-014
CSHARP_SOURCE_EXTENSIONS: Set[str] = {
    ".cs",          # primary
    ".cshtml",      # Razor views
    ".aspx",        # Web Forms pages
    ".aspx.cs",     # Web Forms code-behind
    ".ascx",        # Web Forms user controls
    ".ashx",        # generic HTTP handlers
    ".svc",         # WCF service hosts
}

# Build module markers per ADR-014. Checked in addition to BUILD_MODULE_MARKERS
# from base.py - we don't modify base.py to keep it language-neutral.
CSHARP_BUILD_MARKERS: Set[str] = {
    "Directory.Packages.props",
    "Directory.Build.props",
    "packages.config",
    "global.json",
}

# .csproj and .sln are matched by suffix (any name)
CSHARP_BUILD_SUFFIXES: Tuple[str, ...] = (".csproj", ".sln")

# Boundary patterns added to DEFAULT_BOUNDARY_PATTERNS per ADR-014
CSHARP_BOUNDARY_PATTERNS: Set[str] = {
    "controllers", "viewmodels", "views", "pages", "areas",
    "infrastructure", "persistence", "repositories",
    "domain", "application", "presentation",
    "hubs", "middleware", "filters", "attributes",
    "extensions", "abstractions", "contracts",
    "migrations", "seeders",
}

# Excluded paths added to DEFAULT_EXCLUDED_PATTERNS per ADR-014
CSHARP_EXCLUDED_PATTERNS: Set[str] = {
    "bin/", "obj/", "packages/", ".vs/",
    "TestResults/", "artifacts/",
    "Properties/", "_ReSharper.Caches/",
}

# Test project recognition per ADR-016
TEST_PROJECT_SUFFIX_RE = re.compile(
    r'\.(Tests?|UnitTests?|IntegrationTests?|E2ETests?|EndToEndTests?|'
    r'AcceptanceTests?|SystemTests?|FunctionalTests?)$',
    re.IGNORECASE,
)

TEST_FRAMEWORK_PATTERNS: Dict[str, "re.Pattern"] = {
    "xunit":  re.compile(r'PackageReference\s+Include="xunit"', re.IGNORECASE),
    "nunit":  re.compile(r'PackageReference\s+Include="NUnit"', re.IGNORECASE),
    "mstest": re.compile(r'PackageReference\s+Include="MSTest\.TestFramework"',
                         re.IGNORECASE),
}

# Suffix-to-group map per ADR-016
SUFFIX_TO_GROUP: Dict[str, str] = {
    "tests": "unit", "test": "unit",
    "unittests": "unit", "unittest": "unit",
    "integrationtests": "integration", "integrationtest": "integration",
    "e2etests": "e2e", "endtoendtests": "e2e",
    "acceptancetests": "functional", "systemtests": "functional",
    "functionaltests": "functional",
}

# MVC subunit folders per ADR-014: dir-name -> (unit_kind, allowed_extensions)
# Discovery scans each project root for these conventional folders.
MVC_SUBUNIT_FOLDERS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "Controllers": ("controllers", (".cs",)),
    "Services":    ("services",    (".cs",)),
    "Views":       ("views",       (".cshtml", ".cs")),
    "Pages":       ("views",       (".cshtml", ".cs")),
    "Areas":       ("views",       (".cshtml", ".cs")),
    "Models":      ("models",      (".cs",)),
    "Entities":    ("models",      (".cs",)),
    "DTOs":        ("models",      (".cs",)),
    "Repositories": ("repositories", (".cs",)),
    "Domain":      ("domain",      (".cs",)),
}


class CSharpBackend(CodeMapBackend):
    """
    Backend for C# / .NET applications.

    Per ADR-014, recognizes both .NET Framework 4.x and modern .NET projects.
    Per ADR-017, uses regex/filesystem heuristics only - no AST parsing.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", "https://api.github.com")
        self.max_file_size_tokens = config.get("max_file_size_tokens", 7000)

    def get_backend_name(self) -> str:
        return "C# / .NET Backend"

    def get_backend_type(self) -> str:
        return "csharp"

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration (ADR-009 / ADR-014)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        return CSHARP_SOURCE_EXTENSIONS

    def get_source_roots(self) -> List[str]:
        configured = self.config.get("source_roots")
        if configured:
            return configured
        return ["src", "source", "server", "web", "Source", "Src", ""]

    def get_boundary_patterns(self) -> Set[str]:
        return super().get_boundary_patterns() | CSHARP_BOUNDARY_PATTERNS

    def get_excluded_patterns(self) -> Set[str]:
        return super().get_excluded_patterns() | CSHARP_EXCLUDED_PATTERNS

    def has_build_module_marker(self, dir_path: str, all_files: List[Dict[str, Any]]) -> bool:
        """Override base to add C#-specific markers (.csproj, .sln, etc.)."""
        if super().has_build_module_marker(dir_path, all_files):
            return True
        for f in all_files:
            if f["type"] != "blob":
                continue
            file_path = f["path"]
            if "/" in file_path:
                file_dir, file_name = file_path.rsplit("/", 1)
            else:
                file_dir, file_name = "", file_path
            if file_dir != dir_path:
                continue
            if file_name in CSHARP_BUILD_MARKERS:
                return True
            if file_name.endswith(CSHARP_BUILD_SUFFIXES):
                return True
        return False

    # =========================================================================
    # Code Unit Discovery (ADR-014 / ADR-015)
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover C#/.NET code units.

        Order: csharp-project units, then per-project MVC subunits, then
        test projects, then base infrastructure / docs / kubernetes.
        """
        relevant = [
            f for f in files
            if f["type"] == "blob" and not self.is_excluded_path(f["path"])
        ]

        units: List[CodeUnit] = []

        # Discover test projects first so we can exclude them from project discovery
        test_units, test_project_dirs = self._discover_test_projects(relevant)

        project_units = self._discover_csharp_projects(relevant, test_project_dirs)
        units.extend(project_units)

        for proj in project_units:
            units.extend(self._discover_mvc_subunits(relevant, proj))

        units.extend(test_units)

        if (infra := self.discover_infrastructure(files)):
            units.append(infra)
        if (docs := self.discover_documentation(files)):
            units.append(docs)
        if (k8s := self.discover_kubernetes(files)):
            units.append(k8s)

        logger.info(
            "Discovered %d code units (%d projects, %d total)",
            len(units), len(project_units), len(units),
        )
        return units

    def _discover_csharp_projects(
        self,
        files: List[Dict[str, Any]],
        test_project_dirs: Set[str],
    ) -> List[CodeUnit]:
        """One csharp-project CodeUnit per non-test .csproj file."""
        projects: List[CodeUnit] = []

        # Find each .csproj location
        csproj_locations: List[Tuple[str, str]] = []  # (dir, project_name)
        for f in files:
            path = f["path"]
            if not path.endswith(".csproj"):
                continue
            if "/" in path:
                proj_dir, proj_file = path.rsplit("/", 1)
            else:
                proj_dir, proj_file = "", path
            project_name = proj_file[:-len(".csproj")]
            csproj_locations.append((proj_dir, project_name))

        # Sort deepest first so nested projects don't absorb each other
        csproj_locations.sort(key=lambda x: x[0].count("/"), reverse=True)

        # Track files already claimed by a deeper project
        claimed_files: Set[str] = set()

        for proj_dir, project_name in csproj_locations:
            if proj_dir in test_project_dirs:
                continue

            # Collect all files under this project directory (or repo root)
            if proj_dir == "":
                project_files = [
                    f["path"] for f in files
                    if f["path"] not in claimed_files
                ]
            else:
                prefix = proj_dir + "/"
                project_files = [
                    f["path"] for f in files
                    if (f["path"] == proj_dir or f["path"].startswith(prefix))
                    and f["path"] not in claimed_files
                ]

            # Filter to source files + the .csproj itself
            project_files = [
                p for p in project_files
                if self.is_source_file(p) or p.endswith(".csproj")
            ]

            if not project_files:
                continue

            for p in project_files:
                claimed_files.add(p)

            projects.append(CodeUnit(
                name=project_name,
                unit_type="csharp-project",
                file_paths=sorted(project_files),
                metadata={
                    "batch_type": "csharp-project",
                    "project": project_name,
                    "project_dir": proj_dir,
                    "framework": "unknown",
                },
            ))

        # Restore natural top-down order for output
        projects.sort(key=lambda u: (u.metadata.get("project_dir") or "", u.name))
        return projects

    def _discover_mvc_subunits(
        self,
        files: List[Dict[str, Any]],
        project: CodeUnit,
    ) -> List[CodeUnit]:
        """
        Emit csharp-controllers / -services / -views / -models for conventional
        folders inside a project. Threshold from get_min_files_threshold().
        """
        proj_dir = project.metadata.get("project_dir") or ""
        threshold = self.get_min_files_threshold()
        prefix = (proj_dir + "/") if proj_dir else ""

        # Bucket project files by MVC folder
        buckets: Dict[str, List[str]] = defaultdict(list)
        for path in project.file_paths:
            if proj_dir and not path.startswith(prefix):
                continue
            relative = path[len(prefix):] if prefix else path
            if "/" not in relative:
                continue
            top = relative.split("/", 1)[0]
            if top not in MVC_SUBUNIT_FOLDERS:
                continue
            kind, allowed_exts = MVC_SUBUNIT_FOLDERS[top]
            if not any(path.endswith(ext) for ext in allowed_exts):
                continue
            buckets[kind].append(path)

        subunits: List[CodeUnit] = []
        for kind, paths in buckets.items():
            if len(paths) < threshold:
                continue
            subunits.append(CodeUnit(
                name=project.name,
                unit_type=f"csharp-{kind}",
                file_paths=sorted(paths),
                metadata={
                    "batch_type": f"csharp-{kind}",
                    "project": project.name,
                    "project_dir": proj_dir,
                },
            ))

        return subunits

    def _discover_test_projects(
        self,
        files: List[Dict[str, Any]],
    ) -> Tuple[List[CodeUnit], Set[str]]:
        """
        Group test projects by category per ADR-016.

        Returns (test_units, set_of_test_project_dirs).
        """
        # Find each .csproj that classifies as a test project
        test_projects: List[Tuple[str, str, str, str]] = []  # (dir, project_name, group, framework)
        for f in files:
            path = f["path"]
            if not path.endswith(".csproj"):
                continue
            classification = self._classify_test_project(path, csproj_content=None)
            if classification is None:
                continue
            group, framework = classification
            if "/" in path:
                proj_dir, proj_file = path.rsplit("/", 1)
            else:
                proj_dir, proj_file = "", path
            project_name = proj_file[:-len(".csproj")]
            test_projects.append((proj_dir, project_name, group, framework))

        test_project_dirs: Set[str] = {d for d, _, _, _ in test_projects}

        # Collect files per test project, then merge by group
        group_files: Dict[str, List[str]] = defaultdict(list)
        group_frameworks: Dict[str, Set[str]] = defaultdict(set)
        group_projects: Dict[str, List[str]] = defaultdict(list)

        for proj_dir, project_name, group, framework in test_projects:
            if proj_dir == "":
                proj_files = [f["path"] for f in files]
            else:
                prefix = proj_dir + "/"
                proj_files = [
                    f["path"] for f in files
                    if f["path"] == proj_dir or f["path"].startswith(prefix)
                ]
            proj_files = [
                p for p in proj_files
                if self.is_source_file(p) or p.endswith(".csproj")
            ]
            group_files[group].extend(proj_files)
            group_frameworks[group].add(framework)
            group_projects[group].append(project_name)

        units: List[CodeUnit] = []
        for group, paths in group_files.items():
            frameworks = group_frameworks[group]
            framework_value = (
                next(iter(frameworks)) if len(frameworks) == 1 else "mixed"
            )
            units.append(CodeUnit(
                name=group,
                unit_type="tests",
                file_paths=sorted(set(paths)),
                metadata={
                    "batch_type": "tests",
                    "test_type": group,
                    "language": "csharp",
                    "framework": framework_value,
                    "projects": sorted(group_projects[group]),
                },
            ))

        return units, test_project_dirs

    def _classify_test_project(
        self,
        csproj_path: str,
        csproj_content: Optional[str],
    ) -> Optional[Tuple[str, str]]:
        """Return (group, framework) if .csproj is a test project, else None."""
        name = csproj_path.rsplit("/", 1)[-1].rsplit(".csproj", 1)[0]
        suffix_match = TEST_PROJECT_SUFFIX_RE.search(name)

        framework = "unknown"
        if csproj_content:
            for fw, pattern in TEST_FRAMEWORK_PATTERNS.items():
                if pattern.search(csproj_content):
                    framework = fw
                    break

        if not suffix_match and framework == "unknown":
            return None

        suffix_key = (suffix_match.group(1).lower() if suffix_match else "tests")
        group = SUFFIX_TO_GROUP.get(suffix_key, "unit")
        return group, framework

    # =========================================================================
    # Change Detection (mirrors python_backend.py - language-agnostic)
    # =========================================================================

    def detect_changes(
        self,
        repo: str,
        repo_project: str,
        last_state: Optional[Dict[str, Any]],
        force_full: bool = False,
    ) -> ChangeDetectionResult:
        """Detect changes using git commit comparison."""
        current_sha = self._get_current_commit_sha(repo_project)

        if force_full or not last_state or "commit_sha" not in last_state:
            logger.info("Full regeneration requested or no previous state for %s", repo)
            return ChangeDetectionResult(
                has_changes=True,
                changed_units=[],
                unchanged_units=[],
                last_commit_sha=last_state.get("commit_sha") if last_state else None,
                current_commit_sha=current_sha,
                changed_files=[],
            )

        last_sha = last_state["commit_sha"]

        if current_sha == last_sha:
            logger.info("No changes detected for %s: %s", repo, current_sha)
            return ChangeDetectionResult(
                has_changes=False,
                changed_units=[],
                unchanged_units=[],
                last_commit_sha=last_sha,
                current_commit_sha=current_sha,
                changed_files=[],
                removed_files=[],
            )

        changed_files, removed_files = self._get_changed_files(
            repo_project, last_sha, current_sha
        )
        logger.info(
            "Found %d changed files, %d removed files between %s..%s",
            len(changed_files), len(removed_files), last_sha[:7], current_sha[:7],
        )

        return ChangeDetectionResult(
            has_changes=True,
            changed_units=[],
            unchanged_units=[],
            last_commit_sha=last_sha,
            current_commit_sha=current_sha,
            changed_files=changed_files,
            removed_files=removed_files,
        )

    def _get_default_branch(self, repo_project: str) -> str:
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
                logger.info("Default branch for %s: %s", repo_project, default_branch)
                return default_branch
        except URLError as e:
            logger.warning(
                "Failed to get default branch for %s, using 'main': %s",
                repo_project, e,
            )
            return "main"

    def _get_current_commit_sha(self, repo_project: str) -> str:
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
            logger.error("Failed to get current commit SHA: %s", e)
            raise

    def _get_changed_files(
        self,
        repo_project: str,
        base_sha: str,
        head_sha: str,
    ) -> Tuple[List[str], List[str]]:
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
                changed_files: List[str] = []
                removed_files: List[str] = []
                for f in files:
                    filename = f["filename"]
                    status = f.get("status", "modified")
                    if status == "removed":
                        removed_files.append(filename)
                    else:
                        changed_files.append(filename)
                logger.info(
                    "Changed files: %d, Removed files: %d",
                    len(changed_files), len(removed_files),
                )
                return changed_files, removed_files
        except URLError as e:
            logger.error("Failed to get changed files: %s", e)
            raise

    # =========================================================================
    # Batch Metadata and Storage Keys (ADR-015)
    # =========================================================================

    def generate_batch_metadata(self, code_unit: CodeUnit, repo: str) -> Dict[str, Any]:
        return {
            "batch_type": code_unit.metadata.get("batch_type", code_unit.unit_type),
            "group_name": code_unit.name,
            "files": [{"path": p} for p in code_unit.file_paths],
            "storage_key": self.get_storage_key(code_unit),
            "backend_type": "csharp",
            "language": "csharp",
            "framework": code_unit.metadata.get("framework", "unknown"),
            "project": code_unit.metadata.get("project"),
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """Per ADR-015, csharp units use summary#csharp# prefix; tests stay summary#tests#."""
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)
        project = code_unit.metadata.get("project", code_unit.name)

        if batch_type == "tests":
            return f"summary#tests#{code_unit.name}"
        if batch_type == "infrastructure":
            return "summary#infrastructure"
        if batch_type == "docs":
            return "summary#docs"
        if batch_type == "kubernetes":
            return "summary#kubernetes"
        if batch_type == "csharp-project":
            return f"summary#csharp#project#{project}"
        if batch_type.startswith("csharp-"):
            kind = batch_type[len("csharp-"):]
            return f"summary#csharp#{kind}#{project}"
        return f"summary#{batch_type}"


# Auto-register C# backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("csharp", CSharpBackend)
