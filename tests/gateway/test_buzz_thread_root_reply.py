"""Regression test: Buzz replies go to the thread root, not the immediate parent.

Live symptom: event c8e9... (root), Hermes child 4076... (reply→c8e9),
user child d05... (reply→c8e9), then Hermes response eff91... nested under
d05 because the adapter used `event.message_id` as reply anchor instead of
the stable NIP-10 root.

Fix: The adapter parses NIP-10 e-tags via ``_nip10_extract_root()``, passes
the result through ``_handle_event`` → ``_dispatch_message``, and stores it on
the MessageEvent as ``_hermes_buzz_thread_root``.  base.py's
``_reply_anchor_for_event`` returns it for Buzz over the default ``message_id``.

The integration test constructs a real inbound Nostr event dict, feeds it to
``adapter.BuzzAdapter._handle_event``, and asserts the dispatched
MessageEvent carries the correct ``_hermes_buzz_thread_root``.  This test
would fail before the fix because ``_handle_event`` never computed or passed
the root parameter.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import sys
from pathlib import Path as _Path

PROJECT_ROOT = _Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    _reply_anchor_for_event,
)


# ---------------------------------------------------------------------------
# Reusable constants — all are exactly 64-char lowercase hex event ids
# ---------------------------------------------------------------------------
_ID_C8E9 = "c8e9d273d9ec4a1b5f3e2a7c8d0b9e6f5a4c3d2e1f0a9b8c7d6e5f4a3b2c1d00"
_ID_4076 = "4076ca2a5690b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6"
_ID_D05E = "d05ee03a9ff3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9"
_PUBKEY  = "33ba2f44bb5a33a1b27d63ef2408654a03a42ac31ada1a3887e023ac8cae589e"
# Distinct inbound author: must differ from the adapter's _self_pubkey or
# _handle_event's self-echo suppression drops the event before dispatch.
_AUTHOR  = "aa17c9b04a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d"
_CHANNEL = "ccc2bc1a-aaaa-bbbb-cccc-ddddeeeeeeee"


# ---------------------------------------------------------------------------
# Load the buzz adapter module in isolation (same pattern as test_buzz_websocket)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _buzz_adapter():
    """Load the buzz adapter module and return it."""
    try:
        from tests.gateway._plugin_adapter_loader import load_plugin_adapter
        yield load_plugin_adapter("buzz")
    except FileNotFoundError:
        import importlib.util
        adapter_path = PROJECT_ROOT / "plugins" / "platforms" / "buzz" / "adapter.py"
        spec = importlib.util.spec_from_file_location("plugin_adapter_buzz", adapter_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod


# ── Production helper tests (_nip10_extract_root) ─────────────────────────

class TestNip10ExtractRootProduction:
    """Import and exercise the adapter's *own* ``_nip10_extract_root``.

    These tests prove the production parser lives at the right place and
    implements the correct NIP-10 semantics (root wins > reply fallback > none).
    """

    def test_imported_has_the_helper(self, _buzz_adapter):
        assert hasattr(_buzz_adapter, "_nip10_extract_root")
        result = _buzz_adapter._nip10_extract_root([])
        assert result is None

    def test_nested_reply_returns_root_marker(self, _buzz_adapter):
        tags = [
            ["e", _ID_C8E9, "", "root"],
            ["e", _ID_D05E, "", "reply"],
            ["p", _PUBKEY],
        ]
        assert _buzz_adapter._nip10_extract_root(tags) == _ID_C8E9

    def test_direct_child_falls_back_to_reply_marker(self, _buzz_adapter):
        tags = [["e", _ID_C8E9, "", "reply"], ["p", _PUBKEY]]
        assert _buzz_adapter._nip10_extract_root(tags) == _ID_C8E9

    def test_top_level_returns_none(self, _buzz_adapter):
        tags = [["p", _PUBKEY], ["h", "some-channel"]]
        assert _buzz_adapter._nip10_extract_root(tags) is None

    def test_no_tags_returns_none(self, _buzz_adapter):
        assert _buzz_adapter._nip10_extract_root(None) is None
        assert _buzz_adapter._nip10_extract_root([]) is None

    def test_malformed_tags_ignored_gracefully(self, _buzz_adapter):
        tags = [
            "not-an-array",
            ["e"],
            ["e", "short"],
            ["e", _ID_C8E9, "", "root"],
        ]
        assert _buzz_adapter._nip10_extract_root(tags) == _ID_C8E9

    def test_malicious_root_does_not_shadow_valid_reply(self, _buzz_adapter):
        tags = [
            ["e", "garbage", "", "root"],   # not valid hex → ignored
            ["e", _ID_D05E, "", "reply"],
        ]
        assert _buzz_adapter._nip10_extract_root(tags) == _ID_D05E

    def test_unmarked_e_tags_ignored(self, _buzz_adapter):
        assert _buzz_adapter._nip10_extract_root([["e", _ID_C8E9]]) is None
        assert _buzz_adapter._nip10_extract_root([["e", _ID_D05E, ""]]) is None


# ── Integration test: _handle_event → _dispatch_message sets thread root ──

class TestHandleEventDispatchesWithThreadRoot:
    """End-to-end: feed an inbound Nostr event dict into _handle_event and
    assert the resulting MessageEvent carries the correct _hermes_buzz_thread_root.

    handle_message() spawns complex session machinery, so we monkeypatch it
    to immediately invoke our own handler — this keeps the test focused on
    the adapter wiring without importing unrelated subsystems.

    This is the critical regression guard: if any link breaks
    (missing parser, missing wire, wrong attribute name), the assertion fails.
    """

    @pytest.fixture
    def _adapter_with_handler(self, _buzz_adapter):
        """Build a minimal adapter whose handle_message dispatches captured events."""
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={
            "relay_url": "https://test.relay",
            "channels": [_CHANNEL],
            "home_channel": _CHANNEL,
            # Dispatch un-mentioned group messages too; the thread-root wiring
            # under test is orthogonal to the mention gate.
            "require_mention": False,
        })
        adapter = _buzz_adapter.BuzzAdapter(cfg)
        # Isolate dispatch wiring from any BUZZ_ALLOWED_USERS inherited by pytest.
        adapter._allowed_pubkeys = set()
        adapter._self_pubkey = _PUBKEY
        adapter._private_key = "00" * 31 + "03"
        adapter._display_name = "Chip"
        adapter._user_names = {_AUTHOR: "TestUser"}
        adapter._channel_state[_CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        adapter.channels = [_CHANNEL]
        adapter._channel_names[_CHANNEL] = "tasks"

        captured = []

        async def capture_handler(ev):
            captured.append(ev)

        adapter._message_handler = capture_handler
        # Patch handle_message to skip session machinery and call our handler
        with patch.object(adapter, "handle_message", new=AsyncMock(side_effect=capture_handler)):
            yield adapter, captured

    @pytest.mark.asyncio
    async def test_nested_user_message_anchors_to_stable_root(self, _adapter_with_handler):
        """Hermes receives d05... (nested reply to c8e9...).
        Before the fix: adapter ignored tags → no _hermes_buzz_thread_root →
        base picks up message_id (wrong, nests deeper each turn).
        After the fix: adapter extracts root=c8e9 from tags → sets attr →
        base returns c8e9 as reply anchor.
        """
        adapter, captured = _adapter_with_handler

        # Inbound event shaped like a real Nostr kind-9 from buzz messages get
        inbound = {
            "id": _ID_D05E,
            "kind": 9,
            "pubkey": _AUTHOR,
            "content": "@Chip what's the status?",
            "created_at": 1700000000,
            "tags": [
                ["e", _ID_C8E9, "", "root"],       # stable flat-thread anchor
                ["e", _ID_4076, "", "reply"],      # immediate parent
                ["p", _AUTHOR],                     # author p-tag
                ["h", _CHANNEL],                   # channel tag
            ],
        }

        state = adapter._channel_state[_CHANNEL]
        await adapter._handle_event(_CHANNEL, state, inbound)

        assert len(captured) == 1, \
            f"_handle_event dispatched {len(captured)} events; expected 1"

        ev = captured[0]
        assert isinstance(ev, MessageEvent), "Dispatched object is not a MessageEvent"

        # CRITICAL ASSERTION: the adapter wired the root through
        assert hasattr(ev, "_hermes_buzz_thread_root"), \
            "MessageEvent missing _hermes_buzz_thread_root — " \
            "_handle_event did not pass buzz_thread_root to _dispatch_message"

        assert ev._hermes_buzz_thread_root == _ID_C8E9, \
            f"Expected root {_ID_C8E9} but got {ev._hermes_buzz_thread_root}"

        # message_id stays as incoming event (unchanged for dedupe/reactions)
        assert ev.message_id == _ID_D05E, "message_id was changed from incoming event ID"

        # Verify downstream reply-anchor also agrees
        anchor = _reply_anchor_for_event(ev)
        assert anchor == _ID_C8E9, \
            f"_reply_anchor_for_event returned {anchor}, expected root {_ID_C8E9}"

    @pytest.mark.asyncio
    async def test_top_level_no_root_keeps_default_behavior(self, _adapter_with_handler):
        """Top-level message without NIP-10 tags → no _hermes_buzz_thread_root,
        so _reply_anchor_for_event falls back to message_id."""
        adapter, captured = _adapter_with_handler

        inbound = {
            "id": _ID_C8E9,
            "kind": 9,
            "pubkey": _AUTHOR,
            "content": "hello everyone",
            "created_at": 1700000000,
            "tags": [
                ["p", _AUTHOR],
                ["h", _CHANNEL],
            ],
        }

        state = adapter._channel_state[_CHANNEL]
        await adapter._handle_event(_CHANNEL, state, inbound)

        assert len(captured) == 1
        ev = captured[0]

        # Top-level has no root marker → attribute absent
        assert not hasattr(ev, "_hermes_buzz_thread_root"), \
            "Top-level event should not carry _hermes_buzz_thread_root"

        # Fallback: base returns message_id
        anchor = _reply_anchor_for_event(ev)
        assert anchor == _ID_C8E9

    @pytest.mark.asyncio
    async def test_direct_child_replies_go_to_same_event(self, _adapter_with_handler):
        """Direct reply (only a reply marker, no separate root) → root == parent."""
        adapter, captured = _adapter_with_handler

        inbound = {
            "id": _ID_4076,
            "kind": 9,
            "pubkey": _AUTHOR,
            "content": "replied to top level",
            "created_at": 1700000000,
            "tags": [
                ["e", _ID_4076, "", "reply"],  # direct child: reply points to self
                ["p", _AUTHOR],
                ["h", _CHANNEL],
            ],
        }

        state = adapter._channel_state[_CHANNEL]
        await adapter._handle_event(_CHANNEL, state, inbound)

        assert len(captured) == 1
        ev = captured[0]

        # Reply-only: root equals the reply target
        assert hasattr(ev, "_hermes_buzz_thread_root")
        assert ev._hermes_buzz_thread_root == _ID_4076
        assert _reply_anchor_for_event(ev) == _ID_4076
