# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The feedback recipe stores only a valid rating, under its own key, for 30 days.

``rating`` and ``comment`` are filled in by the model, so the tool checks both;
the comment is the user's own words, so it is never logged.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

_RECIPE = Path(__file__).parent.parent / "recipes" / "feedback" / "tool.py"
PHONE = "+15555550100"


@pytest.fixture(scope="module")
def fb():
    spec = importlib.util.spec_from_file_location("_feedback_recipe", _RECIPE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def put(fb):
    with patch.object(fb.state, "put", new_callable=AsyncMock) as mock:
        yield mock


@pytest.mark.asyncio
async def test_a_valid_rating_is_stored_under_its_prefix_for_30_days(fb, put):
    result = await fb.save_rating(PHONE, 8, "  Quick and clear  ")

    assert result == {"status": "saved", "rating": 8}
    put.assert_awaited_once()
    phone, key, record = put.await_args.args
    assert phone == PHONE
    assert key.startswith(fb.RATING_KEY_PREFIX)
    assert record["rating"] == 8
    assert record["comment"] == "Quick and clear"
    assert record["at"]
    assert put.await_args.kwargs["ttl_minutes"] == 30 * 24 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [0, 11, -3, True, False, 7.5, "8", None])
async def test_a_rating_that_is_not_a_whole_number_from_1_to_10_is_refused(
    fb, put, rating
):
    result = await fb.save_rating(PHONE, rating)

    assert "error" in result
    put.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [1, 10])
async def test_the_ends_of_the_range_are_accepted(fb, put, rating):
    assert await fb.save_rating(PHONE, rating) == {"status": "saved", "rating": rating}


@pytest.mark.asyncio
async def test_no_phone_stores_nothing(fb, put):
    assert "error" in await fb.save_rating("", 8)
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_comment_that_is_not_text_is_refused(fb, put):
    assert "error" in await fb.save_rating(PHONE, 8, {"text": "hi"})
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_long_comment_is_cut(fb, put):
    await fb.save_rating(PHONE, 5, "x" * 5000)

    assert len(put.await_args.args[2]["comment"]) == fb.MAX_COMMENT_CHARS


@pytest.mark.asyncio
async def test_neither_the_comment_nor_the_number_reaches_the_log(fb, put, caplog):
    comment = "my order 4471 never came"
    with caplog.at_level(logging.DEBUG):
        await fb.save_rating(PHONE, 3, comment)
        put.side_effect = RuntimeError(f"PutItem failed for {PHONE}: {comment}")
        result = await fb.save_rating(PHONE, 3, comment)

    assert result == {"error": "The rating could not be saved"}
    assert caplog.records, "the tool logged nothing"
    for record in caplog.records:
        text = record.getMessage()
        assert comment not in text
        assert PHONE not in text and PHONE.lstrip("+") not in text


@pytest.mark.asyncio
async def test_saved_ratings_read_back_through_list_items(fb):
    """The record is a dict, which is what ``list_items`` returns, end to end
    through the dev-mode state store."""
    store: dict = {}
    with (
        patch.object(fb.state, "_get_table", return_value=None),
        patch.object(fb.state, "_dev_read", side_effect=lambda: dict(store)),
        patch.object(fb.state, "_dev_write", side_effect=store.update),
    ):
        await fb.save_rating(PHONE, 9, "great")
        await fb.state.put(PHONE, "timezone", "America/Mexico_City")
        ratings = await fb.state.list_items(PHONE, fb.RATING_KEY_PREFIX)

    assert [(r["rating"], r["comment"]) for r in ratings] == [(9, "great")]
