"""Memory guards.

The crash this module exists to prevent: mlx_sam's own `_load_video_frames`
decodes an entire clip into a list of full-resolution PIL images and then
concatenates every preprocessed 1024x1024 float32 frame, holding both copies
at once. On a 16 GB machine that reliably drives the system into swap thrash
and a watchdog kernel panic.

MLX's set_memory_limit() does NOT protect against that, because the blowup is
in numpy/PIL host memory before MLX is involved. The real defence is to never
hand a whole clip to init_state (see segment.py); these guards are the net
underneath that.
"""
from __future__ import annotations

import resource
import sys

# Abort before macOS starts swapping hard. 16 GB machine, Resolve possibly
# open, so keep our own footprint well under half of physical memory.
DEFAULT_RSS_LIMIT_BYTES = 5 * 1024**3


def current_rss_bytes() -> int:
    """Peak resident set size of this process, in bytes (macOS reports bytes)."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS reports bytes. Disambiguate by magnitude.
    return rss if sys.platform == "darwin" else rss * 1024


def check_rss(limit_bytes: int = DEFAULT_RSS_LIMIT_BYTES, where: str = "") -> None:
    """Raise before the machine starts thrashing, rather than after."""
    rss = current_rss_bytes()
    if rss > limit_bytes:
        raise MemoryError(
            f"RSS {rss / 1024**3:.2f} GB exceeded the {limit_bytes / 1024**3:.2f} GB guard"
            f"{' at ' + where if where else ''}. Aborting before the system swaps."
        )


def apply_mlx_limits(memory_limit_bytes: int = 4 * 1024**3,
                     cache_limit_bytes: int = 1 * 1024**3) -> None:
    """Secondary net: cap MLX's own allocator and buffer cache."""
    try:
        import mlx.core as mx
        mx.set_memory_limit(memory_limit_bytes)
        mx.set_cache_limit(cache_limit_bytes)
    except Exception:
        # Never let a guard be the reason the run fails.
        pass
