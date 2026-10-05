"""README graphics for cogvault — hand-built SVG, one template, two themes.

    python assets/make_graphics.py            # writes assets/*-{dark,light}.svg
    python assets/make_graphics.py --png      # + social-preview.png (needs Chrome)

No image generation, no network: every shape is placed here, so the figures stay
true to what the code does. Change a number in the code, change it here too.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from html import escape

OUT = os.path.dirname(os.path.abspath(__file__))

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

THEMES = {
    "dark": dict(
        bg0="#0b0e1c", bg1="#191c45", surface="#151a2e", surface2="#1c2240",
        border="#2c335a", ink="#eef0fa", ink2="#a6aecb", muted="#6d7597",
        indigo="#7c7ff2", indigo_deep="#4f46e5", indigo_soft="#2a2c6b",
        amber="#f5b041", amber_hi="#fcd34d", amber_soft="#3a2c16",
        teal="#2fa7a0", teal_soft="#143537",
        # benchmark series (validated: dataviz validate_palette --mode dark)
        s1="#c4801f", s2="#7c7ff2", s3="#2fa7a0", s4="#d0609a",
        dot="#3a4170", grid="#232a4a",
    ),
    "light": dict(
        bg0="#fbfbfe", bg1="#eef0fb", surface="#ffffff", surface2="#f4f5fb",
        border="#d9dcec", ink="#141733", ink2="#4a5174", muted="#878daa",
        indigo="#4f46e5", indigo_deep="#4338ca", indigo_soft="#e6e6fd",
        amber="#c97a0a", amber_hi="#f59e0b", amber_soft="#fdf0d9",
        teal="#0d9488", teal_soft="#dcf3f0",
        s1="#c97a0a", s2="#4f46e5", s3="#0d9488", s4="#c0397f",
        dot="#d4d7ea", grid="#e7e9f3",
    ),
}


def t(x, y, s, size=14, fill="ink", weight=400, family=SANS, anchor="start", extra=""):
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{{{fill}}}" text-anchor="{anchor}" {extra}>{_lit(s)}</text>')


def _lit(s):
    """Escape text; literal braces become entities so they never read as tokens."""
    return escape(s).replace("{", "&#123;").replace("}", "&#125;")


def mono(x, y, s, size=13, fill="ink2", **kw):
    return t(x, y, s, size=size, fill=fill, family=MONO, **kw)


def box(x, y, w, h, r=14, fill="surface", stroke="border", sw=1.5, extra=""):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'fill="{{{fill}}}" stroke="{{{stroke}}}" stroke-width="{sw}" {extra}/>')


def arrow(x1, y1, x2, y2, color="amber", sw=2, dash=""):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{{{color}}}" '
            f'stroke-width="{sw}" stroke-linecap="round" {d} marker-end="url(#ah-{color})"/>')


def defs(extra=""):
    marks = "".join(
        f'<marker id="ah-{c}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        f'fill="{{{c}}}"/></marker>' for c in ("amber", "indigo", "teal", "muted"))
    return f"""<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{{bg0}}"/><stop offset="1" stop-color="{{bg1}}"/>
  </linearGradient>
  <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
    <circle cx="2" cy="2" r="1.2" fill="{{dot}}"/>
  </pattern>
  <radialGradient id="fade" cx="0.8" cy="0.3" r="0.8">
    <stop offset="0" stop-color="#fff" stop-opacity="1"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </radialGradient>
  <mask id="dotmask"><rect width="100%" height="100%" fill="url(#fade)"/></mask>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
    <feGaussianBlur stdDeviation="8" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  {marks}
  {extra}
</defs>"""


def frame(w, h, body, extra_defs="", style=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">
{defs(extra_defs)}
<style>{style}
@media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} }}
</style>
<rect width="{w}" height="{h}" rx="22" fill="url(#bg)"/>
<rect width="{w}" height="{h}" rx="22" fill="url(#dots)" mask="url(#dotmask)" opacity="0.9"/>
{body}
</svg>"""


# ---------------------------------------------------------------- the mark ---
def mark(cx, cy, s, animate=True):
    """Nested vault layers with an amber recall spark pulling one card out."""
    o = []
    for frac, col, w in ((1.0, "indigo", s / 15), (0.66, "muted", s / 22), (0.34, "amber", s / 24)):
        sz = s * frac
        o.append(f'<rect x="{cx - sz/2:.1f}" y="{cy - sz/2:.1f}" width="{sz:.1f}" height="{sz:.1f}" '
                 f'rx="{sz/4.6:.1f}" fill="none" stroke="{{{col}}}" stroke-width="{w:.1f}"/>')
    nr = s / 17
    ox, oy = cx + s * 0.34, cy - s * 0.34
    cls = 'class="spark"' if animate else ""
    o.append(f'<g {cls}><line x1="{cx}" y1="{cy}" x2="{ox:.1f}" y2="{oy:.1f}" stroke="{{amber}}" '
             f'stroke-width="{s/28:.1f}" stroke-linecap="round"/>'
             f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="{nr*0.6:.1f}" fill="{{amber_hi}}"/></g>')
    o.append(f'<circle cx="{cx}" cy="{cy}" r="{nr:.1f}" fill="{{amber_hi}}" filter="url(#glow)" '
             f'{"class=\"core\"" if animate else ""}/>')
    return "\n".join(o)


def card(x, y, w, h, lines, accent="muted", fill="surface", rot=0, cls=""):
    """A tiny markdown card: header bar + text lines (widths as fractions)."""
    g = [f'<g transform="rotate({rot} {x + w/2} {y + h/2})" {cls}>',
         box(x, y, w, h, r=9, fill=fill, stroke=accent, sw=1.6),
         f'<rect x="{x+12}" y="{y+12}" width="{w*0.42:.0f}" height="6" rx="3" fill="{{{accent}}}"/>']
    for i, f in enumerate(lines):
        g.append(f'<rect x="{x+12}" y="{y+28+i*11}" width="{(w-24)*f:.0f}" height="4" rx="2" '
                 f'fill="{{muted}}" opacity="0.55"/>')
    g.append("</g>")
    return "\n".join(g)


# ----------------------------------------------------------------- hero -------
def hero():
    W, H = 1280, 420
    style = """
.spark { transform-origin: 250px 210px; animation: sweep 6s ease-in-out infinite; }
.core { animation: pulse 3s ease-in-out infinite; }
.lift { animation: lift 6s ease-in-out infinite; }
@keyframes sweep { 0%,100% { transform: rotate(0deg); } 50% { transform: rotate(8deg); } }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .55; } }
@keyframes lift  { 0%,100% { transform: translate(0,0); } 50% { transform: translate(4px,-6px); } }
"""
    b = []
    # fanned memory cards behind the mark
    b.append(card(118, 120, 120, 150, [1, .8, .9, .6, .85, .7, .5], rot=-14))
    b.append(card(262, 140, 120, 150, [.9, 1, .7, .8, .6, .9, .4], rot=11))
    b.append(f'<g class="lift">{card(300, 64, 110, 92, [1, .7, .85, .5], accent="amber", rot=6)}</g>')
    b.append(mark(250, 210, 190))
    # wordmark
    b.append(t(468, 196, "cogvault", 96, weight=800, extra='letter-spacing="-3.5"'))
    b.append(t(472, 244, "Fleet-grade local memory for AI agents", 28, fill="amber", weight=650))
    b.append(mono(474, 280, "over plain Markdown you own — the index is just a cache", 16, fill="ink2"))
    # chips
    x = 472
    for label, col in (("hybrid recall", "indigo"), ("multi-tenant", "indigo"), ("MCP server", "indigo"),
                       ("offline", "teal"), ("no Docker", "teal"), ("no LLM in the loop", "teal")):
        w = 22 + len(label) * 8.2
        b.append(f'<rect x="{x}" y="312" width="{w:.0f}" height="30" rx="15" fill="{{{col}_soft}}" '
                 f'stroke="{{{col}}}" stroke-opacity="0.5"/>')
        b.append(t(x + w / 2, 332, label, 14, fill="ink", weight=550, anchor="middle"))
        x += w + 10
    return frame(W, H, "\n".join(b), style=style)


# --------------------------------------------------------- architecture ------
def architecture():
    W, H = 1200, 540
    b = [t(40, 58, "How cogvault works", 26, weight=750),
         t(40, 84, "Files are the truth. The index is derived, incremental and rebuildable. Recall is deterministic.",
           15, fill="ink2")]

    # column 1 — markdown card
    b.append(t(40, 132, "1 · YOUR FILES", 12, fill="muted", weight=700, extra='letter-spacing="1.5"'))
    b.append(card(52, 160, 250, 250, [], accent="muted", rot=-4))
    b.append(card(46, 154, 250, 250, [], accent="muted", rot=-2))
    b.append(box(40, 150, 260, 270, r=12, fill="surface", stroke="indigo", sw=1.8))
    ln = [("---", "muted"), ("name: deploy-rules", "ink2"), ("type: feedback", "indigo"),
          ("description: never on Friday", "ink2"), ("---", "muted"), ("", "ink2"),
          ("Never deploy after 16:00 Fri.", "ink"), ("**Why:** no one on call.", "ink2"),
          ("", "ink2"), ("See [[project_ci]]", "amber")]
    for i, (s, c) in enumerate(ln):
        b.append(mono(58, 180 + i * 22, s, 13, fill=c))
    b.append(mono(40, 446, "~/agent/memory/*.md", 13, fill="ink2"))
    b.append(t(40, 468, "open · edit · git diff", 13, fill="muted"))

    # column 2 — the index
    X = 360
    b.append(t(X, 132, "2 · INDEX  (.cogvault.db — derived)", 12, fill="muted", weight=700, extra='letter-spacing="1.5"'))
    b.append(box(X, 150, 420, 270, r=14, fill="surface", stroke="border"))
    b.append(box(X + 18, 168, 384, 56, r=10, fill="surface2", stroke="border"))
    b.append(t(X + 34, 192, "Chunker", 15, weight=650))
    b.append(mono(X + 34, 212, "fit to model token window + summary chunk", 12))
    b.append(box(X + 18, 240, 186, 96, r=10, fill="indigo_soft", stroke="indigo"))
    b.append(t(X + 34, 266, "Vectors", 15, weight=650))
    b.append(mono(X + 34, 288, "FastEmbed · ONNX", 12))
    b.append(mono(X + 34, 306, "384-d · sqlite-vec", 12))
    b.append(mono(X + 34, 324, "on-device", 12, fill="indigo"))
    b.append(box(X + 216, 240, 186, 96, r=10, fill="teal_soft", stroke="teal"))
    b.append(t(X + 232, 266, "Keywords", 15, weight=650))
    b.append(mono(X + 232, 288, "SQLite FTS5", 12))
    b.append(mono(X + 232, 306, "BM25 ranking", 12))
    b.append(mono(X + 232, 324, "exact terms, IDs", 12, fill="teal"))
    b.append(mono(X + 34, 366, "content-hash cache · only changed files re-embed", 12, fill="ink2"))
    b.append(mono(X + 34, 388, "WAL · type + [[links]] parsed at index time", 12, fill="ink2"))
    b.append(t(X, 446, "delete it → it rebuilds from the files", 13, fill="muted"))
    b.append(arrow(306, 285, X - 8, 285))

    # column 3 — recall + interfaces
    X3 = 840
    b.append(t(X3, 132, "3 · RECALL", 12, fill="muted", weight=700, extra='letter-spacing="1.5"'))
    b.append(box(X3, 150, 320, 118, r=14, fill="amber_soft", stroke="amber", sw=1.8))
    b.append(t(X3 + 18, 178, "Hybrid ranking", 15, weight=650))
    for i, s in enumerate(("vector ⊕ BM25 → RRF fusion", "→ temporal decay (opt-in)", "→ MMR diversity → 1 hit / card")):
        b.append(mono(X3 + 18, 202 + i * 20, s, 12, fill="ink"))
    b.append(arrow(X + 428, 209, X3 - 8, 209))
    for i, (head, sub) in enumerate((("MCP", "cogvault_recall · cogvault_record"),
                                     ("CLI", "index · search · doctor · eval"),
                                     ("Python", "Vault(dir).search(\"…\")"))):
        y = 288 + i * 44
        b.append(box(X3, y, 320, 36, r=9, fill="surface", stroke="border"))
        b.append(t(X3 + 14, y + 23, head, 13, weight=700, fill="indigo"))
        b.append(mono(X3 + 74, y + 23, sub, 12))
    b.append(t(X3, 446, "→ your agent", 13, fill="muted"))

    # write-back loop
    b.append(f'<path d="M {X3 + 160} 456 C {X3 + 160} 520, 270 520, 270 {432}" fill="none" '
             f'stroke="{{amber}}" stroke-width="1.8" stroke-dasharray="5 6" marker-end="url(#ah-amber)"/>')
    b.append(t(600, 512, "cogvault_record writes a new Markdown card — and indexes it", 13, fill="amber",
               weight=600, anchor="middle"))
    return frame(W, H, "\n".join(b))


# -------------------------------------------------------- recall anatomy -----
def recall():
    W, H = 1200, 560
    b = [t(40, 58, "Anatomy of a recall", 26, weight=750),
         t(40, 84, "Two independent rankers, fused by rank — not by score — then shaped for an agent's context window.",
           15, fill="ink2")]
    # query
    b.append(box(40, 112, 500, 54, r=27, fill="surface", stroke="amber", sw=1.8))
    b.append(mono(66, 145, "cogvault_recall(\"why does the worker keep restarting\")", 13, fill="ink"))

    def lane(x, title, sub, col, rows):
        o = [box(x, 196, 240, 250, r=14, fill=f"{col}_soft", stroke=col, sw=1.6),
             t(x + 18, 226, title, 16, weight=700), mono(x + 18, 246, sub, 12, fill=col)]
        for i, (f, hot) in enumerate(rows):
            y = 270 + i * 40
            o.append(box(x + 14, y, 212, 32, r=8, fill="surface", stroke="amber" if hot else "border",
                         sw=1.6 if hot else 1))
            o.append(mono(x + 26, y + 21, f"{i+1}", 12, fill="muted"))
            o.append(mono(x + 44, y + 21, f, 12, fill="ink" if hot else "ink2"))
        return "\n".join(o)

    b.append(lane(40, "Semantic", "embeddings · meaning", "indigo",
                  [("worker_crashloop.md", True), ("launchd_jobs.md", False),
                   ("oom_postmortem.md", False), ("pm2_setup.md", False)]))
    b.append(lane(300, "Keyword", "FTS5 · BM25 · exact words", "teal",
                  [("pm2_setup.md", False), ("worker_crashloop.md", True),
                   ("restart_policy.md", False), ("launchd_jobs.md", False)]))
    b.append(arrow(165, 168, 165, 190, color="indigo"))
    b.append(arrow(415, 168, 415, 190, color="teal"))

    # stages
    X = 610
    stages = (("RRF fusion", "Σ 1 / (k + rank)", "k = 60 · agreement between rankers wins"),
              ("Temporal decay", "× ½^(age / hl)", "opt-in · evergreen cards exempt"),
              ("MMR", "λ = 0.7", "relevance vs. near-duplicate penalty"),
              ("One hit per card", "", "best chunk per file — no flooding top-k"))
    for i, (h, f, s) in enumerate(stages):
        y = 196 + i * 66
        b.append(box(X, y, 300, 56, r=10, fill="surface", stroke="border"))
        b.append(f'<circle cx="{X + 24}" cy="{y + 28}" r="12" fill="{{amber_soft}}" stroke="{{amber}}"/>')
        b.append(t(X + 24, y + 33, str(i + 1), 12, fill="amber", weight=700, anchor="middle"))
        b.append(t(X + 46, y + 24, h, 14, weight=650))
        b.append(mono(X + 288, y + 24, f, 11, fill="amber", anchor="end"))
        b.append(t(X + 46, y + 44, s, 12, fill="ink2"))
        if i:
            b.append(arrow(X + 150, y - 10, X + 150, y - 2, color="muted", sw=1.5))
    b.append(f'<path d="M 160 446 V 476 H 590 M 420 446 V 476 M 590 476 V 224 H {X - 6}" fill="none" stroke="{{amber}}" '
             f'stroke-width="2" stroke-linejoin="round" marker-end="url(#ah-amber)"/>')

    # result
    RX = 950
    b.append(box(RX, 196, 210, 250, r=14, fill="amber_soft", stroke="amber", sw=1.8, extra='filter="url(#glow)" opacity="0.35"'))
    b.append(box(RX, 196, 210, 250, r=14, fill="amber_soft", stroke="amber", sw=1.8))
    b.append(t(RX + 18, 226, "Top results", 16, weight=700))
    for i, (f, sc) in enumerate((("worker_crashloop", ".0331"), ("pm2_setup", ".0325"), ("launchd_jobs", ".0323"))):
        y = 252 + i * 38
        b.append(box(RX + 14, y, 182, 30, r=8, fill="surface", stroke="amber" if i == 0 else "border"))
        b.append(mono(RX + 24, y + 20, f, 11, fill="ink" if i == 0 else "ink2"))
        b.append(mono(RX + 188, y + 20, sc, 10, fill="muted", anchor="end"))
    b.append(t(RX + 18, 384, "Related:", 12, fill="ink2", weight=650))
    b.append(mono(RX + 18, 404, "[[restart_policy]]", 12, fill="amber"))
    b.append(t(RX + 18, 430, "type · snippet · score", 12, fill="muted"))
    b.append(arrow(X + 306, 321, RX - 8, 321))
    b.append(t(40, 520, "Deterministic: same files + same query → same answer. No LLM rewrites or summarizes anything.",
               13, fill="muted"))
    return frame(W, H, "\n".join(b))


# ----------------------------------------------------------------- fleet -----
def fleet():
    W, H = 1200, 400
    b = [t(40, 58, "One directory = one tenant", 26, weight=750),
         t(40, 84, "Each agent recalls only its own memory. A process loads each embedding model once and shares it.",
           15, fill="ink2")]
    names = ("agent-a", "agent-b", "agent-c", "agent-d")
    cw, gap, x0 = 250, 34, 40
    for i, n in enumerate(names):
        x = x0 + i * (cw + gap)
        b.append(box(x, 116, cw, 54, r=27, fill="indigo_soft", stroke="indigo", sw=1.6))
        b.append(t(x + cw / 2, 149, n, 15, weight=650, anchor="middle"))
        b.append(arrow(x + cw / 2, 172, x + cw / 2, 198, color="indigo"))
        b.append(box(x, 204, cw, 104, r=12, fill="surface", stroke="border"))
        b.append(mono(x + 18, 232, f"~/agents/{n}/memory/", 12, fill="ink"))
        for j, w in enumerate((.8, .62, .7)):
            b.append(f'<rect x="{x + 18}" y="{246 + j*12}" width="{(cw-36)*w:.0f}" height="5" rx="2.5" '
                     f'fill="{{muted}}" opacity="0.5"/>')
        b.append(mono(x + 18, 296, ".cogvault.db  .cogvault.toml", 11, fill="muted"))
        if i:
            sx = x - gap / 2
            b.append(f'<line x1="{sx}" y1="120" x2="{sx}" y2="304" stroke="{{muted}}" stroke-width="1.5" '
                     f'stroke-dasharray="3 5"/>')
            b.append(f'<circle cx="{sx}" cy="212" r="11" fill="{{bg0}}" stroke="{{muted}}"/>')
            b.append(t(sx, 217, "×", 14, fill="muted", anchor="middle", weight=700))
    b.append(box(40, 330, 1120, 46, r=12, fill="amber_soft", stroke="amber", sw=1.4))
    b.append(t(60, 359, "Model weights load once per process, shared across every tenant it touches", 14, weight=600))
    b.append(mono(1140, 359, "model pinned per tenant in .cogvault.toml", 12, fill="amber", anchor="end"))
    return frame(W, H, "\n".join(b))


# ------------------------------------------------------------------ demo -----
def demo():
    """A Claude Code session using the two tools. Output formats match
    mcp_server.py: recall → `[score] file`, text, `Related:`; record → status JSON."""
    W, H = 1200, 520
    L = [  # (indent, text, color, weight, step) — step = when the line appears
        (0, "› why does the worker keep restarting?", "ink", 700, 0),
        (0, "● cogvault_recall(query=\"worker restart loop\", type=\"feedback\")", "indigo", 600, 1),
        (1, "⎿ [0.0331] feedback_worker_crashloop.md", "amber", 600, 2),
        (2, "launchd KeepAlive restarts the worker when it crashes on a missing .env.", "ink2", 400, 2),
        (2, "Fix: set EnvironmentVariables in the plist. Why: 3 hours lost, Sep 12.", "ink2", 400, 2),
        (2, "Related: reference_launchd_jobs", "muted", 400, 2),
        (0, "● It's the launchd KeepAlive loop from September — the worker dies on a", "ink", 400, 3),
        (1, "missing .env and launchd brings it back. Put the env into the plist.", "ink", 400, 3),
        (0, "", "ink", 400, 3),
        (0, "› remember: deploys always go through the staging flag first", "ink", 700, 4),
        (0, "● cogvault_record(title=\"deploy via staging flag\", type=\"feedback\")", "indigo", 600, 5),
        (1, "⎿ {\"status\": \"ok\", \"file\": \"deploy_via_staging_flag.md\"}", "teal", 600, 6),
        (2, "a plain Markdown card — open it, edit it, git diff it", "muted", 400, 6),
    ]
    steps = 7
    style = "".join(
        f".s{i} {{ animation: show{i} 16s ease-out infinite; }}"
        f"@keyframes show{i} {{ 0%, {i * 9}% {{ opacity: 0; transform: translateY(6px); }} "
        f"{i * 9 + 3}%, 92% {{ opacity: 1; transform: none; }} 100% {{ opacity: 0; }} }}"
        for i in range(1, steps))
    b = [box(24, 24, W - 48, H - 48, r=16, fill="surface", stroke="border")]
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        b.append(f'<circle cx="{52 + i*22}" cy="52" r="6.5" fill="{c}"/>')
    b.append(mono(W / 2, 57, "claude — ~/agents/backend", 13, fill="muted", anchor="middle"))
    b.append(f'<line x1="24" y1="78" x2="{W-24}" y2="78" stroke="{{border}}"/>')
    y = 118
    for ind, text, col, wt, step in L:
        cls = f'class="s{step}"' if step else ""
        if text:
            b.append(f'<g {cls}>' + t(56 + ind * 26, y, text, 15, fill=col, weight=wt, family=MONO) + "</g>")
        y += 28
    b.append(t(W - 56, H - 46, "illustrative session · formats match the real tool output", 12,
               fill="muted", anchor="end"))
    return frame(W, H, "\n".join(b), style=style)


# ------------------------------------------------------------- benchmark -----
BENCH_SERIES = (  # key in benchmark.json, legend label, color token (validated order)
    ("e5_summary", "e5-small + summary chunk (fleet config)", "s1"),
    ("e5_nosummary", "e5-small, no summary chunk", "s4"),
    ("minilm", "multilingual MiniLM (built-in default)", "s2"),
    ("bge_en", "bge-small-en (English-only)", "s3"),
)


def benchmark():
    """Small multiples on one shared 0–1 scale, 95% bootstrap whiskers.
    Numbers come from benchmark.json (aggregates only — the queries are private)."""
    import json
    data = json.load(open(os.path.join(OUT, "benchmark.json")))
    meta = data["_meta"]
    W, H = 1200, 470
    metrics = (("hit@1", "right card ranked first"), ("hit@5", "right card in the top 5"),
               ("mrr@10", "mean reciprocal rank"))
    b = [t(40, 58, f"Recall on {meta['scored']} real agent queries", 26, weight=750),
         t(40, 84, f"Pulled from the query log of {meta['tenants']} live tenants · answers judged against the actual "
                   f"cards · cogvault {meta['version']}", 15, fill="ink2")]
    lx = 40
    for _, name, col in BENCH_SERIES:
        b.append(f'<rect x="{lx}" y="108" width="14" height="14" rx="4" fill="{{{col}}}"/>')
        b.append(t(lx + 22, 120, name, 13, fill="ink2"))
        lx += 40 + len(name) * 6.3
    pw, gap, x0, top = 356, 26, 40, 146
    bar_h, bar_gap = 30, 10
    for m, (metric, sub) in enumerate(metrics):
        px = x0 + m * (pw + gap)
        b.append(box(px, top, pw, 262, r=12, fill="surface", stroke="border", sw=1))
        b.append(t(px + 18, top + 30, metric.replace("mrr@10", "MRR@10"), 16, weight=700))
        b.append(t(px + pw - 18, top + 30, sub, 12, fill="muted", anchor="end"))
        plot_x, plot_w = px + 18, pw - 92
        for q in (0.25, 0.5, 0.75, 1.0):
            gx = plot_x + plot_w * q
            b.append(f'<line x1="{gx:.1f}" y1="{top + 44}" x2="{gx:.1f}" y2="{top + 228}" stroke="{{grid}}"/>')
            b.append(t(gx, top + 248, f"{q:g}", 11, fill="muted", anchor="middle"))
        for k, (key, _, col) in enumerate(BENCH_SERIES):
            row = data[key]
            v, (lo, hi) = row[metric], row["ci95"][m]
            y = top + 48 + k * (bar_h + bar_gap)
            bw = plot_w * v
            b.append(f'<path d="M {plot_x} {y} H {plot_x + bw - 4:.1f} Q {plot_x + bw:.1f} {y} {plot_x + bw:.1f} {y + 4} '
                     f'V {y + bar_h - 4} Q {plot_x + bw:.1f} {y + bar_h} {plot_x + bw - 4:.1f} {y + bar_h} H {plot_x} Z" '
                     f'fill="{{{col}}}"/>')
            cy = y + bar_h / 2
            x1, x2 = plot_x + plot_w * lo, plot_x + plot_w * hi
            b.append(f'<path d="M {x1:.1f} {cy} H {x2:.1f} M {x1:.1f} {cy - 5} V {cy + 5} M {x2:.1f} {cy - 5} V {cy + 5}" '
                     f'stroke="{{ink}}" stroke-opacity="0.55" stroke-width="1.5" fill="none"/>')
            b.append(t(max(plot_x + bw, x2) + 8, y + 20, f"{v:.2f}", 13, fill="ink", weight=700 if k == 0 else 500))
    b.append(t(40, 436, f"Whiskers: 95% bootstrap intervals. Overlapping whiskers = no proven difference at this sample "
                        f"size. {meta['gaps']} {'query' if meta['gaps'] == 1 else 'queries'} with no answer in memory are counted as gaps, not scored.",
               13, fill="muted"))
    b.append(t(40, 456, "Reproduce on your own memory: cogvault eval --tenant DIR  (reads DIR/.cogvault-golden.jsonl)",
               13, fill="muted", family=MONO))
    return frame(W, H, "\n".join(b))


# ------------------------------------------------------------------ icon -----
def icon():
    """Square app icon (plugin directory, avatars): the mark alone, no text."""
    W = 1024
    b = [f'<rect width="{W}" height="{W}" rx="220" fill="url(#bg)"/>',
         f'<rect width="{W}" height="{W}" rx="220" fill="url(#dots)" mask="url(#dotmask)" opacity="0.6"/>',
         mark(W / 2, W / 2, 600, animate=False)]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{W}" viewBox="0 0 {W} {W}">
{defs()}
<style></style>
{chr(10).join(b)}
</svg>"""


# ------------------------------------------------------------------ build ----
FIGURES = {"hero": hero, "demo": demo, "architecture": architecture, "recall": recall, "fleet": fleet, "benchmark": benchmark}


def render(svg: str, theme: str) -> str:
    out = svg
    for k, v in sorted(THEMES[theme].items(), key=lambda kv: -len(kv[0])):
        out = out.replace("{" + k + "}", v)
    assert "{" not in out.split("<style>")[0] + out.split("</style>")[1], "unresolved token"
    return out


def social_png():
    """GitHub social preview (1280x640): the dark hero centred on the gradient."""
    chrome = shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    # drop the card's own rounded background so it melts into the page gradient
    hero_svg = open(os.path.join(OUT, "hero-dark.svg")).read().replace(
        'rx="22" fill="url(#bg)"', 'rx="22" fill="none"')
    html = f"""<html><body style="margin:0;width:1280px;height:640px;display:flex;align-items:center;
justify-content:center;background:linear-gradient(135deg,{THEMES['dark']['bg0']},{THEMES['dark']['bg1']})">
{hero_svg}</body></html>"""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--window-size=1280,640",
                    f"--screenshot={os.path.join(OUT, 'social-preview.png')}", f"file://{f.name}"],
                   check=True, capture_output=True)


def icon_png():
    """1024x1024 PNG of the icon, for the Claude Code plugin directory."""
    chrome = shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    svg = open(os.path.join(OUT, "icon.svg")).read()
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
    out = os.path.join(OUT, "..", "plugin", ".claude-plugin", "icon.png")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--force-device-scale-factor=1",
                    "--window-size=1024,1024", f"--screenshot={os.path.abspath(out)}",
                    f"file://{f.name}"], check=True, capture_output=True)


def main():
    for name, fn in FIGURES.items():
        svg = fn()
        for theme in THEMES:
            with open(os.path.join(OUT, f"{name}-{theme}.svg"), "w") as f:
                f.write(render(svg, theme))
    with open(os.path.join(OUT, "icon.svg"), "w") as f:
        f.write(render(icon(), "dark"))
    if "--png" in sys.argv:
        social_png()
        icon_png()
    print("ok")


if __name__ == "__main__":
    main()
