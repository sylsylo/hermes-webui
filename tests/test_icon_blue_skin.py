"""Icon blue skin registration, light "slate/sky" palette (Caducée mockup) and
navy/cyan dark palette affordances."""

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


def _icon_blue_section() -> str:
    """Everything the skin declares, from its banner comment to the #594 light block."""
    start = CSS.index("/* ═══ ICON BLUE")
    return CSS[start : CSS.index("/* #594: app-dialog light mode overrides", start)]


def _light_scope_rules() -> list[tuple[str, str]]:
    """Rules of the skin section that actually apply in light mode.

    A rule applies in light when its selector targets the skin (or :root) without
    being dark-scoped — the shared rules carrying the dark navy/cyan values are
    exactly the leak the audit test below guards against. Note `:not(.dark)` also
    contains the string ".dark", so the negated form is stripped before testing.
    """
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", _icon_blue_section())
    out = []
    for selector, declarations in rules:
        # The greedy selector capture drags the preceding comment along.
        selector = " ".join(re.sub(r"/\*.*?\*/", "", selector, flags=re.S).split())
        if "data-skin" not in selector:
            continue
        if ".dark" in selector.replace(":not(.dark)", ""):
            continue
        out.append((selector, declarations))
    return out


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
    # The light accent doubles as the user-bubble fill, so white must clear AA on it.
    # The mockup's own #0284C7 only reaches 4.10:1 with white, so the fill stays at
    # sky-700 while the brighter mockup blue is reserved for non-text surfaces.
    assert _token("accent") == "#0369A1"
    assert _contrast("#FFFFFF", _token("accent")) >= 4.5
    for name in ("accent-text", "gold", "code-text", "muted"):
        assert _contrast(_token(name), _token("bg")) >= 4.5, name
    assert _contrast(_token("text"), _token("bg")) >= 7


def test_icon_blue_light_surfaces_sit_above_the_page():
    # The mockup puts WHITE cards on a near-white slate page — the opposite of the
    # Default theme's value ladder, where cards sit *below* the page value. A white
    # card has no value contrast to lean on: the hairline and shadow do the work.
    assert _token("surface") == "#FFFFFF"
    assert _token("bg") == "#F8FAFC"
    assert _lstar(_token("surface")) > _lstar(_token("bg")) >= _lstar(_token("sidebar"))
    assert _lstar(_token("surface")) - _lstar(_token("bg")) > 1


def test_icon_blue_light_palette_is_slate_and_sky():
    assert _token("border") == "#E2E8F0"
    assert _token("text") == "#0F172A"
    assert _token("accent-bg") == "#E0F2FE"
    assert _token("accent-bg-strong") == "#BAE6FD"
    # The mockup's sky-600 lives on, but only where it paints no text.
    assert _token("info") == "#0284C7"


def test_icon_blue_light_mode_retints_grey_chrome_and_warm_syntax():
    assert ':root[data-skin="icon-blue"]:not(.dark) .composer-box' in CSS
    assert ':root[data-skin="icon-blue"]:not(.dark) .token.keyword' in CSS


def test_icon_blue_light_cards_are_white_with_slate_hairlines():
    assert ':root[data-skin="icon-blue"]:not(.dark) .tool-card' in CSS
    assert ':root[data-skin="icon-blue"]:not(.dark) .session-item.active' in CSS
    assert ':root[data-skin="icon-blue"]:not(.dark) .chip' in CSS
    assert ':root[data-skin="icon-blue"]:not(.dark) .msg-body th' in CSS
    # The active session row is the mockup's secondary-container pill.
    scope = dict(_light_scope_rules())
    active = scope[':root[data-skin="icon-blue"]:not(.dark) .session-item.active']
    assert "background:var(--accent-bg)" in active
    assert "border:1px solid var(--accent-bg-strong)" in active


def test_icon_blue_primary_buttons_carry_the_emblem_gradient_in_dark_only():
    # Gradient = the emblem identity, kept for the night surfaces…
    assert "linear-gradient(135deg,#08EBF1,#3889FD)" in CSS
    assert ':root.dark[data-skin="icon-blue"] .send-btn' in CSS
    # …and replaced in light mode by the mockup's flat sky-600 with white ink.
    scope = dict(_light_scope_rules())
    send = scope[':root[data-skin="icon-blue"]:not(.dark) .new-chat-btn, '
                ':root[data-skin="icon-blue"]:not(.dark) .send-btn']
    assert "background:#0284C7" in send
    assert "background-image:none" in send
    assert "color:#FFFFFF" in send


def test_icon_blue_light_rules_never_leak_dark_navy_or_cyan():
    # Several shared skin rules (button gradient, tool cards, scrollbar, badges,
    # lightbox, ::selection) used to be unscoped and painted the dark cyan/navy
    # values on the pale light page. No rule applying in light may carry them.
    forbidden = (
        "rgba(32,186,247",
        "rgba(8,235,241",
        "#08EBF1",
        "#20BAF7",
        "#04162E",
        "#061E3D",
        "#082347",
        # the previous light "frost" palette
        "rgba(11,111,178",
        "#0B6FB2",
        "#F7FBFE",
        "#EFF6FC",
        "#C4D9EC",
        # warm-grey / cream chrome
        "rgba(0,0,0,",
        "#F0EDE8",
    )
    leaks = [
        f"{selector} -> {token}"
        for selector, declarations in _light_scope_rules()
        for token in forbidden
        if token in declarations
    ]
    assert not leaks, "dark/frost/warm colour leaked into the light scope:\n" + "\n".join(leaks)


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
