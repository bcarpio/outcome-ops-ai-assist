"""
Backend abstraction layer for code map generation.

This module provides pluggable backends for analyzing different code architectures:
- Lambda serverless backend (current implementation)
- Kubernetes backend (future)
- Monolith backend (future)

Each backend discovers code units, detects changes, and generates code maps
specific to that architecture.
"""

from .base import CodeMapBackend
from .factory import get_backend, list_backends, register_backend

__all__ = [
    "CodeMapBackend",
    "get_backend",
    "list_backends",
    "register_backend",
]
