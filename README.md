# Rotoless

[![CI](https://github.com/nvanacoleyen/rotoless/actions/workflows/ci.yml/badge.svg)](https://github.com/nvanacoleyen/rotoless/actions/workflows/ci.yml)

AI subject masking for **DaVinci Resolve free edition** — an alternative to
Studio's Magic Mask, using SAM 2.1 running natively on Apple Silicon via MLX.

Click one or more subjects. Each is tracked through the clip and comes back as
its own RGBA cutout sequence, imported and placed automatically on its own
video track above the source clip, frame-aligned. Nothing to drag.

## Why this exists

Resolve's Magic Mask is Studio-only. Standalone alternatives
([Sammie-Roto 2](https://github.com/Zarxrax/Sammie-Roto-2)) solve the
segmentation but have no Resolve round-trip, and the one NLE panel that does
([Samosa](https://github.com/tenetmotion/Samosa)) is After Effects only.
The round-trip is the part that didn't exist.

## Requirements

- macOS on **Apple Silicon** (MLX is Apple-Silicon only)
- DaVinci Resolve — free is fine, no Studio licence needed
- [`ffmpeg`](https://ffmpeg.org/) (`brew install ffmpeg`)
- [`uv`](https://docs.astral.sh/uv/), with Python ≥ 3.14 available to it

You do **not** need a system Python for Resolve's sake — see
[Why the Resolve half is Lua](#why-the-resolve-half-is-lua).

## Install

```bash
git clone https://github.com/nvanacoleyen/rotoless.git
```

```bash
cd rotoless && ./install.sh
```

This syncs the engine venv and installs the script into Resolve's user Scripts
folder. Restart Resolve after a first install — the Scripts menu is built at
startup.

The installer refuses to install if the Lua fails to parse or the placement
tests fail, because Resolve reports Lua errors only at run time in its own
console, which is a slow way to find a typo.

> Moving the repo after installing breaks the link: the engine path is baked
> into the installed script. Re-run `./install.sh` from the new location.

## Use

1. Park the playhead on a clip in the timeline.
2. **Workspace → Scripts → Utility → Rotoless**
3. A browser tab opens with the first frame. Click a subject; shift-click to
   exclude regions. Press **+ Object** to track another subject — each gets its
   own colour, and the chips switch between them. Press **Preview mask** to see
   exactly what SAM selected, refine, and preview again until it looks right.
   Then press **Run**.
4. The same tab shows a live progress bar, ETA and log while it tracks. Watch
   it there, not in Resolve.

Each object is written to
`~/Movies/Rotoless/<clip>_<timestamp>/object_<n>/`, imported into the media
pool, and placed on its own video track above the source clip at the same
timeline position.

## Multi-object tracking

Up to six objects (the palette size) in one pass. SAM 2 propagates them
together, which is faster than a pass each and lets `non_overlap_masks` keep
their mattes mutually exclusive — verified at **zero overlapping pixels**
across a two-person test on real footage. Peak memory is unchanged, since the
objects share one set of decoded frames and one inference state.

Chunk handoff is per object: each is re-seeded from its own final mask via
`add_new_mask`, so the exact one-frame overlap holds however many are tracked.

Objects stack onto successive free tracks — object 1 above the source, object 2
above that, and so on.

## Known limitations

- **Apple Silicon and macOS only.** The engine is MLX-based; there is no CUDA
  or Intel path.
- **A cutout composites, but cannot limit a grade.** If you need to grade
  *through* a matte, link one by hand — see
  [Why a cutout and not an external matte](#why-a-cutout-and-not-an-external-matte).
- **Synthetic flat-shaded shapes can drop out.** SAM is trained on natural
  video and can lose a flat synthetic shape after a few frames even when
  tracked alone. On real footage it is solid. If an object drops out, add a
  second include point on a differently-textured part of it.
- **One run at a time.** The script refuses to start while another run is in
  progress; two concurrent runs are enough memory pressure to cause trouble on
  a 16 GB machine.
- **Boundary drift.** Each chunk handoff adds roughly 0.2 px, so prefer larger
  chunks on long clips.

## Performance

Measured on an M4 MacBook Air (16 GB, 8-core GPU, fanless):

| | |
|---|---|
| Speed | ~420 ms/frame (`sam2.1-hiera-small`) |
| 10 s @ 24 fps | ~1.7 min |
| Peak RSS | 0.7 GB at `--chunk-size 8` |
| Tracking accuracy | 0.38 px mean centroid error vs. ground truth |

Per-frame cost is resolution-independent — the model always works at 1024².

## Engine CLI

The engine is usable on its own, without Resolve:

```bash
cd engine && uv run python -m rotoless.cli --video clip.mov --out ./mattes --pick --json
```

| flag | meaning |
|---|---|
| `--point x,y,label[,obj_id]` | prompt in source pixels; `1` include, `0` exclude |
| `--box X1 Y1 X2 Y2` | box prompt instead of points |
| `--pick` | open the click picker |
| `--start-frame N` | first source frame (timeline clips use a sub-range) |
| `--max-frames N` | cap frames processed |
| `--chunk-size N` | frames per chunk (default 32) |
| `--max-edge N` | decode long edge (default 1024) |
| `--rss-limit-gb N` | abort threshold |
| `--json` | emit the machine-readable summary |

---

# How it works

Resolve free has no external scripting, no OpenFX, and no panel API, so:

```
Resolve (Workspace > Scripts)          engine venv (Python 3.14)
  Rotoless.lua ──io.popen───────────▶  rotoless.cli
    reads clip path + source range       picker.py   click prompts + progress
    imports the returned sequences       decode.py   ffmpeg → frames
    places each on a free track          segment.py  chunked SAM 2.1
                 ◀──── one line of JSON ─ summary.py  the wire contract
```

The two halves only ever talk over argv and one line of JSON.

## Why the Resolve half is Lua

Resolve only enumerates `.py` scripts when a `Python.framework` exists in
`/Library/Frameworks`. A stock macOS + Homebrew machine has none — Homebrew's
framework lives in `/opt/homebrew/Frameworks`, which Resolve never looks at,
and `/usr/bin/python3` is a Command Line Tools stub. The failure is silent: the
Scripts menu simply shows no Python entries.

Fusion embeds its own Lua, so a `.lua` script runs with nothing installed,
which also makes this distributable to anyone. The engine stays Python because
`mlx-sam` needs 3.14, which is not what Resolve embeds. The split is forced,
not stylistic.

## The memory constraint

`mlx_sam.init_state()` decodes an **entire clip** into full-resolution PIL
images, then concatenates every preprocessed 1024×1024 float32 frame — both
copies resident at once. Handing it a whole clip kernel-panicked a 16 GB M4
Air during development.

MLX's `set_memory_limit()` does not help; the blowup is in numpy/PIL *host*
memory before MLX is involved. So `segment.py` never passes a whole clip:

- frames are decoded to a 1024 long edge, so full-res frames never exist
- tracking runs in chunks (default 32 frames), each torn down before the next
- chunks overlap by exactly one frame, and the final mask of chunk N seeds
  frame 0 of chunk N+1 via `add_new_mask` — the overlap frame is literally the
  same frame, so the handoff is exact, not an approximation
- an RSS guard aborts the process before the system starts swapping

Frames are decoded **once** for the whole range, then each chunk is symlinked
out of that set. Decoding per chunk would be quadratic: ffmpeg's `select`
filter has no fast seek, so every chunk would re-scan the file from frame 0.

Peak memory is a function of `chunk_size`, not clip length.

## Preview runs on a dedicated worker thread

MLX state is thread-affine — using a model from a thread other than the one
that built it raises `There is no Stream(gpu, 0) in current thread` — and
`ThreadingHTTPServer` hands every request to a fresh thread. `PreviewEngine`
therefore funnels every MLX call onto one long-lived worker, which also
serialises concurrent preview clicks.

The model loads on that worker while you are still choosing points, and
`reset_state` leaves the frame's encoded features cached, so re-previewing
re-runs only the prompt head rather than the image encoder.

The preview model is released before tracking starts and the run loads its own,
costing about 4 s of a 70 s+ run — cheaper than reasoning about which thread
owns which MLX buffer.

## Progress lives in the browser

The picker server stays alive after collecting points and streams progress to
the page. The browser is the UI; Resolve is only the launcher.

Resolve does **not** freeze while a Scripts-menu script runs — its menus stay
responsive, which is how it is possible to launch a second run by accident
while the first is going. The script therefore refuses to start when `pgrep`
finds an engine already running. The Console is also filled line by line
(`pipe:lines()` rather than `read("*a")`), so a run in progress looks alive
rather than dead.

## Resolve's environment is not your shell's

Scripts launched from the Scripts menu inherit a minimal `PATH` with no
`/opt/homebrew/bin`, so bare `ffmpeg` resolves fine when testing in a terminal
and fails under Resolve. `decode.tool()` resolves external binaries to absolute
paths, checking `PATH`, then Homebrew/MacPorts/system locations, with a
`ROTOLESS_FFMPEG` / `ROTOLESS_FFPROBE` override.

Reproduce Resolve's environment when testing:

```bash
cd engine && env -i HOME="$HOME" PATH=/usr/bin:/bin ./.venv/bin/python -m rotoless.cli --help
```

## Timeline placement

Timeline items do not report which track they sit on, so `find_track_index`
matches by name and start frame across every video track. `free_track_above`
then looks upward for a track with nothing overlapping the clip's range,
reusing an existing empty slot rather than stacking a new track on every run,
and only calls `AddTrack` when every track above is occupied.

The sequence is conformed to the source clip's FPS first — Resolve otherwise
applies its image-sequence preference, which silently desynchronises the cutout
from the footage it came from.

## Why a cutout and not an external matte

An external matte has to be *linked* to its source clip before the Color page
will offer it under "Add Matte". The API call for that,
`AddClipMattesToMediaPool`, is documented for Python but **does not exist on
the Lua `MediaPool` object** in Resolve 21 free — it raises
`attempt to call method 'AddClipMattesToMediaPool' (a nil value)`. Linking by
hand means selecting the source clip, finding the sequence in Media Storage,
and using "Add to Media Pool as a Matte" — easy to get wrong, and impossible
to automate from this edition.

An RGBA cutout needs none of that. A plain `ImportMedia` is enough, and the
alpha channel does the rest.

Full-resolution frames are decoded in a second pass and kept on disk (about
0.4 GB for a 166-frame 1080p clip), read one at a time, so the cutout matches
the source resolution without raising peak memory.

## Why not AutoSubs' HTTP-server pattern

[AutoSubs](https://github.com/tmoroney/auto-subs) solves the same host problem
and lands on the same two decisions — a Lua entry point under
`Workspace > Scripts`, and heavy lifting in a separate process. It then goes
further: its Lua runs an HTTP server on port 56002 (via LuaJIT FFI and a
vendored `ljsocket`), and the external app drives Resolve through it.

That is the right design *for AutoSubs* and the wrong one here, and the reason
is the call pattern rather than the technology. AutoSubs' app is long-lived and
makes many Resolve API calls across a session, so it needs a durable channel it
can call back into. Rotoless makes **one** round trip: hand over a clip path
and a frame range, get back a set of sequences. A socket server would mean
vendoring ~1300 lines of FFI socket code to replace argv and a single line of
JSON.

What is worth taking from it: Resolve's Lua is **LuaJIT with `ffi` available**,
so native sleeps, sockets and C calls are all on the table if a future feature
needs them.

---

# Development

```bash
uv run --with lupa python tests/test_placement.py
```

The suite needs no engine venv and no Apple Silicon — it runs in CI on Linux.
It covers:

- a real Lua parse of the shipped script (via `lupa`)
- the track-selection helpers, **lifted verbatim out of `Rotoless.lua`** at
  test time, so they cannot drift from the code that ships
- the engine↔Lua wire contract: `rotoless.summary.build_summary`'s real output
  is fed through the shipped Lua pattern, so reordering those JSON keys fails
  here rather than silently in Resolve

`rotoless.summary` is deliberately free of numpy/cv2/mlx imports so that last
check costs nothing to run.

## Verified on the free edition

Confirmed live on Resolve 21.0.0.48 free:

- Lua scripts enumerate in the Scripts menu
- the full Resolve API is reachable (project, timeline, clip, media pool)
- `io.popen` works, so the engine can be launched
- `GetClipProperty("File Path")` and the source in/out range both resolve

Tracking verified on real 1920×1080 HEVC GoPro footage: a seated person against
water and rock, held cleanly across a head movement and a framing change.

## License

[MIT](LICENSE) © 2026 Neil Van Acoleyen
