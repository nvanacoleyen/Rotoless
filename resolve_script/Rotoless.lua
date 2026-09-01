--[[
Rotoless -- AI subject masking for DaVinci Resolve (free edition).

  Workspace > Scripts > Utility > Rotoless

Reads the clip under the playhead, opens a click picker in the browser, tracks
the subject with SAM 2.1 (MLX) in a separate Python venv, and places the
resulting RGBA cutout on a video track directly above the source clip.

Written in Lua rather than Python on purpose: Resolve only discovers .py
scripts when a Python.framework exists in /Library/Frameworks, which is not
the case on a stock macOS + Homebrew setup. Fusion embeds its own Lua, so this
runs with nothing installed.
--]]

local VERSION     = "0.1.0"
local ENGINE_DIR  = "__ENGINE_DIR__"
local OUTPUT_ROOT = os.getenv("HOME") .. "/Movies/Rotoless"
local SEC_PER_FRAME = 0.42   -- measured on an M4 Air, sam2.1-hiera-small

local function log(msg)
  print("[Rotoless] " .. tostring(msg))
end

-- Single-quote for /bin/sh, escaping any embedded single quotes. Media paths
-- routinely contain spaces, so this is not optional.
local function q(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function basename_noext(path)
  local base = tostring(path):match("([^/]+)$") or "clip"
  return (base:gsub("%.[^.]+$", ""))
end

local function fail(msg)
  log("ERROR: " .. msg)
  return false
end

-- Timeline items do not report which track they sit on, so find it by matching
-- name and timeline position against the contents of each video track.
local function find_track_index(timeline, item)
  local name, startF = item:GetName(), item:GetStart()
  for track = 1, timeline:GetTrackCount("video") do
    local items = timeline:GetItemListInTrack("video", track) or {}
    for _, candidate in ipairs(items) do
      if candidate:GetName() == name and candidate:GetStart() == startF then
        return track
      end
    end
  end
  return nil
end

-- Prefer an existing empty slot above the source clip over stacking a new
-- track on every run; only add a track when every one above is occupied.
local function free_track_above(timeline, srcTrack, startF, endF)
  for track = srcTrack + 1, timeline:GetTrackCount("video") do
    local clear = true
    for _, candidate in ipairs(timeline:GetItemListInTrack("video", track) or {}) do
      if candidate:GetStart() < endF and candidate:GetEnd() > startF then
        clear = false
        break
      end
    end
    if clear then return track end
  end
  if timeline:AddTrack("video") then
    return timeline:GetTrackCount("video")
  end
  return nil
end

local function run()
  local res = resolve or Resolve()
  if res == nil then return fail("no Resolve object; run from Workspace > Scripts.") end

  local pm = res:GetProjectManager()
  local proj = pm and pm:GetCurrentProject()
  if proj == nil then return fail("no project is open.") end

  local tl = proj:GetCurrentTimeline()
  if tl == nil then return fail("no timeline is open.") end

  local item = tl:GetCurrentVideoItem()
  if item == nil then return fail("no clip under the playhead. Park on a clip and retry.") end

  local mpi = item:GetMediaPoolItem()
  if mpi == nil then return fail("this item has no media pool clip (a title or generator?).") end

  local path = mpi:GetClipProperty("File Path")
  if path == nil or path == "" then return fail("could not resolve a file on disk for this clip.") end

  local srcStart = tonumber(item:GetSourceStartFrame())
  local srcEnd   = tonumber(item:GetSourceEndFrame())
  local count    = srcEnd - srcStart + 1
  local name     = basename_noext(path)
  local outDir   = OUTPUT_ROOT .. "/" .. name .. "_" .. os.date("%Y%m%d_%H%M%S")

  log("Rotoless " .. VERSION)
  log("clip     : " .. name)
  log("range    : frames " .. srcStart .. "-" .. srcEnd .. " (" .. count .. " frames)")
  log("estimate : about " .. math.floor(count * SEC_PER_FRAME) .. "s of inference")

  local python = ENGINE_DIR .. "/.venv/bin/python"
  local venvProbe = io.open(python, "r")
  if venvProbe == nil then
    return fail("engine venv missing at " .. python .. " -- run install.sh first.")
  end
  venvProbe:close()

  local cmd = table.concat({
    "cd", q(ENGINE_DIR), "&&", q(python), "-m", "rotoless.cli",
    "--video", q(path), "--out", q(outDir),
    "--start-frame", tostring(srcStart), "--max-frames", tostring(count),
    "--pick", "--json", "2>&1",
  }, " ")

  -- Resolve keeps its menus responsive while a script runs, so launching a
  -- second run by accident is easy -- and two concurrent runs double the GPU
  -- and memory pressure, which on a 16 GB machine is enough to drive the
  -- system into swap. Refuse rather than let that happen.
  local running_probe = io.popen("pgrep -f 'rotoless\\.cli' 2>/dev/null")
  if running_probe then
    local running = (running_probe:read("*a") or ""):gsub("%s+", " "):gsub("^%s*(.-)%s*$", "%1")
    running_probe:close()
    if running ~= "" then
      return fail("a Rotoless run is already in progress (pid " .. running ..
                  "). Let it finish, or quit it, before starting another.")
    end
  end

  log("A browser tab is opening. Click the subject there, then press Run.")
  log("Watch progress in the browser -- it shows a live bar, ETA and log.")

  local pipe = io.popen(cmd)
  if pipe == nil then return fail("could not launch the engine.") end

  -- Read line by line rather than "*a": the Console then fills in as the run
  -- proceeds instead of dumping everything at the end, which is what made an
  -- earlier version look dead and provoked exactly the double-launch above.
  local lines = {}
  for line in pipe:lines() do
    log("  " .. line)
    lines[#lines + 1] = line
  end
  pipe:close()
  local output = table.concat(lines, "\n")

  -- Our own JSON, fixed shape, so a pattern match avoids pulling a JSON
  -- library into Fusion's Lua. Key order is fixed by the engine to suit this.
  local objects = {}
  for oid, count, dir in output:gmatch('"obj_id":%s*(%d+),%s*"frames":%s*(%d+),%s*"dir":%s*"([^"]*)"') do
    objects[#objects + 1] = { id = tonumber(oid), frames = tonumber(count), dir = dir }
  end

  if #objects == 0 then
    return fail("engine produced no cutouts. See the output above.")
  end
  log("tracked " .. #objects .. " object(s)")

  local mediaPool = proj:GetMediaPool()
  local srcTrack  = find_track_index(tl, item)
  local startF, endF = item:GetStart(), item:GetEnd()
  local fps = mpi:GetClipProperty("FPS")
  local base = srcTrack
  local placedAny = false

  for _, obj in ipairs(objects) do
    local label = "object " .. obj.id
    local ok, imported = pcall(function()
      return mediaPool:ImportMedia({
        { FilePath = obj.dir .. "/cutout_%06d.png", StartIndex = 0, EndIndex = obj.frames - 1 },
      })
    end)

    if not (ok and imported and #imported > 0) then
      log(label .. ": import failed" .. (ok and "." or (": " .. tostring(imported))))
      log("  drag this folder in yourself: " .. obj.dir)
    else
      local cutout = imported[1]
      log(label .. ": imported " .. tostring(cutout:GetName()))

      -- Conform to the source frame rate; Resolve otherwise applies its
      -- image-sequence preference and the cutout drifts out of sync.
      if fps ~= nil and fps ~= "" then
        pcall(function() return cutout:SetClipProperty("FPS", fps) end)
      end

      if srcTrack == nil then
        log("  could not determine the source track -- drag it above " .. name .. ".")
      else
        -- Search above the track we last used, so objects stack rather than
        -- competing for the same slot.
        local target = free_track_above(tl, base, startF, endF)
        if target == nil then
          log("  no free track available -- drag it above " .. name .. ".")
        else
          local placed
          ok, placed = pcall(function()
            return mediaPool:AppendToTimeline({
              {
                mediaPoolItem = cutout,
                startFrame    = 0,
                endFrame      = obj.frames - 1,
                trackIndex    = target,
                recordFrame   = startF,
                mediaType     = 1,
              },
            })
          end)
          if ok and placed and #placed > 0 then
            base = target
            placedAny = true
            log("  placed on V" .. target .. " at frame " .. startF)
            local landed = placed[1]:GetStart()
            if landed ~= startF then
              log("  NOTE: landed at " .. landed .. " rather than " .. startF .. " -- nudge it.")
            end
          else
            log("  placing failed" .. (ok and "." or (": " .. tostring(placed))))
            log("  drag it onto a track above " .. name .. ".")
          end
        end
      end
    end
  end

  if placedAny then
    log("Done. Each cutout has an alpha channel and composites over V" .. tostring(srcTrack) .. ".")
  end
  return true
end

run()
