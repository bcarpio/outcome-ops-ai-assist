# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Backend abstraction layer for code generation.

This module provides pluggable backends for generating code in different languages:
- Python Lambda backend (AWS Lambda with Python)
- Java Spring backend (Spring Boot microservices)
- TypeScript backend (Node.js/TypeScript applications)
- React backend (React/Next.js frontend applications)

Each backend defines language-specific prompts, validation, and formatting.

NOTE: This package is named codegen_backends (not backends) to avoid collision
with the generate-code-maps/backends package during testing.
"""

import os
import sys

# Handle imports for both runtime (relative) and testing (absolute) scenarios
try:
    from .base import (
        CodeGeneratorBackend,
        FormattingResult,
        StandardsContext,
        ValidationResult,
    )
    from .factory import (
        get_backend,
        get_backend_for_repo,
        list_backends,
        register_backend,
    )
    # Import backends to trigger auto-registration
    from .python import PythonCodeGenerator
    from .java import JavaCodeGenerator
    from .typescript import TypeScriptCodeGenerator
    from .react import ReactCodeGenerator
except ImportError:
    # Add codegen_backends directory to path for testing
    codegen_backends_dir = os.path.dirname(os.path.abspath(__file__))
    if codegen_backends_dir not in sys.path:
        sys.path.insert(0, codegen_backends_dir)
    from base import (  # noqa: F401
        CodeGeneratorBackend,
        FormattingResult,
        StandardsContext,
        ValidationResult,
    )
    from factory import (  # noqa: F401
        get_backend,
        get_backend_for_repo,
        list_backends,
        register_backend,
    )
    from python import PythonCodeGenerator  # noqa: F401
    from java import JavaCodeGenerator  # noqa: F401
    from typescript import TypeScriptCodeGenerator  # noqa: F401
    from react import ReactCodeGenerator  # noqa: F401

# Register backends
register_backend("python", PythonCodeGenerator)
register_backend("java", JavaCodeGenerator)
register_backend("typescript", TypeScriptCodeGenerator)
register_backend("react", ReactCodeGenerator)

__all__ = [
    "CodeGeneratorBackend",
    "FormattingResult",
    "StandardsContext",
    "ValidationResult",
    "get_backend",
    "get_backend_for_repo",
    "list_backends",
    "register_backend",
    "PythonCodeGenerator",
    "JavaCodeGenerator",
    "TypeScriptCodeGenerator",
    "ReactCodeGenerator",
]
