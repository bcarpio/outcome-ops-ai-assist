"""
Backend factory for code generator backends.

Provides a registry-based factory pattern for instantiating code generator backends.
"""

import logging
from typing import Any, Dict, Optional, Type

from .base import CodeGeneratorBackend

logger = logging.getLogger()

# Registry of available backends
_BACKEND_REGISTRY: Dict[str, Type[CodeGeneratorBackend]] = {}


def register_backend(name: str, backend_class: Type[CodeGeneratorBackend]) -> None:
    """
    Register a backend class.

    Args:
        name: Backend identifier (e.g., 'python')
        backend_class: Backend class to register
    """
    _BACKEND_REGISTRY[name] = backend_class
    logger.debug(f"Registered code generator backend: {name}")


def get_backend(
    name: str, repo_config: Optional[Dict[str, Any]] = None
) -> CodeGeneratorBackend:
    """
    Get a backend instance by name.

    Args:
        name: Backend identifier (e.g., 'python')
        repo_config: Optional repository configuration

    Returns:
        CodeGeneratorBackend instance

    Raises:
        ValueError: If backend not found
    """
    if name not in _BACKEND_REGISTRY:
        available = ", ".join(_BACKEND_REGISTRY.keys())
        raise ValueError(
            f"Unknown code generator backend: {name}. Available: {available}"
        )

    backend_class = _BACKEND_REGISTRY[name]
    return backend_class(repo_config=repo_config)


def list_backends() -> Dict[str, str]:
    """
    List all registered backends.

    Returns:
        Dict mapping backend name to display name
    """
    result = {}
    for name, backend_class in _BACKEND_REGISTRY.items():
        # Create temporary instance to get display name
        instance = backend_class()
        result[name] = instance.display_name
    return result


def get_backend_for_repo(
    repo_config: Optional[Dict[str, Any]] = None,
    default: str = "python",
) -> CodeGeneratorBackend:
    """
    Get the appropriate backend for a repository.

    Reads the backend from repo_config['code_generation']['backend'],
    falling back to default if not specified.

    Args:
        repo_config: Repository configuration from .outcomeops.yaml
        default: Default backend if not specified in config

    Returns:
        CodeGeneratorBackend instance
    """
    if repo_config:
        backend_name = (
            repo_config.get("code_generation", {}).get("backend", default)
        )
    else:
        backend_name = default

    logger.info(f"Using code generator backend: {backend_name}")
    return get_backend(backend_name, repo_config=repo_config)
