#!/usr/bin/env python3
"""Compatibility shim for the former Codex-lite launcher.

Use ``archon-run.py`` for generated packets and new automation.
"""
from pathlib import Path

_PRIMARY = Path(__file__).with_name("archon-run.py")
exec(compile(_PRIMARY.read_bytes(), str(_PRIMARY), "exec"), globals(), globals())
