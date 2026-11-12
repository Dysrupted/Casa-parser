"""Pure similarity primitives for duplicate detection."""
from __future__ import annotations

import io
import re

from radar.config import norm_text


def bigrams(text: str | None) -> set[tuple[str, str]]:
    words = re.findall(r"\w+", norm_text(text or ""))
    return set(zip(words, words[1:]))


def description_similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity of word bigrams: robust to reordered sentences and agency boilerplate."""
    x, y = bigrams(a), bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def phash_hex(image_bytes: bytes) -> str | None:
    """64-bit perceptual hash as hex; None if the bytes aren't a decodable image."""
    import imagehash
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            return str(imagehash.phash(im.convert("RGB")))
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def min_hamming(a: list[str], b: list[str]) -> int | None:
    if not a or not b:
        return None
    import imagehash

    ha = [imagehash.hex_to_hash(h) for h in a]
    hb = [imagehash.hex_to_hash(h) for h in b]
    return min(x - y for x in ha for y in hb)


def within(a: float, b: float, rel: float) -> bool:
    lo = min(a, b)
    return lo > 0 and abs(a - b) / lo <= rel
