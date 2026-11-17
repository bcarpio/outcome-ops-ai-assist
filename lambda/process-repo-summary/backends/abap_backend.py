# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
ABAP backend for code map generation.

Discovers ABAP source files in abapGit repository format, supporting:
- ABAP source files (.abap) - classes, interfaces, function modules, programs
- CDS views (.asddls) - Core Data Services definitions

This backend uses the generic discovery pattern from ADR-009.
XML metadata files are NOT included - they belong in ingest-docs as documentation.
"""

import logging
import os
import re
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

# ABAP object type mapping based on abapGit filename conventions
# Format: filename pattern -> (object_type, description)
ABAP_OBJECT_TYPES = {
    ".clas.abap": ("class", "ABAP Class"),
    ".intf.abap": ("interface", "ABAP Interface"),
    ".fugr.abap": ("function_group", "Function Group"),
    ".prog.abap": ("program", "ABAP Program/Report"),
    ".ddls.asddls": ("cds_view", "CDS Data Definition"),
    ".dcls.asdcls": ("cds_dcl", "CDS Access Control"),
    ".srvd.srvdsrv": ("service_definition", "Service Definition"),
    ".srvb.srvb": ("service_binding", "Service Binding"),
    ".msag.abap": ("message_class", "Message Class"),
    ".ttyp.abap": ("table_type", "Table Type"),
    ".dtel.abap": ("data_element", "Data Element"),
    ".doma.abap": ("domain", "Domain"),
    ".tabl.abap": ("table", "Database Table"),
    ".view.abap": ("view", "Database View"),
    ".enqu.abap": ("lock_object", "Lock Object"),
    ".shlp.abap": ("search_help", "Search Help"),
    ".tran.abap": ("transaction", "Transaction"),
}


class ABAPBackend(CodeMapBackend):
    """
    Backend for SAP ABAP applications using abapGit format.

    Supports:
    - abapGit repository structure (src/ directory)
    - ABAP source files (.abap) - classes, interfaces, function modules
    - CDS views (.asddls) - Core Data Services definitions

    Uses ADR-009 generic discovery algorithm to find code unit boundaries
    based on directory structure and file count.

    Note: XML metadata files are NOT processed here - they belong in
    ingest-docs as documentation/configuration, not source code.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize ABAP backend.

        Config options:
            - source_roots: Override default source roots (default: ["src"])
            - min_files_threshold: Minimum files to form code unit (default: 2)
            - custom_boundary_patterns: Additional boundary patterns
            - github_token: GitHub API token (required for change detection)
            - github_api_url: GitHub API base URL (default: f"{_github_api()}")
        """
        super().__init__(config)
        self.github_token = config.get("github_token")
        self.github_api_url = config.get("github_api_url", f"{_github_api()}")

    def get_backend_name(self) -> str:
        return "SAP ABAP Backend"

    def get_backend_type(self) -> str:
        return "abap"

    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate ABAP backend configuration."""
        if not self.github_token:
            return False, "github_token is required for change detection"
        return True, None

    # =========================================================================
    # Language Configuration (ADR-009)
    # =========================================================================

    def get_source_extensions(self) -> Set[str]:
        """
        ABAP source file extensions.

        Only includes actual source code files:
        - .abap - ABAP source code (classes, interfaces, function modules, programs)
        - .asddls - CDS Data Definition Language (Core Data Services views)

        XML metadata files (.xml) are NOT included - they are configuration/metadata
        and belong in ingest-docs, not code-maps.
        """
        return {".abap", ".asddls"}

    def get_source_roots(self) -> List[str]:
        """
        Source roots for abapGit projects.

        Standard abapGit convention uses src/ as the main source directory.
        Can be overridden via source_roots in .outcomeops.yaml config.
        """
        configured_roots = self.config.get("source_roots")
        if configured_roots:
            return configured_roots

        return ["src"]

    # =========================================================================
    # ABAP Object Type Detection
    # =========================================================================

    def get_abap_object_type(self, file_path: str) -> Optional[str]:
        """
        Detect ABAP object type from filename.

        abapGit encodes object types in filenames:
        - zcl_flight.clas.abap -> class
        - zif_flight.intf.abap -> interface
        - zfm_travel.fugr.abap -> function_group
        - zi_agency.ddls.asddls -> cds_view

        Args:
            file_path: Path to ABAP source file

        Returns:
            Object type string or None if not recognized
        """
        for pattern, (obj_type, _) in ABAP_OBJECT_TYPES.items():
            if file_path.endswith(pattern):
                return obj_type

        # Fallback: check for generic .abap or .asddls
        if file_path.endswith(".asddls"):
            return "cds_view"
        elif file_path.endswith(".abap"):
            return "unknown_abap"

        return None

    def analyze_object_types(self, file_paths: List[str]) -> Dict[str, Any]:
        """
        Analyze ABAP files and return object type breakdown.

        Args:
            file_paths: List of ABAP source file paths

        Returns:
            Dictionary with object type counts and details
        """
        type_counts: Dict[str, int] = defaultdict(int)
        files_by_type: Dict[str, List[str]] = defaultdict(list)

        for path in file_paths:
            obj_type = self.get_abap_object_type(path)
            if obj_type:
                type_counts[obj_type] += 1
                files_by_type[obj_type].append(path)

        return {
            "object_type_counts": dict(type_counts),
            "files_by_type": dict(files_by_type),
            "total_objects": sum(type_counts.values()),
        }

    # =========================================================================
    # Code Unit Discovery
    # =========================================================================

    def discover_code_units(self, files: List[Dict[str, Any]]) -> List[CodeUnit]:
        """
        Discover code units from ABAP repository files.

        Uses the generic ADR-009 algorithm for source code discovery,
        enhanced with ABAP object type metadata:
        - Classes, interfaces, function modules
        - CDS views and access controls
        - Service definitions and bindings

        Also discovers:
        - Infrastructure (Terraform files)
        - Tests (grouped by type)
        - Documentation (markdown files)

        Args:
            files: List of file objects from GitHub API

        Returns:
            List of discovered CodeUnit objects with ABAP object type metadata
        """
        code_units = []

        # Group 1: Source code units (uses generic ADR-009 algorithm)
        source_units = self.discover_source_units(files)

        # Enhance source units with ABAP object type metadata
        for unit in source_units:
            object_analysis = self.analyze_object_types(unit.file_paths)
            unit.metadata["abap_objects"] = object_analysis
            logger.debug(
                f"Code unit '{unit.name}': {object_analysis['total_objects']} objects "
                f"({object_analysis['object_type_counts']})"
            )

        code_units.extend(source_units)

        # Group 2: Test code units
        test_units = self.discover_tests(files)
        code_units.extend(test_units)

        # Group 3: Infrastructure (Terraform)
        infra_unit = self.discover_infrastructure(files)
        if infra_unit:
            code_units.append(infra_unit)

        # Group 4: Kubernetes manifests (uses base class generic method)
        k8s_unit = self.discover_kubernetes(files)
        if k8s_unit:
            code_units.append(k8s_unit)

        # Group 5: Documentation
        docs_unit = self.discover_documentation(files)
        if docs_unit:
            code_units.append(docs_unit)

        logger.info(f"Discovered {len(code_units)} ABAP code units")
        return code_units

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

        Includes ABAP object type information for richer context.

        Args:
            code_unit: Code unit to generate metadata for
            repo: Repository name

        Returns:
            Batch metadata dictionary with ABAP object types
        """
        metadata = {
            "batch_type": code_unit.metadata.get("batch_type", code_unit.unit_type),
            "group_name": code_unit.name,
            "files": [{"path": path} for path in code_unit.file_paths],
            "storage_key": self.get_storage_key(code_unit),
            "backend_type": "abap",
        }

        # Include ABAP object type information if available
        if "abap_objects" in code_unit.metadata:
            metadata["abap_objects"] = code_unit.metadata["abap_objects"]

        return metadata

    def get_storage_key(self, code_unit: CodeUnit) -> str:
        """
        Generate DynamoDB storage key for code unit.

        Args:
            code_unit: Code unit to generate key for

        Returns:
            Storage key (SK) for DynamoDB

        Examples:
            source-module: "summary#abap-module#legacy"
            tests: "summary#tests#unit"
            infrastructure: "summary#infrastructure"
        """
        batch_type = code_unit.metadata.get("batch_type", code_unit.unit_type)

        if batch_type == "source-module":
            return f"summary#abap-module#{code_unit.name}"
        elif batch_type == "tests":
            return f"summary#tests#{code_unit.name}"
        elif batch_type == "kubernetes":
            return "summary#kubernetes"
        else:
            return f"summary#{batch_type}"


# Auto-register ABAP backend
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("abap", ABAPBackend)
