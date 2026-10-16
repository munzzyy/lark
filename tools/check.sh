#!/usr/bin/env bash
# Compile everything, run the tests, screenshot every page headless and fail on any
# Traceback or error the app prints. Screenshots land in shots/.
set -uo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$here"
fail=0

echo "== py_compile"
if ! python3 - <<'PY'
import pathlib, sys
bad = 0
for p in sorted(pathlib.Path(".").rglob("*.py")):
    if ".git" in p.parts:
        continue
    try:
        compile(p.read_text(), str(p), "exec")
    except SyntaxError as e:
        print(e, file=sys.stderr)
        bad += 1
print("compiled ok" if not bad else f"{bad} file(s) failed")
sys.exit(1 if bad else 0)
PY
then fail=1; fi

echo "== unittest"
export QT_QPA_PLATFORM=offscreen
ut="$(python3 -B -m unittest discover -s tests -t . 2>&1)"; rc=$?
printf '%s\n' "$ut" | tail -n 6
if [ $rc -ne 0 ]; then fail=1; fi
if printf '%s\n' "$ut" | grep -Ei 'warning|error|traceback' | grep -vi 'locale'; then
  echo "FAIL unittest printed warnings or errors, full output follows"; printf '%s\n' "$ut"; fail=1
fi

if [ "${1:-}" = "--quick" ]; then
  [ $fail -eq 0 ] && echo "check (quick): all good" || echo "check (quick): FAILED"
  exit $fail
fi

echo "== screenshots"
unset QT_QPA_PLATFORM
views=$(python3 -c "from lark.app import VIEWS; print(*VIEWS)")
for v in $views; do
  err="$(mktemp)"
  if out="$(./tools/shoot.sh "$v" 2>"$err")"; then
    if grep -Ei 'warning|traceback|error|critical|exception' "$err" | grep -vi 'locale' >/dev/null; then
      echo "FAIL $v: errors in stderr"; grep -Ei -B2 -A8 'warning|traceback|error|critical|exception' "$err" | grep -vi locale | head -30
      fail=1
    else
      echo "ok   $v -> $out"
    fi
  else
    echo "FAIL $v: shoot.sh exited $?"; cat "$err"; fail=1
  fi
  rm -f "$err"
done

if [ $fail -eq 0 ]; then echo "check: all good"; else echo "check: FAILED"; fi
exit $fail
