"""Load the plugin through Hermes' real discovery against a temp HERMES_HOME, with fake Discord endpoints.

The fakes stand in for the Discord API only (channel, message, interaction). The plugin manager, tool
registry, vault tool, session context, interrupt and approval-wait code are the real Hermes modules.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "vendor" / "hermes"
PLUGIN = ROOT / "discord-code-entry"
sys.path.insert(0, str(HERMES))

_ids = itertools.count(10_000)


class FakeMessage:
    def __init__(self, content, view):
        self.id = next(_ids)
        self.content, self.view = content, view
        # Discord clients can still hold the old button after the edit lands; stale taps use this.
        self.posted_view = view
        self.edits = []

    async def edit(self, *, content=None, view=None):
        self.edits.append(content)
        self.content, self.view = content, view


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, content, *, view=None, allowed_mentions=None):
        message = FakeMessage(content, view)
        self.sent.append(message)
        return message


class FakeBot:
    """A running event loop on its own thread, like discord.py's client loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.channels = {}
        self.closed = False

    def channel(self, channel_id):
        return self.channels.setdefault(channel_id, FakeChannel(channel_id))

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channel(channel_id)

    def is_closed(self):
        return self.closed

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


class FakeAdapter:
    """Connection state plus the real BasePlatformAdapter plugin-handler wiring."""
    from gateway.platforms.base import BasePlatformAdapter as _Base

    is_connected = True
    platform, name = SimpleNamespace(value="discord"), "Discord"
    _plugin_handler_native = _plugin_handlers_wired = None
    _wire_plugin_handlers = _Base._wire_plugin_handlers
    rewire_plugin_handlers = _Base.rewire_plugin_handlers


class FakeResponse:
    def __init__(self):
        self.replies, self.modal = [], None

    def is_done(self):
        return bool(self.replies) or self.modal is not None

    async def send_message(self, text, *, ephemeral=False):
        assert ephemeral, "every acknowledgement must be ephemeral"
        self.replies.append(text)

    async def send_modal(self, modal):
        self.modal = modal


def interaction(user_id, channel_id, message_id=None):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), channel_id=channel_id,
                           message=SimpleNamespace(id=message_id), response=FakeResponse())


OTP_CONTROLS = [{"index": 0, "type": "text", "name": "otp", "label": "Authentication code",
                 "autocomplete": "one-time-code"}]


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "discord-code-entry").symlink_to(PLUGIN, target_is_directory=True)
    # JSON is valid YAML.
    (home / "config.yaml").write_text(json.dumps({
        "approvals": {"timeout": 30},
        "plugins": {"enabled": ["discord-code-entry"],
                    "entries": {"discord-code-entry": {"allow_tool_override": True}}},
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_SESSION_PLATFORM", "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def page(monkeypatch):
    """A login page with a one-time-code field; records every secret-bearing fill expression."""
    from tools import browser_vault_tool

    fills = []
    monkeypatch.setattr(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None)
    monkeypatch.setattr(browser_vault_tool, "_eval_js", lambda task_id, expr: {
        "success": True,
        "result": json.dumps(OTP_CONTROLS) if "querySelectorAll" in expr else "https://acme.test/2fa"})

    def fill(task_id, expr):
        fills.append(expr)
        return {"success": True, "result": json.dumps({"filled": 1})}

    monkeypatch.setattr(browser_vault_tool, "_eval_js_secret", fill)
    return fills


@pytest.fixture
def discord_env(hermes_home, page, monkeypatch):
    """Plugin loaded, a connected fake Discord adapter wired through the plugin's handler factory."""
    from hermes_cli.plugins import PluginManager
    from tools import send_message_senders

    manager = PluginManager()
    manager.discover_and_load()
    bot = FakeBot()
    adapter = FakeAdapter()
    for factory, _plugin in manager.get_platform_handler_factories("discord"):
        factory(bot, adapter)
    monkeypatch.setattr(send_message_senders, "_live_adapter", lambda platform, **_: (None, adapter))
    env = SimpleNamespace(manager=manager, bot=bot, adapter=adapter, fills=page)
    yield env
    manager.unload()
    bot.stop()


def call_tool(manager, *, platform="discord", user_id="111", chat_id="222", thread_id="", args=None):
    """Dispatch the tool on a worker thread bound to a gateway session, like the gateway turn does.
    Returns (thread, result box)."""
    from gateway.session_context import set_session_vars
    from tools.registry import registry

    box = {}

    def run():
        set_session_vars(platform=platform, user_id=user_id, chat_id=chat_id, thread_id=thread_id,
                         session_key=f"agent:main:{platform}:{chat_id}")
        box["result"] = registry.dispatch("browser_vault_enter_code", args or {}, scope=manager.scope_key,
                                          task_id="t")

    worker = threading.Thread(target=contextvars.Context().run, args=(run,), daemon=True)
    worker.start()
    return worker, box
