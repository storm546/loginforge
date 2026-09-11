#!/usr/bin/env bash
# CLI smoke test: proves `forge.sh login` (the interface Adrian actually uses)
# spawns the box, prints the handoff banner, and finishes on its own.
set -uo pipefail
cd /root/loginforge
source ./lib.sh
require_engine

IDENT="cli-demo"
OUT="$PWD/out/$IDENT"
PROF="$PWD/profiles/$IDENT"

cp .env .env.bak
printf '\nTWOCAPTCHA_API_KEY=test-key\nTWOCAPTCHA_BASE_URL=http://testsite:8081\nTWOCAPTCHA_POLL_INTERVAL=1\nFORGE_HANDOFF_TIMEOUT=240\n' >> .env
trap 'cp .env.bak .env; rm -f .env.bak; pkill -f "forge.sh login" 2>/dev/null; $ENGINE rm -f forge-$IDENT forge-fixture >/dev/null 2>&1' EXIT

rm -rf "$OUT" "$PROF"
$ENGINE rm -f forge-fixture >/dev/null 2>&1 || true
$ENGINE run -d --name forge-fixture --network "$NET" --network-alias testsite "${SECOPT[@]}" --rm \
  -e MOCK_READY_AFTER=1 "$FIXTURE_IMAGE" >/dev/null
sleep 4

bash forge.sh login testsite "$IDENT" > /tmp/forge-cli.log 2>&1 &

for i in $(seq 1 240); do
  [ -f "$OUT/handoff.READY" ] && break
  sleep 1
done
echo "--- handoff banner seen: $([ -f "$OUT/handoff.READY" ] && echo yes || echo no)"
grep -A5 'HANDOFF READY' /tmp/forge-cli.log | head -8

$ENGINE exec "forge-$IDENT" bash -lc '
  export DISPLAY=:99
  WID=$(xdotool search --class chromium | head -1)
  xdotool windowactivate --sync "$WID" 2>/dev/null
  for i in 1 2 3; do xdotool mousemove 700 500 click 1; sleep 1; done
' >/dev/null 2>&1

for i in $(seq 1 240); do
  [ -f "$OUT/result.json" ] && break
  sleep 1
done
sleep 3
echo "--- container state: $($ENGINE inspect -f '{{.State.Running}}' forge-$IDENT 2>/dev/null || echo gone)"
echo "--- result.json:"
cat "$OUT/result.json" 2>/dev/null | head -14
echo "--- CLI tail:"
tail -18 /tmp/forge-cli.log
