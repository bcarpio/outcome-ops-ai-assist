# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Go backend for code map generation.

Discovers Go code units using Go's natural unit of organization: the package,
which in Go is exactly one directory. Every directory that contains non-test
``.go`` files is one code unit (a package); parent and child directories are
INDEPENDENT packages and are never rolled together (unlike the generic
source-root/boundary heuristic used for Java/TypeScript). This matches `go build`
semantics and keeps gin-style repos (packages at the repo root plus binding/,
render/, internal/... subpackages) mapped cleanly.

Pairs with the Go symbol-graph extractor (`extractor/go_extractor.py`) and
`_LANGUAGE_BY_EXT` ('.go' -> 'go'); this backend is the missing third piece that
lets `.go` files be discovered as code units and flow into code-maps + the
symbol graph.
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set
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


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base)."""
    from shared.github_base import github_api_base
    return github_api_base()


logger = logging.getLogger(__name__)


class GoBackend(CodeMapBackend):
    """
    Backend for Go projects.

    A Go package == a directory. Each directory holding non-test ``.go`` files
    becomes one ``source-module`` code unit; ``*_test.go`` files are grouped into
    a tests unit; ``go.mod``/``go.sum`` form the build unit. Infrastructure,
    Kubernetes, and documentation reuse the generic base-class discovery.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", f"{_github_api()}")

    def get_backend_name(self) -> str:
        return "Go Backend"

    def get_backend_type(self) -> str:
        return "go"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        return {".go"}

    def get_source_roots(self) -> List[str]:
        # Go packages live anywhere (repo root included). Discovery below does
        # not gate on source roots; this is kept for interface completeness.
        return [""]

    @staticmethod
    def _is_test_file(path: str) -> bool:
        return path.endswith("_test.go")

    @staticmethod
    def _dir_of(path: str) -> str:
        return "/".join(path.split("/")[:-1]) if "/" in path else ""

    def _unit_name(self, dir_path: str) -> str:
        """Human-readable name for a package directory ('' -> 'root')."""
        if not dir_path:
            return "root"
        return dir_path.replace("/", "-")

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """Discover Go code units: one per package directory, plus tests, build
        config, infrastructure, Kubernetes, and documentation."""
        code_units: List[CodeUnit] = []

        code_units.extend(self._discover_go_packages(files))
        code_units.extend(self._discover_go_tests(files))

        build_unit = self._discover_build_config(files)
        if build_unit:
            code_units.append(build_unit)

        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        k8s_unit = self.discover_kubernetes(files)
        if k8s_unit:
            code_units.append(k8s_unit)

        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        logger.info(f"Discovered {len(code_units)} code units")
        return code_units

    def _discover_go_packages(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """One code unit per directory containing non-test .go files."""
        by_dir: Dict[str, List[str]] = {}
        for f in files:
            if f["type"] != "blob":
                continue
            path = f["path"]
            if not path.endswith(".go") or self._is_test_file(path):
                continue
            if self.is_excluded_path(path):
                continue
            by_dir.setdefault(self._dir_of(path), []).append(path)

        units: List[CodeUnit] = []
        for dir_path, pkg_files in sorted(by_dir.items()):
            units.append(CodeUnit(
                name=self._unit_name(dir_path),
                unit_type="source-module",
                file_paths=sorted(pkg_files),
                metadata={
                    "batch_type": "source-module",
                    "directory": dir_path,
                    "package": dir_path.split("/")[-1] if dir_path else "main",
                    "discovery_reason": "go-package",
                },
            ))
        return units

    def _discover_go_tests(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """Group *_test.go files into a single tests unit."""
        test_files = [
            f["path"] for f in files
            if f["type"] == "blob"
            and self._is_test_file(f["path"])
            and not self.is_excluded_path(f["path"])
        ]
        if not test_files:
            return []
        return [CodeUnit(
            name="unit",
            unit_type="tests",
            file_paths=sorted(test_files),
            metadata={"batch_type": "tests", "test_type": "unit"},
        )]

    def _discover_build_config(self, files: List[Dict[str, Any]]) -> Optional[CodeUnit]:
        """go.mod / go.sum form the build config unit."""
        build_files = [
            f["path"] for f in files
            if f["type"] == "blob"
            and f["path"].split("/")[-1] in {"go.mod", "go.sum", "go.work", "go.work.sum"}
        ]
        if not build_files:
            return None
        return CodeUnit(
            name="build-config",
            unit_type="build",
            file_paths=sorted(build_files),
            metadata={"batch_type": "build"},
        )

    # =========================================================================
    # Change Detection (git SHA comparison via GitHub API)
    # =========================================================================

    def detect_changes(
        self,
        repo: str,
        repo_project: str,
        last_state: Optional[Dict[str, Any]],
        force_full: bool = False,
    ) -> ChangeDetectionResult:
        current_sha = self._get_current_commit_sha(repo_project)

        if force_full or not last_state or "commit_sha" not in last_state:
            logger.info(f"Full regeneration requested or no previous state for {repo}")
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
            logger.info(f"No changes detected for {repo}: {current_sha}")
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
            f"Found {len(changed_files)} changed files, {len(removed_files)} "
            f"removed files between {last_sha[:7]}..{current_sha[:7]}"
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

    def _gh_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcome-ops-generate-code-maps",
        }

    def _get_default_branch(self, repo_project: str) -> str:
        url = f"{self.github_api_url}/repos/{repo_project}"
        try:
            with urlopen(Request(url, headers=self._gh_headers()), timeout=30) as r:
                return json.loads(r.read().decode("utf-8")).get("default_branch", "main")
        except URLError as e:
            logger.warning(f"Failed to get default branch for {repo_project}, using 'main': {e}")
            return "main"

    def _get_current_commit_sha(self, repo_project: str) -> str:
        default_branch = self._get_default_branch(repo_project)
        url = f"{self.github_api_url}/repos/{repo_project}/branches/{default_branch}"
        try:
            with urlopen(Request(url, headers=self._gh_headers()), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))["commit"]["sha"]
        except URLError as e:
            logger.error(f"Failed to get current commit SHA: {e}")
            raise

    def _get_changed_files(self, repo_project, base_sha, head_sha):
        url = f"{self.github_api_url}/repos/{repo_project}/compare/{base_sha}...{head_sha}"
        try:
            with urlopen(Request(url, headers=self._gh_headers()), timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except URLError as e:
            logger.error(f"Failed to get changed files: {e}")
            raise
        changed_files, removed_files = [], []
        for f in data.get("files", []):
            if f.get("status") == "removed":
                removed_files.append(f["filename"])
            else:
                changed_files.append(f["filename"])
        return changed_files, removed_files

    # =========================================================================
    # Batch Metadata and Storage
    # =========================================================================

    def generate_batch_metadata(self, code_unit: CodeUnit, repo: str) -> Dict[str, Any]:
        return {
            "batch_type": code_unit.metadata.get("batch_type", code_unit.unit_type),
            "group_name": code_unit.name,
            "files": [{"path": path} for path in code_unit.file_paths],
            "storage_key": self.get_storage_key(code_unit),
            "backend_type": "go",
        }

    def get_storage_key(self, code_unit: CodeUnit) -> str:
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


# Auto-register Go backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("go", GoBackend)
