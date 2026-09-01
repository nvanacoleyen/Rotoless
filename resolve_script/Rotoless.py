"""Rotoless -- AI subject masking for DaVinci Resolve (free edition).

Run from Resolve: Workspace > Scripts > Utility > Rotoless.

Reads the clip under the playhead, opens a click picker to select the subject,
tracks it with SAM 2.1 (MLX) in a separate virtualenv, and attaches the result
back onto the clip as a matte.

Deliberately written for an old embedded interpreter: no f-string '=', no
walrus, no PEP 604 unions. All heavy lifting happens in the engine subprocess,
so this file only needs the standard library.
"""

import json
import os
import subprocess
import sys
import time

ENGINE_DIR = "__ENGINE_DIR__"
OUTPUT_ROOT = os.path.expanduser("~/Movies/Rotoless")


def log(msg):
    print("[Rotoless] " + str(msg))


def get_resolve():
    """Resolve injects 'resolve' for menu scripts; fall back to the module."""
    found = globals().get("resolve")
    if found is not None:
        return found
    try:
        import DaVinciResolveScript
        return DaVinciResolveScript.scriptapp("Resolve")
    except Exception as exc:
        log("could not reach the Resolve API: " + repr(exc))
        return None


def fail(msg):
    log("ERROR: " + msg)
    return 1


def main():
    resolve = get_resolve()
    if resolve is None:
        return fail("no Resolve object. Run this from Workspace > Scripts.")

    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        return fail("no project is open.")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        return fail("no timeline is open.")

    item = timeline.GetCurrentVideoItem()
    if item is None:
        return fail("no clip under the playhead. Park on a clip and retry.")

    media_item = item.GetMediaPoolItem()
    if media_item is None:
        return fail("that timeline item has no media pool clip (a title or generator?).")

    path = media_item.GetClipProperty("File Path")
    if not path or not os.path.exists(path):
        return fail("could not resolve a file on disk for this clip: " + repr(path))

    src_start = item.GetSourceStartFrame()
    src_end = item.GetSourceEndFrame()
    count = int(src_end) - int(src_start) + 1
    name = os.path.splitext(os.path.basename(path))[0]

    log("clip      : " + name)
    log("source    : " + path)
    log("range     : frames " + str(src_start) + "-" + str(src_end) + " (" + str(count) + " frames)")
    log("estimate  : about " + str(int(count * 0.42)) + "s of inference on this machine")

    out_dir = os.path.join(OUTPUT_ROOT, name + "_" + time.strftime("%Y%m%d_%H%M%S"))

    python = os.path.join(ENGINE_DIR, ".venv", "bin", "python")
    if not os.path.exists(python):
        return fail("engine venv missing at " + python + " -- run install.sh first.")

    cmd = [python, "-m", "rotoless.cli",
           "--video", path, "--out", out_dir,
           "--start-frame", str(int(src_start)), "--max-frames", str(count),
           "--pick", "--json"]

    log("a browser tab will open -- click the subject, then press Run.")
    try:
        done = subprocess.run(cmd, cwd=ENGINE_DIR, stdout=subprocess.PIPE, universal_newlines=True)
    except Exception as exc:
        return fail("could not launch the engine: " + repr(exc))

    if done.returncode != 0:
        return fail("engine exited with code " + str(done.returncode) + " (see the console above).")

    try:
        summary = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception:
        return fail("could not parse the engine result: " + repr(done.stdout[-400:]))

    log("wrote " + str(summary.get("frames")) + " mattes to " + str(summary.get("out_dir")))

    first = summary.get("first")
    if not first:
        return fail("engine produced no mattes.")

    media_pool = project.GetMediaPool()
    if media_pool.AddClipMattesToMediaPool(media_item, [first]):
        log("attached as a clip matte. Colour page > node graph > right-click > Add Matte.")
    else:
        log("could not attach automatically -- import this sequence manually:")
        log("  " + str(summary.get("out_dir")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
