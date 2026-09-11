#!/usr/bin/env bash
# loginforge E2E suite — real containers, real browser, real LLM, real 2captcha wire format.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "$DIR/lib.sh"

PASS=0; FAIL=0
ok()   { printf '\033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
info() { printf '\033[36m---- %s\033[0m\n' "$*"; }
jp()   { python3 -c "import json;d=json.load(open('$1'));print($2)" 2>/dev/null; }
running() { [ "$($ENGINE inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo gone)" = "true" ]; }
trace_has() { grep -q "$2" "$DIR/out/$1/run.jsonl" 2>/dev/null; }

cleanup() {
  for c in forge-e2e forge-e2e-llm forge-e2e-human forge-fixture; do
    $ENGINE rm -f "$c" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

# --------------------------------------------------------------------- setup
require_engine; info "engine: $ENGINE"
$ENGINE build -q -t "$IMAGE" "$DIR" >/dev/null || { bad "agent image build"; exit 1; }
$ENGINE build -q -t "$FIXTURE_IMAGE" -f "$DIR/testsite/Dockerfile" "$DIR/testsite" >/dev/null \
  || { bad "fixture image build"; exit 1; }
ok "images built"

$ENGINE network inspect "$NET" >/dev/null 2>&1 || $ENGINE network create "$NET" >/dev/null
$ENGINE rm -f forge-fixture >/dev/null 2>&1 || true
$ENGINE run -d --name forge-fixture --network "$NET" --network-alias testsite "${SECOPT[@]}" --rm \
  -e MOCK_READY_AFTER=1 "$FIXTURE_IMAGE" >/dev/null
sleep 4
FIXTURE_IP="$($ENGINE inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' forge-fixture)"
curl -sf "http://$FIXTURE_IP:5000/" >/dev/null && ok "fixture site up at $FIXTURE_IP:5000" || bad "fixture site not reachable"
curl -sf "http://$FIXTURE_IP:8081/res.php?action=getbalance&key=x&json=1" | grep -q '"status": *1' \
  && ok "mock 2captcha answering" || bad "mock 2captcha dead"

# ------------------------------------------------------------------ helpers
start_agent() {  # ident target mode extra-env...
  local ident="$1"
  local target="$2"
  local mode="$3"
  shift 3
  local out="$DIR/out/$ident" prof="$DIR/profiles/$ident"
  mkdir -p "$out" "$prof"
  rm -f "$out/handoff.READY" "$out/handoff.DONE" "$out/novnc_url" "$out/resume" "$out/result.json"
  $ENGINE rm -f "forge-$ident" >/dev/null 2>&1 || true
  $ENGINE run -d --name "forge-$ident" --rm --network "$NET" \
    "${SECOPT[@]}" --shm-size=1g \
    -p "${NOVNC_PORT}:6080" -p "$((NOVNC_PORT+1)):5900" \
    -v "$prof":/profile -v "$out":/out \
    --env-file "$DIR/.env" \
    -e TWOCAPTCHA_API_KEY=test-key \
    -e TWOCAPTCHA_BASE_URL="http://$FIXTURE_IP:8081" \
    -e TWOCAPTCHA_POLL_INTERVAL=1 \
    -e FORGE_HANDOFF_TIMEOUT=120 \
    -e PUBLIC_URL="http://127.0.0.1:${NOVNC_PORT}" \
    "$@" \
    "$IMAGE" --mode "$mode" --target "$target" --identity "$ident" >/dev/null
}

wait_handoff() {
  local ident="$1"
  local t="${2:-180}"
  local out="$DIR/out/$ident"
  for _ in $(seq 1 "$t"); do
    [ -f "$out/handoff.READY" ] && return 0
    running "forge-$ident" || return 1
    sleep 1
  done
  return 1
}

human_click() {  # simulate the human solver over the live X display
  $ENGINE exec "forge-$1" bash -lc '
    export DISPLAY=:99
    WID=$(xdotool search --class chromium | head -1)
    xdotool windowactivate --sync "$WID" 2>/dev/null
    for i in 1 2 3; do xdotool mousemove 700 500 click 1; sleep 1; done
  ' >/dev/null 2>&1
}

wait_result() {
  local ident="$1"
  local t="${2:-240}"
  local out="$DIR/out/$ident"
  for _ in $(seq 1 "$t"); do
    [ -f "$out/result.json" ] && return 0
    sleep 1
  done
  return 1
}

check_vanished() {  # ident label
  sleep 3
  if running "forge-$1"; then bad "$2: container still running"; else ok "$2: container removed itself"; fi
}

# ================================================================== PHASE 0
info "PHASE 0 — LLM agent, fully autonomous: it must clear both challenges itself"
rm -rf "$DIR/out/e2e-llm" "$DIR/profiles/e2e-llm"
start_agent e2e-llm testsite first-login -e FORGE_DETERMINISTIC=0 -e FORGE_MAX_STEPS=30
ok "container started (llm run)"
if wait_result e2e-llm 420; then
  S="$(jp "$DIR/out/e2e-llm/result.json" "d['status']")"
  N="$(jp "$DIR/out/e2e-llm/result.json" "d['steps']")"
  [ "$S" = "success" ] && ok "LLM cleared the login + challenges alone in $N steps" || bad "LLM run status=$S"
  trace_has e2e-llm 'funcaptcha' && ok "Arkose/FunCaptcha handled" || bad "no funcaptcha in trace"
  trace_has e2e-llm 'mock-token' && ok "2captcha token used at least once" || bad "no solver token in trace"
  [ -f "$DIR/out/e2e-llm/handoff.READY" ] && bad "it escalated to a human anyway" \
                                          || ok "no human handoff needed"
else
  bad "LLM run produced no result.json"
fi
check_vanished e2e-llm "llm run"

# ================================================================== PHASE 1
info "PHASE 1 — deterministic first login, zero human: Arkose + reCAPTCHA via 2captcha"
rm -rf "$DIR/out/e2e" "$DIR/profiles/e2e"
start_agent e2e testsite first-login
ok "container started (identity e2e)"
for _ in $(seq 1 90); do [ -f "$DIR/out/e2e/novnc_url" ] && break; sleep 1; done
[ -s "$DIR/out/e2e/novnc_url" ] && ok "noVNC view available: $(cat "$DIR/out/e2e/novnc_url")" || bad "no noVNC URL"

if wait_result e2e 300; then
  S="$(jp "$DIR/out/e2e/result.json" "d['status']")"
  C="$(jp "$DIR/out/e2e/result.json" "d['cookies']")"
  [ "$S" = "success" ] && ok "run status=success with no human input" || bad "run status=$S"
  [ "$C" -gt 0 ] && ok "session cookies captured: $C" || bad "no cookies captured"
else
  bad "no result.json after 300s"
fi

trace_has e2e '"challenge": "funcaptcha"' && ok "guard solved Arkose/FunCaptcha via 2captcha" || bad "funcaptcha not solved"
trace_has e2e '"challenge": "recaptcha_v2"' && ok "guard solved the reCAPTCHA" || bad "recaptcha not solved"
[ "$(grep -c '"action": "solve"' "$DIR/out/e2e/run.jsonl" 2>/dev/null)" -ge 2 ] \
  && ok "two solver rounds recorded" || bad "expected two solver rounds"
[ -f "$DIR/out/e2e/handoff.READY" ] && bad "handoff triggered on an autonomous target" \
                                    || ok "no handoff on the autonomous target"
check_vanished e2e "first-login run"
[ -f "$DIR/out/e2e/session/storage_state.json" ] && ok "session handed back (out/e2e/session/storage_state.json)" || bad "storage_state.json missing"
PFILES="$(find "$DIR/profiles/e2e" -type f 2>/dev/null | wc -l)"
[ "$PFILES" -gt 20 ] && ok "persistent profile kept on host ($PFILES files)" || bad "profile looks empty ($PFILES files)"

# ================================================================== PHASE 2
info "PHASE 2 — challenge run on the warmed session (no human, no re-login)"
start_agent e2e testsite challenge -e FORGE_MAX_STEPS=12
if wait_result e2e 240; then
  S="$(jp "$DIR/out/e2e/result.json" "d['status']")"
  N="$(jp "$DIR/out/e2e/result.json" "d['steps']")"
  [ "$S" = "success" ] && ok "warmed run: success ($(jp "$DIR/out/e2e/result.json" "d.get('evidence','')"))" \
                       || bad "warmed run status=$S"
  [ "$N" -le 4 ] && ok "no re-login fight ($N steps)" || bad "warmed run needed $N steps"
else
  bad "warmed run produced no result.json"
fi
check_vanished e2e "warmed run"

# ================================================================== PHASE 3
info "PHASE 3 — handoff fallback still works (target with handoff_mode: eager)"
rm -rf "$DIR/out/e2e-human" "$DIR/profiles/e2e-human"
start_agent e2e-human testsite-human first-login
ok "container started (identity e2e-human)"
if wait_handoff e2e-human 240; then
  ok "human-only gate parked the session ($(jp "$DIR/out/e2e-human/handoff.json" "d['reason']"))"
  human_click e2e-human && ok "human solved it over noVNC" || bad "xdotool click failed"
else
  bad "no handoff on the eager target"
fi
if wait_result e2e-human 300; then
  S="$(jp "$DIR/out/e2e-human/result.json" "d['status']")"
  [ "$S" = "success" ] && ok "handoff resumed and the run finished" || bad "handoff run status=$S"
else
  bad "handoff run produced no result.json"
fi
check_vanished e2e-human "handoff run"

echo
echo "================ E2E SUMMARY: $PASS passed, $FAIL failed ================"
[ $FAIL -eq 0 ] || exit 1
