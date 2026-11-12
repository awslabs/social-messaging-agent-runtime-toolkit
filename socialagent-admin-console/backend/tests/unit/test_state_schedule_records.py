# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The state views leave out the scheduler's firing records.

With the state table on, the scheduler and notification Lambdas write one
record per scheduled firing under a partition key of its own, ``_schedule#``
and a hash of the schedule id (``FiringRecord`` in
``src/socialagent/handlers/scheduled.py``). Those rows belong to no user, so
neither the stack's State tab nor the global State page lists them or counts
their partition as a user.
"""

import json

import boto3
import pytest
from moto import mock_aws

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides, so import inside each test or fixture.

TABLE = "bot-dev-state"
PHONE = "+15555550100"
SCHEDULE_PK = "_schedule#3f2a9c1d"


@pytest.fixture
def state_routes(monkeypatch):
    from routes import state

    registry = {"region": "us-east-1", "outputs": {"state_table_name": TABLE}}
    monkeypatch.setattr(state, "_get_registry", lambda sid: registry)
    monkeypatch.setattr(state, "_get_all_registries", lambda: {"bot-dev": registry})
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        table.put_item(Item={"pk": PHONE, "sk": "counter#visits", "data": json.dumps(3), "ttl": 0})
        table.put_item(Item={
            "pk": SCHEDULE_PK,
            "sk": "firing#2026-11-04T09:00:00Z",
            "data": json.dumps(True),
            "ttl": 1762419600,
        })
        yield state


def _body(resp) -> list:
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


def test_stack_state_leaves_out_firing_records(state_routes):
    entries = _body(state_routes.handle_get_state({"id": "bot-dev"}, {}))
    assert entries == [{"phone": PHONE, "key": "counter#visits", "value": 3, "ttl": 0}]


def test_global_state_leaves_out_firing_records(state_routes):
    entries = _body(state_routes.handle_list_all_state({}, {}))
    assert entries == [{"phone": PHONE, "key": "counter#visits", "value": 3, "ttl": 0, "stack": "bot-dev"}]
    assert {entry["phone"] for entry in entries} == {PHONE}
