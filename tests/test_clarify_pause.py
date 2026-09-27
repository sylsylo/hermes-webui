"""Clarify pause: cancelling the auto-timeout must never resolve the prompt.

Two layers are covered:
  - api/clarify.py state (paused flag, SSE fan-out, stale ids)
  - api/streaming.py wait loop (a paused entry ignores its deadline)
plus live-server endpoint checks and static checks for the route, the card
markup and the i18n keys.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from tests._pytest_port import BASE

try:
    from api.clarify import (
        _ClarifyEntry,
        clear_pending,
        get_pending,
        sse_subscribe,
        sse_unsubscribe,
        submit_pending,
    )
    CLARIFY_AVAILABLE = True
except ImportError:
    CLARIFY_AVAILABLE = False

# ``pause_clarify`` is deliberately NOT imported here: the guarded block above
# only exists so exotic environments skip instead of erroring. Importing the new
# symbol at module level would turn a missing implementation into a silent skip
# of the whole file, so each pause test imports it itself and FAILS loudly.

_NEEDS_CLARIFY = pytest.mark.skipif(
    not CLARIFY_AVAILABLE, reason="api.clarify not available in this environment"
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(rel):
    with open(os.path.join(_ROOT, rel)) as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Server state (api/clarify.py)
# ══════════════════════════════════════════════════════════════════════════════
@_NEEDS_CLARIFY
class TestPauseState:
    def test_pause_marks_head_entry_and_pending_payload(self):
        from api.clarify import pause_clarify

        sid = f"pause-{uuid.uuid4().hex[:8]}"
        entry = submit_pending(sid, {"question": "Pick", "choices_offered": ["a", "b"]})
        assert entry.paused is False
        assert pause_clarify(sid, entry.clarify_id) is True
        assert entry.paused is True
        assert get_pending(sid)["paused"] is True
        clear_pending(sid)

    def test_pause_is_idempotent(self):
        from api.clarify import pause_clarify

        sid = f"pause-idem-{uuid.uuid4().hex[:8]}"
        entry = submit_pending(sid, {"question": "Pick", "choices_offered": []})
        assert pause_clarify(sid, entry.clarify_id) is True
        assert pause_clarify(sid, entry.clarify_id) is True
        clear_pending(sid)

    def test_pause_unknown_session_or_id_returns_false(self):
        from api.clarify import pause_clarify

        sid = f"pause-none-{uuid.uuid4().hex[:8]}"
        assert pause_clarify(sid, "") is False
        entry = submit_pending(sid, {"question": "Pick", "choices_offered": []})
        assert pause_clarify(sid, "not-this-id") is False
        assert pause_clarify("other-session", entry.clarify_id) is False
        assert entry.paused is False
        clear_pending(sid)

    def test_pause_notifies_sse_subscribers_with_paused_payload(self):
        from api.clarify import pause_clarify

        sid = f"pause-sse-{uuid.uuid4().hex[:8]}"
        q = sse_subscribe(sid)
        try:
            entry = submit_pending(sid, {"question": "Pick", "choices_offered": ["a"]})
            while not q.empty():  # drop the submit notification
                q.get_nowait()
            assert pause_clarify(sid, entry.clarify_id) is True
            payload = q.get_nowait()
            assert payload["pending"]["paused"] is True
            assert payload["pending_count"] == 1
        finally:
            sse_unsubscribe(sid, q)
            clear_pending(sid)

    def test_pause_does_not_resolve_the_waiter(self):
        from api.clarify import pause_clarify

        sid = f"pause-wait-{uuid.uuid4().hex[:8]}"
        entry = submit_pending(sid, {"question": "Pick", "choices_offered": ["a"]})
        pause_clarify(sid, entry.clarify_id)
        # A pause must not look like an answer: a blocked waiter stays blocked.
        assert entry.event.wait(timeout=0.2) is False
        clear_pending(sid)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Wait loop (api/streaming.py)
# ══════════════════════════════════════════════════════════════════════════════
@_NEEDS_CLARIFY
class TestPausedWaitLoop:
    def _entry(self, paused):
        entry = _ClarifyEntry({"question": "Pick", "choices_offered": ["a"]})
        if paused:
            entry.data["paused"] = True
        return entry

    def test_expired_deadline_returns_none(self):
        from api.streaming import _clarify_wait_outcome

        entry = self._entry(paused=False)
        assert _clarify_wait_outcome(entry, time.monotonic() - 1, threading.Event()) is None

    def test_paused_entry_ignores_expired_deadline_and_returns_response(self):
        from api.streaming import _clarify_wait_outcome

        entry = self._entry(paused=True)

        def _answer():
            time.sleep(2.2)  # well past the (expired) deadline
            entry.result = "b"
            entry.event.set()

        threading.Thread(target=_answer, daemon=True).start()
        assert _clarify_wait_outcome(entry, time.monotonic() - 1, threading.Event()) == "b"

    def test_cancel_still_wins_while_paused(self):
        from api.streaming import _clarify_wait_outcome

        entry = self._entry(paused=True)
        cancel = threading.Event()
        threading.Thread(target=lambda: (time.sleep(0.3), cancel.set()), daemon=True).start()
        started = time.monotonic()
        assert _clarify_wait_outcome(entry, time.monotonic() + 30, cancel) is None
        assert time.monotonic() - started < 5


# ══════════════════════════════════════════════════════════════════════════════
# 3. Live endpoints
# ══════════════════════════════════════════════════════════════════════════════
def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _pending(sid):
    url = BASE + "/api/clarify/pending?session_id=" + urllib.parse.quote(sid)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())["pending"]


@_NEEDS_CLARIFY
class TestClarifyPauseHTTP:
    """Endpoints exercised against the live test server (tests/_pytest_port)."""

    def test_pause_endpoint_marks_pending_and_get_reflects_it(self):
        sid = f"http-pause-{uuid.uuid4().hex[:8]}"
        url = BASE + "/api/clarify/inject_test?session_id=" + urllib.parse.quote(sid)
        with urllib.request.urlopen(url, timeout=10) as r:
            assert json.loads(r.read())["ok"] is True

        pending = _pending(sid)
        assert pending["timeout_seconds"] == 120
        assert not pending.get("paused")

        body, status = _post(
            "/api/clarify/pause", {"session_id": sid, "clarify_id": pending["clarify_id"]}
        )
        assert status == 200, body
        assert body["ok"] is True
        assert _pending(sid)["paused"] is True

        # Cleanup: answer it so the injected entry does not linger.
        _post(
            "/api/clarify/respond",
            {"session_id": sid, "response": "a", "clarify_id": pending["clarify_id"]},
        )
        assert _pending(sid) is None

    def test_pause_endpoint_409_when_nothing_pending(self):
        body, status = _post("/api/clarify/pause", {"session_id": f"none-{uuid.uuid4().hex[:8]}"})
        assert status == 409, body
        assert body["stale"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 4. Static checks (route, markup, i18n)
# ══════════════════════════════════════════════════════════════════════════════
class TestPauseFrontendMarkers:
    def test_clarify_module_defines_pause_and_entry_flag(self):
        src = _read("api/clarify.py")
        assert "def pause_clarify(" in src
        assert "def paused(self)" in src

    def test_streaming_module_defines_pause_aware_wait(self):
        src = _read("api/streaming.py")
        assert "def _clarify_wait_outcome(" in src
        assert "entry.paused" in src

    @pytest.mark.parametrize("rel", ["static/index.html", "static/messages.js"])
    def test_card_has_pause_button(self, rel):
        src = _read(rel)
        assert 'id="clarifyPause"' in src
        assert "pauseClarify()" in src

    @pytest.mark.parametrize("rel", ["static/index.html", "static/messages.js"])
    def test_pause_button_sits_between_countdown_and_collapse(self, rel):
        src = _read(rel)
        assert (
            src.index('id="clarifyCountdown"')
            < src.index('id="clarifyPause"')
            < src.index('id="clarifyCollapse"')
        )

    def test_messages_js_handles_paused_state(self):
        src = _read("static/messages.js")
        assert "function _syncClarifyPauseButton(" in src
        assert "function pauseClarify(" in src
        assert "if (_clarifyPaused) return" in src  # _updateClarifyCountdown guard
        assert "pending.paused" in src

    def test_style_has_pause_rules(self):
        src = _read("static/style.css")
        assert ".clarify-pause{" in src
        assert ".clarify-countdown.paused{" in src

    @pytest.mark.parametrize(
        "key",
        [
            "clarify_pause_title",
            "clarify_paused",
            "clarify_paused_title",
            "clarify_paused_toast",
            "clarify_pause_stale",
        ],
    )
    def test_i18n_key_present_in_en_and_fr(self, key):
        src = _read("static/i18n.js")
        assert src.count(f"{key}:") >= 2, f"{key} must exist in en and fr"

    def test_pause_endpoint_wired_in_routes(self):
        src = _read("api/routes.py")
        assert '"/api/clarify/pause"' in src
        assert "def _handle_clarify_pause(" in src
        assert "pause_clarify" in src
