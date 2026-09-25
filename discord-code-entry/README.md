# discord-code-entry

Enter a website's one-time verification code, or save a new login, from your phone through a Discord button and modal, without posting it in chat.

When `browser_vault_enter_code` needs a code in a Discord session, Hermes posts this in the conversation:

> 🔐 @you **github.com** is asking for a verification code.
> Tap **Enter code** to type it into a private form. The code is not posted in this channel, but Discord still processes what you submit.

Tap **Enter code**, type the code into the modal, and submit. The code goes straight to the waiting browser fill. You get a private (ephemeral) confirmation that doesn't contain the code, and the channel message changes to "received", "expired" or "cancelled".

When `browser_vault_save_login` finds a login page with nothing saved for it, Hermes posts:

> 🔑 @you Save a login for **github.com**?
> Tap **Save login** to enter your username and password in a private form. Hermes stores them in your vault and fills the password. They are not posted in this channel, but Discord still processes what you submit, and Discord forms don't mask the password while you type.

The modal has a username field and a password field. The login is stored in the local vault bound to the page's origin and the password is filled. The model receives the handle and the username, never the password.

## What the modal does and doesn't hide

- **Hidden from:** the channel and its other members, the model, the conversation transcript, tool results, and Hermes logs. Hermes also drops discord.py's raw DEBUG log line for these submissions (`hermes -v` would otherwise print the payload).
- **Not hidden from Discord.** The submission travels through Discord's servers to the bot like any other interaction. Only use this if you're comfortable with Discord seeing what you type. A code is short-lived. A password is not: if you don't want Discord to handle it, add the login with `hermes vault add` or the Desktop app instead.
- **Not masked on screen.** Discord modals have no password field type, so the password is visible while you type it.

## Behaviour

- **Authenticator keys still come first.** If the saved login has an authenticator key, Hermes generates the code and never asks.
- **Tied to one person, one conversation, one tool call.** Only the Discord user whose message started the turn can open or submit the form. It has to come from the channel or thread where the button was posted, and it answers only the tool call that posted it. Other users, other channels, stale buttons and repeat submissions get a private rejection.
- **No chat fallback.** Codes or passwords typed as ordinary channel messages or DMs are never used.
- **The waiting tool call always finishes.** It ends on a submission, `/stop`, `approvals.timeout` (default 300s), the Discord adapter disconnecting, or the plugin unloading. Discord's event loop never blocks. The tool call waits on its own worker thread and sends human-wait heartbeats the same way approval prompts do.
- **Concurrent prompts are independent.** Each prompt has its own button, form and binding.
- **Other surfaces don't change.** CLI, TUI, Desktop, Telegram and every other surface use the built-in tool exactly as before.

## Install

```sh
ln -s "$PWD/discord-code-entry" ~/.hermes/plugins/discord-code-entry
hermes plugins enable discord-code-entry     # consent to the tools.override capability
```

Restart the gateway or start a new session. Tool overrides take effect from the next session.

The plugin replaces `browser_vault_enter_code` and `browser_vault_save_login` with wrappers that keep the built-in schemas byte for byte, so prompt caching is unaffected. Without the `tools.override` grant, the plugin logs a warning and stays inactive.

`hermes plugins validate` reports a "built-in tool collision". That check is the plugin catalog's admission policy and rejects every built-in override whether or not you consented. It doesn't block a local install.

## How it fits Hermes

Nothing in Hermes core is modified. The plugin uses:

| Surface | Use |
| --- | --- |
| `ctx.register_tool(..., override=True)` (consent-gated) | Per-call hook around the built-in tool. Unload restores the built-in. |
| `ctx.register_platform_handler("discord", ...)` | Receives the live discord.py `Bot` to post the button. discord.py routes the button and modal to their views. |
| `agent.vault_backends.unlock.set_code_prompt_callback`, `set_save_login_prompt_callback` | The built-in tools' own per-thread "ask the user" slots. |

It also relies on these internals, which may move:

- `tools.send_message_senders._live_adapter`: profile-aware lookup of the session's Discord adapter.
- `tools.approval_context._get_approval_timeout`, `tools.approval_human_wait`, `tools.interrupt`: timeout, heartbeats and `/stop`, shared with approval prompts.
- `tools.browser_vault_tool._handle_vault_enter_code`, `_handle_vault_save_login`, their schemas and `check_fn`.

It works around two core quirks. Each has a regression test:

1. `can_prompt_here()` decides whether a human can answer by checking for the *unlock* prompt callback. Discord has no master-password prompt, so the plugin installs a decliner in that slot for the duration of the call. Neither overridden tool unlocks. Upstream fix: key `can_prompt_here()` on the prompt the tool actually uses.
2. Adapters skip a handler factory they already wired on the live client, keyed by `(plugin, qualname)`. That stops a reloaded plugin from ever receiving the bot again. The plugin gives each load's factory a unique qualname. Upstream fix: include the load generation in the key, or forget a plugin's keys on unload.

## Tests

```sh
cd vendor/hermes && nix shell nixpkgs#python312 -c python -m pm.build_env --source . --out .venv --group dev --group test --extra discord
cd ../../tests && ../vendor/hermes/.venv/bin/python -m pytest -q
```

The plugin loads through Hermes' real plugin discovery against a temp `HERMES_HOME`. Tool calls dispatch through the real registry on a worker thread bound to a gateway session. Only Discord's API (channel, message, interaction) is faked.
