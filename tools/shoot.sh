#!/usr/bin/env bash
# Headless screenshot of one Lark page: tools/shoot.sh <view> [out.png] [WxH]
# Runs against tests/fake_obs.py with a throwaway config dir, so it never touches
# real OBS or ~/.config/lark. Set LARK_SHOT_REAL_OBS=1 to use your own config instead,
# or LARK_SHOT_EXTRA='{"key": value}' to add config keys to the throwaway one.
set -uo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
view="${1:?usage: shoot.sh <view> [out.png] [WxH]}"
out="${2:-$here/shots/$view.png}"
size="${3:-${LARK_SHOT_SIZE:-1600x1000}}"
wait_s="${LARK_SHOT_WAIT:-5}"
mkdir -p "$(dirname "$out")"
tmp="$(mktemp -d)"
pids=()
cleanup() {
  for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null; done
  wait 2>/dev/null
  rm -rf "$tmp"
}
trap cleanup EXIT

for _ in $(seq 1 90); do
  pgrep -x gamescope >/dev/null 2>&1 || break
  sleep 2
done
if pgrep -x gamescope >/dev/null 2>&1; then
  echo "shoot.sh: another gamescope kept running, only one headless at a time" >&2
  exit 3
fi

if [ -z "${LARK_SHOT_REAL_OBS:-}" ]; then
  fport=$((20000 + RANDOM % 10000))
  oport=$((30000 + RANDOM % 10000))
  python3 -c "import json,os,sys; json.dump({**json.loads(os.environ.get('LARK_SHOT_EXTRA') or '{}'), 'obs_port': int(sys.argv[1]), 'obs_password_source': 'custom', 'obs_password': 'pw', 'overlay_port': int(sys.argv[2])}, open(sys.argv[3] + '/config.json', 'w'))" "$fport" "$oport" "$tmp"
  (cd "$here" && exec python3 -m tests.fake_obs --port "$fport" --password pw) >"$tmp/fake.log" 2>&1 &
  pids+=($!)
  export LARK_CONFIG_DIR="$tmp"
  sleep 1
fi

(cd "$here" && exec env -u WAYLAND_DISPLAY QT_QPA_PLATFORM=xcb timeout -s INT 60 \
  gamescope --backend headless -W "${size%x*}" -H "${size#*x}" -- \
  bash -c 'exec python3 -m lark --view "$1" --size "$2" 2>"$3"' _ "$view" "$size" "$tmp/lark.err") >"$tmp/gs.log" 2>&1 &
gpid=$!
pids+=($gpid)

disp=""
for _ in $(seq 1 100); do
  disp="$(sed -n "s/.*Running compositor on wayland display '\(gamescope-[0-9]*\)'.*/\1/p" "$tmp/gs.log" | head -1)"
  [ -n "$disp" ] && [ -S "/run/user/$(id -u)/$disp" ] && break
  sleep 0.2
done
if [ -z "$disp" ]; then echo "shoot.sh: gamescope never came up" >&2; tail -5 "$tmp/gs.log" >&2; exit 4; fi
sleep "$wait_s"
rc=1
for _ in 1 2 3 4; do
  XDG_RUNTIME_DIR=/run/user/$(id -u) GAMESCOPE_WAYLAND_DISPLAY="$disp" gamescopectl screenshot "$out" >/dev/null 2>"$tmp/ctl.err"
  rc=$?
  [ $rc -eq 0 ] && [ -s "$out" ] && break
  sleep 2
done
[ $rc -ne 0 ] && cat "$tmp/ctl.err" >&2
sleep 1
kill -TERM "$gpid" 2>/dev/null
wait "$gpid" 2>/dev/null
# Only the app's own stderr goes out on stderr, so check.sh can scan it.
grep -v '^$' "$tmp/lark.err" >&2 || true
if [ $rc -ne 0 ] || [ ! -s "$out" ]; then echo "shoot.sh: screenshot failed (rc=$rc)" >&2; exit 5; fi
echo "$out"
