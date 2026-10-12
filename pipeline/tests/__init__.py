"""Test package.

Calibration runs only on Coder (`pipeline/benchmark/coder.py`). The suite never
reaches a Coder deployment: it exercises the pipeline with native campaigns
patched or run locally, so it selects the local backend explicitly.
"""
import os

os.environ.setdefault("ARENA_NATIVE_BACKEND", "local")
