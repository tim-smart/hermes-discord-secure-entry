"""Tool-thread side: bind a prompt to the current Discord session, post it, and wait for the code.

The worker thread blocks here (never the event loop). The wait ends on a submission, /stop, the approval
timeout, a disconnected adapter, or plugin unload, whichever comes first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import broker as b
from . import discord_ui

logger = logging.getLogger(__name__)

_POST_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class Binding:
    user_id: int
    channel_id: int
    adapter: Any
    bot: Any

    def connected(self) -> bool:
        return bool(self.adapter.is_connected) and not self.bot.is_closed()


def _live_discord_adapter():
    # The profile-aware resolver tools use for the session's own bot (multiplex never falls back to another
    # profile's bot).
    from gateway.config import Platform
    from tools.send_message_senders import _live_adapter

    return _live_adapter(Platform.DISCORD)[1]


def current_binding(bots: Mapping[Any, Any]) -> Optional[Binding]:
    """The Discord user, channel and bot of the turn running on this thread; None outside a live Discord
    session (the built-in behaviour then applies unchanged)."""
    from gateway.session_context import get_session_env

    if get_session_env("HERMES_SESSION_PLATFORM") != "discord":
        return None
    user_id = get_session_env("HERMES_SESSION_USER_ID")
    channel_id = get_session_env("HERMES_SESSION_THREAD_ID") or get_session_env("HERMES_SESSION_CHAT_ID")
    if not (user_id.isdigit() and channel_id.isdigit()):
        return None
    adapter = _live_discord_adapter()
    bot = bots.get(adapter) if adapter is not None else None
    if bot is None:
        return None
    binding = Binding(user_id=int(user_id), channel_id=int(channel_id), adapter=adapter, bot=bot)
    return binding if binding.connected() else None


@contextmanager
def code_prompt_installed(prompt: Callable[[str, str], str]):
    """Install ``prompt`` as this thread's vault code prompt for one tool call."""
    from agent.vault_backends import unlock

    previous_code, previous_unlock = unlock.get_code_prompt_callback(), unlock.get_unlock_prompt_callback()
    unlock.set_code_prompt_callback(prompt)
    if previous_unlock is None:
        # can_prompt_here() treats the unlock slot as "a human can answer". Discord has no master-password
        # prompt, so a decliner stands in for this call only; browser_vault_enter_code never unlocks.
        unlock.set_unlock_prompt_callback(lambda _backend, _display_name: "")
    try:
        yield
    finally:
        unlock.set_code_prompt_callback(previous_code)
        unlock.set_unlock_prompt_callback(previous_unlock)


def _timeout_s() -> float:
    from tools.approval_context import _get_approval_timeout

    return float(_get_approval_timeout())


def ask(broker: b.Broker, binding: Binding, site: str) -> str:
    """Post the button, wait for the user's code, and return it ("" when none arrived)."""
    timeout = _timeout_s()
    pending = broker.open(user_id=binding.user_id, channel_id=binding.channel_id)
    loop = binding.bot.loop
    outcome = b.CANCELLED
    try:
        posting = asyncio.run_coroutine_threadsafe(
            discord_ui.post(binding.bot, broker, pending, site, timeout), loop)
        try:
            posting.result(timeout=_POST_TIMEOUT_S)
        except Exception as exc:
            posting.cancel()
            logger.warning("code prompt %s: could not post (%s)", pending.prompt_id, type(exc).__name__)
            raise RuntimeError("Could not post the verification-code button in Discord. Nothing was entered.") from None
        outcome = _wait(pending, binding, timeout)
    finally:
        outcome = broker.finish(pending, outcome)
        logger.info("code prompt %s: %s", pending.prompt_id, outcome)
        if pending.message is not None:
            try:
                asyncio.run_coroutine_threadsafe(discord_ui.finalize(pending, site, outcome), loop)
            except RuntimeError:
                pass  # loop already closed: the bot is gone with its message state
    return pending.take_code() if outcome == b.SUBMITTED else ""


def _wait(pending: b.Pending, binding: Binding, timeout: float) -> str:
    from tools.approval_human_wait import activity_heartbeat, human_wait_window
    from tools.interrupt import is_interrupted

    deadline = time.monotonic() + timeout
    heartbeat = activity_heartbeat("waiting for the user's verification code")
    # Human-wait time is excluded from the tool batch deadline, as for approval prompts.
    with human_wait_window():
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b.EXPIRED
            if pending.event.wait(timeout=min(1.0, remaining)):
                return pending.outcome or b.CANCELLED
            if is_interrupted():
                return b.STOPPED
            if not binding.connected():
                return b.DISCONNECTED
            heartbeat()
