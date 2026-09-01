"""CLI entry point. The Resolve script shells out to this in the engine venv."""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

from rotoless import guard
from rotoless.segment import DEFAULT_MODEL, ObjectPrompt, segment_clip


def _point(value: str) -> tuple[float, float, int, int]:
    """Parse 'x,y,label[,obj_id]'; label 1 = include, 0 = exclude."""
    parts = value.split(",")
    if len(parts) not in (3, 4):
        raise argparse.ArgumentTypeError(f"expected x,y,label[,obj_id] -- got {value!r}")
    obj_id = int(parts[3]) if len(parts) == 4 else 1
    return float(parts[0]), float(parts[1]), int(parts[2]), obj_id


def _prompts_from_points(points, box) -> list[ObjectPrompt]:
    by_id: "OrderedDict[int, ObjectPrompt]" = OrderedDict()
    for x, y, label, obj_id in points:
        prompt = by_id.setdefault(obj_id, ObjectPrompt(obj_id=obj_id))
        prompt.points.append((x, y))
        prompt.labels.append(label)
    if box is not None:
        prompt = by_id.setdefault(1, ObjectPrompt(obj_id=1))
        prompt.box = tuple(box)
    return list(by_id.values())


def _prompts_from_session(objects) -> list[ObjectPrompt]:
    return [ObjectPrompt(obj_id=o["obj_id"], points=o["points"], labels=o["labels"])
            for o in objects if o["points"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rotoless", description=__doc__)
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--point", action="append", type=_point, default=[],
                    help="x,y,label[,obj_id] in source pixels; repeatable. "
                         "Use distinct obj_ids to track several objects at once.")
    ap.add_argument("--box", type=float, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--chunk-size", type=int, default=32)
    ap.add_argument("--max-edge", type=int, default=1024)
    ap.add_argument("--max-frames", type=int, default=None,
                    help="cap total frames processed (useful for smoke tests)")
    ap.add_argument("--rss-limit-gb", type=float,
                    default=guard.DEFAULT_RSS_LIMIT_BYTES / 1024**3)
    ap.add_argument("--start-frame", type=int, default=0,
                    help="first frame of the source file to process")
    ap.add_argument("--pick", action="store_true",
                    help="open the click picker instead of taking --point")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable summary")
    args = ap.parse_args(argv)

    session = None
    engine = None
    if args.pick:
        from rotoless.picker import NotReadyError, Session
        from rotoless.segment import PreviewEngine

        # Load the model while the user is still choosing points, so "Preview
        # mask" is responsive rather than paying for a cold start on click.
        engine = PreviewEngine(args.video, frame_index=args.start_frame,
                               model_id=args.model)
        engine.start()

        def preview_fn(objects):
            if not engine.ready.wait(timeout=120):
                raise NotReadyError("model is still loading -- try again shortly")
            if engine.error is not None:
                raise engine.error
            return engine.overlay_png(objects)

        session = Session(args.video, frame=args.start_frame)
        session.preview_fn = preview_fn
        session.start()
        prompts = _prompts_from_session(session.wait_for_objects())

        # Release the preview model before the run. Its MLX state is bound to
        # the worker thread, so it cannot be reused from here; paying one extra
        # model load (~4s of a much longer run) buys thread-safety.
        engine.close()
    else:
        if not args.point and not args.box:
            ap.error("at least one --point or a --box is required")
        prompts = _prompts_from_points(args.point, args.box)

    if not prompts:
        ap.error("no objects were prompted")

    def log(msg):
        print(msg, file=sys.stderr, flush=True)
        if session is not None:
            session.log(msg)

    try:
        written = segment_clip(
            args.video, args.out, prompts,
            model_id=args.model, chunk_size=args.chunk_size, max_edge=args.max_edge,
            total_frames=args.max_frames, start_frame=args.start_frame,
            rss_limit=int(args.rss_limit_gb * 1024**3),
            progress=log,
            on_frame=(session.advance if session else None),
            on_total=(session.set_total if session else None),
        )
    except MemoryError as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        if session is not None:
            session.finish(False, "Aborted: memory guard tripped.")
            session.stop(linger=20)
        return 2
    except Exception as exc:
        if session is not None:
            session.log(repr(exc))
            session.finish(False, "Failed. See the log below.")
            session.stop(linger=20)
        raise

    # Key order matters: the Lua side pattern-matches this rather than parsing
    # JSON, so obj_id/frames/dir/first must stay in this order.
    objects = [
        OrderedDict((("obj_id", obj_id),
                     ("frames", len(paths)),
                     ("dir", str(args.out / f"object_{obj_id}")),
                     ("first", str(paths[0]) if paths else None)))
        for obj_id, paths in sorted(written.items())
    ]
    frames = max((o["frames"] for o in objects), default=0)
    summary = OrderedDict((
        ("objects", objects),
        ("frames", frames),
        ("out_dir", str(args.out)),
        ("pattern", "cutout_%06d.png"),
        ("peak_rss_gb", round(guard.current_rss_bytes() / 1024**3, 2)),
    ))

    if session is not None:
        session.finish(True)
        session.stop(linger=15)

    if args.json:
        print(json.dumps(summary), flush=True)
    else:
        log(f"wrote {frames} frames for {len(objects)} object(s) to {summary['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
