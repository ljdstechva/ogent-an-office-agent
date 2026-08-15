#!/usr/bin/env python3
"""Compatibility facade for the extracted agent providers module."""

from __future__ import annotations

from ogent_app.compat.module_fragments import load_module_fragments


load_module_fragments(
    globals(),
    'agent_providers',
    (
        'foundation',
    'providers',
    'claude',
    ),
)
