"""Split an image into horizontal strips that are encoded side by side.

PNG and JPEG encoders run on one core, so a large frame takes as long to encode
as its biggest single piece of work. Cutting it into strips and encoding them on
a thread pool (OpenCV releases the GIL while it encodes) divides the time by
roughly the number of strips; the receiving services decode them the same way.

On the wire the strips stay one text value: each strip's base64, top to bottom,
joined by STRIP_SEPARATOR ("|" never appears in base64). Storage, MQTT and
exports therefore need no changes, and a one-strip image is exactly the old format.
"""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from os import cpu_count
from typing import Callable

import numpy as np

STRIP_SEPARATOR = "|"
#: Strip heights are multiples of this, so JPEG's 16-row blocks are never cut.
ROW_ALIGN = 16

_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=min(8, cpu_count() or 1), thread_name_prefix="image_strip")
    return _POOL


def strip_bounds(height: int, strips: int, align: int = ROW_ALIGN) -> list[tuple[int, int]]:
    """Row ranges for ``strips`` strips of near-equal height, cut on ``align`` rows."""
    strips = max(1, min(int(strips), max(1, height // align)))
    cuts = [0]
    for i in range(1, strips):
        cut = int(round(height * i / strips / align)) * align
        if cuts[-1] < cut < height:
            cuts.append(cut)
    cuts.append(height)
    return list(zip(cuts, cuts[1:]))


def run_in_row_blocks(rows: int, work: Callable[[int, int], None], blocks: int = 4, min_rows: int = 256) -> None:
    """Call ``work(top, bottom)`` for ``blocks`` row ranges on the thread pool and wait.

    For whole-frame numpy work (lookups, rounding) that runs on one core: the
    blocks write disjoint rows of a preallocated output, and numpy releases the
    GIL on large array operations. Small images run in one piece.
    """
    if blocks <= 1 or rows < min_rows:
        work(0, rows)
        return
    cuts = [rows * i // blocks for i in range(blocks + 1)]
    list(_pool().map(lambda i: work(cuts[i], cuts[i + 1]), range(blocks)))


def encode_strips(image: np.ndarray, strips: int, encode: Callable[[np.ndarray], bytes]) -> str:
    """Encode ``image`` as ``strips`` horizontal strips in parallel; returns the wire text."""
    if strips is None or int(strips) <= 1:
        return b64encode(encode(image)).decode("ascii")
    pieces = [image[top:bottom] for top, bottom in strip_bounds(image.shape[0], strips)]
    encoded = _pool().map(lambda piece: b64encode(encode(piece)).decode("ascii"), pieces)
    return STRIP_SEPARATOR.join(encoded)
