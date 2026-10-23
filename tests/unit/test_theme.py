import re
from pathlib import Path

import dash_mantine_components as dmc

from logdash.analyses.base import Registry
from logdash.app import theme
from logdash.app.shell import FilterOptions, make_layout
from tests.unit.test_app import app, post  # noqa: F401  (fixture and helper)

SRC = Path(theme.__file__).parents[1]
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(")

NAMED_SCALE = re.compile(r"[\"'](Blues|RdBu_r|Viridis)[\"']")


def test_scheme_logic():
    assert theme.scheme_name("dark") == "dark"
    assert theme.scheme_name("x") == theme.scheme_name(None) == "light"
    assert theme.next_theme("light") == "dark"
    assert theme.next_theme("dark") == "light"
    assert theme.next_theme(None) == "dark"


def test_template_schemes_differ():
    light, dark = theme.template("light"), theme.template(None)
    assert light.layout.font.color != theme.template("dark").layout.font.color
    assert dark.layout.font.color == light.layout.font.color
    assert theme.template("dark").layout.hoverlabel.bgcolor != light.layout.hoverlabel.bgcolor


def test_sequential_scale_differs_by_scheme():
    assert theme.sequential("light") == "Blues"
    dark = theme.sequential("dark")
    assert isinstance(dark, list) and dark[0][0] == 0.0 and dark != theme.sequential("light")


def test_diverging_and_magnitude_scales_suit_dark():
    assert theme.diverging("light") == "RdBu_r"
    dark = theme.diverging("dark")
    assert isinstance(dark, list) and dark[len(dark) // 2][1] == theme.COLORS["dark"].hover_bg
    assert theme.magnitude("light") == theme.magnitude("dark") == "Viridis"


def test_provider_props_follow_os_until_explicit():
    assert theme.provider_props() == {"defaultColorScheme": "auto"}
    assert theme.provider_props("dark")["forceColorScheme"] == "dark"
    assert "forceColorScheme" not in theme.provider_props("auto")


def test_layout_uses_auto_provider_and_toggle():
    layout = make_layout(Registry(), FilterOptions())
    assert layout.defaultColorScheme == "auto"
    assert getattr(layout, "forceColorScheme", None) is None
    text = str(layout)
    assert "theme-toggle" in text and "aria-label" in text
    assert "id='theme'" in text and "id='theme-scheme'" in text


def test_toggle_callback_flips_resolved_scheme(app):  # noqa: F811
    client = app.server.test_client()
    flip = [("theme-toggle", "n_clicks", 1)]
    assert post(client, "theme.data", flip, [("theme-scheme", "data", "light")]) == "dark"
    assert post(client, "theme.data", flip, [("theme-scheme", "data", "dark")]) == "light"


def test_scheme_callback_is_clientside(app):  # noqa: F811
    deps = app.server.test_client().get("/_dash-dependencies").get_json()
    apply = [d for d in deps if "theme-scheme.data" in d["output"]]
    assert apply and apply[0]["clientside_function"]
    assert "mantine-provider.forceColorScheme" in apply[0]["output"]


def test_theme_is_only_known_to_theme_module():
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "theme.py":
            continue
        text = path.read_text()
        assert not COLOUR_LITERAL.search(text), f"colour literal in {path.relative_to(SRC)}"
        assert not NAMED_SCALE.search(text), f"colour scale name in {path.relative_to(SRC)}"
        assert "THEME_STORE_ID" not in text and "SCHEME_STORE_ID" not in text, path.name


def test_toggle_is_a_labelled_tooltip():
    tip = theme.toggle()
    assert isinstance(tip, dmc.Tooltip) and tip.children.id == theme.THEME_TOGGLE_ID
