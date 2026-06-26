"""Shared pytest fixtures for yggdrasil tests."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from fakes import ...` when running with PYTHONPATH=src only.
_TESTS_ROOT = Path(__file__).resolve().parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
