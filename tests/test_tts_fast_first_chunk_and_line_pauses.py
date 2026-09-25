"""TTS read-aloud: fast first chunk, loading ring, real pauses on line breaks.

User-visible behaviours locked here (all three reported from real usage):

1. A long message stayed silent for ~17 s before the first word. Cause: the
   ``edge`` engine requested its FIRST chunk at ``_EDGE_TTS_CHUNK_CHARS`` (1200),
   and synthesis time grows with text length (measured against /api/tts on this
   host: 120 chars → 3.1 s, 400 → 6.7 s, 1200 → 16.6-33 s, 2400 → 37.6 s). The
   chunk sizes now RAMP up from ``_EDGE_TTS_FIRST_CHUNK_CHARS`` (160 → 240 → 360
   → 540 → 810 → 1200), so the first word arrives after a short synthesis while
   every following chunk is still synthesized during the playback of the one
   before it. The plan is executed for real in node below — not just grepped.

2. The speaker button looked inert while synthesis ran. It now carries
   ``data-loading="1"`` while waiting, rendered as a rotating ring.

3. Text with bare line breaks was read as one run-on sentence with no pause:
   the whitespace collapse in ``_stripForTTS`` erased the newlines. A line break
   is now turned into sentence-final punctuation before the collapse.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from tests.js_source_extract import extract_function

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


UI_JS = _read('ui.js')
CSS = _read('style.css')


def _const(name):
    m = re.search(r'const ' + name + r'=(\d+);', UI_JS)
    assert m, f"{name} not found in static/ui.js"
    return int(m.group(1))


def _const_growth():
    m = re.search(r'const _EDGE_TTS_CHUNK_GROWTH=([\d.]+);', UI_JS)
    assert m, "_EDGE_TTS_CHUNK_GROWTH not found in static/ui.js"
    return float(m.group(1))


def _node(harness):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def _tts_consts():
    """The `const _EDGE_TTS_…=…;` lines of ui.js, injected as-is into node."""
    found = re.findall(r'^const _EDGE_TTS_[A-Z_]+=.*;$', UI_JS, re.M)
    assert len(found) >= 4, \
        f"expected the Edge TTS chunk constants in static/ui.js, found {found}"
    return "\n".join(found)


def _tts_harness(body):
    """Prepend the REAL TTS text/chunk functions extracted from ui.js."""
    return "\n".join([
        _tts_consts(),
        extract_function(UI_JS, "_stripForTTS"),
        extract_function(UI_JS, "_splitForTTS"),
        extract_function(UI_JS, "_edgeTtsChunkPlan"),
        extract_function(UI_JS, "_splitEdgeTtsChunks"),
        body,
    ])


LONG_FR = (
    "Le pipeline de prefetch découpe le message en tronçons. "
    "Chaque requête de synthèse coûte du temps de trajet réseau. "
) * 20


class TestFirstChunkStartsPlaybackFast:
    """The first synthesized chunk must be short, so audio starts quickly."""

    def test_first_chunk_is_much_smaller_than_the_regular_chunk(self):
        assert _const('_EDGE_TTS_FIRST_CHUNK_CHARS') <= 320, (
            "the first chunk must stay short — its synthesis time is the entire "
            "delay before the first word (16.6 s measured at 1200 chars)"
        )
        assert _const('_EDGE_TTS_FIRST_CHUNK_CHARS') * 2 < _const('_EDGE_TTS_CHUNK_CHARS'), (
            "the first chunk must be much smaller than the regular chunk size"
        )

    def test_edge_playback_uses_the_short_head_chunk_plan(self):
        assert 'const chunks=_splitEdgeTtsChunks(text);' in UI_JS, (
            "chunked Edge playback must use _splitEdgeTtsChunks, not the plain "
            "1200-char split (regression: ~17 s of silence before the first word)"
        )

    def test_first_chunk_measured_on_real_text(self):
        """Execute the real plan: short head, lossless rest, no mid-word cut."""
        out = _node(_tts_harness(f"""
        const text = {json.dumps(LONG_FR)};
        const chunks = _splitEdgeTtsChunks(text);
        console.log(JSON.stringify({{
          count: chunks.length,
          first: chunks[0].length,
          overRegular: chunks.slice(1).filter(c => c.length > _EDGE_TTS_CHUNK_CHARS).length,
          empty: chunks.filter(c => !c.length).length,
          whole: chunks.join(' ').replace(/\\s+/g, ' ') === text.replace(/\\s+/g, ' ').trim(),
          headEndsClean: /[.!?…]$/.test(chunks[0]),
        }}));
        """))

        assert out["count"] > 2, "a long text must still be split into several chunks"
        assert out["first"] <= _const('_EDGE_TTS_FIRST_CHUNK_CHARS'), \
            f"first chunk is {out['first']} chars — playback would start late"
        assert out["overRegular"] == 0, "follow-up chunks must respect the regular size"
        assert out["empty"] == 0, "empty chunks must never be requested"
        assert out["whole"] is True, "chunking must not drop or duplicate any word"
        assert out["headEndsClean"] is True, \
            "the head chunk should end at a sentence boundary when one is available"

    def test_chunk_sizes_ramp_geometrically(self):
        """The plan must grow by at most the allowed factor at each step.

        Arithmetic behind the guard: a chunk is only synthesized while the one
        before it plays, so gap-free playback needs
        playback(s_i) >= synthesis(s_i+1). Measured here: playback ≈ 0.05 s/char
        vs synthesis ≈ 0.006 s/char (+ a few seconds fixed), so a jump bigger
        than ~1.5x stops fitting inside the previous chunk's playback and the
        next word only arrives after a silence.
        """
        out = _node(_tts_harness("""
        const plan = _edgeTtsChunkPlan();
        console.log(JSON.stringify({
          plan,
          sorted: plan.every((s, i) => i === 0 || s > plan[i-1]),
          maxRatio: Math.max(...plan.slice(1).map((s, i) => s / plan[i])),
        }));
        """))
        plan = out["plan"]
        assert plan[0] == _const('_EDGE_TTS_FIRST_CHUNK_CHARS')
        assert plan[-1] == _const('_EDGE_TTS_CHUNK_CHARS')
        assert out["sorted"] is True, f"plan must be increasing: {plan}"
        assert out["maxRatio"] <= _const_growth() + 0.01, (
            f"plan {plan} grows by {out['maxRatio']}x at a step — a chunk that "
            "much bigger than its predecessor cannot be ready in time"
        )

    def test_two_chunks_are_synthesized_ahead(self):
        assert _const('_EDGE_TTS_PREFETCH_DEPTH') >= 2, (
            "the pipeline must stay a full chunk ahead of playback, so a slow "
            "synthesis round trip is absorbed instead of being waited for"
        )
        chunked = UI_JS[UI_JS.index('function _playEdgeTtsChunked('):]
        chunked = chunked[:chunked.index('\nfunction speakMessage(')]
        assert '_pump(idx+1, _EDGE_TTS_PREFETCH_DEPTH);' in chunked, \
            "playback must pump the prefetch queue, not a single next chunk"

    def test_short_text_is_a_single_chunk(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({n: _splitEdgeTtsChunks('Bonjour, court message.').length}));
        """))
        assert out["n"] == 1


class TestLineBreaksBecomePauses:
    """A bare line break must be spoken as a sentence end, not swallowed."""

    def test_line_break_becomes_sentence_end(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({
          simple: _stripForTTS('Ligne un\\nLigne deux'),
          blank: _stripForTTS('Titre\\n\\nParagraphe suivant'),
          already: _stripForTTS('Déjà fini.\\nSuite de la phrase'),
          question: _stripForTTS('Vraiment ?\\nOui.'),
          trailing: _stripForTTS('Fin du message\\n'),
          collapsed: _stripForTTS('a  b\\n\\n\\n   c'),
        }));
        """))

        assert out["simple"] == "Ligne un. Ligne deux", out["simple"]
        assert out["blank"] == "Titre. Paragraphe suivant", out["blank"]
        assert out["already"] == "Déjà fini. Suite de la phrase", out["already"]
        assert out["question"] == "Vraiment ? Oui.", out["question"]
        assert out["trailing"] == "Fin du message.", out["trailing"]
        assert out["collapsed"] == "a b. c", out["collapsed"]

    def test_no_double_punctuation_is_created(self):
        out = _node(_tts_harness("""
        const samples = [
          'Titre\\ncorps', 'Fin.\\ncorps', 'Fin!\\ncorps', 'Fin?\\ncorps',
          'Fin;\\ncorps', 'Fin :\\ncorps', 'Liste :\\n- un\\n- deux',
          'Texte…\\ncorps', 'guillemets »\\ncorps', 'fichier.md\\ncorps',
        ];
        console.log(JSON.stringify(samples.map(s => _stripForTTS(s))));
        """))
        for cleaned in out:
            assert '..' not in cleaned, cleaned
            assert '!.' not in cleaned and '?.' not in cleaned, cleaned
            assert ':. ' not in cleaned and ':.' not in cleaned.replace(' :.', ''), cleaned
        assert out[6] == "Liste : - un. - deux", out[6]
        assert out[9] == "fichier.md. corps", out[9]

    def test_rule_only_adds_punctuation(self):
        """The line-break rule may ADD punctuation, never drop or rewrite text."""
        out = _node(_tts_harness("""
        const norm = s => s.replace(/\\./g,'').replace(/\\s+/g,' ').trim();
        const samples = ['Alpha\\nbeta', 'Un deux\\nTrois!\\nQuatre', 'x\\n\\ny', 'Fin...\\ncorps'];
        console.log(JSON.stringify(samples.map(s => ({
          same: norm(_stripForTTS(s)) === norm(s),
        }))));
        """))
        assert all(row["same"] for row in out), \
            "the line-break rule must only insert punctuation"


class TestSpeakerButtonShowsLoadingRing:
    """The button must signal that synthesis is running."""

    def test_loading_helpers_exist(self):
        assert 'function _ttsBtnLoadingStart(' in UI_JS
        assert 'function _ttsBtnLoadingStop(' in UI_JS

    def test_click_sets_loading_and_playback_clears_it(self):
        # Set when the click dispatches to an engine…
        assert '  _ttsBtnLoadingStart(btn, 200);' in UI_JS, \
            "speakMessage must arm the loading state before synthesizing"
        # …and cleared when audio actually starts / on every failure path.
        chunked = UI_JS[UI_JS.index('function _playEdgeTtsChunked('):]
        chunked = chunked[:chunked.index('\nfunction speakMessage(')]
        assert chunked.count('_ttsBtnLoadingStop(btn);') >= 3, (
            "the Edge path must clear the loading state on playback start, on "
            "end of the queue and on failure"
        )
        assert '_ttsBtnLoadingStart(btn, 300);' in chunked, (
            "the Edge path must re-arm the ring when a chunk is not ready yet"
        )
        audio_buf = UI_JS[UI_JS.index('function _playAudioBuf('):]
        audio_buf = audio_buf[:audio_buf.index('\nfunction stopTTS(')]
        assert '_ttsBtnLoadingStop(btn);' in audio_buf, \
            "single-request engines (elevenlabs/openai/extension) must clear it too"

    def test_unsupported_browser_clears_the_ring(self):
        """Bailing out must clear the ring, or it spins forever."""
        assert "_ttsBtnLoadingStop(btn);\n    showToast(t('tts_not_supported')" in UI_JS, \
            "the engine='browser' bail-out on a browser without speechSynthesis " \
            "returns after the ring was armed and would leave it spinning"

    def test_stop_clears_a_pending_ring(self):
        stop = UI_JS[UI_JS.index('function stopTTS('):]
        stop = stop[:stop.index('\nfunction autoReadLastAssistant(')]
        assert '_ttsBtnLoadingStop(null);' in stop
        assert '[data-loading="1"]' in stop, \
            "stopping playback must clear the ring on any button still showing it"

    def test_ring_is_styled_and_rotates(self):
        rule = re.search(r'\.msg-tts-btn\[data-loading="1"\]::after\{([^}]*)\}', CSS)
        assert rule, "style.css must style the loading ring of .msg-tts-btn"
        body = rule.group(1)
        assert 'border-radius:50%' in body, "the loading indicator must be a circle"
        assert 'animation:spin' in body, "the circle must rotate"
        assert 'visibility:hidden' in CSS, "the speaker icon must be hidden meanwhile"
