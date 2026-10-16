#!/usr/bin/env bash
# Starts the real Flatpak OBS headless on a throwaway copy of the "Streaming" scene collection
# (no screen capture), runs tools/real_obs_smoke.py against it, then puts everything back.
# Refuses to run if OBS is already open. Only one headless gamescope at a time.
set -uo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
obsd="$HOME/.var/app/com.obsproject.Studio/config/obs-studio"
scenes="$obsd/basic/scenes"

if pgrep -x obs >/dev/null; then echo "OBS is open, not touching it" >&2; exit 2; fi
if pgrep -x gamescope >/dev/null; then echo "another gamescope is running" >&2; exit 3; fi
[ -f "$scenes/Streaming.json" ] || { echo "no Streaming collection" >&2; exit 4; }

bak="$(mktemp -d)"
cp -a "$obsd/user.ini" "$bak/user.ini"
cp -a "$obsd/global.ini" "$bak/global.ini"
had_bak=0; [ -e "$scenes/CamTest.json.bak" ] && had_bak=1
gpid=""
cleanup() {
  [ -n "$gpid" ] && kill -TERM "$gpid" 2>/dev/null
  sleep 2
  pgrep -x obs >/dev/null && pkill -x obs
  rm -f "$scenes/CamTest.json"
  [ $had_bak -eq 0 ] && rm -f "$scenes/CamTest.json.bak"
  cp -a "$bak/user.ini" "$obsd/user.ini"
  cp -a "$bak/global.ini" "$obsd/global.ini"
  find "$obsd/.sentinel" -type f -delete 2>/dev/null
  rm -rf "$bak"
  rm -f "/run/user/$(id -u)/gamescope-0" 2>/dev/null
}
trap cleanup EXIT

jq 'del(.sources[] | select(.id=="pipewire-screen-capture-source")) | (.sources[] | select(.name=="Game") | .settings.items) |= map(select(.name!="Game capture")) | .name="CamTest"' \
  "$scenes/Streaming.json" > "$scenes/CamTest.json" || exit 5

(env -u WAYLAND_DISPLAY timeout -s INT 90 gamescope --backend headless -W 1920 -H 1080 -- \
  flatpak run --user --socket=x11 --nosocket=wayland com.obsproject.Studio \
  --collection CamTest --profile Streaming) >"$bak/obs.log" 2>&1 &
gpid=$!

port="$(jq -r '.server_port // 4455' "$obsd/plugin_config/obs-websocket/config.json")"
up=0
for _ in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then up=1; break; fi
  sleep 1
done
[ $up -eq 1 ] || { echo "OBS websocket never came up"; tail -5 "$bak/obs.log"; exit 6; }
sleep 2
(cd "$here" && PYTHONPATH="$here" python3 tools/real_obs_smoke.py)
rc=$?
exit $rc
