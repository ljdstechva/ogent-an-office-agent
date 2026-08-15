#!/usr/bin/env python3
"""Compatibility launcher for the modular Ogent application."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ogent_app.compat.runtime_loader import load_runtime  # noqa: E402


load_runtime(globals())
