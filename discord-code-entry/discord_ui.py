"""Discord side: the prompt button, its modal, and the message lifecycle.

Everything here runs on the bot's event loop. Interaction checks are in-memory so every tap and submit is
answered well inside Discord's 3-second window. No reply, edit or log line carries a submitted value.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from . import broker as b

logger = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "hermes-code-entry:"
_CODE_RE = re.compile(r"[A-Za-z0-9]{4,12}")

_REJECTIONS = {
    b.GONE: "This prompt has expired or was already used. Nothing was entered.",
    b.WRONG_USER: "Only the person who started this sign-in can use this prompt.",
    b.WRONG_CHANNEL: "This prompt belongs to a different conversation.",
}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    placeholder: str
    min_length: int
    max_length: int


@dataclass(frozen=True)
class Kind:
    """One kind of prompt: what the message, button and modal say, and how a submission is parsed.
    ``parse`` returns (value, None) or (None, reason shown to the user)."""
    emoji: str
    button: str
    modal_title: str
    fields: Tuple[Field, ...]
    parse: Callable[[Dict[str, str]], Tuple[Any, Optional[str]]]
    ask: str
    submitted: str
    expired: str
    cancelled: str
    accepted: str


def normalize_code(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "")


def is_plausible_code(code: str) -> bool:
    return bool(_CODE_RE.fullmatch(code))


def _parse_code(values):
    code = normalize_code(values["code"])
    if not is_plausible_code(code):
        return None, "That doesn't look like a verification code (4 to 12 letters or digits)."
    return code, None


def _parse_login(values):
    identifier, password = values["identifier"].strip(), values["password"]
    if not identifier or not password:
        return None, "Both the username and the password are needed."
    return {"identifier": identifier, "password": password}, None


CODE = Kind(
    emoji="🔐", button="Enter code", modal_title="Code for {site}",
    fields=(Field("code", "Verification code", "123456", 4, 20),),
    parse=_parse_code,
    ask=("**{site}** is asking for a verification code.\n"
         "Tap **Enter code** to type it into a private form. The code is not posted in this channel, "
         "but Discord still processes what you submit."),
    submitted="🔐 Verification code for **{site}** received.",
    expired="⌛ Code prompt for **{site}** expired. Nothing was entered.",
    cancelled="⏹ Code prompt for **{site}** was cancelled. Nothing was entered.",
    accepted="✅ Code sent to the waiting sign-in.",
)

LOGIN = Kind(
    emoji="🔑", button="Save login", modal_title="Login for {site}",
    fields=(Field("identifier", "Username, email or phone", "you@example.com", 1, 320),
            Field("password", "Password", "Not masked while you type", 1, 1024)),
    parse=_parse_login,
    ask=("Save a login for **{site}**?\n"
         "Tap **Save login** to enter your username and password in a private form. Hermes stores them in "
         "your vault and fills the password. They are not posted in this channel, but Discord still processes "
         "what you submit, and Discord forms don't mask the password while you type."),
    submitted="🔑 Login for **{site}** received.",
    expired="⌛ Login prompt for **{site}** expired. Nothing was saved.",
    cancelled="⏹ Login prompt for **{site}** was cancelled. Nothing was saved.",
    accepted="✅ Login sent to the waiting sign-in. Hermes saves it to your vault and fills the password.",
)


def prompt_text(kind: Kind, pending: b.Pending, site: str, expires_at: float) -> str:
    return f"{kind.emoji} <@{pending.user_id}> {kind.ask.format(site=site)}\nExpires <t:{int(expires_at)}:R>."


def final_text(kind: Kind, outcome: str, site: str) -> str:
    text = {b.SUBMITTED: kind.submitted, b.EXPIRED: kind.expired}.get(outcome, kind.cancelled)
    return text.format(site=site)


async def post(bot, broker: b.Broker, kind: Kind, pending: b.Pending, site: str, timeout: float) -> None:
    import discord

    channel = bot.get_channel(pending.channel_id) or await bot.fetch_channel(pending.channel_id)
    # The view outlives the wait slightly; the tool thread normally removes it first.
    view = _view_classes()[0](broker, kind, pending, site, timeout=timeout + 30)
    pending.view = view
    pending.message = await channel.send(
        prompt_text(kind, pending, site, time.time() + timeout), view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    pending.message_id = pending.message.id
    if pending.event.is_set():  # the wait ended while the send was in flight
        await finalize(kind, pending, site, pending.outcome or b.CANCELLED)


async def finalize(kind: Kind, pending: b.Pending, site: str, outcome: str) -> None:
    view, message = pending.view, pending.message
    if view is not None:
        view.stop()
    if message is None:
        return
    try:
        await message.edit(content=final_text(kind, outcome, site), view=None)
    except Exception as exc:
        logger.debug("prompt %s: could not update message (%s)", pending.prompt_id, type(exc).__name__)


async def _reply(interaction, text: str) -> None:
    await interaction.response.send_message(text, ephemeral=True)


def _ids(interaction):
    user = getattr(interaction, "user", None)
    return getattr(user, "id", None), getattr(interaction, "channel_id", None)


@functools.cache
def _view_classes():
    import discord

    class PromptModal(discord.ui.Modal):
        def __init__(self, broker: b.Broker, kind: Kind, pending: b.Pending, site: str, timeout: float):
            super().__init__(title=kind.modal_title.format(site=site)[:45], timeout=timeout,
                             custom_id=f"{CUSTOM_ID_PREFIX}{pending.prompt_id}:modal")
            self.broker, self.kind, self.pending = broker, kind, pending
            self.inputs = {}
            for f in kind.fields:
                self.inputs[f.key] = discord.ui.TextInput(label=f.label, placeholder=f.placeholder, required=True,
                                                          min_length=f.min_length, max_length=f.max_length)
                self.add_item(self.inputs[f.key])

        async def on_submit(self, interaction):
            value, problem = self.kind.parse({key: item.value or "" for key, item in self.inputs.items()})
            if problem is not None:
                await _reply(interaction, f"{problem} Tap **{self.kind.button}** to try again.")
                return
            user_id, channel_id = _ids(interaction)
            rejection = self.broker.submit(self.pending.prompt_id, user_id=user_id, channel_id=channel_id, value=value)
            if rejection is not None:
                logger.info("prompt %s: submission rejected (%s)", self.pending.prompt_id, rejection)
                await _reply(interaction, _REJECTIONS[rejection])
                return
            logger.info("prompt %s: submitted", self.pending.prompt_id)
            await _reply(interaction, self.kind.accepted)

        async def on_error(self, interaction, error):
            # Replaces discord.py's default handler so a failure is answered and logged by type only.
            logger.warning("prompt %s: modal error (%s)", self.pending.prompt_id, type(error).__name__)
            if not interaction.response.is_done():
                await _reply(interaction, f"Something went wrong. Nothing was entered; tap **{self.kind.button}** to retry.")

    class PromptButtonView(discord.ui.View):
        def __init__(self, broker: b.Broker, kind: Kind, pending: b.Pending, site: str, timeout: float):
            super().__init__(timeout=timeout)
            self.broker, self.kind, self.pending, self.site = broker, kind, pending, site
            self.deadline = time.monotonic() + timeout
            button = discord.ui.Button(label=kind.button, emoji=kind.emoji, style=discord.ButtonStyle.primary,
                                       custom_id=f"{CUSTOM_ID_PREFIX}{pending.prompt_id}")
            button.callback = self.open_modal
            self.add_item(button)

        async def open_modal(self, interaction):
            user_id, channel_id = _ids(interaction)
            message_id = getattr(getattr(interaction, "message", None), "id", None)
            rejection = self.broker.check(self.pending.prompt_id, user_id=user_id, channel_id=channel_id,
                                          message_id=message_id)
            if rejection is not None:
                logger.info("prompt %s: tap rejected (%s)", self.pending.prompt_id, rejection)
                await _reply(interaction, _REJECTIONS[rejection])
                return
            remaining = max(1.0, self.deadline - time.monotonic())
            await interaction.response.send_modal(PromptModal(self.broker, self.kind, self.pending, self.site, remaining))

        async def on_error(self, interaction, error, item):
            logger.warning("prompt %s: button error (%s)", self.pending.prompt_id, type(error).__name__)
            if not interaction.response.is_done():
                await _reply(interaction, "Something went wrong. Nothing was entered.")

    return PromptButtonView, PromptModal


class DropCodeSubmitPayloads(logging.Filter):
    """discord.py logs every raw gateway event at DEBUG (``hermes -v``), which would include the modal's field
    values. Drop the raw record for submissions of this plugin's modals; everything else passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "For Shard ID %s: WebSocket Event: %s":
            return True
        args = record.args if isinstance(record.args, tuple) else ()
        payload = args[1] if len(args) > 1 else None
        if not isinstance(payload, dict) or payload.get("t") != "INTERACTION_CREATE":
            return True
        data = (payload.get("d") or {}).get("data") or {}
        return not str(data.get("custom_id", "")).startswith(CUSTOM_ID_PREFIX)
