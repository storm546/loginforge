#!/usr/bin/env bash
# Boots the virtual display + VNC + noVNC, then hands control to the agent.
set -euo pipefail

DISP_NUM="${DISPLAY_NUM:-99}"
SCREEN="${SCREEN:-1440x900x24}"
VNC_PASS="${VNC_PASSWORD:-handoff}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

mkdir -p /out /profile /tmp/.X11-unix

export DISPLAY=":${DISP_NUM}"

log() { echo "[entrypoint] $*" | tee -a /out/boot.log; }

# --- virtual display ---------------------------------------------------------
Xvfb ":${DISP_NUM}" -screen 0 "${SCREEN}" -ac +extension RANDR -nolisten tcp \
    >/out/xvfb.log 2>&1 &
XVFB_PID=$!

for _ in $(seq 1 60); do
    xdpyinfo -display ":${DISP_NUM}" >/dev/null 2>&1 && break
    sleep 0.2
done
if ! xdpyinfo -display ":${DISP_NUM}" >/dev/null 2>&1; then
    log "FATAL: Xvfb did not come up"; cat /out/xvfb.log; exit 90
fi
log "Xvfb up on :${DISP_NUM} (${SCREEN}) pid=${XVFB_PID}"

# Minimal WM so Chromium popups/tooltips behave like a real desktop.
if command -v matchbox-window-manager >/dev/null 2>&1; then
    matchbox-window-manager -use_titlebar no >/out/wm.log 2>&1 &
fi

# --- VNC + noVNC -------------------------------------------------------------
x11vnc -display ":${DISP_NUM}" -rfbport "${VNC_PORT}" -passwd "${VNC_PASS}" \
    -forever -shared -noxdamage -repeat -quiet >/out/x11vnc.log 2>&1 &
log "x11vnc on ${VNC_PORT}"

NOVNC_WEB=""
for d in /usr/share/novnc /usr/share/webapps/novnc /opt/novnc; do
    [ -f "$d/vnc.html" ] && NOVNC_WEB="$d" && break
done

if [ -n "$NOVNC_WEB" ]; then
    websockify --web "$NOVNC_WEB" "${NOVNC_PORT}" "localhost:${VNC_PORT}" \
        >/out/websockify.log 2>&1 &
    log "noVNC on ${NOVNC_PORT} (web root ${NOVNC_WEB})"
else
    websockify "${NOVNC_PORT}" "localhost:${VNC_PORT}" \
        >/out/websockify.log 2>&1 &
    log "websockify on ${NOVNC_PORT} (no web root found - raw VNC-over-WS only)"
fi
echo "${VNC_PASS}" > /out/vnc_password

PUBLIC="${PUBLIC_URL:-http://127.0.0.1:${NOVNC_PORT}}"
HANDOFF_URL="${PUBLIC%/}/vnc.html?autoconnect=1&resize=scale&password=${VNC_PASS}"
echo "$HANDOFF_URL" > /out/novnc_url
log "HANDOFF URL: ${HANDOFF_URL}"

# --- agent -------------------------------------------------------------------
exec python -m agent.main "$@"
