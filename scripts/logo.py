"""Draw the Fight Canon mark: a career curve that peaks right of center, inside an uncertainty
band that is wide at the start, tight at the peak and flares at the end. Three solid golds;
the line tapers toward both ends.

    python scripts/logo.py site     # writes site/logo.svg and site/favicon.svg
"""
import math
import sys
from pathlib import Path

PEAK = 0.62
N = 60
X0, X1 = 6.0, 58.0          # mark spans x in a 64-unit box
BASE, HEIGHT = 41.0, 26.0   # curve from y=BASE up to y=BASE-HEIGHT


def h(t):
    s = 0.33 if t < PEAK else 0.24
    return math.exp(-((t - PEAK) / s) ** 2)


def y(t):
    return BASE - HEIGHT * h(t)


def band(t):
    if t < PEAK:
        return 2.2 + 9.0 * ((PEAK - t) / PEAK) ** 1.6
    return 2.2 + 12.0 * ((t - PEAK) / (1 - PEAK)) ** 1.6


def line(t):
    return 0.25 + 1.55 * math.sin(math.pi * t) ** 1.3


def ribbon(width):
    ts = [i / N for i in range(N + 1)]
    xs = [X0 + (X1 - X0) * t for t in ts]
    top = [(x, y(t) - width(t)) for x, t in zip(xs, ts)]
    bot = [(x, y(t) + width(t)) for x, t in zip(xs, ts)]
    pts = top + bot[::-1]
    return "M" + " L".join(f"{a:.1f} {b:.1f}" for a, b in pts) + "Z"


def mark():
    return (f'<path fill="#5E4A27" d="{ribbon(band)}"/>'
            f'<path fill="#9C7431" d="{ribbon(lambda t: band(t) * 0.5)}"/>'
            f'<path fill="#F2B84B" d="{ribbon(line)}"/>')


site = Path(sys.argv[1])
(site / "logo.svg").write_text(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{mark()}</svg>\n', encoding="utf-8")
(site / "favicon.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    f'<rect width="64" height="64" rx="14" fill="#252E3E"/>{mark()}</svg>\n', encoding="utf-8")
