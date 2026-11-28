# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Templates routes: list, detail, cross-stack health."""
from __future__ import annotations

import json
import re
import time

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import require_group
from shared.aws import REGION
from shared.http import error_response as _error, json_response as _json
from shared.registry import get_registry as _get_registry, get_all_registries as _get_all_registries, template_matches_stack
from shared.resolver import app, from_legacy

tracer = Tracer(service="admin-console-backend")
logger = Logger(service="admin-console-backend", child=True)

# Meta template names: lowercase letters, digits and underscores, at most 512
# (the rule src/socialagent/templates/validate.py applies before a sync). The
# console reads the name from conversation text an end user can write, so refuse
# anything else before it costs a registry read and the EUM Social calls.
# fullmatch, not match with `$`: Python's `$` also matches before a trailing newline.
_TEMPLATE_NAME_RE = re.compile(r"[a-z0-9_]{1,512}")

# ListWhatsAppMessageTemplates and ListLinkedWhatsAppBusinessAccounts return at
# most 100 items a page (maxResults 1-100) and a nextToken while more remain.
_PAGE_SIZE = 100

# Timeouts and retries for every EUM Social client these routes create. With
# botocore's defaults (60 s to connect, 60 s to read, up to 5 attempts in
# legacy mode) one call started just before the deadline below could run for
# minutes, so the deadline alone could not keep a request inside 30 s. With
# these, one call ends within 11 s: 2 attempts of at most 2 s to connect and
# 3 s to read, 10 s, plus the standard mode's one backoff between them,
# random() * min(_BASE ** (attempt - 1), max_backoff) after attempt 1, with
# _BASE 2 and max_backoff 20: at most 1 s (ExponentialBackoff in
# botocore/retries/standard.py). So a throttled or slow call is retried once,
# then fails. total_max_attempts, not max_attempts: in a client Config,
# max_attempts counts the retries after the first attempt, so max_attempts=2
# would make 3 attempts and up to 18 s (3 x 5 + 1 + 2).
_EUM_CONFIG = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"mode": "standard", "total_max_attempts": 2},
)

# Seconds into a list or health request after which it starts no EUM Social
# call (phone lookup, WABA lookup, listing page or template detail), counted
# on time.monotonic() from the moment the route handler starts, so the
# registry read comes out of these 15 s. The console backend Lambda (its
# timeout is in infra-terraform/modules/api/main.tf, which
# tests/unit/test_template_budget.py reads) and the HTTP API in front of it
# stop at 30 s: a call started just before the deadline ends by 15 + 11 s
# (_EUM_CONFIG), and 3 s are left for a cold start, the caller's group
# lookup (require_group, before the handler starts) and the response, 29 s
# in all. 16 s would fit too, with no slack for what the timeouts do not
# bound (a DNS lookup; the read timeout bounds each wait for data, not a
# whole response). The registry read and the group lookup themselves are
# bounded only by shared/clients.py: both are cached, but on a miss with S3
# or Cognito slow a request can still reach 30 s.
# A budget, not a thread pool: GetWhatsAppMessageTemplate's quota of 10
# requests a second per account and Region cannot be raised, so it caps any
# parallelism. A template the budget does not reach is reported as not
# checked, never as not reclassified, and a listing the budget cuts short
# fails its stack rather than pass for a complete one.
_EUM_BUDGET_SECONDS = 15.0


class _DeadlinePassed(Exception):
    """The request's EUM Social deadline passed before the next call could start."""


def _check_deadline(deadline: float | None) -> None:
    """Raise ``_DeadlinePassed`` once *deadline*, a ``time.monotonic()`` reading, has passed."""
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlinePassed


def _eum_client(region: str):
    """An EUM Social client for *region*, its calls bounded by ``_EUM_CONFIG``."""
    return boto3.client("socialmessaging", region_name=region, config=_EUM_CONFIG)


def _log_failed_call(event: str, exc: Exception, **context) -> None:
    """Log *exc*, the failure being handled, with a traceback only when it is not botocore's.

    Call from an ``except`` block. Throttled, a stack with a few hundred
    templates fails detail call after detail call, each after its one retry,
    until the budget runs out, and a traceback apiece would bury a real
    error. A botocore error (a ``ClientError`` such as ThrottlingException,
    or a ``BotoCoreError`` such as a read timeout) is told apart by its class
    and code, so it is a warning that names them. Anything else, such as a
    detail that is not JSON or a fault in this code, keeps its traceback.
    """
    # stacklevel=3, one more than Powertools' default: the record's location
    # then names the function that handled the failure, not this helper.
    if not isinstance(exc, (ClientError, BotoCoreError)):
        logger.exception(event, extra=context, stacklevel=3)
        return
    context["error_type"] = type(exc).__name__
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code:
            context["error_code"] = code
    logger.warning(event, extra=context, stacklevel=3)


def _paginate(list_call, items_key, *, deadline: float | None = None, **kwargs):
    """Yield every item an EUM Social list call returns, following nextToken.

    Lazy: a caller that stops iterating makes no further calls. With a
    *deadline*, a page that would start after it raises ``_DeadlinePassed``
    instead, so a caller cannot take a listing cut short for a complete one.
    """
    kwargs["maxResults"] = _PAGE_SIZE
    while True:
        _check_deadline(deadline)
        resp = list_call(**kwargs)
        yield from resp.get(items_key, [])
        next_token = resp.get("nextToken")
        if not next_token:
            return
        kwargs["nextToken"] = next_token


def _template_category(listing: dict, detail: dict | None) -> tuple[str, str | None, bool]:
    """``(category, previous_category, reclassified)`` for one listed template.

    Both the template list and the health check decide with this. The category
    comes from the template's *detail*, which carries ``previous_category``
    beside it, and from the listing's ``templateCategory`` when the detail has
    none (``None`` when it was not read, and anything but an object counts as
    empty). A previous category other than the current one means Meta moved
    the template, commonly UTILITY to MARKETING, which suppresses its delivery
    without an error.
    """
    if not isinstance(detail, dict):
        detail = {}
    category = detail.get("category") or listing.get("templateCategory", "UTILITY")
    previous = detail.get("previous_category") or None
    return category, previous, bool(previous and previous != category)


def _read_template_detail(eum, waba_id: str, listing: dict, deadline: float) -> dict | None:
    """The listed template's detail, or ``None`` when it was not read.

    Makes no call once *deadline* (a ``time.monotonic()`` reading) has passed.
    A call that fails, or a detail whose JSON is not an object, is ``None``
    too: either way it cannot say whether Meta moved the template, so the
    caller reports it as not checked rather than as not reclassified. A
    failure is logged with the template's name, by ``_log_failed_call``.
    """
    if time.monotonic() >= deadline:
        return None
    try:
        resp = eum.get_whatsapp_message_template(
            id=waba_id, metaTemplateId=listing.get("metaTemplateId", "")
        )
        detail = json.loads(resp.get("template", "{}"))
    except Exception as exc:
        _log_failed_call("template_detail_failed", exc, template=listing.get("templateName", ""))
        return None
    return detail if isinstance(detail, dict) else None


def handle_get_template_detail(params, _body):
    """Get detail for a specific WhatsApp template including components.

    Its calls are bounded by ``_EUM_CONFIG`` but not by the list routes'
    deadline: it stops at the first match and reads one detail, and a request
    that runs out of time fails rather than answer in part.
    """
    template_name = params["template_name"]
    if not _TEMPLATE_NAME_RE.fullmatch(template_name):
        return _json(400, {"error": "Invalid template name"})
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    try:
        eum = _eum_client(config.get("region", REGION))
        phone_number_id = config.get("phone_number_id", "")
        waba_id = None
        if phone_number_id:
            try:
                phone_resp = eum.get_linked_whatsapp_business_account_phone_number(id=phone_number_id)
                waba_id = phone_resp.get("linkedWhatsAppBusinessAccountId", "")
            except Exception:
                logger.exception("handler_error")
        if not waba_id:
            for acct in _paginate(eum.list_linked_whatsapp_business_accounts, "linkedAccounts"):
                waba_id = acct.get("id")
                break
        if not waba_id:
            return _json(404, {"error": "No WABA found"})
        # Find the template's metaTemplateId, reading pages only until it turns up
        meta_id = None
        listing_language = "en"
        for t in _paginate(eum.list_whatsapp_message_templates, "templates", id=waba_id):
            if t.get("templateName") == template_name:
                meta_id = t.get("metaTemplateId")
                # templateLanguage from listing is authoritative (e.g. "pt_BR")
                listing_language = t.get("templateLanguage", "en")
                break
        if not meta_id:
            return _json(404, {"error": f"Template '{template_name}' not found"})
        # Get full template detail
        detail_resp = eum.get_whatsapp_message_template(metaTemplateId=meta_id, id=waba_id)
        template_json = detail_resp.get("template", "{}")
        template_data = json.loads(template_json) if isinstance(template_json, str) else template_json
        components = []
        for comp in template_data.get("components", []):
            component = {"type": comp.get("type", "BODY"), "text": comp.get("text", "")}
            example = comp.get("example", {})
            if example.get("body_text"):
                variables = []
                for i, val in enumerate(example["body_text"][0] if example["body_text"] else []):
                    variables.append({"position": i + 1, "name": val})
                component["variables"] = variables
            components.append(component)
        # Prefer language from the detail JSON; fall back to listing's templateLanguage
        # (listing is authoritative — detail JSON may omit the field).
        language = template_data.get("language") or listing_language
        return _json(200, {
            "name": template_data.get("name", template_name),
            "category": template_data.get("category", "UTILITY"),
            "language": language,
            "status": template_data.get("status", "UNKNOWN").lower(),
            "components": components,
        })
    except Exception:
        return _error(500, "Failed to fetch template")


def handle_get_template_health(_params, _body):
    """Global template health check across all stacks.

    Returns templates reclassified by Meta (UTILITY → MARKETING usually),
    which silently break delivery. Also surfaces templates stuck PENDING
    or REJECTED across all deployed stacks so the Overview page can show
    a single badge count.

    Response shape:
        {
            "reclassified": [{name, stack, category, previousCategory}, ...],
            "nonApproved": [{name, stack, approvalStatus}, ...],
            "total": int,
            "unchecked": [{name, stack}, ...],
            "failedStacks": [stack, ...]
        }

    ``unchecked`` names the counted templates whose detail was not read, past
    the deadline or because the call failed, so ``reclassified`` cannot
    vouch for them. ``failedStacks`` names the stacks whose phone number
    lookup failed or found no WABA, or whose listing failed, on any page, and
    those the deadline reached before their lookup or one of their listing
    pages could start: a stack whose listing stopped on a later page keeps
    what it listed before in the counts, but its listing is incomplete. A
    stack with no phone number is skipped, not failed. One deadline covers
    the whole request: every stack is listed first, in registry order, so
    once the deadline passes during the listing every stack after that one
    is failed too, and the details are then read in the same order with what
    is left of it.
    """
    deadline = time.monotonic() + _EUM_BUDGET_SECONDS
    try:
        registries = _get_all_registries()
    except Exception:
        return _error(500, "Registry error")

    reclassified = []
    non_approved = []
    unchecked = []
    failed_stacks = []
    total = 0

    # Cache EUM clients and WABA lookups to avoid duplicate API calls
    eum_by_region: dict[str, boto3.client] = {}

    # Every stack is listed before any detail is read. A page is one call per
    # 100 templates and a detail one call per template, so with the details
    # read page by page a large stack's details would use up the deadline
    # before its next page or the next stack's lookup could start, and fail
    # those stacks where they should only leave templates unchecked. Each
    # entry is (stack, client, WABA id, listed template), in registry and
    # listing order.
    listed = []
    for stack_id, config in registries.items():
        phone_arn = config.get("phone_number_id", "")
        if not phone_arn:
            continue
        region = config.get("region", REGION)
        if region not in eum_by_region:
            eum_by_region[region] = _eum_client(region)
        eum = eum_by_region[region]

        try:
            _check_deadline(deadline)
            phone_resp = eum.get_linked_whatsapp_business_account_phone_number(id=phone_arn)
            waba_id = phone_resp.get("linkedWhatsAppBusinessAccountId", "")
            if not waba_id:
                failed_stacks.append(stack_id)
                continue
            for t in _paginate(
                eum.list_whatsapp_message_templates, "templates", deadline=deadline, id=waba_id
            ):
                name = t.get("templateName", "")
                if not template_matches_stack(name, config):
                    continue
                total += 1
                status = t.get("templateStatus", "PENDING")
                if status != "APPROVED":
                    non_approved.append({
                        "name": name,
                        "stack": stack_id,
                        "approvalStatus": status.lower(),
                    })
                listed.append((stack_id, eum, waba_id, t))
        except _DeadlinePassed:
            # Not a failed call, so no traceback: the counts keep what the
            # stack listed before, as when a later page fails.
            logger.warning("template_deadline_passed", extra={"stack": stack_id})
            failed_stacks.append(stack_id)
        except Exception as exc:
            _log_failed_call("template_stack_failed", exc, stack=stack_id)
            failed_stacks.append(stack_id)

    # Check reclassification with what is left of the deadline
    for stack_id, eum, waba_id, t in listed:
        name = t.get("templateName", "")
        tpl = _read_template_detail(eum, waba_id, t, deadline)
        if tpl is None:
            unchecked.append({"name": name, "stack": stack_id})
            continue
        cat, prev, moved = _template_category(t, tpl)
        if moved:
            reclassified.append({
                "name": name,
                "stack": stack_id,
                "category": cat,
                "previousCategory": prev,
            })

    return _json(200, {
        "reclassified": reclassified,
        "nonApproved": non_approved,
        "total": total,
        "unchecked": unchecked,
        "failedStacks": failed_stacks,
    })


def handle_get_templates(params, _body):
    """List one stack's templates, each with its category and whether Meta moved it.

    ``checked`` is true only when the template's detail was read, before the
    deadline, as a JSON object; an unchecked template carries the listing's
    category and ``reclassified`` false, which then does not mean that it was
    not moved. When the deadline comes before the phone or WABA lookup, or a
    listing page, can start, the request fails rather than list part of the
    stack's templates.
    """
    deadline = time.monotonic() + _EUM_BUDGET_SECONDS
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    phone_number_id = config.get("phone_number_id", "")
    if not phone_number_id:
        return _json(200, [])
    stack_id = f"{config.get('project', 'unknown')}-{config.get('environment', 'unknown')}"
    try:
        eum = _eum_client(config.get("region", REGION))
        # Get WABA ID from the phone number's linked account. The deadline
        # check stays outside the inner try: out of time, the request fails
        # instead of falling back to the first WABA.
        waba_id = None
        _check_deadline(deadline)
        try:
            phone_resp = eum.get_linked_whatsapp_business_account_phone_number(id=phone_number_id)
            waba_id = phone_resp.get("linkedWhatsAppBusinessAccountId", "")
        except Exception:
            logger.exception("handler_error")
        if not waba_id:
            # Fallback: first WABA
            for acct in _paginate(
                eum.list_linked_whatsapp_business_accounts, "linkedAccounts", deadline=deadline
            ):
                waba_id = acct.get("id")
                break
        if not waba_id:
            return _json(200, [])
        # Every page before any detail: read page by page, a full page's
        # details would use up the deadline before the next page could start,
        # and fail the request where they should only leave templates unchecked.
        listed = [
            t
            for t in _paginate(
                eum.list_whatsapp_message_templates, "templates", deadline=deadline, id=waba_id
            )
            if template_matches_stack(t.get("templateName", ""), config)
        ]
        templates = []
        for t in listed:
            name = t.get("templateName", "")
            # Detect category reclassification by fetching the template detail.
            tpl = _read_template_detail(eum, waba_id, t, deadline)
            current_cat, previous_cat, reclassified = _template_category(t, tpl)
            templates.append({
                "name": name,
                "stack": stack_id,
                "category": current_cat,
                "previousCategory": previous_cat,
                "reclassified": reclassified,
                "checked": tpl is not None,
                "language": t.get("templateLanguage", "en"),
                "approvalStatus": t.get("templateStatus", "PENDING").lower(),
            })
        return _json(200, templates)
    except Exception:
        return _error(500, "Templates error")



@app.get("/api/stacks/<id>/templates")
@require_group("operator")
def get_templates(id: str):
    return from_legacy(handle_get_templates({"id": id}, {}))


@app.get("/api/stacks/<id>/templates/<template_name>")
@require_group("operator")
def get_template_detail(id: str, template_name: str):
    return from_legacy(handle_get_template_detail({"id": id, "template_name": template_name}, {}))


@app.get("/api/templates/health")
@require_group("operator")
def get_template_health():
    return from_legacy(handle_get_template_health({}, {}))
