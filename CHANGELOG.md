# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-01

First public release. Working end to end on real footage.

### Added

- Subject masking for DaVinci Resolve **free** edition, driven from
  `Workspace > Scripts > Utility > Rotoless`.
- SAM 2.1 segmentation via `mlx-sam`, running natively on Apple Silicon with
  no PyTorch dependency.
- Browser-based click picker: include/exclude points, per-object colours, and
  a **Preview mask** round trip that re-runs only the prompt head.
- Multi-object tracking, up to six objects in a single propagation pass with
  `non_overlap_masks` keeping the mattes mutually exclusive.
- Live progress bar, ETA and log streamed to the picker page during a run.
- Automatic timeline placement: each object's cutout is imported, conformed to
  the source FPS, and placed on a free video track above the source clip at
  the matching timeline position.
- Chunked tracking with exact one-frame overlap handoff, so peak memory is a
  function of chunk size rather than clip length.
- RSS guard that aborts before the machine starts swapping.
- `install.sh`, gated on a real Lua parse and the placement test suite.
- Test suite covering the Lua parse, the track-selection helpers (extracted
  verbatim from the shipped script), and the engine↔Lua JSON wire contract.
- CI running the full suite on Linux.

### Notes

- The project was called **MagicMatte** during development. It was renamed
  before release: the old name sat one letter from Blackmagic's own "Magic
  Mask", the Studio feature this replaces.
- `install.sh` removes any `MagicMatte.lua` / `MagicMatte.py` left in Resolve's
  Scripts folder by a pre-release install, since those now fail on launch.

[0.1.0]: https://github.com/nvanacoleyen/Rotoless/releases/tag/v0.1.0
