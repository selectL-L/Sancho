"""Pytest configuration for the test suite.

This file is automatically loaded by pytest before any tests run.
It ensures the project root is in sys.path so that imports work correctly
in CI environments (e.g., GitHub Actions).
"""
import os
import sys

# Add project root to sys.path BEFORE any test modules are imported
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
