"""Chunked SAM 2.1 video segmentation.

The clip is never handed to init_state whole. It is decoded and tracked in
bounded chunks that overlap by exactly one frame: the final mask of chunk N
seeds frame 0 of chunk N+1 via add_new_mask, and that duplicated frame is
dropped from the output. Because the overlap frame is literally the same
frame, the handoff is exact rather than an approximation, and peak memory
becomes a function of chunk_size instead of clip length.
"""
from __future__ import annotations

import queue
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from rotoless import decode, guard

DEFAULT_MODEL = "avbiswas/sam2.1-hiera-small-mlx"


# Shared with the picker page so an object's colour is the same in the browser
# overlay and in any diagnostic render. Index by (obj_id - 1).
PALETTE = [
    (61, 220, 132),    # green
    (94, 190, 255),    # blue
    (255, 120, 200),   # pink
    (255, 176, 66),    # orange
    (200, 140, 255),   # purple
    (255, 235, 90),    # yellow
]


def color_for(obj_id: int) -> tuple[int, int, int]:
    return PALETTE[(int(obj_id) - 1) % len(PALETTE)]


@dataclass
class ObjectPrompt:
    """Click prompts for one tracked object, in ORIGINAL clip pixel coordinates."""
    obj_id: int = 1
    points: list[tuple[float, float]] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)   # 1 = include, 0 = exclude
    box: tuple[float, float, float, float] | None = None

    def scaled(self, sx: float, sy: float) -> "ObjectPrompt":
        return ObjectPrompt(
            obj_id=self.obj_id,
            points=[(x * sx, y * sy) for x, y in self.points],
            labels=list(self.labels),
            box=None if self.box is None
            else (self.box[0] * sx, self.box[1] * sy, self.box[2] * sx, self.box[3] * sy),
        )


def _write_cutout(logits: np.ndarray, source_frame: Path, dest: Path) -> np.ndarray:
    """Composite the subject onto transparency and write an RGBA PNG.

    The mask is computed at 1024px but the deliverable must match the source,
    so the binary mask is upscaled to the full frame. Linear interpolation
    antialiases the edge, which is what makes the alpha usable for compositing
    rather than a hard nearest-neighbour staircase.

    Returns the decoded-resolution binary mask for the next chunk's handoff.
    """
    binary = (logits > 0).astype(np.float32)
    rgb = np.asarray(Image.open(source_frame).convert("RGB"))
    height, width = rgb.shape[:2]
    alpha = cv2.resize(binary, (width, height), interpolation=cv2.INTER_LINEAR)
    rgba = np.dstack([rgb, np.clip(alpha * 255.0, 0, 255).astype(np.uint8)])
    Image.fromarray(rgba, "RGBA").save(dest)
    return binary


class PreviewEngine:
    """Loads the model and answers preview requests, all on one worker thread.

    MLX state is thread-affine -- using a model from a thread other than the
    one that built it raises "There is no Stream(gpu, 0) in current thread" --
    and ThreadingHTTPServer hands every request to a fresh thread. So rather
    than reason about which thread owns what, every MLX call is funnelled onto
    a single long-lived worker. That also serialises concurrent preview clicks,
    which MLX would not tolerate anyway.

    The frame's encoded features stay cached in the inference state (reset_state
    deliberately leaves them alone), so re-previewing with different points
    re-runs only the lightweight prompt head, not the image encoder.
    """

    def __init__(self, video: Path, frame_index: int = 0,
                 model_id: str = DEFAULT_MODEL, obj_id: int = 1):
        self.video = Path(video)
        self.frame_index = frame_index
        self.model_id = model_id
        self.obj_id = obj_id
        self.predictor = None
        self.state = None
        self.error: Exception | None = None
        self.ready = threading.Event()
        self._dir: Path | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._worker.start()

    def _serve(self) -> None:
        try:
            from mlx_sam import SAM2VideoPredictor
            guard.apply_mlx_limits()
            self._dir = Path(tempfile.mkdtemp(prefix="mm_preview_"))
            # Native resolution: the picker shows the frame full size, so clicks
            # arrive in source pixels and the mask returns matching them.
            frames = decode.extract_frames(self.video, self._dir, max_edge=None,
                                           start_frame=self.frame_index,
                                           num_frames=1, fmt="png")
            if not frames:
                raise ValueError("could not decode the preview frame")
            self.predictor = SAM2VideoPredictor.from_pretrained(
                self.model_id, non_overlap_masks=True)
            self.state = self.predictor.init_state(str(self._dir))
        except Exception as exc:                      # surfaced to the page
            self.error = exc
        finally:
            self.ready.set()

        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, box, done = job
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

    def _submit(self, fn):
        if self.error is not None:
            raise self.error
        box: dict = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _overlay(self, objects) -> bytes:
        """Render every object's mask in its own colour as one RGBA PNG.

        `objects` is [{"obj_id": int, "points": [(x, y)], "labels": [int]}].
        Adding an object returns masks for every object registered so far, so
        the last call carries all of them.
        """
        self.predictor.reset_state(self.state)
        obj_ids, masks = None, None
        for entry in objects:
            if not entry["points"]:
                continue
            _, obj_ids, masks = self.predictor.add_new_points_or_box(
                self.state, 0, int(entry["obj_id"]),
                points=entry["points"], labels=entry["labels"])

        height = int(self.state["video_height"])
        width = int(self.state["video_width"])
        rgba = np.zeros((height, width, 4), np.uint8)

        if masks is not None:
            kernel = np.ones((3, 3), np.uint8)
            for slot, obj_id in enumerate(obj_ids):
                mask = (masks[slot, 0] > 0).astype(np.uint8)
                if not mask.any():
                    continue
                red, green, blue = color_for(obj_id)
                edge = (cv2.dilate(mask, kernel, iterations=2)
                        - cv2.erode(mask, kernel, iterations=2))
                rgba[mask > 0] = (red, green, blue, 78)    # light enough to see through
                rgba[edge > 0] = (red, green, blue, 255)   # crisp boundary

        ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        if not ok:
            raise RuntimeError("failed to encode the preview overlay")
        return buf.tobytes()

    def overlay_png(self, objects) -> bytes:
        return self._submit(lambda: self._overlay(objects))

    def close(self) -> None:
        """Stop the worker and release the model before the main run starts."""
        self._jobs.put(None)
        self._worker.join(timeout=10)
        self.predictor = None
        self.state = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass


def segment_clip(video: Path, out_dir: Path, prompts: list[ObjectPrompt], *,
                 model_id: str = DEFAULT_MODEL, chunk_size: int = 32,
                 max_edge: int = 1024,
                 total_frames: int | None = None, start_frame: int = 0,
                 rss_limit: int = guard.DEFAULT_RSS_LIMIT_BYTES,
                 progress=print, on_frame=None, on_total=None,
                 predictor=None) -> dict[int, list[Path]]:
    """Track every prompted object, writing one cutout sequence per object.

    Each object gets its own subfolder, `object_<id>/cutout_%06d.png`, so each
    can be imported into Resolve as an independent sequence and dropped on its
    own track. Frames are numbered from 0 regardless of start_frame.

    start_frame offsets into the source file, so a timeline clip that uses only
    part of its media segments just that range.

    All objects are tracked in a single pass: SAM 2 propagates them together,
    which is both faster than one pass each and lets non_overlap_masks keep
    their mattes mutually exclusive.
    """
    if not prompts:
        raise ValueError("at least one object prompt is required")
    from mlx_sam import SAM2VideoPredictor
    import mlx.core as mx

    guard.apply_mlx_limits()
    out_dir.mkdir(parents=True, exist_ok=True)

    info = decode.probe(video)
    dec_w, dec_h = decode.scaled_size(info, max_edge)
    total = total_frames or info.nb_frames
    if not total:
        raise ValueError(f"Could not determine frame count for {video}")

    # Prompts arrive in source pixels; the model sees the downscaled frames.
    scaled_prompts = [p.scaled(dec_w / info.width, dec_h / info.height) for p in prompts]

    progress(f"source {info.width}x{info.height} @ {info.fps:.3f} fps, {total} frames")
    progress(f"decoding to {dec_w}x{dec_h}, chunks of {chunk_size}")
    progress(f"tracking {len(prompts)} object(s): "
             + ", ".join(f"#{p.obj_id} ({len(p.points)} pts)" for p in prompts))

    # One decode pass for the whole range. Frames live on disk (~150 KB each),
    # so this is cheap; only a chunk's worth is ever loaded into memory.
    frames_root = Path(tempfile.mkdtemp(prefix="mm_frames_"))
    all_frames = decode.extract_frames(video, frames_root, max_edge=max_edge,
                                       start_frame=start_frame, num_frames=total)
    if not all_frames:
        raise ValueError(f"decoded no frames from {video}")
    total = min(total, len(all_frames))
    progress(f"decoded {len(all_frames)} frames to disk")

    # Second pass at native resolution. The model never sees these; they exist
    # only so the cutout matches the source frame size. Kept on disk and read
    # one at a time, so this costs disk rather than memory.
    full_root = Path(tempfile.mkdtemp(prefix="mm_full_"))
    full_frames = decode.extract_frames(video, full_root, max_edge=None,
                                        start_frame=start_frame, num_frames=total,
                                        fmt="png")
    if len(full_frames) < total:
        raise ValueError(f"full-resolution decode produced {len(full_frames)} of {total} frames")
    progress(f"decoded {len(full_frames)} full-resolution frames "
             f"({sum(f.stat().st_size for f in full_frames) / 1024**3:.2f} GB on disk)")
    if on_total is not None:
        on_total(total)

    if predictor is None:
        # non_overlap_masks makes the objects mutually exclusive, so two
        # subjects that touch do not both claim the same pixels.
        predictor = SAM2VideoPredictor.from_pretrained(
            model_id, non_overlap_masks=len(prompts) > 1)
    written: dict[int, list[Path]] = {p.obj_id: [] for p in prompts}
    last_masks: dict[int, np.ndarray] = {}
    for p in prompts:
        (out_dir / f"object_{p.obj_id}").mkdir(parents=True, exist_ok=True)
    done = 0
    start = 0

    while start < total:
        overlap = 0 if start == 0 else 1
        chunk_start = start - overlap
        want = min(chunk_size + overlap, total - chunk_start)

        chunk_dir = Path(tempfile.mkdtemp(prefix="mm_chunk_"))
        try:
            # Symlink this chunk's slice out of the single decoded set. Decoding
            # per chunk instead would re-scan the file from frame 0 every time,
            # because ffmpeg's select filter has no fast seek.
            frames = []
            for offset in range(want):
                src = all_frames[chunk_start + offset]
                link = chunk_dir / src.name
                link.symlink_to(src)
                frames.append(link)
            if not frames:
                break

            state = predictor.init_state(str(chunk_dir))
            if overlap == 0:
                for sp in scaled_prompts:
                    predictor.add_new_points_or_box(
                        state, 0, sp.obj_id,
                        points=sp.points or None,
                        labels=sp.labels or None,
                        box=sp.box,
                    )
            else:
                # Re-seed every object from its own final mask in the previous
                # chunk. The overlap frame is literally the same frame, so the
                # handoff stays exact no matter how many objects there are.
                for sp in scaled_prompts:
                    predictor.add_new_mask(state, 0, sp.obj_id, last_masks[sp.obj_id])

            for local_idx, obj_ids, masks in predictor.propagate_in_video(state):
                if local_idx < overlap:
                    continue                      # duplicated seed frame
                global_idx = chunk_start + local_idx
                for slot, oid in enumerate(obj_ids):
                    dest = out_dir / f"object_{oid}" / f"cutout_{global_idx:06d}.png"
                    last_masks[oid] = _write_cutout(
                        masks[slot, 0], full_frames[global_idx], dest)
                    written[oid].append(dest)
                done += 1
                if on_frame is not None:
                    on_frame(done)

            del state
            mx.clear_cache()
            guard.check_rss(rss_limit, where=f"chunk starting at frame {chunk_start}")
            progress(f"  frames {chunk_start}-{chunk_start + len(frames) - 1} done "
                     f"({done}/{total}), RSS {guard.current_rss_bytes()/1024**3:.2f} GB")
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)

        start += chunk_size

    shutil.rmtree(frames_root, ignore_errors=True)
    shutil.rmtree(full_root, ignore_errors=True)
    return written
