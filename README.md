# Rotoless

AI subject masking for **DaVinci Resolve free edition** — an alternative to
Studio's Magic Mask, using SAM 2.1 running natively on Apple Silicon via MLX.

Click one or more subjects; each is tracked through the clip and comes back as
its own RGBA cutout sequence, placed automatically on its own video track above
the source clip.

## Why this exists

Resolve's Magic Mask is Studio-only. Standalone alternatives
([Sammie-Roto 2](https://github.com/Zarxrax/Sammie-Roto-2)) solve the
segmentation but have no Resolve round-trip, and the one NLE panel that does
([Samosa](https://github.com/tenetmotion/Samosa)) is After Effects only.
The round-trip is the part that didn't exist.

## Requirements

- macOS, Apple Silicon
- DaVinci Resolve (free is fine — no Studio needed)
- `ffmpeg`, `uv`, Python ≥ 3.14 available to uv

You do **not** need a system Python for Resolve's sake — see below.

## Install

```bash
./install.sh
```

Syncs the engine venv and installs the script to Resolve's user Scripts folder.

## Use

1. Park the playhead on a clip in the timeline.
2. **Workspace → Scripts → Utility → Rotoless**
3. A browser tab opens with the first frame. Click a subject (shift-click to
   exclude regions). Press **+ Object** to track another subject — each gets
   its own colour, and the chips let you switch between them. Press
   **Preview mask** to see exactly what SAM selected on that frame, each object
   in its own colour — refine and preview again until it looks right, then
   press **Run**.
   The same tab then shows a **live progress bar, ETA and log** while it
   tracks — watch it there, not in Resolve.
4. Each object is written to
   `~/Movies/Rotoless/<clip>_<timestamp>/object_<n>/`, imported into the
   media pool, and **placed automatically on its own video track above the
   source clip**, aligned to the same timeline position. Nothing to drag.

## Multi-object tracking

Up to six objects (the palette size) in one pass. SAM 2 propagates them
together, which is faster than a pass each and lets `non_overlap_masks` keep
their mattes mutually exclusive — verified at **zero overlapping pixels** across
a two-person test on real footage. Peak memory is unchanged, since the objects
share one set of decoded frames and one inference state.

Chunk handoff is per object: each is re-seeded from its own final mask via
`add_new_mask`, so the exact one-frame overlap holds however many are tracked.

Objects stack onto successive free tracks — object 1 above the source, object 2
above that, and so on.

A caveat worth knowing: SAM is trained on natural video and can lose a
*synthetic* flat-shaded shape after a few frames even when tracked alone. On
real footage it is solid. If an object drops out, add a second include point on
a differently-textured part of it.

## Timeline placement

The cutout is placed for you. Timeline items do not report which track they sit
on, so `find_track_index` matches by name and start frame across every video
track. `free_track_above` then looks upward for a track with nothing
overlapping the clip's range, reusing an existing empty slot rather than
stacking a new track on every run, and only calls `AddTrack` when every track
above is occupied. The sequence is conformed to the source clip's FPS first —
Resolve otherwise applies its image-sequence preference, which silently
desynchronises the cutout from the footage it came from.

Both helpers are unit-tested against a mock Resolve API:

```bash
uv run --with lupa python tests/test_placement.py
```

`install.sh` runs that test plus a real Lua parse before installing, and
refuses to install if either fails. Resolve reports Lua errors only at run
time in its own console, which is a slow way to find a typo.

## Why not AutoSubs' HTTP-server pattern

[AutoSubs](https://github.com/tmoroney/auto-subs) solves the same host problem
and lands on the same two decisions — a Lua entry point under
`Workspace > Scripts`, and heavy lifting in a separate process. It then goes
further: its Lua runs an HTTP server on port 56002 (via LuaJIT FFI and a
vendored `ljsocket`), and the external app drives Resolve through it —
`ExportAudio` returns immediately and is polled with `GetExportProgress`.

That is the right design *for AutoSubs* and the wrong one for Rotoless, and
the reason is the call pattern rather than the technology. AutoSubs' app is
long-lived and makes many Resolve API calls across a session — export audio,
poll it, add subtitles, restore the user's track and marker state — so it needs
a durable channel it can call back into. Rotoless makes **one** round trip:
hand over a clip path and a frame range, get back a set of sequences. A socket
server would mean vendoring ~1300 lines of FFI socket code to replace argv and
a single line of JSON.

What is worth taking from it: Resolve's Lua is **LuaJIT with `ffi` available**,
so native sleeps, sockets and C calls are all on the table if a future feature
needs them. And AutoSubs' bind-failure handling is the same idea as the
single-instance guard above, arrived at from the other direction.

## Architecture

Resolve free has no external scripting, no OpenFX, and no panel API, so:

```
Resolve (Workspace > Scripts)          engine venv (Python 3.14)
  Rotoless.lua ──io.popen───────────▶ rotoless.cli
    reads clip path + source range        picker.py   click prompts
    attaches matte via                    decode.py   ffmpeg → frames
    AddClipMattesToMediaPool              segment.py  chunked SAM 2.1
```

**Why the Resolve half is Lua.** Resolve only enumerates `.py` scripts when a
`Python.framework` exists in `/Library/Frameworks`. A stock macOS + Homebrew
machine has none — Homebrew's framework lives in `/opt/homebrew/Frameworks`,
which Resolve never looks at, and `/usr/bin/python3` is a CLT stub. The result
is silent: the Scripts menu simply shows no Python entries. Fusion embeds its
own Lua, so a `.lua` script runs with nothing installed, which also makes this
plugin distributable to anyone.

The engine stays Python because `mlx-sam` needs 3.14, which is not what Resolve
embeds. The two halves only ever talk over argv and one line of JSON.

**Preview is answered by a dedicated worker thread.** MLX state is
thread-affine — using a model from a thread other than the one that built it
raises `There is no Stream(gpu, 0) in current thread` — and
`ThreadingHTTPServer` hands every request to a fresh thread. `PreviewEngine`
therefore funnels every MLX call onto one long-lived worker, which also
serialises concurrent preview clicks. The model loads on that worker while you
are still choosing points, and `reset_state` leaves the frame's encoded
features cached, so re-previewing re-runs only the prompt head rather than the
image encoder.

The preview model is released before tracking starts and the run loads its own,
costing about 4 s of a 70 s+ run — cheaper than reasoning about which thread
owns which MLX buffer.

**Progress lives in the browser.** The picker server stays alive after
collecting points and streams progress to the page. The browser is the UI;
Resolve is only the launcher.

Resolve does **not** freeze while a Scripts-menu script runs — its menus stay
responsive, which is how it is possible to launch a second run by accident
while the first is going. Two concurrent runs on a 16 GB machine is exactly
the memory pressure that causes trouble, so the script refuses to start when
`pgrep` finds an engine already running. The Console is also filled line by
line (`pipe:lines()` rather than `read("*a")`), so a run in progress looks
alive rather than dead.

**Resolve's environment is not your shell's.** Scripts launched from the
Scripts menu inherit a minimal `PATH` with no `/opt/homebrew/bin`, so bare
`ffmpeg` resolves fine when testing in a terminal and fails under Resolve.
`decode.tool()` resolves external binaries to absolute paths, checking `PATH`,
then Homebrew/MacPorts/system locations, with a `ROTOLESS_FFMPEG` /
`ROTOLESS_FFPROBE` override. Test changes with:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin ./engine/.venv/bin/python -m rotoless.cli ...
```

## The memory constraint (important)

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

## Measured on an M4 MacBook Air (16 GB, 8-core GPU)

| | |
|---|---|
| Speed | ~420 ms/frame (`sam2.1-hiera-small`) |
| 10 s @ 24 fps | ~1.7 min |
| Peak RSS | 0.7 GB at `--chunk-size 8` |
| Tracking accuracy | 0.38 px mean centroid error vs. ground truth |

Per-frame cost is resolution-independent — the model always works at 1024².

Boundary handoffs add roughly 0.2 px of drift each, so prefer larger chunks on
long clips.

## Engine CLI

Usable on its own, without Resolve:

```bash
cd engine
uv run python -m rotoless.cli --video clip.mov --out ./mattes --pick --json
```

| flag | meaning |
|---|---|
| `--point x,y,label` | prompt in source pixels; `1` include, `0` exclude |
| `--box X1 Y1 X2 Y2` | box prompt instead of points |
| `--pick` | open the click picker |
| `--start-frame N` | first source frame (timeline clips use a sub-range) |
| `--max-frames N` | cap frames processed |
| `--chunk-size N` | frames per chunk (default 32) |
| `--rss-limit-gb N` | abort threshold |

## Verified on the free edition

Confirmed live on Resolve 21.0.0.48 free, via a Lua probe:

- Lua scripts enumerate in the Scripts menu
- the full Resolve API is reachable (project, timeline, clip, media pool)
- `io.popen` works, so the engine can be launched
- `GetClipProperty("File Path")` and the source in/out range both resolve

Tracking verified on real 1920x1080 HEVC GoPro footage: a seated person against
water and rock, held cleanly across a head movement and a framing change.

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
alpha channel does the rest. The trade-off is that a cutout composites but
cannot limit a grade; if you need to grade *through* a matte, link one by hand.

Full-resolution frames are decoded in a second pass and kept on disk (about
0.4 GB for a 166-frame 1080p clip), read one at a time, so the cutout matches
the source resolution without raising peak memory.

## Status

Working end to end on real footage: 166-frame GoPro clip tracked cleanly,
mattes written, live progress in the browser. The only manual step is adding
the imported matte to a node in the Colour page.
