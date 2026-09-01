"""ffmpeg-backed frame extraction.

Frames are written downscaled to a long edge of `max_edge` (1024 by default),
which is exactly the resolution SAM 2.1 works at internally. Decoding straight
to that size means we never materialise full-resolution frames in memory, and
costs nothing in mask quality -- mlx_sam would have downsampled to 1024 anyway.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Resolve launches scripts with a minimal PATH that does not include Homebrew,
# so bare "ffmpeg" resolves in a normal shell but not under Resolve. Always use
# an absolute path.
_SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin")


@lru_cache(maxsize=8)
def tool(name: str) -> str:
    """Absolute path to an external binary, independent of the inherited PATH."""
    override = os.environ.get("ROTOLESS_" + name.upper())
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    for base in _SEARCH_DIRS:
        candidate = Path(base) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(
        f"{name} not found. Install it (brew install ffmpeg), or set "
        f"ROTOLESS_{name.upper()} to its absolute path. "
        f"Searched PATH and {', '.join(_SEARCH_DIRS)}."
    )


@dataclass(frozen=True)
class SourceInfo:
    width: int
    height: int
    fps: float
    nb_frames: int | None


def probe(video: Path) -> SourceInfo:
    """Read stream geometry without decoding any pixels."""
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, _, den = stream["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1)
    raw_frames = stream.get("nb_frames")
    return SourceInfo(
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=fps,
        nb_frames=int(raw_frames) if raw_frames not in (None, "N/A") else None,
    )


def scaled_size(info: SourceInfo, max_edge: int | None) -> tuple[int, int]:
    """Target decode size: long edge clamped to max_edge, aspect preserved, even dims.

    max_edge of None means decode at native resolution.
    """
    if max_edge is None:
        return info.width, info.height
    long_edge = max(info.width, info.height)
    if long_edge <= max_edge:
        return info.width, info.height
    ratio = max_edge / long_edge
    # Force even dimensions -- some encoders reject odd sizes.
    return (max(2, int(round(info.width * ratio)) // 2 * 2),
            max(2, int(round(info.height * ratio)) // 2 * 2))


def _rate_control_args() -> list[str]:
    """ffmpeg >= 5 replaced -vsync with -fps_mode; ffmpeg 9 removed -vsync outright."""
    global _RATE_ARGS
    if _RATE_ARGS is None:
        probe_out = subprocess.run([tool("ffmpeg"), "-hide_banner", "-h", "full"],
                                   capture_output=True, text=True).stdout
        _RATE_ARGS = ["-fps_mode", "passthrough"] if "fps_mode" in probe_out else ["-vsync", "0"]
    return _RATE_ARGS


_RATE_ARGS: list[str] | None = None


def extract_frames(video: Path, out_dir: Path, max_edge: int | None = 1024,
                   start_frame: int = 0, num_frames: int | None = None,
                   fmt: str = "jpg") -> list[Path]:
    """Decode [start_frame, start_frame+num_frames) to numbered frames in out_dir.

    fmt "jpg" for the model's input (small, lossy is fine at 1024), "png" for
    full-resolution frames that will be composited into a deliverable.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    width, height = scaled_size(info, max_edge)

    # select is frame-accurate; -ss would seek to a keyframe and drift.
    # No shell is involved, so the filter string must NOT be quoted.
    vf = f"select=gte(n\\,{start_frame}),scale={width}:{height}"
    cmd = [tool("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-vf", vf, *_rate_control_args()]
    if fmt == "jpg":
        cmd += ["-q:v", "2"]
    if num_frames is not None:
        cmd += ["-frames:v", str(num_frames)]
    cmd += [str(out_dir / f"%06d.{fmt}")]

    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({done.returncode}): {done.stderr.strip()}")

    return sorted(out_dir.glob(f"*.{fmt}"))
