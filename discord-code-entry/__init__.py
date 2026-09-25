"""Answer the browser vault's code and save-login prompts from Discord through a private modal instead of the chat.

Overrides the built-in ``browser_vault_enter_code`` and ``browser_vault_save_login`` with the same schemas. In a
live Discord session each override installs a prompt that posts a button, then runs the built-in tool unchanged:
an authenticator key still mints the code without asking, and the answer still goes straight into the vault or
the page. Every other surface calls the built-in handler directly.
"""

from __future__ import annotations

import logging
import weakref

from . import discord_ui, prompt
from .broker import Broker
from .discord_ui import DropCodeSubmitPayloads

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    if not ctx.has_capability("tools.override"):
        logger.warning("discord-code-entry needs the tools.override capability to replace the browser vault "
                       "prompts; not active. Grant it with `hermes plugins enable discord-code-entry`.")
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

    def override(schema, builtin, slot, make_prompt) -> None:
        def handler(args, **kwargs):
            binding = prompt.current_binding(bots)
            if binding is None:
                return builtin(args, **kwargs)
            with prompt.prompt_installed(slot, make_prompt(binding)):
                return builtin(args, **kwargs)

        ctx.register_tool(name=schema["name"], toolset="browser", schema=schema, handler=handler,
                          check_fn=vault._check_vault_available, emoji="🔐", override=True)

    override(vault.BROWSER_VAULT_ENTER_CODE_SCHEMA, vault._handle_vault_enter_code, "code",
             lambda binding: lambda site, _hint: prompt.ask(broker, binding, discord_ui.CODE, site))
    override(vault.BROWSER_VAULT_SAVE_LOGIN_SCHEMA, vault._handle_vault_save_login, "save_login",
             lambda binding: lambda _origin, host: prompt.ask(broker, binding, discord_ui.LOGIN, host))

    gateway_log = logging.getLogger("discord.gateway")
    log_filter = DropCodeSubmitPayloads()
    gateway_log.addFilter(log_filter)

    def unload() -> None:
        broker.cancel_all()
        gateway_log.removeFilter(log_filter)
        bots.clear()

    ctx.on_unload(unload)
