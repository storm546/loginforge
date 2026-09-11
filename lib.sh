#!/usr/bin/env bash
# Shared config + container-engine detection for loginforge.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${FORGE_IMAGE:-loginforge:latest}"
FIXTURE_IMAGE="${FIXTURE_IMAGE:-loginforge-testsite:latest}"
NET="${FORGE_NET:-forge-net}"
NOVNC_PORT="${FORGE_NOVNC_PORT:-6080}"
HOST_IP="${FORGE_HOST_IP:-$(hostname -I | awk '{print $1}')}"

# Both docker and podman are accepted. On this LXC (AppArmor confined),
# docker cannot build images and podman can, so the probe decides.
detect_engine() {
  if [ -n "${FORGE_ENGINE:-}" ]; then echo "$FORGE_ENGINE"; return; fi
  local marker="$DIR/.engine"
  if [ -s "$marker" ]; then cat "$marker"; return; fi

  local probe=/tmp/forge-engine-probe
  mkdir -p "$probe"
  printf 'FROM alpine:3.20\nRUN true\n' > "$probe/Dockerfile"

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 \
     && docker build -q -t forge-probe "$probe" >/dev/null 2>&1; then
    echo docker > "$marker"; echo docker; return
  fi
  if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1 \
     && podman build -q -t forge-probe "$probe" >/dev/null 2>&1; then
    echo podman > "$marker"; echo podman; return
  fi
  echo "none" > "$marker"; echo "none"
}

ENGINE="$(detect_engine)"
# AppArmor-confined hosts need this; it is a no-op elsewhere.
SECOPT=(--security-opt apparmor=unconfined)

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

require_engine() {
  [ "$ENGINE" != "none" ] || {
    c_err "no working container engine (docker and podman both failed)"; exit 1; }
}
