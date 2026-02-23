"""
Unit tests for check_handlers package initialization.

Tests that the package exports are correctly configured.
"""

import sys
import os
import importlib.util
from types import ModuleType
from unittest.mock import MagicMock, patch


# Mock boto3 before any imports
mock_boto3 = MagicMock()
sys.modules['boto3'] = mock_boto3

# Mock botocore and all its submodules
mock_botocore = MagicMock()
mock_botocore_exceptions = MagicMock()
mock_botocore_exceptions.ClientError = Exception
mock_botocore_config = MagicMock()
sys.modules['botocore'] = mock_botocore
sys.modules['botocore.exceptions'] = mock_botocore_exceptions
sys.modules['botocore.config'] = mock_botocore_config

# Base path for check_handlers
check_handlers_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers'
)


def load_module(name, filename):
    """Load a module from the check_handlers directory."""
    filepath = os.path.join(check_handlers_path, filename)
    spec = importlib.util.spec_from_file_location(name, filepath)
    module = importlib.util.module_from_spec(spec)
    return module, spec


# Create the check_handlers package first
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [check_handlers_path]
sys.modules['check_handlers'] = check_handlers_package

# Load bedrock_utils first (dependency for others)
bedrock_utils_module, bedrock_utils_spec = load_module(
    "check_handlers.bedrock_utils", "bedrock_utils.py"
)
sys.modules['check_handlers.bedrock_utils'] = bedrock_utils_module
bedrock_utils_spec.loader.exec_module(bedrock_utils_module)

# Load individual handler modules
handler_modules = [
    ("adr_compliance", "adr_compliance.py"),
    ("architectural_duplication", "architectural_duplication.py"),
    ("breaking_changes", "breaking_changes.py"),
    ("license_compliance", "license_compliance.py"),
    ("readme_freshness", "readme_freshness.py"),
    ("test_coverage", "test_coverage.py"),
]

for mod_name, filename in handler_modules:
    full_name = f"check_handlers.{mod_name}"
    module, spec = load_module(full_name, filename)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)

# Now load the __init__.py
init_path = os.path.join(check_handlers_path, "__init__.py")
init_spec = importlib.util.spec_from_file_location(
    "check_handlers",
    init_path,
    submodule_search_locations=[check_handlers_path]
)
# Re-use the package module but exec the init
init_spec.loader.exec_module(check_handlers_package)


class TestCheckHandlersExports:
    """Test check_handlers package exports."""

    def test_exports_check_adr_compliance(self):
        """Test: Package exports check_adr_compliance."""
        assert hasattr(check_handlers_package, 'check_adr_compliance')
        assert 'check_adr_compliance' in check_handlers_package.__all__

    def test_exports_check_architectural_duplication(self):
        """Test: Package exports check_architectural_duplication."""
        assert hasattr(check_handlers_package, 'check_architectural_duplication')
        assert 'check_architectural_duplication' in check_handlers_package.__all__

    def test_exports_check_breaking_changes(self):
        """Test: Package exports check_breaking_changes."""
        assert hasattr(check_handlers_package, 'check_breaking_changes')
        assert 'check_breaking_changes' in check_handlers_package.__all__

    def test_exports_check_license_compliance(self):
        """Test: Package exports check_license_compliance."""
        assert hasattr(check_handlers_package, 'check_license_compliance')
        assert 'check_license_compliance' in check_handlers_package.__all__

    def test_exports_check_readme_freshness(self):
        """Test: Package exports check_readme_freshness."""
        assert hasattr(check_handlers_package, 'check_readme_freshness')
        assert 'check_readme_freshness' in check_handlers_package.__all__

    def test_exports_check_test_coverage(self):
        """Test: Package exports check_test_coverage."""
        assert hasattr(check_handlers_package, 'check_test_coverage')
        assert 'check_test_coverage' in check_handlers_package.__all__

    def test_exports_usage_tracker(self):
        """Test: Package exports UsageTracker."""
        assert hasattr(check_handlers_package, 'UsageTracker')
        assert 'UsageTracker' in check_handlers_package.__all__

    def test_all_exports_count(self):
        """Test: __all__ has expected number of exports."""
        assert len(check_handlers_package.__all__) == 7
