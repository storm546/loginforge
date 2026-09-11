#!/usr/bin/env bash
# loginforge — spawn an Xvfb+VNC browser container, let an OpenRouter agent work,
# hand the live session to a human when needed, hand the session back, kill the box.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$DIR/lib.sh"

usage() {
  cat <<EOF
loginforge — containerised first-login + captcha automation  (engine: $ENGINE)

  forge build                      build the agent image (and the test fixture)
  forge login <target> [identity]  FIRST RUN: human-in-the-loop login, session saved
  forge run   <target> [identity]  SUBSEQUENT RUN: reuse saved session, 2captcha clears
  forge ps                         running forge containers
  forge url   <identity>           print the noVNC handoff URL
  forge logs  <identity> [n]       tail agent logs
  forge kill  <identity>           stop + remove (session in profiles/<id> is kept)
  forge shell <identity>           bash inside the running container
  forge test                       run the local E2E suite

Targets are files in targets/<name>.yaml. Identities map to profiles/<identity>,
which is the session that gets handed back to the host.
EOF
}

ensure_net() {
  $ENGINE network inspect "$NET" >/dev/null 2>&1 || $ENGINE network create "$NET" >/dev/null
}

need_env() {
  [ -f "$DIR/.env" ] || { c_err "missing $DIR/.env (copy .env.example)"; exit 1; }
  grep -q '^OPENROUTER_API_KEY=..*' "$DIR/.env" || { c_err "OPENROUTER_API_KEY empty in .env"; exit 1; }
}

cmd_build() {
  require_engine
  $ENGINE build -t "$IMAGE" "$DIR"
  $ENGINE build -t "$FIXTURE_IMAGE" -f "$DIR/testsite/Dockerfile" "$DIR/testsite"
  c_ok "built $IMAGE and $FIXTURE_IMAGE with $ENGINE"
}

print_handoff() {
  local ident="$1" url="$2"
  cat <<EOF

$(c_ok '────────────────────────────────────────────────────────')
  🔔 HANDOFF READY — a human is needed for identity $ident
  open this on your phone/laptop:
      $url
  solve the challenge in that window, then the agent resumes
  on its own (or: $ENGINE exec forge-$ident touch /out/resume)
$(c_ok '────────────────────────────────────────────────────────')

EOF
}

watch_container() {
  local ident="$1" name="forge-$ident" out="$DIR/out/$ident"
  local announced=0
  while [ "$($ENGINE inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)" = "true" ]; do
    if [ $announced -eq 0 ] && [ -f "$out/handoff.READY" ]; then
      print_handoff "$ident" "$(cat "$out/novnc_url" 2>/dev/null || echo "http://$HOST_IP:$NOVNC_PORT/vnc.html")"
      announced=1
    fi
    sleep 2
  done
  echo "[forge] container $name exited"
}

start_forge() {
  local mode="$1" target="$2" ident="$3"
  local name="forge-$ident" out="$DIR/out/$ident"
  require_engine
  mkdir -p "$DIR/profiles/$ident" "$out"
  rm -f "$out/handoff.READY" "$out/handoff.DONE" "$out/novnc_url" "$out/resume"
  ensure_net

  $ENGINE rm -f "$name" >/dev/null 2>&1 || true
  $ENGINE run -d --name "$name" --rm \
    --network "$NET" \
    "${SECOPT[@]}" \
    --shm-size=1g \
    -p "${NOVNC_PORT}:6080" -p "$((NOVNC_PORT + 1)):5900" \
    -v "$DIR/profiles/$ident":/profile \
    -v "$out":/out \
    --env-file "$DIR/.env" \
    -e PUBLIC_URL="http://$HOST_IP:$NOVNC_PORT" \
    -e FORGE_TARGET="$target" \
    -e FORGE_IDENTITY="$ident" \
    "$IMAGE" --mode "$mode" --target "$target" --identity "$ident" >/dev/null

  echo "[forge] started $name (engine=$ENGINE mode=$mode target=$target)"

  for _ in $(seq 1 40); do
    [ -f "$out/novnc_url" ] && break
    sleep 0.5
  done
  local url
  url="$(cat "$out/novnc_url" 2>/dev/null || echo "http://$HOST_IP:$NOVNC_PORT/vnc.html?autoconnect=1&resize=scale")"
  echo "[forge] live view: $url"

  $ENGINE logs -f "$name" 2>&1 &
  local logpid=$!
  watch_container "$ident"
  wait "$logpid" 2>/dev/null || true

  echo
  if [ -f "$out/result.json" ]; then
    c_ok "[forge] result:"
    cat "$out/result.json"
  else
    c_warn "[forge] no result.json — check $out/run.jsonl"
  fi
  c_ok "[forge] container removed; session kept in profiles/$ident"
}

cmd_ps()     { require_engine; $ENGINE ps --filter "name=forge-" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'; }
cmd_kill()   { local i="${1:-}"; [ -n "$i" ] || { c_err "usage: forge kill <identity>"; exit 1; }; $ENGINE rm -f "forge-$i" && c_ok "killed forge-$i (profile kept)"; }
cmd_logs()   { local i="${1:-}" n="${2:-80}"; [ -n "$i" ] || { c_err "usage: forge logs <identity> [lines]"; exit 1; }; $ENGINE logs --tail "$n" "forge-$i"; }
cmd_shell()  { local i="${1:-}"; [ -n "$i" ] || { c_err "usage: forge shell <identity>"; exit 1; }; $ENGINE exec -it "forge-$i" bash; }

cmd_url() {
  local ident="${1:-}" out="$DIR/out/$ident"
  if [ -f "$out/novnc_url" ]; then cat "$out/novnc_url"; echo
  else echo "http://$HOST_IP:$NOVNC_PORT/vnc.html?autoconnect=1&resize=scale"; fi
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  login) shift; [ $# -ge 1 ] || { usage; exit 1; }; need_env; start_forge first-login "$1" "${2:-default}" ;;
  run)   shift; [ $# -ge 1 ] || { usage; exit 1; }; need_env; start_forge challenge "$1" "${2:-default}" ;;
  ps)    cmd_ps ;;
  url)   shift; cmd_url "$@" ;;
  logs)  shift; cmd_logs "$@" ;;
  kill)  shift; cmd_kill "$@" ;;
  shell) shift; cmd_shell "$@" ;;
  test)  shift; bash "$DIR/tests/e2e.sh" "$@" ;;
  *)     usage ;;
esac
