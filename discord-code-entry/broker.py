"""Pending code and login prompts, shared by the waiting tool thread and the Discord event loop.

Each prompt belongs to one tool invocation and is bound to the Discord user who started the turn and the
channel the button was posted in. The first valid submission wins; every later or foreign interaction is
rejected. Nothing here logs or formats a submitted value.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SUBMITTED = "submitted"
EXPIRED = "expired"
STOPPED = "stopped"
CANCELLED = "cancelled"
DISCONNECTED = "disconnected"

# Rejections, returned to the Discord side so it can pick the ephemeral reply.
WRONG_USER = "wrong_user"
WRONG_CHANNEL = "wrong_channel"
GONE = "gone"


@dataclass(eq=False)
class Pending:
    user_id: int
    channel_id: int
    prompt_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    event: threading.Event = field(default_factory=threading.Event)
    outcome: Optional[str] = None
    message_id: Optional[int] = None
    # Discord-side objects, only touched on the event loop.
    message: Any = None
    view: Any = None
    _value: Any = field(default=None, repr=False)

    def take_value(self) -> Any:
        value, self._value = self._value, None
        return value


class Broker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, Pending] = {}
        self._closed = False

    def open(self, *, user_id: int, channel_id: int) -> Pending:
        pending = Pending(user_id=user_id, channel_id=channel_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("The Discord code-entry plugin is unloading.")
            self._pending[pending.prompt_id] = pending
        return pending

    def check(self, prompt_id: str, *, user_id: int, channel_id: Optional[int],
              message_id: Optional[int] = None) -> Optional[str]:
        """Why this interaction may not act on the prompt, or None when it may."""
        with self._lock:
            pending = self._pending.get(prompt_id)
        if pending is None:
            return GONE
        if user_id != pending.user_id:
            return WRONG_USER
        if channel_id != pending.channel_id or (message_id is not None and message_id != pending.message_id):
            return WRONG_CHANNEL
        return None

    def submit(self, prompt_id: str, *, user_id: int, channel_id: Optional[int], value: Any) -> Optional[str]:
        """Hand ``value`` to the waiting tool thread. Returns None on success, else the rejection."""
        with self._lock:
            pending = self._pending.get(prompt_id)
            if pending is None:
                return GONE
            if user_id != pending.user_id:
                return WRONG_USER
            if channel_id != pending.channel_id:
                return WRONG_CHANNEL
            del self._pending[prompt_id]
            pending._value = value
            pending.outcome = SUBMITTED
        pending.event.set()
        return None

    def finish(self, pending: Pending, outcome: str) -> str:
        """Close the prompt from the tool thread. A value that landed before this call still counts unless the
        turn was stopped; returns the final outcome."""
        with self._lock:
            self._pending.pop(pending.prompt_id, None)
            if pending.outcome == SUBMITTED and outcome != STOPPED:
                return SUBMITTED
            pending.take_value()
            pending.outcome = outcome
        pending.event.set()
        return outcome

    def cancel_all(self, outcome: str = CANCELLED) -> None:
        """Wake every waiting thread without a value (plugin unload)."""
        with self._lock:
            self._closed = True
            pending, self._pending = list(self._pending.values()), {}
            for entry in pending:
                entry.outcome = outcome
        for entry in pending:
            entry.event.set()
