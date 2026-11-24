# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
EUM Social (AWS socialmessaging) implementation of MessageProvider.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import NoReturn

import boto3
from botocore.config import Config
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.types import CertificatePublicKeyTypes
from cryptography.x509 import load_pem_x509_certificate

from socialagent.constants import OUTBOUND_GAP_SECONDS
from socialagent.providers import MessageProvider, TemplateNotApprovedError
from socialagent.providers.utils import (
    chunk_text,
    extract_changes,
    parse_message,
    parse_status,
    sanitize_template_param,
)
from socialagent.types import InboundMessage, OutboundMessage, StatusUpdate
from socialagent.utils.phone import mask_phone

logger = logging.getLogger(__name__)


class MediaTooLarge(RuntimeError):
    """A media object exceeds the caller's cap; raised before it is read."""

    def __init__(self, size: int, max_bytes: int) -> None:
        super().__init__(f"media is {size} bytes, over the {max_bytes}-byte cap")
        self.size = size
        self.max_bytes = max_bytes


# How long a template found APPROVED is sent without checking again. Meta can
# pause an approved template, so the answer is not kept for the container's
# life.
_APPROVED_TEMPLATE_TTL_SECONDS = 300.0

# How long a template found not APPROVED is refused without checking again.
# The lookup fails open, so in a burst of sends of a PENDING template (a
# reminder firing for many users) a throttled listing would let out the very
# template the check stops. The refusal lives on the provider, one per Lambda
# execution environment, and the burst runs as concurrent invocations, so it
# covers only the later sends that reach the same warm environment: the first
# send in each new environment lists the templates, and a throttled one there
# still sends unchecked. Kept short, as an approval takes effect only once it
# expires.
_REFUSED_TEMPLATE_TTL_SECONDS = 60.0

# At most this many refusals are kept. An agent can queue a template by any
# name, and each name the WABA does not list is a refusal of its own.
_REFUSED_TEMPLATE_MAX_ENTRIES = 256

# The approval lookup runs before a send, often with little of the Lambda's
# time left (the webhook's reply follows a long agent turn), so it gets two
# attempts of at most 2 s to connect and 3 s to read, about 11 s in all, as
# the admin console's template reads do, instead of the send client's legacy
# retries and 60 s timeouts. A lookup that still fails sends unchecked.
_LOOKUP_CONFIG = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"mode": "standard", "total_max_attempts": 2},
)

# Top-level keys of a WhatsApp send that the provider owns, never the payload.
# recipient_type is one because it decides what "to" addresses (a user or a group).
_ENVELOPE_KEYS = frozenset({"messaging_product", "recipient_type", "to", "type"})

# Key roots (under MEDIA_PREFIX) that upload_media_from_s3 accepts: where
# voice-reply, generate-image and EUM inbound media write. Keep in step with the
# notification role's MediaUploadSourceRead grant in modules/iam/tool-roles.tf.
_MEDIA_UPLOAD_ROOTS = ("voice", "images", "media")


class EUMSocialProvider(MessageProvider):
    """MessageProvider backed by AWS End User Messaging Social (socialmessaging)."""

    def __init__(
        self,
        phone_number_id: str,
        region: str = "us-east-1",
        meta_api_version: str = "v20.0",
        template_prefix: str = "",
        transcription_bucket: str = "",
        conversation_logs_bucket: str = "",
        media_prefix: str = "",
        message_gap_seconds: float = OUTBOUND_GAP_SECONDS,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._meta_api_version = meta_api_version
        self._template_prefix = template_prefix
        self._transcription_bucket = transcription_bucket
        self._conversation_logs_bucket = conversation_logs_bucket
        self._media_prefix = media_prefix
        # Between the chunks of a long text: WhatsApp can deliver messages sent
        # back to back out of order, which would scramble a split reply.
        self._message_gap_seconds = message_gap_seconds
        self._client = boto3.client("socialmessaging", region_name=region)
        self._lookup_client = boto3.client(
            "socialmessaging", region_name=region, config=_LOOKUP_CONFIG
        )
        self._waba_id: str = ""  # lazy-loaded on the first template sync or send
        # Both template caches are keyed by (full template name, language): the
        # provider sends from one number, so from one WABA, and the full name
        # carries the stack's prefix.
        # The time.monotonic() until which the template counts as approved
        # without a lookup.
        self._approved_until: dict[tuple[str, str], float] = {}
        # The time.monotonic() until which the template is refused without a
        # lookup, and the status that refused it. Insertion order is expiry
        # order, as every refusal is kept as long.
        self._refused_until: dict[tuple[str, str], tuple[float, str]] = {}
        self._refused_lock = threading.Lock()

    # ── Inbound ───────────────────────────────────────────────────────────────

    async def parse_webhook(
        self, raw_event: dict
    ) -> InboundMessage | StatusUpdate | None:
        """Parse an SNS-wrapped EUM Social webhook event into an InboundMessage or StatusUpdate."""
        try:
            sns_body = raw_event.get("Records", [{}])[0].get("Sns", {})
            if sns_body and not await asyncio.to_thread(
                _verify_sns_signature, sns_body
            ):
                logger.warning("SNS signature verification failed — dropping event")
                return None
            body = json.loads(sns_body.get("Message", "{}"))
        except (KeyError, json.JSONDecodeError):
            body = raw_event

        entry = body.get("whatsAppWebhookEntry", body)
        if isinstance(entry, str):
            entry = json.loads(entry)

        changes = extract_changes(entry)
        if changes is None:
            return None

        statuses = changes.get("statuses")
        if statuses:
            return parse_status(statuses[0])

        messages = changes.get("messages")
        if not messages:
            return None

        return parse_message(messages[0])

    # ── Outbound ──────────────────────────────────────────────────────────────

    def _send_raw(self, payload: dict) -> str:
        """Synchronous send via socialmessaging API. Returns the message ID."""
        resp = self._client.send_whatsapp_message(
            originationPhoneNumberId=self._phone_number_id,
            message=json.dumps(payload).encode(),
            metaApiVersion=self._meta_api_version,
        )
        message_id = resp.get("messageId", "")
        # Log every send so status callbacks can be correlated by message_id.
        # Meta silently drops some sends — pairing this log with an on_status
        # hook's record of the same message_id is the only way to see outcomes.
        logger.info(
            "send message_id=%s to=%s type=%s template=%s",
            message_id,
            mask_phone(payload.get("to", "")),
            payload.get("type", ""),
            payload.get("template", {}).get("name", ""),
        )
        return message_id

    async def send_text(self, to: str, text: str) -> str:
        """Send plain text, chunking at MAX_MSG_LEN chars if needed. Returns last message ID."""
        last_id = ""
        if not to.startswith("+"):
            to = f"+{to}"
        # The LLM sometimes emits the literal two-char sequence "\n" (from skill
        # examples written as "...\n\n") instead of a real newline. WhatsApp shows
        # it verbatim. Normalize to real newlines so messages render correctly.
        text = text.replace(r"\n", "\n")
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": chunk},
            }
            last_id = await asyncio.to_thread(self._send_raw, payload)
            if i < len(chunks) - 1:
                await asyncio.sleep(self._message_gap_seconds)
        return last_id

    async def send_message(self, to: str, message: OutboundMessage) -> str:
        """Send an OutboundMessage of any type. Returns the message ID."""
        if not to.startswith("+"):
            to = f"+{to}"
        # Delegate text messages to send_text so chunking applies consistently
        if message.type == "text":
            body = message.payload.get("text", {}).get("body", "")
            return await self.send_text(to, body)
        # The payload can be agent-built (the webhook drain, the notification
        # Lambda, a scheduled message), so it must not override the envelope: a
        # "to" inside it would redirect the message past the caller's recipient
        # pin. No builder sets these keys, so one present is logged and dropped.
        overridden = sorted(_ENVELOPE_KEYS.intersection(message.payload))
        if overridden:
            logger.warning(
                "Dropped envelope keys %s from a %s payload — the recipient is pinned",
                overridden,
                message.type,
            )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": message.type,
            **{k: v for k, v in message.payload.items() if k not in _ENVELOPE_KEYS},
        }
        return await asyncio.to_thread(self._send_raw, payload)

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        params: list[str],
        header_params: list[str] | None = None,
        button_payloads: list[str] | None = None,
    ) -> str:
        """Send a pre-approved WhatsApp template message. Returns the message ID.

        Raises:
            TemplateNotApprovedError: the WhatsApp Business Account lists the
                template in ``language`` with a status other than APPROVED, or
                does not list it. Nothing is sent. This provider remembers a
                refusal for 60 seconds, so a send through it in that time is
                refused without a lookup. Otherwise, when the lookup itself
                fails, the template is sent unchecked, as before the check.
        """
        if not to.startswith("+"):
            to = f"+{to}"
        full_name = f"{self._template_prefix}{name}" if self._template_prefix else name
        await asyncio.to_thread(self._require_approved, full_name, language)
        components: list[dict] = []
        if header_params:
            components.append(
                {
                    "type": "header",
                    "parameters": [
                        {"type": "text", "text": sanitize_template_param(p)}
                        for p in header_params
                    ],
                }
            )
        if params:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": sanitize_template_param(p)}
                        for p in params
                    ],
                }
            )
        if button_payloads:
            for i, bp in enumerate(button_payloads):
                components.append(
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": i,
                        "parameters": [{"type": "payload", "payload": bp}],
                    }
                )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": full_name,
                "language": {"code": language},
                "components": components,
            },
        }
        return await asyncio.to_thread(self._send_raw, payload)

    async def send_typing(self, to: str, message_id: str) -> None:  # noqa: ARG002
        """Send a read receipt and typing indicator. Silently ignores failures."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        with contextlib.suppress(Exception):  # typing indicator failure is non-fatal
            await asyncio.to_thread(self._send_raw, payload)

    async def mark_read(self, message_id: str) -> None:
        """No-op — EUM Social uses send_typing for read receipts."""

    async def upload_media_from_s3(self, bucket: str, key: str) -> str:
        """Upload media from S3 to WhatsApp via EUM Social. Returns the media ID.

        The ``bucket``/``key`` can originate from agent-produced rich-message
        links (see webhook ``_upload_media_if_needed``), so they are treated as
        untrusted and validated against this stack's own media bucket + key
        prefix before use. This blocks a prompt-injected agent from
        exfiltrating an arbitrary S3 object into a WhatsApp message via a
        cross-bucket link. When no media bucket is configured the upload is
        rejected (see fail-closed below).

        The allowed keys are the three roots the toolkit writes media to, under
        the stack's ``MEDIA_PREFIX`` (empty for a dedicated per-stack bucket):
        ``voice/`` (voice-reply), ``images/`` (generate-image), and ``media/``
        (EUM inbound media) — the same roots the notification role may read.
        Anything else under the prefix (conversation data, for one) is
        rejected, as is any key outside those roots or a different bucket.

        Fail-closed: this provider is only constructed for real (non-dev)
        deployments — the ``socialagent dev`` REPL uses a separate ``_DevProvider`` — so
        a stack that reaches here with no media bucket configured is a
        misconfiguration, not a dev session. Rather than silently skip the
        allowlist (which would let any bucket/key through), we reject the upload
        and log a warning so the operator can set ``CONVERSATION_LOGS_BUCKET``.
        """
        allowed_bucket = self._conversation_logs_bucket
        if not allowed_bucket:
            logger.warning(
                "Rejected media upload: no media bucket configured "
                "(CONVERSATION_LOGS_BUCKET unset) — cannot validate S3 source, failing closed. "
                "bucket=%s key=%s",
                bucket,
                key,
            )
            return ""
        prefix = self._media_prefix.strip("/")
        base = f"{prefix}/" if prefix else ""
        allowed_key_roots = tuple(f"{base}{root}/" for root in _MEDIA_UPLOAD_ROOTS)
        # Reject path traversal and out-of-root / cross-bucket keys.
        if (
            bucket != allowed_bucket
            or ".." in key
            or not key.startswith(allowed_key_roots)
        ):
            logger.warning(
                "Rejected media upload from disallowed S3 location bucket=%s key=%s "
                "(allowed bucket=%s key roots=%r)",
                bucket,
                key,
                allowed_bucket,
                allowed_key_roots,
            )
            return ""

        def _upload() -> str:
            resp = self._client.post_whatsapp_message_media(
                originationPhoneNumberId=self._phone_number_id,
                sourceS3File={"bucketName": bucket, "key": key},
            )
            return resp.get("mediaId", "")

        return await asyncio.to_thread(_upload)

    async def download_media(
        self, media_id: str, max_bytes: int | None = None
    ) -> bytes:
        """Download media bytes from EUM Social by media ID.

        EUM Social stores the file in S3, then we read it back. With
        ``max_bytes``, an object over the cap raises :class:`MediaTooLarge`
        from the listing, before any byte is read into memory; the staged
        object is deleted either way.
        """
        bucket = self._transcription_bucket or self._conversation_logs_bucket
        if not bucket:
            raise RuntimeError(
                "No S3 bucket configured for media download (set TRANSCRIPTION_BUCKET)"
            )
        s3 = boto3.client("s3")
        prefix = self._media_prefix.rstrip("/")
        media_root = f"{prefix}/media/" if prefix else "media/"

        def _download():
            # key is a PREFIX — EUM Social appends mediaId + extension
            key_prefix = f"{media_root}{media_id}"
            self._client.get_whatsapp_message_media(
                mediaId=media_id,
                originationPhoneNumberId=self._phone_number_id,
                destinationS3File={"bucketName": bucket, "key": media_root},
            )
            # Find the file — EUM appends extension we don't know in advance
            for _ in range(10):
                try:
                    objects = s3.list_objects_v2(
                        Bucket=bucket, Prefix=key_prefix, MaxKeys=1
                    )
                    contents = objects.get("Contents", [])
                    if contents:
                        actual_key = contents[0]["Key"]
                        size = int(contents[0].get("Size", 0))
                        if max_bytes is not None and size > max_bytes:
                            with contextlib.suppress(Exception):
                                s3.delete_object(Bucket=bucket, Key=actual_key)
                            raise MediaTooLarge(size, max_bytes)
                        resp = s3.get_object(Bucket=bucket, Key=actual_key)
                        data = resp["Body"].read()
                        with contextlib.suppress(Exception):
                            s3.delete_object(Bucket=bucket, Key=actual_key)
                        return data
                except MediaTooLarge:
                    raise
                except Exception:  # noqa: BLE001, S110
                    pass
                time.sleep(1)
            raise RuntimeError(f"Media not found after retries: {key_prefix}*")

        return await asyncio.to_thread(_download)

    # ── Template sync ─────────────────────────────────────────────────────────

    def _get_waba_id(self) -> str:
        """Return the WABA ID, fetching it from EUM Social if not yet cached."""
        if not self._waba_id:
            resp = self._client.get_linked_whatsapp_business_account_phone_number(
                id=self._phone_number_id,
            )
            self._waba_id = resp["linkedWhatsAppBusinessAccountId"]
        return self._waba_id

    async def list_registered_templates(self, prefix: str = "") -> list[dict]:
        """Return all templates registered with the WABA, optionally filtered by prefix.

        Paginates through all results.
        """

        def _list() -> list[dict]:
            waba_id = self._get_waba_id()
            results: list[dict] = []
            kwargs: dict = {"id": waba_id, "maxResults": 100}
            while True:
                resp = self._client.list_whatsapp_message_templates(**kwargs)
                for t in resp.get("templates", []):
                    name: str = t.get("templateName", "")
                    if not prefix or name.startswith(prefix):
                        results.append(
                            {
                                "name": name,
                                "metaTemplateId": t.get("metaTemplateId", ""),
                                "category": t.get("templateCategory", ""),
                                "language": t.get("templateLanguage", ""),
                                "status": t.get("templateStatus", ""),
                            }
                        )
                next_token = resp.get("nextToken")
                if not next_token:
                    break
                kwargs["nextToken"] = next_token
            return results

        return await asyncio.to_thread(_list)

    def _require_approved(self, full_name: str, language: str) -> None:
        """Raise TemplateNotApprovedError unless the WABA lists the template
        APPROVED in ``language``. The listing is read page by page up to the
        template, and a lookup that fails before reaching it lets the send go
        ahead unchecked, so a missing grant or a throttle loses no message that
        went out before the check existed. A template this provider (one per
        Lambda execution environment) refused in the last
        ``_REFUSED_TEMPLATE_TTL_SECONDS`` is refused again from the remembered
        refusal, with no lookup, so a throttle in that time cannot let it out."""
        key = (full_name, language)
        now = time.monotonic()
        if self._approved_until.get(key, 0.0) > now:
            return
        refused = self._refused_until.get(key)
        if refused and refused[0] > now:
            self._refuse(full_name, language, refused[1])
        status: str | None = None
        try:
            if not self._waba_id:
                self._waba_id = self._lookup_client.get_linked_whatsapp_business_account_phone_number(
                    id=self._phone_number_id
                )["linkedWhatsAppBusinessAccountId"]
            kwargs: dict = {"id": self._waba_id, "maxResults": 100}
            while status is None:
                resp = self._lookup_client.list_whatsapp_message_templates(**kwargs)
                status = next(
                    (
                        t.get("templateStatus", "")
                        for t in resp.get("templates", [])
                        if t.get("templateName") == full_name
                        and t.get("templateLanguage") == language
                    ),
                    None,
                )
                if not resp.get("nextToken"):
                    break
                kwargs["nextToken"] = resp["nextToken"]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Could not check template %s (%s) before sending it, sending it unchecked: %s: %s",
                full_name,
                language,
                type(e).__name__,
                mask_phone(str(e)),
            )
            return
        if status != "APPROVED":
            self._remember_refusal(key, status or "")
            self._refuse(full_name, language, status or "")
        self._approved_until[key] = time.monotonic() + _APPROVED_TEMPLATE_TTL_SECONDS

    def _remember_refusal(self, key: tuple[str, str], status: str) -> None:
        """Refuse *key* without a lookup for ``_REFUSED_TEMPLATE_TTL_SECONDS``,
        keeping at most ``_REFUSED_TEMPLATE_MAX_ENTRIES`` refusals."""
        now = time.monotonic()
        # The lock keeps a send in another thread from changing the dict while
        # this one drops entries from its front.
        with self._refused_lock:
            refused = self._refused_until
            refused.pop(key, None)
            while refused and (
                next(iter(refused.values()))[0] <= now
                or len(refused) >= _REFUSED_TEMPLATE_MAX_ENTRIES
            ):
                del refused[next(iter(refused))]
            refused[key] = (now + _REFUSED_TEMPLATE_TTL_SECONDS, status)

    @staticmethod
    def _refuse(full_name: str, language: str, status: str) -> NoReturn:
        error = TemplateNotApprovedError(full_name, language, status)
        # Every caller sees the refusal in the log, including one that falls
        # back without logging it.
        logger.warning("%s: not sending it", error)
        raise error

    async def register_template(self, template: dict, prefix: str = "") -> None:
        """Create or update a template with the WABA.

        The ``templateDefinition`` blob is the full Meta template JSON
        (name, category, language, components). Applies ``prefix`` to the name.
        """
        definition = dict(template)
        if prefix:
            definition["name"] = f"{prefix}{definition['name']}"

        # Strip internal-only keys not accepted by Meta API (top-level and per-component)
        definition.pop("variables", None)
        definition["components"] = [
            {k: v for k, v in c.items() if k != "variables"}
            for c in definition.get("components", [])
        ]

        def _create() -> None:
            self._client.create_whatsapp_message_template(
                id=self._get_waba_id(),
                templateDefinition=json.dumps(definition).encode(),
            )

        await asyncio.to_thread(_create)

    async def delete_template(self, name: str) -> None:
        """Delete a template from the WABA by name."""

        def _delete() -> None:
            self._client.delete_whatsapp_message_template(
                id=self._get_waba_id(),
                templateName=name,
                deleteAllLanguages=True,
            )

        await asyncio.to_thread(_delete)


# ── SNS signature verification ────────────────────────────────────────────────

_SNS_SIGNED_KEYS_NOTIFICATION = (
    "Message",
    "MessageId",
    "Subject",
    "SubscribeURL",
    "Timestamp",
    "TopicArn",
    "Type",
)
_SNS_SIGNED_KEYS_SUBSCRIPTION = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)

# SNS serves its signing certs from the regional endpoint, e.g.
# https://sns.us-east-1.amazonaws.com/SimpleNotificationService-<id>.pem, with a
# .amazonaws.com.cn host in the China partitions.
# The middle label must be region-shaped (us-east-1, us-gov-west-1, cn-north-1):
# a bare [a-z0-9-]+ would also admit S3 hosts such as sns.s3.amazonaws.com,
# where whoever owns a bucket named "sns" could serve the signing key.
_SNS_CERT_HOST = re.compile(r"sns\.[a-z]{2}(-[a-z]+)+-\d+\.amazonaws\.com(\.cn)?")
_SNS_CERT_FETCH_TIMEOUT_SECONDS = 5
# Each cert has its own URL and rotates rarely, so a few entries cover every
# region a warm Lambda hears from; the bound stops the cache growing past that.
_SNS_CERT_CACHE_SIZE = 8


def _sns_cert_fetch_url(cert_url: str) -> str | None:
    """Return the URL to fetch an SNS signing cert from, or None if it is not one.

    The URL is parsed, not string-matched: a prefix plus substring test passed
    ``https://sns.example.org/.amazonaws.com/cert.pem``, which lets a forger pick
    the cert their own signature is checked against. The URL returned is rebuilt
    from the validated parts, so the fetch goes to exactly the host checked here.
    """
    try:
        parts = urllib.parse.urlsplit(cert_url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or parts.username is not None
        or port not in (None, 443)
        or not _SNS_CERT_HOST.fullmatch(host)
        or not parts.path.endswith(".pem")
    ):
        return None
    return f"https://{host}{parts.path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so the cert comes from the validated host or not at all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_SNS_CERT_OPENER = urllib.request.build_opener(_NoRedirect)


@functools.lru_cache(maxsize=_SNS_CERT_CACHE_SIZE)
def _sns_signing_key(fetch_url: str) -> CertificatePublicKeyTypes:
    """Fetch the signing cert at ``fetch_url`` and return its public key.

    Cached by URL, so a warm Lambda fetches each cert once instead of once per
    message. A failed fetch raises, and lru_cache does not cache exceptions, so
    the next message retries it.
    """
    # https only and the host is validated by _sns_cert_fetch_url; a redirect
    # to any other host is refused rather than followed.
    with _SNS_CERT_OPENER.open(
        fetch_url, timeout=_SNS_CERT_FETCH_TIMEOUT_SECONDS
    ) as resp:
        cert_pem = resp.read()
    return load_pem_x509_certificate(cert_pem).public_key()


def _verify_sns_signature(sns: dict) -> bool:
    """Verify the SNS message signature.

    Fails **closed**: returns ``True`` only on a cryptographically valid
    signature. This function is only called for messages that already carry an
    ``Sns`` envelope (see ``parse_webhook``), so a missing/invalid signature or
    a cert-fetch/crypto error means the message is not a verifiable SNS
    notification and is rejected. A prior version returned ``True`` on
    missing fields and on crypto errors, allowing trivially forged or
    unverifiable messages through.
    """
    # SNS spells the cert-URL key differently per delivery protocol: HTTP/HTTPS
    # deliveries use "SigningCertURL"/"UnsubscribeURL" (all-caps URL), while
    # Lambda (and SQS) deliveries use "SigningCertUrl"/"UnsubscribeUrl" (camelCase
    # Url). This topic fans out to Lambda, so accept both spellings — reading only
    # the all-caps form drops 100% of real Lambda-delivered notifications.
    cert_url = sns.get("SigningCertURL") or sns.get("SigningCertUrl") or ""
    signature_b64 = sns.get("Signature", "")
    msg_type = sns.get("Type", "")

    if not cert_url or not signature_b64:
        # An SNS envelope with no signature fields cannot be verified — reject.
        # Real SNS notifications always include Signature + SigningCertURL.
        logger.warning("SNS message missing Signature/SigningCertURL — rejecting")
        return False

    fetch_url = _sns_cert_fetch_url(cert_url)
    if fetch_url is None:
        logger.warning("SNS cert URL is not an SNS endpoint: %r", cert_url)
        return False

    # Canonical string: include a signed key only when it is actually present
    # AND non-null. Lambda-delivered envelopes carry every optional key (e.g.
    # "Subject") with a JSON null value when it was not set at publish time;
    # SNS does NOT include those in the signed string. Testing key presence
    # alone appended "Subject\nNone\n" and broke verification for every message.
    keys = (
        _SNS_SIGNED_KEYS_SUBSCRIPTION
        if msg_type == "SubscriptionConfirmation"
        else _SNS_SIGNED_KEYS_NOTIFICATION
    )
    string_to_sign = "".join(
        f"{k}\n{sns[k]}\n" for k in keys if sns.get(k) is not None
    ).encode()

    try:
        pub_key = _sns_signing_key(fetch_url)
        pub_key.verify(
            base64.b64decode(signature_b64),
            string_to_sign,
            padding.PKCS1v15(),
            hashes.SHA1(),  # noqa: S303 — SNS uses SHA1
        )
    except Exception:
        # Cert fetch / decode / signature mismatch — fail closed. A
        # transient cert-fetch error will be retried by SNS's own delivery
        # retries; we do not trade authenticity for availability here.
        logger.exception("SNS signature verification error — rejecting")
        return False
    else:
        return True
