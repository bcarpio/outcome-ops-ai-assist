"""Pytest configuration and fixtures."""

import sys
from pathlib import Path

# Add lambda directories to path so we can import handlers
lambda_dir = Path(__file__).parent.parent
sys.path.insert(0, str(lambda_dir))
sys.path.insert(0, str(lambda_dir / "ingest-docs"))
