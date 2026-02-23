"""
Factory for creating test backends based on project configuration.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Type
import yaml

from test_backend_base import TestBackend

logger = logging.getLogger(__name__)

# Registry of available backends
_BACKENDS: Dict[str, Type[TestBackend]] = {}


def register_backend(backend_type: str, backend_class: Type[TestBackend]) -> None:
    """
    Register a backend class for a given type.

    Args:
        backend_type: Backend identifier (e.g., "python", "java")
        backend_class: Backend class to register
    """
    _BACKENDS[backend_type] = backend_class
    logger.info(f"Registered test backend: {backend_type}")


def get_backend(repo_dir: str) -> Optional[TestBackend]:
    """
    Get the appropriate test backend for a repository.

    Reads .outcomeops.yaml to determine the backend type, then
    instantiates the appropriate backend class.

    Args:
        repo_dir: Path to the cloned repository

    Returns:
        TestBackend instance, or None if no backend is available
    """
    config = _load_repo_config(repo_dir)
    if not config:
        logger.warning(f"No .outcomeops.yaml found in {repo_dir}")
        return None

    # Get backend type from config
    backend_type = config.get("code_generation", {}).get("backend", "python")
    logger.info(f"Repository backend type: {backend_type}")

    # Look up backend class
    backend_class = _BACKENDS.get(backend_type)
    if not backend_class:
        logger.warning(f"No test backend registered for type: {backend_type}")
        return None

    return backend_class(repo_dir, config)


def _load_repo_config(repo_dir: str) -> Optional[Dict[str, Any]]:
    """Load .outcomeops.yaml from repository."""
    config_path = Path(repo_dir) / ".outcomeops.yaml"
    if not config_path.exists():
        # Try .outcomeops.yml as alternative
        config_path = Path(repo_dir) / ".outcomeops.yml"
        if not config_path.exists():
            return None

    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load {config_path}: {e}")
        return None


def list_backends() -> Dict[str, Type[TestBackend]]:
    """Return all registered backends."""
    return dict(_BACKENDS)
