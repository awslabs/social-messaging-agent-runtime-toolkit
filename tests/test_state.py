# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for SocialAgentState and EventContext.state wiring."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialagent.app import SocialAgentApp
from socialagent.config import SocialAgentConfig
from socialagent.handlers.event import EventContext
from socialagent.state.table import SocialAgentState


def _config(table_name: str = "") -> SocialAgentConfig:
    """Return a minimal SocialAgentConfig."""
    return SocialAgentConfig(
        agent_runtime_arn="arn:aws:bedrock-agentcore:::runtime/x",
        memory_id="mem-x",
        provider="eum_social",
        phone_number_id="ph-id",
        _state_table_name=table_name,
    )


# ── No-op when table not configured ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_when_no_table():
    """get() returns None when _state_table_name is empty."""
    state = SocialAgentState(_config())
    assert await state.get("+521234567890", "key") is None


@pytest.mark.asyncio
async def test_put_noop_when_no_table():
    """put() is a no-op when _state_table_name is empty."""
    state = SocialAgentState(_config())
    await state.put("+521234567890", "key", {"x": 1})  # should not raise


@pytest.mark.asyncio
async def test_delete_noop_when_no_table():
    """delete() is a no-op when _state_table_name is empty."""
    state = SocialAgentState(_config())
    await state.delete("+521234567890", "key")  # should not raise


@pytest.mark.asyncio
async def test_dev_sentinel_table_is_noop():
    """The dev-mode sentinel table name behaves as 'no table' (never hits AWS)."""
    from socialagent.constants import DEV_MODE_VALUE

    state = SocialAgentState(_config(DEV_MODE_VALUE))
    with patch("socialagent.state.table.boto3.resource") as mock_resource:
        assert await state.get("+521234567890", "key") is None
        await state.put("+521234567890", "key", {"x": 1})
        await state.delete("+521234567890", "key")
    mock_resource.assert_not_called()


# ── DynamoDB operations ────────────────────────────────────────────────────────


@pytest.fixture
def mock_table():
    """Mock DynamoDB Table resource."""
    table = MagicMock()
    with patch("socialagent.state.table.boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.mark.asyncio
async def test_get_returns_value(mock_table):
    """get() deserializes and returns stored JSON value."""
    mock_table.get_item.return_value = {"Item": {"data": json.dumps({"step": 3})}}
    state = SocialAgentState(_config("my-table"))
    result = await state.get("+521234567890", "onboarding")
    assert result == {"step": 3}
    mock_table.get_item.assert_called_once_with(
        Key={"pk": "+521234567890", "sk": "onboarding"}
    )


@pytest.mark.asyncio
async def test_get_returns_none_when_item_missing(mock_table):
    """get() returns None when item does not exist."""
    mock_table.get_item.return_value = {}
    state = SocialAgentState(_config("my-table"))
    assert await state.get("+521234567890", "missing") is None


@pytest.mark.asyncio
async def test_put_writes_json_with_ttl(mock_table):
    """put() serializes data to JSON and sets TTL."""
    state = SocialAgentState(_config("my-table"))
    await state.put("+521234567890", "todo", ["item1"], ttl_minutes=60)

    call_kwargs = mock_table.put_item.call_args.kwargs["Item"]
    assert call_kwargs["pk"] == "+521234567890"
    assert call_kwargs["sk"] == "todo"
    assert json.loads(call_kwargs["data"]) == ["item1"]
    assert isinstance(call_kwargs["ttl"], int)


@pytest.mark.asyncio
async def test_put_default_ttl_is_30_days(mock_table):
    """put() default TTL is 30 days (43200 minutes)."""
    state = SocialAgentState(_config("my-table"))
    before = int(time.time())
    await state.put("+521234567890", "k", "v")
    after = int(time.time())

    ttl = mock_table.put_item.call_args.kwargs["Item"]["ttl"]
    expected = 60 * 60 * 24 * 30  # 30 days in seconds
    assert before + expected <= ttl <= after + expected + 2


@pytest.mark.asyncio
async def test_delete_calls_dynamodb(mock_table):
    """delete() calls DynamoDB delete_item with correct keys."""
    state = SocialAgentState(_config("my-table"))
    await state.delete("+521234567890", "todo")
    mock_table.delete_item.assert_called_once_with(
        Key={"pk": "+521234567890", "sk": "todo"}
    )


# ── claim_once ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "__socialagent_dev__"])
async def test_claim_once_is_none_without_a_table(name):
    """No table: None, so a caller can tell it from a claim someone else holds."""
    with patch("socialagent.state.table.boto3.resource") as resource:
        assert await SocialAgentState(_config(name)).claim_once("+52", "x") is None
    resource.assert_not_called()


def test_table_configured_counts_the_dev_sentinel():
    assert SocialAgentState(_config("my-table")).table_configured
    assert SocialAgentState(_config("__socialagent_dev__")).table_configured
    assert not SocialAgentState(_config("")).table_configured


@pytest.mark.asyncio
async def test_claim_once_is_one_conditional_write(mock_table):
    """One conditional PutItem and no read: a read-then-write would let two
    concurrent deliveries both see the event unclaimed."""
    state = SocialAgentState(_config("my-table"))
    assert await state.claim_once("+521234567890", "evt-1", ttl_minutes=10) is True
    mock_table.get_item.assert_not_called()
    kwargs = mock_table.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == (
        "attribute_not_exists(sk) OR (attribute_type(#ttl, :n) AND #ttl < :now)"
    )
    assert kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}
    assert kwargs["ExpressionAttributeValues"][":n"] == "N"
    assert int(time.time()) - 5 <= kwargs["ExpressionAttributeValues"][":now"]
    item = kwargs["Item"]
    assert (item["pk"], item["sk"]) == ("+521234567890", "claim#evt-1")
    assert item["ttl"] >= int(time.time()) + 9 * 60


@pytest.mark.asyncio
async def test_claim_once_is_false_for_the_second_caller(mock_table):
    from botocore.exceptions import ClientError

    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
    )
    state = SocialAgentState(_config("my-table"))
    assert await state.claim_once("+521234567890", "evt-1") is False


@pytest.mark.asyncio
async def test_claim_once_raises_any_other_error(mock_table):
    from botocore.exceptions import ClientError

    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
    )
    state = SocialAgentState(_config("my-table"))
    with pytest.raises(ClientError):
        await state.claim_once("+521234567890", "evt-1")


# ── EventContext.state wiring ──────────────────────────────────────────────────


def test_event_context_exposes_state():
    """EventContext.state is the SocialAgentState instance passed in."""
    provider = AsyncMock()
    memory_writer = AsyncMock()
    state = SocialAgentState(_config("my-table"))
    ctx = EventContext(provider, memory_writer, None, state=state)
    assert ctx.state is state


def test_event_context_state_none_by_default():
    """EventContext.state is None when not provided."""
    ctx = EventContext(AsyncMock(), AsyncMock(), None)
    assert ctx.state is None


def test_socialagent_app_creates_state_instance():
    """SocialAgentApp.state is an SocialAgentState instance."""
    with patch("socialagent.app.EUMSocialProvider"):
        app = SocialAgentApp(_config("my-table"))
    assert isinstance(app.state, SocialAgentState)
