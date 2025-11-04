"""
Backend factory and registry for code map generation.

Provides factory methods to instantiate backends and list available backends.
"""

import logging
import os
import sys
from typing import Dict, List, Optional, Type

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import CodeMapBackend
except ImportError:
    # Add backends directory to path for testing
    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)
    from base import CodeMapBackend  # noqa: F401

logger = logging.getLogger(__name__)

# Global registry of available backends
_BACKEND_REGISTRY: Dict[str, Type[CodeMapBackend]] = {}


def register_backend(name: str, backend_class: Type[CodeMapBackend]) -> None:
    """
    Register a backend implementation.

    Args:
        name: Backend identifier (e.g., "lambda", "k8s", "monolith")
        backend_class: Backend class (must inherit from CodeMapBackend)

    Raises:
        ValueError: If backend is already registered or invalid
    """
    if not issubclass(backend_class, CodeMapBackend):
        raise ValueError(f"Backend class must inherit from CodeMapBackend: {backend_class}")

    if name in _BACKEND_REGISTRY:
        logger.warning(f"Backend '{name}' already registered, replacing with {backend_class}")

    _BACKEND_REGISTRY[name] = backend_class
    logger.info(f"Registered backend: {name} -> {backend_class.__name__}")


def get_backend(name: str, config: Dict) -> CodeMapBackend:
    """
    Get backend instance by name.

    Args:
        name: Backend identifier (e.g., "lambda", "k8s", "monolith")
        config: Backend-specific configuration

    Returns:
        Initialized backend instance

    Raises:
        ValueError: If backend not found or invalid configuration
    """
    if name not in _BACKEND_REGISTRY:
        available = ", ".join(_BACKEND_REGISTRY.keys()) or "none"
        raise ValueError(
            f"Backend '{name}' not found. Available backends: {available}"
        )

    backend_class = _BACKEND_REGISTRY[name]
    backend = backend_class(config)

    # Validate configuration
    is_valid, error_msg = backend.validate_config()
    if not is_valid:
        raise ValueError(f"Invalid backend configuration: {error_msg}")

    logger.info(f"Initialized backend: {name} ({backend.get_backend_name()})")
    return backend


def list_backends() -> List[Dict[str, str]]:
    """
    List all available backends.

    Returns:
        List of backend info dictionaries with 'name' and 'class' keys

    Example:
        [
            {"name": "lambda", "class": "LambdaServerlessBackend"},
            {"name": "k8s", "class": "KubernetesBackend"}
        ]
    """
    return [
        {
            "name": name,
            "class": backend_class.__name__,
            "description": backend_class.__doc__.split("\n")[0] if backend_class.__doc__ else "No description"
        }
        for name, backend_class in _BACKEND_REGISTRY.items()
    ]


def is_backend_registered(name: str) -> bool:
    """
    Check if a backend is registered.

    Args:
        name: Backend identifier

    Returns:
        True if backend is registered, False otherwise
    """
    return name in _BACKEND_REGISTRY
