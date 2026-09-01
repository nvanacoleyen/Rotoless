"""Exercise Rotoless.lua's track-selection helpers against a mock Resolve API.

The helpers are lifted verbatim out of the shipped script, so this tests the
real code rather than a copy that can drift. Run: uv run --with lupa python tests/test_placement.py
"""
import sys
from pathlib import Path

import lupa

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_script" / "Rotoless.lua"

MOCKS = """
local function mkitem(name, s, e)
  return { GetName=function(self) return name end,
           GetStart=function(self) return s end,
           GetEnd=function(self) return e end }
end
local function mktl(tracks)
  local t = { _t = tracks, added = 0 }
  function t:GetTrackCount(_) return #self._t end
  function t:GetItemListInTrack(_, i) return self._t[i] end
  function t:AddTrack(_) self._t[#self._t+1] = {}; self.added = self.added + 1; return true end
  return t
end
"""

CASES = """
local out, failures = {}, 0
local function check(label, got, want)
  local ok = (got == want)
  if not ok then failures = failures + 1 end
  out[#out+1] = string.format("%-46s got=%-6s want=%-6s %s",
    label, tostring(got), tostring(want), ok and "PASS" or "FAIL")
end

local target = mkitem("clip.mov", 100, 200)

local tl = mktl({ { mkitem("other", 0, 50) }, { target }, {}, {} })
check("find_track_index locates the clip on V2", find_track_index(tl, target), 2)

tl = mktl({ {}, { target }, {}, {} })
check("empty V3 is reused", free_track_above(tl, 2, 100, 200), 3)
check("  ...without adding a track", tl.added, 0)

tl = mktl({ {}, { target }, { mkitem("busy", 150, 250) }, {} })
check("overlapping V3 skipped for V4", free_track_above(tl, 2, 100, 200), 4)

tl = mktl({ {}, { target }, { mkitem("elsewhere", 400, 500) } })
check("non-overlapping clip on V3 allows V3", free_track_above(tl, 2, 100, 200), 3)

tl = mktl({ {}, { target }, { mkitem("abuts", 0, 100) } })
check("abutting clip does not block", free_track_above(tl, 2, 100, 200), 3)

tl = mktl({ {}, { target }, { mkitem("busy", 100, 200) } })
check("all busy -> new track", free_track_above(tl, 2, 100, 200), 4)
check("  ...exactly one added", tl.added, 1)

-- Multi-object: each object searches above the track the previous one took,
-- so they stack instead of competing for the same slot.
tl = mktl({ {}, { target }, {}, {}, {} })
local base = 2
local picks = {}
for _ = 1, 3 do
  local t = free_track_above(tl, base, 100, 200)
  picks[#picks+1] = t
  base = t
end
check("three objects stack onto V3/V4/V5", table.concat(picks, ","), "3,4,5")
check("  ...no tracks needed adding", tl.added, 0)

return table.concat(out, "\\n") .. "\\n__FAILURES__" .. failures
"""


JSON_CASE = """
local sample = [==[
{"objects": [{"obj_id": 1, "frames": 166, "dir": "/tmp/out/object_1", "first": "/tmp/out/object_1/cutout_000000.png"}, {"obj_id": 2, "frames": 166, "dir": "/tmp/out/object_2", "first": "x"}], "frames": 166}
]==]
local found = {}
for oid, count, dir in sample:gmatch(__PATTERN__) do
  found[#found+1] = oid .. ":" .. count .. ":" .. dir
end
return table.concat(found, "|")
"""


def _check_json_pattern(src: str) -> bool:
    """The Lua side pattern-matches the engine's JSON; keep them in step."""
    line = next(l for l in src.split("\n") if "obj_id" in l and "gmatch" in l)
    pattern = line[line.index("gmatch(") + len("gmatch("):line.rindex(")")]
    got = lupa.LuaRuntime().execute(JSON_CASE.replace("__PATTERN__", pattern))
    want = "1:166:/tmp/out/object_1|2:166:/tmp/out/object_2"
    ok = got == want
    print(f'{"engine JSON parses into objects":<46} '
          f'{"PASS" if ok else "FAIL got=" + repr(got)}')
    return ok


def main() -> int:
    src = SCRIPT.read_text()
    lupa.LuaRuntime().compile(src)          # syntax gate

    helpers = src[src.index("local function find_track_index"):src.index("local function run()")]
    helpers = helpers.replace("local function find_track_index", "function find_track_index")
    helpers = helpers.replace("local function free_track_above", "function free_track_above")

    report = lupa.LuaRuntime().execute(MOCKS + helpers + CASES)
    body, _, failures = report.partition("__FAILURES__")
    print(body)
    if not _check_json_pattern(src):
        failures = str(int(failures) + 1)
    if int(failures):
        print(f"{failures} FAILED")
        return 1
    print("all placement tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
