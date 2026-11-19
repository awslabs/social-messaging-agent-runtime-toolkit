# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A deploys read that failed is an error, never an empty history.

``handle_get_deploys`` caught every exception from CodeBuild and answered
``200 []``, so a permissions or throttling error looked like a stack that was
never deployed, and the Deploys tab had no alert to show. A failed read now
answers 502 with the AWS error code as its reason, the way the log routes
report a group they could not read; ``200 []`` is left for the project that
really has no builds. The CodeBuild client is also created in the stack's
region, and a zip stack is read from ``<prefix>-agent-zip-build``, so neither
of those looks like an empty history either (#62).
"""

import json

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CONSOLE_REGION = "us-east-1"
ELSEWHERE = "eu-west-1"

# The console's own Region, so these cases read through the module's client;
# the region case below moves the stack away from it.
STACK = {
    "project": "bot",
    "environment": "dev",
    "region": CONSOLE_REGION,
    "deployment_type": "docker",
    "outputs": {"webhook_lambda": "dev-bot-webhook"},
}
ELSEWHERE_STACK = {**STACK, "region": ELSEWHERE}


class FakeCodeBuild:
    """The two CodeBuild calls the Deploys tab makes, and what each answers."""

    def __init__(self, ids=None, builds=None, fail=None):
        self.ids = [] if ids is None else ids
        self.builds = [] if builds is None else builds
        self.fail = fail or {}
        self.calls = []

    def list_builds_for_project(self, **kw):
        self.calls.append(("list_builds_for_project", kw))
        if "list" in self.fail:
            raise self.fail["list"]
        return {"ids": self.ids}

    def batch_get_builds(self, **kw):
        self.calls.append(("batch_get_builds", kw))
        if "batch" in self.fail:
            raise self.fail["batch"]
        return {"builds": self.builds}


def _client_error(code: str, message: str = "failed") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ListBuildsForProject")


@pytest.fixture
def stacks(monkeypatch):
    """`routes.stacks` with the console's Region pinned, as test_log_region does."""
    from routes import stacks as module

    monkeypatch.setattr(module, "REGION", CONSOLE_REGION)
    monkeypatch.setattr(module, "_get_registry", lambda _id: dict(STACK))
    return module


def _regions(monkeypatch, stacks, fakes: dict) -> list:
    """Pin `client(service, region)` and record every region it was asked for."""
    asked = []

    def client(service, region):
        asked.append((service, region))
        return fakes.setdefault(region, FakeCodeBuild())

    monkeypatch.setattr(stacks, "client", client)
    return asked


def test_a_failed_list_is_a_502_with_the_aws_code(stacks, monkeypatch):
    monkeypatch.setattr(
        stacks,
        "codebuild",
        FakeCodeBuild(fail={"list": _client_error("AccessDeniedException", "not authorized")}),
    )

    resp = stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["reason"] == "AccessDeniedException"
    # The Deploys tab renders `error`, so the code has to be in the sentence too.
    # Another test's event can still be on `app.current_event` when the whole
    # suite runs, and error_response appends its request id then, so the
    # sentence is checked for its start rather than for an exact match.
    assert body["error"].startswith(
        "Could not read the deploys of dev-bot-agent-build from CodeBuild (AccessDeniedException)"
    )
    # boto3's own text names ARNs and the console's role, so it stays in the log.
    assert "not authorized" not in resp["body"]


def test_a_failed_batch_get_is_a_502_too(stacks, monkeypatch):
    monkeypatch.setattr(
        stacks,
        "codebuild",
        FakeCodeBuild(
            ids=["dev-bot-agent-build:abc"],
            fail={"batch": _client_error("ThrottlingException", "Rate exceeded")},
        ),
    )

    resp = stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert resp["statusCode"] == 502
    assert json.loads(resp["body"])["reason"] == "ThrottlingException"


def test_a_connection_error_names_its_class(stacks, monkeypatch):
    monkeypatch.setattr(
        stacks,
        "codebuild",
        FakeCodeBuild(fail={"list": EndpointConnectionError(endpoint_url="https://codebuild")}),
    )

    resp = stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert resp["statusCode"] == 502
    assert json.loads(resp["body"])["reason"] == "EndpointConnectionError"


def test_a_project_with_no_builds_is_still_an_empty_history(stacks, monkeypatch):
    monkeypatch.setattr(stacks, "codebuild", FakeCodeBuild(ids=[]))

    resp = stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == []


def test_a_zip_stack_is_read_from_its_zip_build_project(stacks, monkeypatch):
    fake = FakeCodeBuild(ids=[])
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: {**STACK, "deployment_type": "zip"})
    monkeypatch.setattr(stacks, "codebuild", fake)

    stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert fake.calls[0][1]["projectName"] == "dev-bot-agent-zip-build"


def test_the_stack_region_gets_its_own_codebuild_client(stacks, monkeypatch):
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: dict(ELSEWHERE_STACK))
    # Patching the module client keeps this test offline: if the handler ever fell
    # back to the console's client, it would send a real CodeBuild request first.
    monkeypatch.setattr(stacks, "codebuild", FakeCodeBuild())
    fakes = {}
    asked = _regions(monkeypatch, stacks, fakes)
    fakes[ELSEWHERE] = FakeCodeBuild(
        ids=["dev-bot-agent-build:abc"],
        builds=[
            {
                "id": "dev-bot-agent-build:abc",
                "buildStatus": "SUCCEEDED",
                "sourceVersion": "0123456789abcdef",
            }
        ],
    )

    resp = stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert asked == [("codebuild", ELSEWHERE)]
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])[0]["commit"] == "01234567"


def test_a_stack_in_the_console_region_keeps_the_module_client(stacks, monkeypatch):
    fake = FakeCodeBuild(ids=[])
    monkeypatch.setattr(stacks, "_get_registry", lambda _id: {**STACK, "region": CONSOLE_REGION})
    monkeypatch.setattr(stacks, "codebuild", fake)
    asked = _regions(monkeypatch, stacks, {})

    stacks.handle_get_deploys({"id": "bot-dev"}, {})

    assert asked == []
    assert fake.calls[0][1]["projectName"] == "dev-bot-agent-build"
