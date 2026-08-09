"""
Palette gate.

The theme tokens live in CSS and the validator is a standalone script, so
nothing would ordinarily stop a colour change from shipping unmeasured. This
wires the check into the same `pytest` run as everything else: change a hex,
break the build.

The specific regression it exists to prevent already happened once. The
original profit/loss pair passed every contrast check and still collapsed to a
CIEDE2000 distance of 5.4 under red-green colour blindness — the most important
colour distinction in the product, invisible to roughly 8% of men, and
undetectable by looking at it.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
CHECKER = ROOT / "tools" / "palette_check.py"

STYLESHEETS = [
    ROOT / "apps" / "web" / "src" / "app" / "globals.css",
    ROOT / "tools" / "artifact" / "base.css",
]


@pytest.mark.parametrize("css", STYLESHEETS, ids=lambda p: p.parent.name)
def test_every_theme_passes_contrast_and_cvd(css: pathlib.Path):
    if not CHECKER.exists() or not css.exists():
        pytest.skip(f"{css} not present in this checkout")
    proc = subprocess.run([sys.executable, str(CHECKER), "--css", str(css)],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("css", STYLESHEETS, ids=lambda p: p.parent.name)
def test_all_five_themes_are_present(css: pathlib.Path):
    """
    The app and the standalone artifact ship the same five themes. If one
    stylesheet gains a theme and the other does not, the settings panel offers
    a choice that does nothing on one of the two surfaces.
    """
    if not css.exists():
        pytest.skip(f"{css} not present in this checkout")
    sys.path.insert(0, str(ROOT / "tools"))
    from palette_check import parse_themes                    # noqa: PLC0415

    themes = parse_themes(css.read_text())
    assert set(themes) == {"dark-blue", "light-black", "eggshell", "glass", "midnight"}, sorted(themes)


def test_the_checker_actually_fails_on_a_bad_palette(tmp_path: pathlib.Path):
    """
    A validator that cannot fail is worse than none, because it is read as
    evidence. This feeds it a theme whose series colours are four shades of the
    same blue -- fine on contrast, indistinguishable as a categorical set.
    """
    if not CHECKER.exists():
        pytest.skip("checker not present")
    good = STYLESHEETS[0].read_text()
    # Derive the sabotage from the live palette rather than hard-coding a hex:
    # a literal here goes stale the moment a colour changes, and a mutation
    # that no longer applies turns this into a test that always passes.
    sys.path.insert(0, str(ROOT / "tools"))
    from palette_check import parse_themes                     # noqa: PLC0415
    series_1 = parse_themes(good)["dark-blue"]["--series-1"]
    series_2_line = [ln for ln in good.splitlines()
                     if ln.strip().startswith("--series-2:")][0]
    bad = good.replace(series_2_line, f"  --series-2: {series_1};", 1)
    assert bad != good, "sabotage did not apply — the test would pass vacuously"
    css = tmp_path / "bad.css"
    css.write_text(bad)
    proc = subprocess.run([sys.executable, str(CHECKER), "--css", str(css)],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode != 0, "checker passed a palette with two near-identical series"
    assert "CVD dE" in proc.stdout


@pytest.mark.parametrize("css", STYLESHEETS, ids=lambda p: p.parent.name)
def test_no_undefined_custom_properties(css: pathlib.Path):
    """
    Every `var(--x)` must have a matching `--x:` definition somewhere.

    A dangling reference is not a CSS error — the browser drops the whole
    declaration and carries on. That is how `top: var(--header-h)` shipped once
    with no `--header-h` defined: `position: sticky` became inert and the
    sidebar scrolled off with the page, with nothing anywhere reporting a
    problem. The two stylesheets are kept in sync by copying blocks between
    them, so this class of mistake is structural rather than careless, and it
    needs a test rather than care.
    """
    if not css.exists():
        pytest.skip(f"{css} not present in this checkout")
    sys.path.insert(0, str(ROOT / "tools"))
    from palette_check import undefined_vars                  # noqa: PLC0415

    missing = undefined_vars(css.read_text())
    assert not missing, f"{css.name} references undefined custom properties: {missing}"
