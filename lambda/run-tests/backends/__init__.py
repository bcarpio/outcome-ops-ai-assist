"""
Run-tests backends for different project types.

This module provides language/framework-specific test runners:
- Python: pytest with virtualenv, Claude auto-fix for import/syntax errors
- Java: Gradle/Maven test runners, Claude auto-fix for compilation errors
- TypeScript: npm/yarn/pnpm with Jest/Vitest, Claude auto-fix for import/type errors

When no backend is available for a project type, the system will
still create a PR and let the repository's CI handle testing.
"""

import os
import sys
import importlib.util

_backends_dir = os.path.dirname(os.path.abspath(__file__))


def _load_local_module(name: str, filename: str):
    """Load a module from the backends directory, avoiding cached modules."""
    filepath = os.path.join(_backends_dir, filename)
    # Use a unique module name to avoid conflicts
    module_name = f"run_tests_backends_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Load base module first (needed by others)
_base_module = _load_local_module("base", "test_backend_base.py")
TestBackend = _base_module.TestBackend
TestResult = _base_module.TestResult
CommandResult = _base_module.CommandResult

# Register base module in sys.modules BEFORE loading factory (factory imports from it)
sys.modules["test_backend_base"] = _base_module

# Load factory module
_factory_module = _load_local_module("factory", "factory.py")
get_backend = _factory_module.get_backend
register_backend = _factory_module.register_backend

# Make factory available for the backend modules to import
sys.modules["factory"] = _factory_module

# Load backend implementations (they register themselves)
_python_backend = _load_local_module("python_backend", "python_backend.py")
_java_backend = _load_local_module("java_backend", "java_backend.py")
_typescript_backend = _load_local_module("typescript_backend", "typescript_backend.py")

__all__ = [
    "TestBackend",
    "TestResult",
    "CommandResult",
    "get_backend",
    "register_backend",
]
