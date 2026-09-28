"""Masked vault prompts (password managers) for the WebUI.

Hermes asks the *surface* for password-manager material: the master password that
unlocks a manager (``bw unlock``, ``op signin``), and the one-time code a site just
sent. The CLI and the TUI install those callbacks; the WebUI did not, so every
``browser_vault_*`` call in a WebUI session answered ``prompt_unavailable`` and a
locked manager could never be used from the browser.

This module installs WebUI-backed callbacks. They reuse the clarify card plumbing --
pending entry in :mod:`api.clarify`, the 3 s poll, ``POST /api/clarify/respond`` --
because that machinery already survives a reload and drives every open tab. Two
differences from a clarify question, enforced here and in ``static/messages.js``:

* the card renders a **masked** input (``secret: true`` in the payload), and
* the answer is **never echoed**: no transcript message, no composer draft, no log.

**Autonomous mode (default).** For a manager unlock the password is not asked for at
all: it is read from the local vault item the user saved for the manager's own
web-vault origin (``_MANAGER_ORIGINS`` / ``HERMES_WEBUI_<BACKEND>_ORIGIN``) and handed
straight to the CLI's environment. That is what makes a shared Bitwarden login usable
without the user retyping the master password every 30 minutes. The masked card stays
as the fallback -- no matching item, or ``HERMES_WEBUI_VAULT_AUTO_UNLOCK=0`` -- and it
is the only path for a one-time code, which has no stored equivalent.

The value travels browser -> POST -> this process' memory -> the vault backend, or
vault -> CLI environment in autonomous mode. It is never written to disk and never
becomes a tool result: the tools that ask report only counts (``filled_fields``), by
construction of the agent-side vault tools.

Threading: the agent-side registry is thread-local and tool workers only inherit the
*callbacks* (``tools.thread_context.propagate_context_to_thread``), never this
module's state. Each installed callback therefore carries its session in a closure
instead of reading a thread-local -- otherwise a prompt issued from a tool worker
would find no session and silently answer "".
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# Generous on purpose: the run is blocked on the answer, and a manager unlock is a
# deliberate human act (fetch the phone, type a long master password). The card shows
# the countdown and can be left alone; the Stop button remains the escape hatch.
DEFAULT_PROMPT_TIMEOUT_S = 600

_POLL_SLICE_S = 0.5

# Where each manager keeps the web vault a local login item would be bound to. A local
# vault item saved for that exact origin holds the manager's own master password, which
# is what autonomous mode hands to the CLI. Overridable per backend for self-hosted
# setups (Vaultwarden, 1Password Connect): HERMES_WEBUI_<BACKEND>_ORIGIN.
_MANAGER_ORIGINS = {"bitwarden": "https://vault.bitwarden.com"}


def _vault_unlock_api():
    """The agent-side callback registry (lazy: the WebUI must import without it)."""
    from agent.vault_backends import unlock as vault_unlock

    return vault_unlock


def _cancel_requested(cancel_event) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except Exception:
        return False


def _ask(session_key: str, cancel_event, data: dict, *, timeout: int = DEFAULT_PROMPT_TIMEOUT_S) -> str:
    """Park a masked prompt on the clarify card and return the typed value.

    Returns ``""`` when the user cancelled (Stop), the prompt expired, or the session
    went away -- every one of which the vault tools treat as "the user did not
    answer". The value is copied out once and dropped from the entry.
    """
    if not session_key:
        return ""
    # Stop pressed before we got here: show nothing rather than a card that is
    # already dead.
    if _cancel_requested(cancel_event):
        return ""
    from api import clarify

    payload = dict(data)
    payload["secret"] = True
    payload["timeout_seconds"] = int(timeout)
    entry = clarify.submit_pending(session_key, payload)
    deadline = time.monotonic() + timeout
    try:
        while not entry.event.wait(_POLL_SLICE_S):
            if _cancel_requested(cancel_event):
                clarify.clear_pending(session_key)
                return ""
            if time.monotonic() >= deadline:
                clarify.clear_pending(session_key)
                return ""
    finally:
        value = entry.result or ""
        entry.result = None
    return value


def _unlock_data(backend_name: str, display_name: str) -> dict:
    label = (display_name or backend_name or "gestionnaire").strip()
    return {
        "question": f"Mot de passe maître {label}",
        "vault": {"kind": "unlock", "backend": backend_name},
    }


def _code_data(site: str, hint: str) -> dict:
    site = (site or "ce site").strip()
    question = f"Code de vérification pour {site}"
    if hint:
        question += f" ({hint})"
    return {"question": question, "vault": {"kind": "code", "site": site}}


def _bind_owner_session(session_key: str) -> None:
    """Record this session as the owner of any token the unlock is about to mint.

    ``unlock._current_session_tls`` is thread-local and is NOT one of the values
    ``propagate_context_to_thread`` copies into tool workers, so the binding has to
    happen on the worker thread that actually runs the unlock -- which is where these
    prompts run. Without it the token has no owner and cannot be released early; it
    would linger until the idle TTL.
    """
    try:
        _vault_unlock_api().set_current_session_id(session_key)
    except Exception:
        logger.debug("Could not bind the vault session owner", exc_info=True)


def _auto_unlock_enabled() -> bool:
    """Autonomous unlock is the default here; set HERMES_WEBUI_VAULT_AUTO_UNLOCK=0 to
    fall back to asking the user with a masked card on every unlock."""
    raw = (os.environ.get("HERMES_WEBUI_VAULT_AUTO_UNLOCK", "1") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _manager_origin(backend_name: str) -> str:
    override = os.environ.get(f"HERMES_WEBUI_{backend_name.upper()}_ORIGIN", "")
    return (override or _MANAGER_ORIGINS.get(backend_name, "")).strip()


def _local_manager_password(backend_name: str) -> str:
    """The manager's master password, read from the local vault item bound to the
    manager's own web-vault origin.

    This is the non-browser twin of ``browser_vault_fill``: the value goes from the
    encrypted local vault straight into the CLI's environment, never into a tool
    result, a log line or this process' argv. Empty string when the user has no such
    item saved -- the caller then falls back to the masked card.
    """
    origin = _manager_origin(backend_name)
    if not origin:
        return ""
    try:
        from agent.vault_store import get_vault_store

        store = get_vault_store()
        for meta in store.list_items():
            if meta.kind != "login" or str(meta.origin or "") != origin:
                continue
            password = str((store.resolve_secret(meta.id) or {}).get("password") or "")
            if password:
                # Item ids and origins only: the value never reaches a log.
                logger.info(
                    "Unlocking %s from the local vault item %s saved for %s",
                    backend_name, meta.id, origin,
                )
                return password
    except Exception:
        logger.debug("Local vault lookup for %s failed", backend_name, exc_info=True)
    return ""


def install_vault_prompts(session_key: str, cancel_event=None) -> bool:
    """Install this session's masked prompts on the CURRENT thread.

    Call from the thread that runs the agent. Returns False (and installs nothing)
    when the agent-side registry is unavailable, so a WebUI that cannot import the
    agent still runs -- the vault tools then report ``prompt_unavailable`` as before.
    """
    try:
        api = _vault_unlock_api()
        set_unlock = api.set_unlock_prompt_callback
        set_code = api.set_code_prompt_callback
    except Exception:
        logger.debug("Vault prompt callbacks unavailable", exc_info=True)
        return False

    def unlock_prompt(backend_name: str, display_name: str) -> str:
        _bind_owner_session(session_key)
        if _auto_unlock_enabled():
            password = _local_manager_password(backend_name)
            if password:
                return password
        return _ask(session_key, cancel_event, _unlock_data(backend_name, display_name))

    def code_prompt(site: str, hint: str) -> str:
        _bind_owner_session(session_key)
        return _ask(session_key, cancel_event, _code_data(site, hint))

    set_unlock(unlock_prompt)
    set_code(code_prompt)
    return True


def uninstall_vault_prompts() -> None:
    """Drop the callbacks installed by :func:`install_vault_prompts` (run end).

    Mandatory: the callback slots live on the (pooled) run thread, so a stale
    installation would let the next run's prompt land in the previous session.
    """
    try:
        api = _vault_unlock_api()
        api.set_unlock_prompt_callback(None)
        api.set_code_prompt_callback(None)
    except Exception:
        logger.debug("Vault prompt callbacks could not be cleared", exc_info=True)
