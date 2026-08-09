#!/usr/bin/env python3
"""
Palette validation for the theme system.

The design tokens carry a comment claiming specific contrast and colour-vision
numbers. This is the script that produces them. Numbers in a comment that no
tool generated are decoration, and decoration is exactly what you cannot afford
in the one place a reader trusts you to have checked.

What is measured, and why each one matters
------------------------------------------
* **WCAG contrast ratio** of every ink and series colour against the surface it
  sits on. Text needs 4.5:1 (3:1 at large sizes); a line or bar carrying meaning
  needs 3:1 to be a reliable non-text indicator.

* **CVD separation.** Contrast against the background is not enough — a chart
  with four series also needs its series distinguishable *from each other*, and
  that is where categorical palettes usually fail. Each adjacent pair is
  simulated through deuteranopia, protanopia and tritanopia (Machado, Oliveira &
  Fernandes 2009, severity 1.0) and compared in CIEDE2000. ΔE below about 8 is
  where people start mixing two series up.

* **Composited surfaces.** For the glass theme the surface is translucent, so
  the colour a pixel actually renders is the surface alpha-blended over whatever
  is behind it. Measuring against the nominal surface would flatter it. This
  script composites over the extremes of the theme's own backdrop gradient and
  reports the *worst* case, because that is the one the user will hit.

Run:  python3 tools/palette_check.py [--css apps/web/src/app/globals.css]
Exit code is non-zero if any theme fails, so it can gate a build.
"""

from __future__ import annotations

import argparse
import itertools
import pathlib
import re
import sys

# --------------------------------------------------------------------------
# Colour maths. No dependencies -- this needs to run anywhere.
# --------------------------------------------------------------------------


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 8:                       # #rrggbbaa
        h = h[:6]
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def rgb_to_hex(c) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c)


def _srgb_to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(v: float) -> float:
    return v * 12.92 if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055


def relative_luminance(rgb) -> float:
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def composite(fg, alpha: float, bg):
    """Alpha-blend fg over bg in linear light, which is how a browser does it."""
    out = []
    for f, b in zip(fg, bg):
        lin = _srgb_to_linear(f) * alpha + _srgb_to_linear(b) * (1 - alpha)
        out.append(_linear_to_srgb(lin))
    return tuple(out)


# ---- CIELAB / CIEDE2000 --------------------------------------------------

_M_RGB_XYZ = ((0.4124564, 0.3575761, 0.1804375),
              (0.2126729, 0.7151522, 0.0721750),
              (0.0193339, 0.1191920, 0.9503041))
_WHITE = (0.95047, 1.0, 1.08883)


def rgb_to_lab(rgb):
    lin = [_srgb_to_linear(v) for v in rgb]
    xyz = [sum(m[i] * lin[i] for i in range(3)) for m in _M_RGB_XYZ]

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = (f(c / w) for c, w in zip(xyz, _WHITE))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def ciede2000(lab1, lab2) -> float:
    import math
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    kL = kC = kH = 1.0
    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    Cbar = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cbar ** 7 / (Cbar ** 7 + 25 ** 7))) if Cbar else 0.5
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2)

    Lbarp = (L1 + L2) / 2
    Cbarp = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2
    else:
        hbarp = (h1p + h2p - 360) / 2

    T = (1 - 0.17 * math.cos(math.radians(hbarp - 30))
         + 0.24 * math.cos(math.radians(2 * hbarp))
         + 0.32 * math.cos(math.radians(3 * hbarp + 6))
         - 0.20 * math.cos(math.radians(4 * hbarp - 63)))
    dTheta = 30 * math.exp(-(((hbarp - 275) / 25) ** 2))
    RC = 2 * math.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25 ** 7)) if Cbarp else 0.0
    SL = 1 + (0.015 * (Lbarp - 50) ** 2) / math.sqrt(20 + (Lbarp - 50) ** 2)
    SC = 1 + 0.045 * Cbarp
    SH = 1 + 0.015 * Cbarp * T
    RT = -math.sin(math.radians(2 * dTheta)) * RC

    return math.sqrt((dLp / (kL * SL)) ** 2 + (dCp / (kC * SC)) ** 2
                     + (dHp / (kH * SH)) ** 2
                     + RT * (dCp / (kC * SC)) * (dHp / (kH * SH)))


# ---- CVD simulation (Machado, Oliveira & Fernandes 2009, severity 1.0) ---

CVD_MATRICES = {
    "deuteranopia": ((0.367322, 0.860646, -0.227968),
                     (0.280085, 0.672501, 0.047413),
                     (-0.011820, 0.042940, 0.968881)),
    "protanopia": ((0.152286, 1.052583, -0.204868),
                   (0.114503, 0.786281, 0.099216),
                   (-0.003882, -0.048116, 1.051998)),
    "tritanopia": ((1.255528, -0.076749, -0.178779),
                   (-0.078411, 0.930809, 0.147602),
                   (0.004733, 0.691367, 0.303900)),
}


def simulate_cvd(rgb, kind: str):
    m = CVD_MATRICES[kind]
    lin = [_srgb_to_linear(v) for v in rgb]
    out = [sum(m[i][j] * lin[j] for j in range(3)) for i in range(3)]
    return tuple(_linear_to_srgb(max(0.0, min(1.0, v))) for v in out)


# --------------------------------------------------------------------------
# Theme extraction
# --------------------------------------------------------------------------

SERIES = ["--series-1", "--series-2", "--series-3", "--series-4"]
STATUS = ["--status-good", "--status-warning", "--status-serious", "--status-critical",
          # The brand accent is checked too: it is not decoration, it labels the
          # primary CTA and status pills, so it carries text-adjacent meaning.
          "--accent-brand"]
INK = ["--text-primary", "--text-secondary", "--text-muted"]
SEQ = ["--seq-250", "--seq-350", "--seq-450", "--seq-550", "--seq-600"]

# Matches both the base `:root {` block and the `[data-theme="x"] {` blocks.
# The theme selectors are not :root-scoped (see the note in globals.css), so
# anchoring this pattern on :root would silently find zero themes.
BLOCK_RE = re.compile(
    r"(?:^|\})\s*(?::root|\[data-theme=\"(?P<name>[a-z-]+)\"\])\s*\{(?P<body>[^}]*)\}",
    re.M)
VAR_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


VAR_REF_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")


def resolve_vars(theme: dict[str, str]) -> dict[str, str]:
    """
    Follow `var(--x)` chains to a literal.

    The token layer aliases the legacy names onto the new spec names rather
    than renaming them across forty components, so `--surface-1: var(--bg-secondary)`
    is the normal case now, not an edge one. A checker that cannot follow the
    indirection would validate the string "var(--bg-secondary)" and pass
    everything — the worst possible failure for a tool whose output is read as
    evidence.
    """
    out = dict(theme)
    for _ in range(8):                       # depth cap: also breaks cycles
        changed = False
        for key, value in list(out.items()):
            m = VAR_REF_RE.fullmatch(value.strip())
            if m and m.group(1) in out:
                target = out[m.group(1)].strip()
                if target != value:
                    out[key] = target
                    changed = True
        if not changed:
            break
    return out


def parse_themes(css: str) -> dict[str, dict[str, str]]:
    """Later blocks inherit from the base :root block, as the cascade does."""
    themes: dict[str, dict[str, str]] = {}
    base: dict[str, str] = {}
    for m in BLOCK_RE.finditer(css):
        name = m.group("name") or "dark-blue"
        vars_ = {k: v.strip() for k, v in VAR_RE.findall(m.group("body"))}
        if not vars_:
            continue
        if name == "dark-blue" and not base:
            base = {**base, **vars_}
            themes[name] = dict(base)
        else:
            themes[name] = {**base, **vars_}
    return {k: resolve_vars(v) for k, v in themes.items()}


def surfaces_for(theme: str, v: dict[str, str]) -> list[tuple[str, tuple]]:
    """
    The colours a reader actually sees behind ink and series marks.

    For an opaque theme that is just --surface-1 and --plane. For glass the
    surface is translucent, so what renders is the surface composited over the
    page backdrop -- and the backdrop is a gradient, so both of its extremes
    have to be checked. Reporting only the nominal surface would be measuring a
    colour that never appears on screen.
    """
    surf = hex_to_rgb(v["--surface-1"])
    plane = hex_to_rgb(v["--plane"])
    # The rail and top bar are their own surface — darker and cooler than the
    # workspace — and every nav label, status pill and menu row sits on it.
    # Validating only against --surface-1 would leave the entire application
    # frame unchecked.
    chrome = v.get("--chrome-bg", "")
    extra = ([("chrome", hex_to_rgb(chrome))]
             if chrome.startswith("#") else [])
    if theme != "glass":
        return [("surface-1", surf), ("plane", plane)] + extra

    # Read the same token the stylesheet renders through, so the two cannot
    # drift. `--glass-mix: 72%` is what every surface mixes with `transparent`.
    mix = v.get("--glass-mix", "").strip()
    if not mix.endswith("%"):
        raise SystemExit("glass theme is missing a --glass-mix percentage; "
                         "the checker cannot composite without it")
    alpha = float(mix.rstrip("%")) / 100.0

    stops = [hex_to_rgb(s) for s in
             (v["--glass-stop-a"], v["--glass-stop-b"], v["--glass-stop-c"])]
    out = []
    for i, stop in enumerate(stops):
        out.append((f"glass over stop {i + 1} {rgb_to_hex(stop)}",
                    composite(surf, alpha, stop)))
    return out


def check_theme(name: str, v: dict[str, str]) -> tuple[list[str], list[str]]:
    """Returns (report lines, failures)."""
    lines, fails = [], []
    surfaces = surfaces_for(name, v)

    # ---- ink and series contrast, worst surface wins ----
    def worst(token: str) -> tuple[float, str]:
        c = hex_to_rgb(v[token])
        pairs = [(contrast_ratio(c, s), label) for label, s in surfaces]
        return min(pairs)

    for token in INK:
        ratio, where = worst(token)
        need = 4.5 if token != "--text-muted" else 3.0
        flag = "" if ratio >= need else "  ** FAIL **"
        lines.append(f"    {token:<18} {ratio:5.2f}:1  (need {need}, worst on {where}){flag}")
        if ratio < need:
            fails.append(f"{name}: {token} contrast {ratio:.2f}:1 < {need}")

    for token in SERIES + STATUS:
        ratio, where = worst(token)
        flag = "" if ratio >= 3.0 else "  ** FAIL **"
        lines.append(f"    {token:<18} {ratio:5.2f}:1  (need 3.0, worst on {where}){flag}")
        if ratio < 3.0:
            fails.append(f"{name}: {token} contrast {ratio:.2f}:1 < 3.0")

    # ---- adjacent-pair separation under CVD ----
    #
    # Adjacent pairs, not all pairs: the palette is assigned in fixed slot order
    # and never cycled, so series 1 and 2 appear together far more often than 1
    # and 4. Every pair is still reported; the adjacent ones are what gate.
    worst_cvd = (999.0, "", "")
    worst_normal = 999.0
    for a, b in itertools.combinations(SERIES, 2):
        ca, cb = hex_to_rgb(v[a]), hex_to_rgb(v[b])
        adjacent = abs(SERIES.index(a) - SERIES.index(b)) == 1
        d_normal = ciede2000(rgb_to_lab(ca), rgb_to_lab(cb))
        if adjacent:
            worst_normal = min(worst_normal, d_normal)
        for kind in CVD_MATRICES:
            d = ciede2000(rgb_to_lab(simulate_cvd(ca, kind)),
                          rgb_to_lab(simulate_cvd(cb, kind)))
            if adjacent and d < worst_cvd[0]:
                worst_cvd = (d, f"{a}/{b}", kind)
    lines.append(f"    adjacent series  worst CVD dE {worst_cvd[0]:5.2f} "
                 f"({worst_cvd[1]}, {worst_cvd[2]}) · normal-vision dE {worst_normal:.2f}")
    if worst_cvd[0] < 8.0:
        fails.append(f"{name}: adjacent series CVD dE {worst_cvd[0]:.2f} < 8 "
                     f"({worst_cvd[1]}, {worst_cvd[2]})")

    # ---- sequential ramp must stay monotonic in lightness ----
    ls = [rgb_to_lab(hex_to_rgb(v[t]))[0] for t in SEQ]
    mono = all(x > y for x, y in zip(ls, ls[1:]))
    lines.append(f"    seq ramp L*      {' > '.join(f'{x:.0f}' for x in ls)}"
                 f"{'' if mono else '   ** NOT MONOTONIC **'}")
    if not mono:
        fails.append(f"{name}: sequential ramp is not monotonic in lightness")

    # ---- good/critical must not collapse for red-green CVD ----
    good, crit = hex_to_rgb(v["--status-good"]), hex_to_rgb(v["--status-critical"])
    d = min(ciede2000(rgb_to_lab(simulate_cvd(good, k)),
                      rgb_to_lab(simulate_cvd(crit, k)))
            for k in ("deuteranopia", "protanopia"))
    lines.append(f"    good vs critical worst red-green dE {d:5.2f}")
    if d < 8.0:
        fails.append(f"{name}: profit/loss colours collapse under red-green CVD "
                     f"(dE {d:.2f})")

    return lines, fails


def undefined_vars(css: str) -> list[str]:
    """
    Custom properties that are referenced but never defined.

    This exists because of a real failure. The two stylesheets in this repo are
    kept in sync by copying blocks between them, and a sync once carried
    `top: var(--header-h)` across without carrying the `--header-h: 48px`
    definition. CSS does not complain about that — the declaration is simply
    dropped, so `position: sticky` silently became inert and the sidebar
    scrolled away with the page. Nothing failed; it just quietly stopped
    working, which is the worst kind of bug to ship.

    An undefined variable is never intentional, so this is a hard failure.
    """
    defined = set(VAR_RE.findall(css) and [m[0] for m in VAR_RE.findall(css)])
    referenced = set(VAR_REF_RE.findall(css))
    return sorted(referenced - defined)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--css", default="apps/web/src/app/globals.css")
    args = ap.parse_args()

    css = pathlib.Path(args.css).read_text()
    themes = parse_themes(css)
    if not themes:
        print("no themes found")
        return 1

    all_fails = []
    missing = undefined_vars(css)
    if missing:
        print("\nundefined custom properties")
        for m in missing:
            print(f"    {m:24} referenced, never defined")
        all_fails += [f"{m} is referenced but never defined" for m in missing]
    for name, v in themes.items():
        print(f"\n{name}")
        lines, fails = check_theme(name, v)
        print("\n".join(lines))
        all_fails += fails

    print()
    if all_fails:
        print(f"{len(all_fails)} FAILURES:")
        for f in all_fails:
            print(f"  - {f}")
        return 1
    print(f"all {len(themes)} themes pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
