# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""State routes: per-stack state, delete key, cross-stack list."""
from __future__ import annotations

import json
import re

import boto3

from shared.auth import require_group
from shared.aws import REGION
from shared.http import error_response as _error, json_response as _json
from shared.registry import get_registry as _get_registry, get_all_registries as _get_all_registries
from shared.resolver import app, from_legacy

# SMART state tables are always named "<project>-<env>-state". Validate the
# registry-supplied table name against this shape before passing it to
# ddb.Table(), so a tampered/misconfigured registry entry can't redirect
# reads/deletes at an arbitrary DynamoDB table.
_STATE_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]+-state$")


def _is_valid_state_table(name: str) -> bool:
    return bool(name) and len(name) <= 255 and _STATE_TABLE_RE.match(name) is not None


# The scheduler and notification Lambdas keep one firing record per scheduled
# firing in the state table, under a partition key of its own (``_schedule#``
# and a hash of the schedule id, see FiringRecord in
# src/socialagent/handlers/scheduled.py). Those rows belong to no user, so the
# state views leave them out instead of listing and counting them as users.
_SCHEDULE_PK_PREFIX = "_schedule#"


def _is_schedule_record(item: dict) -> bool:
    pk = item.get("pk", "")
    return isinstance(pk, str) and pk.startswith(_SCHEDULE_PK_PREFIX)

def handle_delete_state_key(params, body):
    """Delete a single key from the state table, phone and key from the JSON body.

    Never from the path: the table's pk is the E.164 phone with its ``+`` and its
    sort keys carry ``#``, so in a path both arrive percent-encoded (the resolver
    does not unquote route parameters) and the delete matches no item.
    """
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    table_name = config.get("outputs", {}).get("state_table_name", "")
    if not table_name:
        return _json(400, {"error": "No state table"})
    if not _is_valid_state_table(table_name):
        return _json(400, {"error": "Invalid state table name"})
    phone = body.get("phone")
    key = body.get("key")
    if not isinstance(phone, str) or not isinstance(key, str) or not phone or not key:
        return _json(400, {"error": "phone and key are required"})
    try:
        ddb = boto3.resource("dynamodb", region_name=config.get("region", REGION))
        table = ddb.Table(table_name)
        # DeleteItem succeeds on a key that does not exist, so only the old
        # item it hands back says whether anything was deleted.
        resp = table.delete_item(Key={"pk": phone, "sk": key}, ReturnValues="ALL_OLD")
        return _json(200, {"deleted": "Attributes" in resp, "phone": phone, "key": key})
    except Exception:
        return _error(500, "Delete state key failed")


def handle_get_state(params, _body):
    """Scan the DynamoDB state table for a stack and return all key-value pairs grouped by phone."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    table_name = config.get("outputs", {}).get("state_table_name", "")
    if not table_name:
        return _json(200, [])
    if not _is_valid_state_table(table_name):
        return _json(400, {"error": "Invalid state table name"})
    try:
        ddb = boto3.resource("dynamodb", region_name=config.get("region", REGION))
        table = ddb.Table(table_name)
        result = []
        kwargs: dict = {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                if _is_schedule_record(item):
                    continue
                raw = item.get("data", "")
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    value = raw
                result.append({
                    "phone": item.get("pk", ""),
                    "key": item.get("sk", ""),
                    "value": value,
                    "ttl": int(item.get("ttl", 0)),
                })
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
            if len(result) >= 2000:  # safety cap to avoid Lambda timeout
                break
        return _json(200, result)
    except Exception:
        return _error(500, "State scan failed")


def handle_list_all_state(_params, _body):
    """List state entries across all stacks."""
    registries = _get_all_registries()
    all_entries = []
    for sid, cfg in registries.items():
        table_name = cfg.get("outputs", {}).get("state_table_name", "")
        if not table_name or not _is_valid_state_table(table_name):
            continue
        try:
            ddb = boto3.resource("dynamodb", region_name=cfg.get("region", REGION))
            table = ddb.Table(table_name)
            kwargs: dict = {}
            while True:
                resp = table.scan(**kwargs)
                for item in resp.get("Items", []):
                    if _is_schedule_record(item):
                        continue
                    raw = item.get("data", "")
                    try:
                        value = json.loads(raw) if isinstance(raw, str) else raw
                    except (json.JSONDecodeError, TypeError):
                        value = raw
                    all_entries.append({
                        "phone": item.get("pk", ""),
                        "key": item.get("sk", ""),
                        "value": value,
                        "ttl": int(item.get("ttl", 0)),
                        "stack": sid,
                    })
                lek = resp.get("LastEvaluatedKey")
                if not lek or len(all_entries) >= 2000:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except Exception:
            continue
    return _json(200, all_entries)



@app.get("/api/stacks/<id>/state")
@require_group("operator")
def get_state(id: str):
    return from_legacy(handle_get_state({"id": id}, {}))


@app.post("/api/stacks/<id>/state/delete")
@require_group("admin")
def delete_state_key(id: str):
    """Delete a state key with phone+key in the JSON body (safe for + and #)."""
    from shared.resolver import current_body
    body = current_body()
    return from_legacy(handle_delete_state_key({"id": id}, body))


@app.get("/api/state")
@require_group("operator")
def list_all_state():
    return from_legacy(handle_list_all_state({}, {}))
