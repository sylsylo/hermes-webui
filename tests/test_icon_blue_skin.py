"""Icon blue skin registration and navy/cyan palette affordances."""

from pathlib import Path
import re

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
SHARE_HTML = (REPO / "static" / "share.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_icon_blue_skin_is_registered_in_all_files():
    assert "{name:'Icon blue', value:'icon-blue'" in BOOT_JS
    assert "'icon-blue':1" in INDEX_HTML
    assert "'icon-blue':1" in SHARE_HTML
    assert '"icon-blue"' in CONFIG_PY


def test_icon_blue_defines_both_light_and_dark_variants():
    assert ':root[data-skin="icon-blue"]' in CSS
    assert ':root.dark[data-skin="icon-blue"]' in CSS


def test_icon_blue_dark_surfaces_are_the_app_icon_navy():
    # Navy sampled from the Hermes app icon (deep background → emblem backdrop).
    assert "--bg:#04162E" in CSS
    assert "--sidebar:#061E3D" in CSS
    assert "--surface:#082347" in CSS


def test_icon_blue_accent_is_the_cyan_to_blue_emblem_gradient():
    # Dark accent is the emblem's azure core; hover is the emblem's cyan tip,
    # matching the brand pair already used for the logo gradient.
    assert "--accent:#20BAF7" in CSS
    assert "--accent-hover:#08EBF1" in CSS
    assert "--gold:#08EBF1" in CSS


def _light_block() -> str:
    """The light-variant declaration block (`:root[data-skin="icon-blue"]`, no .dark)."""
    start = CSS.index(':root[data-skin="icon-blue"]{')
    return CSS[start : CSS.index("}", start)]


def _token(name: str) -> str:
    match = re.search(rf"--{name}:(#[0-9A-Fa-f]{{6}})", _light_block())
    assert match, f"--{name} not found in the light icon-blue palette"
    return match.group(1)


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _lstar(hex_color: str) -> float:
    y = _luminance(hex_color)
    return 116 * (y ** (1 / 3)) - 16 if y > 0.008856 else 903.3 * y


def test_icon_blue_light_variant_is_aa_readable():
    # The light accent doubles as the user-bubble fill, so white must clear AA on it:
    # the previous #0A7FC4 bubble was only 4.33:1.
    assert _token("accent") == "#0B6FB2"
    assert _contrast("#FFFFFF", _token("accent")) >= 4.5
    for name in ("accent-text", "gold", "code-text", "muted"):
        assert _contrast(_token(name), _token("bg")) >= 4.5, name
    assert _contrast(_token("text"), _token("bg")) >= 7


def test_icon_blue_light_variant_keeps_defaults_value_ladder():
    # Default light: bg 99.0 > sidebar 97.3 > surface 94.2 L* — cards sit *below*
    # the page value. The first frost draft had surface=#FFFFFF (brighter than bg),
    # which flattened the whole layout.
    assert _lstar(_token("bg")) > _lstar(_token("sidebar")) > _lstar(_token("surface"))
    assert _lstar(_token("bg")) - _lstar(_token("surface")) > 3


def test_icon_blue_light_mode_retints_grey_chrome_and_warm_syntax():
    assert ':root[data-skin="icon-blue"]:not(.dark) .composer-box' in CSS
    assert ':root[data-skin="icon-blue"]:not(.dark) .token.keyword' in CSS


def test_icon_blue_primary_buttons_carry_the_emblem_gradient():
    assert "linear-gradient(135deg,#08EBF1,#3889FD)" in CSS
    assert ':root[data-skin="icon-blue"] .send-btn' in CSS


def test_icon_blue_navy_chrome_and_bubbles():
    assert ':root.dark[data-skin="icon-blue"] .sidebar{background:#061E3D;' in CSS
    assert ':root.dark[data-skin="icon-blue"] .composer-box' in CSS
    assert ':root.dark[data-skin="icon-blue"] .session-item.active' in CSS
    assert ':root.dark[data-skin="icon-blue"] .app-dialog' in CSS
    assert ':root.dark[data-skin="icon-blue"] .msg-row[data-role="user"] .msg-body' in CSS


def test_icon_blue_i18n_lists_skin_in_all_locales():
    # There are 15 locales; each should include icon-blue as the trailing skin.
    # 13 locales use ASCII closing paren, 2 Chinese locales use full-width paren.
    assert I18N_JS.count("/icon-blue)") + I18N_JS.count("/icon-blue）") == 15
