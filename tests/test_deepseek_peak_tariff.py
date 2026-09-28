"""Tarif DeepSeek (heures creuses / pleines) dans le panneau du compteur de contexte.

Trois niveaux :
1. execution reelle du coeur extrait de ``static/ui.js`` sous node, avec des instants
   figes — la logique de fenetres (UTC), de week-ends, de jours feries chinois et de
   bascule suivante n'est pas approximative ;
2. cablage de la source (ids DOM, appels ``t()``, tick de 30 s, point ambre) ;
3. parite i18n : les 5 cles existent dans chaque locale (convention du depot,
   meme controle que tests/test_issue1014_model_not_found.py).

Reference des fenetres : https://api-docs.deepseek.com/quick_start/pricing
« Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and
06:00 - 10:00 UTC, Monday through Friday, excluding Chinese public holidays. »)
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "static" / "ui.js"
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

CORE_BEGIN = "// >>> deepseek-tariff-core"
CORE_END = "// <<< deepseek-tariff-core"

KEYS = (
    "deepseek_tariff_offpeak",
    "deepseek_tariff_peak",
    "deepseek_tariff_next_offpeak",
    "deepseek_tariff_next_peak",
    "deepseek_tariff_window",
)

# (instant UTC, heures pleines attendues, prochaine bascule attendue)
PEAK_CASES = [
    # lundi 2026-01-05, 1re fenetre 01:00-04:00
    ("2026-01-05T02:00:00Z", True, "2026-01-05T04:00:00.000Z"),
    # lundi, creux 04:00-06:00 entre les deux fenetres
    ("2026-01-05T05:00:00Z", False, "2026-01-05T06:00:00.000Z"),
    # lundi, 2e fenetre 06:00-10:00
    ("2026-01-05T07:00:00Z", True, "2026-01-05T10:00:00.000Z"),
    # lundi apres les fenetres -> mardi 01:00
    ("2026-01-05T10:30:00Z", False, "2026-01-06T01:00:00.000Z"),
    # samedi -> lundi 01:00 (week-end entierement hors pointe)
    ("2026-01-03T02:00:00Z", False, "2026-01-05T01:00:00.000Z"),
    # vendredi apres-midi -> lundi 01:00
    ("2026-01-02T12:00:00Z", False, "2026-01-05T01:00:00.000Z"),
    # jeudi 1er janvier ferie, puis vendredi ferie, puis week-end -> lundi 5
    ("2026-01-01T02:00:00Z", False, "2026-01-05T01:00:00.000Z"),
    # pleine semaine du Nouvel An chinois (15-23 fevrier) -> mardi 24
    ("2026-02-16T07:00:00Z", False, "2026-02-24T01:00:00.000Z"),
    # dimanche (cas reel de la session d'ecriture) -> lundi 01:00
    ("2026-09-27T21:33:00Z", False, "2026-09-28T01:00:00.000Z"),
]


def _ui() -> str:
    return UI.read_text(encoding="utf-8")


def _core_source() -> str:
    src = _ui()
    start = src.index(CORE_BEGIN)
    end = src.index(CORE_END) + len(CORE_END)
    return src[start:end]


def _clock_source() -> str:
    """Coeur + horloge locale (le texte des fenetres locales en depend)."""
    src = _ui()
    start = src.index("// Horloge locale de l")
    end = src.index("function _syncDeepseekTariffRow(", start)
    assert start < end, "bloc horloge introuvable dans ui.js"
    return _core_source() + "\n" + src[start:end]


def _run_node(script: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    assert NODE is not None
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, env=env)
    return proc


# ── 1. Logique executee sous node ────────────────────────────────────────────

@pytest.mark.skipif(NODE is None, reason="node est requis pour executer le coeur tarifaire")
@pytest.mark.parametrize("tz", ["UTC", "Europe/Paris"])
def test_peak_windows_follow_deepseek_docs(tz):
    """Fenetres pleines, week-ends, feries chinois et bascule suivante.

    Le coeur n'utilise que des getters UTC : le resultat doit etre identique quel que
    soit le fuseau du navigateur (sinon l'affichage local mentirait).
    """
    checks = "\n".join(f"['{iso}',{str(peak).lower()},'{nxt}']," for iso, peak, nxt in PEAK_CASES)
    harness = _core_source() + f"""
const checks=[{checks}];
const bad=[];
for(const [iso,peak,next] of checks){{
  const st=_deepseekPeakState(new Date(iso));
  const got=st.nextAt?st.nextAt.toISOString():null;
  if(st.peak!==peak||got!==next) bad.push(`${{iso}}: peak=${{st.peak}} next=${{got}} attendu peak=${{peak}} next=${{next}}`);
}}
if(bad.length){{console.error(bad.join('\\n'));process.exit(1);}}
console.log('OK');
"""
    proc = _run_node(harness, {"TZ": tz})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "OK"


@pytest.mark.skipif(NODE is None, reason="node est requis pour executer le coeur tarifaire")
def test_route_gate_matches_only_direct_deepseek():
    """OpenRouter, `custom:` et les autres routes ne doivent pas declencher la ligne."""
    harness = _core_source() + """
const cases=[['deepseek',true],['DeepSeek',true],[' deepseek ',true],
  ['openrouter',false],['custom:backup',false],['custom:deepseek',false],
  ['@deepseek:deepseek-flash',false],['',false],[null,false],[undefined,false]];
const bad=cases.filter(([p,exp])=>_deepseekTariffRouteMatches(p)!==exp);
if(bad.length){console.error(JSON.stringify(bad));process.exit(1);}
console.log('OK');
"""
    proc = _run_node(harness)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "OK"


@pytest.mark.skipif(NODE is None, reason="node est requis pour executer le coeur tarifaire")
def test_route_gate_normalises_but_never_unwraps_custom():
    """`custom:deepseek` est un proxy personnel : conditions de facturation differentes."""
    harness = _core_source() + """
const cases=[[' deepseek ',true],['DEEPSEEK',true],['custom:deepseek',false],['openrouter',false],[null,false]];
const bad=cases.filter(([p,exp])=>_deepseekTariffRouteMatches(p)!==exp);
if(bad.length){console.error(JSON.stringify(bad));process.exit(1);}
console.log('OK');
"""
    proc = _run_node(harness)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "OK"


@pytest.mark.skipif(NODE is None, reason="node est requis pour executer le coeur tarifaire")
@pytest.mark.parametrize("when,expected", [
    # hiver (CET, UTC+1) et ete (CEST, UTC+2) : les bornes UTC sont fixes, l'heure
    # locale affichee ne l'est pas. Reference = date du jour, pas une date figee.
    ("2026-01-05T12:00:00Z", "02:00\u201305:00 & 07:00\u201311:00"),
    ("2026-07-06T12:00:00Z", "03:00\u201306:00 & 08:00\u201312:00"),
])
def test_local_windows_follow_the_current_dst_offset(when, expected):
    harness = _clock_source() + f"""
global.document={{documentElement:{{lang:'fr-FR'}}}};
console.log(_deepseekTariffLocalWindowsText(new Date('{when}')));
"""
    proc = _run_node(harness, {"TZ": "Europe/Paris"})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == expected


@pytest.mark.skipif(NODE is None, reason="node est requis pour executer le coeur tarifaire")
def test_local_clock_follows_the_ui_language():
    """Une interface francaise ne doit pas afficher « 03:00 AM » (horloge 12 h)."""
    harness = _clock_source() + """
global.document={documentElement:{lang:'fr-FR'}};
const fr=_deepseekTariffLocalWindowsText(new Date('2026-07-06T12:00:00Z'));
global.document={documentElement:{lang:'en-US'}};
const en=_deepseekTariffLocalWindowsText(new Date('2026-07-06T12:00:00Z'));
if(/AM|PM/.test(fr)){console.error('fr 12h: '+fr);process.exit(1);}
if(!/03:00/.test(fr)){console.error('fr: '+fr);process.exit(1);}
if(!/AM/.test(en)){console.error('en: '+en);process.exit(1);}
console.log('OK');
"""
    proc = _run_node(harness, {"TZ": "Europe/Paris"})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "OK"


# ── 2. Cablage de la source ──────────────────────────────────────────────────

class TestWiring:
    def test_panel_row_present_after_context_row(self):
        """La ligne tarifaire vit dans le panneau, apres la ligne contexte."""
        panel = HTML[HTML.index('id="composerMobileConfigPanel"'):HTML.index('<div class="profile-dropdown"')]
        assert 'id="composerTariffRow"' in panel
        assert panel.index('id="composerMobileContextAction"') < panel.index('id="composerTariffRow"')

    def test_panel_row_not_in_composer_right(self):
        """Elle ne prend pas un nouveau creneau du composer (contrainte mobile #1381)."""
        right = HTML[HTML.index('<div class="composer-right">'):HTML.index('<div class="composer-mobile-config-panel"')]
        assert 'id="composerTariffRow"' not in right

    def test_tooltip_line_present(self):
        assert 'id="ctxTooltipDeepseek"' in HTML

    def test_js_uses_translated_strings_and_ids(self):
        src = _ui()
        for key in KEYS:
            assert f"t('{key}'" in src, f"{key} doit etre consommee via t()"
        for element_id in ("composerTariffRow", "composerTariffLabel", "composerTariffNext",
                           "composerTariffWindow", "ctxTooltipDeepseek"):
            assert f"'{element_id}'" in src, f"#{element_id} doit etre lu par ui.js"

    def test_respects_hide_composer_context(self):
        src = _ui()
        body = src[src.index("function _syncDeepseekTariffRow"):]
        body = body[:body.index("\n}\n")]
        assert "hide_composer_context" in body, "la ligne doit suivre le masquage du controle contexte"

    def test_refreshed_on_panel_open_and_by_timer(self):
        src = _ui()
        opener = src[src.index("function openMobileComposerConfig"):]
        opener = opener[:opener.index("\n}\n")]
        assert "_syncDeepseekTariffRow()" in opener, "recalcul a l'ouverture du panneau"
        assert "_deepseekTariffTimer" in src, "tick periodique pour rester juste a la bascule"
        indicator = src[src.index("function _syncCtxIndicator"):]
        indicator = indicator[:indicator.index("\n}\n")]
        assert "_syncDeepseekTariffRow()" in indicator, (
            "le changement de session doit recalculer l'etat tarifaire"
        )

    def test_no_cost_caveat_anywhere(self):
        """Aucun texte d'avertissement de cout : retire le 2026-09-27 (choix utilisateur)."""
        assert "cost_caveat" not in HTML
        assert "cost_caveat" not in UI.read_text(encoding="utf-8")
        assert "cost_caveat" not in I18N.read_text(encoding="utf-8")
        assert "tariff-caveat" not in CSS

    def test_peak_dot_on_context_indicator(self):
        """Le point ambre de la pastille suit l'etat, et son libelle est accessible."""
        src = _ui()
        body = src[src.index("function _syncDeepseekTariffRow"):]
        body = body[:body.index("\n}\n")]
        assert "classList.toggle('ctx-peak',st.peak)" in body
        assert "_deepseekTariffAriaSuffix" in body
        indicator = src[src.index("function _syncCtxIndicator"):]
        indicator = indicator[:indicator.index("\n}\n")]
        # Le suffixe n'est ajoute qu'en heures pleines (variable vide sinon).
        assert "if(_deepseekTariffAriaSuffix) label+=" in indicator

    def test_styles_present(self):
        for selector in (".composer-mobile-tariff-action", ".composer-tariff-pill--offpeak",
                         ".composer-tariff-pill--peak", ".ctx-tooltip-line--deepseek",
                         ".ctx-indicator.ctx-peak::after"):
            assert selector in CSS, f"{selector} manquant dans style.css"

    def test_tariff_kicker_stays_translatable_safe(self):
        """La kicker « DeepSeek » est un nom de marque : pas de cle i18n inexistante."""
        row = HTML[HTML.index('id="composerTariffRow"'):]
        row = row[:row.index('</div>\n          </div>')]
        assert 'data-i18n="composer_mobile_tariff"' not in row

    def test_holiday_list_documents_its_source_and_year(self):
        """La liste des feries chinois doit rester datee et tracable (maintenance annuelle)."""
        core = _core_source()
        assert "api-docs.deepseek.com/quick_start/pricing" in core
        assert "2026-02-15" in core and "2026-10-07" in core


# ── 3. Parite i18n ───────────────────────────────────────────────────────────

def test_window_line_shows_local_time_only():
    """La ligne affichee ne cite que l'heure locale : aucune reference UTC (choix utilisateur)."""
    src = I18N.read_text(encoding="utf-8")
    values = re.findall(r"deepseek_tariff_window: '([^']*)'", src)
    assert len(values) >= 15, f"valeurs trouvees : {len(values)}"
    for v in values:
        assert "UTC" not in v, v
        assert "{0}" in v, v


def test_all_locales_have_tariff_keys():
    """Les 6 cles doivent exister dans chaque locale (meme controle que #1014)."""
    src = I18N.read_text(encoding="utf-8")
    locales = re.findall(r"^\s{2}(?:'([A-Za-z0-9-]+)'|([A-Za-z0-9-]+))\s*:\s*\{", src, re.MULTILINE)
    locale_count = len(locales)
    assert locale_count >= 15, f"locales detectees : {locale_count}"
    for key in KEYS:
        count = len(re.findall(r"\b" + re.escape(key) + r"\b", src))
        assert count >= locale_count, f"{key} present {count} fois, attendu >= {locale_count}"
