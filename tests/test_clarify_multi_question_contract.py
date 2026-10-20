"""Regression: the WebUI clarify bridge must honour the Hermes >= 0.21 callback contract.

Hermes calls ``clarify_callback(questions)`` with a *list* of normalized question dicts and
expects ``{"answers": {qid: raw}, "outcome": ...}`` back (``tools/clarify_tool.py::_result``).
The bridge used to be declared ``lambda question, choices: ...``, so EVERY clarify call died
with "missing 1 required positional argument: 'choices'" before the card could appear.

These tests pin the shape end-to-end (bridge -> core ``_result``) without a live service:
``submit_pending`` is stubbed to resolve the entry immediately, so no browser is involved.
"""

import json
import os
import queue  # noqa: F401  (conftest parity with the other clarify tests)
import sys
import threading

import pytest

_AGENT_DIR = "/usr/local/lib/hermes-agent"


@pytest.fixture()
def streaming_mod():
    from api import streaming
    return streaming


@pytest.fixture()
def clarify_mod():
    from api import clarify
    return clarify


@pytest.fixture(autouse=True)
def _cleanup_subscribers(clarify_mod):
    yield
    clarify_mod._clarify_sse_subscribers.clear()
    clarify_mod._gateway_queues.clear()
    clarify_mod._pending.clear()


def _questions(*specs):
    """Normalized question dicts, as ``clarify_tool._normalize_questions`` emits them."""
    out = []
    for index, (text, choices) in enumerate(specs):
        out.append({
            "qid": f"q{index}",
            "question": text,
            "choices": list(choices) if choices else None,
            "choices_offered": list(choices) if choices else None,
            "multi_select": False,
        })
    return out


class TestBridgeContract:
    def test_returns_answers_keyed_by_qid(self, streaming_mod, monkeypatch, clarify_mod):
        seen = []

        def fake_submit(sid, data):
            seen.append(dict(data))
            entry = clarify_mod.submit_pending(sid, data)
            entry.result = "Oui, nettoyage complet"
            entry.event.set()
            return entry

        monkeypatch.setattr(clarify_mod, "submit_pending", fake_submit)
        reply = streaming_mod._clarify_callback_impl(
            _questions(("Je pars sur ce plan ?", ["Oui", "Non"])), "sess-bridge", threading.Event()
        )
        assert reply == {"answers": {"q0": "Oui, nettoyage complet"}, "outcome": "submitted"}
        assert seen[0]["choices_offered"] == ["Oui", "Non"]
        assert seen[0]["kind"] == "clarify"

    def test_multi_question_is_asked_sequentially(self, streaming_mod, monkeypatch, clarify_mod):
        texts = []

        def fake_submit(sid, data):
            texts.append(data["question"])
            entry = clarify_mod.submit_pending(sid, data)
            entry.result = f"réponse {len(texts)}"
            entry.event.set()
            return entry

        monkeypatch.setattr(clarify_mod, "submit_pending", fake_submit)
        reply = streaming_mod._clarify_callback_impl(
            _questions(("Première ?", None), ("Seconde ?", None)), "sess-multi", threading.Event()
        )
        assert reply["outcome"] == "submitted"
        assert reply["answers"] == {"q0": "réponse 1", "q1": "réponse 2"}
        # Cards stay self-describing, and de-duplication cannot collapse two questions.
        assert texts == ["(1/2) Première ?", "(2/2) Seconde ?"]

    def test_timeout_returns_timed_out_with_notice(self, streaming_mod, monkeypatch, clarify_mod):
        monkeypatch.setattr(clarify_mod, "submit_pending",
                            lambda sid, data: clarify_mod.submit_pending(sid, data))
        reply = streaming_mod._clarify_callback_impl(
            _questions(("Personne ne répond ?", None)), "sess-timeout", threading.Event(), timeout=0.01
        )
        assert reply["outcome"] == "timed_out"
        assert reply["answers"] == {}
        assert "time limit" in reply["notice"]

    def test_cancelled_run_is_reported_as_cancelled(self, streaming_mod, monkeypatch, clarify_mod):
        cancel = threading.Event()
        cancel.set()
        monkeypatch.setattr(clarify_mod, "submit_pending",
                            lambda sid, data: clarify_mod.submit_pending(sid, data))
        reply = streaming_mod._clarify_callback_impl(
            _questions(("Stop cliqué ?", None)), "sess-cancel", cancel, timeout=0.01
        )
        assert reply["outcome"] == "cancelled"


class TestContractWithHermesCore:
    """The dict the bridge returns must satisfy ``clarify_tool._result`` as-is."""

    @pytest.fixture(autouse=True)
    def _agent_on_path(self):
        if not os.path.isdir(_AGENT_DIR):
            pytest.skip("hermes-agent install not present")
        if _AGENT_DIR not in sys.path:
            sys.path.insert(0, _AGENT_DIR)

    def test_result_json_maps_answers(self, streaming_mod, monkeypatch, clarify_mod):
        from tools import clarify_tool

        def fake_submit(sid, data):
            entry = clarify_mod.submit_pending(sid, data)
            entry.result = "Oui (Recommended)"
            entry.event.set()
            return entry

        monkeypatch.setattr(clarify_mod, "submit_pending", fake_submit)
        normalized, error = clarify_tool._normalize_questions(
            [{"question": "On y va ?", "choices": ["Oui", "Non"]}]
        )
        assert error is None
        reply = streaming_mod._clarify_callback_impl(normalized, "sess-core", threading.Event())
        payload = json.loads(clarify_tool._result(normalized, reply))
        assert payload["outcome"] == "submitted"
        assert payload["responses"][0]["status"] == "answered"
        # Presentation label must not leak into the answer the agent sees.
        assert payload["responses"][0]["user_response"] == "Oui"


class TestCallbackWiring:
    """The registered lambda must accept the single positional list Hermes sends."""

    def test_registration_uses_single_argument(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "api", "streaming.py"),
                   encoding="utf-8").read()
        assert "lambda questions: _clarify_callback_impl(" in src
        assert "lambda question, choices:" not in src
