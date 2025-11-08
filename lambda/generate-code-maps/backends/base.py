"""
Base abstract class for code map generation backends.

Each backend implements methods for:
- Discovering code units (e.g., Lambda handlers, K8s services, modules)
- Detecting changes since last run (git diff, timestamps, etc.)
- Generating architecture-specific prompts for Claude
- Parsing Claude's output into structured code maps
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


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
        changed_files: Optional[List[str]] = None
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
        """
        self.has_changes = has_changes
        self.changed_units = changed_units
        self.unchanged_units = unchanged_units
        self.last_commit_sha = last_commit_sha
        self.current_commit_sha = current_commit_sha
        self.changed_files = changed_files or []

    def __repr__(self) -> str:
        return (
            f"ChangeDetectionResult(has_changes={self.has_changes}, "
            f"changed={len(self.changed_units)}, unchanged={len(self.unchanged_units)}, "
            f"files={len(self.changed_files)})"
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
