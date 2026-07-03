#!/usr/bin/env bash
# Record the README demo GIF of a bmad-loop run / TUI session.
#
# Two routes, pick whichever you have:
#
#   1. VHS (charmbracelet/vhs) — DETERMINISTIC, scripted, repeatable. Best for a
#      clean README hero. Drives a real terminal from the .tape script below.
#         install: https://github.com/charmbracelet/vhs   (`go install` or your pkg mgr)
#         run:     scripts/record-demo.sh vhs
#
#   2. asciinema + agg — records a REAL interactive run you drive by hand, then
#      converts the cast to a GIF. Best when you want a genuine live run.
#         install: https://docs.asciinema.org  +  https://github.com/asciinema/agg
#         run:     scripts/record-demo.sh cast     # records; Ctrl-D / `exit` to stop
#                  scripts/record-demo.sh gif      # converts the cast to docs/images/demo.gif
#
# Output: docs/images/demo.gif  (uncomment the embed block in README.md to use it).

set -euo pipefail
cd "$(dirname "$0")/.."

OUT_GIF="docs/images/demo.gif"
CAST="docs/images/demo.cast"
TAPE="scripts/demo.tape"

mode="${1:-help}"

case "$mode" in
vhs)
  command -v vhs >/dev/null || { echo "vhs not found — see https://github.com/charmbracelet/vhs"; exit 1; }
  if [ ! -f "$TAPE" ]; then
    cat >"$TAPE" <<'TAPE'
# VHS tape — edit timings/commands to taste, then: scripts/record-demo.sh vhs
# docs: https://github.com/charmbracelet/vhs
Output docs/images/demo.gif
Set FontSize 16
Set Width 1400
Set Height 850
Set Padding 12
Set Theme "Catppuccin Mocha"

Type "bmad-loop tui"   Sleep 500ms   Enter
Sleep 3s
# 'r' opens the start-run modal; drive a short --max-stories 1 run
Type "r"               Sleep 1s
# fill the modal fields here (Tab between them), then submit:
# Type "1"  Tab  Type "1-2-account-mgmt"  Tab  Type "1"  Enter
Sleep 20s
# 'a' to attach to the live agent session, watch the journal tail
Type "a"               Sleep 8s
# detach + quit
Type "q"               Sleep 1s
TAPE
    echo "Wrote a starter tape to $TAPE — edit the modal/timings, then re-run: scripts/record-demo.sh vhs"
    exit 0
  fi
  vhs "$TAPE"
  echo "Wrote $OUT_GIF"
  ;;

cast)
  command -v asciinema >/dev/null || { echo "asciinema not found — see https://docs.asciinema.org"; exit 1; }
  echo "Recording to $CAST — drive a short run (e.g. 'bmad-loop run --max-stories 1' or 'bmad-loop tui'),"
  echo "then press Ctrl-D or type 'exit' to stop."
  asciinema rec --overwrite "$CAST"
  echo "Recorded $CAST — now: scripts/record-demo.sh gif"
  ;;

gif)
  command -v agg >/dev/null || { echo "agg not found — see https://github.com/asciinema/agg"; exit 1; }
  [ -f "$CAST" ] || { echo "no $CAST yet — run: scripts/record-demo.sh cast"; exit 1; }
  agg --theme monokai --font-size 16 "$CAST" "$OUT_GIF"
  echo "Wrote $OUT_GIF"
  ;;

*)
  sed -n '2,30p' "$0"
  ;;
esac
