"""Read a UFC.com official scorecard image (the current template) into structured data.

OpenCV finds the three judge cards and their grid lines; each score cell is split into
digit glyphs and matched against templates of the card's font. Tesseract reads only the
text: judge names, fighter names, event and result.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

TEMPLATES_PATH = Path(__file__).with_name("digits.npz")
GLYPH = 24          # glyphs are normalized to GLYPH x GLYPH
TEMPLATES = None    # {digit: glyph}; None until load_templates()


# --- OCR helpers ------------------------------------------------------------------

def tess(img, psm, digits=False):
    ok, buf = cv2.imencode(".png", img)
    cmd = ["tesseract", "stdin", "stdout", "--psm", str(psm)]
    if digits:
        cmd += ["-c", "tessedit_char_whitelist=0123456789"]
    # One thread per call: tesseract otherwise spreads each call over every core, which
    # thrashes when several cards are read in parallel.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    return subprocess.run(cmd, input=buf.tobytes(), capture_output=True, env=env).stdout.decode().strip()


def prep(crop, scale=4):
    """Upscale + binarize a crop for tesseract (black text on white)."""
    big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(bw, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)


def is_blank(crop):
    return (crop < 128).mean() < 0.01


def lines(mask, axis, min_len):
    """Positions of long straight lines (axis 0 = horizontal, 1 = vertical), grouped."""
    k = (min_len, 1) if axis == 0 else (1, min_len)
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, k))
    idx = np.where(m.sum(axis=1 - axis) > 0)[0]
    if not len(idx):
        return []
    return [int(g.mean()) for g in np.split(idx, np.where(np.diff(idx) > 2)[0] + 1)]


# --- digits -------------------------------------------------------------------------

def glyphs(crop):
    """Split a cell into digit glyphs (left to right), each normalized to a square bitmap."""
    big = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _, ink = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink)
    H, W = ink.shape
    # Point deductions show the original score struck through in light grey next to the
    # corrected one: keep only components as dark as the darkest (the bold, current value).
    darkness = {i: float(big[lab == i].min()) for i in range(1, n)}
    darkest = min(darkness.values(), default=0)

    def inside(i):  # drop cell borders / shading edges that touch the crop edge
        x, y, w, h = stats[i, :4]
        return x > 0 and y > 0 and x + w < W and y + h < H

    comps = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > 60
             and stats[i, cv2.CC_STAT_HEIGHT] > H * 0.3 and inside(i)
             and darkness[i] <= darkest + 60]
    pieces = []
    for i in sorted(comps, key=lambda i: stats[i, cv2.CC_STAT_LEFT]):
        x, y, w, h = stats[i, :4]
        g = (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255
        if w > 0.85 * h:  # two touching digits: split at the thinnest column near the middle
            ink_cols = (g > 0).sum(axis=0)
            lo, hi = int(w * 0.3), int(w * 0.7)
            cut = lo + int(np.argmin(ink_cols[lo:hi]))
            pieces += [g[:, :cut], g[:, cut:]]
        else:
            pieces.append(g)
    out = []
    for g in pieces:
        cols = np.where(g.max(axis=0) > 0)[0]
        rows = np.where(g.max(axis=1) > 0)[0]
        if not len(cols):
            continue
        g = g[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        h, w = g.shape
        side = max(w, h)  # pad to a square so narrow glyphs (1) keep their shape
        sq = np.zeros((side, side), np.uint8)
        sq[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = g
        out.append(cv2.resize(sq, (GLYPH, GLYPH), interpolation=cv2.INTER_AREA).astype(np.float32) / 255)
    return out


def load_templates(path=TEMPLATES_PATH):
    global TEMPLATES
    data = np.load(path)
    TEMPLATES = {int(k): data[k] for k in data.files}


def classify(glyph):
    """Closest digit template: (digit, distance)."""
    best = min(TEMPLATES.items(), key=lambda kv: np.abs(kv[1] - glyph).mean())
    return best[0], float(np.abs(best[1] - glyph).mean())


def read_number(crop, max_dist=0.18):
    """A number cell via digit templates: int, None (blank) or '?' (no confident read)."""
    if is_blank(crop):
        return None
    gs = glyphs(crop)
    if not gs or len(gs) > 2:
        return "?"
    digits = [classify(g) for g in gs]
    if any(d > max_dist for _, d in digits):
        return "?"
    return int("".join(str(d) for d, _ in digits))


# Bootstrap readers, used only before templates exist (see templates.py).

def _shape(crop):
    big = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _, ink = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink)
    comps = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > 40]
    cnts, hier = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = []
    if hier is not None:
        for c, hrow in zip(cnts, hier[0]):
            if hrow[3] != -1 and cv2.contourArea(c) > 15:
                holes.append(cv2.boundingRect(c))
    ys = ([stats[i, cv2.CC_STAT_TOP] for i in comps]
          + [stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in comps])
    mid = (min(ys) + max(ys)) / 2 if ys else 0
    return len(comps), len(holes), any(hy + hh / 2 < mid for _, hy, _, hh in holes)


def bootstrap_score(crop):
    """10 = two marks, one with a hole; 8 = two holes; 9 = one hole on top; 7 = none."""
    if is_blank(crop):
        return None
    comps, holes, top = _shape(crop)
    if comps == 2 and holes == 1:
        return 10
    if comps == 1:
        if holes == 2:
            return 8
        if holes == 1 and top:
            return 9
        if holes == 0:
            return 7
    return "?"


def bootstrap_number(crop):
    if is_blank(crop):
        return None
    t = tess(prep(crop), 7, digits=True)
    return int(t) if t.isdigit() else bootstrap_score(crop)


# --- the card ---------------------------------------------------------------------------

def read_card(path, keep_crops=False):
    img = cv2.imread(str(path))
    if img is None:
        return {"error": "unreadable image"}
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = g.shape
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cards = sorted(r for r in map(cv2.boundingRect, cnts) if r[2] > W * 0.2 and r[3] > H * 0.25)
    if len(cards) != 3:
        return {"error": f"found {len(cards)} judge cards"}

    score = read_number if TEMPLATES else bootstrap_score
    number = read_number if TEMPLATES else bootstrap_number
    judges = []
    for x, y, w, h in cards:
        sub_bw = bw[y:y + h, x:x + w]
        sub = g[y:y + h, x:x + w]
        hs = lines(sub_bw, 0, w // 3)
        vs = lines(sub_bw, 1, h // 8)
        if len(hs) < 6 or len(vs) < 6:
            return {"error": f"grid not found (h={hs}, v={vs})"}
        # columns: red score | red deduction | round | blue deduction | blue score
        cols = [vs[0], vs[1], vs[2], vs[-3], vs[-2], vs[-1]]
        round_rows = list(zip(hs[2:-3], hs[3:-2]))
        pad = 3

        def cell(r, c):
            return sub[r[0] + pad:r[1] - pad + 1, cols[c] + pad:cols[c + 1] - pad + 1]

        rounds, crops = [], {"rounds": [], "round_no": []}
        for r in round_rows:
            rounds.append({"red": score(cell(r, 0)), "red_ded": number(cell(r, 1)),
                           "blue_ded": number(cell(r, 3)), "blue": score(cell(r, 4))})
            crops["rounds"].append((cell(r, 0), cell(r, 4)))
            crops["round_no"].append(cell(r, 2))
        tr = (hs[-3], hs[-2])  # totals sit left and right of the "TOTAL" label
        red_crop = sub[tr[0] + pad:tr[1] - pad, cols[0] + pad:cols[2] - pad]
        blue_crop = sub[tr[0] + pad:tr[1] - pad, cols[3] + pad:cols[5] - pad]
        crops["total"] = (red_crop, blue_crop)
        name_txt = tess(prep(sub[hs[-2] + pad:h - pad, pad:w - pad], 3), 6)
        name_lines = [ln for ln in name_txt.splitlines() if ln.strip() and "JUDGE" not in ln.upper()]
        judge = {"judge": name_lines[-1].strip() if name_lines else "?", "rounds": rounds,
                 "total": (number(red_crop), number(blue_crop))}
        if keep_crops:
            judge["_crops"] = crops
        judges.append(judge)

    # Header above the cards: left block has event + date, right block has "RED vs. BLUE".
    top_y = cards[0][1]
    right = tess(prep(g[:top_y, int(W * 0.42):], 3), 6)
    vs_line = next((ln for ln in right.splitlines() if re.search(r"\bVS\b", ln.upper())), "")
    parts = re.split(r"\s+VS\.?\s+", vs_line.upper(), maxsplit=1)

    def clean(t):
        return " ".join(re.sub(r"[^A-Z'. -]", " ", t).split()).strip(" .")

    red, blue = (clean(parts[0]), clean(parts[1])) if len(parts) == 2 else ("", "")
    top = tess(prep(g[:top_y, :int(W * 0.42)], 3), 6)
    m = re.search(r"(\d{2})\D{0,2}(\d{2})\D{0,2}(20\d{2})", top)
    date = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None
    event = next((ln.strip() for ln in top.splitlines()
                  if "UFC" in ln.upper() and (":" in ln or re.search(r"\bV[S5]\b", ln.upper()))), None)
    # All the name text on the card (header + first card's name boxes), for bout matching.
    x0, y0, w0, h0 = cards[0]
    hs0 = lines(bw[y0:y0 + h0, x0:x0 + w0], 0, w0 // 3)
    box = g[y0 + 2:y0 + hs0[1] - 2, x0 + 3:x0 + w0 - 3]
    names_text = " ".join([right, tess(prep(box, 3), 6)])
    res = tess(prep(g[cards[0][1] + cards[0][3]:, int(W * 0.66):], 3), 6)
    result = " ".join(ln for ln in res.splitlines() if ln.strip() and "RESULT" not in ln.upper())
    return {"event": event, "date": date, "red": red, "blue": blue, "result": result,
            "names_text": names_text, "top_text": top, "judges": judges}
