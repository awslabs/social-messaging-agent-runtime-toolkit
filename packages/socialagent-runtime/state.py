# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Key-value state store scoped per-phone, used by agents running inside AgentCore Runtime.

Two modes of operation, selected by ``STATE_TABLE_NAME``:
- Production: DynamoDB-backed. Auto-discovers the table name from the env var.
  Writes include a TTL attribute (default 30 days).
- Dev mode (``STATE_TABLE_NAME=__socialagent_dev__`` or unset): JSON file at
  ``{tempdir}/socialagent-dev-<uid>/socialagent_dev_state.json``, owner-only
  (directory 0700, file 0600). Shared across processes so multiple
  ``socialagent dev`` terminals of the same user see the same state.

All operations are async and keyed by ``(phone, key)``.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
import logging
import time
from typing import Any

import boto3

try:
    from ._devfiles import dev_dir, private_dir, write_private
    from .config import config
    from .constants import DEV_MODE_VALUE
except ImportError:
    from _devfiles import dev_dir, private_dir, write_private  # type: ignore[no-redef]
    from config import config  # type: ignore[no-redef]
    from constants import DEV_MODE_VALUE  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_DEV_STATE_PATH = dev_dir() / "socialagent_dev_state.json"
# Dev mode file storage is intentionally not concurrent-safe.
# socialagent dev is a single-user REPL — concurrent writes cannot occur in normal use.


def _get_table() -> Any | None:
    """Return the DynamoDB state table, or ``None`` in dev mode.

    Reads the table name from ``config.state_table_name``. When that is unset
    or equals ``DEV_MODE_VALUE``, dev-mode file storage is used instead and
    this returns ``None``.
    """
    table_name = config.state_table_name
    if not table_name or table_name == DEV_MODE_VALUE:
        return None
    return boto3.resource("dynamodb").Table(table_name)


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


def _live() -> Any:
    """A query filter that keeps only items ``_expired`` would not reject.

    ``list_keys`` and ``list_items`` use it so they agree with ``get``;
    ``delete_prefix`` does not, so cleanup still removes expired items.
    """
    attr = boto3.dynamodb.conditions.Attr("ttl")
    return attr.not_exists() | ~attr.attribute_type("N") | attr.gte(int(time.time()))


def _dev_read() -> dict:
    """Read the dev-mode state JSON file, returning ``{}`` if absent or corrupt."""
    try:
        # Never read from a directory another user owns: it may hold planted state.
        private_dir(_DEV_STATE_PATH.parent)
        return json.loads(_DEV_STATE_PATH.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {}
    except OSError as exc:
        # A refused dev dir (a symlink, or another user's) reads as empty state;
        # say so, or every get() just returns None.
        logger.warning(
            "Dev state %s not read (%s); treating it as empty",
            _DEV_STATE_PATH,
            type(exc).__name__,
        )
        return {}


def _dev_write(data: dict) -> None:
    """Write the dev-mode state dict to the JSON file, owner-only (0600)."""
    write_private(_DEV_STATE_PATH, json.dumps(data))


async def get(phone: str, key: str) -> Any | None:
    """Read a stored value for ``(phone, key)``.

    Args:
        phone: User phone number (partition key).
        key: State key (sort key).

    Returns:
        The stored value, or ``None`` if no entry exists or its ``ttl`` has
        passed, even when DynamoDB has not deleted it yet.
    """
    table = _get_table()
    if table is None:
        return _dev_read().get(f"{phone}#{key}")

    def _read() -> Any | None:
        item = table.get_item(Key={"pk": phone, "sk": key}).get("Item") or {}
        if _expired(item):
            return None
        # An item can exist without a `data` attribute — atomic_increment writes
        # only pk/sk/n/ttl, and list_keys does not hide the reserved counter#/claim#
        # namespaces from callers. `item["data"]` then raised KeyError where the
        # docstring promises None, and the dev branch above returns the raw stored
        # value, so the same read crashed in prod but "worked" under socialagent dev.
        raw = item.get("data")
        return json.loads(raw) if raw is not None else None

    return await asyncio.to_thread(_read)


async def put(phone: str, key: str, data: Any, ttl_minutes: int = 60 * 24 * 30) -> None:
    """Store a value for ``(phone, key)``.

    Args:
        phone: User phone number (partition key).
        key: State key (sort key).
        data: JSON-serializable value to store.
        ttl_minutes: Expiry in minutes (production only; default 30 days).
    """
    table = _get_table()
    if table is None:
        store = _dev_read()
        store[f"{phone}#{key}"] = data
        _dev_write(store)
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


async def delete(phone: str, key: str) -> None:
    """Delete the stored value for ``(phone, key)`` if it exists.

    Args:
        phone: User phone number (partition key).
        key: State key (sort key).
    """
    table = _get_table()
    if table is None:
        store = _dev_read()
        store.pop(f"{phone}#{key}", None)
        _dev_write(store)
        return
    await asyncio.to_thread(table.delete_item, Key={"pk": phone, "sk": key})


async def list_keys(phone: str) -> list[str]:
    table = _get_table()
    if table is None:
        return [k.split("#", 1)[1] for k in _dev_read() if k.startswith(f"{phone}#")]

    def _query() -> list[str]:
        # Paginate over LastEvaluatedKey: a single DynamoDB query response is
        # capped at 1MB. Without this loop, users with >1MB of items get a
        # silently truncated key list — causing reset_user_memory to leave
        # state behind and any list-based read to miss records.
        keys: list[str] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": boto3.dynamodb.conditions.Key("pk").eq(phone),
            "FilterExpression": _live(),
            "ProjectionExpression": "sk",
        }
        while True:
            resp = table.query(**kwargs)
            keys.extend(item["sk"] for item in resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return keys

    return await asyncio.to_thread(_query)


async def atomic_increment(
    phone: str, name: str, ttl_minutes: int = 60 * 24 * 30
) -> int:
    """Atomically increment a per-phone integer counter; return the new value.

    Stored under sk = f"counter#{name}" so the counter never collides with a
    regular ``put``/``get`` sk for the same phone. The DynamoDB
    ``UpdateExpression: ADD n :one`` is unconditional and atomic, so concurrent
    callers each receive a unique, monotonically-increasing integer with no
    lost updates — even when several Lambdas run the same code path against the
    same phone in the same millisecond. Used by the photo upload path so two
    photos arriving in the same burst never get the same seq (a
    read-modify-write would race).

    The first caller for a given (phone, name) sees ``1``; subsequent calls see
    ``2, 3, ...`` In dev mode the counter lives in the local JSON file (single
    user, no concurrency in practice).
    """
    sk = f"counter#{name}"
    table = _get_table()
    if table is None:
        store = _dev_read()
        k = f"{phone}#{sk}"
        n = int(store.get(k, 0)) + 1
        store[k] = n
        _dev_write(store)
        return n

    def _bump() -> int:
        resp = table.update_item(
            Key={"pk": phone, "sk": sk},
            UpdateExpression="SET #t = :ttl ADD #n :one",
            ExpressionAttributeNames={"#t": "ttl", "#n": "n"},
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": int(time.time()) + ttl_minutes * 60,
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["n"])

    return await asyncio.to_thread(_bump)


async def claim_once(phone: str, name: str, ttl_minutes: int = 60) -> bool:
    """Atomically claim a one-shot marker; return True only for the FIRST caller.

    Used for inbound-event idempotency: WhatsApp message ids are unique, but
    SNS→Lambda delivery is at-least-once, so the same message can be processed
    more than once (redelivery, retry, or two warm containers racing). The first
    caller for a given (phone, name) gets True and should process the event; every
    later caller gets False and must skip it.

    Implemented as a DynamoDB ``put_item`` with an ``attribute_not_exists``
    condition so the claim is decided in a single atomic write — a read-then-write
    would let two concurrent callers both observe "not claimed" and both proceed.
    The marker carries a short TTL so the claim ledger self-cleans (a duplicate
    arriving days later is vanishingly unlikely and not worth retaining state for).
    A marker past its ``ttl`` that DynamoDB has not deleted yet counts as absent,
    so it does not refuse a new claim.

    Stored under sk = f"claim#{name}" so it never collides with regular state.
    In dev mode there is no concurrency, so a plain presence check suffices.
    """
    sk = f"claim#{name}"
    table = _get_table()
    if table is None:
        store = _dev_read()
        k = f"{phone}#{sk}"
        if k in store:
            return False
        store[k] = int(time.time())
        _dev_write(store)
        return True

    def _claim() -> bool:
        from botocore.exceptions import ClientError

        now = int(time.time())
        try:
            table.put_item(
                Item={
                    "pk": phone,
                    "sk": sk,
                    "data": json.dumps(now),
                    "ttl": now + ttl_minutes * 60,
                },
                # An expired marker DynamoDB has not deleted yet counts as absent.
                ConditionExpression=(
                    "attribute_not_exists(sk) OR (attribute_type(#ttl, :n) AND #ttl < :now)"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": now, ":n": "N"},
            )
            return True
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return False
            raise

    return await asyncio.to_thread(_claim)


async def release_claim(phone: str, name: str) -> None:
    """Release a marker taken by :func:`claim_once` so the event can be reclaimed.

    Used when processing fails after the claim is taken but before the work
    completes: releasing lets an at-least-once redelivery be reprocessed instead
    of being suppressed as a duplicate. Keys the same ``claim#{name}`` marker that
    ``claim_once`` writes (callers pass the bare ``name``, not the ``claim#`` sk).
    """
    await delete(phone, f"claim#{name}")


async def compare_and_set(
    phone: str, key: str, expected, factory, ttl_minutes: int = 60 * 24 * 30
):
    """Atomically set ``key`` to ``factory()`` iff its current value == ``expected``.

    Returns the value that ends up stored — ``factory()``'s result if this caller
    won the write, or the other caller's value if it lost. Concurrent callers
    that observed the same ``expected`` therefore CONVERGE on one value instead of
    each writing their own (last-write-wins). ``expected=None`` means "only if the
    key is absent". Used to mint/rotate ``current_operation_id`` from the photo
    path, where two webhook Lambdas in one multi-photo burst would otherwise each
    create a distinct operation_id and split the batch across two operations.

    ``factory`` is a zero-arg side-effect-free callable (it may be invoked even
    when its value loses the race). Stored as a normal item so ``get``/``put``/
    ``delete`` on the same key interoperate.

    CONTRACT: ``expected`` must be a scalar (str/int/None). Prod compares the
    serialized JSON of the stored ``data`` attribute, dev compares the raw value;
    these agree for scalars but NOT for dicts/lists (JSON key order is
    significant in the prod string compare). Don't pass containers as ``expected``.

    Raises:
        TypeError: If ``expected`` is a ``dict`` or ``list`` — enforced here
            because the dev/prod comparison divergence above would otherwise
            fail silently (dev mode "works," prod mode doesn't, or vice versa
            depending on incidental key ordering).
    """
    if isinstance(expected, (dict, list)):
        raise TypeError(
            "compare_and_set: `expected` must be a scalar (str/int/None), not "
            f"{type(expected).__name__}. Dev mode compares the raw value while "
            "prod compares serialized JSON — dicts/lists agree only by "
            "incidental key ordering, so this is disallowed rather than left as "
            "a silent dev/prod divergence."
        )
    table = _get_table()
    if table is None:
        store = _dev_read()
        k = f"{phone}#{key}"
        # Prod maps expected=None to attribute_not_exists(sk) — "write only if the
        # key is ABSENT". Dev must mean the same: a plain `store.get(k) != expected`
        # cannot tell an absent key from one holding a stored None, so a
        # put(phone, key, None) followed by compare_and_set(..., expected=None)
        # would write in dev and no-op in prod. Distinguish the two with a sentinel.
        _absent = object()
        current = store.get(k, _absent)
        if expected is None:
            matched = current is _absent
        else:
            matched = current == expected
        if not matched:
            return None if current is _absent else current
        value = factory()
        store[k] = value
        _dev_write(store)
        return value

    def _cas():
        from botocore.exceptions import ClientError

        value = factory()
        now = int(time.time())
        try:
            # An expired item DynamoDB has not deleted yet is absent: it must not
            # block expected=None, and its stale `data` must not match `expected`.
            if expected is None:
                cond = "attribute_not_exists(sk) OR (attribute_type(#ttl, :n) AND #ttl < :now)"
                names = {"#ttl": "ttl"}
                values = {":now": now, ":n": "N"}
            else:
                cond = (
                    "#d = :expected AND (attribute_not_exists(#ttl)"
                    " OR NOT attribute_type(#ttl, :n) OR #ttl >= :now)"
                )
                names = {"#d": "data", "#ttl": "ttl"}
                values = {":expected": json.dumps(expected), ":now": now, ":n": "N"}
            table.put_item(
                Item={
                    "pk": phone,
                    "sk": key,
                    "data": json.dumps(value),
                    "ttl": now + ttl_minutes * 60,
                },
                ConditionExpression=cond,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return value
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                # Lost the race (or the item exists with no/!= `data`) — return
                # whatever the winner stored, so callers that observed the same
                # `expected` converge on one value.
                #
                # ConsistentRead is required, not optional: DynamoDB only
                # guarantees read-your-writes for strongly consistent reads, so an
                # eventually-consistent read here can miss the winner's write and
                # send two racers off with different ids — the split-batch bug this
                # function exists to prevent.
                won = (
                    table.get_item(
                        Key={"pk": phone, "sk": key}, ConsistentRead=True
                    ).get("Item")
                    or {}
                )
                raw = None if _expired(won) else won.get("data")
                if raw is None:
                    # The condition failed and yet nothing comparable is stored:
                    # the item vanished (TTL, or an earlier delete/delete_prefix),
                    # or a foreign writer holds it without a `data` attribute — an
                    # atomic_increment counter, say. Either way our put did not
                    # happen, so reporting `value` as committed would be a lie: a
                    # rotate-the-id caller would write children under an id nothing
                    # ever stored. None is the truthful answer — nothing is stored —
                    # and lets the caller retry with expected=None if it wants.
                    return None
                return json.loads(raw)
            raise

    return await asyncio.to_thread(_cas)


async def delete_prefix(phone: str, prefix: str) -> int:
    """Delete every item whose sk starts with ``prefix`` for this phone.

    Used to clean up append-only namespaces (e.g. all ``op_photos_item#{op_id}#*``
    items at the end of an ad creation flow). Returns the number of items
    deleted. Idempotent — calling on an empty namespace is a no-op.
    """
    table = _get_table()
    if table is None:
        store = _dev_read()
        match = f"{phone}#{prefix}"
        keys = [k for k in store if k.startswith(match)]
        for k in keys:
            store.pop(k, None)
        _dev_write(store)
        return len(keys)

    def _purge() -> int:
        # Collect sks via paginated query, then batch-delete.
        sks: list[str] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                boto3.dynamodb.conditions.Key("pk").eq(phone)
                & boto3.dynamodb.conditions.Key("sk").begins_with(prefix)
            ),
            "ProjectionExpression": "sk",
        }
        while True:
            resp = table.query(**kwargs)
            sks.extend(item["sk"] for item in resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        with table.batch_writer() as batch:
            for sk in sks:
                batch.delete_item(Key={"pk": phone, "sk": sk})
        return len(sks)

    return await asyncio.to_thread(_purge)


async def list_items(phone: str, prefix: str) -> list[dict]:
    """Return the deserialized data of every item whose sk starts with `prefix`.

    Reuses the existing per-pk query so multi-record reads (e.g. all `ad#*`
    items for one user) cost a single DynamoDB call instead of N get_item calls.
    Like ``get``, it skips an item whose ``ttl`` has passed.
    """
    table = _get_table()
    if table is None:
        store = _dev_read()
        out: list[dict] = []
        for k, v in store.items():
            phone_part, _, key = k.partition("#")
            if phone_part == phone and key.startswith(prefix) and isinstance(v, dict):
                out.append(v)
        return out

    def _query() -> list[dict]:
        # Paginate over LastEvaluatedKey — see list_keys for rationale.
        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                boto3.dynamodb.conditions.Key("pk").eq(phone)
                & boto3.dynamodb.conditions.Key("sk").begins_with(prefix)
            ),
            "FilterExpression": _live(),
        }
        while True:
            resp = table.query(**kwargs)
            for item in resp.get("Items", []):
                data = item.get("data")
                if not data:
                    continue
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    items.append(parsed)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    return await asyncio.to_thread(_query)
