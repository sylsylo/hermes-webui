"""TTS Edge : attente bornée avant de parler plutôt qu'un blanc en pleine phrase.

Ce que verrouillent ces tests (comportement rapporté depuis l'usage réel) :

1. Le premier tronçon d'un message peut être minuscule — le découpage suit les
   fins de phrase, donc « Oui. » suivi d'une longue phrase sans point donne un
   premier tronçon de 4 caractères (0,7 s de lecture). La synthèse du suivant
   ne tient pas dedans, et le serveur n'accepte de toute façon qu'une requête
   toutes les 2 s : la frontière produisait un blanc de 2,8 à 4,3 s (mesuré).
   Une tête en dessous de ``_EDGE_TTS_MIN_HEAD_MS`` est maintenant fusionnée
   avec le tronçon suivant : la frontière disparaît au lieu d'être attendue.

2. Avant de démarrer la lecture d'un tronçon, on estime la synthèse restante du
   SUIVANT et on la compare à la durée de lecture du tronçon en main (durée
   exacte du tampon décodé). Si ça ne rentre pas, on attend un peu AVANT de
   parler — jamais d'attente quand le suivant est déjà là ou que la marge
   suffit, et plafonnée pour ne pas laisser l'utilisateur dans le vide.

3. Le préchargement lançait deux synthèses d'un coup : la seconde était refusée
   (HTTP 429, 1 requête / 2 s) et le retry à délai fixe de 1,5 s retombait
   régulièrement sur la fenêtre. Les départs sont maintenant étalés et le retry
   recule exponentiellement avec un peu d'aléa.

Les helpers sont exécutés pour de vrai dans node (pas seulement greppés).
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


def _const(name):
    m = re.search(r'const ' + name + r'=(\d+);', UI_JS)
    assert m, f"{name} not found in static/ui.js"
    return int(m.group(1))


def _node(harness):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def _tts_consts():
    found = re.findall(r'^const _EDGE_TTS_[A-Z_]+=.*;$', UI_JS, re.M)
    assert len(found) >= 8, \
        f"expected the Edge TTS constants in static/ui.js, found {found}"
    return "\n".join(found)


def _samples_decl():
    m = re.search(r'^let _edgeTtsSynthSamples=.*;$', UI_JS, re.M)
    assert m, "let _edgeTtsSynthSamples=…; not found in static/ui.js"
    return m.group(0)


def _tts_harness(body):
    """Prepend the REAL estimation/decision functions extracted from ui.js."""
    return "\n".join([
        _tts_consts(),
        _samples_decl(),
        extract_function(UI_JS, "_edgeTtsSynthModel"),
        extract_function(UI_JS, "_edgeTtsSynthEstimateMs"),
        extract_function(UI_JS, "_edgeTtsRecordSynthSample"),
        extract_function(UI_JS, "_edgeTtsPlaybackMs"),
        extract_function(UI_JS, "_edgeTtsHoldMs"),
        extract_function(UI_JS, "_stripForTTS"),
        extract_function(UI_JS, "_splitForTTS"),
        extract_function(UI_JS, "_edgeTtsChunkPlan"),
        extract_function(UI_JS, "_splitEdgeTtsChunks"),
        body,
    ])


LONG_FR = (
    "Le pipeline de prefetch découpe le message en tronçons. "
    "Chaque requête de synthèse coûte du temps de trajet réseau. "
) * 12

# Le cas rapporté : une phrase courte, puis une longue phrase SANS point (donc
# aucun découpage possible avant 240 caractères).
RUNON = (
    "Le pipeline de synthèse vocale découpe le message en tronçons et chaque requête "
    "coûte un aller-retour réseau vers Microsoft ce qui prend du temps à revenir "
)
TINY_HEAD_TEXT = "Oui. " + RUNON + LONG_FR


class TestTinyHeadIsMerged:
    """Un premier tronçon trop court pour masquer le suivant n'est pas gardé."""

    def test_short_first_sentence_does_not_become_a_tiny_first_chunk(self):
        out = _node(_tts_harness(f"""
        const chunks = _splitEdgeTtsChunks({json.dumps(TINY_HEAD_TEXT)});
        console.log(JSON.stringify({{
          first: chunks[0].length,
          minUsable: Math.ceil(_EDGE_TTS_MIN_HEAD_MS / _EDGE_TTS_PLAYBACK_MS_PER_CHAR),
          count: chunks.length,
          empty: chunks.filter(c => !c.length).length,
          overRegular: chunks.filter(c => c.length > _EDGE_TTS_CHUNK_CHARS).length,
          whole: chunks.join(' ').replace(/\\s+/g, ' ') === {json.dumps(TINY_HEAD_TEXT)}.replace(/\\s+/g, ' ').trim(),
          startsWith: chunks[0].slice(0, 5),
        }}));
        """))
        assert out["first"] >= out["minUsable"], (
            f"first chunk is {out['first']} chars — too short to cover the next "
            "synthesis, the boundary would be an audible hole"
        )
        assert out["startsWith"] == "Oui. "[:5] or out["startsWith"].startswith("Oui."), \
            f"the merge must keep the beginning of the message: {out['startsWith']!r}"
        assert out["count"] >= 2, "the rest of a long message must still be split"
        assert out["empty"] == 0
        assert out["overRegular"] == 0, "the merged head must stay within the regular size"
        assert out["whole"] is True, "merging must not drop or duplicate any word"

    def test_a_normal_head_is_left_untouched(self):
        """Pas de fusion quand la tête est déjà utilisable (pas de 1er mot tardif)."""
        out = _node(_tts_harness(f"""
        const chunks = _splitEdgeTtsChunks({json.dumps(LONG_FR)});
        console.log(JSON.stringify({{
          first: chunks[0].length,
          headMax: _EDGE_TTS_FIRST_CHUNK_CHARS,
        }}));
        """))
        assert out["first"] <= out["headMax"], (
            f"a usable head must stay short ({out['first']} chars) — merging it "
            "would delay the first word for nothing"
        )

    def test_short_message_stays_a_single_chunk(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({n: _splitEdgeTtsChunks('Bonjour, court message.').length}));
        """))
        assert out["n"] == 1


class TestPrerollHold:
    """Attendre un peu au début plutôt que laisser un trou au milieu."""

    def test_no_useless_wait(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({
          fits: _edgeTtsHoldMs(10000, 5000, 0, 2600),
          smallNext: _edgeTtsHoldMs(3000, 1000, 0, 2600),
          alreadyDone: _edgeTtsHoldMs(1000, 5000, 6000, 2600),
          noCapGiven: _edgeTtsHoldMs(1000, 9000, 0),
        }));
        """))
        assert out["fits"] == 0, "a next chunk that fits in the playback must not be waited for"
        assert out["smallNext"] == 0
        assert out["alreadyDone"] == 0, "elapsed synthesis time must count as advance"
        assert out["noCapGiven"] == _const('_EDGE_TTS_MAX_HOLD_MS'), \
            "without an explicit cap, the default cap applies"

    def test_wait_is_the_deficit_and_is_capped(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({
          deficit: _edgeTtsHoldMs(2000, 4000, 0, 10000),
          capped: _edgeTtsHoldMs(1000, 20000, 0, 2600),
          remaining: _edgeTtsHoldMs(4000, 12000, 6000, 10000),
          safety: _edgeTtsHoldMs(5000, 4400, 0, 10000),
        }));
        """))
        # 4000 ms de synthèse pour 2000 ms de lecture : l'attente couvre le
        # manque par rapport à la marge utile (2000 - 500 ms de sécurité)
        assert out["deficit"] == 4000 - (2000 - _const('_EDGE_TTS_SYNTH_SAFETY_MS'))
        assert out["capped"] == 2600, "the wait must stay bounded"
        assert out["remaining"] == 12000 - 6000 - (4000 - _const('_EDGE_TTS_SYNTH_SAFETY_MS'))
        assert out["safety"] == 0, "a synthesis that fits within the safety margin is not waited for"


class TestSynthesisEstimate:
    """L'estimation de durée de synthèse se recalibre sur les mesures réelles."""

    def test_default_model_is_monotonic(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({
          zero: _edgeTtsSynthEstimateMs(0),
          small: _edgeTtsSynthEstimateMs(200),
          big: _edgeTtsSynthEstimateMs(1200),
          fixed: _EDGE_TTS_SYNTH_FIXED_MS,
        }));
        """))
        assert out["zero"] == out["fixed"], "an empty request still pays the fixed cost"
        assert out["small"] < out["big"]

    def test_slow_measurements_raise_the_estimate(self):
        out = _node(_tts_harness("""
        const before = _edgeTtsSynthEstimateMs(1200);
        [100, 200, 300].forEach(c => _edgeTtsRecordSynthSample(c, c * 40));
        const after = _edgeTtsSynthEstimateMs(1200);
        const samples = _edgeTtsSynthSamples.length;
        for (let i = 0; i < 40; i++) _edgeTtsRecordSynthSample(300, 9000);
        console.log(JSON.stringify({before, after, samples, bounded: _edgeTtsSynthSamples.length}));
        """))
        assert out["after"] > out["before"], \
            "a synthesis measured at 40 ms/char must raise the estimate for a long chunk"
        assert out["samples"] == 3
        assert out["bounded"] <= _const('_EDGE_TTS_SYNTH_SAMPLES'), \
            "the sample window must stay bounded"

    def test_absurd_measurements_stay_clamped(self):
        out = _node(_tts_harness("""
        [100, 200, 300].forEach(c => _edgeTtsRecordSynthSample(c, 10));
        const fast = _edgeTtsSynthEstimateMs(1200);
        _edgeTtsSynthSamples.length = 0;
        [100, 200, 300].forEach(c => _edgeTtsRecordSynthSample(c, 600000));
        const slow = _edgeTtsSynthEstimateMs(1200);
        console.log(JSON.stringify({fast, slow, sane: fast > 0 && slow < 600000}));
        """))
        assert out["fast"] > 0
        assert out["sane"] is True, "clamped to a plausible range, never absurd"


class TestPlaybackDuration:
    """La durée de lecture vient du tampon décodé quand il est disponible."""

    def test_exact_buffer_duration_wins(self):
        out = _node(_tts_harness("""
        console.log(JSON.stringify({
          exact: _edgeTtsPlaybackMs({duration: 8.88}, 'x'.repeat(1000)),
          estimated: _edgeTtsPlaybackMs(new ArrayBuffer(8), 'x'.repeat(200)),
          empty: _edgeTtsPlaybackMs(null, ''),
        }));
        """))
        assert out["exact"] == 8880, "the decoded duration is exact, it must not be estimated"
        assert out["estimated"] == 200 * _const('_EDGE_TTS_PLAYBACK_MS_PER_CHAR')
        assert out["empty"] == 0


class TestPrefetchSpacing:
    """Une seule requête toutes les 2 s côté serveur : on étale les départs."""

    def test_gap_respects_the_server_window(self):
        assert _const('_EDGE_TTS_REQUEST_GAP_MS') >= 2000, (
            "the gap between request starts must cover the server's 1 request / 2 s window"
        )

    def test_chunked_playback_spaces_requests(self):
        chunked = UI_JS[UI_JS.index('function _playEdgeTtsChunked('):]
        chunked = chunked[:chunked.index('\nfunction speakMessage(')]
        assert '_EDGE_TTS_REQUEST_GAP_MS' in chunked, \
            "the prefetch must space out request starts"
        assert 'setTimeout(res,1500)' not in chunked, \
            "the fixed 1.5 s retry kept landing back inside the 2 s window"
        assert 'Math.pow(_EDGE_TTS_RETRY_FACTOR' in chunked, \
            "a rate-limited request must back off exponentially"

    def test_hold_is_wired_into_the_playback_path(self):
        chunked = UI_JS[UI_JS.index('function _playEdgeTtsChunked('):]
        chunked = chunked[:chunked.index('\nfunction speakMessage(')]
        assert '_edgeTtsHoldMs(' in chunked, \
            "the decision to wait must be taken before starting each chunk"
        assert 'ready.has(nextIdx)' in chunked, \
            "the wait must be skipped entirely when the next chunk is already in hand"

    def test_stopping_cancels_a_pending_hold(self):
        chunked = UI_JS[UI_JS.index('function _playEdgeTtsChunked('):]
        chunked = chunked[:chunked.index('\nfunction speakMessage(')]
        hold_block = chunked[chunked.index('_edgeTtsHoldMs('):]
        assert 'Promise.race(' in hold_block
        assert 'stopped||!_ttsSpeaking' in hold_block, \
            "a stopped reading must not resume after the bounded wait"
