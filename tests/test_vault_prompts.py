"""Masked vault prompts in the WebUI: unlock a password manager from the browser.

Hermes' credential vault asks the *surface* for the manager master password (and for
a one-time code). The CLI/TUI install those callbacks; the WebUI did not, so every
``browser_vault_*`` call in a WebUI session answered ``prompt_unavailable``.

Three layers are covered here:
  - ``api/vault_prompts.py``: the masked pending entry the card renders, the value
    that comes back, and every way the answer can legitimately be empty
  - the wiring in ``api/streaming.py``: installed per run, cleared in the finally
  - static checks on the served front (masked input, no echo of a secret answer,
    i18n keys in ``en`` and ``fr``)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests._pytest_port import BASE  # noqa: F401  (port convention, unused directly)

try:
    from api import clarify
    from api import vault_prompts

    VAULT_PROMPTS_AVAILABLE = True
except ImportError:  # pragma: no cover - exotic environment
    VAULT_PROMPTS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not VAULT_PROMPTS_AVAILABLE, reason="api.vault_prompts is unavailable in this build"
)

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _sid() -> str:
    return f"vault-prompt-test-{uuid.uuid4().hex[:8]}"


def _agent_callbacks():
    from agent.vault_backends import unlock as vault_unlock

    return vault_unlock


def _install_fake_store(monkeypatch, items):
    """Replace the local vault store with a fixed one.

    Records are ``(id, origin, kind, password)``. Tests must never read the developer's
    real vault: an item saved for the manager's origin would silently switch the card
    tests over to autonomous mode.
    """
    from agent import vault_store

    class _Meta:
        def __init__(self, rec):
            self.id, self.origin, self.kind = rec[0], rec[1], rec[2]

    class _Store:
        def list_items(self):
            return [_Meta(rec) for rec in items]

        def resolve_secret(self, item_id):
            for rec in items:
                if rec[0] == item_id:
                    return {"password": rec[3] if len(rec) > 3 else ""}
            return {}

    monkeypatch.setattr(vault_store, "get_vault_store", lambda: _Store())


@pytest.fixture(autouse=True)
def _empty_local_vault(monkeypatch):
    _install_fake_store(monkeypatch, [])


def _wait_for_pending(sid: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = clarify.get_pending(sid)
        if pending:
            return pending
        time.sleep(0.05)
    raise AssertionError("no masked prompt ever reached the clarify queue")


def _run_callback(cb, *args) -> dict:
    """Call an installed prompt callback on another thread (as a tool worker would)."""
    out: dict = {}

    def worker():
        try:
            out["value"] = cb(*args)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            out["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return {"out": out, "thread": thread, "join": lambda: thread.join(5)}


class TestMaskedEntry:
    def test_unlock_prompt_parks_a_masked_entry_and_returns_the_typed_value(self):
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            cb = _agent_callbacks().get_unlock_prompt_callback()
            assert cb is not None
            # A tool worker thread: the closure, not a thread-local, must carry the session.
            run = _run_callback(cb, "bitwarden", "Bitwarden")
            pending = _wait_for_pending(sid)
            assert pending.get("secret") is True
            assert pending["vault"] == {"kind": "unlock", "backend": "bitwarden"}
            assert "Bitwarden" in pending["question"]
            assert not pending.get("choices_offered")
            assert float(pending.get("expires_at", 0)) > time.time()
            assert clarify.resolve_clarify_by_id(sid, pending["clarify_id"], "s3cret-master")
            run["join"]()
            assert "error" not in run["out"]
            assert run["out"]["value"] == "s3cret-master"
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_code_prompt_names_the_site_it_asks_about(self):
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            cb = _agent_callbacks().get_code_prompt_callback()
            assert cb is not None
            run = _run_callback(cb, "vault.bitwarden.com", "sent to your email")
            pending = _wait_for_pending(sid)
            assert pending.get("secret") is True
            assert pending["vault"]["kind"] == "code"
            assert "vault.bitwarden.com" in pending["question"]
            assert clarify.resolve_clarify_by_id(sid, pending["clarify_id"], "123456")
            run["join"]()
            assert run["out"]["value"] == "123456"
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_the_typed_secret_is_not_left_in_clarify_state(self):
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            cb = _agent_callbacks().get_unlock_prompt_callback()
            run = _run_callback(cb, "bitwarden", "Bitwarden")
            pending = _wait_for_pending(sid)
            clarify.resolve_clarify_by_id(sid, pending["clarify_id"], "s3cret-master")
            run["join"]()
            assert run["out"]["value"] == "s3cret-master"
            # Nothing that the card, the poll or the next render can read keeps the value.
            assert clarify.get_pending(sid) is None
            for entry in list(clarify._gateway_queues.get(sid) or []):
                assert entry.result is None
                assert "s3cret-master" not in repr(entry.data)
            assert "s3cret-master" not in repr(clarify._pending.get(sid))
        finally:
            vault_prompts.uninstall_vault_prompts()


class TestEmptyAnswers:
    def test_stop_before_the_prompt_shows_nothing(self):
        sid = _sid()
        cancel = threading.Event()
        cancel.set()
        assert vault_prompts.install_vault_prompts(sid, cancel) is True
        try:
            cb = _agent_callbacks().get_unlock_prompt_callback()
            assert cb("bitwarden", "Bitwarden") == ""
            assert clarify.get_pending(sid) is None
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_stop_while_waiting_clears_the_card_and_returns_empty(self):
        sid = _sid()
        cancel = threading.Event()
        assert vault_prompts.install_vault_prompts(sid, cancel) is True
        try:
            cb = _agent_callbacks().get_unlock_prompt_callback()
            run = _run_callback(cb, "bitwarden", "Bitwarden")
            _wait_for_pending(sid)
            cancel.set()
            run["join"]()
            assert run["out"]["value"] == ""
            assert clarify.get_pending(sid) is None
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_expiry_returns_empty_and_clears_the_card(self):
        sid = _sid()
        value = vault_prompts._ask(
            sid, None, {"question": "Mot de passe maître Bitwarden", "vault": {"kind": "unlock"}},
            timeout=1,
        )
        assert value == ""
        assert clarify.get_pending(sid) is None

    def test_no_session_answers_empty_without_queuing_anything(self):
        sid = _sid()
        value = vault_prompts._ask(sid if False else "", None, {"question": "x", "vault": {}})
        assert value == ""
        assert clarify.get_pending(sid) is None


class TestAutonomousUnlock:
    """Default mode: the master password comes from the local vault, no card shown."""

    def test_unlock_uses_the_saved_master_password_without_a_card(self, monkeypatch, caplog):
        _install_fake_store(
            monkeypatch,
            [("vault_saved", "https://vault.bitwarden.com", "login", "master-from-vault")],
        )
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            with caplog.at_level(logging.INFO):
                value = _agent_callbacks().get_unlock_prompt_callback()("bitwarden", "Bitwarden")
            assert value == "master-from-vault"
            assert clarify.get_pending(sid) is None, "no card in autonomous mode"
            assert "master-from-vault" not in caplog.text, "the value never reaches a log"
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_an_item_for_another_origin_is_never_borrowed(self, monkeypatch):
        _install_fake_store(
            monkeypatch,
            [("vault_other", "https://example.test", "login", "unrelated-secret")],
        )
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            run = _run_callback(_agent_callbacks().get_unlock_prompt_callback(), "bitwarden", "Bitwarden")
            assert _wait_for_pending(sid)["secret"] is True
            clarify.clear_pending(sid)
            run["join"]()
            assert run["out"]["value"] == ""
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_autonomous_mode_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_VAULT_AUTO_UNLOCK", "0")
        _install_fake_store(
            monkeypatch,
            [("vault_saved", "https://vault.bitwarden.com", "login", "master-from-vault")],
        )
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            run = _run_callback(_agent_callbacks().get_unlock_prompt_callback(), "bitwarden", "Bitwarden")
            assert _wait_for_pending(sid)["secret"] is True
            clarify.clear_pending(sid)
            run["join"]()
            assert run["out"]["value"] == ""
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_the_manager_origin_can_be_overridden_for_self_hosted(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_BITWARDEN_ORIGIN", "https://vault.example.test")
        _install_fake_store(
            monkeypatch,
            [("vault_self", "https://vault.example.test", "login", "vaultwarden-master")],
        )
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            value = _agent_callbacks().get_unlock_prompt_callback()("bitwarden", "Bitwarden")
            assert value == "vaultwarden-master"
        finally:
            vault_prompts.uninstall_vault_prompts()


class TestWiring:
    def test_prompt_binds_the_session_as_the_token_owner(self, monkeypatch):
        api = _agent_callbacks()
        seen: list = []
        monkeypatch.setattr(api, "set_current_session_id", lambda sid: seen.append(sid))
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        try:
            cb = api.get_unlock_prompt_callback()
            run = _run_callback(cb, "bitwarden", "Bitwarden")
            _wait_for_pending(sid)
            clarify.clear_pending(sid)
            run["join"]()
            # Bound on the worker thread, from the closure -- the value the agent needs
            # to release the token when this session ends.
            assert seen and set(seen) == {sid}
        finally:
            vault_prompts.uninstall_vault_prompts()

    def test_install_sets_and_uninstall_clears_the_agent_callbacks(self):
        api = _agent_callbacks()
        sid = _sid()
        assert vault_prompts.install_vault_prompts(sid, None) is True
        assert api.get_unlock_prompt_callback() is not None
        assert api.get_code_prompt_callback() is not None
        vault_prompts.uninstall_vault_prompts()
        assert api.get_unlock_prompt_callback() is None
        assert api.get_code_prompt_callback() is None

    def test_run_installs_the_prompts_and_clears_them_in_the_finally(self):
        src = _read("api/streaming.py")
        assert "from api.vault_prompts import install_vault_prompts" in src
        assert "install_vault_prompts(session_id, cancel_event)" in src
        assert "from api.vault_prompts import uninstall_vault_prompts" in src
        assert "uninstall_vault_prompts()" in src
        # Installed before the agent runs, cleared after it.
        assert src.index("install_vault_prompts(session_id, cancel_event)") < src.index(
            "result = agent.run_conversation(**_run_conversation_kwargs)"
        )
        assert src.index("uninstall_vault_prompts()") > src.index(
            "result = agent.run_conversation(**_run_conversation_kwargs)"
        )


class TestServedFront:
    def test_card_marks_the_input_as_masked(self):
        src = _read("static/messages.js")
        assert "function _syncClarifySecretUi(" in src
        assert "input.setAttribute('type', _clarifySecret ? 'password' : 'text')" in src
        assert "_syncClarifySecretUi(secretPrompt);" in src
        # Re-applied on every render, so reload/poll/second tab cannot un-mask it.
        assert "_syncClarifySecretUi(false);" in src  # card teardown + reset
        assert "pending.secret && pending.vault" in src

    def test_the_answer_is_never_echoed_nor_kept(self):
        src = _read("static/messages.js")
        # No transcript message for a secret answer.
        assert "if (!secret && S.session && S.session.session_id === sid) {" in src
        # Cleared from the DOM as soon as it is read.
        assert "if (secret && input) input.value = '';" in src
        # Never restored on a failed send, never stashed as a composer draft.
        assert "if (_clarifySecret) return false;" in src
        assert "if (!secret) input.value = draft;" in src
        assert "if (!secret) input.value = draft;" in src

    def test_i18n_keys_exist_in_english_and_french(self):
        src = _read("static/i18n.js")
        for key in ("vault_prompt_heading", "vault_prompt_hint", "vault_prompt_placeholder"):
            assert src.count(f"{key}:") >= 2, key
        assert "vault_prompt_heading: 'Masked entry'" in src
        assert "vault_prompt_heading: 'Saisie masquée'" in src
        assert "vault_prompt_placeholder: 'Mot de passe maître" in src

    def test_both_markup_copies_still_carry_the_masked_input_host(self):
        for rel in ("static/index.html", "static/messages.js"):
            src = _read(rel)
            assert "clarifyInput" in src, rel


class TestLiveInjection:
    def test_injected_masked_prompt_reaches_the_card_with_the_secret_flag(self):
        sid = _sid()
        inject = (
            f"{BASE}/api/clarify/inject_test?session_id={sid}"
            "&question=Mot%20de%20passe%20ma%C3%AEtre%20Bitwarden&secret=1"
        )
        with urllib.request.urlopen(inject, timeout=10) as resp:
            assert json.loads(resp.read().decode())["secret"] is True
        try:
            with urllib.request.urlopen(
                f"{BASE}/api/clarify/pending?session_id={sid}", timeout=10
            ) as resp:
                pending = json.loads(resp.read().decode())["pending"]
            # What the served card receives: masked, and tagged as a vault prompt.
            assert pending["secret"] is True
            assert pending["vault"] == {"kind": "unlock", "backend": "bitwarden"}
            assert not pending["choices_offered"]
        finally:
            clarify.clear_pending(sid)
