"""Brand lookup for subject chips: official logo when known, text otherwise."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import REPO_ROOT

LOGO_ROOT = REPO_ROOT / "skills" / "ai-brief-cover-generator"
LOGO_MANIFEST = LOGO_ROOT / "references" / "logo_manifest.json"


@lru_cache(maxsize=1)
def _entries() -> tuple[dict[str, Any], ...]:
    try:
        data = json.loads(LOGO_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return tuple()
    return tuple(entry for entry in data.get("entries") or [] if isinstance(entry, dict))


def _alias_pattern(alias: str) -> re.Pattern[str]:
    if re.fullmatch(r"[A-Za-z0-9 .\-]+", alias):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(alias))


def find_brand(text: str) -> dict[str, Any] | None:
    """Return the first brand mentioned in ``text`` (earliest position, longest alias)."""

    best: tuple[int, int, dict[str, Any]] | None = None
    for entry in _entries():
        for alias in [entry.get("name"), *(entry.get("aliases") or [])]:
            if not alias:
                continue
            match = _alias_pattern(str(alias)).search(text or "")
            if match:
                key = (match.start(), -len(str(alias)))
                if best is None or key < (best[0], best[1]):
                    best = (key[0], key[1], entry)
    if best is None:
        return None
    entry = best[2]
    asset = LOGO_ROOT / str(entry.get("asset") or "")
    return {"name": entry.get("name"), "logo_path": str(asset) if asset.is_file() else ""}


def logo_silhouette(source: Path, destination: Path, rgb: tuple[int, int, int]) -> tuple[int, int]:
    """One-colour silhouette of a logo (transparent PNG, trimmed), for the big brand backdrop.

    Logos come in every colour (white marks made for dark backgrounds, multi-colour, black on an
    opaque white canvas), so the backdrop uses the shape only: a logo drawn on an opaque white
    canvas is keyed (near-white -> transparent, soft edge) first. Returns the trimmed size.
    """

    from PIL import Image, ImageChops, ImageStat

    image = Image.open(source).convert("RGBA")
    alpha = image.getchannel("A")
    opaque_share = ImageStat.Stat(alpha.point(lambda v: 255 if v > 128 else 0)).mean[0] / 255
    if opaque_share > 0.9:   # drawn on an opaque white canvas: whatever is not near-white is the logo
        r, g, b = image.convert("RGB").split()
        lowest = ImageChops.darker(ImageChops.darker(r, g), b)   # a coloured mark keeps a low channel
        keyed = lowest.point(lambda v: 0 if v > 235 else 255 if v <= 200 else int(255 * (235 - v) / 35))
        alpha = ImageChops.multiply(alpha, keyed)
    box = alpha.point(lambda v: 255 if v > 10 else 0).getbbox() or (0, 0, image.width, image.height)
    alpha = alpha.crop(box)
    silhouette = Image.new("RGBA", alpha.size, tuple(rgb) + (0,))
    silhouette.putalpha(alpha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    silhouette.save(destination)
    return silhouette.size


def trimmed_logo(source: Path, destination: Path, *, padding: int = 14) -> Path:
    """Copy a logo with its empty margin removed so chips size it correctly."""

    from PIL import Image, ImageChops

    image = Image.open(source).convert("RGBA")
    # A white-on-transparent logo (e.g. xAI) would vanish on the white chip: recolor it to the ink colour.
    alpha = image.getchannel("A")
    visible = alpha.point(lambda v: 255 if v > 128 else 0)
    from PIL import ImageStat

    transparent_share = 1 - ImageStat.Stat(visible).mean[0] / 255
    if visible.getbbox() and transparent_share > 0.1:  # only logos drawn on a transparent background
        if ImageStat.Stat(image.convert("L"), mask=visible).mean[0] > 200:
            ink = Image.new("RGBA", image.size, (32, 40, 43, 255))
            ink.putalpha(alpha)
            image = ink
    flat = Image.alpha_composite(Image.new("RGBA", image.size, (255, 255, 255, 255)), image).convert("RGB")
    mask = ImageChops.difference(flat, Image.new("RGB", image.size, (255, 255, 255))).point(lambda v: 255 if v > 30 else 0)
    box = mask.getbbox() or (0, 0, image.width, image.height)
    box = (max(0, box[0] - padding), max(0, box[1] - padding), min(image.width, box[2] + padding), min(image.height, box[3] + padding))
    destination.parent.mkdir(parents=True, exist_ok=True)
    flat.crop(box).save(destination)
    return destination
