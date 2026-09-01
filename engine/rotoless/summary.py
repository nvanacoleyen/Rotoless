"""The wire contract between the engine and the Resolve script.

Kept in its own module, free of numpy/cv2/mlx imports, so the test suite can
check the format without standing up the whole inference environment.

Key order is load-bearing. Fusion's embedded Lua has no JSON library, so the
Resolve side pattern-matches this text rather than parsing it: obj_id, frames
and dir must stay adjacent and in this order. tests/test_placement.py feeds
this function's real output through the shipped Lua pattern, so the two cannot
drift apart unnoticed.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from rotoless import guard

FRAME_PATTERN = "cutout_%06d.png"


def build_summary(written: dict[int, list[Path]], out_dir: Path) -> OrderedDict:
    """One line of JSON describing every cutout sequence that was written."""
    objects = [
        OrderedDict((("obj_id", obj_id),
                     ("frames", len(paths)),
                     ("dir", str(out_dir / f"object_{obj_id}")),
                     ("first", str(paths[0]) if paths else None)))
        for obj_id, paths in sorted(written.items())
    ]
    return OrderedDict((
        ("objects", objects),
        ("frames", max((o["frames"] for o in objects), default=0)),
        ("out_dir", str(out_dir)),
        ("pattern", FRAME_PATTERN),
        ("peak_rss_gb", round(guard.current_rss_bytes() / 1024**3, 2)),
    ))
