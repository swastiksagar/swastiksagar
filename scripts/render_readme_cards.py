#!/usr/bin/env python3
"""Render the GitHub profile README as a stack of animated SVG cards.

    python scripts/render_readme_cards.py          # animated cards -> cards/
    STATIC=1 python scripts/render_readme_cards.py # final frame only (for screenshots)
    DEMO=1   python scripts/render_readme_cards.py # fake data, if data/github.json is missing

Needs: fonttools (pyftsubset) and pillow.   pip install fonttools pillow
Fonts: put WOFF/WOFF2/TTF/OTF files in fonts/ named serif-400, serif-400-italic,
sans-400 and mono-400 (see FONT_CFG). Each card embeds only the glyphs it uses.

Data: data/github.json, refreshed daily by the GitHub Action:
    {"days":  [{"date": "2026-11-12", "count": 4}, ...],
     "repos": [{"name": "owner/repo", "description": "", "language": "Python",
                "stars": 3, "forks": 1}, ...]}
Raw GraphQL output (contributionCalendar + pinnedItems) is accepted too.

Cards are viewed through <img>, so only things that work there are used: an inline
<style> with CSS keyframes, SMIL <animate>, and base64 WOFF data URIs. Every CSS
animation is switched off under prefers-reduced-motion, and the purely decorative
SMIL pieces (class "mo") are hidden there, since CSS cannot pause SMIL.
"""
from __future__ import annotations

import base64
import bisect
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "cards"
FONTS = ROOT / "fonts"
DATA = Path(os.environ.get("DATA_FILE", ROOT / "data" / "github.json"))
STATIC = os.environ.get("STATIC") == "1"
DEMO = os.environ.get("DEMO") == "1"

# ───────────────────────────── EDIT ME ─────────────────────────────

PROFILE = {
    "first": "Swastik",
    "last": "Sagar",
    "eyebrow": "Cybersecurity Analyst",  # shown uppercase
    "tagline": "I build products people use, write about what I learn, and share the code.",
    "site": "swastiksagar.tech",  # TODO: your website, shown under the tagline
    "portrait": "assets/portrait.jpeg",
    "portrait_mode": "ascii",  # "ascii" | "image" | "none"
    "portrait_crop": (0.18, 0.06, 0.78, 0.74),  # fractions of the photo: l, t, r, b
}

BLUE, RED, YELLOW = "#2f54ff", "#f23d2e", "#fdc612"
TILES = [  # the three linked tiles; files are cards/<file>.svg
    dict(file="build", label="Build", num="Wazuh", word="lab", bg=BLUE, fg="#ffffff",
         body="The perfect Wazuh Homelab.",
         href="https://github.com/swastiksagar?tab=wazuhlabproject"),
    dict(file="brand", label="Brand", num="free", word="check", bg=RED, fg="#ffffff",
         body="Check your email.",
         href="https://swastiksagar/projects/email-checker"),
    dict(file="learn", label="Learn", num="like", word="SOC", bg=YELLOW, fg="#111111",
         body="Teaching security operations.",
         href="https://swastiksagar.tech/projects/soc-analyst-from-scratch"),
]

# Palette, read off the design reference.
BG, INK, MUTED, FAINT, RULE = "#f3f3f2", "#111111", "#6f6f6b", "#a4a4a0", "#d8d8d4"
BRAND, BRAND_WORD = BLUE, "blue"  # contribution graph colour and the word in its title
NAVY = "#0f1a70"  # the dark end of the gradient on the surname's first letter

# role: (CSS family, file stem, italic file stem, fallback stack)
FONT_CFG = {
    "serif": ("Instrument Serif", "serif-400", "serif-400-italic",
              "Georgia, 'Times New Roman', serif"),
    "sans": ("Geist", "sans-400", None,
             "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"),
    "mono": ("Geist Mono", "mono-400", None,
             "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"),
}
MAX_REPOS = 4

# ──────────────────────────── primitives ────────────────────────────


@dataclass
class R:
    """A run of text inside one <text>: lets a line mix roles, italics and colours."""
    role: str
    s: str
    italic: bool = False
    fill: str | None = None


def find_font(stem: str | None) -> Path | None:
    if not stem:
        return None
    for ext in (".woff2", ".woff", ".ttf", ".otf"):
        if (FONTS / f"{stem}{ext}").exists():
            return FONTS / f"{stem}{ext}"
    return None


_subset_cache: dict = {}
_warned: set = set()


def subset_b64(stem: str, chars: set) -> str | None:
    """Subset a font to `chars` with pyftsubset and return it as base64 WOFF."""
    src = find_font(stem)
    if src is None:
        if stem not in _warned:
            _warned.add(stem)
            print(f"warning: fonts/{stem}.* not found, using fallback fonts", file=sys.stderr)
        return None
    text = "".join(sorted(chars | {" "}))
    key = (src, text)
    if key not in _subset_cache:
        with tempfile.TemporaryDirectory() as td:
            txt, out = Path(td, "t.txt"), Path(td, "o.woff")
            txt.write_text(text, encoding="utf-8")
            exe = shutil.which("pyftsubset")
            cmd = [exe] if exe else [sys.executable, "-m", "fontTools.subset"]
            subprocess.run(
                cmd + [str(src), f"--text-file={txt}", "--flavor=woff", f"--output-file={out}",
                       "--layout-features=kern,liga", "--no-hinting"],
                check=True, capture_output=True)
            _subset_cache[key] = base64.b64encode(out.read_bytes()).decode()
    return _subset_cache[key]


def mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend a toward b by t (0..1)."""
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


class Card:
    def __init__(self, name: str, w: int, h: int, t0: float, title: str):
        self.name, self.w, self.h, self.t0, self.title = name, w, h, t0, title
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.used: dict = {}

    # animation attributes: base state is the FINAL frame, keyframes only define `from`
    def attr(self, kind: str | None = None, at: float = 0.0, cls: str = "") -> str:
        classes, style = cls, ""
        if kind and not STATIC:
            classes = f"{cls} a {kind}".strip()
            style = f' style="animation-delay:{self.t0 + at:.2f}s"'
        return (f' class="{classes}"' if classes else "") + style

    def add(self, s: str) -> None:
        self.parts.append(s)

    def mo(self, s: str) -> None:
        """Decorative SMIL motion: skipped in STATIC, hidden under reduced motion."""
        if not STATIC:
            self.parts.append(f'<g class="mo">{s}</g>')

    def text(self, x, y, runs, size, fill=INK, anchor="start", ls=0, kind=None, at=0.0):
        if isinstance(runs, str):
            runs = [R("sans", runs)]
        spans = []
        for r in runs:
            self.used.setdefault((r.role, r.italic), set()).update(r.s)
            cls = r.role + (" i" if r.italic else "")
            f = f' fill="{r.fill}"' if r.fill else ""
            spans.append(f'<tspan class="{cls}"{f}>{escape(r.s, quote=False)}</tspan>')
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        l = f' letter-spacing="{ls}"' if ls else ""
        self.add(f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}"{a}{l} '
                 f'xml:space="preserve"{self.attr(kind, at)}>{"".join(spans)}</text>')

    def css(self) -> str:
        out = []
        for role, (family, normal, italic, fallback) in FONT_CFG.items():
            roman = set(self.used.get((role, False), ()))
            ital = set(self.used.get((role, True), ()))
            if not find_font(italic):  # no italic file: let the browser slant the roman
                roman |= ital
                ital = set()
            for stem, chars, style in ((normal, roman, "normal"), (italic, ital, "italic")):
                data = subset_b64(stem, chars) if chars and stem else None
                if data:
                    out.append(f"@font-face{{font-family:'{family}';font-style:{style};"
                               f"font-weight:400;src:url(data:font/woff;base64,{data}) "
                               f"format('woff')}}")
            out.append(f".{role}{{font-family:'{family}',{fallback}}}")
        out.append(".i{font-style:italic}")
        if not STATIC:
            out.append(
                ".a{animation-duration:.9s;animation-timing-function:cubic-bezier(.2,.7,.2,1);"
                "animation-fill-mode:backwards}"
                ".fade{animation-name:fade}.rise{animation-name:rise}"
                ".grow{animation-name:grow;transform-box:fill-box;transform-origin:50% 100%}"
                ".pop{animation-name:pop;transform-box:fill-box;transform-origin:50% 50%}"
                "@keyframes fade{from{opacity:0}}"
                "@keyframes rise{from{opacity:0;transform:translateY(16px)}}"
                "@keyframes grow{from{transform:scaleY(0)}}"
                "@keyframes pop{from{opacity:0;transform:scale(.4)}}"
                "@media (prefers-reduced-motion:reduce){"
                ".a{animation:none!important}.mo{display:none!important}}")
        return "".join(out)

    def render(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.title)}">\n'
                f'<title>{escape(self.title, quote=False)}</title>\n'
                f'<style>{self.css()}</style>\n<defs>{"".join(self.defs)}</defs>\n'
                f'{chr(10).join(self.parts)}\n</svg>\n')


def arrow_ne(x, y, color) -> str:
    return (f'<path d="M{x} {y + 8}L{x + 8} {y}M{x + 1} {y}H{x + 8}V{y + 7}" fill="none" '
            f'stroke="{color}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>')


def arrow_e(x, y, color) -> str:
    return (f'<path d="M{x} {y}H{x + 10}M{x + 6} {y - 4}L{x + 10} {y}L{x + 6} {y + 4}" '
            f'fill="none" stroke="{color}" stroke-width="1.4" stroke-linecap="round" '
            f'stroke-linejoin="round"/>')


# ───────────────────────────── data ─────────────────────────────


def demo_raw() -> dict:
    rnd, end = random.Random(7), date.today()
    days = []
    for i in range(400):
        c = 0 if rnd.random() < 0.05 else int(rnd.expovariate(1 / 9))
        days.append({"date": (end - timedelta(i)).isoformat(), "count": c})
    days[40]["count"] = 128
    repos = [{"name": f"swastiksagar/{n}", "description": d, "language": lang,
              "stars": s, "forks": f} for n, d, lang, s, f in [
        ("project-one", "A short description of the first featured repository, one or two lines.",
         "Python", 120, 14),
        ("project-two", "Another featured project. Descriptions wrap onto a second line if long "
                        "enough to need it.", "TypeScript", 48, 6),
        ("project-three", "The third pinned repo.", "Go", 9, 1),
        ("project-four", "A fourth one, to show the full stack.", "Rust", 1203, 87)]]
    return {"days": days, "repos": repos}


def normalize(raw: dict):
    """Accept my flat format or raw GraphQL; return ({iso_date: count}, [repo dicts])."""
    root = raw.get("data", raw)
    user = root.get("user") or {}
    days: dict[str, int] = {}
    for d in raw.get("days", []):
        days[d["date"][:10]] = int(d.get("count", d.get("contributionCount", 0)))
    cal = (user.get("contributionsCollection") or {}).get("contributionCalendar")
    for w in (cal or {}).get("weeks", []):
        for d in w["contributionDays"]:
            days[d["date"][:10]] = int(d["contributionCount"])
    repos = list(raw.get("repos", []))
    for n in (user.get("pinnedItems") or {}).get("nodes", []):
        repos.append({"name": n["nameWithOwner"], "description": n.get("description") or "",
                      "language": (n.get("primaryLanguage") or {}).get("name"),
                      "stars": n["stargazerCount"], "forks": n["forkCount"]})
    if not days:
        sys.exit(f"{DATA} has no contribution days")
    return days, repos[:MAX_REPOS]


def month_start(d: date, n: int) -> date:
    i = d.year * 12 + d.month - 1 + n
    return date(i // 12, i % 12 + 1, 1)


def compute(days: dict, end: date) -> dict:
    """Totals over the 12 calendar months ending at `end` (so bars sum to the total)."""
    first = month_start(end, -11)
    window = [first + timedelta(i) for i in range((end - first).days + 1)]
    counts = [days.get(d.isoformat(), 0) for d in window]
    longest = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i = len(counts) - 1
    if counts[i] == 0:  # today may simply not have a commit yet
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    months = []
    for k in range(12):
        ms = month_start(end, -11 + k)
        last_day = month_start(end, -10 + k) - timedelta(1)
        total = sum(c for d, c in zip(window, counts) if d.year == ms.year and d.month == ms.month)
        months.append((ms.strftime("%b"), total, last_day > end))
    return dict(total=sum(counts), active=sum(c > 0 for c in counts), span=len(window),
                busiest=max(counts), current=current, longest=longest, months=months)


# ───────────────────────────── cards ─────────────────────────────


def ascii_portrait(path: Path, cols: int, crop, fs: float):
    """Photo -> rows of text. Returns (rows[(char_offset, text, opacity)], pixel_width)."""
    from PIL import Image, ImageFilter, ImageOps
    im = Image.open(path).convert("L")
    w, h = im.size
    im = im.crop((int(w * crop[0]), int(h * crop[1]), int(w * crop[2]), int(h * crop[3])))
    im = ImageOps.autocontrast(im, cutoff=2).filter(ImageFilter.UnsharpMask(2.5, 350, 2))
    adv, pitch = 0.6 * fs, 1.05 * fs
    rows_n = round(cols * adv * (im.height / im.width) / pitch)
    im = im.resize((cols, rows_n), Image.LANCZOS)
    ramp = " .,:;-=+*#%@"
    px = im.load()
    rows = []
    for y in range(rows_n):
        line = "".join(ramp[min(len(ramp) - 1, int(((255 - px[x, y]) / 255) ** 1.0 * len(ramp)))]
                       for x in range(cols))
        body = line.strip()
        if not body:
            rows.append(None)
            continue
        fade = 1.0 if y < rows_n * 0.78 else max(0.15, 1 - (y - rows_n * 0.78) / (rows_n * 0.22))
        rows.append((len(line) - len(line.lstrip()), body, round(fade * 0.9, 2)))
    return rows, cols * adv


def hero(c: Card) -> None:
    W, H = c.w, c.h
    first, last = PROFILE["first"], PROFILE["last"]
    c.add(f'<rect width="{W}" height="{H}" rx="26" fill="{BG}"/>')
    c.defs.append(f'<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="48" y1="200" '
                  f'x2="104" y2="282"><stop offset="0" stop-color="{BRAND}"/>'
                  f'<stop offset="1" stop-color="{NAVY}"/></linearGradient>')

    # eyebrow
    c.add(f'<rect x="48" y="47" width="8" height="8" rx="1.5" fill="{BRAND}"{c.attr("fade", 0)}/>')
    c.mo(f'<circle cx="52" cy="51" r="4" fill="none" stroke="{BRAND}" stroke-width="1.5" '
         f'opacity="0"><animate attributeName="r" values="4;13" dur="2.8s" begin="{c.t0 + 1:.1f}s" '
         f'repeatCount="indefinite"/><animate attributeName="opacity" values=".7;0" dur="2.8s" '
         f'begin="{c.t0 + 1:.1f}s" repeatCount="indefinite"/></circle>')
    c.text(66, 56, [R("mono", PROFILE["eyebrow"].upper())], 12, MUTED, ls=1.2, kind="fade", at=0.05)

    # name
    c.text(44, 186, [R("serif", first)], 112, INK, ls=-2, kind="rise", at=0.15)
    c.text(48, 282, [R("serif", last[0], True, "url(#g)"), R("serif", last[1:], True)],
           112, INK, ls=-2, kind="rise", at=0.3)

    # tagline + site
    for i, line in enumerate(textwrap.wrap(PROFILE["tagline"], 44)[:3]):
        c.text(48, 331 + i * 27, line, 20, "#3b3b39", kind="rise", at=0.6 + i * 0.08)
    c.text(48, 392, [R("mono", PROFILE["site"])], 14, INK, kind="fade", at=0.9)
    c.add(f'<g{c.attr("fade", 0.9)}>{arrow_ne(round(48 + len(PROFILE["site"]) * 8.4 + 14), 383, BRAND)}</g>')

    # portrait
    path = ROOT / PROFILE["portrait"]
    if PROFILE["portrait_mode"] == "none" or not path.exists():
        return
    if PROFILE["portrait_mode"] == "image":
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((420, 420))
        tmp = Path(tempfile.gettempdir()) / "portrait.jpg"
        im.save(tmp, quality=82, optimize=True)
        b64 = base64.b64encode(tmp.read_bytes()).decode()
        c.defs.append('<clipPath id="pc"><rect x="548" y="52" width="284" height="284" rx="22"/></clipPath>')
        c.add(f'<image href="data:image/jpeg;base64,{b64}" x="548" y="52" width="284" height="284" '
              f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"{c.attr("fade", 0.4)}/>')
        return
    fs = 4.6
    rows, width = ascii_portrait(path, 96, PROFILE["portrait_crop"], fs)
    x0, y0 = W - 48 - width, 60
    for i, row in enumerate(rows):
        if row is None:
            continue
        off, body, op = row
        c.used.setdefault(("mono", False), set()).update(body)
        c.add(f'<text x="{x0 + off * 0.6 * fs:.2f}" y="{y0 + i * 1.05 * fs:.2f}" font-size="{fs}" '
              f'fill="#5f5f5b" fill-opacity="{op}" textLength="{len(body) * 0.6 * fs:.2f}" '
              f'xml:space="preserve"{c.attr("fade", 0.35 + i * 0.02, "mono")}>'
              f'{escape(body, quote=False)}</text>')


def tile(c: Card, T: dict) -> None:
    W, H, fg = c.w, c.h, T["fg"]
    c.defs.append(f'<clipPath id="k"><rect width="{W}" height="{H}" rx="22"/></clipPath>'
                  '<linearGradient id="s" x1="0" x2="1" y1="0" y2="0">'
                  '<stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                  '<stop offset=".5" stop-color="#fff" stop-opacity=".95"/>'
                  '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    c.add(f'<rect width="{W}" height="{H}" rx="22" fill="{T["bg"]}"{c.attr("fade", 0)}/>')
    c.add(f'<g clip-path="url(#k)"{c.attr("fade", 0.1)}><rect x="150" y="-30" width="110" height="300" '
          f'transform="skewX(-18)" fill="url(#s)" opacity=".16"/></g>')
    c.mo(f'<g clip-path="url(#k)"><rect x="-160" y="-30" width="50" height="300" '
         f'transform="skewX(-18)" fill="url(#s)" opacity=".3"><animate attributeName="x" '
         f'values="-160;360;360" keyTimes="0;.3;1" dur="8s" begin="{c.t0 + 1.2:.1f}s" '
         f'repeatCount="indefinite"/></rect></g>')
    c.text(24, 39, [R("mono", T["label"].upper())], 12, fg, ls=1.4, kind="rise", at=0.1)
    c.add(f'<g{c.attr("rise", 0.1)}>{arrow_e(262, 35, fg)}</g>')
    c.text(24, 112, [R("serif", T["num"] + " "), R("serif", T["word"], True)], 50, fg,
           ls=-0.5, kind="rise", at=0.22)
    for i, line in enumerate(textwrap.wrap(T["body"], 31)[:3]):
        c.text(24, 150 + i * 21, line, 15.5, fg, kind="rise", at=0.34 + i * 0.06)


def activity(c: Card, S: dict) -> None:
    W, H = c.w, c.h
    c.add(f'<rect width="{W}" height="{H}" rx="26" fill="{BG}"/>')
    c.text(40, 58, [R("mono", "THE LAST 12 MONTHS ON GITHUB")], 12, MUTED, ls=1.2, kind="fade", at=0)
    c.text(40, 112, [R("serif", f'{S["total"]:,}'), R("serif", " contributions", True)], 56,
           INK, ls=-0.5, kind="rise", at=0.1)

    # bars
    months, base, max_h, bw = S["months"], 292, 140, 38
    pitch = (560 - bw) / 11
    peak = max(m[1] for m in months) or 1
    hi = max((i for i, m in enumerate(months) if not m[2]), key=lambda i: months[i][1], default=-1)
    for i, (label, n, partial) in enumerate(months):
        x, h = 40 + i * pitch, max(5, round(max_h * n / peak))
        fill = BRAND if i == hi else ("#c9c9c5" if partial else INK)
        at = 0.35 + i * 0.07
        c.add(f'<rect x="{x:.1f}" y="{base - h}" width="{bw}" height="{h}" rx="4" fill="{fill}"'
              f'{c.attr("grow", at)}/>')
        c.text(f"{x + bw / 2:.1f}", 314, [R("mono", label)], 11, MUTED, "middle", kind="fade", at=at)
        if i == hi:
            c.text(f"{x + bw / 2:.1f}", base - h - 9, [R("mono", f"{n:,}")], 11, BRAND, "middle",
                   kind="fade", at=at + 0.5)

    # stats column
    cur = S["current"]
    same = cur == S["longest"]
    stats = [
        ([R("serif", f"{cur} "), R("serif", "day" if cur == 1 else "days", True)],
         "current streak, and the longest" if same else f'current streak, longest {S["longest"]}'),
        ([R("serif", f'{S["active"]} '), R("serif", f'/ {S["span"]}', fill=FAINT)],
         "days with a commit"),
        ([R("serif", f'{S["busiest"]}')], "on the busiest day"),
    ]
    for i, (big, label) in enumerate(stats):
        y, at = 103 + i * 92, 0.5 + i * 0.18
        c.text(650, y, big, 42, INK, ls=-0.5, kind="rise", at=at)
        c.text(650, y + 24, label, 13.5, MUTED, kind="rise", at=at + 0.08)
        if i < 2:
            c.add(f'<line x1="650" x2="840" y1="{y + 45}" y2="{y + 45}" stroke="{RULE}"'
                  f'{c.attr("fade", at + 0.1)}/>')


def graph(c: Card, days: dict, end: date) -> None:
    W, H = c.w, c.h
    scale = ["#e2e2df"] + [mix(BRAND, "#ffffff", t) for t in (0.78, 0.55, 0.3)] + \
            [BRAND, mix(BRAND, "#000000", 0.25)]
    nz = sorted(v for v in days.values() if v > 0)
    cuts = [nz[int(len(nz) * p)] for p in (0.2, 0.4, 0.6, 0.8)] if nz else [1, 2, 3, 4]

    def level(n):
        return 0 if n <= 0 else 1 + bisect.bisect_left(cuts, n)

    c.add(f'<rect width="{W}" height="{H}" rx="26" fill="{BG}"/>')
    c.text(40, 62, [R("serif", "Every day, "), R("serif", f"in {BRAND_WORD}", True, BRAND)], 38,
           INK, ls=-0.3, kind="rise", at=0)
    c.text(840, 50, [R("mono", "REFRESHED DAILY")], 12, MUTED, "end", ls=1.2, kind="fade", at=0.1)

    start = end - timedelta(days=(end.weekday() + 1) % 7) - timedelta(weeks=51)  # a Sunday
    pitch, x0, y0 = 15.2, 44, 96
    for col in range(52):
        cells = []
        for row in range(7):
            d = start + timedelta(days=col * 7 + row)
            if d > end:
                break
            cells.append(f'<rect x="{x0 + col * pitch:.1f}" y="{y0 + row * pitch:.1f}" width="12" '
                         f'height="12" rx="3" fill="{scale[level(days.get(d.isoformat(), 0))]}"/>')
        c.add(f'<g{c.attr("fade", 0.3 + col * 0.025)}>{"".join(cells)}</g>')

    # today's cell breathes
    n = (end - start).days
    c.mo(f'<rect x="{x0 + (n // 7) * pitch - 2:.1f}" y="{y0 + (n % 7) * pitch - 2:.1f}" width="16" '
         f'height="16" rx="5" fill="none" stroke="{BRAND}" stroke-width="1.5" opacity="0">'
         f'<animate attributeName="opacity" values="0;.9;0" dur="2.4s" begin="{c.t0 + 1.8:.1f}s" '
         f'repeatCount="indefinite"/></rect>')

    # month labels
    last_col = -9
    for k in range(13):
        m = month_start(end, -12 + k)
        col = (m - start).days // 7
        if m >= start and col - last_col >= 3 and m <= end:
            c.text(x0 + col * pitch, y0 + 7 * pitch + 20, [R("mono", m.strftime("%b"))], 11, MUTED,
                   kind="fade", at=1.0)
            last_col = col
    # legend, under the header so it never collides with the month labels
    ly = 80
    c.text(840, ly, [R("mono", "more")], 11, MUTED, "end", kind="fade", at=1.0)
    c.text(708, ly, [R("mono", "less")], 11, MUTED, "end", kind="fade", at=1.0)
    for i, col in enumerate(scale):
        c.add(f'<rect x="{716 + i * 16}" y="{ly - 10}" width="12" height="12" rx="3" fill="{col}"'
              f'{c.attr("fade", 1.0)}/>')


def repo_card(c: Card, r: dict) -> None:
    W, H = c.w, c.h
    short = r["name"].split("/")[-1]
    c.add(f'<rect width="{W}" height="{H}" rx="26" fill="{BG}"{c.attr("fade", 0)}/>')
    c.add(f'<circle cx="46" cy="44" r="4.5" fill="{BRAND}"{c.attr("pop", 0.1)}/>')
    c.text(58, 48, [R("mono", (r.get("language") or "Repository").upper())], 12, MUTED, ls=1.2,
           kind="fade", at=0.1)
    c.add(f'<g{c.attr("fade", 0.2)}>{arrow_ne(824, 36, BRAND)}</g>')
    c.text(40, 96, [R("serif", short)], 44, INK, ls=-0.5, kind="rise", at=0.15)
    for i, line in enumerate(textwrap.wrap(r.get("description") or "", 76)[:2]):
        c.text(40, 128 + i * 22, line, 15.5, "#3b3b39", kind="rise", at=0.3 + i * 0.06)
    for i, (n, word) in enumerate(((r.get("stars", 0), "stars"), (r.get("forks", 0), "forks"))):
        word = word[:-1] if n == 1 else word
        c.text(840, 100 + i * 40, [R("serif", f"{n:,} "), R("serif", word, True)], 30, INK, "end",
               ls=-0.3, kind="rise", at=0.3 + i * 0.1)


# ───────────────────────────── output ─────────────────────────────


def patch_readme(name: str, html: str) -> None:
    p = ROOT / "README.md"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    a, b = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    i, j = s.find(a), s.find(b)
    if i >= 0 and j > i:
        p.write_text(s[:i + len(a)] + "\n" + html + "\n" + s[j:], encoding="utf-8")


def main() -> None:
    if DATA.exists():
        raw = json.loads(DATA.read_text(encoding="utf-8"))
    elif DEMO:
        raw = demo_raw()
    else:
        sys.exit(f"{DATA} not found. Run the refresh step first, or use DEMO=1 for fake data.")
    days, repos = normalize(raw)
    end = max(date.fromisoformat(k) for k in days)
    S = compute(days, end)
    name = f'{PROFILE["first"]} {PROFILE["last"]}'

    CARDS.mkdir(exist_ok=True)
    for old in CARDS.glob("*.svg"):
        old.unlink()

    # Everything starts at page load, so each card's t0 staggers the page top to bottom.
    t = 0.0
    jobs = [(Card("hero", 880, 432, t, f'{name}. {PROFILE["tagline"]}'), lambda c: hero(c))]
    t = 1.1
    for i, T in enumerate(TILES):
        jobs.append((Card(T["file"], 293, 222, t + i * 0.14, f'{T["label"]}: {T["num"]} {T["word"]}. '
                          f'{T["body"]}'), lambda c, T=T: tile(c, T)))
    t += 0.14 * len(TILES) + 0.5
    jobs.append((Card("activity", 880, 352, t, f'{S["total"]:,} contributions in the last 12 months, '
                      f'{S["current"]} day streak'), lambda c: activity(c, S)))
    t += 1.0
    for i, r in enumerate(repos):
        jobs.append((Card(f"repo-{i + 1}", 880, 176, t, f'{r["name"]}: {r.get("description", "")}'),
                     lambda c, r=r: repo_card(c, r)))
        t += 0.3
    jobs.append((Card("graph", 880, 250, t + 0.2, "Contribution graph for the last year"),
                 lambda c: graph(c, days, end)))

    for card, build in jobs:
        build(card)
        (CARDS / f"{card.name}.svg").write_text(card.render(), encoding="utf-8")
        print(f"cards/{card.name}.svg")

    tiles_html = "\n".join(
        f'<a href="{T["href"]}"><img src="./cards/{T["file"]}.svg" width="32.5%" '
        f'alt="{escape(T["label"] + ": " + T["num"] + " " + T["word"])}" /></a>' for T in TILES)
    repos_html = "\n".join(
        f'<a href="https://github.com/{r["name"]}"><img src="./cards/repo-{i + 1}.svg" width="100%" '
        f'alt="{escape(r["name"])}" /></a>' for i, r in enumerate(repos))
    patch_readme("tiles", tiles_html)
    patch_readme("repos", repos_html)

    if STATIC:  # a page that stacks the cards like the README, for screenshots
        prev = ROOT / "preview"
        prev.mkdir(exist_ok=True)
        stack = ['<img src="../cards/hero.svg">', "<div class=row>" + "".join(
            f'<img src="../cards/{T["file"]}.svg">' for T in TILES) + "</div>",
                 '<img src="../cards/activity.svg">'] + \
                [f'<img src="../cards/repo-{i + 1}.svg">' for i in range(len(repos))] + \
                ['<img src="../cards/graph.svg">']
        (prev / "index.html").write_text(
            "<!doctype html><meta charset=utf-8><title>preview</title><style>"
            "body{margin:0;padding:24px;background:#fff;display:grid;gap:12px;width:880px}"
            "img{width:100%;display:block}.row{display:grid;grid-template-columns:repeat(3,1fr);"
            "gap:12px}</style>" + "".join(stack), encoding="utf-8")
        print("preview/index.html (static final frame; do not commit)")


if __name__ == "__main__":
    main()
