"""Discord side: the "Enter code" button, the code modal, and the message lifecycle.

Everything here runs on the bot's event loop. Interaction checks are in-memory so every tap and submit is
answered well inside Discord's 3-second window. No reply, edit or log line carries the submitted code.
"""

from __future__ import annotations

import functools
import logging
import re
import time

from . import broker as b

logger = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "hermes-code-entry:"
_CODE_RE = re.compile(r"[A-Za-z0-9]{4,12}")

_REJECTIONS = {
    b.GONE: "This code prompt has expired or was already used. Nothing was entered.",
    b.WRONG_USER: "Only the person who started this sign-in can enter its code.",
    b.WRONG_CHANNEL: "This code prompt belongs to a different conversation.",
}

_FINAL_TEXT = {
    b.SUBMITTED: "🔐 Verification code for **{site}** received.",
    b.EXPIRED: "⌛ Code prompt for **{site}** expired. Nothing was entered.",
}
_CANCELLED_TEXT = "⏹ Code prompt for **{site}** was cancelled. Nothing was entered."


def normalize_code(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "")


def is_plausible_code(code: str) -> bool:
    return bool(_CODE_RE.fullmatch(code))


def prompt_text(pending: b.Pending, site: str, expires_at: float) -> str:
    return (f"🔐 <@{pending.user_id}> **{site}** is asking for a verification code.\n"
            "Tap **Enter code** to type it into a private form. The code is not posted in this channel, "
            "but Discord still processes what you submit.\n"
            f"Expires <t:{int(expires_at)}:R>.")


def final_text(outcome: str, site: str) -> str:
    return _FINAL_TEXT.get(outcome, _CANCELLED_TEXT).format(site=site)


async def post(bot, broker: b.Broker, pending: b.Pending, site: str, timeout: float) -> None:
    import discord

    channel = bot.get_channel(pending.channel_id) or await bot.fetch_channel(pending.channel_id)
    # The view outlives the wait slightly; the tool thread normally removes it first.
    view = _view_classes()[0](broker, pending, site, timeout=timeout + 30)
    pending.view = view
    pending.message = await channel.send(
        prompt_text(pending, site, time.time() + timeout), view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    pending.message_id = pending.message.id
    if pending.event.is_set():  # the wait ended while the send was in flight
        await finalize(pending, site, pending.outcome or b.CANCELLED)


async def finalize(pending: b.Pending, site: str, outcome: str) -> None:
    view, message = pending.view, pending.message
    if view is not None:
        view.stop()
    if message is None:
        return
    try:
        await message.edit(content=final_text(outcome, site), view=None)
    except Exception as exc:
        logger.debug("code prompt %s: could not update message (%s)", pending.prompt_id, type(exc).__name__)


async def _reply(interaction, text: str) -> None:
    await interaction.response.send_message(text, ephemeral=True)


def _ids(interaction):
    user = getattr(interaction, "user", None)
    return getattr(user, "id", None), getattr(interaction, "channel_id", None)


@functools.cache
def _view_classes():
    import discord

    class CodeModal(discord.ui.Modal):
        def __init__(self, broker: b.Broker, pending: b.Pending, site: str, timeout: float):
            super().__init__(title=f"Code for {site}"[:45], timeout=timeout,
                             custom_id=f"{CUSTOM_ID_PREFIX}{pending.prompt_id}:modal")
            self.broker, self.pending = broker, pending
            self.code = discord.ui.TextInput(label="Verification code", placeholder="123456",
                                             min_length=4, max_length=20, required=True)
            self.add_item(self.code)

        async def on_submit(self, interaction):
            code = normalize_code(self.code.value)
            if not is_plausible_code(code):
                await _reply(interaction, "That doesn't look like a verification code (4 to 12 letters or digits). "
                                          "Tap **Enter code** to try again.")
                return
            user_id, channel_id = _ids(interaction)
            rejection = self.broker.submit(self.pending.prompt_id, user_id=user_id, channel_id=channel_id, code=code)
            if rejection is not None:
                logger.info("code prompt %s: submission rejected (%s)", self.pending.prompt_id, rejection)
                await _reply(interaction, _REJECTIONS[rejection])
                return
            logger.info("code prompt %s: code submitted", self.pending.prompt_id)
            await _reply(interaction, "✅ Code sent to the waiting sign-in.")

        async def on_error(self, interaction, error):
            # Replaces discord.py's default handler so a failure is answered and logged by type only.
            logger.warning("code prompt %s: modal error (%s)", self.pending.prompt_id, type(error).__name__)
            if not interaction.response.is_done():
                await _reply(interaction, "Something went wrong. Nothing was entered; tap **Enter code** to retry.")

    class CodeButtonView(discord.ui.View):
        def __init__(self, broker: b.Broker, pending: b.Pending, site: str, timeout: float):
            super().__init__(timeout=timeout)
            self.broker, self.pending, self.site = broker, pending, site
            self.deadline = time.monotonic() + timeout
            button = discord.ui.Button(label="Enter code", emoji="🔐", style=discord.ButtonStyle.primary,
                                       custom_id=f"{CUSTOM_ID_PREFIX}{pending.prompt_id}")
            button.callback = self.open_modal
            self.add_item(button)

        async def open_modal(self, interaction):
            user_id, channel_id = _ids(interaction)
            message_id = getattr(getattr(interaction, "message", None), "id", None)
            rejection = self.broker.check(self.pending.prompt_id, user_id=user_id, channel_id=channel_id,
                                          message_id=message_id)
            if rejection is not None:
                logger.info("code prompt %s: tap rejected (%s)", self.pending.prompt_id, rejection)
                await _reply(interaction, _REJECTIONS[rejection])
                return
            remaining = max(1.0, self.deadline - time.monotonic())
            await interaction.response.send_modal(CodeModal(self.broker, self.pending, self.site, remaining))

        async def on_error(self, interaction, error, item):
            logger.warning("code prompt %s: button error (%s)", self.pending.prompt_id, type(error).__name__)
            if not interaction.response.is_done():
                await _reply(interaction, "Something went wrong. Nothing was entered.")

    return CodeButtonView, CodeModal



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
