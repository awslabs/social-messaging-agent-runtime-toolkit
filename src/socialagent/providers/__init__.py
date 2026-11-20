# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Abstract base class for all SMART messaging providers.
"""

from abc import ABC, abstractmethod

from socialagent.types import InboundMessage, OutboundMessage, StatusUpdate


class TemplateNotApprovedError(RuntimeError):
    """A template send refused because Meta has not approved the template.

    EUM Social accepts a send of a template that is not approved, and WhatsApp
    drops it later, reported only as a failed delivery status. Refusing it
    before the send lets the caller fall back, for example to text.

    ``name`` is the full template name, with the stack's prefix. ``status`` is
    the one the WhatsApp Business Account lists, or ``""`` when it does not
    list the template in ``language``.
    """

    def __init__(self, name: str, language: str, status: str) -> None:
        super().__init__(
            f"template {name} ({language}) is {status or 'not on the WhatsApp Business Account'}, not APPROVED"
        )
        self.name = name
        self.language = language
        self.status = status


class MessageProvider(ABC):
    """Messaging provider interface.

    All provider implementations must implement every method here.
    Template sync methods (list/register/delete) are required even for
    providers that don't support sync yet — return empty/no-op stubs.
    """

    @abstractmethod
    async def parse_webhook(
        self, raw_event: dict
    ) -> InboundMessage | StatusUpdate | None:
        """Parse a raw Lambda event into an :class:`InboundMessage` or :class:`StatusUpdate`.

        The ``raw_event`` is the Lambda event dict as received by the webhook handler.
        For EUM Social this is an SNS-wrapped payload; other providers may differ.

        Return values:
        - :class:`InboundMessage` — a user sent a message (text, media, reaction, etc.)
        - :class:`StatusUpdate` — WhatsApp reported a delivery/read status for a message we sent
        - ``None`` — the event is not actionable (malformed, signature failure, empty payload)

        Implementors must handle signature verification, payload unwrapping, and
        normalisation to provider-agnostic types. The webhook handler routes the
        result: messages go to the agent, status updates go to ``@app.on_status``,
        ``None`` is silently discarded.
        """

    @abstractmethod
    async def send_text(self, to: str, text: str) -> str:
        """Send a plain-text message. Returns the provider message ID."""

    @abstractmethod
    async def send_message(self, to: str, message: OutboundMessage) -> str:
        """Send any :class:`OutboundMessage`. Returns the provider message ID."""

    @abstractmethod
    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        params: list[str],
        header_params: list[str] | None = None,
        button_payloads: list[str] | None = None,
    ) -> str:
        """Send a pre-approved template message. Returns the provider message ID.

        Raises:
            TemplateNotApprovedError: the template is known not to be approved
                in ``language``, so nothing was sent.
        """

    @abstractmethod
    async def send_typing(self, to: str, message_id: str) -> None:
        """Send a typing indicator / read receipt. Failures are non-fatal."""

    @abstractmethod
    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read."""

    @abstractmethod
    async def upload_media_from_s3(self, bucket: str, key: str) -> str:
        """Upload media from S3 to the provider and return a provider media ID."""

    @abstractmethod
    async def download_media(self, media_id: str) -> bytes:
        """Download media bytes by provider media ID."""

    @abstractmethod
    async def list_registered_templates(self, prefix: str = "") -> list[dict]:
        """Return all templates currently registered with the provider."""

    @abstractmethod
    async def register_template(self, template: dict, prefix: str = "") -> None:
        """Create or update a single template with the provider."""

    @abstractmethod
    async def delete_template(self, name: str) -> None:
        """Delete a template from the provider."""
