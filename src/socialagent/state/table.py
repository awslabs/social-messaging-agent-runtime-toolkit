# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
DynamoDB-backed key-value state store for SMART Lambda handlers and EventContext.

Table schema:
- ``pk`` (str): E.164 phone number
- ``sk`` (str): state key
- ``data`` (str): JSON-serialized value
- ``ttl`` (int): Unix epoch expiry (DynamoDB TTL attribute)

All operations are no-ops returning ``None`` when ``_state_table_name`` is empty
or set to the dev-mode sentinel (``__socialagent_dev__``).

A ``pk`` that starts with ``_`` belongs to no user: the scheduler and
notification Lambdas keep the firing records of schedules under ``_schedule#…``
and the rate limiter its counters under ``_ratelimit#…``.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from socialagent.config import SocialAgentConfig
from socialagent.constants import DEV_MODE_VALUE


def _expired(item: dict) -> bool:
    """Whether a stored item's ``ttl`` has passed.

    DynamoDB deletes expired items in the background, so one can still be
    returned for days. Its TTL acts only on a Number, so an item with no
    ``ttl``, or one that is not a number (a String, a Boolean), never expires.
    """
    ttl = item.get("ttl")
    return (
        isinstance(ttl, (int, float, Decimal))
        and not isinstance(ttl, bool)
        and ttl < int(time.time())
    )


class SocialAgentState:
    """DynamoDB-backed key-value state store scoped to phone numbers.

    Instantiate with an :class:`~socialagent.config.SocialAgentConfig`. All methods are
    async no-ops when ``config._state_table_name`` is empty or set to the
    dev-mode sentinel (``__socialagent_dev__``).
    """

    def __init__(self, config: SocialAgentConfig) -> None:
        self._table_name = config._state_table_name
        self._region = config.region

    @property
    def table_configured(self) -> bool:
        """Whether ``STATE_TABLE_NAME`` names a table, the dev-mode sentinel included.

        False only on a stack deployed without the state table, where every
        call is a no-op; under ``socialagent dev`` calls are no-ops too, but
        that is the dev stand-in for a table, not a missing one.
        """
        return bool(self._table_name)

    def _table(self):
        """Return the DynamoDB Table resource, or ``None`` when unconfigured.

        Returns ``None`` when the table name is empty or the dev-mode sentinel,
        so dev sessions never hit a real table literally named ``__socialagent_dev__``.
        """
        if not self._table_name or self._table_name == DEV_MODE_VALUE:
            return None
        return boto3.resource("dynamodb", region_name=self._region).Table(
            self._table_name
        )

    async def get(self, phone: str, key: str) -> Any | None:
        """Retrieve a value from the state store.

        Args:
            phone: E.164 phone number (partition key).
            key: State key (sort key).

        Returns:
            The stored value, or ``None`` if not found, its ``ttl`` has passed
            (even when DynamoDB has not deleted it yet), or the table is not
            configured.
        """
        table = self._table()
        if table is None:
            return None

        def _read() -> Any | None:
            item = table.get_item(Key={"pk": phone, "sk": key}).get("Item") or {}
            if _expired(item):
                return None
            # Same table the agent's atomic_increment writes to, whose counter
            # items carry no `data` attribute, so a missing `data` must read as None
            # rather than raise KeyError.
            raw = item.get("data")
            return json.loads(raw) if raw is not None else None

        return await asyncio.to_thread(_read)

    async def put(
        self,
        phone: str,
        key: str,
        data: Any,
        ttl_minutes: int = 60 * 24 * 30,
    ) -> None:
        """Store a value with an optional TTL.

        Args:
            phone: E.164 phone number (partition key).
            key: State key (sort key).
            data: JSON-serializable value.
            ttl_minutes: Time-to-live in minutes. Defaults to 30 days.
        """
        table = self._table()
        if table is None:
            return

        def _write() -> None:
            table.put_item(
                Item={
                    "pk": phone,
                    "sk": key,
                    "data": json.dumps(data),
                    "ttl": int(time.time()) + ttl_minutes * 60,
                }
            )

        await asyncio.to_thread(_write)

    async def delete(self, phone: str, key: str) -> None:
        """Delete a value from the state store.

        Args:
            phone: E.164 phone number (partition key).
            key: State key (sort key).
        """
        table = self._table()
        if table is None:
            return
        await asyncio.to_thread(table.delete_item, Key={"pk": phone, "sk": key})

    async def claim_once(
        self, phone: str, name: str, ttl_minutes: int = 60
    ) -> bool | None:
        """Atomically write a ``claim#{name}`` marker; True only for the first caller.

        The Lambda-side counterpart of ``socialagent_runtime.state.claim_once``,
        with the same item shape. One conditional ``PutItem`` decides the claim:
        a read and then a write would let two concurrent deliveries of one event
        both see it unclaimed and both run it.

        Args:
            phone: Partition key: a phone number, or a ``_``-prefixed key for a
                record that belongs to no user.
            name: The event's name; stored under ``claim#{name}``.
            ttl_minutes: How long the marker is kept. DynamoDB deletes expired
                items lazily, so a marker can outlive this; one past its ``ttl``
                counts as absent, so it does not refuse a new claim.

        Returns:
            True when this call wrote the marker, False when it already
            existed, and ``None`` when no table is configured, so a caller can
            tell "claimed by someone else" from "nothing to claim with".

        Raises:
            botocore.exceptions.ClientError: any DynamoDB error but the failed
                condition.
        """
        table = self._table()
        if table is None:
            return None

        def _claim() -> bool:
            now = int(time.time())
            try:
                table.put_item(
                    Item={
                        "pk": phone,
                        "sk": f"claim#{name}",
                        "data": json.dumps(now),
                        "ttl": now + ttl_minutes * 60,
                    },
                    ConditionExpression=(
                        "attribute_not_exists(sk) OR (attribute_type(#ttl, :n) AND #ttl < :now)"
                    ),
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues={":now": now, ":n": "N"},
                )
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    return False
                raise
            return True

        return await asyncio.to_thread(_claim)
