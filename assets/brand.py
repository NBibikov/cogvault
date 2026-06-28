"""Procedural brand graphics for cogvault (no AI image gen — deterministic PIL).
Palette: deep indigo base, slate, amber accent. Motif: a vault / layered memory
mark built from concentric rounded squares + a recall 'spark'."""
from PIL import Image, ImageDraw, ImageFont
import math, os

OUT = os.path.dirname(__file__)

# palette
INK      = (15, 18, 32)        # near-black indigo
INDIGO   = (49, 46, 129)       # deep indigo
INDIGO2  = (79, 70, 229)       # brighter indigo
SLATE    = (148, 163, 184)
AMBER    = (245, 176, 65)      # recall warmth
AMBER_HI = (252, 211, 77)
WHITE    = (237, 240, 250)

def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

HELV_B = "/System/Library/Fonts/HelveticaNeue.ttc"
AVENIR = "/System/Library/Fonts/Avenir Next.ttc"
MENLO  = "/System/Library/Fonts/Menlo.ttc"


def rounded(draw, box, r, **kw):
    draw.rounded_rectangle(box, radius=r, **kw)


def draw_mark(d, cx, cy, s, glow=True):
    """The cogvault mark: 3 nested rounded squares (layers of memory / a vault)
    with an amber recall node bridging them."""
    # outer vault layer
    for i, (sz, col, w) in enumerate([
        (s, INDIGO2, max(3, s // 16)),
        (int(s * 0.66), SLATE, max(2, s // 22)),
        (int(s * 0.34), AMBER, max(2, s // 26)),
    ]):
        box = [cx - sz // 2, cy - sz // 2, cx + sz // 2, cy + sz // 2]
        rounded(d, box, sz // 5, outline=col, width=w)
    # amber recall node (center) + spark line connecting layers (the 'recall')
    nr = max(4, s // 18)
    d.ellipse([cx - nr, cy - nr, cx + nr, cy + nr], fill=AMBER_HI)
    # a diagonal 'retrieval' spark from center to outer corner
    ox = cx + int(s * 0.5 * 0.62); oy = cy - int(s * 0.5 * 0.62)
    d.line([(cx, cy), (ox, oy)], fill=AMBER, width=max(2, s // 30))
    d.ellipse([ox - nr // 2, oy - nr // 2, ox + nr // 2, oy + nr // 2], fill=AMBER_HI)


def gradient_bg(img, top, bot):
    w, h = img.size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    img.paste(base.resize((w, h)), (0, 0))


def make_logo():
    """Square logo 512×512 with mark + wordmark below."""
    S = 512
    img = Image.new("RGB", (S, S), INK)
    gradient_bg(img, INK, (24, 24, 52))
    d = ImageDraw.Draw(img)
    draw_mark(d, S // 2, int(S * 0.40), int(S * 0.46))
    # wordmark
    fw = font(HELV_B, 70)
    txt = "cogvault"
    bb = d.textbbox((0, 0), txt, font=fw)
    d.text(((S - (bb[2] - bb[0])) // 2, int(S * 0.70)), txt, font=fw, fill=WHITE)
    ft = font(AVENIR, 24)
    tag = "memory your agents own"
    bb2 = d.textbbox((0, 0), tag, font=ft)
    d.text(((S - (bb2[2] - bb2[0])) // 2, int(S * 0.84)), tag, font=ft, fill=SLATE)
    img.save(os.path.join(OUT, "logo.png"))
    # icon-only 256
    ic = Image.new("RGB", (256, 256), INK); gradient_bg(ic, INK, (24, 24, 52))
    draw_mark(ImageDraw.Draw(ic), 128, 128, 180)
    ic.save(os.path.join(OUT, "icon.png"))
    print("logo.png, icon.png")


def make_banner():
    """GitHub social banner 1280×640."""
    W, H = 1280, 640
    img = Image.new("RGB", (W, H), INK)
    gradient_bg(img, (18, 20, 40), (30, 27, 75))
    d = ImageDraw.Draw(img)
    # faint markdown-grid texture on the right
    for gx in range(W // 2, W, 40):
        d.line([(gx, 0), (gx, H)], fill=(255, 255, 255, 6), width=1)
    for gy in range(0, H, 40):
        d.line([(W // 2, gy), (W, gy)], fill=(40, 44, 80), width=1)
    # mark on left
    draw_mark(d, 300, H // 2, 300)
    # text block
    fh = font(HELV_B, 92)
    d.text((560, 188), "cogvault", font=fh, fill=WHITE)
    fs = font(AVENIR, 34)
    d.text((566, 300), "Fleet-grade memory for AI agents", font=fs, fill=AMBER_HI)
    fm = font(MENLO, 22)
    for i, ln in enumerate([
        "plain Markdown you own  ·  hybrid recall",
        "multi-tenant  ·  no cloud  ·  no Docker  ·  no LLM",
    ]):
        d.text((566, 360 + i * 36), ln, font=fm, fill=SLATE)
    img.save(os.path.join(OUT, "banner.png"))
    print("banner.png")


def make_diagram():
    """Architecture diagram 1200×620: files -> index -> hybrid -> result."""
    W, H = 1200, 620
    img = Image.new("RGB", (W, H), (18, 20, 36)); d = ImageDraw.Draw(img)
    gradient_bg(img, (18, 20, 36), (26, 24, 56)); d = ImageDraw.Draw(img)
    fb = font(HELV_B, 26); fs = font(AVENIR, 18); fm = font(MENLO, 15)

    def box(x, y, w, h, title, lines, accent=INDIGO2):
        rounded(d, [x, y, x + w, y + h], 16, fill=(28, 30, 54), outline=accent, width=3)
        d.text((x + 20, y + 16), title, font=fb, fill=WHITE)
        for i, ln in enumerate(lines):
            d.text((x + 20, y + 54 + i * 24), ln, font=fm, fill=SLATE)

    def arrow(x1, y, x2):
        d.line([(x1, y), (x2 - 14, y)], fill=AMBER, width=4)
        d.polygon([(x2, y), (x2 - 16, y - 9), (x2 - 16, y + 9)], fill=AMBER)

    d.text((40, 30), "How cogvault works", font=font(HELV_B, 34), fill=WHITE)
    y0 = 130
    box(40, y0, 250, 170, "Markdown", [
        "~/Agents/<tenant>/", "  memory/*.md", "", "source of truth", "git-diffable · yours"])
    arrow(290, y0 + 85, 360)
    box(360, y0, 250, 170, "Index (SQLite)", [
        "content-hash cache", "FastEmbed (bge-384d)", "sqlite-vec + FTS5", "",
        "derived · rebuildable"], accent=SLATE)
    arrow(610, y0 + 85, 680)
    box(680, y0, 230, 170, "Hybrid recall", [
        "vector  +  BM25", "RRF fusion", "temporal decay", "MMR diversity"], accent=AMBER)
    arrow(910, y0 + 85, 980)
    box(980, y0, 180, 170, "MCP", [
        "recall", "record", "", "→ your agent"], accent=INDIGO2)
    # footer line
    d.text((40, 380), "One process. One embedding model, loaded once, shared by every tenant.",
           font=fs, fill=AMBER_HI)
    d.text((40, 412), "No cloud. No Docker. No LLM required for ingest or retrieval.",
           font=fs, fill=SLATE)
    img.save(os.path.join(OUT, "architecture.png")); print("architecture.png")


if __name__ == "__main__":
    make_logo()
    make_banner()
    make_diagram()
