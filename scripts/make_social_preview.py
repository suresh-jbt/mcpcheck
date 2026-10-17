#!/usr/bin/env python3
"""Build ``assets/social-preview.png`` from ``assets/banner.webp``.

GitHub's social card is 1280x640 and accepts only PNG, JPG or GIF under 1 MB, while
the README banner is a 2000x667 WebP. Scaling to the card's width and centring on the
banner's own edge colour keeps every element (wordmark, server, spec, terminal) instead
of cropping a third of the image away.

Run with ``uvx --with pillow python scripts/make_social_preview.py``; Pillow is not a
project dependency because nothing at runtime needs it.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

CARD = (1280, 640)
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "banner.webp"
TARGET = ROOT / "assets" / "social-preview.png"
LIMIT = 1_048_576


def main() -> int:
    """Write the card and report its size; non-zero if GitHub would reject it."""
    from PIL import Image

    src = Image.open(SOURCE).convert("RGB")
    width, height = src.size
    edges = [src.getpixel((x, y)) for x in (0, width - 1) for y in range(0, height, 7)]
    edges += [src.getpixel((x, y)) for y in (0, height - 1) for x in range(0, width, 7)]
    background = collections.Counter(edges).most_common(1)[0][0]

    scaled = src.resize((CARD[0], round(height * CARD[0] / width)), Image.LANCZOS)
    card = Image.new("RGB", CARD, background)
    card.paste(scaled, (0, (CARD[1] - scaled.size[1]) // 2))
    card.save(TARGET, format="PNG", optimize=True)

    size = TARGET.stat().st_size
    print(f"{TARGET.relative_to(ROOT)}: {CARD[0]}x{CARD[1]}, {size} bytes")
    if size > LIMIT:
        print(f"too large: GitHub rejects anything over {LIMIT} bytes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
