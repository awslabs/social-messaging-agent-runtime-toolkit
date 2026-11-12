# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stack-level routes: list, detail, metrics, config, errors, deploys."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import boto3
from aws_lambda_powertools import Logger, Tracer
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import require_group
from shared.aws import s3, cw, logs, codebuild, lambda_client, settings_table, REGION
from shared.clients import client
from shared.config import REGISTRY_BUCKET
from shared.http import error_response, failure_response, json_response as _json
from shared.registry import get_registry as _get_registry, list_registry as _list_registry, _registry_key, _registry_prefix_for
from shared.resolver import app, current_body, from_legacy

tracer = Tracer(service="admin-console-backend")
logger = Logger(service="admin-console-backend", child=True)


def _lambda_suffixes(config: dict) -> list[str]:
    """The stack's Lambdas, by function-name suffix.

    The scheduler Lambda is opt-in (``features.scheduler_lambda`` in the project
    config, published in the registry as ``features.scheduler``) and the default
    deploy has none. Probing it anyway failed on the missing function and marked
    every default stack degraded.
    """
    suffixes = ["webhook", "notification", "event"]
    if (config.get("features") or {}).get("scheduler"):
        suffixes.append("scheduler")
    return suffixes


def _deploys_builder(config: dict, prefix: str):
    """The CodeBuild client and project that build this stack's package.

    The client is created in the stack's region, which may not be the
    console's (as ``_logs_reader`` does for logs), so a stack deployed
    elsewhere is not read as never deployed. A zip stack builds
    ``<prefix>-agent-zip-build`` where a docker stack builds
    ``<prefix>-agent-build`` (#62).
    """
    region = config.get("region") or REGION
    builder = codebuild if region == REGION else client("codebuild", region)
    if config.get("deployment_type") == "zip":
        return builder, f"{prefix}-agent-zip-build"
    return builder, f"{prefix}-agent-build"


def _deploys_reason(exc: Exception) -> str:
    """The AWS error code, as the log routes' ``unreadable`` reports theirs."""
    if isinstance(exc, ClientError):
        return (exc.response.get("Error") or {}).get("Code") or "CodeBuildError"
    return type(exc).__name__


def handle_get_deploys(params, _body):
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    builder, project_name = _deploys_builder(config, prefix)
    try:
        resp = builder.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")
    except (ClientError, BotoCoreError) as exc:
        return _deploys_unreadable(exc, project_name)
    build_ids = resp.get("ids", [])[:20]
    if not build_ids:
        return _json(200, [])
    try:
        builds = builder.batch_get_builds(ids=build_ids)
    except (ClientError, BotoCoreError) as exc:
        return _deploys_unreadable(exc, project_name)
    deploys = []
    for b in builds.get("builds", []):
        deploys.append({
            "deployedAt": str(b.get("endTime", b.get("startTime", ""))),
            "commit": b.get("sourceVersion", "")[:8],
            "triggeredBy": b.get("initiator", "unknown"),
            "duration": str(timedelta(seconds=int((b.get("endTime", datetime.now(UTC)) - b.get("startTime", datetime.now(UTC))).total_seconds()))) if b.get("endTime") and b.get("startTime") else "—",
            "status": b.get("buildStatus", "UNKNOWN").lower().replace("_", "-"),
            "buildId": b.get("id", ""),
        })
    return _json(200, deploys)


def _deploys_unreadable(exc: Exception, project_name: str) -> dict:
    """A deploys read that failed, said out loud.

    This used to answer ``200 []`` for every exception, so a permissions or
    throttling error on the CodeBuild calls looked like a stack that was never
    deployed (#62). A failure now answers 502 with the AWS error code as its
    reason, the way the log routes report an unreadable group, and names that
    code in the message too: the Deploys tab shows the body's ``error``, so the
    operator reads ``AccessDeniedException`` there instead of a generic failure.
    The exception's own text stays in the log, since boto3 names ARNs and the
    console's role.
    """
    reason = _deploys_reason(exc)
    return error_response(
        502,
        f"Could not read the deploys of {project_name} from CodeBuild ({reason})",
        reason=reason,
    )


# The Errors tab reads its stack's Lambdas in parallel within one budget: at
# most this many filter_log_events calls in all, none started after this many
# seconds, so it normally answers inside the page's 10 s timeout. A call under
# way at the deadline is not cut short, so a slow one can still run past it.
_ERRORS_TAB_CALLS = 40
_ERRORS_TAB_SECONDS = 8


def _logs_reader(config: dict):
    """The CloudWatch Logs client for a stack's region, which may not be the console's."""
    region = config.get("region") or REGION
    return logs if region == REGION else client("logs", region)


def handle_get_errors(params, _body):
    """Get recent error log entries from all Lambda functions in a stack."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    suffixes = _lambda_suffixes(config)
    shares = _call_shares(_ERRORS_TAB_CALLS, len(suffixes))
    budget = _CallBudget(_ERRORS_TAB_CALLS, deadline=time.monotonic() + _ERRORS_TAB_SECONDS)
    reader = _logs_reader(config)

    def lambda_errors(suffix: str, calls: int) -> LogRead:
        log_group = f"/aws/lambda/{prefix}-{suffix}"
        try:
            read = _newest_log_events(
                log_group, "?ERROR ?Traceback ?Exception", hours=24,
                client=reader, budget=budget.share(calls),
            )
            rows = [
                {
                    "lambda": suffix,
                    "timestamp": str(datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC)),
                    "message": msg[:500],
                }
                for event in read.events
                if (msg := event.get("message", "").strip()) and "REPORT" not in msg and "START" not in msg
            ]
        except Exception as e:
            # _newest_log_events reports a failed call itself; this is a fault
            # in the code around it, so it keeps its traceback.
            logger.exception("errors_tab_read_failed", extra={"log_group": log_group})
            return LogRead([], type(e).__name__)
        return LogRead(rows, read.reason)

    errors, unreadable, read_any = [], [], False
    with ThreadPoolExecutor(max_workers=len(suffixes)) as pool:
        for suffix, read in zip(suffixes, pool.map(lambda_errors, suffixes, shares)):
            errors.extend(read.events)
            read_any = read_any or read.read_something()
            if read.reason:
                unreadable.append({"lambda": suffix, "reason": read.reason})
    errors.sort(key=lambda e: e["timestamp"], reverse=True)
    return _log_read_response(errors[:30], unreadable, read_any)


# How far back each step of the search reaches: the last 15 minutes, then the
# rest of the last hour, 6 hours and 24 hours. Without startFromHead,
# filter_log_events returns events oldest first, so the newest lines are found
# by reading back from now, not by reading a 24-hour scan from its start.
_ERROR_WINDOWS_HOURS = (0.25, 1, 6, 24)
# A range whose page comes back full, with more to read, is halved while each
# half still spans a second. The call budget below already bounds how deep
# that goes, so the floor only stops a halving that could no longer help.
_MIN_SPLIT_MS = 1_000
_PAGE_LIMIT = 100
# filter_log_events calls one search makes at most, and the seconds after
# which it starts no more, so a busy log group cannot keep a request reading
# until the backend's timeout. A request that reads several groups shares one
# budget out among them.
_MAX_CALLS_PER_SEARCH = 20
_SEARCH_SECONDS = 20


class _CallBudget:
    """filter_log_events calls a request may still start, and until when.

    A request that reads several log groups at once gives each group's search
    a ``share``, so the request's total holds whatever each search does: a
    count kept per search let every group take its own share on top of the
    others. ``take`` checks and spends a call under one lock, since the
    groups are read from several threads and a check-then-decrement without
    it can grant more calls than the budget holds. A share has no deadline of
    its own; the request's applies to it.
    """

    def __init__(self, calls: int, deadline: float | None = None, parent: _CallBudget | None = None):
        self._lock = threading.Lock()
        self._left = calls
        self._deadline = deadline  # a time.monotonic() value
        self._parent = parent
        self.taken = 0

    def _open(self) -> bool:
        return self._left > 0 and (self._deadline is None or time.monotonic() < self._deadline)

    def has_left(self) -> bool:
        """Whether a call could start now. Another thread may take it first."""
        with self._lock:
            if not self._open():
                return False
        return self._parent is None or self._parent.has_left()

    def take(self) -> bool:
        """Spend one call, here and in every budget above; False if any is spent or past its deadline."""
        with self._lock:
            if not self._open() or (self._parent is not None and not self._parent.take()):
                return False
            self._left -= 1
            self.taken += 1
            return True

    def share(self, calls: int) -> _CallBudget:
        """At most ``calls`` of this budget, for one log group's search."""
        return _CallBudget(calls, parent=self)


def _call_shares(total: int, groups: int) -> list[int]:
    """Each group's share of a request's ``total`` calls, in the groups' order,
    so one busy group cannot take the calls the others need.

    Up to ``total`` groups the shares add up to ``total``, unless 20 a group
    (``_MAX_CALLS_PER_SEARCH``) is less. An even split rounded down left up to
    23 of 48 calls unused between 25 and 48 groups, where each group got one
    call: it could not follow a partial page, and on the window-by-window
    path read only its last 15 minutes. So the first ``total % groups``
    groups get one call more, the same groups every time. A share stays its
    group's own: a quiet group that finishes early leaves the rest unused, so
    what each group reads does not depend on which thread finishes first.
    Every group gets at least one call, the least a search makes, so past
    ``total`` groups the shares add up to more than ``total``: a caller reads
    no more groups than that, and the request's budget refuses the rest.
    """
    base, extra = divmod(total, max(groups, 1))
    return [max(1, min(_MAX_CALLS_PER_SEARCH, base + (i < extra))) for i in range(groups)]


def _reads_newest_first(reader) -> bool:
    """True when ``reader`` can ask filter_log_events for the newest events first.

    ``startFromHead`` is in botocore 1.43.33 and later, which requirements.txt
    pins into the package. An older botocore, such as the Lambda runtime may
    include, refuses it before sending. So the window-by-window path, which
    can miss the last minutes of a busy group, runs only on a botocore older
    than the pin, which no package built from requirements.txt contains.
    """
    try:
        shape = reader.meta.service_model.operation_model("FilterLogEvents").input_shape
        return "startFromHead" in shape.members
    except Exception:
        return False


# The reason a log group read stops short when the request's calls or time
# run out first, rather than because a call failed.
UNREAD_BUDGET = "budget"


class LogRead(NamedTuple):
    """What one log group's search read, oldest first, and why it stopped
    short if it did.

    ``reason`` is None when the search read what it needed: ``keep`` events,
    or the whole window. Otherwise it is the error code of the call that
    failed (the error's type when it has no code), or ``UNREAD_BUDGET``. The
    routes answered a bare list, which an empty read and a failed one both
    gave, so the console showed "No errors" for logs it never read. A tuple
    makes every caller decide what to do with the reason.
    """

    events: list[dict]
    reason: str | None = None

    def read_something(self) -> bool:
        """Whether the group was read in full, or gave events before it stopped."""
        return self.reason is None or bool(self.events)


def _log_read_failure(exc: Exception, log_group: str) -> str | None:
    """Log *exc*, a failed filter_log_events call on ``log_group``, and return
    why the group is unread: None for a group that does not exist yet.

    Call from an ``except`` block. A Lambda's log group appears with its first
    invocation, so a group that does not exist yet is no events and is not
    logged. A CloudWatch Logs error message can quote the filter pattern, which
    is what the operator searched for, so a botocore error is a warning with
    its error type and code, without the message or a traceback, and the
    reason is the code alone. Anything else, a fault in this code, keeps its
    traceback.
    """
    if not isinstance(exc, (ClientError, BotoCoreError)):
        logger.exception("filter_log_events_failed", extra={"log_group": log_group}, stacklevel=3)
        return type(exc).__name__
    extra = {"log_group": log_group, "error_type": type(exc).__name__}
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            return None
        if code:
            extra["error_code"] = code
    logger.warning("filter_log_events_failed", extra=extra, stacklevel=3)
    return code or type(exc).__name__


def _log_read_response(results: list[dict], unreadable: list[dict], read_any: bool) -> dict:
    """The answer of a route that reads log groups: ``{results, unreadable}``.

    ``unreadable`` lists each group not read in full as ``{lambda, reason}``.
    The rows that were read are worth showing, so a partial read answers 200
    beside them: FilterLogEvents' quota is account-wide, so one throttled
    group among many is the usual failure, and failing the whole request
    would hide every row the others gave. When no group could be read and at
    least one call failed, the answer is 502, so a caller that ignores
    ``unreadable`` still sees a failure, not an empty list. A read the
    budget alone cut short is not a failure: it answers 200, and lists the
    groups.
    """
    if not read_any and any(u["reason"] != UNREAD_BUDGET for u in unreadable):
        return failure_response(502, "Could not read any log group", unreadable=unreadable)
    return _json(200, {"results": results, "unreadable": unreadable})


def _newest_log_events(
    log_group: str,
    pattern: str,
    hours: float,
    keep: int = 10,
    max_pages: int = 5,
    include: Callable[[dict], bool] | None = None,
    client=None,
    max_calls: int = _MAX_CALLS_PER_SEARCH,
    deadline: float | None = None,
    budget: _CallBudget | None = None,
) -> LogRead:
    """The newest ``keep`` matching events of the last ``hours``, oldest first,
    and why the search stopped short if it did (``LogRead``).

    A page can come back partly full, or empty, with a ``nextToken``: the API
    stopped scanning, not finding. So a page with a token is followed, and only
    a full one says a range holds more matches than one page.

    With a botocore that knows ``startFromHead``, as the package pins, the
    whole window is read back from now, newest first, until ``keep`` events
    are found. Otherwise it reads the last 15 minutes, then the rest of the
    last 1, 6 and ``hours`` hours, each range once, until ``keep`` are found.
    A range whose page comes back full is halved, the newer half first, since
    that page holds the range's oldest matches. Partial pages are followed
    from the range's oldest end, so a busy group with few matches can spend
    the budget before it reaches the range's last minutes. A range under two
    seconds is read for up to ``max_pages`` pages. A failed page keeps what
    was already read, or, before anything was, what the range halved last had
    read, and the reason is the failure's code. A group that does not exist
    yet is no events, read in full.

    Every search starts no call after ``max_calls`` calls or past
    ``deadline`` (a ``time.monotonic()`` value; ``_SEARCH_SECONDS`` from now
    by default) and gives the newest of what it read, with the reason
    ``UNREAD_BUDGET`` when it stopped with more to read, a group that made no
    call at all included. ``budget``, a share of a request's ``_CallBudget``,
    replaces both when the request reads several groups at once. A call under
    way at the deadline runs to the client's own timeout. ``include`` drops
    events before they count towards ``keep``, for lines a filter pattern
    cannot exclude. ``client`` reads with another CloudWatch Logs client than
    the shared one.
    """
    reader = client or logs
    if budget is None:
        if deadline is None:
            deadline = time.monotonic() + _SEARCH_SECONDS
        budget = _CallBudget(max_calls, deadline=deadline)
    now = int(datetime.now(UTC).timestamp() * 1000)

    def kept(resp: dict) -> list[dict]:
        return [e for e in resp.get("events", []) if include is None or include(e)]

    if _reads_newest_first(reader):
        kwargs = {
            "logGroupName": log_group,
            "startTime": now - int(hours * 3_600_000),
            "endTime": now,
            "filterPattern": pattern,
            "limit": _PAGE_LIMIT,
            "startFromHead": False,
        }
        newest: list[dict] = []
        reason = None
        while len(newest) < keep:
            if not budget.take():
                reason = UNREAD_BUDGET
                break
            try:
                resp = reader.filter_log_events(**kwargs)
            except Exception as e:
                reason = _log_read_failure(e, log_group)
                break
            newest += kept(resp)
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]
        return LogRead(list(reversed(newest[:keep])), reason)

    ranges: deque[tuple[int, int]] = deque()
    end = now
    for window in [w for w in _ERROR_WINDOWS_HOURS if w < hours] + [hours]:
        start = now - int(window * 3_600_000)
        ranges.append((start, end))
        end = start - 1
    # Every range left in ``ranges`` is older than every event in ``found``.
    found: list[dict] = []
    # What the range halved last had read, kept for a failed page.
    set_aside: list[dict] = []
    reason = None
    while ranges and len(found) < keep:
        if not budget.take():
            reason = UNREAD_BUDGET
            break
        start, end = ranges.popleft()
        splittable = end - start >= 2 * _MIN_SPLIT_MS
        kwargs = {
            "logGroupName": log_group,
            "startTime": start,
            "endTime": end,
            "filterPattern": pattern,
            "limit": _PAGE_LIMIT,
        }
        events: list[dict] = []
        busy = False
        pages = 0
        try:
            # The loop's take() paid for this range's first page; each page
            # after it is taken from the budget before it is read.
            while True:
                pages += 1
                resp = reader.filter_log_events(**kwargs)
                events += kept(resp)
                token = resp.get("nextToken")
                if not token:
                    break
                if splittable and len(resp.get("events", [])) >= _PAGE_LIMIT:
                    busy = True
                    break
                # Pages are followed from the range's oldest end, so a range
                # left here has its newest lines unread.
                if (not splittable and pages >= max_pages) or not budget.take():
                    reason = UNREAD_BUDGET
                    break
                kwargs["nextToken"] = token
        except Exception as e:
            return LogRead((events + found)[-keep:] or set_aside[-keep:], _log_read_failure(e, log_group))
        if busy and budget.has_left():
            set_aside = events
            mid = start + (end - start) // 2
            ranges.extendleft([(start, mid), (mid + 1, end)])
        else:
            if busy:
                # Too busy to read whole, with no call left to halve it.
                reason = UNREAD_BUDGET
            found = events[-(keep - len(found)) :] + found
    # The budget can run out between the check that halved a range and the
    # next call: another group's search takes the last call, or the deadline
    # passes. The newest rows of the page read before the halving are then
    # all there is.
    return LogRead(found or set_aside[-keep:], reason)


def handle_get_metrics(params, _body):
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    outputs = config.get("outputs", {})
    webhook = outputs.get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    names = [f"{prefix}-{s}" for s in _lambda_suffixes(config)]
    metrics = {"lambdas": _lambda_metrics(names)}

    # Add token usage from CloudWatch (Strands EMF metrics)
    try:
        logs_client = boto3.client("logs", region_name=config.get("region", REGION))
        end = datetime.now(UTC)
        start = end - timedelta(hours=24)
        log_group = f"/aws/bedrock/{params['id']}/invocations"
        # billed_input = sum of every invocation's input (what Bedrock charges, since
        # each agentic-loop call re-sends the growing context). context_input = the
        # single largest invocation, approximating unique context volume across the
        # window — billed reads as inflated vs that because it re-counts the prefix.
        billed_input_tokens = 0
        context_input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        invocations = 0
        resp = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=500,
        )
        model_id = ""
        for event in resp.get("events", []):
            msg = event.get("message", "")
            if not msg or not msg.startswith("{"):
                continue
            try:
                entry = json.loads(msg)
                inp = entry.get("input", {}) or {}
                it = inp.get("inputTokenCount", 0) or 0
                billed_input_tokens += it
                if it > context_input_tokens:
                    context_input_tokens = it
                output_tokens += entry.get("output", {}).get("outputTokenCount", 0)
                cache_read_tokens += inp.get("cacheReadInputTokenCount", 0) or 0
                cache_write_tokens += inp.get("cacheWriteInputTokenCount", 0) or 0
                invocations += 1
                if not model_id:
                    model_id = entry.get("modelId", "")
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        input_tokens = billed_input_tokens
        # No cost is computed: prices change with each model release and differ
        # by inference profile, so a figure here would go stale silently.
        token_metrics = {
            # input_tokens_24h stays the billed sum for back-compat; total_tokens_24h
            # now reflects unique context (context_input + output) so the dashboard
            # number stops looking inflated.
            "input_tokens_24h": input_tokens,
            "billed_input_tokens_24h": billed_input_tokens,
            "context_input_tokens_24h": context_input_tokens,
            "output_tokens_24h": output_tokens,
            "cache_read_tokens_24h": cache_read_tokens,
            "cache_write_tokens_24h": cache_write_tokens,
            "total_tokens_24h": context_input_tokens + output_tokens,
            "billed_tokens_24h": billed_input_tokens + output_tokens,
            "invocations_24h": invocations,
            "model": model_id,
        }
        metrics["token_usage"] = token_metrics
    except Exception:
        metrics["token_usage"] = {"input_tokens_24h": 0, "output_tokens_24h": 0}

    return _json(200, metrics)


def handle_get_stack(params, _body):
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": f"Stack '{params['id']}' not found"})
    return _json(200, _stack_status(config))


def handle_list_stacks(_params, _body):
    stacks = _list_registry()
    return _json(200, [_stack_status(s) for s in stacks])


def handle_update_config(params, body):
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    # Update Lambda env vars
    env_updates = body.get("environmentVariables", {})
    if not env_updates:
        return _json(400, {"error": "No environmentVariables provided"})
    # Reject reserved/runtime-sensitive env keys. Allowing arbitrary keys lets a
    # caller inject AWS_LAMBDA_EXEC_WRAPPER / LD_PRELOAD / PATH / PYTHONPATH /
    # PYTHONHOME and achieve code execution inside the stack Lambdas (privilege
    # escalation). Deny the AWS_ reserved namespace and known loader/interpreter
    # override keys.
    _RESERVED_ENV_KEYS = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "AWS_LAMBDA_EXEC_WRAPPER"}
    rejected = [
        k for k in env_updates
        if not isinstance(k, str) or k.upper().startswith("AWS_") or k.upper() in _RESERVED_ENV_KEYS
    ]
    if rejected:
        return _json(400, {"error": f"Reserved environment variable keys not allowed: {sorted(rejected)}"})
    webhook = config.get("outputs", {}).get("webhook_lambda", "")
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else params["id"]
    updated = []
    for suffix in _lambda_suffixes(config):
        fn_name = f"{prefix}-{suffix}"
        try:
            fn = lambda_client.get_function_configuration(FunctionName=fn_name)
            current_env = fn.get("Environment", {}).get("Variables", {})
            current_env.update(env_updates)
            lambda_client.update_function_configuration(FunctionName=fn_name, Environment={"Variables": current_env})
            updated.append(fn_name)
        except Exception:
            continue
    return _json(200, {"updated": updated})


# Lambda publishes Invocations a minute or more after the invocation, so the
# probes of the last few minutes, the one a page load just fired included, are
# not in the metric yet. The metric and the probe counters are both read up to
# this many minutes ago, on a whole minute.
_METRIC_LAG_MINUTES = 3


def _probe_counter_key(function_name: str, at: datetime) -> str:
    """Sort key of the per-minute health-probe counter for ``function_name`` at ``at``.

    Every console page load probes each stack Lambda, and the probes count in
    its Invocations metric. They are counted per minute so the minutes inside
    that metric's 24-hour window, whose start CloudWatch rounds down to the
    minute, can be subtracted from it. One counter per function kept every
    probe ever fired and drove usage to 0; one per hour also subtracted the
    probes from before the window in its first hour.
    """
    return f"{function_name}#{at.strftime('%Y%m%d%H%M')}"


@tracer.capture_method
def _lambda_metrics(function_names: list[str]) -> list[dict]:
    """Get Lambda health via active probes + CloudWatch metrics (last 24h).

    Probes each function in parallel using ThreadPoolExecutor — for 4 stack
    Lambdas at ~300ms/probe, total wall time drops from ~1.2s to ~0.35s.
    boto3 clients are thread-safe per AWS docs.
    """
    from concurrent.futures import ThreadPoolExecutor

    now = datetime.now(UTC)
    # The metric's EndTime is exclusive: its window holds the minutes from
    # start up to the one before end, and so do the counters read below.
    end = (now - timedelta(minutes=_METRIC_LAG_MINUTES)).replace(second=0, microsecond=0)
    start = end - timedelta(hours=24)

    def _probe_one(name: str) -> dict:
        short_name = name.rsplit("-", 1)[-1] if "-" in name else name
        probe_healthy = None
        invocations = 0
        errors = 0
        recent_errors = 0
        error_rate = "0.00%"
        last_modified = ""

        # Active health probe — invoke Lambda with health_check payload
        try:
            probe_resp = lambda_client.invoke(
                FunctionName=name, InvocationType="RequestResponse",
                Payload=json.dumps({"operation": "health_check"}).encode(),
            )
            status_code = probe_resp.get("StatusCode", 0)
            function_error = probe_resp.get("FunctionError")
            probe_healthy = status_code == 200 and not function_error
            # Count the probe in this minute's counter (atomic increment). The
            # table deletes it at ttl, once the window it is read in has passed.
            try:
                ttl_epoch = int(now.timestamp()) + 2 * 86400
                settings_table.update_item(
                    Key={"pk": "PROBE_COUNT", "sk": _probe_counter_key(name, now)},
                    UpdateExpression="ADD #c :one SET #t = :ttl",
                    ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
                    ExpressionAttributeValues={":one": 1, ":ttl": ttl_epoch},
                )
            except Exception:
                logger.exception("handler_error")
        except Exception:
            probe_healthy = False

        # CloudWatch metrics for error rate and invocation counts
        try:
            resp = cw.get_metric_data(
                StartTime=start, EndTime=end,
                MetricDataQueries=[
                    {"Id": "invocations", "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": "Invocations", "Dimensions": [{"Name": "FunctionName", "Value": name}]}, "Period": 86400, "Stat": "Sum"}},
                    {"Id": "errors", "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": "Errors", "Dimensions": [{"Name": "FunctionName", "Value": name}]}, "Period": 86400, "Stat": "Sum"}},
                    {"Id": "recent_errors", "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": "Errors", "Dimensions": [{"Name": "FunctionName", "Value": name}]}, "Period": 3600, "Stat": "Sum"}, "Label": "recent_errors"},
                ],
            )
            invocations = int(resp["MetricDataResults"][0]["Values"][0]) if resp["MetricDataResults"][0]["Values"] else 0
            errors = int(resp["MetricDataResults"][1]["Values"][0]) if resp["MetricDataResults"][1]["Values"] else 0
            recent_errors = int(resp["MetricDataResults"][2]["Values"][0]) if resp["MetricDataResults"][2]["Values"] else 0
            error_rate = f"{min(errors / invocations * 100, 100):.2f}%" if invocations > 0 else "0.00%"
            fn_config = lambda_client.get_function(FunctionName=name)
            last_modified = fn_config["Configuration"].get("LastModified", "")
        except Exception:
            logger.exception("handler_error")

        # Composite status: probe is authoritative, metrics add context
        if not probe_healthy:
            status = "error"
        elif errors > 0 and recent_errors == 0:
            status = "recovering"
        elif invocations > 0 and errors > 0 and errors / invocations >= 0.2:
            status = "degraded"
        else:
            status = "healthy"

        # Probes in the minutes the Invocations window covers.
        probe_count = 0
        try:
            last_minute = end - timedelta(minutes=1)
            query = {
                "KeyConditionExpression": Key("pk").eq("PROBE_COUNT")
                & Key("sk").between(_probe_counter_key(name, start), _probe_counter_key(name, last_minute)),
            }
            while True:
                resp = settings_table.query(**query)
                for item in resp.get("Items", []):
                    try:
                        probe_count += int(item.get("count", 0))
                    except (ValueError, TypeError):
                        continue
                if "LastEvaluatedKey" not in resp:
                    break
                query["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        except Exception:
            logger.exception("handler_error")
        probe_count = min(probe_count, invocations)
        user_invocations = max(invocations - probe_count, 0)

        return {
            "name": short_name,
            "invocations": invocations,
            "userInvocations": user_invocations,
            "probeInvocations": probe_count,
            "errors": errors,
            "errorRate": error_rate,
            # The function's LastModified. A last-invocation time would always
            # read as now, since this probe just invoked it.
            "lastInvocation": last_modified,
            "status": status,
            "probeStatus": "pass" if probe_healthy else "fail",
        }

    if not function_names:
        return []

    with ThreadPoolExecutor(max_workers=min(8, len(function_names))) as pool:
        return list(pool.map(_probe_one, function_names))


@tracer.capture_method
def _stack_status(config: dict) -> dict:
    """Build a StackStatus from config + live data."""
    outputs = config.get("outputs", {})
    stack_id = f"{config.get('project', 'unknown')}-{config.get('environment', 'unknown')}"

    # Lambda names from outputs
    webhook = outputs.get("webhook_lambda", "")
    function_names = [webhook] if webhook else []
    prefix = webhook.rsplit("-webhook", 1)[0] if webhook else stack_id
    for suffix in _lambda_suffixes(config):
        if suffix != "webhook":
            function_names.append(f"{prefix}-{suffix}")

    lambdas = _lambda_metrics(function_names)
    # The webhook's invocations less the console's own health probes, which
    # every page load fires and which made a reload raise the count.
    webhook_invocations = sum(fn["userInvocations"] for fn in lambdas if fn["name"] == "webhook")

    # Health — weighted by severity
    error_lambdas = [fn for fn in lambdas if fn["status"] == "error"]
    degraded_lambdas = [fn for fn in lambdas if fn["status"] == "degraded"]
    recovering_lambdas = [fn for fn in lambdas if fn["status"] == "recovering"]
    healthy_lambdas = [fn for fn in lambdas if fn["status"] in ("healthy", "recovering")]

    if len(error_lambdas) >= len(lambdas) / 2:
        health = "down"
        health_detail = f"Critical: {len(error_lambdas)}/{len(lambdas)} Lambda health probes failed. Check IAM roles, KMS permissions, and deployment status."
    elif error_lambdas:
        health = "degraded"
        health_detail = f"{', '.join(fn['name'] for fn in error_lambdas)} failed health probe ({len(healthy_lambdas)}/{len(lambdas)} healthy). Likely cause: IAM role, KMS key, or code import error."
    elif degraded_lambdas:
        health = "degraded"
        health_detail = f"{', '.join(fn['name'] for fn in degraded_lambdas)} have elevated error rates ({len(healthy_lambdas)}/{len(lambdas)} healthy). Check CloudWatch logs for recurring errors."
    elif recovering_lambdas:
        health = "recovering"
        health_detail = f"{', '.join(fn['name'] for fn in recovering_lambdas)} had errors in the last 24h but none in the last hour. No action needed — will auto-resolve when the 24h window rolls over."
    else:
        health = "healthy"
        health_detail = f"All {len(lambdas)} Lambda functions passed health probes with clean error metrics."

    # Runtime status
    runtime_status = "running"
    runtime_version = ""
    runtime_description = ""
    try:
        agentcore_cp = boto3.client("bedrock-agentcore-control", region_name=config.get("region", REGION))
        rt = agentcore_cp.get_agent_runtime(agentRuntimeId=outputs.get("runtime_id", ""))
        status = rt.get("status", "UNKNOWN")
        runtime_status = "running" if status in ("ACTIVE", "READY") else "stopped" if status == "INACTIVE" else "error"
        runtime_description = rt.get("description", "")
        runtime_version = str(rt.get("lastUpdatedAt", rt.get("createdAt", "")))
    except Exception:
        runtime_status = "error"

    # Last deploy from CodeBuild, read the way the Deploys tab reads it: the
    # client and the project are the stack's, not the console's, so a stack in
    # another Region, or a zip stack, is not read as never deployed (#62).
    last_deploy = ""
    try:
        builder, project_name = _deploys_builder(config, prefix)
        builds = builder.list_builds_for_project(projectName=project_name, sortOrder="DESCENDING")
        if builds.get("ids"):
            build_info = builder.batch_get_builds(ids=[builds["ids"][0]])
            last_deploy = str(build_info["builds"][0].get("endTime", ""))
    except Exception:
        logger.exception("handler_error")

    # Auto-discover tools and skills from registry if not in config
    if not config.get("tools"):
        try:
            obj = s3.get_object(Bucket=REGISTRY_BUCKET, Key=_registry_key(f"{stack_id}/tools.json"))
            tools_meta = json.loads(obj["Body"].read())
            config["tools"] = list(tools_meta.keys())
        except Exception:
            logger.exception("handler_error")
    if not config.get("skills"):
        try:
            resp = s3.list_objects_v2(Bucket=REGISTRY_BUCKET, Prefix=_registry_prefix_for(f"{stack_id}/skills/"), Delimiter="/")
            config["skills"] = [c["Key"].split("/")[-1].replace(".md", "") for c in resp.get("Contents", []) if c["Key"].endswith(".md")]
        except Exception:
            logger.exception("handler_error")

    return {
        "stack": config,
        "health": health,
        "healthDetail": health_detail,
        "runtimeStatus": runtime_status,
        "runtimeVersion": runtime_version,
        "runtimeDescription": runtime_description,
        "messages24h": webhook_invocations,
        "lastDeploy": last_deploy,
        "lambdas": lambdas,
    }



@app.get("/api/stacks")
@require_group("viewer")
def list_stacks():
    return from_legacy(handle_list_stacks({}, {}))


@app.get("/api/stacks/<id>")
@require_group("viewer")
def get_stack(id: str):
    return from_legacy(handle_get_stack({"id": id}, {}))


@app.get("/api/stacks/<id>/metrics")
@require_group("viewer")
def get_metrics(id: str):
    return from_legacy(handle_get_metrics({"id": id}, {}))


@app.put("/api/stacks/<id>/config")
@require_group("operator")
def update_config(id: str):
    return from_legacy(handle_update_config({"id": id}, current_body()))


# operator, not viewer: handle_get_errors returns raw CloudWatch messages from
# the stack Lambdas, and webhook.py logs `... for %s, phone` on exceptions, so
# these lines carry end-user phone numbers plus botocore error text (assumed-role
# ARNs, account id). The equivalent log search (/api/logs) and /conversations,
# which also expose that data, are both operator-gated; only /errors was not.
@app.get("/api/stacks/<id>/errors")
@require_group("operator")
def get_errors(id: str):
    return from_legacy(handle_get_errors({"id": id}, {}))


@app.get("/api/stacks/<id>/deploys")
@require_group("viewer")
def get_deploys(id: str):
    return from_legacy(handle_get_deploys({"id": id}, {}))
