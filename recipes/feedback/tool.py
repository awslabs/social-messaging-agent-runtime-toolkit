# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Feedback - store a 1-10 conversation rating, with an optional comment, in per-user state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from socialagent_runtime import state
from socialagent_runtime.phone import mask_phone

logger = logging.getLogger(__name__)

RATING_KEY_PREFIX = "feedback#"
RATING_TTL_DAYS = 30
MIN_RATING = 1
MAX_RATING = 10
MAX_COMMENT_CHARS = 500


async def save_rating(phone: str, rating: int, comment: str = "") -> dict:
    """Store a conversation rating for ``phone``.

    Take ``phone`` from the tool context's ``invocation_state``, never from a model-filled argument.
    """
    if not phone:
        return {"error": "No phone number available"}

    if not isinstance(rating, int) or isinstance(rating, bool):
        return {
            "error": f"rating must be a whole number from {MIN_RATING} to {MAX_RATING}"
        }
    if not MIN_RATING <= rating <= MAX_RATING:
        return {"error": f"rating must be from {MIN_RATING} to {MAX_RATING}"}

    if not isinstance(comment, str):
        return {"error": "comment must be text"}

    comment = comment.strip()[:MAX_COMMENT_CHARS]

    now = datetime.now(timezone.utc)
    key = f"{RATING_KEY_PREFIX}{now:%Y-%m-%dT%H:%M:%SZ}"
    record = {"rating": rating, "comment": comment, "at": now.isoformat()}
    try:
        await state.put(phone, key, record, ttl_minutes=RATING_TTL_DAYS * 24 * 60)
    except Exception as exc:
        # the error text can quote the comment, which log masking does not hide
        logger.warning(
            "Rating not saved for %s (%s)", mask_phone(phone), type(exc).__name__
        )
        return {"error": "The rating could not be saved"}
    return {"status": "saved", "rating": rating}
