"""
Backend abstraction layer for code map generation.

This module provides pluggable backends for analyzing different code architectures:
- Lambda serverless backend (current implementation)
- Kubernetes backend (future)
- Monolith backend (future)

Each backend discovers code units, detects changes, and generates code maps
specific to that architecture.
"""

import os
import sys

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import CodeMapBackend
    from .factory import get_backend, list_backends, register_backend
except ImportError:
    # Add backends directory to path for testing
    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)
    from base import CodeMapBackend  # noqa: F401
    from factory import get_backend, list_backends, register_backend  # noqa: F401

__all__ = [
    "CodeMapBackend",
    "get_backend",
    "list_backends",
    "register_backend",
]
