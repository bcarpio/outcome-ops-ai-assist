"""
Backend abstraction layer for code map generation.

This module provides pluggable backends for analyzing different code architectures:
- Python backend (Python applications)
- Java backend (Maven/Gradle projects, Spring Boot)
- TypeScript backend (Node.js, serverless)
- React backend (React/Next.js frontend applications)
- ABAP backend (SAP ABAP applications)

Each backend discovers code units, detects changes, and generates code maps
specific to that architecture.

See ADR-009 for the code unit discovery pattern documentation.
"""

import os
import sys

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import CodeMapBackend
    from .factory import get_backend, list_backends, register_backend
    # Import backends to trigger auto-registration
    from . import python_backend  # noqa: F401
    from . import java_backend  # noqa: F401
    from . import typescript_backend  # noqa: F401
    from . import abap_backend  # noqa: F401
    from . import react_backend  # noqa: F401
except ImportError:
    # Add backends directory to path for testing
    backends_dir = os.path.dirname(os.path.abspath(__file__))
    if backends_dir not in sys.path:
        sys.path.insert(0, backends_dir)
    from base import CodeMapBackend  # noqa: F401
    from factory import get_backend, list_backends, register_backend  # noqa: F401
    import python_backend  # noqa: F401
    import java_backend  # noqa: F401
    import typescript_backend  # noqa: F401
    import abap_backend  # noqa: F401
    import react_backend  # noqa: F401

__all__ = [
    "CodeMapBackend",
    "get_backend",
    "list_backends",
    "register_backend",
]
