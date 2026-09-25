"""Behaviour contracts for the Discord code-entry plugin."""

from __future__ import annotations

import json
import logging
import time

import pytest

from conftest import call_tool, interaction

CODE = "482913"


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _posted(env, channel_id=222, count=1):
    _wait_for(lambda: channel_id in env.bot.channels and len(env.bot.channels[channel_id].sent) >= count)
    return env.bot.channels[channel_id].sent[count - 1]


def _tap(env, message, user_id, channel_id):
    button = message.posted_view.children[0]
    tap = interaction(user_id, channel_id, message.id)
    env.bot.run(button.callback(tap))
    return tap


def _submit(env, modal, user_id, channel_id, value):
    modal.code._value = value
    submit = interaction(user_id, channel_id)
    env.bot.run(modal.on_submit(submit))
    return submit.response.replies


def _discord_texts(env):
    texts = []
    for channel in env.bot.channels.values():
        for message in channel.sent:
            texts += [message.content, *message.edits]
    return texts


def test_code_goes_from_modal_to_page_and_nowhere_else(discord_env, caplog):
    caplog.set_level(logging.DEBUG)
    worker, box = call_tool(discord_env.manager)
    message = _posted(discord_env)
    assert message.content.startswith("🔐 <@111>") and "Discord still processes" in message.content

    tap = _tap(discord_env, message, 111, 222)
    replies = _submit(discord_env, tap.response.modal, 111, 222, "482 913")
    worker.join(timeout=5)

    result = json.loads(box["result"])
    assert result["success"] and result["source"] == "user"
    assert f'"value": "{CODE}"' in discord_env.fills[0]
    everything_else = [box["result"], *replies, *_discord_texts(discord_env), caplog.text]
    assert not any(CODE in text or "482 913" in text for text in everything_else)
    _wait_for(lambda: message.edits)
    assert message.view is None


def test_other_users_channels_and_repeat_submissions_are_rejected(discord_env):
    worker, box = call_tool(discord_env.manager)
    message = _posted(discord_env)

    assert _tap(discord_env, message, 999, 222).response.modal is None
    assert _tap(discord_env, message, 111, 333).response.modal is None
    modal = _tap(discord_env, message, 111, 222).response.modal
    # A modal opened by the right user still refuses a submit arriving as someone else or elsewhere.
    assert "Only the person" in _submit(discord_env, modal, 999, 222, CODE)[0]
    assert "different conversation" in _submit(discord_env, modal, 111, 333, CODE)[0]
    assert "doesn't look like" in _submit(discord_env, modal, 111, 222, "12")[0]
    assert "Code sent" in _submit(discord_env, modal, 111, 222, CODE)[0]
    worker.join(timeout=5)
    assert "expired or was already used" in _submit(discord_env, modal, 111, 222, "111111")[0]
    assert len(discord_env.fills) == 1 and f'"value": "{CODE}"' in discord_env.fills[0]
    assert json.loads(box["result"])["success"]


def test_concurrent_prompts_stay_bound_to_their_own_session(discord_env):
    first, first_box = call_tool(discord_env.manager, user_id="111", chat_id="222")
    second, second_box = call_tool(discord_env.manager, user_id="444", chat_id="555")
    first_msg, second_msg = _posted(discord_env, 222), _posted(discord_env, 555)

    second_modal = _tap(discord_env, second_msg, 444, 555).response.modal
    assert _tap(discord_env, first_msg, 444, 555).response.modal is None
    assert "Code sent" in _submit(discord_env, second_modal, 444, 555, "555000")[0]
    second.join(timeout=5)
    assert first.is_alive()

    first_modal = _tap(discord_env, first_msg, 111, 222).response.modal
    _submit(discord_env, first_modal, 111, 222, "222000")
    first.join(timeout=5)
    assert json.loads(first_box["result"])["success"] and json.loads(second_box["result"])["success"]
    assert sorted(f.split('"value": "')[1][:6] for f in discord_env.fills) == ["222000", "555000"]


def test_stop_releases_the_worker_without_a_code(discord_env):
    from tools.interrupt import set_interrupt

    worker, box = call_tool(discord_env.manager)
    message = _posted(discord_env)
    set_interrupt(True, worker.ident)
    try:
        worker.join(timeout=5)
    finally:
        set_interrupt(False, worker.ident)
    assert not worker.is_alive()
    assert json.loads(box["result"])["error_type"] == "code_declined"
    _wait_for(lambda: message.edits)
    assert "cancelled" in message.edits[-1] and discord_env.fills == []
    assert _tap(discord_env, message, 111, 222).response.modal is None


def test_timeout_and_disconnect_release_the_worker(discord_env, hermes_home):
    config = json.loads((hermes_home / "config.yaml").read_text())
    config["approvals"]["timeout"] = 1
    (hermes_home / "config.yaml").write_text(json.dumps(config))
    worker, box = call_tool(discord_env.manager)
    message = _posted(discord_env)
    worker.join(timeout=5)
    assert json.loads(box["result"])["error_type"] == "code_declined"
    _wait_for(lambda: message.edits)
    assert "expired" in message.edits[-1]

    config["approvals"]["timeout"] = 30
    (hermes_home / "config.yaml").write_text(json.dumps(config))
    worker, box = call_tool(discord_env.manager)
    _posted(discord_env, count=2)
    discord_env.adapter.is_connected = False
    worker.join(timeout=5)
    assert json.loads(box["result"])["error_type"] == "code_declined"


def test_unload_releases_the_worker_and_restores_the_builtin(discord_env):
    from tools import browser_vault_tool
    from tools.registry import registry

    worker, box = call_tool(discord_env.manager)
    message = _posted(discord_env)
    discord_env.manager.unload()
    worker.join(timeout=5)
    assert json.loads(box["result"])["error_type"] == "code_declined"
    assert registry.get_entry("browser_vault_enter_code").handler is browser_vault_tool._handle_vault_enter_code
    assert _tap(discord_env, message, 111, 222).response.modal is None


def test_authenticator_key_path_never_prompts(discord_env, monkeypatch):
    from agent import vault_backends

    backend = type("Backend", (), {"name": "local", "resolve_otp": lambda self, handle: "135790"})()
    monkeypatch.setattr(vault_backends, "backend_for_handle", lambda handle: backend)
    worker, box = call_tool(discord_env.manager, args={"handle": "vault_x"})
    worker.join(timeout=5)
    assert json.loads(box["result"])["source"] == "local"
    assert discord_env.bot.channels == {}


@pytest.mark.parametrize("platform", ["cli", "telegram"])
def test_other_surfaces_keep_the_builtin_behaviour(discord_env, platform):
    worker, box = call_tool(discord_env.manager, platform=platform)
    worker.join(timeout=5)
    assert json.loads(box["result"])["error_type"] == "prompt_unavailable"
    assert discord_env.bot.channels == {}


def test_raw_gateway_debug_log_of_a_code_submission_is_dropped(discord_env):
    record = logging.LogRecord("discord.gateway", logging.DEBUG, __file__, 1, "For Shard ID %s: WebSocket Event: %s",
                               (None, {"t": "INTERACTION_CREATE", "d": {"type": 5, "data": {
                                   "custom_id": "hermes-code-entry:abc:modal",
                                   "components": [{"components": [{"value": CODE}]}]}}}), None)
    other = logging.LogRecord("discord.gateway", logging.DEBUG, __file__, 1, "For Shard ID %s: WebSocket Event: %s",
                              (None, {"t": "MESSAGE_CREATE", "d": {}}), None)
    gateway = logging.getLogger("discord.gateway")
    assert not gateway.filter(record)
    assert gateway.filter(other)



def test_a_reloaded_plugin_is_rewired_to_the_live_bot(discord_env, monkeypatch):
    """The adapter skips factories it already wired on its live client, keyed by (plugin, qualname); a
    reloaded plugin must still receive the bot."""
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    adapter = discord_env.adapter
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: discord_env.manager)
    adapter._wire_plugin_handlers(discord_env.bot)

    discord_env.manager.unload()
    discord_env.manager = PluginManager()
    discord_env.manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: discord_env.manager)
    adapter.rewire_plugin_handlers()

    worker, box = call_tool(discord_env.manager)
    modal = _tap(discord_env, _posted(discord_env), 111, 222).response.modal
    _submit(discord_env, modal, 111, 222, CODE)
    worker.join(timeout=5)
    assert json.loads(box["result"])["success"]
