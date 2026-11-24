# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An item whose ``ttl`` has passed is absent, even before DynamoDB deletes it.

DynamoDB removes expired items in the background, usually within a few days.
Until it does, the item is still in the table, so ``get`` used to return it, and
an expired ``claim#`` marker or value refused ``claim_once`` and
``compare_and_set(expected=None)`` through their ``attribute_not_exists(sk)``
condition. These tests run both layers against moto, which, like DynamoDB before
its sweep, keeps an item whose ``ttl`` is in the past.
"""

from __future__ import annotations

import json
from decimal import Decimal
import sys
import time
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from socialagent.config import SocialAgentConfig
from socialagent.state.table import SocialAgentState

_AGENT_PATH = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

import state as agent_state  # noqa: E402

TABLE = "dev-bot-state"
PHONE = "+15555550100"


@pytest.fixture
def table(monkeypatch):
    """A state table with the stack's key schema, in moto."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        yield resource.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.fixture
def lambda_state(table):
    """The Lambda layer's state store, on the moto table."""
    config = SocialAgentConfig(
        agent_runtime_arn="arn:aws:bedrock-agentcore:::runtime/x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-id",
        region="us-east-1",
        _state_table_name=TABLE,
    )
    return SocialAgentState(config)


@pytest.fixture
def agent(table):
    """The agent runtime's ``state`` module, on the moto table."""
    with patch.object(agent_state, "_get_table", return_value=table):
        yield agent_state


def _set_ttl(table, sk: str, ttl: int) -> None:
    table.update_item(
        Key={"pk": PHONE, "sk": sk},
        UpdateExpression="SET #t = :ttl",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":ttl": ttl},
    )


def _expire(table, sk: str) -> None:
    """Move an item's ``ttl`` into the past, leaving it in the table."""
    _set_ttl(table, sk, int(time.time()) - 60)


def _stored(table, sk: str) -> dict:
    return table.get_item(Key={"pk": PHONE, "sk": sk}, ConsistentRead=True)["Item"]


# ── the expiry test reads the same whole second as the conditions ─────────────


@pytest.mark.parametrize("module", [agent_state, "socialagent.state.table"])
def test_an_item_expires_only_after_its_ttl_second(module):
    """The conditional writes compare ``ttl`` with ``int(time.time())``, so the
    helper must too: at a ``ttl`` equal to the current whole second, ``get`` and
    ``claim_once`` must agree that the item is still live."""
    import importlib

    mod = importlib.import_module(module) if isinstance(module, str) else module
    with patch.object(mod.time, "time", return_value=1000.7):
        assert not mod._expired({"ttl": 1000})
        assert mod._expired({"ttl": 999})
        assert not mod._expired({})


# ── get ───────────────────────────────────────────────────────────────────────


async def test_lambda_get_returns_a_live_value(table, lambda_state):
    await lambda_state.put(PHONE, "k", {"v": 1})
    assert await lambda_state.get(PHONE, "k") == {"v": 1}


async def test_lambda_get_treats_an_expired_item_as_absent(table, lambda_state):
    await lambda_state.put(PHONE, "k", {"v": 1})
    _expire(table, "k")
    assert "Item" in table.get_item(Key={"pk": PHONE, "sk": "k"})  # not yet swept
    assert await lambda_state.get(PHONE, "k") is None


async def test_lambda_get_returns_an_item_that_has_no_ttl(table, lambda_state):
    table.put_item(Item={"pk": PHONE, "sk": "k", "data": json.dumps("v")})
    assert await lambda_state.get(PHONE, "k") == "v"


async def test_agent_get_returns_a_live_value(table, agent):
    await agent.put(PHONE, "k", {"v": 1})
    assert await agent.get(PHONE, "k") == {"v": 1}


async def test_agent_get_treats_an_expired_item_as_absent(table, agent):
    await agent.put(PHONE, "k", {"v": 1})
    _expire(table, "k")
    assert "Item" in table.get_item(Key={"pk": PHONE, "sk": "k"})  # not yet swept
    assert await agent.get(PHONE, "k") is None


async def test_agent_get_returns_an_item_that_has_no_ttl(table, agent):
    table.put_item(Item={"pk": PHONE, "sk": "k", "data": json.dumps("v")})
    assert await agent.get(PHONE, "k") == "v"


# ── claim_once ────────────────────────────────────────────────────────────────


async def test_lambda_claim_once_is_refused_by_a_live_marker(table, lambda_state):
    assert await lambda_state.claim_once(PHONE, "evt") is True
    assert await lambda_state.claim_once(PHONE, "evt") is False


async def test_lambda_claim_once_takes_over_an_expired_marker(table, lambda_state):
    assert await lambda_state.claim_once(PHONE, "evt") is True
    _expire(table, "claim#evt")

    assert await lambda_state.claim_once(PHONE, "evt", ttl_minutes=10) is True

    assert _stored(table, "claim#evt")["ttl"] > time.time() + 9 * 60
    assert await lambda_state.claim_once(PHONE, "evt") is False


async def test_agent_claim_once_is_refused_by_a_live_marker(table, agent):
    assert await agent.claim_once(PHONE, "evt") is True
    assert await agent.claim_once(PHONE, "evt") is False


async def test_agent_claim_once_takes_over_an_expired_marker(table, agent):
    assert await agent.claim_once(PHONE, "evt") is True
    _expire(table, "claim#evt")

    assert await agent.claim_once(PHONE, "evt", ttl_minutes=10) is True

    assert _stored(table, "claim#evt")["ttl"] > time.time() + 9 * 60
    assert await agent.claim_once(PHONE, "evt") is False


# ── compare_and_set (agent side only) ─────────────────────────────────────────


async def test_cas_expected_none_is_refused_by_a_live_value(table, agent):
    await agent.put(PHONE, "k", "old")

    got = await agent.compare_and_set(PHONE, "k", None, lambda: "new")

    assert got == "old"
    assert json.loads(_stored(table, "k")["data"]) == "old"


async def test_cas_expected_none_takes_over_an_expired_value(table, agent):
    await agent.put(PHONE, "k", "old")
    _expire(table, "k")

    got = await agent.compare_and_set(PHONE, "k", None, lambda: "new")

    assert got == "new"
    stored = _stored(table, "k")
    assert json.loads(stored["data"]) == "new"
    assert stored["ttl"] > time.time()


async def test_cas_matches_a_live_value(table, agent):
    await agent.put(PHONE, "k", "old")

    got = await agent.compare_and_set(PHONE, "k", "old", lambda: "new")

    assert got == "new"
    assert json.loads(_stored(table, "k")["data"]) == "new"


async def test_cas_does_not_match_an_expired_value(table, agent):
    """The item is gone as far as callers can tell, so ``expected`` describes
    nothing: nothing is written and nothing comparable is reported stored."""
    await agent.put(PHONE, "k", "old")
    _expire(table, "k")
    stale_ttl = _stored(table, "k")["ttl"]

    got = await agent.compare_and_set(PHONE, "k", "old", lambda: "new")

    assert got is None
    stored = _stored(table, "k")
    assert json.loads(stored["data"]) == "old"
    assert stored["ttl"] == stale_ttl


async def test_cas_loser_of_a_race_reads_the_winners_live_value(table, agent):
    await agent.put(PHONE, "k", "winner")

    got = await agent.compare_and_set(PHONE, "k", "stale-view", lambda: "loser")

    assert got == "winner"


async def test_cas_keeps_an_item_that_has_no_ttl_live(table, agent):
    table.put_item(Item={"pk": PHONE, "sk": "k", "data": json.dumps("old")})

    got = await agent.compare_and_set(PHONE, "k", "old", lambda: "new")

    assert got == "new"


# ── list_keys / list_items hide an expired item; delete_prefix still finds it ─


async def test_list_keys_skips_an_expired_item(table, agent):
    """``get`` returns ``None`` for an expired key, so ``list_keys`` must not
    list it either; a live key and a key with no ``ttl`` are still listed."""
    await agent.put(PHONE, "live", "a")
    await agent.put(PHONE, "gone", "b")
    _expire(table, "gone")
    table.put_item(Item={"pk": PHONE, "sk": "forever", "data": json.dumps("c")})

    assert sorted(await agent.list_keys(PHONE)) == ["forever", "live"]


async def test_list_items_skips_an_expired_item(table, agent):
    await agent.put(PHONE, "photo#1", {"n": 1})
    await agent.put(PHONE, "photo#2", {"n": 2})
    _expire(table, "photo#2")
    table.put_item(Item={"pk": PHONE, "sk": "photo#3", "data": json.dumps({"n": 3})})

    items = await agent.list_items(PHONE, "photo#")

    assert sorted(i["n"] for i in items) == [1, 3]


async def test_delete_prefix_still_deletes_an_expired_item(table, agent):
    """Cleanup must not leave an expired item behind for the sweep."""
    await agent.put(PHONE, "photo#1", {"n": 1})
    await agent.put(PHONE, "photo#2", {"n": 2})
    _expire(table, "photo#2")

    assert await agent.delete_prefix(PHONE, "photo#") == 2

    assert "Item" not in table.get_item(Key={"pk": PHONE, "sk": "photo#2"})


# ── only a number counts as a ttl (#218) ──────────────────────────────────────
# DynamoDB's TTL deletes an item only when its ttl attribute is a Number, so an
# item whose ttl is a String or a Boolean is never swept. It must read as live
# everywhere, not raise in get or vanish from the lists.


@pytest.mark.parametrize("module", [agent_state, "socialagent.state.table"])
@pytest.mark.parametrize(
    "ttl, expired",
    [
        ("123", False),
        (True, False),
        (False, False),
        (Decimal(999), True),
        (999.5, True),
        (Decimal(1001), False),
    ],
    ids=["string", "true", "false", "decimal-past", "float-past", "decimal-live"],
)
def test_only_a_numeric_ttl_can_expire(module, ttl, expired):
    import importlib

    mod = importlib.import_module(module) if isinstance(module, str) else module
    with patch.object(mod.time, "time", return_value=1000.7):
        assert mod._expired({"ttl": ttl}) is expired


def _store_with_ttl(table, sk: str, ttl) -> None:
    table.put_item(Item={"pk": PHONE, "sk": sk, "data": json.dumps("v"), "ttl": ttl})


@pytest.mark.parametrize("ttl", ["1", True], ids=["string", "boolean"])
async def test_an_item_with_a_non_numeric_ttl_reads_as_live(
    table, agent, lambda_state, ttl
):
    _store_with_ttl(table, "k", ttl)
    assert await agent.get(PHONE, "k") == "v"
    assert await lambda_state.get(PHONE, "k") == "v"


@pytest.mark.parametrize("ttl", ["1", True], ids=["string", "boolean"])
async def test_the_lists_keep_an_item_with_a_non_numeric_ttl(table, agent, ttl):
    table.put_item(
        Item={"pk": PHONE, "sk": "photo#1", "data": json.dumps({"n": 1}), "ttl": ttl}
    )
    assert await agent.list_keys(PHONE) == ["photo#1"]
    assert await agent.list_items(PHONE, "photo#") == [{"n": 1}]


@pytest.mark.parametrize("ttl", ["1", True], ids=["string", "boolean"])
async def test_cas_matches_a_value_with_a_non_numeric_ttl(table, agent, ttl):
    """``get`` returns the value, so ``expected`` must match it."""
    _store_with_ttl(table, "k", ttl)
    assert await agent.compare_and_set(PHONE, "k", "v", lambda: "new") == "new"


@pytest.mark.parametrize("ttl", ["1", True], ids=["string", "boolean"])
async def test_a_marker_with_a_non_numeric_ttl_still_refuses(
    table, agent, lambda_state, ttl
):
    """A marker that never expires keeps refusing, on both layers."""
    _store_with_ttl(table, "claim#evt", ttl)
    assert await agent.claim_once(PHONE, "evt") is False
    assert await lambda_state.claim_once(PHONE, "evt") is False


@pytest.mark.parametrize("ttl", ["1", True], ids=["string", "boolean"])
async def test_cas_expected_none_keeps_a_value_with_a_non_numeric_ttl(
    table, agent, ttl
):
    """The condition refuses, and the re-read reports the stored value as live:
    nothing is written and the item stays as it was."""
    _store_with_ttl(table, "k", ttl)

    assert await agent.compare_and_set(PHONE, "k", None, lambda: "new") == "v"

    stored = _stored(table, "k")
    assert json.loads(stored["data"]) == "v"
    assert stored["ttl"] == ttl
