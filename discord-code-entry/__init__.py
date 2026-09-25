"""Enter a website's one-time code from Discord through a private modal instead of the chat.

Overrides the built-in ``browser_vault_enter_code`` with the same schema. In a live Discord session the
override installs a code prompt that posts an "Enter code" button, then runs the built-in tool unchanged:
an authenticator key still mints the code without asking, and the code still goes straight into the page.
Every other surface calls the built-in handler directly.
"""

from __future__ import annotations

import logging
import weakref

from . import prompt
from .broker import Broker
from .discord_ui import DropCodeSubmitPayloads

logger = logging.getLogger(__name__)

_TOOL = "browser_vault_enter_code"


def register(ctx) -> None:
    if not ctx.has_capability("tools.override"):
        logger.warning("discord-code-entry needs the tools.override capability to replace %s; not active. "
                       "Grant it with `hermes plugins enable discord-code-entry`.", _TOOL)
        return

    from tools import browser_vault_tool as vault

    broker = Broker()
    bots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def wire(bot, adapter) -> None:
        bots[adapter] = bot

    # The adapter skips a factory whose (plugin, qualname) it already wired on this bot, so a reloaded
    # plugin needs a fresh qualname to receive the live bot again.
    wire.__qualname__ = f"wire_{id(broker):x}"
    ctx.register_platform_handler("discord", wire)

    def handler(args, **kwargs):
        binding = prompt.current_binding(bots)
        if binding is None:
            return vault._handle_vault_enter_code(args, **kwargs)
        with prompt.code_prompt_installed(lambda site, _hint: prompt.ask(broker, binding, site)):
            return vault._handle_vault_enter_code(args, **kwargs)

    ctx.register_tool(name=_TOOL, toolset="browser", schema=vault.BROWSER_VAULT_ENTER_CODE_SCHEMA,
                      handler=handler, check_fn=vault._check_vault_available, emoji="🔐", override=True)

    gateway_log = logging.getLogger("discord.gateway")
    log_filter = DropCodeSubmitPayloads()
    gateway_log.addFilter(log_filter)

    def unload() -> None:
        broker.cancel_all()
        gateway_log.removeFilter(log_filter)
        bots.clear()

    ctx.on_unload(unload)
