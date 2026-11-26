# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Personal assistant agent — general-purpose AI with memory and media support.

Deployed to AgentCore Runtime via Terraform.
"""

from datetime import datetime, timezone

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from socialagent_runtime import (
    create_session_manager,
    socialagent_notify_context,
    SocialAgentResponse,
)
from socialagent_runtime.config import config

from src.model import MODEL_ID
from src.skills import load_skill_index
from src.tools import RECURRING_OPERATIONS, in_scheduled_task, socialagent_tools

app = BedrockAgentCoreApp()


def _model_kwargs() -> dict:
    """BedrockModel kwargs, adding the Bedrock Guardrail when one is configured.

    When BEDROCK_GUARDRAIL_ID is set (Terraform sets it from terraform.guardrail
    in socialagent.<env>.yaml: the stack's guardrail, or one of your own; see
    "Guardrails" in docs/configuration.md in the SMART repo), the guardrail is
    enforced on every model call. Without it (terraform.guardrail: none) no
    guardrail arg is passed.
    """
    kwargs = {"model_id": MODEL_ID}
    if config.guardrail_enabled:
        kwargs["guardrail_id"] = config.guardrail_id
        kwargs["guardrail_version"] = config.guardrail_version or "DRAFT"
    return kwargs


def _current_time_context() -> str:
    """Build fresh time context per invocation."""
    now = datetime.now(timezone.utc).astimezone()
    return f"Current date/time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')} (epoch {int(now.timestamp())})"


SYSTEM_PROMPT_TEMPLATE = """You are a helpful WhatsApp assistant. Your responses go directly \
as WhatsApp messages — keep them concise and conversational.

{time_context}

FORMATTING RULES (WhatsApp, NOT standard markdown):
- Bold: *text* (single asterisk, NOT **text**)
- Italic: _text_ (underscore)
- Strikethrough: ~text~
- Monospace: ```text```
- NEVER use **double asterisks**, # headers, [links](url), or any standard markdown
- Use emojis naturally
- Break long responses into short paragraphs

If the user sends audio, you'll receive the transcription.
If the user sends an image or document, acknowledge it.

CONVERSATION HISTORY: earlier turns are restored as text only; the tool calls behind them are not shown. A past reply such as "done, I saved that" or "scheduled" was true when it was sent. Never conclude that an earlier action did not happen because its tool call is not in the history, and never apologize for actions you did take: check with the relevant tool if you need the current state, and answer from that.

Always respond in the user's language. Load a skill with `load_skill(name)` when you need detailed usage.

{skill_index}"""

# Added for a scheduled task (handlers/scheduler.py), whose user turn is the
# task alone. These rules live in the system prompt because a Bedrock guardrail
# does not evaluate it: in the user turn the prompt-attack filter scored them
# as an attempt to override instructions.
SCHEDULED_TASK_RULES = """SCHEDULED TASK: this turn is a task the user scheduled earlier, \
not a new message from them. The user's message is the task description. Do the task \
and reply in the language of the task description, in text only: voice notes, images \
and interactive messages are not delivered from a scheduled task."""

model = BedrockModel(**_model_kwargs())


@app.entrypoint
async def invoke(payload: dict, context: object = None) -> dict:
    """Handle an inbound WhatsApp message."""
    sm = create_session_manager(payload)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        time_context=_current_time_context(),
        skill_index=load_skill_index(accepted=RECURRING_OPERATIONS),
    )
    if in_scheduled_task():
        system_prompt = f"{system_prompt}\n\n{SCHEDULED_TASK_RULES}"
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        session_manager=sm,
        tools=socialagent_tools,
    )
    invocation_state = {"phone": payload.get("phone", "")}
    with socialagent_notify_context() as outbound:
        result = agent(payload["prompt"], invocation_state=invocation_state)
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)


app.run()
