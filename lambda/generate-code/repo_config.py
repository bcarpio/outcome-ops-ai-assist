"""
Repository configuration shim — backwards-compat re-export.

The canonical schema lives in ``lambda/shared/repo_config.py`` so that
both ``generate-code`` and ``process-batch-summary`` can consume the
same Pydantic models. This module is preserved as a
re-export so existing imports inside ``generate-code/`` (and the test
suite, which loads ``repo_config.py`` via ``importlib.spec_from_file_location``)
keep working without churn.

DO NOT add new schema fields here — extend
``lambda/shared/repo_config.py`` instead.
"""

import os
import sys

# Make ``shared`` resolvable whether this module is loaded via the
# Lambda runtime (cwd=/var/task with ``generate-code`` on sys.path) or
# via importlib in the test suite (which adds GENERATE_CODE_DIR but not
# the lambda root).
_LAMBDA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.repo_config import (  # noqa: E402,F401
    EXAMPLE_CONFIG,
    CodeGenerationConfig,
    InfrastructureConfig,
    IngestionConfig,
    RepoConfig,
    RequiredAdrsConfig,
    SymbolGraphConfig,
    TestingConfig,
    get_default_config,
    load_repo_config_from_github,
    parse_repo_config,
)
