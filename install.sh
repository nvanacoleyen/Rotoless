#!/usr/bin/env bash
# Install the Rotoless script into DaVinci Resolve's user Scripts folder.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$ROOT/engine"
DEST="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"

if [ ! -d "$DEST" ]; then
  echo "Resolve Scripts folder not found: $DEST" >&2
  exit 1
fi

echo "==> syncing engine venv"
( cd "$ENGINE_DIR" && uv sync --quiet )

echo "==> syntax-checking Rotoless.lua"
# Resolve reports Lua syntax errors only at run time, inside its own console,
# which is a slow way to find a typo. lupa gives us a real parser up front.
if ! uv run --quiet --with lupa python -c "
import lupa, sys
lupa.LuaRuntime().compile(open('$ROOT/resolve_script/Rotoless.lua').read())
print('  syntax OK')
"; then
  echo "Rotoless.lua failed to parse -- not installing." >&2
  exit 1
fi

echo "==> testing track placement logic"
if ! uv run --quiet --with lupa python "$ROOT/tests/test_placement.py" | tail -1; then
  echo "placement tests failed -- not installing." >&2
  exit 1
fi

echo "==> installing Rotoless.lua"
# Bake the engine path in, so the installed script needs no config lookup.
# '|' as the sed delimiter because the path contains '/'.
sed "s|__ENGINE_DIR__|$ENGINE_DIR|g" "$ROOT/resolve_script/Rotoless.lua" > "$DEST/Rotoless.lua"

# Resolve only lists .py scripts when a Python.framework exists in
# /Library/Frameworks. Leaving a dead .py behind is just confusing.
rm -f "$DEST/Rotoless.py"

# The tool was called MagicMatte before v0.2. An installed copy of the old
# script still appears under Workspace > Scripts and now fails on launch --
# it invokes magic_matte.cli, a module this repo no longer ships.
rm -f "$DEST/MagicMatte.lua" "$DEST/MagicMatte.py"

echo
echo "Installed to: $DEST/Rotoless.lua"
echo "Engine:       $ENGINE_DIR"
echo
echo "In Resolve:   Workspace > Scripts > Utility > Rotoless"
echo "Restart Resolve if this is the first install (the menu is built at startup)."
