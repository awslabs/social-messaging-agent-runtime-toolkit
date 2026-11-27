# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Declare dev mode before importing socialagent_runtime, whose config.py builds a
# module-level singleton at import and refuses to start without either a real
# notification Lambda ARN or this sentinel (an empty value is not inferred as dev
# mode). setdefault, so a test that wants production config can set it first.
os.environ.setdefault("NOTIFICATION_LAMBDA_ARN", "__socialagent_dev__")

# Rich reads any non-empty FORCE_COLOR as on (0 included) and TTY_COMPATIBLE=1 as
# a terminal, and the CLI console in socialagent.cli.dev fixes its colour system
# when it is built at import, during collection, so an autouse fixture is too
# late. With either set, escape codes land between wrapped words in captured CLI
# output (#233). COLUMNS and LINES set its size the same way, and a narrow
# terminal wraps a phrase the tests look for. Cleared here so the suite does not
# depend on the terminal; pass pytest --color=yes for coloured pytest output.
for _terminal in ("FORCE_COLOR", "TTY_COMPATIBLE", "COLUMNS", "LINES"):
    os.environ.pop(_terminal, None)

# Mock bedrock_agentcore before importing socialagent_runtime — it's not installed in test env
if "bedrock_agentcore" not in sys.modules:
    mock_bgac = MagicMock()
    sys.modules["bedrock_agentcore"] = mock_bgac
    sys.modules["bedrock_agentcore.memory"] = mock_bgac.memory
    sys.modules["bedrock_agentcore.memory.integrations"] = mock_bgac.memory.integrations
    sys.modules["bedrock_agentcore.memory.integrations.strands"] = (
        mock_bgac.memory.integrations.strands
    )
    sys.modules["bedrock_agentcore.memory.integrations.strands.config"] = (
        mock_bgac.memory.integrations.strands.config
    )
    sys.modules["bedrock_agentcore.memory.integrations.strands.session_manager"] = (
        mock_bgac.memory.integrations.strands.session_manager
    )
    sys.modules["bedrock_agentcore.runtime"] = mock_bgac.runtime

# Make socialagent_runtime importable in tests — source dir is named socialagent-runtime (dash).
_pkg_dir = Path(__file__).parent.parent / "packages" / "socialagent-runtime"
if "socialagent_runtime" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "socialagent_runtime",
        _pkg_dir / "__init__.py",
        submodule_search_locations=[str(_pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["socialagent_runtime"] = mod
    spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _clear_sns_signing_key_cache():
    """Start every test with an empty SNS signing-cert cache.

    The provider caches fetched certs by URL for the life of the process, and
    tests reuse one URL with different certs (and failing fetches), so a key
    cached by one test would decide the next test's outcome.
    """
    eum_social = sys.modules.get("socialagent.providers.eum_social")
    if eum_social is not None:
        eum_social._sns_signing_key.cache_clear()
    yield
    eum_social = sys.modules.get("socialagent.providers.eum_social")
    if eum_social is not None:
        eum_social._sns_signing_key.cache_clear()
