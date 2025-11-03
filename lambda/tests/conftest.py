"""
Pytest configuration and shared fixtures for all tests.

This file is automatically loaded by pytest and makes fixtures
available to all test files.
"""

import sys
import os

# Add parent directory to path so tests can import from lambda modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
