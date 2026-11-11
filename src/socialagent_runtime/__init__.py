# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Editable-install stand-in for ``socialagent_runtime``. The runtime is not here.

The runtime's source is ``packages/socialagent-runtime/``, and a wheel installs
it as ``socialagent_runtime`` through a source rewrite in ``pyproject.toml``.
An editable install cannot rename a directory (hatch refuses a rewrite that
changes a prefix), so it puts ``src/`` on the path and this module swaps itself
for the real package, loaded from that directory on every import, so an edit
there is live without a reinstall. ``only-include`` leaves this directory out
of every wheel and of the Lambda layer.
"""

import importlib.util
import sys
from pathlib import Path

# src/socialagent_runtime/__init__.py -> parents[2] == repo root
_RUNTIME_DIR = Path(__file__).resolve().parents[2] / "packages" / "socialagent-runtime"

_spec = importlib.util.spec_from_file_location(
    __name__,
    _RUNTIME_DIR / "__init__.py",
    submodule_search_locations=[str(_RUNTIME_DIR)],
)
_module = importlib.util.module_from_spec(_spec)
# The import system returns whatever sys.modules holds for this name once this
# body finishes, so the real package, not this stand-in, is what callers get.
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
